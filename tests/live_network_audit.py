"""切断隔离 Chrome 的真实 TCP 代理连接，验收同进程恢复；不影响系统网络。"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from contextlib import suppress
from pathlib import Path

from playwright.async_api import async_playwright

from telegg.browser import fetch_resume_part, open_group, server_media_source, wait_for_login
from telegg.group_menu import parse_groups
from telegg.local_config import CONFIG_FILE, load_paths
from telegg.resumable import CHUNK_SIZE, PartialFile, download_parts
from telegg.scan_record import ScanRecord
from matrix_harness.isolation import sha, write_json


class CuttingProxy:
    """只转发加密流，不记录流量内容；中断仅限连接到此代理的浏览器。"""

    def __init__(self, upstream_port: int):
        self.upstream_port = upstream_port
        self.blocked = False
        self.streams = set()
        self.tasks = set()
        self.cut_connections = self.denied_connections = self.bytes_forwarded = 0

    async def __aenter__(self):
        self.server = await asyncio.start_server(self.accept, '127.0.0.1', 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *_):
        self.server.close()
        await self.server.wait_closed()
        self.cut()
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    def cut(self):
        self.blocked = True
        self.cut_connections += len(self.streams) // 2
        for stream in list(self.streams):
            stream.transport.abort()

    async def pipe(self, reader, writer):
        while data := await reader.read(64 * 1024):
            if self.blocked:
                return
            writer.write(data)
            await writer.drain()
            self.bytes_forwarded += len(data)

    async def accept(self, reader, writer):
        task = asyncio.current_task()
        self.tasks.add(task)
        remote = None
        pumps = []
        try:
            header = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 10)
            if self.blocked:
                self.denied_connections += 1
                writer.write(b'HTTP/1.1 502 Offline test\r\nContent-Length: 0\r\n\r\n')
                await writer.drain()
                return
            target, remote = await asyncio.open_connection('127.0.0.1', self.upstream_port)
            self.streams.update((writer, remote))
            remote.write(header)
            await remote.drain()
            pumps = [asyncio.create_task(self.pipe(reader, remote)),
                     asyncio.create_task(self.pipe(target, writer))]
            await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
        except (OSError, asyncio.IncompleteReadError, TimeoutError):
            pass
        finally:
            for pump in pumps:
                pump.cancel()
            await asyncio.gather(*pumps, return_exceptions=True)
            for stream in (writer, remote):
                if stream:
                    self.streams.discard(stream)
                    stream.close()
                    with suppress(Exception):
                        await stream.wait_closed()
            self.tasks.discard(task)


async def audit(suite: Path, group_number: int, upstream_port: int):
    paths = load_paths()
    groups = parse_groups(json.loads(CONFIG_FILE.read_text(encoding='utf-8-sig'))['groups'])
    group = groups[group_number - 1]
    record = ScanRecord(paths['scan_records'] / f'{group.identity}.jsonl')
    candidates = [(mid, Path(value['detail'])) for mid, value in record.results.items()
                  if value.get('status') in ('downloaded', 'skipped', 'relocated')
                  and value.get('detail') and Path(value['detail']).is_file()
                  and 12 * CHUNK_SIZE < Path(value['detail']).stat().st_size < 40 * CHUNK_SIZE]
    if not candidates:
        raise ValueError('该群没有 12–40 MiB 已验收样本，未启动网络测试')
    mid, original = min(candidates, key=lambda pair: pair[1].stat().st_size)
    work = suite / 'network-recovery'
    work.mkdir()
    committed, restored = asyncio.Event(), asyncio.Event()
    observations = {'python_pid': os.getpid(), 'websocket_closed': 0, 'request_failed': 0}
    async with CuttingProxy(upstream_port) as proxy, async_playwright() as p:
        context = await p.chromium.launch_persistent_context(str(suite / 'profile'),
            channel='chrome', headless=True, proxy={'server': f'http://127.0.0.1:{proxy.port}'})
        outage = None
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            def websocket_opened(socket):
                socket.on('close', lambda: observations.__setitem__('websocket_closed', observations['websocket_closed'] + 1))
            page.on('websocket', websocket_opened)
            page.on('requestfailed', lambda _: observations.__setitem__('request_failed', observations['request_failed'] + 1))
            await page.goto('https://web.telegram.org/k/', wait_until='domcontentloaded')
            await wait_for_login(page, timeout=30_000)
            await open_group(page, group.url)
            source = await server_media_source(page, int(group.identity), mid)
            identity = {key: source[key] for key in ('peer', 'mid', 'kind', 'id', 'dc', 'thumb')}
            partial = PartialFile(work / 'partials', mid, identity, source['size'])

            async def disconnect():
                await committed.wait()
                observations['cut_offset'] = partial.offset
                proxy.cut()
                # 不读取响应正文，只确认代理阻断期间真实请求无法成功。
                observations['offline_request_failed'] = await page.evaluate('''async () => {
                    try { await fetch('https://web.telegram.org/k/?network-audit=' + Date.now(),
                        {cache:'no-store'}); return false; } catch { return true; }
                }''')
                await asyncio.sleep(6)
                proxy.blocked = False
                restored.set()

            async def fetch(offset, limit):
                # 首四个并发分片是真实请求；后续请求在实际断开时进入生产恢复流程。
                if offset >= 4 * CHUNK_SIZE and not committed.is_set():
                    await committed.wait()
                return await fetch_resume_part(page, source, offset, limit)

            outage = asyncio.create_task(disconnect())
            await asyncio.wait_for(download_parts(partial, fetch,
                lambda *_: committed.set()), timeout=125)
            await outage
            size, digest = partial.finalize(work / 'output/sample.bin')
            observations.update(size=size, hash_match=digest == sha(original),
                same_process=observations['python_pid'] == os.getpid() and not page.is_closed(),
                restored=restored.is_set(), cut_connections=proxy.cut_connections,
                denied_connections=proxy.denied_connections, bytes_forwarded=proxy.bytes_forwarded)
        finally:
            if outage and not outage.done():
                outage.cancel()
                await asyncio.gather(outage, return_exceptions=True)
            await context.close()
    observations['passed'] = all(observations.get(key) for key in
        ('hash_match', 'same_process', 'restored', 'offline_request_failed', 'cut_connections'))
    write_json(work / 'result.json', observations)
    print(json.dumps(observations), flush=True)
    return 0 if observations['passed'] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--suite', type=Path, required=True)
    parser.add_argument('--source-group', type=int, default=3)
    parser.add_argument('--upstream-port', type=int, default=10808)
    args = parser.parse_args()
    suite = args.suite.resolve()
    root = Path(__file__).resolve().parents[1] / '~temp/remaining-live'
    if not suite.is_relative_to(root.resolve()) or not (suite / 'profile').is_dir():
        parser.error('suite 必须是项目 ~temp/remaining-live 下的既有隔离登录副本')
    raise SystemExit(asyncio.run(audit(suite, args.source_group, args.upstream_port)))


if __name__ == '__main__':
    main()
