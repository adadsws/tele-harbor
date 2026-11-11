"""只验证诊断范围保护；不为正式下载程序增加按消息屏蔽规则。"""
import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from telegg.local_config import load_paths
from live_test_policy import POLICY_NAME, assert_live_test_allowed


class LiveTestPolicyTests(unittest.TestCase):
    def setUp(self):
        self.root = load_paths()['test_artifacts'] / 'test-policy' / uuid4().hex
        self.root.mkdir(parents=True)
        self.policy = self.root / POLICY_NAME
        self.entry = {'peer': '-1000000005', 'mid': '4294967313',
                      'classification': 'damaged', 'reason': 'user stopped this diagnostic'}
        self.policy.write_text(json.dumps({'version': 1, 'resources': [self.entry]}), encoding='utf-8')

    def test_raw_and_internal_ids_are_excluded_but_other_resources_are_allowed(self):
        for mid in ('17', self.entry['mid']):
            with self.subTest(mid=mid), self.assertRaisesRegex(ValueError, '排除出专项实测'):
                assert_live_test_allowed(self.entry['peer'], mid, policy_path=self.policy)
        assert_live_test_allowed(self.entry['peer'], '18', policy_path=self.policy)
        assert_live_test_allowed('-1000000006', '17', policy_path=self.policy)
        assert_live_test_allowed(self.entry['peer'], '17', policy_path=self.root / 'absent.json')

    def test_invalid_policy_stops_diagnostics(self):
        self.policy.write_text('{broken', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '清单损坏'):
            assert_live_test_allowed(self.entry['peer'], '17', policy_path=self.policy)

    def test_both_diagnostic_entries_stop_before_browser_or_partial_copy(self):
        import live_remote_file_audit as remote
        import live_resume_audit as resume
        config = self.root / 'config.json'
        config.write_text(json.dumps({'groups': [self.entry['peer']]}), encoding='utf-8')
        paths = {'scan_records': self.root / 'scans'}
        args = SimpleNamespace(source_group=1, mid='17')
        for module in (remote, resume):
            with (self.subTest(module=module.__name__),
                  patch.object(module, 'CONFIG_FILE', config),
                  patch.object(module, 'load_paths', return_value=paths, create=True),
                  patch('live_test_policy.load_paths', return_value=paths),
                  patch.object(module, 'async_playwright') as browser,
                  self.assertRaisesRegex(ValueError, '排除出专项实测')):
                asyncio.run(module.audit(args))
            browser.assert_not_called()
