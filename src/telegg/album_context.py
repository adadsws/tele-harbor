"""确认无 group 消息旧侧最近的相册；仅请求身份和日期，不读取附件字节。"""

import asyncio
from datetime import datetime

from .layout import CHINA_TIME, album_folder


PREVIOUS_ALBUM = '''async ({peerId, offset}) => {
    const m = window.appDownloadManager?.managers;
    if (!m?.apiManager || !m?.appPeersManager) throw new Error('Telegram 历史接口不可用');
    const peer = await m.appPeersManager.getInputPeerById(peerId);
    const result = await m.apiManager.invokeApi('messages.getHistory', {
        peer, offset_id: offset, offset_date: 0, add_offset: 0,
        limit: 100, max_id: 0, min_id: 0, hash: 0
    }, {noErrorBox: true, noRetry: true});
    if (!Array.isArray(result?.messages)) throw new Error('历史响应无消息列表');
    return result.messages.map(x => ({id:Number(x.id), type:x._,
        album_id:String(x.grouped_id || ''), date:Number(x.date || 0),
        peer_id:Number(x.peer_id?.channel_id || x.peer_id?.chat_id || 0)}));
}'''


async def preceding_album(page, peer: int, mid: str) -> dict:
    """严格向旧端推进，只有服务器空页才允许确认为首个 group 之前。"""
    offset = int(mid) % (1 << 32)
    found = None
    while True:
        rows = await asyncio.wait_for(page.evaluate(PREVIOUS_ALBUM, {'peerId': peer, 'offset': offset}), 60)
        if not isinstance(rows, list):
            raise RuntimeError('前一相册响应无效')
        if not rows:
            return found or {'album_id': '', 'day': '', 'previous_mid': ''}
        previous = offset
        for row in rows:
            raw = row.get('id')
            if type(raw) is not int or not 0 < raw < previous:
                raise RuntimeError('查找前一相册时服务器未向旧端推进')
            if row.get('type') not in ('message', 'messageService', 'messageEmpty'):
                raise RuntimeError('前一相册响应包含未知消息类型')
            if row['type'] != 'messageEmpty' and row.get('peer_id') != abs(peer):
                raise RuntimeError('前一相册响应群组不符')
            previous = raw
            if found is not None and row.get('album_id') != found['album_id']:
                return found
            if row.get('album_id') and row['type'] == 'message':
                if not str(row['album_id']).isdecimal() or int(row.get('date', 0)) <= 0:
                    raise RuntimeError('前一相册身份或日期无效')
                day = datetime.fromtimestamp(row['date'], CHINA_TIME).strftime('%Y-%m-%d')
                found = {'album_id': str(row['album_id']),
                         'day': min(found['day'], day) if found else day,
                         'previous_mid': str((1 << 32) + raw)}
        offset = previous


def context_from_album(album) -> dict:
    return {'album_id': album.album_id, 'day': album_folder(album).split('-g')[0],
            'previous_mid': album.mid}


async def batch_contexts(page, peer: int, batch: list, known: dict,
                         *, oldest_confirmed: bool = False) -> dict:
    """批内顺序完整时复用相册；旧侧前缀按持久化证据或服务器补查。"""
    result = {}
    previous = None
    for item in sorted(batch, key=lambda message: int(message.mid)):
        if item.album_id:
            previous = context_from_album(item)
            continue
        if previous is None:
            previous = known.get(item.mid)
            if previous is None:
                previous = ({'album_id': '', 'day': '', 'previous_mid': ''} if oldest_confirmed
                            else await preceding_album(page, peer, item.mid))
        result[item.mid] = dict(previous)
    return result
