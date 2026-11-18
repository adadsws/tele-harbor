"""停用目录不被访问，现有文件保护与可恢复归档继续生效。"""

import asyncio
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import AsyncMock, patch

from telegg import browser, local_config
from telegg.__main__ import arguments
from telegg.scan_record import ScanRecord
from matrix_harness.history_window import install


class NoLegacyStorageTests(unittest.TestCase):
    def test_all_config_paths_reject_retired_directory_before_resolution(self):
        example = Path(__file__).resolve().parents[1] / 'config/config.example.json'
        defaults = json.loads(example.read_text(encoding='utf-8'))
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / 'config.json'
            for key in ('output', 'staging', 'partials', 'chrome_profile', 'scan_records', 'test_artifacts'):
                for name in ('~local', '~LOCAL'):
                    with self.subTest(key=key, directory=name):
                        changed = {**defaults, key: str(Path(temp) / name / 'data')}
                        config.write_text(json.dumps(changed), encoding='utf-8')
                        with (patch.object(local_config, 'CONFIG_FILE', config),
                              patch.object(Path, 'resolve', side_effect=AssertionError('must not inspect retired path')),
                              self.assertRaisesRegex(ValueError, '已停用')):
                            # Validate this key directly so other valid keys need no resolution.
                            local_config.checked_path(changed[key], key)
                        with (patch.object(local_config, 'CONFIG_FILE', config),
                              self.assertRaisesRegex(ValueError, '已停用')):
                            local_config.load_paths()

    def test_output_override_and_resolved_links_cannot_reenter_retired_directory(self):
        with self.assertRaisesRegex(ValueError, '已停用'):
            local_config.checked_path(Path('~local') / 'output', '--output')
        with (patch.object(Path, 'resolve', return_value=Path.cwd() / '~LOCAL' / 'output'),
              self.assertRaisesRegex(ValueError, '解析到已停用')):
            local_config.checked_path('output-link', '--output')

    def test_download_indexes_only_active_roots_and_does_not_probe_retired_pool(self):
        async def empty_history(*_args, **_kwargs):
            if False:
                yield None

        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp).resolve()
            paths = {key: base / key for key in ('output', 'staging', 'partials', 'scan_records')}
            original_is_dir = Path.is_dir

            def guarded_is_dir(path):
                if any(part.casefold() == '~local' for part in path.parts):
                    raise AssertionError('retired pool was probed')
                return original_is_dir(path)

            for exact in (False, True):
                with self.subTest(exact=exact):
                    with (patch.object(browser, 'load_paths', return_value=paths),
                          patch.object(browser, 'SCAN_RECORD_ROOT', paths['scan_records']),
                          patch.object(browser, 'open_group', new=AsyncMock(return_value='fixture')),
                          patch.object(browser, 'iter_history', side_effect=empty_history),
                          patch.object(browser, 'existing_media_index', return_value={}) as index,
                          patch.object(Path, 'is_dir', new=guarded_is_dir),
                          redirect_stdout(io.StringIO())):
                        result = asyncio.run(browser.download_group(None, 'https://web.telegram.org/k/#-1',
                                                                   paths['output'], 1, False, exact_verify=exact))
                    self.assertEqual(result, (0, 0, 0, 0))
                    observed = [call.args[0] for call in index.call_args_list]
                    self.assertTrue(observed)
                    self.assertTrue(all(path.is_relative_to(paths['output']) or path.is_relative_to(paths['staging'])
                                        for path in observed))
            self.assertFalse((base / '~local').exists())

    def test_output_argument_explains_the_retired_directory(self):
        error = io.StringIO()
        with redirect_stdout(io.StringIO()), patch('sys.stderr', error), self.assertRaises(SystemExit) as stopped:
            arguments(['--output', str(Path.cwd() / '~LOCAL' / 'output')])
        self.assertEqual(stopped.exception.code, 2)
        self.assertIn('已停用', error.getvalue())
        self.assertNotIn('<lambda>', error.getvalue())

    def test_fast_mode_does_not_import_external_paths_from_completion_records(self):
        async def empty_history(*_args, **_kwargs):
            if False:
                yield None

        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp).resolve()
            paths = {key: base / key for key in ('output', 'staging', 'partials', 'scan_records')}
            scan = ScanRecord(paths['scan_records'] / '-1.jsonl')
            scan.record_result('42', 'downloaded', detail=str(base / '~local' / '-1' / 'm42_1_777.bin'))
            before = scan.path.read_bytes()
            with (patch.object(browser, 'load_paths', return_value=paths),
                  patch.object(browser, 'SCAN_RECORD_ROOT', paths['scan_records']),
                  patch.object(browser, 'open_group', new=AsyncMock(return_value='fixture')),
                  patch.object(browser, 'iter_history', side_effect=empty_history),
                  patch.object(browser, 'existing_media_index', return_value={}),
                  patch.object(browser, 'demote_unready_replies') as demote,
                  redirect_stdout(io.StringIO())):
                self.assertEqual(asyncio.run(browser.download_group(None, 'https://web.telegram.org/k/#-1',
                                                                   paths['output'], 1, False, coarse_skip=True)),
                                 (0, 0, 0, 0))
            self.assertNotIn('42', demote.call_args.args[3])
            self.assertEqual(scan.path.read_bytes(), before)
            self.assertFalse((base / '~local').exists())

    def test_empty_sources_archive_beside_output_and_preserve_nonempty_files(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            output, staging = base / 'output', base / 'staging'
            root = output / 'fixture'
            empty, occupied = staging / 'fixture' / 'empty', staging / 'fixture' / 'occupied'
            empty.mkdir(parents=True)
            occupied.mkdir()
            kept = occupied / 'user.bin'
            kept.write_bytes(b'keep')
            with patch.object(browser, 'load_paths', return_value={'output': output, 'staging': staging}):
                browser.archive_empty_relocated_folders(root, {empty, occupied}, staging / 'fixture')
            self.assertFalse(empty.exists())
            self.assertEqual(kept.read_bytes(), b'keep')
            archives = list((base / '~archive').glob('*/sources.jsonl'))
            self.assertEqual(len(archives), 1)
            row = json.loads(archives[0].read_text(encoding='utf-8').splitlines()[0])
            self.assertTrue(Path(row['archive']).is_dir())
            self.assertFalse((base / '~local').exists())

    def test_history_window_uses_native_media_route_without_creating_storage(self):
        with tempfile.TemporaryDirectory() as temp:
            runtime = Path(temp)
            (runtime / 'src').mkdir()
            install(runtime, {'peer': '-1', 'upper': 42})
            source = (runtime / 'src/sitecustomize.py').read_text(encoding='utf-8')
            with (patch.object(browser, 'server_older_batch', new=AsyncMock(return_value=[])) as older,
                  patch.object(browser, 'discover_media'), patch.object(browser, 'save_media'),
                  patch.object(browser, 'discover_media_server', new=AsyncMock(return_value=[])) as discover):
                exec(compile(source, '<bounded-native-route>', 'exec'), {})
                asyncio.run(browser.server_older_batch(None, -1, 1 << 32))
                older.assert_awaited_once_with(None, -1, 43)
                asyncio.run(browser.discover_media(None, 'https://web.telegram.org/k/#-1', None))
                discover.assert_awaited_once_with(None, -1, None)
                self.assertIs(browser.save_media, browser.save_media_server)
            self.assertEqual([p.name for p in runtime.iterdir()], ['src'])
