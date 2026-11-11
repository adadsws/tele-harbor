"""只读复核本批原数据保护基线；仅输出数量，不输出文件名或内容。"""
import json

from telegg.local_config import CONFIG_FILE, load_paths
from matrix_harness.isolation import protected_state, write_json


def run(suite):
    paths = load_paths()
    roots = {key: paths[key] for key in ('output', 'staging', 'partials', 'scan_records', 'chrome_profile')}
    roots['config'] = CONFIG_FILE
    hashes = [CONFIG_FILE, *paths['scan_records'].glob('*.jsonl')]
    before = json.loads((suite / 'source-before.json').read_text(encoding='utf-8'))
    after = json.loads(json.dumps(protected_state(roots, hashes)))
    write_json(suite / 'source-after.json', after)
    result = {'file_metadata_unchanged': before['files'] == after['files'],
              'config_and_scan_hashes_unchanged': before['hashes'] == after['hashes'],
              'files_checked': len(after['files']), 'hashes_checked': len(after['hashes'])}
    result['passed'] = result['file_metadata_unchanged'] and result['config_and_scan_hashes_unchanged']
    write_json(suite / 'source-protection.json', result)
    print(json.dumps(result), flush=True)
    return 0 if result['passed'] else 1
