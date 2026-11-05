"""以可见 Chrome 中的 Telegram Web 页面扫描消息并触发原生下载。"""

from __future__ import annotations

import asyncio
import base64
import mimetypes
import re
import hashlib
import json
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator, Callable
from urllib.parse import urlsplit

from playwright.async_api import Page, TimeoutError as BrowserTimeout

from .content_archive import (content_payload, prepare_group_folder,
                              prepare_item_folders, save_message_content)
from .content_types import DOWNLOADABLE, METADATA_ONLY, MEDIA_SELECTOR, classify, validate_types
from .diagnostics import detail, failure, log_location
from .layout import Message, destination, group_folder, group_id, group_root, grouped, safe_name
from .local_config import load_paths
from .progress import date_label, file_line, group_line
from .resumable import PartialFile, download_parts
from .scan_record import FoundMedia, ScanRecord

BUBBLE = '#column-center .bubble[data-mid]:not(.service)'
SCROLL = '#column-center .chat.active .bubbles-scrollable, #column-center > .bubbles-scrollable'
MEDIA = MEDIA_SELECTOR
SCAN_RECORD_ROOT = load_paths()['scan_records']


async def server_older_route_id(page: Page, peer_id: int, oldest_mid: int) -> int | None:
    """向已登录 Web K 的服务器查询下一条旧消息；空结果才是可验证的历史边界。"""
    return await page.evaluate('''async ({peerId, oldestMid}) => {
        const managers = window.appDownloadManager?.managers;
        if (!managers?.apiManager || !managers?.appPeersManager)
            throw new Error('Telegram 历史接口不可用');
        const peer = await managers.appPeersManager.getInputPeerById(peerId);
        let offset = oldestMid % 4294967296;
        for (let attempt = 0; attempt < 100; attempt++) {
            const result = await managers.apiManager.invokeApi('messages.getHistory', {
                peer, offset_id: offset, offset_date: 0, add_offset: 0,
                limit: 1, max_id: 0, min_id: 0, hash: 0
            }, {noErrorBox: true});
            if (!Array.isArray(result?.messages)) throw new Error('Telegram 历史响应无消息列表');
            if (!result.messages.length) return null;
            const rows = result.messages.map(message => ({id: Number(message.id), type: message._}));
            if (rows.some(row => !Number.isInteger(row.id) || row.id <= 0 || row.id >= offset))
                throw new Error('Telegram 历史响应的消息 ID 未向旧端推进');
            const content = rows.filter(row => row.type === 'message');
            if (content.length) return Math.max(...content.map(row => row.id));
            if (rows.some(row => row.type !== 'messageService' && row.type !== 'messageEmpty'))
                throw new Error('Telegram 历史响应出现未知消息类型');
            offset = Math.min(...rows.map(row => row.id));
        }
        throw new Error('Telegram 历史接口连续返回服务消息，无法确认最早端');
    }''', {'peerId': peer_id, 'oldestMid': oldest_mid})


async def server_older_message(page: Page, peer_id: int, oldest_mid: int) -> Message | None:
    """只扫描时直接从服务器取紧邻的更早原消息，避开失效的虚拟列表。"""
    row = await page.evaluate('''async ({peerId, oldestMid}) => {
        const m = window.appDownloadManager?.managers;
        if (!m?.apiManager || !m?.appPeersManager) throw new Error('Telegram 历史接口不可用');
        const peer = await m.appPeersManager.getInputPeerById(peerId);
        let offset = oldestMid % 4294967296;
        for (let attempt = 0; attempt < 100; attempt++) {
            const result = await m.apiManager.invokeApi('messages.getHistory', {
                peer, offset_id: offset, offset_date: 0, add_offset: 0,
                limit: 1, max_id: 0, min_id: 0, hash: 0
            }, {noErrorBox: true});
            const rows = result?.messages;
            if (!Array.isArray(rows)) throw new Error('Telegram 历史响应无消息列表');
            if (!rows.length) return null;
            const message = rows[0], id = Number(message.id);
            if (!Number.isInteger(id) || id <= 0 || id >= offset)
                throw new Error('Telegram 历史响应未向旧端推进');
            if (message._ === 'message') {
                const media = message.media;
                const attrs = media?.document?.attributes || [];
                const flags = attrs.flatMap(attr => [attr._ || '',
                    ...(attr.voice || attr.pFlags?.voice ? ['voice'] : []),
                    ...(attr.round_message || attr.pFlags?.round_message ? ['round'] : [])]);
                const detail = media && !['messageMediaDocument', 'messageMediaPhoto'].includes(media._)
                    ? JSON.stringify(media, (_, value) => typeof value === 'bigint' ? value.toString() : value)
                    : '';
                return {id, text: message.message || '', date: Number(message.date),
                    mediaType: media?._ || '', flags, detail: detail?.length <= 200000 ? detail : '__DETAIL_TOO_LARGE__',
                    hasMedia: !!media};
            }
            if (message._ !== 'messageService' && message._ !== 'messageEmpty')
                throw new Error('Telegram 历史响应出现未知消息类型');
            offset = id;
        }
        throw new Error('Telegram 连续返回服务消息，无法确认最早端');
    }''', {'peerId': peer_id, 'oldestMid': oldest_mid})
    if row is None:
        return None
    mid = str((1 << 32) + int(row['id']))
    date = int(row['date'])
    content = str(row['text']).strip()
    media = bool(row['hasMedia'])
    if not date or (not content and not media):
        raise RuntimeError(f'服务器消息 m{mid} 缺少正文、媒体或时间')
    return Message(mid, content, date, int(media), ((mid, content, date, media),),
                   ((mid, classify(str(row['mediaType']), tuple(row['flags']))),),
                   ((mid, str(row['detail'])),))


async def server_older_batch(page: Page, peer_id: int, oldest_mid: int) -> list[Message]:
    """批量取服务器原消息；空批次表示可访问的普通消息已全部取尽。"""
    rows = await page.evaluate('''async ({peerId, oldestMid}) => {
        const m = window.appDownloadManager?.managers;
        if (!m?.apiManager || !m?.appPeersManager) throw new Error('Telegram 历史接口不可用');
        const peer = await m.appPeersManager.getInputPeerById(peerId);
        let offset = oldestMid % 4294967296;
        for (let attempt = 0; attempt < 100; attempt++) {
            const result = await m.apiManager.invokeApi('messages.getHistory', {
                peer, offset_id: offset, offset_date: 0, add_offset: 0,
                limit: 100, max_id: 0, min_id: 0, hash: 0
            }, {noErrorBox: true});
            if (!Array.isArray(result?.messages)) throw new Error('Telegram 历史响应无消息列表');
            if (!result.messages.length) return [];
            const ids = result.messages.map(x => Number(x.id));
            if (ids.some((id, i) => !Number.isInteger(id) || id <= 0 ||
                id >= (i ? ids[i - 1] : offset)))
                throw new Error('Telegram 历史响应的 ID 顺序异常');
            const messages = result.messages.filter(x => x._ === 'message');
            if (result.messages.some(x => !['message', 'messageService', 'messageEmpty'].includes(x._)))
                throw new Error('Telegram 历史响应出现未知消息类型');
            if (messages.length) return messages.map(message => {
                const media = message.media;
                const attrs = media?.document?.attributes || [];
                const flags = attrs.flatMap(attr => [attr._ || '',
                    ...(attr.voice || attr.pFlags?.voice ? ['voice'] : []),
                    ...(attr.round_message || attr.pFlags?.round_message ? ['round'] : [])]);
                const detail = media && !['messageMediaDocument', 'messageMediaPhoto'].includes(media._)
                    ? JSON.stringify(media, (_, value) => typeof value === 'bigint' ? value.toString() : value)
                    : '';
                return {id: Number(message.id), text: message.message || '', date: Number(message.date),
                    mediaType: media?._ || '', flags,
                    detail: detail?.length <= 200000 ? detail : '__DETAIL_TOO_LARGE__', hasMedia: !!media};
            });
            offset = ids.at(-1);
        }
        throw new Error('Telegram 连续返回服务消息，无法确认最早端');
    }''', {'peerId': peer_id, 'oldestMid': oldest_mid})
    result = []
    for row in rows:
        mid = str((1 << 32) + int(row['id']))
        date = int(row['date'])
        content = str(row['text']).strip()
        media = bool(row['hasMedia'])
        if not date or (not content and not media):
            raise RuntimeError(f'服务器消息 m{mid} 缺少正文、媒体或时间')
        result.append(Message(mid, content, date, int(media), ((mid, content, date, media),),
                              ((mid, classify(str(row['mediaType']), tuple(row['flags']))),),
                              ((mid, str(row['detail'])),)))
    return result


class DownloadTransportUnavailable(RuntimeError):
    """下载通道无法恢复；应停止本轮以避免后续附件连锁失败。"""


@dataclass(frozen=True, slots=True)
class MediaTask:
    message: Message
    index: int
    media_mid: str
    folder: Path
    expected_size: int | None
    partial_root: Path | None = None
    position: int = 1
    total_files: int = 1


@dataclass(slots=True)
class ScanProgress:
    reached_oldest: bool = False
    stopped_on_known: bool = False
    count: int = 0


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def existing_media_index(root: Path) -> dict[str, list[Path]]:
    """按 Telegram 媒体 ID 索引已有文件，避免旧目录名导致重复下载。"""
    result: dict[str, list[Path]] = {}
    for path in root.rglob('m*'):
        match = re.match(r'^m(\d+)_\d+_.+', path.name)
        if (match and path.is_file() and path.stat().st_size > 0
                and not re.search(r'\.part(?:\.\d+)?$', path.name)):
            result.setdefault(match.group(1), []).append(path)
    for mid, paths in result.items():
        if len(paths) > 1:
            numbered = sorted((media_version(path), path) for path in paths)
            if [number for number, _ in numbered] == list(range(1, len(paths) + 1)):
                result[mid] = [numbered[-1][1]]
    return result


def media_version(path: Path) -> int:
    """未编号文件视为初版；只识别扩展名前的版本后缀。"""
    match = re.search(r'\.v([1-9]\d*)(?=\.[^.]+$)', path.name)
    return int(match.group(1)) if match else 0


def numbered_media_path(path: Path, number: int) -> Path:
    stem = re.sub(r'\.v[1-9]\d*$', '', path.stem)
    return path.with_name(f'{stem}.v{number}{path.suffix}')


def saved_media_identity(partial_root: Path, media_mid: str, version: int) -> dict | None:
    key = media_mid if version == 0 else f'{media_mid}.v{version}'
    journal = partial_root / f'{key}.jsonl'
    if version == 1 and not journal.exists():
        journal = partial_root / f'{media_mid}.jsonl'
    if not journal.exists():
        return None
    with journal.open('rb') as stream:
        header = json.loads(stream.readline())
    if header.get('kind') != 'identity' or not isinstance(header.get('identity'), dict):
        raise RuntimeError(f'附件身份记录损坏：{journal}')
    return header


def publish_ready_staged_groups(root: Path, staging_root: Path, record: ScanRecord) -> int:
    """完整历史确认后，以持久扫描记录核齐整个编组再发布。"""
    if not record.history_complete:
        return 0
    index = existing_media_index(staging_root)
    published = 0
    for anchor, batch in grouped(record.ordered_messages()):
        prior_root = (root / group_folder(anchor)).is_dir() if anchor else False
        prior_staging = (staging_root / group_folder(anchor)).is_dir() if anchor else False
        prepare_group_folder(root, anchor)
        prepare_group_folder(staging_root, anchor)
        moved = 0
        if anchor:
            moved += prepare_item_folders(root / group_folder(anchor), batch)
            moved += prepare_item_folders(staging_root / group_folder(anchor), batch)
        changed = bool(anchor and (moved or
                       (not prior_root and (root / group_folder(anchor)).is_dir()) or
                       (not prior_staging and (staging_root / group_folder(anchor)).is_dir())))
        if changed:
            index = existing_media_index(staging_root)
        name = group_folder(anchor) if anchor else '未归组'
        pending = staging_root / name
        if not pending.is_dir():
            continue
        expected = {mid for item in batch for mid, kind in item.content_types if kind in DOWNLOADABLE}
        if any(len(index.get(mid, [])) != 1 or mid not in record.results or
               record.results[mid]['status'] == 'failed' or
               (mid in record.media and record.media[mid].expected_size and
                index[mid][0].stat().st_size != record.media[mid].expected_size)
               for mid in expected):
            continue
        prepare_group_folder(staging_root, anchor, create=True)
        target = root / name
        if target.exists():
            raise RuntimeError(f'编组发布目标已存在：{target}')
        target.parent.mkdir(parents=True, exist_ok=True)
        pending.rename(target)
        published += 1
        detail(f'编组文件已核对并移入成品目录：{name}')
    return published


def archive_empty_relocated_folders(root: Path, source_folders: set[Path]) -> None:
    """仅归档本次迁移后变空的项目内旧目录，不物理删除。"""
    if not source_folders:
        return
    project = Path.cwd().resolve()
    root = root.resolve()
    if not root.is_relative_to(project) or root.is_relative_to(project / '.local'):
        detail('旧目录位于项目归档范围外，迁移后空目录已保留')
        return
    candidates: set[Path] = set()
    for source in source_folders:
        current = source.resolve()
        highest = None
        while current != root and current.is_relative_to(root) and current.is_dir():
            if any(current.iterdir()):
                break
            highest = current
            current = current.parent
        if highest is not None:
            candidates.add(highest)
    candidates = {folder for folder in candidates
                  if not any(parent in candidates for parent in folder.parents)}
    if not candidates:
        return
    batch = project / '~archive' / f'{datetime.now():%Y-%m-%d-%H%M%S}-caption-folder-fix-{uuid.uuid4().hex[:8]}'
    for folder in sorted(candidates):
        target = batch / folder.relative_to(project)
        target.parent.mkdir(parents=True, exist_ok=True)
        folder.rename(target)
        detail(f'归档空旧目录 {folder} -> {target}')


async def wait_for_login(page: Page) -> None:
    """等到已登录账号的真实会话入口可见；页面骨架不能算登录成功。"""
    chat = page.locator('#column-left.chatlist-container a[href^="#"]').first
    try:
        await chat.wait_for(state='visible', timeout=12_000)
    except BrowserTimeout:
        print('请在打开的 Chrome 中登录 Telegram Web；等待最多 5 分钟。', flush=True)
        await chat.wait_for(state='visible', timeout=300_000)


async def open_group(page: Page, url: str, *, require_message: bool = True) -> str:
    expected = group_id(url)
    current = urlsplit(page.url)
    if current.hostname == 'web.telegram.org' and not (
        current.fragment == expected or current.fragment.startswith(expected + '_')
    ):
        # Web K 对跨群组的 hash 导航可能保持旧聊天；同一标签先卸载再打开。
        await page.goto('about:blank', wait_until='domcontentloaded')
    await page.goto(url, wait_until='domcontentloaded')
    await wait_for_login(page)
    if not (urlsplit(page.url).fragment == expected or
            urlsplit(page.url).fragment.startswith(expected + '_')):
        await page.goto('about:blank', wait_until='domcontentloaded')
        await page.goto(url, wait_until='domcontentloaded')
    try:
        await page.wait_for_function(
            'expected => location.hash === "#" + expected || location.hash.startsWith("#" + expected + "_")',
            arg=expected, timeout=30_000,
        )
    except BrowserTimeout as exc:
        raise RuntimeError(f'群组 {expected} 未完成页面切换；当前地址：{page.url}') from exc
    await page.wait_for_timeout(700)
    if require_message:
        try:
            await page.locator(BUBBLE).first.wait_for(state='attached', timeout=45_000)
        except BrowserTimeout as exc:
            raise RuntimeError(f'群组 {expected} 没有显示消息；请确认已加入并能在 Chrome 中打开') from exc
    if not (urlsplit(page.url).fragment == expected or
            urlsplit(page.url).fragment.startswith(expected + '_')):
        raise RuntimeError(f'页面跳转到其他群组：{page.url}')
    title = page.locator('#column-center .topbar .peer-title').first
    if not require_message:
        try:
            await title.wait_for(state='visible', timeout=15_000)
        except BrowserTimeout as exc:
            raise RuntimeError(f'群组 {expected} 未显示名称') from exc
    name = (await title.inner_text()).strip() if await title.count() else expected
    return name or expected


async def visible_messages(page: Page) -> list[Message]:
    for attempt in range(6):
        rows = await page.locator(BUBBLE).evaluate_all(r'''(nodes, selector) => nodes.map(b => {
        const own = [...b.querySelectorAll('.translatable-message, .message')]
            .find(node => !node.closest('.reply,.attachment,.media-container,.document-container,.audio-container,.sticker-container'));
        const media = [...b.querySelectorAll(selector)]
            .filter(node => !node.closest('.reply') && !node.parentElement?.closest(selector));
        const port = window.mtprotoMessagePort;
        const primaryPeer = b.dataset.peerId || b.closest('[data-peer-id]')?.dataset.peerId || '';
        const primary = primaryPeer && b.dataset.mid
            ? port?.getMessageByPeer(primaryPeer, Number(b.dataset.mid)) : null;
        const ids = [b, ...b.querySelectorAll('.album-item[data-mid], .grouped-item[data-mid]')];
        const seen = new Set();
        const originals = ids.map(node => {
            const peer = node.dataset.peerId || b.dataset.peerId || b.closest('[data-peer-id]')?.dataset.peerId || '';
            const mid = node.dataset.mid || '';
            const key = `${peer}:${mid}`;
            if (!peer || !mid || seen.has(key)) return null;
            seen.add(key);
            return port?.getMessageByPeer(peer, Number(mid)) || null;
        }).filter(Boolean);
        const hasMetadata = originals.length === seen.size &&
            originals.every(message => typeof message.message === 'string' && Number(message.date) > 0);
        const captions = [...new Set(originals.map(message => message.message?.trim()).filter(Boolean))];
        const parts = originals.map(message => [String(message.mid), (message.message || '').trim(),
            Number(message.date || 0), !!message.media]);
        const types = originals.map(message => {
            const media = message.media;
            const attrs = media?.document?.attributes || [];
            const flags = attrs.flatMap(attr => [attr._ || '',
                ...(attr.voice || attr.pFlags?.voice ? ['voice'] : []),
                ...(attr.round_message || attr.pFlags?.round_message ? ['round'] : [])]);
            return [String(message.mid), media?._ || (media ? 'unknown-media-object' : ''), flags];
        });
        const details = originals.map(message => {
            const media = message.media;
            if (!media || media._ === 'messageMediaDocument' || media._ === 'messageMediaPhoto')
                return [String(message.mid), ''];
            try {
                const serialized = JSON.stringify(media, (_key, value) =>
                    typeof value === 'bigint' ? value.toString() : value);
                return [String(message.mid), serialized.length <= 200000 ? serialized : '__DETAIL_TOO_LARGE__'];
            } catch (error) {
                return [String(message.mid), '__DETAIL_ERROR__:' + String(error)];
            }
        });
        return {
            mid: b.dataset.mid || '',
            text: hasMetadata ? captions.join('\n') : (own?.innerText || '').trim(),
            timestamp: Number(primary?.date || 0),
            mediaCount: media.length || (b.querySelector('.attachment') ? 1 : 0),
            hasMetadata,
            parts,
            types,
            details,
        };
    })''', MEDIA)
        incomplete = [row for row in rows if row['mid'] and
                      (not row['timestamp'] or not row['hasMetadata'] or not row['parts']
                       or any(not part[0] or not part[2] or not (part[1] or part[3])
                              for part in row['parts']))]
        if not incomplete:
            break
        if attempt < 5:
            await page.wait_for_timeout(500)
    if incomplete:
        raise RuntimeError(f"消息 {incomplete[0]['mid']} 缺少 Telegram 原消息元数据，停止归组以免时间或正文错误")
    return [Message(str(row['mid']), row['text'], int(row['timestamp']), int(row['mediaCount']),
                    tuple((str(mid), text, int(date), bool(media)) for mid, text, date, media in row['parts']),
                    tuple((str(mid), classify(str(media_type), tuple(flags)))
                          for mid, media_type, flags in row['types']),
                    tuple((str(mid), str(detail)) for mid, detail in row['details']))
            for row in rows if row['mid'] and row['timestamp']]


async def iter_history(page: Page, limit: int | None, known_ids: set[str],
                       progress: ScanProgress, *, start_at_latest: bool = True,
                       pause_at_oldest: bool = False,
                       peer_id: int | None = None,
                       api_scan: bool = False) -> AsyncIterator[Message]:
    """逐条提供最新到最旧的消息，供下载阶段在每个归组边界间插入。"""
    known_ids = known_ids or set()
    scroll = page.locator(SCROLL).first
    await scroll.wait_for(state='attached', timeout=30_000)
    if start_at_latest:
        await scroll.evaluate('el => { el.scrollTop = el.scrollHeight; }')
        await page.wait_for_timeout(1_000)
    seen: set[str] = set()
    source_seen: set[str] = set()
    stale = 0
    advanced_to_older = False
    earliest_timestamp: int | None = None
    oldest_mid: int | None = None
    recovery_mid: int | None = None
    server_target: int | None = None
    last_report = time.monotonic()
    last_date: str | None = None
    while True:
        before = len(seen)
        for item in reversed(await visible_messages(page)):
            if item.mid in known_ids:
                detail(f'遇到已记录消息 m{item.mid}，停止增量扫描')
                print('[扫描] 已接上上次进度', flush=True)
                progress.stopped_on_known = True
                return
            if item.mid in seen:
                continue
            seen.add(item.mid)
            source_ids = {mid for mid, *_rest in item.parts} or {item.mid}
            source_seen.update(source_ids)
            item_oldest_mid = min(map(int, source_ids))
            oldest_mid = min(oldest_mid, item_oldest_mid) if oldest_mid is not None else item_oldest_mid
            if recovery_mid is not None and oldest_mid < recovery_mid:
                recovery_mid = None
                server_target = None
            earliest_timestamp = min(earliest_timestamp, item.timestamp) if earliest_timestamp else item.timestamp
            progress.count = len(source_seen)
            current_date = date_label(item.timestamp)
            if current_date != last_date:
                print(f'[扫描] 已到 {current_date} · 已读取 {len(source_seen)} 条', flush=True)
                last_date = current_date
            yield item
            if limit and len(seen) >= limit:
                detail(f'已读取 {limit}/{limit} 条页面消息')
                return
        if before and len(seen) > before:
            advanced_to_older = True
        stale = stale + 1 if len(seen) == before else 0
        now = time.monotonic()
        if (len(seen) != before and len(seen) // 25 != before // 25) or now - last_report >= 15:
            print(f'[扫描] 已到 {date_label(earliest_timestamp)} · 已读取 {len(source_seen)} 条', flush=True)
            last_report = now
        await scroll.hover()
        await page.mouse.wheel(0, -500)
        # Web K 有时不响应滚轮；同时直接推动实际滚动容器触发旧消息加载。
        await scroll.evaluate('el => { el.scrollTop = Math.max(0, el.scrollTop - 700); }')
        await page.wait_for_timeout(1_200)
        state = await scroll.evaluate('el => ({top: Math.abs(el.scrollTop) < 3, loading: !!el.querySelector(".preloader,.loader,.progress-line,[aria-busy=true]"), scrollTop: el.scrollTop, scrollHeight: el.scrollHeight, clientHeight: el.clientHeight})')
        if stale >= 10 and stale % 10 == 0:
            detail(f'无新消息 {stale} 次，滚动状态 {state}，最早时间 {earliest_timestamp}')
        if stale == 10 and api_scan and peer_id is not None and oldest_mid is not None:
            print('[扫描] 正在继续读取更早的消息', flush=True)
            while True:
                batch = await server_older_batch(page, peer_id, oldest_mid)
                if not batch:
                    progress.reached_oldest = True
                    print(f'[扫描] 已到可访问历史最早日期 {date_label(earliest_timestamp)}', flush=True)
                    return
                for older in batch:
                    if int(older.mid) >= oldest_mid or older.mid in source_seen:
                        raise RuntimeError(f'服务器历史未向旧端推进：m{older.mid}')
                    oldest_mid = int(older.mid)
                    earliest_timestamp = min(earliest_timestamp, older.timestamp) if earliest_timestamp else older.timestamp
                    seen.add(older.mid)
                    source_seen.add(older.mid)
                    progress.count = len(source_seen)
                    current_date = date_label(older.timestamp)
                    if current_date != last_date:
                        print(f'[扫描] 已到 {current_date} · 已读取 {len(source_seen)} 条', flush=True)
                        last_date = current_date
                    yield older
                    if limit and len(seen) >= limit:
                        return
                    if len(seen) % 25 == 0:
                        print(f'[扫描] 已到 {date_label(earliest_timestamp)} · 已读取 {len(source_seen)} 条', flush=True)
        if stale == 10 and peer_id is not None and oldest_mid is not None:
            server_target = await server_older_route_id(page, peer_id, oldest_mid)
            if server_target is None:
                detail(f'服务器确认 m{oldest_mid} 之前无可访问消息')
                break
            detail(f'服务器确认仍有更早消息 /{server_target}')
        if (stale == 15 and oldest_mid is not None and recovery_mid is None
                and isinstance(page.url, str)):
            # Web K 偶尔停在虚拟列表的顶部；从当前最旧消息前一 ID 重新定位。
            recovery_mid = oldest_mid
            base_url = page.url.split('_', 1)[0]
            route_id = server_target if server_target is not None else (oldest_mid & 0xFFFFFFFF) - 1
            detail(f'顶部未继续加载，尝试定位到 m{oldest_mid} 之前')
            await page.goto(f'{base_url}_{route_id}', wait_until='domcontentloaded')
            await scroll.wait_for(state='attached', timeout=30_000)
            await page.wait_for_timeout(2_000)
            stale = 0
            continue
        if recovery_mid is not None and oldest_mid is not None and oldest_mid >= recovery_mid and stale >= 15:
            if pause_at_oldest:
                print('[人工检查] Chrome 已暂停在当前最早可见日期。'
                      '请检查页面，检查完在终端按 Enter 继续。', flush=True)
                await asyncio.to_thread(input)
            raise RuntimeError(f'重新定位后仍未发现 m{recovery_mid} 之前的消息；不能确认历史最早端')
        # 页面初始位置或失效的滚动容器也会一直报告 top=True；必须先看到旧消息推进。
        if (peer_id is None and state['top'] and not state['loading'] and stale >= 30
                and advanced_to_older):
            break
        if stale >= 60:
            raise RuntimeError('无法确认已扫描到历史顶部；保留已发现消息但停止下载以免遗漏')
    progress.reached_oldest = True
    print(f'[扫描] 已到可访问历史最早日期 {date_label(earliest_timestamp)}', flush=True)


async def locate_message(page: Page, source_url: str, item: Message):
    """优先使用 Web K 消息地址；地址未生效时沿可见历史回找。"""
    selector = f'{BUBBLE}[data-mid="{item.mid}"]'
    bubble = page.locator(selector).first
    if not await bubble.count():
        # 当前页已在相邻旧消息附近时，沿现有虚拟列表往旧端找，避免每次从最新端重滚。
        if page.url.startswith(source_url):
            try:
                visible = await visible_messages(page)
                if visible:
                    target_mid = int(item.mid)
                    if target_mid < min(int(entry.mid) for entry in visible):
                        async for candidate in iter_history(page, 80, set(), ScanProgress(),
                                                            start_at_latest=False):
                            if candidate.mid == item.mid:
                                return bubble
                    elif target_mid > max(int(entry.mid) for entry in visible):
                        scroll = page.locator(SCROLL).first
                        await scroll.hover()
                        for _ in range(40):
                            await page.mouse.wheel(0, 500)
                            await page.wait_for_timeout(300)
                            if await bubble.count():
                                return bubble
            except RuntimeError:
                pass
        route_id = str(int(item.mid) & 0xFFFFFFFF)
        await page.goto(f'{source_url}_{route_id}', wait_until='domcontentloaded')
        try:
            await bubble.wait_for(state='attached', timeout=5_000)
        except BrowserTimeout:
            # Web K 可能丢弃消息 hash 却保留中途的虚拟列表；先重开群组起点。
            await page.goto('about:blank', wait_until='domcontentloaded')
            await open_group(page, source_url)
            async for candidate in iter_history(page, None, set(), ScanProgress()):
                if candidate.mid == item.mid:
                    break
            if not await bubble.count():
                raise RuntimeError(f'无法定位原消息 {item.mid}；已从最新端扫描可见历史')
    return bubble


async def media_targets(bubble):
    """仅标记最外层附件，避免文档或音频子元素重复下载。"""
    await bubble.evaluate('''(bubble, selector) => {
        bubble.querySelectorAll('[data-telegg-media-index]').forEach(node => node.removeAttribute('data-telegg-media-index'));
        [...bubble.querySelectorAll(selector)]
            .filter(node => !node.closest('.reply') && !node.parentElement?.closest(selector))
            .forEach((node, index) => node.setAttribute('data-telegg-media-index', String(index)));
    }''', MEDIA)
    return bubble.locator('[data-telegg-media-index]')


async def task_metadata(target) -> tuple[str, int | None]:
    """读取相册项自己的消息 ID；文档大小来自页面已加载的元数据。"""
    record = await target.evaluate('''node => {
        const element = node.closest('.album-item[data-mid], .grouped-item[data-mid]') || node.closest('.bubble[data-mid]');
        const mid = element?.dataset.mid || '';
        const peer = element?.dataset.peerId || '';
        const message = window.mtprotoMessagePort?.getMessageByPeer(peer, Number(mid));
        const size = Number(message?.media?.document?.size || 0);
        return {mid, size};
    }''')
    if not record['mid']:
        raise RuntimeError('附件缺少 Telegram 消息 ID')
    size = int(record['size']) if record['size'] > 0 else None
    return str(record['mid']), size


async def resume_source(target, media_mid: str | None = None) -> dict:
    """从当前消息取得分片身份；敏感引用仅留在 Chrome 内存中。"""
    # Web K 自身的分片接口与输入位置结构：
    # https://github.com/morethanwords/tweb/blob/master/src/lib/appManagers/apiFileManager.ts
    source = await target.evaluate('''(node, requestedMid) => {
        const item = requestedMid ? node :
            (node.closest('.album-item[data-mid], .grouped-item[data-mid]') || node.closest('.bubble[data-mid]'));
        const peer = item?.dataset.peerId, mid = requestedMid || item?.dataset.mid;
        const message = window.mtprotoMessagePort?.getMessageByPeer(peer, Number(mid));
        const media = message?.media?.document || message?.media?.photo;
        const fileManager = window.appDownloadManager?.managers?.apiFileManager;
        if (!media || typeof fileManager?.requestFilePart !== 'function') return null;
        const photoSize = media._ === 'photo' ? media.sizes?.at(-1) : null;
        const size = media._ === 'document' ? media.size :
            (photoSize?.size || Math.max(0, ...(photoSize?.sizes || [])));
        const name = media._ === 'document' ? (media.file_name || `document_${media.id}`) :
            `photo_${media.id}_${photoSize?.type || ''}.jpg`;
        return {peer, mid, kind: media._, id: String(media.id),
            dc: media.dc_id, size: Number(size), thumb: photoSize?.type || '',
            mime_type: media.mime_type || '', name};
    }''', media_mid)
    if not source or source['kind'] not in ('document', 'photo'):
        raise RuntimeError('Telegram Web K 分片接口不可用或媒体类型不支持续传')
    if not source['peer'] or not source['mid'] or not source['id'] or source['size'] <= 0:
        raise RuntimeError('附件缺少分片身份或准确大小，不能安全续传')
    return source


async def fetch_resume_part(page: Page, source: dict, offset: int, limit: int) -> bytes:
    """在已登录 Web K 中按偏移取一块，不把 access_hash 写入磁盘。"""
    if source['kind'] == 'photo' and source['size'] <= 1024 * 1024:
        encoded = await page.evaluate('''async ({source, offset, limit}) => {
            const message = window.mtprotoMessagePort?.getMessageByPeer(source.peer, Number(source.mid));
            const photo = message?.media?.photo;
            if (!photo || String(photo.id) !== source.id || Number(photo.dc_id) !== source.dc)
                throw new Error('照片身份在下载期间变化');
            const thumb = photo.sizes?.find(size => size.type === source.thumb);
            if (!thumb || Number(thumb.size) !== source.size)
                throw new Error('照片原图大小在下载期间变化');
            const timeout = (promise) => Promise.race([
                promise, new Promise((_, reject) => setTimeout(() => reject(new Error('timeout')), 20000))
            ]);
            const location = {
                _: 'inputPhotoFileLocation', id: photo.id, access_hash: photo.access_hash,
                file_reference: photo.file_reference, thumb_size: source.thumb
            };
            let bytes, directError = '';
            try {
                const part = await timeout(window.appDownloadManager.managers.apiManager.invokeApi(
                    'upload.getFile', {location, offset, limit},
                    {dcId: photo.dc_id, fileDownload: true, floodMaxTimeout: 10}));
                if (part?._ !== 'upload.file' || !(part.bytes instanceof Uint8Array))
                    throw new Error('Telegram 未返回普通照片分片');
                bytes = part.bytes;
            } catch (error) {
                directError = String(error);
                const blob = await timeout(window.appDownloadManager.managers.apiFileManager.downloadMedia({
                    media: photo, thumb
                })).catch(nativeError => {
                    throw new Error(`照片原图双路径失败：${directError}；${String(nativeError)}`);
                });
                if (!(blob instanceof Blob) || blob.size !== source.size)
                    throw new Error(`照片原图大小不符：${blob?.size} / ${source.size}`);
                bytes = new Uint8Array(await blob.slice(offset, offset + limit).arrayBuffer());
            }
            const wanted = Math.min(limit, source.size - offset);
            if (bytes.length !== wanted) throw new Error(`照片分片长度不符：${bytes.length} / ${wanted}`);
            let binary = '';
            for (let i = 0; i < bytes.length; i += 32768)
                binary += String.fromCharCode(...bytes.subarray(i, i + 32768));
            return btoa(binary);
        }''', {'source': source, 'offset': offset, 'limit': limit})
        return base64.b64decode(encoded, validate=True)
    encoded = await page.evaluate('''async ({source, offset, limit}) => {
        const message = window.mtprotoMessagePort?.getMessageByPeer(source.peer, Number(source.mid));
        const media = message?.media?.document || message?.media?.photo;
        if (!media || media._ !== source.kind || String(media.id) !== source.id ||
            Number(media.dc_id) !== source.dc) throw new Error('附件身份在下载期间变化');
        const location = {
            _: media._ === 'document' ? 'inputDocumentFileLocation' : 'inputPhotoFileLocation',
            id: media.id, access_hash: media.access_hash,
            file_reference: media.file_reference, thumb_size: source.thumb
        };
        const timeout = (promise) => Promise.race([
            promise, new Promise((_, reject) => setTimeout(() => reject(new Error('timeout')), 20000))
        ]);
        let part, directError = '';
        try {
            part = await timeout(window.appDownloadManager.managers.apiManager.invokeApi(
                'upload.getFile', {location, offset, limit},
                {dcId: media.dc_id, fileDownload: true, floodMaxTimeout: 10}));
        } catch (error) {
            directError = String(error);
            part = await timeout(window.appDownloadManager.managers.apiFileManager.requestFilePart({
                dcId: media.dc_id, location, offset, limit
            })).catch(nativeError => {
                throw new Error(`附件分片双路径失败：${directError}；${String(nativeError)}`);
            });
        }
        if (part?._ !== 'upload.file' || !(part.bytes instanceof Uint8Array))
            throw new Error('Telegram 未返回普通文件分片');
        let binary = '';
        for (let i = 0; i < part.bytes.length; i += 32768)
            binary += String.fromCharCode(...part.bytes.subarray(i, i + 32768));
        return btoa(binary);
    }''', {'source': source, 'offset': offset, 'limit': limit})
    return base64.b64decode(encoded, validate=True)


async def save_one(page: Page, target, task: MediaTask,
                   existing_index: dict[str, list[Path]]) -> tuple[str, Path, int, str, Path | None]:
    prefix = f'm{task.media_mid}_{task.index}_'
    source = await resume_source(target, task.media_mid if task.index > task.message.media_count else None)
    if source['mid'] != task.media_mid:
        raise RuntimeError(f'附件位置变化：预期 m{task.media_mid}，实际 m{source["mid"]}')
    size_changed = bool(task.expected_size and source['size'] != task.expected_size)
    partial_root = task.partial_root or load_paths()['partials']
    identity = {key: source[key] for key in ('peer', 'mid', 'kind', 'id', 'dc', 'thumb')}
    candidates = [path for path in existing_index.get(task.media_mid, []) if path.is_file()]
    existing = [path for path in candidates if path.name.startswith(prefix)]
    if candidates and not existing:
        raise RuntimeError(f'同一消息的附件位置变化，无法确认版本：m{task.media_mid} 位置 {task.index}')
    version = 0
    if existing:
        if len(existing) != 1:
            raise RuntimeError(f'同一媒体存在多个本地文件，无法确认：m{task.media_mid}')
        path = existing[0]
        size = path.stat().st_size
        prior_version = media_version(path)
        prior = saved_media_identity(partial_root, task.media_mid, prior_version)
        changed = size != source['size'] or (prior_version > 0 and prior is None) or (prior is not None and
                  (prior['identity'] != identity or prior['size'] != source['size']))
        if not changed:
            if size_changed:
                raise RuntimeError(f'附件大小与扫描记录不符：m{task.media_mid}')
            digest = await asyncio.to_thread(sha256_file, path)
            old_folder = None
            if path.parent != task.folder:
                moved = task.folder / path.name
                if moved.exists():
                    raise RuntimeError(f'迁移目标已存在，未覆盖：{moved}')
                task.folder.mkdir(parents=True, exist_ok=True)
                old_folder = path.parent
                path.rename(moved)
                existing_index[task.media_mid] = [moved]
                path = moved
            return 'relocated' if old_folder else 'skipped', path, size, digest, old_folder
        if prior_version == 0:
            first = numbered_media_path(path, 1)
            if first.exists():
                raise RuntimeError(f'附件初版目标已存在，未覆盖：{first}')
            path.rename(first)
            existing_index[task.media_mid] = [first]
        version = max(2, prior_version + 1)
    elif size_changed:
        raise RuntimeError(f'附件大小变化但未找到旧文件：m{task.media_mid}')
    name = safe_name(source['name'], 120)
    if not Path(name).suffix:
        name += mimetypes.guess_extension(source.get('mime_type', '').split(';', 1)[0].strip()) or '.bin'
    final = task.folder / f'{prefix}{name}'
    if version:
        final = numbered_media_path(final, version)
    if final.is_file() and final.stat().st_size > 0:
        raise RuntimeError(f'目标文件在下载期间已出现，未覆盖：{final}')
    if final.exists():
        raise RuntimeError(f'已有空文件，未覆盖：{final}')
    partial_key = task.media_mid if not version else f'{task.media_mid}.v{version}'
    partial = PartialFile(partial_root, partial_key, identity, source['size'])
    last_render = 0.0
    last_sample = (time.monotonic(), 0)
    line_width = 0
    displayed = False

    def show_progress(done: int, total: int) -> None:
        nonlocal last_render, last_sample, line_width, displayed
        now = time.monotonic()
        elapsed = now - last_sample[0]
        speed = (done - last_sample[1]) / elapsed if elapsed > 0 else None
        if now - last_render >= 0.5 or done == total:
            line = file_line(task.media_mid, task.position, task.total_files,
                             done, total, speed)
            line_width = max(line_width, len(line))
            print('\r' + line.ljust(line_width), end='', flush=True)
            displayed = True
            last_render = now
            last_sample = (now, done)

    try:
        resumed = partial.open()
        last_sample = (time.monotonic(), resumed)
        if resumed:
            print(f'    [续传] 文件 {task.position}/{task.total_files}，从已保存进度继续', flush=True)
            detail(f'续传 m{task.media_mid}: {resumed}/{source["size"]} 字节')
        await download_parts(partial,
                             lambda offset, limit: fetch_resume_part(page, source, offset, limit),
                             show_progress)
    finally:
        if displayed:
            print(flush=True)
    if final.exists():
        raise RuntimeError(f'目标文件下载期间已出现，未覆盖：{final}')
    size, saved_hash = await asyncio.to_thread(partial.finalize, final)
    existing_index[task.media_mid] = [final]
    return 'downloaded', final, size, saved_hash, None


async def discover_media(page: Page, url: str, item: Message) -> list[FoundMedia]:
    """在当前页面识别一条消息的附件，媒体 ID 用于续跑去重。"""
    validate_types(item.parts, item.content_types)
    bubble = await locate_message(page, url, item)
    targets = await media_targets(bubble)
    count = await targets.count()
    if count > item.media_count and len(item.parts) > 1:
        raise RuntimeError(f'页面显示 {item.media_count} 个附件，但识别到 {count} 个可操作媒体')
    expected = ({mid for mid, kind in item.content_types if kind in DOWNLOADABLE}
                if item.content_types else
                {mid for mid, _text, _date, has_media in item.parts if has_media})
    found: list[FoundMedia] = []
    seen: set[str] = set()
    for index in range(count):
        media_mid, expected_size = await task_metadata(targets.nth(index))
        if media_mid not in expected:
            continue
        if media_mid not in seen:
            seen.add(media_mid)
            # 单条服务器原消息可能位于 Web K 的相册泡泡内；按原 ID 从缓存取附件。
            position = item.media_count + 1 if len(item.parts) == 1 and count > item.media_count else index + 1
            found.append(FoundMedia(item.mid, position, media_mid, expected_size))
    actual = {entry.media_mid for entry in found}
    if actual - expected:
        raise RuntimeError(f'消息 m{item.mid} 附件 ID 不齐：原消息 {sorted(expected)}，页面可操作 {sorted(actual)}')
    # Web K 有时只渲染相册的一部分；未渲染项仍可通过原消息 ID 从页面缓存读取。
    fallback_index = item.media_count
    for mid, _text, _timestamp, _has_media in item.parts:
        if mid not in expected or mid in actual:
            continue
        try:
            source = await resume_source(bubble, mid)
        except RuntimeError as exc:
            raise RuntimeError(f'消息 m{item.mid} 附件 ID 不齐：缺少 m{mid}，'
                               f'页面缓存读取失败：{exc}') from exc
        if source['mid'] != mid:
            raise RuntimeError(f'相册缓存附件 ID 不符：预期 {mid}，实际 {source["mid"]}')
        fallback_index += 1
        found.append(FoundMedia(item.mid, fallback_index, mid, source['size']))
        actual.add(mid)
    if actual != expected:
        raise RuntimeError(f'消息 m{item.mid} 附件 ID 不齐：原消息 {sorted(expected)}，页面可操作 {sorted(actual)}')
    return found


async def save_media(page: Page, url: str, task: MediaTask,
                     existing_index: dict[str, list[Path]]) -> tuple[str, Path | None]:
    """定位当前附件并核对身份，再决定复用或生成新版本。"""
    bubble = await locate_message(page, url, task.message)
    if task.index > task.message.media_count:
        target = bubble
    else:
        targets = await media_targets(bubble)
        target = targets.nth(task.index - 1)
        if not await target.count():
            raise RuntimeError(f'附件在页面中消失：m{task.media_mid}')
        current_mid, _ = await task_metadata(target)
        if current_mid != task.media_mid:
            raise RuntimeError(f'附件位置变化：预期 {task.media_mid}，实际 {current_mid}')
    state, path, _size, _digest, old_folder = await save_one(page, target, task, existing_index)
    return state, old_folder


def is_transport_timeout(error: Exception) -> bool:
    """仅识别分片传输耗尽重试的超时，避免重试身份或文件校验错误。"""
    detail = str(error).lower()
    return '分片偏移' in detail and '连续失败' in detail and 'timeout' in detail


async def save_media_with_recovery(scan_page: Page, download_page: Page, url: str,
                                   task: MediaTask, existing_index: dict[str, list[Path]]
                                   ) -> tuple[str, Path | None, Page]:
    """连接持续超时时重建下载页；同一分片由 PartialFile 核验后续传。"""
    try:
        state, old_folder = await save_media(download_page, url, task, existing_index)
        return state, old_folder, download_page
    except Exception as error:
        if not is_transport_timeout(error):
            raise
        print('  下载连接暂时中断，正在恢复后继续', flush=True)
        detail(f'下载连接超时，重建页面后续传 m{task.media_mid}')
        replacement = None
        try:
            replacement = await scan_page.context.new_page()
            await open_group(replacement, url)
        except Exception as recovery_error:
            if replacement is not None:
                with suppress(Exception):
                    await replacement.close()
            raise DownloadTransportUnavailable('下载页面重建失败；保留分片并停止本轮，'
                                               '待连接恢复后重跑 BAT 续传') from recovery_error
        try:
            state, old_folder = await save_media(replacement, url, task, existing_index)
        except Exception as retry_error:
            with suppress(Exception):
                await replacement.close()
            if is_transport_timeout(retry_error):
                raise DownloadTransportUnavailable('重建下载页面后仍连续超时；保留分片并停止本轮，'
                                                   '待连接恢复后重跑 BAT 续传') from retry_error
            raise
        if download_page is not scan_page:
            with suppress(Exception):
                await download_page.close()
        return state, old_folder, replacement


async def restore_scan_position(page: Page, url: str, item: Message) -> None:
    """单页下载跳转后回到刚确认的消息，继续向更旧历史滚动。"""
    bubble = await locate_message(page, url, item)
    await bubble.scroll_into_view_if_needed()


async def audit_messages(page: Page, url: str, minimum_sources: int) -> tuple[int, int]:
    """逐页面消息块核对至少 N 条原消息，并保留逐原消息扫描记录。"""
    title = await open_group(page, url)
    record = ScanRecord(SCAN_RECORD_ROOT / f'{group_id(url)}.jsonl')
    progress = ScanProgress()
    bubbles = sources = failures = 0
    print(f'[核验] {title}：正在检查最近消息，不下载附件', flush=True)
    async for item in iter_history(page, None, set(), progress):
        bubbles += 1
        sources += len(item.parts)
        record.record_scan([item], reached_oldest=False)
        try:
            validate_types(item.parts, item.content_types)
            downloadable = {mid for mid, kind in item.content_types if kind in DOWNLOADABLE}
            if downloadable or item.media_count:
                found = await discover_media(page, url, item)
                record.record_discovered(item.mid, found)
                actual = {entry.media_mid for entry in found}
                if actual != downloadable:
                    raise RuntimeError(f'附件 ID 不齐：原消息 {sorted(downloadable)}，页面 {sorted(actual)}')
        except Exception as exc:
            failures += 1
            for mid, _text, _date, _has_media in item.parts:
                record.record_result(mid, 'failed', detail=f'扫描核验失败：{exc}')
            failure(f'消息 m{item.mid} 核验失败')
            print(f'  一条消息核验失败，详情见 {log_location()}', flush=True)
        if sources >= minimum_sources:
            break
    print(f'[核验] 已检查 {sources} 条消息，失败 {failures}', flush=True)
    if failures:
        raise RuntimeError(f'{failures} 个页面消息块核验失败')
    if sources < minimum_sources:
        print('[核验] 已到达可访问历史最早端', flush=True)
    return bubbles, sources


async def download_group(page: Page, url: str, output: Path, limit: int | None,
                         dry_run: bool, on_group_open: Callable[[str], None] | None = None,
                         full_scan: bool = False, pause_at_oldest: bool = False) -> tuple[int, int, int, int]:
    """单页流式处理：完整确认下一组后下载上一组，最多领先一组。"""
    title = await open_group(page, url)
    if on_group_open:
        on_group_open(title)
    root = group_root(output, title, group_id(url))
    record = ScanRecord(SCAN_RECORD_ROOT / f'{group_id(url)}.jsonl')
    prior_messages = record.ordered_messages()
    incremental = (record.history_complete and limit is None and not full_scan
                   and all(item.parts for item in record.messages.values()))
    known_ids = set(record.messages) if incremental else set()
    if full_scan and not dry_run:
        record.append([{'kind': 'reset'}])
    paths = load_paths()
    staging_root = ((paths['staging'] if output.resolve() == paths['output'] else
                     output.resolve().parent / f'.{output.resolve().name}.telegg-staging') / root.name)
    existing_index = {} if dry_run else existing_media_index(root)
    if not dry_run:
        for mid, files in existing_media_index(staging_root).items():
            existing_index.setdefault(mid, []).extend(files)
    progress = ScanProgress()
    scanned_desc: list[Message] = []
    wanted: set[str] = set()
    handled: set[str] = set()
    seen_media: set[str] = set()
    relocated_sources: set[Path] = set()
    downloaded = reused = failed = 0
    download_page = page
    confirmed_groups = processed_groups = 0
    last_processed_date: str | None = None
    ready_group: tuple[list[Message], set[str]] | None = None

    async def process_group(batch: list[Message], selected: set[str]) -> None:
        nonlocal downloaded, reused, failed, existing_index, download_page, last_processed_date
        items = [item for item in reversed(batch) if item.mid in selected and item.mid not in handled]
        if not items:
            return
        before_downloaded, before_reused, before_failed = downloaded, reused, failed
        batch_name = group_folder(batch[-1]) if batch[-1].text.strip() else '未归组'
        published = root / batch_name
        pending = staging_root / batch_name
        if not dry_run:
            anchor = batch[-1] if batch[-1].text.strip() else None
            prior_root = published.is_dir()
            prior_staging = pending.is_dir()
            prepare_group_folder(root, anchor)
            prepare_group_folder(staging_root, anchor)
            moved = (prepare_item_folders(published, batch) +
                     prepare_item_folders(pending, batch))
            if moved or (not prior_root and published.is_dir()) or (not prior_staging and pending.is_dir()):
                existing_index = existing_media_index(root)
                for mid, files in existing_media_index(staging_root).items():
                    existing_index.setdefault(mid, []).extend(files)
        if not dry_run and published.is_dir():
            expected = {mid for item in batch for mid, _text, _date, has_media in item.parts if has_media}
            if any(not existing_index.get(mid) for mid in expected):
                if pending.exists():
                    raise RuntimeError(f'编组同时存在于下载中和已完成目录：{batch_name}')
                pending.parent.mkdir(parents=True, exist_ok=True)
                published.rename(pending)
                existing_index = existing_media_index(root)
                for mid, files in existing_media_index(staging_root).items():
                    existing_index.setdefault(mid, []).extend(files)
        work_root = root if published.is_dir() else staging_root
        total_files = sum(sum(kind in DOWNLOADABLE for _mid, kind in item.content_types)
                          if item.content_types else item.media_count for item in items)
        done_files = 0
        current_date = date_label(min(item.timestamp for item in items))
        last_processed_date = current_date
        print(f'[{"预览" if dry_run else "下载"}] 正在处理 {current_date} · '
              f'本组 {len(items)} 条消息、{total_files} 个文件', flush=True)
        detail(f'编组 {processed_groups + 1}/{confirmed_groups} 日期 {current_date}')
        if not dry_run:
            print(group_line(0, total_files), flush=True)
        anchor = batch[-1] if batch[-1].text.strip() else None
        if not dry_run:
            prepare_group_folder(work_root, anchor, create=True)
        for message_number, item in enumerate(items, 1):
            handled.add(item.mid)
            folder = destination(work_root, anchor, batch, item)
            if dry_run:
                print(f'  消息 {message_number}/{len(items)} · 约 {item.media_count} 个文件', flush=True)
                detail(f'预览 m{item.mid}: {folder}')
                continue
            try:
                validate_types(item.parts, item.content_types)
                for part in item.parts:
                    if not item.content_types:
                        continue  # 旧扫描记录仍按原流程续跑。
                    mid = part[0]
                    content_path = save_message_content(paths['scan_records'].parent / 'messages' / root.name, item, part)
                    record.record_content_file(mid, content_path)
                    if dict(item.content_types)[mid] in METADATA_ONLY:
                        record.record_result(mid, 'saved_content', detail=str(content_path))
                has_downloadable = any(kind in DOWNLOADABLE for _mid, kind in item.content_types)
                if not item.media_count or (item.content_types and not has_downloadable):
                    if has_downloadable:
                        raise RuntimeError('原消息有可下载媒体，页面却没有可操作附件')
                    detail(f'消息 m{item.mid} 内容已保存，无文件附件')
                    if not item.content_types:
                        for mid, _text, _timestamp, _has_media in item.parts:
                            record.record_result(mid, 'no_media')
                    continue
                expected_ids = ({mid for mid, kind in item.content_types if kind in DOWNLOADABLE}
                                if item.content_types else
                                {mid for mid, _text, _date, has_media in item.parts if has_media})
                cached_ids = {found.media_mid for found in record.media.values()
                              if found.source_mid == item.mid}
                if item.mid in record.discovered and expected_ids and cached_ids != expected_ids:
                    detail(f'消息 m{item.mid} 旧附件记录不完整，重新识别')
                    record.invalidate_media(item.mid)
                if item.mid not in record.discovered:
                    record.record_discovered(item.mid, await discover_media(page, url, item))
                media = sorted((found for found in record.media.values()
                                if found.source_mid == item.mid), key=lambda found: found.index)
                media = [found for found in media if found.media_mid not in seen_media]
            except Exception as exc:
                failed += 1
                done_files += (sum(kind in DOWNLOADABLE for _mid, kind in item.content_types)
                               if item.content_types else item.media_count)
                for mid, _text, _timestamp, _has_media in item.parts:
                    record.record_result(mid, 'failed', detail=f'附件识别失败：{exc}')
                failure(f'消息 m{item.mid} 附件识别失败')
                print(f'  消息 {message_number}/{len(items)}：附件识别失败，详情见 {log_location()}', flush=True)
                print(group_line(done_files, total_files), flush=True)
                continue
            reused_in_message = 0
            for media_number, found in enumerate(media, 1):
                seen_media.add(found.media_mid)
                partial_base = (paths['partials'] if output.resolve() == paths['output'] else
                                output.resolve().parent / f'.{output.resolve().name}.telegg-partials')
                partial_root = partial_base / group_id(url)
                task = MediaTask(item, found.index, found.media_mid, folder, found.expected_size,
                                 partial_root, done_files + 1, total_files)
                try:
                    state, old_folder, download_page = await save_media_with_recovery(
                        page, download_page, url, task, existing_index)
                    if old_folder:
                        relocated_sources.add(old_folder)
                    if state == 'downloaded':
                        downloaded += 1
                        saved_paths = existing_index.get(found.media_mid, [])
                        if len(saved_paths) == 1 and saved_paths[0].is_file():
                            size = saved_paths[0].stat().st_size
                            print(file_line(found.media_mid, done_files + 1, total_files,
                                            size, size, None, finished=True), flush=True)
                        detail(f'消息 m{item.mid} 附件 m{found.media_mid} 已下载')
                    else:
                        reused += 1
                        reused_in_message += 1
                    media_paths = existing_index.get(found.media_mid, [])
                    if (len(media_paths) == 1 and media_paths[0].is_file() and
                            found.expected_size != media_paths[0].stat().st_size):
                        record.record_discovered(item.mid, [FoundMedia(
                            item.mid, found.index, found.media_mid, media_paths[0].stat().st_size)])
                    record.record_result(found.media_mid, state,
                                         detail=str(media_paths[0]) if len(media_paths) == 1 else '')
                    if old_folder:
                        archive_empty_relocated_folders(root, relocated_sources)
                        relocated_sources.clear()
                except Exception as exc:
                    failed += 1
                    record.record_result(found.media_mid, 'failed', detail=f'{type(exc).__name__}: {exc}')
                    failure(f'消息 m{item.mid} 附件 m{found.media_mid} 失败')
                    print(f'  文件 {done_files + 1}/{total_files} 处理失败，详情见 {log_location()}', flush=True)
                    if page.is_closed() or 'TargetClosedError' in type(exc).__name__:
                        raise RuntimeError('Chrome 页面已关闭；本次停止，重新运行 BAT 可接着处理未完成附件') from exc
                    if isinstance(exc, DownloadTransportUnavailable):
                        raise
                finally:
                    done_files += 1
                    print(group_line(done_files, total_files), flush=True)
            if reused_in_message:
                detail(f'消息 m{item.mid} 已有附件 {reused_in_message}/{len(media)} 已核对')
            for mid, _text, _timestamp, _has_media in item.parts:
                if mid not in record.results:
                    failed += 1
                    record.record_result(mid, 'failed', detail='未匹配到附件处理结果')
                    detail(f'消息 m{mid} 缺少处理结果')
                    print(f'  消息 {message_number}/{len(items)} 处理未完成', flush=True)
        if not dry_run:
            expected = {mid for item in batch for mid, _text, _date, has_media in item.parts if has_media}
            ready = (selected == {item.mid for item in batch} and
                     failed == before_failed and
                     all(len(existing_index.get(mid, [])) == 1 and
                         (mid not in record.media or not record.media[mid].expected_size or
                          existing_index[mid][0].stat().st_size == record.media[mid].expected_size)
                         for mid in expected))
            if ready and pending.is_dir():
                if published.exists():
                    raise RuntimeError(f'编组发布目标已存在：{published}')
                published.parent.mkdir(parents=True, exist_ok=True)
                pending.rename(published)
                existing_index = existing_media_index(root)
                for mid, files in existing_media_index(staging_root).items():
                    existing_index.setdefault(mid, []).extend(files)
                detail(f'编组文件已核对并移入成品目录：{batch_name}')
            print(f'[本组完成] 新下载 '
                  f'{downloaded - before_downloaded}，已有 {reused - before_reused}，'
                  f'失败 {failed - before_failed}', flush=True)

    async def confirm_group(batch: list[Message], selected: set[str]) -> None:
        nonlocal ready_group, confirmed_groups, processed_groups
        if not selected:
            return
        confirmed_groups += 1
        detail(f'扫描已确认 {confirmed_groups} 组，已处理 {processed_groups} 组')
        if ready_group is not None:
            await process_group(*ready_group)
            processed_groups += 1
        ready_group = (batch, selected)

    print(f'[扫描] {title}：正在读取消息', flush=True)
    pending_desc: list[Message] = []
    seen_text = False
    async for item in iter_history(page, None, known_ids, progress,
                                   pause_at_oldest=pause_at_oldest, peer_id=int(group_id(url)),
                                   api_scan=True):
        scan_url = getattr(page, 'url', None)
        scanned_desc.append(item)
        if not dry_run:
            record.record_scan([item], reached_oldest=False)
        if limit is None or len(wanted) < limit:
            wanted.add(item.mid)
        selected = item.mid in wanted
        if limit is not None and selected and (len(wanted) == 1 or len(wanted) % 10 == 0
                                               or len(wanted) == limit):
            print(f'[扫描] 已选 {len(wanted)}/{limit} 条消息 · {date_label(item.timestamp)}', flush=True)
        if not seen_text and not item.text.strip():
            # 最新端无后续正文，目录已经确定为“未归组”。
            await confirm_group([item], {item.mid} if selected else set())
            if limit is not None and len(wanted) >= limit:
                break
            if getattr(page, 'url', None) != scan_url:
                await restore_scan_position(page, url, item)
            continue
        if item.text.strip() and pending_desc:
            batch = list(reversed(pending_desc))
            await confirm_group(batch, {entry.mid for entry in batch if entry.mid in wanted})
            pending_desc = [item]
        else:
            pending_desc.append(item)
        seen_text = seen_text or bool(item.text.strip())
        if limit is not None and not selected and item.text.strip():
            break
        # 下载会跳到较新的媒体消息，继续滚动前返回当前旧端边界。
        if getattr(page, 'url', None) != scan_url:
            await restore_scan_position(page, url, item)

    if incremental and progress.stopped_on_known and pending_desc:
        # 新文字可能使上轮末尾的无文字消息改归本组。
        for old in reversed(prior_messages):
            if old.text.strip():
                break
            pending_desc.append(old)
            wanted.add(old.mid)
    if pending_desc:
        batch = list(reversed(pending_desc))
        await confirm_group(batch, {entry.mid for entry in batch if entry.mid in wanted})
    if ready_group is not None:
        await process_group(*ready_group)
        processed_groups += 1
    if not dry_run:
        if progress.reached_oldest and limit is None and scanned_desc:
            record.record_scan([scanned_desc[-1]], reached_oldest=True)
        # 已记录但本轮未扫描的附件仍需重试，按组从新到旧处理。
        for anchor, batch in reversed(grouped(record.ordered_messages())):
            remaining = {entry.mid for entry in batch if entry.mid not in handled and
                         (limit is None or entry.mid in wanted)}
            if remaining:
                confirmed_groups += 1
                await process_group(batch, remaining)
                processed_groups += 1
        publish_ready_staged_groups(root, staging_root, record)
        archive_empty_relocated_folders(root, relocated_sources)
        selected_parts = [part for item in scanned_desc if item.mid in wanted for part in item.parts]
        missing = [mid for mid, text, _date, has_media in selected_parts
                   if mid not in record.source_messages or
                   (mid not in record.results and (text or has_media)) or
                   (mid in record.source_messages and
                    record.source_messages[mid].get('content_type') not in (None, 'unclassified') and
                    (mid not in record.content_files or
                     not Path(record.content_files[mid]).is_file()))]
        if missing:
            failed += len(missing)
            detail('缺少扫描或处理记录：' + ', '.join('m' + mid for mid in missing[:10]))
            print(f'[核验] {len(missing)} 条消息处理未完成，详情见 {log_location()}', flush=True)
        else:
            print(f'[核验] 已选 {len(selected_parts)} 条消息均已处理', flush=True)
    if last_processed_date:
        print(f'[进度] 本次已处理至 {last_processed_date}', flush=True)
    return len({item.mid for item in scanned_desc if item.mid in wanted}), downloaded, reused, failed
