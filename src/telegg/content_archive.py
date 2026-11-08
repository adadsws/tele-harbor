"""保存原消息 TXT、非文件内容 JSON 与头像段清单。"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from .content_types import DOWNLOADABLE, METADATA_ONLY
from .layout import (CHINA_TIME, Message, group_folder, legacy_group_folder,
                     old_group_folder, old_item_folder, previous_group_folder)
from datetime import datetime


def prepare_group_folder(root: Path, anchor: Message | None, *, create: bool = False) -> Path:
    """准备头像段目录；旧标题目录只从复用区按媒体 ID 搬回。"""
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
        raise RuntimeError(f'旧目录不能整体迁移；请仅逐文件复用已下载媒体：{present}')
    if create:
        folder.mkdir(parents=True, exist_ok=True)
    return folder


def save_text_file(folder: Path, part: tuple[str, str, int, bool]) -> Path | None:
    """将原消息文字写为普通 TXT；相同编号编辑时不覆盖旧版。"""
    mid, text, timestamp, _has_media = part
    if not text:
        return None
    from .layout import safe_name
    day = datetime.fromtimestamp(timestamp, CHINA_TIME).strftime('%Y-%m-%d')
    path = folder / f'{day}-m{mid}_{safe_name(text[:30], 30)}.txt'
    encoded = text.encode('utf-8')
    versions = sorted((int(match.group(1)), old) for old in folder.glob(f'{path.stem}.v*.txt')
                      if (match := re.fullmatch(re.escape(path.stem) + r'\.v([1-9]\d*)\.txt', old.name)))
    if versions:
        if path.exists() or [number for number, _ in versions] != list(range(1, len(versions) + 1)):
            raise RuntimeError(f'文字文件版本冲突：{path}')
        if versions[-1][1].read_bytes() == encoded:
            return versions[-1][1]
        path = folder / f'{path.stem}.v{len(versions) + 1}.txt'
    elif path.is_file():
        if path.read_bytes() == encoded:
            return path
        path.rename(folder / f'{path.stem}.v1.txt')
        path = folder / f'{path.stem}.v2.txt'
    elif path.exists():
        raise RuntimeError(f'文字文件路径不是文件：{path}')
    folder.mkdir(parents=True, exist_ok=True)
    with path.open('xb') as stream:
        stream.write(encoded)
    return path


def save_group_manifest(folder: Path, batch: list[Message]) -> Path:
    """顺序记录头像段中各相册 ID 和原消息 ID，不保存正文。"""
    rows = [{'grouped_id': item.album_id or None,
             'message_ids': [part[0] for part in item.parts] or [item.mid]}
            for item in batch]
    encoded = (json.dumps({'items': rows}, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    path = folder / '段内清单.json'
    if path.is_file():
        current = path.read_bytes()
        if current == encoded:
            return path
        previous_ids = {mid for row in json.loads(current)['items'] for mid in row['message_ids']}
        next_ids = {mid for row in rows for mid in row['message_ids']}
        if previous_ids - next_ids:
            raise RuntimeError('头像段清单缩小，拒绝覆盖；需重新确认完整头像段')
        versions = [int(match.group(1)) for old in folder.glob('段内清单.v*.json')
                    if (match := re.fullmatch(r'段内清单\.v([1-9]\d*)\.json', old.name))]
        archived = folder / f'段内清单.v{max(versions, default=0) + 1}.json'
        path.rename(archived)
    if path.exists():
        raise RuntimeError(f'组清单路径不是文件：{path}')
    folder.mkdir(parents=True, exist_ok=True)
    with path.open('xb') as stream:
        stream.write(encoded)
    return path


def prepare_item_folders(folder: Path, batch: list[Message]) -> int:
    """禁止旧消息子目录迁移；重建只按媒体 ID 逐文件复用。"""
    if len(batch) <= 1 or not folder.is_dir():
        return 0
    moved = 0
    base = folder.resolve()
    for item in batch:
        old = folder / old_item_folder(item)
        if not old.exists():
            continue
        raise RuntimeError(f'旧消息子目录不能整体迁移：{old}')
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
