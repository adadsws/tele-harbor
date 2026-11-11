# 文档导航

从使用说明进入操作，从项目规范进入命名与交付要求，从遗留索引查看当前任务。历史报告只证明所记版本和范围，不覆盖当前规则或待办。

| 文件 | 用途 |
| --- | --- |
| [中文使用说明](../README.md) / [English](../README_EN.md) | 安装、菜单、保存位置和两种模式 |
| [命令行](COMMAND_LINE.md) | 参数、组合限制、窗口方式和退出码 |
| [项目规范](AGENT_CONTEXT.md) | 代码职责、当前命名、下载规则及五项优先验收 |
| [代理规则](AGENTS.md) | 本项目执行与交付要求 |
| [变更记录](CHANGELOG.md) | 行为变化与验证结果 |
| [实测目录](LIVE_TESTS.md) | 场景编号、运行入口、预期和证据格式；列出不代表通过 |
| [当前遗留事项](plans/2026-11-08-remaining-verification.md) | 唯一遗留状态入口：未完成、未验证、远端暂缓 |
| [五项优先实测](plans/2026-11-10-five-priority-live.md) | 本轮范围、发现的缺陷、版本证据与仍缺条件 |

`plans/` 只放仍活动的任务；已结束阶段的验收摘要放入 `finished_plans/`，未完成目标明确移交遗留索引，不因文件迁移而视为完成。`lessons/` 记录可复用教训，不重复维护规范和任务状态。

## 阶段证据索引

以下为脱敏摘要，私有原始日志及精确标识只留本机。旧失败、后续修复和最后有效证据须分别阅读；测试数量、耗时和文件数量不是当前全项目状态。

| 阶段 | 记录 |
| --- | --- |
| 真实下载、端点段成员缺失修复、三群重建与中断续传 | [2026-11-08 真实运行](finished_plans/2026-11-08-real-runtime-verification.md) |
| 逐文件只读报错、错误路径/损坏/关联 JSON 负例 | [2026-11-08 严格核验](finished_plans/2026-11-08-strict-exact-local-validation.md) |
| CMD 日期、窄终端和实际传输/校验阶段 | [2026-11-08 显示验收](finished_plans/2026-11-08-cmd-download-status.md) |
| 旧文档和根目录测试产物整理 | [2026-11-08 整理](finished_plans/2026-11-08-project-documentation-cleanup.md) |
| BAT 退出、超时、服务器错误及失败后继续下载 | [2026-11-09 BAT 排障](finished_plans/2026-11-09-bat-timeout-diagnostics.md) |
| 第二群后续相册、网页/服务器及本地结构对照 | [2026-11-10 后续下载](finished_plans/2026-11-10-second-group-continuation.md) |
| 导航、同进程断网、可见窗口、第三群断点授权写回 | [2026-11-10 遗留场景](finished_plans/2026-11-10-remaining-live-verification.md) |
| 19 类自动场景、42 种进度与模式组合 | [2026-11-10 进度矩阵](finished_plans/2026-11-10-download-progress-matrix.md) |
| 自动同名 TXT 说明、索引排除及恢复归档 | [2026-11-10 异常说明](finished_plans/2026-11-10-auto-failure-notes.md) |
| 取消辅助名称百分号标记和旧登记安全迁移 | [2026-11-10 命名迁移](finished_plans/2026-11-10-remove-percent-markers.md) |
| 本次文档分工、任务移交、脱敏和链接核对 | [2026-11-10 文档统一](finished_plans/2026-11-10-documentation-consolidation.md) |
| Vivaldi 真实定位、同页路由失效修复、全天计数与原件/邻件对照 | [2026-11-10 消息定位](finished_plans/2026-11-10-web-message-navigation.md)；原件仍 500，移交原件排障 |
| 旧原件按损坏处置，用户停止后续专项恢复 | [诊断结案](finished_plans/2026-11-10-remote-file-root-cause.md)、[实测排除](finished_plans/2026-11-10-damaged-resource.md)；正式程序仍通用报错跳过并留下同名 TXT |

## 复盘索引

| 主题 | 记录 |
| --- | --- |
| 复杂度与异常边界 | [职责拆分](lessons/2026-11-07-reduce-cyclomatic-complexity.md) |
| 日期和进度范围 | [CMD 显示](lessons/2026-11-08-cmd-download-status.md) |
| 当前规则与历史证据分工 | [文档整理](lessons/2026-11-08-documentation-root-cleanup.md) |
| 完整头像段、实际中断与续传 | [真实恢复](lessons/2026-11-08-real-recovery-verification.md) |
| 本地异常不应被修复掩盖 | [只读核验](lessons/2026-11-08-strict-local-errors.md) |
| 说明 TXT 所有权及旧命名兼容 | [异常说明](lessons/2026-11-10-auto-failure-notes.md) |
| 副本状态、模式和窗口边界 | [进度矩阵](lessons/2026-11-10-download-progress-matrix.md) |
| 暂存关联预检、标记跳端点与独立观察边界 | [优先实测](lessons/2026-11-10-priority-live.md) |

依赖信息统一在 [requirements.txt](../requirements.txt)。本机配置、媒体、会话和历史私有归档不纳入公开文档；现用路径以配置为准，不因整理文档而迁移下载数据。
