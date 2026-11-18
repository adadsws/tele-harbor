# tele-harbor

[简体中文](README.md) · [English](README_EN.md)

在 Windows 上下载 Telegram 群组里的文件和文字，自动按群组整理，支持中断后继续下载。

## 开始使用

1. 安装 Python 3.11 或更高版本，以及 Google Chrome。
2. 复制一份 [配置示例](config/config.example.json)，保存为 `config/config.local.json`，设置下载位置。
3. 双击 `download.bat`，在打开的 Chrome 中登录 Telegram。
4. 按数字菜单添加群组，选择一个群组或全部群组开始下载。

文件保存在配置指定的位置，示例为 `~outputs-final/downloaded`。中断后保留记录和临时文件，重新运行继续；失败原因见窗口和日志。

程序不再从旧媒体目录自动导入文件。请保留当前下载记录和续传文件，需要补齐已有缺失时使用逐文件下载或检查模式，并在结束提示中确认。

按需要查看对应指南：

- [安装与登录](docs/guides/SETUP.md)：首次准备与启动。
- [群组管理](docs/guides/GROUPS.md)：获取群号、添加和移除群组。
- [下载与续跑](docs/guides/DOWNLOADS.md)：开始任务、查看保存位置、中断后继续。
- [数据与文件管理](docs/guides/DATA.md)：数据关系、移动和添加文件。
- [模式选择与检查](docs/guides/MODES.md)：三种模式及缺失、多余文件的处理。

本机配置、登录资料和下载内容请留在自己的电脑上，不要上传。

[更多用法与旧目录迁移](docs/AGENT_CONTEXT.md#命令行用法)
