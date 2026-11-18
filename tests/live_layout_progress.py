"""真实 BAT 的布局迁移进度验收；只在已验收矩阵副本构造旧布局。"""
import argparse
import json
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path

from telegg.scan_record import ScanRecord
from telegg.local_config import CONFIG_FILE, load_paths
from live_test_policy import assert_live_test_allowed
from matrix_harness.execution import run_bat
from matrix_harness.fixtures import hold, rows, save_rows
from matrix_harness.isolation import configure, copy_case, files, prepare_runtime, protected_state, sha, write_json
from matrix_harness.history_window import install


INJECTOR = '''
import os
if os.environ.get('TELEGG_LAYOUT_STOP_AFTER'):
    import telegg.layout_migration as migration
    move = migration._move_one
    count = 0
    def interrupted(operation, roots):
        global count
        move(operation, roots)
        count += 1
        if count == int(os.environ['TELEGG_LAYOUT_STOP_AFTER']):
            raise RuntimeError('SYNTHETIC_LAYOUT_INTERRUPTION_AFTER_MOVE')
    migration._move_one = interrupted
'''


def legacy_copy(baseline, work):
    copy_case(baseline, work, {})
    record_path = next((work / 'scans').glob('*.jsonl'))
    record = ScanRecord(record_path)
    folders = {Path(row['detail']).parent for row in record.results.values()
               if row['status'] in ('downloaded', 'skipped', 'relocated')}
    mapping = {}
    for folder in folders:
        children = list(folder.iterdir())
        if any(not path.is_file() for path in children):
            raise ValueError('只支持没有嵌套目录的无 group 样本')
        for path in children:
            target = folder.parent / path.name
            if target.exists():
                raise ValueError('旧布局目标冲突')
            mapping[str(path)] = str(target)
            path.rename(target)
        hold(work, folder)
    for path in [record_path, *(work / 'partials').rglob('*.verified.jsonl')]:
        values = rows(path)
        values = [row for row in values if row.get('kind') != 'standalone_context']
        for row in values:
            for field in ('detail', 'path'):
                if row.get(field) in mapping:
                    row[field] = mapping[row[field]]
            if 'sha256' in row and 'path' in row:
                # 基线发布后，核验快照可能仍指向旧暂存路径。独立副本必须
                # 按实际结果文件重新绑定文件身份，保留原 SHA-256 信任基线。
                mid = row.get('identity', {}).get('mid')
                result = record.results.get(mid, {})
                actual = Path(mapping.get(result.get('detail'), result.get('detail', '')))
                if actual.is_file() and actual.name == Path(row['path']).name:
                    stat = actual.stat()
                    row.update(path=str(actual), size=stat.st_size, mtime_ns=stat.st_mtime_ns,
                               dev=stat.st_dev, ino=stat.st_ino)
        save_rows(path, values)
    return ScanRecord(record_path)


def migrate(runtime, peer, log, *, stop_after=None):
    previous_logs = set((runtime/'~temp/logs').glob('telegg-*.log'))
    env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'}
    if stop_after is not None:
        env['TELEGG_LAYOUT_STOP_AFTER'] = str(stop_after)
    else:
        env.pop('TELEGG_LAYOUT_STOP_AFTER', None)
    start = time.monotonic()
    with log.open('wb') as stream:
        proc = subprocess.Popen(['cmd', '/d', '/c', 'download.bat',
            f'https://web.telegram.org/k/#{peer}', '--migrate-standalone-layout',
            '--debug-window', 'hidden'], cwd=runtime, env=env, stdin=subprocess.PIPE,
            stdout=stream, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            proc.communicate(b'\n', timeout=240)
        except subprocess.TimeoutExpired:
            subprocess.run(['taskkill', '/PID', str(proc.pid), '/T', '/F'],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
            proc.wait()
            raise RuntimeError('布局迁移 BAT 超时')
    text = log.read_text(encoding='utf-8', errors='replace')
    diagnostic = set((runtime/'~temp/logs').glob('telegg-*.log')) - previous_logs
    injected = any('SYNTHETIC_LAYOUT_INTERRUPTION_AFTER_MOVE' in p.read_text(encoding='utf-8')
                   for p in diagnostic)
    return {'exit': proc.returncode, 'seconds': round(time.monotonic()-start, 2),
            'synthetic_interruption': injected,
            'media_transfer': '[传输]' in text}


def audit(suite):
    baseline = suite/'baseline'
    if not json.loads((suite/'baseline-result.json').read_text(encoding='utf-8'))['expected_behavior_pass']:
        raise ValueError('基线未通过')
    sample = json.loads((suite/'sample.json').read_text(encoding='utf-8'))
    if sample.get('kind') != 'standalone':
        raise ValueError('需要无 group 完整头像段批次')
    batch = suite/'layout-progress'/datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    batch.mkdir(parents=True)
    baseline_record = ScanRecord(next((baseline/'scans').glob('*.jsonl')))
    peer = baseline_record.path.stem
    for mid in baseline_record.results:
        assert_live_test_allowed(peer, mid)
    paths = load_paths()
    roots = {key: paths[key] for key in ('output', 'staging', 'partials', 'scan_records', 'chrome_profile')}
    roots['config'] = CONFIG_FILE
    hash_paths = [Path(p) for p in json.loads((suite/'source-before.json').read_text(encoding='utf-8'))['hashes']]
    protected_before = protected_state(roots, hash_paths)
    write_json(batch/'source-before.json', protected_before)
    runtime, profile = prepare_runtime(Path(__file__).resolve().parents[1], batch/'environment', paths['chrome_profile'])
    install(runtime, {'peer': peer, 'upper': sample['upper']})
    references = {mid: sha(Path(row['detail'])) for mid, row in baseline_record.results.items()
                  if row['status'] in ('downloaded', 'skipped', 'relocated')}
    layers = {mid: Path(baseline_record.results[mid]['detail']).parent.name for mid in references}
    hook = runtime/'src/sitecustomize.py'
    original = hook.read_text(encoding='utf-8')
    write_json(batch/'hook-before.json', {'body': original})
    hook.write_text(original + INJECTOR, encoding='utf-8')
    result = []
    try:
        for label, count in (('one-file-moved', 1), ('all-files-moved-before-record', len(references))):
            work = batch/label
            record = legacy_copy(baseline, work)
            configure(runtime, work, profile, peer)

            before = {mid: (sha(Path(row['detail'])), Path(row['detail']).stat().st_ino)
                      for mid, row in record.results.items() if mid in references}
            refusal_before = protected_state({'work': work}, list(files(work)))
            rejected = run_bat(runtime, batch/f'{label}-legacy.txt', peer,
                               'fast', set(references), limit=sample['limit'])
            reject_message = rejected.pop('_console')
            refusal_preserved = refusal_before == protected_state({'work': work}, list(files(work)))
            blocked = (rejected['exit'] == 1 and rejected['stop'] is None
                       and '--migrate-standalone-layout' in reject_message and refusal_preserved)
            failed = migrate(runtime, peer, batch/f'{label}-interrupted.txt', stop_after=count)
            pending = list((work/'layout-migrations'/peer).glob('*.json'))
            interrupted_ok = failed['exit'] == 1 and failed['synthetic_interruption']
            recovered = migrate(runtime, peer, batch/f'{label}-recovered.txt')
            after = ScanRecord(record.path)
            matches = sum(Path(after.results[mid]['detail']).is_file()
                          and sha(Path(after.results[mid]['detail'])) == digest
                          and Path(after.results[mid]['detail']).stat().st_ino == inode
                          and Path(after.results[mid]['detail']).parent.name == layers[mid]
                          for mid, (digest, inode) in before.items())
            contexts_ok = all(after.standalone_contexts.get(mid) == value
                              for mid, value in baseline_record.standalone_contexts.items())
            verified = run_bat(runtime, batch/f'{label}-exact.txt', peer,
                               'exact', set(references), limit=sample['limit'])
            verified.pop('_console')
            value = {'case': label, 'legacy_download_blocked': blocked,
                     'legacy_refusal_preserved': refusal_preserved,
                     'interrupted': failed, 'pending_plan_count': len(pending), 'recovered': recovered,
                     'files': len(references), 'hash_inode_layer_matches': matches,
                     'contexts_match': contexts_ok, 'exact': verified}
            value['passed'] = (blocked and interrupted_ok and len(pending) == 1
                and recovered['exit'] == 0 and not recovered['media_transfer']
                and matches == len(references) and contexts_ok and verified['exit'] == 0
                and verified['stop'] is None)
            result.append(value)
            write_json(batch/'results.json', result)
            print(json.dumps(value, ensure_ascii=False), flush=True)
    finally:
        hook.write_text(original, encoding='utf-8')
        protected_after = protected_state(roots, hash_paths)
        protection = {'unchanged': protected_before == protected_after}
        write_json(batch/'source-after.json', protected_after)
        write_json(batch/'source-protection.json', protection)
        write_json(suite/'layout-progress-latest.json', {'batch': str(batch)})
        print(json.dumps({'source_protection': protection}), flush=True)
    return 0 if all(row['passed'] for row in result) and protection['unchanged'] else 1


def recheck(suite, batch):
    """只重验既有真实执行证据；不重写原结果，不把构造失败补判为通过。"""
    baseline = ScanRecord(next((suite/'baseline/scans').glob('*.jsonl')))
    values = json.loads((batch/'results.json').read_text(encoding='utf-8'))
    diagnostics = list((batch/'environment/runtime/~temp/logs').glob('telegg-*.log'))
    for row in values:
        log = batch/f'{row["case"]}-interrupted.txt'
        matches = [p for p in diagnostics if abs(p.stat().st_mtime-log.stat().st_mtime) < 2
                   and 'SYNTHETIC_LAYOUT_INTERRUPTION_AFTER_MOVE' in p.read_text(encoding='utf-8')]
        record = ScanRecord(next((batch/row['case']/'scans').glob('*.jsonl')))
        row['interrupted']['synthetic_interruption'] = len(matches) == 1
        row['contexts_match'] = all(record.standalone_contexts.get(mid) == context
                                    for mid, context in baseline.standalone_contexts.items())
        references = {mid:Path(value['detail']) for mid,value in baseline.results.items()
                      if value.get('status') in ('downloaded','skipped','relocated')}
        row['current_hash_layer_matches'] = sum(
            Path(record.results.get(mid,{}).get('detail','')).is_file()
            and sha(Path(record.results[mid]['detail'])) == sha(path)
            and Path(record.results[mid]['detail']).parent.name == path.parent.name
            for mid,path in references.items())
        row['passed'] = (row['legacy_download_blocked'] and row['legacy_refusal_preserved']
            and row['interrupted']['exit'] == 1 and row['interrupted']['synthetic_interruption']
            and row['pending_plan_count'] == 1 and row['recovered']['exit'] == 0
            and not row['recovered']['media_transfer'] and row['hash_inode_layer_matches'] == row['files']
            and row['current_hash_layer_matches'] == row['files']
            and row['contexts_match'] and row['exact']['exit'] == 0 and row['exact']['stop'] is None)
    path = batch/f'validation-recheck-{datetime.now().strftime("%H%M%S%f")}.json'
    write_json(path, values)
    protection = json.loads((batch/'source-protection.json').read_text(encoding='utf-8'))
    print(json.dumps({'rechecked_cases':len(values),'passed':all(v['passed'] for v in values),
                      'source_protection':protection,'evidence':path.name}), flush=True)
    return 0 if all(v['passed'] for v in values) and protection['unchanged'] else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--suite', required=True, type=Path)
    parser.add_argument('--recheck-batch', type=Path, help='只重验已执行批次的诊断标记和所选上下文，不启动 BAT')
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[1]
    root = (project/'~temp/live-matrix').resolve()
    suite = args.suite.resolve()
    if not suite.is_relative_to(root) or suite == root:
        parser.error('suite 必须是项目 ~temp/live-matrix 下的单批次')
    if args.recheck_batch:
        batch = args.recheck_batch.resolve()
        if not batch.is_relative_to(suite/'layout-progress'):
            parser.error('recheck-batch 必须位于当前 suite/layout-progress 内')
        raise SystemExit(recheck(suite, batch))
    raise SystemExit(audit(suite))
