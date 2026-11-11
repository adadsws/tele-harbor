"""准备可重建的有界实测批次：仅复制登录资料和媒体，状态由实际 BAT 新建。"""
import argparse
import json
import shutil
from datetime import datetime
from pathlib import Path

from telegg.local_config import CONFIG_FILE, load_paths
from telegg.scan_record import ScanRecord
from matrix_harness.execution import run_bat
from matrix_harness.isolation import configure, prepare_runtime, protected_state, sha, write_json
from matrix_harness.runner import sample


def prepare(args):
    project = Path(__file__).resolve().parents[1]
    paths = load_paths()
    info = sample(paths, args.source_group, args.max_sample_mib)
    suite = project / '~temp/remaining-live' / datetime.now().strftime('run-%Y%m%d-%H%M%S-%f')
    roots = {key: paths[key] for key in ('output', 'staging', 'partials', 'scan_records', 'chrome_profile')}
    roots['config'] = CONFIG_FILE
    if any(suite.is_relative_to(root) or root.is_relative_to(suite) for root in roots.values()):
        raise ValueError('隔离测试根与原数据目录重叠，未创建批次')
    hashes = [CONFIG_FILE, info['record'], *info['files'].values()]
    before = protected_state(roots, hashes)
    suite.mkdir(parents=True)
    (suite / 'logs').mkdir()
    write_json(suite / 'source-before.json', before)
    runtime, profile = prepare_runtime(project, suite, paths['chrome_profile'])
    work = suite / 'baseline'
    pool = work / '~local' / info['peer']
    pool.mkdir(parents=True)
    for original in info['files'].values():
        shutil.copy2(original, pool / original.name)
    configure(runtime, work, profile, info['peer'])
    result = run_bat(runtime, suite / 'logs/baseline.txt', info['peer'], 'fast', set(info['files']))
    console = result.pop('_console')
    record = ScanRecord(work / 'scans' / f'{info["peer"]}.jsonl')
    matches = sum(mid in record.results and Path(record.results[mid].get('detail', '')).is_file()
        and sha(Path(record.results[mid]['detail'])) == sha(original)
        for mid, original in info['files'].items())
    after = protected_state(roots, hashes)
    protected = before == after
    result.update(files=len(info['files']), hash_matches=matches,
        source_unchanged=protected, network_download_started='[下载] 新资源已开始' in console)
    result['passed'] = (result['exit'] == 0 and matches == len(info['files']) and protected
                        and not result['network_download_started'])
    write_json(suite / 'source-after.json', after)
    write_json(suite / 'prepare-result.json', result)
    print(json.dumps({'suite': str(suite), **result}), flush=True)
    return 0 if result['passed'] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', action='store_true', help='明确准备副本并运行真实 BAT')
    parser.add_argument('--source-group', type=int)
    parser.add_argument('--max-sample-mib', type=int, default=80)
    args = parser.parse_args()
    if not args.run:
        parser.print_help()
        return
    raise SystemExit(prepare(args))


if __name__ == '__main__':
    main()
