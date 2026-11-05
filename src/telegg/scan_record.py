"""逐群保存扫描覆盖范围和附件 ID，供续跑时复用。"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
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
        self.history_complete = False
        if path.is_file():
            for line_number, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
                try:
                    self._apply(json.loads(line))
                except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise RuntimeError(f'扫描记录损坏：{path} 第 {line_number} 行') from exc

    def _apply(self, event: dict) -> None:
        kind = event['kind']
        if kind == 'reset':
            self.messages.clear()
            self.source_messages.clear()
            self.results.clear()
            self.content_files.clear()
            self.media.clear()
            self.discovered.clear()
            self.history_complete = False
        elif kind == 'message':
            message = Message(str(event['mid']), str(event['text']),
                              int(event['timestamp']), int(event['media_count']),
                              tuple((str(part[0]), str(part[1]), int(part[2]), bool(part[3]))
                                    for part in event.get('parts', [])),
                              tuple((str(part[0]), str(part[1]))
                                    for part in event.get('content_types', [])),
                              tuple((str(part[0]), str(part[1]))
                                    for part in event.get('content_details', [])))
            if not message.mid.isdecimal() or message.timestamp <= 0 or message.media_count < 0:
                raise ValueError('消息元数据无效')
            previous = self.messages.get(message.mid)
            if previous and previous.media_count != message.media_count:
                self.discovered.discard(message.mid)
                self.media = {mid: item for mid, item in self.media.items()
                              if item.source_mid != message.mid}
            self.messages[message.mid] = message
        elif kind == 'source_message':
            mid = str(event['mid'])
            if not mid.isdecimal() or int(event['timestamp']) <= 0:
                raise ValueError('原消息元数据无效')
            self.source_messages[mid] = event
        elif kind == 'result':
            mid = str(event['mid'])
            if not mid.isdecimal() or event['status'] not in ('downloaded', 'skipped', 'relocated', 'failed', 'no_media', 'saved_content'):
                raise ValueError('原消息处理结果无效')
            self.results[mid] = event
        elif kind == 'content_file':
            mid = str(event['mid'])
            if not mid.isdecimal() or not event['path']:
                raise ValueError('消息内容文件记录无效')
            self.content_files[mid] = str(event['path'])
        elif kind == 'media':
            item = FoundMedia(str(event['source_mid']), int(event['index']),
                              str(event['media_mid']), event['expected_size'])
            if not item.source_mid.isdecimal() or not item.media_mid.isdecimal() or item.index < 1:
                raise ValueError('附件元数据无效')
            if item.expected_size is not None and (not isinstance(item.expected_size, int) or item.expected_size < 1):
                raise ValueError('附件大小无效')
            self.media[item.media_mid] = item
        elif kind == 'discovered':
            mid = str(event['source_mid'])
            if not mid.isdecimal():
                raise ValueError('消息 ID 无效')
            self.discovered.add(mid)
        elif kind == 'invalidate_media':
            mid = str(event['source_mid'])
            if not mid.isdecimal():
                raise ValueError('消息 ID 无效')
            self.discovered.discard(mid)
            self.media = {key: item for key, item in self.media.items()
                          if item.source_mid != mid}
        elif kind == 'history_complete':
            if not self.messages:
                raise ValueError('空消息记录不能标为历史已完成')
            # 旧版仅凭静止的 scrollTop 标记完成，不能作为增量扫描的依据。
            self.history_complete = event.get('scan_proof') == 'older_messages_observed_v2'
        else:
            raise ValueError(f'未知扫描记录事件：{kind}')

    def append(self, events: list[dict]) -> None:
        if not events:
            return
        fields = ('messages', 'source_messages', 'results', 'content_files', 'media', 'discovered')
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
                'content_details': item.content_details}

    @staticmethod
    def source_events(item: Message) -> list[dict]:
        types = dict(item.content_types)
        return [{'kind': 'source_message', 'mid': mid, 'bubble_mid': item.mid,
                 'text': text, 'timestamp': timestamp, 'has_media': has_media,
                 'content_type': types.get(mid, 'unclassified')}
                for mid, text, timestamp, has_media in item.parts]

    def record_result(self, mid: str, status: str, *, detail: str = '') -> None:
        self.append([{'kind': 'result', 'mid': mid, 'status': status, 'detail': detail}])

    def record_content_file(self, mid: str, path: Path) -> None:
        self.append([{'kind': 'content_file', 'mid': mid, 'path': str(path)}])

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
        events.append({'kind': 'history_complete', 'scan_proof': 'older_messages_observed_v2'})
        self.append(events)

    def ordered_messages(self) -> list[Message]:
        return sorted(self.messages.values(), key=lambda item: (item.timestamp, int(item.mid)))
