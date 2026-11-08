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
from telegg.progress import (bar, date_label, file_line, group_line, media_timestamp,
                             media_label, ProgressLine, remaining_label, display_width, message_count)
from telegg.scan_record import FoundMedia, ScanRecord
from telegg.local_config import load_paths

TEST_ROOT = load_paths()['test_artifacts']


class ProgressTests(unittest.TestCase):
    def test_album_progress_uses_member_time_and_separates_transfer_from_validation(self):
        item = Message('42', '', 1700000000, 2,
                       (('42', '', 1700000000, True), ('43', '', 1700086400, True)))
        stamp = media_timestamp(item, '43')
        self.assertEqual(message_count([item]), 2)
        self.assertEqual(stamp, 1700086400)
        self.assertIn('2023-11-16', media_label(item, '43', 2, 2))
        self.assertIn('m43', media_label(item, '43', 2, 2))
        self.assertNotIn(':', media_label(item, '43', 2, 2))
        line = file_line('43', 2, 2, 50, 100, 10, timestamp=stamp)
        self.assertIn('2023-11-16', line)
        self.assertIn('5秒', line)
        self.assertLess(display_width(line), 80)
        with patch('telegg.progress.shutil.get_terminal_size', return_value=SimpleNamespace(columns=80)):
            narrow = file_line('43', 1061, 1061, 1048576, 1984127, 713*1024, timestamp=stamp)
        self.assertLess(display_width(narrow), 80)
        self.assertIn('本段文件', narrow)
        transferring = file_line('43', 2, 2, 100, 100, 10, timestamp=stamp)
        self.assertIn('校验中', transferring)
        self.assertNotIn('校验通过', transferring)
        self.assertIn('校验通过', file_line('43', 2, 2, 100, 100, None, True, timestamp=stamp))
        self.assertIn('计算中', remaining_label(0, 100, 0))

    def test_redirected_progress_has_full_lines_and_terminal_finishes_before_next_stage(self):
        log = io.StringIO()
        renderer = ProgressLine(log)
        renderer.write('long progress')
        renderer.write('short')
        renderer.finish()
        self.assertNotIn('\r', log.getvalue())
        self.assertEqual(len(log.getvalue().splitlines()), 2)
        terminal = io.StringIO()
        terminal.isatty = lambda: True
        renderer = ProgressLine(terminal)
        renderer.write('long progress')
        renderer.write('short')
        renderer.finish()
        self.assertTrue(terminal.getvalue().endswith('\n'))
        self.assertIn('\rshort        ', terminal.getvalue())

    def test_fast_prefix_also_publishes_completed_staging(self):
        record = MagicMock()
        record.ordered_messages.return_value = []
        record.media = {}
        record.messages = {}
        record.source_messages = {}
        record.history_complete = False
        with (patch('telegg.browser.ScanRecord', return_value=record),
              patch('telegg.browser.open_group', new_callable=AsyncMock, return_value='群组'),
              patch('telegg.browser.existing_media_index', return_value={}),
              patch('telegg.browser.resume_endpoint', return_value=99),
              patch('telegg.browser.verify_resume_prefix', new_callable=AsyncMock,
                    return_value=(99, 1, 0)),
              patch('telegg.browser.reconcile_reply_groups'),
              patch('telegg.browser.demote_unready_replies', return_value=False) as demote,
              patch('telegg.browser.publish_ready_staged_groups') as publish,
              redirect_stdout(io.StringIO())):
            result = asyncio.run(download_group(None, 'https://web.telegram.org/k/#-1',
                                                TEST_ROOT / 'fast-prefix-output', 1, False,
                                                coarse_skip=True))
        self.assertEqual(result, (1, 0, 0, 0))
        self.assertEqual(demote.call_count, 2)
        publish.assert_called_once()

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
              patch('telegg.browser.verified_group_signature', return_value='fixture'),
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
                         '101', TEST_ROOT / 'identity-check-output', None)
        existing = MagicMock()
        existing.is_file.return_value = True
        target = MagicMock()
        target.count = AsyncMock(return_value=1)
        targets = MagicMock()
        targets.count = AsyncMock(return_value=1)
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

        async def save(_page, _url, task, _index, **_kwargs):
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
              patch('telegg.browser.verified_group_signature', return_value='fixture'),
              patch('telegg.browser.restore_scan_position', side_effect=restore),
              patch('telegg.browser.existing_media_index', return_value={}),
              patch('telegg.browser.archive_empty_relocated_folders'),
              redirect_stdout(output)):
            result = asyncio.run(download_group(page, 'https://web.telegram.org/k/#-1',
                                                TEST_ROOT / 'progress-output', None, False))
        self.assertEqual(result, (4, 4, 0, 0))
        self.assertEqual([call for call in calls if call.startswith('download-')],
                         ['download-4', 'download-3', 'download-2', 'download-1'])
        self.assertLess(calls.index('scan-2'), calls.index('download-4'))
        self.assertLess(calls.index('download-4'), calls.index('scan-1'))
        self.assertLess(calls.index('restore-2'), calls.index('scan-1'))
        self.assertIn('正在处理 2023-11-15', output.getvalue())
        self.assertIn('本次已处理至 2023-11-15', output.getvalue())
        self.assertIn('本段完成', output.getvalue())
        self.assertNotIn('入队', output.getvalue())
        self.assertNotIn('批', output.getvalue())


if __name__ == '__main__':
    unittest.main()
