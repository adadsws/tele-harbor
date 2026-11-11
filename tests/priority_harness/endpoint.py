"""用部分文件副本模拟深处旧端点，真实下载缺件；不冒称占位副本完整性通过。"""
import json
import shutil
from datetime import datetime
from pathlib import Path

from telegg.group_menu import load_groups
from telegg.local_config import load_paths
from telegg.scan_record import ScanRecord
from telegg.content_types import DOWNLOADABLE
from telegg.layout import grouped, group_folder
import hashlib
from matrix_harness.execution import run_bat
from matrix_harness.fixtures import hold, prefix_copy
from matrix_harness.isolation import configure, copy_case, sha, sync_code, write_json


def select_target(record):
    complete = {'downloaded', 'skipped', 'relocated', 'saved_content', 'no_media'}
    incomplete = [mid for mid in record.source_messages
                  if record.results.get(mid, {}).get('status') not in complete]
    boundary = max(map(int, incomplete), default=0)
    if boundary:
        source = record.source_messages[str(boundary)]
        candidate = record.messages[source['bubble_mid']]
        kinds = dict(candidate.content_types)
        # 实际首个待下载相册若全为照片，直接验证真实未覆盖资源，不跳过它去测试更旧的人造缺口。
        ids = [mid for mid, *_ in candidate.parts if kinds.get(mid) in DOWNLOADABLE]
        if ids and all(kinds[mid] == 'photo' for mid in ids) and all(mid not in record.media for mid in ids):
            return candidate, ids, max(ids, key=int)
    candidates = []
    for item in record.ordered_messages():
        ids = [mid for mid, *_ in item.parts if mid in record.media]
        if not ids or min(int(p[0]) for p in item.parts) <= boundary:
            continue
        if any(record.results.get(mid, {}).get('status') not in {'downloaded', 'skipped', 'relocated'} for mid in ids):
            continue
        size = sum(Path(record.results[mid]['detail']).stat().st_size for mid in ids)
        if size <= 80 * 1024 * 1024:
            candidates.append((min(int(p[0]) for p in item.parts), item, ids))
    if not candidates:
        raise ValueError('没有符合范围的深端点完整小相册')
    _, item, ids = min(candidates, key=lambda row: row[0])
    mid = min(ids, key=lambda m: Path(record.results[m]['detail']).stat().st_size)
    return item, ids, mid


def destination(path, paths, work):
    for key in ('output', 'staging'):
        if path.is_relative_to(paths[key]):
            return work / key / path.relative_to(paths[key])
    messages = paths['scan_records'].parent / 'messages'
    if path.is_relative_to(messages):
        return work / 'messages' / path.relative_to(messages)
    raise ValueError('副本来源不在已知业务根或内容记录根')


def deep_fixture(paths, group, work):
    original = ScanRecord(paths['scan_records'] / f'{group.identity}.jsonl')
    item, album, missing = select_target(original)
    full = set(album)
    old_result = original.results.get(missing, {})
    trusted_original = old_result.get('status') in {'downloaded', 'skipped', 'relocated'}
    expected_hash = sha(Path(old_result['detail'])) if trusted_original else None
    partial_copies = copied = 0
    for mid, found in original.media.items():
        row = original.results.get(mid, {})
        if row.get('status') not in {'downloaded', 'skipped', 'relocated'}:
            continue
        old = Path(row['detail'])
        new = destination(old, paths, work)
        if mid in full:
            new.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(old, new)
        else:
            # 故意只复制前缀，验收快速模式信任记录的能力边界，不称这些副本已通过哈希。
            prefix_copy(old, new, min(1024, old.stat().st_size))
            partial_copies += 1
        copied += 1
        snapshot = paths['partials'] / group.identity / f'{mid}.verified.jsonl'
        if snapshot.is_file():
            values = [json.loads(line) for line in snapshot.read_text(encoding='utf-8').splitlines()]
            for value in values:
                value['path'] = str(new)
            target = work / 'partials' / group.identity / snapshot.name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(''.join(json.dumps(v, ensure_ascii=False) + '\n' for v in values), encoding='utf-8')
    for key in ('output', 'staging'):
        for group_root in paths[key].iterdir():
            if group_root.is_dir() and group_root.name.endswith(f'[{group.identity}]'):
                for old in group_root.rglob('*.json'):
                    if old.name in {'段内清单.json', '关联清单.json'}:
                        new = destination(old, paths, work)
                        new.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(old, new)
    for old_value in set(original.content_files.values()):
        old = Path(old_value)
        if old.is_file():
            new = destination(old, paths, work)
            new.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(old, new)
    # 重建当前有效视图，不重放含旧路径的历史事件，不复制旧断点与状态文件夹。
    record = ScanRecord(work / 'scans' / original.path.name)
    record.record_scan(original.ordered_messages(), reached_oldest=original.history_complete)
    record.append(list(original.source_messages.values()))
    rows = []
    for mid, row in original.results.items():
        if mid == missing:
            continue
        row = dict(row)
        if row.get('detail') and Path(row['detail']).is_absolute():
            row['detail'] = str(destination(Path(row['detail']), paths, work))
        rows.append(row)
    rows.extend({'kind': 'content_file', 'mid': mid, 'path': str(destination(Path(path), paths, work))}
                for mid, path in original.content_files.items())
    rows.extend({'kind': 'media', 'source_mid': found.source_mid, 'index': found.index,
                 'media_mid': mid, 'expected_size': found.expected_size}
                for mid, found in original.media.items())
    rows.extend({'kind': 'discovered', 'source_mid': mid} for mid in original.discovered)
    affected = next(group_folder(anchor) for anchor, batch in grouped(original.ordered_messages())
                    if any(block.mid == item.mid for block in batch))
    rows.extend({'kind': 'group_verified', 'group': name, 'signature': signature}
                for name, signature in original.verified_groups.items() if name != affected)
    rows.extend({'kind': 'message_verified', 'mid': mid, 'signature': signature}
                for mid, signature in original.verified_messages.items() if mid != item.mid)
    record.append(rows)
    if trusted_original:
        target = destination(Path(old_result['detail']), paths, work)
        hold(work, target)
        hold(work, work / 'partials' / group.identity / f'{missing}.verified.jsonl')
    limit = sum(int(mid) >= min(int(p[0]) for p in item.parts) for mid in original.source_messages)
    return missing, album, expected_hash, limit, copied, partial_copies


def verified_network_file(path, journal, snapshot, expected):
    if not path.is_file() or not journal.is_file() or not snapshot.is_file():
        return False
    rows = [json.loads(line) for line in journal.read_text(encoding='utf-8').splitlines()]
    digest = sha(path)
    proof = json.loads(snapshot.read_text(encoding='utf-8').splitlines()[-1])
    offset = 0
    with path.open('rb') as stream:
        for row in rows:
            if row['kind'] != 'chunk':
                continue
            if row['offset'] != offset or hashlib.sha256(stream.read(row['length'])).hexdigest() != row['sha256']:
                return False
            offset += row['length']
    return (offset == path.stat().st_size == rows[0]['size'] == proof['size']
            and digest == proof['sha256'] and (expected is None or digest == expected))


def repeat_gap(work, group, mid, path, record):
    """只移走本轮自有文件，保留首次在线核对补齐的其他消息标记。"""
    backup = work / 'retry-held'
    candidates = [path, *(work / 'partials' / group.identity).glob(f'{mid}.*')]
    for source in candidates:
        if not source.is_file():
            continue
        target = backup / source.relative_to(work)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            raise ValueError('重试副本归档目标已存在')
        source.rename(target)
    bubble = record.source_messages[mid]['bubble_mid']
    record.record_result(mid, 'failed')
    record.mark_message_verified(bubble, '0' * 64)


def run(project, suite):
    paths = load_paths()
    sync_code(project, suite / 'runtime')
    results = []
    for number, group in enumerate(load_groups(), 1):
        work = suite / ('deep-endpoint-' + str(number) + '-' + datetime.now().strftime('%H%M%S%f'))
        mid, album, expected, limit, copied, partial = deep_fixture(paths, group, work)
        configure(suite / 'runtime', work, suite / 'profile', group.identity)
        outcome = run_bat(suite / 'runtime', suite / 'logs' / (work.name + '.txt'), group.identity,
                          'fast', set(album), limit=limit, timeout=180)
        console = outcome.pop('_console')
        record = ScanRecord(work / 'scans' / f'{group.identity}.jsonl')
        row = record.results.get(mid, {})
        path = Path(row.get('detail', ''))
        partials = work / 'partials' / group.identity
        valid = verified_network_file(path, partials / f'{mid}.jsonl', partials / f'{mid}.verified.jsonl', expected)
        result = {'group': number, 'input': 'partial local copies with trusted completion records; one full-album gap',
                  'scope_raw_messages': limit, 'indexed_copies': copied, 'partial_copies_unverified': partial,
                  'exit': outcome['exit'], 'seconds': outcome['seconds'], 'stop': outcome['stop'],
                  'first_transfer_seconds': outcome['first_transfer_seconds'],
                  'first_network_seconds': outcome['first_network_seconds'],
                  'trusted_original_reference_available': expected is not None,
                  'real_network_started': '[下载] 新资源已开始' in console,
                  'target_chunks_size_and_hash_verified': valid}
        result['passed'] = (outcome['exit'] == 0 and outcome['stop'] is None and valid
                            and result['real_network_started'] and outcome['first_network_seconds'] is not None)
        if result['passed']:
            repeat_gap(work, group, mid, path, record)
            warm = run_bat(suite / 'runtime', suite / 'logs' / (work.name + '-warm.txt'), group.identity,
                           'fast', set(album), limit=limit, timeout=180)
            warm_console = warm.pop('_console')
            updated = ScanRecord(record.path)
            warm_path = Path(updated.results.get(mid, {}).get('detail', ''))
            warm_valid = verified_network_file(warm_path, partials / f'{mid}.jsonl',
                                               partials / f'{mid}.verified.jsonl', expected)
            result['warm'] = {'exit': warm['exit'], 'stop': warm['stop'], 'seconds': warm['seconds'],
                'first_network_seconds': warm['first_network_seconds'],
                'used_marker_shortcut': '按有效消息核验标记直接定位端点' in warm_console,
                'target_chunks_size_and_hash_verified': warm_valid}
            result['warm']['passed'] = (warm['exit'] == 0 and warm['stop'] is None and warm_valid
                                       and warm['first_network_seconds'] is not None)
            result['passed'] = result['passed'] and result['warm']['passed']
        write_json(work / 'result.json', result)
        results.append(result)
        print(json.dumps(result), flush=True)
    write_json(suite / 'deep-endpoint-result.json', results)
    return 0 if all(row['passed'] for row in results) else 1
