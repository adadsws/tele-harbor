"""废弃参数应明确拒绝，且不得启动真实浏览器。"""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from telegg.__main__ import run


class CliTests(unittest.TestCase):
    def test_retired_options_fail_before_opening_browser(self):
        for option in ('rebuild_local', 'pause_at_oldest'):
            with self.subTest(option=option):
                args = SimpleNamespace(limit=1, audit_messages=None, full_scan=False,
                                       dry_run=False, rebuild_local=False, pause_at_oldest=False)
                setattr(args, option, True)
                with (patch('telegg.__main__.arguments', return_value=args),
                      patch('telegg.__main__.async_playwright') as browser):
                    with self.assertRaisesRegex(ValueError, '已停用'):
                        asyncio.run(run([]))
                    browser.assert_not_called()
