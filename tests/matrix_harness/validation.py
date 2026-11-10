"""验收退出码、恢复字节与保留证据；不展示附件内容或原始文件名。"""
import json
from pathlib import Path

from telegg.scan_record import ScanRecord
from .isolation import sha


def evidence(work):
    """只记录关键文件的哈希，不将日志/记录正常追加误判为覆写证据。"""
    result = {}
    for directory in ('output', 'staging', 'partials'):
        for path in (work / directory).rglob('*'):
            if path.is_file() and not path.name.endswith('.verified.jsonl'):
                result[str(path.relative_to(work))] = sha(path)
    return result


def validate(case, mode, work, peer, reference, run, before):
    r = ScanRecord(work / 'scans' / f'{peer}.jsonl')
    valid = 0
    for mid, digest in reference.items():
        value = r.results.get(mid, {})
        path = Path(value.get('detail', ''))
        if value.get('status') in ('downloaded', 'skipped', 'relocated') and path.is_file():
            valid += sha(path) == digest
    expected = getattr(case, mode)
    messages = run.pop('_console')
    issues = []
    if run['stop'] is not None:
        issues.append('observer_stopped')
    if expected == 'success':
        if run['exit'] != 0 or valid != len(reference):
            issues.append('success_or_hash_missing')
    elif expected == 'reject':
        if run['exit'] != 1:
            issues.append('expected_rejection')
        after = evidence(work)
        for relative, digest in before.items():
            if after.get(relative) != digest:
                issues.append('failure_modified_file_evidence')
                break
    elif expected == 'trusted_unverified':
        if run['exit'] != 0 or '[下载] 新资源已开始' in messages:
            issues.append('unexpected_fast_behavior')
        if valid == len(reference):
            issues.append('corruption_fixture_not_present')
    elif expected == 'repair_or_reject':
        if not (run['exit'] == 1 or (run['exit'] == 0 and valid == len(reference))):
            issues.append('wrong_path_falsely_completed')
    empty = sum(d.is_dir() and not any(d.iterdir()) for directory in ('output', 'staging')
                for d in (work / directory).rglob('*'))
    if expected == 'success' and empty:
        issues.append('empty_business_directories')
    return {'case': case.id, 'mode': mode, 'expectation': expected, 'run': run,
            'expected_files': len(reference), 'intact_files': valid,
            'integrity_pass': valid == len(reference), 'expected_behavior_pass': not issues,
            'issues': issues, 'empty_folders': empty,
            'network_started': '[下载] 新资源已开始' in messages or '[下载] 续传已开始' in messages,
            'resume_displayed': '[续传]' in messages}
