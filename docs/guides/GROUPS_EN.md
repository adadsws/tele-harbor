# Manage groups

[简体中文](GROUPS.md) · [English](GROUPS_EN.md)

## Get a group ID

1. Open [Telegram Web K](https://web.telegram.org/k/), sign in and open your group.
2. Wait for messages, then press `Ctrl+L` to view the address, such as `https://web.telegram.org/k/#-1000000005`.
3. Copy the complete negative number after `#`, or the full numeric URL. This ID is fictional; use your own, keep the minus sign and do not change its prefix.

Group names and menu numbers are not group IDs. For a previously downloaded group, the ID is also in square brackets in its folder name.

| Different address | What to do |
| --- | --- |
| Contains `/a/`, or is a `t.me` link | Open the group in Web K above and check its address |
| Has `?message=…` after the ID | Copy only the negative number between `#` and `?` |
| Shows `#@username` | Unsupported by this program; use the ID from an existing download folder if available |

## Add or remove

- Add: run `download.bat`, choose “新增群组” (Add group), paste the ID or numeric Web K URL and press Enter. Select the existing entry if already listed.
- Remove: choose “移除群组” (Remove group), then its menu number. Downloaded files are kept.

Use the numbers shown in the menu. If a correct ID will not open, check that the program's Chrome is signed in with an account that has access.

Continue with [Download and resume](DOWNLOADS_EN.md).

[Back to project README](../../README_EN.md)
