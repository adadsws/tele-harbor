# tele-harbor

[简体中文](README.md) · [English](README_EN.md)

## Features

- Download Telegram group media through a visible Chrome window, without Telegram API credentials.
- Group by caption: messages without text join the next message with text in time order; newer messages still awaiting a caption go under `未归组/`.
- See download progress and the dates reached while scanning and downloading.

## Configuration

1. On Windows, install Python 3.11 or later and Google Chrome, and make sure the `py` command is available.
2. Copy the [example configuration](config/config.example.json) to `config/config.local.json`. Adjust `output` (finished files) and other data directories as needed; keep the file under `config/`.
3. Double-click `download.bat`. On the first run, sign in to Telegram Web in the Chrome window. You can then add groups from the menu. The session is stored in the configured `chrome_profile` directory.

`config/config.local.json` contains local group and path information. Git ignores it; do not upload it.

## Usage

Double-click `download.bat` and use the numbered menu after signing in. If no groups are saved, enter `1` for “Add group,” then enter a Telegram Web K group link or group ID. The group is saved to `config/config.local.json`.

When groups are listed, enter the number beside a group to download it. The menu also offers “Download all groups,” “Add group,” and “Remove group”; enter `0` to exit. Numbers change as the group list changes, so follow the displayed menu. Removing a group from the list keeps its downloaded files. For CMD options, see [command-line usage](docs/COMMAND_LINE.md).
