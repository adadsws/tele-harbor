"""用户指定的实测排除清单；只约束诊断工具，正式下载程序不读取。"""
import json
import re
from pathlib import Path

from telegg.local_config import load_paths


POLICY_NAME = 'live-test-exclusions.json'


def assert_live_test_allowed(peer: str, mid: str, *, policy_path: Path | None = None) -> None:
    path = policy_path or load_paths()['scan_records'].parent / POLICY_NAME
    if not path.exists():
        return
    try:
        if path.is_symlink():
            raise ValueError('policy must be a regular file')
        data = json.loads(path.read_text(encoding='utf-8'))
        if data['version'] != 1 or not isinstance(data['resources'], list):
            raise ValueError('invalid policy')
        keys = set()
        for row in data['resources']:
            if (not re.fullmatch(r'-[0-9]+', row['peer'])
                    or not re.fullmatch(r'[0-9]+', row['mid']) or int(row['mid']) <= 0
                    or row['classification'] != 'damaged'
                    or not isinstance(row['reason'], str) or not row['reason'].strip()):
                raise ValueError('invalid exclusion')
            keys.add((row['peer'], int(row['mid']) % (1 << 32)))
    except (OSError, ValueError, TypeError, KeyError) as error:
        raise ValueError('实测排除清单损坏；未启动诊断或下载') from error
    if (str(peer), int(mid) % (1 << 32)) in keys:
        raise ValueError('该资源已按用户要求排除出专项实测；正式程序仍使用通用错误处理')
