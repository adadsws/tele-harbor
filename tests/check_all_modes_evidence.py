"""独立复读实际 BAT 证据：提交分片摘要、归档映射和安全拒绝现场。"""
import argparse
import hashlib
import json
from pathlib import Path


def check_journal(media,journal,mid):
    rows=[json.loads(line) for line in journal.read_text(encoding='utf-8').splitlines()]
    header=rows[0]
    if header['kind']!='identity' or str(header['identity']['mid'])!=mid:
        return False
    position=0;whole=hashlib.sha256()
    with media.open('rb') as stream:
        for row in rows[1:]:
            data=stream.read(row['length'])
            if (row['kind']!='chunk' or row['offset']!=position or len(data)!=row['length']
                    or hashlib.sha256(data).hexdigest()!=row['sha256']):return False
            position+=len(data);whole.update(data)
        if stream.read(1):return False
    return position==header['size']==media.stat().st_size


def audit(batch,expected,supplements=()):
    batches=[batch,*supplements];latest={};executions=0
    baseline_code=json.loads((batch/'code-sha256.json').read_text(encoding='utf-8'))
    for source in batches:
        rows=json.loads((source/'results.json').read_text(encoding='utf-8'));executions+=len(rows)
        for row in rows:latest[(row['section'],row['case'],row['mode'])]=(source,row)
    checked_media=archive_count=0;issues=[]
    if len(latest)!=expected:issues.append('case_count')
    if any(not row['expected_behavior_pass'] for _,row in latest.values()):issues.append('case_failure')
    for source,row in latest.values():
        work=source/'cases'/row['case']
        if row['section']=='progress':work=source/'cases'/f'{row["case"]}-{row["mode"]}'
        # 连续切换的第二步复用第一步目录，逐文件复读最终状态。
        if row['section']=='switches' and row['case'].endswith('-1'):
            work=source/'cases'/(row['case'][:-1]+'0')
        if row.get('expectation')=='success':
            for record in (work/'scans').glob('*.jsonl'):
                completed={}
                for line in record.read_text(encoding='utf-8').splitlines():
                    value=json.loads(line)
                    if value.get('kind')=='result':completed[value['mid']]=value
                for mid,value in completed.items():
                    if value['status'] not in ('downloaded','skipped','relocated'):continue
                    media=Path(value['detail']);journal=work/'partials'/record.stem/f'{mid}.jsonl'
                    if not media.is_file() or not journal.is_file() or not check_journal(media,journal,mid):
                        issues.append(f'chunk_evidence:{row["case"]}:{row["mode"]}')
                    checked_media+=1
            if row.get('case') in ('D04','D05','D12') and row['mode']!='check' and not row.get('resume_displayed'):
                issues.append(f'expected_resume:{row["case"]}:{row["mode"]}')
        if row['case'].startswith('empty_directory-'):
            if len(list((work/'downloaded').rglob('neutral-empty')))!=1:issues.append('empty_rejection_moved_directory')
    for source in batches:
        if json.loads((source/'code-sha256.json').read_text(encoding='utf-8'))!=baseline_code:
            issues.append('production_versions_differ')
        for sidecar in source.joinpath('cases').rglob('*.archive.json'):
            info=json.loads(sidecar.read_text(encoding='utf-8'))
            target=Path(info['archive']);original=Path(info['source'])
            if (not target.is_relative_to(source/'cases') or not original.is_relative_to(source/'cases')
                    or target!=sidecar.with_name(sidecar.name.removesuffix('.archive.json'))
                    or not target.is_file()):
                issues.append('archive_mapping');continue
            case_root=source/'cases'/target.relative_to(source/'cases').parts[0]
            relative=target.relative_to(case_root/'~archive')
            if Path(*relative.parts[1:])!=original.relative_to(case_root):issues.append('archive_relative_path')
            with target.open('rb') as stream:digest=hashlib.file_digest(stream,'sha256').hexdigest()
            if digest!=info['sha256']:issues.append('archive_sha256')
            archive_count+=1
        protection=json.loads((source/'source-protection.json').read_text(encoding='utf-8'))
        if not all(protection.values()):issues.append('source_or_code_changed')
    original=json.loads((batch/'source-before.json').read_text(encoding='utf-8'))
    for source in batches:
        if any(json.loads((source/name).read_text(encoding='utf-8'))!=original
               for name in ('source-before.json','source-after.json')):issues.append('original_changed_between_batches')
    mode_counts={mode:sum(row['mode']==mode for _,row in latest.values()) for mode in ('fast','exact','check')}
    result={'case_count':len(latest),'bat_executions':executions,'chunk_verified_media':checked_media,'archive_verified_files':archive_count,
            'mode_counts':mode_counts,'evidence_reader_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'issues':issues,'passed':not issues}
    (batch/'evidence-audit.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False))
    return int(bool(issues))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--batch',type=Path,required=True);parser.add_argument('--expected',type=int,default=107)
    parser.add_argument('--supplement',type=Path,nargs='*',default=[],help='保留首次失败，以后续独立重跑结果补齐对应组合')
    args=parser.parse_args();root=Path(__file__).resolve().parents[1]/'~temp/all-modes'
    batch=args.batch.resolve()
    supplements=[p.resolve() for p in args.supplement]
    if any(not p.is_relative_to(root) or p==root for p in (batch,*supplements)):
        parser.error('batch 必须在项目 all-modes 单批次下')
    raise SystemExit(audit(batch,args.expected,supplements))
