"""共享媒体索引与版本识别，供快速跳过和逐文件只读预检复用。"""
import re
from pathlib import Path

from .failure_notes import registered_notes


def media_version(path: Path) -> int:
    """未编号文件视为初版；只识别扩展名前的版本后缀。"""
    match = re.search(r'\.v([1-9]\d*)(?=\.[^.]+$)', path.name)
    return int(match.group(1)) if match else 0


def existing_media_index(root: Path, *extra_roots: Path,
                         known_text_media: set[str] | None = None) -> dict[str, list[Path]]:
    """批量索引各业务根；连续 .v1/.v2 版本只保留最新项，普通重复不合并。"""
    result: dict[str, list[Path]] = {}
    notes: dict[Path, dict] = {}
    for base in (root, *extra_roots):
        for path in base.rglob('*'):
            match = re.match(r'^(?:\d{4}-\d{2}-\d{2}[-_]m|m)(\d+)_[1-9]\d*_(?:(?:document|photo|video|audio|animation|sticker)_)?\d+(?:[_.]|$)', path.name)
            if (match and (path.suffix.lower() != '.txt' or
                           (known_text_media is not None and match.group(1) in known_text_media))
                    and path.is_file() and path.stat().st_size > 0
                    and not re.search(r'\.part(?:\.\d+)?$', path.name)):
                if path.suffix.lower() == '.txt':
                    if path.parent not in notes:
                        notes[path.parent] = registered_notes(path.parent)
                    if path.name in notes[path.parent]:
                        continue
                result.setdefault(match.group(1), []).append(path)
    for mid, paths in result.items():
        if len(paths) > 1:
            numbered = sorted((media_version(path), path) for path in paths)
            if [number for number, _ in numbered] == list(range(1, len(paths) + 1)):
                result[mid] = [numbered[-1][1]]
    return result
