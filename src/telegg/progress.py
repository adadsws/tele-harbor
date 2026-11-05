"""CMD 进度条只使用已经确认的文件数和浏览器报告的字节数。"""

from __future__ import annotations

from datetime import datetime

from .layout import CHINA_TIME


def date_label(timestamp: int) -> str:
    """面向用户的扫描与下载日期，按本地约定使用上海时间。"""
    return datetime.fromtimestamp(timestamp, CHINA_TIME).strftime('%Y-%m-%d')


def bar(done: int, total: int, width: int = 20) -> str:
    if total <= 0:
        return '[' + '-' * width + ']'
    filled = min(width, max(0, int(done * width / total)))
    return '[' + '#' * filled + '-' * (width - filled) + ']'


def size_label(size: int) -> str:
    if size < 1024:
        return f'{size} B'
    if size < 1024 * 1024:
        return f'{size / 1024:.0f} KiB'
    return f'{size / (1024 * 1024):.1f} MiB'


def group_line(done: int, total: int) -> str:
    if total == 0:
        return '  [本组] 无文件，正文已保存'
    return f'  [本组] {bar(done, total)} {done}/{total} 个文件'


def file_line(mid: str, position: int, total_files: int,
              done: int, total_bytes: int, speed_bytes: float | None,
              finished: bool = False) -> str:
    percent = min(100, max(0, done * 100 / total_bytes)) if total_bytes else 0
    if finished:
        speed = '完成'
    elif speed_bytes is None:
        speed = '计算中'
    elif speed_bytes < 1024 * 1024:
        speed = f'{speed_bytes / 1024:.0f} KiB/s'
    else:
        speed = f'{size_label(int(speed_bytes))}/s'
    return (f'    [文件 {position}/{total_files}] {bar(done, total_bytes)} '
            f'{percent:5.1f}% {size_label(done)}/{size_label(total_bytes)} {speed}')
