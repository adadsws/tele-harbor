"""持久保存分片和提交日志，核验完整后才移动到正式文件。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import uuid
from pathlib import Path
from typing import Awaitable, Callable

CHUNK_SIZE = 1024 * 1024
LEGACY_CHUNK_SIZE = 256 * 1024


def verified_completed_file(path: Path, journal: Path, *, peer: str, mid: str) -> tuple[int, str]:
    """断在结果登记前时，用完整分片提交日志核验已入库文件。"""
    lines = journal.read_text(encoding='utf-8').splitlines()
    if not lines:
        raise RuntimeError('缺少已提交分片记录')
    header = json.loads(lines[0])
    identity = header.get('identity', {})
    size, chunk_size = header.get('size'), header.get('chunk_size')
    if (header.get('kind') != 'identity' or not isinstance(identity, dict)
            or str(identity.get('peer')) != peer or str(identity.get('mid')) != mid
            or not isinstance(size, int) or size <= 0
            or chunk_size not in (LEGACY_CHUNK_SIZE, CHUNK_SIZE)
            or path.stat().st_size != size):
        raise RuntimeError('已入库文件与分片身份或大小不符')
    offset = 0
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for line in lines[1:]:
            event = json.loads(line)
            length = min(chunk_size, size - offset)
            data = stream.read(length)
            if (offset >= size or event.get('kind') != 'chunk' or event.get('offset') != offset
                    or event.get('length') != length or len(data) != length
                    or hashlib.sha256(data).hexdigest() != event.get('sha256')):
                raise RuntimeError('已入库文件与分片提交记录不符')
            digest.update(data)
            offset += length
    if offset != size:
        raise RuntimeError('已入库文件的分片提交记录未覆盖全部字节')
    return size, digest.hexdigest()


def _sync(stream) -> None:
    stream.flush()
    os.fsync(stream.fileno())


def _line(value: dict) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True) + '\n').encode('utf-8')


class PartialFile:
    """日志先记录身份，数据先落盘，提交记录后才算已核验前缀。"""

    def __init__(self, directory: Path, key: str, identity: dict, size: int):
        self.directory = directory
        self.payload = directory / f'{key}.part'
        self.journal = directory / f'{key}.jsonl'
        self.identity = identity
        self.size = size
        self.offset = 0
        self.opened = False
        self.chunk_size = CHUNK_SIZE

    def open(self) -> int:
        self.directory.mkdir(parents=True, exist_ok=True)
        has_payload, has_journal = self.payload.exists(), self.journal.exists()
        if has_payload != has_journal:
            raise RuntimeError(f'续传数据与记录不成对：{self.payload} / {self.journal}')
        if not has_payload:
            with self.payload.open('xb') as stream:
                _sync(stream)
            with self.journal.open('xb') as stream:
                stream.write(_line({'kind': 'identity', 'identity': self.identity,
                                    'size': self.size, 'chunk_size': self.chunk_size}))
                _sync(stream)
            self.opened = True
            return 0
        raw = self.journal.read_bytes()
        lines = raw.splitlines(keepends=True)
        if not lines or not lines[0].endswith(b'\n'):
            raise RuntimeError(f'续传身份记录不完整：{self.journal}')
        try:
            header = json.loads(lines[0])
        except (ValueError, UnicodeError) as exc:
            raise RuntimeError(f'续传身份记录损坏：{self.journal}') from exc
        if (header.get('kind') != 'identity' or header.get('identity') != self.identity
                or header.get('size') != self.size
                or header.get('chunk_size') not in (LEGACY_CHUNK_SIZE, CHUNK_SIZE)):
            raise RuntimeError(f'续传身份或大小已变化，拒绝混接：{self.journal}')
        self.chunk_size = header['chunk_size']
        valid_lines = [lines[0]]
        with self.payload.open('rb') as payload:
            for index, line in enumerate(lines[1:], 1):
                if not line.endswith(b'\n') and index == len(lines) - 1:
                    break  # 断电时未写完的最后一条记录不算提交。
                try:
                    record = json.loads(line)
                except (ValueError, UnicodeError) as exc:
                    raise RuntimeError(f'续传提交记录损坏：{self.journal}:{index + 1}') from exc
                length = record.get('length')
                if (record.get('kind') != 'chunk' or record.get('offset') != self.offset
                        or not isinstance(length, int) or length != min(self.chunk_size, self.size - self.offset)
                        or self.offset >= self.size):
                    raise RuntimeError(f'续传提交边界无效：{self.journal}:{index + 1}')
                data = payload.read(length)
                if len(data) != length or hashlib.sha256(data).hexdigest() != record.get('sha256'):
                    raise RuntimeError(f'已提交分片损坏：{self.payload}，偏移 {self.offset}')
                self.offset += length
                valid_lines.append(line)
        if len(valid_lines) != len(lines):
            backup = self.journal.with_name(self.journal.name + f'.torn-{uuid.uuid4().hex}')
            self.journal.rename(backup)
            with self.journal.open('xb') as stream:
                stream.writelines(valid_lines)
                _sync(stream)
        actual = self.payload.stat().st_size
        if actual < self.offset or actual > self.size or actual - self.offset > self.chunk_size:
            raise RuntimeError(f'续传文件长度与提交边界不符：{actual} / {self.offset}')
        self.opened = True
        return self.offset

    def append(self, data: bytes) -> None:
        expected = min(self.chunk_size, self.size - self.offset)
        if len(data) != expected or self.payload.stat().st_size != self.offset:
            raise RuntimeError(f'分片长度或写入位置错误：{len(data)} / {expected} / {self.offset}')
        with self.payload.open('ab') as stream:
            stream.write(data)
            _sync(stream)
        with self.journal.open('ab') as stream:
            stream.write(_line({'kind': 'chunk', 'offset': self.offset, 'length': len(data),
                                'sha256': hashlib.sha256(data).hexdigest()}))
            _sync(stream)
        self.offset += len(data)

    async def reconcile_tail(self, fetch: Callable[[int, int], Awaitable[bytes]],
                             timeout: float = 45) -> None:
        extra = self.payload.stat().st_size - self.offset
        if extra <= 0:
            return
        wanted = min(self.chunk_size, self.size - self.offset)
        data = await asyncio.wait_for(fetch(self.offset, self.chunk_size), timeout=timeout)
        if len(data) != wanted:
            raise RuntimeError(f'未提交分片重取长度错误：{len(data)} / {wanted}')
        with self.payload.open('rb') as stream:
            stream.seek(self.offset)
            prefix = stream.read(extra)
        if prefix != data[:extra]:
            raise RuntimeError(f'未提交分片与 Telegram 不一致：{self.payload}，偏移 {self.offset}')
        with self.payload.open('ab') as stream:
            stream.write(data[extra:])
            _sync(stream)
        with self.journal.open('ab') as stream:
            stream.write(_line({'kind': 'chunk', 'offset': self.offset, 'length': len(data),
                                'sha256': hashlib.sha256(data).hexdigest()}))
            _sync(stream)
        self.offset += len(data)

    def finalize(self, final: Path) -> tuple[int, str]:
        if self.offset != self.size or self.payload.stat().st_size != self.size:
            raise RuntimeError(f'文件未完整，不能入库：{self.offset} / {self.size}')
        # 再读一遍已落盘文件，避免只凭写入时的内存字节入库。
        verified = PartialFile(self.directory, self.payload.stem, self.identity, self.size)
        if verified.open() != self.size:
            raise RuntimeError('最终分片核验未覆盖全部字节')
        digest = hashlib.sha256()
        with self.payload.open('rb') as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(block)
        final.parent.mkdir(parents=True, exist_ok=True)
        if final.exists():
            raise RuntimeError(f'目标文件已存在，未覆盖：{final}')
        if self.payload.stat().st_dev != final.parent.stat().st_dev:
            raise RuntimeError('暂存与目标不在同一卷，无法保证原子入库')
        self.payload.rename(final)
        return self.size, digest.hexdigest()


async def download_parts(partial: PartialFile, fetch: Callable[[int, int], Awaitable[bytes]],
                         progress: Callable[[int, int], None] | None = None,
                         *, fetch_timeout: float = 45) -> int:
    """仅在数据和提交记录均落盘后推进偏移；重试从最近提交边界开始。"""
    resumed = partial.offset if partial.opened else partial.open()
    await partial.reconcile_tail(fetch, timeout=fetch_timeout)

    async def fetch_one(offset: int, wanted: int) -> bytes:
        for attempt in range(3):
            try:
                data = await asyncio.wait_for(fetch(offset, partial.chunk_size),
                                              timeout=fetch_timeout)
                if len(data) != wanted:
                    raise RuntimeError(f'Telegram 分片长度不符：{len(data)} / {wanted}')
                return data
            except Exception as exc:
                if attempt == 2:
                    raise RuntimeError(f'分片偏移 {offset} 连续失败：{type(exc).__name__}: {exc}') from exc
                await asyncio.sleep(attempt + 1)
        raise AssertionError('分片重试应已返回或抛错')

    while partial.offset < partial.size:
        offsets = list(range(partial.offset,
                             min(partial.size, partial.offset + 4 * partial.chunk_size),
                             partial.chunk_size))
        tasks = [asyncio.create_task(fetch_one(offset, min(partial.chunk_size, partial.size - offset)))
                 for offset in offsets]
        try:
            for task in tasks:
                data = await task
                partial.append(data)
                if progress:
                    progress(partial.offset, partial.size)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    return resumed
