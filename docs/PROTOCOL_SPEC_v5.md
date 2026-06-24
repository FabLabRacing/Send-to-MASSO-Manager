# MASSO Link UDP Protocol Specification

**⚠️ WORK IN PROGRESS - INCOMPLETE DOCUMENTATION**

**Note**: This protocol specification is based on reverse engineering, packet captures, and controller testing. It includes confirmed upload behavior from SendToMasso V1.7.12+ and the current Send-to-MASSO Manager V1.8.5 RC line. Many packet fields and status bytes are still not fully understood. Treat this as a working document, not official MASSO documentation.

---

This document describes the observed MASSO controller UDP protocol used by MASSO Link and compatible clients.

## Overview

- **Transport**: UDP
- **Controller IP**: User-specified
- **Controller Port**: UDP `65535`
- **Client Ports / Sockets**:
  - RX/status socket: bind to UDP `11000-11050` (commonly `11000`).
  - TX/upload socket: MASSO Link behavior has been observed using an ephemeral source port.
  - SendToMasso / Send-to-MASSO Manager V1.7.10+ refreshes/recreates the TX socket before each upload. This gives each upload a fresh ephemeral source port and fixed a repeat-upload failure where the first upload after connect worked, but later uploads failed until reconnect.
  - Keep listening for ACKs on both the documented RX socket and the TX socket, because observed replies can arrive on either path depending on controller/network behavior.
- **Packet Structure**:

```text
[CRC16-CCITT 2 bytes][Magic 0x03 0x00 2 bytes][Type 1 byte][Payload...]
```

- **Checksum Input**: all bytes after the checksum field, starting with `0x03 0x00`.

## Packet Types

### Discovery / Version Request (Type `0x02`)

#### Request

Total packet length: 10 bytes including checksum.

```text
[CRC16][03 00][02][f8 2a 00 00 ??]
```

Known/observed payload:

```text
f8 2a 00 00 0b
```

In later MASSO Link captures, the final byte matched the current month, for example:

```text
f8 2a 00 00 06
```

where `0x06` was observed during June testing. This final byte may be a date/month field rather than a fixed constant.

#### Response

- Length: 46 bytes
- Version string starts at byte 12.
- Example strings observed/expected:
  - `@Lathe v5.09`
  - MASSO Touch / Standalone version strings may vary by firmware/core version.

---

### Configuration Request (Type `0x03`)

#### Request

Total packet length: 14 bytes including checksum.

```text
[CRC16][03 00][03][hour minute second day month year 00 00 00]
```

Earlier testing showed the controller will still respond if these fields are zeroed. However, MASSO Link appears to use these fields to set/sync the MASSO clock. Sending zeros can result in the controller clock being set to `12:00 AM`.

Observed MASSO Link example:

```text
03 00 03 0b 01 15 04 06 1a 00 00 00
```

Decoded:

```text
hour   = 0x0b = 11
minute = 0x01 = 1
second = 0x15 = 21
day    = 0x04 = 4
month  = 0x06 = 6
year   = 0x1a = 26
```

Meaning:

```text
11:01:21 on 04/06/26
```

#### Response

- Length: 10 bytes
- Type: `0x03`
- Bytes 5-6: controller serial number, little-endian.

Example extraction:

```python
serial = int.from_bytes(data[5:7], "little")
```

---

### Keepalive / Status Request (Type `0x01`)

#### Request

Total packet length: 10 bytes including checksum.

```text
[CRC16][03 00][01][hour minute second day month]
```

MASSO Link sends this roughly once per second after connection.

Observed examples:

```text
03 00 01 0b 01 15 04 06
```

Decoded:

```text
11:01:21 on day 04, month 06
```

The keepalive does not include the year byte. The year is present in the configuration request.

#### Response

- The controller sends 270-byte status packets.

---

### Tool Data Request (Type `0x08`)

#### Request

Total packet length: 10 bytes including checksum.

```text
[CRC16][03 00][08][tool_index][22 2c 1c 0b]
```

- `tool_index`: 1 byte, usually `1-255`.

#### Response

- Length: 38 bytes
- Type: `0x08`
- Byte 5: tool index
- Byte 6 onward: ASCII tool name, null-terminated.

#### Tool Data Scope / Caveat

The known `0x08` request/response currently documents only the observed tool index and tool name exchange.

MASSO milling/turning tool tables are believed to contain a fuller binary record with fields similar to:

```text
Tool Number
Slot
Tool Name
Z Offset
Tool Diameter
Tool Diameter Wear
```

That full six-field binary tool-table format has not yet been decoded or confirmed through captures in this document. The safe implementation scope is read-only download/export. Do not implement tool-table editing or upload until the full binary format is verified.

---

## Status Packet Structure

Status packets are 270 bytes.

Known fields:

| Byte(s) | Meaning | Notes |
|---:|---|---|
| 0-1 | CRC/checksum | Little-endian CRC16-CCITT |
| 2-3 | Magic | `03 00` |
| 4 | Packet type | Often `0x01` for status |
| 5 | Job progress percentage | `0-100`, e.g. `0x64 = 100%` |
| 6 | Execution active flag | `0x00 = not running`, `0x02 = running` |
| 7 | Fault/status code | `0xFF = normal/no fault observed`, `0x15 = torch breakaway observed` |
| 8-11 | Job count | Little-endian integer |
| 12 | User prompt / tool-change waiting flag | `0x01 = normal`, `0x00 = waiting for user input` |
| 13 | Line number | Single byte, wraps at 255 |
| 14-16 | Reserved/unknown | Usually `00 00 00` in observed captures |
| 17-80 | Current/last file path/name | ASCII, null-terminated |
| 81-269 | Unused/padding/unknown | Usually zero in normal operation |

### Byte 5 - Job Progress

- Decimal `0-100`.
- During idle/stopped, it may remain at the last completed value, often `100`.
- During running, MASSO Link displays this as the running percentage.

### Byte 6 - Execution Active Flag

Observed values:

```text
0x00 = not running / stopped / idle
0x02 = actively running
```

A brief `0x00` blip was observed during a running capture, so applications should debounce before re-enabling upload.

Recommended upload-enable logic:

```text
Upload allowed only when:
- connected
- byte 6 == 0x00
- byte 7 == 0xFF
- stopped state has been stable for ~1.5 seconds
- no upload is already active
```

### Byte 7 - Fault / Status Code

Earlier notes listed byte 7 as fixed `0xFF`, but breakaway testing disproved that.

Observed values:

```text
0xFF = normal / no fault observed
0x15 = torch breakaway active
```

This byte should be treated as a status/fault/alarm code field. Additional codes are unknown.

### Byte 12 - User Prompt / Tool Change

Observed/working values:

```text
0x01 = normal operation
0x00 = machine paused, waiting for user input, such as M6/manual tool change
```

### Byte 13 - Line Number

- Single-byte line/index value.
- Can wrap at 255.
- Useful for activity detection, but not a full G-code line number.

---

## Feed Hold Detection

Feed hold is not fully decoded yet.

Existing client logic infers feed hold when:

1. Execution active flag is running (`byte 6 == 0x02`)
2. Line number has not changed for ~1.5 seconds
3. Line number is greater than 0

MASSO Link appears able to display “Feed Hold,” but it is not yet confirmed whether it uses this same inference or a separate status byte.

---

## File Upload - Start Upload (Type `0x0A`)

There appear to be at least two start-upload formats.

### Simple / Short Start Upload Format

Earlier observed/documented format:

- Total packet length: 30 bytes including checksum.
- Payload length after checksum: 28 bytes.

```text
[CRC16][03 00][0A][file_size 4 LE][00 00 01][5c 00][filename NUL][padding]
```

Notes:

- This form appears to be limited by the fixed packet size.
- Earlier client implementations assumed a 15-character filename limit.
- This may be a simplified/root-upload format.

### Folder-Aware Start Upload Format

MASSO Link captures showed a longer folder-aware start-upload packet.

Observed total packet lengths include:

```text
38 bytes
50 bytes
```

This format carries folder path and filename separately and supports paths such as:

```text
\Test\
```

and filenames longer than the original assumed 15-character limit.

Known-good observed filename:

```text
18_Inch__CLAD.nc
```

This is 16 characters including `.nc`, so the 15-character limit is not universal.

#### Folder-Aware Payload Alignment

MASSO Link start-upload packets observed during long-name testing had payload lengths after the CRC that landed on a 4-byte boundary. Examples:

```text
38-byte UDP payload  => 36-byte payload after CRC
50-byte UDP payload  => 48-byte payload after CRC
62-byte UDP payload  => 60-byte payload after CRC
```

SendToMasso V1.7.9+ uses this rule for longer folder-aware start-upload packets:

```python
pad_len = (-len(start_payload_after_crc)) % 4
```

The shorter known-good form still pads to a 48-byte payload after CRC, producing a 50-byte UDP payload.

### Start Upload Response

Response length: 10 bytes.

Type: `0x0A`.

The response type alone is not enough to determine success. Byte 5 appears to be the most stable accepted/rejected discriminator. Earlier testing treated bytes 5-6 as a two-byte status code, but work-controller captures showed a successful start-upload ACK of `00 44`. Retrying after treating that packet as failure caused the controller to reject the duplicate start.

Observed accepted ACK forms include:

```text
bytes 5-6 = 00 00  => start upload accepted
bytes 5-6 = 00 44  => start upload accepted on work controller / hotspot test
```

Observed rejected/failure form:

```text
bytes 5-6 = f7 00  => start upload rejected / failed
```

Example accepted ACKs:

```text
?? ?? 03 00 0a 00 00 ff ?? ??
?? ?? 03 00 0a 00 44 00 00 33
```

Recommended logic:

```python
start_ok = (ack[4] == 0x0A and ack[5] == 0x00)
start_rejected = (ack[4] == 0x0A and ack[5] == 0xF7)
```

---

## File Upload - Data Chunk (Type `0x0B`)

### Full Chunk Packet

Normal full chunks use 1422 bytes of data.

Total UDP payload length:

```text
1438 bytes
```

Structure:

```text
[CRC16 2]
[03 00 2]
[0B 1]
[chunk_index 4 LE]
[chunk_length 4 LE]
[data 1422]
[pad 3]
```

For full chunks:

```text
chunk_length = 1422
trailing pad = 3 bytes
total length = 2 + 2 + 1 + 4 + 4 + 1422 + 3 = 1438
```

### Final Short Chunk Packet

Successful MASSO Link and SendToMasso captures confirmed that the final short chunk is formatted differently than earlier notes.

For a final chunk shorter than 1422 bytes:

```text
chunk_length = actual remaining byte count
data = actual remaining bytes
trailing pad = 3 bytes if the final chunk length is even
trailing pad = 4 bytes if the final chunk length is odd
```

The goal appears to be that the total UDP payload length is always even.

Packet length formula:

```text
13-byte header + final_chunk_length + trailer
```

Where the 13-byte data packet header is:

```text
2 checksum
2 magic
1 type
4 chunk index
4 chunk length
= 13 bytes
```

Working rule:

```python
trailer = 3 if final_chunk_length % 2 == 0 else 4
```

Confirmed examples:

```text
File size: 49,369 bytes
Full chunks: 34
Final chunk index: 34
Final chunk length: 1021  # odd
Trailer: 4 bytes
Final UDP payload length: 13 + 1021 + 4 = 1038
Result: upload accepted
```

```text
File size: 60,630 bytes
Full chunks: 42
Final chunk index: 42
Final chunk length: 906  # even
Trailer: 3 bytes
Final UDP payload length: 13 + 906 + 3 = 922
Result: upload accepted
```

Failed tests showed that using the wrong trailer length on the final short chunk causes MASSO to ignore the final packet and not send the final data ACK.

### Final Short Chunk Compatibility Fallback

A work-controller / Windows-hotspot test exposed another compatibility case. The controller accepted the start-upload packet, but ignored a short final data packet. This happened both for a 411-byte file where the first chunk was also the final chunk and for the final chunk of a larger 180,499-byte file.

Working fallback:

```text
- keep chunk_length field = actual remaining byte count
- pad the wire data area out to 1422 bytes
- append the normal 3-byte full-chunk trailer
- total UDP payload length = 1438 bytes
```

In other words, the fallback packet looks like a normal full-size data packet on the wire, but the length field still tells MASSO how many bytes are real file data.

Recommended robust behavior:

```text
For short final chunks:
1. Try the compact short-final packet with the 3/4-byte trailer rule.
2. If no 0x0B ACK is received, retry the same chunk using full-wire-real-length format.
```

Observed proof from work testing:

```text
411-byte file:
short-final packet length 428  => no ACK
full-wire-real-length length 1438 => ACK received, upload complete
```

### Data Chunk Response

Response length: 10 bytes.

Type: `0x0B`.

The ACK appears to contain the next expected chunk number.

Observed behavior:

```text
Send chunk 0  -> ACK indicates 1
Send chunk 1  -> ACK indicates 2
Send chunk 34 -> ACK indicates 35
```

Example final ACK:

```text
45 a7 03 00 0b 00 23 00 00 00
```

`0x23` is decimal `35`, indicating MASSO accepted chunk 34 and advanced to expected chunk 35.

Working interpretation:

```python
ack_next_chunk = int.from_bytes(ack[5:7], "big")
expected_next = chunk_index + 1
chunk_ok = (ack[4] == 0x0B and ack_next_chunk == expected_next)
```

This byte order should be verified with more captures, but it matched observed chunk ACKs.

---

## File Upload Process

Recommended process:

1. Connect to controller.
2. Confirm status packets are being received.
3. Do not upload while machine is running or faulted.
4. Send start-upload packet with folder/path, filename, and file size.
5. Wait for type `0x0A` ACK.
6. Confirm start ACK by checking `ack[4] == 0x0A` and `ack[5] == 0x00`. Do not require byte 6 to also be zero.
7. Send full data chunks:
   - chunk length field = `1422`
   - 1422 data bytes
   - 3 trailing pad bytes
8. Send final short chunk:
   - first try compact short-final format
   - chunk length field = actual remaining bytes
   - actual remaining bytes of data
   - 3 trailing pad bytes if the final chunk length is even
   - 4 trailing pad bytes if the final chunk length is odd
9. If the final short chunk receives no ACK, retry the same final chunk as full-wire-real-length:
   - chunk length field = actual remaining bytes
   - data area padded to 1422 bytes
   - 3 trailing pad bytes
10. Wait for type `0x0B` ACK after each chunk.
11. Confirm ACK advances to the next expected chunk number.

---

## Filename and Folder Notes

Observed / tested behavior:

- ASCII filenames and folders work.
- Non-ASCII names should be blocked. Example: `café.tap` failed.
- `part#12.tap` worked, so `#` should not be blocked.
- Backslash `\` is the MASSO folder separator.
- Forward slash `/` in the UI can be accepted and normalized to `\`.
- Nested folders work. Confirmed example:

```text
\aaa\bbbb\ccccccc\ddddddddddddd\eeeeeeeee\ffffff\ggggggg\Clean_flag_with_longer_nameasaaaaaabbb.tap
```

- Missing folders appear to be created automatically during upload.
- Existing target files are overwritten by upload.
- The earlier 15-character filename limit applies only to the short fixed-size start-upload assumption and should not be treated as universal.
- A short 39-character path/name limit assumption was disproven by later tests.

Known MASSO file extensions:

```text
.nc   native / preferred
.cnc
.tap
.eia
.txt
```

Recommended UI validation:

- Show a MASSO target preview after normalizing `/` to `\`.
- Block non-ASCII target names.
- Block these Windows-style invalid filename characters inside file/folder names:

```text
: * ? " < > |
```

- Do not block `#`; it has been tested successfully.
- Warn, but do not necessarily block, if the file extension is not one of `.nc`, `.cnc`, `.tap`, `.eia`, or `.txt`.
- Do not show a normal path-length warning unless future testing finds a real controller limit.

---

## QR Code Generation - Non-UDP App Feature

QR-code generation is not part of the UDP upload protocol. It is an app-side helper that creates MASSO-compatible QR-code images for loading a G-code file from the MASSO screen.

Observed / documented MASSO QR payload format:

```text
^CSLG<path-to-gcode-file>^CE
```

Where:

```text
^CS = command start
LG  = load G-code
^CE = command end
```

Send-to-MASSO Manager builds the QR payload from the same MASSO target preview used for upload.

Example displayed MASSO target:

```text
\Test\part#12.tap
```

Current app QR payload:

```text
^CSLGTest\part#12.tap^CE
```

Notes:

- The app strips the leading root backslash for QR payloads because MASSO examples do not show a leading root separator.
- Internal folder separators remain MASSO-style backslashes.
- QR output is a PNG image.
- QR-code functionality should still be verified on real MASSO hardware before being treated as fully production-proven.
- If testing shows MASSO expects the leading root backslash in some cases, make this behavior configurable or update the QR builder.

---

## Error Handling Notes

- Start-upload ACK type `0x0A` may still indicate failure; inspect byte 5.
- If start-upload byte 5 is `0x00`, treat as accepted.
- If start-upload byte 5 is `0xF7`, treat as rejected/failed and do not send chunks.
- If a chunk ACK is not received, resend the same chunk.
- MASSO Link appears to retry chunks quickly.
- The final chunk is especially sensitive to packet length/padding.
- If a short final packet receives no ACK, retry the final chunk as a full-wire-real-length packet.

---

## Checksum Calculation

CRC16-CCITT:

- Polynomial: `0x1021`
- Initial value: `0x0000`
- Input data: all bytes after checksum
- Output: little-endian 2 bytes

Example:

```python
def calculate_checksum(data: bytes) -> bytes:
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
```

---

## Implementation Notes

- Keepalive packets are normally sent about once per second.
- Refresh/recreate the TX/upload socket before each upload to avoid repeat-upload failures caused by stale UDP source-port/session behavior.
- MASSO status packets arrive frequently, around 10 packets per second in observed captures.
- The UI should disable uploads when the machine is running, faulted, or recently stopped.
- A stopped debounce of about 1.5 seconds is recommended.
- Store last successful upload time locally if showing “file sent X seconds ago”; this appears to be a UI-side MASSO Link feature, not a decoded controller timestamp.

---

## Known Observed Values

### Status Packet

| Field | Value | Meaning |
|---|---:|---|
| Byte 5 | `0x00-0x64` | Progress percent |
| Byte 6 | `0x00` | Not running |
| Byte 6 | `0x02` | Running |
| Byte 7 | `0xFF` | Normal/no fault observed |
| Byte 7 | `0x15` | Torch breakaway |
| Byte 12 | `0x01` | Normal/no user prompt |
| Byte 12 | `0x00` | Waiting for user input/tool change |

### Upload ACK

| Packet Type | Bytes 5-6 | Meaning |
|---|---|---|
| `0x0A` | `00 00` | Start upload accepted |
| `0x0A` | `00 44` | Start upload accepted, observed on work controller |
| `0x0A` | `f7 00` | Start upload rejected/failed |
| `0x0B` | next chunk | Data chunk accepted, next expected chunk |

---

## Confirmed Chunk Boundary Tests

The following file sizes were tested successfully with SendToMasso after the final-chunk trailer rule was corrected:

| File Size | Chunk Pattern | Final Remainder | Result |
|---:|---|---:|---|
| 1,421 bytes | one short final chunk | 1,421 | accepted |
| 1,422 bytes | exactly one full chunk | 0 | accepted |
| 1,423 bytes | one full chunk + one short final chunk | 1 | accepted |
| 2,844 bytes | exactly two full chunks | 0 | accepted |
| 2,845 bytes | two full chunks + one short final chunk | 1 | accepted |


Additional cross-environment tests with SendToMasso V1.7.12:

| File Size | Environment | Important Behavior | Result |
|---:|---|---|---|
| 411 bytes | Work MASSO via Windows hotspot | compact short-final ignored, full-wire-real-length fallback ACKed | accepted |
| 180,499 bytes | Work MASSO via Windows hotspot | normal full chunks ACKed; final short chunk required fallback | accepted |

This confirms handling for:

```text
- files smaller than one full chunk
- files exactly one full chunk
- files just over one full chunk
- files exactly multiple full chunks
- files just over multiple full chunks
- odd final remainders
- even final remainders
```


## Open Questions

- Complete mapping of byte 7 fault/status codes.
- Feed hold: inferred from stalled line number or represented by a dedicated byte?
- Exact meaning of every field in folder-aware start-upload packet.
- Whether MASSO officially expects client TX to be bound to `11000-11050`, or whether MASSO Link's ephemeral TX source port is valid/intentional.
- Whether the discovery packet final byte is always the current month.
- True maximum filename/path length, if any, for folder-aware upload. Long nested paths have been confirmed, but no hard controller limit has been found yet.
- Whether remote directory listing, delete, rename, or browse packets exist.
- Whether QR payloads should ever include the leading root backslash.
- Full six-field binary tool-table format and whether MASSO Link uses another packet or file-transfer mechanism to export it.

---
