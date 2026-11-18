# tele-harbor

[简体中文](README.md) · [English](README_EN.md)

Download files and text from Telegram groups on Windows. Organize them by group and continue after an interruption.

## Get started

1. Install Python 3.11 or later and Google Chrome.
2. Copy the [example configuration](config/config.example.json) to `config/config.local.json` and set your download location.
3. Double-click `download.bat` and sign in to Telegram in Chrome.
4. Use the numbered menu to add groups, then select one group or all groups to download.

Files go to the configured location; the example uses `~outputs-final/downloaded`. Keep records and temporary files to continue after an interruption. Check the console and logs for failures.

The program no longer imports files from older media directories. Keep current download records and resumable files. To recover missing completed files, use Exact download or Check mode and confirm the recovery prompt at the end.

Choose the guide you need:

- [Install and sign in](docs/guides/SETUP_EN.md): prepare and start the program.
- [Manage groups](docs/guides/GROUPS_EN.md): get IDs, add and remove groups.
- [Download and resume](docs/guides/DOWNLOADS_EN.md): start tasks, find files and continue later.
- [Data and file management](docs/guides/DATA_EN.md): understand records, move and add files.
- [Choose modes and review files](docs/guides/MODES_EN.md): choose a mode and handle missing files and extras.

Keep local configuration, sign-in data and downloads on your own computer; do not upload them.

[More options and older layouts](docs/AGENT_CONTEXT.md#english-command-line-usage)
