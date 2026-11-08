"""依据原消息引用关系连接头像段，并安全调整现有目录。"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from .layout import CHINA_TIME, Message, group_folder, grouped
from .local_reuse import has_group_files, move_files_only
from .scan_record import ScanRecord


LINKED_NAME = re.compile(r'^\[From_\d{4}-\d{2}-\d{2}-m[1-9]\d*\] 关联组$')


def linked_anchors(record: ScanRecord) -> dict[str, str]:
    """把引用边映射到头像段；连接具有传递性。"""
    messages = record.ordered_messages()
    groups = grouped(messages)
    by_bubble = {item.mid: anchor.mid for anchor, batch in groups if anchor for item in batch}
    by_mid = {mid: by_bubble.get(str(source.get('bubble_mid', mid)))
              for mid, source in getattr(record, 'source_messages', {}).items()}
    for anchor, batch in groups:
        if anchor is None:
            continue
        for item in batch:
            for part in (item.parts or ((item.mid, '', 0, False),)):
                if not by_mid.get(part[0]):
                    by_mid[part[0]] = anchor.mid
    parents = {anchor.mid: anchor.mid for anchor, _batch in groups if anchor}
    touched: set[str] = set()

    def find(mid: str) -> str:
        while parents[mid] != mid:
            parents[mid] = parents[parents[mid]]
            mid = parents[mid]
        return mid

    for source, entry in getattr(record, 'source_messages', {}).items():
        target = str(entry.get('reply_to') or '')
        left, right = by_mid.get(source), by_mid.get(target)
        if not left or not right:
            continue
        touched.update((left, right))
        a, b = find(left), find(right)
        if a != b:
            low, high = sorted((a, b), key=int)
            parents[high] = low
    return {anchor: find(anchor) for anchor in touched}


def linked_group_names(record: ScanRecord) -> dict[str, str]:
    """以连通分量里最旧头像段的最旧消息日期和编号命名。"""
    links = linked_anchors(record)
    anchors = {anchor.mid: anchor for anchor, _batch in grouped(record.ordered_messages()) if anchor}
    oldest: dict[str, Message] = {}
    for mid, leader in links.items():
        item = anchors[mid]
        current = oldest.get(leader)
        if current is None or (item.timestamp, int(item.mid)) < (current.timestamp, int(current.mid)):
            oldest[leader] = item
    names = {leader: f'{group_folder(item)} 关联组' for leader, item in oldest.items()}
    return {mid: names[leader] for mid, leader in links.items()}


def parent_for(root: Path, anchor: Message | None,
               names: dict[str, str]) -> Path:
    if anchor is None or anchor.mid not in names:
        return root
    return root / names[anchor.mid]


def _trial_oldest_name(anchor: Message, batch: list[Message]) -> str:
    """识别上一轮未提交试跑使用的最旧原消息组名。"""
    mid, timestamp = min(((mid, timestamp) for item in batch
                          for mid, _text, timestamp, _media in
                          (item.parts or ((item.mid, item.text, item.timestamp, False),))),
                         key=lambda part: (part[1], int(part[0])))
    day = datetime.fromtimestamp(timestamp, CHINA_TIME).strftime('%Y-%m-%d')
    return f'[From_{day}-m{mid}]'


def _record_moved_paths(record: ScanRecord, source: Path, target: Path) -> None:
    """目录移动后同步追加成功结果中的本机路径，不重写旧日志。"""
    events = []
    for result in getattr(record, 'results', {}).values():
        detail = result.get('detail', '')
        path = Path(detail) if detail else Path()
        if not path.is_absolute() or not path.is_relative_to(source):
            continue
        events.append({**result, 'detail': str(target / path.relative_to(source))})
    for mid, value in getattr(record, 'content_files', {}).items():
        path = Path(value)
        if path.is_absolute() and path.is_relative_to(source):
            events.append({'kind': 'content_file', 'mid': mid,
                           'path': str(target / path.relative_to(source))})
    if events and hasattr(record, 'append'):
        record.append(events)


def reconcile_reply_groups(root: Path, record: ScanRecord) -> int:
    """逐文件调整唯一的现有编组；目标存在或发现副本时停止。"""
    if not root.is_dir():
        return 0
    links = linked_group_names(record)
    moved = 0
    base = root.resolve()
    locations: dict[str, list[Path]] = {}
    for path in root.iterdir():
        if not path.is_dir():
            continue
        if LINKED_NAME.fullmatch(path.name):
            for child in path.iterdir():
                if has_group_files(child):
                    locations.setdefault(child.name, []).append(child)
        elif has_group_files(path):
            locations.setdefault(path.name, []).append(path)
    for anchor, batch in grouped(record.ordered_messages()):
        if anchor is None:
            continue
        name = group_folder(anchor)
        trial_name = name
        desired = parent_for(root, anchor, links) / name
        present = list(dict.fromkeys([*locations.get(name, []), *locations.get(trial_name, [])]))
        if len(present) > 1 or (present and not present[0].is_dir()):
            raise RuntimeError(f'关联编组目录不唯一或类型异常：{name}')
        if present and present[0] != desired:
            if (not present[0].resolve().is_relative_to(base) or
                    not desired.resolve().is_relative_to(base)):
                raise RuntimeError(f'关联编组目录超出指定根目录：{name}')
            if has_group_files(desired):
                raise RuntimeError(f'关联编组目标已存在，未覆盖：{desired}')
            desired.parent.mkdir(parents=True, exist_ok=True)
            source = present[0]
            move_files_only(source, desired)
            _record_moved_paths(record, source, desired)
            if not hasattr(record, 'relocated_folders'):
                record.relocated_folders = set()
            record.relocated_folders.add(source)
            if (trial_name != name and hasattr(record, 'verified_groups') and
                    trial_name in record.verified_groups and hasattr(record, 'mark_group_verified')):
                record.mark_group_verified(name, record.verified_groups[trial_name])
            locations[trial_name] = []
            locations[name] = [desired]
            moved += 1
        if not desired.is_dir():
            continue
    reconcile_link_manifests(root, record, links)
    return moved


def reconcile_link_manifests(root: Path, record: ScanRecord, links: dict[str, str]) -> None:
    """关联成员齐全后生成引用清单，发生变化时保留旧版本。"""
    if links:
        groups = grouped(record.ordered_messages())
        anchor_folders = {anchor.mid: group_folder(anchor) for anchor, _batch in groups if anchor}
        by_source = {mid: anchor.mid for anchor, batch in groups for item in batch
                     for mid, *_rest in (item.parts or ((item.mid, '', 0, False),))}
        for parent_name in sorted(set(links.values())):
            pairs = sorted(((mid, str(entry.get('reply_to')))
                            for mid, entry in record.source_messages.items()
                            if entry.get('reply_to') and
                            links.get(by_source.get(mid, '')) == parent_name and
                            links.get(by_source.get(str(entry['reply_to']), '')) == parent_name),
                           key=lambda pair: int(pair[0]))
            if not pairs:
                continue
            parent = root / parent_name
            if not parent.is_dir():
                continue
            if any(not all(has_group_files(parent / anchor_folders[by_source[mid]])
                           for mid in (source, target)) for source, target in pairs):
                continue
            content = (json.dumps({'replies': [
                {'source_message_id': source, 'target_message_id': target}
                for source, target in pairs]}, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
            manifest = parent / '关联清单.json'
            if manifest.is_file() and manifest.read_bytes() == content:
                continue
            if manifest.is_file():
                versions = [int(match.group(1)) for old in parent.glob('关联清单.v*.json')
                            if (match := re.fullmatch(r'关联清单\.v([1-9]\d*)\.json', old.name))]
                manifest.rename(parent / f'关联清单.v{max(versions, default=0) + 1}.json')
            elif manifest.exists():
                raise RuntimeError(f'关联清单路径不是文件：{manifest}')
            with manifest.open('xb') as stream:
                stream.write(content)
