"""Manually compare bounded Telegram Web DOM metadata with finished manifests.

Run from the project root with PYTHONPATH=src and one or more group Web K URLs.
This script reads IDs, timestamps and DOM grouping classes only. DOM dates are
diagnostic; compare authoritative server dates with live_server_date_audit.py.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter
from pathlib import Path

from playwright.async_api import async_playwright

from telegg.browser import open_group, wait_for_login
from telegg.local_config import load_paths
from telegg.scan_record import ScanRecord


SNAPSHOT = """() => [...document.querySelectorAll('#column-center .bubble[data-mid]')]
    .map(e => ({
        id: e.getAttribute('data-mid'),
        timestamp: e.getAttribute('data-timestamp'),
        first: e.classList.contains('is-group-first'),
        last: e.classList.contains('is-group-last'),
        service: e.classList.contains('service'),
        members: [...e.querySelectorAll('.grouped-item[data-mid]')]
            .map(x => x.getAttribute('data-mid'))
    }))"""


def expected(group_folder: Path) -> tuple[dict[str, str], set[tuple[str, ...]]]:
    membership: dict[str, str] = {}
    albums: set[tuple[str, ...]] = set()
    for manifest in group_folder.rglob('段内清单.json'):
        segment = str(manifest.parent)
        for item in json.loads(manifest.read_text(encoding='utf-8'))['items']:
            ids = tuple(item['message_ids'])
            for mid in ids:
                if mid in membership:
                    raise RuntimeError(f'同一消息出现在多个成品段：m{mid}')
                membership[mid] = segment
            if len(ids) > 1:
                albums.add(ids)
    return membership, albums


async def audit(page, url: str, output: Path, scans: Path) -> dict:
    identity = url.rsplit('#', 1)[-1]
    folders = [p for p in output.iterdir() if p.is_dir() and p.name.endswith(f'[{identity}]')]
    if len(folders) != 1:
        raise RuntimeError(f'成品群组目录数量不是 1：{identity}')
    membership, expected_albums = expected(folders[0])
    if not membership:
        raise RuntimeError(f'没有成品消息清单：{identity}')
    records = ScanRecord(scans / f'{identity}.jsonl')
    target = min(map(int, membership))
    seen: set[str] = set()
    observed_albums: set[tuple[str, ...]] = set()
    checked_pairs: set[tuple[str, str]] = set()
    service_bridges: set[tuple[str, str]] = set()
    bad_pairs: set[tuple[str, str]] = set()
    timestamps: dict[str, str] = {}
    await open_group(page, url)
    stalls = 0
    previous_seen = 0
    reached = False
    for step in range(300):
        bubbles = await page.evaluate(SNAPSHOT)
        lowest = None
        for bubble in bubbles:
            if not bubble['id'] or bubble['service']:
                continue
            ids = bubble['members'] or [bubble['id']]
            seen.update(ids)
            if len(ids) > 1:
                observed_albums.add(tuple(ids))
            if bubble['timestamp']:
                timestamps[bubble['id']] = bubble['timestamp']
            oldest = min(map(int, ids))
            lowest = min(lowest, oldest) if lowest is not None else oldest
        for index, left in enumerate(bubbles[:-1]):
            if left['service']:
                continue
            next_index = index + 1
            while next_index < len(bubbles) and bubbles[next_index]['service']:
                next_index += 1
            if next_index == len(bubbles):
                continue
            right = bubbles[next_index]
            left_id = (left['members'] or [left['id']])[-1]
            right_id = (right['members'] or [right['id']])[0]
            if left_id not in membership or right_id not in membership:
                continue
            pair = (left_id, right_id)
            if next_index > index + 1:
                service_bridges.add(pair)
                continue
            checked_pairs.add(pair)
            same = membership[left_id] == membership[right_id]
            if (left['last'], right['first']) != ((not same), (not same)):
                bad_pairs.add(pair)
        if step % 20 == 0:
            print(f'[{identity}] DOM 步骤 {step}，已见成品消息 {len(seen & membership.keys())}/{len(membership)}，最旧 m{lowest}', flush=True)
        if lowest is not None and lowest <= target:
            reached = True
            break
        await page.evaluate("""() => {
            const e = document.querySelector('.bubbles-scrollable');
            if (e) {
                e.scrollTop = Math.max(0, e.scrollTop - Math.max(200, e.clientHeight * 0.7));
                e.dispatchEvent(new Event('scroll', {bubbles:true}));
            }
        }""")
        await page.wait_for_timeout(500)
        stalls = stalls + 1 if len(seen) == previous_seen else 0
        previous_seen = len(seen)
        if stalls >= 10:
            await page.evaluate("""() => {
                const e = document.querySelector('.bubbles-scrollable');
                if (e) { e.scrollTop = 0; e.dispatchEvent(new Event('scroll', {bubbles:true})); }
            }""")
        if stalls >= 80:
            break
    missing = sorted(membership.keys() - seen, key=int)
    missing_albums = expected_albums - observed_albums
    extra_albums = {album for album in observed_albums if any(mid in membership for mid in album)} - expected_albums
    ordered = sorted(membership, key=int)
    expected_boundaries = {(left, right) for left, right in zip(ordered, ordered[1:])
                           if membership[left] != membership[right]}
    missing_boundaries = expected_boundaries - checked_pairs - service_bridges
    time_mismatches = []
    for mid, actual in timestamps.items():
        source = records.source_messages.get(mid)
        if source and int(actual) != source['timestamp']:
            time_mismatches.append((mid, actual, source['timestamp']))
    offsets = Counter(expected - int(actual) for _mid, actual, expected in time_mismatches)
    result = dict(group=identity, reached_oldest_selected=reached, expected_messages=len(membership),
                  seen_messages=len(seen & membership.keys()), missing_messages=len(missing),
                  expected_albums=len(expected_albums), missing_albums=len(missing_albums),
                  extra_albums=len(extra_albums), checked_adjacent_pairs=len(checked_pairs),
                  checked_cross_segment_pairs=sum(membership[a] != membership[b] for a, b in checked_pairs),
                  service_segment_boundaries=sum(membership[a] != membership[b]
                                                 for a, b in service_bridges),
                  expected_segment_boundaries=len(expected_boundaries),
                  missing_segment_boundaries=len(missing_boundaries),
                  bad_adjacent_pairs=len(bad_pairs), timestamp_mismatches=len(time_mismatches),
                  timestamp_offset_counts=dict(offsets),
                  examples=dict(missing=missing[:5], missing_albums=list(missing_albums)[:2],
                                bad_pairs=list(bad_pairs)[:5], missing_boundaries=list(missing_boundaries)[:5],
                                times=time_mismatches[:5]))
    return result


async def main(urls: list[str]) -> None:
    paths = load_paths()
    failed = False
    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            user_data_dir=str(paths['chrome_profile']), channel='chrome', headless=False,
            viewport={'width': 1280, 'height': 900})
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto('https://web.telegram.org/k/', wait_until='domcontentloaded')
            await wait_for_login(page)
            for url in urls:
                result = await audit(page, url, paths['output'], paths['scan_records'])
                print(json.dumps(result, ensure_ascii=False), flush=True)
                failed |= (not result['reached_oldest_selected']
                           or any(result[key] for key in ('missing_messages', 'missing_albums',
                                                           'extra_albums', 'bad_adjacent_pairs',
                                                           'missing_segment_boundaries')))
        finally:
            await context.close()
    if failed:
        raise SystemExit(1)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('urls', nargs='+')
    asyncio.run(main(parser.parse_args().urls))
