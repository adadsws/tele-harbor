"""将运行细节写入本机日志，CMD 只显示用户进度。"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

LOGGER = logging.getLogger('telegg')
LOG_PATH: Path | None = None


def start_log() -> Path:
    global LOG_PATH
    root = Path(__file__).resolve().parents[2] / '~temp' / 'logs'
    root.mkdir(parents=True, exist_ok=True)
    path = root / f'telegg-{datetime.now():%Y%m%d-%H%M%S-%f}.log'
    handler = logging.FileHandler(path, encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    LOGGER.addHandler(handler)
    LOGGER.setLevel(logging.INFO)
    LOG_PATH = path
    return path


def detail(message: str) -> None:
    LOGGER.info(message)


def failure(message: str) -> None:
    LOGGER.exception(message)


def log_location() -> str:
    return str(LOG_PATH) if LOG_PATH else '本机诊断日志'
