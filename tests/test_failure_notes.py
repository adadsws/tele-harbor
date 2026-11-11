"""预测冲突与真实服务器失败反馈：只使用中性内容，保留测试产物。"""
import asyncio
import hashlib
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from telegg.browser import MediaTask, canonical_media_name, save_one
from telegg.errors import LocalDataError, RemoteFileError
from telegg.failure_notes import (REGISTRY, LEGACY_REGISTRY, failure_note_path,
                                  migrate_registry, registered_notes, retire_failure_note,
                                  validate_failure_notes, write_failure_note)
from telegg.layout import Message
from telegg.local_config import load_paths
from telegg.local_validation import validate_local_archive
from telegg.media_index import existing_media_index


class FailureNoteTests(unittest.TestCase):
    def setUp(self):
        self.root = load_paths()['test_artifacts'] / 'failure-notes' / uuid4().hex
        self.root.mkdir(parents=True)
        self.media = self.root / '2026-01-01-m42_1_777_example.tar.gz'
        self.error = RemoteFileError(500, 'UNKNOWN')

    def test_last_extension_only_and_generated_template(self):
        note = write_failure_note(self.media, '42', 123, self.error)
        self.assertEqual(note.name, '2026-01-01-m42_1_777_example.tar.txt')
        text = note.read_text(encoding='utf-8')
        self.assertIn('脚本自动生成', text)
        self.assertIn('500 UNKNOWN', text)
        self.assertIn('预期字节数：123', text)
        self.assertEqual(write_failure_note(self.media, '42', 123, self.error), note)
        self.assertEqual(len((self.root / REGISTRY).read_text(encoding='utf-8').splitlines()), 1)
        self.assertFalse(self.media.exists())
        self.assertTrue(all(not p.name.startswith('%') for p in self.root.iterdir()))

    def test_index_ignores_registered_note_but_keeps_real_txt(self):
        write_failure_note(self.media, '42', 123, self.error)
        real = self.root / '2026-01-01-m43_1_778_real.txt'
        real.write_bytes(b'real telegram text attachment')
        self.assertEqual(existing_media_index(self.root, known_text_media={'42', '43'}), {'43': [real]})

    def test_unregistered_same_name_is_never_overwritten(self):
        note = failure_note_path(self.media)
        note.write_bytes(b'user data')
        with self.assertRaises(LocalDataError):
            write_failure_note(self.media, '42', 123, self.error)
        retire_failure_note(self.media, self.root / 'archive')
        self.assertEqual(note.read_bytes(), b'user data')
        self.assertFalse((self.root / REGISTRY).exists())

    def test_edited_note_errors_without_mutation(self):
        note = write_failure_note(self.media, '42', 123, self.error)
        note.write_bytes(b'user edited data')
        before = {p.name: p.read_bytes() for p in self.root.iterdir()}
        for action in (lambda: write_failure_note(self.media, '42', 123, self.error),
                       lambda: retire_failure_note(self.media, self.root / 'archive'),
                       lambda: validate_local_archive(self.root, self.root / 'staging', None,
                                                       self.root / 'partials')):
            with self.assertRaises(LocalDataError):
                action()
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.iterdir()})

    def test_corrupt_registry_is_rejected(self):
        (self.root / REGISTRY).write_text('{torn', encoding='utf-8')
        with self.assertRaises(LocalDataError):
            validate_failure_notes(self.root)

    def test_archive_only_owned_note_preserves_media(self):
        note = write_failure_note(self.media, '42', 123, self.error)
        payload = note.read_bytes()
        self.media.write_bytes(b'real original')
        archive = self.root / 'archive'
        retire_failure_note(self.media, archive)
        self.assertFalse(note.exists())
        self.assertFalse((self.root / REGISTRY).exists())
        self.assertEqual(self.media.read_bytes(), b'real original')
        self.assertEqual(next(archive.rglob(note.name)).read_bytes(), payload)
        self.assertEqual(existing_media_index(archive, known_text_media={'42'}), {})
        validate_failure_notes(archive)
        retire_failure_note(self.media, archive)

    def _task(self, suffix):
        source = {'peer': '-1', 'mid': '42', 'kind': 'document', 'id': '777',
                  'dc': 1, 'thumb': '', 'size': 4, 'name': f'report.{suffix}',
                  'original_name': f'report.{suffix}', 'mime_type': 'application/octet-stream'}
        task = MediaTask(Message('42', '', 1700000000, 1), 1, '42', self.root / 'files',
                         4, self.root / 'partials')
        return task, source, task.folder / canonical_media_name(task, source)

    def test_save_one_generates_note_and_keeps_failed_download_unindexed(self):
        task, source, media = self._task('zip')
        index = {}
        with patch('telegg.browser.download_parts', new_callable=AsyncMock, side_effect=self.error):
            with self.assertRaises(RemoteFileError):
                asyncio.run(save_one(None, None, task, index, server_source=source))
        self.assertTrue(failure_note_path(media).is_file())
        self.assertFalse(media.exists())
        self.assertEqual(index, {})
        self.assertEqual(existing_media_index(task.folder, known_text_media={'42'}), {})
        validate_failure_notes(task.folder)

    def _retry(self, suffix):
        task, source, media = self._task(suffix)
        write_failure_note(media, '42', 4, self.error)
        async def fetch(*args):
            return b'data'
        with patch('telegg.browser.fetch_resume_part', side_effect=fetch):
            result = asyncio.run(save_one(None, None, task, {}, server_source=source,
                                          exact_verify=True))
        self.assertEqual(result[0], 'downloaded')
        self.assertEqual(result[3], hashlib.sha256(b'data').hexdigest())
        self.assertEqual(media.read_bytes(), b'data')
        self.assertFalse((task.folder / REGISTRY).exists())
        self.assertTrue(list((task.partial_root / '~archive').rglob(REGISTRY)))
        self.assertIn('42', existing_media_index(task.folder, known_text_media={'42'}))
        return task, source, media

    def test_txt_original_retry_downloads_instead_of_skipping_note(self):
        self._retry('txt')

    def test_binary_retry_retires_note_only_after_verified_success(self):
        task, source, media = self._retry('zip')
        write_failure_note(media, '42', 4, self.error)
        result = asyncio.run(save_one(None, None, task, {'42': [media]},
                                      server_source=source, exact_verify=True))
        self.assertEqual(result[0], 'skipped')
        self.assertFalse(failure_note_path(media).exists())

    def test_multiple_notes_retire_independently(self):
        other = self.root / '2026-01-01-m43_1_778_other.zip'
        first = write_failure_note(self.media, '42', 123, self.error)
        second = write_failure_note(other, '43', 124, self.error)
        retire_failure_note(self.media, self.root / 'archive')
        self.assertFalse(first.exists())
        self.assertTrue(second.is_file())
        self.assertEqual(set(registered_notes(self.root)), {second.name})
        validate_failure_notes(self.root)
        self.assertEqual(existing_media_index(self.root, known_text_media={'42', '43'}), {})

    def _legacy(self):
        note = write_failure_note(self.media, '42', 123, self.error)
        legacy = self.root / LEGACY_REGISTRY
        (self.root / REGISTRY).rename(legacy)
        return note, legacy

    def test_legacy_read_only_index_then_write_migrates_registry(self):
        note, legacy = self._legacy()
        before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_ino)
                  for p in self.root.iterdir()}
        self.assertEqual(existing_media_index(self.root, known_text_media={'42'}), {})
        validate_failure_notes(self.root)
        self.assertEqual(before, {p.name: (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_ino)
                                 for p in self.root.iterdir()})
        self.assertEqual(write_failure_note(self.media, '42', 123, self.error), note)
        self.assertFalse(legacy.exists())
        self.assertEqual((self.root / REGISTRY).read_bytes(), before[legacy.name][0])
        self.assertEqual(note.read_bytes(), before[note.name][0])
        self.assertEqual(existing_media_index(self.root, known_text_media={'42'}), {})

    def test_retire_legacy_registry_keeps_new_archive_names(self):
        note, legacy = self._legacy()
        archive = self.root / 'archive'
        retire_failure_note(self.media, archive)
        self.assertFalse(note.exists())
        self.assertFalse(legacy.exists())
        self.assertTrue(list(archive.rglob(REGISTRY)))
        self.assertFalse(list(self.root.rglob(LEGACY_REGISTRY)))
        self.assertEqual(existing_media_index(archive, known_text_media={'42'}), {})
        validate_failure_notes(archive)

    def test_new_and_legacy_collision_reports_without_changes(self):
        _, legacy = self._legacy()
        (self.root / REGISTRY).write_bytes(legacy.read_bytes())
        before = {p.name: p.read_bytes() for p in self.root.iterdir()}
        for action in (lambda: migrate_registry(self.root),
                       lambda: existing_media_index(self.root, known_text_media={'42'}),
                       lambda: validate_failure_notes(self.root)):
            with self.assertRaises(LocalDataError):
                action()
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.iterdir()})

    def test_modified_legacy_note_blocks_migration_without_changes(self):
        note, legacy = self._legacy()
        note.write_bytes(b'user modified note')
        before = {p.name: p.read_bytes() for p in self.root.iterdir()}
        with self.assertRaises(LocalDataError):
            migrate_registry(self.root)
        self.assertTrue(legacy.exists())
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.iterdir()})

    def test_empty_legacy_registry_is_rejected_without_rename(self):
        legacy = self.root / LEGACY_REGISTRY
        legacy.write_bytes(b'')
        with self.assertRaises(LocalDataError):
            migrate_registry(self.root)
        self.assertTrue(legacy.is_file())
        self.assertFalse((self.root / REGISTRY).exists())
