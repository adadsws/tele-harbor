"""在无可见窗口 Chrome 的本地页面验证消息识别与浏览器下载事件。"""

import asyncio
import json
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from playwright.async_api import Error as BrowserError, TimeoutError as BrowserTimeout, async_playwright

from telegg.browser import (BUBBLE, MediaTask, discover_media, existing_media_index,
                            is_transport_timeout,
                            locate_message, media_targets, open_group, open_group_from_chatlist,
                            open_message_with_native_ui,
                            publish_ready_staged_groups, save_media, save_one,
                            save_media_with_recovery, visible_messages, wait_for_login)
from telegg.content_types import classify, validate_types
from telegg.content_archive import (content_payload, prepare_group_folder,
                                    prepare_item_folders, save_message_content,
                                    save_text_file)
from telegg.layout import Message, grouped, destination, group_folder, previous_group_folder, group_root
from telegg.local_config import load_paths
from telegg.errors import LocalDataError
from telegg.browser import iter_history, ScanProgress
from telegg.browser import navigation_retryable, reset_group_navigation

TEST_ROOT = load_paths()['test_artifacts']


class PublicationFailureTests(unittest.TestCase):
    def test_navigation_reset_tolerates_one_late_chrome_error_page(self):
        error = BrowserError('Navigation to "about:blank" is interrupted by another navigation to "chrome-error://chromewebdata/"')
        page = MagicMock()
        page.goto = AsyncMock(side_effect=[error, None])
        page.wait_for_timeout = AsyncMock()
        asyncio.run(reset_group_navigation(page))
        self.assertEqual(page.goto.await_count, 2)
        self.assertTrue(navigation_retryable(error))
        page.goto = AsyncMock(side_effect=error)
        with self.assertRaises(BrowserError):
            asyncio.run(reset_group_navigation(page))
        self.assertEqual(page.goto.await_count, 2)

    def test_navigation_retry_is_bounded_and_does_not_retry_identity_failures(self):
        page = MagicMock()
        page.is_closed.return_value = False
        page.goto = AsyncMock()
        with (patch('telegg.browser._open_group_once', new_callable=AsyncMock,
                    side_effect=[BrowserTimeout('synthetic timeout'), 'fixture']) as attempt,
              patch('telegg.browser.detail')):
            self.assertEqual(asyncio.run(open_group(page, 'https://web.telegram.org/k/#-1')), 'fixture')
            self.assertEqual(attempt.await_count, 2)
        with (patch('telegg.browser._open_group_once', new_callable=AsyncMock,
                    side_effect=BrowserTimeout('persistent timeout')) as attempt,
              self.assertRaises(BrowserTimeout)):
            asyncio.run(open_group(page, 'https://web.telegram.org/k/#-1'))
        self.assertEqual(attempt.await_count, 2)
        with (patch('telegg.browser._open_group_once', new_callable=AsyncMock,
                    side_effect=RuntimeError('页面跳转到其他群组')) as attempt,
              self.assertRaisesRegex(RuntimeError, '其他群组')):
            asyncio.run(open_group(page, 'https://web.telegram.org/k/#-1'))
        self.assertEqual(attempt.await_count, 1)
        self.assertTrue(navigation_retryable(BrowserTimeout('fixture')))
        self.assertFalse(navigation_retryable(LocalDataError('fixture')))

    def test_timed_out_part_collects_late_driver_failure_without_committing_it(self):
        from telegg.browser import fetch_resume_part

        async def scenario():
            release, finished = asyncio.Event(), asyncio.Event()
            errors = []
            loop = asyncio.get_running_loop()
            loop.set_exception_handler(lambda _loop, context: errors.append(context))

            async def late_driver(*_args):
                await release.wait()
                finished.set()
                raise RuntimeError('synthetic late driver failure')

            with patch('telegg.browser._fetch_resume_part', side_effect=late_driver):
                outer = asyncio.create_task(fetch_resume_part(None, {}, 0, 1024))
                await asyncio.sleep(0)
                outer.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await outer
                release.set()
                await asyncio.wait_for(finished.wait(), timeout=1)
                await asyncio.sleep(0)
                await asyncio.sleep(0)
            self.assertEqual(errors, [])

        asyncio.run(scenario())

    def test_later_history_failure_does_not_leave_published_source_directories(self):
        """模拟后续扫描失败：已发布头像段须在整轮结束前收尾。"""
        from telegg.browser import download_group
        base = TEST_ROOT / f'publish-before-failure-{uuid.uuid4().hex}'
        output, staging = base / 'output', base / 'staging'
        rows = [Message(str(mid), 'fixture', 1700000000 + mid * 300, 0,
                        ((str(mid), 'fixture', 1700000000 + mid * 300, False),),
                        ((str(mid), 'text'),)) for mid in (303, 302, 301)]

        async def history(*_args, **_kwargs):
            for row in rows:
                yield row
            raise RuntimeError('synthetic later history failure')

        paths = {'output': output, 'staging': staging, 'partials': base / 'partials',
                 'scan_records': base / 'records'}
        with (patch('telegg.browser.load_paths', return_value=paths),
              patch('telegg.browser.SCAN_RECORD_ROOT', paths['scan_records']),
              patch('telegg.browser.open_group', new_callable=AsyncMock, return_value='fixture'),
              patch('telegg.browser.iter_history', side_effect=history),
              self.assertRaisesRegex(RuntimeError, 'synthetic later history failure')):
            asyncio.run(download_group(None, 'https://web.telegram.org/k/#-1000000005',
                                       output, None, False, coarse_skip=False))
        root = group_root(output, 'fixture', '-1000000005')
        self.assertTrue((root / group_folder(rows[0]) / '段内清单.json').is_file())
        self.assertFalse((staging / root.name / group_folder(rows[0])).exists())


class SessionEntryTests(unittest.TestCase):
    def test_url_fallback_reaches_album_member_with_message_parameter(self):
        async def scenario():
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(channel='chrome', headless=True)
                try:
                    page = await browser.new_page()
                    # 中性路由夹具仅响应当前 Web K 的 message 参数；无登录及真实正文/媒体。
                    fixture = '''<div id="column-center"></div><script>
                    const raw = Number(new URLSearchParams(location.hash.split('?')[1] || '').get('message'));
                    if(raw) {
                        const mid = 4294967296 + raw % 4294967296;
                        const bubble = document.createElement('div');bubble.className = 'bubble';
                        bubble.dataset.peerId = '-1';bubble.dataset.mid = Number(mid) - 1;
                        const member = document.createElement('div');member.className = 'album-item';
                        member.dataset.mid = mid;bubble.append(member);
                        document.querySelector('#column-center').append(bubble);
                    }
                    // 模拟真实 Web K 中同文档路由被旧聊天入口覆盖，完整加载仍可定位。
                    addEventListener('hashchange', () => {
                        history.replaceState(null, '', '#-1');
                        document.querySelector('#column-center').replaceChildren();
                    });
                    </script>'''
                    await page.route('https://web.telegram.org/**', lambda route: route.fulfill(
                        status=200, content_type='text/html', body=fixture))
                    target = Message('4294967400', '', 1700000000, 0)
                    source = 'https://web.telegram.org/k/#-1'
                    await page.goto(source)
                    await page.goto(source + '?message=104')
                    self.assertEqual(await page.locator('.bubble').count(), 0)
                    with patch('telegg.browser.visible_messages', new_callable=AsyncMock, return_value=[]):
                        bubble = await locate_message(page, source, target)
                    self.assertEqual(await bubble.count(), 1)
                    self.assertEqual(await bubble.get_attribute('data-peer-id'), '-1')
                    self.assertEqual(page.url, 'https://web.telegram.org/k/#-1?message=104')
                finally:
                    await browser.close()
        asyncio.run(scenario())

    def test_native_navigation_requires_matching_peer_and_message(self):
        page = MagicMock()
        page.evaluate = AsyncMock(return_value=True)
        page.locator.return_value.first.wait_for = AsyncMock(
            side_effect=BrowserTimeout('target not present'))
        self.assertFalse(asyncio.run(open_message_with_native_ui(
            page, 'https://web.telegram.org/k/#-1', '4294967400')))
        selector = page.locator.call_args.args[0]
        self.assertIn('[data-peer-id="-1"]', selector)
        self.assertIn('[data-mid="4294967400"]', selector)
        page.evaluate = AsyncMock(return_value=False)
        page.locator.reset_mock()
        self.assertFalse(asyncio.run(open_message_with_native_ui(
            page, 'https://web.telegram.org/k/#-1', '4294967400')))
        page.locator.assert_not_called()

    def test_native_navigation_in_visible_chrome_reaches_requested_message(self):
        async def scenario():
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(channel='chrome', headless=True)
                try:
                    page = await browser.new_page()
                    await page.set_content('''<div id="column-center">
                    <div class="bubble" data-mid="4294967400" data-peer-id="-2"></div>
                    </div><script>
                    window.appImManager = {async setInnerPeer(options) {
                        window.navigation = options;
                        const e = document.createElement('div');e.className = 'bubble';
                        e.dataset.mid = options.lastMsgId - 1;e.dataset.peerId = options.peerId;
                        const member = document.createElement('div');member.className = 'album-item';
                        member.dataset.mid = options.lastMsgId;e.append(member);
                        document.querySelector('#column-center').append(e);
                    }};
                    </script>''')
                    target = Message('4294967400', '', 1700000000, 0)
                    located = await locate_message(page, 'https://web.telegram.org/k/#-1', target)
                    self.assertEqual(await located.count(), 1)
                    self.assertEqual(await located.get_attribute('data-peer-id'), '-1')
                    self.assertEqual(await located.get_attribute('data-mid'), '4294967399')
                    self.assertEqual(await page.evaluate('window.navigation'),
                                     {'peerId': -1, 'lastMsgId': 4294967400})
                finally:
                    await browser.close()
        asyncio.run(scenario())

    def test_part_timeout_collects_real_browser_rejection(self):
        from telegg.browser import fetch_resume_part, _PENDING_PART_READS

        async def scenario():
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(channel='chrome', headless=True)
                try:
                    page = await browser.new_page()
                    await page.set_content('''<script>
                    window.__teleggServerMedia = new Map([['42', {media: {document: {
                        _: 'document', id: '777', dc_id: 4, file_reference: []
                    }}}]]);
                    window.appDownloadManager = {managers: {
                        apiManager: {invokeApi() {return new Promise((_, reject) =>
                            setTimeout(() => reject(new Error('fixture late failure')), 50));}},
                        apiFileManager: {async requestFilePart() {throw new Error('fixture fallback failure');}}
                    }};
                    </script>''')
                    errors = []
                    asyncio.get_running_loop().set_exception_handler(
                        lambda _loop, context: errors.append(context))
                    with self.assertRaises(TimeoutError):
                        await asyncio.wait_for(fetch_resume_part(page, {
                            'kind': 'document', 'mid': '42', 'id': '777', 'dc': 4,
                            'peer': '-1', 'thumb': ''}, 0, 1024), timeout=.01)
                    await asyncio.sleep(.2)
                    self.assertFalse(_PENDING_PART_READS)
                    self.assertEqual(errors, [])
                    from telegg.errors import RemoteFileError
                    await page.evaluate('''() => {
                        window.nativeCalls = 0;
                        window.appDownloadManager.managers.apiManager.invokeApi = async (_m, _p, options) => {
                            window.lastOptions = options;
                            throw {code: 500, type: 'UNKNOWN'};
                        };
                        window.appDownloadManager.managers.apiFileManager.requestFilePart = async () => {
                            window.nativeCalls++; throw new Error('must not mask server error');
                        };
                    }''')
                    with self.assertRaises(RemoteFileError) as failed:
                        await fetch_resume_part(page, {'kind': 'document', 'mid': '42',
                            'id': '777', 'dc': 4, 'peer': '-1', 'thumb': ''}, 0, 1024)
                    self.assertEqual(failed.exception.code, 500)
                    self.assertEqual(await page.evaluate('window.nativeCalls'), 0)
                    self.assertTrue(await page.evaluate('window.lastOptions.noRetry'))
                    self.assertTrue(await page.evaluate('window.lastOptions.noErrorBox'))
                finally:
                    await browser.close()
        asyncio.run(scenario())

    def test_visible_chat_links_or_open_messages_are_session_entry_but_skeleton_is_not(self):
        async def scenario():
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(channel='chrome', headless=True)
                try:
                    page = await browser.new_page()
                    for html in (
                        '<div id="column-left"><a href="#-1">fixture</a></div>',
                        '<div id="column-center"><div class="bubble" data-mid="42" '
                        'data-peer-id="-1" style="height:10px;width:10px"></div></div>',
                    ):
                        await page.set_content(html)
                        await wait_for_login(page, timeout=2_000)
                    await page.set_content('<div id="column-left"></div>')
                    with self.assertRaises(BrowserTimeout):
                        await wait_for_login(page, timeout=100)
                finally:
                    await browser.close()
        asyncio.run(scenario())


HTML = '''
<div id="column-center"><div class="bubble" data-mid="4294967301" data-peer-id="-1000000005" data-timestamp="1700000001">
  <div class="attachment"><div class="album-item" data-mid="4294967302" data-peer-id="-1000000005"><div class="media-container">
    Protected media fixture without a visible download control.
  </div></div></div>
  <div class="message">说明文字</div>
</div></div>
<a id="file" href="data:application/octet-stream;base64,YWJj" download="fixture.bin">file</a>
<script>
window.mtprotoMessagePort = {getMessageByPeer(peer, mid) { return {mid, date:1700000018, message:mid === 4294967301 ? '说明文字' : '', media:mid === 4294967301 ? null : {_: 'messageMediaDocument', document:{_: 'document', id:mid, dc_id:1, size:3, file_name:'fixture.bin', access_hash:'fixture', file_reference:[1]}}}; }};
window.appDownloadManager = {managers:{apiFileManager:{async requestFilePart({offset, limit}) {
  if (offset !== 0 || limit !== 1048576) throw Error('wrong part');
  return {_: 'upload.file', bytes: new Uint8Array([97,98,99])};
}}}};
</script>
'''

TEXT_CASE_HTML = '''
<div id="column-center">
  <div class="bubble" data-mid="101" data-peer-id="-1" data-timestamp="1700000001">
    <div class="attachment"><div class="document-container"><span class="message">sample.zip</span></div></div>
  </div>
  <div class="bubble" data-mid="102" data-peer-id="-1" data-timestamp="1700000002">
    <div class="message">真正的说明</div>
  </div>
  <div class="bubble" data-mid="103" data-peer-id="-1" data-timestamp="1700000003">
    <div class="attachment"><div class="document-container"><span class="message">archive.zip</span></div></div>
  </div>
  <div class="bubble" data-mid="104" data-peer-id="-1" data-timestamp="1700000004">
    <div class="attachment">
      <div class="album-item" data-mid="105" data-peer-id="-1"><div class="media-container">first.zip</div></div>
      <div class="album-item" data-mid="106" data-peer-id="-1"><div class="media-container">second.zip</div></div>
    </div>
  </div>
  <div class="bubble" data-mid="107" data-peer-id="-1">
    <div class="message">
      <div class="document-container grouped-item" data-mid="107" data-peer-id="-1"><div class="document">first</div></div>
      <div class="document-container grouped-item" data-mid="108" data-peer-id="-1"><div class="document">second</div></div>
    </div>
  </div>
</div>
<script>
window.mtprotoMessagePort = {getMessageByPeer(peer, mid) {
  const body = {101:'', 102:'真正的说明', 103:'说明包含 archive.zip', 104:'', 105:'', 106:'相册说明', 107:'', 108:'#第226期'};
  return {mid, date:1700000000 + mid, message:body[mid], media: mid !== 102 ? {_: 'messageMediaDocument', document:{size:3}} : null};
}};
</script>
'''


class BrowserTests(unittest.TestCase):
    def test_download_scan_uses_server_dates_without_reading_dom_cache(self):
        item = Message('4294967400', 'fixture', 1793680677, 0,
                       (('4294967400', 'fixture', 1793680677, False),))
        page = MagicMock()
        progress = ScanProgress()
        async def scan():
            return [entry async for entry in iter_history(
                page, None, set(), progress, peer_id=-1, api_scan=True)]
        with (patch('telegg.browser.server_history_with_recovery',
                    new_callable=AsyncMock, side_effect=[[item], []]) as history,
              patch('telegg.browser.visible_messages', new_callable=AsyncMock) as dom):
            self.assertEqual(asyncio.run(scan()), [item])
            self.assertEqual(history.call_args_list[0].args[2], 1 << 32)
            dom.assert_not_called()
            self.assertTrue(progress.reached_oldest)

    def test_media_positions_follow_source_ids_when_dom_order_changes(self):
        item = Message('41', '', 1700000000, 2,
                       (('41', '', 1700000000, True), ('42', '', 1700000001, True)),
                       (('41', 'document'), ('42', 'document')))
        targets = MagicMock()
        targets.count = AsyncMock(return_value=2)
        targets.nth.side_effect = lambda index: index
        with (patch('telegg.browser.locate_message', new_callable=AsyncMock),
              patch('telegg.browser.media_targets', new_callable=AsyncMock, return_value=targets),
              patch('telegg.browser.task_metadata', new_callable=AsyncMock,
                    side_effect=[('42', 3), ('41', 3)])):
            found = asyncio.run(discover_media(None, 'https://web.telegram.org/k/#-1', item))
        self.assertEqual({entry.media_mid: entry.index for entry in found}, {'41': 1, '42': 2})

    def test_changed_media_identity_creates_numbered_file_version(self):
        base = TEST_ROOT / f'media-edited-{uuid.uuid4().hex}'
        folder, partials = base / 'output', base / 'partials'
        folder.mkdir(parents=True)
        partials.mkdir(parents=True)
        old = folder / 'm42_1_777_file.bin'
        old.write_bytes(b'old')
        source = {'peer': '-1', 'mid': '42', 'kind': 'document', 'id': '778',
                  'dc': 1, 'thumb': '', 'size': 4, 'name': 'file.bin',
                  'mime_type': 'application/octet-stream'}
        original_identity = {key: source[key] for key in ('peer', 'mid', 'kind', 'id', 'dc', 'thumb')}
        original_identity['id'] = '777'
        (partials / '42.jsonl').write_text(json.dumps({
            'kind': 'identity', 'identity': original_identity, 'size': 3,
        }) + '\n', encoding='utf-8')
        task = MediaTask(Message('42', '', 1700000000, 1), 1, '42', folder, 3, partials)
        partial = MagicMock()
        partial.open.return_value = 0

        def finish(path):
            path.write_bytes(b'new!')
            return 4, '0' * 64

        partial.finalize.side_effect = finish
        with (patch('telegg.browser.resume_source', new_callable=AsyncMock, return_value=source),
              patch('telegg.browser.PartialFile', return_value=partial) as partial_type,
              patch('telegg.browser.download_parts', new_callable=AsyncMock)):
            state, final, *_ = asyncio.run(save_one(None, None, task, {'42': [old]}))
        self.assertEqual(state, 'downloaded')
        self.assertEqual((folder / 'm42_1_777_file.v1.bin').read_bytes(), b'old')
        self.assertEqual(final.name, '2023-11-15-m42_1_778_file.v2.bin')
        self.assertEqual(final.read_bytes(), b'new!')
        self.assertEqual(partial_type.call_args.args[1], '42.v2')
        self.assertEqual(existing_media_index(folder), {'42': [final]})
        (partials / '42.v2.jsonl').write_text(json.dumps({
            'kind': 'identity', 'identity': {key: source[key] for key in original_identity},
            'size': 4,
        }) + '\n', encoding='utf-8')
        current_task = MediaTask(task.message, 1, '42', folder, 4, partials)
        with patch('telegg.browser.resume_source', new_callable=AsyncMock, return_value=source):
            self.assertEqual(asyncio.run(save_one(None, None, current_task, existing_media_index(folder)))[0], 'skipped')
            moved_position = MediaTask(task.message, 2, '42', folder, 4, partials)
            with self.assertRaisesRegex(RuntimeError, '附件位置变化'):
                asyncio.run(save_one(None, None, moved_position, existing_media_index(folder)))
        self.assertEqual(len(list(folder.iterdir())), 2)

    def test_transport_timeout_reopens_download_page_and_retries_same_media(self):
        scan = MagicMock()
        replacement = MagicMock()
        scan.context.new_page = AsyncMock(return_value=replacement)
        task = MagicMock(media_mid='42')
        timeout = RuntimeError('分片偏移 1024 连续失败：Error: timeout')
        self.assertTrue(is_transport_timeout(timeout))
        self.assertFalse(is_transport_timeout(RuntimeError('续传身份或大小已变化')))
        self.assertFalse(is_transport_timeout(LocalDataError('本地路径含分片偏移连续失败timeout')))
        with (patch('telegg.browser.open_group', new_callable=AsyncMock),
              patch('telegg.browser.save_media', new_callable=AsyncMock,
                    side_effect=[timeout, ('downloaded', None)]) as save):
            state, old_folder, page = asyncio.run(save_media_with_recovery(
                scan, scan, 'https://web.telegram.org/k/#-1', task, {}))
        self.assertEqual((state, old_folder, page), ('downloaded', None, replacement))
        self.assertIs(save.await_args_list[1].args[0], replacement)

    def test_persistent_transport_timeout_stops_after_one_page_rebuild(self):
        scan = MagicMock()
        replacement = MagicMock()
        replacement.close = AsyncMock()
        scan.context.new_page = AsyncMock(return_value=replacement)
        task = MagicMock(media_mid='42')
        timeout = RuntimeError('分片偏移 1024 连续失败：Error: timeout')
        with (patch('telegg.browser.open_group', new_callable=AsyncMock),
              patch('telegg.browser.save_media', new_callable=AsyncMock,
                    side_effect=[timeout, timeout])):
            with self.assertRaisesRegex(RuntimeError, '重建下载页面后仍连续超时'):
                asyncio.run(save_media_with_recovery(
                    scan, scan, 'https://web.telegram.org/k/#-1', task, {}))
        replacement.close.assert_awaited_once()

    def test_recovery_page_open_failure_stops_batch(self):
        scan = MagicMock()
        replacement = MagicMock()
        replacement.close = AsyncMock()
        scan.context.new_page = AsyncMock(return_value=replacement)
        task = MagicMock(media_mid='42')
        timeout = RuntimeError('分片偏移 0 连续失败：Error: timeout')
        with (patch('telegg.browser.open_group', new_callable=AsyncMock,
                    side_effect=RuntimeError('连接中断')),
              patch('telegg.browser.save_media', new_callable=AsyncMock,
                    side_effect=timeout)):
            with self.assertRaisesRegex(RuntimeError, '下载页面重建失败'):
                asyncio.run(save_media_with_recovery(
                    scan, scan, 'https://web.telegram.org/k/#-1', task, {}))
        replacement.close.assert_awaited_once()
    def test_locate_nearby_newer_message_without_reopening_group(self):
        url = 'https://web.telegram.org/k/#-1'
        target = Message('105', '正文', 1700000002, 0)
        page = MagicMock()
        page.url = url
        bubble = MagicMock()
        bubble.count = AsyncMock(side_effect=[0, 1])
        scroll = MagicMock()
        scroll.hover = AsyncMock()
        page.locator.side_effect = lambda selector: SimpleNamespace(
            first=bubble if '[data-mid="105"]' in selector else scroll)
        page.mouse.wheel = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        with patch('telegg.browser.visible_messages', new_callable=AsyncMock,
                   return_value=[Message('100', '较旧', 1700000001, 0)]):
            result = asyncio.run(locate_message(page, url, target))
        self.assertIs(result, bubble)
        page.mouse.wheel.assert_awaited_once_with(0, 500)
        page.goto.assert_not_called()

    def test_locate_nearby_older_message_without_reopening_group(self):
        url = 'https://web.telegram.org/k/#-1'
        target = Message('100', '正文', 1700000001, 0)
        page = MagicMock()
        page.url = url
        bubble = MagicMock()
        bubble.count = AsyncMock(return_value=0)
        page.locator.return_value.first = bubble

        async def nearby(*_args, **_kwargs):
            yield target

        with (patch('telegg.browser.visible_messages', new_callable=AsyncMock,
                    return_value=[Message('105', '较新', 1700000002, 0)]),
              patch('telegg.browser.iter_history', side_effect=nearby) as history):
            result = asyncio.run(locate_message(page, url, target))
        self.assertIs(result, bubble)
        page.goto.assert_not_called()
        self.assertFalse(history.call_args.kwargs['start_at_latest'])

    def test_staged_group_is_published_only_after_all_files_are_verified(self):
        base = TEST_ROOT / f'group-publish-{uuid.uuid4().hex}'
        root, staging = base / 'downloaded', base / 'downloading'
        message = Message('1', '完整编组文字超过二十字符时需要保存全部原始文字', 1700000001, 2,
                          (('101', '', 1700000001, True), ('102', '', 1700000001, True)),
                          (('101', 'document'), ('102', 'document')))
        record = SimpleNamespace(history_complete=True, ordered_messages=lambda: [message],
                                 results={'101': {'status': 'downloaded'},
                                          '102': {'status': 'downloaded'}}, media={},
                                 source_messages={'101': {'content_type': 'document'},
                                                  '102': {'content_type': 'document'}},
                                 content_files={})
        pending = staging / group_folder(message)
        pending.mkdir(parents=True)
        (pending / '2023-11-15-m101_1_701.bin').write_bytes(b'a')
        self.assertEqual(publish_ready_staged_groups(root, staging, record), 0)
        self.assertTrue(pending.is_dir())
        (pending / '2023-11-15-m102_1_702.bin').write_bytes(b'b')
        self.assertEqual(publish_ready_staged_groups(root, staging, record), 0)
        for mid in ('101', '102'):
            content = base / f'{mid}.json'
            content.write_bytes(b'{}')
            record.content_files[mid] = str(content)
        self.assertEqual(publish_ready_staged_groups(root, staging, record), 1)
        self.assertTrue((root / group_folder(message) / '2023-11-15-m102_1_702.bin').is_file())
        self.assertFalse(pending.exists())

    def test_long_text_migrates_old_folder_and_saves_exact_text(self):
        base = TEST_ROOT / f'text-folder-{uuid.uuid4().hex}'
        anchor = Message('42', '一二三四五六七八九十甲乙丙丁戊己庚辛壬癸\n完整文字', 1700000000, 1)
        old = base / '2023-11-15-一二三四五六七八九十甲乙丙丁戊己庚辛壬癸 完整文字 [m42]'
        old.mkdir(parents=True)
        (old / 'm42_1_a.bin').write_bytes(b'a')
        with self.assertRaisesRegex(RuntimeError, '旧目录不能整体迁移'):
            prepare_group_folder(base, anchor, create=True)
        self.assertTrue((old / 'm42_1_a.bin').is_file())
        self.assertFalse((base / group_folder(anchor)).exists())

    def test_edited_full_text_uses_numbered_versions(self):
        base = TEST_ROOT / f'text-edited-{uuid.uuid4().hex}'
        original = Message('42', 'abcdefghijklmnopqrst-old', 1700000000, 0)
        edited = Message('42', 'abcdefghijklmnopqrst-new', 1700000000, 0)
        folder = prepare_group_folder(base, original, create=True)
        first = save_text_file(folder, original.parts[0] if original.parts else
                               (original.mid, original.text, original.timestamp, False))
        second = save_text_file(folder, edited.parts[0] if edited.parts else
                                (edited.mid, edited.text, edited.timestamp, False))
        self.assertNotEqual(first, second)
        self.assertEqual(first.read_text(encoding='utf-8'), original.text)
        self.assertEqual(second.read_text(encoding='utf-8'), edited.text)

    def test_ten_character_folder_migrates_to_full_text_when_at_most_twenty(self):
        base = TEST_ROOT / f'text-twenty-{uuid.uuid4().hex}'
        anchor = Message('42', 'abcdefghijklmnop', 1700000000, 1)
        old = base / previous_group_folder(anchor)
        old.mkdir(parents=True)
        (old / 'm42_1_a.bin').write_bytes(b'a')
        with self.assertRaisesRegex(RuntimeError, '旧目录不能整体迁移'):
            prepare_group_folder(base, anchor, create=True)
        self.assertTrue((old / 'm42_1_a.bin').is_file())

    def test_text_folder_conflict_does_not_overwrite(self):
        base = TEST_ROOT / f'text-conflict-{uuid.uuid4().hex}'
        anchor = Message('42', '一二三四五六七八九十一', 1700000000, 1)
        old = base / '2023-11-15-一二三四五六七八九十一 [m42]'
        old.mkdir(parents=True)
        new = base / group_folder(anchor)
        new.mkdir()
        with self.assertRaisesRegex(RuntimeError, '旧目录不能整体迁移'):
            prepare_group_folder(base, anchor, create=True)
        self.assertTrue(old.is_dir())
        self.assertTrue(new.is_dir())

    def test_old_message_subfolder_is_renamed_without_touching_media(self):
        base = TEST_ROOT / f'item-folder-{uuid.uuid4().hex}'
        anchor = Message('42', '说明', 1700000000, 1)
        item = Message('41', '', 1699999999, 1)
        group = base / group_folder(anchor)
        old = group / '2023-11-15 06-13-19 [m41]'
        old.mkdir(parents=True)
        (old / 'm41_1_a.bin').write_bytes(b'a')
        with self.assertRaisesRegex(RuntimeError, '旧消息子目录不能整体迁移'):
            prepare_item_folders(group, [item, anchor])
        self.assertTrue((old / 'm41_1_a.bin').is_file())

    def test_content_type_categories(self):
        self.assertEqual(classify('messageMediaDocument', ('documentAttributeVideo',)), 'video')
        self.assertEqual(classify('messageMediaDocument', ('documentAttributeFilename',)), 'document')
        self.assertEqual(classify('messageMediaWebPage'), 'link_preview')
        self.assertEqual(classify('messageMediaFuture'), 'unknown:messageMediaFuture')
        with self.assertRaisesRegex(RuntimeError, '未识别'):
            validate_types((('1', '', 1700000001, True),),
                           (('1', classify('messageMediaFuture')),))
        with self.assertRaisesRegex(RuntimeError, '暂不支持完整保存'):
            validate_types((('2', '', 1700000002, True),), (('2', 'story'),))

    def test_text_and_poll_content_are_saved_without_overwrite(self):
        root = TEST_ROOT / 'content-archive-fixture'
        item = Message('201', '投票说明', 1700000201, 0,
                       (('201', '投票说明', 1700000201, True),),
                       (('201', 'poll'),),
                       (('201', '{"_":"messageMediaPoll","poll":{"question":"选择"}}'),))
        path = save_message_content(root, item, item.parts[0])
        self.assertEqual(save_message_content(root, item, item.parts[0]), path)
        self.assertIn('选择', path.read_text(encoding='utf-8'))
        self.assertIn('投票说明', path.read_text(encoding='utf-8'))
        preview = Message('202', 'link', 1700000202, 0,
                          (('202', 'link', 1700000202, True),),
                          (('202', 'link_preview'),),
                          (('202', '{"webpage":{"photo":{"id":"1"}}}'),))
        with self.assertRaisesRegex(RuntimeError, '内嵌媒体'):
            content_payload(preview, preview.parts[0])

    def test_switch_group_reloads_same_page(self):
        page = MagicMock()
        page.url = 'https://web.telegram.org/k/#-1'
        async def goto(url, **_kwargs):
            page.url = url
        page.goto = AsyncMock(side_effect=goto)
        page.wait_for_function = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        bubble = MagicMock()
        bubble.wait_for = AsyncMock()
        title = MagicMock()
        title.count = AsyncMock(return_value=1)
        title.inner_text = AsyncMock(return_value='第二群')
        page.locator.side_effect = lambda selector: (bubble if selector.startswith(BUBBLE) else title)
        bubble.first = bubble
        title.first = title
        target = 'https://web.telegram.org/k/#-2'
        with patch('telegg.browser.wait_for_login', new_callable=AsyncMock):
            self.assertEqual(asyncio.run(open_group(page, target)), '第二群')
        self.assertEqual([call.args[0] for call in page.goto.await_args_list], ['about:blank', target])

    def test_open_group_from_home_unloads_home_route(self):
        page = MagicMock()
        page.url = 'https://web.telegram.org/k/'
        async def goto(url, **_kwargs):
            page.url = url
        page.goto = AsyncMock(side_effect=goto)
        page.wait_for_function = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        bubble = MagicMock()
        bubble.wait_for = AsyncMock()
        title = MagicMock()
        title.count = AsyncMock(return_value=1)
        title.inner_text = AsyncMock(return_value='真实群名')
        page.locator.side_effect = lambda selector: (bubble if selector.startswith(BUBBLE) else title)
        bubble.first = bubble
        title.first = title
        target = 'https://web.telegram.org/k/#-1000000005'
        with patch('telegg.browser.wait_for_login', new_callable=AsyncMock):
            self.assertEqual(asyncio.run(open_group(page, target)), '真实群名')
        self.assertEqual([call.args[0] for call in page.goto.await_args_list], ['about:blank', target])

    def test_open_group_keeps_already_visible_chat(self):
        target = 'https://web.telegram.org/k/#-1000000005'
        page = MagicMock()
        page.url = target
        page.goto = AsyncMock()
        page.wait_for_function = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        bubble = MagicMock()
        bubble.count = AsyncMock(return_value=1)
        bubble.wait_for = AsyncMock()
        title = MagicMock()
        title.count = AsyncMock(return_value=1)
        title.inner_text = AsyncMock(return_value='已打开的群组')
        page.locator.side_effect = lambda selector: bubble if selector.startswith(BUBBLE) else title
        bubble.first = bubble
        title.first = title
        self.assertEqual(asyncio.run(open_group(page, target)), '已打开的群组')
        page.goto.assert_not_awaited()

    def test_chatlist_fallback_searches_peer_title_and_clicks(self):
        page = MagicMock()
        page.goto = AsyncMock()
        page.evaluate = AsyncMock(return_value='目标群组')
        target = MagicMock()
        target.count = AsyncMock(return_value=0)
        target.wait_for = AsyncMock()
        target.click = AsyncMock()
        search = MagicMock()
        search.fill = AsyncMock()
        bubble = MagicMock()
        bubble.wait_for = AsyncMock()
        target.first = target
        search.first = search
        bubble.first = bubble
        page.locator.side_effect = lambda selector: (target if selector.startswith('#column-left')
                                                    else search if selector == '.input-search-input'
                                                    else bubble)
        with patch('telegg.browser.wait_for_login', new_callable=AsyncMock):
            asyncio.run(open_group_from_chatlist(page, '-1000000005'))
        search.fill.assert_awaited_once_with('目标群组')
        target.click.assert_awaited_once()
        bubble.wait_for.assert_awaited_once()

    def test_open_group_recovers_empty_deep_link(self):
        target = 'https://web.telegram.org/k/#-1000000005'
        page = MagicMock()
        page.url = 'https://web.telegram.org/k/'
        async def goto(url, **_kwargs):
            page.url = url
        page.goto = AsyncMock(side_effect=goto)
        page.wait_for_function = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        bubble = MagicMock()
        bubble.wait_for = AsyncMock(side_effect=BrowserTimeout('empty route'))
        title = MagicMock()
        title.count = AsyncMock(return_value=1)
        title.inner_text = AsyncMock(return_value='目标群组')
        bubble.first = bubble
        title.first = title
        page.locator.side_effect = lambda selector: bubble if selector.startswith(BUBBLE) else title
        with (patch('telegg.browser.wait_for_login', new_callable=AsyncMock),
              patch('telegg.browser.open_group_from_chatlist', new_callable=AsyncMock) as recover):
            self.assertEqual(asyncio.run(open_group(page, target)), '目标群组')
        recover.assert_awaited_once_with(page, '-1000000005')

    def test_visible_chrome_fixture(self):
        async def check():
            async with async_playwright() as playwright:
                context = await playwright.chromium.launch_persistent_context(
                    str(TEST_ROOT / 'chrome-fixture-profile'), channel='chrome',
                    headless=True, accept_downloads=True,
                )
                try:
                    page = context.pages[0] if context.pages else await context.new_page()
                    await page.set_content(HTML)
                    messages = await visible_messages(page)
                    self.assertEqual([(m.mid, m.text, m.media_count) for m in messages],
                                     [('4294967301', '说明文字', 1)])
                    self.assertEqual(messages[0].timestamp, 1700000018)
                    targets = await media_targets(page.locator(BUBBLE).first)
                    self.assertEqual(await targets.count(), 1)
                    self.assertEqual(await page.locator('.download-button').count(), 0)
                    found = await discover_media(page, 'https://web.telegram.org/k/#-1000000005', messages[0])
                    self.assertEqual([media.media_mid for media in found], ['4294967302'])
                    root = TEST_ROOT / uuid.uuid4().hex
                    task = MediaTask(messages[0], found[0].index, found[0].media_mid,
                                     destination(root, messages[0], messages, messages[0]), None,
                                     root / '.partials')
                    self.assertEqual((await save_media(page, 'https://web.telegram.org/k/#-1000000005',
                                                       task, {}))[0], 'downloaded')
                    self.assertEqual(next(root.rglob('*.bin')).read_bytes(), b'abc')
                    await page.set_content('''
                    <div id="column-center"><div class="bubble" data-mid="1" data-peer-id="-1">
                      <div class="album-item" data-mid="101" data-peer-id="-1"><div class="media-container"></div></div>
                    </div></div>
                    <script>
                    window.mtprotoMessagePort = {getMessageByPeer(peer, mid) { return {
                      media:{document:{_: 'document', id:mid, dc_id:1, size:3,
                        file_name:'cached.bin', access_hash:'fixture', file_reference:[1]}}}; }};
                    window.appDownloadManager = {managers:{apiFileManager:{async requestFilePart() {
                      return {_: 'upload.file', bytes: new Uint8Array([97,98,99])};
                    }}}};
                    </script>''')
                    partial_album = Message('1', '正文', 1700000001, 2,
                                            (('101', '', 1700000001, True),
                                             ('102', '', 1700000001, True)),
                                            (('101', 'document'), ('102', 'document')))
                    recovered = await discover_media(page, 'https://web.telegram.org/k/#-1', partial_album)
                    self.assertEqual([(m.media_mid, m.index) for m in recovered], [('101', 1), ('102', 2)])
                    cached_root = TEST_ROOT / uuid.uuid4().hex
                    cached_task = MediaTask(partial_album, 2, '102', cached_root, 3,
                                            cached_root / '.partials')
                    self.assertEqual((await save_media(page, 'https://web.telegram.org/k/#-1',
                                                       cached_task, {}))[0], 'downloaded')
                    self.assertEqual(next(cached_root.glob('*-m102_2_*.bin')).read_bytes(), b'abc')
                    await page.set_content(TEXT_CASE_HTML)
                    text_cases = await visible_messages(page)
                    self.assertEqual([message.text for message in text_cases],
                                     ['', '真正的说明', '说明包含 archive.zip', '相册说明', '#第226期'])
                    self.assertEqual([part[0] for part in text_cases[-1].parts], ['107', '108'])
                    doc_targets = await media_targets(page.locator(f'{BUBBLE}[data-mid="107"]'))
                    self.assertEqual([await doc_targets.nth(i).evaluate('n => n.closest("[data-mid]").dataset.mid')
                                      for i in range(await doc_targets.count())], ['107', '108'])
                    self.assertEqual([[message.mid for message in batch] for _, batch in grouped(text_cases)],
                                     [['101', '102', '103', '104', '107']])
                    await page.set_content('''<div id="column-center"><div class="bubble" data-mid="201" data-peer-id="-1">
                      <div class="attachment"><div class="new-attachment-box">file</div></div></div></div>
                      <script>window.mtprotoMessagePort={getMessageByPeer(peer,mid){return {mid,date:1700000201,
                      message:'',media:{_: 'messageMediaDocument',document:{size:3}}}}}</script>''')
                    unknown_box = (await visible_messages(page))[0]
                    with self.assertRaisesRegex(RuntimeError, '附件'):
                        await discover_media(page, 'https://web.telegram.org/k/#-1', unknown_box)
                    await page.set_content(TEXT_CASE_HTML)
                    await page.evaluate('delete window.mtprotoMessagePort')
                    with self.assertRaises(RuntimeError):
                        await visible_messages(page)
                    await page.set_content('''
                    <div id="column-center"><div class="bubble" data-mid="201" data-peer-id="-1">
                      <div class="album-item" data-mid="201" data-peer-id="-1"><div class="media-container"></div></div>
                      <div class="album-item" data-mid="202" data-peer-id="-1"><div class="media-container"></div></div>
                    </div></div>
                    <script>window.mtprotoMessagePort={getMessageByPeer(peer,mid){return {
                      mid,date:mid===201?1700000201:1700000190,message:'',
                      media:{_: 'messageMediaPhoto'}}}}</script>''')
                    with self.assertRaisesRegex(RuntimeError, '时间顺序异常'):
                        await visible_messages(page)
                finally:
                    await context.close()
        asyncio.run(check())


if __name__ == '__main__':
    unittest.main()
