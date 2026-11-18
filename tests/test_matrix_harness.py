"""矩阵构造器必须确实制造目标差异，且不改源文件；不连接 Telegram。"""
import hashlib
import json
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path

from matrix_harness.catalog import CASES, select
from matrix_harness.fixtures import build, rows, save_rows
from matrix_harness.isolation import copy_case, sha
from matrix_harness.runner import sample_batch
from matrix_harness.history_window import HOOK
from telegg.resumable import CHUNK_SIZE, PartialFile
from telegg.scan_record import ScanRecord
from live_layout_progress import legacy_copy
from telegg.verification_manifest import verified_digest, read_snapshot
from mode_review.fixtures import business_snapshot, production_layout, snapshot, storage_fault
from telegg.extra_files import loose_storage_files
from check_all_modes_evidence import check_journal


class MatrixHarnessTests(unittest.TestCase):
    def test_download_business_comparison_allows_only_owned_caches(self):
        root=Path(__file__).resolve().parents[1]/'~temp/tests'/uuid.uuid4().hex
        roots={'output':root/'downloaded','staging':root/'downloading/groups','partials':root/'downloading/partials'}
        scan=root/'scans/-1.jsonl';cache=roots['partials']/'-1/42.verified.jsonl'
        protected=roots['output']/'user.verified.jsonl'
        for p in (scan,cache,protected):p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(b'neutral')
        before=snapshot(root,roots)
        self.assertTrue(all('\\' not in key for key in before))
        scan.write_bytes(b'normal update');cache.write_bytes(b'normal cache update')
        self.assertEqual(business_snapshot(before,root,roots),business_snapshot(snapshot(root,roots),root,roots))
        protected.write_bytes(b'changed user file')
        self.assertNotEqual(business_snapshot(before,root,roots),business_snapshot(snapshot(root,roots),root,roots))

    def test_independent_evidence_reader_detects_corruption_and_commit_gaps(self):
        root=Path(__file__).resolve().parents[1]/'~temp/tests'/uuid.uuid4().hex
        root.mkdir(parents=True);media=root/'neutral.bin';journal=root/'neutral.jsonl'
        media.write_bytes(b'abcd')
        rows=[{'kind':'identity','identity':{'mid':'42'},'size':4},
              {'kind':'chunk','offset':0,'length':2,'sha256':hashlib.sha256(b'ab').hexdigest()},
              {'kind':'chunk','offset':2,'length':2,'sha256':hashlib.sha256(b'cd').hexdigest()}]
        save_rows(journal,rows);self.assertTrue(check_journal(media,journal,'42'))
        media.write_bytes(b'abce');self.assertFalse(check_journal(media,journal,'42'))
        media.write_bytes(b'abcd');rows[-1]['offset']=3
        save_rows(journal,rows);self.assertFalse(check_journal(media,journal,'42'))

    def test_production_layout_covers_entire_downloading_and_retains_original_fixture(self):
        root=Path(__file__).resolve().parents[1]/'~temp/tests'/uuid.uuid4().hex
        baseline=root/'baseline';media=baseline/'output/neutral [-1]/segment/2023-11-15-m42_1_999.bin'
        media.parent.mkdir(parents=True);media.write_bytes(b'neutral')
        record=ScanRecord(baseline/'scans/-1.jsonl');record.record_result('42','downloaded',detail=str(media))
        work=root/'work';copy_case(baseline,work,{})
        roots=production_layout(work)
        changed=ScanRecord(work/'scans/-1.jsonl');target=Path(changed.results['42']['detail'])
        self.assertTrue(target.is_relative_to(work/'downloaded'));self.assertEqual(sha(target),sha(media))
        fact=storage_fault('extras',work,'-1','42',roots)
        loose=set(loose_storage_files(roots['output'],roots['staging'],roots['partials']))
        expected={Path(p) for p in fact['extras'] if Path(p).parent!=target.parent}
        self.assertEqual(loose,expected)
        self.assertIn(work/'downloading/neutral-extra-1.bin',loose)
        self.assertEqual(ScanRecord(record.path).results['42']['detail'],str(media))
        self.assertEqual(media.read_bytes(),b'neutral')

    def test_legacy_migration_fixture_flattens_only_copy_and_remaps_evidence(self):
        root = Path(__file__).resolve().parents[1] / '~temp/tests' / uuid.uuid4().hex
        baseline = root/'baseline'
        folder = baseline/'output/fixture/[From_2023-11-15-m42]/2023-11-15-g777后'
        folder.mkdir(parents=True)
        media = folder/'2023-11-15-m42_1_888.bin'
        media.write_bytes(b'neutral-media')
        text = folder/'2023-11-15-m42_neutral.txt'
        text.write_bytes(b'neutral')
        record = ScanRecord(baseline/'scans/-1.jsonl')
        record.record_result('42', 'downloaded', detail=str(media))
        record.record_content_file('42', text)
        record.append([{'kind':'standalone_context','mid':'42','context':{
            'album_id':'777','day':'2023-11-15','previous_mid':'41'}}])
        journal = baseline/'partials/-1/42.verified.jsonl'
        verified_digest(media, {'peer':'-1','mid':'42'}, journal, sha)
        snapshots = rows(journal)
        snapshots[-1]['path'] = str(baseline/'staging'/media.name)
        save_rows(journal, snapshots)
        work = root/'case'
        copied = legacy_copy(baseline, work)
        self.assertFalse(copied.standalone_contexts)
        target = Path(copied.results['42']['detail'])
        self.assertEqual(target.parent.name, '[From_2023-11-15-m42]')
        self.assertEqual(sha(target), sha(media))
        self.assertEqual(Path(copied.content_files['42']).parent, target.parent)
        snapshot = read_snapshot(work/'partials/-1/42.verified.jsonl')
        self.assertEqual(snapshot['path'], str(target))
        self.assertEqual(snapshot['ino'], target.stat().st_ino)
        self.assertEqual(snapshot['sha256'], sha(media))
        self.assertTrue(media.is_file())
        self.assertTrue(ScanRecord(record.path).standalone_contexts)

    def test_historical_window_only_changes_latest_request_offset(self):
        import asyncio
        import sys
        import telegg
        calls = []
        async def original(page, peer, offset):
            calls.append((peer, offset))
            return ['real-response']
        fake = SimpleNamespace(server_older_batch=original, save_media_server=object())
        namespace = {'PEER': -1, 'UPPER': (1 << 32) + 50}
        with patch.dict(sys.modules, {'telegg.browser': fake}), patch.object(telegg, 'browser', fake, create=True):
            exec(HOOK, namespace)
        async def run():
            self.assertEqual(await fake.server_older_batch(None, -1, 1 << 32), ['real-response'])
            await fake.server_older_batch(None, -1, (1 << 32) + 20)
            await fake.server_older_batch(None, -2, 1 << 32)
        asyncio.run(run())
        self.assertEqual(calls, [(-1, (1 << 32) + 51), (-1, (1 << 32) + 20), (-2, 1 << 32)])

    def test_standalone_sample_preserves_whole_segment_and_selects_single_target(self):
        root = Path(__file__).resolve().parents[1] / '~temp/tests' / uuid.uuid4().hex
        root.mkdir(parents=True)
        media = {}
        batch = []
        for mid, album in [('41', '777'), ('42', ''), ('43', '')]:
            path = root / mid
            with path.open('xb') as stream:
                stream.truncate(4 * 1024 * 1024)
            media[mid] = {'status': 'downloaded', 'detail': str(path)}
            batch.append(SimpleNamespace(mid=mid, album_id=album,
                content_types=((mid, 'video'),), parts=((mid, '', 1, True),)))
        contexts = {mid: {'album_id': '777', 'day': '2023-11-15', 'previous_mid': '41'}
                    for mid in ('42', '43')}
        r = SimpleNamespace(results=media, standalone_contexts=contexts, path=root/'record.jsonl')
        with patch('matrix_harness.runner.assert_live_test_allowed') as policy:
            info = sample_batch(r, SimpleNamespace(identity='-1'), 1, batch, 20, 'standalone')
        self.assertEqual(set(info['files']), {'41', '42', '43'})
        self.assertIn(info['target'], {'42', '43'})
        self.assertEqual((info['upper'], info['limit']), (43, 3))
        self.assertEqual(info['contexts'], contexts)
        self.assertEqual(policy.call_count, 3)

    def test_sample_skips_missing_completed_files_without_changing_record(self):
        missing = Path(__file__).resolve().parents[1] / '~temp/tests' / uuid.uuid4().hex / 'missing.bin'
        result = {'status': 'downloaded', 'detail': str(missing)}
        record = SimpleNamespace(results={'42': result.copy()})
        item = SimpleNamespace(mid='42', album_id='', content_types=(('42', 'video'),),
                               parts=(('42', '', 1, True),))
        with patch('matrix_harness.runner.assert_live_test_allowed'):
            info = sample_batch(record, SimpleNamespace(identity='-1'), 1, [item], 8, 'standalone')
        self.assertIsNone(info)
        self.assertEqual(record.results['42'], result)
        self.assertFalse(missing.parent.exists())

    def test_catalog_selection_is_explicit_and_unique(self):
        self.assertEqual(len(CASES), len({c.id for c in CASES}))
        self.assertEqual([c.id for c in select(['D04'])], ['D04'])
        self.assertTrue(all(c.category == 'progress' for c in select(categories=['progress'])))
        with self.assertRaises(ValueError):
            select(['G00'])
        with self.assertRaises(ValueError):
            select(['does-not-exist'])

    def test_partial_fixtures_copy_bytes_and_keep_source_unchanged(self):
        root = Path(__file__).resolve().parents[1] / '~temp/tests' / uuid.uuid4().hex
        baseline = root / 'baseline'
        source = root / 'source.bin'
        source.parent.mkdir(parents=True)
        data = bytes(range(256)) * ((CHUNK_SIZE * 4) // 256) + b'end'
        source.write_bytes(data)
        final = baseline / 'staging/fixture/album/2023-11-15-m42_1_777.bin'
        identity = {'peer': '-1', 'mid': '42', 'kind': 'document', 'id': '777', 'dc': 4, 'thumb': ''}
        p = PartialFile(baseline / 'partials/-1', '42', identity, len(data))
        p.open()
        for offset in range(0, len(data), CHUNK_SIZE):
            p.append(data[offset:offset + CHUNK_SIZE])
        p.finalize(final)
        record = baseline / 'scans/-1.jsonl'
        record.parent.mkdir(parents=True)
        record.write_text(json.dumps({'kind': 'result', 'mid': '42', 'status': 'downloaded',
                                      'detail': str(final)}) + '\n', encoding='utf-8')
        source_hash = sha(source)
        for cid, length, committed in (
            ('D04', CHUNK_SIZE, 1), ('D05', CHUNK_SIZE * 3 // 2, 1),
            ('D06', CHUNK_SIZE, 2), ('D13', len(data), 5),
        ):
            case = next(c for c in CASES if c.id == cid)
            work = root / cid
            copy_case(baseline, work, {final.name: source})
            build(case, work, '-1', '42', source)
            payload = work / 'partials/-1/42.part'
            self.assertEqual(payload.stat().st_size, length)
            self.assertEqual(payload.read_bytes(), data[:length])
            journal = rows(work / 'partials/-1/42.jsonl')
            self.assertEqual(len(journal) - 1, committed)
            self.assertEqual(journal[1]['sha256'], hashlib.sha256(data[:CHUNK_SIZE]).hexdigest())
            self.assertEqual(sha(source), source_hash)
        self.assertEqual(sha(final), source_hash)

    def test_copy_remaps_only_path_fields(self):
        root = Path(__file__).resolve().parents[1] / '~temp/tests' / uuid.uuid4().hex
        baseline = root / 'baseline'
        baseline.mkdir(parents=True)
        pointer = str(baseline / 'file.bin')
        (baseline / 'file.bin').write_bytes(b'fixture')
        (baseline / 'records.jsonl').write_text(json.dumps({
            'kind': 'content_file', 'mid': '42', 'path': pointer, 'text': pointer}) + '\n', encoding='utf-8')
        work = root / 'copy'
        copy_case(baseline, work, {})
        value = rows(work / 'records.jsonl')[0]
        self.assertEqual(value['path'], str(work / 'file.bin'))
        self.assertEqual(value['text'], pointer)


if __name__ == '__main__':
    unittest.main()
