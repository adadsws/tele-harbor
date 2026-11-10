# 命令行用法 / Command-line usage

## 简体中文

BAT 结束时会标明正常结束、任务未完成、参数错误或用户中断，退出码分别为 0、1、2、130。持续分片超时在重建页面后仍失败时会明确显示“下载连接中断”，保留分片并停止本群，不无限重试。
服务器明确返回文件 5xx 错误时，会显示“远端文件错误”，有界重试后记录该附件失败并继续其他附件；未完成文件不算已下载，本轮退出码仍为 1。
诊断日志使用 `RUN_START` 和 `RUN_EXIT` 标记 PID、退出码及原因；帮助和参数错误也有结束记录。只有开始记录而没有结束记录时，需要结合进程状态和系统事件核实，不能仅凭空日志或最后一条下载记录断言崩溃。

在 CMD 中运行 `download.bat`。不带参数时进入数字菜单；也可以直接指定处理范围：

```bat
download.bat --limit 20
download.bat --limit 20 --dry-run
download.bat --limit 20 --verify exact
download.bat "https://web.telegram.org/k/#-1000000005" --limit 20
```

`--limit 20` 按每组最新的原消息数计算，跨越限额的相册保持完整，实际处理数可能超过 20；`--dry-run` 只预览，不下载。不带 `--limit` 时处理所选群组可访问的历史消息；下次运行继续检查新消息。

`--verify fast` 为默认值：从服务器最新消息批量核对 ID、时间、类型和本机完成记录，跳到未完成日期端点；限额全部已完成时直接结束，不逐文件读大小或 SHA-256。`--verify exact` 按时间从新到旧逐文件重新核验已有文件的 SHA-256。新下载在两种模式中都核验分片、大小和 SHA-256。数字菜单用单个“切换模式”选项在两者之间切换。

其他诊断参数：`--audit-messages N` 核对网页消息结构且不下载；`--login-only` 仅登录；`--full-scan` 忽略扫描断点并从最新端完整扫描，可访问历史很大时会运行很久，不可与 `--limit` 同用。旧 `--rebuild-local` 和 `--pause-at-oldest` 已停用，传入会在启动浏览器前报错。

逐文件模式的预检覆盖该群已有记录的本地路径和清单，不受 `--limit` 限制；媒体 SHA-256 重读仅覆盖本次选中范围。缺文件、大小/内容/路径不符、清单损坏或空业务目录均输出 `[本地数据错误]` 并停止该群、最终退出码为 1；不自动修复或重下已有异常成品。真正的新资源仍可下载，快速模式保持信任已有完成记录。

`--output <路径>` 覆盖成品位置；登录资料和扫描记录仍使用本机配置，不是完全独立的数据会话。

## English

BAT distinguishes normal completion (0), an incomplete task (1), invalid arguments (2), and user interruption (130). Persistent transfer timeouts stop the affected group after bounded recovery and preserve partial downloads.
Explicit file-server 5xx errors remain file failures after bounded retries while other attachments continue. Failed files are never counted as downloaded, and the run still exits with code 1.
Diagnostic logs include `RUN_START` and `RUN_EXIT` with PID, code, and reason, including help and parser exits. A missing exit marker requires checking process state and system events; it alone does not establish a crash.

Run `download.bat` from CMD. With no options, it opens the numbered menu. You can also choose a range directly:

```bat
download.bat --limit 20
download.bat --limit 20 --dry-run
download.bat --limit 20 --verify exact
download.bat "https://web.telegram.org/k/#-1000000005" --limit 20
```

`--limit 20` counts original messages per group and keeps an album intact across the boundary, so the actual count may exceed 20; `--dry-run` previews without downloading. Without `--limit`, the selected groups' accessible history is processed. Later runs continue checking for new messages.

`--verify fast` is the default: it checks server IDs, dates, types, and local completion records in batches from newest to oldest, then jumps to the incomplete date. A limit wholly inside the completed prefix finishes immediately, without checking each file's size or SHA-256. `--verify exact` checks every existing file's SHA-256 in newest-to-oldest order. Both modes verify chunks, size, and SHA-256 for new downloads. The numeric menu has one option to switch between the two modes.

Diagnostics: `--audit-messages N` checks webpage structure without downloading; `--login-only` opens sign-in only. `--full-scan` ignores scan checkpoints and scans accessible history from the newest end; it can run for a long time and cannot be combined with `--limit`. Retired `--rebuild-local` and `--pause-at-oldest` options fail before opening Chrome.

Exact mode checks recorded local paths and manifests across the group before processing, regardless of `--limit`; media hashing applies to the selected range. Missing or corrupt local data, invalid paths, and empty business directories print `[本地数据错误]`, stop that group, and cause exit code 1. Existing invalid results are not automatically repaired or downloaded again. Unprocessed resources can still be downloaded.

`--output <path>` overrides finished output. The browser profile and scan records still come from local configuration, so this is not a fully isolated data session.
