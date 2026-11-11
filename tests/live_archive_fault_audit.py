"""用已下载文件的副本实测清单、空目录和重复媒体；原数据不写入。"""
import argparse
import json
import re
import shutil
from datetime import datetime
from pathlib import Path

from telegg.local_config import load_paths
from telegg.local_validation import validate_local_archive
from telegg.scan_record import ScanRecord
from matrix_harness.execution import run_bat
from matrix_harness.isolation import configure, copy_case, sha, sync_code, write_json
from matrix_harness.validation import evidence


def remap(row, paths, baseline):
    field = 'detail' if row.get('kind') == 'result' else 'path'
    value = row.get(field)
    if not isinstance(value, str) or not Path(value).is_absolute():
        return row
    for name, source in paths.items():
        if name not in ('output', 'staging', 'partials') or not Path(value).is_relative_to(source):
            continue
        target = baseline / name / Path(value).relative_to(source)
        row[field] = str(target)
        if 'sha256' in row and target.is_file():
            stat = target.stat()
            row.update(size=stat.st_size, mtime_ns=stat.st_mtime_ns, ino=stat.st_ino, dev=stat.st_dev)
        break
    return row


def linked_baseline(suite):
    """仅复制最小的真实关联分量，保留全量分组元数据但不借用其他结果。"""
    paths = load_paths()
    candidates = []
    for group in paths['output'].iterdir():
        if group.is_dir():
            for linked in group.glob('*关联组'):
                size = sum(p.stat().st_size for p in linked.rglob('*') if p.is_file())
                candidates.append((size, linked))
    if not candidates:
        raise ValueError('没有可用真实关联分量，未执行关联 JSON 故障测试')
    size, linked = min(candidates)
    if size > 80 * 1024 * 1024:
        raise ValueError('最小关联分量超过 80 MiB，未扩大复制范围')
    peer = re.search(r'\[(-\d+)\]$', linked.parent.name).group(1)
    ids = {mid for manifest in linked.rglob('段内清单.json')
           for item in json.loads(manifest.read_text(encoding='utf-8'))['items']
           for mid in item['message_ids']}
    baseline = suite / ('linked-baseline-' + datetime.now().strftime('%H%M%S%f'))
    for source in linked.rglob('*'):
        if source.is_file():
            target = baseline / 'output' / source.relative_to(paths['output'])
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    rows = [json.loads(line) for line in (paths['scan_records'] / f'{peer}.jsonl')
            .read_text(encoding='utf-8').splitlines()]
    rows = [row for row in rows if row['kind'] not in ('group_verified', 'message_verified')
            and (row['kind'] not in ('result', 'content_file') or row.get('mid') in ids)]
    for mid in ids:
        for source in (paths['partials'] / peer).glob(mid + '*.jsonl'):
            target = baseline / 'partials' / peer / source.name
            target.parent.mkdir(parents=True, exist_ok=True)
            events = [remap(json.loads(line), paths, baseline)
                      for line in source.read_text(encoding='utf-8').splitlines()]
            target.write_text(''.join(json.dumps(e, ensure_ascii=False) + '\n' for e in events), encoding='utf-8')
    record = baseline / 'scans' / f'{peer}.jsonl'
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(''.join(json.dumps(remap(row, paths, baseline), ensure_ascii=False) + '\n'
                              for row in rows), encoding='utf-8')
    root = baseline / 'output' / linked.parent.name
    validate_local_archive(root, baseline / 'staging' / linked.parent.name,
                           ScanRecord(record), baseline / 'partials' / peer)
    return baseline


def change(kind, work, record):
    manifests = [p for name in ('output', 'staging') for p in (work / name).rglob('段内清单.json')]
    manifest = manifests[0]
    if kind == 'manifest_missing':
        manifest.rename(work / 'held-manifest.json')
    elif kind == 'manifest_bad':
        manifest.write_bytes(b'{broken')
    elif kind == 'empty_directory':
        (manifest.parent / 'empty-fixture').mkdir()
    elif kind == 'duplicate_media':
        target = Path(next(v['detail'] for v in record.results.values()
                           if v['status'] in ('downloaded', 'skipped', 'relocated')))
        shutil.copy2(target, target.with_name(target.stem + '.duplicate' + target.suffix))
    else:
        link = next((work / 'output').rglob('关联清单.json'))
        if kind == 'association_missing':
            link.rename(work / 'held-link.json')
        elif kind == 'association_bad':
            link.write_bytes(b'{broken')
        else:
            link.write_text('{"replies":[]}', encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--suite', type=Path, required=True)
    parser.add_argument('--baseline', type=Path, required=True)
    parser.add_argument('--case', nargs='+', choices=('manifest_missing', 'manifest_bad',
        'empty_directory', 'duplicate_media', 'association_missing', 'association_bad',
        'association_relations'), default=['manifest_missing', 'manifest_bad', 'empty_directory',
        'duplicate_media', 'association_missing', 'association_bad', 'association_relations'])
    args = parser.parse_args()
    suite, baseline = args.suite.resolve(), args.baseline.resolve()
    project = Path(__file__).resolve().parents[1]
    if not suite.is_relative_to(project / '~temp/remaining-live') or not baseline.is_relative_to(suite):
        parser.error('测试和基线必须在项目 ~temp/remaining-live 同一隔离批次内')
    sync_code(project, suite / 'runtime')
    associated = None
    results = []
    for kind in args.case:
        source = baseline
        if kind.startswith('association'):
            associated = associated or linked_baseline(suite)
            source = associated
        peer = next((source / 'scans').glob('*.jsonl')).stem
        work = suite / ('fault-' + kind + '-' + datetime.now().strftime('%H%M%S%f'))
        work.mkdir()
        copy_case(source, work, {})
        record = ScanRecord(work / 'scans' / f'{peer}.jsonl')
        change(kind, work, record)
        before, record_hash = evidence(work), sha(record.path)
        configure(suite / 'runtime', work, suite / 'profile', peer)
        run = run_bat(suite / 'runtime', suite / 'logs' / (work.name + '.txt'), peer,
                      'exact', set(record.results), timeout=100)
        text = run.pop('_console')
        result = {'case': kind, 'exit': run['exit'], 'local_error': '[本地数据错误]' in text,
                  'files_unchanged': before == evidence(work),
                  'records_unchanged': record_hash == sha(record.path), 'work': str(work)}
        result['passed'] = (result['exit'] == 1 and result['local_error']
                            and result['files_unchanged'] and result['records_unchanged'])
        results.append(result)
        write_json(suite / 'archive-fault-final.json', results)
        print(json.dumps({k: v for k, v in result.items() if k != 'work'}), flush=True)
    raise SystemExit(0 if all(r['passed'] for r in results) else 1)


if __name__ == '__main__':
    main()
