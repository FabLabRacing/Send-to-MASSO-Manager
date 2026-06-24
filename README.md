# Send-to-MASSO Manager

Send-to-MASSO Manager is a shop utility for sending G-code files to a MASSO controller over the network. It is intended to be easier to use than MASSO Link for day-to-day file sending, especially when you need to send several files at once.  

Send-to-MASSO Manager is an independent utility and is not affiliated with or endorsed by MASSO.

## Features

- Connects to a MASSO controller by IP address.
- Saves named MASSO connection profiles.
- Shows live machine status, including stopped/running state, progress, job count, line number, current/last file, tool-change/user-prompt state, and torch breakaway/fault state when available.
- Blocks file uploads while the machine is running, faulted, waiting for user input, or not connected.
- Supports an upload queue so multiple files can be added and sent one at a time.
- Supports adding multiple files at once.
- Allows queued files to be removed, cleared, or moved up/down before sending.
- Optional auto-clear of the queue after a successful queue upload.
- Generates MASSO-compatible QR-code PNG files for selected files or the whole queue.
- Shows the exact MASSO target path before sending.
- Accepts `/` or `\` in the target folder field and sends MASSO-style backslashes.
- Supports a custom logo image in the app panel.
- Stores settings beside the program so the bundle can be kept self-contained.

## Supported file types

MASSO normally expects G-code files with one of these extensions:

- `.nc`
- `.cnc`
- `.tap`
- `.eia`
- `.txt`

The app will warn if a file has a different extension. Invalid target characters and non-ASCII names are blocked because they are known to cause problems on MASSO.

Allowed examples:

```text
part#12.tap
Clean_flag.tap
Clean-flag_test.nc
```

Problem examples:

```text
café.tap
part:12.tap
part?12.tap
```

## Installing from the bundle

The normal release is distributed as:

```text
send_to_masso.zip
```

To install:

1. Download `send_to_masso.zip`.
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

No Python installation is required when using the bundled version.

Do not run the program directly from inside the ZIP file. Extract it first.

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
6. Review the queue.
7. Use **Move Up** or **Move Down** if the files need to be sent in a specific order.
8. Click **Send Queue**.

Files are sent one at a time. If a file fails to send, the queue stops so the problem can be checked before continuing.

## Queue controls

- **Add Files...** adds one or more files to the upload queue.
- **Remove Selected** removes highlighted files from the queue.
- **Clear Queue** removes all queued files when no upload is running.
- **Move Up** moves the selected file earlier in the queue.
- **Move Down** moves the selected file later in the queue.
- **Send Queue** sends pending files one at a time.
- **Auto-clear when queue completes** clears completed queue items after a successful queue run.

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

## Notes and limitations

- MASSO file browsing is not currently supported. The app can send files to a typed target folder, but it cannot list files already stored on the MASSO.
- Tool table download is not currently implemented.
- The app does not delete files from the MASSO.
- The app does not edit MASSO settings.
- QR-code generation should be verified with your MASSO workflow before depending on it for production.
- The controller must be reachable on the network before the app can connect.

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

## Suggested workflow

For a batch of parts:

1. Connect to the MASSO.
2. Enter the MASSO target folder.
3. Add all G-code files for the job.
4. Generate QR codes if needed.
5. Send the queue.
6. Confirm the files are available on the MASSO before running the job.

