"""只读核验的持续进度与可取消分块哈希；不改变完成记录或验收基线。"""
from __future__ import annotations

import asyncio
import hashlib
import threading
import time

from .errors import LocalDataError
from .progress import ProgressLine, remaining_label, size_label
from .verification_manifest import read_snapshot


async def with_progress(work, status, *, interval=1.0):
    """线程执行文件 I/O，主循环持续显示状态并传递取消。"""
    stop=threading.Event()
    task=asyncio.create_task(asyncio.to_thread(work,stop))
    display=ProgressLine();display.write(status())
    try:
        while not task.done():
            await asyncio.wait((task,),timeout=interval)
            display.write(status())
        return task.result()
    except asyncio.CancelledError:
        stop.set()
        await asyncio.shield(task)
        raise
    finally:
        display.finish()


def file_stamp(path):
    st=path.stat()
    return st.st_size,st.st_mtime_ns,st.st_dev,st.st_ino


def hash_chunks(path,stop,advance,*,chunk_size=4*1024*1024):
    digest=hashlib.sha256()
    with path.open('rb') as stream:
        while not stop.is_set():
            data=stream.read(chunk_size)
            if not data:return digest.hexdigest()
            digest.update(data);advance(len(data))
    return None


async def verify_media(expected,missing,extras,stamps,partials,peer,*,previous=None,snapshots=None):
    items=[(path,mid) for path,(mid,role) in expected.items()
           if role=='media' and path not in missing and path not in extras]
    total=sum(stamps[path][0] for path,_mid in items)
    state={'done':0,'read':0,'covered':0,'cached':0,'path':'准备读取'}
    started=time.monotonic();cache={}
    def status():
        elapsed=max(time.monotonic()-started,.001)
        speed=state['read']/elapsed
        remaining='已完成' if state['done']==len(items) else remaining_label(state['covered'],total,speed)
        return (f'[完整性检查] SHA-256 {state["done"]}/{len(items)} 个文件 · '
                f'{size_label(state["covered"])}/{size_label(total)} · '
                f'{size_label(int(speed))}/s · {remaining} · '
                f'本轮免重复读取 {state["cached"]} 个 · 当前：{state["path"]}')
    def work(stop):
        for path,mid in items:
            if stop.is_set():return cache
            state['path']=str(path)
            baseline=(snapshots or {}).get(mid)
            if baseline is None:baseline=read_snapshot(partials/peer/f'{mid}.verified.jsonl',strict=True,required=True)
            key=(stamps[path],baseline['sha256'])
            if file_stamp(path)!=stamps[path]:raise LocalDataError(f'校验前文件变化，保留现场：{path}')
            if previous is not None and previous.hash_cache.get(path)==key:
                state['cached']+=1;state['covered']+=stamps[path][0]
            else:
                def advance(count):
                    state['read']+=count;state['covered']+=count
                actual=hash_chunks(path,stop,advance)
                if actual is None:return cache
                if actual!=baseline['sha256']:raise LocalDataError(f'已有文件 SHA-256 不符，未自动修改：{path}')
            if file_stamp(path)!=stamps[path]:raise LocalDataError(f'校验期间文件变化，保留现场：{path}')
            cache[path]=key;state['done']+=1
        state['path']='校验完成'
        return cache
    print(f'[完整性检查] 将核对 {len(items)} 个已有媒体，共 {size_label(total)}；大文件会持续显示读取进度',flush=True)
    return await with_progress(work,status)
