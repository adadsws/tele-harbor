"""独立 Chrome 观察器：只提取白名单元数据，不导入产品导航、分组或分类函数。"""
import asyncio
import json
from collections import Counter
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright, TimeoutError as BrowserTimeout

from telegg.group_menu import load_groups
from telegg.local_config import load_paths
from telegg.scan_record import ScanRecord
from matrix_harness.isolation import copy_profile, write_json
from .inventory import roots_for


HISTORY = """async ({peer, offset}) => {
    const managers = window.appDownloadManager.managers;
    const input = await managers.appPeersManager.getInputPeerById(peer);
    const result = await managers.apiManager.invokeApi('messages.getHistory', {
        peer:input, offset_id:offset, offset_date:0, add_offset:0,
        limit:100, max_id:0, min_id:0, hash:0
    }, {noErrorBox:true, noRetry:true});
    return result.messages.map(m => ({
        raw:Number(m.id), mid:String(4294967296 + Number(m.id)), kind:m._,
        date:Number(m.date || 0), album:String(m.grouped_id || ''),
        reply:m.reply_to?.reply_to_peer_id ? '' : (m.reply_to?.reply_to_msg_id
            ? String(4294967296 + Number(m.reply_to.reply_to_msg_id)) : ''),
        media:m.media?._ || '', media_id:String(m.media?.document?.id || m.media?.photo?.id || ''),
        size:Number(m.media?.document?.size || 0), mime:m.media?.document?.mime_type || '',
        attrs:(m.media?.document?.attributes || []).map(a => a._),
        voice:(m.media?.document?.attributes || []).some(a => !!(a.voice || a.pFlags?.voice)),
        round:(m.media?.document?.attributes || []).some(a => !!(a.round_message || a.pFlags?.round_message))
    }));
}"""

DOM = """() => [...document.querySelectorAll('#column-center .bubble[data-mid]')].map(e => ({
    id:e.getAttribute('data-mid'), peer:e.getAttribute('data-peer-id') || e.closest('[data-peer-id]')?.getAttribute('data-peer-id'),
    timestamp:e.getAttribute('data-timestamp'), service:e.classList.contains('service'),
    first:e.classList.contains('is-group-first'), last:e.classList.contains('is-group-last'),
    members:[...e.querySelectorAll('.grouped-item[data-mid],.album-item[data-mid]')].map(x=>x.getAttribute('data-mid')),
    replyBlocks:e.querySelectorAll('.reply').length,
    structures:[...e.querySelectorAll('.attachment,.document,.audio,.video,.poll,.webpage,.sticker-wrapper')]
        .map(x=>[...x.classList].filter(c=>['attachment','document','audio','video','poll','webpage','sticker-wrapper'].includes(c)))
}))"""


def observation(path, payload):
    if path.exists():
        path.rename(path.with_name(path.stem + '.' + datetime.now().strftime('%H%M%S%f') + path.suffix))
    write_json(path, payload)


async def navigate(page, group, mid):
    if page.url.split('#', 1)[-1].split('_', 1)[0] != group.identity:
        # Web K 已加载时直接改 hash 可能仍保留旧聊天；先卸载再以目标群初始化。
        await page.goto('about:blank', wait_until='domcontentloaded')
        await page.goto(group.url, wait_until='domcontentloaded', timeout=30000)
        await page.wait_for_function("""peer=>[...document.querySelectorAll('#column-center .bubble[data-mid]')]
            .some(e=>e.getAttribute('data-peer-id')===peer)""", arg=group.identity, timeout=30000)
        await page.wait_for_function('!!window.appImManager', timeout=20000)
    selector = (f'#column-center .bubble[data-peer-id="{group.identity}"][data-mid="{mid}"], '
                f'#column-center .bubble[data-peer-id="{group.identity}"]:has(.grouped-item[data-mid="{mid}"]), '
                f'#column-center .bubble[data-peer-id="{group.identity}"]:has(.album-item[data-mid="{mid}"]), '
                f'#column-center [data-peer-id="{group.identity}"] .bubble[data-mid="{mid}"]')
    locator = page.locator(selector)
    for attempt in range(2):
        await asyncio.wait_for(page.evaluate("""async ({peer,mid}) => {
            await window.appImManager.setInnerPeer({peerId:Number(peer),lastMsgId:Number(mid)});
        }""", {'peer': group.identity, 'mid': mid}), 20)
        try:
            await locator.wait_for(state='attached', timeout=15000)
            break
        except BrowserTimeout:
            # 初始化可能吞掉首次跳转；仅重试同一目标一次，仍以实际气泡验证。
            print(json.dumps({'navigation_attempt_failed': mid, 'attempt': attempt + 1}), flush=True)
            if attempt:
                raise
    return locator


def media_type(row):
    simple = {'messageMediaPhoto': 'photo', 'messageMediaWebPage': 'link_preview',
              'messageMediaPoll': 'poll', 'messageMediaContact': 'contact',
              'messageMediaGeo': 'location', 'messageMediaGeoLive': 'location',
              'messageMediaVenue': 'venue'}
    if not row['media']:
        return 'text'
    if row['media'] != 'messageMediaDocument':
        return simple.get(row['media'], row['media'])
    attrs = row['attrs']
    if 'documentAttributeSticker' in attrs or 'documentAttributeCustomEmoji' in attrs:
        return 'sticker'
    if 'documentAttributeAnimated' in attrs:
        return 'animation'
    if 'documentAttributeVideo' in attrs:
        return 'video_note' if row['round'] else 'video'
    if 'documentAttributeAudio' in attrs:
        return 'voice' if row['voice'] else 'audio'
    return 'document'


def manifests(roots):
    membership, albums, edges = {}, set(), set()
    for root in roots:
        for manifest in root.rglob('段内清单.json'):
            for row in json.loads(manifest.read_text(encoding='utf-8'))['items']:
                ids = tuple(row['message_ids'])
                for mid in ids:
                    membership[mid] = manifest.parent.name
                if row.get('grouped_id'):
                    albums.add(tuple(sorted(ids, key=int)))
        for manifest in root.rglob('关联清单.json'):
            edges.update((r['source_message_id'], r['target_message_id'])
                         for r in json.loads(manifest.read_text(encoding='utf-8'))['replies'])
    return membership, albums, edges


async def server_group(page, group, record, number, suite):
    expected = record.source_messages
    oldest, newest = min(map(int, expected)), max(map(int, expected))
    offset, seen, pages, new, extra = 0, {}, 0, [], {}
    while pages < 100:
        batch = await asyncio.wait_for(page.evaluate(HISTORY, {'peer': int(group.identity), 'offset': offset}), 25)
        pages += 1
        if not batch:
            break
        if not offset:
            new = [row for row in batch if int(row['mid']) > newest and row['kind'] == 'message']
        for row in batch:
            if row['mid'] in expected:
                seen[row['mid']] = row
            elif row['kind'] == 'message' and oldest <= int(row['mid']) <= newest:
                extra[row['mid']] = row
        following = batch[-1]['raw']
        if offset and following >= offset:
            raise RuntimeError('历史偏移未向旧端推进')
        offset = following
        if offset + 4294967296 <= oldest:
            break
    issues = []
    issues.extend({'mid':mid,'type':'unrecorded_remote_message'} for mid in extra)
    for mid, source in expected.items():
        row = seen.get(mid)
        if row is None:
            issues.append({'mid': mid, 'type': 'remote_missing'})
            continue
        item = record.messages.get(source.get('bubble_mid'))
        if row['date'] != source['timestamp']:
            issues.append({'mid': mid, 'type': 'date'})
        if row['reply'] != str(source.get('reply_to') or ''):
            issues.append({'mid': mid, 'type': 'reply'})
        if item and row['album'] != item.album_id:
            issues.append({'mid': mid, 'type': 'album'})
        # 类型结果来自独立映射；文本和链接预览的规范名称可能另有命名，单列观察。
    result = {'group': number, 'pages': pages, 'expected_messages': len(expected),
              'seen_messages': len(seen), 'issues': len(issues),
              'unrecorded_remote_messages': len(extra),
              'natural_new_messages_in_latest_page': len(new), 'passed': not issues}
    observation(suite / f'server-group-{number}.json', {'summary': result, 'rows': seen,
                                                     'new': new, 'extra': extra, 'issues': issues})
    print(json.dumps(result), flush=True)
    return result


def targets_for(record, membership, edges):
    targets = [max(record.source_messages, key=int)]
    if edges:
        source, target = max(edges, key=lambda edge: int(edge[0]))
        targets.extend((source, target))
        chain = next((b for a, b in edges if a == target), None)
        if chain:
            targets.append(chain)
    ordered = sorted(membership, key=int)
    boundaries = [(a, b) for a, b in zip(ordered, ordered[1:]) if membership[a] != membership[b]]
    if boundaries:
        targets.extend(boundaries[-1])
    return list(dict.fromkeys(targets))


def compare_bubbles(bubbles, membership, albums):
    issues, checked = [], []
    for bubble in bubbles:
        members = tuple(sorted(bubble['members'], key=int))
        if members and any(mid in membership for mid in members) and members not in albums:
            issues.append({'type': 'album_members', 'ids': members})
    for left, right in zip(bubbles, bubbles[1:]):
        if left['service'] or right['service']:
            continue
        a = (left['members'] or [left['id']])[-1]
        b = (right['members'] or [right['id']])[0]
        if a not in membership or b not in membership:
            continue
        same = membership[a] == membership[b]
        checked.append({'left': a, 'right': b, 'same_segment': same,
                        'dom_last_first': [left['last'], right['first']]})
        if (left['last'], right['first']) != (not same, not same):
            issues.append({'type': 'avatar_boundary', 'ids': [a, b]})
    return issues, checked


async def dom_group(page, group, record, number, paths, suite):
    membership, albums, edges = manifests(roots_for(paths, group.identity))
    observations, issues, checked, seen, offsets = [], [], [], set(), {}
    for mid in targets_for(record, membership, edges):
        await navigate(page, group, mid)
        bubbles = await page.evaluate(DOM)
        selected = [b for b in bubbles if b['peer'] == group.identity]
        found = any(mid in (b['members'] or [b['id']]) for b in selected)
        if not found:
            issues.append({'type': 'target_not_observed', 'mid': mid})
        differences, pairs = compare_bubbles(selected, membership, albums)
        issues.extend(differences)
        for bubble in selected:
            source = record.source_messages.get(bubble['id'])
            if source:
                # DOM 时间只作诊断，网页可调整其时间；原始日期必须由独立服务器清单核对。
                offsets[bubble['id']] = int(bubble['timestamp'] or 0) - int(source['timestamp'])
        checked.extend(pairs)
        seen.update(mid for b in selected if not b['service'] for mid in (b['members'] or [b['id']]))
        observations.append({'target': mid, 'bubbles': selected})
    unique = {(p['left'], p['right']): p for p in checked}
    low, high = min(map(int, record.source_messages)), max(map(int, record.source_messages))
    unknown = {mid for mid in seen if low <= int(mid) <= high and mid not in record.source_messages}
    issues.extend({'type':'unrecorded_dom_message','mid':mid} for mid in unknown)
    result = {'group': number, 'targets': len(observations), 'observed_known_messages': len(seen & membership.keys()),
              'checked_adjacent_pairs': len(unique), 'checked_boundaries': sum(not p['same_segment'] for p in unique.values()),
              'unrecorded_dom_messages': len(unknown),
              'dom_timestamp_offset_counts': dict(Counter(offsets.values())),
              'issues': len(issues), 'passed': not issues and bool(checked)}
    observation(suite / f'dom-group-{number}.json', {'summary': result, 'observations': observations,
                                                 'pairs': checked, 'issues': issues})
    print(json.dumps(result), flush=True)
    return result


async def reply_group(page, group, number, paths, suite):
    _, _, edges = manifests(roots_for(paths, group.identity))
    if not edges:
        result = {'group': number, 'applicable': False, 'passed': True}
    else:
        source, target = max(edges, key=lambda pair: int(pair[0]) - int(pair[1]))
        bubble = await navigate(page, group, source)
        target_selector = (f'#column-center .bubble[data-peer-id="{group.identity}"][data-mid="{target}"], '
                           f'#column-center .bubble[data-peer-id="{group.identity}"]:has(.grouped-item[data-mid="{target}"]), '
                           f'#column-center .bubble[data-peer-id="{group.identity}"]:has(.album-item[data-mid="{target}"])')
        target_bubble = page.locator(target_selector)
        await target_bubble.wait_for(state='attached', timeout=15000)
        delta = await target_bubble.evaluate("""e=>e.getBoundingClientRect().bottom-e.closest('.bubbles-scrollable').getBoundingClientRect().top+20""")
        await bubble.evaluate("""(e,d)=>e.closest('.bubbles-scrollable').scrollBy(0,d)""", delta)
        visible_before = await target_bubble.evaluate("""e=>{
            const r=e.getBoundingClientRect(),c=e.closest('.bubbles-scrollable').getBoundingClientRect();return r.bottom>c.top&&r.top<c.bottom;
        }""")
        quotes = bubble.locator('.reply')
        if await quotes.count() != 1:
            raise RuntimeError('引用预览数量不唯一，不能确定点击目标')
        await quotes.click()
        await page.wait_for_function("""({peer,mid})=>{
            const e=[...document.querySelectorAll('#column-center .bubble[data-mid]')].find(x=>x.getAttribute('data-peer-id')===peer
                && (x.getAttribute('data-mid')===mid||x.querySelector('.grouped-item[data-mid="'+mid+'"],.album-item[data-mid="'+mid+'"]')));
            if(!e)return false;const r=e.getBoundingClientRect(),c=e.closest('.bubbles-scrollable').getBoundingClientRect();
            return r.bottom>c.top&&r.top<c.bottom;
        }""", arg={'peer':group.identity,'mid':target}, timeout=15000)
        result = {'group':number,'applicable':True,'target_offscreen_before_click':not visible_before,
                  'same_peer_target_visible_after_click':True,'passed':not visible_before}
        observation(suite / f'reply-group-{number}-metadata.json', {'source':source,'target':target})
    observation(suite / f'reply-group-{number}.json', result)
    print(json.dumps(result), flush=True)
    return result


async def latest_group(page, group, record, number, suite):
    rows = await asyncio.wait_for(page.evaluate(HISTORY, {'peer': int(group.identity), 'offset': 0}), 25)
    latest = max(map(int, record.source_messages))
    new = [row for row in rows if row['kind'] == 'message' and int(row['mid']) > latest]
    summary = {'group': number, 'observation_succeeded': True, 'new_messages': len(new),
               'new_albums': len({r['album'] for r in new if r['album']}),
               'incremental_download_verified': False}
    observation(suite / f'latest-group-{number}.json', {'summary': summary, 'new': new})
    print(json.dumps(summary), flush=True)
    return summary


async def observe(suite, *, server=False, dom=False, replies=False, latest=False, numbers=()):
    paths = load_paths()
    profile = suite / 'observer-profile'
    if not profile.exists():
        copy_profile(paths['chrome_profile'], profile)
    results = []
    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(str(profile), channel='chrome', headless=True)
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto('https://web.telegram.org/k/', wait_until='domcontentloaded', timeout=30000)
            await page.wait_for_function('!!window.appDownloadManager?.managers?.apiManager && !!window.appImManager', timeout=30000)
            if dom or replies:
                # 管理器对象出现早于聊天列表就绪；过早调用原生跳转可能被界面初始化吞掉。
                await page.wait_for_function("document.querySelectorAll('#column-left .chatlist [data-peer-id]').length>0",
                                             timeout=25000)
            for number, group in enumerate(load_groups(), 1):
                if numbers and number not in numbers:
                    continue
                record = ScanRecord(paths['scan_records'] / f'{group.identity}.jsonl')
                if latest:
                    results.append(await latest_group(page, group, record, number, suite))
                if server:
                    results.append(await server_group(page, group, record, number, suite))
                if dom:
                    results.append(await dom_group(page, group, record, number, paths, suite))
                if replies:
                    results.append(await reply_group(page, group, number, paths, suite))
        except Exception as error:
            results.append({'passed': False, 'incomplete': True, 'error_type': type(error).__name__})
            raise
        finally:
            observation(suite / 'observer-result.json', results)
            await context.close()
    return 0 if results and all(r.get('passed', r.get('observation_succeeded', False)) for r in results) else 1
