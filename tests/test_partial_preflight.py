"""关联组实测回归：预检坏断点前不得移动完好成员；有效尾部预检不提交字节。"""
import asyncio
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

from matrix_harness.isolation import files, protected_state
from telegg.errors import LocalDataError, RemoteFileError
from telegg.partial_preflight import validate_pending_media
from telegg.resumable import CHUNK_SIZE, PartialFile


class PartialPreflightTests(unittest.TestCase):
    def fixture(self):
        root = Path(__file__).resolve().parents[1]/'~temp/tests/partial-preflight'/uuid4().hex
        source = {'peer': '-1', 'mid': '42', 'kind': 'document', 'id': '777', 'dc': 1, 'thumb': ''}
        content = b'x'*CHUNK_SIZE + b'y'*CHUNK_SIZE + b'end'
        partial = PartialFile(root/'partials', '42', source, len(content))
        partial.open(); partial.append(content[:CHUNK_SIZE])
        source = {**source, 'size': len(content)}
        items = [SimpleNamespace(content_types=(('42', 'video'),))]
        record = SimpleNamespace(results={})
        return root, source, content, partial, items, record

    def run_preflight(self, source, content, partial, items, record, *, fetch=None):
        get_source = AsyncMock(return_value=source)
        fetch = fetch or AsyncMock(side_effect=lambda _page, _source, offset, limit: content[offset:offset+limit])
        asyncio.run(validate_pending_media(None, -1, items, record, {}, partial.directory, get_source, fetch))
        return get_source, fetch

    def test_bad_local_states_and_server_identity_are_read_only(self):
        for kind in ('record_ahead', 'orphan_payload', 'orphan_journal', 'identity', 'committed', 'tail'):
            with self.subTest(kind=kind):
                root, source, content, partial, items, record = self.fixture()
                if kind == 'record_ahead':
                    partial.append(content[CHUNK_SIZE:2*CHUNK_SIZE])
                    with partial.payload.open('r+b') as stream:
                        stream.truncate(CHUNK_SIZE)
                elif kind == 'orphan_payload':
                    partial.journal.rename(root/'held-journal')
                elif kind == 'orphan_journal':
                    partial.payload.rename(root/'held-payload')
                elif kind == 'identity':
                    source['id'] = '778'
                elif kind == 'committed':
                    with partial.payload.open('r+b') as stream:
                        stream.write(b'z')
                elif kind == 'tail':
                    with partial.payload.open('ab') as stream:
                        stream.write(b'z'*100)
                before = protected_state({'root': root}, list(files(root)))
                with self.assertRaises(LocalDataError):
                    self.run_preflight(source, content, partial, items, record)
                self.assertEqual(before, protected_state({'root': root}, list(files(root))))

    def test_valid_tail_and_torn_journal_are_not_repaired_during_preflight(self):
        root, source, content, partial, items, record = self.fixture()
        with partial.payload.open('ab') as stream:
            stream.write(content[CHUNK_SIZE:CHUNK_SIZE+100])
        with partial.journal.open('ab') as stream:
            stream.write(b'{"kind":"chunk"')
        before = protected_state({'root': root}, list(files(root)))
        _source, fetch = self.run_preflight(source, content, partial, items, record)
        fetch.assert_awaited_once()
        self.assertEqual(before, protected_state({'root': root}, list(files(root))))
        reopened = PartialFile(partial.directory, '42', {k:v for k,v in source.items() if k!='size'}, len(content))
        self.assertEqual(reopened.open(), CHUNK_SIZE)
        self.assertTrue(list(partial.directory.glob('*.torn-*')))

    def test_missing_completed_media_stops_before_remote_requests(self):
        _root, source, content, partial, items, record = self.fixture()
        record.results['42'] = {'status':'downloaded'}
        with self.assertRaisesRegex(LocalDataError, '已完成媒体缺失'):
            self.run_preflight(source, content, partial, items, record)

    def test_remote_file_failure_remains_in_normal_note_and_continue_path(self):
        _root, source, content, partial, items, record = self.fixture()
        with partial.payload.open('ab') as stream:
            stream.write(content[CHUNK_SIZE:CHUNK_SIZE+100])
        self.run_preflight(source, content, partial, items, record,
                           fetch=AsyncMock(side_effect=RemoteFileError(500, 'SYNTHETIC')))
