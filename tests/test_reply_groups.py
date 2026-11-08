"""虚构消息验证引用连通分量、目录迁移和标记安全性。"""

import json
import tempfile
import unittest
from unittest.mock import patch
from dataclasses import replace
from pathlib import Path

from telegg.browser import (demote_unready_replies, normalized_reply_id,
                            pending_reply_target, publish_ready_staged_groups)
from telegg.layout import Message, group_folder
from telegg.reply_groups import linked_anchors, linked_group_names, reconcile_reply_groups
from telegg.scan_record import ScanRecord
from telegg.local_reuse import move_files_only


def message(mid: int, text: str, target: int | None = None) -> Message:
    source = str(mid)
    return Message(source, text, 1700000000 + mid, 0,
                   ((source, text, 1700000000 + mid, False),),
                   ((source, 'text'),), (),
                   ((source, str(target)),) if target else ())


class ReplyGroupTests(unittest.TestCase):
    def test_link_manifest_exists_before_any_component_member_is_published(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root, staging = base / 'output', base / 'staging'
            record = ScanRecord(base / 'record.jsonl')
            older, target, source = message(99, 'older'), message(100, 'target'), message(101, 'reply', 100)
            record.record_scan([older, target, source], reached_oldest=False)
            names = linked_group_names(record)
            for item in (target, source):
                folder = staging / names[item.mid] / group_folder(item)
                folder.mkdir(parents=True)
                content = folder / 'text.txt'
                content.write_bytes(b'fixture')
                record.record_content_file(item.mid, content)
                record.record_result(item.mid, 'saved_content')
            checked = []
            def publish(old, new):
                self.assertTrue((new.parent / '关联清单.json').is_file())
                checked.append(new)
                return move_files_only(old, new)
            with patch('telegg.browser.move_files_only', side_effect=publish):
                self.assertEqual(publish_ready_staged_groups(root, staging, record), 2)
            self.assertEqual(len(checked), 2)
            self.assert_replies(root / names[target.mid], [('101', '100')])

    def test_reply_target_segment_waits_for_entire_linked_component(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            record = ScanRecord(base / 'record.jsonl')
            target, source = message(100, 'target'), message(101, 'reply', 100)
            record.record_scan([target, source], reached_oldest=False)
            content = base / 'target.json'
            content.write_bytes(b'{}')
            record.record_content_file(target.mid, content)
            record.record_result(target.mid, 'saved_content')
            self.assertTrue(pending_reply_target([target], record, {}))
            record.record_content_file(source.mid, content)
            record.record_result(source.mid, 'saved_content')
            self.assertFalse(pending_reply_target([target], record, {}))

    def test_reply_id_accepts_raw_and_web_k_prefixed_values(self):
        self.assertEqual(normalized_reply_id('76879'), '4295044175')
        self.assertEqual(normalized_reply_id('4294974175'), '4294974175')
        self.assertEqual(normalized_reply_id('8589941471'), '8589941471')

    def assert_replies(self, folder: Path, pairs: list[tuple[str, str]],
                       name: str = '关联清单.json') -> None:
        payload = json.loads((folder / name).read_text(encoding='utf-8'))
        self.assertEqual(payload, {'replies': [
            {'source_message_id': source, 'target_message_id': target}
            for source, target in pairs]})

    def test_internal_reply_can_publish_with_its_avatar_segment(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            record = ScanRecord(base / 'record.jsonl')
            older = message(100, 'target')
            newer = message(101, 'reply', 100)
            older, newer = replace(older, sender_id='same'), replace(newer, sender_id='same')
            record.record_scan([older, newer], reached_oldest=False)
            for item in (older, newer):
                content = base / f'{item.mid}.json'
                content.write_text('{}', encoding='utf-8')
                record.record_content_file(item.mid, content)
                record.record_result(item.mid, 'saved_content')
            self.assertFalse(pending_reply_target([older, newer], record, {}, base / 'downloaded'))

    def test_bounded_scan_publishes_reply_after_target_is_downloaded(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root, staging = base / 'downloaded', base / 'staging'
            record = ScanRecord(base / 'record.jsonl')
            older, target, source = message(99, 'older'), message(100, 'target'), message(101, 'reply', 100)
            record.record_scan([older, target, source], reached_oldest=False)
            parent = '[From_2023-11-15-m100] 关联组'
            target_folder = root / parent / group_folder(target)
            source_folder = staging / parent / group_folder(source)
            for item, folder in ((target, target_folder), (source, source_folder)):
                folder.mkdir(parents=True)
                (folder / '组清单.txt').write_text('fixture', encoding='utf-8')
                content = base / f'{item.mid}.json'
                content.write_text('{}', encoding='utf-8')
                record.record_content_file(item.mid, content)
                record.record_result(item.mid, 'saved_content')
            self.assertFalse(record.history_complete)
            self.assertEqual(reconcile_reply_groups(root, record), 0)
            self.assertFalse((root / parent / '关联清单.json').exists())
            self.assertEqual(publish_ready_staged_groups(root, staging, record), 1)
            self.assertTrue((root / parent / group_folder(source) / '组清单.txt').is_file())
            self.assert_replies(root / parent, [('101', '100')])

    def test_unready_reply_moves_file_to_staging_before_linking_output(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root, staging = base / 'downloaded', base / 'downloading'
            root.mkdir()
            record = ScanRecord(base / 'record.jsonl')
            target, source = message(100, 'target'), message(101, 'reply', 100)
            record.record_scan([target, source], reached_oldest=False)
            original = root / group_folder(source)
            original.mkdir()
            (original / 'media.bin').write_bytes(b'file')
            self.assertEqual(demote_unready_replies(root, staging, record, {}), 1)
            self.assertEqual(reconcile_reply_groups(root, record), 0)
            self.assertFalse((root / '[From_2023-11-15-m100] 关联组').exists())
            self.assertTrue((staging / '[From_2023-11-15-m100] 关联组' /
                             group_folder(source) / 'media.bin').is_file())

    def test_chain_connects_three_groups_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'download'
            root.mkdir()
            record = ScanRecord(Path(temp) / 'record.jsonl')
            items = [message(101, 'a'), message(102, 'b', 101), message(103, 'c', 102)]
            record.record_scan(items, reached_oldest=False)
            for item in items:
                folder = root / group_folder(item)
                folder.mkdir()
                (folder / 'keep.bin').write_bytes(b'unchanged')
            record.record_result('101', 'downloaded', detail=str(root / group_folder(items[0]) / 'keep.bin'))
            self.assertEqual(linked_anchors(record), {'101': '101', '102': '101', '103': '101'})
            self.assertEqual(reconcile_reply_groups(root, record), 3)
            linked = root / '[From_2023-11-15-m101] 关联组'
            for item in items:
                self.assertEqual((linked / group_folder(item) / 'keep.bin').read_bytes(), b'unchanged')
            self.assertEqual(record.results['101']['detail'], str(linked / group_folder(items[0]) / 'keep.bin'))
            self.assert_replies(linked, [('102', '101'), ('103', '102')])
            self.assertTrue(all((root / group_folder(item)).is_dir() for item in items))
            self.assertEqual(reconcile_reply_groups(root, record), 0)
            additional = message(104, 'd', 103)
            record.record_scan([additional], reached_oldest=False)
            extra = root / group_folder(additional)
            extra.mkdir()
            (extra / 'keep.bin').write_bytes(b'extra')
            self.assertEqual(reconcile_reply_groups(root, record), 1)
            self.assert_replies(linked, [('102', '101'), ('103', '102'), ('104', '103')])
            self.assert_replies(linked, [('102', '101'), ('103', '102')], '关联清单.v1.json')

    def test_same_group_reference_marks_message_subfolder(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'download'
            root.mkdir()
            record = ScanRecord(Path(temp) / 'record.jsonl')
            older = message(201, '', 200)
            anchor = message(202, 'caption')
            target = message(200, 'earlier')
            record.record_scan([target, older, anchor], reached_oldest=False)
            source = root / group_folder(older)
            source.mkdir()
            (source / 'keep.bin').write_bytes(b'file')
            target_folder = root / group_folder(target)
            target_folder.mkdir()
            (target_folder / 'keep.bin').write_bytes(b'target')
            self.assertEqual(reconcile_reply_groups(root, record), 2)
            linked = root / '[From_2023-11-15-m200] 关联组'
            self.assertEqual((linked / group_folder(older) / 'keep.bin').read_bytes(), b'file')
            self.assert_replies(linked, [('201', '200')])

    def test_missing_target_does_not_guess_group(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'download'
            root.mkdir()
            record = ScanRecord(Path(temp) / 'record.jsonl')
            item = message(301, 'a', 999)
            record.record_scan([item], reached_oldest=False)
            (root / group_folder(item)).mkdir()
            self.assertEqual(reconcile_reply_groups(root, record), 0)
            self.assertTrue((root / group_folder(item)).is_dir())

    def test_record_roundtrip_and_repeat_keeps_association(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'download'
            root.mkdir()
            path = Path(temp) / 'record.jsonl'
            record = ScanRecord(path)
            first, second, third = message(301, 'a'), message(302, 'b'), message(303, 'c', 301)
            record.record_scan([first, second, third], reached_oldest=False)
            record = ScanRecord(path)
            self.assertEqual(record.messages['303'].reply_targets, (('303', '301'),))
            (root / group_folder(first)).mkdir()
            (root / group_folder(first) / 'keep.bin').write_bytes(b'target')
            (root / group_folder(third)).mkdir()
            (root / group_folder(third) / 'keep.bin').write_bytes(b'file')
            reconcile_reply_groups(root, record)
            first_parent = root / '[From_2023-11-15-m301] 关联组'
            self.assert_replies(first_parent, [('303', '301')])
            self.assertEqual(reconcile_reply_groups(root, record), 0)
            self.assertEqual((first_parent / group_folder(third) / 'keep.bin').read_bytes(), b'file')

    def test_duplicate_destination_stops_without_overwrite(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'download'
            root.mkdir()
            record = ScanRecord(Path(temp) / 'record.jsonl')
            first, second = message(401, 'a'), message(402, 'b', 401)
            record.record_scan([first, second], reached_oldest=False)
            source = root / group_folder(second)
            target = root / '[From_2023-11-15-m401] 关联组' / group_folder(second)
            source.mkdir()
            target.mkdir(parents=True)
            (source / 'keep.bin').write_bytes(b'source')
            (target / 'keep.bin').write_bytes(b'target')
            with self.assertRaisesRegex(RuntimeError, '不唯一'):
                reconcile_reply_groups(root, record)
            self.assertTrue(source.is_dir() and target.is_dir())

    def test_reply_backfill_keeps_existing_album_bubble(self):
        with tempfile.TemporaryDirectory() as temp:
            record = ScanRecord(Path(temp) / 'record.jsonl')
            album = Message('502', 'caption', 1700000502, 2,
                            (('501', '', 1700000501, True),
                             ('502', 'caption', 1700000502, True)),
                            (('501', 'photo'), ('502', 'photo')))
            record.record_scan([album], reached_oldest=False)
            record.record_reply('501', '499')
            self.assertEqual(set(record.messages), {'502'})
            self.assertEqual(record.messages['502'].reply_targets, (('501', '499'),))
            self.assertEqual(record.source_messages['501']['reply_to'], '499')

    def test_oldest_group_uses_its_latest_caption_and_migrates_trial_names(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'download'
            root.mkdir()
            record = ScanRecord(Path(temp) / 'record.jsonl')
            first = message(901, 'old')
            second = message(902, '', 901)
            caption = message(903, 'new')
            record.record_scan([first, second, caption], reached_oldest=False)
            old = root / '[2023-11-15_m901] 关联组' / '[2023-11-15_m902] new'
            old.mkdir(parents=True)
            (old / 'keep.bin').write_bytes(b'original')
            self.assertEqual(linked_group_names(record)['902'], '[From_2023-11-15-m901] 关联组')
            self.assertFalse(linked_group_names(record).get('903'))
            self.assertEqual(reconcile_reply_groups(root, record), 0)
            self.assertEqual((old / 'keep.bin').read_bytes(), b'original')


if __name__ == '__main__':
    unittest.main()
