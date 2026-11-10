"""可选择范围的真实下载进度实测入口；默认仅列出目录。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from matrix_harness.runner import main

if __name__ == '__main__':
    raise SystemExit(main())
