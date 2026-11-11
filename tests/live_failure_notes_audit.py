"""真实 BAT 验收自动说明：隔离副本注入文件错误，再联网恢复；不修改原数据。"""
import argparse
import json
import os
from datetime import datetime
from pathlib import Path

from telegg.failure_notes import REGISTRY, LEGACY_REGISTRY, registered_notes, registry_files
from telegg.media_index import existing_media_index
from telegg.scan_record import ScanRecord
from matrix_harness.execution import run_bat
from matrix_harness.fixtures import clear_completion, hold
from matrix_harness.isolation import configure, copy_case, sha, sync_code, write_json


INJECTOR = '''import os
if os.environ.get('TELEGG_TEST_NOTE_ERROR_MID'):
    import telegg.browser as browser
    from telegg.errors import RemoteFileError
    original = browser.fetch_resume_part
    async def fetch(page, source, offset, limit):
        if source['mid'] == os.environ['TELEGG_TEST_NOTE_ERROR_MID']:
            raise RemoteFileError(500, 'SYNTHETIC_TEST')
        return await original(page, source, offset, limit)
    browser.fetch_resume_part = fetch
'''


def audit(suite, *, legacy_registry=False):
    project = Path(__file__).resolve().parents[1]
    runtime, baseline = suite / 'runtime', suite / 'baseline'
    record_path = next((baseline / 'scans').glob('*.jsonl'))
    peer = record_path.stem
    before = ScanRecord(record_path)
    references = {mid: Path(row['detail']) for mid, row in before.results.items()
                  if row['status'] in {'downloaded', 'skipped', 'relocated'}}
    large = {mid: p for mid, p in references.items() if p.stat().st_size > 3 * 1024 * 1024}
    mid = min(large, key=lambda m: large[m].stat().st_size)
    work = suite / ('failure-notes-' + datetime.now().strftime('%H%M%S%f'))
    copy_case(baseline, work, {})
    copied_record = work / 'scans' / record_path.name
    record = ScanRecord(copied_record)
    target = Path(record.results[mid]['detail'])
    for path in (target, work / 'partials' / peer / f'{mid}.verified.jsonl',
                 work / 'partials' / peer / f'{mid}.jsonl'):
        hold(work, path)
    clear_completion(copied_record, mid)
    sync_code(project, runtime)
    configure(runtime, work, suite / 'profile', peer)
    (runtime / 'src/sitecustomize.py').write_text(INJECTOR, encoding='utf-8')
    previous = os.environ.get('TELEGG_TEST_NOTE_ERROR_MID')
    try:
        os.environ['TELEGG_TEST_NOTE_ERROR_MID'] = mid
        failed = run_bat(runtime, suite / 'logs' / f'{work.name}-fast.txt', peer, 'fast', set(references))
    finally:
        if previous is None:
            os.environ.pop('TELEGG_TEST_NOTE_ERROR_MID', None)
        else:
            os.environ['TELEGG_TEST_NOTE_ERROR_MID'] = previous
    marker = '[异常说明]' in failed.pop('_console')
    record = ScanRecord(copied_record)
    registries = [p for base in ('output', 'staging') for p in registry_files(work / base)]
    notes = [(p.parent / name, row) for p in registries
             for name, row in registered_notes(p.parent).items()]
    indexed = existing_media_index(work / 'output', work / 'staging', known_text_media=set(references))
    failure_ok = (failed['exit'] == 1 and failed['stop'] is None and marker
                  and record.results.get(mid, {}).get('status') == 'failed' and mid not in indexed
                  and len(notes) == 1 and notes[0][0].is_file()
                  and notes[0][0].name == Path(notes[0][1]['media']).with_suffix('.txt').name
                  and all(p.name == REGISTRY for p in registries))
    result = {'error_source': 'synthetic RemoteFileError; real BAT and Telegram metadata',
              'fast_failure': failed, 'automatic_note_passed': failure_ok,
              'legacy_registry_fixture': legacy_registry}
    if failure_ok:
        if legacy_registry:
            # 仅在隔离副本构造旧版本数据，正常程序不生成旧前缀。
            registry = registries[0]
            registry.rename(registry.with_name(LEGACY_REGISTRY))
        restored = run_bat(runtime, suite / 'logs' / f'{work.name}-exact.txt', peer, 'exact', set(references))
        network = '[下载] 新资源已开始' in restored.pop('_console')
        record = ScanRecord(copied_record)
        matches = sum(row.get('status') in {'downloaded', 'skipped', 'relocated'}
                      and Path(row.get('detail', '')).is_file()
                      and sha(Path(row['detail'])) == sha(original)
                      for m, original in references.items() for row in [record.results.get(m, {})])
        active = [p for base in ('output', 'staging') for p in registry_files(work / base)]
        archive = list(registry_files(work / '~local/~archive'))
        archive_names_valid = bool(archive) and all(p.name == REGISTRY for p in archive)
        recovery_ok = (restored['exit'] == 0 and restored['stop'] is None and network
                       and matches == len(references) and not active and archive_names_valid)
        result.update(exact_recovery=restored, real_network_download_started=network,
                      file_count=len(references), hash_matches=matches,
                      note_archived=bool(archive), archive_names_valid=archive_names_valid,
                      recovery_passed=recovery_ok)
    result['passed'] = failure_ok and result.get('recovery_passed', False)
    write_json(work / 'result.json', result)
    print(json.dumps(result), flush=True)
    return 0 if result['passed'] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--suite', type=Path, required=True)
    parser.add_argument('--legacy-registry', action='store_true', help='在副本中构造旧登记，验收只读兼容及恢复迁移')
    args = parser.parse_args()
    suite = args.suite.resolve()
    root = Path(__file__).resolve().parents[1] / '~temp/remaining-live'
    if not suite.is_relative_to(root.resolve()) or not (suite / 'prepare-result.json').is_file():
        parser.error('suite 必须是已准备的隔离实测批次')
    if not json.loads((suite / 'prepare-result.json').read_text(encoding='utf-8'))['passed']:
        parser.error('批次基线验收未通过')
    raise SystemExit(audit(suite, legacy_registry=args.legacy_registry))


if __name__ == '__main__':
    main()
