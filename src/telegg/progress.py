"""CMD 进度条只使用已经确认的文件数和浏览器报告的字节数。"""

from __future__ import annotations

from datetime import datetime
import math
import sys
import shutil
import unicodedata

from .layout import CHINA_TIME, Message


def media_timestamp(message: Message, mid: str) -> int:
    """相册每个附件使用其原消息时间，不使用相册代表消息时间。"""
    return next((stamp for source_mid, _text, stamp, _media in message.parts
                 if source_mid == mid), message.timestamp)


def message_count(messages: list[Message]) -> int:
    return sum(len(item.parts) or 1 for item in messages)


def media_label(message: Message, mid: str, position: int, total: int) -> str:
    stamp = datetime.fromtimestamp(media_timestamp(message, mid), CHINA_TIME)
    return f'本段文件 {position}/{total} · 群内日期 {stamp:%Y-%m-%d} · m{mid}'


def display_width(line: str) -> int:
    return sum(2 if unicodedata.east_asian_width(char) in ('W', 'F') else 1 for char in line)


class ProgressLine:
    """CMD 原行刷新；重定向输出完整行，避免日志残留回车与覆盖字符。"""

    def __init__(self, stream=None):
        self.stream = stream if stream is not None else sys.stdout
        self.interactive = self.stream.isatty()
        self.width = 0
        self.active = False

    def write(self, line: str) -> None:
        width = display_width(line)
        self.width = max(self.width, width)
        prefix, suffix = ('\r', '') if self.interactive else ('', '\n')
        padding = ' ' * (self.width - width) if self.interactive else ''
        print(prefix + line + padding, end=suffix, file=self.stream, flush=True)
        self.active = True

    def finish(self) -> None:
        if self.active and self.interactive:
            print(file=self.stream, flush=True)
        self.active = False


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
    if size < 1024 * 1024 * 1024:
        return f'{size / (1024 * 1024):.1f} MiB'
    return f'{size / (1024 * 1024 * 1024):.1f} GiB'


def remaining_label(done: int, total: int, speed: float | None) -> str:
    if not speed or speed <= 0:
        return '预计剩余计算中'
    seconds = max(0, math.ceil((total - done) / speed))
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    duration = f'{hours}时{minutes}分' if hours else f'{minutes}分{seconds}秒' if minutes else f'{seconds}秒'
    return f'剩余约 {duration}'


def group_line(done: int, total: int) -> str:
    if total == 0:
        return '  [本段] 无文件，消息内容已保存'
    return f'  [本段] {bar(done, total)} {done}/{total} 个文件'


def file_line(mid: str, position: int, total_files: int,
              done: int, total_bytes: int, speed_bytes: float | None,
              finished: bool = False, *, timestamp: int | None = None) -> str:
    percent = min(100, max(0, done * 100 / total_bytes)) if total_bytes else 0
    if finished:
        speed = '完成 · 校验通过'
    elif total_bytes and done >= total_bytes:
        speed = '校验中'
    elif speed_bytes is None:
        speed = '计算中'
    elif speed_bytes < 1024 * 1024:
        speed = f'{speed_bytes / 1024:.0f} KiB/s'
    else:
        speed = f'{size_label(int(speed_bytes))}/s'
    if not finished and done < total_bytes:
        speed += ' · ' + remaining_label(done, total_bytes, speed_bytes)
    day = f' · {date_label(timestamp)}' if timestamp is not None else ''
    line = (f'    [本段文件 {position}/{total_files}{day}] {bar(done, total_bytes, 12)} '
            f'{percent:5.1f}% {size_label(done)}/{size_label(total_bytes)} {speed}')
    if display_width(line) < shutil.get_terminal_size((80, 24)).columns:
        return line
    # 80 列 CMD 保留日期、百分比、字节和剩余量；去掉附件条避免自动折行。
    sizes = f'{size_label(done)}/{size_label(total_bytes)}'.replace(' ', '')
    compact_speed = speed.replace(' · ', ' ').replace('剩余约 ', '余约')
    return f'[本段文件 {position}/{total_files}{day}] {percent:.1f}% {sizes} {compact_speed}'
