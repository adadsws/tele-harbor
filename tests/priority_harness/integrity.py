"""逐文件独立复读哈希并追加持久结果，原媒体及验收快照只读。"""
import hashlib
import json
import time
from pathlib import Path

from matrix_harness.isolation import write_json


def stamp(path):
    stat = path.stat()
    return [stat.st_mtime_ns, stat.st_ino, stat.st_dev]


def run(suite):
    inventory = json.loads((suite / 'inventory.json').read_text(encoding='utf-8'))
    journal = suite / 'independent-hashes.jsonl'
    old = ([json.loads(line) for line in journal.read_text(encoding='utf-8').splitlines()]
           if journal.exists() else [])
    verified = {(row['peer'], row['mid']): row for row in old if row['passed']}
    total, done, count, failures = sum(j['size'] for j in inventory['jobs']), 0, 0, []
    started, last = time.monotonic(), 0.0
    with journal.open('a', encoding='utf-8', newline='\n') as stream:
        for job in inventory['jobs']:
            path = Path(job['path'])
            key = (job['peer'], job['mid'])
            cached = verified.get(key)
            if cached and cached['sha256'] == job['sha256'] and stamp(path) == job['stamp']:
                done += job['size']
                count += 1
                continue
            before = stamp(path)
            with path.open('rb') as media:
                digest = hashlib.file_digest(media, 'sha256').hexdigest()
            passed = (digest == job['sha256'] and path.stat().st_size == job['size']
                      and before == job['stamp'] == stamp(path))
            copies_ok = True
            for candidate in job['old_copies']:
                with Path(candidate).open('rb') as original:
                    copies_ok &= hashlib.file_digest(original, 'sha256').hexdigest() == digest
            passed &= copies_ok
            row = {'peer': job['peer'], 'mid': job['mid'], 'group': job['group'],
                   'sha256': digest, 'size': job['size'], 'passed': passed,
                   'old_copies_checked': len(job['old_copies'])}
            stream.write(json.dumps(row) + '\n')
            stream.flush()
            if not passed:
                failures.append({'group': job['group'], 'mid': job['mid']})
            done += job['size']
            count += 1
            now = time.monotonic()
            if now - last >= 5 or count == len(inventory['jobs']):
                print(json.dumps({'group': job['group'], 'hashed_files': count,
                    'total_files': len(inventory['jobs']), 'bytes_read': done, 'total_bytes': total,
                    'seconds': round(now - started, 2), 'hash_failures': len(failures)}), flush=True)
                last = now
    result = {'files': count, 'bytes': done, 'seconds': round(time.monotonic() - started, 2),
              'hash_failures': len(failures), 'inventory_passed': not inventory['issues'],
              'passed': not failures and not inventory['issues'] and count == len(inventory['jobs'])}
    write_json(suite / 'integrity-result.json', result)
    print(json.dumps(result), flush=True)
    return 0 if result['passed'] else 1
