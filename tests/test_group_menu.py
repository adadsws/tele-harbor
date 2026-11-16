"""离线验证群组菜单的数字选择与配置增删。"""

import asyncio
import argparse
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout,redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from telegg.browser import download_group
from telegg.group_menu import ConfiguredGroup, choose_groups, load_groups, parse_groups
from telegg.__main__ import arguments, run
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

    def test_one_numeric_option_cycles_download_and_read_only_modes(self):
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
        self.assertEqual(changed, ['exact', 'extras', 'fast'])
        self.assertIn('7. 切换模式（当前：快速模式（不检查缺失/额外））', output.getvalue())
        self.assertIn('7. 切换模式（当前：逐文件下载（检查缺失/额外））', output.getvalue())
        self.assertIn('7. 切换模式（当前：检查缺失和额外文件（不下载新内容））', output.getvalue())
        self.assertIn('1. 检查 群组 -1000000005', output.getvalue())

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

    def test_normal_entry_is_visible_and_debug_can_be_hidden(self):
        with (patch('telegg.__main__.load_paths', return_value={'output': Path('fixture-output')}),
              patch('telegg.__main__.load_groups', return_value=[])):
            self.assertEqual(arguments([]).debug_window, 'visible')
            self.assertEqual(arguments(['--debug-window', 'hidden']).debug_window, 'hidden')
            self.assertEqual(arguments(['--debug-window', 'hidden', '--verify', 'exact']).verify, 'exact')

    def test_debug_window_selection_reaches_browser_and_closes_it(self):
        for mode in ('hidden', 'visible'):
            with self.subTest(mode=mode):
                page, context = AsyncMock(), AsyncMock()
                context.pages = [page]
                launch = AsyncMock(return_value=context)
                manager = MagicMock()
                manager.__aenter__ = AsyncMock(return_value=SimpleNamespace(
                    chromium=SimpleNamespace(launch_persistent_context=launch)))
                manager.__aexit__ = AsyncMock(return_value=None)
                args = argparse.Namespace(groups=[FIRST], output=Path('fixture-output'),
                    limit=1, dry_run=False, audit_messages=None, full_scan=False,
                    login_only=False, verify='fast', debug_window=mode)
                with (patch('telegg.__main__.arguments', return_value=args),
                      patch('telegg.__main__.load_paths', return_value={
                          'chrome_profile': Path(__file__).parents[1] / '~temp/tests/window-profile'}),
                      patch('telegg.__main__.async_playwright', return_value=manager),
                      patch('telegg.__main__.wait_for_login', new_callable=AsyncMock) as login,
                      patch('telegg.__main__.download_group', new_callable=AsyncMock,
                            return_value=(1, 0, 1, 0)), redirect_stdout(io.StringIO())):
                    self.assertEqual(asyncio.run(run([])), 0)
                self.assertEqual(launch.call_args.kwargs['headless'], mode == 'hidden')
                self.assertEqual(login.call_args.kwargs['timeout'], 15_000 if mode == 'hidden' else 300_000)
                context.close.assert_awaited_once()

    def test_hidden_login_only_fails_before_starting_browser(self):
        args = argparse.Namespace(groups=[], limit=None, audit_messages=None,
                                  full_scan=False, debug_window='hidden', login_only=True)
        with (patch('telegg.__main__.arguments', return_value=args),
              patch('telegg.__main__.async_playwright') as browser,
              self.assertRaisesRegex(ValueError, 'visible')):
            asyncio.run(run([]))
        browser.assert_not_called()

    def test_three_modes_differ_only_in_new_download_and_fast_skips_review(self):
        for mode in ('fast','exact','extras'):
            with self.subTest(mode=mode):
                page,context=AsyncMock(),AsyncMock();context.pages=[page]
                manager=MagicMock();manager.__aenter__=AsyncMock(return_value=SimpleNamespace(chromium=SimpleNamespace(
                    launch_persistent_context=AsyncMock(return_value=context))))
                manager.__aexit__=AsyncMock(return_value=None)
                base=Path(__file__).parents[1]/'~temp/tests'/('mode-'+mode)
                args=argparse.Namespace(groups=[FIRST],output=base/'output',limit=None,dry_run=False,
                    audit_messages=None,full_scan=False,login_only=False,check_extra_files=mode=='extras',
                    debug_window='hidden',verify=mode if mode!='extras' else 'fast')
                events=[]
                async def download(*args,**kwargs):events.append('download');return (1,0,1,0)
                async def finished(*args,**kwargs):events.append('confirm');return (0,False)
                with (patch('telegg.__main__.arguments',return_value=args),
                      patch('telegg.__main__.load_paths',return_value={'output':base/'output','staging':base/'staging',
                          'partials':base/'partials','chrome_profile':base/'profile'}),
                      patch('telegg.__main__.load_groups',return_value=[ConfiguredGroup('-1000000005')]),
                      patch('telegg.__main__.async_playwright',return_value=manager),
                      patch('telegg.__main__.wait_for_login',new_callable=AsyncMock),
                      patch('telegg.__main__.inspect_group',new_callable=AsyncMock,return_value=SimpleNamespace()) as inspect,
                      patch('telegg.__main__.finish_review',side_effect=finished) as finish,
                      patch('telegg.__main__.download_group',side_effect=download) as get,
                      redirect_stdout(io.StringIO())):
                    self.assertEqual(asyncio.run(run([])),0)
                if mode=='fast':
                    inspect.assert_not_awaited();finish.assert_not_called();self.assertEqual(events,['download'])
                else:
                    finish.assert_called_once()
                    self.assertEqual(events,['download','confirm'] if mode=='exact' else ['confirm'])
                    self.assertEqual(get.call_count,int(mode=='exact'))

    def test_failed_initial_review_is_not_repeated_at_workflow_end(self):
        from telegg.errors import LocalDataError
        for mode in ('exact','extras'):
            page,context=AsyncMock(),AsyncMock();context.pages=[page]
            manager=MagicMock();manager.__aenter__=AsyncMock(return_value=SimpleNamespace(chromium=SimpleNamespace(
                launch_persistent_context=AsyncMock(return_value=context))))
            manager.__aexit__=AsyncMock(return_value=None)
            base=Path(__file__).parents[1]/'~temp/tests/review-failed'
            args=argparse.Namespace(groups=[FIRST],output=base/'output',limit=None,dry_run=False,
                audit_messages=None,full_scan=False,login_only=False,check_extra_files=mode=='extras',
                debug_window='hidden',verify='exact')
            with (self.subTest(mode=mode),patch('telegg.__main__.arguments',return_value=args),
                  patch('telegg.__main__.load_paths',return_value={'output':base/'output','staging':base/'staging',
                      'partials':base/'partials','chrome_profile':base/'profile'}),
                  patch('telegg.__main__.load_groups',return_value=[ConfiguredGroup('-1000000005')]),
                  patch('telegg.__main__.async_playwright',return_value=manager),
                  patch('telegg.__main__.wait_for_login',new_callable=AsyncMock),
                  patch('telegg.__main__.inspect_group',new_callable=AsyncMock,side_effect=LocalDataError('neutral error')) as inspect,
                  patch('telegg.__main__.finish_review',new_callable=AsyncMock) as finish,
                  patch('telegg.__main__.download_group',new_callable=AsyncMock) as download,
                  redirect_stdout(io.StringIO()),redirect_stderr(io.StringIO())):
                self.assertEqual(asyncio.run(run([])),1)
            inspect.assert_awaited_once();finish.assert_not_awaited();download.assert_not_awaited()

    def test_extra_file_entry_reports_paths_without_downloading(self):
        page, context = AsyncMock(), AsyncMock()
        context.pages = [page]
        manager = MagicMock()
        manager.__aenter__ = AsyncMock(return_value=SimpleNamespace(chromium=SimpleNamespace(
            launch_persistent_context=AsyncMock(return_value=context))))
        manager.__aexit__ = AsyncMock(return_value=None)
        base = Path(__file__).parents[1] / '~temp/tests/extra-cli'
        args = argparse.Namespace(groups=[FIRST], output=base / 'output', limit=None,
                                  dry_run=False, audit_messages=None, full_scan=False,
                                  login_only=False, check_extra_files=True, debug_window='hidden')
        output = io.StringIO()
        with (patch('telegg.__main__.arguments', return_value=args),
              patch('telegg.__main__.load_paths', return_value={
                  'output': base / 'output', 'staging': base / 'staging', 'partials': base / 'partials',
                  'chrome_profile': base / 'profile'}),
              patch('telegg.__main__.load_groups', return_value=[ConfiguredGroup('-1000000005')]),
              patch('telegg.__main__.async_playwright', return_value=manager),
              patch('telegg.__main__.wait_for_login', new_callable=AsyncMock),
              patch('telegg.__main__.inspect_group', new_callable=AsyncMock,
                    return_value=SimpleNamespace(extras=[base/'output/extra.bin'])) as check,
              patch('telegg.__main__.finish_review',new_callable=AsyncMock,return_value=(0,False)) as finish,
              patch('telegg.__main__.download_group', new_callable=AsyncMock) as download,
              patch('telegg.__main__.audit_messages', new_callable=AsyncMock) as audit,
              redirect_stdout(output)):
            self.assertEqual(asyncio.run(run([])), 0)
        check.assert_awaited_once()
        download.assert_not_awaited()
        audit.assert_not_awaited()
        finish.assert_awaited_once()
        self.assertEqual(finish.call_args.args[1][0].extras,[base/'output/extra.bin'])
        self.assertNotIn('新下载', output.getvalue())

    def test_extra_file_entry_rejects_range_or_download_options_before_browser(self):
        for option, value in [('limit', 1), ('dry_run', True), ('full_scan', True),
                              ('audit_messages', 1), ('login_only', True)]:
            args = argparse.Namespace(groups=[], limit=None, audit_messages=None,
                dry_run=False, full_scan=False, login_only=False, check_extra_files=True)
            setattr(args, option, value)
            with (self.subTest(option=option), patch('telegg.__main__.arguments', return_value=args),
                  patch('telegg.__main__.async_playwright') as browser,
                  self.assertRaisesRegex(ValueError, '不可与')):
                asyncio.run(run([]))
            browser.assert_not_called()

    def test_layout_migration_preview_never_calls_downloader(self):
        page, context = AsyncMock(), AsyncMock(); context.pages = [page]
        manager = MagicMock()
        manager.__aenter__ = AsyncMock(return_value=SimpleNamespace(chromium=SimpleNamespace(
            launch_persistent_context=AsyncMock(return_value=context))))
        manager.__aexit__ = AsyncMock(return_value=None)
        base = Path(__file__).parents[1]/'~temp/tests/migration-cli'
        args = argparse.Namespace(groups=[FIRST], output=base/'output', limit=None,
            audit_messages=None, dry_run=True, full_scan=False, login_only=False,
            migrate_standalone_layout=True, debug_window='hidden')
        paths = {'output':base/'output', 'staging':base/'staging', 'partials':base/'partials',
                 'scan_records':base/'scans', 'chrome_profile':base/'profile'}
        output = io.StringIO()
        with (patch('telegg.__main__.arguments', return_value=args),
              patch('telegg.__main__.load_paths', return_value=paths),
              patch('telegg.__main__.async_playwright', return_value=manager),
              patch('telegg.__main__.wait_for_login', new_callable=AsyncMock),
              patch('telegg.__main__.migrate_group_layout', new_callable=AsyncMock,
                    return_value=(3, base/'plan.json')) as migrate,
              patch('telegg.__main__.download_group', new_callable=AsyncMock) as download,
              redirect_stdout(output)):
            self.assertEqual(asyncio.run(run([])), 0)
        migrate.assert_awaited_once_with(page, FIRST, paths, apply=False)
        download.assert_not_awaited()
        self.assertIn('迁移预览', output.getvalue())


if __name__ == '__main__':
    unittest.main()
