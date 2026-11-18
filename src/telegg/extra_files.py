"""只读比较下载目录与当前服务器消息；只返回候选额外文件路径。"""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .failure_notes import LEGACY_REGISTRY, REGISTRY, registered_notes, validate_failure_notes
from .layout import group_id
from .local_config import load_paths
from .media_index import media_version
from .verification_manifest import read_snapshot


MEDIA_NAME = re.compile(
    r'^(?:\d{4}-\d{2}-\d{2}[-_]m|m)(\d+)_[1-9]\d*_'
    r'(?:(?:document|photo|video|audio|animation|sticker)_)?(\d+)(?:[_.]|$)')
TEXT_NAME = re.compile(r'^\d{4}-\d{2}-\d{2}[-_]m(\d+)_.+\.txt$', re.IGNORECASE)
HELPER_NAME = re.compile(r'^%?(段内清单|关联清单)(?:\.v[1-9]\d*)?\.json$')
SEGMENT_NAME = re.compile(r'^\[From_\d{4}-\d{2}-\d{2}-m[1-9]\d*\](?: 关联组)?$')
MODULUS = 1 << 32
PARTIAL_NAME = re.compile(r'^(\d+)(\.v[1-9]\d*)?(\.(?:verified\.)?jsonl(?:\.torn-[0-9a-f]+)?|\.part)$')

# 复用产品已有 Web K 历史接口；结果严格限定为身份元数据，不传回正文或媒体字节。
HISTORY_METADATA = '''async ({peerId, offset}) => {
    const m = window.appDownloadManager?.managers;
    if (!m?.apiManager || !m?.appPeersManager) throw new Error('Telegram 历史接口不可用');
    const peer = await m.appPeersManager.getInputPeerById(peerId);
    const result = await m.apiManager.invokeApi('messages.getHistory', {
        peer, offset_id: offset, offset_date: 0, add_offset: 0,
        limit: 100, max_id: 0, min_id: 0, hash: 0
    }, {noErrorBox: true, noRetry: true});
    if (!Array.isArray(result?.messages)) throw new Error('Telegram 历史响应无消息列表');
    return result.messages.map(message => ({
        id: Number(message.id), type: message._,
        media_id: String(message.media?.document?.id || message.media?.photo?.id || ''),
        has_text: !!message.message,
        peer_id: Number(message.peer_id?.channel_id || message.peer_id?.chat_id || 0)
    }));
}'''


@dataclass(frozen=True)
class LocalFile:
    path: Path
    mid: int | None = None
    media_id: str = ''
    role: str = 'unknown'


def _is_link(path: Path) -> bool:
    return path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction())


def group_roots(output: Path, staging: Path, identity: str) -> list[Path]:
    """只选择指定群的活动下载目录；不进入历史目录、归档或分片。"""
    roots = []
    for requested in (output.absolute(), staging.absolute()):
        if any(_is_link(parent) for parent in (requested, *requested.parents)):
            raise RuntimeError(f'检查目录含链接或目录联接：{requested}')
    for base in dict.fromkeys((output.resolve(), staging.resolve())):
        if _is_link(base):
            raise RuntimeError(f'检查目录是链接或目录联接：{base}')
        if not base.is_dir():
            continue
        matches = []
        for path in base.iterdir():
            if path.name.endswith(f' [{identity}]'):
                if _is_link(path) or not path.is_dir():
                    raise RuntimeError(f'群组目录类型异常：{path}')
                matches.append(path)
        if len(matches) > 1:
            raise RuntimeError(f'同群存在多个目录，无法确认检查范围：{base}')
        roots.extend(matches)
    return roots


def inventory(roots: list[Path], *, excluded: set[Path] | None = None) -> tuple[list[Path], dict[Path, tuple]]:
    """只读盘点并保存文件身份；不跟随链接，也不读媒体或原文字节。"""
    files, stamps = [], {}
    for root in roots:
        if _is_link(root):
            raise RuntimeError(f'检查目录是链接或目录联接：{root}')
        for folder, dirs, names in os.walk(root, followlinks=False, onerror=_walk_error):
            parent = Path(folder)
            for name in [*dirs, *names]:
                if _is_link(parent / name):
                    raise RuntimeError(f'检查范围含链接或目录联接：{parent / name}')
            dirs[:] = [name for name in dirs if name != '~archive' and
                       parent / name not in (excluded or set())]
            for name in names:
                path = parent / name
                stat = path.stat()
                if not path.is_file():
                    raise RuntimeError(f'检查范围含非普通文件：{path}')
                files.append(path)
                stamps[path] = (stat.st_size, stat.st_mtime_ns, stat.st_dev, stat.st_ino)
    return sorted(files), stamps


def _walk_error(error: OSError) -> None:
    raise error


def storage_groups(output: Path, staging: Path, partials: Path) -> set[str]:
    """全部检查也纳入目录里已有但已从配置移除的群，不把未核对群误报为额外。"""
    identities = set()
    for base in (output, staging, partials):
        if any(_is_link(p) for p in (base.absolute(), *base.absolute().parents)):
            raise RuntimeError(f'检查目录含链接或目录联接：{base}')
        if not base.is_dir():
            continue
        for child in base.iterdir():
            match = re.search(r' \[(-\d+)\]$', child.name) if base != partials else re.fullmatch(r'-\d+', child.name)
            if match:
                identities.add(match[1] if base != partials else match[0])
    return identities


def loose_storage_files(output: Path, staging: Path, partials: Path) -> list[Path]:
    """检查整个下载管理根的散落文件；合法群目录由在线群组检查负责。"""
    parents = [output, staging, partials]
    if staging.parent == partials.parent and staging.name == 'groups':
        parents = [output, staging.parent]
    for root in parents:
        if any(_is_link(p) for p in (root.absolute(), *root.absolute().parents)):
            raise RuntimeError(f'检查目录含链接或目录联接：{root}')
    parents = [root.resolve() for root in dict.fromkeys(parents) if root.exists()]
    owned = []
    for identity in storage_groups(output, staging, partials):
        owned.extend(group_roots(output, staging, identity))
        if (partials / identity).is_dir():
            owned.append((partials / identity).resolve())
    extras, before = inventory(parents, excluded=set(owned))
    _files, after = inventory(parents, excluded=set(owned))
    if before != after:
        raise RuntimeError('盘点期间本地文件发生变化，散落文件结果未确认')
    return extras


def classify_partials(files: list[Path], peer: str) -> list[LocalFile]:
    """只读分片身份头和验收日志；不调用 PartialFile.open，不读取 .part 字节。"""
    result, identities = [], {}
    for path in files:
        match = PARTIAL_NAME.fullmatch(path.name)
        if not match:
            result.append(LocalFile(path))
            continue
        key = match[1] + (match[2] or '')
        journal = path.parent / f'{key}.jsonl' if path.suffix == '.part' else path
        if journal not in identities:
            try:
                if journal.name.endswith('.verified.jsonl'):
                    header = read_snapshot(journal, strict=True, required=True)
                else:
                    with journal.open('rb') as stream:
                        header = json.loads(stream.readline(65537))
                    if header.get('kind') != 'identity':
                        raise ValueError('不是分片身份记录')
                identity = header['identity']
                if (str(identity['peer']) != peer or str(identity['mid']) != match[1]
                        or not str(identity['id']).isdecimal() or int(header['size']) <= 0):
                    raise ValueError('分片身份不符')
                identities[journal] = str(identity['id'])
            except (OSError, ValueError, KeyError, TypeError, AttributeError, RuntimeError):
                identities[journal] = ''
        if identities[journal]:
            result.append(LocalFile(path, int(match[1]) % MODULUS, identities[journal], 'partial'))
        else:
            result.append(LocalFile(path))
    return result


def text_family(path: Path) -> Path:
    """已保存原文的编号版本共享文件族，避免数字开头的正文被误认作 TXT 附件。"""
    return path.with_name(re.sub(r'\.v[1-9]\d*$', '', path.stem) + path.suffix)


def recorded_text_paths(record_path: Path) -> set[Path]:
    paths = set()
    if record_path.is_file():
        with record_path.open(encoding='utf-8') as stream:
            for line in stream:
                event = json.loads(line)
                if event.get('kind') == 'content_file':
                    path = Path(event['path'])
                    if path.suffix.lower() == '.txt':
                        paths.add(text_family(path))
    return paths


def classify_files(files: list[Path], text_paths: set[Path] | None = None) -> list[LocalFile]:
    """保留合法辅助文件及已登记说明；其余按消息号、媒体身份分类。"""
    notes = {}
    result = []
    for path in files:
        if re.search(r'\.part(?:\.\d+)?$', path.name):
            result.append(LocalFile(path))
            continue
        if path.parent not in notes:
            notes[path.parent] = registered_notes(path.parent)
        if path.name in notes[path.parent] or path.name in (REGISTRY, LEGACY_REGISTRY):
            result.append(LocalFile(path, role='helper'))
            continue
        helper = HELPER_NAME.fullmatch(path.name)
        if helper and SEGMENT_NAME.fullmatch(path.parent.name):
            result.append(LocalFile(path, role='helper'))
            continue
        text = TEXT_NAME.fullmatch(path.name)
        if text and text_family(path) in (text_paths or set()):
            result.append(LocalFile(path, int(text[1]) % MODULUS, role='text'))
            continue
        media = MEDIA_NAME.match(path.name)
        if media:
            result.append(LocalFile(path, int(media[1]) % MODULUS, media[2], 'media'))
            continue
        if text:
            result.append(LocalFile(path, int(text[1]) % MODULUS, role='text'))
            continue
        result.append(LocalFile(path))
    return result


async def current_messages(page, peer_id: int, wanted: set[int],
                           progress: Callable[[int], None] | None = None) -> dict[int, dict]:
    """分页覆盖本地最旧消息为止；不完整或重复的响应绝不解释为远端删除。"""
    if not wanted:
        return {}
    if min(wanted) <= 0:
        raise RuntimeError('本地消息号无效，无法核对范围')
    oldest, offset, found, count = min(wanted), 0, {}, 0
    while True:
        rows = await asyncio.wait_for(
            page.evaluate(HISTORY_METADATA, {'peerId': peer_id, 'offset': offset}), timeout=60)
        if not isinstance(rows, list):
            raise RuntimeError('服务器检查响应不是消息列表')
        if not rows:
            return found
        previous = offset or MODULUS
        for row in rows:
            mid = row.get('id')
            if (type(mid) is not int or not 0 < mid < previous or
                    row.get('type') not in ('message', 'messageService', 'messageEmpty')):
                raise RuntimeError('服务器检查分页顺序或消息类型异常')
            if row['type'] != 'messageEmpty' and row.get('peer_id') != abs(peer_id):
                raise RuntimeError('服务器检查响应群组不符')
            previous = mid
            if row['type'] == 'message':
                if not isinstance(row.get('media_id'), str) or type(row.get('has_text')) is not bool:
                    raise RuntimeError('服务器检查消息身份字段无效')
                if mid in wanted:
                    found[mid] = row
            count += 1
        if progress:
            progress(count)
        if previous <= oldest:
            return found
        offset = previous


def extra_paths(local: list[LocalFile], remote: dict[int, dict]) -> list[Path]:
    """普通重复列出所有候选；连续编号版本保留，最新版本仍核对服务器身份。"""
    extras = set()
    media = defaultdict(list)
    partials, retained_versions = [], set()
    for item in local:
        if item.role == 'unknown':
            extras.add(item.path)
        elif item.role == 'text':
            if item.mid not in remote:
                extras.add(item.path)
        elif item.role == 'media':
            media[item.mid].append(item)
        elif item.role == 'partial':
            partials.append(item)
    for mid, items in media.items():
        current = remote.get(mid)
        if current is None:
            extras.update(item.path for item in items)
            continue
        numbered = sorted(items, key=lambda item: media_version(item.path))
        versions = [media_version(item.path) for item in numbered]
        if len(items) > 1 and versions != list(range(1, len(items) + 1)):
            extras.update(item.path for item in items)
            continue
        retained_versions.update((mid, item.media_id) for item in numbered[:-1])
        latest = numbered[-1]
        if latest.media_id != current['media_id']:
            extras.add(latest.path)
    for item in partials:
        current = remote.get(item.mid)
        historical = (re.match(r'^\d+\.v[1-9]\d*\.', item.path.name) or
                      (item.mid, item.media_id) in retained_versions)
        if current is None or (item.media_id != current['media_id'] and not historical):
            extras.add(item.path)
    return sorted(extras)


def redundant_helpers(local: list[LocalFile], roots: list[Path]) -> set[Path]:
    """仅报告整棵暂存子树已无业务文件且成品已有相同副本的辅助清单。"""
    extras = set()
    if len(roots) != 2:
        return extras
    output, staging = roots
    for item in local:
        if item.role != 'helper' or not HELPER_NAME.fullmatch(item.path.name):
            continue
        if not item.path.is_relative_to(staging):
            continue
        siblings = [other for other in local if other.path.is_relative_to(item.path.parent)]
        if any(other.role != 'helper' for other in siblings):
            continue
        counterpart = output / item.path.relative_to(staging)
        if counterpart.is_file() and counterpart.read_bytes() == item.path.read_bytes():
            extras.add(item.path)
    return extras


async def check_extra_files(page, url: str, output: Path, staging: Path,
                            on_group_open: Callable[[str], None] | None = None,
                            *, partials: Path | None = None, expected_mids: set[int] | None = None,
                            details: bool = False, expected_paths=None):
    """独立检查入口；绝不进入下载、记录追加或目录整理代码。"""
    from .browser import open_group
    title = await open_group(page, url, require_message=False)
    if on_group_open:
        on_group_open(title)
    label='缺失/额外联合检查' if expected_paths is not None else '额外文件检查'
    print(f'[{label}] 正在只读盘点本地目录', flush=True)
    group_folders = group_roots(output, staging, group_id(url))
    roots = list(group_folders)
    partial_group = partials / group_id(url) if partials is not None else None
    if partial_group is not None and partial_group.exists():
        if _is_link(partial_group):
            raise RuntimeError(f'分片目录是链接或目录联接：{partial_group}')
        roots.append(partial_group.resolve())
    files, before = await asyncio.to_thread(inventory, roots)
    missing=sorted(path for path in (expected_paths or ()) if path not in before)
    if expected_paths is not None:
        print(f'[{label}] 同轮路径比对：已下载缺失 {len(missing)} 个；继续核对额外文件的服务器身份',flush=True)
    for root in roots:
        registries = [path for path in files if path.name in (REGISTRY, LEGACY_REGISTRY)
                      and path.is_relative_to(root)]
        await asyncio.to_thread(validate_failure_notes, root, registries=registries)
    record_path = load_paths()['scan_records'] / f'{group_id(url)}.jsonl'
    text_paths = await asyncio.to_thread(recorded_text_paths, record_path)
    partial_files = [path for path in files if partial_group is not None and path.is_relative_to(partial_group)]
    if partial_files:
        print(f'[{label}] 正在检查分片与续传记录，共 {len(partial_files)} 个文件', flush=True)
    partial_set = set(partial_files)
    local = await asyncio.to_thread(classify_files, [path for path in files if path not in partial_set], text_paths)
    local.extend(await asyncio.to_thread(classify_partials, partial_files, group_id(url)))
    wanted = {item.mid for item in local if item.mid is not None} | (expected_mids or set())
    print(f'[{label}] 本地 {len(files)} 个文件，正在核对涉及的服务器消息范围', flush=True)
    def progress(count: int) -> None:
        if count % 1000 == 0:
            print(f'[{label}] 已核对 {count} 条服务器消息', flush=True)
    remote = await current_messages(page, int(group_id(url)), wanted, progress)
    extras = set(extra_paths(local, remote))
    extras.update(await asyncio.to_thread(redundant_helpers, local, group_folders))
    after_roots = group_roots(output, staging, group_id(url))
    if partial_group is not None and partial_group.exists():
        after_roots.append(partial_group.resolve())
    _files, after = await asyncio.to_thread(inventory, after_roots)
    if roots != after_roots or before != after:
        raise RuntimeError('检查期间本地文件发生变化，结果未确认；请在下载暂停后重试')
    if details:
        return {'extras':sorted(extras),'missing':missing,'local':local,'remote':remote,'stamps':before}
    return sorted(extras)
