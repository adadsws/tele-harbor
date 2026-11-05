# 命令行用法 / Command-line usage

## 简体中文

在 CMD 中运行 `download.bat`。不带参数时进入数字菜单；也可以直接指定处理范围：

```bat
download.bat --limit 20
download.bat --limit 20 --dry-run
download.bat "https://web.telegram.org/k/#-1000000005" --limit 20
```

`--limit 20` 只处理每组最新的 20 条消息；`--dry-run` 只预览，不下载。不带 `--limit` 时处理所选群组可访问的历史消息；下次运行继续检查新消息。

## English

Run `download.bat` from CMD. With no options, it opens the numbered menu. You can also choose a range directly:

```bat
download.bat --limit 20
download.bat --limit 20 --dry-run
download.bat "https://web.telegram.org/k/#-1000000005" --limit 20
```

`--limit 20` handles only the latest 20 messages per group; `--dry-run` previews without downloading. Without `--limit`, the selected groups' accessible history is processed. Later runs continue checking for new messages.
