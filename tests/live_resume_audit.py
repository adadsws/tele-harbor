"""复制已有失败断点并有界恢复；结果只留隔离暂存，不改正式数据。"""
import argparse
import asyncio
import json
import shutil
from pathlib import Path

from playwright.async_api import async_playwright

from telegg.browser import MediaTask, open_group, save_one, server_media_source, wait_for_login
from telegg.group_menu import parse_groups
from telegg.local_config import CONFIG_FILE, load_paths
from telegg.scan_record import ScanRecord
from telegg.failure_notes import REGISTRY, registered_notes
from matrix_harness.isolation import sha, write_json
from live_test_policy import assert_live_test_allowed


async def audit(args):
    paths = load_paths()
    group = parse_groups(json.loads(CONFIG_FILE.read_text(encoding='utf-8-sig'))['groups'])[args.source_group - 1]
    assert_live_test_allowed(group.identity, args.mid)
    record = ScanRecord(paths['scan_records'] / f'{group.identity}.jsonl')
    if record.results.get(args.mid, {}).get('status') != 'failed':
        raise ValueError('只允许选择已有失败记录，不扩大下载范围')
    source = paths['partials'] / group.identity
    journal, payload = source / f'{args.mid}.jsonl', source / f'{args.mid}.part'
    with journal.open(encoding='utf-8') as stream:
        header = json.loads(stream.readline())
    saved, total = payload.stat().st_size, header['size']
    if not 0 < total - saved <= args.max_new_mib * 1024 * 1024:
        raise ValueError('剩余下载量超出显式上限，未复制或启动下载')
    work = args.suite / f'resume-m{args.mid}'
    work.mkdir()
    partials = work / 'partials'
    partials.mkdir()
    before = {str(p): (p.stat().st_size, p.stat().st_mtime_ns) for p in (journal, payload, record.path)}
    for original in (journal, payload):
        shutil.copy2(original, partials / original.name)
    result = {'initial_saved': saved, 'total': total, 'maximum_new_bytes': total - saved,
              'original_record_status': 'failed', 'formal_data_changed': False}
    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(str(args.suite / 'profile'),
            channel='chrome', headless=True)
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto('https://web.telegram.org/k/', wait_until='domcontentloaded')
            await wait_for_login(page, timeout=30_000)
            await open_group(page, group.url, require_message=False)
            remote = await server_media_source(page, int(group.identity), args.mid)
            if remote['size'] != total:
                raise ValueError('服务器大小与断点不同，拒绝混接')
            found = record.media[args.mid]
            task = MediaTask(record.messages[found.source_mid], found.index, args.mid,
                             work / 'staging', total, partials)
            state, path, size, digest, _ = await asyncio.wait_for(
                save_one(page, None, task, {}, exact_verify=True, server_source=remote),
                timeout=args.timeout)
            result.update(state=state, size=size, independent_hash_matches=sha(path) == digest,
                          file=str(path), passed=size == total and sha(path) == digest)
        except Exception as error:
            result.update(passed=False, error_type=type(error).__name__)
            if hasattr(error, 'code'):
                result.update(server_error_code=error.code, server_error_type=error.error_type)
        finally:
            await context.close()
    after = {str(p): (p.stat().st_size, p.stat().st_mtime_ns) for p in (journal, payload, record.path)}
    result['original_metadata_unchanged'] = before == after
    notes = [(path.parent / name, entry) for path in (work / 'staging').rglob(REGISTRY)
             for name, entry in registered_notes(path.parent).items()]
    result['automatic_same_name_note'] = (len(notes) == 1 and notes[0][0].is_file()
        and notes[0][1]['mid'] == args.mid
        and notes[0][0].name == Path(notes[0][1]['media']).with_suffix('.txt').name)
    result['passed'] = result['passed'] and before == after
    write_json(work / 'result.json', result)
    print(json.dumps({key: value for key, value in result.items() if key != 'file'}), flush=True)
    return 0 if result['passed'] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--suite', type=Path, required=True)
    parser.add_argument('--source-group', type=int, required=True)
    parser.add_argument('--mid', required=True)
    parser.add_argument('--max-new-mib', type=int, default=768)
    parser.add_argument('--timeout', type=int, default=240)
    args = parser.parse_args()
    args.suite = args.suite.resolve()
    root = Path(__file__).resolve().parents[1] / '~temp/remaining-live'
    if not args.suite.is_relative_to(root.resolve()) or not (args.suite / 'profile').is_dir():
        parser.error('suite 必须是项目 ~temp/remaining-live 下的登录副本')
    if not args.mid.isdecimal() or args.max_new_mib <= 0 or args.timeout <= 0:
        parser.error('消息号、下载上限和时限必须有效')
    raise SystemExit(asyncio.run(audit(args)))


if __name__ == '__main__':
    main()
