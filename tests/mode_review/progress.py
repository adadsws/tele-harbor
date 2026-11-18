"""三模式进度预期：检查模式不声称验收未完成分片的字节完整性。"""
from pathlib import Path
from matrix_harness.catalog import CASES
from matrix_harness.fixtures import build
from matrix_harness.isolation import configure,copy_case,sha
from matrix_harness.execution import run_bat
from matrix_harness.validation import evidence,validate
from telegg.scan_record import ScanRecord

PROGRESS_CASES=[c for c in CASES if c.automated and c.fixture!='switch']
CHECK_PROMPTS={'D07':1,'D09':1,'L00':1,'L03':1}
CHECK_REJECT={'L01','L02'}


def run_progress(context,case,mode,label=None,work=None):
    work=work or context['batch']/'cases'/(label or f'{case.id}-{mode}')
    if not work.exists():
        work.mkdir(parents=True)
        if case.fixture!='fresh':
            copy_case(context['baseline'],work,{})
            build(case,work,context['peer'],context['target_mid'],context['target_source'])
    runtime=context['runtime'];peer=context['peer']
    configure(runtime,work,context['profile'],peer)

    before=evidence(work);scan=work/'scans'/f'{peer}.jsonl'
    old_scan=sha(scan) if scan.is_file() else None
    snapshots={str(p):sha(p) for p in (work/'partials').rglob('*.verified.jsonl')}
    run=run_bat(runtime,context['batch']/'logs'/f'{label or case.id+"-"+mode}.txt',peer,
                'exact' if mode=='check' else mode,set(context['hashes']),
                limit=None if mode=='check' else context['info']['limit'],
                extra_args=('--check-extra-files',) if mode=='check' else ())
    console=run['_console'];prompts=console.count('是否修改以上问题')
    network=('[下载] 新资源已开始' in console or '[下载] 续传已开始' in console)
    if mode!='check':
        result=validate(case,mode,work,peer,context['hashes'],run,before,
                        layers=context['layers'],contexts=context['contexts'])
        if mode=='fast' and prompts:
            result['issues'].append('fast_prompted');result['expected_behavior_pass']=False
    else:
        run.pop('_console')
        expected_exit=1 if case.id in CHECK_REJECT else 0
        new_scan=sha(scan) if scan.is_file() else None
        issues=[]
        if run['exit']!=expected_exit:issues.append('check_exit')
        if run['stop']:issues.append('observer_stopped')
        if before!=evidence(work) or old_scan!=new_scan:issues.append('check_wrote_data')
        if any(not Path(p).is_file() or sha(Path(p))!=v
               for p,v in snapshots.items()):issues.append('check_wrote_verification')
        if network:issues.append('check_started_download')
        if prompts!=CHECK_PROMPTS.get(case.id,0):issues.append('check_prompt_count')
        result={'case':case.id,'mode':mode,'expectation':'reject' if expected_exit else 'readonly',
                'run':run,'issues':issues,'expected_behavior_pass':not issues}
    result.update(section='progress',prompts=prompts,network_started=network)
    return result,work
