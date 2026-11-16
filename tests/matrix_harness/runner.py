"""选择实测范围、复制基线并顺序执行两种模式，结果可复核。"""
import argparse
import json
import hashlib
import sys
from datetime import datetime
from pathlib import Path

from telegg.content_types import DOWNLOADABLE
from telegg.group_menu import parse_groups
from telegg.local_config import CONFIG_FILE, load_paths
from telegg.scan_record import ScanRecord
from telegg.layout import grouped
from live_test_policy import assert_live_test_allowed

from .catalog import CASES, select
from .execution import run_bat
from .fixtures import build
from .isolation import configure, copy_case, prepare_runtime, protected_state, sha, write_json, sync_code
from .validation import evidence, validate
from .history_window import install, configure_window

PROJECT = Path(__file__).resolve().parents[2]


def fingerprint(debug_window='hidden'):
    paths = [* (PROJECT / 'src').rglob('*.py'), *Path(__file__).parent.glob('*.py'),
             PROJECT / 'download.bat', PROJECT / 'requirements.txt', PROJECT / 'tests/live_test_policy.py']
    digest = hashlib.sha256()
    digest.update(debug_window.encode())
    for path in sorted(paths):
        digest.update(str(path.relative_to(PROJECT)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def sample(paths, group_number, max_mib, sample_kind='latest'):
    choices = []
    settings = json.loads(CONFIG_FILE.read_text(encoding='utf-8-sig'))
    for number, group in enumerate(parse_groups(settings.get('groups', [])), 1):
        if group_number is not None and number != group_number:
            continue
        r = ScanRecord(paths['scan_records'] / f'{group.identity}.jsonl')
        batches = ([[max(r.messages.values(), key=lambda m: max(int(p[0]) for p in m.parts))]]
                   if sample_kind == 'latest' else [batch for _, batch in grouped(r.ordered_messages())
                   if any(not m.album_id for m in batch)])
        for batch in batches:
            candidate = sample_batch(r, group, number, batch, max_mib, sample_kind)
            if candidate is not None:
                choices.append(candidate)
    if not choices:
        raise ValueError('没有满足体积限制且完整下载的样本；请选择其他群或提高 --max-sample-mib')
    return min(choices, key=lambda x: x['bytes'])


def sample_batch(r, group, number, batch, max_mib, sample_kind):
    ids = [mid for item in batch for mid, kind in item.content_types if kind in DOWNLOADABLE]
    if not ids or any(r.results.get(mid, {}).get('status') not in
                      ('downloaded', 'skipped', 'relocated') for mid in ids):
        return None
    for item in batch:
        for part in item.parts:
            assert_live_test_allowed(group.identity, part[0])
    source = {mid: Path(r.results[mid]['detail']) for mid in ids}
    sizes = {mid: p.stat().st_size for mid, p in source.items()}
    singles = {mid for item in batch if not item.album_id for mid, _ in item.content_types}
    large = [mid for mid, size in sizes.items() if size > 3 * 1024 * 1024
             and (sample_kind == 'latest' or mid in singles)]
    if not large or sum(sizes.values()) > max_mib * 1024 * 1024:
        return None
    info = {'group_number': number, 'peer': group.identity, 'files': source,
                    'bytes': sum(sizes.values()), 'target': min(large, key=sizes.get),
                    'record': r.path,
                    'layers': {mid: path.parent.name for mid, path in source.items()},
                    'contexts': {item.mid: r.standalone_contexts[item.mid] for item in batch
                                 if not item.album_id}}
    if sample_kind != 'latest':
        info.update(upper=max(int(p[0]) for item in batch for p in item.parts),
                    limit=sum(len(item.parts) for item in batch))
    return info

def run_case(case, mode, suite, runtime, profile, baseline, info, references, label=None, reuse=None,
             debug_window='hidden'):
    work = reuse or suite / 'cases' / (label or f'{case.id}-{mode}')
    if reuse is None:
        work.mkdir(parents=True)
        if case.fixture != 'fresh':
            originals = {p.name: p for p in info['files'].values()}
            copy_case(baseline, work, originals)
            fact = build(case, work, info['peer'], info['target'], info['files'][info['target']])
        else:
            fact = {'trusted_offset': 0}
    else:
        fact = {'continued_from_previous_mode': True}
    configure(runtime, work, profile, info['peer'])
    configure_window(work, info)
    before = evidence(work)
    log = suite / 'logs' / ((label or f'{case.id}-{mode}') + '.txt')
    write_json(log.with_suffix('.before.json'), before)
    result = run_bat(runtime, log, info['peer'], mode, set(references), debug_window=debug_window,
                     limit=info.get('limit', 1))
    result = validate(case, mode, work, info['peer'], references, result, before,
                      layers=info['layers'], contexts=info['contexts'])
    result['fixture'] = fact
    return result, work


def save_result(suite, results, value, version):
    value['fingerprint'] = version
    results.append(value)
    write_json(suite / 'results.json', results)
    print(json.dumps({k: value[k] for k in ('case', 'mode', 'expectation',
        'intact_files', 'expected_files', 'integrity_pass', 'expected_behavior_pass', 'issues')},
        ensure_ascii=False), flush=True)


def switches(suite, runtime, profile, baseline, info, references, results, version, debug_window):
    catalog = {c.id: c for c in CASES}
    scenarios = [('D04', ('fast', 'exact')), ('D04', ('exact', 'fast')),
                 ('L01', ('fast', 'exact'))]
    for index, (cid, modes) in enumerate(scenarios):
        sequence = '→'.join(modes)
        if all(any(r['case'] == cid and r['mode'] == mode and r.get('switch_sequence') == sequence
                   and r.get('fingerprint') == version and r['expected_behavior_pass'] for r in results)
               for mode in modes):
            print(json.dumps({'skipped_switch': cid, 'sequence': sequence}), flush=True)
            continue
        work = None
        for step, mode in enumerate(modes):
            result, work = run_case(catalog[cid], mode, suite, runtime, profile, baseline,
                info, references, label=f'R01-{index}-{step}-{len(results)}-{mode}', reuse=work,
                debug_window=debug_window)
            result['switch_sequence'] = sequence
            # 恢复后的第二模式应完整成功；损坏样本的 exact 仍须拒绝。
            save_result(suite, results, result, version)


def execute(args, chosen):
    paths = load_paths()
    info = sample(paths, args.source_group, args.max_sample_mib, args.sample_kind)
    parent = PROJECT / '~temp' / 'live-matrix'
    suite = (Path(args.resume).resolve() if args.resume else
             parent / datetime.now().strftime('run-%Y%m%d-%H%M%S-%f'))
    if not suite.resolve().is_relative_to(parent.resolve()) or suite.resolve() == parent.resolve():
        raise ValueError('--resume 必须是项目 ~temp/live-matrix 下的单次运行目录')
    if not args.resume:
        suite.mkdir(parents=True)
        (suite / 'logs').mkdir()
    roots = {k: paths[k] for k in ('output', 'staging', 'partials', 'scan_records', 'chrome_profile')}
    roots['config'] = CONFIG_FILE
    for root in roots.values():
        if suite.resolve().is_relative_to(root.resolve()) or root.resolve().is_relative_to(suite.resolve()):
            raise ValueError('实测根不能与原数据目录嵌套')
    hash_paths = [CONFIG_FILE, info['record'], *info['files'].values()]
    before = protected_state(roots, hash_paths)
    if args.resume:
        original = json.loads((suite / 'source-before.json').read_text(encoding='utf-8'))
        if original != json.loads(json.dumps(before)):
            raise RuntimeError('原数据相对本批开始时有变化，停止续跑，不重写原保护基线')
    else:
        write_json(suite / 'source-before.json', before)
    references = {mid: sha(path) for mid, path in info['files'].items()}
    write_json(suite / 'sample.json', {'group_number': info['group_number'],
               'files': len(references), 'bytes': info['bytes'], 'target': info['target'],
               'kind': args.sample_kind, 'upper': info.get('upper'), 'limit': info.get('limit', 1)})
    print(json.dumps({'suite': str(suite.relative_to(PROJECT)), 'sample_group': info['group_number'],
                     'sample_files': len(references), 'sample_bytes': info['bytes']}, ensure_ascii=False), flush=True)
    results = (json.loads((suite / 'results.json').read_text(encoding='utf-8'))
               if (suite / 'results.json').is_file() else [])
    current = fingerprint(args.debug_window)
    try:
        if args.resume:
            runtime, profile = suite / 'runtime', suite / 'profile'
            sync_code(PROJECT, runtime)
        else:
            runtime, profile = prepare_runtime(PROJECT, suite, paths['chrome_profile'])
        install(runtime, info)
        baseline = suite / 'baseline'
        fresh = next(c for c in CASES if c.id == 'D00')
        if args.resume:
            base_result = json.loads((suite / 'baseline-result.json').read_text(encoding='utf-8'))
        else:
            baseline.mkdir()
            configure(runtime, baseline, profile, info['peer'])
            configure_window(baseline, info)
            base_run = run_bat(runtime, suite / 'logs/baseline.txt', info['peer'], 'fast', set(references),
                               debug_window=args.debug_window, limit=info.get('limit', 1))
            base_result = validate(fresh, 'fast', baseline, info['peer'], references, base_run, {},
                                   layers=info['layers'], contexts=info['contexts'])
            base_result['fingerprint'] = current
            write_json(suite / 'baseline-result.json', base_result)
        if not base_result['expected_behavior_pass']:
            save_result(suite, results, base_result, current)
            raise RuntimeError('空白基线没有通过；停止依赖它的场景')
        for case in chosen:
            if case.fixture == 'switch':
                switches(suite, runtime, profile, baseline, info, references, results, current, args.debug_window)
                continue
            for mode in args.modes:
                if any(v['case'] == case.id and v['mode'] == mode and not v.get('switch_sequence')
                       and v.get('fingerprint') == current and v['expected_behavior_pass'] for v in results):
                    print(json.dumps({'skipped_passed': case.id, 'mode': mode}), flush=True)
                    continue
                if case.id == 'D00' and mode == 'fast' and base_result.get('fingerprint') == current:
                    value = base_result
                else:
                    value, _ = run_case(case, mode, suite, runtime, profile, baseline, info, references,
                                       label=f'{case.id}-{mode}-{len(results)}', debug_window=args.debug_window)
                save_result(suite, results, value, current)
    finally:
        after = protected_state(roots, hash_paths)
        write_json(suite / 'source-after.json', after)
        protection = {'file_metadata_unchanged': before['files'] == after['files'],
                      'sample_config_record_hashes_unchanged': before['hashes'] == after['hashes']}
        write_json(suite / 'source-protection.json', protection)
        write_json(PROJECT / '~temp/live-matrix/latest.json', {'suite': str(suite), 'protection': protection})
        print(json.dumps({'source_protection': protection}, ensure_ascii=False), flush=True)
    latest = {(r['case'], r['mode'], r.get('switch_sequence', '')): r for r in results
              if r.get('fingerprint') == current}
    passed = all(r['expected_behavior_pass'] for r in latest.values()) and all(protection.values())
    print(json.dumps({'cases': len(latest), 'expected_behavior_pass': passed,
                     'unexpected_failures': sum(not r['expected_behavior_pass'] for r in latest.values())}), flush=True)
    return 0 if passed else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description='只用副本模拟下载进度，执行真实 BAT 双模式实测')
    parser.add_argument('--list', action='store_true', help='列出全部实测类型，不启动浏览器')
    parser.add_argument('--run', action='store_true', help='明确开始真实网络实测')
    parser.add_argument('--case', nargs='+', default=[])
    parser.add_argument('--category', nargs='+', default=[])
    parser.add_argument('--modes', nargs='+', choices=('fast', 'exact'), default=['fast', 'exact'])
    parser.add_argument('--debug-window', choices=('hidden', 'visible'), default='hidden',
                        help='自动实测默认无可见窗口；仅明确选择 visible 时显示 Chrome')
    parser.add_argument('--source-group', type=int, help='原配置中的群组序号，默认选最小可用样本')
    parser.add_argument('--max-sample-mib', type=int, default=80)
    parser.add_argument('--sample-kind', choices=('latest', 'standalone'), default='latest',
                        help='standalone 选完整历史头像段，仅在隔离运行副本控制服务器历史起点')
    parser.add_argument('--resume', help='续跑已有隔离批次，同版本已通过项跳过')
    args = parser.parse_args(argv)
    if args.list or not args.run:
        for c in CASES:
            print(f'{c.id} | {c.category} | {"自动" if c.automated else "单独实测"} | {c.title}')
        return 0
    return execute(args, select(args.case, args.category))
