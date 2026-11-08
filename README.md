# tele-harbor

[简体中文](README.md) · [English](README_EN.md)

在 Windows 上下载并整理 Telegram 群组中的文件和文字。支持多个群组、断点续传和快速跳过已完成内容，下载时显示消息日期和进度。

## 开始使用

1. 安装 Python 3.11 或更高版本，以及 Google Chrome。
2. 将 [配置示例](config/config.example.json) 复制为 `config/config.local.json`，按需设置保存位置。
3. 双击 `download.bat`，在打开的 Chrome 中登录 Telegram。
4. 按数字菜单添加群组，再选择要下载的群组，也可以选择全部群组。

下载按消息日期从新到旧进行。中断后重新运行即可继续，文件和文字会按群组整理保存。

## 两种模式

- 快速模式（默认）：跳过已完成内容，继续未完成的下载。
- 逐文件检查模式：检查已有文件；发现文件、路径或记录异常时明确报错并停止当前群组。

使用菜单中的“切换模式”切换。输入 `0` 退出；从菜单移除群组不会删除已下载文件。

本机配置、登录资料和下载内容请留在自己的电脑上，不要上传。

[更多用法](docs/COMMAND_LINE.md) · [文档导航](docs/README.md)
