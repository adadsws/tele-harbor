"""扫描断点与下载任务独立的边界样例；无需连接 Telegram。"""

import asyncio
import io
import unittest
import uuid
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from telegg.browser import (ScanProgress, combine_server_album, download_group,
                            iter_history, iter_history_from_endpoint,
                            server_history_with_recovery)
from telegg.layout import Message, grouped
from telegg.scan_record import FoundMedia, ScanRecord
from telegg.local_config import load_paths

TEST_ROOT = load_paths()['test_artifacts']


class ScanRecordTests(unittest.TestCase):
    def test_server_album_keeps_caption_with_all_ten_media_across_pages(self):
        rows = [Message(str(mid), '#第44期' if mid == 251 else '', 1700000000 + mid,
                        1, ((str(mid), '#第44期' if mid == 251 else '', 1700000000 + mid, True),),
                        ((str(mid), 'video'),), (), (), 'album-44')
                for mid in range(253, 243, -1)]
        page = MagicMock()
        progress = ScanProgress()

        async def collect():
            return [item async for item in iter_history_from_endpoint(page, -1, 253, progress)]

        with patch('telegg.browser.server_older_batch', new_callable=AsyncMock,
                   side_effect=[rows[:4], rows[4:], []]), redirect_stdout(io.StringIO()):
            bubbles = asyncio.run(collect())
        self.assertEqual(len(bubbles), 1)
        self.assertEqual([part[0] for part in bubbles[0].parts],
                         [str(mid) for mid in range(244, 254)])
        self.assertEqual(bubbles[0].mid, '244')
        self.assertEqual(bubbles[0].text, '#第44期')
        self.assertEqual(bubbles[0].media_count, 10)
        self.assertEqual(grouped(bubbles)[0][0].mid, '244')

    def test_server_history_timeout_reopens_group_once(self):
        page = MagicMock()
        page.is_closed.return_value = False
        page.goto = AsyncMock()
        request = AsyncMock(side_effect=[TimeoutError('stalled'), ['older']])
        with patch('telegg.browser.open_group', new_callable=AsyncMock) as reopen, redirect_stdout(io.StringIO()):
            result = asyncio.run(server_history_with_recovery(page, -1000000004, 4294967400, request))
        self.assertEqual(result, ['older'])
        self.assertEqual(request.await_count, 2)
        page.goto.assert_awaited_once_with('about:blank', wait_until='domcontentloaded')
        reopen.assert_awaited_once_with(page, 'https://web.telegram.org/k/#-1000000004')

    def test_server_history_repeated_timeout_stops_without_completion(self):
        page = MagicMock()
        page.is_closed.return_value = False
        page.goto = AsyncMock()
        request = AsyncMock(side_effect=TimeoutError('stalled'))
        with patch('telegg.browser.open_group', new_callable=AsyncMock), redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, '连续超时'):
                asyncio.run(server_history_with_recovery(page, -1000000004, 4294967400, request))
        self.assertEqual(request.await_count, 2)

    def test_invalid_event_does_not_corrupt_append_log(self):
        path = TEST_ROOT / f'append-validation-{uuid.uuid4().hex}.jsonl'
        record = ScanRecord(path)
        record.record_scan([Message('1', '正文', 1700000001, 1)], reached_oldest=False)
        before = path.read_bytes()
        with self.assertRaisesRegex(ValueError, '附件元数据无效'):
            record.record_discovered('1', [FoundMedia('1', 0, '2', 3)])
        self.assertEqual(path.read_bytes(), before)
        self.assertNotIn('1', record.discovered)
        self.assertEqual(ScanRecord(path).messages['1'].text, '正文')

    @staticmethod
    async def scan(page, limit, known=None):
        progress = ScanProgress()
        messages = [item async for item in iter_history(page, limit, known or set(), progress)]
        return messages, progress

    def record(self):
        record = ScanRecord(TEST_ROOT / f'nonexistent-{uuid.uuid4().hex}.jsonl')
        record.append = lambda events: [record._apply(event) for event in events]
        return record

    def test_partial_scan_cannot_claim_oldest_and_failure_remains_discoverable(self):
        record = self.record()
        message = Message('100', '', 1700000000, 1)
        record.record_scan([message], reached_oldest=False)
        self.assertFalse(record.history_complete)
        self.assertNotIn(message.mid, record.discovered)
        record.record_scan([message], reached_oldest=True, replace=True)
        self.assertTrue(record.history_complete)
        self.assertNotIn(message.mid, record.discovered)
        record.record_discovered(message.mid, [FoundMedia('100', 1, '101', 5)])
        self.assertIn('101', record.media)
        self.assertIn(message.mid, record.discovered)
        record.record_scan([Message('102', '文字', 1700000002, 0)], reached_oldest=False)
        self.assertTrue(record.history_complete)
        self.assertEqual([item.mid for item in record.ordered_messages()], ['100', '102'])

    def test_changed_media_count_reopens_discovery(self):
        record = self.record()
        record.record_scan([Message('100', '', 1700000000, 1)], reached_oldest=False)
        record.record_discovered('100', [FoundMedia('100', 1, '100', None)])
        record.record_scan([Message('100', '', 1700000000, 2)], reached_oldest=False)
        self.assertNotIn('100', record.discovered)
        self.assertEqual(record.media, {})

    def test_each_album_source_message_has_record_and_result(self):
        record = self.record()
        item = Message('100', '#第226期', 1700000000, 2,
                       (('100', '', 1700000000, True),
                        ('101', '#第226期', 1700000001, True)))
        record.record_scan([item], reached_oldest=False)
        record.record_result('100', 'skipped')
        record.record_result('101', 'downloaded')
        self.assertEqual(set(record.source_messages), {'100', '101'})
        self.assertEqual(record.source_messages['101']['text'], '#第226期')
        self.assertEqual(record.results['100']['status'], 'skipped')
        self.assertEqual(record.results['101']['status'], 'downloaded')

    def test_invalidate_incomplete_album_discovery(self):
        record = self.record()
        record.record_discovered('100', [FoundMedia('100', 1, '100', None)])
        record.invalidate_media('100')
        self.assertNotIn('100', record.discovered)
        self.assertFalse(record.media)

    def test_scanner_stops_at_known_message_without_claiming_oldest(self):
        older = Message('100', '', 1700000000, 1)
        newer = Message('101', '新消息', 1700000001, 1)
        scroll = MagicMock()
        scroll.wait_for = AsyncMock()
        scroll.evaluate = AsyncMock()
        page = MagicMock()
        page.locator.return_value.first = scroll
        page.wait_for_timeout = AsyncMock()
        with patch('telegg.browser.visible_messages', new_callable=AsyncMock,
                   return_value=[older, newer]), redirect_stdout(io.StringIO()):
            messages, progress = asyncio.run(self.scan(page, None, {'100'}))
        self.assertEqual([item.mid for item in messages], ['101'])
        self.assertFalse(progress.reached_oldest)
        self.assertTrue(progress.stopped_on_known)
        page.mouse.wheel.assert_not_called()

    def test_limited_scan_does_not_claim_oldest(self):
        messages = [Message('100', '', 1700000000, 1),
                    Message('101', '文字', 1700000001, 1)]
        scroll = MagicMock()
        scroll.wait_for = AsyncMock()
        scroll.evaluate = AsyncMock()
        page = MagicMock()
        page.locator.return_value.first = scroll
        page.wait_for_timeout = AsyncMock()
        with patch('telegg.browser.visible_messages', new_callable=AsyncMock,
                   return_value=messages), redirect_stdout(io.StringIO()):
            messages, progress = asyncio.run(self.scan(page, 1))
        self.assertEqual([item.mid for item in messages], ['101'])
        self.assertFalse(progress.reached_oldest)

    def test_oldest_requires_stable_top(self):
        newer = Message('101', '文字', 1700000001, 0)
        older = Message('100', '文字', 1700000000, 0)
        scroll = MagicMock()
        scroll.wait_for = AsyncMock()
        scroll.evaluate = AsyncMock(return_value={'top': True, 'loading': False})
        scroll.hover = AsyncMock()
        page = MagicMock()
        page.locator.return_value.first = scroll
        page.wait_for_timeout = AsyncMock()
        page.mouse.wheel = AsyncMock()
        with patch('telegg.browser.visible_messages', new_callable=AsyncMock,
                   side_effect=[[newer], *[[older, newer]] * 40]), redirect_stdout(io.StringIO()):
            messages, progress = asyncio.run(self.scan(page, None))
        self.assertTrue(progress.reached_oldest)
        self.assertEqual([item.mid for item in messages], ['101', '100'])
        self.assertGreaterEqual(page.mouse.wheel.await_count, 31)

    def test_stuck_at_top_does_not_claim_oldest(self):
        message = Message('100', '文字', 1700000000, 0)
        scroll = MagicMock()
        scroll.wait_for = AsyncMock()
        scroll.evaluate = AsyncMock(return_value={'top': True, 'loading': False})
        scroll.hover = AsyncMock()
        page = MagicMock()
        page.locator.return_value.first = scroll
        page.wait_for_timeout = AsyncMock()
        page.mouse.wheel = AsyncMock()
        with patch('telegg.browser.visible_messages', new_callable=AsyncMock,
                   return_value=[message]), redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, '无法确认已扫描到历史顶部'):
                asyncio.run(self.scan(page, None))

    def test_legacy_completion_marker_is_not_trusted(self):
        record = self.record()
        record.record_scan([Message('100', '文字', 1700000000, 0)], reached_oldest=False)
        record.append([{'kind': 'history_complete'}])
        self.assertFalse(ScanRecord(record.path).history_complete)

    def test_server_empty_history_proves_oldest(self):
        message = Message(str((1 << 32) + 6), '', 1788351294, 0)
        scroll = MagicMock()
        scroll.wait_for = AsyncMock()
        scroll.evaluate = AsyncMock(return_value={'top': True, 'loading': False})
        scroll.hover = AsyncMock()
        page = MagicMock()
        page.locator.return_value.first = scroll
        page.wait_for_timeout = AsyncMock()
        page.mouse.wheel = AsyncMock()
        progress = ScanProgress()

        async def collect():
            return [item async for item in iter_history(page, None, set(), progress,
                                                        peer_id=-1000000004)]

        with patch('telegg.browser.visible_messages', new_callable=AsyncMock,
                   return_value=[message]), patch('telegg.browser.server_older_route_id',
                   new_callable=AsyncMock, return_value=None) as probe, redirect_stdout(io.StringIO()):
            messages = asyncio.run(collect())
        self.assertEqual([item.mid for item in messages], [message.mid])
        self.assertTrue(progress.reached_oldest)
        probe.assert_awaited_once_with(page, -1000000004, int(message.mid))

    def test_server_older_message_prevents_false_completion(self):
        message = Message(str((1 << 32) + 100), '', 1788351294, 0)
        scroll = MagicMock()
        scroll.wait_for = AsyncMock()
        scroll.evaluate = AsyncMock(return_value={'top': True, 'loading': False})
        scroll.hover = AsyncMock()
        page = MagicMock()
        page.url = 'https://web.telegram.org/k/#-1000000004'
        page.goto = AsyncMock()
        page.locator.return_value.first = scroll
        page.wait_for_timeout = AsyncMock()
        page.mouse.wheel = AsyncMock()
        progress = ScanProgress()

        async def collect():
            return [item async for item in iter_history(page, None, set(), progress,
                                                        peer_id=-1000000004)]

        with patch('telegg.browser.visible_messages', new_callable=AsyncMock,
                   return_value=[message]), patch('telegg.browser.server_older_route_id',
                   new_callable=AsyncMock, return_value=90), redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, '不能确认历史最早端'):
                asyncio.run(collect())
        self.assertFalse(progress.reached_oldest)
        self.assertEqual(page.goto.await_count, 2)
        self.assertEqual(page.goto.await_args_list[0].args, ('about:blank',))
        page.goto.assert_awaited_with('https://web.telegram.org/k/#-1000000004?message=90',
                                     wait_until='domcontentloaded')

    def test_api_scan_finishes_when_server_has_no_older_content(self):
        newer = Message(str((1 << 32) + 7), '新', 1788351394, 0)
        oldest = Message(str((1 << 32) + 6), '旧', 1788351294, 0)
        scroll = MagicMock()
        scroll.wait_for = AsyncMock()
        scroll.evaluate = AsyncMock(return_value={'top': True, 'loading': False})
        scroll.hover = AsyncMock()
        page = MagicMock()
        page.locator.return_value.first = scroll
        page.wait_for_timeout = AsyncMock()
        page.mouse.wheel = AsyncMock()
        progress = ScanProgress()

        async def collect():
            return [item async for item in iter_history(page, None, set(), progress,
                peer_id=-1000000004, api_scan=True)]

        with patch('telegg.browser.visible_messages', new_callable=AsyncMock,
                   return_value=[newer]), patch('telegg.browser.server_older_batch',
                   new_callable=AsyncMock, side_effect=[[newer, oldest], []]), redirect_stdout(io.StringIO()):
            messages = asyncio.run(collect())
        self.assertEqual([item.mid for item in messages], [newer.mid, oldest.mid])
        self.assertTrue(progress.reached_oldest)

    def test_september_stall_continues_to_august_sample(self):
        # 群组 -1000000003 的旧误判停在 9 月；真实最早消息日期样例是 2026-08-23。
        september = Message('4294974010', '九月', 1789872715, 0)
        august = Message('4294973000', '八月',
                         int(datetime(2026, 8, 23, tzinfo=timezone.utc).timestamp()), 0)
        scroll = MagicMock()
        scroll.wait_for = AsyncMock()
        scroll.evaluate = AsyncMock(return_value={'top': True, 'loading': False})
        scroll.hover = AsyncMock()
        page = MagicMock()
        page.locator.return_value.first = scroll
        page.wait_for_timeout = AsyncMock()
        page.mouse.wheel = AsyncMock()
        progress = ScanProgress()

        async def collect():
            return [item async for item in iter_history(page, None, set(), progress,
                                                        peer_id=-1000000003, api_scan=True)]

        with patch('telegg.browser.visible_messages', new_callable=AsyncMock,
                   return_value=[september]), patch('telegg.browser.server_older_batch',
                   new_callable=AsyncMock, side_effect=[[september], [august], []]), redirect_stdout(io.StringIO()):
            messages = asyncio.run(collect())
        self.assertEqual([item.mid for item in messages], [september.mid, august.mid])
        self.assertTrue(progress.reached_oldest)

    def test_server_scan_keeps_album_complete_across_pages(self):
        bubble = Message('110', '相册', 1700000110, 2,
                         (('110', '相册', 1700000110, True),
                          ('109', '', 1700000109, True)))
        older = Message('108', '旧', 1700000108, 0)
        album_new = Message('110', '', 1700000110, 1,
                            (('110', '', 1700000110, True),), album_id='77')
        album_old = Message('109', '相册', 1700000109, 1,
                            (('109', '相册', 1700000109, True),), album_id='77')
        scroll = MagicMock()
        scroll.wait_for = AsyncMock()
        scroll.evaluate = AsyncMock(return_value={'top': True, 'loading': False})
        scroll.hover = AsyncMock()
        page = MagicMock()
        page.locator.return_value.first = scroll
        page.wait_for_timeout = AsyncMock()
        page.mouse.wheel = AsyncMock()
        progress = ScanProgress()

        async def collect():
            return [item async for item in iter_history(page, None, set(), progress,
                                                        peer_id=-1, api_scan=True)]

        with patch('telegg.browser.visible_messages', new_callable=AsyncMock,
                   return_value=[bubble]), patch('telegg.browser.server_older_batch',
                   new_callable=AsyncMock, side_effect=[[album_new], [album_old, older], []]) as server, redirect_stdout(io.StringIO()):
            messages = asyncio.run(collect())
        self.assertEqual([item.mid for item in messages], ['109', '108'])
        self.assertEqual([part[0] for part in messages[0].parts], ['109', '110'])
        self.assertEqual(progress.count, 3)
        self.assertEqual(server.await_args_list[0].args[2], 1 << 32)
        self.assertEqual(server.await_args_list[1].args[2], 110)

    def test_incremental_scan_retries_cached_media(self):
        record = self.record()
        record.record_scan([Message('100', '文字', 1700000000, 1,
                                    (('100', '文字', 1700000000, True),))],
                           reached_oldest=True, replace=True)
        record.record_discovered('100', [FoundMedia('100', 1, '100', None)])
        calls = []

        async def history(_page, _limit, known_ids, progress, **_kwargs):
            self.assertEqual(known_ids, {'100'})
            progress.stopped_on_known = True
            if False:
                yield None

        async def save(_page, _url, task, _index, **_kwargs):
            calls.append(task.media_mid)
            return 'downloaded', None

        with (patch('telegg.browser.ScanRecord', return_value=record),
              patch('telegg.browser.verified_group_signature', return_value='fixture'),
              patch('telegg.browser.open_group', new_callable=AsyncMock, return_value='群组'),
              patch('telegg.browser.iter_history', side_effect=history),
              patch('telegg.browser.discover_media', new_callable=AsyncMock) as discover,
              patch('telegg.browser.save_media', side_effect=save),
              patch('telegg.browser.restore_scan_position', new_callable=AsyncMock),
              patch('telegg.browser.existing_media_index', return_value={}),
              patch('telegg.browser.archive_empty_relocated_folders'),
              redirect_stdout(io.StringIO())):
            result = asyncio.run(download_group(None, 'https://web.telegram.org/k/#-1',
                                                TEST_ROOT / 'scan-output', None, False))
        self.assertEqual(result, (0, 1, 0, 0))
        self.assertEqual(calls, ['100'])
        discover.assert_not_called()

    def test_limit_uses_older_boundary_without_saving_it(self):
        record = self.record()
        messages = [Message('104', '新', 1700000004, 1),
                    Message('103', '', 1700000003, 1),
                    Message('102', '旧', 1700000002, 1)]
        scanned = []
        saved = []

        async def history(_page, _limit, _known, progress, **_kwargs):
            for item in messages:
                scanned.append(item.mid)
                yield item

        async def discover(_page, _url, item):
            return [FoundMedia(item.mid, 1, item.mid, None)]

        async def save(_page, _url, task, _index, **_kwargs):
            saved.append(task)
            return 'downloaded', None

        with (patch('telegg.browser.ScanRecord', return_value=record),
              patch('telegg.browser.open_group', new_callable=AsyncMock, return_value='群组'),
              patch('telegg.browser.iter_history', side_effect=history),
              patch('telegg.browser.discover_media', side_effect=discover),
              patch('telegg.browser.save_media', side_effect=save),
              patch('telegg.browser.verified_group_signature', return_value='fixture'),
              patch('telegg.browser.restore_scan_position', new_callable=AsyncMock),
              patch('telegg.browser.existing_media_index', return_value={}),
              patch('telegg.browser.archive_empty_relocated_folders'),
              redirect_stdout(io.StringIO())):
            result = asyncio.run(download_group(None, 'https://web.telegram.org/k/#-1',
                                                TEST_ROOT / 'scan-output', 1, False))
        self.assertEqual(scanned, ['104', '103'])
        self.assertEqual(result, (1, 1, 0, 0))
        self.assertEqual([task.media_mid for task in saved], ['104'])
        self.assertIn('-m104', str(saved[0].folder))
        self.assertFalse(record.history_complete)


if __name__ == '__main__':
    unittest.main()
