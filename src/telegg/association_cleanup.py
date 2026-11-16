"""只归档已发布完整关联分量的重复辅助清单；媒体和未知文件保持原位。"""
import hashlib
import json
from datetime import datetime
from pathlib import Path

from .errors import LocalDataError
from .layout import group_folder, grouped
from .media_index import existing_media_index
from .reply_groups import linked_group_names


def _regular(path):
    if (not path.is_file() or any(p.is_symlink() or (hasattr(p, 'is_junction') and p.is_junction())
                                for p in (path, *path.parents))):
        raise LocalDataError(f'关联清单不是普通文件或路径含链接：{path}')


def archive_duplicate_associations(root: Path, staging: Path, record, complete, archive_root: Path,
                                   *, exclude=None):
    parents = [parent for parent in staging.glob('*关联组')
               if (parent/'关联清单.json').exists()
               and parent/'关联清单.json' not in (exclude or set())
               and {p.name for p in parent.iterdir()} == {'关联清单.json'}
               and (root/parent.name/'关联清单.json').exists()]
    if not parents:
        return set()  # 没有辅助残留时，不增加快速模式的全量媒体索引。
    links = linked_group_names(record)
    groups = grouped(record.ordered_messages())
    by_source = {mid: links.get(anchor.mid) for anchor, batch in groups for item in batch
                 for mid, *_ in item.parts}
    index = existing_media_index(root, known_text_media=set(record.media))
    plans = []
    for parent in parents:
        source = parent/'关联清单.json'
        if not source.exists() or {p.name for p in parent.iterdir()} != {source.name}:
            continue
        target = root/parent.name/source.name
        if not target.exists():
            continue
        _regular(source); _regular(target)
        body = source.read_bytes()
        if body != target.read_bytes():
            raise LocalDataError(f'暂存与成品关联清单不同，保留现场：{source}')
        pairs = sorted((mid, str(row['reply_to'])) for mid, row in record.source_messages.items()
                       if row.get('reply_to') and by_source.get(mid) == parent.name
                       and by_source.get(str(row['reply_to'])) == parent.name)
        expected = {'replies':[{'source_message_id':a,'target_message_id':b} for a,b in pairs]}
        try:
            valid = pairs and json.loads(body) == expected
        except (ValueError, UnicodeError):
            valid = False
        if not valid:
            raise LocalDataError(f'关联清单与当前引用记录不符，保留现场：{source}')
        members = [(anchor,batch) for anchor,batch in groups if links.get(anchor.mid) == parent.name]
        if not members or any(not (root/parent.name/group_folder(anchor)).is_dir()
                              or not complete(batch, record, index) for anchor,batch in members):
            continue
        for anchor,batch in members:
            from .local_validation import _check_manifest
            _check_manifest(root/parent.name/group_folder(anchor), batch)
        plans.append((source, target, body))
    if not plans:
        return set()
    if any(p.is_symlink() or (hasattr(p, 'is_junction') and p.is_junction())
           for p in (archive_root, *archive_root.parents)):
        raise LocalDataError('关联清单归档路径包含链接或目录联接')
    batch = archive_root/datetime.now().strftime('duplicate-associations-%Y%m%d-%H%M%S-%f')
    retired = set()
    for source,target,body in plans:
        if source.read_bytes() != body or target.read_bytes() != body or list(source.parent.iterdir()) != [source]:
            raise LocalDataError('关联清单归档前发生变化，已停止')
        destination = batch/root.name/source.parent.name/source.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            raise LocalDataError('关联清单归档目标已存在')
        ledger = destination.with_name('archive-record.json')
        with ledger.open('x', encoding='utf-8') as stream:
            json.dump({'source':str(source),'archive':str(destination),
                       'sha256':hashlib.sha256(body).hexdigest()},stream,ensure_ascii=False,indent=2)
        source.rename(destination)
        retired.add(source.parent)
    return retired
