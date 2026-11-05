"""读取项目本地配置中的运行数据目录。"""

import json
from pathlib import Path

CONFIG_FILE = Path(__file__).resolve().parents[2] / 'config' / 'config.local.json'


def load_paths() -> dict[str, Path]:
    config = json.loads(CONFIG_FILE.read_text(encoding='utf-8-sig'))
    if not isinstance(config, dict):
        raise ValueError('本地配置必须是 JSON 对象')
    paths = {}
    for key in ('output', 'staging', 'partials', 'chrome_profile', 'scan_records', 'test_artifacts'):
        value = config.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f'本地配置缺少有效的 {key}：{CONFIG_FILE}')
        paths[key] = Path(value).resolve()
    return paths
