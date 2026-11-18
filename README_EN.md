# tele-harbor

[简体中文](README.md) · [English](README_EN.md)

Download files and text from Telegram groups on Windows and organize them by conversation structure.

## Result preview

Fictional example; manifests and other supporting files are omitted:

```text
Travel photos [-1000000001]/
└── [From_2026-11-18-m101] 关联组/
    ├── [From_2026-11-18-m101]/
    │   └── 2026-11-18-g7001/
    │       ├── 2026-11-18-m101_1_8001_Beach.mp4
    │       └── 2026-11-18-m102_2_8002.jpg
    └── [From_2026-11-18-m110]/
        └── 2026-11-18-g7001后/
            └── 2026-11-18-m110_This photo looks great.txt
```

Album attachments stay together; replies keep their own folders within the same linked group. Folders without reply links sit directly under the Telegram group.

Legend:

| Marker | Meaning |
| --- | --- |
| `[-1000000001]` | Telegram group ID. |
| `关联组` | Several sets of messages linked by replies or quotes, kept together. |
| `From_date-m101` | Consecutive messages from the same sender: think of each new appearance of the sender's avatar in the chat as the start of a separate set. Named after its earliest message's date and ID. |
| `g7001` | Telegram album ID. |
| `m101` | Message ID. |
| `g7001后` | Standalone messages after that album. |
| `_1_8001` | Attachment position 1 and media ID 8001. |

## Get started

1. Install Python 3.11 or later and Google Chrome.
2. Copy the [example configuration](config/config.example.json) to `config/config.local.json` and set your download location.
3. Double-click `download.bat` and sign in to Telegram in Chrome.
4. Use the numbered menu to add groups, then select one group or all groups to download.

Choose the guide you need:

- [Install and sign in](docs/guides/SETUP_EN.md): prepare and start the program.
- [Manage groups](docs/guides/GROUPS_EN.md): get IDs, add and remove groups.
- [Download and resume](docs/guides/DOWNLOADS_EN.md): start tasks, find files and continue later.
- [Data and file management](docs/guides/DATA_EN.md): understand records, move and add files.
- [Choose modes and review files](docs/guides/MODES_EN.md): choose a mode and handle missing files and extras.

Keep local configuration, sign-in data and downloads on your own computer; do not upload them.

[More options and older layouts](docs/AGENT_CONTEXT.md#english-command-line-usage)
