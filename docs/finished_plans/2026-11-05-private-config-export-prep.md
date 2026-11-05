# 本机配置与远端提交准备

- [x] 盘点本机配置、群组归档、历史文档、测试和所有本地 refs。
- [x] 在 `~temp/history-rewrite/` 建立完整备份及独立重写镜像。
- [x] 从可达历史移除六个本机 JSON 路径，并替换五个群组标识及本机路径。
- [x] 保留本机 `config.local.json`，增加 Git 忽略规则和公开样例。
- [x] 让群组菜单测试改用临时虚构配置；离线 61 项测试通过。
- [x] 从允许公开的当前文件建立独立 Git 历史的本地导出副本，不带归档与源仓库 refs。
- [ ] 真实 Telegram 登录和下载未在本任务执行；远端尚未配置，也未推送。

验收证据：重写镜像的四个 ref 对目标路径及原值命中数为零；本机配置的 Git blob 哈希与备份一致；`PYTHONPATH=src .venv-browser/Scripts/python.exe -m unittest discover -s tests -p test_*.py -q` 为 61 项通过。
