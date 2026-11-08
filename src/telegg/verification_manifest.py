"""保存已验收文件的证据；快速模式只复用文件状态完全匹配的哈希。"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Callable
from .errors import LocalDataError


def read_snapshot(journal: Path, *, strict: bool = False, required: bool = False) -> dict | None:
    if not journal.is_file():
        if required:
            raise LocalDataError(f'已有媒体缺少验收清单：{journal}')
        return None
    previous = None
    try:
        for line in journal.read_text(encoding='utf-8').splitlines():
            previous = json.loads(line)
            if strict:
                valid = (isinstance(previous, dict) and isinstance(previous.get('identity'), dict)
                         and isinstance(previous.get('path'), str)
                         and isinstance(previous.get('size'), int)
                         and isinstance(previous.get('sha256'), str)
                         and re.fullmatch(r'[0-9a-f]{64}', previous['sha256']))
                if not valid:
                    raise ValueError('验收记录字段无效')
        if strict and previous is None:
            raise ValueError('验收清单为空')
    except (OSError, ValueError, TypeError) as exc:
        if strict:
            raise LocalDataError(f'本地验收清单损坏：{journal}') from exc
        return None
    return previous


def verified_digest(path: Path, identity: dict, journal: Path,
                    hash_file: Callable[[Path], str], *, exact: bool = False,
                    known_digest: str | None = None) -> str:
    """清单缺失或状态变化时读全文件；精确模式每次都读全文件。"""
    stat = path.stat()
    stamp = {'path': str(path.resolve()), 'size': stat.st_size,
             'mtime_ns': stat.st_mtime_ns, 'dev': stat.st_dev, 'ino': stat.st_ino,
             'identity': identity}
    previous = read_snapshot(journal, strict=exact)
    if (isinstance(previous, dict) and (previous.get('path') == stamp['path'] or exact)
            and previous.get('identity') not in (None, identity)):
        error = LocalDataError if exact else RuntimeError
        raise error(f'已有文件媒体身份与验收清单不符：{path}')
    same_file = (isinstance(previous, dict)
                 and all(previous.get(key) == value for key, value in stamp.items()
                         if key != 'path')
                 and isinstance(previous.get('sha256'), str)
                 and re.fullmatch(r'[0-9a-f]{64}', previous['sha256']))
    if not exact and known_digest is None and same_file:
        if previous['path'] == stamp['path']:
            return previous['sha256']
        # 同卷重命名保留文件身份；更新路径快照，无需重读大文件。
        known_digest = previous['sha256']
    digest = known_digest if known_digest is not None else hash_file(path)
    if not re.fullmatch(r'[0-9a-f]{64}', digest):
        raise RuntimeError(f'文件 SHA-256 无效：{path}')
    if (isinstance(previous, dict) and (previous.get('path') == stamp['path'] or exact)
            and previous.get('identity') == identity
            and previous.get('size') == stat.st_size
            and isinstance(previous.get('sha256'), str)
            and re.fullmatch(r'[0-9a-f]{64}', previous['sha256'])
            and previous['sha256'] != digest):
        error = LocalDataError if exact else RuntimeError
        raise error(f'已有文件 SHA-256 与验收清单不符：{path}')
    # 哈希期间文件变化时不能将其登记为可信快照。
    after = path.stat()
    if (after.st_size, after.st_mtime_ns, after.st_dev, after.st_ino) != (
            stat.st_size, stat.st_mtime_ns, stat.st_dev, stat.st_ino):
        raise RuntimeError(f'校验期间文件发生变化：{path}')
    journal.parent.mkdir(parents=True, exist_ok=True)
    with journal.open('a', encoding='utf-8', newline='\n') as stream:
        stream.write(json.dumps({**stamp, 'sha256': digest}, ensure_ascii=False) + '\n')
        stream.flush()
        os.fsync(stream.fileno())
    return digest
