"""离线验证群组菜单的数字选择与配置增删。"""

import asyncio
import argparse
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from telegg.browser import download_group
from telegg.group_menu import ConfiguredGroup, choose_groups, load_groups, parse_groups
from telegg.__main__ import run
from telegg.local_config import load_paths


FIRST = 'https://web.telegram.org/k/#-1000000005'
SECOND = 'https://web.telegram.org/k/#-1000000003'
THIRD = 'https://web.telegram.org/k/#-1000000004'
NEW = 'https://web.telegram.org/k/#-1000000002'


class GroupMenuTests(unittest.TestCase):
    def menu(self, answers, groups=None):
        groups = list(groups if groups is not None else [
            ConfiguredGroup('-1000000005'), ConfiguredGroup('-1000000003'), ConfiguredGroup('-1000000004'),
        ])
        saved = []
        responses = iter(answers)
        output = io.StringIO()
        with redirect_stdout(output):
            selected = choose_groups(groups, lambda updated: saved.append(list(updated)),
                                     lambda _: next(responses))
        return selected, groups, saved, output.getvalue()

    def test_initial_config_and_single_choice(self):
        with tempfile.TemporaryDirectory() as directory:
            sample = Path(directory) / 'config.local.json'
            sample.write_text(json.dumps({'groups': [
                '-1000000005', '-1000000003', '-1000000004',
            ]}), encoding='utf-8')
            configured = load_groups(sample)
        self.assertEqual([item.url for item in configured], [FIRST, SECOND, THIRD])
        self.assertEqual([item.identity for item in configured],
                         ['-1000000005', '-1000000003', '-1000000004'])
        selected, _, saved, output = self.menu(['2'])
        self.assertEqual(selected, [SECOND])
        self.assertEqual(saved, [])
        self.assertIn('2. 下载 群组 -1000000003', output)

    def test_all_and_exit(self):
        self.assertEqual(self.menu(['4'])[0], [FIRST, SECOND, THIRD])
        self.assertIsNone(self.menu(['0'])[0])

    def test_one_numeric_option_toggles_only_two_modes(self):
        groups = [ConfiguredGroup('-1000000005'), ConfiguredGroup('-1000000003'),
                  ConfiguredGroup('-1000000004')]
        changed = []
        answers = iter(['7', '7', '7', '1'])
        output = io.StringIO()
        with redirect_stdout(output):
            selected = choose_groups(groups, lambda _items: None,
                                     lambda _prompt: next(answers),
                                     on_verify_change=changed.append)
        self.assertEqual(selected, [FIRST])
        self.assertEqual(changed, ['exact', 'fast', 'exact'])
        self.assertIn('7. 切换模式（当前：快速模式）', output.getvalue())
        self.assertIn('7. 切换模式（当前：逐文件检查模式）', output.getvalue())

    def test_live_names_appear_without_changing_group_ids(self):
        groups = [ConfiguredGroup('-1000000005'), ConfiguredGroup('-1000000003')]
        output = io.StringIO()
        with redirect_stdout(output):
            selected = choose_groups(groups, lambda _updated: None, lambda _prompt: '0',
                                     names={'-1000000005': '当前群名'})
        self.assertIsNone(selected)
        self.assertIn('1. 下载 当前群名', output.getvalue())
        self.assertNotIn('当前群名 [-1000000005]', output.getvalue())
        self.assertIn('2. 下载 群组 -1000000003', output.getvalue())
        self.assertEqual([item.identity for item in groups], ['-1000000005', '-1000000003'])

    def test_add_persists_then_can_choose(self):
        selected, groups, saved, output = self.menu(['5', 'bad-url', FIRST, NEW, '4'])
        self.assertEqual(selected, [NEW])
        self.assertEqual(groups[-1], ConfiguredGroup('-1000000002'))
        self.assertEqual(saved[-1], groups)
        self.assertIn('链接无效', output)
        self.assertIn('已在清单中', output)
        self.assertIn('打开群组后才读取实际群名', output)

    def test_remove_only_changes_config(self):
        selected, groups, saved, output = self.menu(['6', '9', '2', '2'])
        self.assertEqual(selected, [THIRD])
        self.assertEqual([item.url for item in groups], [FIRST, THIRD])
        self.assertEqual(saved[-1], groups)
        self.assertIn('已有下载文件保留', output)

    def test_invalid_choice_and_empty_config(self):
        self.assertEqual(self.menu(['x', '1'])[0], [FIRST])
        selected, groups, saved, _ = self.menu(['1', NEW, '1'], groups=[])
        self.assertEqual(selected, [NEW])
        self.assertEqual(groups[0].identity, '-1000000002')
        self.assertEqual(len(saved), 1)

    def test_rejects_duplicate_config(self):
        with self.assertRaises(ValueError):
            parse_groups(['-1000000005', '-1000000005'])

    def test_missing_interactive_input_does_not_select_a_group(self):
        def no_input(_):
            raise EOFError

        with redirect_stdout(io.StringIO()), self.assertRaisesRegex(ValueError, '没有可用的 CMD 输入'):
            choose_groups([ConfiguredGroup('-1000000005')], lambda _: None, no_input)

    def test_legacy_names_are_ignored_and_urls_reduce_to_ids(self):
        self.assertEqual(parse_groups([{'name': '旧名称', 'url': NEW}]),
                         [ConfiguredGroup('-1000000002')])

    def test_opened_group_passes_real_title_to_cache(self):
        titles = []
        async def empty_history(*_args, **_kwargs):
            if False:
                yield None

        with (patch('telegg.browser.open_group', new_callable=AsyncMock, return_value='实际群名'),
              patch('telegg.browser.iter_history', side_effect=empty_history),
              redirect_stdout(io.StringIO())):
            result = asyncio.run(download_group(None, NEW, load_paths()['test_artifacts'] / 'menu-output', None, True,
                                                on_group_open=titles.append))
        self.assertEqual(result, (0, 0, 0, 0))
        self.assertEqual(titles, ['实际群名'])

    def test_completed_menu_task_returns_to_main_menu(self):
        page = AsyncMock()
        context = AsyncMock()
        context.pages = [page]
        playwright = SimpleNamespace(chromium=SimpleNamespace(
            launch_persistent_context=AsyncMock(return_value=context)))
        manager = MagicMock()
        manager.__aenter__ = AsyncMock(return_value=playwright)
        manager.__aexit__ = AsyncMock(return_value=None)
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            args = argparse.Namespace(groups=[], output=Path(directory), limit=None, dry_run=False,
                                      audit_messages=None, full_scan=False, login_only=False)
            with (patch('telegg.__main__.arguments', return_value=args),
                  patch('telegg.__main__.load_paths', return_value={'chrome_profile': Path(directory)}),
                  patch('telegg.__main__.async_playwright', return_value=manager),
                  patch('telegg.__main__.wait_for_login', new_callable=AsyncMock),
                  patch('telegg.__main__.open_group', new_callable=AsyncMock, return_value='群名'),
                  patch('telegg.__main__.choose_groups', side_effect=[[FIRST], None]) as menu,
                  patch('telegg.__main__.download_group', new_callable=AsyncMock,
                        return_value=(1, 0, 1, 0)) as download,
                  redirect_stdout(output)):
                self.assertEqual(asyncio.run(run([])), 0)
        self.assertEqual(menu.call_count, 2)
        download.assert_awaited_once()
        self.assertIn('返回主菜单', output.getvalue())


if __name__ == '__main__':
    unittest.main()
