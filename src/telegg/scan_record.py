"""逐群保存扫描覆盖范围和附件 ID，供续跑时复用。"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace
from pathlib import Path

from .layout import Message


@dataclass(frozen=True, slots=True)
class FoundMedia:
    source_mid: str
    index: int
    media_mid: str
    expected_size: int | None


class ScanRecord:
    """只有看到真实历史顶部才记 complete；追加日志不覆盖旧记录。"""

    def __init__(self, path: Path):
        self.path = path
        self.messages: dict[str, Message] = {}
        self.source_messages: dict[str, dict] = {}
        self.results: dict[str, dict] = {}
        self.content_files: dict[str, str] = {}
        self.media: dict[str, FoundMedia] = {}
        self.discovered: set[str] = set()
        self.verified_groups: dict[str, str] = {}
        self.verified_messages: dict[str, str] = {}
        self.history_complete = False
        if path.is_file():
            for line_number, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
                try:
                    self._apply(json.loads(line))
                except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise RuntimeError(f'扫描记录损坏：{path} 第 {line_number} 行') from exc

    _EVENT_HANDLERS = {
        'reset': '_apply_reset',
        'message': '_apply_message',
        'source_message': '_apply_source_message',
        'result': '_apply_result',
        'content_file': '_apply_content_file',
        'media': '_apply_media',
        'discovered': '_apply_discovered',
        'invalidate_media': '_apply_invalidate_media',
        'history_complete': '_apply_history_complete',
        'group_verified': '_apply_group_verified',
        'message_verified': '_apply_message_verified',
    }

    def _apply(self, event: dict) -> None:
        kind = event['kind']
        handler = self._EVENT_HANDLERS.get(kind)
        if handler is None:
            raise ValueError(f'未知扫描记录事件：{kind}')
        getattr(self, handler)(event)

    def _apply_reset(self, event: dict) -> None:
        self.messages.clear()
        self.source_messages.clear()
        self.results.clear()
        self.content_files.clear()
        self.media.clear()
        self.discovered.clear()
        self.verified_groups.clear()
        self.verified_messages.clear()
        self.history_complete = False

    def _apply_message(self, event: dict) -> None:
        message = Message(str(event['mid']), str(event['text']),
                          int(event['timestamp']), int(event['media_count']),
                          tuple((str(part[0]), str(part[1]), int(part[2]), bool(part[3]))
                                for part in event.get('parts', [])),
                          tuple((str(part[0]), str(part[1]))
                                for part in event.get('content_types', [])),
                          tuple((str(part[0]), str(part[1]))
                                for part in event.get('content_details', [])),
                          tuple((str(part[0]), str(part[1]))
                                for part in event.get('reply_targets', [])),
                          str(event.get('album_id', '')),
                          str(event.get('sender_id', '')),
                          bool(event.get('outgoing', False)),
                          str(event.get('post_author', '')))
        if not message.mid.isdecimal() or message.timestamp <= 0 or message.media_count < 0:
            raise ValueError('消息元数据无效')
        previous = self.messages.get(message.mid)
        if previous and previous.media_count != message.media_count:
            self.discovered.discard(message.mid)
            self.media = {mid: item for mid, item in self.media.items()
                          if item.source_mid != message.mid}
        self.messages[message.mid] = message

    def _apply_source_message(self, event: dict) -> None:
        mid = str(event['mid'])
        if not mid.isdecimal() or int(event['timestamp']) <= 0:
            raise ValueError('原消息元数据无效')
        self.source_messages[mid] = event

    def _apply_result(self, event: dict) -> None:
        mid = str(event['mid'])
        if not mid.isdecimal() or event['status'] not in ('downloaded', 'skipped', 'relocated', 'failed', 'no_media', 'saved_content'):
            raise ValueError('原消息处理结果无效')
        self.results[mid] = event

    def _apply_content_file(self, event: dict) -> None:
        mid = str(event['mid'])
        if not mid.isdecimal() or not event['path']:
            raise ValueError('消息内容文件记录无效')
        self.content_files[mid] = str(event['path'])

    def _apply_media(self, event: dict) -> None:
        item = FoundMedia(str(event['source_mid']), int(event['index']),
                          str(event['media_mid']), event['expected_size'])
        if not item.source_mid.isdecimal() or not item.media_mid.isdecimal() or item.index < 1:
            raise ValueError('附件元数据无效')
        if item.expected_size is not None and (not isinstance(item.expected_size, int) or item.expected_size < 1):
            raise ValueError('附件大小无效')
        self.media[item.media_mid] = item

    def _apply_discovered(self, event: dict) -> None:
        mid = str(event['source_mid'])
        if not mid.isdecimal():
            raise ValueError('消息 ID 无效')
        self.discovered.add(mid)

    def _apply_invalidate_media(self, event: dict) -> None:
        mid = str(event['source_mid'])
        if not mid.isdecimal():
            raise ValueError('消息 ID 无效')
        self.discovered.discard(mid)
        self.media = {key: item for key, item in self.media.items()
                      if item.source_mid != mid}

    def _apply_history_complete(self, event: dict) -> None:
        if not self.messages:
            raise ValueError('空消息记录不能标为历史已完成')
        # 旧版仅凭静止的 scrollTop 标记完成，不能作为增量扫描的依据。
        self.history_complete = event.get('scan_proof') == 'older_messages_observed_v2'

    def _apply_group_verified(self, event: dict) -> None:
        name, signature = event['group'], event['signature']
        if not isinstance(name, str) or not name or not isinstance(signature, str) or len(signature) != 64:
            raise ValueError('编组核验标记无效')
        self.verified_groups[name] = signature

    def _apply_message_verified(self, event: dict) -> None:
        mid, signature = str(event['mid']), event['signature']
        if not mid.isdecimal() or not isinstance(signature, str) or len(signature) != 64:
            raise ValueError('消息核验标记无效')
        self.verified_messages[mid] = signature


    def append(self, events: list[dict]) -> None:
        if not events:
            return
        fields = ('messages', 'source_messages', 'results', 'content_files', 'media',
                  'discovered', 'verified_groups', 'verified_messages')
        preview = object.__new__(ScanRecord)
        preview.path = self.path
        for field in fields:
            setattr(preview, field, getattr(self, field).copy())
        preview.history_complete = self.history_complete
        for event in events:
            preview._apply(event)
        lines = ''.join(json.dumps(event, ensure_ascii=False) + '\n' for event in events)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open('a', encoding='utf-8', newline='\n') as stream:
            stream.write(lines)
            stream.flush()
            os.fsync(stream.fileno())
        for field in fields:
            setattr(self, field, getattr(preview, field))
        self.history_complete = preview.history_complete

    @staticmethod
    def message_event(item: Message) -> dict:
        return {'kind': 'message', 'mid': item.mid, 'text': item.text,
                'timestamp': item.timestamp, 'media_count': item.media_count,
                'parts': item.parts, 'content_types': item.content_types,
                'content_details': item.content_details,
                'reply_targets': item.reply_targets, 'album_id': item.album_id,
                'sender_id': item.sender_id, 'outgoing': item.outgoing,
                'post_author': item.post_author}

    @staticmethod
    def source_events(item: Message) -> list[dict]:
        types = dict(item.content_types)
        replies = dict(item.reply_targets)
        return [{'kind': 'source_message', 'mid': mid, 'bubble_mid': item.mid,
                 'text': text, 'timestamp': timestamp, 'has_media': has_media,
                 'content_type': types.get(mid, 'unclassified'),
                 'reply_to': replies.get(mid, ''), 'sender_id': item.sender_id}
                for mid, text, timestamp, has_media in item.parts]

    def record_result(self, mid: str, status: str, *, detail: str = '',
                      naming_revision: int | None = None) -> None:
        event = {'kind': 'result', 'mid': mid, 'status': status, 'detail': detail}
        if naming_revision is not None:
            event['naming_revision'] = naming_revision
        self.append([event])

    def record_content_file(self, mid: str, path: Path) -> None:
        self.append([{'kind': 'content_file', 'mid': mid, 'path': str(path)}])

    def record_reply(self, mid: str, target: str) -> None:
        """只补齐现有原消息的引用，不把相册泡泡拆成新消息。"""
        source = self.source_messages.get(mid)
        if source is None:
            raise ValueError(f'原消息引用元数据无效：m{mid}')
        self.record_source_metadata(mid, int(source['timestamp']), target)

    def record_source_metadata(self, mid: str, timestamp: int, target: str) -> bool:
        """以服务器最新时间和引用更新已有原消息，保留相册泡泡结构。"""
        source = self.source_messages.get(mid)
        if source is None or timestamp <= 0 or (target and not target.isdecimal()):
            raise ValueError(f'原消息引用元数据无效：m{mid}')
        if int(source['timestamp']) == timestamp and source.get('reply_to', '') == target:
            return False
        events = []
        bubble = self.messages.get(str(source.get('bubble_mid', mid)))
        if bubble is not None:
            replies = dict(bubble.reply_targets)
            if target:
                replies[mid] = target
            else:
                replies.pop(mid, None)
            parts = tuple((part_mid, text, timestamp if part_mid == mid else part_time, has_media)
                          for part_mid, text, part_time, has_media in bubble.parts)
            ordered = tuple((part[0], replies[part[0]]) for part in bubble.parts
                            if part[0] in replies)
            events.append(self.message_event(replace(
                bubble, timestamp=timestamp if bubble.mid == mid else bubble.timestamp,
                parts=parts, reply_targets=ordered)))
        events.append({**source, 'timestamp': timestamp, 'reply_to': target})
        self.append(events)
        return True

    def mark_group_verified(self, name: str, signature: str) -> None:
        if self.verified_groups.get(name) != signature:
            self.append([{'kind': 'group_verified', 'group': name, 'signature': signature}])

    def mark_message_verified(self, mid: str, signature: str) -> None:
        if self.verified_messages.get(mid) != signature:
            self.append([{'kind': 'message_verified', 'mid': mid, 'signature': signature}])

    def record_scan(self, messages: list[Message], *, reached_oldest: bool,
                    replace: bool = False) -> None:
        if reached_oldest and not messages:
            raise ValueError('没有消息，不能确认历史已扫描到底')
        if reached_oldest and replace:
            events = [{'kind': 'reset'}, *(self.message_event(item) for item in messages)]
            events.extend(event for item in messages for event in self.source_events(item))
            events.append({'kind': 'history_complete', 'scan_proof': 'older_messages_observed_v2'})
        else:
            events = [self.message_event(item) for item in messages
                      if self.messages.get(item.mid) != item]
            for item in messages:
                for event in self.source_events(item):
                    mid = event['mid']
                    if self.source_messages.get(mid) != event:
                        events.append(event)
            if reached_oldest and not self.history_complete:
                events.append({'kind': 'history_complete', 'scan_proof': 'older_messages_observed_v2'})
        self.append(events)

    def record_discovered(self, source_mid: str, items: list[FoundMedia]) -> None:
        events = [{'kind': 'media', 'source_mid': item.source_mid, 'index': item.index,
                   'media_mid': item.media_mid, 'expected_size': item.expected_size}
                  for item in items]
        events.append({'kind': 'discovered', 'source_mid': source_mid})
        self.append(events)

    def invalidate_media(self, source_mid: str) -> None:
        self.append([{'kind': 'invalidate_media', 'source_mid': source_mid}])

    def full_snapshot(self, messages: list[Message]) -> None:
        """历史顶部确认后以追加 reset 事件封存本轮完整视图。"""
        if not messages:
            raise ValueError('没有消息，不能确认完整历史')
        active = {item.mid for item in messages}
        events = [{'kind': 'reset'}, *(self.message_event(item) for item in messages)]
        events.extend(event for item in messages for event in self.source_events(item))
        events.extend({'kind': 'media', 'source_mid': item.source_mid, 'index': item.index,
                       'media_mid': item.media_mid, 'expected_size': item.expected_size}
                      for item in self.media.values() if item.source_mid in active)
        events.extend({'kind': 'discovered', 'source_mid': mid}
                      for mid in self.discovered if mid in active)
        source_ids = {mid for item in messages for mid, *_rest in item.parts}
        events.extend(result for mid, result in self.results.items() if mid in source_ids)
        events.extend({'kind': 'content_file', 'mid': mid, 'path': path}
                      for mid, path in self.content_files.items() if mid in source_ids)
        events.extend({'kind': 'group_verified', 'group': name, 'signature': signature}
                      for name, signature in self.verified_groups.items())
        events.extend({'kind': 'message_verified', 'mid': mid, 'signature': signature}
                      for mid, signature in self.verified_messages.items() if mid in active)
        events.append({'kind': 'history_complete', 'scan_proof': 'older_messages_observed_v2'})
        self.append(events)

    def ordered_messages(self) -> list[Message]:
        # 与服务器历史分页及流式归组保持同一原消息 ID 顺序；Telegram 时间戳偶有倒序。
        return sorted(self.messages.values(), key=lambda item: min(
            (int(part[0]) for part in item.parts), default=int(item.mid)))
