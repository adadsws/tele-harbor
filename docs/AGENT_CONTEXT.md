# 项目规范

本文件统一维护项目结构、命名与下载规则、命令参数、评测场景和验收标准。历史记录仅作证据，不覆盖当前实现；面向使用者的操作说明见 [项目 README](../README.md) 及其分别引用的指南。

## 目录与数据

| 位置 | 用途 |
| --- | --- |
| `src/telegg/` | 下载、组织、校验与菜单代码 |
| `tests/` | 离线评测、隔离实测和本机只读审计入口 |
| `config/` | 公开配置示例及被忽略的本机配置 |
| `docs/` | 当前规则、导航、计划及近期验收记录 |
| `~outputs-final/downloaded/` | 示例配置的成品位置 |
| `~outputs-intermediate/telegram/` | 示例配置的暂存、分片和扫描记录 |
| `~temp/` | 示例配置的浏览器会话、测试产物及可重建工具缓存 |
| `~archive/` | 可恢复的旧文件，仅本机保留含私有数据的批次 |

实际运行位置由 `config/config.local.json` 决定；整理项目不迁移该配置指向的数据。运行、测试和文档示例不得自行创建根目录 `downloads/` 或 `unused` 暂存目录。已完成文件、断点和会话不能当作普通缓存随意清空。
依赖版本、来源、许可证与重建说明统一在 [requirements.txt](../requirements.txt)；不复制上游源码。

### 数据职责与实际存储

用户指南中的采集数据、下载记录、下载文件是下载业务的三个方面，不是完整的文件类型枚举，也不是三套独立数据库。实际存储按职责交叉组织：

| 职责 | 当前存储与用途 |
| --- | --- |
| 消息信息与处理状态 | `scan_records/<群ID>.jsonl` 同时追加消息/原消息、附件发现、处理结果、原文路径、完成标记、历史边界和无 group 定位事件；采集信息和下载记录共用此文件 |
| 续传与验收 | `partials/<群ID>/` 中的 `.part` 为未完成字节，`.jsonl` 为身份/提交分片记录，`.verified.jsonl` 为完成文件身份、大小、状态与 SHA-256 快照；完成后保留的日志不是多余文件 |
| 已保存内容及组织信息 | `output` 是成品根，`staging` 保存尚未满足发布条件的消息/附件；两者均可有完整文件、原文 TXT、段内/关联清单及失败说明登记，暂存不等于其中每个附件都没下载完 |
| 运行配置与登录 | 本机配置保存群号和路径，`chrome_profile` 保存 Chrome/Telegram 会话；不作为已下载内容或下载完成凭证 |
| 维护与诊断 | 运行日志、隔离测试产物、迁移计划、恢复任务及可恢复归档各有独立用途，不由上述三个方面决定是否可清理 |

位置以配置为准，以上目录名表示配置职责，不要求用户磁盘上恰好有同名目录。扫描和处理交错进行，不是先采集全群再下载；历史扫描到顶也不等于附件齐全。SHA-256 通常以已接收分片和本机验收记录为基线，不能据此声称取得了 Telegram 原件的服务端哈希，或证明全群消息/附件没有遗漏。

共用检查使用配置的 `output`、`staging` 和 `partials`。`staging` 名为 `groups` 且与 `partials` 同级时，还盘点其共同父目录的散落文件，即当前所称整个 `downloading`；自定义为其他布局时只检查这三个配置根。`--output` 改写成品根时使用对应的旁置暂存/分片根，扫描记录和登录资料仍共享本机配置。

## 代码职责

| 模块 | 职责 |
| --- | --- |
| `__main__`、`group_menu`、`local_config` | BAT 命令行、数字菜单和本机配置 |
| `browser` | 已登录会话、服务器分页、媒体定位、恢复与下载流程 |
| `layout`、`content_types` | 头像段、group、命名与内容分类 |
| `reply_groups`、`content_archive` | 引用连通分量、关联清单、段内清单与原文文件 |
| `association_cleanup` | 完整关联分量发布后的重复暂存辅助清单核对与可恢复归档 |
| `local_reuse`、`folder_archive` | 按文件复用、发布核验与空来源收尾 |
| `resumable`、`partial_preflight`、`verification_manifest` | 分片续传、只读断点预检、身份、大小和哈希证据 |
| `local_validation`、`errors` | 逐文件模式只读预检与错误分类 |
| `extra_files`、`storage_review` | 共用缺失/额外检查、流程结束一次确认、服务器补缺与可恢复归档 |
| `review_progress` | 只读结构心跳、分块 SHA-256 字节进度、取消读取及本轮复核缓存 |
| `album_context`、`layout_migration` | 无 group 消息的前一相册边界、持久化定位证据和显式逐文件迁移 |
| `media_index`、`failure_notes` | 批量媒体索引、异常说明登记及安全迁移归档 |
| `scan_record` | 追加式扫描、结果、端点及完成标记 |
| `diagnostics`、`progress` | 日志与用户进度 |

## 组织与命名

层级统一为：群组 → 关联组（如有）→ 头像段 → group 或无 group 定位目录 → 消息文件。

项目生成的辅助文件和目录不再使用 `%` 作为名称标记。Telegram 原始文件名、正文中的字符及进度百分比保留，不因这项规则改写。历史带前缀数据仅兼容读取，不再生成。

- 群组目录为 `<群标题> [<群ID>]`。
- 头像段目录为 `[From_<段内最旧消息日期>-m<最旧消息号>]`，恰好对应一个完整头像段；发送者非空且相同、方向和署名相同、上海日期相同且相邻时间不超过 121 秒的消息连续归段，相册不可拆开。
- 引用连接的头像段共享 `[From_<关联段最旧消息日期>-m<最旧消息号>] 关联组` 上层，保留段界，共用一份 `关联清单.json`。
- 每段有 `段内清单.json`；相册文件进入 `<消息日期>-g<grouped_id>`，无 group 文件进入下述前一相册定位层。
- 无 group 消息在头像段内增加 `<前一相册日期>-g<前一相册ID>后`；原消息顺序中前一相册可跨头像段和日期，层名复用该相册的目录日期。只有已确认历史最早边界或服务器空页证明此前无相册时，才使用 `<消息日期>-首个group前`。单条附件、原文 TXT、媒体历史版本及失败说明共用定位层，失败说明登记跟随所属目录。
- 媒体名为 `<消息日期>-m<消息号>_<附件位置>_<媒体ID>_<原名>.<后缀>`；照片及没有原名的附件省略原名。文字保存为 `<消息日期>-m<消息号>_<文字前30字符>.txt`，内容为原文。

只有消息、内容记录、附件及完整引用连通分量都齐全时才进入成品。已有文件不覆盖；复用只移动经过核验的媒体文件，不迁入旧目录或状态。

关联发布前后都核对仅含 `关联清单.json` 的暂存父目录：成品清单须逐字节一致且符合当前引用关系，所有关联头像段、段内清单、内容和附件须完成，才将暂存副本可恢复归档并收尾空目录。内容冲突先报错；成员不齐或存在其他文件保持原位。三模式的新规则优先：启动前已有的辅助残留不在下载阶段自动归档，后两种模式结束时统一确认；本轮新发布产生的辅助记录仍正常收尾。

无 group 定位使用扫描日志的 `standalone_context` 事件保存前一相册 ID、日期和消息号。下载从新到旧时，批内没有旧侧相册上下文的前缀先查服务器元数据；超时、分页重复或群身份不符必须失败，不用“首个group前”掩盖未知边界。既有定位证据可复用，媒体名及 `naming_revision=3` 不变。

旧无 group 文件通过显式 `--migrate-standalone-layout` 调整，正常下载在业务写入前提示迁移，逐文件预检只读兼容尚无定位记录的旧路径；迁移后仅接受新路径。`--dry-run` 只保存本机预览；`--layout-plan` 绑定单群预览清单及原扫描日志哈希。执行前检查所有来源、目标、文件身份、说明登记和验收日志；目标冲突或用户改动时停止。不可变任务计划先写入状态根 `layout-migrations/<群ID>/`，记录可恢复到已完成的逐文件移动；旧扫描日志及旧说明登记备份保存在数据管理根 `~archive/`。移动保留文件身份、大小和修改时间，不读媒体/原文字节；追加路径快照与扫描结果/内容路径，完成、失败状态不升级。分片及旧媒体复用池不迁移。异常断电导致扫描日志尾行损坏时仍严格拒绝，保留备份交接，不宣称覆盖所有断电恢复场景。

服务器原件错误有界重试结束后，下载脚本自动在原件应保存的暂存目录生成异常说明。文件名与原件完整文件名相同，仅将最后一个后缀替换为 `.txt`；内容由代码模板生成，含日期、消息号、预期大小、错误及未完成说明，不含正文或凭据。失败及断点保留，说明不作为媒体、原文或完成凭证。

用户可将某份已知不可读原件登记为“损坏、停止专项实测”。该处置只记录在本机状态根的 `live-test-exclusions.json`，仅供实测工具和代理选样使用；生产程序不读取，不添加按群号/消息号的永久屏蔽规则或新的结果状态。正常运行仍对所有资源使用相同的有界错误处理：遇错跳过本轮附件、生成同名 TXT、保留 failed 和断点，再继续其他资源；下次正常运行仍按通用流程处理。损坏处置不等于下载完成或服务端物理损坏鉴定。

`下载异常说明.jsonl` 追加登记说明的文件名、媒体名、消息号及 SHA-256；媒体索引排除已登记说明，真实 TXT 附件仍按原规则处理。原件为 TXT 时，下载前先核对说明所有权并逐文件归档；其他原件校验成功或可信跳过后归档说明，归档中保留有效登记，归档再次被扫描时也排除说明。不覆盖用户同名文件或修改过的说明；逐文件预检发现说明或登记损坏时只读报错。旧手工命名格式只作为历史证据，不再生成。

历史带前缀异常登记在索引和逐文件预检中只读兼容；正常写入或显式迁移时，先验证登记及说明所有权/哈希，再逐文件改为当前名称，不移动目录。新旧登记同时存在、说明损坏或目标冲突时明确报错，禁止覆盖或自动合并；归档也使用当前名称。

## 下载和三种模式

下载、扫描和跳过按消息时间从新到旧，使用服务器原始日期；仅服务器空页能确认可访问历史最早端。确认段界后即处理，不为显示总量预扫全历史。

- 快速模式默认开启：核对服务器新消息后按完成记录跳过旧区间，不检查已下载缺失或额外，不做修正询问；实际新下载及未完成断点仍进行身份、分片和哈希保护。
- 逐文件下载与检查模式共用已有文件检查：核对登记路径、大小、原文、清单与媒体 SHA-256。唯一业务差异是前者保存新增消息/附件/数据，后者不保存新增内容。缺失和额外项在所选群组流程末尾分别列出并只问一次，确认后服务器补缺/归档额外；拒绝或 EOF 保持问题项原位。现存哈希、身份、清单结构冲突仍报错，不改信任基线。
- 普通重复/错位副本纳入额外清单，可靠登记路径是保留依据；连续编号的合法版本链仍保留。补缺不复用这些本地副本，独立下载后匹配原身份/大小/验收摘要才放回登记路径。
- 快速和逐文件下载模式的新下载均验证分片、大小与哈希；重启核验已保存分片后续传。
- 选中编组在写入及回退关联成员前，对待续传媒体只读核对服务器身份、提交分片及未提交尾部；坏断点先报错，完好关联成员保持原位。预检不修复日志尾行或补写尾部；有效断点交正式续传提交，远端文件错误仍走同名说明及继续机制。
- `--limit` 计算原消息数，跨越限额的相册保持完整；限额复核不得缩小已有段内清单。

快速模式信任已核验旧区间，不保证每次发现最新页之外的历史编辑、删除或本地内容损坏；需要重新核对时使用逐文件模式选择相应范围。逐文件模式同时预检成品及暂存的关联清单：暂存允许目标尚未落盘，但清单引用关系仍须与扫描元数据一致；缺失、损坏或错链不能先自动修复再检查。

### 缺失和额外检查及确认修正

BAT 第三模式及兼容 `--check-extra-files` 检查 downloaded 和整个 downloading，包括 groups、partials 和根层散落文件；全选仍纳入目录中已有但已移出配置的群。缺失依据完成媒体与保存原文的当前路径登记；内部状态 JSON 不是下载成品，不算缺失附件。生成清单的结构异常仍显式报错。服务器仅返回身份元数据用于核对，不由检查动作保存新增消息或附件。

检查阶段不写业务文件或扫描状态；现存媒体读取 SHA-256，分片仍只读身份/验收头，不读 `.part` 字节。正常旧版本、有效异常说明及正常分片/日志保留；归档、复用池与登录资料排除。服务器分页或身份错误、本地并发变化不解释为无额外文件。检查结果覆盖记录涉及范围，下载 `--limit` 不缩短检查范围。

所有所选群处理结束后，有问题才询问一次。仅 `y/yes/是/1` 同意；其他输入、回车或 EOF 都不修改。确认后先重新核对记录哈希、文件状态与目标缺失条件，额外文件保留原相对路径和摘要可恢复归档；已被归档的旧完成媒体撤销完成状态。缺失媒体在独立恢复分片目录从服务器下载，不搜索其他本地副本，匹配原媒体身份、大小和 SHA-256 后才放回原路径并追加验收/结果；原文按服务器内容与旧记录核对后恢复。目标突然出现、身份变化、内容冲突时失败并保留现场。确认后的检查模式仅修已有数据，不下载尚未完成或新增的消息/附件。

缺失与额外从同一份目录盘点判断，联合结果先汇总；结构和媒体完整性另标阶段，每秒显示进度，慢文件也显示已读字节。结构检查在线程中只读执行，媒体以 4 MiB 分块读取、响应取消并复核前后文件身份。结构阶段读取的验收快照传给哈希阶段；本轮已验证且状态及基线均未变的文件，收尾复核不重复读媒体字节。初次检查失败且未进入下载时，不在收尾重复整组检查。版本链先按消息建索引，禁止逐消息重扫整个本地列表。

评测入口为 `tests/test_storage_review.py`、`tests/test_review_progress.py`、三模式 CLI 测试和 `tests/live_storage_review.py`；全面进度/目录/菜单/多群实测入口为 `tests/live_all_modes.py`，独立证据复读为 `tests/check_all_modes_evidence.py`。真实大目录本地阶段用 `tests/live_review_progress.py` 有界验证进度与取消，不代表服务器联合比较或整组哈希完成。各状态的输入、过程、结果与边界统一见实测目录。当前规则取代历史记录中的“检查模式永不修改”和“已完成缺失一律立即拒绝”；历史实测只证明当时版本。

CMD 只显示当前附件原消息的上海日期 YYYY-MM-DD，不显示时分秒。本段文件序号只计本次选中范围，字节总量只描述当前附件；传输 100% 后仍需校验，通过后才显示完成。

窗口显示独立于校验模式：正常 BAT 默认 `visible`，代理自动调试默认显式传入 `hidden`。无可见窗口模式不自动开窗恢复登录；有可见窗口模式需明确选择。已打开的用户 CMD 不因 hidden 参数隐藏。

旧消息定位先调用 Web K 原生界面（内部消息号），失败时先卸载旧页面，再使用群组片段后的 `?message=<原始消息号>` 全新加载；滚动卡住后的回找复用同一路由入口。同页 hash 导航可能被旧聊天入口覆盖，不能仅改地址再等待。URL 构造器把内部号还原成原始 32 位 ID，不使用“群号_消息号”拼接。地址改变不代表定位成功，必须核对目标群及消息/相册成员气泡。路由修复不作为 500 原件恢复或全天消息范围完整的证据。

## 命令行用法

### 简体中文

后两种模式使用一次目录盘点联合检查缺失和额外，先显示两类数量。其后的完整性阶段单独显示结构条数及 SHA-256 文件数、字节、速度、预计剩余和当前路径；它仍可能读取整组大量媒体。Ctrl+C 会停止分块读取并保持现场。已在本轮校验且状态未变的媒体，收尾复核不会重复读取。

旧无 group 布局可用 `download.bat --migrate-standalone-layout --dry-run` 生成全部已配置群的只读预览；正式执行为 `download.bat --migrate-standalone-layout`。也可传群链接限定为单群，或用 `--layout-plan <预览JSON路径>` 按已核对的单群清单执行（绑定群号和扫描日志哈希）。已有未完成任务时，不带 `--layout-plan` 重跑会接着核对计划中的移动，不覆盖目标。此流程只调整成品/暂存单条消息位置及必要路径记录，不下载、不移动分片、不升级 failed 状态；同名冲突或身份变化会报错。不能与 `--limit`、`--audit-messages`、`--full-scan`、`--login-only` 或 `--check-extra-files` 同用。正常用户运行仍显示窗口；代理测试显式使用 hidden。

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

`--verify fast` 下载新增内容并信任完成记录，不检查已下载缺失或额外文件，不进行结束修正询问。`--verify exact` 下载新增内容，并与检查模式共用已有文件的缺失、额外和完整性检查。新下载始终核验分片、大小和 SHA-256。菜单在三种模式间循环。

其他诊断参数：`--audit-messages N` 核对网页消息结构且不下载；`--login-only` 仅登录；`--full-scan` 忽略扫描断点并从最新端完整扫描，可访问历史很大时会运行很久，不可与 `--limit` 同用。旧 `--rebuild-local` 和 `--pause-at-oldest` 已停用，传入会在启动浏览器前报错。

兼容参数 `download.bat --check-extra-files` 进入“检查缺失和额外文件”；可追加群组链接。它不保存新增消息、原文或附件，与逐文件下载模式共用检查和最终一次询问。发现问题后输入 `y` 才从服务器补齐已有缺失并可恢复归档额外文件；默认回车、`n`、EOF 不修改。选择全部覆盖 downloaded、整个 downloading 以及仍有目录的已移出配置群；归档、复用池、浏览器资料排除。不与 `--limit`、`--dry-run`、`--audit-messages`、`--full-scan`、`--login-only` 同用。

`--limit`、`--audit-messages` 均要求正整数；`--audit-messages` 不可与 `--limit`、`--dry-run`、`--full-scan` 同用。`--login-only --debug-window hidden` 会在启动浏览器前报错，登录需要可见窗口。
这类运行前校验失败在 CMD 中显示启动失败及日志位置，具体原因写入诊断日志。

检查以当前登记路径为准，不搜索本地替代副本；已有媒体读哈希，缺失和额外路径延迟到流程末尾确认。检查范围覆盖该群已登记数据，不受下载 `--limit` 限制；下载新增消息仍受限额约束。清单结构损坏、现存媒体哈希冲突等安全异常仍报错。发现问题但拒绝修改可正常退出 0；检查不完整或修正失败退出 1，Ctrl+C 为 130。确认等待期间文件或记录变化会停止修正，不覆盖新出现的目标。

`--output <路径>` 覆盖成品位置；登录资料和扫描记录仍使用本机配置，不是完全独立的数据会话。

手动查看原消息时，在新标签页打开 `https://web.telegram.org/k/#-1000000005?message=104`。示例群号是虚构值，须换成已加入的真实群号；URL 中的 `message` 使用 Telegram 原始消息号。本例项目内部号为 2³² + 104，程序会转换回 104。不要使用“群号_消息号”拼接；已有 Web K 标签页的同文档导航也可能恢复旧聊天入口而丢失消息参数。项目先尝试原生定位，失败后先卸载旧页面，再全新加载该链接，并核对目标群和实际气泡。正确定位不保证 Telegram 能返回原件字节。

### English command-line usage

Both review modes use one inventory to detect missing files and extras together. A combined count precedes structural and SHA-256 integrity checks, which show item counts, files, bytes, speed, estimated time and the current path. Large archives still require full media reads; Ctrl+C stops chunked reading. Unchanged files already verified in this run are not read again during the final refresh.

Preview the old standalone-message layout with `download.bat --migrate-standalone-layout --dry-run`, then apply with `download.bat --migrate-standalone-layout`. This defaults to configured groups; pass a group URL to limit it, or `--layout-plan <preview-JSON>` to apply an already reviewed single-group plan bound to the peer and scan-log hash. Rerun without `--layout-plan` to resume an unfinished plan. Targets are never overwritten. This moves standalone finished/staging files and updates path records without downloading attachments, moving partial payloads, or upgrading failed states. Conflicts or changed identities fail explicitly. It cannot be combined with `--limit`, `--audit-messages`, `--full-scan`, `--login-only`, or `--check-extra-files`. Normal user runs remain visible; agent tests explicitly select hidden.

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

`--verify fast` downloads new content and trusts completion records without checking missing files or extras. `--verify exact` downloads new content and shares existing-file checks and the final confirmation with Check mode. New downloads always verify chunks, size and SHA-256.

Diagnostics: `--audit-messages N` checks webpage structure without downloading; `--login-only` opens sign-in only. `--full-scan` ignores scan checkpoints and scans accessible history from the newest end; it can run for a long time and cannot be combined with `--limit`. Retired `--rebuild-local` and `--pause-at-oldest` options fail before opening Chrome.

The compatible `download.bat --check-extra-files` entry now selects Check mode. It does not save new messages, text or attachments. Both review modes list missing and extra paths and ask once after all selected groups. Only `y` restores known missing files from the server and recoverably archives extras; Enter, `n`, or EOF leaves issues unchanged. No alternate local copy is used. Completed-file SHA-256 and structural errors remain protected. Successful checks or declined fixes exit 0; incomplete checks or failed fixes exit 1. Concurrent changes abort modification. Existing range/preview/login option restrictions still apply.

`--limit` and `--audit-messages` require positive integers. `--audit-messages` cannot be combined with `--limit`, `--dry-run`, or `--full-scan`. `--login-only --debug-window hidden` fails before opening the browser because sign-in requires a visible window.
For these pre-run validation failures, CMD shows a startup failure and the log location; the diagnostic log contains the specific reason.

Exact and Check modes inspect recorded local paths, manifests and completed-media hashes across the group, regardless of the download `--limit`. Missing completed files and ordinary extra/duplicate files are deferred to the final confirmation. Existing corruption, invalid manifests or unsafe paths still stop the group with an error; they are not silently repaired. Only Exact mode downloads new content.

`--output <path>` overrides finished output. The browser profile and scan records still come from local configuration, so this is not a fully isolated data session.

To open an original message manually, open `https://web.telegram.org/k/#-1000000005?message=104` in a new tab. The group ID is fictional; replace it with a group you have joined. The URL uses the original Telegram message ID; the application converts its internal 2³² + 104 back to 104. Underscore-concatenated routes are not used. Same-document navigation in an existing Web K tab may restore the old chat route and lose the message parameter. The application tries native navigation first, then unloads the old page before loading the query route, and checks the actual peer and message bubble. Successful navigation does not guarantee that Telegram can return the original file bytes.

## 评测与验收

可选择的实测类型、三种模式的当前预期、旧双模式矩阵、隔离副本运行方式及证据格式见 [实测目录](AGENT_CONTEXT.md#实测场景与执行)。目录中的未执行类型不得记作已通过；快速模式的记录跳过不等同于实际内容校验。

### 交付优先实测

交付前按以下顺序优先验收，以发现错分组、遗漏及假完成。这五项不替代实测目录中的其他适用项目；写入规范不代表已经执行或通过。

| 优先级 | 必须实测的项目 | 通过标准 | 对应目录 |
| --- | --- | --- | --- |
| 1 | 代理用浏览器独立核对脚本 | 三群分别选真实复杂样本，实际定位后独立采集消息 ID、日期、附件类型/数量、相册和头像段结构及引用关系，与脚本记录和实际目录交叉比对；选中范围无遗漏、重复和错分组。不得以脚本自己的输出充当独立证据，不查看隐私正文或图片；样本结论不外推为完整历史通过 | G00、G01、G05、A02 |
| 2 | 三群已有下载范围完整性 | 将旧媒体副本、重新获取的远端消息/附件清单、扫描记录及实际文件交叉比对，覆盖原已下载范围；文件身份、数量、路径和独立 SHA-256 一致，无漏件、重复和假完成。不能只相信 downloaded 标记；无法取得的资源保留在预期清单中单列失败 | A00、A01、L00–L03 |
| 3 | 关联组发布与空目录 | 覆盖引用目标未下载、多跳引用、多个头像段及中断后恢复；关联分量不齐留暂存，齐全后带正确的上层关联 JSON 发布，各头像段及 group 保持完整。正常、失败、迁移和发布收尾均不因逻辑错误遗留活动空目录；仅搬走空目录不算修复通过 | G02、G03、L04、L05 |
| 4 | 不同进度交叉双模式 | 优先覆盖部分相册、文件领先记录、记录领先文件、已提交分片损坏及不同可信断点，实际运行 fast→exact、exact→fast 续跑；可恢复的完成大小/哈希核验，不可恢复的明确拒绝并保留现场。逐文件模式发现哈希、身份或结构异常必须先报错；普通缺失和额外项统一在结束时确认修正 | D02–D12、R01 |
| 5 | 快速模式真实端点与速度 | 从较深旧断点启动，确认直接接近未完成日期端点，不逐文件遍历已完成范围；再以真实外部新增相册验收首次增量，不漏新资源。同起始状态、同环境记录两模式定位耗时、首次新分片耗时及总耗时；复制或注入样本不替代自然新增证据 | R02 |

每项证据记录代码版本、群组与消息范围、样本来源、初始进度、校验/窗口模式、实际 BAT 日志或独立浏览器记录、退出码、耗时、完整性结论和原数据保护结果。模拟故障、真实故障、预期拒绝和下载完整性分别记录；隐私原始证据只留本机，公开摘要脱敏。

已有通过证据仅在代码、构造器和适用条件仍有效时复用；改动后重测受影响项，不无差别重跑全部矩阵。未执行、失败、外部条件缺失或证据不足须列入未完成/未验证，注明影响范围，不得宣称全部实测完成。当前遗留状态以 [遗留清单](plans/2026-11-08-remaining-verification.md) 为准。

下载范围仍为覆盖原已下载范围并按授权有界验证；不要求全历史下载，不恢复已取消的“每群额外下载两个完整头像段”要求。命名与分组只验收本文件当前规则，不沿用旧文字标题分组和旧路径格式。

其他适用场景及其逐项判定统一维护在 [实测目录](AGENT_CONTEXT.md#实测场景与执行)，不在多处重复场景表。历史验收摘要保存在 `finished_plans/`，当前状态见 [遗留索引](plans/2026-11-08-remaining-verification.md)。

离线入口：`python -m unittest discover -s tests -p "test_*.py" -q`。修改下载流程还需通过真实 `download.bat` 有界验证。
`tests/live_output_audit.py`、`tests/live_server_date_audit.py`、`tests/live_dom_audit.py` 分别核对成品、服务器日期与网页结构；只采集 ID、时间、类型和结构，不查看正文或图片。
`finished_plans/` 保存脱敏的阶段验收摘要，不能作为当前状态或原始证据的替代。原始日志、登录副本和精确路径只留本机；未完成和未验证项见 [当前遗留事项](plans/2026-11-08-remaining-verification.md)。

### 实测场景与执行

以下编号用于指定后续实测范围；列出场景不代表已执行或已通过。

交付前的五项优先顺序及通过标准统一见 [交付优先实测](AGENT_CONTEXT.md#交付优先实测)：浏览器独立核对、三群已有范围完整性、关联发布与空目录、不同进度交叉双模式、真实端点与速度。它们不替代本目录其他适用项目，也不表示现有证据已全部满足。

#### 当前三模式验收

大目录停顿反馈另用 `python tests/live_review_progress.py --group-number <配置序号> --hash-seconds 3` 验证：读取实际结构，哈希预算仅允许 1–10 秒，验证持续字节进度和取消后读取结束，核对正式源保护。它不执行服务器联合比较，不代表整组媒体 SHA 完成。中间字节进度、事件循环响应、取消、本轮免重复读取和同轮缺失/额外发现分别由离线反例与实际 BAT 回归覆盖。小媒体样本不能外推为近两万文件的耗时表现，实际证据见 [进度排障](finished_plans/2026-11-12-review-progress.md)。

全面入口为 `python tests/live_all_modes.py --suite <已验收的无group基线批次>`，默认依次执行 progress、storage、switches、menu、multi 五部分；可用 `--sections`、`--modes`、`--case` 缩小范围。它复用 18 种进度/成品构造，逐一运行 fast/exact/check，并增加生产目录形状、回车/EOF、合法版本、重复媒体、结构错误、六种双向模式切换、实际菜单和多群确认。实际结果与边界见 [全面实测](finished_plans/2026-11-12-all-modes-live.md)，列出不等于通过。

完成后运行 `python tests/check_all_modes_evidence.py --batch <all-modes批次>`，独立复读成功成品对应的提交分片摘要、偏移连续性、归档路径映射与归档文件摘要，检查空目录拒绝现场和原件保护。用 `--supplement <重跑批次>` 显式合并后续结果，保留首次失败；下载程序版本必须一致，正式数据在批次之间也必须一致。复核器使用中性样本反例验证同大小损坏与提交偏移缺口能被拒绝，不依赖控制台“完成”文字判断完整性。

检查模式对未完成分片只核对身份头及额外归属，不从 `.part` 读取或补传字节；因此 D06/D10/D11 等在检查模式“保持不动”不代表坏断点通过续传校验。孤立 payload 和身份不符的分片可作为额外项等待确认。可信/坏分片的实际字节验收仍由下载模式完成。所有构造均为副本，不冒称实际历史故障。

联合检查和确认修正的有界入口为 `python tests/live_storage_review.py --suite <已验收的无group基线批次>`。使用真实 BAT、隔离运行环境与登录副本，构造同群错位媒体、根层散落文件和启动前已有的重复辅助清单；缺失项不从本地副本恢复。9 个场景覆盖：快速不检查不询问；逐文件/检查的拒绝与确认；EOF 保持不动；逐文件/快速能下载未完成新内容而检查模式不下载。确认后比较恢复媒体与原参考 SHA-256，额外归档保留原位映射并核验哈希，正式数据保护独立验证。

后两种模式在所有所选群组流程结束后才问一次；没有问题时不问。检查阶段不写业务状态，逐文件下载阶段允许正常记录新增内容。已有辅助残留受保护，不得在最终确认前由旧收尾旁路归档。现存哈希/身份/结构冲突仍拒绝。媒体/原文缺失以最新完成登记路径为准，未完成分片与内部状态 JSON 不算已下载缺失；整个目录树或生成清单损坏不冒充普通缺件恢复成功。

旧阶段的“缺失立即拒绝”“检查模式永不修改”“错位文件快速归位”仅适用于当时版本。当前矩阵 L00/L03 改为快速信任、逐文件列出并等待确认；`reported_unmodified` 表示检查完成且默认拒绝后文件不变，不是缺件已恢复。其他旧手工故障脚本须按当前模式语义判断，不直接把旧断言当作新要求。

#### 选择与执行

```powershell
python tests/run_live_matrix.py --list
python tests/run_live_matrix.py --run --case D00 D04 D05 D06 --modes fast exact
python tests/run_live_matrix.py --run --category progress
python tests/run_live_matrix.py --run --case R01
python tests/run_live_matrix.py --run --case D04 --debug-window visible
python tests/run_live_matrix.py --run --resume "<项目根目录>/~temp/live-matrix/<批次>"
```

不带 `--run` 只列目录，不开浏览器。默认自动选择最新相册已在本机完整下载、总量不超过 80 MiB 且含大于三个分片附件的最小样本；可用 `--source-group <配置序号>` 和 `--max-sample-mib` 指定。原始文件、配置、扫描记录和浏览器资料只读，运行代码、登录资料、状态和媒体均复制到单独批次。

调试显示只有 `--debug-window hidden`（自动实测默认，无可见窗口）和 `--debug-window visible`（明确选择时显示 Chrome）两种，独立于 fast/exact。正常双击根目录 BAT 保持可见 CMD 和 Chrome；自动工具不创建可见终端，登录未就绪只报错，不自动弹窗。显示方式变化会重新验收所选场景，不复用另一显示方式的通过记录。

每个普通自动场景分别用 `fast`、`exact` 从独立副本执行实际 BAT；R01 固定包含 fast→exact、exact→fast 的可信断点切换，以及损坏成品 fast→exact 的拒绝验证，不受单模式选择限制。

续跑保留旧结果和失败现场，不物理删除文件。相同程序及场景版本下已通过的普通项目跳过；代码或构造器变化后选中项重新验收。试验期间若原下载任务同时写入原数据，保护核对会失败，不可据此宣称原件未变。

独立的遗留场景入口：

```powershell
python tests/prepare_live_audits.py --run
python tests/live_network_audit.py --suite "<上一步批次>" --source-group 3
python tests/live_archive_fault_audit.py --suite "<批次>" --baseline "<批次>/baseline"
python tests/live_archive_fault_audit.py --suite "<批次>" --baseline "<批次>/baseline" --case association_missing duplicate_media
python tests/live_remote_file_audit.py --suite "<批次>" --source-group 2 --mid <待查原消息号> --case original-thumbnail original-4k neighbor-4k
python tests/live_resume_audit.py --suite "<批次>" --source-group 3 --mid <已有失败消息号> --max-new-mib 768
python tests/live_failure_notes_audit.py --suite "<批次>"
python tests/live_failure_notes_audit.py --suite "<批次>" --legacy-registry
```

五项优先实测入口（默认无可见窗口）：

```powershell
python tests/run_priority_checks.py --prepare --hash
python tests/run_priority_checks.py --suite "<优先实测批次>" --observe-server --observe-dom --observe-replies
python tests/run_priority_checks.py --suite "<优先实测批次>" --observe-latest
python tests/run_priority_checks.py --suite "<优先实测批次>" --review
python tests/run_priority_checks.py --suite "<优先实测批次>" --check-source
python tests/run_priority_checks.py --suite "<优先实测批次>" --runtime-suite "<prepare_live_audits 批次>" --publication --staging-faults --deep-endpoint
```

盘点取指定旧重建清单中的媒体 ID，与成品、暂存及完成记录逐项核对，独立重读全部现有媒体 SHA-256；仍在旧归档中的候选副本也比对哈希。重新获取已下载范围元数据时同时检查漏记的远端普通消息，服务消息单列，不算媒体遗漏。独立 Chrome 只提取 ID、日期、类型和 DOM 结构，不导入产品导航或分组函数；头像段边界与相册成员对照实际清单，引用必须实际点击后确认同群目标可见。没有真实引用的群记录不适用，不能制造一条 Telegram 引用替代。

`--observe-latest` 只获取每群最新一页，记录真实新增普通消息和相册数，避免为等新增反复扫描旧区间。读取成功不代表首次增量下载通过，`incremental_download_verified` 仍为 false。DOM `data-timestamp` 只留诊断偏移统计；日期依据独立服务器原始时间核对，不能用 DOM 秒值覆盖可信日期。

关联发布用三个真实文字头像段的文件副本，修改副本元数据构造两跳引用；它验证发布逻辑，不冒称远端真实多跳案例。目标缺件时整个分量留暂存，恢复后逐文件核对三段、上层关联 JSON、内容哈希及活动空目录。暂存关联 JSON 缺失、损坏、错链分别执行 exact，必须先报错且文件与记录未改动。

深端点采用部分媒体副本及完成记录模拟大量已完成文件：明确保留 `partial_copies_unverified`，不称这些占位副本完整。选中缺件真实联网下载，独立核对所有提交分片、大小及最终哈希；同副本再次制造缺口，确认补标记后直接跳端点。与原件比对只适用于有可信原件的目标。双模式耗时比较另取矩阵 D00/D01 的同一起始状态与完整文件样本，不拿部分副本的 exact 拒绝结果比较速度。自然外部新增相册仍需另行等待真实条件，不以人工缺口或首次下载旧照片替代。

共享运行环境的 publication、staging-faults、deep-endpoint 必须顺序执行，避免互相改写测试配置；独立矩阵和观察器使用各自 Chrome 副本。证据与仍缺项见 [本轮计划](plans/2026-11-10-five-priority-live.md)。

准备入口选取原记录中已完整下载的小样本，仅复制媒体到隔离复用区，真实 BAT 新建状态；原件保护和样本哈希核对通过才算准备成功。默认最大 80 MiB，可指定 `--source-group`。服务器最新样本变化会触发范围保护，不静默扩大下载。

N00 网络入口需要一个可用的本机 HTTP 代理（默认端口 10808，可用 `--upstream-port` 修改）；测试再套一层独立代理，只切断本批连接，不修改系统网络。L04/L05 遗留入口实际运行逐文件 BAT，对缺失/损坏清单、空目录和重复文件逐一验收；旧脚本断言按当前三模式规则复核，普通重复文件不能沿用“立即拒绝”的旧预期；关联 JSON 使用现有最小真实关联分量的副本，先验证基线有效再造故障。500 诊断仅输出类型、字节数和时长，遇到限速即停止；原生调用超时会关闭隔离上下文，诊断完成不代表原件恢复。

旧消息回退链接的预测样例在真实 Chrome 中运行中性路由夹具，先复现同文档路由丢失消息参数，再验证产品卸载旧页后，以 `message` 原始编号定位正确群的相册成员；它属于离线验收，不替代 G05 的真实 Telegram 定位。插件调试连接中断、地址改变或旧气泡仍在 DOM 中，均不能算实际跳转成功。插件命令拆成单步，慢调用给足时限；不要把工具自身 30 秒中断误当作浏览器断线。实测见 [网页原消息定位](finished_plans/2026-11-10-web-message-navigation.md)。

已有失败断点续传入口只复制指定 `.part` 和提交日志；先检查剩余量上限，再从可信边界续传并独立复读最终 SHA-256。恢复文件只留测试暂存，原正式文件和记录保持不变，不能把副本恢复自动算作正式数据已修复。

N03 异常说明入口使用真实 BAT 和登录副本，在隔离运行环境中注入明确标记的服务器文件错误，验收快速模式自动说明、失败状态和索引排除，再关闭注入，以逐文件模式真实联网下载并独立复读哈希，确认说明逐文件归档。注入错误不算真实 Telegram 故障；真实 500 样例另用续传入口重试。离线预测样例另覆盖真实 TXT 附件、双重后缀、用户同名文件、说明被编辑和登记损坏。

`--legacy-registry` 仅在副本中将脚本生成的新登记改成旧前缀格式，再执行 exact，验证旧登记的只读识别及恢复后的安全迁移、归档。正常生成及归档名称不得带百分号标记；新旧登记冲突、损坏或内容被修改应保留现场并报错，不得静默合并。

代理运行均遵守当次窗口要求；这些隔离入口默认 hidden，正常用户 BAT 仍默认可见。可见模式的一次实际窗口验收、当前新增证据及尚缺条件见 [遗留闭环](finished_plans/2026-11-10-remaining-live-verification.md)。

#### 已知损坏资源的实测边界

后续专项恢复实测须先检查配置中 `scan_records` 上一级状态目录里的 `live-test-exclusions.json`。它只在本机记录 `version: 1` 和 `resources` 列表，每项包含 `peer`、`mid`、`classification: damaged`、用户处置原因；真实值不得写入公开配置或文档。原件诊断及已有失败断点续传两个入口会在打开浏览器、复制分片或请求媒体前拒绝已排除项，原始/内部消息号统一匹配。其他实测选样同样避开该资源，结构盘点仍保留其已知未下载事实。

该清单不改变正常下载程序：正常运行遇到它仍照常尝试，再按通用错误流程跳过、提示并留下原件同名 TXT；正式结果保持 failed，不设永久黑名单，不虚报 downloaded。某个资源被排除后，不再专项重试、补查存储原因或以恢复它作为交付条件；重新纳入须有用户明确的新要求。通用异常行为可用其他健康文件的隔离副本注入错误来验收。

#### 类型与预期

自动场景的输入均由副本人为构造，不能称为真实历史事故。网络与网页项来自会话需求、既有问题和预测边界，须按真实条件单独验收。

| 编号 | 分类 | 实测类型 | 执行方式 | 快速模式 | 逐文件模式 |
| --- | --- | --- | --- | --- | --- |
| D00 | progress | 全新下载，无文件和记录 | 自动副本实测 | 成功并独立核验完整文件 | 成功并独立核验完整文件 |
| D01 | progress | 全部完成，文件与记录一致 | 自动副本实测 | 成功并独立核验完整文件 | 成功并独立核验完整文件 |
| D02 | progress | 相册仅完成部分文件，未下载项无记录 | 自动副本实测 | 成功并独立核验完整文件 | 成功并独立核验完整文件 |
| D03 | progress | 完整文件已落盘，结果记录未提交 | 自动副本实测 | 成功并独立核验完整文件 | 成功并独立核验完整文件 |
| D04 | progress | 已提交分片与实际前缀一致 | 自动副本实测 | 成功并独立核验完整文件 | 成功并独立核验完整文件 |
| D05 | progress | 数据领先记录，未提交尾部一致 | 自动副本实测 | 成功并独立核验完整文件 | 成功并独立核验完整文件 |
| D06 | progress | 记录领先数据，提交数据缺失 | 自动副本实测 | 明确拒绝，保留问题证据 | 明确拒绝，保留问题证据 |
| D07 | progress | 只有分片文件，无提交日志 | 自动副本实测 | 明确拒绝，保留问题证据 | 明确拒绝，保留问题证据 |
| D08 | progress | 只有提交日志，无分片文件 | 自动副本实测 | 明确拒绝，保留问题证据 | 明确拒绝，保留问题证据 |
| D09 | progress | 断点媒体身份或大小与服务器不同 | 自动副本实测 | 明确拒绝，保留问题证据 | 明确拒绝，保留问题证据 |
| D10 | progress | 已提交分片同大小损坏 | 自动副本实测 | 明确拒绝，保留问题证据 | 明确拒绝，保留问题证据 |
| D11 | progress | 未提交尾部与服务器不一致 | 自动副本实测 | 明确拒绝，保留问题证据 | 明确拒绝，保留问题证据 |
| D12 | progress | 日志最后一行撕裂，可信前缀完整 | 自动副本实测 | 成功并独立核验完整文件 | 成功并独立核验完整文件 |
| D13 | progress | 分片全部提交，尚未最终入库 | 自动副本实测 | 成功并独立核验完整文件 | 成功并独立核验完整文件 |
| L00 | local | 已完成记录仍在，成品文件缺失 | 自动副本实测 | 信任完成记录，不检查缺失 | 报告缺失，流程结束默认不修改，可确认补齐 |
| L01 | local | 成品同大小损坏，时间戳保持不变 | 自动副本实测 | 可按完成记录跳过；完整性未通过 | 明确拒绝，保留问题证据 |
| L02 | local | 成品截断，但完成记录未更新 | 自动副本实测 | 可按完成记录跳过；完整性未通过 | 明确拒绝，保留问题证据 |
| L03 | local | 文件在错误头像段或目录 | 自动副本实测 | 信任完成记录，不检查错位副本 | 报告缺失/额外，默认保留，可确认服务器补齐及归档 |
| L04 | local | 缺少或损坏段内/关联清单 | 单独有界实测 | 依范围单独验收 | 明确拒绝，保留问题证据 |
| L05 | local | 空目录或同消息重复文件 | 单独有界实测 | 不主动检查已完成范围内的额外项；业务遇到的结构异常另行报错 | 空目录结构异常拒绝；普通重复文件列为额外项，默认保留，可确认归档 |
| G00 | grouping | 相册跨页、跨限额和断点中部 | 单独有界实测 | 相册完整且不缩小清单 | 相册完整且不缩小清单 |
| G01 | grouping | 头像段边界及关联组内多个完整头像段 | 单独有界实测 | 段界与网页一致 | 段界与网页一致 |
| G02 | grouping | 引用目标缺失或未下载，关联分量暂存 | 单独有界实测 | 不齐关联分量留暂存 | 不齐关联分量留暂存 |
| G03 | grouping | 引用多跳连通分量及发布前关联 JSON | 单独有界实测 | 完整引用清单再发布 | 完整引用清单再发布 |
| G04 | grouping | 文字/相册/单消息与所有附件类型 | 单独有界实测 | 内容分类完整或明确拒绝 | 内容分类完整或明确拒绝 |
| G05 | grouping | 不同群同消息号、相册成员定位、网页覆盖范围 | 单独有界实测 | 群与原消息号实际命中 | 群与原消息号实际命中 |
| N00 | network | 受控中断后重启，实际断网后同进程重连 | 单独有界实测 | 可信边界恢复；两种中断分别验收 | 可信边界恢复；两种中断分别验收 |
| N01 | network | 服务器 5xx、限流及文件引用刷新 | 单独有界实测 | 有界失败，未完成不假报成功 | 有界失败，未完成不假报成功 |
| N02 | network | 浏览器关闭、未登录和进群超时 | 单独有界实测 | 明确失败并保存进度 | 明确失败并保存进度 |
| N03 | network | 原件同名 TXT 自动说明及恢复归档 | 单独有界实测 | 脚本生成、未完成、索引排除 | 实际恢复后归档；本地冲突只读报错 |
| R00 | runtime | 真实 BAT 环境准备、参数、菜单与退出码 | 单独有界实测 | 实际入口、交互和退出码符合预期 | 实际入口、交互和退出码符合预期 |
| R01 | runtime | fast→exact / exact→fast 连续切换 | 自动副本实测 | 连续切换，状态与哈希保持正确 | 连续切换，状态与哈希保持正确 |
| R02 | runtime | 快速端点、自然新增相册及双模式耗时 | 单独有界实测 | 接近未完成端点再下载 | 选中范围逐文件哈希 |
| R03 | runtime | 磁盘不足、权限和同名冲突 | 单独有界实测 | 不覆盖、不隐匿异常 | 不覆盖、不隐匿异常 |
| A00 | audit | 原数据保护、实际文件与记录不遗漏、无假完成 | 单独有界实测 | 原件不变，记录与文件一致 | 原件不变，记录与文件一致 |
| A01 | audit | 服务器日期、路径字段、文字原文摘要和媒体哈希 | 单独有界实测 | 日期与路径元数据一致 | 原文及媒体完整校验 |
| A02 | audit | 网页独立比对，原始内容不展示，发布脱敏 | 单独有界实测 | 不展示内容，公开记录脱敏 | 不展示内容，公开记录脱敏 |

N00 的重启续传与同进程真实断网重连必须分别记录；N01 的 5xx、限流、引用刷新也分别记录，不以其中一个成功替代其余情形。R00 还包含数字菜单、帮助、错误参数、正常/失败/中断退出。G04 需覆盖照片、视频、音频、语音、视频便笺、动画、贴纸、文档（含 ZIP）、纯文字、投票、联系人、位置、链接预览，以及未知类型明确拒绝；文字与原始文件名的特殊字符、版本保存一起核对。

G00/G01 需检查消息时间新到旧、相册附件位置稳定、跨页/跨限额、头像段完整、日期及最旧消息号命名；G02/G03 需检查多跳引用和多个头像段、不齐目标不得进入成品、关联 JSON 先于成员发布。L04/L05 的缺失/损坏清单、关联 JSON、空目录、重复文件各为子项；结构异常只读报错，普通重复文件按当前额外项规则等待确认。

#### 结果如何判断

R00 同时包含 CMD 显示子项：当前消息只显示群内日期、当前文件/本段序号准确、传输后仍区分校验阶段、总量不触发预扫全历史、窄窗口/重定向可读；正常 BAT 默认可见，无可见/有可见窗口两种调试方式与 fast/exact 各自独立，登录失效不得自动弹窗。可见方式若未实际开窗体验，只能记为参数/模拟验证，不能称为真实界面验收通过。

- `expected_behavior_pass` 表示符合该输入在该模式下的预期；异常被正确拒绝也可通过该项。
- `integrity_pass` 单独表示样本输出实际大小/内容可被原始参考 SHA-256 验收，不能由退出码或哈希命中记录替代。
- 快速模式按完成标记跳过，同大小隐性损坏等情况可能不会检查出来；这种结果标为“信任记录但完整性未通过”，必须由逐文件模式拒绝，不能算损坏文件已验收。
- 失败文件、孤立断点或不可取得原件不得算作 downloaded。预期拒绝项目核对媒体及分片证据不被静默覆盖；正常日志与记录追加单列。
- 新下载需看到真实传输且独立校验；D13 的完整分片最终入库只是本地完成操作，不能冒充重新网络下载。

#### 证据位置与保护

旧关联清单收尾回归保留为历史证据；当前 `tests/live_association_cleanup.py` 命令入口委托三模式新验收。此前的自动收尾回归只复制基线并构造仅含重复清单、冲突清单或额外用户文件的暂存目录，实际 BAT 双模式运行，正例再重跑检查幂等；核对成品清单/媒体哈希、归档数量、来源目录收尾及正式数据保护。未完成成员和损坏段内清单另以中性离线样例验收，不将其冒充真实网络故障。证据在 `~temp/association-closeout/<批次>/`。

每次批次保存在 `~temp/live-matrix/<批次>/`：

| 文件或目录 | 用途 |
| --- | --- |
| `results.json` | 分项目、模式、退出码、耗时、可信断点、完整性及预期行为结果 |
| `source-before.json` / `source-after.json` | 原数据全范围文件数量/大小/修改时间/文件身份，以及样本、配置、源扫描记录的 SHA-256 对照 |
| `source-protection.json` | 原数据保护结论 |
| `runtime/` | 与项目源码和 BAT 对应的独立运行副本及独立 Python 环境 |
| `profile/` | 登录资料副本，不启动原 profile |
| `baseline/` | 一次真实空白下载产生的状态基线 |
| `cases/` | 全量或部分复制的媒体与构造状态，`held/` 保留被移出场景的副本文件 |
| `logs/` | 实际 BAT 输出和诊断日志 |

这些产物可能含本机路径、群号、原始文件名及登录资料，只留本机，不纳入公开 Git。复建入口为本页的 `tests/run_live_matrix.py` 命令；批次内文件不可当作原下载数据使用。

历史矩阵证据见 [进度矩阵验收](finished_plans/2026-11-10-download-progress-matrix.md)：19 类自动场景、42 种组合合并当时两个实现版本的有效证据后符合预期，其中 12 项为路径修复后复测。后续代码改动须按影响范围复核，不能将该记录写成最新版本全场景重跑通过。当前未完成与未验证范围见 [遗留清单](plans/2026-11-08-remaining-verification.md)。

##### 无 group 定位层与迁移进度

`--sample-kind standalone` 从原扫描记录选择包含无 group 附件的完整头像段，进度故障仍仅在副本构造。隔离运行副本的 `sitecustomize.py` 仅将服务器“最新页”请求的 offset 改为该头像段最新消息的下一号；后续分页、前一相册查询、消息解析和媒体下载均使用原实现及真实服务器。不会伪造空页、历史最早端或媒体字节。空的测试复用池只启用原生 API 媒体定位，不提供本地复用文件。该场景是受控历史窗口，不代表自然外部新增。

```powershell
python tests/run_live_matrix.py --run --sample-kind standalone --source-group 2 --case D00 D04 --modes fast exact
python tests/run_live_matrix.py --run --sample-kind standalone --source-group 2 --category progress --case R01 --resume "<项目>/~temp/live-matrix/<无group批次>"
python tests/live_layout_progress.py --suite "<项目>/~temp/live-matrix/<无group批次>"
```

普通矩阵也先检查专项实测排除政策。成功场景除了独立媒体哈希，还核对相册/无 group 目录层及无 group 的持久化前一相册证据；这些参考来自原数据，空白下载须独立向服务器查询定位。`--resume` 应使用同一采样参数和版本，已通过项按指纹跳过。

迁移入口复用已验收无 group 基线，在新副本恢复旧平铺布局，先用真实 BAT 确认普通下载要求显式迁移，再分别在移动一个文件后、全部媒体移动但扫描路径尚未更新时注入明确标记的异常。以实际迁移 BAT 重跑，核对不可变任务续跑、媒体 SHA-256/文件身份/目标层、前一相册记录和最后逐文件模式运行。受控异常不等于真实断电或撕裂扫描日志恢复。每次新建 `layout-progress/<时间>/`，保留 `results.json`、`source-protection.json` 及对应 BAT 日志；`layout-progress-latest.json` 仅指向最近一批，不覆盖首次失败证据。

迁移使用独立运行环境与登录副本。快照按实际复制文件重绑定路径/文件身份，保留参考 SHA-256；中断标记以对应诊断日志为准。上下文按所选参考消息逐一比较，允许服务器为仅扫描的邻条补齐记录。`--recheck-batch <批次>` 仅只读重验既有实际执行证据并另存结果，不重新启动 BAT；不得把未到注入点的构造失败补判为通过。
