"""额外文件检查的预测样例：服务器删除/替换、重复、保留版本及只读失败。"""

import asyncio
import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from telegg.errors import RemoteFileError
from telegg.extra_files import (check_extra_files, classify_files, current_messages,
                                extra_paths, group_roots, loose_storage_files, storage_groups)
from telegg.failure_notes import write_failure_note


URL = 'https://web.telegram.org/k/#-1000000005'


def row(mid, media_id='', has_text=False, kind='message'):
    return {'id': mid, 'type': kind, 'media_id': media_id,
            'has_text': has_text, 'peer_id': 1000000005}


class ExtraFilesTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(__file__).resolve().parents[1] / '~temp/tests/extra-files' / uuid4().hex
        self.output, self.staging = self.base / 'output', self.base / 'staging'
        self.name = '中性测试 [-1000000005]'
        self.segment = '[From_2026-01-01-m4294967338]'
        self.root = self.output / self.name
        self.folder = self.root / self.segment
        self.folder.mkdir(parents=True)

    def file(self, name, folder=None):
        path = (folder or self.folder) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'neutral fixture')
        return path

    def audit(self, rows):
        page = AsyncMock()
        page.evaluate.return_value = rows
        with (patch('telegg.browser.open_group', new_callable=AsyncMock, return_value='中性测试'),
              patch('telegg.extra_files.load_paths', return_value={'scan_records': self.base / 'scans'})):
            with redirect_stdout(io.StringIO()):
                return asyncio.run(check_extra_files(page, URL, self.output, self.staging))

    def test_deleted_replaced_and_unknown_files_are_paths_only_and_read_only(self):
        valid = self.file('2026-01-01-m4294967338_1_777.bin')
        deleted = self.file('2026-01-01-m4294967339_1_778.bin')
        replaced = self.file('2026-01-01-m4294967340_1_779.bin')
        unknown = self.file('other.bin')
        self.file('2026-01-01-m4294967338_caption.txt')
        self.file('段内清单.json')
        before = {p: p.read_bytes() for p in self.base.rglob('*') if p.is_file()}
        extras = self.audit([row(44, '999'), row(42, '777', True)])
        self.assertEqual(extras, sorted([deleted, replaced, unknown]))
        self.assertNotIn(valid, extras)
        self.assertEqual(before, {p: p.read_bytes() for p in self.base.rglob('*') if p.is_file()})

    def test_plain_duplicates_across_output_and_staging_report_both(self):
        name = '2026-01-01-m4294967338_1_777.bin'
        a = self.file(name)
        b = self.file(name, self.staging / self.name / self.segment)
        self.assertEqual(self.audit([row(42, '777')]), sorted([a, b]))

    def test_continuous_media_versions_keep_history_and_check_latest(self):
        a = self.file('2026-01-01-m4294967338_1_777.v1.bin')
        b = self.file('2026-01-01-m4294967338_1_778.v2.bin')
        self.assertEqual(self.audit([row(42, '778')]), [])
        self.assertEqual(self.audit([row(42, '779')]), [b])
        self.assertEqual(self.audit([]), sorted([a, b]))

    def test_owned_failure_note_is_not_a_media_duplicate_or_extra(self):
        target = self.folder / '2026-01-01-m4294967338_1_777.bin'
        write_failure_note(target, '4294967338', 100, RemoteFileError(500, 'UNKNOWN'))
        self.assertEqual(self.audit([row(42, '777')]), [])

    def test_only_orphan_staged_helper_is_reported(self):
        parent = '[From_2026-01-01-m4294967338] 关联组'
        a = self.file('关联清单.json', self.root / parent)
        b = self.file('关联清单.json', self.staging / self.name / parent)
        self.assertEqual(self.audit([]), [b])
        self.file('2026-01-01-m4294967338_1_777.bin', b.parent / self.segment)
        self.assertEqual(self.audit([row(42, '777')]), [])
        self.assertEqual(a.read_bytes(), b.read_bytes())

    def test_helper_historical_versions_are_preserved(self):
        self.file('段内清单.v1.json')
        self.assertEqual(self.audit([]), [])

    def test_numeric_caption_uses_recorded_text_path_not_media_name(self):
        text = self.file('2026-01-01-m4294967338_1_777.txt')
        local = classify_files([text], {text})
        self.assertEqual(extra_paths(local, {42: row(42, '888', True)}), [])
        self.assertEqual(extra_paths(classify_files([text]), {42: row(42, '888', True)}), [text])

    def test_server_failure_never_reports_deleted_files(self):
        self.file('2026-01-01-m4294967338_1_777.bin')
        page = AsyncMock()
        page.evaluate.side_effect = RuntimeError('synthetic network failure')
        with (patch('telegg.browser.open_group', new_callable=AsyncMock),
              patch('telegg.extra_files.load_paths', return_value={'scan_records': self.base / 'scans'})):
            with redirect_stdout(io.StringIO()), self.assertRaisesRegex(RuntimeError, 'network'):
                asyncio.run(check_extra_files(page, URL, self.output, self.staging))

    def test_server_pagination_covers_local_range_and_rejects_repeated_page(self):
        page = AsyncMock()
        page.evaluate.side_effect = [[row(100, '1')], [row(50, '2')], []]
        found = asyncio.run(current_messages(page, -1000000005, {42, 50}))
        self.assertEqual(set(found), {50})
        self.assertEqual(page.evaluate.await_count, 3)
        page.evaluate.side_effect = [[row(100)], [row(100)]]
        with self.assertRaisesRegex(RuntimeError, '分页'):
            asyncio.run(current_messages(page, -1000000005, {42}))

    def test_service_rows_advance_and_foreign_peer_is_rejected(self):
        page = AsyncMock()
        page.evaluate.side_effect = [[row(50, kind='messageService')], [row(42, '7')]]
        self.assertEqual(set(asyncio.run(current_messages(page, -1000000005, {42}))), {42})
        other = row(42, '7'); other['peer_id'] = 999
        page.evaluate.side_effect = [[other]]
        with self.assertRaisesRegex(RuntimeError, '群组'):
            asyncio.run(current_messages(page, -1000000005, {42}))

    def test_concurrent_change_invalidates_entire_result(self):
        self.file('2026-01-01-m4294967338_1_777.bin')
        page = AsyncMock()
        async def changed(*args):
            self.file('appeared.bin')
            return [row(42, '777')]
        page.evaluate.side_effect = changed
        with (patch('telegg.browser.open_group', new_callable=AsyncMock),
              patch('telegg.extra_files.load_paths', return_value={'scan_records': self.base / 'scans'})):
            with redirect_stdout(io.StringIO()), self.assertRaisesRegex(RuntimeError, '发生变化'):
                asyncio.run(check_extra_files(page, URL, self.output, self.staging))

    def test_archive_directories_are_outside_scan(self):
        self.file('held.bin', self.root / '~archive')
        self.file('下载异常说明.jsonl', self.root / '~archive')
        self.assertEqual(self.audit([]), [])

    def test_stray_part_inside_download_tree_is_reported(self):
        path = self.file('2026-01-01-m4294967338_1_777.bin.part')
        self.assertEqual(self.audit([row(42, '777')]), [path])

    def test_linked_download_root_is_rejected_before_traversal(self):
        with patch('telegg.extra_files._is_link', side_effect=lambda p: p == self.output.absolute()):
            with self.assertRaisesRegex(RuntimeError, '链接'):
                group_roots(self.output, self.staging, '-1000000005')

    def partial_files(self, *, mid='4294967338', media_id='777'):
        root = self.base / 'partials' / '-1000000005'
        root.mkdir(parents=True, exist_ok=True)
        identity = {'peer': '-1000000005', 'mid': mid, 'id': media_id}
        journal = root / f'{mid}.jsonl'
        journal.write_text(json.dumps({'kind': 'identity', 'identity': identity,
                                     'size': 100, 'chunk_size': 1048576})+'\n', encoding='utf-8')
        verified = root / f'{mid}.verified.jsonl'
        verified.write_text(json.dumps({'identity': identity, 'path': str(self.folder/'media.bin'),
                                       'size': 100, 'sha256': 'a'*64})+'\n', encoding='utf-8')
        payload = self.file(f'{mid}.part', root)
        return journal, verified, payload

    def audit_with_partials(self, rows):
        page = AsyncMock(); page.evaluate.return_value = rows
        with (patch('telegg.browser.open_group', new_callable=AsyncMock),
              patch('telegg.extra_files.load_paths', return_value={'scan_records': self.base/'scans'}),
              redirect_stdout(io.StringIO())):
            return asyncio.run(check_extra_files(page, URL, self.output, self.staging,
                                                 partials=self.base/'partials'))

    def test_normal_partials_and_completed_journals_are_not_extra(self):
        journal, verified, payload = self.partial_files()
        before = {p: p.read_bytes() for p in (journal, verified, payload)}
        self.assertEqual(self.audit_with_partials([row(42, '777')]), [])
        self.assertEqual(before, {p: p.read_bytes() for p in before})
        payload.rename(self.base/'held-payload.bin')
        self.assertEqual(self.audit_with_partials([row(42, '777')]), [])

    def test_deleted_or_replaced_partial_identity_is_reported(self):
        files = self.partial_files()
        self.assertEqual(self.audit_with_partials([]), sorted(files))
        self.assertEqual(self.audit_with_partials([row(42, '888')]), sorted(files))

    def test_orphan_payload_without_journal_is_reported_without_reading_bytes(self):
        root = self.base/'partials'/'-1000000005'
        payload = self.file('4294967338.part', root)
        self.assertEqual(self.audit_with_partials([row(42, '777')]), [payload])

    def test_partial_historical_version_and_torn_journal_are_retained(self):
        journal, verified, payload = self.partial_files()
        older = journal.with_name('4294967338.v1.jsonl')
        older.write_text(journal.read_text(encoding='utf-8').replace('777', '666'), encoding='utf-8')
        torn = journal.with_name(journal.name+'.torn-abc123')
        torn.write_bytes(journal.read_bytes())
        self.assertEqual(self.audit_with_partials([row(42, '777')]), [])

    def test_original_unnumbered_journal_belongs_to_retained_first_media_version(self):
        self.partial_files()
        self.file('2026-01-01-m4294967338_1_777.v1.bin')
        self.file('2026-01-01-m4294967338_1_888.v2.bin')
        self.assertEqual(self.audit_with_partials([row(42, '888')]), [])

    def test_unrecognized_partial_header_only_reports_its_path(self):
        root = self.base/'partials'/'-1000000005'
        path = self.file('4294967338.jsonl', root)
        for content in ('[]', 'null', '{}', '{broken'):
            with self.subTest(content=content):
                path.write_text(content, encoding='utf-8')
                self.assertEqual(self.audit_with_partials([row(42, '777')]), [path])

    def test_entire_downloading_and_output_roots_include_loose_files(self):
        staging = self.base/'downloading/groups'
        partials = self.base/'downloading/partials'
        loose = [self.file('extra.bin', self.output),
                 self.file('extra.bin', staging.parent),
                 self.file('extra.bin', staging),
                 self.file('extra.bin', partials),
                 self.file('other.bin', staging.parent/'unknown')]
        self.file('media.bin', staging/self.name)
        self.file('4294967338.jsonl', partials/'-1000000005')
        self.file('archived.bin', staging.parent/'~archive')
        self.assertEqual(loose_storage_files(self.output, staging, partials), sorted(loose))
        self.assertEqual(storage_groups(self.output, staging, partials), {'-1000000005'})


if __name__ == '__main__':
    unittest.main()
