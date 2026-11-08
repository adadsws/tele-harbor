# 项目结构与约定

## 入口与配置

`download.bat` 启动 `src/telegg/__main__.py`。复制 `config/config.example.json` 为本机配置后使用数字菜单。
本机配置、会话、下载、分片与日志不得提交到公开仓库。公开测试使用虚构配置。

## 文件组织

成品按群组、关联组（如有）、头像段、group（如有）和消息文件组织。头像段使用段内最旧消息的上海日期和编号；相册保持完整。引用连接的段共享关联上层和关联清单。
附件保留原文件名；照片省略原名。文字按原消息单独保存。段内清单列成员；关联清单列引用源和目标。
只有整个待发布单元的消息、内容与附件齐全时才能进入成品目录；已有文件不覆盖，只逐文件复用已核实媒体。

## 模块

- `browser`：服务器消息、定位、下载与发布流程。
- `layout`、`reply_groups`、`content_archive`：组织、关联与内容文件。
- `resumable`、`verification_manifest`：分片、身份、大小与哈希验证。
- `scan_record`、`local_validation`：运行记录与逐文件模式只读预检。
- `progress`、`diagnostics`：日期、进度和诊断。

## 行为与验证

默认快速模式信任完成记录，并从最新端逐步处理；不为展示总量预扫全部历史。逐文件模式发现本地文件、路径或清单问题时停止并报错，不自动掩盖异常。
CMD 日期仅显示 YYYY-MM-DD，使用每个附件原消息的上海日期；字节总量只描述当前文件，文件序号只描述本段本次选中范围。传输达到 100% 后仍需校验，通过后才完成。
维护时先运行 `python -m unittest discover -s tests -p "test_*.py" -q`；修改下载流程还应通过真实 BAT 进行有界实测。
`tests/live_output_audit.py`、`tests/live_server_date_audit.py` 和 `tests/live_dom_audit.py` 用于本机只读审计；浏览器审计只采集 ID、时间和结构，不查看正文或图片。
