# Changelog

This project is still in release-candidate testing. Version notes below focus on user-visible behavior and protocol fixes, not every internal cleanup commit.

## v1.8.20 RC - in testing

- Corrected file-data ACK decoding to use bytes 6-7 as a little-endian 16-bit next-expected index.
- Removed the v1.8.18 modulo-256 ACK workaround. It restored large-file uploads, but later captures showed the apparent rollover was caused by reading the wrong byte offset/order.
- Corrected compact final-packet padding so the payload after the CRC is a multiple of four bytes.
- The old odd/even trailer rule was incomplete; final lengths congruent to 3 modulo 4 (including the historical 411-byte and 180,499-byte cases) were padded incorrectly.
- Retained the full-wire-real-length final-packet fallback as a compatibility/recovery path.
- Changed keepalive and Tool Data request time fields to reuse a connection-time snapshot, matching newer MASSO Link capture analysis.
- Updated protocol documentation and regression-test targets to reflect the corrected interpretation.

## v1.8.19 RC - in testing

- Fixed queue target-folder behavior.
- Changing the target MASSO folder after files are already in the queue now updates Pending and Failed queue items.
- Sending and Done queue items are left unchanged so the queue still shows where already-sent files actually went.
- Cleaned up repository documentation around install, testing, reporting, and protocol notes.

## v1.8.18 RC

- Restored larger-file uploads that were failing at the apparent 255/256 ACK boundary.
- Before this fix, a roughly 929 KB G-code file failed consistently at that point.
- A modulo-256 ACK workaround was added and outside testing then reported successful uploads of more than 10 files ranging from about 2 KB to 1200 KB.
- Later v1.8.20 analysis showed MASSO was not actually rolling an 8-bit counter; the app had been reading the wrong ACK byte offset/order. v1.8.20 replaces the workaround with the corrected 16-bit little-endian decoder.

## v1.8.17 RC

- Added named MASSO alarm/fault display for confirmed status codes.
- Known alarms include X/Y/Z/A/B Motor Alarm, Spindle Alarm, Air Pressure Low Alarm, Lubricant Low Alarm, and Torch Breakaway.
- Uploads remain blocked for any non-normal alarm/fault code.
- Unknown future codes display as `Fault / Alarm 0x??` and are still treated as unsafe.

## v1.8.16 RC

- Improved Tools Data workflow.
- **Get Tools Data** downloads MASSO tool slots 1-118.
- The app automatically generates and opens a MASSO-style text file after tool data is downloaded.
- Tool export is read-only and currently includes tool number plus tool name.
- **Generate Text File** remains as a manual fallback.

## v1.8.8 through v1.8.15 RC

- Added and refined the scrollable HMI-style main window.
- Improved Tools Data export and open-after-save behavior.
- Continued cleanup of queue/status UI behavior.

## v1.8.6 RC

- Corrected status bytes 13-16 to elapsed run time in seconds.
- Removed earlier line-number/feed-hold assumptions based on that field.
- Updated the UI label from line-number style wording to elapsed-time wording.

## v1.8.3 through v1.8.5 RC

- Added optional auto-clear of the queue after successful queue upload.
- Improved queue and status panel layout.
- Removed noisy raw/last-packet display from the normal UI.

## v1.8.1 through v1.8.2 RC

- Added QR-code generation for selected files and queue items.
- Added custom logo support.

## v1.8.0 RC

- Added batch/queue upload workflow.
- Settings and profiles stored beside the program for portable ZIP-style use.
- Improved status display and queue controls.

## v1.7.x development line

- Added folder-aware uploads.
- Added stronger filename/folder validation.
- Fixed repeat-upload behavior by recreating the upload socket for each file.
- Added compatibility handling for short final file transfers.
- Confirmed boundary behavior around very small files and files around one full upload block.

## Earlier development

- Basic UDP connection/status handling.
- Initial G-code upload sequence.
- Initial profile/config support.
- Protocol work based on Andrew's `masso-link-protocol-client` project plus additional packet captures and controller testing.
