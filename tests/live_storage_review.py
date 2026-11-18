"""真实 BAT 验收三模式与结束确认；人工进度只在隔离副本构造，媒体来自真实服务器。"""
import argparse
import json
import shutil
from datetime import datetime
from pathlib import Path

from live_test_policy import assert_live_test_allowed
from matrix_harness.execution import run_bat
from matrix_harness.fixtures import clear_completion, hold
from matrix_harness.history_window import install
from matrix_harness.isolation import configure, copy_case, files, protected_state, sha, sync_code, write_json
from matrix_harness.validation import evidence
from telegg.local_config import CONFIG_FILE, load_paths
from telegg.scan_record import ScanRecord


def audit(suite):
    project=Path(__file__).resolve().parents[1];baseline=suite/'baseline'
    sample=json.loads((suite/'sample.json').read_text(encoding='utf-8'))
    if sample.get('kind')!='standalone':raise ValueError('需要已验收的完整历史无 group 基线')
    record=ScanRecord(next((baseline/'scans').glob('*.jsonl')));peer=record.path.stem
    refs={mid:Path(row['detail']) for mid,row in record.results.items() if row['status'] in ('downloaded','skipped','relocated')}
    for mid in refs:assert_live_test_allowed(peer,mid)
    target_mid=min(refs,key=lambda mid:refs[mid].stat().st_size)
    hashes={mid:sha(path) for mid,path in refs.items()}
    paths=load_paths();roots={k:paths[k] for k in ('output','staging','partials','scan_records','chrome_profile')};roots['config']=CONFIG_FILE
    hash_paths=[Path(p) for p in json.loads((suite/'source-before.json').read_text(encoding='utf-8'))['hashes']]
    before=protected_state(roots,hash_paths)
    batch=project/'~temp/storage-review'/datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    batch.mkdir(parents=True);write_json(batch/'source-before.json',before)
    runtime,profile=suite/'runtime',suite/'profile';sync_code(project,runtime)
    info={'peer':peer,'upper':sample['upper']};install(runtime,info)
    cases=[('fast-mixed','fast','mixed',b'\n',False),
           ('exact-no','exact','mixed',b'n\n\n',False),('exact-yes','exact','mixed',b'y\n\n',True),
           ('check-no','check','mixed',b'n\n\n',False),('check-yes','check','mixed',b'y\n\n',True),
           ('check-eof','check','mixed',b'',False),('exact-new','exact','new',b'\n',False),
           ('check-new','check','new',b'\n',False),('fast-new','fast','new',b'\n',False)]
    results=[]
    try:
        for label,mode,fixture,answer,modify in cases:
            work=batch/label;copy_case(baseline,work,{})
            scan=work/'scans'/record.path.name;r=ScanRecord(scan);target=Path(r.results[target_mid]['detail'])
            if fixture=='mixed':
                moved=target.parent/'user-moved'/target.name;moved.parent.mkdir();target.rename(moved)
                loose=work/'output/loose-fixture.bin';loose.write_bytes(b'neutral-extra')
                association=next((work/'output').rglob('关联清单.json'))
                staged=work/'staging'/association.relative_to(work/'output')
                staged.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(association,staged)
            else:
                hold(work,target);clear_completion(scan,target_mid)
                for p in (work/'partials'/peer).glob(f'{target_mid}*.jsonl'):hold(work,p)
            configure(runtime,work,profile,peer)
            old=evidence(work);record_hash=sha(scan)
            run=run_bat(runtime,batch/f'{label}.txt',peer,'exact' if mode=='check' else mode,set(refs),
                        limit=None if mode=='check' else sample['limit'],input_bytes=answer,
                        extra_args=('--check-extra-files',) if mode=='check' else ())
            console=run.pop('_console');after=ScanRecord(scan)
            intact=sum(Path(after.results.get(mid,{}).get('detail','')).is_file()
                       and sha(Path(after.results[mid]['detail']))==value for mid,value in hashes.items())
            prompts=console.count('是否修改以上问题')
            network='[下载] 新资源已开始' in console or '[下载] 续传已开始' in console
            if fixture=='mixed':
                expected_prompts=int(mode!='fast')
                if modify:
                    archived=list((work/'~archive').rglob(target.name))
                    passed=(intact==len(refs) and target.is_file() and not moved.exists() and not loose.exists()
                            and len(archived)==1 and sha(archived[0])==hashes[target_mid] and network)
                else:
                    passed=(not target.exists() and moved.is_file() and loose.is_file()
                            and evidence(work)==old and not network)
                if mode=='check' and not modify:passed=passed and sha(scan)==record_hash
            else:
                expected_prompts=0
                passed=(not target.exists() and not network and sha(scan)==record_hash) if mode=='check' else (intact==len(refs) and network)
            passed=passed and run['exit']==0 and run['stop'] is None and prompts==expected_prompts
            value={'case':label,'mode':mode,'fixture':fixture,'prompts':prompts,'network_started':network,
                   'intact_files':intact,'expected_files':len(refs),'run':run,'passed':passed}
            results.append(value);write_json(batch/'results.json',results);print(json.dumps(value,ensure_ascii=False),flush=True)
    finally:
        after=protected_state(roots,hash_paths);write_json(batch/'source-after.json',after)
        protection={'unchanged':before==after};write_json(batch/'source-protection.json',protection)
        write_json(project/'~temp/storage-review/latest.json',{'batch':str(batch)})
        print(json.dumps({'source_protection':protection}),flush=True)
    return 0 if all(r['passed'] for r in results) and protection['unchanged'] else 1


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--suite',type=Path,required=True)
    args=parser.parse_args();root=Path(__file__).resolve().parents[1];suite=args.suite.resolve()
    if not suite.is_relative_to((root/'~temp/live-matrix').resolve()):parser.error('suite 必须位于项目 live-matrix 下')
    raise SystemExit(audit(suite))
