"""Read only Telegram server IDs and dates for messages in finished manifests."""

from __future__ import annotations

import asyncio
import json

from playwright.async_api import async_playwright

from live_dom_audit import expected
from telegg.browser import open_group, wait_for_login
from telegg.local_config import load_paths
from telegg.group_menu import load_groups
from telegg.scan_record import ScanRecord


FETCH = """async ({peerId, offset}) => {
    const managers = window.appDownloadManager.managers;
    const peer = await managers.appPeersManager.getInputPeerById(peerId);
    const response = await managers.apiManager.invokeApi('messages.getHistory', {
        peer, offset_id: offset, offset_date: 0, add_offset: 0,
        limit: 100, max_id: 0, min_id: 0, hash: 0
    }, {noErrorBox: true});
    return response.messages.map(message => ({
        id: Number(message.id), date: Number(message.date || 0),
        edit_date: Number(message.edit_date || 0), kind: message._
    }));
}"""


async def main() -> None:
    paths = load_paths()
    failed = False
    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            user_data_dir=str(paths['chrome_profile']), channel='chrome', headless=False)
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto('https://web.telegram.org/k/', wait_until='domcontentloaded')
            await wait_for_login(page)
            for group in load_groups():
                peer_id = int(group.identity)
                matches = [folder for folder in paths['output'].iterdir()
                           if folder.is_dir() and folder.name.endswith(f'[{peer_id}]')]
                if len(matches) != 1:
                    raise RuntimeError(f'{peer_id}: expected one finished folder')
                membership, _ = expected(matches[0])
                records = ScanRecord(paths['scan_records'] / f'{peer_id}.jsonl')
                await open_group(page, group.url)
                low = min(int(mid) % 4294967296 for mid in membership)
                offset = 0
                seen = {}
                for _ in range(100):
                    batch = await page.evaluate(FETCH, {'peerId': peer_id, 'offset': offset})
                    if not batch:
                        break
                    ids = [row['id'] for row in batch]
                    if any(id_ <= 0 or (index and id_ >= ids[index - 1])
                           for index, id_ in enumerate(ids)):
                        raise RuntimeError(f'{peer_id}: server IDs out of order')
                    for row in batch:
                        mid = str((1 << 32) + row['id'])
                        if mid in membership:
                            seen[mid] = row
                    offset = ids[-1]
                    if offset <= low:
                        break
                missing = membership.keys() - seen.keys()
                differences = [(mid, records.source_messages[mid]['timestamp'], seen[mid]['date'])
                               for mid in membership.keys() & seen.keys()
                               if records.source_messages[mid]['timestamp'] != seen[mid]['date']]
                differences.sort(key=lambda item: int(item[0]))
                failed |= bool(missing or differences)
                print(json.dumps({'group': peer_id, 'expected': len(membership),
                                  'seen': len(seen), 'missing': len(missing),
                                  'date_differences': len(differences),
                                  'examples': differences[:8]}, ensure_ascii=False), flush=True)
        finally:
            await context.close()
    if failed:
        raise SystemExit(1)


if __name__ == '__main__':
    asyncio.run(main())
