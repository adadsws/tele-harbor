"""逐文件模式的只读运行前校验；不修复、移动或补写任何数据。"""

import json
from pathlib import Path

from .content_archive import content_payload
from .errors import LocalDataError
from .failure_notes import validate_failure_notes
from .layout import destination, group_folder, grouped
from .media_index import existing_media_index
from .reply_groups import linked_group_names, parent_for
from .verification_manifest import read_snapshot


COMPLETE_MEDIA = {'downloaded', 'skipped', 'relocated'}


def _require_file(path: Path, label: str) -> None:
    if not path.is_file() or path.is_symlink():
        raise LocalDataError(f'{label}缺失或不是普通文件：{path}')


def _read_json(path: Path):
    _require_file(path, '本地清单')
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (ValueError, OSError) as exc:
        raise LocalDataError(f'本地清单损坏：{path}') from exc


def _check_content(record, item, part, folders) -> None:
    mid = part[0]
    value = record.content_files.get(mid)
    if not value:
        raise LocalDataError(f'消息 m{mid} 缺少内容文件记录')
    path = Path(value)
    _require_file(path, f'消息 m{mid} 内容文件')
    if path.suffix == '.txt':
        if path.parent not in folders or path.read_bytes() != part[1].encode('utf-8'):
            raise LocalDataError(f'消息 m{mid} 原文文件内容或路径不符：{path}')
    elif _read_json(path) != content_payload(item, part):
        raise LocalDataError(f'消息 m{mid} 内容记录不符：{path}')


def _check_media(record, mid, folders, partials) -> None:
    result = record.results[mid]
    value = result.get('detail')
    if not value:
        raise LocalDataError(f'媒体 m{mid} 缺少成品路径记录')
    path = Path(value)
    _require_file(path, f'媒体 m{mid}')
    if path.parent not in folders:
        raise LocalDataError(f'媒体 m{mid} 路径不符合头像段/group：{path}')
    found = record.media.get(mid)
    if found and found.expected_size and path.stat().st_size != found.expected_size:
        raise LocalDataError(f'媒体 m{mid} 本地大小与记录不符：{path}')
    snapshot = read_snapshot(partials / f'{mid}.verified.jsonl', strict=True, required=True)
    if snapshot['identity'].get('mid') != mid or snapshot['size'] != path.stat().st_size:
        raise LocalDataError(f'媒体 m{mid} 验收证据与本地文件不符：{path}')
    if Path(snapshot['path']).name != path.name:
        raise LocalDataError(f'媒体 m{mid} 文件名与验收证据不符：{path}')


def _check_manifest(folder, batch) -> None:
    path = folder / '段内清单.json'
    rows = [{'grouped_id': item.album_id or None,
             'message_ids': [part[0] for part in item.parts] or [item.mid]} for item in batch]
    if _read_json(path) != {'items': rows}:
        raise LocalDataError(f'头像段清单成员或相册分组不符：{path}')


def _check_links(root, record, by_mid, *, allow_incomplete=False) -> None:
    for parent in root.glob('*关联组'):
        required = {(mid, str(source['reply_to'])) for mid, source in record.source_messages.items()
                    if source.get('reply_to') and by_mid.get(mid) == parent.name
                    and by_mid.get(str(source['reply_to'])) == parent.name}
        payload = _read_json(parent / '关联清单.json')
        try:
            actual = {(row['source_message_id'], row['target_message_id']) for row in payload['replies']}
        except (KeyError, TypeError) as exc:
            raise LocalDataError(f'关联清单格式错误：{parent}') from exc
        ids = {mid for manifest in parent.rglob('段内清单.json')
               for row in _read_json(manifest)['items'] for mid in row['message_ids']}
        # 暂存引用目标可以尚无文件，但必须属于已知关联元数据；成品则要求成员全部在场。
        known = record.source_messages if allow_incomplete else ids
        if not required or actual != required or any(a not in known or b not in known for a, b in actual):
            raise LocalDataError(f'关联清单引用关系不符：{parent}')


def validate_local_archive(root: Path, staging: Path, record, partials: Path,
                           *, enabled: bool = True) -> None:
    if not enabled:
        return
    print('[逐文件检查] 先只读核对已有路径与清单；异常即报错停止', flush=True)
    for base in (root, staging):
        validate_failure_notes(base)
    text_media = {mid for mid, value in record.source_messages.items()
                  if value.get('content_type') == 'document'}
    for mid, paths in existing_media_index(root, staging, known_text_media=text_media).items():
        if len(paths) > 1:
            raise LocalDataError(f'同一消息存在重复媒体文件：m{mid}，数量 {len(paths)}')
    links = linked_group_names(record)
    by_mid = {}
    allowed_segments = set()
    for anchor, batch in grouped(record.ordered_messages()):
        parents = [parent_for(base, anchor, links) for base in (root, staging)]
        segments = [parent / group_folder(anchor) for parent in parents]
        allowed_segments.update(segments)
        for segment in segments:
            if segment.is_dir() and any(segment.rglob('*')):
                _check_manifest(segment, batch)
        for item in batch:
            folders = {destination(parent, anchor, batch, item) for parent in parents}
            for part in item.parts:
                mid = part[0]
                by_mid[mid] = links.get(anchor.mid)
                status = record.results.get(mid, {}).get('status')
                if status not in COMPLETE_MEDIA | {'saved_content', 'no_media'}:
                    continue
                _check_content(record, item, part, folders)
                if status in COMPLETE_MEDIA:
                    _check_media(record, mid, folders, partials)
    for base in (root, staging):
        for folder in base.rglob('*'):
            if folder.is_dir() and not any(folder.iterdir()):
                raise LocalDataError(f'业务目录存在空文件夹：{folder}')
        for manifest in base.rglob('段内清单.json'):
            if manifest.parent not in allowed_segments:
                raise LocalDataError(f'头像段目录不符合记录：{manifest.parent}')
    _check_links(root, record, by_mid)
    _check_links(staging, record, by_mid, allow_incomplete=True)
