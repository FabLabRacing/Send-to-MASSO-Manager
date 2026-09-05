# Testing Guide

This guide is for users who want to help test Send-to-MASSO Manager.

You do not need to understand the MASSO network protocol to help. The most useful testing is normal shop-style use with clear notes about what worked or failed.

## Safety first

Do not create unsafe machine conditions just to test the app.

For alarm/fault testing, only report alarm states that happen naturally or that you can reproduce safely. The app should treat unknown alarms as unsafe and block uploads.

## What to report

When reporting a problem, include:

```text
MASSO model/controller type:
MASSO firmware/core version, if known:
Send-to-MASSO Manager version:
Windows bundle or Python source:
Network setup, if relevant:
File size:
File extension:
Target MASSO folder:
What you expected:
What happened instead:
Relevant app log lines:
```

A screenshot of the app log is fine if that is easier than copying text.

A short Wireshark capture can be very helpful, but only if you already know what Wireshark is and are comfortable using it. Wireshark is optional and not required for normal bug reports.

## Upload tests that help most

The app has already had real production use on plasma tables, but edge-case files are still useful.

Helpful file sizes to test:

```text
Very small files, under 1 KB
Files around 1.4 KB
Files around 350 KB to 400 KB
Files around 700 KB to 750 KB
Files around 900 KB to 1200 KB
Files larger than 1200 KB, if you normally use files that large
```

Helpful upload patterns:

```text
One small file
One large file
Several small files in a queue
Mixed small and large files in a queue
Repeatedly uploading the same file name to confirm overwrite behavior
Uploading after changing the target folder before sending
Retrying a Failed queue item after changing the target folder
```

## File and folder name tests

Known-good names include plain ASCII characters, spaces, dashes, underscores, parentheses, and `#`.

Helpful names to test:

```text
Bracket_Left.nc
Bracket Left.nc
Bracket-Left.nc
Bracket-(Left).nc
part#12.tap
```

The app intentionally blocks known-problem characters and non-ASCII names.

Do not expect these to work:

```text
café.tap
part:12.tap
part?12.tap
```

Avoid these characters in MASSO file/folder names:

```text
: * ? " < > |
```

## Target folder tests

Helpful folder tests:

```text
\
\Test\
/Test/
\Jobs\CustomerA\
\Jobs\CustomerA\NestedFolder\
```

Forward slashes in the app should be normalized to MASSO-style backslashes.

If you change the target folder after files are already queued, Pending and Failed files should update to the new folder. Files already Sending or Done should not change.

## Queue tests

Helpful queue tests:

```text
Add 10+ files at once
Move files up and down before sending
Remove one file before sending
Clear the queue before sending
Use Auto-clear when queue completes
Send a queue containing both small and large files
Confirm the queue stops if a file fails
```

## Machine status tests

Useful feedback includes whether the app enables/disables sending correctly when MASSO is:

```text
Stopped and ready
Running a program
Recently stopped
Waiting for user input/tool change
In a known alarm state
In an unknown or unusual alarm state
```

If the app shows an alarm name that does not match the MASSO screen, report both names.

## Tools Data tests

Useful Tools Data feedback:

```text
Does Get Tools Data complete?
Does the generated text file open automatically?
Are tool numbers and names correct?
Are empty tools skipped correctly?
Do factory tools, such as Plasma Torch or Camera, appear as expected?
```

Current Tools Data support is read-only and exports tool number plus tool name.

## QR-code tests

Useful QR feedback:

```text
QR for a file in the MASSO root folder
QR for a file in a subfolder
QR for a file name with spaces
QR for a file name with #
QR Queue for several files
```

The QR code should load the same target path shown in the app. Make sure the file has actually been uploaded to that same MASSO folder.

## Good bug reports

A good report does not have to be long. Something like this is very useful:

```text
MASSO G3 Touch, plasma table
Send-to-MASSO Manager v1.8.19 RC
Windows ZIP version
File: nested_bracket.tap, 938 KB
Target: \Jobs\Test\
Expected: upload completes
Actual: failed after retrying near the middle of the upload
App log screenshot attached
```

If you know Wireshark and are comfortable using it, attaching a short capture of the failed attempt is even better. If not, the app log and file size are still useful.
