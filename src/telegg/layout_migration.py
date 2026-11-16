"""无 group 定位层的显式迁移：先持久化计划，再逐文件改名和追加状态，可重跑。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from .album_context import batch_contexts
from .errors import LocalDataError
from .failure_notes import REGISTRY, _check_note, registered_notes, registry_path
from .layout import destination, group_folder, group_id, grouped, standalone_folder
from .local_validation import validate_local_archive
from .reply_groups import linked_group_names, parent_for
from .scan_record import ScanRecord
from .verification_manifest import read_snapshot


FILE_MID = re.compile(r'^(?:\d{4}-\d{2}-\d{2}[-_]m|m)(\d+)_')


def stamp(path: Path) -> list[int]:
    stat = path.stat()
    return [stat.st_size, stat.st_mtime_ns, stat.st_dev, stat.st_ino]


def digest(path: Path) -> str:
    """只用于计划、登记和验收日志；不传入媒体或原文文件。"""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def existing_roots(output: Path, staging: Path, peer: str) -> list[Path]:
    from .extra_files import group_roots
    return group_roots(output, staging, peer)


def legacy_files(roots: list[Path], record) -> list[Path]:
    """只枚举头像段根的已知无 group 消息文件，清单和未知用户文件保持原位。"""
    singles = {part[0] for item in record.ordered_messages() if not item.album_id
               for part in (item.parts or ((item.mid, '', 0, False),))}
    return [path for root in roots for path in root.rglob('*')
            if path.is_file() and re.fullmatch(r'\[From_\d{4}-\d{2}-\d{2}-m\d+\]', path.parent.name)
            and (match := FILE_MID.match(path.name)) and match[1] in singles]


def _append(path: Path, events: list[dict]) -> None:
    with path.open('a', encoding='utf-8', newline='\n') as stream:
        for event in events:
            stream.write(json.dumps(event, ensure_ascii=False) + '\n')
        stream.flush()
        os.fsync(stream.fileno())


def _write_new(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise LocalDataError(f'迁移元数据目标已存在：{path}')
    temporary = path.with_name(path.name+'.'+uuid4().hex+'.tmp')
    with temporary.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.rename(path)


def _registry_plans(moves: list[dict], archive: Path, data_root: Path) -> list[dict]:
    """登记按说明的目标目录拆分；旧登记可恢复归档，不覆盖任何现有文件。"""
    changes = {}
    def state(folder):
        if folder not in changes:
            source = registry_path(folder)
            active = registered_notes(folder)
            old = source.read_bytes() if source.is_file() else b''
            changes[folder] = {'source': source, 'active': dict(active), 'old': old}
        return changes[folder]
    for move in moves:
        source, target = Path(move['source']), Path(move['target'])
        entries = registered_notes(source.parent)
        if source.name not in entries:
            continue
        entry = entries[source.name]
        _check_note(source, entry)
        left, right = state(source.parent), state(target.parent)
        if target.name in right['active']:
            raise LocalDataError(f'目标已有同名失败说明登记，未覆盖：{target}')
        del left['active'][source.name]
        right['active'][target.name] = entry
    result = []
    for folder, value in changes.items():
        source = value['source']
        body = ''.join(json.dumps(entry, ensure_ascii=False)+'\n' for entry in value['active'].values())
        result.append({'source': str(source), 'target': str(folder/REGISTRY), 'old_sha256':
                       hashlib.sha256(value['old']).hexdigest() if value['old'] else '',
                       'body': body, 'backup': str(archive/'registry'/source.relative_to(data_root)),
                       'temporary': str(folder/f'telegg-layout-{uuid4().hex}.tmp')})
    return result


def _snapshot_plans(moves: list[dict], partials: Path) -> list[dict]:
    result = []
    seen = set()
    for move in moves:
        source = Path(move['source']); match = FILE_MID.match(source.name)
        if not match:
            continue
        journal = partials / f'{match[1]}.verified.jsonl'
        if journal in seen or not journal.is_file():
            continue
        snapshot = read_snapshot(journal, strict=True)
        if snapshot is None or Path(snapshot['path']).name != source.name:
            continue  # 旧版本及文字不覆盖当前媒体的验收快照。
        values = [snapshot.get(key) for key in ('size', 'mtime_ns', 'dev', 'ino')]
        if values != move['stamp']:
            raise LocalDataError(f'迁移前文件与验收身份不符：{source}')
        result.append({'journal': str(journal), 'old_sha256': digest(journal),
                       'snapshot': {**snapshot, 'path': move['target']}})
        seen.add(journal)
    return result


async def build_plan(page, peer: str, roots: list[Path], record: ScanRecord,
                     partials: Path, archive: Path, data_root: Path) -> dict:
    links = linked_group_names(record)
    contexts = dict(record.standalone_contexts)
    groups = grouped(record.ordered_messages())
    print(f'[布局迁移] 正在确认 {len(groups)} 个头像段的无 group 消息位置', flush=True)
    for position, (_anchor, batch) in enumerate(groups):
        contexts.update(await batch_contexts(page, int(peer), batch, contexts,
                                            oldest_confirmed=position == 0 and record.history_complete))
    destinations = {}
    for anchor, batch in groups:
        for item in batch:
            if item.album_id:
                continue
            for root in roots:
                parent = parent_for(root, anchor, links)
                segment = parent/group_folder(anchor)
                target = destination(parent, anchor, batch, item, standalone=contexts[item.mid])
                for mid, *_ in (item.parts or ((item.mid, '', 0, False),)):
                    destinations[(segment, mid)] = target
    moves = []
    for source in legacy_files(roots, record):
        mid = FILE_MID.match(source.name)[1]
        target = destinations[(source.parent, mid)]/source.name
        if target.exists():
            raise LocalDataError(f'迁移目标已存在，未覆盖：{target}')
        if any(path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction())
               for path in (source, *source.parents, target, *target.parents)):
            raise LocalDataError('迁移路径包含链接或目录联接')
        moves.append({'source': str(source), 'target': str(target), 'stamp': stamp(source)})
    return {'version': 1, 'peer': peer, 'record': str(record.path), 'archive': str(archive),
            'record_sha256': digest(record.path) if record.path.is_file() else '',
            'moves': moves, 'contexts': contexts,
            'registries': _registry_plans(moves, archive, data_root),
            'snapshots': _snapshot_plans(moves, partials)}


def _move_one(move: dict, roots: list[Path]) -> None:
    source, target = Path(move['source']), Path(move['target'])
    if not any(source.resolve().is_relative_to(root.resolve()) and
               target.resolve().is_relative_to(root.resolve()) for root in roots):
        raise LocalDataError('迁移计划路径超出所选群目录')
    if source.exists():
        if target.exists() or stamp(source) != move['stamp']:
            raise LocalDataError(f'迁移文件发生变化或目标冲突：{source}')
        target.parent.mkdir(parents=True, exist_ok=True)
        source.rename(target)
    if not target.is_file() or stamp(target) != move['stamp']:
        raise LocalDataError(f'迁移目标身份无法确认：{target}')


def _update_registry(operation: dict) -> None:
    source, target, backup = map(Path, (operation['source'], operation['target'], operation['backup']))
    body = operation['body'].encode('utf-8')
    new_hash = hashlib.sha256(body).hexdigest()
    if target.is_file() and digest(target) == new_hash:
        return
    if source.is_file():
        if not operation['old_sha256'] or digest(source) != operation['old_sha256'] or backup.exists():
            raise LocalDataError(f'失败说明登记已变化，未覆盖：{source}')
        backup.parent.mkdir(parents=True, exist_ok=True)
        source.rename(backup)
    elif operation['old_sha256'] and (not backup.is_file() or digest(backup) != operation['old_sha256']):
        raise LocalDataError(f'缺少失败说明登记的恢复备份：{source}')
    if body:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(operation['temporary'])
        if temporary.exists() and digest(temporary) != new_hash:
            held = backup.parent/(temporary.name+'.interrupted-'+uuid4().hex)
            held.parent.mkdir(parents=True, exist_ok=True)
            temporary.rename(held)
        if not temporary.exists():
            with temporary.open('xb') as stream:
                stream.write(body); stream.flush(); os.fsync(stream.fileno())
        temporary.rename(target)


def _check_registry_plan(operation: dict) -> None:
    source, target, backup = map(Path, (operation['source'], operation['target'], operation['backup']))
    new_hash = hashlib.sha256(operation['body'].encode('utf-8')).hexdigest()
    if target.is_file() and digest(target) == new_hash:
        if source != target and source.exists():
            raise LocalDataError(f'新旧失败说明登记同时存在：{source}')
        return
    if target.exists() and target != source:
        raise LocalDataError(f'失败说明登记目标冲突：{target}')
    if source.exists():
        if not operation['old_sha256'] or digest(source) != operation['old_sha256'] or backup.exists():
            raise LocalDataError(f'失败说明登记已变化：{source}')
    elif operation['old_sha256'] and (not backup.is_file() or digest(backup) != operation['old_sha256']):
        raise LocalDataError(f'缺少失败说明登记备份：{source}')


def validate_plan(plan: dict, record: ScanRecord, roots: list[Path], data_root: Path, partials: Path) -> None:
    """所有冲突和恢复状态先只读检查；修改前确认计划仍在获准数据根内。"""
    archive = Path(plan['archive']).resolve()
    if (plan.get('version') != 1 or plan.get('peer') != record.path.stem or Path(plan['record']) != record.path or
            not archive.is_relative_to((data_root/'~archive').resolve())):
        raise LocalDataError('迁移计划身份或归档位置不符')
    for location in (Path(plan['archive']), record.path, partials):
        if any(p.is_symlink() or (hasattr(p, 'is_junction') and p.is_junction())
               for p in (location, *location.parents)):
            raise LocalDataError('迁移状态或归档路径包含链接或目录联接')
    probe = object.__new__(ScanRecord); probe.standalone_contexts = {}
    singles = {part[0]: item for item in record.ordered_messages() if not item.album_id
               for part in (item.parts or ((item.mid, '', 0, False),))}
    for mid, context in plan['contexts'].items():
        try:
            probe._apply_standalone_context({'mid': mid, 'context': context})
        except (ValueError, TypeError, KeyError) as exc:
            raise LocalDataError('迁移计划的定位证据无效') from exc
    for move in plan['moves']:
        source, target = Path(move['source']), Path(move['target'])
        match = FILE_MID.match(source.name)
        item = singles.get(match[1]) if match else None
        if (item is None or item.mid not in plan['contexts'] or
                target != source.parent/standalone_folder(item, plan['contexts'][item.mid])/source.name):
            raise LocalDataError('迁移目标不符合无 group 定位规则')
        if not any(source.resolve().is_relative_to(root.resolve()) and
                   target.resolve().is_relative_to(root.resolve()) for root in roots):
            raise LocalDataError('迁移计划文件超出所选群目录')
        if any(p.is_symlink() or (hasattr(p, 'is_junction') and p.is_junction())
               for path in (source, target) for p in (path, *path.parents)):
            raise LocalDataError('迁移计划包含链接或目录联接')
        if source.exists() and target.exists():
            raise LocalDataError(f'迁移目标冲突，文件未移动：{target}')
        current = source if source.exists() else target
        if not current.is_file() or stamp(current) != move['stamp']:
            raise LocalDataError(f'迁移文件身份发生变化：{current}')
    for operation in plan['registries']:
        if any(not any(Path(operation[key]).resolve().is_relative_to(root.resolve()) for root in roots)
               for key in ('source', 'target', 'temporary')):
            raise LocalDataError('失败说明登记位置越界')
        if not Path(operation['backup']).resolve().is_relative_to(archive):
            raise LocalDataError('失败说明登记备份位置越界')
        _check_registry_plan(operation)
    for operation in plan['snapshots']:
        journal = Path(operation['journal'])
        if not journal.resolve().is_relative_to(partials.resolve()):
            raise LocalDataError('验收记录位置越界')
        if read_snapshot(journal, strict=True) != operation['snapshot'] and digest(journal) != operation['old_sha256']:
            raise LocalDataError(f'验收记录在迁移前发生变化：{journal}')


def execute_plan(plan: dict, record: ScanRecord, roots: list[Path], job: Path,
                 *, data_root: Path, partials: Path) -> None:
    validate_plan(plan, record, roots, data_root, partials)
    archive = Path(plan['archive'])
    backup = archive/'scan-records'/record.path.name
    if record.path.is_file() and not backup.exists():
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(record.path, backup)
    for index, move in enumerate(plan['moves'], 1):
        _move_one(move, roots)
        if index % 25 == 0 or index == len(plan['moves']):
            print(f'[布局迁移] 已核对并移动 {index}/{len(plan["moves"])} 个文件', flush=True)
    for operation in plan['registries']:
        _update_registry(operation)
    for operation in plan['snapshots']:
        journal = Path(operation['journal']); snapshot = operation['snapshot']
        if read_snapshot(journal, strict=True) == snapshot:
            continue
        if digest(journal) != operation['old_sha256']:
            raise LocalDataError(f'验收记录在迁移期间变化：{journal}')
        _append(journal, [snapshot])
    mapping = {move['source']: move['target'] for move in plan['moves']}
    events = [{'kind': 'standalone_context', 'mid': mid, 'context': context}
              for mid, context in plan['contexts'].items() if record.standalone_contexts.get(mid) != context]
    events.extend({**value, 'detail': mapping[value['detail']]} for value in record.results.values()
                  if value.get('detail') in mapping)
    events.extend({'kind': 'content_file', 'mid': mid, 'path': mapping[path]}
                  for mid, path in record.content_files.items() if path in mapping)
    record.append(events)
    if not job.with_suffix('.done.json').exists():
        _write_new(job.with_suffix('.done.json'), {'files': len(plan['moves']), 'complete': True})


async def migrate_group_layout(page, url: str, paths: dict, *, apply: bool,
                                preview: Path | None = None) -> tuple[int, Path]:
    from .browser import open_group
    peer = group_id(url)
    await open_group(page, url, require_message=False)
    record = ScanRecord(paths['scan_records']/f'{peer}.jsonl')
    roots = existing_roots(paths['output'], paths['staging'], peer)
    jobs = paths['scan_records'].parent/'layout-migrations'/peer
    pending = [job for job in jobs.glob('*.json') if not job.name.endswith('.done.json')
               and not job.with_suffix('.done.json').exists()]
    if len(pending) > 1:
        raise LocalDataError('存在多个未完成布局迁移计划，未自动选择')
    if preview is not None:
        if pending:
            raise LocalDataError('已有未完成计划，请先不带 --layout-plan 续跑')
        plan = json.loads(preview.read_text(encoding='utf-8'))
        if (plan.get('peer') != peer or Path(plan.get('record', '')) != record.path or
                plan.get('record_sha256') != (digest(record.path) if record.path.is_file() else '')):
            raise LocalDataError('预览计划与当前群组或扫描记录不一致，未执行')
        validate_plan(plan, record, roots, paths['output'].parent, paths['partials']/peer)
        if not apply:
            return len(plan['moves']), preview
        job = jobs/(datetime.now().strftime('%Y%m%d-%H%M%S-')+uuid4().hex[:8]+'.json')
        _write_new(job, plan)
    elif pending:
        job = pending[0]; plan = json.loads(job.read_text(encoding='utf-8'))
    else:
        print('[布局迁移] 先只读核对现有文件、路径和清单', flush=True)
        for root in roots:
            other = (paths['staging'] if root.parent == paths['output'] else paths['output'])/root.name
            validate_local_archive(root if root.parent == paths['output'] else other,
                                   other if root.parent == paths['output'] else root,
                                   record, paths['partials']/peer)
            break
        tag = datetime.now().strftime('%Y%m%d-%H%M%S-')+uuid4().hex[:8]
        archive = paths['output'].parent/'~archive'/f'{tag}-standalone-layout'
        plan = await build_plan(page, peer, roots, record, paths['partials']/peer,
                                archive, paths['output'].parent)
        job = jobs/f'{tag}.json'
        if apply:
            _write_new(job, plan)
        else:
            preview = Path(__file__).resolve().parents[2]/'~outputs-intermediate/layout-migration'/f'{tag}-{peer}.json'
            _write_new(preview, plan)
            return len(plan['moves']), preview
    if apply:
        await asyncio.to_thread(execute_plan, plan, record, roots, job,
                                data_root=paths['output'].parent, partials=paths['partials']/peer)
    return len(plan['moves']), job
