"""长文件只读核验必须显示中途字节、保持事件循环响应并能取消。"""
import asyncio
import hashlib
import io
import threading
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from telegg.review_progress import file_stamp,hash_chunks,verify_media,with_progress


class ReviewProgressTests(unittest.TestCase):
    def fixture(self):
        base=Path(__file__).resolve().parents[1]/'~temp/tests/review-progress'/uuid4().hex
        base.mkdir(parents=True);media=base/'neutral.bin';media.write_bytes(b'abcd'*(1024*1024))
        expected={media:('42','media')};stamps={media:file_stamp(media)}
        snapshots={'42':{'sha256':hashlib.sha256(media.read_bytes()).hexdigest()}}
        return base,media,expected,stamps,snapshots

    def test_long_file_reports_intermediate_bytes_without_blocking_loop(self):
        base,media,expected,stamps,snapshots=self.fixture();output=io.StringIO();ticks=[]
        def slow(path,stop,advance):
            def step(count):advance(count);time.sleep(.003)
            return hash_chunks(path,stop,step,chunk_size=65536)
        async def frequent(work,status):return await with_progress(work,status,interval=.01)
        async def run():
            task=asyncio.create_task(verify_media(expected,set(),set(),stamps,base,'-1',snapshots=snapshots))
            while not task.done():ticks.append(1);await asyncio.sleep(.01)
            return await task
        with (redirect_stdout(output),patch('telegg.review_progress.hash_chunks',side_effect=slow),
              patch('telegg.review_progress.with_progress',side_effect=frequent)):
            cache=asyncio.run(run())
        self.assertGreater(len(ticks),3);self.assertIn(media,cache)
        lines=output.getvalue().splitlines()
        self.assertGreater(sum('SHA-256 0/1' in line and '0 B/4.0 MiB' not in line for line in lines),1)
        self.assertIn('SHA-256 1/1',output.getvalue())
        self.assertEqual(file_stamp(media),stamps[media])

    def test_cancellation_stops_reader_and_never_reports_complete(self):
        base,media,expected,stamps,snapshots=self.fixture();output=io.StringIO();finished=threading.Event()
        def slow(path,stop,advance):
            def step(count):advance(count);time.sleep(.01)
            try:return hash_chunks(path,stop,step,chunk_size=65536)
            finally:finished.set()
        async def run():
            task=asyncio.create_task(verify_media(expected,set(),set(),stamps,base,'-1',snapshots=snapshots))
            await asyncio.sleep(.03);task.cancel()
            with self.assertRaises(asyncio.CancelledError):await task
        with redirect_stdout(output),patch('telegg.review_progress.hash_chunks',side_effect=slow):asyncio.run(run())
        self.assertTrue(finished.is_set());self.assertNotIn('校验完成',output.getvalue())
        self.assertEqual(file_stamp(media),stamps[media])

    def test_final_refresh_reuses_only_this_run_verified_media(self):
        base,media,expected,stamps,snapshots=self.fixture()
        with redirect_stdout(io.StringIO()):
            first=asyncio.run(verify_media(expected,set(),set(),stamps,base,'-1',snapshots=snapshots))
        output=io.StringIO()
        with redirect_stdout(output),patch('telegg.review_progress.hash_chunks',side_effect=AssertionError('rehashed')):
            second=asyncio.run(verify_media(expected,set(),set(),stamps,base,'-1',snapshots=snapshots,
                                             previous=SimpleNamespace(hash_cache=first)))
        self.assertEqual(first,second);self.assertIn('免重复读取 1 个',output.getvalue())
