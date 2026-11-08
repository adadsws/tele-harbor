# 文档导航

当前规范集中在下列文件；旧方案和重复报告已归档，近期原始验收记录只作为证据。

| 文件 | 用途 |
| --- | --- |
| [中文使用说明](../README.md) / [English](../README_EN.md) | 安装、路径和两个校验模式 |
| [命令行](COMMAND_LINE.md) | 可执行参数与边界 |
| [项目上下文](AGENT_CONTEXT.md) | 架构、数据规范、评测集与未验证范围 |
| [代理规则](AGENTS.md) | 本项目执行与交付要求 |
| [变更记录](CHANGELOG.md) | 行为变化与验证结果 |
| `plans/` | 正在实施的计划 |
| `finished_plans/` | 已完成或按调整后的范围关闭的计划；保留当时的证据和限制 |
| `lessons/` | 精简复盘，不替代当前规范 |

未完成与未验证任务统一维护在 [当前遗留事项](plans/2026-11-08-remaining-verification.md)，不沿用旧计划中过期的待办。

## 根目录与运行数据

根目录保留 README、启动入口、依赖清单、代码/测试/配置/文档，以及约定的输出、临时和归档目录。依赖说明已并入 [requirements.txt](../requirements.txt)，没有独立的 reference 说明目录。
示例配置将成品放在 `~outputs-final/downloaded`，暂存、分片和扫描记录放在 `~outputs-intermediate/telegram`，会话和测试产物放在 `~temp`。实际本机配置可指定其他位置，整理项目不迁移现有数据。
根目录旧 `downloads`、无用途的测试暂存与旧 pytest 缓存已按文件可恢复归档；内部记录与旧测试数据所在批次不进入公开 Git。

## 代码与验证入口

| 模块 | 职责 |
| --- | --- |
| `__main__`、`group_menu`、`local_config` | BAT 命令行、数字菜单和本机配置 |
| `browser` | 已登录会话、服务器分页、媒体定位、恢复与下载流程 |
| `layout`、`content_types` | 头像段、group、命名与内容分类 |
| `reply_groups`、`content_archive` | 引用连通分量、关联清单、段内清单与原文文件 |
| `local_reuse`、`folder_archive` | 按文件复用、发布核验与空来源收尾 |
| `resumable`、`verification_manifest` | 分片续传、身份、大小和哈希证据 |
| `local_validation`、`errors` | 逐文件模式只读预检与本地异常停止 |
| `scan_record` | 追加式扫描、结果、端点及完成标记 |
| `diagnostics`、`progress` | 日志与用户进度 |

离线套件、真实 BAT 与三个独立审计入口见 [项目上下文](AGENT_CONTEXT.md)。审计结果只代表指定范围，不能推断完整历史无遗漏。
