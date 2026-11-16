"""后两种模式共用的已知文件检查与流程结束后一次修正；不寻找本地替代副本。"""
from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from .errors import LocalDataError
from .extra_files import check_extra_files
from .layout import group_id
from .scan_record import ScanRecord
from .verification_manifest import read_snapshot, verified_digest
from .media_index import media_version
from .review_progress import verify_media,with_progress

COMPLETE = {'downloaded', 'skipped', 'relocated'}


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def checked_path(path, bases):
    path = Path(path).absolute()
    if not any(path.is_relative_to(base.absolute()) for base in bases):
        raise LocalDataError(f'记录路径超出本次下载管理范围：{path}')
    if any(p.is_symlink() or (hasattr(p, 'is_junction') and p.is_junction()) for p in (path, *path.parents)):
        raise LocalDataError(f'检查路径包含链接或目录联接：{path}')
    if path.exists() and not path.is_file():
        raise LocalDataError(f'记录路径不是普通文件：{path}')
    return path


def expected_files(record, output, staging):
    """只以最新结果/原文路径登记为准；不把未完成分片当作已下载缺失。"""
    result = {}
    def owned(value):
        path = checked_path(value,(output,staging))
        base = next(base for base in (output,staging) if path.is_relative_to(base.absolute()))
        if not path.relative_to(base.absolute()).parts[0].endswith(f' [{record.path.stem}]'):
            raise LocalDataError(f'登记路径不属于所选群，保留现场：{path}')
        return path
    for mid, value in record.results.items():
        if value['status'] in COMPLETE and value.get('detail'):
            path = owned(value['detail'])
            result[path] = (mid, 'media')
        if value['status'] in COMPLETE | {'saved_content', 'no_media'}:
            text = record.content_files.get(mid)
            if text and any(Path(text).absolute().is_relative_to(base.absolute()) for base in (output, staging)):
                result[owned(text)] = (mid, 'text')
    return result


@dataclass
class Review:
    url: str
    output: Path
    staging: Path
    partials: Path
    record: ScanRecord
    expected: dict
    missing: list[Path]
    extras: list[Path]
    stamps: dict
    record_hash: str
    hash_cache: dict = field(default_factory=dict)


async def inspect_group(page, url, output, staging, partials, *, verify=True, previous=None):
    from .local_config import load_paths
    record = ScanRecord(load_paths()['scan_records']/f'{group_id(url)}.jsonl')
    expected = expected_files(record, output, staging)
    details = await check_extra_files(page, url, output, staging, partials=partials,
                                     expected_mids={int(mid) % (1 << 32) for mid, _ in expected.values()},
                                     details=True,expected_paths=expected)
    missing=details['missing']
    extras = set(details['extras'])
    # 登记路径是保留依据；错位副本归为额外，绝不用于补缺。
    by_mid = {int(mid) % (1 << 32): path for path, (mid, role) in expected.items() if role == 'media'}
    text_by_mid = {int(mid) % (1 << 32): path for path, (mid, role) in expected.items() if role == 'text'}
    historical = set()
    media_by_mid={}
    for item in details['local']:
        if item.role=='media':media_by_mid.setdefault(item.mid,[]).append(item)
    for mid in by_mid:
        versions = sorted(media_by_mid.get(mid,()),
                          key=lambda item:media_version(item.path))
        if len(versions)>1 and [media_version(item.path) for item in versions] == list(range(1,len(versions)+1)):
            historical.update(item.path for item in versions[:-1])
    for item in details['local']:
        if item.path in historical:
            continue
        if item.role == 'media' and item.mid in by_mid:
            wanted = by_mid[item.mid]
            current = details['remote'].get(item.mid)
            if item.path != wanted:
                extras.add(item.path)
            elif current and current['media_id'] == item.media_id:
                extras.discard(item.path)
        elif item.role == 'text' and item.mid in text_by_mid:
            wanted = text_by_mid[item.mid]
            if item.path != wanted and item.path.name == wanted.name:
                extras.add(item.path)
    hash_cache = {}
    print(f'[缺失/额外联合检查] 同轮结果：已下载缺失 {len(missing)} 个，额外/重复 {len(extras)} 个；'
          '流程结束后统一列出并询问，当前不修改',flush=True)
    if verify and record.path.exists():
        from .local_validation import validate_local_archive
        from .extra_files import group_roots
        roots = group_roots(output, staging, group_id(url))
        snapshots={}
        if roots:
            final = next((p for p in roots if p.parent == output), output/roots[0].name)
            pending = next((p for p in roots if p.parent == staging), staging/roots[0].name)
            phase=['准备核对路径与清单',0,len(record.source_messages)]
            def progress(name,done,total):phase[:]=[name,done,total]
            def structure(stop):
                return validate_local_archive(final,pending,record,partials/group_id(url),
                    allow_missing=set(missing),ignore_extra=extras,progress=progress,snapshots=snapshots,stop=stop)
            await with_progress(structure,lambda:f'[完整性检查] {phase[0]} {phase[1]}/{phase[2]} 条')
        hash_cache=await verify_media(expected,set(missing),extras,details['stamps'],partials,group_id(url),
                                      previous=previous,snapshots=snapshots)
    return Review(url, output, staging, partials, record, expected, missing, sorted(extras),
                  details['stamps'], digest(record.path) if record.path.exists() else '',hash_cache)


async def repair_one(page, review, path):
    """新分片独立暂存，先与原验收摘要核对，再放回原登记路径。"""
    from .browser import MediaTask, save_one, server_media_source, server_older_batch
    mid, role = review.expected[path]
    peer = group_id(review.url)
    if path.exists():
        raise LocalDataError(f'补齐目标已出现，未覆盖：{path}')
    job = review.partials/peer/'recovery'/uuid4().hex
    checked_path(job,(review.partials,))
    if role == 'text':
        rows = await server_older_batch(page, int(peer), int(mid)+1)
        source = next((m for m in rows if m.mid == mid), None)
        previous = review.record.source_messages[mid]
        if source is None or source.text != previous['text']:
            raise LocalDataError(f'服务器原文已变化或不可用，未补写：m{mid}')
        job.mkdir(parents=True)
        candidate = job/'original.txt'
        candidate.write_bytes(source.text.encode('utf-8'))
    else:
        snapshot_path = review.partials/peer/f'{mid}.verified.jsonl'
        checked_path(snapshot_path,(review.partials,))
        baseline = read_snapshot(snapshot_path, strict=True, required=True)
        source = await server_media_source(page, int(peer), mid)
        identity = {k: source[k] for k in ('peer','mid','kind','id','dc','thumb')}
        if identity != baseline['identity'] or source['size'] != baseline['size']:
            raise LocalDataError(f'服务器媒体身份或大小已变化，未补齐：m{mid}')
        found = review.record.media[mid]
        item = review.record.messages[found.source_mid]
        task = MediaTask(item, found.index, mid, job/'candidate', source['size'], job/'parts')
        _state, candidate, size, saved_hash, _old = await save_one(page, None, task, {}, server_source=source)
        if saved_hash != baseline['sha256']:
            raise LocalDataError(f'补下载哈希与原验收记录不符，保留恢复分片：m{mid}')
    checked_path(path, (review.output, review.staging))
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise LocalDataError(f'补齐目标在下载期间出现，未覆盖：{path}')
    candidate.rename(path)
    if role == 'media':
        verified_digest(path, identity, snapshot_path, digest, known_digest=saved_hash)
        review.record.record_result(mid, 'downloaded', detail=str(path), naming_revision=3)
    print(f'[补齐完成] {path}', flush=True)


async def finish_review(page, reviews, loose, *, read=None):
    """每批选中群组只问一次；拒绝或 EOF 保持现场。"""
    missing = [(review,path) for review in reviews for path in review.missing]
    extras = sorted(set(loose) | {path for review in reviews for path in review.extras})
    def stamp(path):
        st = path.stat()
        return (st.st_size,st.st_mtime_ns,st.st_dev,st.st_ino)
    loose_stamps = {path:stamp(path) for path in loose}
    print(f'[已下载缺失路径] {len(missing)} 个', flush=True)
    for _review,path in missing: print(path, flush=True)
    print(f'[额外或重复文件路径] {len(extras)} 个', flush=True)
    for path in extras: print(path, flush=True)
    if not missing and not extras:
        return 0, False
    question = '流程已完成。是否修改以上问题（服务器补齐缺失、可恢复归档额外文件）？[y/N]：'
    try:
        answer = await asyncio.to_thread(read or input, question)
    except EOFError:
        answer = ''
    if answer.strip().casefold() not in ('y','yes','是','1'):
        print('[保持不动] 未补齐或归档任何文件', flush=True)
        return 0, False
    # 所有变化先检查，避免确认等待期间用户移动文件后按旧清单执行。
    for review in reviews:
        if (digest(review.record.path) if review.record.path.exists() else '') != review.record_hash:
            raise LocalDataError('扫描记录在检查后变化，修正未启动')
        for path, expected_stamp in review.stamps.items():
            st = path.stat()
            if (st.st_size,st.st_mtime_ns,st.st_dev,st.st_ino) != expected_stamp:
                raise LocalDataError(f'检查后文件变化，修正未启动：{path}')
        if any(path.exists() for path in review.missing):
            raise LocalDataError('缺失目标已出现，修正未启动')
    if any(stamp(path) != before for path,before in loose_stamps.items()):
        raise LocalDataError('散落文件在检查后变化，修正未启动')
    manager = reviews[0].output.parent if reviews else None
    if extras and manager is None:
        raise LocalDataError('缺少额外文件归档管理根')
    batch = manager/'~archive'/datetime.now().strftime('storage-review-%Y%m%d-%H%M%S-%f') if manager else None
    plans = []
    for path in extras:
        checked_path(path, (manager,))
        target = batch/path.relative_to(manager)
        checked_path(target,(manager,))
        if target.exists(): raise LocalDataError('额外文件归档目标已存在')
        if target.with_name(target.name+'.archive.json').exists():
            raise LocalDataError('额外归档登记已存在')
        plans.append((path,target))
    for path,target in plans:
        target.parent.mkdir(parents=True, exist_ok=True)
        prior_stamp = stamp(path)
        before = await asyncio.to_thread(digest, path)
        if stamp(path) != prior_stamp:
            raise LocalDataError('归档读取期间文件变化，已停止')
        with target.with_name(target.name+'.archive.json').open('x',encoding='utf-8') as stream:
            json.dump({'source':str(path),'archive':str(target),'sha256':before},stream,ensure_ascii=False)
        path.rename(target)
        if await asyncio.to_thread(digest,target) != before:
            raise LocalDataError('归档哈希核验失败，保留现场')
        print(f'[已归档额外文件] {path}', flush=True)
        for review in reviews:
            if path in review.expected:
                mid, _role = review.expected[path]
                review.record.record_result(mid,'failed',detail='已确认归档额外文件，不再记为已下载')
    if plans:
        from .folder_archive import archive_empty_folders
        boundaries = {manager} | {base for review in reviews for base in (review.output,review.staging,review.partials)}
        for original,_target in plans:
            parent = original.parent
            while parent not in boundaries and parent.is_dir() and not any(parent.iterdir()):
                next_parent = parent.parent
                archive_empty_folders(parent,archive_root=batch/'empty-folders',include_root=True)
                parent = next_parent
    failed = 0
    for review,path in missing:
        print(f'[从服务器补齐] {path}',flush=True)
        try:
            await repair_one(page,review,path)
        except Exception as exc:
            failed += 1
            print(f'[补齐失败] {path}：{exc}',flush=True)
    print(f'[修改结束] 缺失 {len(missing)}，额外归档 {len(extras)}，失败 {failed}',flush=True)
    return failed, True
