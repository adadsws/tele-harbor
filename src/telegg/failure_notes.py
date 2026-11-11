"""脚本生成的异常说明：保持原件同名，独立登记，绝不充当原件。"""

import hashlib
import json
import os
import re
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from .errors import LocalDataError, RemoteFileError


REGISTRY = '下载异常说明.jsonl'
LEGACY_REGISTRY = '%' + REGISTRY  # 只兼容读取旧数据，新生成名称不使用该标记。


def registry_path(folder: Path) -> Path:
    paths = [folder / name for name in (REGISTRY, LEGACY_REGISTRY)]
    present = [path for path in paths if path.exists() or path.is_symlink()]
    if len(present) > 1:
        raise LocalDataError(f'新旧异常说明登记同时存在，未合并或覆盖：{folder}')
    return present[0] if present else paths[0]


def registry_files(root: Path):
    """同时识别历史登记；只读取，不因扫描或预检改名。"""
    for name in (REGISTRY, LEGACY_REGISTRY):
        yield from root.rglob(name)


def failure_note_path(media: Path) -> Path:
    return media.with_suffix('.txt')


def registered_notes(folder: Path) -> dict[str, dict]:
    path = registry_path(folder)
    if not path.exists() and not path.is_symlink():
        return {}
    if path.is_symlink() or not path.is_file():
        raise LocalDataError(f'异常说明登记不是普通文件：{path}')
    active = {}
    try:
        lines = path.read_text(encoding='utf-8').splitlines()
        if not lines:
            raise ValueError('empty registration')
        for line in lines:
            row = json.loads(line)
            name, media = row['note'], row['media']
            if (row['version'] != 1 or row['action'] not in {'active', 'retired'}
                    or not all(isinstance(v, str) and v and not any(c in v for c in '/\\:')
                               and v not in {'.', '..'} for v in (name, media))
                    or name != Path(media).with_suffix('.txt').name
                    or not row['mid'].isdecimal()
                    or not re.fullmatch('[0-9a-f]{64}', row['sha256'])):
                raise ValueError('invalid entry')
            if row['action'] == 'active':
                if name in active:
                    raise ValueError('duplicate registration')
                active[name] = row
            else:
                old = active.pop(name)
                if any(old[key] != row[key] for key in ('media', 'mid', 'sha256')):
                    raise ValueError('retirement differs')
    except (ValueError, KeyError, TypeError, AttributeError, OSError) as exc:
        raise LocalDataError(f'异常说明登记损坏：{path}') from exc
    return active


def _append(folder: Path, entry: dict) -> None:
    migrate_registry(folder)
    with (folder / REGISTRY).open('a', encoding='utf-8', newline='\n') as stream:
        stream.write(json.dumps(entry, ensure_ascii=False) + '\n')
        stream.flush()
        os.fsync(stream.fileno())


def _check_note(path: Path, entry: dict) -> None:
    if (path.is_symlink() or not path.is_file() or path.stat().st_size > 65536
            or hashlib.sha256(path.read_bytes()).hexdigest() != entry['sha256']):
        raise LocalDataError(f'脚本异常说明缺失、被修改或不是普通文件：{path}')


def migrate_registry(folder: Path) -> Path:
    """正常写入时迁移可信旧登记；预检和索引不得调用。"""
    path = registry_path(folder)
    if path.name == LEGACY_REGISTRY:
        for name, entry in registered_notes(folder).items():
            _check_note(folder / name, entry)
        target = folder / REGISTRY
        if target.exists() or target.is_symlink():
            raise LocalDataError(f'异常说明新登记目标已存在，未覆盖：{target}')
        path.rename(target)
        return target
    return path


def validate_failure_notes(root: Path) -> None:
    """只读校验登记及说明，不修复本地数据。"""
    for registry in registry_files(root):
        for name, entry in registered_notes(registry.parent).items():
            _check_note(registry.parent / name, entry)


def retire_failure_note(media: Path, archive: Path) -> None:
    """仅归档具有有效脚本登记和哈希证据的说明文件，不移动目录。"""
    active = registered_notes(media.parent)
    note = failure_note_path(media)
    entry = active.get(note.name)
    if entry is None:
        return
    _check_note(note, entry)
    migrate_registry(media.parent)
    batch = archive / (datetime.now().strftime('%Y%m%d-%H%M%S-') + uuid4().hex)
    batch.mkdir(parents=True)
    if len(active) > 1:
        _append(batch, entry)
    note.rename(batch / note.name)
    if len(active) == 1:
        # 登记随说明一起归档，归档区再次被索引时也不能将说明当成 TXT 原件。
        (media.parent / REGISTRY).rename(batch / REGISTRY)
    else:
        _append(media.parent, {**entry, 'action': 'retired'})


def write_failure_note(media: Path, mid: str, size: int, error: RemoteFileError) -> Path:
    """仅下载脚本调用；同名用户文件和被编辑的说明均报错，不覆盖。"""
    note = failure_note_path(media)
    active = registered_notes(media.parent)
    if note.name in active:
        _check_note(note, active[note.name])
        migrate_registry(media.parent)
        return note
    if note.exists() or note.is_symlink():
        raise LocalDataError(f'异常说明目标已存在且没有脚本登记，未覆盖：{note}')
    payload = ('下载异常说明（由脚本自动生成）\n'
               f'记录日期：{datetime.now():%Y-%m-%d}\n'
               f'消息号：m{mid}\n预期字节数：{size}\n'
               f'服务器错误：{error.code} {error.error_type}\n'
               '原始附件尚未取得；此文件是诊断说明，不是原件，也不代表下载完成。\n'
               '失败记录及已验证分片保留；下次运行继续尝试，原件校验通过后自动归档此说明。\n'
               ).encode('utf-8')
    migrate_registry(media.parent)
    media.parent.mkdir(parents=True, exist_ok=True)
    temporary = media.parent / f'telegg-failure-note-{uuid4().hex}.tmp'
    with temporary.open('xb') as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    _append(media.parent, {'version': 1, 'action': 'active', 'mid': str(mid),
                         'note': note.name, 'media': media.name,
                         'sha256': hashlib.sha256(payload).hexdigest()})
    temporary.rename(note)
    return note
