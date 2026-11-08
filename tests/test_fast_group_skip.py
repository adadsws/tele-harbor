"""已完成消息编组按标记和目录索引快速跳过的样例。"""

import hashlib
import json
import unittest
import uuid

from telegg.browser import (bootstrap_completed_media, newest_first_media,
                            recover_interrupted_result,
                            relocate_completed_message,
                            reusable_group_count,
                            reusable_message_count, verified_group_signature)
from telegg.layout import Message, group_folder, item_folder
from telegg.local_config import load_paths
from telegg.scan_record import FoundMedia, ScanRecord


class FastGroupSkipTests(unittest.TestCase):
    def test_album_download_order_is_newest_first_without_renaming_positions(self):
        item = Message('601', 'anchor', 1700000603, 3,
                       (('601', '', 1700000601, True),
                        ('602', '', 1700000602, True),
                        ('603', 'anchor', 1700000603, True)),
                       (('601', 'video'), ('602', 'video'), ('603', 'video')))
        found = [FoundMedia(item.mid, index, mid, None)
                 for index, mid in enumerate(('601', '602', '603'), 1)]
        ordered = newest_first_media(item, found)
        self.assertEqual([entry.media_mid for entry in ordered], ['603', '602', '601'])
        self.assertEqual([entry.index for entry in ordered], [3, 2, 1])

    def test_old_album_position_bootstraps_from_unique_completed_file(self):
        root = load_paths()['test_artifacts'] / f'old-album-{uuid.uuid4().hex}'
        old = root / 'old'
        target = root / 'new'
        old.mkdir(parents=True)
        path = old / '2023-11-15-m505_3_777.bin'
        path.write_bytes(b'fixture')
        item = Message('505', '', 1700000505, 1,
                       (('505', '', 1700000505, True),), (('505', 'video'),))
        record = ScanRecord(root / 'scan.jsonl')
        record.record_result('505', 'skipped', detail=str(path), naming_revision=3)
        record.record_content_file('505', root / 'content.json')
        index = {'505': [path]}
        stale = Message(item.mid, item.text, item.timestamp + 1, item.media_count,
                        item.parts, item.content_types)
        self.assertIsNone(bootstrap_completed_media(item, stale, record, index))
        media = bootstrap_completed_media(item, None, record, index)
        self.assertEqual(media['505'].index, 3)
        self.assertEqual(relocate_completed_message(item, item, media, target,
                                                    record, index), {old})
        self.assertEqual(reusable_message_count(item, item, media, target,
                                                record, index), 1)
        self.assertIsNone(bootstrap_completed_media(item, item, record, index))

    def test_completed_message_relocates_without_browser_or_overwrite(self):
        root = load_paths()['test_artifacts'] / f'relocated-{uuid.uuid4().hex}'
        source_folder = root / 'old'
        target_folder = root / 'new'
        source_folder.mkdir(parents=True)
        path = source_folder / '2023-11-15-m404_1_777.bin'
        path.write_bytes(b'fixture')
        item = Message('404', 'anchor', 1700000404, 1,
                       (('404', 'anchor', 1700000404, True),), (('404', 'photo'),))
        record = ScanRecord(root / 'scan.jsonl')
        found = FoundMedia(item.mid, 1, item.mid, 7)
        record.record_discovered(item.mid, [found])
        record.record_result(item.mid, 'downloaded', detail=str(path), naming_revision=3)
        record.record_content_file(item.mid, root / 'content.json')
        index = {item.mid: [path]}
        moved = relocate_completed_message(item, item, {item.mid: found},
                                           target_folder, record, index)
        self.assertEqual(moved, {source_folder})
        self.assertEqual(index[item.mid], [target_folder / path.name])
        self.assertEqual(record.results[item.mid]['status'], 'relocated')
        self.assertEqual((target_folder / path.name).read_bytes(), b'fixture')
        conflicting = root / 'conflicting'
        conflicting.mkdir()
        (conflicting / path.name).write_bytes(b'other')
        self.assertEqual(relocate_completed_message(item, item, {item.mid: found},
                                                     conflicting, record, index), set())
        self.assertEqual((conflicting / path.name).read_bytes(), b'other')

    def test_interrupted_result_recovers_only_from_complete_committed_file(self):
        root = load_paths()['test_artifacts'] / f'interrupted-{uuid.uuid4().hex}'
        item = Message('303', 'anchor', 1700000303, 1,
                       (('303', 'anchor', 1700000303, True),), (('303', 'photo'),))
        folder = root / group_folder(item)
        folder.mkdir(parents=True)
        path = folder / '2023-11-15-m303_1_777.bin'
        path.write_bytes(b'fixture')
        partial = root / 'partials'
        partial.mkdir()
        journal = partial / '303.jsonl'
        journal.write_text('\n'.join(json.dumps(event) for event in (
            {'kind': 'identity', 'identity': {'peer': '-1000000005', 'mid': '303'},
             'size': 7, 'chunk_size': 1048576},
            {'kind': 'chunk', 'offset': 0, 'length': 7,
             'sha256': hashlib.sha256(b'fixture').hexdigest()})) + '\n', encoding='utf-8')
        record = ScanRecord(root / 'scan.jsonl')
        record.record_discovered(item.mid, [FoundMedia(item.mid, 1, item.mid, 7)])
        record.record_result(item.mid, 'failed', detail='TargetClosedError: browser closed')
        recover_interrupted_result(item, folder, partial, '-1000000005', record,
                                   {item.mid: [path]})
        self.assertEqual(record.results[item.mid]['status'], 'skipped')
        self.assertTrue((partial / '303.verified.jsonl').exists())
        record.record_result(item.mid, 'failed', detail='TargetClosedError: browser closed')
        path.write_bytes(b'changed')
        recover_interrupted_result(item, folder, partial, '-1000000005', record,
                                   {item.mid: [path]})
        self.assertEqual(record.results[item.mid]['status'], 'failed')

    def test_completed_newer_message_skips_inside_unfinished_group(self):
        root = load_paths()['test_artifacts'] / f'mixed-group-{uuid.uuid4().hex}'
        newer = Message('202', 'anchor', 1700000202, 1,
                        (('202', 'anchor', 1700000202, True),), (('202', 'video'),))
        older = Message('201', '', 1700000201, 1,
                        (('201', '', 1700000201, True),), (('201', 'video'),))
        folder = root / group_folder(newer)
        folder.mkdir(parents=True)
        path = folder / '2023-11-15-m202_1_777.bin'
        path.write_bytes(b'fixture')
        record = ScanRecord(root / 'scan.jsonl')
        record.record_scan([older, newer], reached_oldest=False)
        found = FoundMedia(newer.mid, 1, newer.mid, 7)
        record.record_discovered(newer.mid, [found])
        record.record_result(newer.mid, 'downloaded', detail=str(path), naming_revision=3)
        record.record_content_file(newer.mid, root / 'content.json')
        index = {newer.mid: [path]}
        self.assertIsNone(reusable_group_count([older, newer], {older.mid, newer.mid},
                                               folder.parent, root / '.staging',
                                               {older.mid: older, newer.mid: newer},
                                               {newer.mid: {newer.mid: found}}, record, index))
        self.assertEqual(reusable_message_count(newer, newer, {newer.mid: found},
                                                folder, record, index), 1)
        self.assertIsNone(reusable_message_count(older, older, {},
                                                 root / group_folder(newer),
                                                 record, index))

    def test_marked_group_skips_and_missing_or_changed_group_falls_back(self):
        root = load_paths()['test_artifacts'] / f'fast-group-{uuid.uuid4().hex}'
        item = Message('101', 'fixture', 1700000000, 1,
                       (('101', 'fixture', 1700000000, True),),
                       (('101', 'video'),))
        published = root / group_folder(item)
        published.mkdir(parents=True)
        path = published / '2023-11-15-m101_1_777.bin'
        path.write_bytes(b'fixture')
        record = ScanRecord(root / 'scan.jsonl')
        record.record_scan([item], reached_oldest=False)
        found = FoundMedia(item.mid, 1, '101', len(b'fixture'))
        record.record_discovered(item.mid, [found])
        record.record_result('101', 'downloaded', detail=str(path), naming_revision=3)
        record.record_content_file('101', root / 'content.json')
        media = {item.mid: {'101': found}}
        signature = verified_group_signature([item], media)
        record.mark_group_verified(published.name, signature)
        reloaded = ScanRecord(record.path)
        args = ([item], {item.mid}, published, root / '.staging' / published.name,
                {item.mid: item}, media, reloaded)
        self.assertEqual(reusable_group_count(*args, {'101': [path]}), 1)
        self.assertIsNone(reusable_group_count(*args, {}))
        changed = Message(item.mid, item.text, item.timestamp + 1, item.media_count,
                          item.parts, item.content_types)
        self.assertIsNone(reusable_group_count([changed], {changed.mid}, published,
                                               args[3], {changed.mid: changed}, media,
                                               reloaded, {'101': [path]}))
        reloaded.mark_group_verified(published.name, '0' * 64)
        self.assertIsNone(reusable_group_count(*args, {'101': [path]}))
        self.assertEqual(reusable_group_count(*args, {'101': [path]}, check_mark=False), 1)
        reloaded.record_result('101', 'failed')
        self.assertIsNone(reusable_group_count(*args, {'101': [path]}, check_mark=False))


if __name__ == '__main__':
    unittest.main()
