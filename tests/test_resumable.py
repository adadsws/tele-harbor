"""验证断点边界、崩溃尾部及正式文件的原子入库条件。"""

import asyncio
import os
import unittest
import uuid
from pathlib import Path

from telegg.resumable import (CHUNK_SIZE, LEGACY_CHUNK_SIZE, PartialFile,
                              download_parts, verified_completed_file)
from telegg.local_config import load_paths

TEST_ROOT = load_paths()['test_artifacts']


class ResumeTests(unittest.TestCase):
    def setUp(self):
        self.root = TEST_ROOT / f'resume-{uuid.uuid4().hex}'
        self.content = bytes(range(256)) * (CHUNK_SIZE // 256 * 2) + b'end'
        self.identity = {'peer': '-1', 'mid': '42', 'kind': 'document',
                         'id': '777', 'dc': 1, 'thumb': ''}

    def partial(self):
        return PartialFile(self.root / 'partials', '42', self.identity, len(self.content))

    def test_restart_uses_committed_offset_and_only_then_publishes(self):
        first = self.partial()
        calls = []

        async def interrupted(offset, limit):
            calls.append(offset)
            if offset:
                raise RuntimeError('断网')
            return self.content[offset:offset + limit]

        with self.assertRaisesRegex(RuntimeError, '连续失败'):
            asyncio.run(download_parts(first, interrupted))
        self.assertEqual(first.offset, CHUNK_SIZE)
        final = self.root / 'downloads' / 'file.bin'
        self.assertFalse(final.exists())
        second = self.partial()
        resumed_calls = []

        async def resumed(offset, limit):
            resumed_calls.append(offset)
            return self.content[offset:offset + limit]

        self.assertEqual(asyncio.run(download_parts(second, resumed)), CHUNK_SIZE)
        self.assertEqual(resumed_calls[0], CHUNK_SIZE)
        size, digest = second.finalize(final)
        self.assertEqual(size, len(self.content))
        self.assertEqual(final.read_bytes(), self.content)
        self.assertEqual(len(digest), 64)
        self.assertFalse(second.payload.exists())

    def test_completed_file_recovery_verifies_every_committed_chunk(self):
        partial = self.partial()

        async def fetch(offset, limit):
            return self.content[offset:offset + limit]

        asyncio.run(download_parts(partial, fetch))
        final = self.root / 'downloads' / 'file.bin'
        size, digest = partial.finalize(final)
        self.assertEqual(verified_completed_file(final, partial.journal,
                                                 peer='-1', mid='42'), (size, digest))
        with final.open('r+b') as stream:
            stream.seek(CHUNK_SIZE + 1)
            stream.write(b'X')
        with self.assertRaisesRegex(RuntimeError, '分片提交记录不符'):
            verified_completed_file(final, partial.journal, peer='-1', mid='42')

    def test_uncommitted_tail_is_compared_with_source_before_resume(self):
        partial = self.partial()
        partial.open()
        with partial.payload.open('ab') as stream:
            stream.write(self.content[:8192])
            stream.flush()
            os.fsync(stream.fileno())

        async def fetch(offset, limit):
            return self.content[offset:offset + limit]

        self.assertEqual(asyncio.run(download_parts(self.partial(), fetch)), 0)
        final = self.root / 'downloads' / 'file.bin'
        completed = self.partial()
        completed.open()
        completed.finalize(final)
        self.assertEqual(final.read_bytes(), self.content)

    def test_corrupted_committed_chunk_and_identity_change_never_publish(self):
        partial = self.partial()
        partial.open()
        partial.append(self.content[:CHUNK_SIZE])
        with partial.payload.open('r+b') as stream:
            stream.write(b'X')
            stream.flush()
            os.fsync(stream.fileno())
        with self.assertRaisesRegex(RuntimeError, '分片损坏'):
            self.partial().open()
        changed = PartialFile(partial.directory, '42', {**self.identity, 'id': '778'},
                              len(self.content))
        with self.assertRaisesRegex(RuntimeError, '身份或大小'):
            changed.open()
        self.assertFalse((self.root / 'downloads' / 'file.bin').exists())

    def test_existing_256k_journal_remains_resumable_after_size_upgrade(self):
        partial = self.partial()
        partial.chunk_size = LEGACY_CHUNK_SIZE
        partial.open()
        partial.append(self.content[:LEGACY_CHUNK_SIZE])
        reopened = self.partial()
        self.assertEqual(reopened.open(), LEGACY_CHUNK_SIZE)
        self.assertEqual(reopened.chunk_size, LEGACY_CHUNK_SIZE)

        async def fetch(offset, limit):
            self.assertEqual(limit, LEGACY_CHUNK_SIZE)
            return self.content[offset:offset + limit]

        self.assertEqual(asyncio.run(download_parts(reopened, fetch)), LEGACY_CHUNK_SIZE)
        final = self.root / 'downloads' / 'file.bin'
        reopened.finalize(final)
        self.assertEqual(final.read_bytes(), self.content)

    def test_existing_destination_is_not_overwritten(self):
        partial = self.partial()

        async def fetch(offset, limit):
            return self.content[offset:offset + limit]

        asyncio.run(download_parts(partial, fetch))
        final = self.root / 'downloads' / 'file.bin'
        final.parent.mkdir(parents=True)
        final.write_bytes(b'user-file')
        with self.assertRaisesRegex(RuntimeError, '已存在'):
            partial.finalize(final)
        self.assertEqual(final.read_bytes(), b'user-file')
        self.assertTrue(partial.payload.exists())

    def test_changed_expected_size_is_rejected(self):
        self.partial().open()
        changed = PartialFile(self.root / 'partials', '42', self.identity,
                              len(self.content) + 1)
        with self.assertRaisesRegex(RuntimeError, '身份或大小'):
            changed.open()

    def test_hung_source_times_out_without_publishing_and_can_resume(self):
        partial = self.partial()
        calls = 0

        async def hung(offset, limit):
            nonlocal calls
            calls += 1
            await asyncio.Event().wait()

        with self.assertRaisesRegex(RuntimeError, '连续失败'):
            asyncio.run(download_parts(partial, hung, fetch_timeout=0.01))
        self.assertGreaterEqual(calls, 3)
        self.assertEqual(partial.offset, 0)
        self.assertFalse((self.root / 'downloads' / 'file.bin').exists())

        async def resumed(offset, limit):
            return self.content[offset:offset + limit]

        reopened = self.partial()
        self.assertEqual(asyncio.run(download_parts(reopened, resumed)), 0)
        final = self.root / 'downloads' / 'file.bin'
        reopened.finalize(final)
        self.assertEqual(final.read_bytes(), self.content)

    def test_torn_last_journal_line_is_preserved_and_repaired(self):
        partial = self.partial()
        partial.open()
        partial.append(self.content[:CHUNK_SIZE])
        with partial.journal.open('ab') as stream:
            stream.write(b'{"kind":"chunk"')
            stream.flush()
            os.fsync(stream.fileno())
        reopened = self.partial()
        self.assertEqual(reopened.open(), CHUNK_SIZE)
        self.assertEqual(len(list(partial.directory.glob('42.jsonl.torn-*'))), 1)

        async def fetch(offset, limit):
            return self.content[offset:offset + limit]

        asyncio.run(download_parts(reopened, fetch))
        final = self.root / 'downloads' / 'file.bin'
        reopened.finalize(final)
        self.assertEqual(final.read_bytes(), self.content)


if __name__ == '__main__':
    unittest.main()
