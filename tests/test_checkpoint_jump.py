"""用虚构消息验证服务器前缀核对与限额跳转。"""

import asyncio
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import AsyncMock, patch

from telegg.browser import completed_source, resume_endpoint, resume_title_anchor, verify_resume_prefix
from telegg.browser import resume_avatar_prefix
from telegg.browser import verified_group_signature
from telegg.scan_record import FoundMedia
from telegg.layout import Message


BASE = 1 << 32


def message(number: int, kind: str = 'photo') -> Message:
    mid = str(BASE + number)
    return Message(mid, '', number, 1, ((mid, '', number, True),),
                   ((mid, kind),))


class CheckpointJumpTests(unittest.TestCase):
    def marked_prefix(self):
        rows = [message(n) for n in range(201, 99, -1)]
        sources = {r.mid: {'timestamp': r.timestamp, 'text': '', 'has_media': True,
                          'content_type': 'photo', 'bubble_mid': r.mid} for r in rows}
        paths = {r.mid: [Path('fixture') / (r.mid + '.bin')] for r in rows}
        found = {r.mid: FoundMedia(r.mid, 1, r.mid, 1) for r in rows}
        record = SimpleNamespace(source_messages=sources, messages={r.mid: r for r in rows},
            media=found, results={mid: {'status': 'downloaded', 'naming_revision': 3, 'detail': str(p[0])}
                                   for mid, p in paths.items()},
            content_files={mid: 'fixture-content' for mid in paths}, verified_messages={})
        for row in rows:
            record.verified_messages[row.mid] = verified_group_signature([row], {row.mid: {row.mid: found[row.mid]}})
        record.mark_message_verified = lambda mid, signature: record.verified_messages.update({mid: signature})
        return rows, record, paths

    def check_marked(self, record, paths, rows, limit=None):
        async def history(_page, _peer, offset, _request):
            return rows[:2] if offset == BASE else rows[2:]
        with patch('telegg.browser.server_history_with_recovery', side_effect=history) as fetch:
            result = asyncio.run(verify_resume_prefix(None, -1000000001, BASE + 100, record, paths, limit))
        return result, fetch.await_count

    def test_valid_marks_jump_after_one_latest_page_without_file_io(self):
        rows, record, paths = self.marked_prefix()
        with patch.object(Path, 'stat', side_effect=AssertionError('must not stat trusted files')):
            result, calls = self.check_marked(record, paths, rows)
        self.assertEqual(result, (BASE + 100, 101, 101))
        self.assertEqual(calls, 1)

    def test_old_prefix_without_marks_is_checked_then_promoted(self):
        rows, record, paths = self.marked_prefix()
        record.verified_messages.clear()
        result, calls = self.check_marked(record, paths, rows)
        self.assertEqual(result, (BASE + 100, 101, 101))
        self.assertEqual(calls, 2)
        self.assertIn(str(BASE + 150), record.verified_messages)
        self.assertEqual(self.check_marked(record, paths, rows)[1], 1)

    def test_stale_marker_is_not_trusted(self):
        rows, record, paths = self.marked_prefix()
        record.verified_messages[str(BASE + 150)] = '0' * 64
        self.assertEqual(self.check_marked(record, paths, rows)[1], 2)

    def test_missing_indexed_file_does_not_get_skipped_by_mark(self):
        rows, record, paths = self.marked_prefix()
        paths.pop(str(BASE + 180))
        result, calls = self.check_marked(record, paths, rows)
        self.assertEqual(result, (BASE + 180, 21, 21))
        self.assertEqual(calls, 2)

    def test_new_latest_message_is_found_before_mark_shortcut(self):
        rows, record, paths = self.marked_prefix()
        result, calls = self.check_marked(record, paths, [message(202), *rows])
        self.assertEqual(result, (BASE + 202, 0, 0))
        self.assertEqual(calls, 1)

    def test_marked_shortcut_respects_limit(self):
        rows, record, paths = self.marked_prefix()
        result, calls = self.check_marked(record, paths, rows, 3)
        self.assertEqual(result, (BASE + 198, 3, 3))
        self.assertEqual(calls, 1)

    def test_marked_shortcut_keeps_whole_album_at_limit(self):
        from dataclasses import replace
        from telegg.browser import combine_server_album
        rows, record, paths = self.marked_prefix()
        rows[2:5] = [replace(r, album_id='fixture-album') for r in rows[2:5]]
        bubble = combine_server_album(rows[2:5])
        for row in rows[2:5]:
            record.source_messages[row.mid]['bubble_mid'] = bubble.mid
            record.messages.pop(row.mid)
        record.messages[bubble.mid] = bubble
        for index, row in enumerate(reversed(rows[2:5]), 1):
            record.media[row.mid] = FoundMedia(bubble.mid, index, row.mid, 1)
        media = {r.mid: record.media[r.mid] for r in rows[2:5]}
        record.verified_messages[bubble.mid] = verified_group_signature([bubble], {bubble.mid: media})
        result, calls = self.check_marked(record, paths, rows, 3)
        self.assertEqual(result, (BASE + 196, 5, 5))
        self.assertEqual(calls, 1)

    def test_changed_latest_album_is_found_before_cached_jump(self):
        from dataclasses import replace
        rows, record, paths = self.marked_prefix()
        rows[0] = replace(rows[0], album_id='new-album')
        result, calls = self.check_marked(record, paths, rows)
        self.assertEqual(result, (BASE + 201, 0, 0))
        self.assertEqual(calls, 1)

    def test_completed_prefix_checks_indexed_path_without_per_file_io(self):
        mid = str(BASE + 104)
        path = Path('fixture-output/correct/file.bin')
        record = SimpleNamespace(results={mid: {
            'status': 'downloaded', 'naming_revision': 3, 'detail': str(path)}},
            content_files={mid: 'fixture-content'})
        with patch.object(Path, 'stat', side_effect=AssertionError('fast prefix must not stat files')):
            self.assertTrue(completed_source(mid, {'content_type': 'photo'}, record, {mid: [path]}))
            self.assertFalse(completed_source(mid, {'content_type': 'photo'}, record,
                                             {mid: [Path('fixture-output/wrong/file.bin')]}))

    def test_resume_carries_all_newer_blocks_in_same_avatar_segment(self):
        rows = [Message(str(BASE + mid), '', 1700000000 + mid, 0, sender_id=sender)
                for mid, sender in ((209, 'same'), (219, 'same'), (229, 'same'),
                                    (230, 'same'), (234, 'other'))]
        self.assertEqual([row.mid for row in resume_avatar_prefix(rows, BASE + 218)],
                         [str(BASE + mid) for mid in (230, 229, 219)])
        self.assertEqual(resume_avatar_prefix(rows, BASE + 234), [])

    def setUp(self):
        self.rows = [message(104), message(103), message(102), message(101)]
        self.record = SimpleNamespace(source_messages={
            row.mid: {'timestamp': row.timestamp, 'text': row.text,
                      'has_media': True, 'content_type': 'photo'} for row in self.rows
        })

    def verify(self, max_messages=None, endpoint=BASE + 101):
        async def history(_page, _peer, offset, _request):
            return self.rows if offset == BASE else []

        with (patch('telegg.browser.server_history_with_recovery', side_effect=history),
              patch('telegg.browser.completed_source', return_value=True)):
            return asyncio.run(verify_resume_prefix(None, -1000000001, endpoint,
                                                    self.record, {}, max_messages))

    def test_latest_batch_reaches_endpoint(self):
        self.assertEqual(self.verify(), (BASE + 101, 3, 3))

    def test_limit_entirely_in_completed_prefix(self):
        self.assertEqual(self.verify(2), (BASE + 102, 2, 2))

    def test_fast_prefix_keeps_album_whole_at_limit(self):
        self.rows = [message(104), message(103), message(102), message(101), message(100)]
        from dataclasses import replace
        self.rows[1:4] = [replace(row, album_id='album') for row in self.rows[1:4]]
        self.record.source_messages = {
            row.mid: {'timestamp': row.timestamp, 'text': row.text,
                      'has_media': True, 'content_type': 'photo'} for row in self.rows}
        self.assertEqual(self.verify(2, BASE + 100), (BASE + 100, 4, 4))

    def test_changed_message_becomes_new_endpoint(self):
        self.record.source_messages[str(BASE + 103)]['content_type'] = 'document'
        self.assertEqual(self.verify(), (BASE + 103, 1, 1))

    def test_new_album_starts_at_newest_member(self):
        from dataclasses import replace
        self.rows[:2] = [replace(row, album_id='new-album') for row in self.rows[:2]]
        del self.record.source_messages[str(BASE + 104)]
        del self.record.source_messages[str(BASE + 103)]
        self.assertEqual(self.verify(), (BASE + 104, 0, 0))

    def test_changed_member_inside_completed_album_falls_back(self):
        from dataclasses import replace
        self.rows[:2] = [replace(row, album_id='same-album') for row in self.rows[:2]]
        self.record.source_messages[str(BASE + 103)]['content_type'] = 'document'
        self.assertIsNone(self.verify())

    def test_resume_inside_group_carries_newer_title(self):
        rows = [Message(str(BASE + 90), '', 90, 1),
                Message(str(BASE + 100), 'title', 100, 0),
                Message(str(BASE + 110), 'next title', 110, 0)]
        self.assertEqual(resume_title_anchor(rows, BASE + 95), rows[1])
        self.assertIsNone(resume_title_anchor(rows, BASE + 110))

    def test_misgrouped_file_is_not_skipped_at_endpoint(self):
        media_mid, title_mid = str(BASE + 90), str(BASE + 100)
        record = SimpleNamespace(
            source_messages={media_mid: {'content_type': 'photo', 'text': ''},
                             title_mid: {'content_type': 'text', 'text': 'title'}},
            results={media_mid: {'status': 'downloaded', 'naming_revision': 2},
                     title_mid: {'status': 'saved_content'}},
            content_files={media_mid: 'saved', title_mid: 'saved'})
        from pathlib import Path
        index = {media_mid: [Path('output') / '未归组' / f'm{media_mid}_1_file.jpg']}
        self.assertEqual(resume_endpoint(record, index), int(media_mid))


if __name__ == '__main__':
    unittest.main()
