"""复制运行环境，记录源文件保护证据；所有写入均在独立测试根内。"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def files(root):
    if root.is_file():
        yield root
    elif root.is_dir():
        yield from (p for p in root.rglob('*') if p.is_file())


def protected_state(roots, hash_paths):
    state = {}
    for label, root in roots.items():
        for path in files(root):
            stat = path.stat()
            relative = str(path.relative_to(root)) if root.is_dir() else '.'
            state[label + '/' + relative] = (stat.st_size, stat.st_mtime_ns, stat.st_ino, stat.st_dev)
    return {'files': state, 'hashes': {str(p): sha(p) for p in hash_paths}}


def copy_profile(source, target):
    """登录资料只复制到新目录；不启动或修改原 profile。"""
    excluded = {'Cache', 'Code Cache', 'GPUCache', 'ShaderCache', 'GrShaderCache',
                'DawnCache', 'Crashpad', 'BrowserMetrics', 'component_crx_cache'}
    for current, dirs, names in os.walk(source):
        dirs[:] = [d for d in dirs if d not in excluded]
        for name in names:
            old = Path(current) / name
            if old.is_symlink() or name in ('SingletonLock', 'SingletonCookie', 'SingletonSocket', 'lockfile'):
                continue
            new = target / old.relative_to(source)
            new.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(old, new)


def sync_code(project, runtime):
    for old in (project / 'src').rglob('*.py'):
        new = runtime / old.relative_to(project)
        new.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(old, new)
    for name in ('download.bat', 'requirements.txt'):
        shutil.copy2(project / name, runtime / name)
    assert all(sha(runtime / p.relative_to(project)) == sha(p)
               for p in (project / 'src').rglob('*.py'))
    assert sha(runtime / 'download.bat') == sha(project / 'download.bat')


def prepare_runtime(project, suite, source_profile):
    runtime = suite / 'runtime'
    sync_code(project, runtime)
    profile = suite / 'profile'
    copy_profile(source_profile, profile)
    subprocess.run([sys.executable, '-m', 'venv', str(runtime / '.venv-browser')], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    return runtime, profile


def configure(runtime, work, profile, peer):
    paths = {key: str((work / folder).resolve()) for key, folder in (
        ('output', 'output'), ('staging', 'staging'), ('partials', 'partials'),
        ('scan_records', 'scans'), ('test_artifacts', 'tests'))}
    paths.update(chrome_profile=str(profile.resolve()), groups=[peer])
    write_json(runtime / 'config/config.local.json', paths)
    return {key: Path(value) for key, value in paths.items() if key != 'groups'}


def copy_case(baseline, work, original_media):
    """媒体来自用户文件的独立副本；状态来自真实空白运行产生的基线。"""
    for old in files(baseline):
        new = work / old.relative_to(baseline)
        new.parent.mkdir(parents=True, exist_ok=True)
        source = original_media.get(old.name, old)
        shutil.copy2(source, new)
    for path in work.rglob('*.jsonl'):
        rows = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
        for row in rows:
            field = 'detail' if row.get('kind') == 'result' else 'path'
            value = row.get(field)
            if isinstance(value, str) and Path(value).is_absolute() and Path(value).is_relative_to(baseline):
                remapped = work / Path(value).relative_to(baseline)
                row[field] = str(remapped)
                if 'sha256' in row and remapped.is_file():
                    stat = remapped.stat()
                    row.update(size=stat.st_size, mtime_ns=stat.st_mtime_ns, dev=stat.st_dev, ino=stat.st_ino)
        path.write_text(''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in rows), encoding='utf-8')
