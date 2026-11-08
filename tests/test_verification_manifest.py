"""已验收清单的快速复用和精确回退样例。"""

import hashlib
import unittest
import uuid

from telegg.local_config import load_paths
from telegg.verification_manifest import verified_digest


class VerificationManifestTests(unittest.TestCase):
    def test_fast_reuses_snapshot_and_exact_hashes_again(self):
        root = load_paths()['test_artifacts'] / f'verify-{uuid.uuid4().hex}'
        root.mkdir(parents=True)
        path = root / 'm42_1_test.bin'
        path.write_bytes(b'original')
        journal = root / '42.verified.jsonl'
        calls = []

        def hash_file(candidate):
            calls.append(candidate)
            return hashlib.sha256(candidate.read_bytes()).hexdigest()

        identity = {'mid': '42', 'id': 'remote-1'}
        original = verified_digest(path, identity, journal, hash_file)
        self.assertEqual(verified_digest(path, identity, journal, hash_file), original)
        self.assertEqual(len(calls), 1)
        moved = root / 'm42_1_moved.bin'
        path.rename(moved)
        path = moved
        self.assertEqual(verified_digest(path, identity, journal, hash_file), original)
        self.assertEqual(len(calls), 1)
        verified_digest(path, identity, journal, hash_file, exact=True)
        self.assertEqual(len(calls), 2)
        path.write_bytes(b'changed!')
        with self.assertRaisesRegex(RuntimeError, 'SHA-256 与验收清单不符'):
            verified_digest(path, identity, journal, hash_file)
        self.assertEqual(len(calls), 3)
        with self.assertRaisesRegex(RuntimeError, 'SHA-256 与验收清单不符'):
            verified_digest(path, identity, journal, hash_file, exact=True)
        self.assertEqual(len(calls), 4)
        with self.assertRaisesRegex(RuntimeError, '媒体身份与验收清单不符'):
            verified_digest(path, {'mid': '42', 'id': 'remote-2'}, journal, hash_file)
        self.assertEqual(len(calls), 4)


if __name__ == '__main__':
    unittest.main()
