"""用虚构消息验证服务器前缀核对与限额跳转。"""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegg.browser import resume_endpoint, resume_title_anchor, verify_resume_prefix
from telegg.browser import resume_avatar_prefix
from telegg.layout import Message


BASE = 1 << 32


def message(number: int, kind: str = 'photo') -> Message:
    mid = str(BASE + number)
    return Message(mid, '', number, 1, ((mid, '', number, True),),
                   ((mid, kind),))


class CheckpointJumpTests(unittest.TestCase):
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
