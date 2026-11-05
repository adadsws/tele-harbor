# 项目代理规则

适用范围：项目根目录及所有子目录。全局 `AGENTS.md` 仍适用；本文件只记录 telegg 的专项约束。

## 运行与数据

- `download.bat` 是交给用户的 Windows 入口。修改启动或下载流程后，先运行离线测试，再以该入口做有界试运行；未登录或网络不可用时明确记录未验证项。
- 真实 Telegram 会话、扫描记录、分片和下载文件由 `config/config.local.json` 指向本机数据目录。核验路径前先读配置，不能把历史文档里的 `X:\TelegramData` 当作当前真实位置。
- `config/config.local.json` 与群组清单归档只留本机，公开配置结构见 `config/config.example.json`；测试使用临时虚构群组，不读取本机清单。
- 对真实群组使用 `--limit` 或 `--audit-messages` 限制样本；只扫描不能当作下载成功的证据。完整历史仅在服务器返回空页且记录边界满足要求时视为确认。
- 媒体文件进入成品目录前，核对已提交分片、文件大小和 SHA-256；未齐编组留在暂存目录。已有文件不得覆盖。

## 验收与记录

- 项目规范和评测集见 [项目上下文](AGENT_CONTEXT.md)。修改功能时同步更新适用样例，并记录实际输入、过程证据、结果和未验证项。
- 面向用户的行为变化同步更新 [中文说明](../README.md)、[英文说明](../README_EN.md) 与 [变更记录](CHANGELOG.md)。
- 活动任务计划放在 `docs/plans/`；确实完成后再移至 `docs/finished_plans/`，并在 `docs/lessons/` 记录精简复盘。
