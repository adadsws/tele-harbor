"""矩阵构造器必须确实制造目标差异，且不改源文件；不连接 Telegram。"""
import hashlib
import json
import unittest
import uuid
from pathlib import Path

from matrix_harness.catalog import CASES, select
from matrix_harness.fixtures import build, rows
from matrix_harness.isolation import copy_case, sha
from telegg.resumable import CHUNK_SIZE, PartialFile


class MatrixHarnessTests(unittest.TestCase):
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
