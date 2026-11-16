"""通过实际 BAT 验证重复关联清单收尾；复用已验收历史基线，仅写独立副本。"""
import argparse
import json
import shutil
from datetime import datetime
from pathlib import Path

from live_test_policy import assert_live_test_allowed
from matrix_harness.execution import run_bat
from matrix_harness.history_window import configure_window, install
from matrix_harness.isolation import configure, copy_case, files, protected_state, sha, sync_code, write_json
from matrix_harness.validation import evidence
from telegg.local_config import CONFIG_FILE, load_paths
from telegg.scan_record import ScanRecord


def audit(suite):
    project=Path(__file__).resolve().parents[1]
    sample=json.loads((suite/'sample.json').read_text(encoding='utf-8'))
    if sample.get('kind')!='standalone':raise ValueError('需要已验收的完整无 group 头像段基线')
    baseline=suite/'baseline';record=ScanRecord(next((baseline/'scans').glob('*.jsonl')))
    peer=record.path.stem
    references={mid:Path(row['detail']) for mid,row in record.results.items()
                if row['status'] in ('downloaded','skipped','relocated')}
    for mid in references:assert_live_test_allowed(peer,mid)
    reference_hashes={mid:sha(path) for mid,path in references.items()}
    paths=load_paths();roots={k:paths[k] for k in ('output','staging','partials','scan_records','chrome_profile')}
    roots['config']=CONFIG_FILE
    hashes=[Path(p) for p in json.loads((suite/'source-before.json').read_text(encoding='utf-8'))['hashes']]
    before=protected_state(roots,hashes)
    batch=project/'~temp/association-closeout'/datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    batch.mkdir(parents=True);write_json(batch/'source-before.json',before)
    runtime,profile=suite/'runtime',suite/'profile';sync_code(project,runtime)
    info={'peer':peer,'upper':sample['upper']};install(runtime,info)
    results=[]
    try:
        for kind in ('duplicate','conflict','unknown_file'):
            for mode in ('fast','exact'):
                work=batch/f'{kind}-{mode}';copy_case(baseline,work,{})
                final=next((work/'output').rglob('关联清单.json'))
                pending=work/'staging'/final.relative_to(work/'output')
                pending.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(final,pending)
                if kind=='conflict':pending.write_bytes(b'{"replies":[]}')
                if kind=='unknown_file':(pending.parent/'user-fixture.bin').write_bytes(b'keep')
                configure(runtime,work,profile,peer);configure_window(work,info)
                business_before=evidence(work);scan=next((work/'scans').glob('*.jsonl'));scan_hash=sha(scan)
                final_hash=sha(final)
                run=run_bat(runtime,batch/f'{kind}-{mode}.txt',peer,mode,set(references),limit=sample['limit'])
                console=run.pop('_console');after=ScanRecord(scan)
                intact=sum(Path(after.results.get(mid,{}).get('detail','')).is_file()
                           and sha(Path(after.results[mid]['detail']))==digest for mid,digest in reference_hashes.items())
                archived=list((work/'~local/~archive').rglob('关联清单.json'))
                source_gone=not pending.parent.exists()
                media_ok=intact==len(references) and sha(final)==final_hash
                if kind=='duplicate':
                    passed=(run['exit']==0 and source_gone and len(archived)==1 and media_ok
                            and sha(archived[0])==final_hash)
                    repeat=run_bat(runtime,batch/f'{kind}-{mode}-repeat.txt',peer,mode,set(references),limit=sample['limit'])
                    repeat.pop('_console')
                    passed=passed and repeat['exit']==0 and len(list((work/'~local/~archive').rglob('关联清单.json')))==1
                elif kind=='conflict':
                    passed=(run['exit']==1 and '[本地数据错误]' in console
                            and business_before==evidence(work) and not archived and media_ok)
                    if mode=='exact':passed=passed and sha(scan)==scan_hash
                else:
                    passed=run['exit']==0 and business_before==evidence(work) and not archived and media_ok
                value={'fixture':kind,'mode':mode,'run':run,'intact_files':intact,'reference_files':len(references),
                       'archived_manifests':len(archived),'source_parent_removed':source_gone,'passed':passed}
                results.append(value);write_json(batch/'results.json',results)
                print(json.dumps(value,ensure_ascii=False),flush=True)
    finally:
        after=protected_state(roots,hashes);write_json(batch/'source-after.json',after)
        protection={'unchanged':before==after};write_json(batch/'source-protection.json',protection)
        write_json(project/'~temp/association-closeout/latest.json',{'batch':str(batch)})
        print(json.dumps({'source_protection':protection}),flush=True)
    return 0 if all(r['passed'] for r in results) and protection['unchanged'] else 1


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--suite',type=Path,required=True)
    args=parser.parse_args();project=Path(__file__).resolve().parents[1];suite=args.suite.resolve()
    if not suite.is_relative_to((project/'~temp/live-matrix').resolve()):parser.error('suite 必须在项目 live-matrix 下')
    from live_storage_review import audit as current_audit
    raise SystemExit(current_audit(suite))
