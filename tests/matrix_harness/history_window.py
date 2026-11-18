"""隔离 BAT 的真实历史采样上界；不伪造消息、空页或媒体响应。"""

HOOK = '''import telegg.browser as browser
original = browser.server_older_batch
async def bounded(page, peer_id, oldest_mid):
    if peer_id == PEER and oldest_mid == (1 << 32):
        oldest_mid = UPPER + 1
    return await original(page, peer_id, oldest_mid)
browser.server_older_batch = bounded
async def native_discover(page, url, item):
    return await browser.discover_media_server(page, int(browser.group_id(url)), item)
browser.discover_media = native_discover
browser.save_media = browser.save_media_server
'''


def install(runtime, info):
    if 'upper' not in info:
        return
    body = f"PEER = {int(info['peer'])}\nUPPER = {int(info['upper'])}\n" + HOOK
    (runtime / 'src/sitecustomize.py').write_text(body, encoding='utf-8')
