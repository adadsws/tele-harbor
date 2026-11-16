"""只调整隔离副本的存储布局与故障；不读取媒体正文，不删除文件。"""
import json
import shutil
from pathlib import Path

from matrix_harness.fixtures import hold
from matrix_harness.isolation import files, sha, write_json
from telegg.scan_record import ScanRecord


def production_layout(work):
    roots={'output':work/'downloaded','staging':work/'downloading/groups',
           'partials':work/'downloading/partials'}
    old_roots={name:work/name for name in roots}
    for name,target in roots.items():
        source=old_roots[name]
        target.parent.mkdir(parents=True,exist_ok=True)
        if source.exists():source.rename(target)
    for path in work.rglob('*.jsonl'):
        rows=[json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
        for row in rows:
            field='detail' if row.get('kind')=='result' else 'path'
            value=row.get(field)
            if not isinstance(value,str) or not Path(value).is_absolute():continue
            for name,source in old_roots.items():
                if Path(value).is_relative_to(source):
                    row[field]=str(roots[name]/Path(value).relative_to(source));break
        path.write_text(''.join(json.dumps(row,ensure_ascii=False)+'\n' for row in rows),encoding='utf-8')
    return roots


def configure(runtime,work,profile,peer,roots,*,groups=None):
    values={k:str(p) for k,p in roots.items()}
    values.update(scan_records=str(work/'scans'),test_artifacts=str(work/'tests'),
                  chrome_profile=str(profile),groups=groups or [peer])
    write_json(runtime/'config/config.local.json',values)


def snapshot(work,roots):
    """检查模式包括扫描记录、内部 JSON 和验收日志均不可写入。"""
    return {p.relative_to(work).as_posix():sha(p)
            for base in (*roots.values(),work/'scans') for p in files(base)}


def business_snapshot(state,work,roots):
    """下载模式可追加扫描和指定分片区验收缓存；其他原有文件仍须保持。"""
    partials=roots['partials'].relative_to(work)
    return {p:value for p,value in state.items() if Path(p).parts[0]!='scans'
            and not (Path(p).is_relative_to(partials) and p.endswith('.verified.jsonl'))}


def storage_fault(kind,work,peer,target_mid,roots):
    record=ScanRecord(work/'scans'/f'{peer}.jsonl')
    media=Path(record.results[target_mid]['detail'])
    facts={'target':str(media),'extras':[]}
    if kind in ('missing','mixed'):
        hold(work,media)
    if kind=='mixed':
        # 错位副本、整个 downloading 根、groups/partials 根和群内未知文件。
        moved=media.parent/'moved'/media.name;moved.parent.mkdir(parents=True)
        shutil.copy2(work/'held'/media.relative_to(work),moved)
        facts['extras'].append(str(moved))
    if kind in ('extras','mixed'):
        for i,base in enumerate((roots['output'],roots['staging'].parent,roots['staging'],roots['partials'],media.parent)):
            path=base/f'neutral-extra-{i}.bin';path.parent.mkdir(parents=True,exist_ok=True)
            path.write_bytes(f'neutral-{i}'.encode());facts['extras'].append(str(path))
    if kind=='duplicate':
        path=media.with_name(media.stem+'.duplicate'+media.suffix)
        shutil.copy2(media,path);facts['extras'].append(str(path))
    if kind=='versions':
        older=media.with_name(media.stem+'.v1'+media.suffix);media.rename(older)
        latest=media.with_name(media.stem+'.v2'+media.suffix);shutil.copy2(older,latest)
        record.record_result(target_mid,'downloaded',detail=str(latest),naming_revision=3)
        from telegg.verification_manifest import verified_digest
        journal=roots['partials']/peer/f'{target_mid}.verified.jsonl'
        rows=[json.loads(line) for line in journal.read_text(encoding='utf-8').splitlines()]
        verified_digest(latest,rows[-1]['identity'],journal,sha)
        facts['target']=str(latest)
    if kind in ('manifest_missing','manifest_bad','empty_directory','association_missing','association_bad'):
        manifest=next(roots['output'].rglob('段内清单.json'))
        if kind=='manifest_missing':hold(work,manifest)
        elif kind=='manifest_bad':manifest.write_bytes(b'{broken')
        elif kind=='empty_directory':(manifest.parent/'neutral-empty').mkdir()
        else:
            association=next(roots['output'].rglob('关联清单.json'))
            if kind=='association_missing':hold(work,association)
            else:association.write_bytes(b'{broken')
    return facts
