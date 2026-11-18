"""发布编组时逐文件移动，拒绝覆盖、路径嵌套和目录链接。"""

from __future__ import annotations

from pathlib import Path


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
