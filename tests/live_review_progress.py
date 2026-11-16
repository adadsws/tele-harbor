"""在真实大目录只读验证结构进度和有界哈希取消，不冒称整组哈希已完成。"""
import argparse
import asyncio
import contextlib
import json
import time
from datetime import datetime
from pathlib import Path

from live_test_policy import assert_live_test_allowed
from matrix_harness.isolation import protected_state,write_json
from telegg.extra_files import group_roots,inventory
from telegg.group_menu import load_groups
from telegg.local_config import CONFIG_FILE,load_paths
from telegg.local_validation import validate_local_archive
from telegg.review_progress import verify_media,with_progress
from telegg.scan_record import ScanRecord
from telegg.storage_review import expected_files


def audit(number,seconds):
    paths=load_paths();group=load_groups()[number-1]
    record=ScanRecord(paths['scan_records']/f'{group.identity}.jsonl')
    expected=expected_files(record,paths['output'],paths['staging'])
    for _path,(mid,role) in expected.items():
        if role=='media':assert_live_test_allowed(group.identity,mid)
    folders=group_roots(paths['output'],paths['staging'],group.identity)
    _files,stamps=inventory(folders);missing={p for p in expected if p not in stamps}
    name=folders[0].name
    batch=Path(__file__).resolve().parents[1]/'~temp/review-progress'/datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    batch.mkdir(parents=True)
    roots={k:paths[k] for k in ('output','staging','partials','scan_records','chrome_profile')};roots['config']=CONFIG_FILE
    before=protected_state(roots,[CONFIG_FILE,record.path]);write_json(batch/'source-before.json',before)
    snapshots={};phase=['准备',0,len(record.source_messages)];result={}
    def progress(stage,done,total):phase[:]=[stage,done,total]
    async def run():
        started=time.monotonic()
        await with_progress(lambda stop:validate_local_archive(paths['output']/name,paths['staging']/name,
            record,paths['partials']/group.identity,allow_missing=missing,progress=progress,
            snapshots=snapshots,stop=stop),lambda:f'[完整性检查] {phase[0]} {phase[1]}/{phase[2]} 条')
        result['structure_seconds']=round(time.monotonic()-started,3)
        started=time.monotonic()
        try:
            await asyncio.wait_for(verify_media(expected,missing,set(),stamps,paths['partials'],group.identity,
                                               snapshots=snapshots),timeout=seconds)
            result['bounded_hash_cancelled']=False
        except TimeoutError:result['bounded_hash_cancelled']=True
        result['hash_seconds']=round(time.monotonic()-started,3)
    try:
        with (batch/'progress.txt').open('w',encoding='utf-8') as log,contextlib.redirect_stdout(log):asyncio.run(run())
    finally:
        after=protected_state(roots,[CONFIG_FILE,record.path]);write_json(batch/'source-after.json',after)
        result['source_unchanged']=before==after
    lines=(batch/'progress.txt').read_text(encoding='utf-8').splitlines()
    sha_lines=[line for line in lines if 'SHA-256 ' in line and '当前：' in line]
    result.update(media_count=sum(role=='media' for _,role in expected.values()),
                  existing_media_bytes=sum(stamps[p][0] for p,(_,role) in expected.items() if role=='media' and p in stamps),
                  missing_paths=len(missing),hash_progress_lines=len(sha_lines),
                  intermediate_bytes_visible=any('0 B/' not in line for line in sha_lines),
                  server_comparison='not_run_local_phase_only',full_group_hash_completed=not result['bounded_hash_cancelled'])
    result['passed']=(result['source_unchanged'] and len(sha_lines)>=2 and result['intermediate_bytes_visible'])
    write_json(batch/'result.json',result);write_json(batch.parent/'latest.json',{'batch':str(batch)})
    print(json.dumps(result,ensure_ascii=False),flush=True)
    return int(not result['passed'])


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--group-number',type=int,required=True);parser.add_argument('--hash-seconds',type=float,default=3)
    args=parser.parse_args()
    if args.group_number<1 or args.group_number>len(load_groups()) or not 1<=args.hash_seconds<=10:
        parser.error('群序号必须有效，哈希预算为 1–10 秒')
    raise SystemExit(audit(args.group_number,args.hash_seconds))
