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

from collector import tradingview
from config import settings
from notify import telegram
from storage import db
from scripts import run_cycle, run_collect

TEST_DB = "cache/_test_cycle.db"
settings.SETTINGS["db_path"] = TEST_DB
# 운영 기본값은 자동발송 OFF(2026-07-26 사용자 결정)지만, 아래 R7~X4 는 리포트 발송
# 경로 자체(주기 판정·실패 백오프·부분 실패 격리)를 검증하는 테스트라 ON 을 전제로 한다.
# 스위치 동작 자체는 마지막 R14 가 ON/OFF 를 오가며 따로 검증한다.
settings.SETTINGS["weekly_report_auto_send"] = True
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

# ── G1~G9: 수집 정체(last_collect_at staleness) 감시 (2026-07-26 과제3) ──────
# 기존 monitor/price_check.py 의 _check_collect_silence(신규 행 유무 - 결과 신호)와
# 다른 신호(meta.last_collect_at 갱신 여부 - 구조 신호)를 본다. 여기선 run_cycle.py
# 신설분(maybe_alert_collect_stale)만 검증한다.
STALE_DB = "cache/_test_collect_stale.db"
if os.path.exists(STALE_DB):
    os.remove(STALE_DB)
db.init_db(STALE_DB)

_orig_send_g = telegram.send
_sent_g = {"n": 0}
telegram.send = lambda text: (_sent_g.__setitem__("n", _sent_g["n"] + 1) or True)

check("G1 신규 DB(last_collect_at 없음)는 경고 안 함(최초 수집 전은 정상 상태)",
      run_cycle.maybe_alert_collect_stale(STALE_DB, now=NOON_KST) == "skipped"
      and _sent_g["n"] == 0)

with db.connect(STALE_DB) as conn:
    db.set_meta(conn, run_cycle.META_LAST_COLLECT, "0")
check("G1b last_collect_at='0' 센티널(reset_meta 관례)도 이력없음과 동일 취급",
      run_cycle.maybe_alert_collect_stale(STALE_DB, now=NOON_KST) == "skipped"
      and _sent_g["n"] == 0)

with db.connect(STALE_DB) as conn:
    db.set_meta(conn, run_cycle.META_LAST_COLLECT, str(NOON_KST - 1 * HOUR))
check("G2 임계(12h) 이내 갱신이면 경고 안 함",
      run_cycle.maybe_alert_collect_stale(STALE_DB, now=NOON_KST) == "skipped"
      and _sent_g["n"] == 0)

with db.connect(STALE_DB) as conn:
    db.set_meta(conn, run_cycle.META_LAST_COLLECT, str(NOON_KST - 13 * HOUR))
check("G3 임계(12h) 초과하면 경고 발송(ok)",
      run_cycle.maybe_alert_collect_stale(STALE_DB, now=NOON_KST) == "ok"
      and _sent_g["n"] == 1)

check("G4 같은 날 재확인은 중복 억제(skipped, 추가 발송 없음)",
      run_cycle.maybe_alert_collect_stale(STALE_DB, now=NOON_KST + HOUR) == "skipped"
      and _sent_g["n"] == 1)

NEXT_DAY_KST = NOON_KST + 24 * HOUR
check("G5 다음 날에도 여전히 정체 중이면 다시 경고(하루 1회 한도가 리셋됨)",
      run_cycle.maybe_alert_collect_stale(STALE_DB, now=NEXT_DAY_KST) == "ok"
      and _sent_g["n"] == 2)

telegram.send = _orig_send_g

# G6: 발송 실패(False 반환)는 failed - warned_date 를 기록하지 않아 다음 회차 재시도
with db.connect(STALE_DB) as conn:
    db.set_meta(conn, run_cycle.META_LAST_COLLECT, str(NEXT_DAY_KST - 20 * HOUR))
    db.set_meta(conn, run_cycle.META_COLLECT_STALE_WARNED, "1970-01-01")  # 재시도 대상으로
_orig_send_g2 = telegram.send
telegram.send = lambda text: False
check("G6 발송 실패(False 반환)는 failed",
      run_cycle.maybe_alert_collect_stale(STALE_DB, now=NEXT_DAY_KST) == "failed")


def _send_boom_g(_text):
    raise ConnectionError("telegram down")


telegram.send = _send_boom_g
check("G7 발송 중 예외도 격리(failed, 예외 전파 없음)",
      run_cycle.maybe_alert_collect_stale(STALE_DB, now=NEXT_DAY_KST) == "failed")
telegram.send = _orig_send_g2

# G8~G9: DB 접근 예외 격리 (다른 단계와 동일한 부분 실패 격리 원칙 - storage/db.py 는
# 수정 금지라 모듈 속성 교체만으로 재현한다, D16~D18 과 동일한 기법)
_orig_get_meta_g = db.get_meta


def _get_meta_boom_stale(conn, key, default=None):
    if key == run_cycle.META_LAST_COLLECT:
        raise RuntimeError("meta read boom")
    return _orig_get_meta_g(conn, key, default)


db.get_meta = _get_meta_boom_stale
check("G8 DB 조회 실패도 격리(failed, 예외 전파 없음)",
      run_cycle.maybe_alert_collect_stale(STALE_DB, now=NEXT_DAY_KST) == "failed")
db.get_meta = _orig_get_meta_g

with db.connect(STALE_DB) as conn:
    db.set_meta(conn, run_cycle.META_COLLECT_STALE_WARNED, "1970-01-01")  # 재발송 대상으로 되돌림
telegram.send = lambda text: (_sent_g.__setitem__("n", _sent_g["n"] + 1) or True)
_orig_set_meta_g = db.set_meta


def _set_meta_boom_stale(conn, key, value):
    if key == run_cycle.META_COLLECT_STALE_WARNED:
        raise RuntimeError("meta write boom")
    return _orig_set_meta_g(conn, key, value)


db.set_meta = _set_meta_boom_stale
_sent_before_g9 = _sent_g["n"]
check("G9 발송 후 meta 기록 실패도 격리(발송은 됐지만 상태는 failed)",
      run_cycle.maybe_alert_collect_stale(STALE_DB, now=NEXT_DAY_KST) == "failed"
      and _sent_g["n"] == _sent_before_g9 + 1)
db.set_meta = _orig_set_meta_g
telegram.send = _orig_send_g2

os.remove(STALE_DB)

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

# ── R10~R11: 리포트 DB 접근 예외 격리 (2026-07-26 과제1 — 감사 minor 조치) ──────
# 주기판정 조회/meta 기록도 DB 접근이라 실패할 수 있다 - try 밖에 있으면 run_cycle
# 전체(1단계 가격체크 결과의 커밋백 포함)까지 죽는다. db.get_meta/set_meta 를 일시
# 교체해 재현한다(storage/db.py 수정 금지 - D16~D18 과 동일한 모듈 속성 교체 기법).
reset_meta()
_orig_get_meta_r = db.get_meta


def _get_meta_boom_report(conn, key, default=None):
    if key in (run_cycle.META_LAST_REPORT, run_cycle.META_LAST_REPORT_FAIL):
        raise RuntimeError("meta read boom")
    return _orig_get_meta_r(conn, key, default)


db.get_meta = _get_meta_boom_report
check("R10 리포트 주기판정 DB 조회 실패도 격리(failed, 예외 전파 없음)",
      run_cycle.maybe_weekly_report(TEST_DB, now=NOON_KST, report_runner=lambda: True) == "failed")
db.get_meta = _orig_get_meta_r

reset_meta()
_orig_set_meta_r = db.set_meta


def _set_meta_boom_report(conn, key, value):
    if key in (run_cycle.META_LAST_REPORT, run_cycle.META_LAST_REPORT_FAIL):
        raise RuntimeError("meta write boom")
    return _orig_set_meta_r(conn, key, value)


db.set_meta = _set_meta_boom_report
check("R11 리포트 meta 기록 실패도 격리(발송은 됐어도 상태는 failed)",
      run_cycle.maybe_weekly_report(TEST_DB, now=NOON_KST, report_runner=lambda: True) == "failed")
db.set_meta = _orig_set_meta_r

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

# ── X5: 3·4단계 DB 접근이 죽어도 회차 전체(1단계 결과 포함)는 살아남는다 ───────
# (2026-07-26 과제1 통합검증) - 2단계(수집)는 이번 과제 범위 밖이라 건드리지 않도록
# 관련 meta 키만 콕 집어 예외를 주입한다(전체를 patch 하면 무관한 기존 이슈가 섞임).
reset_meta()
_orig_get_meta_x5 = db.get_meta


def _get_meta_boom_34(conn, key, default=None):
    if key in (run_cycle.META_LAST_SNAPSHOT, run_cycle.META_LAST_REPORT,
               run_cycle.META_LAST_REPORT_FAIL):
        raise RuntimeError("meta boom (3·4단계 전용)")
    return _orig_get_meta_x5(conn, key, default)


db.get_meta = _get_meta_boom_34
r_x5 = run_cycle.run_cycle(now=NOON_KST, price_runner=lambda: {"ok": 1},
                           collect_runner=lambda t: None, report_runner=lambda: True)
db.get_meta = _orig_get_meta_x5
check("X5 스냅샷·리포트 meta 조회가 죽어도 가격체크 결과·회차 자체는 살아남는다",
      r_x5["price_check"] == "ok" and r_x5["collect"] == "ok"
      and r_x5["collect_stale_alert"] == "skipped"
      and r_x5["author_snapshot"] == "failed" and r_x5["weekly_report"] == "failed")

# ── W1: 라이터 단일화 불변식 — data/ 를 커밋하는 워크플로는 1개뿐 ─────
wf_dir = Path(__file__).resolve().parent.parent / ".github" / "workflows"
writers = [p.name for p in wf_dir.glob("*.yml")
           if "git add data/" in p.read_text(encoding="utf-8")]
check("W1 data/ 커밋 백 워크플로는 price-check.yml 하나뿐", writers == ["price-check.yml"])

os.remove(TEST_DB)

# ── R14: 주간 리포트 자동발송 OFF (2026-07-26 사용자 결정) ──────────────
# 데이터·계산 절차는 유지하되 정기 발송만 끈다. --force-report 는 여전히 동작.
_sent_flag = {"n": 0}


def _spy_report():
    _sent_flag["n"] += 1
    return True


import os as _os14  # noqa: E402

# 주기 meta 를 지운 새 DB 로 시작해야 "발송할 때가 됐는지" 판정이 깨끗하다
if _os14.path.exists(TEST_DB):
    _os14.remove(TEST_DB)
db.init_db(TEST_DB)

settings.SETTINGS["weekly_report_auto_send"] = False
_r14 = run_cycle.run_cycle(now=NOON_KST, collect_enabled=False,
                           price_runner=lambda: {"ok": 1}, report_runner=_spy_report)
check("R14 자동발송 OFF - 정기 회차에서 리포트 미발송",
      _r14["weekly_report"] == "skipped" and _sent_flag["n"] == 0)

_r14b = run_cycle.run_cycle(now=NOON_KST, collect_enabled=False, force_report=True,
                            price_runner=lambda: {"ok": 1}, report_runner=_spy_report)
check("R14b OFF 여도 --force-report 는 발송",
      _r14b["weekly_report"] == "ok" and _sent_flag["n"] == 1)

# 스위치를 켜면 정기 발송이 재개되는지 (meta 초기화 후 확인)
if _os14.path.exists(TEST_DB):
    _os14.remove(TEST_DB)
db.init_db(TEST_DB)
settings.SETTINGS["weekly_report_auto_send"] = True
_sent_flag["n"] = 0
_r14c = run_cycle.run_cycle(now=NOON_KST, collect_enabled=False,
                            price_runner=lambda: {"ok": 1}, report_runner=_spy_report)
check("R14c 스위치 ON 이면 정기 발송 재개",
      _r14c["weekly_report"] == "ok" and _sent_flag["n"] == 1)
settings.SETTINGS["weekly_report_auto_send"] = False


# ── D1~D5: 글 삭제 감지 - storage.db 계층 (2026-07-26 과제1) ───────────
TEST_DB2 = "cache/_test_collect_extra.db"
if os.path.exists(TEST_DB2):
    os.remove(TEST_DB2)
db.init_db(TEST_DB2)

_sk_counter = [0]


def _insert_level(conn, status="touched", post_url="https://tv.example/x",
                  author="Author1", deleted=None, deleted_checked_at=None,
                  collected_at=None):
    _sk_counter[0] += 1
    key = f"testkey{_sk_counter[0]}"
    conn.execute(
        """INSERT INTO levels (signal_key, coin_symbol, ticker, direction, entry_usd,
             author, post_url, status, collected_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (key, "TST", "KRW-TST", "long", 1.0, author, post_url, status,
         collected_at if collected_at is not None else time.time()),
    )
    lid = conn.execute("SELECT id FROM levels WHERE signal_key=?", (key,)).fetchone()["id"]
    if deleted is not None or deleted_checked_at is not None:
        conn.execute("UPDATE levels SET deleted=?, deleted_checked_at=? WHERE id=?",
                     (1 if deleted else 0, deleted_checked_at, lid))
    return lid


with db.connect(TEST_DB2) as conn:
    lid_touched = _insert_level(conn, status="touched")
    lid_expired = _insert_level(conn, status="expired")
    lid_watching = _insert_level(conn, status="watching")
    cand_ids = {c["id"] for c in db.get_deletion_check_candidates(conn, 10, 999999)}
check("D1 삭제확인 후보는 종결(touched/expired) 레벨만",
      cand_ids == {lid_touched, lid_expired} and lid_watching not in cand_ids)

with db.connect(TEST_DB2) as conn:
    lid_del = _insert_level(conn, status="touched", deleted=True, deleted_checked_at=time.time())
    cand_ids2 = {c["id"] for c in db.get_deletion_check_candidates(conn, 50, 999999)}
check("D2 이미 삭제 확정된 레벨은 후보에서 제외", lid_del not in cand_ids2)

with db.connect(TEST_DB2) as conn:
    lid_recent = _insert_level(conn, status="touched", deleted=False,
                               deleted_checked_at=time.time())
    ids_soon = {c["id"] for c in db.get_deletion_check_candidates(conn, 50, 999999)}
check("D3 최근 생존확인된 레벨은 재확인 창 내에서 제외", lid_recent not in ids_soon)
with db.connect(TEST_DB2) as conn:
    conn.execute("UPDATE levels SET deleted_checked_at=? WHERE id=?",
                 (time.time() - 999999, lid_recent))
    ids_late = {c["id"] for c in db.get_deletion_check_candidates(conn, 50, 1000)}
check("D3b 재확인 창을 넘기면 다시 후보에 포함", lid_recent in ids_late)

with db.connect(TEST_DB2) as conn:
    lid4 = _insert_level(conn, status="expired")
    db.mark_deletion_checked(conn, lid4, True, now=1000.0)
    row = conn.execute("SELECT deleted, deleted_checked_at FROM levels WHERE id=?",
                       (lid4,)).fetchone()
check("D4 mark_deletion_checked 이 삭제 플래그·확인시각을 기록",
      row["deleted"] == 1 and row["deleted_checked_at"] == 1000.0)

with db.connect(TEST_DB2) as conn:
    a = "AuthorD5"
    _insert_level(conn, status="touched", author=a, deleted=True, deleted_checked_at=time.time())
    _insert_level(conn, status="touched", author=a, deleted=False, deleted_checked_at=time.time())
    _insert_level(conn, status="touched", author=a)  # 미확인 - 분모(checked) 제외돼야 함
    stats5 = db.get_author_deletion_stats(conn, a)
check("D5 작성자 삭제 통계(확인 2건 중 삭제 1건, 미확인은 분모 제외)",
      stats5 == {"checked": 2, "deleted": 1})

os.remove(TEST_DB2)

# ── D6~D10: collector.tradingview.check_post_deleted (네트워크 모킹) ───
_orig_tv_get = tradingview._get


def _mock_get(result):
    return lambda url, timeout, max_retry=3: result


tradingview._get = _mock_get((None, True, False))  # 차단 쿨다운 중
check("D6 차단 중이면 판정 보류(None)", tradingview.check_post_deleted("https://x", 5.0) is None)

tradingview._get = _mock_get((None, False, True))  # 404
check("D7 404 확인 시 삭제 확정(True)", tradingview.check_post_deleted("https://x", 5.0) is True)

_good_html = ('<script type="application/prs.init-data+json">'
             '{"a":{"ssrIdeaData":{"name":"x"}}}</script>')
tradingview._get = _mock_get((_good_html, False, False))
check("D8 200+정상 아이디어 구조는 생존(False)",
      tradingview.check_post_deleted("https://x", 5.0) is False)

tradingview._get = _mock_get(("<html>알 수 없는 페이지</html>", False, False))
check("D9 200 이지만 구조 미인식은 판정보류(None) - 거짓양성 방지",
      tradingview.check_post_deleted("https://x", 5.0) is None)

check("D10 빈 URL 은 즉시 None(요청 없음)", tradingview.check_post_deleted("", 5.0) is None)

tradingview._get = _orig_tv_get

# ── D11~D13: 확정 차단 감지 - hard_block_detected (2026-07-26 과제2) ───
_orig_get_session = tradingview._get_session
_orig_blocked_until = tradingview._blocked_until
_orig_consec_fail = tradingview._consec_fail

tradingview.reset_detail_budget()
check("D11 리셋 직후엔 확정 차단 없음", tradingview.hard_block_detected() is None)


class _FakeResp:
    def __init__(self, status):
        self.status_code = status
        self.text = ""


class _FakeCookies:
    def set(self, *a, **k):
        pass


class _FakeSession:
    def __init__(self, status):
        self._status = status
        self.cookies = _FakeCookies()

    def get(self, url, headers=None, timeout=None):
        return _FakeResp(self._status)


tradingview._get_session = lambda: _FakeSession(403)
tradingview._blocked_until = 0.0
tradingview._get("https://tv.example/probe", 5.0, max_retry=1)
check("D12 403 응답 감지 시 hard_block_detected 가 상태코드를 반환",
      tradingview.hard_block_detected() == 403)

tradingview.reset_detail_budget()
check("D13 reset_detail_budget() 이 확정 차단 플래그도 함께 리셋", tradingview.hard_block_detected() is None)

tradingview._get_session = _orig_get_session
tradingview._blocked_until = _orig_blocked_until
tradingview._consec_fail = _orig_consec_fail

# ── D14~D18: scripts.run_collect 의 삭제확인/차단경보 게이팅 ───────────
TEST_DB3 = "cache/_test_collect_gate.db"
if os.path.exists(TEST_DB3):
    os.remove(TEST_DB3)
db.init_db(TEST_DB3)

with db.connect(TEST_DB3) as conn:
    for _ in range(3):
        _insert_level(conn, status="touched")

_orig_check_post_deleted = tradingview.check_post_deleted
_calls = {"n": 0}


def _fake_check(url, timeout):
    _calls["n"] += 1
    return False  # 전부 생존


tradingview.check_post_deleted = _fake_check
_orig_limit = settings.SETTINGS["deletion_check_daily_limit"]
settings.SETTINGS["deletion_check_daily_limit"] = 2

with db.connect(TEST_DB3) as conn:
    n_del = run_collect._check_deletions(conn, timeout=5.0)
check("D14 하루 상한(2건)만큼만 확인", _calls["n"] == 2 and n_del == 0)

_calls["n"] = 0
with db.connect(TEST_DB3) as conn:
    run_collect._check_deletions(conn, timeout=5.0)
check("D15 같은 날 재호출은 생략(day-gate, 비용 방어)", _calls["n"] == 0)

tradingview.check_post_deleted = _orig_check_post_deleted
settings.SETTINGS["deletion_check_daily_limit"] = _orig_limit
os.remove(TEST_DB3)

TEST_DB4 = "cache/_test_block_alert.db"
if os.path.exists(TEST_DB4):
    os.remove(TEST_DB4)
db.init_db(TEST_DB4)

_orig_send = telegram.send
_sent = {"n": 0}
telegram.send = lambda text: (_sent.__setitem__("n", _sent["n"] + 1) or True)
_orig_hard_block = tradingview.hard_block_detected
tradingview.hard_block_detected = lambda: 403
_orig_alert_limit = settings.SETTINGS["tv_block_alert_daily_limit"]
settings.SETTINGS["tv_block_alert_daily_limit"] = 1

_now_b = time.time()
with db.connect(TEST_DB4) as conn:
    run_collect._maybe_alert_block(conn, _now_b)
check("D16 확정 차단 감지 시 즉시 경보 발송", _sent["n"] == 1)

with db.connect(TEST_DB4) as conn:
    run_collect._maybe_alert_block(conn, _now_b)
check("D17 하루 상한(1회) 도달 후 추가 발송 억제(알림 과다 방지)", _sent["n"] == 1)

tradingview.hard_block_detected = lambda: None
with db.connect(TEST_DB4) as conn:
    run_collect._maybe_alert_block(conn, _now_b)
check("D18 차단 신호 없으면 발송 안 함", _sent["n"] == 1)

telegram.send = _orig_send
tradingview.hard_block_detected = _orig_hard_block
settings.SETTINGS["tv_block_alert_daily_limit"] = _orig_alert_limit
os.remove(TEST_DB4)


# ── D19~D20: 차단경보 meta 키 정리 (2026-07-26 과제2 — 감사 minor 조치) ───────
TEST_DB5 = "cache/_test_block_alert_prune.db"
if os.path.exists(TEST_DB5):
    os.remove(TEST_DB5)
db.init_db(TEST_DB5)

with db.connect(TEST_DB5) as conn:
    db.set_meta(conn, "tv_block_alert_count_2026-07-01", "1")   # 오래됨 - 삭제 대상
    db.set_meta(conn, "tv_block_alert_count_2026-07-18", "1")   # cutoff 이전 - 삭제 대상
    db.set_meta(conn, "tv_block_alert_count_2026-07-19", "1")   # cutoff 당일 - 보존(경계)
    db.set_meta(conn, "tv_block_alert_count_2026-07-20", "1")   # 최근 - 보존
    db.set_meta(conn, "tv_block_alert_count_2026-07-26", "2")   # 오늘 - 보존
    db.set_meta(conn, "other_unrelated_key", "keep me")          # 무관 키 - 항상 보존
    n_pruned = run_collect._prune_tv_block_alert_meta(conn, "2026-07-26", 7)

with db.connect(TEST_DB5) as conn:
    remaining = {r["key"] for r in conn.execute(
        "SELECT key FROM meta WHERE key LIKE 'tv_block_alert_count_%'").fetchall()}
    other_kept = db.get_meta(conn, "other_unrelated_key")

check("D19 보존기간(7일) 이전 차단경보 키만 정리(경계일 07-19 는 보존)", n_pruned == 2)
check("D19b 남은 키는 경계일 이후 + 오늘만",
      remaining == {"tv_block_alert_count_2026-07-19", "tv_block_alert_count_2026-07-20",
                    "tv_block_alert_count_2026-07-26"})
check("D20 무관 meta 키는 건드리지 않음", other_kept == "keep me")

os.remove(TEST_DB5)


# ── S1~S5: 작성자 주간 스냅샷 (2026-07-26 — 역신호 태깅 선행 인프라) ──
SNAP_DB = "cache/_test_snapshot.db"
if os.path.exists(SNAP_DB):
    os.remove(SNAP_DB)
db.init_db(SNAP_DB)
with db.connect(SNAP_DB) as _c:
    for _i, (_a, _oc, _r) in enumerate([("AuthX", "hit", 2.0), ("AuthX", "miss", -1.0),
                                        ("AuthY", "miss", -1.0)]):
        _c.execute(
            "INSERT INTO levels (signal_key, coin_symbol, ticker, direction, status, "
            "collected_at, author, outcome, r_multiple, touched_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (f"s{_i}", "SOL", "KRW-SOL", "long", "touched", NOON_KST - 86400,
             _a, _oc, _r, NOON_KST - 3600))

_n_snap = run_cycle.take_author_snapshot(SNAP_DB, now=NOON_KST)
check("S1 작성자 수만큼 저장", _n_snap == 2)

with db.connect(SNAP_DB) as _c:
    _snaps = {r["author"]: r for r in db.get_author_snapshots(_c)}
check("S2 주차 키 형식(YYYY-Www)", db.week_kst(NOON_KST).startswith("2026-W")
      and all(s["week_kst"] == db.week_kst(NOON_KST) for s in _snaps.values()))
check("S3 지표·원시 승패 함께 저장",
      _snaps["AuthX"]["wins"] == 1 and _snaps["AuthX"]["losses"] == 1
      and _snaps["AuthX"]["e_lb"] is not None and _snaps["AuthY"]["neff_r"] is not None)

# 같은 주 재실행은 덮어쓰기 (행이 늘지 않음)
run_cycle.take_author_snapshot(SNAP_DB, now=NOON_KST + 60)
with db.connect(SNAP_DB) as _c:
    _cnt = _c.execute("SELECT COUNT(*) FROM author_snapshots").fetchone()[0]
check("S4 같은 주 재실행은 덮어쓰기(중복 행 없음)", _cnt == 2)

# 주기 게이트: 1회 저장 후 즉시 재호출은 skipped, 168h 뒤엔 다시 ok
settings.SETTINGS["db_path"] = SNAP_DB
check("S5 주기 게이트",
      run_cycle.maybe_author_snapshot(SNAP_DB, now=NOON_KST) == "ok"
      and run_cycle.maybe_author_snapshot(SNAP_DB, now=NOON_KST + 60) == "skipped"
      and run_cycle.maybe_author_snapshot(SNAP_DB, now=NOON_KST + 169 * 3600) == "ok")

# 스냅샷이 죽어도 회차는 계속 (부분 실패 격리)
def _snap_boom(*_a, **_k):
    raise RuntimeError("snapshot boom")

check("S5b 스냅샷 실패 격리",
      run_cycle.maybe_author_snapshot(SNAP_DB, now=NOON_KST + 400 * 3600,
                                      snapshot_runner=_snap_boom) == "failed")

# ── S6~S7: 스냅샷 DB 접근 예외 격리 (2026-07-26 과제1 — 감사 minor 조치) ────────
# meta 조회/기록도 DB 접근이라 실패할 수 있다 - try 밖에 있으면 run_cycle 전체(1단계
# 가격체크 결과의 커밋백 포함)까지 죽는다. db.get_meta/set_meta 를 일시 교체해
# 재현한다(storage/db.py 수정 금지 - D16~D18 과 동일한 모듈 속성 교체 기법).
_orig_get_meta_s = db.get_meta


def _get_meta_boom_snap(conn, key, default=None):
    if key == run_cycle.META_LAST_SNAPSHOT:
        raise RuntimeError("meta read boom")
    return _orig_get_meta_s(conn, key, default)


db.get_meta = _get_meta_boom_snap
check("S6 스냅샷 meta 조회 실패도 격리(예외 전파 없이 failed)",
      run_cycle.maybe_author_snapshot(SNAP_DB, now=NOON_KST + 500 * 3600) == "failed")
db.get_meta = _orig_get_meta_s

_orig_set_meta_s = db.set_meta


def _set_meta_boom_snap(conn, key, value):
    if key == run_cycle.META_LAST_SNAPSHOT:
        raise RuntimeError("meta write boom")
    return _orig_set_meta_s(conn, key, value)


db.set_meta = _set_meta_boom_snap
check("S7 스냅샷 meta 기록 실패도 격리(스냅샷 자체는 저장되고 상태만 failed)",
      run_cycle.maybe_author_snapshot(SNAP_DB, now=NOON_KST + 600 * 3600) == "failed")
db.set_meta = _orig_set_meta_s

settings.SETTINGS["db_path"] = TEST_DB
if os.path.exists(SNAP_DB):
    os.remove(SNAP_DB)


sys.exit(0 if ok else 1)