#!/usr/bin/env python3
"""特征一致性检查台启动入口（兼容 python -I 直接运行）。

用法：
  python3 -I run.py run --config samples/config.json \
      --offline samples/offline.csv --online samples/online.csv
  python3 -I run.py serve --port 8765
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from feature_check.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
