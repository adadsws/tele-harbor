"""盘点旧媒体覆盖、现有文件与可信验收快照，生成本机哈希任务清单。"""
import json
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from telegg.group_menu import load_groups
from telegg.local_config import CONFIG_FILE, load_paths
from telegg.local_validation import validate_local_archive
from telegg.media_index import existing_media_index
from telegg.scan_record import ScanRecord
from telegg.verification_manifest import read_snapshot
from matrix_harness.isolation import protected_state, write_json


MEDIA = re.compile(r'^(?:\d{4}-\d{2}-\d{2}[-_]m|m)(\d+)_[1-9]\d*_(?:(?:document|photo|video|audio|animation|sticker)_)?\d+(?:[_.]|$)')


def old_coverage(paths):
    """只读指定重建清单，不遍历旧归档中的媒体或会话。"""
    ids, copies = defaultdict(set), defaultdict(list)
    journals = list((paths['output'].parent / '~local/~archive').glob('rebuild-empty-logic-*/moves.jsonl'))
    for journal in journals:
        for line in journal.read_text(encoding='utf-8').splitlines():
            row = json.loads(line)
            source = Path(row['from'])
            peer = next((m[1] for part in source.parts if (m := re.search(r'\[(-\d+)\]$', part))), None)
            match = MEDIA.match(source.name)
            if peer and match:
                mid = match[1]
                ids[peer].add(mid)
                target = Path(row['to'])
                if target.is_file() and not target.is_symlink():
                    copies[(peer, mid)].append(str(target))
    return ids, copies, len(journals)


def roots_for(paths, peer):
    roots = []
    for key in ('output', 'staging'):
        found = [p for p in paths[key].iterdir() if p.is_dir() and p.name.endswith(f'[{peer}]')]
        if len(found) > 1:
            raise ValueError('同一业务根存在重复群组目录')
        roots.append(found[0] if found else paths[key] / f'absent [{peer}]')
    return roots


def group_inventory(number, group, paths, baseline, copies):
    record = ScanRecord(paths['scan_records'] / f'{group.identity}.jsonl')
    output, staging = roots_for(paths, group.identity)
    validate_local_archive(output, staging, record, paths['partials'] / group.identity)
    index = existing_media_index(output, staging, known_text_media=set(record.media))
    issues, jobs = [], []
    complete = {'downloaded', 'skipped', 'relocated'}
    failed = [mid for mid, row in record.results.items() if row.get('status') == 'failed']
    for mid, found in record.media.items():
        result = record.results.get(mid, {})
        if result.get('status') not in complete:
            if mid not in failed:
                issues.append({'type': 'unfinished_media_record', 'mid': mid})
            continue
        files = index.get(mid, [])
        path = Path(result.get('detail', ''))
        if len(files) != 1 or files[0] != path or not path.is_file() or path.is_symlink():
            issues.append({'type': 'result_index_mismatch', 'mid': mid})
            continue
        snapshot = read_snapshot(paths['partials'] / group.identity / f'{mid}.verified.jsonl', strict=True, required=True)
        if (snapshot['identity']['mid'] != mid or snapshot['size'] != path.stat().st_size
                or (found.expected_size and found.expected_size != snapshot['size'])):
            issues.append({'type': 'snapshot_size_or_identity', 'mid': mid})
            continue
        stat = path.stat()
        jobs.append({'group': number, 'peer': group.identity, 'mid': mid, 'path': str(path),
                     'sha256': snapshot['sha256'], 'size': stat.st_size,
                     'stamp': [stat.st_mtime_ns, stat.st_ino, stat.st_dev],
                     'old_copies': copies.get((group.identity, mid), [])})
    missing_old = sorted(baseline.get(group.identity, set()) - index.keys(), key=int)
    if missing_old:
        issues.append({'type': 'old_media_missing', 'mids': missing_old})
    empty = sum(1 for root in (output, staging) for p in root.rglob('*')
                if p.is_dir() and not any(p.iterdir()))
    if empty:
        issues.append({'type': 'empty_business_directories', 'count': empty})
    summary = {'group': number, 'source_messages': len(record.source_messages),
               'old_media': len(baseline.get(group.identity, set())), 'missing_old': len(missing_old),
               'indexed_media': sum(map(len, index.values())), 'hash_jobs': len(jobs),
               'bytes': sum(j['size'] for j in jobs), 'known_failed': len(failed),
               'duplicate_ids': sum(len(v) > 1 for v in index.values()), 'empty_dirs': empty,
               'issues': len(issues)}
    return summary, jobs, issues


def prepare(project):
    paths = load_paths()
    suite = project / '~temp/priority-live' / datetime.now().strftime('run-%Y%m%d-%H%M%S-%f')
    suite.mkdir(parents=True)
    protected = {k: paths[k] for k in ('output', 'staging', 'partials', 'scan_records', 'chrome_profile')}
    protected['config'] = CONFIG_FILE
    hashes = [CONFIG_FILE, *paths['scan_records'].glob('*.jsonl')]
    write_json(suite / 'source-before.json', protected_state(protected, hashes))
    baseline, copies, journals = old_coverage(paths)
    summaries, jobs, issues = [], [], []
    for number, group in enumerate(load_groups(), 1):
        summary, selected, failures = group_inventory(number, group, paths, baseline, copies)
        summaries.append(summary)
        jobs.extend(selected)
        issues.extend({'group': number, **failure} for failure in failures)
        print(json.dumps(summary), flush=True)
    write_json(suite / 'inventory.json', {'groups': summaries, 'jobs': jobs, 'issues': issues,
                                        'old_coverage_journals': journals})
    write_json(project / '~temp/priority-live/latest.json', {'suite': str(suite)})
    print(json.dumps({'suite': str(suite), 'jobs': len(jobs), 'bytes': sum(j['size'] for j in jobs),
                      'old_coverage_journals': journals, 'inventory_passed': not issues}), flush=True)
    return suite
