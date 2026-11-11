"""将独立服务器/DOM 观察与实际文件字段交叉核对，不读取私密正文。"""
import json
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from telegg.group_menu import load_groups
from telegg.local_config import load_paths
from telegg.scan_record import ScanRecord
from matrix_harness.isolation import write_json
from .observer import media_type


def run(suite):
    paths, results = load_paths(), []
    names = re.compile(r'^(\d{4}-\d{2}-\d{2})-m(\d+)_(\d+)_(\d+)(?:_|\.)')
    for number, group in enumerate(load_groups(), 1):
        record = ScanRecord(paths['scan_records'] / f'{group.identity}.jsonl')
        remote = json.loads((suite / f'server-group-{number}.json').read_text(encoding='utf-8'))
        dom = json.loads((suite / f'dom-group-{number}.json').read_text(encoding='utf-8'))
        pairs = {(p['left'], p['right']): p for p in dom['pairs']}
        issues, media_checked = [], 0
        for name, observed in (('server', remote), ('dom', dom)):
            if not observed['summary']['passed']:
                issues.append({'type': 'failed_observation', 'source': name})
        kinds = Counter()
        for mid, row in remote['rows'].items():
            kind = media_type(row)
            kinds[kind] += 1
            if kind != record.source_messages[mid]['content_type']:
                issues.append({'mid': mid, 'type': 'content_type'})
            found = record.media.get(mid)
            result = record.results.get(mid, {})
            if found is None or result.get('status') not in {'downloaded', 'skipped', 'relocated'}:
                continue
            path = Path(result['detail'])
            match = names.match(path.name)
            day = datetime.fromtimestamp(row['date'], timezone(timedelta(hours=8))).strftime('%Y-%m-%d')
            if not match or match.groups() != (day, mid, str(found.index), row['media_id']):
                issues.append({'mid': mid, 'type': 'media_filename_fields'})
            if row['size'] and row['size'] != path.stat().st_size:
                issues.append({'mid': mid, 'type': 'remote_file_size'})
            media_checked += 1
        for pair in pairs.values():
            if pair['dom_last_first'] != [not pair['same_segment']] * 2:
                issues.append({'type': 'avatar_boundary', 'ids': [pair['left'], pair['right']]})
        result = {'group': number, 'fresh_messages': len(remote['rows']), 'types': dict(kinds),
                  'media_fields_checked': media_checked,
                  'dom_messages': dom['summary']['observed_known_messages'],
                  'unique_dom_pairs': len(pairs),
                  'unique_dom_boundaries': sum(not p['same_segment'] for p in pairs.values()),
                  'issues': len(issues), 'passed': not issues}
        write_json(suite / f'review-group-{number}.json', {'summary': result, 'issues': issues})
        results.append(result)
        print(json.dumps(result), flush=True)
    write_json(suite / 'review-result.json', results)
    return 0 if all(r['passed'] for r in results) else 1
