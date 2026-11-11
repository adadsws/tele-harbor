# 命令行用法 / Command-line usage

## 简体中文

BAT 退出码为：正常结束 0，任务未完成或运行前范围/组合校验失败 1，参数解析错误 2，用户中断 130。持续分片超时在重建页面后仍失败时会明确显示“下载连接中断”，保留分片并停止本群，不无限重试。
服务器明确返回文件 5xx 错误时，会显示“远端文件错误”，有界重试后记录该附件失败并继续其他附件；未完成文件不算已下载，本轮退出码仍为 1。
诊断日志使用 `RUN_START` 和 `RUN_EXIT` 标记 PID、退出码及原因；帮助和参数错误也有结束记录。只有开始记录而没有结束记录时，需要结合进程状态和系统事件核实，不能仅凭空日志或最后一条下载记录断言崩溃。

在 CMD 中运行 `download.bat`。不带参数时进入数字菜单；也可以直接指定处理范围：

正常双击 BAT 保留可见 CMD 和 Chrome。调试窗口独立于两种校验模式：`--debug-window hidden` 使用无可见 Chrome 窗口，`--debug-window visible` 使用可见窗口（正常入口默认）。自动实测工具默认 hidden，不自动弹出窗口，也不会在登录失效时自动切换为 visible；首次登录请使用正常 BAT 或显式 `--login-only --debug-window visible`。从已打开的 CMD 传入 hidden 不会隐藏该 CMD 本身。

```bat
download.bat --limit 20
download.bat --limit 20 --dry-run
download.bat --limit 20 --verify exact
download.bat --limit 20 --debug-window hidden
download.bat "https://web.telegram.org/k/#-1000000005" --limit 20
```

`--limit 20` 按每组最新的原消息数计算，跨越限额的相册保持完整，实际处理数可能超过 20；`--dry-run` 只预览，不下载。不带 `--limit` 时处理所选群组可访问的历史消息；下次运行继续检查新消息。

`--verify fast` 为默认值：核对服务器最新页后，用有效消息核验标记和批量路径索引直接接近未完成端点；旧记录首次需要顺序核对补标记，标记失效则回退核对。限额全部已完成时直接结束，不逐文件读大小或 SHA-256。快速模式不逐页重查已核验旧区间的后续编辑、删除。`--verify exact` 按时间从新到旧重新核验所选范围已有文件的 SHA-256。新下载在两种模式中都核验分片、大小和 SHA-256。数字菜单用单个“切换模式”选项在两者之间切换。

其他诊断参数：`--audit-messages N` 核对网页消息结构且不下载；`--login-only` 仅登录；`--full-scan` 忽略扫描断点并从最新端完整扫描，可访问历史很大时会运行很久，不可与 `--limit` 同用。旧 `--rebuild-local` 和 `--pause-at-oldest` 已停用，传入会在启动浏览器前报错。

`--limit`、`--audit-messages` 均要求正整数；`--audit-messages` 不可与 `--limit`、`--dry-run`、`--full-scan` 同用。`--login-only --debug-window hidden` 会在启动浏览器前报错，登录需要可见窗口。
这类运行前校验失败在 CMD 中显示启动失败及日志位置，具体原因写入诊断日志。

逐文件模式的预检覆盖该群已有记录的本地路径和清单，不受 `--limit` 限制；媒体 SHA-256 重读仅覆盖本次选中范围。缺文件、大小/内容/路径不符、重复媒体、清单损坏或空业务目录均输出 `[本地数据错误]` 并停止该群、最终退出码为 1；不自动修复或重下已有异常成品。真正的新资源仍可下载，快速模式保持信任已有完成记录。

`--output <路径>` 覆盖成品位置；登录资料和扫描记录仍使用本机配置，不是完全独立的数据会话。

手动查看原消息时，在新标签页打开 `https://web.telegram.org/k/#-1000000005?message=104`。示例群号是虚构值，须换成已加入的真实群号；URL 中的 `message` 使用 Telegram 原始消息号。本例项目内部号为 2³² + 104，程序会转换回 104。不要使用“群号_消息号”拼接；已有 Web K 标签页的同文档导航也可能恢复旧聊天入口而丢失消息参数。项目先尝试原生定位，失败后先卸载旧页面，再全新加载该链接，并核对目标群和实际气泡。正确定位不保证 Telegram 能返回原件字节。

## English

BAT distinguishes normal completion (0), an incomplete task or failed pre-run range/option validation (1), argument-parsing errors (2), and user interruption (130). Persistent transfer timeouts stop the affected group after bounded recovery and preserve partial downloads.
Explicit file-server 5xx errors remain file failures after bounded retries while other attachments continue. Failed files are never counted as downloaded, and the run still exits with code 1.
Diagnostic logs include `RUN_START` and `RUN_EXIT` with PID, code, and reason, including help and parser exits. A missing exit marker requires checking process state and system events; it alone does not establish a crash.

Run `download.bat` from CMD. With no options, it opens the numbered menu. You can also choose a range directly:

Normal double-click use keeps CMD and Chrome visible. Debug display is independent of verification: `--debug-window hidden` runs Chrome without a visible window, while `--debug-window visible` is the normal entry default. The live-test harness defaults to hidden and never opens a visible browser automatically on sign-in failure. Use normal BAT or `--login-only --debug-window visible` to sign in. The hidden option does not hide an already open CMD window.

```bat
download.bat --limit 20
download.bat --limit 20 --dry-run
download.bat --limit 20 --verify exact
download.bat --limit 20 --debug-window hidden
download.bat "https://web.telegram.org/k/#-1000000005" --limit 20
```

`--limit 20` counts original messages per group and keeps an album intact across the boundary, so the actual count may exceed 20; `--dry-run` previews without downloading. Without `--limit`, the selected groups' accessible history is processed. Later runs continue checking for new messages.

`--verify fast` is the default: after checking the latest server page, valid message markers and the bulk path index allow a direct jump close to unfinished downloads. Older records need an initial sequential check to populate markers; invalid markers trigger another sequential check. A limit wholly inside the completed prefix finishes without reading each file's size or SHA-256. Fast mode does not recheck every old page for later edits or deletions. `--verify exact` rechecks existing file hashes within the selected range in newest-to-oldest order. Both modes verify chunks, size, and SHA-256 for new downloads. The numeric menu has one option to switch between the two modes.

Diagnostics: `--audit-messages N` checks webpage structure without downloading; `--login-only` opens sign-in only. `--full-scan` ignores scan checkpoints and scans accessible history from the newest end; it can run for a long time and cannot be combined with `--limit`. Retired `--rebuild-local` and `--pause-at-oldest` options fail before opening Chrome.

`--limit` and `--audit-messages` require positive integers. `--audit-messages` cannot be combined with `--limit`, `--dry-run`, or `--full-scan`. `--login-only --debug-window hidden` fails before opening the browser because sign-in requires a visible window.
For these pre-run validation failures, CMD shows a startup failure and the log location; the diagnostic log contains the specific reason.

Exact mode checks recorded local paths and manifests across the group before processing, regardless of `--limit`; media hashing applies to the selected range. Missing or corrupt local data, duplicate media, invalid paths, and empty business directories print `[本地数据错误]`, stop that group, and cause exit code 1. Existing invalid results are not automatically repaired or downloaded again. Unprocessed resources can still be downloaded.

`--output <path>` overrides finished output. The browser profile and scan records still come from local configuration, so this is not a fully isolated data session.

To open an original message manually, open `https://web.telegram.org/k/#-1000000005?message=104` in a new tab. The group ID is fictional; replace it with a group you have joined. The URL uses the original Telegram message ID; the application converts its internal 2³² + 104 back to 104. Underscore-concatenated routes are not used. Same-document navigation in an existing Web K tab may restore the old chat route and lose the message parameter. The application tries native navigation first, then unloads the old page before loading the query route, and checks the actual peer and message bubble. Successful navigation does not guarantee that Telegram can return the original file bytes.
