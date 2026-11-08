"""按媒体 ID 逐文件复用旧媒体，并核验发布的文件去向。"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .resumable import verified_completed_file


def has_group_files(path: Path) -> bool:
    return path.is_dir() and any(item.is_file() for item in path.rglob('*'))


def move_files_only(source: Path, target: Path) -> int:
    """仅逐个移动文件并重建目标目录结构，不迁移任何目录本身。"""
    if not source.is_dir():
        return 0
    source, target = source.resolve(), target.resolve()
    if target.is_relative_to(source) or source.is_relative_to(target):
        raise RuntimeError('逐文件移动的来源与目标不能嵌套')
    paths = list(source.rglob('*'))
    if any(path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction())
           for path in paths):
        raise RuntimeError('逐文件移动遇到链接或目录联接，已停止')
    files = [path for path in paths if path.is_file()]
    sizes = [path.stat().st_size for path in files]
    targets = [target / path.relative_to(source) for path in files]
    if any(path.exists() for path in targets):
        raise RuntimeError('逐文件移动目标已有同名文件，未覆盖')
    for old, new in zip(files, targets):
        new.parent.mkdir(parents=True, exist_ok=True)
        old.rename(new)
    if (any(old.exists() for old in files) or
            any(not new.is_file() or new.stat().st_size != size
                for new, size in zip(targets, sizes)) or has_group_files(source)):
        raise RuntimeError('逐文件发布未完整完成；保留来源目录供恢复')
    return len(files)


def reuse_root(output: Path, group_id: str) -> Path:
    return output.resolve().parent / '~local' / group_id


def rekey_legacy_media(reuse_group: Path, archive_root: Path, peer: str) -> int:
    """用旧完整分片日志核验无媒体 ID 的旧文件，仅给媒体文件名补 ID。

    新扫描仍须从 Telegram 获取身份并再次比对；旧日志不写入新扫描记录。
    """
    if not reuse_group.is_dir() or not archive_root.is_dir():
        return 0
    if reuse_group.resolve() != (archive_root / peer).resolve():
        raise RuntimeError('复用目录与群组身份不符')
    pool_base = reuse_group.resolve()
    archive_base = archive_root.resolve()
    candidates = []
    for path in reuse_group.rglob('*'):
        if (not path.is_file() or path.is_symlink() or
                (hasattr(path, 'is_junction') and path.is_junction()) or
                not path.resolve().is_relative_to(pool_base)):
            continue
        match = re.fullmatch(r'm(\d+)_([1-9]\d*)_(.+)', path.name)
        if match and not re.match(
                r'(?:(?:document|photo|video|audio|animation|sticker)_)?\d{15,}(?:_|\.)',
                match.group(3)):
            candidates.append((path, *match.groups()))
    if not candidates:
        return 0
    wanted = {mid for _path, mid, _position, _tail in candidates}
    journals: dict[str, list[Path]] = {mid: [] for mid in wanted}
    for path in archive_root.rglob('*.jsonl'):
        if (path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction()) or
                not path.resolve().is_relative_to(archive_base)):
            continue
        match = re.fullmatch(r'(\d+)(?:\.verified)?\.jsonl', path.name)
        if match and match.group(1) in wanted:
            journals[match.group(1)].append(path)
    changed = 0
    for path, mid, position, tail in candidates:
        matched: dict[str, Path] = {}
        for journal in journals[mid]:
            try:
                with journal.open('rb') as stream:
                    header = json.loads(stream.readline())
                identity = header.get('identity', {})
                media_id = str(identity.get('id', ''))
                if (header.get('kind') != 'identity' or
                        str(identity.get('peer')) != peer or
                        str(identity.get('mid')) != mid or
                        not re.fullmatch(r'\d{15,}', media_id) or
                        header.get('size') != path.stat().st_size):
                    continue
                verified_completed_file(path, journal, peer=peer, mid=mid)
                matched[media_id] = journal
            except (OSError, ValueError, KeyError, RuntimeError, TypeError):
                continue
        if len(matched) != 1:
            continue
        media_id = next(iter(matched))
        target = path.with_name(f'm{mid}_{position}_{media_id}_{tail}')
        if target.exists():
            continue
        path.rename(target)
        changed += 1
    return changed


def stage_existing_group(output_group: Path, staging_group: Path,
                         pool: Path, *, content_group: Path | None = None,
                         scan_record: Path | None = None,
                         partial_group: Path | None = None) -> int:
    """旧入口禁止目录与状态迁移，避免把错误分组带入新结果。"""
    raise RuntimeError('--rebuild-local 的旧目录迁移方式已禁用；当前只从 ~local 逐个复用媒体文件')
