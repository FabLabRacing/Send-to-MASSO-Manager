# MASSO UDP Protocol Notes

**Work in progress.** These notes are based on reverse engineering, packet captures, and real-controller testing. They are not official MASSO documentation.

This file is the canonical protocol note for this repository. Older versioned protocol files were kept during development, but this document should be updated going forward.

## Scope

These notes describe the observed UDP protocol used by MASSO Link and compatible clients such as Send-to-MASSO Manager.

Confirmed implementation line covered here:

```text
Send-to-MASSO Manager v1.8.19 RC development/test line
```

Major confirmed behavior includes:

- Basic connection/handshake.
- 270-byte status packets.
- Folder-aware G-code upload.
- Short-final upload fallback behavior.
- Large-file upload ACK rollover behavior.
- Read-only Tools Data request/export.
- QR-code payload generation as an app-side helper.

## Transport overview

```text
Transport:        UDP
Controller port:  65535
Client RX ports:  11000-11050, commonly 11000
```

Observed client/socket behavior:

- MASSO Link uses a receive/status socket bound to the 11000 range.
- MASSO Link has been observed sending uploads from an ephemeral source port.
- Send-to-MASSO Manager keeps the RX/status socket bound and recreates the TX/upload socket before each upload.
- Recreating the TX socket before each upload fixed repeat-upload failures where the first upload after connect worked but later uploads failed until reconnect.
- Clients should listen for ACKs on both the RX socket and TX socket because replies have been observed on both paths.

## Packet wrapper

Observed packet structure:

```text
[CRC16 2 bytes][Magic 03 00 2 bytes][Type 1 byte][Payload...]
```

Checksum:

- CRC16-CCITT.
- Polynomial: `0x1021`.
- Initial value: `0x0000`.
- Input data: all bytes after the checksum field, starting with `03 00`.
- Output stored little-endian.

Python reference:

```python
def crc16_ccitt_le(data: bytes) -> bytes:
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

## Packet types

Known packet types:

| Type | Meaning |
|---:|---|
| `0x01` | Keepalive/status |
| `0x02` | Discovery/version request |
| `0x03` | Configuration/serial response |
| `0x08` | Tool Data request/response |
| `0x0A` | Start file upload |
| `0x0B` | File data transfer |

## Discovery / version request - type `0x02`

Request length: 10 bytes including checksum.

Observed payload after checksum:

```text
03 00 02 f8 2a 00 00 ??
```

Earlier notes used a fixed final byte such as:

```text
03 00 02 f8 2a 00 00 0b
```

Later MASSO Link captures showed the final byte matching the current month, for example `0x06` during June testing. It is likely date/month related rather than a true fixed constant.

Response:

- Length observed: 46 bytes.
- Version string starts around byte 12.
- Version string varies by controller/firmware.

## Configuration request - type `0x03`

Request length: 14 bytes including checksum.

Observed payload after checksum:

```text
03 00 03 hour minute second day month year 00 00 00
```

MASSO Link appears to send local PC time. Sending zeros can set the MASSO clock to `12:00 AM`, so compatible clients should send real local time fields.

Observed example:

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

Response:

- Length observed: 10 bytes.
- Type: `0x03`.
- Bytes 5-6: controller serial number, little-endian.

Reference:

```python
serial = int.from_bytes(data[5:7], "little")
```

## Keepalive / status request - type `0x01`

Request length: 10 bytes including checksum.

Observed payload after checksum:

```text
03 00 01 hour minute second day month
```

MASSO Link sends this repeatedly after connection, roughly once per second in observed captures.

Response:

- 270-byte status packet.

## Status packet structure

Status packets are 270 bytes.

Known fields:

| Byte(s) | Meaning | Notes |
|---:|---|---|
| 0-1 | CRC/checksum | Little-endian CRC16-CCITT |
| 2-3 | Magic | `03 00` |
| 4 | Packet type | Usually `0x01` for status |
| 5 | Job progress percentage | `0-100` |
| 6 | Execution active flag | `0x00 = not running`, `0x02 = running/active` |
| 7 | Fault/alarm code | `0xFF = normal/no fault`; other values are unsafe |
| 8-11 | Job count | Little-endian integer |
| 12 | User prompt/tool-change flag | `0x01 = normal`, `0x00 = waiting for user input` |
| 13-16 | Elapsed run time | Little-endian uint32 seconds |
| 17-80 | Current/last file path/name | ASCII, null-terminated |
| 81-269 | Unknown/padding | Usually zero in normal operation |

### Byte 5 - progress

- Decimal `0-100`.
- During idle/stopped, it may remain at the last completed value, often `100`.

### Byte 6 - execution active flag

Observed values:

```text
0x00 = not running / stopped / idle
0x02 = active / running
```

Important nuance: during a running job, short `0x00` windows have been observed, likely during rapid/non-cutting/non-feed movement. Do not treat one idle-looking packet as proof the program has ended.

Recommended upload-enable logic:

```text
Upload allowed only when:
- connected
- byte 6 has been stopped/idle long enough to be stable
- byte 7 indicates no fault
- byte 12 indicates no user prompt/tool-change wait
- no upload is already active
```

Send-to-MASSO Manager currently uses a stopped debounce of about 1.5 seconds.

### Byte 7 - fault / alarm code

When byte 7 is not `0xFF`, clients should treat the machine as unsafe for upload.

Confirmed values:

```text
0xFF = normal / no fault

0x00 = X Motor Alarm
0x01 = Y Motor Alarm
0x02 = Z Motor Alarm
0x03 = A Motor Alarm
0x04 = B Motor Alarm

0x05 = Spindle Alarm
0x06 = Air Pressure Low Alarm
0x14 = Lubricant Low Alarm
0x15 = Torch Breakaway
```

Notes:

- MASSO Link displayed axis alarms as `<axis> Motor Alarm` in observed comparisons.
- Air Pressure Low Alarm produced byte `0x06`; MASSO Link displayed `Machine Stopped` in the observed test, but the byte still distinguishes the condition.
- Unknown non-`0xFF` values should still be treated as faults and block uploads.

### Byte 12 - user prompt/tool-change flag

Observed values:

```text
0x01 = normal
0x00 = waiting for user input / tool change
```

Clients should block uploads while MASSO is waiting for user input.

### Bytes 13-16 - elapsed run time

These bytes are elapsed run time in seconds, little-endian uint32.

Confirmed examples:

```text
3:29 elapsed = 209 seconds = d1 00 00 00
255 seconds  = ff 00 00 00
256 seconds  = 00 01 00 00
285 seconds  = 1d 01 00 00
```

Earlier line-number/feed-hold assumptions based on this field were wrong and should not be used.

## File upload - start upload, type `0x0A`

There appear to be at least two start-upload formats.

### Short/root-oriented format

Earlier observed/documented fixed-size format:

```text
[CRC16][03 00][0A][file_size 4 LE][00 00 01][5c 00][filename NUL][padding]
```

This format led earlier clients to assume a short filename limit. Later folder-aware captures disproved that as a universal limit.

### Folder-aware format

MASSO Link captures showed longer folder-aware start-upload packets that carry folder path and filename separately.

Observed total UDP payload lengths include:

```text
38 bytes
50 bytes
62 bytes
```

Payload after the CRC lands on a 4-byte boundary:

```text
38-byte UDP payload -> 36 bytes after CRC
50-byte UDP payload -> 48 bytes after CRC
62-byte UDP payload -> 60 bytes after CRC
```

Working alignment rule:

```python
pad_len = (-len(start_payload_after_crc)) % 4
```

Observed behavior:

- Longer filenames work.
- Nested folders work.
- Missing folders appear to be created automatically.
- Existing target files are overwritten by upload.

### Start upload response

Response length: 10 bytes.

Type: `0x0A`.

The response type alone is not enough to determine success. Byte 5 is the most stable accepted/rejected discriminator found so far.

Observed accepted forms:

```text
bytes 5-6 = 00 00
bytes 5-6 = 00 44
```

Observed rejected/failure form:

```text
bytes 5-6 = f7 00
```

Recommended logic:

```python
start_ok = (ack[4] == 0x0A and ack[5] == 0x00)
start_rejected = (ack[4] == 0x0A and ack[5] == 0xF7)
```

Do not require byte 6 to be zero for start-upload success.

## File upload - data transfer, type `0x0B`

### Full data packet

Normal full data packets carry 1422 bytes of file data.

Total UDP payload length:

```text
1438 bytes
```

Structure:

```text
[CRC16 2]
[03 00 2]
[0B 1]
[file data index 4 LE]
[file data length 4 LE]
[data 1422]
[pad 3]
```

For a full data packet:

```text
file data length = 1422
total payload length = 2 + 2 + 1 + 4 + 4 + 1422 + 3 = 1438
```

### Final short data packet

For the final transfer when fewer than 1422 bytes remain:

```text
file data length = actual remaining byte count
data = actual remaining bytes
trailing pad = 3 bytes if the final data length is even
trailing pad = 4 bytes if the final data length is odd
```

The goal appears to be an even total UDP payload length.

Formula:

```text
13-byte header + final_data_length + trailer
```

Where the 13-byte header is:

```text
2 checksum
2 magic
1 type
4 file data index
4 file data length
```

Working rule:

```python
trailer = 3 if final_data_length % 2 == 0 else 4
```

Confirmed examples:

```text
49,369 byte file:
final data length = 1021, odd
trailer = 4
total final UDP payload = 13 + 1021 + 4 = 1038
accepted
```

```text
60,630 byte file:
final data length = 906, even
trailer = 3
total final UDP payload = 13 + 906 + 3 = 922
accepted
```

### Final short compatibility fallback

A work-controller / Windows-hotspot test exposed a compatibility case where MASSO accepted the start-upload packet but ignored a compact short final data packet.

Observed cases:

```text
411-byte file:
compact short-final packet length 428 -> no ACK
full-wire-real-length packet length 1438 -> ACK received, upload complete
```

```text
180,499-byte file:
normal full data packets ACKed
final short packet required full-wire-real-length fallback
```

Working fallback:

```text
- keep file data length field = actual remaining byte count
- pad the wire data area out to 1422 bytes
- append the normal 3-byte full-packet trailer
- total UDP payload length = 1438 bytes
```

Recommended behavior:

```text
For final short transfers:
1. Try compact short-final format.
2. If no 0x0B ACK is received, retry the same final transfer using full-wire-real-length format.
```

## Data ACK behavior - type `0x0B`

Response length: 10 bytes.

Type: `0x0B`.

For small files, bytes 5-6 appear to contain the next expected file-data number in big-endian order:

```text
Send data 0  -> ACK indicates 1
Send data 1  -> ACK indicates 2
Send data 34 -> ACK indicates 35
```

Example:

```text
45 a7 03 00 0b 00 23 00 00 00
```

`0x23` is decimal 35, indicating the controller accepted data number 34 and advanced to expected number 35.

### Large-file ACK rollover

Important correction from v1.8.18 testing:

The ACK field occupies two bytes, but the observed behavior rolls over after 255. Treat the ACK as a modulo-256 confirmation once the expected next value is 256 or greater.

Observed pattern:

```text
expected next = 255 -> ACK 255
expected next = 256 -> ACK 0
expected next = 257 -> ACK 1
expected next = 258 -> ACK 2
```

Recommended logic:

```python
ack_next = int.from_bytes(ack[5:7], "big")
expected_next = file_data_index + 1

ack_matches = (
    ack_next == expected_next
    or (
        expected_next >= 256
        and ack_next == (expected_next & 0xFF)
    )
)
```

This is a protocol-interpretation fix, not a retry/timeout workaround.

The file data packet still carries the full 4-byte file data index. The ACK appears to act as a rolling low-byte confirmation token for larger files.

Known test evidence:

- A roughly 929 KB file failed consistently at the rollover point before the fix.
- After the fix, outside testing reported more than 10 successful uploads from about 2 KB to 1200 KB.
- Additional local testing confirmed the same fix on another MASSO controller.

## Recommended upload process

1. Connect to the controller.
2. Confirm status packets are being received.
3. Do not upload while the machine is running, faulted, or waiting for user input.
4. Send start-upload packet with folder/path, filename, and file size.
5. Wait for type `0x0A` ACK.
6. Accept start upload when `ack[4] == 0x0A` and `ack[5] == 0x00`.
7. Send full 1422-byte data packets.
8. For a final short transfer, first try compact short-final format.
9. If the compact short-final packet receives no ACK, retry it as full-wire-real-length.
10. Wait for type `0x0B` ACK after each data packet.
11. For data ACKs, accept either the full expected value or the modulo-256 rollover value once expected is 256 or greater.

## Filename and folder behavior

Observed/tested:

- ASCII filenames and folders work.
- Non-ASCII names should be blocked. Example: `café.tap` failed.
- `part#12.tap` worked, so `#` should not be blocked.
- Backslash `\` is the MASSO folder separator.
- Forward slash `/` can be accepted in a UI and normalized to `\`.
- Nested folders work.
- Missing folders appear to be created automatically during upload.
- Existing target files are overwritten by upload.
- The earlier 15-character filename limit was an artifact of the short fixed-size upload assumption and is not universal.
- A later 39-character path/name limit assumption was also disproven by long nested path testing.

Known useful extensions:

```text
.nc
.cnc
.tap
.eia
.txt
```

Recommended UI validation:

- Show a MASSO target preview after normalizing `/` to `\`.
- Block non-ASCII target names.
- Block control characters.
- Block these filename/folder characters:

```text
: * ? " < > |
```

- Do not block `#`.
- Warn, but do not necessarily block, if the extension is not one of `.nc`, `.cnc`, `.tap`, `.eia`, or `.txt`.
- Do not show a normal path-length warning unless future testing finds a real controller limit.

## Tools Data - type `0x08`

### Request

Request length: 10 bytes including checksum.

Observed structure:

```text
[CRC16][03 00][08][tool_index][time/date-ish 4 bytes]
```

Earlier examples used a suffix such as:

```text
22 2c 1c 0b
```

Later captures showed different suffix bytes, so the suffix should not be treated as fixed tool data. It is likely time/session/date related.

### Response

Response length: 38 bytes.

Known fields:

| Byte(s) | Meaning |
|---:|---|
| 4 | Type `0x08` |
| 5 | Tool number/index |
| 6 onward | Tool name ASCII, null-terminated |

Parser rule:

```python
name = data[6:].split(b"\x00", 1)[0].decode("ascii", errors="ignore").strip()
```

Do not parse bytes after the first NUL as part of the name. Empty tool responses may contain leftover/stale-looking bytes after the first NUL.

Observed scope:

- Send-to-MASSO Manager requests tool slots 1 through 118.
- Slots 1-104 are user tool slots.
- Slots 105-110 appear reserved/hidden/unused in observed testing.
- Slots 111-118 are factory-defined MASSO process/tool entries.

Known factory entries:

```text
111 Laser Engraving/Cutting
112 Plasma Torch
113 Oxy Torch
114 WaterJet
115 Scribe Tool
116 Pen 1
117 Pen 2
118 Camera
```

Current safe implementation scope:

- Read-only request/export.
- Export tool number plus tool name.
- Do not implement tool-table editing/upload until the full binary format is decoded and confirmed.

## QR code generation

QR-code generation is not part of the UDP upload protocol. It is an app-side helper.

Observed/documented MASSO QR payload format:

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

Example displayed target:

```text
\Test\part#12.tap
```

Current QR payload:

```text
^CSLGTest\part#12.tap^CE
```

Notes:

- The app strips the leading root backslash for QR payloads because MASSO examples do not show a leading root separator.
- Internal folder separators remain MASSO-style backslashes.
- QR-code behavior still needs more real-controller testing.

## Confirmed upload boundary tests

Confirmed small/boundary tests:

| File size | Important behavior | Result |
|---:|---|---|
| 411 bytes | Compact short final ignored on one test controller; full-wire fallback accepted | Accepted with fallback |
| 1,421 bytes | Short final / just under full data size | Accepted |
| 1,422 bytes | Exactly one full data packet | Accepted |
| 1,423 bytes | Full data packet plus 1-byte final | Accepted |
| 2,844 bytes | Exactly two full data packets | Accepted |
| 2,845 bytes | Two full data packets plus 1-byte final | Accepted |
| 180,499 bytes | Final short transfer required fallback on work controller | Accepted |
| About 929 KB | Failed at ACK rollover before v1.8.18 fix | Accepted after rollover fix |
| About 2 KB to 1200 KB | Outside tester range after v1.8.18 fix | Accepted after rollover fix |

Useful future regression targets:

```text
Very small files under 1 KB
Files around 1.4 KB
Files around 350-400 KB
Files around 700-750 KB
Files around 900-1200 KB
Large files with short/odd final transfer sizes
Mixed small/large queue uploads
```

## Error handling notes

- Start-upload ACK type `0x0A` may still indicate failure; inspect byte 5.
- If start-upload byte 5 is `0x00`, treat as accepted.
- If start-upload byte 5 is `0xF7`, treat as rejected/failed and do not send file data.
- If a data ACK is not received, resend the same data packet.
- The final short transfer is sensitive to packet length/padding.
- If a short final packet receives no ACK, retry the same final data as full-wire-real-length.
- For large files, handle ACK rollover at 256.
- If the controller is running, faulted, or waiting for user input, the app should block uploads before attempting transfer.

## Open questions

- Complete mapping of all fault/status codes, especially lathe-specific alarms.
- Feed hold: whether it has a dedicated byte/bit, is inferred another way, or is not transmitted.
- Exact meaning of every field in the folder-aware start-upload packet.
- Whether MASSO officially expects client TX to be bound to the 11000-11050 range, or whether ephemeral TX source port is valid/intentional.
- Whether the discovery packet final byte is always the current month.
- True maximum filename/path length, if any, for folder-aware uploads.
- Whether remote directory listing, delete, rename, or browse packets exist.
- Whether time remaining is transmitted anywhere in the 270-byte status packet.
- Whether QR payloads should ever include the leading root backslash.
- Full binary tool-table format beyond tool number/name.

## Implementation guidance

For compatible clients:

- Send real local time in config/keepalive fields.
- Maintain a receive socket bound to the 11000-11050 range.
- Use a fresh ephemeral TX/upload socket per upload unless further testing proves another method equally reliable.
- Listen for ACKs on both RX and TX paths.
- Treat byte 6 idle/running transitions conservatively and debounce stopped state.
- Treat any unknown non-`0xFF` byte-7 alarm as unsafe.
- Do not infer field width only from packet byte count; verify rollover behavior.
