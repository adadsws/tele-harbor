"""只归档确认空的目录。"""

import tempfile
import unittest
from pathlib import Path

from telegg.folder_archive import archive_empty_folders


class FolderArchiveTests(unittest.TestCase):
    def test_archive_inside_root_is_excluded_from_repeat_walk(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive_root = root / '~archive'
            preserved = archive_root / 'previous' / 'empty'
            preserved.mkdir(parents=True)
            (root / 'data' / 'empty').mkdir(parents=True)
            count, _ = archive_empty_folders(root, archive_root=archive_root)
            self.assertEqual(count, 2)
            self.assertTrue(preserved.is_dir())
            self.assertEqual(archive_empty_folders(root, archive_root=archive_root)[0], 0)

    def test_nested_empty_tree_moves_but_kept_files_stay(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'downloaded'
            (root / 'group' / 'empty' / 'child').mkdir(parents=True)
            (root / 'group' / 'kept').mkdir()
            (root / 'group' / 'kept' / 'file.bin').write_bytes(b'original')
            count, archive = archive_empty_folders(
                root, archive_root=Path(temp) / '~archive', batch_name='test-empty-folders')
            self.assertEqual(count, 2)
            self.assertEqual(len(list((archive / 'folders').iterdir())), 2)
            self.assertFalse((root / 'group' / 'empty').exists())
            self.assertEqual((root / 'group' / 'kept' / 'file.bin').read_bytes(), b'original')
            self.assertEqual(archive_empty_folders(root, archive_root=Path(temp) / '~archive')[0], 0)


if __name__ == '__main__':
    unittest.main()
