"""新头像段目录、文字和清单的最小评测集。"""

import json
import tempfile
import unittest
from pathlib import Path

from telegg.content_archive import save_group_manifest, save_text_file
from telegg.browser import archive_empty_relocated_folders
from telegg.layout import Message, destination, group_folder, grouped
from telegg.local_reuse import move_files_only


class AvatarLayoutTests(unittest.TestCase):
    def test_partial_segment_cannot_replace_complete_manifest(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            rows = [Message(str(mid), '', 1700000000 + mid, 0) for mid in (1, 2, 3)]
            path = save_group_manifest(root, rows)
            original = path.read_bytes()
            with self.assertRaisesRegex(RuntimeError, '清单缩小'):
                save_group_manifest(root, rows[:2])
            self.assertEqual(path.read_bytes(), original)
            self.assertFalse(list(root.glob('段内清单.v*.json')))

    def test_sender_date_and_121_second_boundary(self):
        rows = [Message(str(i), '', 1793121000 + delta, 1, sender_id=sender)
                for i, delta, sender in ((1, 0, 'u1'), (2, 121, 'u1'),
                                         (3, 243, 'u1'), (4, 244, 'u2'))]
        self.assertEqual([[item.mid for item in batch] for _, batch in grouped(rows)],
                         [['1', '2'], ['3'], ['4']])
        self.assertEqual(group_folder(rows[0]), '[From_2026-10-28-m1]')

    def test_caption_inside_album_does_not_split_avatar_group(self):
        first = Message('234', '', 1793121469, 10,
                        tuple((str(mid), '', 1793121469 + mid - 234, True)
                              for mid in range(234, 244)), sender_id='channel')
        second = Message('244', '标题', 1793121479, 10,
                         tuple((str(mid), '标题' if mid == 251 else '',
                                1793121479 + mid - 244, True)
                               for mid in range(244, 254)), sender_id='channel')
        groups = grouped([first, second])
        self.assertEqual(len(groups), 1)
        self.assertEqual(group_folder(groups[0][0]), '[From_2026-10-28-m234]')

    def test_small_timestamp_inversion_keeps_adjacent_albums_together(self):
        albums = [Message(str(mid), '', 1787761513 + offset, 10,
                          tuple((str(part), '', 1787761513 + offset, True)
                                for part in range(mid, mid + 10)),
                          album_id=str(mid), sender_id='same')
                  for mid, offset in ((3528, 35), (3538, 0), (3548, 0))]
        groups = grouped(albums)
        self.assertEqual(len(groups), 1)
        self.assertEqual(sum(len(item.parts) for item in groups[0][1]), 30)

    def test_text_filename_contains_prefix_and_file_contains_original(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            original = '开头三十字以内\n后面的原文保留'
            path = save_text_file(folder, ('42', original, 1793121469, False))
            self.assertEqual(path.name, '2026-10-28-m42_开头三十字以内 后面的原文保留.txt')
            self.assertEqual(path.read_text(encoding='utf-8'), original)

    def test_edited_text_with_same_prefix_keeps_both_versions(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            prefix = 'a' * 30
            first = save_text_file(folder, ('42', prefix + 'old', 1793121469, False))
            second = save_text_file(folder, ('42', prefix + 'new', 1793121469, False))
            self.assertEqual(first.with_name(first.stem + '.v1.txt').read_text(encoding='utf-8'),
                             prefix + 'old')
            self.assertEqual(second.name, first.stem + '.v2.txt')
            self.assertEqual(second.read_text(encoding='utf-8'), prefix + 'new')
            self.assertEqual(save_text_file(folder, ('42', prefix + 'new', 1793121469, False)), second)

    def test_group_manifest_lists_album_ids_without_text(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            album = Message('244', 'private', 1793121479, 2,
                            (('244', '', 1793121479, True), ('245', 'private', 1793121480, True)),
                            album_id='123', sender_id='channel')
            path = save_group_manifest(folder, [album])
            self.assertEqual(path.name, '段内清单.json')
            self.assertEqual(json.loads(path.read_text(encoding='utf-8')),
                             {'items': [{'grouped_id': '123', 'message_ids': ['244', '245']}]})
            self.assertEqual(save_group_manifest(folder, [album]), path)

    def test_album_folder_and_single_message_share_avatar_segment(self):
        first = Message('244', '', 1793121479, 2,
                        (('244', '', 1793121479, True), ('245', '', 1793121480, True)),
                        album_id='123', sender_id='same')
        second = Message('246', 'caption', 1793121481, 1,
                         (('246', 'caption', 1793121481, True),),
                         album_id='124', sender_id='same')
        single = Message('247', 'text', 1793121482, 0, sender_id='same')
        anchor, batch = grouped([first, second, single])[0]
        root = Path('out')
        self.assertEqual(destination(root, anchor, batch, first),
                         root / '[From_2026-10-28-m244]' / '2026-10-28-g123')
        self.assertEqual(destination(root, anchor, batch, second),
                         root / '[From_2026-10-28-m244]' / '2026-10-28-g124')
        self.assertEqual(destination(root, anchor, batch, single),
                         root / '[From_2026-10-28-m244]')

    def test_publish_moves_files_and_leaves_source_directory_in_place(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / 'staging' / 'group'
            target = Path(temp) / 'downloaded' / 'group'
            source.mkdir(parents=True)
            (source / 'media.bin').write_bytes(b'abc')
            self.assertEqual(move_files_only(source, target), 1)
            self.assertTrue(source.is_dir())
            self.assertFalse((source / 'media.bin').exists())
            self.assertEqual((target / 'media.bin').read_bytes(), b'abc')

    def test_file_move_preflights_conflict_without_touching_source(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / 'old'
            target = Path(temp) / 'new'
            source.mkdir()
            target.mkdir()
            (source / 'a.bin').write_bytes(b'old')
            (target / 'a.bin').write_bytes(b'new')
            with self.assertRaisesRegex(RuntimeError, '已有同名文件'):
                move_files_only(source, target)
            self.assertEqual((source / 'a.bin').read_bytes(), b'old')
            self.assertEqual((target / 'a.bin').read_bytes(), b'new')

    def test_completed_source_folder_is_archived(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'output'
            folder = root / 'source'
            folder.mkdir(parents=True)
            archive_empty_relocated_folders(root, {folder})
            self.assertFalse(folder.exists())
            self.assertTrue(list((Path(temp) / '~archive').rglob('sources.jsonl')))


if __name__ == '__main__':
    unittest.main()
