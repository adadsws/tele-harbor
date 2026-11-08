"""把已确认空的目录移入归档，不移动含文件的目录。"""

from __future__ import annotations

from pathlib import Path
from datetime import datetime
import json


def archive_empty_folders(root: Path, *, archive_root: Path,
                          batch_name: str | None = None,
                          include_root: bool = False) -> tuple[int, Path | None]:
    """由深到浅归档空目录；仅 include_root=True 时允许归档空来源根。"""
    if not root.is_dir():
        return 0, None
    root = root.resolve()
    archive_root = archive_root.resolve()
    if root == archive_root or root.is_relative_to(archive_root):
        raise RuntimeError('空目录归档来源不能位于归档目录内')
    excluded = archive_root if archive_root.is_relative_to(root) else None
    candidates = list(root.rglob('*'))
    if any(path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction())
           for path in candidates):
        raise RuntimeError('空目录归档遇到链接或目录联接，已停止')
    folders = [path for path in candidates if path.is_dir() and
               (excluded is None or not path.is_relative_to(excluded))]
    if include_root:
        folders.append(root)
    folders.sort(key=lambda path: len(path.parts), reverse=True)
    name = batch_name or datetime.now().strftime('empty-folders-%Y%m%d-%H%M%S-%f')
    destination = archive_root / name
    if destination.exists():
        raise RuntimeError(f'空目录归档目标已存在：{destination}')
    moved = 0
    failures = []
    for folder in folders:
        if not folder.is_dir() or any(folder.iterdir()):
            continue
        if not folder.resolve().is_relative_to(root):
            raise RuntimeError(f'空目录归档来源越界：{folder}')
        target = destination / 'folders' / f'{moved + 1:06d}'
        if target.exists():
            raise RuntimeError(f'空目录归档目标冲突：{target}')
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            folder.rename(target)
        except PermissionError:
            failures.append(str(folder))
            continue
        with (destination / 'sources.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps({'source': str(folder), 'archive': str(target)},
                                    ensure_ascii=False) + '\n')
        moved += 1
    if failures:
        raise RuntimeError(f'{len(failures)} 个空目录被占用，未归档；首个：{failures[0]}')
    return moved, destination if moved else None
