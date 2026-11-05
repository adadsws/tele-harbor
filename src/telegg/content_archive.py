"""把每条原消息的文字与非文件内容保存为可复核的本地 JSON。"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from .content_types import DOWNLOADABLE, METADATA_ONLY
from .layout import (Message, group_folder, item_folder, legacy_group_folder,
                     old_group_folder, old_item_folder, previous_group_folder)


def save_full_text(folder: Path, text: str) -> Path:
    """同一消息正文变化时顺序保留版本；重复运行复用最新版本。"""
    encoded = text.encode('utf-8')
    plain = folder / '完整文字.txt'
    versions = []
    for path in folder.glob('完整文字.v*.txt'):
        match = re.fullmatch(r'完整文字\.v([1-9]\d*)\.txt', path.name)
        if not match or not path.is_file():
            raise RuntimeError(f'完整文字版本文件异常：{path}')
        versions.append((int(match.group(1)), path))
    versions.sort()
    if versions:
        if plain.exists() or [number for number, _ in versions] != list(range(1, len(versions) + 1)):
            raise RuntimeError(f'完整文字版本序列冲突：{folder}')
        latest = versions[-1][1]
        if latest.read_bytes() == encoded:
            return latest
        destination = folder / f'完整文字.v{len(versions) + 1}.txt'
    elif plain.exists():
        if not plain.is_file():
            raise RuntimeError(f'完整文字路径不是文件：{plain}')
        if plain.read_bytes() == encoded:
            return plain
        plain.rename(folder / '完整文字.v1.txt')
        destination = folder / '完整文字.v2.txt'
    else:
        destination = plain
    with destination.open('xb') as stream:
        stream.write(encoded)
    return destination


def prepare_group_folder(root: Path, anchor: Message | None, *, create: bool = False) -> Path:
    """迁移旧目录并保存全文；同编号编辑时按 v1、v2 递增。"""
    if anchor is None:
        return root / '未归组'
    folder = root / group_folder(anchor)
    olds = [root / name for name in dict.fromkeys((previous_group_folder(anchor),
                                                   old_group_folder(anchor), legacy_group_folder(anchor)))
            if name != folder.name]
    base = root.resolve()
    if not folder.resolve().is_relative_to(base) or any(not old.resolve().is_relative_to(base) for old in olds):
        raise RuntimeError(f'编组目录超出指定根目录：{root}')
    present = [old for old in olds if old.exists()]
    if present:
        if len(present) != 1 or folder.exists() or not present[0].is_dir():
            raise RuntimeError(f'旧目录与新目录冲突，未覆盖：{present} -> {folder}')
        present[0].rename(folder)
    if create and len(anchor.text) > 20:
        folder.mkdir(parents=True, exist_ok=True)
        save_full_text(folder, anchor.text)
    return folder


def prepare_item_folders(folder: Path, batch: list[Message]) -> int:
    """多消息编组的旧时间子目录改为日期和消息 ID。"""
    if len(batch) <= 1 or not folder.is_dir():
        return 0
    moved = 0
    base = folder.resolve()
    for item in batch:
        old = folder / old_item_folder(item)
        new = folder / item_folder(item)
        if not old.exists():
            continue
        if (not old.resolve().is_relative_to(base) or
                not new.resolve().is_relative_to(base) or
                not old.is_dir() or new.exists()):
            raise RuntimeError(f'消息子目录冲突，未覆盖：{old} -> {new}')
        old.rename(new)
        moved += 1
    return moved


def content_payload(item: Message, part: tuple[str, str, int, bool]) -> dict:
    """构造可保存内容；带内嵌文件的预览不能只保存文字。"""
    mid, text, timestamp, has_media = part
    kind = dict(item.content_types)[mid]
    detail = dict(item.content_details).get(mid, '')
    if kind not in DOWNLOADABLE | METADATA_ONLY:
        raise RuntimeError(f'原消息 m{mid} 内容类型不支持保存：{kind}')
    if kind in METADATA_ONLY and kind != 'text':
        if not detail or detail.startswith('__DETAIL_'):
            raise RuntimeError(f'原消息 m{mid} 的 {kind} 内容元数据不可读取：{detail}')
        media = json.loads(detail)
        if kind == 'link_preview':
            webpage = media.get('webpage') or {}
            if webpage.get('photo') or webpage.get('document'):
                raise RuntimeError(f'原消息 m{mid} 的链接预览含内嵌媒体，尚无完整下载路径')
    else:
        media = None
    payload = {'message_id': mid, 'bubble_id': item.mid, 'timestamp': timestamp,
               'text': text, 'content_type': kind, 'has_media': has_media}
    if media is not None:
        payload['media'] = media
    return payload


def save_message_content(root: Path, item: Message, part: tuple[str, str, int, bool]) -> Path:
    """按内容哈希命名；重跑复用同一文件，编辑后的消息保留旧版。"""
    payload = content_payload(item, part)
    mid = part[0]
    encoded = (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode('utf-8')
    digest = hashlib.sha256(encoded).hexdigest()[:16]
    path = root / '.messages' / f'm{mid}-{digest}.json'
    if path.exists():
        if not path.is_file() or path.read_bytes() != encoded:
            raise RuntimeError(f'消息内容文件已存在但不一致，未覆盖：{path}')
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('xb') as stream:
        stream.write(encoded)
    return path
