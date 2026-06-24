#!/usr/bin/env python3
"""
TURBO Send-to-MASSO Manager - first run Tkinter proof of concept (v1.2)

What this V1.2 does:
- Tkinter GUI with saved MASSO IP profiles
- Connect/disconnect to MASSO over UDP
- Live status display from 270-byte MASSO status packets
- Decodes stopped/running/progress/current file/line/tool prompt/breakaway
- Disables upload unless the machine is safely stopped and fault-free
- Uploads one selected .nc file using the MASSO Link-style UDP upload sequence
- Logs upload ACK/failure/retry activity in the GUI

Notes:
- This is a first-run shop test build, not polished production software.
- Directory browsing on the MASSO side is not implemented yet.
- Remote folder support is based on Wireshark captures. Keep target folder/name short for now.
"""

from __future__ import annotations

import json
import os
import queue
import socket
import struct
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any, Callable

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

APP_NAME = "TURBO Send-to-MASSO Manager v1.2"
CONFIG_FILE = Path.home() / ".turbo_send_to_masso.json"
CONTROLLER_PORT = 65535
LOCAL_PORT_START = 11000
LOCAL_PORT_END = 11050
CHUNK_SIZE = 1422
STOPPED_STABLE_SECONDS = 1.5


# -----------------------------
# Protocol helpers
# -----------------------------

def crc16_ccitt_le(data: bytes) -> bytes:
    """Calculate CRC16-CCITT, returned as little-endian bytes."""
    crc = 0x0000
    poly = 0x1021
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = (crc << 1) ^ poly
            else:
                crc <<= 1
            crc &= 0xFFFF
    return crc.to_bytes(2, "little")


def with_crc(payload: bytes) -> bytes:
    return crc16_ccitt_le(payload) + payload


def normalize_masso_folder(folder: str) -> str:
    r"""MASSO Link captures used backslash-delimited folders like \Folder\."""
    folder = (folder or "\\").strip().replace("/", "\\")
    if not folder:
        folder = "\\"
    if not folder.startswith("\\"):
        folder = "\\" + folder
    if not folder.endswith("\\"):
        folder += "\\"
    return folder


@dataclass
class MassoStatus:
    connected: bool = False
    progress: int = 0
    run_flag: int = 0
    fault_code: int = 0xFF
    job_count: int = 0
    prompt_state: int = 0x01
    line_number: int = 0
    filename: str = ""
    raw_len: int = 0
    last_packet_time: float = 0.0
    stopped_since: Optional[float] = None
    feed_hold_active: bool = False

    @property
    def running(self) -> bool:
        return self.run_flag == 0x02

    @property
    def breakaway(self) -> bool:
        return self.fault_code == 0x15

    @property
    def faulted(self) -> bool:
        return self.fault_code != 0xFF

    @property
    def prompt_waiting(self) -> bool:
        return self.prompt_state == 0x00

    def state_text(self) -> str:
        if self.breakaway:
            return "Torch Breakaway"
        if self.faulted:
            return f"Fault / Alarm 0x{self.fault_code:02X}"
        if self.prompt_waiting:
            return "Waiting for User / Tool Change"
        if self.feed_hold_active:
            return "Feed Hold"
        if self.running:
            return f"Machine Running - {self.progress}%"
        return "Machine Stopped"

    def upload_allowed(self) -> tuple[bool, str]:
        if not self.connected:
            return False, "Not connected"
        if self.running:
            return False, "Machine running"
        if self.faulted:
            return False, f"Fault/alarm 0x{self.fault_code:02X}"
        if self.prompt_waiting:
            return False, "Waiting for user/tool change"
        if self.stopped_since is None:
            return False, "Waiting for stopped status"
        stable_for = time.monotonic() - self.stopped_since
        if stable_for < STOPPED_STABLE_SECONDS:
            return False, "Stopped debounce"
        return True, "Ready"


class MassoClient:
    """Small UDP protocol client. All GUI updates are sent through event_queue."""

    def __init__(self, event_queue: queue.Queue):
        self.event_queue = event_queue
        self.host: Optional[str] = None
        # MASSO Link appears to use two UDP sockets:
        #   - receive/status socket bound to UDP 11000
        #   - send socket using an ephemeral source port
        # V1.1 used one bound socket for both directions. That worked for status,
        # but file chunks timed out on the Touch. V1.2 separates RX and TX.
        self.socket: Optional[socket.socket] = None       # RX socket, bound to 11000-11050
        self.tx_socket: Optional[socket.socket] = None    # TX socket, ephemeral source port
        self.local_port: Optional[int] = None
        self.tx_local_port: Optional[int] = None
        self.connected = False
        self.listening = False
        self.upload_in_progress = False

        self.listen_thread: Optional[threading.Thread] = None
        self.keepalive_thread: Optional[threading.Thread] = None
        self.status = MassoStatus()
        self.last_status_raw: Optional[bytes] = None

        self._ack_event = threading.Event()
        self._last_ack: Optional[bytes] = None
        self._last_line_value: Optional[int] = None
        self._last_line_change_time: Optional[float] = None

        self._lock = threading.Lock()

    def post(self, event_type: str, payload: Any = None) -> None:
        self.event_queue.put((event_type, payload))

    def log(self, msg: str) -> None:
        self.post("log", msg)

    def start(self, host: str) -> bool:
        self.stop()
        self.host = host.strip()
        if not self.host:
            self.log("No MASSO IP specified")
            return False

        for port in range(LOCAL_PORT_START, LOCAL_PORT_END + 1):
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind(("", port))
                s.settimeout(0.25)
                self.socket = s
                self.local_port = port
                break
            except OSError:
                continue

        if not self.socket:
            self.log(f"Could not bind UDP port {LOCAL_PORT_START}-{LOCAL_PORT_END}")
            return False

        try:
            self.tx_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.tx_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            # Do not bind TX. Let the OS choose an ephemeral source port, matching MASSO Link behavior.
            self.tx_socket.connect((self.host, CONTROLLER_PORT))
            self.tx_local_port = self.tx_socket.getsockname()[1]
        except Exception as exc:
            self.log(f"Could not create TX socket: {exc}")
            try:
                self.socket.close()
            except OSError:
                pass
            self.socket = None
            return False

        self.listening = True
        self.connected = True
        self.status = MassoStatus(connected=True)
        self.post("status", self.status)

        self.listen_thread = threading.Thread(target=self._listen_loop, daemon=True)
        self.keepalive_thread = threading.Thread(target=self._keepalive_loop, daemon=True)
        self.listen_thread.start()
        self.keepalive_thread.start()

        self.log(
            f"RX UDP {self.local_port}; TX UDP {self.tx_local_port}; "
            f"connecting to MASSO {self.host}:{CONTROLLER_PORT}"
        )
        self._send_handshake()
        return True

    def stop(self) -> None:
        self.connected = False
        self.listening = False
        if self.socket:
            try:
                self.socket.close()
            except OSError:
                pass
        if self.tx_socket:
            try:
                self.tx_socket.close()
            except OSError:
                pass
        self.socket = None
        self.tx_socket = None
        self.tx_local_port = None
        self.status.connected = False
        self.post("status", self.status)

    def _send(self, packet: bytes) -> None:
        if not self.tx_socket or not self.host:
            raise RuntimeError("TX socket not started")
        # TX socket is connected to the MASSO controller and uses an ephemeral source port.
        self.tx_socket.send(packet)

    def _time_fields(self) -> tuple[int, int, int, int, int, int]:
        """Return MASSO Link-style local time fields.

        Captures show packet bytes ordered as:
          hour, minute, second, day, month, year_offset

        Example from Touch capture on 2026-06-04 around 11:01:21:
          keepalive payload: 03 00 01 0b 01 15 04 06
          config payload:    03 00 03 0b 01 15 04 06 1a 00 00 00
        """
        now = datetime.now()
        return now.hour, now.minute, now.second, now.day, now.month, now.year - 2000

    def _build_discovery_payload(self) -> bytes:
        # MASSO Link Touch capture used final byte = current month.
        _hour, _minute, _second, _day, month, _year = self._time_fields()
        return bytes([0x03, 0x00, 0x02, 0xF8, 0x2A, 0x00, 0x00, month & 0xFF])

    def _build_config_payload(self) -> bytes:
        hour, minute, second, day, month, year = self._time_fields()
        payload = bytearray([0x03, 0x00, 0x03, hour, minute, second, day, month])
        payload.extend(year.to_bytes(4, "little", signed=False))
        return bytes(payload)

    def _build_keepalive_payload(self) -> bytes:
        hour, minute, second, day, month, _year = self._time_fields()
        return bytes([0x03, 0x00, 0x01, hour, minute, second, day, month])

    def _send_handshake(self) -> None:
        try:
            self._send(with_crc(self._build_discovery_payload()))
            time.sleep(0.15)
            self._send(with_crc(self._build_config_payload()))
            self.log("Handshake sent with local clock time")
        except Exception as exc:
            self.log(f"Handshake error: {exc}")

    def _keepalive_loop(self) -> None:
        while self.connected:
            try:
                self._send(with_crc(self._build_keepalive_payload()))
            except Exception as exc:
                self.log(f"Keepalive error: {exc}")
                break
            time.sleep(1.0)

    def _listen_loop(self) -> None:
        while self.listening and self.socket:
            try:
                data, _addr = self.socket.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            except Exception as exc:
                self.log(f"Receive error: {exc}")
                continue

            if len(data) == 270:
                self._handle_status(data)
            elif len(data) == 10:
                self._handle_small_packet(data)
            elif len(data) == 38 and data[4] == 0x08:
                # Tool packets can be decoded later.
                pass

    def _handle_small_packet(self, data: bytes) -> None:
        pkt_type = data[4]
        if pkt_type in (0x0A, 0x0B):
            self._last_ack = data
            self._ack_event.set()
            return
        if pkt_type == 0x03:
            serial = int.from_bytes(data[5:7], "little")
            self.log(f"Configuration response: controller serial {serial}")

    def _handle_status(self, data: bytes) -> None:
        now = time.monotonic()
        old = self.status
        st = MassoStatus(connected=True)
        st.raw_len = len(data)
        st.last_packet_time = now
        st.progress = data[5]
        st.run_flag = data[6]
        st.fault_code = data[7]
        st.job_count = int.from_bytes(data[8:12], "little")
        st.prompt_state = data[12]
        st.line_number = data[13]
        filename_bytes = data[17:80]
        st.filename = filename_bytes.split(b"\x00", 1)[0].decode("ascii", errors="ignore")

        # Stopped debounce tracking
        if st.run_flag == 0x00 and st.fault_code == 0xFF:
            if old.stopped_since is not None and old.run_flag == 0x00 and old.fault_code == 0xFF:
                st.stopped_since = old.stopped_since
            else:
                st.stopped_since = now
        else:
            st.stopped_since = None

        # Conservative feed-hold inference: running + line number not changing for >1.5s.
        line_changed = self._last_line_value is None or st.line_number != self._last_line_value
        if st.running:
            if line_changed:
                self._last_line_change_time = now
                self._last_line_value = st.line_number
                st.feed_hold_active = False
            else:
                st.feed_hold_active = (
                    self._last_line_change_time is not None
                    and st.line_number > 0
                    and now - self._last_line_change_time >= 1.5
                )
        else:
            self._last_line_change_time = now
            self._last_line_value = st.line_number
            st.feed_hold_active = False

        self.status = st
        self.last_status_raw = data
        self.post("status", st)

    # -----------------------------
    # Upload
    # -----------------------------

    def upload_file_async(self, local_path: str, remote_folder: str) -> None:
        if self.upload_in_progress:
            self.log("Upload already in progress")
            return
        t = threading.Thread(target=self._upload_file_worker, args=(local_path, remote_folder), daemon=True)
        t.start()

    def _build_start_upload_packet(self, filesize: int, remote_folder: str, filename: str) -> bytes:
        """Build MASSO Link-style 50-byte start-upload packet.

        Observed capture layout, including checksum:
          [0:2]   CRC16 LE
          [2:4]   03 00
          [4]     0A
          [5:9]   file size little-endian
          [9:11]  00 00
          [11]    folder length, not including NUL
          [12:]   folder ASCII + NUL, filename ASCII + NUL, then pad/trailing bytes

        The last few bytes appear not to be critical in the capture. V1 pads with zeros.
        Total packet length is kept at 50 bytes to match MASSO Link captures.
        """
        folder = normalize_masso_folder(remote_folder)
        folder_b = folder.encode("ascii", errors="replace")
        name_b = filename.encode("ascii", errors="replace")

        payload = bytearray()
        payload.extend(b"\x03\x00")
        payload.append(0x0A)
        payload.extend(filesize.to_bytes(4, "little"))
        payload.extend(b"\x00\x00")
        payload.append(len(folder_b))  # capture used 0x11 for 17-byte folder, not including NUL
        payload.extend(folder_b)
        payload.append(0x00)
        payload.extend(name_b)
        payload.append(0x00)

        # MASSO Link fills the last three unused bytes with ASCII digits/spaces in captures
        # rather than zeros. Their exact meaning is not fully decoded yet; use a stable
        # non-zero trailer to match the official packet shape more closely.
        if len(payload) <= 45:
            payload.extend(b"826"[: 48 - len(payload)])

        # Payload must be 48 bytes so checksum + payload = 50 bytes.
        if len(payload) > 48:
            raise ValueError(
                f"Remote folder/name too long for V1 packet ({len(payload)} payload bytes, max 48). "
                "Use a shorter target folder and filename for now."
            )
        while len(payload) < 48:
            payload.append(0x00)
        return with_crc(bytes(payload))

    def _build_data_packet(self, chunk_index: int, chunk: bytes, *, pad_to_full_chunk: bool = False) -> bytes:
        """Build a file data packet.

        MASSO Link uses 1422-byte chunks. In captures, normal chunks are always
        1438 bytes on the wire: 2 CRC + 11-byte header + 1422 data + 3 pad.

        We have not yet captured an official one-chunk/small-file upload. V1.2
        sent a short final packet and the Touch accepted the start packet but
        never ACKed chunk 0. V1.3 intentionally pads small/final chunks to the
        normal 1422-byte wire size and sets the length field to 1422. The file
        size in the start-upload packet still tells MASSO how many bytes are real.
        """
        wire_chunk = chunk
        length_field = len(chunk)
        if pad_to_full_chunk and len(chunk) < CHUNK_SIZE:
            wire_chunk = chunk + (b"\x00" * (CHUNK_SIZE - len(chunk)))
            length_field = CHUNK_SIZE

        payload = bytearray()
        payload.extend(b"\x03\x00")
        payload.append(0x0B)
        payload.extend(chunk_index.to_bytes(4, "little"))
        payload.extend(length_field.to_bytes(4, "little"))
        payload.extend(wire_chunk)
        payload.extend(b"\x00\x00\x00")
        return with_crc(bytes(payload))

    def _wait_for_ack(self, expected_type: int, timeout: float = 2.0) -> Optional[bytes]:
        self._ack_event.clear()
        self._last_ack = None
        if self._ack_event.wait(timeout=timeout):
            ack = self._last_ack
            if ack and len(ack) == 10 and ack[4] == expected_type:
                return ack
        return None

    def _send_with_ack(self, packet: bytes, expected_type: int, timeout: float = 2.0) -> Optional[bytes]:
        self._ack_event.clear()
        self._last_ack = None
        self._send(packet)
        if self._ack_event.wait(timeout=timeout):
            ack = self._last_ack
            if ack and len(ack) == 10 and ack[4] == expected_type:
                return ack
        return None

    def _upload_file_worker(self, local_path: str, remote_folder: str) -> None:
        self.upload_in_progress = True
        self.post("upload_state", True)
        try:
            allowed, reason = self.status.upload_allowed()
            if not allowed:
                self.log(f"Upload blocked: {reason}")
                return

            path = Path(local_path)
            if not path.exists() or not path.is_file():
                self.log(f"File not found: {local_path}")
                return
            if path.stat().st_size <= 0:
                self.log("Upload blocked: file is empty")
                return

            filename = path.name
            filesize = path.stat().st_size
            remote_folder = normalize_masso_folder(remote_folder)

            self.log(f"Starting upload: {filename} ({filesize} bytes) -> {remote_folder}")
            start_packet = self._build_start_upload_packet(filesize, remote_folder, filename)

            start_ack = None
            for attempt in range(1, 4):
                start_ack = self._send_with_ack(start_packet, 0x0A, timeout=2.0)
                if start_ack is None:
                    self.log(f"Start upload attempt {attempt}: no ACK")
                    continue
                code = start_ack[5:7]
                if code == b"\x00\x00":
                    self.log(f"Start upload accepted; ACK={start_ack.hex(' ')}")
                    break
                self.log(f"Start upload rejected: code {code.hex(' ')} ACK={start_ack.hex(' ')}")
                start_ack = None
                time.sleep(0.4)

            if start_ack is None:
                self.log("Upload failed: MASSO did not accept start upload")
                return

            total_chunks = (filesize + CHUNK_SIZE - 1) // CHUNK_SIZE
            with path.open("rb") as f:
                for chunk_index in range(total_chunks):
                    chunk = f.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    # V1.3 test behavior: pad the final/small chunk to the normal 1422-byte
                    # MASSO Link wire size. This is especially important for one-chunk files.
                    pad_this_chunk = len(chunk) < CHUNK_SIZE
                    packet = self._build_data_packet(chunk_index, chunk, pad_to_full_chunk=pad_this_chunk)
                    expected_next = chunk_index + 1
                    if chunk_index == 0:
                        self.log(
                            f"Sending first data packet from TX UDP {self.tx_local_port}: "
                            f"len={len(packet)} real_chunk_len={len(chunk)} "
                            f"wire_len={'1422 padded' if pad_this_chunk else len(chunk)} "
                            f"head={packet[:16].hex(' ')}"
                        )

                    ok = False
                    for attempt in range(1, 5):
                        ack = self._send_with_ack(packet, 0x0B, timeout=2.0)
                        if ack is None:
                            self.log(f"Chunk {expected_next}/{total_chunks}: no ACK, retry {attempt}")
                            continue

                        # Capture shows data ACK bytes 5:7 as big-endian next expected chunk.
                        ack_next = int.from_bytes(ack[5:7], "big")
                        if ack_next == expected_next:
                            if expected_next == 1:
                                self.log(f"First chunk ACK received: {ack.hex(' ')}")
                            ok = True
                            break
                        self.log(
                            f"Chunk {expected_next}/{total_chunks}: unexpected ACK next={ack_next}, retry {attempt}"
                        )
                        time.sleep(0.2)

                    if not ok:
                        self.log(f"Upload failed at chunk {expected_next}/{total_chunks}")
                        return

                    progress = int(expected_next * 100 / total_chunks)
                    self.post("upload_progress", progress)
                    if expected_next == 1 or expected_next == total_chunks or expected_next % 5 == 0:
                        self.log(f"Sent chunk {expected_next}/{total_chunks} ({progress}%)")

            self.log(f"Upload complete: {filename}")
            self.post("upload_progress", 100)
        except Exception as exc:
            self.log(f"Upload error: {exc}")
        finally:
            self.upload_in_progress = False
            self.post("upload_state", False)


# -----------------------------
# Config
# -----------------------------

def load_config() -> Dict[str, Any]:
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text())
        except Exception:
            pass
    return {
        "profiles": [
            {"name": "MASSO", "ip": "192.168.137.245"},
        ],
        "last_profile": "MASSO",
        "last_folder": "\\",
        "last_local_dir": str(Path.home()),
    }


def save_config(cfg: Dict[str, Any]) -> None:
    try:
        CONFIG_FILE.write_text(json.dumps(cfg, indent=2))
    except Exception:
        pass


# -----------------------------
# Tkinter GUI
# -----------------------------

class TurboSendGui:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(APP_NAME)
        self.root.geometry("900x650")
        self.root.minsize(780, 560)

        self.events: queue.Queue = queue.Queue()
        self.client = MassoClient(self.events)
        self.config = load_config()
        self.selected_file: Optional[str] = None
        self.upload_busy = False
        self.current_status = MassoStatus()

        self._build_ui()
        self._load_profiles_into_combo()
        self._poll_events()
        self._refresh_upload_button()

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=10)
        outer.pack(fill="both", expand=True)

        # Connection frame
        conn = ttk.LabelFrame(outer, text="Connection", padding=10)
        conn.pack(fill="x")

        ttk.Label(conn, text="Connect to:").grid(row=0, column=0, sticky="w")
        self.profile_var = tk.StringVar()
        self.profile_combo = ttk.Combobox(conn, textvariable=self.profile_var, width=26, state="readonly")
        self.profile_combo.grid(row=0, column=1, padx=5, sticky="w")
        self.profile_combo.bind("<<ComboboxSelected>>", self.on_profile_selected)

        ttk.Label(conn, text="IP:").grid(row=0, column=2, sticky="e")
        self.ip_var = tk.StringVar()
        self.ip_entry = ttk.Entry(conn, textvariable=self.ip_var, width=18)
        self.ip_entry.grid(row=0, column=3, padx=5, sticky="w")

        self.connect_btn = ttk.Button(conn, text="Connect", command=self.connect)
        self.connect_btn.grid(row=0, column=4, padx=5)
        self.disconnect_btn = ttk.Button(conn, text="Disconnect", command=self.disconnect)
        self.disconnect_btn.grid(row=0, column=5, padx=5)
        self.save_profile_btn = ttk.Button(conn, text="Save Profile", command=self.save_current_profile)
        self.save_profile_btn.grid(row=0, column=6, padx=5)

        conn.columnconfigure(7, weight=1)

        # Status frame
        status = ttk.LabelFrame(outer, text="MASSO Status", padding=10)
        status.pack(fill="x", pady=(10, 0))
        status.columnconfigure(1, weight=1)

        self.state_var = tk.StringVar(value="Not connected")
        self.upload_allowed_var = tk.StringVar(value="Upload: Not connected")
        self.progress_var = tk.StringVar(value="0%")
        self.file_var = tk.StringVar(value="")
        self.line_var = tk.StringVar(value="0")
        self.job_var = tk.StringVar(value="0")
        self.raw_var = tk.StringVar(value="")

        rows = [
            ("Machine:", self.state_var),
            ("Upload:", self.upload_allowed_var),
            ("Progress:", self.progress_var),
            ("Current/Last File:", self.file_var),
            ("Line:", self.line_var),
            ("Job Count:", self.job_var),
            ("Raw:", self.raw_var),
        ]
        for r, (label, var) in enumerate(rows):
            ttk.Label(status, text=label).grid(row=r, column=0, sticky="w", pady=2)
            ttk.Label(status, textvariable=var).grid(row=r, column=1, sticky="w", pady=2)

        self.progress_bar = ttk.Progressbar(status, maximum=100, mode="determinate")
        self.progress_bar.grid(row=2, column=2, sticky="ew", padx=10)
        status.columnconfigure(2, weight=1)

        # File frame
        files = ttk.LabelFrame(outer, text="Send File", padding=10)
        files.pack(fill="x", pady=(10, 0))
        files.columnconfigure(1, weight=1)

        ttk.Label(files, text="Local file:").grid(row=0, column=0, sticky="w")
        self.local_file_var = tk.StringVar(value="")
        ttk.Entry(files, textvariable=self.local_file_var).grid(row=0, column=1, sticky="ew", padx=5)
        ttk.Button(files, text="Browse...", command=self.browse_file).grid(row=0, column=2, padx=5)

        ttk.Label(files, text="Target MASSO folder:").grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.remote_folder_var = tk.StringVar(value=self.config.get("last_folder", "\\"))
        ttk.Entry(files, textvariable=self.remote_folder_var).grid(row=1, column=1, sticky="ew", padx=5, pady=(8, 0))
        ttk.Label(files, text="example: \\5178-24_44-IDUC\\").grid(row=1, column=2, sticky="w", pady=(8, 0))

        self.send_btn = ttk.Button(files, text="Send Selected File", command=self.send_selected_file)
        self.send_btn.grid(row=2, column=0, pady=(10, 0), sticky="w")
        self.upload_progress = ttk.Progressbar(files, maximum=100, mode="determinate")
        self.upload_progress.grid(row=2, column=1, columnspan=2, sticky="ew", padx=5, pady=(10, 0))

        # Log frame
        logf = ttk.LabelFrame(outer, text="Log", padding=10)
        logf.pack(fill="both", expand=True, pady=(10, 0))
        logf.rowconfigure(0, weight=1)
        logf.columnconfigure(0, weight=1)

        self.log_text = tk.Text(logf, height=12, wrap="word")
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(logf, command=self.log_text.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=scroll.set)

        self.log("V1.3 build loaded. Connect to MASSO before sending files.")

    def _load_profiles_into_combo(self) -> None:
        profiles = self.config.get("profiles", [])
        names = [p.get("name", "MASSO") for p in profiles]
        self.profile_combo["values"] = names
        last = self.config.get("last_profile") or (names[0] if names else "")
        if last in names:
            self.profile_var.set(last)
        elif names:
            self.profile_var.set(names[0])
        self.on_profile_selected()

    def current_profile(self) -> Optional[Dict[str, str]]:
        name = self.profile_var.get()
        for p in self.config.get("profiles", []):
            if p.get("name") == name:
                return p
        return None

    def on_profile_selected(self, event=None) -> None:
        p = self.current_profile()
        if p:
            self.ip_var.set(p.get("ip", ""))

    def save_current_profile(self) -> None:
        name = self.profile_var.get().strip() or "MASSO"
        ip = self.ip_var.get().strip()
        if not ip:
            messagebox.showwarning(APP_NAME, "Enter an IP address first.")
            return
        profiles = self.config.setdefault("profiles", [])
        for p in profiles:
            if p.get("name") == name:
                p["ip"] = ip
                break
        else:
            profiles.append({"name": name, "ip": ip})
        self.config["last_profile"] = name
        save_config(self.config)
        self._load_profiles_into_combo()
        self.log(f"Saved profile: {name} -> {ip}")

    def connect(self) -> None:
        ip = self.ip_var.get().strip()
        if not ip:
            messagebox.showwarning(APP_NAME, "Enter a MASSO IP address.")
            return
        self.config["last_profile"] = self.profile_var.get().strip()
        save_config(self.config)
        self.client.start(ip)

    def disconnect(self) -> None:
        self.client.stop()
        self.log("Disconnected")

    def browse_file(self) -> None:
        initial_dir = self.config.get("last_local_dir", str(Path.home()))
        path = filedialog.askopenfilename(
            title="Select G-code file",
            initialdir=initial_dir,
            filetypes=[("G-code / NC files", "*.nc *.tap *.txt *.gcode"), ("All files", "*.*")],
        )
        if path:
            self.selected_file = path
            self.local_file_var.set(path)
            self.config["last_local_dir"] = str(Path(path).parent)
            save_config(self.config)
            self._refresh_upload_button()

    def send_selected_file(self) -> None:
        path = self.local_file_var.get().strip()
        folder = self.remote_folder_var.get().strip() or "\\"
        if not path:
            messagebox.showwarning(APP_NAME, "Select a local file first.")
            return
        allowed, reason = self.current_status.upload_allowed()
        if not allowed:
            messagebox.showwarning(APP_NAME, f"Upload not allowed: {reason}")
            return
        self.config["last_folder"] = folder
        save_config(self.config)
        self.upload_progress["value"] = 0
        self.client.upload_file_async(path, folder)

    def _poll_events(self) -> None:
        try:
            while True:
                event_type, payload = self.events.get_nowait()
                if event_type == "log":
                    self.log(str(payload))
                elif event_type == "status":
                    self.update_status(payload)
                elif event_type == "upload_state":
                    self.upload_busy = bool(payload)
                    self._refresh_upload_button()
                elif event_type == "upload_progress":
                    self.upload_progress["value"] = int(payload)
        except queue.Empty:
            pass
        self.root.after(100, self._poll_events)

    def update_status(self, st: MassoStatus) -> None:
        self.current_status = st
        self.state_var.set(st.state_text() if st.connected else "Not connected")
        allowed, reason = st.upload_allowed()
        if self.upload_busy:
            self.upload_allowed_var.set("Upload in progress")
        else:
            self.upload_allowed_var.set(f"{'Allowed' if allowed else 'Disabled'} - {reason}")
        self.progress_var.set(f"{st.progress}%")
        self.progress_bar["value"] = st.progress
        self.file_var.set(st.filename or "(none)")
        self.line_var.set(str(st.line_number))
        self.job_var.set(str(st.job_count))
        self.raw_var.set(
            f"run=0x{st.run_flag:02X} fault=0x{st.fault_code:02X} prompt=0x{st.prompt_state:02X} len={st.raw_len}"
            if st.connected else ""
        )
        self._refresh_upload_button()

    def _refresh_upload_button(self) -> None:
        allowed, _reason = self.current_status.upload_allowed()
        file_ok = bool(self.local_file_var.get().strip())
        if allowed and file_ok and not self.upload_busy:
            self.send_btn.configure(state="normal")
        else:
            self.send_btn.configure(state="disabled")

    def log(self, msg: str) -> None:
        timestamp = time.strftime("%H:%M:%S")
        self.log_text.insert("end", f"[{timestamp}] {msg}\n")
        self.log_text.see("end")

    def on_close(self) -> None:
        self.client.stop()
        self.root.destroy()


def main() -> int:
    root = tk.Tk()
    try:
        # Use native-ish themed controls where available.
        style = ttk.Style(root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
    except Exception:
        pass
    TurboSendGui(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
