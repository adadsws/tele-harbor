"""用消息样例验证纯脚本的分组与目录层级。"""

import unittest
from pathlib import Path
from unittest.mock import patch

from telegg.layout import Message, destination, group_folder, group_id, group_root, grouped, item_folder


class LayoutTests(unittest.TestCase):
    def test_long_group_text_uses_twenty_characters_and_suffix(self):
        short = Message('10', 'abcdefghijklmnopqrst', 1700000000, 1)
        long = Message('11', 'abcdefghijklmnopqrstu', 1700000000, 1)
        self.assertEqual(group_folder(short), '[2023-11-15_m10] abcdefghijklmnopqrst')
        self.assertEqual(group_folder(long), '[2023-11-15_m11] abcdefghijklmnopqrst++')

    def test_requested_groups(self):
        messages = [Message(str(i), text, 1700000000 + i, 1) for i, text in enumerate(
            ['', '文字2', '文字3', '', '', '', '文字7'], 1
        )]
        groups = grouped(messages)
        self.assertEqual([[x.mid for x in batch] for _, batch in groups],
                         [['1', '2'], ['3'], ['4', '5', '6', '7']])
        root = Path('downloads') / '测试群'
        first = destination(root, *groups[0], messages[0])
        second = destination(root, *groups[0], messages[1])
        single = destination(root, *groups[1], messages[2])
        self.assertEqual(first.parent, second.parent)
        self.assertEqual(first.name, item_folder(messages[0]))
        self.assertEqual(second.name, item_folder(messages[1]))
        self.assertEqual(single.name, '[2023-11-15_m3] 文字3')
        self.assertEqual(first.name, '2023-11-15_m1')

    def test_same_second_and_trailing(self):
        messages = [Message('4', '', 1700000000, 1), Message('5', '', 1700000000, 1)]
        anchor, batch = grouped(messages)[0]
        self.assertIsNone(anchor)
        self.assertNotEqual(item_folder(messages[0]), item_folder(messages[1]))
        self.assertEqual(destination(Path('out'), anchor, batch, messages[0]), Path('out/未归组'))
        self.assertEqual(destination(Path('out'), anchor, batch, messages[1]), Path('out/未归组'))
        single_anchor, single_batch = grouped(messages[:1])[0]
        self.assertIsNone(single_anchor)
        self.assertEqual(destination(Path('out'), single_anchor, single_batch, messages[0]),
                         Path('out/未归组'))

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
