"""隔离 BAT 的真实历史采样上界；不伪造消息、空页或媒体响应。"""

HOOK = '''import telegg.browser as browser
original = browser.server_older_batch
async def bounded(page, peer_id, oldest_mid):
    if peer_id == PEER and oldest_mid == (1 << 32):
        oldest_mid = UPPER + 1
    return await original(page, peer_id, oldest_mid)
browser.server_older_batch = bounded
'''


def install(runtime, info):
    if 'upper' not in info:
        return
    body = f"PEER = {int(info['peer'])}\nUPPER = {int(info['upper'])}\n" + HOOK
    (runtime / 'src/sitecustomize.py').write_text(body, encoding='utf-8')


def configure_window(work, info):
    if 'upper' in info:
        # 空池只启用产品原生 API 媒体定位；不注入本地媒体或完成证据。
        (work / '~local' / info['peer']).mkdir(parents=True, exist_ok=True)
