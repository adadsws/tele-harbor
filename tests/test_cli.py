"""废弃参数应明确拒绝，且不得启动真实浏览器。"""

import asyncio
import io
import unittest
from contextlib import redirect_stdout, redirect_stderr
from types import SimpleNamespace
from unittest.mock import patch

from telegg.__main__ import run, main, report_group_error
from telegg.browser import DownloadTransportUnavailable
from playwright.async_api import TimeoutError as BrowserTimeout


class CliTests(unittest.TestCase):
    def test_lifecycle_records_success_failure_help_bad_arguments_and_interrupt(self):
        cases=[(0, None, 0, 'completed'), (1, None, 1, 'incomplete'),
               (None, SystemExit(0), 0, 'cli'), (None, SystemExit(2), 2, 'cli'),
               (None, KeyboardInterrupt(), 130, 'interrupted'),
               (None, RuntimeError('synthetic startup failure'), 1, 'error')]
        for result,error,expected,reason in cases:
            with self.subTest(reason=reason, code=expected):
                with (patch('telegg.__main__.start_log'),patch('telegg.__main__.detail') as log,
                      patch('telegg.__main__.failure'),patch('telegg.__main__.os.getpid',return_value=42),
                      patch('telegg.__main__.run',new=lambda:None),
                      patch('telegg.__main__.asyncio.run',return_value=result,side_effect=error),
                      redirect_stdout(io.StringIO()),redirect_stderr(io.StringIO()),
                      self.assertRaises(SystemExit) as stopped):
                    main()
                self.assertEqual(stopped.exception.code,expected)
                self.assertEqual(log.call_args_list[0].args[0],'RUN_START pid=42')
                self.assertEqual(log.call_args_list[-1].args[0],f'RUN_EXIT pid=42 code={expected} reason={reason}')

    def test_transport_stop_is_explained_in_console(self):
        output=io.StringIO()
        with patch('telegg.__main__.failure'),redirect_stderr(output):
            report_group_error(DownloadTransportUnavailable('连续超时；断点已保留'),1,1,
                               'https://web.telegram.org/k/#-1')
        self.assertIn('[下载连接中断]',output.getvalue())
        self.assertIn('断点已保留',output.getvalue())

    def test_page_timeout_is_distinct_from_download_connection_failure(self):
        output = io.StringIO()
        with patch('telegg.__main__.failure'), redirect_stderr(output):
            report_group_error(BrowserTimeout('synthetic navigation timeout'), 1, 1,
                               'https://web.telegram.org/k/#-1')
        self.assertIn('[页面超时]', output.getvalue())
        self.assertIn('已有文件和断点保留', output.getvalue())
        self.assertNotIn('[下载连接中断]', output.getvalue())

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
