# 단일 라이터 회차(run_cycle) 회귀 테스트 — 네트워크·텔레그램 없이 러너 주입으로 검증.
# 커버: 수집/리포트 주기 판정, 실패 백오프, 부분 실패 격리, 발송 시간대 창, 종료코드 의미.
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import logging
logging.basicConfig(level=logging.CRITICAL)

from config import settings
from storage import db
from scripts import run_cycle

TEST_DB = "cache/_test_cycle.db"
settings.SETTINGS["db_path"] = TEST_DB
if os.path.exists(TEST_DB):
    os.remove(TEST_DB)
db.init_db(TEST_DB)

ok = True
HOUR = 3600.0
KST = timezone(timedelta(hours=9))


def check(name, cond):
    global ok
    print(("✅" if cond else "❌"), name)
    ok = ok and cond


def reset_meta():
    with db.connect(TEST_DB) as conn:
        for k in (run_cycle.META_LAST_COLLECT, run_cycle.META_LAST_COLLECT_FAIL,
                  run_cycle.META_LAST_REPORT, run_cycle.META_LAST_REPORT_FAIL):
            db.set_meta(conn, k, "0")


def set_meta(key, val):
    with db.connect(TEST_DB) as conn:
        db.set_meta(conn, key, str(val))


def get_meta(key):
    with db.connect(TEST_DB) as conn:
        return run_cycle._meta_float(conn, key)


def due_collect(now):
    with db.connect(TEST_DB) as conn:
        return run_cycle.collect_due(conn, now, 4 * HOUR, 30 * 60)


def due_report(now, hour_from=0, hour_to=24):
    with db.connect(TEST_DB) as conn:
        return run_cycle.report_due(conn, now, 168 * HOUR, 60 * 60, hour_from, hour_to)


# KST 정오에 해당하는 기준 시각 (시간대 창 테스트가 로컬 시간대와 무관하도록 고정)
NOON_KST = datetime(2026, 7, 20, 12, 0, tzinfo=KST).timestamp()

# ── C1~C5: 수집 주기 판정 ─────────────────────────────────────────────
reset_meta()
check("C1 수집 이력 없으면 즉시 due", due_collect(NOON_KST)[0] is True)

set_meta(run_cycle.META_LAST_COLLECT, NOON_KST - 2 * HOUR)
check("C2 2시간 경과는 아직 아님", due_collect(NOON_KST)[0] is False)

set_meta(run_cycle.META_LAST_COLLECT, NOON_KST - 4.1 * HOUR)
check("C3 4시간 경과하면 due", due_collect(NOON_KST)[0] is True)

set_meta(run_cycle.META_LAST_COLLECT_FAIL, NOON_KST - 5 * 60)
check("C4 실패 직후 5분은 백오프로 차단", due_collect(NOON_KST)[0] is False)

set_meta(run_cycle.META_LAST_COLLECT_FAIL, NOON_KST - 31 * 60)
check("C5 백오프(30분) 지나면 재시도 허용", due_collect(NOON_KST)[0] is True)

# C6: 미래 시각(시계 역행/DB 수동 편집)이 영구 굶주림을 만들지 않는다
reset_meta()
set_meta(run_cycle.META_LAST_COLLECT, NOON_KST + 100 * HOUR)
check("C6 미래 타임스탬프는 무시하고 due", due_collect(NOON_KST)[0] is True)

# ── C7~C11: maybe_collect 실행/마킹/격리 ──────────────────────────────
reset_meta()
calls = []
check("C7 성공 시 ok 반환",
      run_cycle.maybe_collect(TEST_DB, now=NOON_KST,
                              collect_runner=lambda t: calls.append(t)) == "ok")
check("C7b last_collect_at 갱신", abs(get_meta(run_cycle.META_LAST_COLLECT) - NOON_KST) < 1)
check("C7c 실패 마커 초기화", get_meta(run_cycle.META_LAST_COLLECT_FAIL) == 0)
check("C7d 러너에 타임아웃 인자 전달", calls and calls[0] == run_cycle.COLLECT_TIMEOUT_SEC)

ran = []
check("C8 주기 전이면 건너뛴다(러너 미호출)",
      run_cycle.maybe_collect(TEST_DB, now=NOON_KST + HOUR,
                              collect_runner=lambda t: ran.append(1)) == "skipped" and not ran)

check("C9 force 는 주기를 무시하고 실행",
      run_cycle.maybe_collect(TEST_DB, now=NOON_KST + HOUR, force=True,
                              collect_runner=lambda t: ran.append(1)) == "ok" and len(ran) == 1)

check("C10 --no-collect 는 무조건 건너뜀",
      run_cycle.maybe_collect(TEST_DB, now=NOON_KST + 99 * HOUR, enabled=False,
                              collect_runner=lambda t: ran.append(1)) == "skipped" and len(ran) == 1)


def boom(_t):
    raise RuntimeError("TradingView 차단")


reset_meta()
res = run_cycle.maybe_collect(TEST_DB, now=NOON_KST, collect_runner=boom)
check("C11 수집 예외를 삼키고 failed 반환(예외 전파 없음)", res == "failed")
check("C11b 실패는 성공으로 기록되지 않음", get_meta(run_cycle.META_LAST_COLLECT) == 0)
check("C11c 실패 시각 기록(백오프 근거)",
      abs(get_meta(run_cycle.META_LAST_COLLECT_FAIL) - NOON_KST) < 1)


def hang(_t):
    raise subprocess.TimeoutExpired(cmd="run_collect.py", timeout=1)


reset_meta()
check("C12 수집 타임아웃도 동일하게 격리",
      run_cycle.maybe_collect(TEST_DB, now=NOON_KST, collect_runner=hang) == "failed")


def die(_t):
    raise SystemExit(3)  # BaseException 계열(subprocess 실패·sys.exit)도 회차를 죽이면 안 된다


reset_meta()
check("C13 SystemExit 도 격리",
      run_cycle.maybe_collect(TEST_DB, now=NOON_KST, collect_runner=die) == "failed")

# ── R1~R6: 주간 리포트 주기·시간대 ────────────────────────────────────
reset_meta()
check("R1 이력 없으면 즉시 due(첫 회차 발송)", due_report(NOON_KST)[0] is True)

set_meta(run_cycle.META_LAST_REPORT, NOON_KST - 100 * HOUR)
check("R2 7일 전이면 대기", due_report(NOON_KST)[0] is False)

set_meta(run_cycle.META_LAST_REPORT, NOON_KST - 169 * HOUR)
check("R3 7일 지나면 due", due_report(NOON_KST)[0] is True)

check("R4 KST 발송 창 밖이면 대기", due_report(NOON_KST, 13, 22)[0] is False)
check("R5 KST 발송 창 안이면 발송", due_report(NOON_KST, 9, 22)[0] is True)
new_year_dawn = datetime(2026, 7, 21, 3, 0, tzinfo=KST).timestamp()
check("R5b 새벽 3시(KST)는 기본 창(9~22) 밖",
      due_report(new_year_dawn, 9, 22)[0] is False)

set_meta(run_cycle.META_LAST_REPORT_FAIL, NOON_KST - 10 * 60)
check("R6 발송 실패 후 60분 백오프", due_report(NOON_KST)[0] is False)

# ── R7~R9: maybe_weekly_report ────────────────────────────────────────
reset_meta()
check("R7 발송 성공(True) → ok + 시각 기록",
      run_cycle.maybe_weekly_report(TEST_DB, now=NOON_KST, report_runner=lambda: True) == "ok"
      and abs(get_meta(run_cycle.META_LAST_REPORT) - NOON_KST) < 1)

reset_meta()
check("R8 발송 실패(False) → failed, 성공 기록 안 함(그 주 리포트 유실 방지)",
      run_cycle.maybe_weekly_report(TEST_DB, now=NOON_KST, report_runner=lambda: False) == "failed"
      and get_meta(run_cycle.META_LAST_REPORT) == 0
      and abs(get_meta(run_cycle.META_LAST_REPORT_FAIL) - NOON_KST) < 1)


def rboom():
    raise ConnectionError("telegram down")


reset_meta()
check("R9 발송 예외도 격리(failed)",
      run_cycle.maybe_weekly_report(TEST_DB, now=NOON_KST, report_runner=rboom) == "failed")

# ── X1~X4: run_cycle 통합(부분 실패 격리·순서) ────────────────────────
reset_meta()
order = []
r = run_cycle.run_cycle(now=NOON_KST,
                        price_runner=lambda: order.append("price"),
                        collect_runner=lambda t: order.append("collect"),
                        report_runner=lambda: order.append("report") or True)
check("X1 가격체크가 가장 먼저(수집이 알림을 지연시키지 않음)", order[0] == "price")
check("X1b 세 단계 모두 수행", order == ["price", "collect", "report"])
check("X1c 요약 상태", (r["price_check"], r["collect"], r["weekly_report"]) == ("ok", "ok", "ok"))

reset_meta()
order = []
r = run_cycle.run_cycle(now=NOON_KST,
                        price_runner=lambda: order.append("price"),
                        collect_runner=boom,
                        report_runner=lambda: order.append("report") or True)
check("X2 수집이 죽어도 가격체크·리포트는 정상 수행",
      r["collect"] == "failed" and r["price_check"] == "ok" and r["weekly_report"] == "ok")


def pboom():
    raise RuntimeError("업비트 API 장애")


reset_meta()
order = []
r = run_cycle.run_cycle(now=NOON_KST, price_runner=pboom,
                        collect_runner=lambda t: order.append("collect"),
                        report_runner=lambda: True)
check("X3 가격체크가 죽어도 수집은 수행되고 회차는 계속",
      r["price_check"] == "failed" and r["collect"] == "ok" and order == ["collect"])

reset_meta()
r = run_cycle.run_cycle(now=NOON_KST, price_runner=lambda: None,
                        collect_runner=boom, report_runner=lambda: False)
check("X4 종료코드는 가격체크만 반영(수집·리포트 실패는 경고)",
      r["price_check"] == "ok" and r["collect"] == "failed" and r["weekly_report"] == "failed")

# ── W1: 라이터 단일화 불변식 — data/ 를 커밋하는 워크플로는 1개뿐 ─────
wf_dir = Path(__file__).resolve().parent.parent / ".github" / "workflows"
writers = [p.name for p in wf_dir.glob("*.yml")
           if "git add data/" in p.read_text(encoding="utf-8")]
check("W1 data/ 커밋 백 워크플로는 price-check.yml 하나뿐", writers == ["price-check.yml"])

os.remove(TEST_DB)
sys.exit(0 if ok else 1)
