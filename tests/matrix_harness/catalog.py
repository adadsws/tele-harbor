"""可选择的实测类型；预期异常与未执行项目不算下载成功。"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Case:
    id: str
    category: str
    title: str
    fixture: str
    fast: str
    exact: str
    automated: bool = True


CASES = (
    Case('D00', 'progress', '全新下载，无文件和记录', 'fresh', 'success', 'success'),
    Case('D01', 'progress', '全部完成，文件与记录一致', 'complete', 'success', 'success'),
    Case('D02', 'progress', '相册仅完成部分文件，未下载项无记录', 'partial_album', 'success', 'success'),
    Case('D03', 'progress', '完整文件已落盘，结果记录未提交', 'result_missing', 'success', 'success'),
    Case('D04', 'progress', '已提交分片与实际前缀一致', 'committed', 'success', 'success'),
    Case('D05', 'progress', '数据领先记录，未提交尾部一致', 'data_ahead', 'success', 'success'),
    Case('D06', 'progress', '记录领先数据，提交数据缺失', 'record_ahead', 'reject', 'reject'),
    Case('D07', 'progress', '只有分片文件，无提交日志', 'orphan_payload', 'reject', 'reject'),
    Case('D08', 'progress', '只有提交日志，无分片文件', 'orphan_journal', 'reject', 'reject'),
    Case('D09', 'progress', '断点媒体身份或大小与服务器不同', 'identity_changed', 'reject', 'reject'),
    Case('D10', 'progress', '已提交分片同大小损坏', 'committed_corrupt', 'reject', 'reject'),
    Case('D11', 'progress', '未提交尾部与服务器不一致', 'tail_conflict', 'reject', 'reject'),
    Case('D12', 'progress', '日志最后一行撕裂，可信前缀完整', 'torn_journal', 'success', 'success'),
    Case('D13', 'progress', '分片全部提交，尚未最终入库', 'full_partial', 'success', 'success'),
    Case('L00', 'local', '已完成记录仍在，成品文件缺失', 'final_missing', 'reject', 'reject'),
    Case('L01', 'local', '成品同大小损坏，时间戳保持不变', 'final_corrupt', 'trusted_unverified', 'reject'),
    Case('L02', 'local', '成品截断，但完成记录未更新', 'final_truncated', 'trusted_unverified', 'reject'),
    Case('L03', 'local', '文件在错误头像段或目录', 'wrong_path', 'repair_or_reject', 'reject'),
    Case('L04', 'local', '缺少或损坏段内/关联清单', 'manual', 'scope_dependent', 'reject', False),
    Case('L05', 'local', '空目录或同消息重复文件', 'manual', 'scope_dependent', 'reject', False),
    Case('G00', 'grouping', '相册跨页、跨限额和断点中部', 'manual', 'whole_album', 'whole_album', False),
    Case('G01', 'grouping', '头像段边界及关联组内多个完整头像段', 'manual', 'match_dom', 'match_dom', False),
    Case('G02', 'grouping', '引用目标缺失或未下载，关联分量暂存', 'manual', 'stage', 'stage', False),
    Case('G03', 'grouping', '引用多跳连通分量及发布前关联 JSON', 'manual', 'complete_links', 'complete_links', False),
    Case('G04', 'grouping', '文字/相册/单消息与所有附件类型', 'manual', 'typed_content', 'typed_content', False),
    Case('G05', 'grouping', '不同群同消息号、相册成员定位、网页覆盖范围', 'manual', 'target_ids', 'target_ids', False),
    Case('N00', 'network', '受控中断后重启，实际断网后同进程重连', 'manual', 'resume', 'resume', False),
    Case('N01', 'network', '服务器 5xx、限流及文件引用刷新', 'manual', 'bounded_failure', 'bounded_failure', False),
    Case('N02', 'network', '浏览器关闭、未登录和进群超时', 'manual', 'explicit_failure', 'explicit_failure', False),
    Case('R00', 'runtime', '真实 BAT 环境准备、参数、菜单与退出码', 'manual', 'entry', 'entry', False),
    Case('R01', 'runtime', 'fast→exact / exact→fast 连续切换', 'switch', 'switch', 'switch'),
    Case('R02', 'runtime', '快速端点、自然新增相册及双模式耗时', 'manual', 'endpoint', 'hash_range', False),
    Case('R03', 'runtime', '磁盘不足、权限和同名冲突', 'manual', 'preserve', 'preserve', False),
    Case('A00', 'audit', '原数据保护、实际文件与记录不遗漏、无假完成', 'manual', 'protected', 'protected', False),
    Case('A01', 'audit', '服务器日期、路径字段、文字原文摘要和媒体哈希', 'manual', 'metadata', 'full_hash', False),
    Case('A02', 'audit', '网页独立比对，原始内容不展示，发布脱敏', 'manual', 'private', 'private', False),
)


def select(ids=(), categories=()):
    """只选明确实现的类型；目录中的手工项不能冒充自动通过。"""
    known = {c.id for c in CASES}
    if set(ids) - known:
        raise ValueError('未知实测编号：' + ','.join(sorted(set(ids) - known)))
    chosen = [c for c in CASES if c.id in ids or c.category in categories]
    if not ids and not categories:
        chosen = [c for c in CASES if c.automated]
    manual = [c.id for c in chosen if not c.automated]
    if manual:
        raise ValueError('需单独有界实测的类型（本工具不自动执行）：' + ','.join(manual))
    if not chosen:
        raise ValueError('未选择任何实测类型')
    return chosen
