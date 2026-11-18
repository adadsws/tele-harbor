"""实际 BAT 全模式实测：18 种进度、生产目录形状、确认、菜单与模式切换。"""
import argparse
import itertools
import json
from datetime import datetime
from pathlib import Path

from live_test_policy import assert_live_test_allowed
from matrix_harness.execution import run_bat
from matrix_harness.history_window import install
from matrix_harness.isolation import copy_case,protected_state,sha,sync_code,write_json
from mode_review.fixtures import business_snapshot,configure,production_layout,snapshot,storage_fault
from mode_review.progress import PROGRESS_CASES,run_progress
from telegg.group_menu import MODE_LABELS,load_groups
from telegg.local_config import CONFIG_FILE,load_paths
from telegg.scan_record import ScanRecord

MODES=('fast','exact','check')
SECTIONS=('progress','storage','switches','menu','multi')


def save(context,result):
    context['results'].append(result)
    write_json(context['batch']/'results.json',context['results'])
    print(json.dumps({k:result[k] for k in ('section','case','mode','expected_behavior_pass','issues')},
                     ensure_ascii=False),flush=True)


def prepare_storage(context,label,kind):
    work=context['batch']/'cases'/label
    copy_case(context['baseline'],work,{})
    roots=production_layout(work)
    fact=storage_fault(kind,work,context['peer'],context['small_mid'],roots)
    return work,roots,fact


def storage_cases():
    cases=[]
    for kind in ('healthy','missing','duplicate','versions'):
        cases.extend((f'{kind}-{mode}',kind,mode,'y') for mode in MODES)
    for kind in ('extras','mixed'):
        cases.append((f'{kind}-fast',kind,'fast','n'))
        for mode in ('exact','check'):
            answers=('n','y') if kind=='extras' else ('n','y','','eof')
            cases.extend((f'{kind}-{mode}-{answer or "blank"}',kind,mode,answer) for answer in answers)
    for kind in ('manifest_missing','manifest_bad','empty_directory','association_missing','association_bad'):
        cases.extend((f'{kind}-{mode}',kind,mode,'n') for mode in ('exact','check'))
    return cases


def run_storage(context,label,kind,mode,answer,*,peers=None,groups=None):
    work,roots,fact=prepare_storage(context,label,kind)
    configure(context['runtime'],work,context['profile'],context['peer'],roots,groups=groups)

    before=snapshot(work,roots);extra_hashes={p:sha(Path(p)) for p in fact['extras']}
    write_json(context['batch']/'logs'/f'{label}.before.json',before)
    data=b'' if answer=='eof' else (answer+'\n\n').encode()
    run=run_bat(context['runtime'],context['batch']/'logs'/f'{label}.txt',
                peers or context['peer'],'exact' if mode=='check' else mode,set(context['hashes']),
                limit=None if mode=='check' else context['info']['limit'],input_bytes=data,
                extra_args=('--check-extra-files',) if mode=='check' else ())
    console=run.pop('_console');prompts=console.count('是否修改以上问题')
    safety=kind in ('manifest_missing','manifest_bad','empty_directory','association_missing','association_bad')
    needs=kind in ('missing','mixed','extras','duplicate')
    approved=mode!='fast' and needs and answer=='y'
    target=Path(fact['target']);issues=[]
    expected_prompts=int(mode!='fast' and needs and not safety)
    if run['exit']!=(1 if safety else 0):issues.append('exit')
    if run['stop']:issues.append('observer_stopped')
    if prompts!=expected_prompts:issues.append('prompt_count')
    network=('[下载] 新资源已开始' in console or '[下载] 续传已开始' in console)
    if network!=(approved and kind in ('missing','mixed')):issues.append('download_boundary')
    # 快速可正常追加完成标记；媒体、辅助文件和额外项都必须原位保留。
    after=snapshot(work,roots)
    if not approved:
        compare=lambda state:business_snapshot(state,work,roots)
        if compare(before)!=compare(after):issues.append('unapproved_business_change')
        if mode=='check' and before!=after:issues.append('check_state_write')
        if safety and before!=after:issues.append('rejection_state_write')
    else:
        if not target.is_file() or sha(target)!=context['hashes'][context['small_mid']]:issues.append('recovery_hash')
        for name,value in extra_hashes.items():
            source=Path(name);archive=list((work/'~archive').rglob(source.name))
            if source.exists() or len(archive)!=1 or sha(archive[0])!=value:issues.append('extra_archive')
            elif not archive[0].with_name(archive[0].name+'.archive.json').is_file():issues.append('archive_mapping')
    if mode=='fast' and needs and '[已下载缺失路径]' in console:issues.append('fast_reviewed')
    if peers is not None or groups is not None:
        if '[群组 2/2]' not in console:issues.append('second_group_not_checked')
        if expected_prompts and console.index('是否修改以上问题')<console.index('[群组 2/2]'):
            issues.append('prompt_before_all_groups')
    return {'section':'multi' if peers is not None or groups is not None else 'storage',
            'case':label,'mode':mode,'run':run,'prompts':prompts,'network_started':network,
            'issues':issues,'expected_behavior_pass':not issues}


def run_menu(context,mode):
    label=f'menu-{mode}';work,roots,_=prepare_storage(context,label,'healthy')
    configure(context['runtime'],work,context['profile'],context['peer'],roots)
    before=snapshot(work,roots)
    toggles={'fast':0,'exact':1,'check':2}[mode]
    answers=('5\n'*toggles+'1\n0\n\n').encode()
    run=run_bat(context['runtime'],context['batch']/'logs'/f'{label}.txt',None,'fast',
                set(context['hashes']),limit=None if mode=='check' else context['info']['limit'],input_bytes=answers)
    console=run.pop('_console');issues=[]
    task=[line for line in console.splitlines() if '[任务]' in line]
    wanted=MODE_LABELS['extras' if mode=='check' else mode]
    if len(task)!=1 or wanted not in task[0]:issues.append('menu_wrong_mode')
    if run['exit']!=0 or run['stop']:issues.append('menu_exit')
    if console.count('是否修改以上问题'):issues.append('healthy_prompted')
    if '[下载] 新资源已开始' in console:issues.append('healthy_downloaded')
    if mode=='check' and snapshot(work,roots)!=before:issues.append('check_state_write')
    return {'section':'menu','case':label,'mode':mode,'run':run,'issues':issues,'expected_behavior_pass':not issues}


def run_switches(context):
    complete=next(c for c in PROGRESS_CASES if c.id=='D01')
    pending=next(c for c in PROGRESS_CASES if c.id=='D04')
    for first,second in itertools.permutations(MODES,2):
        label=f'switch-{first}-{second}'
        result,work=run_progress(context,pending,first,label=label+'-0')
        result.update(section='switches',case=label+'-0');save(context,result)
        second_case=pending if first=='check' else complete
        result,_=run_progress(context,second_case,second,label=label+'-1',work=work)
        result.update(section='switches',case=label+'-1');save(context,result)


def execute(args):
    project=Path(__file__).resolve().parents[1];suite=args.suite.resolve()
    parent=project/'~temp/live-matrix'
    if not suite.is_relative_to(parent) or suite==parent:raise ValueError('suite 必须在 live-matrix 单批次下')
    baseline=suite/'baseline';record=ScanRecord(next((baseline/'scans').glob('*.jsonl')))
    refs={mid:Path(row['detail']) for mid,row in record.results.items() if row['status'] in ('downloaded','skipped','relocated')}
    for mid in refs:assert_live_test_allowed(record.path.stem,mid)
    sample=json.loads((suite/'sample.json').read_text(encoding='utf-8'))
    if sample.get('kind')!='standalone':raise ValueError('需要完整历史无 group 基线')
    batch=project/'~temp/all-modes'/datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    batch.mkdir(parents=True);(batch/'logs').mkdir()
    context={'batch':batch,'runtime':suite/'runtime','profile':suite/'profile','baseline':baseline,
             'peer':record.path.stem,'hashes':{mid:sha(p) for mid,p in refs.items()},'results':[],
             'target_mid':sample['target'],'target_source':refs[sample['target']],
             'small_mid':min(refs,key=lambda m:refs[m].stat().st_size),
             'info':{'peer':record.path.stem,'upper':sample['upper'],'limit':sample['limit']},
             'layers':{mid:p.parent.name for mid,p in refs.items()},'contexts':record.standalone_contexts}
    paths=load_paths();roots={k:paths[k] for k in ('output','staging','partials','scan_records','chrome_profile')}
    roots['config']=CONFIG_FILE
    hash_paths=[Path(p) for p in json.loads((suite/'source-before.json').read_text(encoding='utf-8'))['hashes']]
    before=protected_state(roots,hash_paths);write_json(batch/'source-before.json',before)
    sync_code(project,context['runtime']);install(context['runtime'],context['info'])
    production=[*project.joinpath('src').rglob('*.py'),project/'download.bat',project/'requirements.txt']
    code={p.relative_to(project).as_posix():sha(p) for p in production};write_json(batch/'code-sha256.json',code)
    harness=[Path(__file__),*project.joinpath('tests/mode_review').glob('*.py'),
             *project.joinpath('tests/matrix_harness').glob('*.py'),project/'tests/live_test_policy.py']
    harness_code={p.relative_to(project).as_posix():sha(p) for p in harness}
    write_json(batch/'harness-sha256.json',harness_code)
    print(json.dumps({'batch':str(batch.relative_to(project)),'sample_files':len(refs),'sections':args.sections}),flush=True)
    write_json(project/'~temp/all-modes/latest.json',{'batch':str(batch)})
    try:
        if 'progress' in args.sections:
            for case in PROGRESS_CASES:
                if args.case and case.id not in args.case:continue
                for mode in args.modes:save(context,run_progress(context,case,mode)[0])
        if 'storage' in args.sections:
            for label,kind,mode,answer in storage_cases():
                if mode in args.modes and (not args.case or label in args.case):
                    save(context,run_storage(context,label,kind,mode,answer))
        if 'switches' in args.sections:run_switches(context)
        if 'menu' in args.sections:
            for mode in args.modes:save(context,run_menu(context,mode))
        if 'multi' in args.sections:
            others=[g.identity for g in load_groups() if g.identity!=context['peer']]
            if not others:raise ValueError('没有第二个可用群组，多群未验证')
            other=others[0]
            save(context,run_storage(context,'multi-explicit-decline','mixed','check','n',
                                    peers=[context['peer'],other],groups=[context['peer'],other]))
            save(context,run_storage(context,'all-removed-confirm','mixed','check','y',
                                    peers=other,groups=[other]))
    finally:
        after=protected_state(roots,hash_paths);write_json(batch/'source-after.json',after)
        mismatches=[name for name,value in code.items() if sha(project/name)!=value or sha(context['runtime']/name)!=value]
        harness_changed=[name for name,value in harness_code.items() if sha(project/name)!=value]
        protection={'original_unchanged':before==after,'code_unchanged':not mismatches,
                    'harness_unchanged':not harness_changed}
        write_json(batch/'source-protection.json',protection)
        print(json.dumps({'source_protection':protection}),flush=True)
    failures=sum(not r['expected_behavior_pass'] for r in context['results'])
    summary={'cases':len(context['results']),'passed':len(context['results'])-failures,'failed':failures,**protection}
    write_json(batch/'summary.json',summary);print(json.dumps(summary),flush=True)
    return int(bool(failures) or not all(protection.values()))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--suite',type=Path,required=True)
    parser.add_argument('--sections',nargs='+',choices=SECTIONS,default=list(SECTIONS))
    parser.add_argument('--modes',nargs='+',choices=MODES,default=list(MODES))
    parser.add_argument('--case',nargs='+',default=[])
    raise SystemExit(execute(parser.parse_args()))
