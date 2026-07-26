#!/usr/bin/env python3
"""
가격체크 단독 실행 (디버깅·수동 확인용).

2026-07-26 구조 개선으로 **운영 회차의 엔트리포인트는 scripts/run_cycle.py** 다
(가격체크 + 4시간마다 수집 + 7일마다 주간 리포트를 한 프로세스에서 처리 = 단일 DB 라이터).
이 파일을 실행하면 수집/리포트 없이 가격체크만 1회 돈다.

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
