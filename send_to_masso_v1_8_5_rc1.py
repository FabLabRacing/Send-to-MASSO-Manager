#!/usr/bin/env python3
"""
Send-to-MASSO Manager - HMI-style Tkinter uploader (v1.8.5 Release Candidate)

What this V1.8 does:
- Tkinter GUI with named/saved MASSO IP profiles
- Connect/disconnect to MASSO over UDP
- Live status display from 270-byte MASSO status packets
- Decodes stopped/running/progress/current file/line/tool prompt/breakaway
- Disables upload unless the machine is safely stopped and fault-free
- Queues one or more selected MASSO G-code files and uploads them one at a time
- Logs upload ACK/failure/retry activity in the GUI
- Generates MASSO QR-code PNG files for queued targets

Notes:
- This is the first release candidate build, may still need a bit of polish.
- Tools Data downloads are not implemented yet.
- Directory browsing on the MASSO side is a Wish-List Item.
- QR-code functionality has not been fully tested.
- Need a more complete list of error codes.
"""

from __future__ import annotations

import json
import os
import queue
import sys
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

APP_NAME = "Send-to-MASSO Manager v1.8.5 RC"
# Keep the shop utility self-contained: profiles/config live beside the program.
if getattr(sys, "frozen", False):
    APP_DIR = Path(sys.executable).resolve().parent
else:
    APP_DIR = Path(__file__).resolve().parent
CONFIG_FILE = APP_DIR / "send_to_masso.json"
CONTROLLER_PORT = 65535
LOCAL_PORT_START = 11000
LOCAL_PORT_END = 11050
CHUNK_SIZE = 1422
STOPPED_STABLE_SECONDS = 1.5
SUPPORTED_EXTENSIONS = {".nc", ".cnc", ".tap", ".eia", ".txt"}
INVALID_TARGET_CHARS = set(':*?"<>|')



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


def build_remote_target_preview(local_path: str, remote_folder: str) -> str:
    """Return the exact MASSO-style target string shown to the operator."""
    filename = Path(local_path).name if local_path else ""
    folder = normalize_masso_folder(remote_folder)
    return folder + filename if filename else folder


def build_masso_qr_payload(local_path: str, remote_folder: str) -> str:
    """Build MASSO QR payload for loading a G-code file.

    MASSO documentation describes payloads in the form:
        ^CSLG<path-to-gcode-file>^CE

    The examples do not show a leading root backslash, so strip leading root backslashes from
    the displayed MASSO target for QR generation. Internal folder separators are
    left as MASSO-style backslashes.
    """
    target = build_remote_target_preview(local_path, remote_folder)
    qr_target = target.lstrip("\\")
    return f"^CSLG{qr_target}^CE"


def default_qr_filename(local_path: str) -> str:
    path = Path(local_path)
    return f"{path.stem}_MASSO_QR.png"


def validate_masso_name_parts(local_path: str, remote_folder: str) -> tuple[bool, list[str], list[str], str]:
    """Validate the target path/name using the shop-tested practical rule set.

    Forward slashes in folder input are normalized to backslashes before
    validation. Backslashes are allowed as folder separators.
    Returns: ok, errors, warnings, preview
    """
    errors: list[str] = []
    warnings: list[str] = []
    folder = normalize_masso_folder(remote_folder)
    filename = Path(local_path).name if local_path else ""
    preview = folder + filename if filename else folder

    names_only = preview.replace("\\", "")
    for ch in names_only:
        if ord(ch) < 32:
            errors.append("Control characters are not allowed in MASSO file/folder names.")
            break
    bad = sorted({ch for ch in names_only if ch in INVALID_TARGET_CHARS})
    if bad:
        errors.append("Invalid MASSO character(s): " + " ".join(bad))
    if any(ord(ch) > 127 for ch in names_only):
        errors.append("MASSO target contains non-ASCII characters. Use plain ASCII names, for example cafe.tap instead of café.tap.")

    if filename:
        ext = Path(filename).suffix.lower()
        if ext not in SUPPORTED_EXTENSIONS:
            allowed = ", ".join(sorted(SUPPORTED_EXTENSIONS))
            warnings.append(f"MASSO normally expects one of: {allowed}")

    return not errors, errors, warnings, preview


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
        self.tx_listen_thread: Optional[threading.Thread] = None
        self._seen_first_status = False
        self.status = MassoStatus()
        self.last_status_raw: Optional[bytes] = None

        self._ack_event = threading.Event()
        self._last_ack: Optional[bytes] = None
        self._last_line_value: Optional[int] = None
        self._last_line_change_time: Optional[float] = None

        self._lock = threading.Lock()
        self._tx_lock = threading.Lock()
        self._tx_generation = 0

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
            self.tx_socket.settimeout(0.25)
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
        self._seen_first_status = False
        self.status = MassoStatus(connected=True)
        self.post("status", self.status)

        self._tx_generation += 1
        tx_generation = self._tx_generation

        self.listen_thread = threading.Thread(target=self._listen_loop, daemon=True)
        self.tx_listen_thread = threading.Thread(
            target=self._tx_listen_loop,
            args=(self.tx_socket, tx_generation),
            daemon=True,
        )
        self.keepalive_thread = threading.Thread(target=self._keepalive_loop, daemon=True)
        self.listen_thread.start()
        self.tx_listen_thread.start()
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
        self._tx_generation += 1
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
        with self._tx_lock:
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


    def _tx_listen_loop(self, sock: Optional[socket.socket] = None, generation: Optional[int] = None) -> None:
        """Listen for replies sent back to the TX ephemeral port.

        MASSO Link captures show some upload ACKs can be sent to the ephemeral
        sender port as well as UDP 11000. Listening here makes us tolerant of
        either behavior.

        A generation value is used because V1.7.10 can refresh the TX socket
        before each upload. This prevents old listener threads from racing the
        new listener on the replacement socket.
        """
        sock = sock or self.tx_socket
        generation = self._tx_generation if generation is None else generation

        while self.listening and sock and generation == self._tx_generation:
            try:
                data = sock.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            except Exception as exc:
                self.log(f"TX receive error: {exc}")
                continue

            if len(data) == 10:
                self._handle_small_packet(data, source="TX")
            elif len(data) == 270:
                self._handle_status(data)

    def _handle_small_packet(self, data: bytes, source: str = "RX") -> None:
        pkt_type = data[4]
        if pkt_type in (0x0A, 0x0B):
            self._last_ack = data
            self._ack_event.set()
            return
        if pkt_type == 0x03:
            serial = int.from_bytes(data[5:7], "little")
            self.log(f"Configuration response ({source}): controller serial {serial}")

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

        if not self._seen_first_status:
            self._seen_first_status = True
            self.log(
                f"Status received: {st.state_text()} "
                f"progress={st.progress}% line={st.line_number} job={st.job_count}"
            )

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

    def _reset_tx_socket_for_upload(self) -> bool:
        """Refresh the TX/upload socket before a file send.

        Home testing with V1.7.9 showed long-name uploads could succeed on the
        first send after connect, then fail on the second send until the app was
        disconnected/reconnected. Recreating only the TX socket gives the upload
        path a fresh ephemeral UDP source port/session while keeping the status
        RX socket alive.
        """
        if not self.host:
            self.log("Cannot reset TX socket: no MASSO host set")
            return False

        with self._tx_lock:
            old_sock = self.tx_socket
            self._tx_generation += 1
            generation = self._tx_generation

            try:
                new_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                new_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                new_sock.connect((self.host, CONTROLLER_PORT))
                new_sock.settimeout(0.25)
                self.tx_socket = new_sock
                self.tx_local_port = new_sock.getsockname()[1]
            except Exception as exc:
                self.log(f"Could not reset TX socket for upload: {exc}")
                self.tx_socket = old_sock
                return False

            if old_sock:
                try:
                    old_sock.close()
                except OSError:
                    pass

        self.tx_listen_thread = threading.Thread(
            target=self._tx_listen_loop,
            args=(self.tx_socket, generation),
            daemon=True,
        )
        self.tx_listen_thread.start()
        self.log(f"TX socket refreshed for upload: UDP {self.tx_local_port}")

        # Re-send the short connection sequence from the fresh TX port. This
        # mimics the manual disconnect/reconnect workaround without dropping the
        # UI/status RX socket.
        try:
            self._send_handshake()
            time.sleep(0.15)
        except Exception as exc:
            self.log(f"TX refresh handshake warning: {exc}")

        return True


    # -----------------------------
    # Upload
    # -----------------------------

    def upload_file_async(self, local_path: str, remote_folder: str, upload_id: Optional[int] = None) -> None:
        if self.upload_in_progress:
            self.log("Upload already in progress")
            return
        t = threading.Thread(target=self._upload_file_worker, args=(local_path, remote_folder, upload_id), daemon=True)
        t.start()

    def _build_start_upload_packets(self, filesize: int, remote_folder: str, filename: str):
        """Build MASSO Link-style start-upload packet candidates.

        V1.7.9 finding: MASSO Link start-upload packets appear to pad the
        payload AFTER the 2-byte CRC to a 4-byte boundary. The bytes after the
        filename NUL in captures look like garbage/text residue, but the packet
        payload length is consistently divisible by 4.

        Keep the proven fixed-50 packet for names that fit. For longer names,
        build a compact variable-length packet and zero-pad only to the next
        4-byte payload boundary.
        """
        folder = normalize_masso_folder(remote_folder)
        folder_b = folder.encode("ascii", errors="replace")
        name_b = filename.encode("ascii", errors="replace")

        if len(folder_b) > 255:
            raise ValueError(f"Remote folder is too long for 1-byte folder length field ({len(folder_b)} bytes).")

        base = bytearray()
        base.extend(b"\x03\x00")
        base.append(0x0A)
        base.extend(filesize.to_bytes(4, "little"))
        base.extend(b"\x00\x00")
        base.append(len(folder_b))  # folder length, not including NUL
        base.extend(folder_b)
        base.append(0x00)
        base.extend(name_b)
        base.append(0x00)

        candidates = []

        # Candidate 1: proven fixed-50 packet for normal names.
        # Payload length after CRC is 48 bytes, which is also 4-byte aligned.
        if len(base) <= 48:
            p = bytearray(base)
            if len(p) <= 45:
                p.extend(b"826"[: 48 - len(p)])
            while len(p) < 48:
                p.append(0x00)
            candidates.append(("fixed-50", with_crc(bytes(p))))
            return candidates

        # Long-name experimental path. Do not try multiple bad variants first;
        # the controller may enter/hold a failed receive state after a bad start.
        if len(base) > 180:
            raise ValueError(
                f"Remote folder/name too long for experimental packet ({len(base)} payload bytes, max 180). "
                "Use a shorter target folder and filename for now."
            )

        p = bytearray(base)
        pad_len = (-len(p)) % 4
        if pad_len:
            p.extend(b"\x00" * pad_len)
        candidates.append((f"compact-4byte-align-pad{pad_len}", with_crc(bytes(p))))

        return candidates

    def _build_data_packet(self, chunk_index: int, chunk: bytes, *, pad_to_full_chunk: bool = False, final_chunk: bool = False) -> bytes:
        """Build a file data packet.

        MASSO Link captures show:
          normal chunk: 1422 data bytes, packet length 1438
          final short chunk: actual remaining byte count, plus a 4-byte trailer.

        V1.6 test change: final short chunks use a 4-byte trailer and quick
        resend cadence. In the clean home captures, V1.5 got ACKs through
        chunk 33 and failed only on the final short chunk. MASSO Link's final
        packet was one byte longer than V1.5's final packet.
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
        if final_chunk and len(chunk) < CHUNK_SIZE and not pad_to_full_chunk:
            # Clean MASSO Link captures show final short chunks need at least a
            # 4-byte trailer. A later 60,630-byte test exposed the fuller rule:
            # the complete UDP payload length must also be even. With the 13-byte
            # data header, an even-sized final chunk needs 3 pad bytes, while an
            # odd-sized final chunk needs 4 pad bytes.
            final_pad_len = 4 if (len(chunk) % 2) else 3
            payload.extend(b"\x00" * final_pad_len)
        else:
            payload.extend(b"\x00\x00\x00")
        return with_crc(bytes(payload))


    def _build_data_packet_full_wire_actual_length(self, chunk_index: int, chunk: bytes) -> bytes:
        """Build a compatibility data packet for small/single-chunk uploads.

        Some captures from the work MASSO show start-upload accepted, but the
        controller ignores a short first/final packet. This variant keeps the
        length field equal to the real file bytes, but pads the wire data area
        out to CHUNK_SIZE so the UDP packet length matches a normal full chunk.
        If MASSO honors the length field, the saved file should remain the
        correct size; the extra bytes are just transport padding.
        """
        wire_chunk = chunk
        if len(wire_chunk) < CHUNK_SIZE:
            wire_chunk = wire_chunk + (b"\x00" * (CHUNK_SIZE - len(wire_chunk)))

        payload = bytearray()
        payload.extend(b"\x03\x00")
        payload.append(0x0B)
        payload.extend(chunk_index.to_bytes(4, "little"))
        payload.extend(len(chunk).to_bytes(4, "little"))
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

    def _upload_file_worker(self, local_path: str, remote_folder: str, upload_id: Optional[int] = None) -> None:
        self.upload_in_progress = True
        self.post("upload_state", True)

        def fail(reason: str) -> None:
            self.log(reason)
            self.post("upload_failed", {
                "upload_id": upload_id,
                "path": local_path,
                "folder": remote_folder,
                "reason": reason,
                "timestamp": time.time(),
            })

        try:
            allowed, reason = self.status.upload_allowed()
            if not allowed:
                fail(f"Upload blocked: {reason}")
                return

            path = Path(local_path)
            if not path.exists() or not path.is_file():
                fail(f"File not found: {local_path}")
                return
            if path.stat().st_size <= 0:
                fail("Upload blocked: file is empty")
                return

            filename = path.name
            filesize = path.stat().st_size
            remote_folder = normalize_masso_folder(remote_folder)

            self.log(f"Starting upload: {filename} ({filesize} bytes) -> {remote_folder}")

            # V1.7.10: refresh TX/upload socket before every file. This matches
            # the observed workaround where reconnecting made the first upload
            # reliable again.
            if not self._reset_tx_socket_for_upload():
                fail("Upload failed: could not refresh TX socket")
                return

            start_packets = self._build_start_upload_packets(filesize, remote_folder, filename)

            start_ack = None
            start_variant = None
            for variant_name, start_packet in start_packets:
                self.log(f"Trying start-upload packet: {variant_name} len={len(start_packet)}")
                for attempt in range(1, 4):
                    start_ack = self._send_with_ack(start_packet, 0x0A, timeout=2.0)
                    if start_ack is None:
                        self.log(f"Start upload {variant_name} attempt {attempt}: no ACK")
                        continue
                    # Start-upload ACKs vary by controller/firmware. Home captures
                    # commonly showed bytes 5:7 == 00 00 for accepted, while a
                    # work-controller capture showed 00 44 followed by duplicate-start
                    # rejects if we did not treat the first ACK as accepted. The stable
                    # discriminator appears to be byte 5:
                    #   00 = start accepted
                    #   F7 = start rejected/failure
                    code = start_ack[5:7]
                    if start_ack[5] == 0x00:
                        start_variant = variant_name
                        self.log(f"Start upload accepted via {variant_name}; code={code.hex(' ')} ACK={start_ack.hex(' ')}")
                        break
                    self.log(f"Start upload {variant_name} rejected: code {code.hex(' ')} ACK={start_ack.hex(' ')}")
                    start_ack = None
                    time.sleep(0.4)
                if start_ack is not None:
                    break

            if start_ack is None:
                fail("Upload failed: MASSO did not accept start upload")
                return

            total_chunks = (filesize + CHUNK_SIZE - 1) // CHUNK_SIZE
            with path.open("rb") as f:
                for chunk_index in range(total_chunks):
                    chunk = f.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    # V1.6/V1.7.4: keep actual final chunk length, but mark final
                    # short chunks so they get the MASSO Link-style trailer.
                    pad_this_chunk = False
                    is_final_chunk = (chunk_index == total_chunks - 1)
                    expected_next = chunk_index + 1

                    packet_variants = [(
                        "short-final" if is_final_chunk and len(chunk) < CHUNK_SIZE else "normal",
                        self._build_data_packet(
                            chunk_index,
                            chunk,
                            pad_to_full_chunk=pad_this_chunk,
                            final_chunk=is_final_chunk,
                        ),
                    )]

                    # Work MASSO compatibility test: a 411-byte single-chunk file
                    # accepted start-upload but ignored the short final packet. Try a
                    # full-size wire packet with the real length field if the proven
                    # short packet gets no ACK.
                    if is_final_chunk and len(chunk) < CHUNK_SIZE:
                        packet_variants.append((
                            "full-wire-real-length",
                            self._build_data_packet_full_wire_actual_length(chunk_index, chunk),
                        ))

                    ok = False
                    for packet_variant_name, packet in packet_variants:
                        if chunk_index == 0:
                            self.log(
                                f"Sending first data packet variant {packet_variant_name} from TX UDP {self.tx_local_port}: "
                                f"len={len(packet)} real_chunk_len={len(chunk)} "
                                f"final={is_final_chunk} head={packet[:16].hex(' ')}"
                            )

                        # MASSO Link resends chunks quickly until the ACK advances.
                        max_attempts = 10 if packet_variant_name != "full-wire-real-length" else 12
                        if is_final_chunk and packet_variant_name == "short-final":
                            max_attempts = 6
                        ack_timeout = 0.20 if is_final_chunk else 0.30
                        for attempt in range(1, max_attempts + 1):
                            ack = self._send_with_ack(packet, 0x0B, timeout=ack_timeout)
                            if ack is None:
                                if attempt in (1, max_attempts) or attempt % 5 == 0:
                                    self.log(f"Chunk {expected_next}/{total_chunks} {packet_variant_name}: no ACK, retry {attempt}")
                                continue

                            # Capture shows data ACK bytes 5:7 as big-endian next expected chunk.
                            ack_next = int.from_bytes(ack[5:7], "big")
                            if ack_next == expected_next:
                                if expected_next == 1:
                                    self.log(f"First chunk ACK received via {packet_variant_name}: {ack.hex(' ')}")
                                ok = True
                                break

                            # MASSO often returns a stale/previous ACK first. Keep resending
                            # the same chunk until the ACK advances to the expected value.
                            if attempt in (1, max_attempts) or attempt % 5 == 0:
                                self.log(
                                    f"Chunk {expected_next}/{total_chunks} {packet_variant_name}: "
                                    f"stale/unexpected ACK next={ack_next}, retry {attempt}"
                                )
                        if ok:
                            break

                    if not ok:
                        fail(f"Upload failed at chunk {expected_next}/{total_chunks}")
                        return

                    progress = int(expected_next * 100 / total_chunks)
                    self.post("upload_progress", progress)
                    if expected_next == 1 or expected_next == total_chunks or expected_next % 5 == 0:
                        self.log(f"Sent chunk {expected_next}/{total_chunks} ({progress}%)")

            self.log(f"Upload complete: {filename}")
            self.post("upload_progress", 100)
            self.post("upload_complete", {
                "upload_id": upload_id,
                "filename": filename,
                "folder": remote_folder,
                "path": str(path),
                "size": filesize,
                "timestamp": time.time(),
            })
        except Exception as exc:
            fail(f"Upload error: {exc}")
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
        "auto_clear_queue": False,
    }


def save_config(cfg: Dict[str, Any]) -> None:
    try:
        CONFIG_FILE.write_text(json.dumps(cfg, indent=2))
    except Exception:
        pass


# -----------------------------
# Tkinter GUI
# -----------------------------

class SendGui:
    """Backgauge-inspired shop/HMI style GUI.

    The protocol/upload code above is intentionally kept close to V1.6.
    This class mostly changes layout, readability, and operator feedback.
    """

    BG = "#15181d"
    PANEL = "#20252c"
    PANEL_2 = "#252b33"
    TEXT = "#e7edf5"
    MUTED = "#9aa6b2"
    BORDER = "#343b45"
    READY = "#1f8a4c"
    RUNNING = "#b47b20"
    FAULT = "#b33a3a"
    DISABLED = "#5b6470"
    BLUE = "#2d6cdf"

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(APP_NAME)
        self.root.geometry("1160x780")
        self.root.minsize(1020, 680)
        self.root.configure(bg=self.BG)

        self.events: queue.Queue = queue.Queue()
        self.client = MassoClient(self.events)
        self.config = load_config()
        self.selected_file: Optional[str] = None
        self.upload_busy = False
        self.current_status = MassoStatus()
        self.last_upload_info: Optional[Dict[str, Any]] = None
        self.upload_queue: list[Dict[str, Any]] = []
        self.next_queue_id = 1
        self.queue_running = False
        self.current_queue_id: Optional[int] = None
        self.auto_clear_queue_var = tk.BooleanVar(value=bool(self.config.get("auto_clear_queue", False)))
        self.logo_photo = None

        self._setup_styles()
        self._build_ui()
        self._wire_target_preview_updates()
        self._load_profiles_into_combo()
        self._poll_events()
        self._tick_last_sent()
        self._refresh_upload_button()

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    # -----------------------------
    # UI construction
    # -----------------------------

    def _setup_styles(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except Exception:
            pass

        style.configure("App.TFrame", background=self.BG)
        style.configure("Panel.TFrame", background=self.PANEL, borderwidth=1, relief="solid")
        style.configure("Panel.TLabelframe", background=self.PANEL, borderwidth=1, relief="solid")
        style.configure("Panel.TLabelframe.Label", background=self.PANEL, foreground=self.TEXT, font=("Segoe UI Semibold", 12))
        style.configure("SubPanel.TFrame", background=self.PANEL_2)
        style.configure("TLabel", background=self.PANEL, foreground=self.TEXT, font=("Segoe UI", 10))
        style.configure("Muted.TLabel", background=self.PANEL, foreground=self.MUTED, font=("Segoe UI", 9))
        style.configure("Header.TLabel", background=self.BG, foreground=self.TEXT, font=("Segoe UI Semibold", 19))
        style.configure("Version.TLabel", background=self.BG, foreground=self.MUTED, font=("Segoe UI", 9))
        style.configure("Section.TLabel", background=self.PANEL, foreground=self.TEXT, font=("Segoe UI Semibold", 12))
        style.configure("BigStatus.TLabel", background=self.PANEL_2, foreground=self.TEXT, font=("Segoe UI Semibold", 22))
        style.configure("BigValue.TLabel", background=self.PANEL, foreground=self.TEXT, font=("Segoe UI Semibold", 14))
        style.configure("Value.TLabel", background=self.PANEL, foreground=self.TEXT, font=("Segoe UI", 10))
        style.configure("TEntry", fieldbackground="#f4f6f8", foreground="#111111")
        style.configure("TCombobox", fieldbackground="#f4f6f8", foreground="#111111")
        style.configure("TButton", font=("Segoe UI Semibold", 10), padding=(10, 6))
        style.configure("Primary.TButton", font=("Segoe UI Semibold", 12), padding=(16, 10))
        style.configure("Danger.TButton", font=("Segoe UI Semibold", 10), padding=(10, 6))
        style.configure("Horizontal.TProgressbar", troughcolor="#111318", background=self.BLUE)

    def _panel(self, parent: tk.Widget, title: str) -> ttk.Frame:
        # Use a LabelFrame so callers can freely use grid() inside the panel.
        # Do not pack a header widget inside this frame; Tkinter does not allow
        # mixing pack() and grid() for children of the same parent.
        return ttk.LabelFrame(parent, text=title.upper(), style="Panel.TLabelframe", padding=14)

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, style="App.TFrame", padding=14)
        outer.pack(fill="both", expand=True)

        # Top title bar
        top = ttk.Frame(outer, style="App.TFrame")
        top.pack(fill="x", pady=(0, 12))
        ttk.Label(top, text=" Send-to-MASSO Manager", style="Header.TLabel").pack(side="left")
        ttk.Label(top, text="v1.8.5 RC1", style="Version.TLabel").pack(side="right", pady=(9, 0))

        body = ttk.Frame(outer, style="App.TFrame")
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=3, uniform="cols")
        body.columnconfigure(1, weight=2, uniform="cols")
        body.rowconfigure(2, weight=1)

        # Connection panel
        conn = self._panel(body, "Connection")
        conn.grid(row=0, column=0, sticky="nsew", padx=(0, 8), pady=(0, 10))
        conn.columnconfigure(1, weight=1)
        conn.columnconfigure(3, weight=1)

        ttk.Label(conn, text="Saved profile", style="Muted.TLabel").grid(row=0, column=0, sticky="w")
        self.profile_var = tk.StringVar()
        self.profile_combo = ttk.Combobox(conn, textvariable=self.profile_var, state="readonly", width=24)
        self.profile_combo.grid(row=1, column=0, sticky="ew", padx=(0, 8), pady=(2, 8))
        self.profile_combo.bind("<<ComboboxSelected>>", self.on_profile_selected)

        ttk.Label(conn, text="Profile name", style="Muted.TLabel").grid(row=0, column=1, sticky="w")
        self.profile_name_var = tk.StringVar()
        self.profile_name_entry = ttk.Entry(conn, textvariable=self.profile_name_var)
        self.profile_name_entry.grid(row=1, column=1, sticky="ew", padx=(0, 8), pady=(2, 8))

        ttk.Label(conn, text="MASSO IP", style="Muted.TLabel").grid(row=0, column=2, sticky="w")
        self.ip_var = tk.StringVar()
        self.ip_entry = ttk.Entry(conn, textvariable=self.ip_var, width=18)
        self.ip_entry.grid(row=1, column=2, sticky="ew", padx=(0, 8), pady=(2, 8))

        btns = ttk.Frame(conn, style="Panel.TFrame")
        btns.grid(row=1, column=3, sticky="e", pady=(2, 8))
        self.connect_btn = ttk.Button(btns, text="CONNECT", command=self.connect, style="Primary.TButton")
        self.connect_btn.pack(side="left", padx=(0, 6))
        self.disconnect_btn = ttk.Button(btns, text="DISCONNECT", command=self.disconnect)
        self.disconnect_btn.pack(side="left")

        profile_btns = ttk.Frame(conn, style="Panel.TFrame")
        profile_btns.grid(row=2, column=0, columnspan=4, sticky="ew")
        self.save_profile_btn = ttk.Button(profile_btns, text="Save / Update Profile", command=self.save_current_profile)
        self.save_profile_btn.pack(side="left", padx=(0, 6))
        self.new_profile_btn = ttk.Button(profile_btns, text="New", command=self.new_profile)
        self.new_profile_btn.pack(side="left", padx=(0, 6))
        self.delete_profile_btn = ttk.Button(profile_btns, text="Delete", command=self.delete_current_profile)
        self.delete_profile_btn.pack(side="left")

        # Status panel
        status = self._panel(body, "Machine Status")
        status.grid(row=0, column=1, rowspan=2, sticky="nsew", padx=(8, 0), pady=(0, 10))
        status.columnconfigure(0, weight=1)

        self.status_banner = tk.Label(
            status,
            text="NOT CONNECTED",
            bg=self.DISABLED,
            fg="white",
            font=("Segoe UI Semibold", 22),
            padx=12,
            pady=16,
            anchor="center",
        )
        self.status_banner.grid(row=0, column=0, sticky="ew", pady=(0, 12))

        self.upload_allowed_banner = tk.Label(
            status,
            text="UPLOAD LOCKED",
            bg=self.DISABLED,
            fg="white",
            font=("Segoe UI Semibold", 13),
            padx=8,
            pady=8,
            anchor="center",
        )
        self.upload_allowed_banner.grid(row=1, column=0, sticky="ew", pady=(0, 12))

        self.progress_var = tk.StringVar(value="0%")
        progress_row = ttk.Frame(status, style="Panel.TFrame")
        progress_row.grid(row=2, column=0, sticky="ew", pady=(0, 10))
        progress_row.columnconfigure(0, weight=1)
        ttk.Label(progress_row, text="Progress", style="Muted.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(progress_row, textvariable=self.progress_var, style="BigValue.TLabel").grid(row=0, column=1, sticky="e")
        self.progress_bar = ttk.Progressbar(progress_row, maximum=100, mode="determinate")
        self.progress_bar.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(4, 0))

        # Larger HMI values
        # Use a plain tk.Frame here instead of a styled ttk frame so there is no
        # extra border/relief showing behind the two value boxes.
        big_values = tk.Frame(status, bg=self.PANEL, bd=0, highlightthickness=0)
        big_values.grid(row=3, column=0, sticky="ew", pady=(0, 10))
        big_values.columnconfigure(0, weight=1, uniform="statusbig")
        big_values.columnconfigure(1, weight=1, uniform="statusbig")
        big_values.rowconfigure(0, weight=1)

        self.job_var = tk.StringVar(value="0")
        self.line_var = tk.StringVar(value="0")
        self._big_status_box(big_values, 0, "JOB COUNT", self.job_var)
        self._big_status_box(big_values, 1, "LINE", self.line_var)

        # Detail values
        self.file_var = tk.StringVar(value="(none)")

        self._detail_row(status, 4, "Current / Last File", self.file_var)

        logo_frame = ttk.Frame(status, style="SubPanel.TFrame", padding=8)
        logo_frame.grid(row=5, column=0, sticky="ew", pady=(10, 0))
        logo_frame.columnconfigure(0, weight=1)
        self.logo_label = tk.Label(
            logo_frame,
            text="",
            bg=self.PANEL_2,
            fg=self.TEXT,
            font=("Segoe UI Semibold", 16),
            anchor="center",
            justify="center",
        )
        self.logo_label.grid(row=0, column=0, sticky="ew")
        self.logo_note_label = tk.Label(
            logo_frame,
            text="Brand image loads from send_to_masso_logo.png beside the app.",
            bg=self.PANEL_2,
            fg=self.MUTED,
            font=("Segoe UI", 8),
            anchor="center",
        )
        self.logo_note_label.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        self._load_brand_logo()

        # File queue / send panel
        send = self._panel(body, "Upload Queue")
        send.grid(row=1, column=0, sticky="nsew", padx=(0, 8), pady=(0, 10))
        send.columnconfigure(0, weight=1)
        send.columnconfigure(1, weight=1)
        send.columnconfigure(2, weight=1)
        send.rowconfigure(4, weight=1)

        ttk.Label(send, text="Target MASSO folder for newly added files", style="Muted.TLabel").grid(row=0, column=0, columnspan=2, sticky="w")
        self.remote_folder_var = tk.StringVar(value=self.config.get("last_folder", "\\"))
        ttk.Entry(send, textvariable=self.remote_folder_var).grid(row=1, column=0, columnspan=2, sticky="ew", padx=(0, 8), pady=(2, 8))
        ttk.Label(send, text=r"example: \Test\  or  /Test/", style="Muted.TLabel").grid(row=1, column=2, sticky="w", pady=(2, 8))

        preview = ttk.Frame(send, style="SubPanel.TFrame", padding=8)
        preview.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(0, 8))
        preview.columnconfigure(0, weight=1)
        ttk.Label(preview, text="SELECTED / NEXT MASSO TARGET", style="Muted.TLabel", background=self.PANEL_2).grid(row=0, column=0, sticky="w")
        self.local_file_var = tk.StringVar(value="")
        self.target_preview_var = tk.StringVar(value="Queue empty")
        self.target_validation_var = tk.StringVar(value="Add files to begin")
        tk.Label(
            preview,
            textvariable=self.target_preview_var,
            bg=self.PANEL_2,
            fg=self.TEXT,
            font=("Consolas", 10),
            anchor="w",
            wraplength=760,
        ).grid(row=1, column=0, sticky="ew", pady=(3, 0))
        self.target_validation_label = tk.Label(
            preview,
            textvariable=self.target_validation_var,
            bg=self.PANEL_2,
            fg=self.MUTED,
            font=("Segoe UI", 9),
            anchor="w",
        )
        self.target_validation_label.grid(row=2, column=0, sticky="ew", pady=(3, 0))

        queue_btns = ttk.Frame(send, style="Panel.TFrame")
        queue_btns.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(0, 8))
        ttk.Button(queue_btns, text="Add Files...", command=self.add_files_to_queue).pack(side="left", padx=(0, 6))
        ttk.Button(queue_btns, text="Remove Selected", command=self.remove_selected_queue_items).pack(side="left", padx=(0, 6))
        ttk.Button(queue_btns, text="Clear Queue", command=self.clear_queue).pack(side="left", padx=(0, 6))
        ttk.Button(queue_btns, text="Move Up", command=lambda: self.move_selected_queue_item(-1)).pack(side="left", padx=(12, 6))
        ttk.Button(queue_btns, text="Move Down", command=lambda: self.move_selected_queue_item(1)).pack(side="left", padx=(0, 6))
        ttk.Button(queue_btns, text="QR Selected...", command=self.generate_qr_for_selected).pack(side="left", padx=(12, 6))
        ttk.Button(queue_btns, text="QR Queue...", command=self.generate_qr_for_queue).pack(side="left", padx=(0, 12))
        self.auto_clear_check = tk.Checkbutton(
            queue_btns,
            text="Auto-clear when queue completes",
            variable=self.auto_clear_queue_var,
            command=self.on_auto_clear_changed,
            bg=self.PANEL,
            fg=self.TEXT,
            selectcolor=self.PANEL_2,
            activebackground=self.PANEL,
            activeforeground=self.TEXT,
            font=("Segoe UI", 9),
        )
        self.auto_clear_check.pack(side="left")

        columns = ("name", "size", "folder", "status")
        self.queue_tree = ttk.Treeview(send, columns=columns, show="headings", height=7, selectmode="extended")
        self.queue_tree.heading("name", text="File")
        self.queue_tree.heading("size", text="Size")
        self.queue_tree.heading("folder", text="MASSO Folder")
        self.queue_tree.heading("status", text="Status")
        self.queue_tree.column("name", width=360, anchor="w")
        self.queue_tree.column("size", width=90, anchor="e")
        self.queue_tree.column("folder", width=180, anchor="w")
        self.queue_tree.column("status", width=120, anchor="w")
        self.queue_tree.grid(row=4, column=0, columnspan=3, sticky="nsew", pady=(0, 8))
        self.queue_tree.bind("<<TreeviewSelect>>", lambda _e: self._update_target_preview())
        qscroll = ttk.Scrollbar(send, command=self.queue_tree.yview)
        qscroll.grid(row=4, column=3, sticky="ns", pady=(0, 8))
        self.queue_tree.configure(yscrollcommand=qscroll.set)

        self.send_btn = tk.Button(
            send,
            text="SEND QUEUE",
            command=self.send_queue,
            bg=self.BLUE,
            fg="white",
            activebackground="#1e54b6",
            activeforeground="white",
            disabledforeground="#c2c6cc",
            font=("Segoe UI Semibold", 16),
            padx=16,
            pady=12,
            relief="flat",
            cursor="hand2",
        )
        self.send_btn.grid(row=5, column=0, sticky="ew", pady=(2, 0))
        self.upload_progress = ttk.Progressbar(send, maximum=100, mode="determinate")
        self.upload_progress.grid(row=5, column=1, columnspan=2, sticky="ew", padx=(8, 0), pady=(2, 0))

        self.last_sent_var = tk.StringVar(value="No successful upload yet")
        last = ttk.Frame(send, style="SubPanel.TFrame", padding=10)
        last.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        last.columnconfigure(0, weight=1)
        ttk.Label(last, text="LAST SEND", style="Muted.TLabel", background=self.PANEL_2).grid(row=0, column=0, sticky="w")
        tk.Label(
            last,
            textvariable=self.last_sent_var,
            bg=self.PANEL_2,
            fg=self.TEXT,
            font=("Segoe UI Semibold", 12),
            anchor="w",
        ).grid(row=1, column=0, sticky="ew", pady=(3, 0))

        # Log panel
        logf = self._panel(body, "Activity Log")
        logf.grid(row=2, column=0, columnspan=2, sticky="nsew", pady=(0, 0))
        logf.rowconfigure(0, weight=1)
        logf.columnconfigure(0, weight=1)

        self.log_text = tk.Text(
            logf,
            height=12,
            wrap="word",
            bg="#0f1115",
            fg="#d8dee9",
            insertbackground="#d8dee9",
            relief="flat",
            font=("Consolas", 10),
        )
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(logf, command=self.log_text.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=scroll.set)

        self.log("V1.8.5 RC HMI GUI loaded. Release-candidate layout cleanup build.")


    def _load_brand_logo(self) -> None:
        """Load the logo from the program directory if present."""
        candidates = [
            APP_DIR / "send_to_masso_logo.png",
            APP_DIR / "industrial_racing_logo_design.png",
            APP_DIR / "fablab_racing_logo.png",
        ]
        try:
            from PIL import Image, ImageTk
        except Exception:
            self.logo_label.configure(text="")
            self.logo_note_label.configure(text="Install Pillow to display the brand image.")
            return

        for path in candidates:
            if not path.exists():
                continue
            try:
                image = Image.open(path)
                max_width = 430
                max_height = 120
                scale = min(max_width / image.width, max_height / image.height, 1.0)
                new_size = (max(1, int(image.width * scale)), max(1, int(image.height * scale)))
                if new_size != image.size:
                    image = image.resize(new_size, Image.LANCZOS)
                self.logo_photo = ImageTk.PhotoImage(image)
                self.logo_label.configure(image=self.logo_photo, text="")
                self.logo_note_label.configure(text="")
                return
            except Exception as exc:
                self.logo_label.configure(text="")
                self.logo_note_label.configure(text=f"Logo load failed: {exc}")
                return

        self.logo_label.configure(text="")
        self.logo_note_label.configure(text="Place send_to_masso_logo.png beside the app to display the logo.")

    def _big_status_box(self, parent: tk.Widget, column: int, label: str, var: tk.StringVar) -> None:
        box = tk.Frame(parent, bg=self.PANEL_2, bd=0, highlightthickness=0, padx=12, pady=10)
        box.grid(row=0, column=column, sticky="nsew", padx=(0, 6) if column == 0 else (6, 0))
        box.columnconfigure(0, weight=1)
        tk.Label(
            box,
            text=label,
            bg=self.PANEL_2,
            fg=self.MUTED,
            font=("Segoe UI", 9),
            anchor="w",
        ).grid(row=0, column=0, sticky="w")
        tk.Label(
            box,
            textvariable=var,
            bg=self.PANEL_2,
            fg=self.TEXT,
            font=("Segoe UI Semibold", 24),
            anchor="center",
            justify="center",
        ).grid(row=1, column=0, sticky="ew", pady=(10, 4))

    def _detail_row(self, parent: tk.Widget, row: int, label: str, var: tk.StringVar) -> None:
        box = ttk.Frame(parent, style="SubPanel.TFrame", padding=(10, 8))
        box.grid(row=row, column=0, sticky="ew", pady=(0, 8))
        box.columnconfigure(0, weight=1)
        ttk.Label(box, text=label, style="Muted.TLabel", background=self.PANEL_2).grid(row=0, column=0, sticky="w")
        tk.Label(
            box,
            textvariable=var,
            bg=self.PANEL_2,
            fg=self.TEXT,
            font=("Segoe UI", 10),
            anchor="w",
            justify="left",
            wraplength=660,
        ).grid(row=1, column=0, sticky="ew", pady=(4, 0))

    # -----------------------------
    # Profile handling
    # -----------------------------

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
            self.profile_name_var.set(p.get("name", ""))
            self.ip_var.set(p.get("ip", ""))

    def save_current_profile(self) -> None:
        name = self.profile_name_var.get().strip()
        ip = self.ip_var.get().strip()
        if not name:
            messagebox.showwarning(APP_NAME, "Enter a profile name first, for example Home MASSO or Work Torchmate.")
            self.profile_name_entry.focus_set()
            return
        if not ip:
            messagebox.showwarning(APP_NAME, "Enter an IP address first.")
            self.ip_entry.focus_set()
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
        self.profile_var.set(name)
        self.profile_name_var.set(name)
        self.ip_var.set(ip)
        self.log(f"Saved profile: {name} -> {ip}")

    def new_profile(self) -> None:
        self.profile_var.set("")
        self.profile_name_var.set("")
        self.ip_var.set("")
        self.profile_name_entry.focus_set()

    def delete_current_profile(self) -> None:
        name = self.profile_name_var.get().strip() or self.profile_var.get().strip()
        if not name:
            return
        profiles = self.config.get("profiles", [])
        match = [p for p in profiles if p.get("name") == name]
        if not match:
            messagebox.showinfo(APP_NAME, f"No saved profile named {name!r}.")
            return
        if not messagebox.askyesno(APP_NAME, f"Delete profile {name!r}?"):
            return
        self.config["profiles"] = [p for p in profiles if p.get("name") != name]
        names = [p.get("name", "MASSO") for p in self.config["profiles"]]
        self.config["last_profile"] = names[0] if names else ""
        save_config(self.config)
        self._load_profiles_into_combo()
        if not names:
            self.profile_name_var.set("")
            self.ip_var.set("")
        self.log(f"Deleted profile: {name}")

    # -----------------------------
    # Commands
    # -----------------------------

    def on_auto_clear_changed(self) -> None:
        self.config["auto_clear_queue"] = bool(self.auto_clear_queue_var.get())
        save_config(self.config)
        self.log(f"Auto-clear queue when complete: {'ON' if self.auto_clear_queue_var.get() else 'OFF'}")

    def connect(self) -> None:
        ip = self.ip_var.get().strip()
        if not ip:
            messagebox.showwarning(APP_NAME, "Enter a MASSO IP address.")
            return
        self.config["last_profile"] = self.profile_name_var.get().strip() or self.profile_var.get().strip()
        save_config(self.config)
        self.client.start(ip)

    def disconnect(self) -> None:
        self.client.stop()
        self.log("Disconnected")

    def _wire_target_preview_updates(self) -> None:
        self.remote_folder_var.trace_add("write", lambda *_: self._update_target_preview())
        self._update_target_preview()

    def _selected_or_next_queue_item(self) -> Optional[Dict[str, Any]]:
        if not hasattr(self, "queue_tree"):
            return None
        selected = self.queue_tree.selection()
        if selected:
            qid = int(selected[0])
            for item in self.upload_queue:
                if item["id"] == qid:
                    return item
        for item in self.upload_queue:
            if item.get("status") in ("Pending", "Failed"):
                return item
        return self.upload_queue[0] if self.upload_queue else None

    def _target_check_for_item(self, item: Optional[Dict[str, Any]] = None) -> tuple[bool, list[str], list[str], str]:
        if item is None:
            item = self._selected_or_next_queue_item()
        if item is None:
            folder = self.remote_folder_var.get().strip() or "\\"
            return True, [], [], normalize_masso_folder(folder)
        return validate_masso_name_parts(item["path"], item["folder"])

    def _update_target_preview(self) -> None:
        if not hasattr(self, "target_preview_var"):
            return
        item = self._selected_or_next_queue_item()
        if item is None:
            self.target_preview_var.set(normalize_masso_folder(self.remote_folder_var.get().strip() or "\\"))
            self.target_validation_var.set("Queue empty")
            self.target_validation_label.configure(fg=self.MUTED)
            self._refresh_upload_button()
            return
        ok, errors, warnings, preview = self._target_check_for_item(item)
        self.target_preview_var.set(preview)
        if errors:
            self.target_validation_var.set("Invalid target: " + "  ".join(errors))
            self.target_validation_label.configure(fg="#ffb3b3")
        elif warnings:
            self.target_validation_var.set("Warning: " + "  ".join(warnings))
            self.target_validation_label.configure(fg="#ffd27f")
        else:
            self.target_validation_var.set("Target name looks OK")
            self.target_validation_label.configure(fg=self.MUTED)
        self._refresh_upload_button()

    def _format_size(self, size: int) -> str:
        if size < 1024:
            return f"{size} B"
        if size < 1024 * 1024:
            return f"{size / 1024:.1f} KB"
        return f"{size / (1024 * 1024):.1f} MB"

    def _refresh_queue_tree(self) -> None:
        if not hasattr(self, "queue_tree"):
            return
        current_selection = set(self.queue_tree.selection())
        self.queue_tree.delete(*self.queue_tree.get_children())
        for item in self.upload_queue:
            iid = str(item["id"])
            path = Path(item["path"])
            size = path.stat().st_size if path.exists() else 0
            self.queue_tree.insert(
                "",
                "end",
                iid=iid,
                values=(path.name, self._format_size(size), item["folder"], item.get("status", "Pending")),
            )
            if iid in current_selection:
                self.queue_tree.selection_add(iid)
        self._update_target_preview()
        self._refresh_upload_button()

    def _set_queue_item_status(self, upload_id: Optional[int], status: str) -> None:
        if upload_id is None:
            return
        for item in self.upload_queue:
            if item["id"] == upload_id:
                item["status"] = status
                break
        self._refresh_queue_tree()

    def add_files_to_queue(self) -> None:
        initial_dir = self.config.get("last_local_dir", str(Path.home()))
        paths = filedialog.askopenfilenames(
            title="Add MASSO G-code files",
            initialdir=initial_dir,
            filetypes=[("MASSO G-code files", "*.nc *.cnc *.tap *.eia *.txt"), ("All files", "*.*")],
        )
        if not paths:
            return
        folder = normalize_masso_folder(self.remote_folder_var.get().strip() or "\\")
        added = 0
        for raw_path in paths:
            path = Path(raw_path)
            if not path.exists() or not path.is_file():
                continue
            item = {
                "id": self.next_queue_id,
                "path": str(path),
                "folder": folder,
                "status": "Pending",
            }
            self.next_queue_id += 1
            self.upload_queue.append(item)
            added += 1
        if added:
            self.config["last_local_dir"] = str(Path(paths[-1]).parent)
            self.config["last_folder"] = folder
            save_config(self.config)
            self.log(f"Added {added} file(s) to upload queue -> {folder}")
        self._refresh_queue_tree()

    def remove_selected_queue_items(self) -> None:
        selected = {int(iid) for iid in self.queue_tree.selection()} if hasattr(self, "queue_tree") else set()
        if not selected:
            return
        if self.queue_running and self.current_queue_id in selected:
            messagebox.showwarning(APP_NAME, "Cannot remove the file that is currently uploading.")
            return
        self.upload_queue = [item for item in self.upload_queue if item["id"] not in selected]
        self.log(f"Removed {len(selected)} item(s) from queue")
        self._refresh_queue_tree()

    def clear_queue(self) -> None:
        if self.queue_running or self.upload_busy:
            messagebox.showwarning(APP_NAME, "Cannot clear the queue while an upload is in progress.")
            return
        self.upload_queue.clear()
        self.current_queue_id = None
        self.queue_running = False
        self.log("Queue cleared")
        self._refresh_queue_tree()

    def move_selected_queue_item(self, direction: int) -> None:
        selected = list(self.queue_tree.selection()) if hasattr(self, "queue_tree") else []
        if len(selected) != 1:
            return
        qid = int(selected[0])
        idx = next((i for i, item in enumerate(self.upload_queue) if item["id"] == qid), None)
        if idx is None:
            return
        new_idx = idx + direction
        if new_idx < 0 or new_idx >= len(self.upload_queue):
            return
        self.upload_queue[idx], self.upload_queue[new_idx] = self.upload_queue[new_idx], self.upload_queue[idx]
        self._refresh_queue_tree()
        self.queue_tree.selection_set(str(qid))

    def _ensure_qr_available(self) -> bool:
        try:
            import qrcode  # noqa: F401
            from PIL import Image  # noqa: F401
            return True
        except Exception as exc:
            messagebox.showerror(
                APP_NAME,
                "QR-code generation requires the Python packages qrcode and Pillow.\n\n"
                "Install them with:\n"
                "pip install qrcode[pil] pillow\n\n"
                f"Import error: {exc}",
            )
            return False

    def _make_qr_png(self, payload: str, output_path: Path) -> None:
        import qrcode

        qr = qrcode.QRCode(
            version=None,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=10,
            border=4,
        )
        qr.add_data(payload)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        img.save(output_path)

    def _qr_item_check(self, item: Optional[Dict[str, Any]]) -> Optional[str]:
        if item is None:
            return "Select a queued file first."
        ok, errors, warnings, preview = self._target_check_for_item(item)
        if not ok:
            return f"Invalid MASSO target:\n\n{preview}\n\n" + "\n".join(errors)
        return None

    def generate_qr_for_selected(self) -> None:
        item = self._selected_or_next_queue_item()
        msg = self._qr_item_check(item)
        if msg:
            messagebox.showwarning(APP_NAME, msg)
            return
        if not self._ensure_qr_available():
            return
        assert item is not None
        local_path = item["path"]
        payload = build_masso_qr_payload(local_path, item["folder"])
        default_path = Path(local_path).with_name(default_qr_filename(local_path))
        output = filedialog.asksaveasfilename(
            title="Save MASSO QR code",
            initialdir=str(default_path.parent),
            initialfile=default_path.name,
            defaultextension=".png",
            filetypes=[("PNG image", "*.png"), ("All files", "*.*")],
        )
        if not output:
            return
        out_path = Path(output)
        try:
            self._make_qr_png(payload, out_path)
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Could not generate QR code:\n\n{exc}")
            return
        preview = build_remote_target_preview(local_path, item["folder"])
        self.log(f"QR generated: {out_path}")
        self.log(f"QR target: {preview}")
        self.log(f"QR payload: {payload}")
        messagebox.showinfo(APP_NAME, f"QR code saved:\n{out_path}\n\nPayload:\n{payload}")

    def generate_qr_for_queue(self) -> None:
        items = [item for item in self.upload_queue if item.get("status") in ("Pending", "Failed", "Done", "Sending")]
        if not items:
            messagebox.showwarning(APP_NAME, "Add one or more files to the queue first.")
            return
        bad_messages = []
        for item in items:
            msg = self._qr_item_check(item)
            if msg:
                bad_messages.append(f"{Path(item['path']).name}: {msg}")
        if bad_messages:
            messagebox.showwarning(APP_NAME, "Cannot generate QR code(s):\n\n" + "\n\n".join(bad_messages[:5]))
            return
        if not self._ensure_qr_available():
            return
        initial_dir = str(Path(items[0]["path"]).parent) if items else str(APP_DIR)
        output_dir = filedialog.askdirectory(title="Choose folder for MASSO QR PNG files", initialdir=initial_dir)
        if not output_dir:
            return
        out_dir = Path(output_dir)
        made = 0
        errors = []
        for item in items:
            local_path = item["path"]
            payload = build_masso_qr_payload(local_path, item["folder"])
            out_path = out_dir / default_qr_filename(local_path)
            try:
                self._make_qr_png(payload, out_path)
                made += 1
            except Exception as exc:
                errors.append(f"{Path(local_path).name}: {exc}")
        self.log(f"Generated {made} QR code(s) in {out_dir}")
        if errors:
            self.log("QR generation errors: " + " | ".join(errors))
            messagebox.showwarning(APP_NAME, f"Generated {made} QR code(s), with errors:\n\n" + "\n".join(errors[:8]))
        else:
            messagebox.showinfo(APP_NAME, f"Generated {made} QR code(s) in:\n{out_dir}")

    def send_queue(self) -> None:
        if self.queue_running or self.upload_busy:
            return
        if not self.upload_queue:
            messagebox.showwarning(APP_NAME, "Add one or more files to the queue first.")
            return
        allowed, reason = self.current_status.upload_allowed()
        if not allowed:
            messagebox.showwarning(APP_NAME, f"Upload not allowed: {reason}")
            return
        pending = [item for item in self.upload_queue if item.get("status") in ("Pending", "Failed")]
        if not pending:
            messagebox.showinfo(APP_NAME, "There are no pending files to send.")
            return
        warnings: list[str] = []
        for item in pending:
            ok, errors, item_warnings, preview = self._target_check_for_item(item)
            if not ok:
                messagebox.showwarning(APP_NAME, f"Invalid MASSO target:\n\n{preview}\n\n" + "\n".join(errors))
                return
            if item_warnings:
                warnings.append(f"{Path(item['path']).name}: " + "  ".join(item_warnings))
        if warnings:
            proceed = messagebox.askyesno(
                APP_NAME,
                "MASSO target warning:\n\n"
                + "\n".join(warnings[:8])
                + ("\n..." if len(warnings) > 8 else "")
                + "\n\nSend queue anyway?",
            )
            if not proceed:
                return
        self.config["last_folder"] = self.remote_folder_var.get().strip() or "\\"
        save_config(self.config)
        self.queue_running = True
        self.upload_progress["value"] = 0
        self.log(f"Starting queue: {len(pending)} pending file(s)")
        self._start_next_queue_item()

    def _start_next_queue_item(self) -> None:
        if not self.queue_running:
            return
        allowed, reason = self.current_status.upload_allowed()
        if not allowed:
            self.queue_running = False
            self.current_queue_id = None
            self.log(f"Queue stopped: {reason}")
            self._refresh_upload_button()
            return
        next_item = next((item for item in self.upload_queue if item.get("status") in ("Pending", "Failed")), None)
        if next_item is None:
            self.queue_running = False
            self.current_queue_id = None
            self.log("Queue complete")
            if self.auto_clear_queue_var.get():
                done_count = len([item for item in self.upload_queue if item.get("status") == "Done"])
                self.upload_queue.clear()
                self._refresh_queue_tree()
                self._update_target_preview()
                self.log(f"Auto-cleared {done_count} completed queue item(s)")
            self._refresh_upload_button()
            return
        self.current_queue_id = next_item["id"]
        next_item["status"] = "Sending"
        self._refresh_queue_tree()
        self.queue_tree.selection_set(str(next_item["id"]))
        self.upload_progress["value"] = 0
        self.client.upload_file_async(next_item["path"], next_item["folder"], upload_id=next_item["id"])


    # -----------------------------
    # Event / status handling
    # -----------------------------

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
                elif event_type == "upload_complete":
                    self.last_upload_info = payload
                    self._update_last_sent_label()
                    upload_id = payload.get("upload_id") if isinstance(payload, dict) else None
                    self._set_queue_item_status(upload_id, "Done")
                    if self.queue_running and upload_id == self.current_queue_id:
                        self.current_queue_id = None
                        self.root.after(250, self._start_next_queue_item)
                elif event_type == "upload_failed":
                    upload_id = payload.get("upload_id") if isinstance(payload, dict) else None
                    reason = payload.get("reason", "Upload failed") if isinstance(payload, dict) else "Upload failed"
                    self._set_queue_item_status(upload_id, "Failed")
                    if self.queue_running and upload_id == self.current_queue_id:
                        self.queue_running = False
                        self.current_queue_id = None
                        self.log(f"Queue stopped after failure: {reason}")
        except queue.Empty:
            pass
        self.root.after(100, self._poll_events)

    def update_status(self, st: MassoStatus) -> None:
        self.current_status = st
        state = st.state_text() if st.connected else "Not connected"
        self.status_banner.configure(text=state.upper(), bg=self._state_color(st))

        allowed, reason = st.upload_allowed()
        if self.upload_busy:
            upload_text = "UPLOAD IN PROGRESS"
            upload_bg = self.BLUE
        elif allowed:
            upload_text = "UPLOAD READY"
            upload_bg = self.READY
        else:
            upload_text = f"UPLOAD LOCKED - {reason}".upper()
            upload_bg = self.DISABLED if not st.faulted and not st.running else self._state_color(st)
        self.upload_allowed_banner.configure(text=upload_text, bg=upload_bg)

        self.progress_var.set(f"{st.progress}%")
        self.progress_bar["value"] = st.progress
        self.file_var.set(st.filename or "(none)")
        self.line_var.set(str(st.line_number))
        self.job_var.set(str(st.job_count))
        self._refresh_upload_button()

    def _state_color(self, st: MassoStatus) -> str:
        if not st.connected:
            return self.DISABLED
        if st.faulted or st.breakaway:
            return self.FAULT
        if st.prompt_waiting:
            return self.RUNNING
        if st.running:
            return self.RUNNING
        return self.READY

    def _refresh_upload_button(self) -> None:
        allowed, _reason = self.current_status.upload_allowed()
        queue_has_pending = any(item.get("status") in ("Pending", "Failed") for item in self.upload_queue)
        target_ok = True
        if hasattr(self, "target_preview_var"):
            target_ok, _errors, _warnings, _preview = self._target_check_for_item()
        if allowed and queue_has_pending and target_ok and not self.upload_busy and not self.queue_running:
            self.send_btn.configure(state="normal", bg=self.BLUE, cursor="hand2")
        else:
            self.send_btn.configure(state="disabled", bg=self.DISABLED, cursor="arrow")

    def _tick_last_sent(self) -> None:
        self._update_last_sent_label()
        self.root.after(1000, self._tick_last_sent)

    def _update_last_sent_label(self) -> None:
        info = self.last_upload_info
        if not info:
            self.last_sent_var.set("No successful upload yet")
            return
        filename = info.get("filename", "(unknown)")
        folder = info.get("folder", "\\")
        size = int(info.get("size", 0))
        ts = float(info.get("timestamp", time.time()))
        elapsed = max(0, int(time.time() - ts))
        if elapsed < 60:
            ago = f"{elapsed} seconds ago"
        else:
            mins, secs = divmod(elapsed, 60)
            if mins < 60:
                ago = f"{mins} minutes, {secs:02d} seconds ago"
            else:
                hours, mins = divmod(mins, 60)
                ago = f"{hours} hours, {mins:02d} minutes ago"
        self.last_sent_var.set(f"{filename}  →  {folder}  •  {size:,} bytes  •  sent {ago}")

    def log(self, msg: str) -> None:
        timestamp = time.strftime("%H:%M:%S")
        self.log_text.insert("end", f"[{timestamp}] {msg}\n")
        self.log_text.see("end")

    def on_close(self) -> None:
        self.client.stop()
        self.root.destroy()


def main() -> int:
    root = tk.Tk()
    SendGui(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
