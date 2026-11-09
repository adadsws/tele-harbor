# tele-harbor

[简体中文](README.md) · [English](README_EN.md)

Download and organize files and text from Telegram groups on Windows. Supports multiple groups, resumable downloads, and quick skipping of completed items. Shows message dates and download progress.

## Get started

1. Install Python 3.11 or later and Google Chrome.
2. Copy the [example configuration](config/config.example.json) to `config/config.local.json` and choose your save location.
3. Double-click `download.bat` and sign in to Telegram in the Chrome window.
4. Use the numbered menu to add groups, then download a selected group or all groups.

Messages are processed from newest to oldest. Run the program again after an interruption to continue. Files and text are organized by group.
If a connection remains unavailable, the program explains the stop and keeps your progress. Run it again once the connection recovers.

The example configuration saves downloads in `~outputs-final/downloaded` by default.

## Two modes

- Fast mode (default): skip completed items and continue unfinished downloads.
- Per-file check mode: check existing files. File, path, or record problems stop the affected group with an error.

Use “Switch mode” in the menu to change modes. Enter `0` to exit. Removing a group from the menu keeps its downloaded files.

Keep local configuration, sign-in data, and downloads on your own computer; do not upload them.

[More usage options](docs/COMMAND_LINE.md) · [Documentation](docs/README.md)
