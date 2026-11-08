"""本地复用区迁移与旧附件位置重建。"""

import asyncio
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from telegg.browser import MediaTask, canonical_media_name, existing_media_index, save_one
from telegg.layout import Message
from telegg.local_reuse import rekey_legacy_media, reuse_root, stage_existing_group
from telegg.errors import LocalDataError
from telegg.verification_manifest import verified_digest


class LocalReuseTests(unittest.TestCase):
    def test_legacy_file_rekey_requires_complete_matching_chunks(self):
        with tempfile.TemporaryDirectory() as temp:
            archive = Path(temp)
            pool = archive / '-1'
            pool.mkdir()
            valid = pool / 'm42_1_old.zip'
            corrupt = pool / 'm43_1_old.zip'
            valid.write_bytes(b'good')
            corrupt.write_bytes(b'bad!')
            for mid in ('42', '43'):
                journal = archive / f'{mid}.jsonl'
                identity = {'peer': '-1', 'mid': mid, 'kind': 'document',
                            'id': '6066461496794164614', 'dc': 5, 'thumb': ''}
                journal.write_text(''.join(json.dumps(event) + '\n' for event in (
                    {'kind': 'identity', 'identity': identity, 'size': 4,
                     'chunk_size': 1048576},
                    {'kind': 'chunk', 'offset': 0, 'length': 4,
                     'sha256': hashlib.sha256(b'good').hexdigest()},
                )), encoding='utf-8')
            self.assertEqual(rekey_legacy_media(pool, archive, '-1'), 1)
            self.assertTrue((pool / 'm42_1_6066461496794164614_old.zip').is_file())
            self.assertTrue(corrupt.is_file())
            self.assertEqual(rekey_legacy_media(pool, archive, '-1'), 0)

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

    def test_stage_both_roots_without_overwrite(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            output = base / 'downloaded'
            finished, pending = output / 'group', base / 'staging' / 'group'
            finished.mkdir(parents=True)
            pending.mkdir(parents=True)
            (finished / 'm42_2_file.bin').write_bytes(b'file')
            (pending / 'm43_1_file.bin').write_bytes(b'part')
            messages = base / 'state' / 'messages' / 'group'
            messages.mkdir(parents=True)
            (messages / '42.txt').write_text('saved', encoding='utf-8')
            record = base / 'state' / 'scan-records' / '-1000000001.jsonl'
            record.parent.mkdir(parents=True)
            record.write_text('{}\n', encoding='utf-8')
            partials = base / 'downloading' / 'partials' / '-1000000001'
            partials.mkdir(parents=True)
            (partials / '42.jsonl').write_text('journal', encoding='utf-8')
            pool = reuse_root(output, '-1000000001')
            with self.assertRaisesRegex(RuntimeError, '已禁用'):
                stage_existing_group(finished, pending, pool,
                                     content_group=messages, scan_record=record,
                                     partial_group=partials)
            self.assertTrue((finished / 'm42_2_file.bin').is_file())
            self.assertTrue((pending / 'm43_1_file.bin').is_file())
            self.assertTrue((messages / '42.txt').is_file())
            self.assertTrue(record.is_file())
            self.assertTrue((partials / '42.jsonl').is_file())
            self.assertFalse(pool.exists())
            with self.assertRaisesRegex(RuntimeError, '已禁用'):
                stage_existing_group(finished, pending, pool)

    def test_existing_old_position_moves_into_canonical_flat_name(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            pool = base / 'reuse-pool' / '-1000000001'
            old = pool / 'output' / 'old' / 'm42_2_document_777.bin'
            old.parent.mkdir(parents=True)
            old.write_bytes(b'file')
            old_partial = pool / 'latest-partials' / '42.jsonl'
            old_partial.parent.mkdir(parents=True)
            old_partial.write_text('old state', encoding='utf-8')
            folder = base / 'downloaded' / 'group'
            source = {'peer': '-1', 'mid': '42', 'kind': 'document', 'id': '777',
                      'dc': 1, 'thumb': '', 'size': 4, 'name': 'document_777.bin',
                      'mime_type': 'application/octet-stream'}
            message = Message('42', 'caption', 1700000000, 1,
                              (('42', 'caption', 1700000000, True),), (('42', 'document'),))
            task = MediaTask(message, 1, '42', folder, 4, base / 'partials', 1, 1, pool)
            state, final, *_ = asyncio.run(save_one(None, None, task, {'42': [old]},
                                                     server_source=source))
            self.assertEqual(state, 'relocated')
            self.assertEqual(final.name, '2023-11-15-m42_1_777.bin')
            self.assertEqual(final.read_bytes(), b'file')
            self.assertFalse(old.exists())
            self.assertEqual(old_partial.read_text(encoding='utf-8'), 'old state')
            self.assertFalse((base / 'partials' / '42.jsonl').exists())

    def test_reuse_rejects_wrong_media_id_without_moving_file(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            pool = base / 'reuse'
            old = pool / 'm42_1_document_777.bin'
            old.parent.mkdir(parents=True)
            old.write_bytes(b'file')
            folder = base / 'downloaded'
            source = {'peer': '-1', 'mid': '42', 'kind': 'document', 'id': '778',
                      'dc': 1, 'thumb': '', 'size': 4, 'name': 'document_778.bin',
                      'mime_type': 'application/octet-stream'}
            task = MediaTask(Message('42', '', 1700000000, 1), 1, '42', folder,
                             4, base / 'partials', 1, 1, pool)
            with self.assertRaisesRegex(RuntimeError, '媒体 ID 与服务器不符'):
                asyncio.run(save_one(None, None, task, {'42': [old]}, server_source=source))
            self.assertEqual(old.read_bytes(), b'file')
            self.assertFalse(folder.exists())

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


if __name__ == '__main__':
    unittest.main()
