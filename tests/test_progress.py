"""单页下载的简短进度与顺序验证。"""

import asyncio
import io
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from telegg.browser import MediaTask, audit_messages, download_group, save_media
from telegg.layout import Message
from telegg.progress import bar, date_label, file_line, group_line
from telegg.scan_record import FoundMedia, ScanRecord
from telegg.local_config import load_paths

TEST_ROOT = load_paths()['test_artifacts']


class ProgressTests(unittest.TestCase):
    def test_audit_keeps_all_originals_in_one_album(self):
        record = ScanRecord(TEST_ROOT / 'nonexistent-audit.jsonl')
        record.append = lambda events: [record._apply(event) for event in events]
        album = Message('100', '正文', 1700000100, 2,
                        (('100', '', 1700000100, True),
                         ('101', '正文', 1700000101, True)),
                        (('100', 'document'), ('101', 'document')))

        async def history(_page, _limit, _known, _progress, **_kwargs):
            yield album

        with (patch('telegg.browser.ScanRecord', return_value=record),
              patch('telegg.browser.open_group', new_callable=AsyncMock, return_value='群组'),
              patch('telegg.browser.iter_history', side_effect=history),
              patch('telegg.browser.discover_media', new_callable=AsyncMock,
                    return_value=[FoundMedia('100', 1, '100', 3),
                                  FoundMedia('100', 2, '101', 4)]),
              redirect_stdout(io.StringIO())):
            self.assertEqual(asyncio.run(audit_messages(SimpleNamespace(),
                                                        'https://web.telegram.org/k/#-1', 1)), (1, 2))
        self.assertEqual(set(record.source_messages), {'100', '101'})
        self.assertIn('100', record.discovered)

    def test_group_and_file_bars_use_real_counts_and_bytes(self):
        self.assertEqual(date_label(1700000000), '2023-11-15')
        self.assertIn('无文件', group_line(0, 0))
        self.assertIn('3/5', group_line(3, 5))
        self.assertEqual(bar(3, 5, 10), '[######----]')
        line = file_line('42', 3, 5, 5 * 1024 * 1024, 10 * 1024 * 1024,
                         2 * 1024 * 1024)
        self.assertIn('50.0%', line)
        self.assertIn('2.0 MiB/s', line)
        self.assertIn('文件 3/5', line)
        self.assertNotIn('m42', line)
        self.assertIn('64 KiB/s', file_line('42', 3, 5, 1, 2, 64 * 1024))
        self.assertIn('完成', file_line('42', 5, 5, 10, 10, None, finished=True))

    def test_existing_file_rechecks_browser_identity(self):
        task = MediaTask(Message('1', '正文', 1700000001, 1), 1,
                         '101', Path('unused/文件'), None)
        existing = MagicMock()
        existing.is_file.return_value = True
        target = MagicMock()
        target.count = AsyncMock(return_value=1)
        targets = MagicMock()
        targets.nth.return_value = target
        with (patch('telegg.browser.locate_message', new_callable=AsyncMock) as locate,
              patch('telegg.browser.media_targets', new_callable=AsyncMock, return_value=targets),
              patch('telegg.browser.task_metadata', new_callable=AsyncMock, return_value=('101', 3)),
              patch('telegg.browser.save_one', new_callable=AsyncMock,
                    return_value=('skipped', existing, 3, 'abc', None))):
            result = asyncio.run(save_media(None, 'https://web.telegram.org/k/#-1',
                                            task, {'101': [existing]}))
        self.assertEqual(result, ('skipped', None))
        locate.assert_awaited_once()

    def test_newest_message_is_saved_first_on_same_page(self):
        record = ScanRecord(TEST_ROOT / 'nonexistent-progress.jsonl')
        record.append = lambda events: [record._apply(event) for event in events]
        messages = [Message('4', '最新', 1700000004, 1),
                    Message('3', '新', 1700000003, 1),
                    Message('2', '中', 1700000002, 1),
                    Message('1', '旧', 1700000001, 1)]
        page = SimpleNamespace(url='group')
        calls = []

        async def history(_page, _limit, _known, progress, **_kwargs):
            for item in messages:
                calls.append(f'scan-{item.mid}')
                yield item
            progress.reached_oldest = True

        async def discover(_page, _url, item):
            self.assertIs(_page, page)
            return [FoundMedia(item.mid, 1, item.mid, None)]

        async def save(_page, _url, task, _index):
            self.assertIs(_page, page)
            calls.append(f'download-{task.media_mid}')
            page.url = f'group_{task.media_mid}'
            return 'downloaded', None

        async def restore(_page, _url, item):
            calls.append(f'restore-{item.mid}')
            page.url = 'group'

        output = io.StringIO()
        with (patch('telegg.browser.ScanRecord', return_value=record),
              patch('telegg.browser.open_group', new_callable=AsyncMock, return_value='群组'),
              patch('telegg.browser.iter_history', side_effect=history),
              patch('telegg.browser.discover_media', side_effect=discover),
              patch('telegg.browser.save_media', side_effect=save),
              patch('telegg.browser.restore_scan_position', side_effect=restore),
              patch('telegg.browser.existing_media_index', return_value={}),
              patch('telegg.browser.archive_empty_relocated_folders'),
              redirect_stdout(output)):
            result = asyncio.run(download_group(page, 'https://web.telegram.org/k/#-1',
                                                Path('unused'), None, False))
        self.assertEqual(result, (4, 4, 0, 0))
        self.assertEqual([call for call in calls if call.startswith('download-')],
                         ['download-4', 'download-3', 'download-2', 'download-1'])
        self.assertLess(calls.index('scan-2'), calls.index('download-4'))
        self.assertLess(calls.index('download-4'), calls.index('scan-1'))
        self.assertLess(calls.index('restore-2'), calls.index('scan-1'))
        self.assertIn('正在处理 2023-11-15', output.getvalue())
        self.assertIn('本次已处理至 2023-11-15', output.getvalue())
        self.assertIn('本组完成', output.getvalue())
        self.assertNotIn('入队', output.getvalue())
        self.assertNotIn('批', output.getvalue())


if __name__ == '__main__':
    unittest.main()
