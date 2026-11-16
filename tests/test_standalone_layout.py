"""用户示例及预测变体：跨头像段前一相册、迁移冲突、中断续跑和说明归属。"""

import asyncio
import hashlib
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from telegg.album_context import batch_contexts, context_from_album, preceding_album
from telegg.errors import LocalDataError, RemoteFileError
from telegg.failure_notes import REGISTRY, registered_notes, write_failure_note
from telegg.layout import Message, destination, group_folder
from telegg.layout_migration import build_plan, execute_plan, stamp
from telegg.scan_record import FoundMedia, ScanRecord
from telegg.verification_manifest import read_snapshot, verified_digest


class StandaloneLayoutTests(unittest.TestCase):
    def test_requested_structure_and_first_group_boundary(self):
        album = Message('4294967396', '', 1700000000, 1, album_id='1234567890')
        single = Message('4294967406', '', 1700000001, 1)
        anchor = Message('4294967386', '', 1699999990, 1)
        self.assertEqual(destination(Path('out'), anchor, [album, single], single).name,
                         '2023-11-15-g1234567890后')
        self.assertEqual(destination(Path('out'), single, [single], single).name,
                         '2023-11-15-首个group前')

    def test_cross_avatar_and_day_context_uses_preceding_album_day(self):
        single = Message('30', '', 1700086400, 1)
        context = {'album_id': '123', 'day': '2023-11-15', 'previous_mid': '10'}
        self.assertEqual(destination(Path('out'), single, [single], single, standalone=context).name,
                         '2023-11-15-g123后')

    def test_preceding_album_checks_oldest_album_member_across_pages(self):
        def row(mid, album, date):
            return {'id': mid, 'album_id': album, 'date': date,
                    'type': 'message', 'peer_id': 1}
        page = AsyncMock()
        page.evaluate.side_effect = [[row(29, '', 1700000010), row(28, '123', 1700000001)],
                                     [row(27, '123', 1699913600), row(26, '122', 1699913599)]]
        value = asyncio.run(preceding_album(page, -1, '30'))
        self.assertEqual(value['album_id'], '123')
        self.assertEqual(value['previous_mid'], str((1 << 32)+27))
        self.assertEqual(value['day'], '2023-11-14')

    def test_unknown_old_side_never_becomes_first_group_before_on_error(self):
        page = AsyncMock(); page.evaluate.side_effect = RuntimeError('network unavailable')
        with self.assertRaisesRegex(RuntimeError, 'network'):
            asyncio.run(batch_contexts(page, -1, [Message('42', '', 1700000000, 1)], {}))
        page.evaluate.side_effect = None; page.evaluate.return_value = []
        self.assertEqual(asyncio.run(preceding_album(page, -1, '42'))['album_id'], '')

    def test_repeated_old_page_is_rejected(self):
        row = {'id': 40, 'album_id': '', 'date': 1700000000, 'type': 'message', 'peer_id': 1}
        page = AsyncMock(); page.evaluate.side_effect = [[row], [row]]
        with self.assertRaisesRegex(RuntimeError, '推进'):
            asyncio.run(preceding_album(page, -1, '42'))

    def test_album_folder_day_uses_minimum_server_date_when_ids_and_dates_disagree(self):
        page = AsyncMock()
        page.evaluate.return_value = [
            {'id': 29, 'album_id': '123', 'date': 1700000000, 'type': 'message', 'peer_id': 1},
            {'id': 28, 'album_id': '123', 'date': 1700086400, 'type': 'message', 'peer_id': 1},
            {'id': 27, 'album_id': '', 'date': 1699913600, 'type': 'message', 'peer_id': 1}]
        self.assertEqual(asyncio.run(preceding_album(page, -1, '30'))['day'], '2023-11-15')

    def fixture(self):
        base = Path(__file__).resolve().parents[1]/'~temp/tests/standalone-layout'/uuid4().hex
        peer = '-1000000005'
        root = base/'downloaded'/f'中性测试 [{peer}]'
        partials = base/'downloading/partials'/peer
        record = ScanRecord(base/'state/scan-records'/f'{peer}.jsonl')
        album = Message('10', '', 1700000000, 1, (('10', '', 1700000000, True),),
                        (('10', 'video'),), album_id='123', sender_id='first')
        single = Message('20', 'neutral caption', 1700000300, 1,
                         (('20', 'neutral caption', 1700000300, True),),
                         (('20', 'video'),), sender_id='second')
        failed = Message('21', '', 1700000301, 1, (('21', '', 1700000301, True),),
                         (('21', 'video'),), sender_id='second')
        record.record_scan([album, single, failed], reached_oldest=True)
        segment = root/group_folder(single); segment.mkdir(parents=True)
        media = segment/'2023-11-15-m20_1_777.bin'; media.write_bytes(b'neutral media')
        text = segment/'2023-11-15-m20_neutral caption.txt'; text.write_text(single.text, encoding='utf-8')
        (segment/'段内清单.json').write_text('{}', encoding='utf-8')
        record.record_result('20', 'downloaded', detail=str(media), naming_revision=3)
        record.record_content_file('20', text)
        record.record_discovered('20', [FoundMedia('20', 1, '20', len(b'neutral media'))])
        journal = partials/'20.verified.jsonl'
        identity = {'mid': '20', 'peer': peer, 'id': '777'}
        verified_digest(media, identity, journal, lambda p: hashlib.sha256(p.read_bytes()).hexdigest())
        failed_media = segment/'2023-11-15-m21_1_778.bin'
        note = write_failure_note(failed_media, '21', 123, RemoteFileError(500, 'UNKNOWN'))
        record.record_result('21', 'failed', detail='neutral remote failure')
        with patch('telegg.album_context.preceding_album', new_callable=AsyncMock,
                   return_value=context_from_album(album)):
            plan = asyncio.run(build_plan(None, peer, [root], record, partials,
                                         base/'~archive/job', base))
        return base, root, record, partials, plan, (media, text, note), journal

    def test_preview_is_read_only_and_apply_preserves_files_notes_and_failed_status(self):
        base, root, record, partials, plan, files, journal = self.fixture()
        before = {p: stamp(p) for p in files}
        self.assertTrue(all(p.is_file() for p in files))
        job = base/'job.json'
        execute_plan(plan, record, [root], job, data_root=base, partials=partials)
        for move in plan['moves']:
            self.assertEqual(stamp(Path(move['target'])), before[Path(move['source'])])
            self.assertFalse(Path(move['source']).exists())
        new_folder = Path(plan['moves'][0]['target']).parent
        self.assertEqual(new_folder.name, '2023-11-15-g123后')
        self.assertIn(files[2].name, registered_notes(new_folder))
        self.assertFalse((files[0].parent/REGISTRY).exists())
        self.assertEqual(record.results['21']['status'], 'failed')
        self.assertEqual(read_snapshot(journal)['path'], record.results['20']['detail'])
        self.assertEqual(record.content_files['20'], str(new_folder/files[1].name))
        reloaded = ScanRecord(record.path)
        self.assertEqual(reloaded.standalone_contexts['20']['album_id'], '123')
        before_record = record.path.read_bytes()
        execute_plan(plan, reloaded, [root], job, data_root=base, partials=partials)
        self.assertEqual(record.path.read_bytes(), before_record)

    def test_conflict_is_rejected_before_any_file_moves(self):
        base, root, record, partials, plan, files, _journal = self.fixture()
        conflict = Path(plan['moves'][-1]['target'])
        conflict.parent.mkdir(parents=True); conflict.write_bytes(b'user file')
        with self.assertRaisesRegex(LocalDataError, '冲突'):
            execute_plan(plan, record, [root], base/'job.json', data_root=base, partials=partials)
        self.assertTrue(all(p.exists() for p in files))
        self.assertEqual(conflict.read_bytes(), b'user file')

    def test_modified_plan_context_is_rejected_before_any_file_moves(self):
        base, root, record, partials, plan, files, _journal = self.fixture()
        plan['contexts']['20']['album_id'] = '../invalid'
        with self.assertRaisesRegex(LocalDataError, '定位证据'):
            execute_plan(plan, record, [root], base/'job.json', data_root=base, partials=partials)
        self.assertTrue(all(p.exists() for p in files))

    def test_text_history_versions_move_together_and_keep_current_pointer(self):
        base, root, record, partials, _plan, files, _journal = self.fixture()
        text = files[1]
        first = text.with_name(text.stem+'.v1.txt')
        latest = text.with_name(text.stem+'.v2.txt')
        text.rename(first)
        latest.write_bytes(first.read_bytes())
        record.record_content_file('20', latest)
        with patch('telegg.album_context.preceding_album', new_callable=AsyncMock,
                   return_value={'album_id': '123', 'day': '2023-11-15', 'previous_mid': '10'}):
            plan = asyncio.run(build_plan(None, '-1000000005', [root], record, partials,
                                         base/'~archive/versions', base))
        execute_plan(plan, record, [root], base/'version-job.json', data_root=base, partials=partials)
        folder = Path(record.content_files['20']).parent
        self.assertEqual(Path(record.content_files['20']).name, latest.name)
        self.assertEqual((folder/first.name).read_bytes(), (folder/latest.name).read_bytes())

    def test_interrupted_rename_resumes_using_plan_without_overwrite(self):
        from telegg.layout_migration import _move_one
        base, root, record, partials, plan, files, _journal = self.fixture()
        calls = []
        def interrupted(move, roots):
            if calls:
                raise RuntimeError('synthetic interruption')
            calls.append(1); _move_one(move, roots)
        with patch('telegg.layout_migration._move_one', side_effect=interrupted):
            with self.assertRaisesRegex(RuntimeError, 'interruption'):
                execute_plan(plan, record, [root], base/'job.json', data_root=base, partials=partials)
        execute_plan(plan, record, [root], base/'job.json', data_root=base, partials=partials)
        self.assertTrue(all(Path(move['target']).is_file() for move in plan['moves']))


if __name__ == '__main__':
    unittest.main()
