"""在独立副本中构造进度；不删除文件，需移出的副本留在 held 中。"""
import json
import os
import shutil
from pathlib import Path

from telegg.resumable import CHUNK_SIZE
from telegg.scan_record import ScanRecord


def rows(path):
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]


def save_rows(path, values):
    path.write_text(''.join(json.dumps(v, ensure_ascii=False) + '\n' for v in values), encoding='utf-8')


def hold(work, path):
    if not path.exists():
        return
    target = work / 'held' / path.relative_to(work)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise RuntimeError('副本保留目标已存在')
    path.rename(target)


def clear_completion(record, mid):
    values = [v for v in rows(record) if v['kind'] not in ('group_verified', 'message_verified')
              and not (v['kind'] == 'result' and v['mid'] == mid)]
    save_rows(record, values)


def flip(path, offset=0):
    stamp = path.stat()
    with path.open('r+b') as stream:
        stream.seek(offset)
        byte = stream.read(1)
        if not byte:
            raise ValueError('模拟损坏偏移超出副本')
        stream.seek(offset)
        stream.write(bytes([byte[0] ^ 1]))
    os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))


def prefix_copy(source, target, length):
    target.parent.mkdir(parents=True, exist_ok=True)
    with source.open('rb') as old, target.open('xb') as new:
        remaining = length
        while remaining:
            block = old.read(min(1024 * 1024, remaining))
            if not block:
                raise ValueError('原样本不足以构造副本前缀')
            new.write(block)
            remaining -= len(block)


def build(case, work, peer, mid, original):
    """返回模拟器事实，后续据此验收真实应用的行为。"""
    record = work / 'scans' / f'{peer}.jsonl'
    r = ScanRecord(record)
    target = Path(r.results[mid]['detail'])
    partials = work / 'partials' / peer
    journal = partials / f'{mid}.jsonl'
    snapshot = partials / f'{mid}.verified.jsonl'
    payload = partials / f'{mid}.part'
    for path in (target, journal, snapshot, payload):
        if not path.resolve().is_relative_to(work.resolve()):
            raise RuntimeError('场景写入目标超出隔离副本')
    fact = {'target': str(target), 'mid': mid, 'trusted_offset': 0}
    kind = case.fixture
    if kind == 'complete':
        return fact
    if kind == 'final_corrupt':
        flip(target)
        return fact
    if kind == 'final_truncated':
        length = target.stat().st_size - 9
        hold(work, target)
        prefix_copy(original, target, length)
        return fact
    if kind == 'wrong_path':
        wrong = target.parent.parent / 'wrong-segment' / target.name
        wrong.parent.mkdir(parents=True, exist_ok=True)
        target.rename(wrong)
        fact['misplaced'] = str(wrong)
        return fact
    if kind == 'final_missing':
        hold(work, target)
        return fact
    clear_completion(record, mid)
    if kind == 'result_missing':
        return fact
    log = rows(journal)
    hold(work, target)
    hold(work, snapshot)
    if kind == 'partial_album':
        hold(work, journal)
        return fact
    count = 2 if kind == 'record_ahead' else 1
    length = CHUNK_SIZE
    if kind in ('data_ahead', 'tail_conflict'):
        length += CHUNK_SIZE // 2
    if kind == 'full_partial':
        length = original.stat().st_size
        count = len(log) - 1
    prefix_copy(original, payload, length)
    save_rows(journal, log[:count + 1])
    fact['trusted_offset'] = sum(v.get('length', 0) for v in log[1:count + 1])
    if kind == 'orphan_payload':
        hold(work, journal)
    elif kind == 'orphan_journal':
        hold(work, payload)
    elif kind == 'identity_changed':
        altered = rows(journal)
        altered[0]['identity']['id'] += '0'
        save_rows(journal, altered)
    elif kind == 'committed_corrupt':
        flip(payload)
    elif kind == 'tail_conflict':
        flip(payload, CHUNK_SIZE + 13)
    elif kind == 'torn_journal':
        with journal.open('ab') as stream:
            stream.write(b'{"kind":"chunk"')
    return fact
