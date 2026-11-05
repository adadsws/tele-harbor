"""Telegram 消息归组与 Windows 安全目录名；不依赖浏览器。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

CHINA_TIME = timezone(timedelta(hours=8))
INVALID_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


@dataclass(frozen=True, slots=True)
class Message:
    mid: str
    text: str
    timestamp: int
    media_count: int
    parts: tuple[tuple[str, str, int, bool], ...] = ()
    content_types: tuple[tuple[str, str], ...] = ()
    content_details: tuple[tuple[str, str], ...] = ()


def group_id(url: str) -> str:
    parts = urlsplit(url)
    fragment = parts.fragment
    if parts.hostname != 'web.telegram.org' or not parts.path.startswith('/k/') or not re.fullmatch(r"-\d+", fragment):
        raise ValueError(f"需要 Telegram Web K 群组链接：{url}")
    return fragment


def group_root(output: Path, title: str, identity: str) -> Path:
    """群名变化时继续复用唯一的既有群组目录，避免重复下载。"""
    desired = output / f'{safe_name(title, 70)} [{identity}]'
    if not output.is_dir():
        return desired
    existing = [path for path in output.iterdir()
                if path.is_dir() and path.name.endswith(f' [{identity}]')]
    if len(existing) > 1:
        raise RuntimeError(f'发现多个同 ID 群组目录，无法安全选择：{identity}')
    return existing[0] if existing else desired


def safe_name(value: str, limit: int = 100) -> str:
    name = INVALID_NAME.sub("_", " ".join(value.split())).strip(" .")[:limit].rstrip(" .") or "未命名"
    return f"_{name}" if name.split(".", 1)[0].upper() in WINDOWS_RESERVED else name


def message_time(message: Message) -> datetime:
    return datetime.fromtimestamp(message.timestamp, CHINA_TIME)


def group_folder(message: Message) -> str:
    date = message_time(message).strftime("%Y-%m-%d")
    label = (f'{safe_name(message.text[:20], 20)}++' if len(message.text) > 20
             else safe_name(message.text, 20))
    return f"[{date}_m{message.mid}] {label}"


def previous_group_folder(message: Message) -> str:
    """上一版 10 字符正文目录名，仅供迁移已有文件。"""
    date = message_time(message).strftime('%Y-%m-%d')
    label = (f'{safe_name(message.text[:10], 10)}++' if len(message.text) > 10
             else safe_name(message.text, 10))
    return f'[{date}_m{message.mid}] {label}'


def old_group_folder(message: Message) -> str:
    """更早的日期前缀目录名，仅供迁移已有文件。"""
    date = message_time(message).strftime('%Y-%m-%d')
    label = (f'{safe_name(message.text[:10], 10)}++' if len(message.text) > 10
             else safe_name(message.text, 10))
    return f'{date}-{label} [m{message.mid}]'


def legacy_group_folder(message: Message) -> str:
    """最早的长正文目录名，仅供迁移已有文件。"""
    return f'{message_time(message):%Y-%m-%d}-{safe_name(message.text, 90)} [m{message.mid}]'


def item_folder(message: Message) -> str:
    return f"{message_time(message):%Y-%m-%d}_m{message.mid}"


def old_item_folder(message: Message) -> str:
    return f"{message_time(message):%Y-%m-%d %H-%M-%S} [m{message.mid}]"


def grouped(messages: list[Message]) -> list[tuple[Message | None, list[Message]]]:
    """输入旧到新消息；无文字消息归于下一条有文字消息。"""
    batches: list[tuple[Message | None, list[Message]]] = []
    pending: list[Message] = []
    for message in messages:
        pending.append(message)
        if message.text.strip():
            batches.append((message, pending))
            pending = []
    if pending:
        batches.append((None, pending))
    return batches


def destination(root: Path, anchor: Message | None, batch: list[Message], item: Message) -> Path:
    if anchor is None:
        return root / "未归组"
    parent = root / group_folder(anchor)
    return parent / item_folder(item) if len(batch) > 1 else parent
