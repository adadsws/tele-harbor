# Choose modes and review files

[简体中文](MODES.md) · [English](MODES_EN.md)

Choose “切换模式” (Switch mode), then select your groups. Fast mode is the default.

| Mode | Download new content and continue unfinished tasks | Check existing files for missing items, extras or damage |
| --- | --- | --- |
| Fast | Yes | No; trust completion records |
| Per-file download | Yes | Yes |
| Check | No | Yes |

The last two modes check the configured finished and in-progress download folders together. Missing files and extras are listed together, with one confirmation after the selected groups finish:

- Enter `y`: attempt to recover previously saved, now missing files from Telegram and move extras into a recoverable archive, without searching for local copies.
- Press Enter or type `n`: leave the issues unchanged.

Damage, conflicting records or incomplete checks cause an error rather than being overwritten. Recovery may fail and does not automatically rebuild lost directory trees or records. Large collections take time; follow the progress in the window.

Before moving files, see [Data and file management](DATA_EN.md).

[Back to project README](../../README_EN.md)
