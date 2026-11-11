"""用真实文本段和最新相册副本构造三段引用链，执行真实 BAT 发布与拒绝发布。"""
import json
import shutil
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from telegg.group_menu import load_groups
from telegg.local_config import load_paths
from telegg.scan_record import ScanRecord
from matrix_harness.execution import run_bat
from matrix_harness.isolation import configure, sha, write_json, sync_code
from matrix_harness.validation import evidence
from .inventory import roots_for


def select_segments(record, output):
    candidates = []
    for manifest in output.glob('*/段内清单.json'):
        ids = [mid for row in json.loads(manifest.read_text(encoding='utf-8'))['items'] for mid in row['message_ids']]
        if (len(ids) == 1 and record.source_messages[ids[0]]['content_type'] == 'text'
                and Path(record.content_files.get(ids[0], '')).suffix == '.txt'):
            candidates.append((ids[0], manifest.parent))
    if len(candidates) < 3:
        raise ValueError('需要三个真实独立纯文字头像段')
    return sorted(candidates, key=lambda row: int(row[0]))[:3]


def build(project, suite, work):
    paths = load_paths()
    group = load_groups()[2]
    original = ScanRecord(paths['scan_records'] / f'{group.identity}.jsonl')
    output, staging = roots_for(paths, group.identity)
    selected = select_segments(original, output)
    edges = [(selected[1][0], selected[0][0]), (selected[2][0], selected[1][0])]
    reply_by_mid = dict(edges)
    items = [replace(item, reply_targets=tuple((mid, reply_by_mid[mid]) for mid, *_ in item.parts if mid in reply_by_mid))
             for item in original.ordered_messages()]
    record = ScanRecord(work / 'scans' / f'{group.identity}.jsonl')
    record.record_scan(items, reached_oldest=False)
    root = work / 'staging' / output.name
    linked = root / (selected[0][1].name + ' 关联组')
    reference = {}
    for mid, source in selected:
        for old in source.rglob('*'):
            if old.is_file():
                target = linked / source.name / old.relative_to(source)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(old, target)
        path = linked / source.name / Path(original.content_files[mid]).name
        record.record_content_file(mid, path)
        record.record_result(mid, original.results[mid]['status'])
        reference[mid] = (path, sha(path))
    write_json(linked / '关联清单.json', {'replies': [{'source_message_id': a, 'target_message_id': b} for a, b in edges]})
    latest = max(original.messages.values(), key=lambda m: max(int(part[0]) for part in m.parts))
    latest_ids = {part[0] for part in latest.parts}
    # 最新相册保持真实完整副本，作为 --limit 1 的入口范围；其他原始元数据保留以确认段界。
    for mid in latest_ids:
        old_content = Path(original.content_files[mid])
        if old_content.suffix == '.txt':
            content_root = next(r for r in (output, staging) if old_content.is_relative_to(r))
            content = root / old_content.relative_to(content_root)
        else:
            content = work / 'messages' / output.name / old_content.name
        content.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(old_content, content)
        record.record_content_file(mid, content)
        old = Path(original.results[mid]['detail'])
        old_root = next(r for r in (output, staging) if old.is_relative_to(r))
        new = root / old.relative_to(old_root)
        new.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(old, new)
        record.record_result(mid, original.results[mid]['status'], detail=str(new), naming_revision=3)
        found = original.media[mid]
        record.record_discovered(found.source_mid, [found])
        journal = paths['partials'] / group.identity / f'{mid}.verified.jsonl'
        rows = [json.loads(line) for line in journal.read_text(encoding='utf-8').splitlines()]
        for row in rows:
            row['path'] = str(new)
        target = work / 'partials' / group.identity / journal.name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in rows), encoding='utf-8')
    # 当前最新段清单从正式完整清单复制，不因只处理一个相册缩小。
    for parent in (output, staging):
        for manifest in parent.rglob('段内清单.json'):
            ids = {mid for row in json.loads(manifest.read_text(encoding='utf-8'))['items'] for mid in row['message_ids']}
            if ids & latest_ids:
                target = root / manifest.relative_to(parent)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(manifest, target)
    missing = selected[0][0]
    target = Path(record.content_files[missing])
    held = work / 'held' / target.name
    held.parent.mkdir(parents=True)
    target.rename(held)
    record.record_result(missing, 'failed')
    return group, record, linked, selected, edges, reference, missing, held, target


def run(project, suite):
    sync_code(project, suite / 'runtime')
    results = []
    for mode in ('fast', 'exact'):
        work = suite / ('publication-' + mode + '-' + datetime.now().strftime('%H%M%S%f'))
        values = build(project, suite, work)
        group, record, linked, selected, edges, reference, missing, held, target = values
        configure(suite / 'runtime', work, suite / 'profile', group.identity)
        allowed = set(record.media)
        pending = run_bat(suite / 'runtime', suite / 'logs' / f'{work.name}-pending.txt', group.identity, mode, allowed)
        pending.pop('_console')
        out = work / 'output' / linked.parent.name / linked.name
        remained = all((linked / source.name).is_dir() for _, source in selected)
        pending_ok = pending['exit'] == 0 and not out.exists() and remained
        held.rename(target)
        original = ScanRecord(load_paths()['scan_records'] / f'{group.identity}.jsonl')
        record = ScanRecord(record.path)
        record.record_result(missing, original.results[missing]['status'])
        complete = run_bat(suite / 'runtime', suite / 'logs' / f'{work.name}-complete.txt', group.identity, mode, allowed)
        complete.pop('_console')
        parent_manifest = out / '关联清单.json'
        actual = (json.loads(parent_manifest.read_text(encoding='utf-8'))['replies'] if parent_manifest.is_file() else [])
        pairs = {(row['source_message_id'], row['target_message_id']) for row in actual}
        segments_ok = all((out / source.name / '段内清单.json').is_file() for _, source in selected)
        hashes_ok = all((out / p.parent.name / p.name).is_file() and sha(out / p.parent.name / p.name) == digest
                        for p, digest in reference.values())
        empty = sum(1 for base in ('output', 'staging') for d in (work / base).rglob('*')
                    if d.is_dir() and not any(d.iterdir()))
        result = {'mode': mode, 'fixture': 'synthetic two-hop reply chain from three real text segments',
                  'pending_exit': pending['exit'], 'pending_stayed_staging': pending_ok,
                  'complete_exit': complete['exit'], 'segments': len(selected),
                  'links_correct': pairs == set(edges), 'segments_complete': segments_ok,
                  'copied_content_hashes_match': hashes_ok, 'empty_directories': empty}
        result['passed'] = pending_ok and complete['exit'] == 0 and pairs == set(edges) and segments_ok and hashes_ok and not empty
        write_json(work / 'result.json', result)
        results.append(result)
        print(json.dumps(result), flush=True)
    return 0 if all(r['passed'] for r in results) else 1


def staging_faults(project, suite):
    """损坏的暂存清单必须在任何自动整理或记录追加前被拒绝。"""
    sync_code(project, suite / 'runtime')
    results = []
    for kind in ('missing', 'corrupt', 'wrong_links'):
        work = suite / ('staged-link-' + kind + '-' + datetime.now().strftime('%H%M%S%f'))
        values = build(project, suite, work)
        group, record, linked, *_ = values
        manifest = linked / '关联清单.json'
        held = work / 'held/link-original.json'
        if kind == 'missing':
            manifest.rename(held)
        else:
            shutil.copy2(manifest, held)
            manifest.write_bytes(b'{broken' if kind == 'corrupt' else b'{"replies":[]}')
        configure(suite / 'runtime', work, suite / 'profile', group.identity)
        before, record_hash = evidence(work), sha(record.path)
        outcome = run_bat(suite / 'runtime', suite / 'logs' / (work.name + '.txt'),
                          group.identity, 'exact', set(record.media))
        console = outcome.pop('_console')
        result = {'case': kind, 'exit': outcome['exit'], 'local_error': '[本地数据错误]' in console,
                  'files_unchanged': before == evidence(work), 'record_unchanged': record_hash == sha(record.path)}
        result['passed'] = (result['exit'] == 1 and result['local_error']
                            and result['files_unchanged'] and result['record_unchanged'])
        write_json(work / 'result.json', result)
        results.append(result)
        print(json.dumps(result), flush=True)
    write_json(suite / 'staged-link-faults.json', results)
    return 0 if all(row['passed'] for row in results) else 1
