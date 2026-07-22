#!/usr/bin/env python3
"""
가격체크 잡 엔트리포인트 (5~10분마다 — cron-job.org → GitHub Actions).
로직은 monitor/price_check.py 참고.
"""

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

from monitor.price_check import run_once

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

if __name__ == "__main__":
    summary = run_once()
    sys.exit(0)
