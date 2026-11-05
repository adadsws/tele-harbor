# tele-harbor

[简体中文](README.md) · [English](README_EN.md)

## 特色功能

- 通过可见的 Chrome 下载 Telegram 群组媒体，无需 Telegram API 凭据。
- 按正文归组：无正文消息并入时间上下一条有正文的消息；最新端还没有对应正文的消息放入 `未归组/`。
- 下载窗口显示进度，以及扫描和下载到的日期。

## 配置

1. 在 Windows 上安装 Python 3.11 或更高版本及 Google Chrome，并确保 `py` 命令可用。
2. 将 [配置示例](config/config.example.json) 复制为 `config/config.local.json`。按需修改 `output`（成品目录）及其他数据目录；配置文件始终放在 `config/` 下。
3. 双击 `download.bat`。首次运行时，在打开的 Chrome 中登录 Telegram Web；之后可在菜单中添加群组。登录状态保存在配置的 `chrome_profile` 目录。

`config/config.local.json` 包含本机群组和路径信息，已被 Git 忽略，请勿上传。

## 使用

双击 `download.bat`，登录后按数字菜单操作。首次没有群组时，输入 `1` 选择“新增群组”，再输入 Telegram Web K 群组链接或群组号；群组会保存到 `config/config.local.json`。

已有群组时，输入群组前的数字开始下载；也可按菜单数字选择“下载全部群组”“新增群组”或“移除群组”，输入 `0` 退出。菜单编号随群组数量变化，以屏幕显示为准。移除群组只修改配置清单，已下载文件保留。CMD 参数见 [命令行用法](docs/COMMAND_LINE.md)。
