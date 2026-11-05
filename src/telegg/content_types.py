"""按 Telegram 原消息元数据分类；未知结构保留原始类型供审计。"""

from __future__ import annotations


# 每个选择器代表一个可操作附件；外层容器和内部子节点由调用方去重。
MEDIA_SELECTOR = '.media-container, .document-container, .audio-container, .sticker-container, .document, .audio, .voice, .sticker'
DOWNLOADABLE = {'photo', 'document', 'video', 'video_note', 'audio', 'voice', 'sticker', 'animation'}
METADATA_ONLY = {'text', 'poll', 'contact', 'location', 'venue', 'link_preview'}


def classify(media_type: str, attributes: tuple[str, ...] = ()) -> str:
    """返回稳定内容类别；任何未见过的 Telegram media 构造名均显式标记。"""
    if not media_type:
        return 'text'
    if media_type == 'messageMediaPhoto':
        return 'photo'
    if media_type == 'messageMediaDocument':
        if 'documentAttributeSticker' in attributes or 'documentAttributeCustomEmoji' in attributes:
            return 'sticker'
        if 'documentAttributeAnimated' in attributes:
            return 'animation'
        if 'documentAttributeAudio' in attributes:
            return 'voice' if 'voice' in attributes else 'audio'
        if 'documentAttributeVideo' in attributes:
            return 'video_note' if 'round' in attributes else 'video'
        return 'document'
    return {
        'messageMediaWebPage': 'link_preview',
        'messageMediaPoll': 'poll',
        'messageMediaContact': 'contact',
        'messageMediaGeo': 'location',
        'messageMediaGeoLive': 'location',
        'messageMediaVenue': 'venue',
        'messageMediaGame': 'game',
        'messageMediaInvoice': 'invoice',
        'messageMediaStory': 'story',
    }.get(media_type, f'unknown:{media_type}')


def validate_types(parts: tuple[tuple[str, str, int, bool], ...],
                   types: tuple[tuple[str, str], ...]) -> None:
    """不允许有媒体的原消息落入“无附件”或未知类别。"""
    if not types:  # 兼容升级前已保存的历史记录；新扫描总会带分类。
        return
    classified = dict(types)
    for mid, _text, _date, has_media in parts:
        kind = classified.get(mid, 'unknown:missing')
        if kind.startswith('unknown:'):
            raise RuntimeError(f'原消息 m{mid} 的内容类型未识别：{kind}')
        if kind not in DOWNLOADABLE and kind not in METADATA_ONLY:
            raise RuntimeError(f'原消息 m{mid} 的内容类型暂不支持完整保存：{kind}')
        if has_media and kind == 'text':
            raise RuntimeError(f'原消息 m{mid} 的媒体类型未处理：{kind}')
        if not has_media and kind != 'text':
            raise RuntimeError(f'原消息 m{mid} 的内容类型与媒体元数据不一致：{kind}')
