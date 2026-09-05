# Send-to-MASSO Manager

Send-to-MASSO Manager is an independent shop utility for sending G-code files to a MASSO controller over the network.

It was built for day-to-day CNC shop use, with a focus on reliable uploads, clear status, and a simple batch/queue workflow. It is not affiliated with or endorsed by MASSO.

## Current status

- Current development/test line: **v1.8.19 RC**.
- Primary tested platform: **Windows**.
- The Python/Tkinter source should be portable to other desktop operating systems, but non-Windows use needs more tester feedback.
- The Windows release bundle does **not** require Python.
- The protocol is reverse engineered from packet captures and real-controller testing, not official MASSO documentation.

## Main features

- Connects to a MASSO controller by IP address. A resolvable hostname may also work, but IP address is the recommended/default setup.
- Saves named MASSO connection profiles.
- Shows live machine status, including stopped/running state, progress, job count, elapsed run time, current/last file, user-prompt/tool-change state, and known alarm/fault states.
- Blocks uploads while the machine is running, faulted, waiting for user input, or not connected.
- Supports adding multiple files and sending them one at a time from a queue.
- Allows queued files to be removed, cleared, or moved up/down before sending.
- Keeps Pending/Failed queued files synced to the current target folder if the target folder is changed before sending.
- Leaves Sending/Done queue entries unchanged so the queue still shows where already-sent files actually went.
- Optional auto-clear of the queue after a successful queue upload.
- Shows the exact MASSO target path before sending.
- Accepts `/` or `\` in the target folder field and sends MASSO-style backslashes.
- Generates MASSO-compatible QR-code PNG files for selected files or the whole queue.
- Downloads MASSO Tools Data and generates a MASSO-style text file. This is read-only and currently exports tool number plus tool name.
- Uses a fresh upload socket for each file and has retry/fallback behavior for upload edge cases seen during real controller testing.
- Handles the larger-file upload ACK rollover behavior found during v1.8.18 testing.
- Supports a custom logo image in the app panel.
- Stores settings beside the program so the bundle can be kept self-contained.

## Quick start with the Windows bundle

The normal Windows release is distributed as a ZIP file, for example:

```text
send_to_masso.zip
```

To install:

1. Download the ZIP file.
2. Right-click the ZIP file and choose **Extract All...**.
3. Extract it to a normal writable folder, for example:

   ```text
   C:\SendToMASSO\
   ```

   or:

   ```text
   C:\Users\<your name>\Desktop\SendToMASSO\
   ```

4. Open the extracted folder.
5. Double-click the Send-to-MASSO `.exe` file.

Do not run the program directly from inside the ZIP file. Extract it first.

Avoid installing the portable bundle under `C:\Program Files\` unless you know what you are doing. The app stores its settings beside the program, so a normal writable folder is preferred.

## First-time setup

1. Start Send-to-MASSO Manager.
2. Enter a profile name, such as:

   ```text
   Shop MASSO
   ```

3. Enter the MASSO controller IP address.
4. Click **Save / Update Profile**.
5. Click **Connect**.
6. Wait for the Machine Status panel to show that the controller is connected.

The app saves profiles and settings in:

```text
send_to_masso.json
```

This file is stored beside the program. Keep it with the app if you move the folder to another computer.

## Sending files

1. Connect to the MASSO controller.
2. Make sure the MASSO is stopped and ready.
3. Enter the target MASSO folder.

   Examples:

   ```text
   \
   \Test\
   /Test/
   \Jobs\CustomerA\
   ```

   Forward slashes are accepted and automatically converted to backslashes.

4. Click **Add Files...**.
5. Select one or more G-code files.
6. Review the queue and the MASSO target preview.
7. Use **Move Up** or **Move Down** if the files need to be sent in a specific order.
8. Click **Send Queue**.

Files are sent one at a time. If a file fails to send, the queue stops so the problem can be checked before continuing.

### Target folder behavior

The target folder field is treated as the live target for anything not yet sent.

If you add files to the queue and then change the target folder:

- Pending files update to the new target folder.
- Failed files update to the new target folder so they can be retried somewhere else.
- Sending/Done files are left alone so the queue still shows where those files actually went.

## Queue controls

- **Add Files...** adds one or more files to the upload queue.
- **Remove Selected** removes highlighted files from the queue.
- **Clear Queue** removes all queued files when no upload is running.
- **Move Up** moves the selected file earlier in the queue.
- **Move Down** moves the selected file later in the queue.
- **Send Queue** sends pending files one at a time.
- **Auto-clear when queue completes** clears completed queue items after a successful queue run.

## Supported file types and names

MASSO normally expects G-code files with one of these extensions:

- `.nc`
- `.cnc`
- `.tap`
- `.eia`
- `.txt`

The app warns if a file has a different extension. Invalid target characters and non-ASCII names are blocked because they are known to cause problems on MASSO.

Allowed examples:

```text
part#12.tap
Clean_flag.tap
Clean-flag_test.nc
Bracket Left.nc
Part-(Rev-A).tap
```

Problem examples:

```text
café.tap
part:12.tap
part?12.tap
```

Avoid these characters in MASSO file/folder names:

```text
: * ? " < > |
```

Plain ASCII shop-friendly names are recommended.

## QR-code generation

The app can generate MASSO-compatible QR-code PNG files for loading G-code files from the MASSO screen.

To generate a QR code for one file:

1. Add the file to the queue.
2. Select the file in the queue.
3. Click **QR Selected...**.
4. Choose where to save the PNG file.

To generate QR codes for every queued file:

1. Add all desired files to the queue.
2. Click **QR Queue...**.
3. Choose an output folder.
4. The app creates one QR PNG per queued file.

QR files are named like this:

```text
YourFileName_MASSO_QR.png
```

The QR code uses the MASSO target path shown in the app. Make sure the file is actually uploaded to that same folder on the MASSO.

## Tools Data export

The app can request the MASSO tool list and generate a MASSO-style text file.

Current scope:

- Read-only.
- Requests tool slots 1 through 118.
- Exports populated tool numbers and tool names.
- Does not edit or upload MASSO tool data.
- Does not currently decode offsets, diameters, wear values, or other tool-table fields.

## Custom logo

The logo is easy to customize.

To use your own logo:

1. Create or choose a PNG image.
2. Name it exactly:

   ```text
   send_to_masso_logo.png
   ```

3. Put it in the same folder as the Send-to-MASSO `.exe`.
4. Restart the app.

The app will load that image automatically. A wide horizontal logo works best.

## Running from Python source

Windows users should normally use the release ZIP.

For source testing, use a recent Python 3 version and run the main script directly, for example:

```text
python send_to_masso_v1_8_19_rc.py
```

Tkinter is required. QR-code generation and logo support may also require Python packages such as `qrcode` and `Pillow`, depending on how the release/source environment is set up.

## Reporting a problem

When reporting an upload or connection problem, include:

- MASSO model/controller type and firmware/core version if known.
- Send-to-MASSO Manager version.
- Windows bundle or Python source.
- File size.
- Target MASSO folder.
- What you expected to happen.
- What actually happened.
- The relevant lines from the app log window. A screenshot is fine if that is easier.

A short Wireshark capture can be very helpful, but only if you already know what Wireshark is and are comfortable using it. It is not required for normal bug reports.

## Basic troubleshooting

### The app does not connect

Check:

- The MASSO is powered on.
- The MASSO Wi-Fi/network connection is active.
- The IP address is correct.
- The PC is on the same network or hotspot as the MASSO.
- Windows Firewall or security software is not blocking the app.

### Send Queue is disabled

The app only enables sending when:

- It is connected to the MASSO.
- The machine is stopped.
- The MASSO is not faulted.
- The MASSO is not waiting for user input/tool change.
- At least one valid file is pending in the queue.

### A filename is rejected

Use plain ASCII filenames. Avoid special characters such as:

```text
: * ? " < > |
```

Use normal shop-friendly names such as:

```text
CustomerPart_01.tap
Bracket-Left.nc
part#12.tap
```

### QR code does not load the file on MASSO

Check that:

- The G-code file was uploaded to the same MASSO folder shown in the app.
- The QR code was generated after the correct target folder was set.
- The file still exists on the MASSO.
- The filename and folder names match exactly.

## Notes and limitations

- The app does not browse MASSO folders.
- The app does not delete, rename, or move files already on the MASSO.
- The app does not edit MASSO settings.
- The controller must be reachable on the network before the app can connect.
- Unknown alarm codes are treated as unsafe and will block uploads, but may display as `Fault / Alarm 0x??` until someone provides a capture or report for that alarm state.
- QR-code behavior should still be treated as needing more real-world tester feedback.
- The protocol notes are based on reverse engineering and may change as more captures are collected.

## Developer / tester docs

- [Changelog](CHANGELOG.md)
- [Testing guide](docs/TESTING.md)
- [Reverse-engineered protocol notes](docs/PROTOCOL_SPEC.md)
- [Third-party notices](THIRD_PARTY_NOTICES.md)

## Suggested shop workflow

For a batch of parts:

1. Connect to the MASSO.
2. Enter the MASSO target folder.
3. Add all G-code files for the job.
4. Review the queue and target preview.
5. Generate QR codes if needed.
6. Send the queue.
