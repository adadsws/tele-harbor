"""通过已登录 Chrome 会话扫描服务器原消息并分片下载媒体。"""

from __future__ import annotations

import asyncio
import base64
import mimetypes
import re
import hashlib
import json
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Callable
from urllib.parse import urlsplit

from playwright.async_api import Page, TimeoutError as BrowserTimeout

from .content_archive import (prepare_group_folder,
                              prepare_item_folders, save_group_manifest,
                              save_message_content, save_text_file)
from .content_types import DOWNLOADABLE, METADATA_ONLY, MEDIA_SELECTOR, classify, validate_types
from .diagnostics import detail, failure, log_location
from .folder_archive import archive_empty_folders
from .layout import (Message, destination, group_folder, group_id, group_root, grouped,
                     same_avatar_group, safe_name)
from .reply_groups import (LINKED_NAME, _record_moved_paths, linked_group_names,
                           parent_for, reconcile_reply_groups)
from .local_reuse import (has_group_files, move_files_only, rekey_legacy_media,
                          reuse_root)
from .local_config import load_paths
from .progress import (date_label, file_line, group_line, media_label, media_timestamp, size_label,
                       message_count, ProgressLine)
from .resumable import PartialFile, download_parts, verified_completed_file
from .scan_record import FoundMedia, ScanRecord
from .verification_manifest import verified_digest, read_snapshot
from .errors import LocalDataError, RemoteFileError
from .local_validation import validate_local_archive

BUBBLE = '#column-center .bubble[data-mid]:not(.service)'
SCROLL = '#column-center .chat.active .bubbles-scrollable, #column-center > .bubbles-scrollable'
MEDIA = MEDIA_SELECTOR
SCAN_RECORD_ROOT = load_paths()['scan_records']
SERVER_HISTORY_TIMEOUT = 60
_PENDING_PART_READS: set[asyncio.Task] = set()


async def server_history_with_recovery(page: Page, peer_id: int, oldest_mid: int,
                                       request: Callable) -> object:
    """历史请求超时后重建可见群组会话并重试一次，仍失败则保留未完成状态。"""
    try:
        return await asyncio.wait_for(request(page, peer_id, oldest_mid),
                                      timeout=SERVER_HISTORY_TIMEOUT)
    except (TimeoutError, BrowserTimeout):
        if page.is_closed():
            raise RuntimeError(f'读取 m{oldest_mid} 之前的历史时 Chrome 已关闭')
        detail(f'服务器历史读取超时，重开群组并重试：m{oldest_mid}')
        print('[扫描] 服务器响应缓慢，正在重建群组连接', flush=True)
        await page.goto('about:blank', wait_until='domcontentloaded')
        await open_group(page, f'https://web.telegram.org/k/#{peer_id}')
        try:
            return await asyncio.wait_for(request(page, peer_id, oldest_mid),
                                          timeout=SERVER_HISTORY_TIMEOUT)
        except (TimeoutError, BrowserTimeout) as exc:
            raise RuntimeError(f'读取 m{oldest_mid} 之前的服务器历史连续超时') from exc


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


def normalized_reply_id(value: int | str) -> str:
    """Web K 可能已给引用 ID 加上频道偏移；只对原始 32 位 ID 加一次。"""
    number = int(value)
    if number <= 0:
        raise ValueError('Telegram 引用目标 ID 无效')
    return str(number if number >= (1 << 32) else number + (1 << 32))


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
                const reply = message.reply_to?.reply_to_peer_id ? 0 :
                    Number(message.reply_to?.reply_to_msg_id || 0);
                return {id, reply, albumId: String(message.grouped_id || ''),
                    sender: JSON.stringify(message.from_id || message.peer_id || {}),
                    outgoing: !!(message.pFlags?.out || message.out), postAuthor: message.post_author || '',
                    text: message.message || '', date: Number(message.date),
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
    raw_text = str(row['text'])
    content = raw_text.strip()
    media = bool(row['hasMedia'])
    if not date or (not content and not media):
        raise RuntimeError(f'服务器消息 m{mid} 缺少正文、媒体或时间')
    return Message(mid, raw_text, date, int(media), ((mid, raw_text, date, media),),
                   ((mid, classify(str(row['mediaType']), tuple(row['flags']))),),
                   ((mid, str(row['detail'])),),
                   ((mid, normalized_reply_id(row['reply'])),) if row['reply'] else (),
                   str(row['albumId']), str(row['sender']), bool(row['outgoing']), str(row['postAuthor']))


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
                (i || offset ? id >= (i ? ids[i - 1] : offset) : false)))
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
                const reply = message.reply_to?.reply_to_peer_id ? 0 :
                    Number(message.reply_to?.reply_to_msg_id || 0);
                return {id: Number(message.id), reply, albumId: String(message.grouped_id || ''),
                    sender: JSON.stringify(message.from_id || message.peer_id || {}),
                    outgoing: !!(message.pFlags?.out || message.out), postAuthor: message.post_author || '',
                    text: message.message || '', date: Number(message.date),
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
        raw_text = str(row['text'])
        content = raw_text.strip()
        media = bool(row['hasMedia'])
        if not date or (not content and not media):
            raise RuntimeError(f'服务器消息 m{mid} 缺少正文、媒体或时间')
        result.append(Message(mid, raw_text, date, int(media), ((mid, raw_text, date, media),),
                              ((mid, classify(str(row['mediaType']), tuple(row['flags']))),),
                              ((mid, str(row['detail'])),),
                              ((mid, normalized_reply_id(row['reply'])),) if row['reply'] else (),
                              str(row['albumId']), str(row['sender']), bool(row['outgoing']), str(row['postAuthor'])))
    return result


def combine_server_album(newest_first: list[Message]) -> Message:
    """将服务器逐条返回的相册还原为网页上的一个消息块。"""
    if len(newest_first) == 1:
        return newest_first[0]
    ordered = list(reversed(newest_first))
    album_id = ordered[0].album_id
    if not album_id or any(item.album_id != album_id or len(item.parts) != 1 for item in ordered):
        raise RuntimeError('服务器相册消息不连续，停止归组')
    captions = list(dict.fromkeys(item.text for item in ordered if item.text.strip()))
    return Message(ordered[0].mid, '\n'.join(captions), ordered[0].timestamp,
                   sum(item.media_count for item in ordered),
                   tuple(part for item in ordered for part in item.parts),
                   tuple(part for item in ordered for part in item.content_types),
                   tuple(part for item in ordered for part in item.content_details),
                   tuple(part for item in ordered for part in item.reply_targets), album_id,
                   ordered[0].sender_id, ordered[0].outgoing, ordered[0].post_author)


async def server_media_source(page: Page, peer_id: int, media_mid: str) -> dict:
    """按原消息 ID 在已登录页面取媒体身份，敏感引用只留在 Chrome 内存。"""
    source = await page.evaluate('''async ({peerId, mid}) => {
        const m = window.appDownloadManager?.managers;
        if (!m?.apiManager || !m?.appPeersManager) throw new Error('Telegram 历史接口不可用');
        const low = Number(mid) % 4294967296;
        const peer = await m.appPeersManager.getInputPeerById(peerId);
        const result = await m.apiManager.invokeApi('messages.getHistory', {
            peer, offset_id: low + 1, offset_date: 0, add_offset: 0,
            limit: 1, max_id: 0, min_id: 0, hash: 0
        }, {noErrorBox: true});
        const message = result?.messages?.[0];
        if (message?._ !== 'message' || Number(message.id) !== low)
            throw new Error('服务器未返回准确的附件原消息');
        const media = message.media?.document || message.media?.photo;
        if (!media || !['document', 'photo'].includes(media._))
            throw new Error('原消息没有可下载的文档或照片');
        const photoSize = media._ === 'photo' ? media.sizes?.at(-1) : null;
        const size = media._ === 'document' ? media.size :
            (photoSize?.size || Math.max(0, ...(photoSize?.sizes || [])));
        const nameAttr = media._ === 'document' ?
            media.attributes?.find(attr => attr._ === 'documentAttributeFilename') : null;
        const originalName = media._ === 'document' ? (nameAttr?.file_name || '') : '';
        const name = media._ === 'document' ?
            (originalName || `document_${media.id}`) :
            `photo_${media.id}_${photoSize?.type || ''}.jpg`;
        (window.__teleggServerMedia ||= new Map()).set(String(mid), message);
        return {peer: String(peerId), mid: String(mid), kind: media._,
            id: String(media.id), dc: Number(media.dc_id), size: Number(size),
            thumb: photoSize?.type || '', mime_type: media.mime_type || '', name,
            original_name: originalName};
    }''', {'peerId': peer_id, 'mid': media_mid})
    if (source['mid'] != media_mid or source['size'] <= 0 or not source['id']):
        raise RuntimeError(f'服务器附件身份或大小无效：m{media_mid}')
    return source


def completed_source(mid: str, source: dict, record: ScanRecord,
                     existing_index: dict[str, list[Path]]) -> bool:
    """快速端点只越过路径记录与批量索引一致的原消息，不逐文件读盘。"""
    kind = source.get('content_type')
    result = record.results.get(mid, {})
    status = result.get('status')
    if kind in DOWNLOADABLE:
        files = existing_index.get(mid, [])
        return (result.get('naming_revision') == 3
                and status in ('downloaded', 'skipped', 'relocated')
                and len(files) == 1
                and isinstance(result.get('detail'), str)
                and files[0] == Path(result['detail'])
                and mid in record.content_files)
    return status in ('saved_content', 'no_media') and mid in record.content_files


def pending_reply_target(batch: list[Message], record: ScanRecord,
                         existing_index: dict[str, list[Path]],
                         output_root: Path | None = None) -> bool:
    """引用目标未核齐时，引用所在正文组不得发布为成品。"""
    targets = {target for item in batch for _mid, target in item.reply_targets if target}
    sources = getattr(record, 'source_messages', {})
    targets.update(str(sources.get(mid, {}).get('reply_to') or '')
                   for item in batch for mid, *_rest in item.parts)
    targets.discard('')
    links = linked_group_names(record)
    component = links.get(batch[0].mid)
    if component:
        members = [part[0] for anchor, group in grouped(record.ordered_messages())
                   if links.get(anchor.mid) == component for item in group for part in item.parts]
        return any(mid not in sources or
                   not completed_source(mid, sources[mid], record, existing_index)
                   for mid in members)
    if not targets:
        return False
    if batch[0].mid not in linked_group_names(record):
        return True
    if any(target not in record.source_messages or
           not completed_source(target, record.source_messages[target], record, existing_index)
           for target in targets):
        return True
    if output_root is None:
        return False
    links = linked_group_names(record)
    source_to_anchor = {part[0]: anchor for anchor, group in grouped(record.ordered_messages())
                        for item in group for part in item.parts}
    return any(target not in source_to_anchor or
               (source_to_anchor[target].mid != batch[0].mid and
                not has_group_files(parent_for(output_root, source_to_anchor[target], links) /
                                    group_folder(source_to_anchor[target])))
               for target in targets)


def demote_unready_replies(root: Path, staging_root: Path, record: ScanRecord,
                           existing_index: dict[str, list[Path]]) -> int:
    """把尚未形成完整关联的成品组移回暂存，消除裸露的引用标记。"""
    if not root.is_dir():
        return 0
    links = linked_group_names(record)
    moves: list[tuple[Path, Path]] = []
    for anchor, batch in grouped(record.ordered_messages()):
        if anchor is None or not pending_reply_target(batch, record, existing_index, root):
            continue
        candidates = list(dict.fromkeys((parent_for(root, anchor, links) / group_folder(anchor),
                                         root / group_folder(anchor))))
        present = [source for source in candidates if has_group_files(source)]
        if len(present) > 1:
            raise RuntimeError(f'引用组成品位置不唯一：{group_folder(anchor)}')
        if present:
            source = present[0]
            moves.append((source, parent_for(staging_root, anchor, links) / source.name))
    for source in root.iterdir():
        if has_group_files(source) and not LINKED_NAME.fullmatch(source.name) and any(
                source.rglob('%引用目标*.txt')) and not any(old == source for old, _ in moves):
            moves.append((source, staging_root / source.name))
    for source, target in moves:
        if has_group_files(target):
            raise RuntimeError(f'引用组暂存目标已存在，未覆盖：{target}')
    for source, target in moves:
        target.parent.mkdir(parents=True, exist_ok=True)
        move_files_only(source, target)
        _record_moved_paths(record, source, target)
        if not hasattr(record, 'relocated_folders'):
            record.relocated_folders = set()
        record.relocated_folders.add(source)
    return len(moves)


def resume_endpoint(record: ScanRecord,
                    existing_index: dict[str, list[Path]]) -> int | None:
    """选最近的未完成原消息；全已完成时从最旧已知 ID 之后继续。"""
    if not record.source_messages:
        return None
    for mid in sorted(record.source_messages, key=int, reverse=True):
        if not completed_source(mid, record.source_messages[mid], record, existing_index):
            bubble_mid = str(record.source_messages[mid].get('bubble_mid', mid))
            bubble = getattr(record, 'messages', {}).get(bubble_mid)
            return max((int(part[0]) for part in bubble.parts), default=int(mid)) if bubble else int(mid)
    return min(map(int, record.source_messages)) - 1


def resume_title_anchor(messages: list[Message], endpoint: int | None) -> Message | None:
    """从断点较新一侧带回相邻消息块，供跨断点头像段归组。"""
    if endpoint is None:
        return None
    return min((item for item in messages
                if int(item.mid) > endpoint),
               key=lambda item: int(item.mid), default=None)


def resume_avatar_prefix(messages: list[Message], endpoint: int | None) -> list[Message]:
    """带回断点较新侧的完整连续头像段后缀，避免重写清单时遗漏已跳过成员。"""
    nearest = resume_title_anchor(messages, endpoint)
    if nearest is None:
        return []
    prefix = [nearest]
    for item in sorted((item for item in messages if int(item.mid) > int(nearest.mid)),
                       key=lambda item: int(item.mid)):
        if not same_avatar_group(prefix[-1], item):
            break
        prefix.append(item)
    return list(reversed(prefix))


async def verify_resume_prefix(page: Page, peer_id: int, endpoint: int,
                               record: ScanRecord,
                               existing_index: dict[str, list[Path]],
                               max_messages: int | None = None) -> tuple[int, int, int] | None:
    """核对端点前缀；遇到首个变化或缺失项就在该处开始处理。"""
    seen: set[str] = set()
    media_files = 0
    limit_album = ''
    last_seen_album = ''
    last_included = 0
    offset = 1 << 32  # Telegram offset_id=0 表示从最新端读取。
    while True:
        batch = await server_history_with_recovery(page, peer_id, offset, server_older_batch)
        if not batch:
            print('[快速模式] 端点前服务器返回空页，改为顺序扫描', flush=True)
            return None
        reached = False
        for item in batch:
            mid = item.mid
            if limit_album and item.album_id != limit_album:
                return last_included - 1, len(seen), media_files
            if int(mid) <= endpoint:
                if str(endpoint) in record.source_messages and int(mid) != endpoint:
                    # 端点消息已不可访问；只在较新区间完整匹配时从下一条继续。
                    return int(mid), len(seen), media_files
                reached = True
                break
            previous = record.source_messages.get(mid)
            valid = (previous is not None and previous.get('timestamp') == item.timestamp
                    and previous.get('text') == item.text
                    and previous.get('has_media') == item.parts[0][3]
                    and previous.get('content_type') == item.content_types[0][1]
                    and completed_source(mid, previous, record, existing_index))
            if not valid:
                if item.album_id and last_seen_album == item.album_id:
                    print('[快速模式] 相册中部出现变化，改为顺序扫描', flush=True)
                    return None  # 改动落在已跳过相册中部时回退完整顺序扫描。
                return int(mid), len(seen), media_files
            if previous.get('reply_to', '') != dict(item.reply_targets).get(mid, ''):
                record.record_reply(mid, dict(item.reply_targets).get(mid, ''))
            seen.add(mid)
            last_seen_album = item.album_id
            last_included = int(mid)
            media_files += previous.get('content_type') in DOWNLOADABLE
            if max_messages is not None and len(seen) >= max_messages:
                if item.album_id:
                    limit_album = item.album_id
                else:
                    return int(mid) - 1, len(seen), media_files
        if reached:
            return endpoint, len(seen), media_files
        if offset != 1 << 32 and int(batch[-1].mid) >= offset:
            print('[快速模式] 端点分页未向旧端推进，改为顺序扫描', flush=True)
            return None
        offset = int(batch[-1].mid)


async def iter_history_from_endpoint(page: Page, peer_id: int, endpoint: int,
                                     progress: ScanProgress) -> AsyncIterator[Message]:
    """直接从服务器端点继续向旧端分页，避免网页逐条滚动。"""
    offset = (1 << 32) if endpoint == 0 else endpoint + 1
    pending_album: list[Message] = []
    while True:
        batch = await server_history_with_recovery(page, peer_id, offset, server_older_batch)
        if not batch:
            if pending_album:
                yield combine_server_album(pending_album)
            progress.reached_oldest = True
            print('[扫描] 已到服务器确认的最早端', flush=True)
            return
        for item in batch:
            if offset != 1 << 32 and int(item.mid) >= offset:
                raise RuntimeError(f'服务器端点未向旧消息推进：m{item.mid}')
            progress.count += 1
            if pending_album and (not item.album_id or item.album_id != pending_album[0].album_id):
                yield combine_server_album(pending_album)
                pending_album = []
            pending_album.append(item)
            if not item.album_id:
                yield combine_server_album(pending_album)
                pending_album = []
        offset = int(batch[-1].mid)


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
    reuse_root: Path | None = None


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


def existing_media_index(root: Path, *, known_text_media: set[str] | None = None) -> dict[str, list[Path]]:
    """按 Telegram 媒体 ID 索引已有文件，避免旧目录名导致重复下载。"""
    result: dict[str, list[Path]] = {}
    for path in root.rglob('*'):
        match = re.match(r'^(?:\d{4}-\d{2}-\d{2}[-_]m|m)(\d+)_[1-9]\d*_(?:(?:document|photo|video|audio|animation|sticker)_)?\d+(?:[_.]|$)', path.name)
        if (match and (path.suffix.lower() != '.txt' or
                       (known_text_media is not None and match.group(1) in known_text_media))
                and path.is_file() and path.stat().st_size > 0
                and not re.search(r'\.part(?:\.\d+)?$', path.name)):
            result.setdefault(match.group(1), []).append(path)
    for mid, paths in result.items():
        if len(paths) > 1:
            numbered = sorted((media_version(path), path) for path in paths)
            if [number for number, _ in numbered] == list(range(1, len(paths) + 1)):
                result[mid] = [numbered[-1][1]]
    return result


def media_file_position(path: Path, mid: str) -> int | None:
    match = re.match(rf'^(?:\d{{4}}-\d{{2}}-\d{{2}}[-_]m|m){re.escape(mid)}_([1-9]\d*)_(?:(?:document|photo|video|audio|animation|sticker)_)?\d+(?:[_.]|$)',
                     path.name)
    return int(match.group(1)) if match else None


def canonical_media_name(task: MediaTask, source: dict) -> str:
    """文档保留 Telegram 原名；照片只保留媒体 ID 和扩展名。"""
    timestamp = next((date for mid, _text, date, _media in task.message.parts
                      if mid == task.media_mid), task.message.timestamp)
    day = date_label(timestamp)
    name = safe_name(source['name'], 180)
    suffix = Path(name).suffix or (mimetypes.guess_extension(
        source.get('mime_type', '').split(';', 1)[0].strip()) or '.bin')
    media_id = safe_name(str(source['id']), 48)
    prefix = f'{day}-m{task.media_mid}_{task.index}_{media_id}'
    if source['kind'] == 'photo':
        return prefix + suffix
    original = source.get('original_name', source['name'])
    if not original or (source.get('original_name') is None and
                        Path(original).stem == f'document_{source["id"]}'):
        return prefix + suffix
    stem = Path(original).stem if Path(original).suffix else original
    return f'{prefix}_{safe_name(stem, 130)}{suffix}'


def verified_group_signature(batch: list[Message],
                             media: dict[str, dict[str, FoundMedia]]) -> str:
    """标记绑定消息元数据和原附件 ID，内容仅参与本机摘要计算。"""
    payload = [(ScanRecord.message_event(item), sorted(
        (entry.media_mid, entry.index, entry.expected_size)
        for entry in media.get(item.mid, {}).values())) for item in batch]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()


def reusable_message_count(item: Message, prior: Message | None,
                           media: dict[str, FoundMedia], folder: Path,
                           record: ScanRecord, existing_index: dict[str, list[Path]],
                           *, check_mark: bool = True) -> int | None:
    """混合编组中按消息跳过已完成附件，未完成消息仍进入下载流程。"""
    if prior != item or not item.content_types:
        return None
    signature = verified_group_signature([item], {item.mid: media})
    marked = record.verified_messages.get(item.mid)
    if check_mark and marked is not None and marked != signature:
        return None
    if any(mid not in record.content_files or
           record.results.get(mid, {}).get('status') not in
           ('downloaded', 'skipped', 'relocated', 'saved_content', 'no_media')
           for mid, *_ in item.parts):
        return None
    expected = {mid for mid, kind in item.content_types if kind in DOWNLOADABLE}
    if expected != set(media) or (expected and item.mid not in record.discovered):
        return None
    for mid in expected:
        files = existing_index.get(mid, [])
        if (len(files) != 1 or files[0].parent != folder
                or media_file_position(files[0], mid) != media[mid].index
                or record.results.get(mid, {}).get('naming_revision') != 3
                or record.results.get(mid, {}).get('status') not in
                ('downloaded', 'skipped', 'relocated')):
            return None
    return len(expected)


def recover_interrupted_result(item: Message, folder: Path, partial_root: Path,
                               peer: str, record: ScanRecord,
                               existing_index: dict[str, list[Path]]) -> None:
    """浏览器关闭后结果未登记时，以完整分片日志核验已入库文件。"""
    for mid, kind in item.content_types:
        result = record.results.get(mid, {})
        if (kind not in DOWNLOADABLE or result.get('status') != 'failed'
                or 'TargetClosedError' not in result.get('detail', '')):
            continue
        found = record.media.get(mid)
        files = existing_index.get(mid, [])
        if (not found or found.source_mid != item.mid or len(files) != 1
                or files[0].parent != folder
                or media_file_position(files[0], mid) != found.index):
            continue
        path = files[0]
        version = media_version(path)
        key = mid if version == 0 else f'{mid}.v{version}'
        journal = partial_root / f'{key}.jsonl'
        if version == 1 and not journal.exists():
            journal = partial_root / f'{mid}.jsonl'
        try:
            size, digest = verified_completed_file(path, journal, peer=peer, mid=mid)
            if found.expected_size and size != found.expected_size:
                raise RuntimeError('已入库文件与附件扫描大小不符')
            header = saved_media_identity(partial_root, mid, version)
            if header is None:
                raise RuntimeError('缺少附件身份记录')
            verified_digest(path, header['identity'], partial_root / f'{mid}.verified.jsonl',
                            sha256_file, known_digest=digest)
        except (OSError, ValueError, RuntimeError) as exc:
            detail(f'中断结果无法仅凭本机记录恢复 m{mid}: {type(exc).__name__}: {exc}')
            continue
        record.record_result(mid, 'skipped', detail=str(path))
        detail(f'已用完整分片记录恢复中断结果 m{mid}')


def bootstrap_completed_media(item: Message, prior: Message | None,
                              record: ScanRecord,
                              existing_index: dict[str, list[Path]]) -> dict[str, FoundMedia] | None:
    """旧相册被拆成单消息时，从成功结果和唯一文件名补齐附件位置。"""
    if ((prior is not None and prior != item) or not item.content_types
            or item.mid in record.discovered):
        return None
    if any(found.source_mid == item.mid for found in record.media.values()):
        return None
    expected = [mid for mid, kind in item.content_types if kind in DOWNLOADABLE]
    if not expected or any(mid not in record.content_files for mid, *_ in item.parts):
        return None
    found: list[FoundMedia] = []
    positions: set[int] = set()
    for mid in expected:
        files = existing_index.get(mid, [])
        if (len(files) != 1 or record.results.get(mid, {}).get('status') not in
                ('downloaded', 'skipped', 'relocated')):
            return None
        position = media_file_position(files[0], mid)
        if position is None or position in positions:
            return None
        positions.add(position)
        found.append(FoundMedia(item.mid, position, mid, None))
    record.record_discovered(item.mid, found)
    return {entry.media_mid: entry for entry in found}


def newest_first_media(item: Message, media: list[FoundMedia]) -> list[FoundMedia]:
    """处理顺序按原消息时间，文件位置编号仍保持历史稳定。"""
    time_by_mid = {mid: (timestamp, int(mid)) for mid, _text, timestamp, _has_media in item.parts}
    return sorted(media, key=lambda found: time_by_mid.get(
        found.media_mid, (item.timestamp, int(found.media_mid))), reverse=True)


def relocate_completed_message(item: Message, prior: Message | None,
                               media: dict[str, FoundMedia], folder: Path,
                               record: ScanRecord,
                               existing_index: dict[str, list[Path]]) -> set[Path]:
    """编组位置变化但附件身份已记录时，只移动唯一的已完成文件。"""
    if prior != item or not item.content_types:
        return set()
    expected = {mid for mid, kind in item.content_types if kind in DOWNLOADABLE}
    if expected != set(media) or (expected and item.mid not in record.discovered):
        return set()
    if any(mid not in record.content_files or
           record.results.get(mid, {}).get('status') not in
           ('downloaded', 'skipped', 'relocated', 'saved_content', 'no_media')
           for mid, *_ in item.parts):
        return set()
    moves: list[tuple[str, Path, Path]] = []
    for mid in expected:
        files = existing_index.get(mid, [])
        if (len(files) != 1 or media_file_position(files[0], mid) != media[mid].index
                or record.results.get(mid, {}).get('status') not in
                ('downloaded', 'skipped', 'relocated')):
            return set()
        source = files[0]
        target = folder / source.name
        if source != target:
            if target.exists():
                return set()
            moves.append((mid, source, target))
    old_folders: set[Path] = set()
    if moves:
        folder.mkdir(parents=True, exist_ok=True)
    for mid, source, target in moves:
        source.rename(target)
        existing_index[mid] = [target]
        record.record_result(mid, 'relocated', detail=str(target),
                             naming_revision=record.results.get(mid, {}).get('naming_revision'))
        old_folders.add(source.parent)
    return old_folders


def reusable_group_count(batch: list[Message], selected: set[str], published: Path,
                         pending: Path, prior_messages: dict[str, Message],
                         prior_media: dict[str, dict[str, FoundMedia]],
                         record: ScanRecord, existing_index: dict[str, list[Path]],
                         *, check_mark: bool = True) -> int | None:
    """仅凭上次完成记录与启动时的文件索引判定整组可复用。"""
    if (selected != {item.mid for item in batch} or not has_group_files(published)
            or has_group_files(pending)):
        return None
    signature = verified_group_signature(batch, prior_media)
    marked = record.verified_groups.get(published.name)
    if check_mark and marked is not None and marked != signature:
        return None
    anchor = batch[0]
    reused = 0
    for item in batch:
        folder = destination(published.parent, anchor, batch, item)
        count = reusable_message_count(item, prior_messages.get(item.mid),
                                       prior_media.get(item.mid, {}), folder,
                                       record, existing_index)
        if count is None:
            return None
        reused += count
    return reused


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


def ensure_publish_association(target: Path, pending: Path, record: ScanRecord,
                               links: dict[str, str]) -> None:
    """发布关联组成员前准备上层引用清单。"""
    if LINKED_NAME.fullmatch(target.parent.name):
        association = pending.parent / '关联清单.json'
        final_association = target.parent / association.name
        if not final_association.exists():
            if association.is_file():
                association.rename(final_association)
            else:
                by_mid = {part[0]: links.get(anchor.mid)
                          for anchor, group in grouped(record.ordered_messages())
                          for item in group for part in item.parts}
                pairs = sorted((mid, str(entry['reply_to']))
                               for mid, entry in record.source_messages.items()
                               if entry.get('reply_to') and
                               by_mid.get(mid) == target.parent.name and
                               by_mid.get(str(entry['reply_to'])) == target.parent.name)
                if not pairs:
                    raise RuntimeError('关联组发布前无有效引用清单，保留暂存数据')
                with final_association.open('x', encoding='utf-8') as stream:
                    json.dump({'replies': [{'source_message_id': a, 'target_message_id': b}
                                           for a, b in pairs]}, stream, ensure_ascii=False, indent=2)


def staged_batch_complete(batch: list[Message], record: ScanRecord,
                          index: dict[str, list[Path]]) -> bool:
    """同时核对原消息结果、内容文件及附件，供暂存发布使用。"""
    sources = [part[0] for item in batch for part in item.parts]
    if any(mid not in record.results or record.results[mid]['status'] == 'failed' or
           (record.source_messages.get(mid, {}).get('content_type') not in (None, 'unclassified') and
            (mid not in record.content_files or not Path(record.content_files[mid]).is_file()))
           for mid in sources):
        return False
    expected = {mid for item in batch for mid, kind in item.content_types if kind in DOWNLOADABLE}
    if any(len(index.get(mid, [])) != 1 or mid not in record.results or
           record.results[mid]['status'] == 'failed' or
           (mid in record.media and record.media[mid].expected_size and
            index[mid][0].stat().st_size != record.media[mid].expected_size)
           for mid in expected):
        return False
    return True


def publish_ready_staged_groups(root: Path, staging_root: Path, record: ScanRecord) -> int:
    """已确认较旧边界的完整编组可在有界续跑中发布。"""
    reconcile_reply_groups(root, record)
    reconcile_reply_groups(staging_root, record)
    links = linked_group_names(record)
    known_text_media = set(record.media)
    index = existing_media_index(staging_root, known_text_media=known_text_media)
    dependency_index = existing_media_index(root, known_text_media=known_text_media)
    for mid, files in index.items():
        dependency_index.setdefault(mid, []).extend(files)
    published = 0
    for position, (anchor, batch) in enumerate(grouped(record.ordered_messages())):
        if position == 0 and not record.history_complete:
            continue
        if pending_reply_target(batch, record, dependency_index, root):
            continue
        output_parent = parent_for(root, anchor, links)
        staging_parent = parent_for(staging_root, anchor, links)
        prior_root = (output_parent / group_folder(anchor)).is_dir() if anchor else False
        prior_staging = (staging_parent / group_folder(anchor)).is_dir() if anchor else False
        prepare_group_folder(output_parent, anchor)
        prepare_group_folder(staging_parent, anchor)
        moved = 0
        if anchor:
            moved += prepare_item_folders(output_parent / group_folder(anchor), batch)
            moved += prepare_item_folders(staging_parent / group_folder(anchor), batch)
        changed = bool(anchor and (moved or
                       (not prior_root and (output_parent / group_folder(anchor)).is_dir()) or
                       (not prior_staging and (staging_parent / group_folder(anchor)).is_dir())))
        if changed:
            index = existing_media_index(staging_root, known_text_media=known_text_media)
        name = group_folder(anchor) if anchor else '未归组'
        pending = staging_parent / name
        if not has_group_files(pending):
            continue
        if not staged_batch_complete(batch, record, index):
            continue
        prepare_group_folder(staging_parent, anchor, create=True)
        target = output_parent / name
        if has_group_files(target):
            raise RuntimeError(f'编组发布目标已存在：{target}')
        target.parent.mkdir(parents=True, exist_ok=True)
        ensure_publish_association(target, pending, record, links)
        move_files_only(pending, target)
        _record_moved_paths(record, pending, target)
        archive_empty_relocated_folders(root, {pending})
        published += 1
        detail(f'编组文件已核对并移入成品目录：{name}')
    if published:
        reconcile_reply_groups(root, record)
    return published


def archive_empty_relocated_folders(root: Path, source_folders: set[Path],
                                    staging_root: Path | None = None) -> None:
    """逐文件发布后，将已确认空的来源目录放入 ~archive。"""
    paths = load_paths()
    archive_root = (paths['output'].parent / '~local' / '~archive'
                    if root.is_relative_to(paths['output']) else root.parent / '~archive')
    total = 0
    boundaries = (paths['output'], paths['staging'], paths['output'].parent / '~local')
    for source in sorted(source_folders, key=lambda path: len(path.parts), reverse=True):
        if not source.is_dir():
            continue
        boundary = next((base for base in boundaries if source.is_relative_to(base)), root)
        while source.is_dir():
            if source == boundary and any(source.iterdir()):
                break
            parent = source.parent
            moved, _ = archive_empty_folders(source, archive_root=archive_root, include_root=True)
            total += moved
            if source.exists() or source == boundary:
                break
            source = parent
    if total:
        detail(f'已将 {total} 个空来源目录放入 {archive_root}')


async def wait_for_login(page: Page, *, timeout: int = 300_000) -> None:
    """等到已登录账号的真实会话入口可见；页面骨架不能算登录成功。"""
    chat = page.locator('#column-left a[href^="#"]:visible, '
                        '#column-center .bubble[data-mid][data-peer-id]:visible').first
    try:
        await chat.wait_for(state='visible', timeout=min(12_000, timeout))
    except BrowserTimeout:
        status = await page.evaluate('''() => ({
            ready: document.readyState,
            chatLinks: [...document.querySelectorAll('#column-left a[href^="#"]')]
                .filter(node => node.getClientRects().length > 0).length,
            bubbles: document.querySelectorAll('#column-center .bubble[data-mid][data-peer-id]').length
        })''')
        detail(f'WEB_READY_WAIT ready={status["ready"]} chat_links={status["chatLinks"]} '
               f'bubbles={status["bubbles"]}')
        print(f'Telegram 会话入口尚未就绪，继续等待最多 {timeout // 1000} 秒。', flush=True)
        await chat.wait_for(state='visible', timeout=timeout)


async def open_group_from_chatlist(page: Page, expected: str) -> None:
    """深链接未装载聊天时，从已登录会话的聊天列表真实点击目标群。"""
    await page.goto('https://web.telegram.org/k/', wait_until='domcontentloaded')
    await wait_for_login(page)
    target = page.locator(f'#column-left a[href="#{expected}"]:visible').first
    if not await target.count():
        try:
            title = await asyncio.wait_for(page.evaluate('''async peerId => {
            const managers = window.appDownloadManager?.managers;
            if (!managers) return '';
            try {
                const chat = await managers.appChatsManager.getChat(Math.abs(Number(peerId)));
                if (typeof chat?.title === 'string') return chat.title;
            } catch (_) {}
            try {
                const peer = await managers.appPeersManager.getPeer(Number(peerId));
                if (typeof peer?.title === 'string') return peer.title;
            } catch (_) {}
            return '';
        }''', expected), timeout=15)
        except TimeoutError as exc:
            raise RuntimeError(f'群组 {expected} 的聊天名称读取超时') from exc
        if not title:
            raise RuntimeError(f'群组 {expected} 无法从已登录会话定位聊天名称')
        await page.locator('.input-search-input').first.fill(title)
    await target.wait_for(state='visible', timeout=20_000)
    await target.click()
    await page.locator(f'{BUBBLE}[data-peer-id="{expected}"]').first.wait_for(
        state='attached', timeout=45_000)


async def open_group(page: Page, url: str, *, require_message: bool = True) -> str:
    expected = group_id(url)
    current = urlsplit(page.url)
    on_target = (current.fragment == expected or current.fragment.startswith(expected + '_'))
    already_visible = on_target and await page.locator(
        f'{BUBBLE}[data-peer-id="{expected}"]').count() > 0
    if not already_visible:
        if current.hostname == 'web.telegram.org' and not on_target:
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
            await page.locator(f'{BUBBLE}[data-peer-id="{expected}"]').first.wait_for(
                state='attached', timeout=12_000)
        except BrowserTimeout:
            await open_group_from_chatlist(page, expected)
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
        const ordered = [...originals].sort((left, right) => Number(left.mid) - Number(right.mid));
        const chronological = ordered.every((message, index) => !index ||
            Number(message.date) >= Number(ordered[index - 1].date));
        const hasMetadata = originals.length === seen.size &&
            originals.every(message => typeof message.message === 'string' && Number(message.date) > 0) &&
            chronological;
        const captions = [...new Set(originals.map(message => message.message?.trim()).filter(Boolean))];
        const parts = originals.map(message => [String(message.mid), message.message || '',
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
        const replies = originals.filter(message => message.reply_to?.reply_to_msg_id &&
            !message.reply_to.reply_to_peer_id)
            .map(message => [String(message.mid),
                String(Number(message.reply_to.reply_to_msg_id) >= 4294967296
                    ? Number(message.reply_to.reply_to_msg_id)
                    : 4294967296 + Number(message.reply_to.reply_to_msg_id))]);
        return {
            mid: b.dataset.mid || '',
            text: hasMetadata ? captions.join('\n') : (own?.innerText || '').trim(),
            timestamp: Number(primary?.date || 0),
            mediaCount: media.length || (b.querySelector('.attachment') ? 1 : 0),
            hasMetadata,
            chronological,
            parts,
            types,
            details,
            replies,
            albumId: String(primary?.grouped_id || ''),
            sender: JSON.stringify(primary?.from_id || primary?.peer_id || {}),
            outgoing: !!(primary?.pFlags?.out || primary?.out),
            postAuthor: primary?.post_author || '',
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
        if incomplete[0]['chronological'] is False:
            raise RuntimeError(f"消息 {incomplete[0]['mid']} 的原消息时间顺序异常，停止归组以免时间错误")
        raise RuntimeError(f"消息 {incomplete[0]['mid']} 缺少 Telegram 原消息元数据，停止归组以免时间或正文错误")
    return [Message(str(row['mid']), row['text'], int(row['timestamp']), int(row['mediaCount']),
                    tuple((str(mid), text, int(date), bool(media)) for mid, text, date, media in row['parts']),
                    tuple((str(mid), classify(str(media_type), tuple(flags)))
                          for mid, media_type, flags in row['types']),
                    tuple((str(mid), str(detail)) for mid, detail in row['details']),
                    tuple((str(mid), str(target)) for mid, target in row['replies']),
                    str(row['albumId']), str(row['sender']), bool(row['outgoing']),
                    str(row['postAuthor']))
            for row in rows if row['mid'] and row['timestamp']]


async def iter_history(page: Page, limit: int | None, known_ids: set[str],
                       progress: ScanProgress, *, start_at_latest: bool = True,
                       pause_at_oldest: bool = False,
                       peer_id: int | None = None,
                       api_scan: bool = False) -> AsyncIterator[Message]:
    """逐条提供最新到最旧的消息，供下载阶段在每个归组边界间插入。"""
    known_ids = known_ids or set()
    if api_scan and peer_id is not None:
        count = 0
        last_day = None
        async for item in iter_history_from_endpoint(page, peer_id, 0, progress):
            if item.mid in known_ids:
                progress.stopped_on_known = True
                return
            day = date_label(item.timestamp)
            if day != last_day:
                print(f'[扫描] 已到 {day} · 已读取 {progress.count} 条', flush=True)
                last_day = day
            yield item
            count += 1
            if limit and count >= limit:
                return
        return
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
    async def recover_stalled_history(state: dict) -> bool:
        nonlocal server_target, recovery_mid, stale
        if stale == 10 and peer_id is not None and oldest_mid is not None:
            server_target = await server_history_with_recovery(
                page, peer_id, oldest_mid, server_older_route_id)
            if server_target is None:
                detail(f'服务器确认 m{oldest_mid} 之前无可访问消息')
                return True
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
            return False
        if recovery_mid is not None and oldest_mid is not None and oldest_mid >= recovery_mid and stale >= 15:
            if pause_at_oldest:
                print('[人工检查] Chrome 已暂停在当前最早可见日期。'
                      '请检查页面，检查完在终端按 Enter 继续。', flush=True)
                await asyncio.to_thread(input)
            raise RuntimeError(f'重新定位后仍未发现 m{recovery_mid} 之前的消息；不能确认历史最早端')
        # 页面初始位置或失效的滚动容器也会一直报告 top=True；必须先看到旧消息推进。
        if (peer_id is None and state['top'] and not state['loading'] and stale >= 30
                and advanced_to_older):
            return True
        if stale >= 60:
            raise RuntimeError('无法确认已扫描到历史顶部；保留已发现消息但停止下载以免遗漏')
        return False

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
        if await recover_stalled_history(state):
            break
    progress.reached_oldest = True
    print(f'[扫描] 已到可访问历史最早日期 {date_label(earliest_timestamp)}', flush=True)


def message_bubble_selector(source_url: str, mid: str) -> str:
    """相册成员的原消息号也能定位到所属气泡，且必须属于目标群。"""
    number = str(int(mid))
    return (f'{BUBBLE}[data-peer-id="{group_id(source_url)}"]:is('
            f'[data-mid="{number}"],:has(.album-item[data-mid="{number}"]),'
            f':has(.grouped-item[data-mid="{number}"]))')


async def open_message_with_native_ui(page: Page, source_url: str, mid: str) -> bool:
    """原生跳转后核对目标群和消息，地址变化本身不能证明已定位。"""
    # Telegram Web K AppImManager.setInnerPeer，2026-11-10 验证：
    # https://github.com/morethanwords/tweb/blob/master/src/lib/appImManager.ts
    try:
        called = await asyncio.wait_for(page.evaluate('''async ({peerId, mid}) => {
            const im = window.appImManager;
            if (typeof im?.setInnerPeer !== 'function') return false;
            await im.setInnerPeer({peerId: Number(peerId), lastMsgId: Number(mid)});
            return true;
        }''', {'peerId': group_id(source_url), 'mid': mid}), timeout=12)
        if called is not True:
            return False
        await page.locator(message_bubble_selector(source_url, mid)).first.wait_for(
            state='attached', timeout=8_000)
        return True
    except Exception as exc:
        detail(f'原生消息定位未完成 m{mid}: {type(exc).__name__}')
        return False


async def locate_message(page: Page, source_url: str, item: Message):
    """先使用原生界面定位旧消息；不可用时保留地址和可见历史回找。"""
    selector = message_bubble_selector(source_url, item.mid)
    bubble = page.locator(selector).first
    if not await bubble.count():
        if await open_message_with_native_ui(page, source_url, item.mid):
            return bubble
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
        const item = node.closest('.album-item[data-mid], .grouped-item[data-mid]') ||
            node.closest('.bubble[data-mid]');
        const peer = item?.dataset.peerId, mid = requestedMid || item?.dataset.mid;
        const message = window.mtprotoMessagePort?.getMessageByPeer(peer, Number(mid));
        const media = message?.media?.document || message?.media?.photo;
        const fileManager = window.appDownloadManager?.managers?.apiFileManager;
        if (!media || typeof fileManager?.requestFilePart !== 'function') return null;
        const photoSize = media._ === 'photo' ? media.sizes?.at(-1) : null;
        const size = media._ === 'document' ? media.size :
            (photoSize?.size || Math.max(0, ...(photoSize?.sizes || [])));
        const nameAttr = media._ === 'document' ?
            media.attributes?.find(attr => attr._ === 'documentAttributeFilename') : null;
        const originalName = media._ === 'document' ?
            (media.file_name || nameAttr?.file_name || '') : '';
        const name = media._ === 'document' ? (originalName || `document_${media.id}`) :
            `photo_${media.id}_${photoSize?.type || ''}.jpg`;
        return {peer, mid, kind: media._, id: String(media.id),
            dc: media.dc_id, size: Number(size), thumb: photoSize?.type || '',
            mime_type: media.mime_type || '', name, original_name: originalName};
    }''', media_mid)
    if not source or source['kind'] not in ('document', 'photo'):
        raise RuntimeError('Telegram Web K 分片接口不可用或媒体类型不支持续传')
    if not source['peer'] or not source['mid'] or not source['id'] or source['size'] <= 0:
        raise RuntimeError('附件缺少分片身份或准确大小，不能安全续传')
    return source


async def fetch_resume_part(page: Page, source: dict, offset: int, limit: int) -> bytes:
    """外层限时后仍接收浏览器调用的终态，避免取消遗留未回收的驱动异常。"""
    pending = asyncio.create_task(_part_read_outcome(page, source, offset, limit))
    _PENDING_PART_READS.add(pending)
    pending.add_done_callback(_consume_finished_part)
    data, error = await asyncio.shield(pending)
    if error is not None:
        remote = re.search(r'TELEGG_REMOTE_FILE_ERROR:(5\d\d):([A-Z0-9_]+)', str(error))
        if remote:
            raise RemoteFileError(int(remote[1]), remote[2]) from error
        raise error
    return data


async def _part_read_outcome(page: Page, source: dict, offset: int,
                             limit: int) -> tuple[bytes, Exception | None]:
    """接管迟到错误；遵循 Python 3.14 shield 的任务引用与取消语义。

    https://docs.python.org/3.14/library/asyncio-task.html#shielding-from-cancellation
    """
    try:
        return await _fetch_resume_part(page, source, offset, limit), None
    except Exception as error:
        return b'', error


def _consume_finished_part(task: asyncio.Task) -> None:
    """超时调用可能在页面关闭时才失败；只回收其结果，不提交迟到分片。"""
    _PENDING_PART_READS.discard(task)
    if not task.cancelled():
        task.exception()


async def _fetch_resume_part(page: Page, source: dict, offset: int, limit: int) -> bytes:
    """在已登录 Web K 中按偏移取一块，不把 access_hash 写入磁盘。"""
    # Web K ApiManager 的 noRetry 将 5xx 交给本项目的有界分片重试，避免内部无限重试。
    # 参考 tweb ApiManager（2026-11-09）：
    # https://github.com/morethanwords/tweb/blob/master/src/lib/appManagers/apiManager.ts
    if source['kind'] == 'photo' and source['size'] <= 1024 * 1024:
        encoded = await page.evaluate('''async ({source, offset, limit}) => {
            const message = window.__teleggServerMedia?.get(String(source.mid)) ||
                window.mtprotoMessagePort?.getMessageByPeer(source.peer, Number(source.mid));
            const photo = message?.media?.photo;
            if (!photo || String(photo.id) !== source.id || Number(photo.dc_id) !== source.dc)
                throw new Error('照片身份在下载期间变化');
            const thumb = photo.sizes?.find(size => size.type === source.thumb);
            const currentSize = Number(thumb?.size || Math.max(0, ...(thumb?.sizes || [])));
            if (!thumb || currentSize !== source.size)
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
                    {dcId: photo.dc_id, fileDownload: true, floodMaxTimeout: 10,
                     noRetry: true, noErrorBox: true}));
                if (part?._ !== 'upload.file' || !(part.bytes instanceof Uint8Array))
                    throw new Error('Telegram 未返回普通照片分片');
                bytes = part.bytes;
            } catch (error) {
                if (Number(error?.code) >= 500 && Number(error?.code) < 600)
                    throw new Error(`TELEGG_REMOTE_FILE_ERROR:${error.code}:${/^[A-Z0-9_]+$/.test(error.type || '') ? error.type : 'UNKNOWN'}`);
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
        const message = window.__teleggServerMedia?.get(String(source.mid)) ||
            window.mtprotoMessagePort?.getMessageByPeer(source.peer, Number(source.mid));
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
                {dcId: media.dc_id, fileDownload: true, floodMaxTimeout: 10,
                 noRetry: true, noErrorBox: true}));
        } catch (error) {
            if (Number(error?.code) >= 500 && Number(error?.code) < 600)
                throw new Error(`TELEGG_REMOTE_FILE_ERROR:${error.code}:${/^[A-Z0-9_]+$/.test(error.type || '') ? error.type : 'UNKNOWN'}`);
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


def matching_existing_media(task: MediaTask, source: dict, canonical: Path,
                            existing_index: dict[str, list[Path]]) -> tuple[list[Path], bool]:
    """按媒体身份与附件位置筛选旧文件，识别独立复用区来源。"""
    candidates = [path for path in existing_index.get(task.media_mid, []) if path.is_file()]
    if (source['kind'] == 'document' and canonical.suffix.lower() == '.txt'
            and canonical.is_file() and canonical not in candidates):
        # 普通消息文字同样是 TXT；只在服务器给出的完整媒体文件名完全匹配时复用。
        candidates.append(canonical)
    if task.reuse_root and candidates:
        for path in candidates:
            if not path.is_relative_to(task.reuse_root):
                continue
            match = re.match(r'^(?:\d{4}-\d{2}-\d{2}[-_]m|m)\d+_[1-9]\d*_(?:(?:document|photo|video|audio|animation|sticker)_)?(\d+)(?:[_.]|$)',
                             path.name)
            if not match or match.group(1) != str(source['id']):
                raise RuntimeError(f'复用区媒体 ID 与服务器不符，未移动：m{task.media_mid}')
    from_reuse = bool(task.reuse_root and len(candidates) == 1 and
                      candidates[0].is_relative_to(task.reuse_root))
    existing = [path for path in candidates
                if media_file_position(path, task.media_mid) == task.index or from_reuse]
    if not existing and len(candidates) == 1:
        # 旧相册曾按单条消息保存为位置 1；媒体 ID 与服务器一致时可安全改名。
        marker = re.match(r'^(?:\d{4}-\d{2}-\d{2}_)?m\d+_\d+_(\d+)(?:_|\.)',
                          candidates[0].name)
        if marker and marker.group(1) == str(source['id']):
            existing = candidates
    if candidates and not existing:
        raise RuntimeError(f'同一消息的附件位置变化，无法确认版本：m{task.media_mid} 位置 {task.index}')
    return existing, from_reuse


async def save_one(page: Page, target, task: MediaTask,
                   existing_index: dict[str, list[Path]], *, exact_verify: bool = False,
                   server_source: dict | None = None
                   ) -> tuple[str, Path, int, str, Path | None]:
    source = server_source if server_source is not None else await resume_source(target, task.media_mid)
    if source['mid'] != task.media_mid:
        raise RuntimeError(f'附件位置变化：预期 m{task.media_mid}，实际 m{source["mid"]}')
    size_changed = bool(task.expected_size and source['size'] != task.expected_size)
    partial_root = task.partial_root or load_paths()['partials']
    identity = {key: source[key] for key in ('peer', 'mid', 'kind', 'id', 'dc', 'thumb')}
    manifest = partial_root / f'{task.media_mid}.verified.jsonl'
    canonical = task.folder / canonical_media_name(task, source)
    existing, from_reuse = matching_existing_media(task, source, canonical, existing_index)
    version = 0
    async def resolve_existing_version():
        if len(existing) != 1:
            raise RuntimeError(f'同一媒体存在多个本地文件，无法确认：m{task.media_mid}')
        path = existing[0]
        size = path.stat().st_size
        prior_version = media_version(path)
        expected = numbered_media_path(canonical, prior_version) if prior_version else canonical
        if exact_verify and not from_reuse:
            if path != expected:
                raise LocalDataError(f'已有媒体路径不符合规范：{path}；预期：{expected}')
            read_snapshot(manifest, strict=True, required=True)
        # 旧复用区只提供已下载的媒体文件；不读取或搬运旧断点与状态。
        prior_roots = [] if from_reuse else [partial_root]
        prior = next((found for root_candidate in prior_roots
                      if (found := saved_media_identity(root_candidate, task.media_mid,
                                                        prior_version)) is not None), None)
        changed = size != source['size'] or (prior_version > 0 and prior is None) or (prior is not None and
                  (prior['identity'] != identity or prior['size'] != source['size']))
        if exact_verify and not from_reuse and changed:
            raise LocalDataError(f'已有媒体大小或身份与服务器不符：m{task.media_mid}；{path}')
        if not changed:
            if size_changed:
                raise RuntimeError(f'附件大小与扫描记录不符：m{task.media_mid}')
            if exact_verify or from_reuse:
                print(f'  [复核] {media_label(task.message, task.media_mid, task.position, task.total_files)}'
                      ' · 读取本地文件并核验 SHA-256', flush=True)
            digest = await asyncio.to_thread(verified_digest, path, identity, manifest,
                                             sha256_file, exact=exact_verify or from_reuse)
            old_folder = None
            moved = numbered_media_path(canonical, prior_version) if prior_version else canonical
            if path != moved:
                if moved.exists():
                    raise RuntimeError(f'迁移目标已存在，未覆盖：{moved}')
                task.folder.mkdir(parents=True, exist_ok=True)
                old_folder = path.parent
                path.rename(moved)
                existing_index[task.media_mid] = [moved]
                path = moved
                await asyncio.to_thread(verified_digest, path, identity, manifest,
                                        sha256_file, known_digest=digest)
            existing_index[task.media_mid] = [path]
            return (('relocated' if old_folder else 'skipped', path, size, digest, old_folder), 0)
        if prior_version == 0:
            first = numbered_media_path(path, 1)
            if first.exists():
                raise RuntimeError(f'附件初版目标已存在，未覆盖：{first}')
            path.rename(first)
            existing_index[task.media_mid] = [first]
        return None, max(2, prior_version + 1)

    if existing:
        reused_result, version = await resolve_existing_version()
        if reused_result is not None:
            return reused_result
    elif size_changed:
        raise RuntimeError(f'附件大小变化但未找到旧文件：m{task.media_mid}')
    final = canonical
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
    display = ProgressLine()
    stamp = media_timestamp(task.message, task.media_mid)
    label = media_label(task.message, task.media_mid, task.position, task.total_files)
    first_committed = False

    def show_progress(done: int, total: int) -> None:
        nonlocal last_render, last_sample, first_committed
        if not first_committed and done > resumed:
            first_committed = True
            display.finish()
            action = '续传已开始' if resumed else '新资源已开始'
            print(f'  [下载] {action} · {label}', flush=True)
            detail(f'{action}，首个分片已提交 m{task.media_mid}: {done}/{total}')
        now = time.monotonic()
        elapsed = now - last_sample[0]
        speed = (done - last_sample[1]) / elapsed if elapsed > 0 else None
        if now - last_render >= 0.5 or done == total:
            line = file_line(task.media_mid, task.position, task.total_files,
                             done, total, speed, timestamp=stamp)
            display.write(line)
            last_render = now
            last_sample = (now, done)

    try:
        resumed = partial.open()
        last_sample = (time.monotonic(), resumed)
        print(f'  [传输] {label}', flush=True)
        print(f'    [本文件] 大小 {size_label(source["size"])} · 已保存 {size_label(resumed)}', flush=True)
        if resumed:
            print(f'    [续传] 从已保存进度继续 · {resumed}/{source["size"]} 字节', flush=True)
            detail(f'续传 m{task.media_mid}: {resumed}/{source["size"]} 字节')
        await download_parts(partial,
                             lambda offset, limit: fetch_resume_part(page, source, offset, limit),
                             show_progress)
    finally:
        display.finish()
    if final.exists():
        raise RuntimeError(f'目标文件下载期间已出现，未覆盖：{final}')
    print(f'  [校验] {label} · SHA-256', flush=True)
    size, saved_hash = await asyncio.to_thread(partial.finalize, final)
    await asyncio.to_thread(verified_digest, final, identity, manifest,
                            sha256_file, known_digest=saved_hash)
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
    # 文件位置取原消息顺序，不能取 Web K 的可变 DOM 顺序。
    positions = {mid: position for position, (mid, *_rest) in enumerate(
        (part for part in item.parts if part[0] in expected), 1)}
    for index in range(count):
        media_mid, expected_size = await task_metadata(targets.nth(index))
        if media_mid not in expected:
            continue
        if media_mid not in seen:
            seen.add(media_mid)
            found.append(FoundMedia(item.mid, positions[media_mid], media_mid, expected_size))
    actual = {entry.media_mid for entry in found}
    if actual - expected:
        raise RuntimeError(f'消息 m{item.mid} 附件 ID 不齐：原消息 {sorted(expected)}，页面可操作 {sorted(actual)}')
    # Web K 有时只渲染相册的一部分；未渲染项仍可通过原消息 ID 从页面缓存读取。
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
        found.append(FoundMedia(item.mid, positions[mid], mid, source['size']))
        actual.add(mid)
    if actual != expected:
        raise RuntimeError(f'消息 m{item.mid} 附件 ID 不齐：原消息 {sorted(expected)}，页面可操作 {sorted(actual)}')
    return found


async def discover_media_server(page: Page, peer_id: int, item: Message) -> list[FoundMedia]:
    """端点续跑时按服务器原消息 ID 识别附件，不回滚网页虚拟列表。"""
    validate_types(item.parts, item.content_types)
    downloadable = {mid for mid, kind in item.content_types if kind in DOWNLOADABLE}
    positions = {mid: index for index, (mid, *_rest) in enumerate(
        (part for part in item.parts if part[0] in downloadable), 1)}
    result = []
    for mid in positions:
        source = await server_media_source(page, peer_id, mid)
        result.append(FoundMedia(item.mid, positions[mid], mid, source['size']))
        await page.evaluate('mid => window.__teleggServerMedia?.delete(String(mid))', mid)
    if set(positions) != downloadable:
        raise RuntimeError(f'服务器附件 ID 不齐：m{item.mid}')
    return result


async def save_media(page: Page, url: str, task: MediaTask,
                     existing_index: dict[str, list[Path]], *, exact_verify: bool = False
                     ) -> tuple[str, Path | None]:
    """定位当前附件并核对身份，再决定复用或生成新版本。"""
    bubble = await locate_message(page, url, task.message)
    targets = await media_targets(bubble)
    target = None
    for index in range(await targets.count()):
        candidate = targets.nth(index)
        current_mid, _ = await task_metadata(candidate)
        if current_mid == task.media_mid:
            target = candidate
            break
    if target is None:
        # 缺席的相册项可由当前泡泡中的原消息缓存读取。
        target = bubble
    state, path, _size, _digest, old_folder = await save_one(
        page, target, task, existing_index, exact_verify=exact_verify)
    return state, old_folder


async def save_media_server(page: Page, url: str, task: MediaTask,
                            existing_index: dict[str, list[Path]],
                            *, exact_verify: bool = False) -> tuple[str, Path | None]:
    source = await server_media_source(page, int(group_id(url)), task.media_mid)
    try:
        state, _path, _size, _digest, old_folder = await save_one(
            page, None, task, existing_index, exact_verify=exact_verify,
            server_source=source)
        return state, old_folder
    finally:
        if not page.is_closed():
            with suppress(Exception):
                await page.evaluate('mid => window.__teleggServerMedia?.delete(String(mid))',
                                    task.media_mid)


def is_transport_timeout(error: Exception) -> bool:
    """仅识别分片传输耗尽重试的超时，避免重试身份或文件校验错误。"""
    detail = str(error).lower()
    return (not isinstance(error, LocalDataError)
            and '分片偏移' in detail and '连续失败' in detail and 'timeout' in detail)


async def save_media_with_recovery(scan_page: Page, download_page: Page, url: str,
                                   task: MediaTask, existing_index: dict[str, list[Path]],
                                   *, exact_verify: bool = False,
                                   server_mode: bool = False
                                   ) -> tuple[str, Path | None, Page]:
    """连接持续超时时重建下载页；同一分片由 PartialFile 核验后续传。"""
    try:
        operation = save_media_server if server_mode else save_media
        state, old_folder = await operation(download_page, url, task, existing_index,
                                            exact_verify=exact_verify)
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
            state, old_folder = await operation(replacement, url, task, existing_index,
                                                exact_verify=exact_verify)
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
                         full_scan: bool = False, pause_at_oldest: bool = False,
                         exact_verify: bool = False,
                         coarse_skip: bool = False,
                         rebuild_local: bool = False) -> tuple[int, int, int, int]:
    """单页流式处理：完整确认下一组后下载上一组，最多领先一组。"""
    if rebuild_local:
        raise ValueError('--rebuild-local 已停用；只允许从 ~local 逐个复用媒体文件')
    title = await open_group(page, url)
    if on_group_open:
        on_group_open(title)
    root = group_root(output, title, group_id(url))
    record = ScanRecord(SCAN_RECORD_ROOT / f'{group_id(url)}.jsonl')
    paths = load_paths()
    staging_root = ((paths['staging'] if output.resolve() == paths['output'] else
                     output.resolve().parent / f'.{output.resolve().name}.telegg-staging') / root.name)
    reuse_group = reuse_root(output, group_id(url))
    partial_base = (paths['partials'] if output.resolve() == paths['output'] else
                    output.resolve().parent / f'.{output.resolve().name}.telegg-partials')
    partial_root = partial_base / group_id(url)
    await asyncio.to_thread(validate_local_archive, root, staging_root, record, partial_root,
                            enabled=exact_verify)
    if reuse_group.is_dir():
        print('[复用] 正在核验旧文件与完整分片记录', flush=True)
        rekeyed = await asyncio.to_thread(rekey_legacy_media, reuse_group,
                                          reuse_group.parent, group_id(url))
        if rekeyed:
            print(f'[复用] {rekeyed} 个旧文件已逐块核验，等待服务器媒体 ID 再确认', flush=True)
    def current_media_index() -> dict[str, list[Path]]:
        known_text_media = set(record.media)
        index = existing_media_index(root, known_text_media=known_text_media)
        for mid, files in existing_media_index(staging_root, known_text_media=known_text_media).items():
            index.setdefault(mid, []).extend(files)
        for mid, files in existing_media_index(reuse_group).items():
            index.setdefault(mid, files)
        return index

    prior_messages = record.ordered_messages()
    prior_by_mid = {item.mid: item for item in prior_messages}
    prior_media: dict[str, dict[str, FoundMedia]] = {}
    for found in record.media.values():
        prior_media.setdefault(found.source_mid, {})[found.media_mid] = found
    incremental = (record.history_complete and limit is None and not full_scan
                   and all(item.parts for item in record.messages.values()))
    known_ids = set(record.messages) if incremental else set()
    if full_scan and not dry_run:
        record.append([{'kind': 'reset'}])
    if not dry_run:
        reconcile_reply_groups(root, record)
        reconcile_reply_groups(staging_root, record)
    existing_index = {} if dry_run else current_media_index()
    if not dry_run and demote_unready_replies(root, staging_root, record, existing_index):
        existing_index = current_media_index()
    server_mode = False
    api_media = reuse_group.is_dir()
    prefix_count = prefix_files = 0
    endpoint = resume_endpoint(record, existing_index) if coarse_skip and not dry_run and not full_scan else None
    async def read_resume_prefix():
        try:
            prefix = await verify_resume_prefix(page, int(group_id(url)), endpoint,
                                                record, existing_index, limit)
        except Exception as exc:
            detail(f'快速端点核对失败，回退顺序扫描：{type(exc).__name__}: {exc}')
            prefix = None
            if not page.is_closed():
                await open_group(page, url)
        return prefix

    if endpoint is not None:
        prefix = await read_resume_prefix()
        if prefix is not None:
            reconcile_reply_groups(root, record)
            reconcile_reply_groups(staging_root, record)
            existing_index = current_media_index()
            endpoint, prefix_count, prefix_files = prefix
            server_mode = True
            marker = (record.source_messages.get(str(endpoint)) or
                      record.source_messages.get(str(endpoint + 1)))
            date = date_label(marker['timestamp']) if marker else '已知最旧端之后'
            outcome = ('本次限额内均已完成' if limit is not None and prefix_count >= limit
                       else f'从 {date} 继续')
            print(f'[快速模式] 已核对并跳过 {prefix_count} 条、{prefix_files} 个文件；'
                  f'{outcome}', flush=True)
            detail(f'快速端点 m{endpoint}，服务器核对前缀 {prefix_count} 条')
            if limit is not None and prefix_count >= limit:
                demote_unready_replies(root, staging_root, record, current_media_index())
                publish_ready_staged_groups(root, staging_root, record)
                archive_empty_relocated_folders(root, getattr(record, 'relocated_folders', set()), staging_root)
                print(f'[核验] 已选 {prefix_count} 条原消息均已处理', flush=True)
                return prefix_count, 0, prefix_files, 0
        else:
            print('[快速模式] 端点未确认，按时间顺序扫描', flush=True)
    progress = ScanProgress()
    scanned_desc: list[Message] = []
    wanted: set[str] = set()
    wanted_sources = 0
    handled: set[str] = set()
    seen_media: set[str] = set()
    relocated_sources: set[Path] = set()
    downloaded = failed = 0
    reused = prefix_files
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
        if not dry_run:
            moved = reconcile_reply_groups(root, record) + reconcile_reply_groups(staging_root, record)
            if moved:
                existing_index = current_media_index()
        links = linked_group_names(record)
        anchor = batch[0]
        batch_name = group_folder(anchor)
        output_parent = parent_for(root, anchor, links)
        staging_parent = parent_for(staging_root, anchor, links)
        published = output_parent / batch_name
        pending = staging_parent / batch_name
        if coarse_skip and not dry_run and not pending_reply_target(batch, record, existing_index, root):
            count = reusable_group_count(batch, selected, published, pending,
                                         prior_by_mid, prior_media, record, existing_index)
            if count is not None:
                handled.update(item.mid for item in batch)
                reused += count
                last_processed_date = date_label(min(item.timestamp for item in items))
                record.mark_group_verified(batch_name, verified_group_signature(batch, prior_media))
                if not server_mode:
                    print(f'[快速跳过] {last_processed_date} · 已完成编组，复用 {count} 个文件', flush=True)
                return
        def prepare_work_root() -> Path:
            nonlocal existing_index
            if not dry_run:
                prior_root = has_group_files(published)
                prior_staging = has_group_files(pending)
                prepare_group_folder(output_parent, anchor)
                prepare_group_folder(staging_parent, anchor)
                moved = (prepare_item_folders(published, batch) +
                         prepare_item_folders(pending, batch))
                if moved or (not prior_root and has_group_files(published)) or (not prior_staging and has_group_files(pending)):
                    existing_index = current_media_index()
            if not dry_run and has_group_files(published):
                expected = {mid for item in batch for mid, _text, _date, has_media in item.parts if has_media}
                if any(not existing_index.get(mid) for mid in expected):
                    if has_group_files(pending):
                        raise RuntimeError(f'编组同时存在于下载中和已完成目录：{batch_name}')
                    pending.parent.mkdir(parents=True, exist_ok=True)
                    move_files_only(published, pending)
                    _record_moved_paths(record, published, pending)
                    archive_empty_relocated_folders(root, {published})
                    existing_index = current_media_index()
            return output_parent if has_group_files(published) else staging_parent

        work_root = prepare_work_root()
        total_files = sum(sum(kind in DOWNLOADABLE for _mid, kind in item.content_types)
                          if item.content_types else item.media_count for item in items)
        done_files = 0
        current_date = date_label(min(item.timestamp for item in items))
        last_processed_date = current_date
        if not server_mode:
            print(f'[{"预览" if dry_run else "下载"}] 正在处理 {current_date} · '
                  f'本段本次选中 {message_count(items)} 条原消息、'
                  f'{total_files} 个文件', flush=True)
        detail(f'编组 {processed_groups + 1}/{confirmed_groups} 日期 {current_date}')
        if not dry_run and not server_mode:
            print(group_line(0, total_files), flush=True)
        anchor = batch[0]
        if not dry_run:
            prepare_group_folder(work_root, anchor, create=True)
            save_group_manifest(work_root / group_folder(anchor), batch)
        def persist_message_content(item: Message, folder: Path) -> bool:
            validate_types(item.parts, item.content_types)
            for part in item.parts:
                if not item.content_types:
                    continue  # 旧扫描记录仍按原流程续跑。
                mid = part[0]
                content_path = save_message_content(paths['scan_records'].parent / 'messages' / root.name, item, part)
                text_path = save_text_file(folder, part)
                record.record_content_file(mid, text_path or content_path)
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
                return False
            return True

        async def persist_and_discover(item: Message, folder: Path) -> list[FoundMedia] | None:
            if not persist_message_content(item, folder):
                return None
            expected_ids = ({mid for mid, kind in item.content_types if kind in DOWNLOADABLE}
                            if item.content_types else
                            {mid for mid, _text, _date, has_media in item.parts if has_media})
            cached_ids = {found.media_mid for found in record.media.values()
                          if found.source_mid == item.mid}
            expected_positions = {mid: index for index, (mid, *_rest) in enumerate(
                (part for part in item.parts if part[0] in expected_ids), 1)}
            cached_positions = {mid: record.media[mid].index for mid in cached_ids}
            if (item.mid in record.discovered and expected_ids and
                    (cached_ids != expected_ids or cached_positions != expected_positions)):
                detail(f'消息 m{item.mid} 旧附件记录位置或数量变化，重新识别')
                record.invalidate_media(item.mid)
            if item.mid not in record.discovered:
                discovered = (await discover_media_server(page, int(group_id(url)), item)
                              if server_mode or api_media else await discover_media(page, url, item))
                record.record_discovered(item.mid, discovered)
            media = newest_first_media(item, [found for found in record.media.values()
                                              if found.source_mid == item.mid])
            media = [found for found in media if found.media_mid not in seen_media]
            return media

        async def process_message_media(item: Message, folder: Path, media: list[FoundMedia],
                                        message_number: int) -> None:
            nonlocal downloaded, reused, failed, download_page, done_files
            reused_in_message = 0
            for media_number, found in enumerate(media, 1):
                seen_media.add(found.media_mid)
                task = MediaTask(item, found.index, found.media_mid, folder, found.expected_size,
                                 partial_root, done_files + 1, total_files, reuse_group)
                try:
                    state, old_folder, download_page = await save_media_with_recovery(
                        page, download_page, url, task, existing_index,
                        exact_verify=exact_verify, server_mode=server_mode or api_media)
                    if old_folder:
                        relocated_sources.add(old_folder)
                    if state == 'downloaded':
                        downloaded += 1
                        saved_paths = existing_index.get(found.media_mid, [])
                        if len(saved_paths) == 1 and saved_paths[0].is_file():
                            size = saved_paths[0].stat().st_size
                            print(file_line(found.media_mid, done_files + 1, total_files,
                                            size, size, None, finished=True,
                                            timestamp=media_timestamp(item, found.media_mid)), flush=True)
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
                                         detail=str(media_paths[0]) if len(media_paths) == 1 else '',
                                         naming_revision=3)
                    if old_folder:
                        archive_empty_relocated_folders(root, relocated_sources)
                        relocated_sources.clear()
                except Exception as exc:
                    failed += 1
                    if isinstance(exc, LocalDataError):
                        failure(f'消息 m{item.mid} 附件 m{found.media_mid} 本地数据校验失败')
                        raise
                    record.record_result(found.media_mid, 'failed', detail=f'{type(exc).__name__}: {exc}')
                    if isinstance(exc, RemoteFileError):
                        print(f'  [远端文件错误] m{found.media_mid} · {exc}', flush=True)
                    failure(f'消息 m{item.mid} 附件 m{found.media_mid} 失败')
                    print(f'  [失败] {media_label(item, found.media_mid, done_files + 1, total_files)} · '
                          f'详情见 {log_location()}', flush=True)
                    if (page is not None and page.is_closed()) or 'TargetClosedError' in type(exc).__name__:
                        raise RuntimeError('Chrome 页面已关闭；本次停止，重新运行 BAT 可接着处理未完成附件') from exc
                    if isinstance(exc, DownloadTransportUnavailable):
                        raise
                finally:
                    done_files += 1
                    milestone = (done_files == total_files or done_files == 1 or
                                 done_files * 10 // max(total_files, 1) >
                                 (done_files - 1) * 10 // max(total_files, 1))
                    if milestone and (not server_mode or downloaded > before_downloaded or
                                      reused > before_reused or failed > before_failed):
                        print(group_line(done_files, total_files), flush=True)
            if reused_in_message:
                detail(f'消息 m{item.mid} 已有附件 {reused_in_message}/{len(media)} 已核对')
            for mid, _text, _timestamp, _has_media in item.parts:
                if mid not in record.results:
                    failed += 1
                    record.record_result(mid, 'failed', detail='未匹配到附件处理结果')
                    detail(f'消息 m{mid} 缺少处理结果')
                    print(f'  消息 {message_number}/{len(items)} 处理未完成', flush=True)

        def finalize_group() -> None:
            nonlocal existing_index
            if not dry_run:
                expected = {mid for item in batch for mid, _text, _date, has_media in item.parts if has_media}
                ready = (not pending_reply_target(batch, record, existing_index, root) and
                         selected == {item.mid for item in batch} and
                         failed == before_failed and
                         all(len(existing_index.get(mid, [])) == 1 and
                             (mid not in record.media or not record.media[mid].expected_size or
                              existing_index[mid][0].stat().st_size == record.media[mid].expected_size)
                             for mid in expected))
                if ready and has_group_files(pending) and not LINKED_NAME.fullmatch(published.parent.name):
                    if has_group_files(published):
                        raise RuntimeError(f'编组发布目标已存在：{published}')
                    published.parent.mkdir(parents=True, exist_ok=True)
                    move_files_only(pending, published)
                    _record_moved_paths(record, pending, published)
                    # 后续相册失败时 finalize_scan 不会执行，不能把来源收尾留到整轮结束。
                    archive_empty_relocated_folders(root, {pending})
                    relocated_sources.add(pending)
                    existing_index = current_media_index()
                    detail(f'编组文件已核对并移入成品目录：{batch_name}')
                current_media: dict[str, dict[str, FoundMedia]] = {}
                batch_ids = {item.mid for item in batch}
                for found in record.media.values():
                    if found.source_mid in batch_ids:
                        current_media.setdefault(found.source_mid, {})[found.media_mid] = found
                verified_root = root if has_group_files(published) else staging_root
                for item in items:
                    item_folder = destination(verified_root, anchor, batch, item)
                    if reusable_message_count(item, item, current_media.get(item.mid, {}),
                                              item_folder, record, existing_index,
                                              check_mark=False) is not None:
                        record.mark_message_verified(
                            item.mid, verified_group_signature([item], {item.mid: current_media.get(item.mid, {})}))
                if ready and has_group_files(published):
                    if reusable_group_count(batch, selected, published, pending,
                                            {item.mid: item for item in batch}, current_media,
                                            record, existing_index, check_mark=False) is not None:
                        record.mark_group_verified(batch_name, verified_group_signature(batch, current_media))
                if not server_mode or downloaded > before_downloaded or reused > before_reused or failed > before_failed:
                    print(f'[本段完成] {current_date} · 新下载 '
                          f'{downloaded - before_downloaded}，已有 {reused - before_reused}，'
                          f'失败 {failed - before_failed}', flush=True)

        for message_number, item in enumerate(items, 1):
            handled.add(item.mid)
            folder = destination(work_root, anchor, batch, item)
            if dry_run:
                print(f'  消息 {message_number}/{len(items)} · 约 {item.media_count} 个文件', flush=True)
                detail(f'预览 m{item.mid}: {folder}')
                continue
            if coarse_skip:
                bootstrapped = bootstrap_completed_media(
                    item, prior_by_mid.get(item.mid), record, existing_index)
                if bootstrapped is not None:
                    prior_media[item.mid] = bootstrapped
                    prior_by_mid.setdefault(item.mid, item)
                recover_interrupted_result(item, folder, partial_root, group_id(url),
                                           record, existing_index)
                relocated_sources.update(relocate_completed_message(
                    item, prior_by_mid.get(item.mid), prior_media.get(item.mid, {}),
                    folder, record, existing_index))
                count = reusable_message_count(item, prior_by_mid.get(item.mid),
                                               prior_media.get(item.mid, {}), folder,
                                               record, existing_index)
                if count is not None:
                    reused += count
                    done_files += count
                    record.mark_message_verified(
                        item.mid, verified_group_signature([item], {item.mid: prior_media.get(item.mid, {})}))
                    detail(f'快速跳过 m{item.mid}，复用 {count} 个文件')
                    continue
            try:
                media = await persist_and_discover(item, folder)
                if media is None:
                    continue
            except Exception as exc:
                if page.is_closed() or 'TargetClosedError' in type(exc).__name__:
                    raise RuntimeError('Chrome 页面已关闭；本次停止，重新运行 BAT 可接着处理未完成附件') from exc
                failed += 1
                done_files += (sum(kind in DOWNLOADABLE for _mid, kind in item.content_types)
                               if item.content_types else item.media_count)
                for mid, _text, _timestamp, _has_media in item.parts:
                    record.record_result(mid, 'failed', detail=f'附件识别失败：{exc}')
                failure(f'消息 m{item.mid} 附件识别失败')
                print(f'  消息 {message_number}/{len(items)}：附件识别失败，详情见 {log_location()}', flush=True)
                print(group_line(done_files, total_files), flush=True)
                continue
            await process_message_media(item, folder, media, message_number)
        finalize_group()

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

    async def scan_selected_groups() -> None:
        nonlocal wanted_sources, processed_groups
        print(f'[扫描] {title}：正在读取消息', flush=True)
        # 已跳过的较新成员仍属于当前头像段，必须完整保留在段内清单。
        pending_desc = resume_avatar_prefix(prior_messages, endpoint) if server_mode else []
        history = (iter_history_from_endpoint(page, int(group_id(url)), endpoint, progress)
                   if server_mode else iter_history(page, None, known_ids, progress,
                                                    pause_at_oldest=pause_at_oldest,
                                                    peer_id=int(group_id(url)), api_scan=True))
        async for item in history:
            scan_url = getattr(page, 'url', None)
            scanned_desc.append(item)
            if not dry_run:
                record.record_scan([item], reached_oldest=False)
            if limit is None or prefix_count + wanted_sources < limit:
                wanted.add(item.mid)
                wanted_sources += len(item.parts) or 1
            selected = item.mid in wanted
            if limit is not None and selected and (len(wanted) == 1 or wanted_sources % 10 == 0
                                                   or prefix_count + wanted_sources >= limit):
                print(f'[扫描] {date_label(item.timestamp)} · 已选 {prefix_count + wanted_sources} 条原消息 · '
                      f'本次限额 {limit}（相册不拆分）', flush=True)
            boundary = bool(pending_desc and not same_avatar_group(item, pending_desc[-1]))
            if boundary:
                batch = list(reversed(pending_desc))
                await confirm_group(batch, {entry.mid for entry in batch if entry.mid in wanted})
                pending_desc = [item]
            else:
                pending_desc.append(item)
            if limit is not None and not selected and boundary:
                break
            # 下载会跳到较新的媒体消息，继续滚动前返回当前旧端边界。
            if getattr(page, 'url', None) != scan_url:
                await restore_scan_position(page, url, item)

        if incremental and progress.stopped_on_known and pending_desc:
            # 新消息可能与上次最旧端组成同一头像段。
            for old in reversed(prior_messages):
                if not same_avatar_group(old, pending_desc[-1]):
                    break
                pending_desc.append(old)
                wanted.add(old.mid)
        if pending_desc:
            batch = list(reversed(pending_desc))
            await confirm_group(batch, {entry.mid for entry in batch if entry.mid in wanted})
        if ready_group is not None:
            await process_group(*ready_group)
            processed_groups += 1

    await scan_selected_groups()
    async def finalize_scan() -> None:
        nonlocal confirmed_groups, processed_groups, failed
        if not dry_run:
            if progress.reached_oldest and limit is None and scanned_desc:
                record.record_scan([scanned_desc[-1]], reached_oldest=True)
            # 已记录但本轮未扫描的附件仍需重试，按组从新到旧处理。
            for anchor, batch in (() if server_mode else reversed(grouped(record.ordered_messages()))):
                remaining = {entry.mid for entry in batch if entry.mid not in handled and
                             (limit is None or entry.mid in wanted)}
                if remaining:
                    confirmed_groups += 1
                    await process_group(batch, remaining)
                    processed_groups += 1
            demote_unready_replies(root, staging_root, record, current_media_index())
            reconcile_reply_groups(root, record)
            reconcile_reply_groups(staging_root, record)
            publish_ready_staged_groups(root, staging_root, record)
            relocated_sources.update(getattr(record, 'relocated_folders', set()))
            archive_empty_relocated_folders(root, relocated_sources, staging_root)
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

    await finalize_scan()
    if last_processed_date:
        print(f'[进度] 本次已处理至 {last_processed_date}', flush=True)
    return (prefix_count + sum(len(item.parts) or 1 for item in scanned_desc if item.mid in wanted),
            downloaded, reused, failed)
