"""逐文件检查必须在任何修复之前拒绝损坏的本地结果。"""

import hashlib
import json
from pathlib import Path
import unittest
import uuid
from dataclasses import replace

from telegg.errors import LocalDataError
from telegg.layout import Message, group_folder
from telegg.local_validation import validate_local_archive
from telegg.scan_record import ScanRecord, FoundMedia
from telegg.content_archive import save_group_manifest, content_payload
from telegg.verification_manifest import verified_digest


class LocalValidationTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(__file__).resolve().parents[1] / '~temp' / 'tests' / uuid.uuid4().hex
        self.root = self.base / 'output'
        self.staging = self.base / 'staging'
        self.partials = self.base / 'partials'
        self.item = Message('42', '', 1700000000, 1,
                            (('42', '', 1700000000, True),), (('42', 'document'),), sender_id='sender')
        self.folder = self.root / group_folder(self.item)
        self.folder.mkdir(parents=True)
        self.path = self.folder / '2023-11-15-m42_1_remote.bin'
        self.path.write_bytes(b'original')
        self.content = self.base / '42.json'
        self.content.write_text(json.dumps(content_payload(self.item, self.item.parts[0])), encoding='utf-8')
        save_group_manifest(self.folder, [self.item])
        self.record = ScanRecord(self.base / 'record.jsonl')
        self.record.messages['42'] = self.item
        self.record.results['42'] = {'status': 'downloaded', 'detail': str(self.path)}
        self.record.content_files['42'] = str(self.content)
        self.record.media['42'] = FoundMedia('42', 1, '42', 8)
        self.journal = self.partials / '42.verified.jsonl'
        self.identity = {'mid': '42', 'id': 'remote'}
        verified_digest(self.path, self.identity, self.journal, self.hash)

    @staticmethod
    def hash(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def check(self):
        validate_local_archive(self.root, self.staging, self.record, self.partials)

    def test_valid_preflight_is_read_only(self):
        before = {p: p.read_bytes() for p in self.base.rglob('*') if p.is_file()}
        self.check()
        self.assertEqual(before, {p: p.read_bytes() for p in self.base.rglob('*') if p.is_file()})

    def pending_link(self):
        root, staging = self.base / 'linked-output', self.base / 'linked-staging'
        target = Message('100', 'target', 1700000000, 0,
                         (('100', 'target', 1700000000, False),), (('100', 'text'),), sender_id='a')
        source = Message('200', 'source', 1700000600, 0,
                         (('200', 'source', 1700000600, False),), (('200', 'text'),),
                         reply_targets=(('200', '100'),), sender_id='b')
        record = ScanRecord(self.base / 'pending-record.jsonl')
        record.record_scan([target, source], reached_oldest=False)
        parent = staging / (group_folder(target) + ' 关联组')
        folder = parent / group_folder(source)
        save_group_manifest(folder, [source])
        content = folder / '2023-11-15-m200_source.txt'
        content.write_text('source', encoding='utf-8')
        record.record_content_file('200', content)
        record.record_result('200', 'saved_content')
        record.record_result('100', 'failed')
        manifest = parent / '关联清单.json'
        manifest.write_text(json.dumps({'replies': [{'source_message_id': '200', 'target_message_id': '100'}]}), encoding='utf-8')
        return root, staging, record, manifest

    def test_pending_link_with_missing_target_files_is_valid_and_read_only(self):
        root, staging, record, _ = self.pending_link()
        before = {p: p.read_bytes() for p in self.base.rglob('*') if p.is_file()}
        validate_local_archive(root, staging, record, self.partials)
        self.assertEqual(before, {p: p.read_bytes() for p in self.base.rglob('*') if p.is_file()})

    def test_missing_staged_link_manifest_is_rejected_before_mutation(self):
        root, staging, record, manifest = self.pending_link()
        manifest.rename(self.base / 'held-link.json')
        before = {p: p.read_bytes() for p in self.base.rglob('*') if p.is_file()}
        with self.assertRaises(LocalDataError):
            validate_local_archive(root, staging, record, self.partials)
        self.assertEqual(before, {p: p.read_bytes() for p in self.base.rglob('*') if p.is_file()})

    def test_corrupt_staged_link_manifest_is_rejected_before_mutation(self):
        root, staging, record, manifest = self.pending_link()
        manifest.write_bytes(b'{broken')
        before = {p: p.read_bytes() for p in self.base.rglob('*') if p.is_file()}
        with self.assertRaises(LocalDataError):
            validate_local_archive(root, staging, record, self.partials)
        self.assertEqual(before, {p: p.read_bytes() for p in self.base.rglob('*') if p.is_file()})

    def test_wrong_staged_reply_target_is_rejected_before_mutation(self):
        root, staging, record, manifest = self.pending_link()
        manifest.write_text(json.dumps({'replies': [{'source_message_id': '200', 'target_message_id': '999'}]}), encoding='utf-8')
        before = {p: p.read_bytes() for p in self.base.rglob('*') if p.is_file()}
        with self.assertRaises(LocalDataError):
            validate_local_archive(root, staging, record, self.partials)
        self.assertEqual(before, {p: p.read_bytes() for p in self.base.rglob('*') if p.is_file()})

    def test_duplicate_media_is_rejected_before_records_change(self):
        first = self.folder / '2023-11-15-m999_1_777.bin'
        second = self.folder / '2023-11-15-m999_1_777_duplicate.bin'
        first.write_bytes(b'fixture')
        second.write_bytes(b'fixture')
        before = {p: p.read_bytes() for p in self.base.rglob('*') if p.is_file()}
        with self.assertRaisesRegex(LocalDataError, '重复媒体'):
            self.check()
        self.assertEqual(before, {p: p.read_bytes() for p in self.base.rglob('*') if p.is_file()})

    def test_renamed_completed_file_is_not_repaired(self):
        moved = self.path.with_name('wrong.bin')
        self.path.rename(moved)
        with self.assertRaisesRegex(LocalDataError, '缺失'):
            self.check()
        self.assertTrue(moved.is_file())
        self.assertFalse(self.path.exists())

    def test_bad_manifest_and_empty_directory_are_errors(self):
        manifest = self.folder / '段内清单.json'
        original = manifest.read_bytes()
        manifest.write_text('{"items": []}', encoding='utf-8')
        with self.assertRaisesRegex(LocalDataError, '清单成员'):
            self.check()
        manifest.write_bytes(original)
        (self.folder / 'empty').mkdir()
        with self.assertRaisesRegex(LocalDataError, '空文件夹'):
            self.check()

    def test_bad_snapshot_and_same_size_corruption_do_not_reset_baseline(self):
        baseline = self.journal.read_bytes()
        self.path.write_bytes(b'changed!')
        with self.assertRaisesRegex(LocalDataError, 'SHA-256'):
            verified_digest(self.path, self.identity, self.journal, self.hash, exact=True)
        self.assertEqual(baseline, self.journal.read_bytes())
        self.journal.write_text('broken', encoding='utf-8')
        with self.assertRaisesRegex(LocalDataError, '清单损坏'):
            self.check()

    def test_exact_hash_cannot_trust_a_renamed_corrupt_file(self):
        moved = self.path.with_name('moved.bin')
        self.path.rename(moved)
        moved.write_bytes(b'changed!')
        with self.assertRaisesRegex(LocalDataError, 'SHA-256'):
            verified_digest(moved, self.identity, self.journal, self.hash, exact=True)

    def test_corrupt_text_is_not_silently_versioned(self):
        item = replace(self.item, text='fixture', parts=(('42', 'fixture', 1700000000, True),))
        self.record.messages['42'] = item
        path = self.folder / '2023-11-15-m42_fixture.txt'
        path.write_bytes(b'fixture')
        self.record.content_files['42'] = str(path)
        self.check()
        path.write_bytes(b'corrupt')
        with self.assertRaisesRegex(LocalDataError, '原文文件内容或路径'):
            self.check()
        self.assertEqual(path.read_bytes(), b'corrupt')
        self.assertFalse(list(self.folder.glob('*.v*.txt')))

    def test_missing_baseline_is_not_created_for_a_completed_file(self):
        saved = self.journal.with_suffix('.saved')
        self.journal.rename(saved)
        with self.assertRaisesRegex(LocalDataError, '缺少验收清单'):
            self.check()
        self.assertFalse(self.journal.exists())
        self.assertTrue(saved.exists())
