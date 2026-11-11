# tele-harbor

[简体中文](README.md) · [English](README_EN.md)

Download and organize files and text from Telegram groups on Windows. Supports multiple groups, resumable downloads, and quick skipping of completed items. Shows message dates and download progress.

## Get started

1. Install Python 3.11 or later and Google Chrome.
2. Copy the [example configuration](config/config.example.json) to `config/config.local.json` and choose your save location.
3. Double-click `download.bat` and sign in to Telegram in the Chrome window.
4. Use the numbered menu to add groups, then download a selected group or all groups.

Messages are processed from newest to oldest. Run the program again after an interruption to continue. Files and text are organized by group.

Connection problems are explained and your progress is kept. If a file still fails after retries, the script writes a same-name TXT explanation, keeps its failed status, and continues with other files. The explanation is archived once the original file is recovered and verified.

The example configuration saves downloads in `~outputs-final/downloaded` by default.

## Two modes

- Fast mode (default): check the latest messages, then use valid completion markers to jump close to unfinished downloads. Older records may need an initial marker update.
- Per-file check mode: check existing files. Duplicate files, path, or record problems stop the affected group with an error.

Use “Switch mode” in the menu to change modes. Enter `0` to exit. Removing a group from the menu keeps its downloaded files.

Keep local configuration, sign-in data, and downloads on your own computer; do not upload them.

Opening the BAT normally shows its window. Automated debugging supports hidden and visible browser windows; see [More usage options](docs/COMMAND_LINE.md).

The usage guide also explains how to link directly to an original message.

[More usage options](docs/COMMAND_LINE.md) · [Documentation](docs/README.md)
