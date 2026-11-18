"""当前成品和暂存文件的索引、可信跳过及文件名兼容性。"""

import asyncio
import hashlib
import tempfile
import unittest
from pathlib import Path

from telegg.browser import MediaTask, canonical_media_name, existing_media_index, save_one
from telegg.layout import Message
from telegg.errors import LocalDataError
from telegg.verification_manifest import verified_digest


class ExistingMediaTests(unittest.TestCase):
    def test_media_index_does_not_import_text_or_old_state(self):
        with tempfile.TemporaryDirectory() as temp:
            pool = Path(temp)
            (pool / '2026-10-28-m42_1_备注.txt').write_text('text', encoding='utf-8')
            (pool / '2026-10-28-m42_1_123.txt').write_text('text', encoding='utf-8')
            (pool / '2026-10-28-m43_1_777.bin').write_bytes(b'media')
            (pool / 'm44_1_document_778.zip').write_bytes(b'archive')
            (pool / '45.jsonl').write_text('old state', encoding='utf-8')
            self.assertEqual(set(existing_media_index(pool)), {'43', '44'})
            self.assertNotIn('42', existing_media_index(pool))


    def test_downloaded_text_document_is_reused_with_known_media_id(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            path = base / '2023-11-15-m42_1_777_report.txt'
            path.write_bytes(b'data')
            self.assertNotIn('42', existing_media_index(base))
            self.assertEqual(existing_media_index(base, known_text_media={'42'}), {'42': [path]})
            task = MediaTask(Message('42', '', 1700000000, 1), 1, '42', base, 4,
                             base / 'partials')
            source = {'peer': '-1', 'mid': '42', 'kind': 'document', 'id': '777',
                      'dc': 1, 'thumb': '', 'size': 4, 'name': 'report.txt',
                      'original_name': 'report.txt', 'mime_type': 'text/plain'}
            index = {}
            identity = {key: source[key] for key in ('peer', 'mid', 'kind', 'id', 'dc', 'thumb')}
            verified_digest(path, identity, base / 'partials' / '42.verified.jsonl',
                            lambda p: hashlib.sha256(p.read_bytes()).hexdigest())
            state, final, *_ = asyncio.run(save_one(None, None, task, index,
                                                     server_source=source, exact_verify=True))
            self.assertEqual(state, 'skipped')
            self.assertEqual(final, path)
            self.assertEqual(index, {'42': [path]})


    def test_document_original_name_and_photo_without_name(self):
        item = Message('42', '', 1700000000, 1)
        task = MediaTask(item, 2, '42', Path('output'), 4)
        document = {'kind': 'document', 'id': '777', 'name': '原稿.v1.mp4',
                    'original_name': '原稿.v1.mp4', 'mime_type': 'video/mp4'}
        photo = {'kind': 'photo', 'id': '888', 'name': 'photo_888_y.jpg',
                 'original_name': '', 'mime_type': 'image/jpeg'}
        self.assertEqual(canonical_media_name(task, document),
                         '2023-11-15-m42_2_777_原稿.v1.mp4')
        self.assertEqual(canonical_media_name(task, photo),
                         '2023-11-15-m42_2_888.jpg')


    def test_unique_media_id_allows_old_album_position_to_move(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            old = base / '2023-11-15_m42_1_777.jpg'
            old.write_bytes(b'file')
            task = MediaTask(Message('42', '', 1700000000, 1), 3, '42', base, 4,
                             base / 'partials')
            source = {'peer': '-1', 'mid': '42', 'kind': 'photo', 'id': '777',
                      'dc': 1, 'thumb': 'y', 'size': 4, 'name': 'photo_777_y.jpg',
                      'original_name': '', 'mime_type': 'image/jpeg'}
            with self.assertRaisesRegex(LocalDataError, '路径不符合规范'):
                asyncio.run(save_one(None, None, task, {'42': [old]},
                                     server_source=source, exact_verify=True))
            self.assertTrue(old.is_file())
            state, final, *_ = asyncio.run(save_one(None, None, task, {'42': [old]},
                                                     server_source=source))
            self.assertEqual(state, 'relocated')
            self.assertEqual(final.name, '2023-11-15-m42_3_777.jpg')
            self.assertEqual(final.read_bytes(), b'file')
