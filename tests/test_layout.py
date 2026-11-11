"""用消息样例验证纯脚本的分组与目录层级。"""

import unittest
from pathlib import Path
from unittest.mock import patch

from telegg.layout import Message, destination, group_folder, group_id, group_root, grouped, item_folder, message_url


class LayoutTests(unittest.TestCase):
    def test_message_url_normalizes_raw_and_internal_ids(self):
        source = 'https://web.telegram.org/k/#-1000000005'
        expected = source + '?message=104'
        self.assertEqual(message_url(source, 104), expected)
        self.assertEqual(message_url(source, '4294967400'), expected)

    def test_message_url_rejects_invalid_ids_and_foreign_origins(self):
        for mid in ('0', '-1', '4294967296', '1&message=2', 'invalid'):
            with self.subTest(mid=mid), self.assertRaises(ValueError):
                message_url('https://web.telegram.org/k/#-1', mid)
        with self.assertRaises(ValueError):
            message_url('https://example.com/#-1', '104')

    def test_group_folder_ignores_text(self):
        short = Message('10', 'abcdefghijklmnopqrst', 1700000000, 1)
        long = Message('11', 'abcdefghijklmnopqrstu', 1700000000, 1)
        self.assertEqual(group_folder(short), '[From_2023-11-15-m10]')
        self.assertEqual(group_folder(long), '[From_2023-11-15-m11]')

    def test_requested_groups(self):
        messages = [Message(str(i), text, 1700000000 + i, 1,
                            sender_id=('a' if i <= 2 else 'b' if i == 3 else 'c')) for i, text in enumerate(
            ['', '文字2', '文字3', '', '', '', '文字7'], 1
        )]
        groups = grouped(messages)
        self.assertEqual([[x.mid for x in batch] for _, batch in groups],
                         [['1', '2'], ['3'], ['4', '5', '6', '7']])
        root = Path('downloads') / '测试群'
        first = destination(root, *groups[0], messages[0])
        second = destination(root, *groups[0], messages[1])
        single = destination(root, *groups[1], messages[2])
        self.assertEqual(first, second)
        self.assertEqual(first.name, group_folder(messages[0]))
        self.assertEqual(single.name, '[From_2023-11-15-m3]')

    def test_album_caption_uses_caption_source_mid_after_entire_album(self):
        album = Message('244', '#第44期', 1793121479, 10,
                        tuple((str(mid), '#第44期' if mid == 251 else '',
                               1793121479 + mid - 244, True)
                              for mid in range(244, 254)))
        self.assertEqual(group_folder(album), '[From_2026-10-28-m244]')

    def test_same_second_and_trailing(self):
        messages = [Message('4', '', 1700000000, 1, sender_id='same'),
                    Message('5', '', 1700000000, 1, sender_id='same')]
        anchor, batch = grouped(messages)[0]
        self.assertEqual(anchor.mid, '4')
        self.assertNotEqual(item_folder(messages[0]), item_folder(messages[1]))
        self.assertEqual(destination(Path('out'), anchor, batch, messages[0]), Path('out/[From_2023-11-15-m4]'))
        self.assertEqual(destination(Path('out'), anchor, batch, messages[1]), Path('out/[From_2023-11-15-m4]'))
        single_anchor, single_batch = grouped(messages[:1])[0]
        self.assertEqual(single_anchor.mid, '4')
        self.assertEqual(destination(Path('out'), single_anchor, single_batch, messages[0]),
                         Path('out/[From_2023-11-15-m4]'))

    def test_urls(self):
        self.assertEqual(group_id('https://web.telegram.org/k/#-1000000005'), '-1000000005')
        with self.assertRaises(ValueError):
            group_id('https://example.com/#-1')

    def test_group_rename_reuses_existing_output(self):
        existing = Path('downloads/旧名字 [-1]')
        with (patch.object(Path, 'is_dir', return_value=True),
              patch.object(Path, 'iterdir', return_value=[existing])):
            root = group_root(Path('downloads'), '新名字', '-1')
        self.assertEqual(root, existing)

    def test_ambiguous_group_directories_stop(self):
        with (patch.object(Path, 'is_dir', return_value=True),
              patch.object(Path, 'iterdir', return_value=[
                  Path('downloads/旧名字 [-1]'), Path('downloads/新名字 [-1]'),
              ])):
            with self.assertRaises(RuntimeError):
                group_root(Path('downloads'), '新名字', '-1')


if __name__ == '__main__':
    unittest.main()
