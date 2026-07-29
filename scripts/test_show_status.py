# scripts.show_status 회귀 테스트 — 읽기 전용 조회 도구.
# 네트워크/텔레그램 호출 없음(render_weekly_report 는 텍스트 조립만, send() 미호출).
# 임시 DB만 사용 — 프로덕션 data/levels.db 는 절대 건드리지 않는다.
import io
import os
import sqlite3
import sys
import time
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import logging
logging.basicConfig(level=logging.CRITICAL)

from storage import db
from scripts import show_status

TEST_DB = "cache/_test_show_status.db"
NOW = 1_800_000_000.0  # 고정 시각 — 상대 시간 표현("N분 전")이 흔들리지 않게

ok = True


def check(name, cond):
    global ok
    print(("✅" if cond else "❌"), name)
    ok = ok and cond


def fresh_db():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    db.init_db(TEST_DB)


def capture(**kw):
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = show_status.run(TEST_DB, kw.pop("days", 7), kw.pop("report", False), now=NOW)
    return code, buf.getvalue()


# ── S1: DB 자체가 없음 → 에러 코드 1, 깨지지 않음 ────────────────────
missing = "cache/_test_show_status_missing.db"
if os.path.exists(missing):
    os.remove(missing)
buf = io.StringIO()
with redirect_stdout(buf):
    code = show_status.run(missing, 7, False, now=NOW)
check("S1 DB 없음 -> 종료코드 1", code == 1)
check("S1 DB 없음 -> 메시지", "DB 없음" in buf.getvalue())

# ── S2: 스키마만 있고 데이터 전혀 없음 → 깨지지 않고 '없음' 계열 문구 ──
fresh_db()
code, out = capture(days=5)
check("S2 빈 DB 정상 종료", code == 0)
check("S2 관찰 집계 없음 문구", "관찰 집계 없음" in out)
check("S2 등급분포 대상없음 문구", "대상 없음" in out)
check("S2 작성자 종결표본 없음 문구", "종결 표본 없음" in out)
check("S2 가격체크 이력 없음 경고", "가격체크 이력 없음" in out)
check("S2 수집 이력 없음 경고", "수집 이력 없음" in out)

# ── S3: 관찰 집계(daily_stats) 표시 + 전환율 계산 ────────────────────
fresh_db()
with db.connect(TEST_DB) as conn:
    db.bump_daily_stats(conn, "2026-07-25", touches_total=8, previews_total=2,
                        suppressed_grade=3, suppressed_cap=1, preview_dwell=1,
                        suppressed_send_fail=1, suppressed_tp_too_close=2)
    db.record_alert(conn, "BTC", "touch", [1], "2026-07-25", NOW)
    db.record_alert(conn, "BTC", "touch", [2], "2026-07-25", NOW)
code, out = capture(days=7)
check("S3 날짜 행 표시", "2026-07-25" in out)
# 발송 2건 / (터치8+예고2=10) = 20%
check("S3 전환율 20% 계산", "20%" in out)
# 2026-07-26 감사 MAJOR-1: 중복예고는 억제가 아니라 체류 관측이라 열이 분리됐다
check("S3 억제 사유 열 표시(등급/상한/실패/근접TP)", "3/1/1/2" in out)
check("S3b 체류 열은 억제와 분리 표시", "체류" in out)
check("S3 발송실패 있으면 경고 아이콘", "⚠️" in out)

# ── S4: 파이프라인 상태 — 레벨 상태별 카운트 + 등급 분포 ─────────────
fresh_db()
with db.connect(TEST_DB) as conn:
    for i, (status_grade) in enumerate(["A", "B", "B"]):
        key = f"lvl{i}"
        db.upsert_level(conn, dict(
            signal_key=key, coin_symbol="ETH", ticker="KRW-ETH", direction="long",
            entry_usd=100, sl_usd=90, tp_usd=120, rr=2.0, grade=status_grade, score=70,
            author="alice", collected_at=NOW - 1000))
    db.set_meta(conn, "last_check_at", str(NOW - 60))       # 1분 전 — 정상
    db.set_meta(conn, "last_collect_at", str(NOW - 3600))   # 1시간 전 — 정상(주기 4시간)
code, out = capture(days=7)
check("S4 감시중 3건", "감시중(watching) 3" in out)
check("S4 등급분포 A:1 B:2", "A:1" in out and "B:2" in out)
check("S4 마지막 체크 신선 - 경고 없음", "가격체크가" not in out)
check("S4 다음 수집 ETA 표시", "다음 수집" in out)

# ── S5: 작성자 랭킹 — analytics.ranking 그대로 재사용 확인 ──────────
fresh_db()
with db.connect(TEST_DB) as conn:
    for i in range(6):
        key = f"out{i}"
        db.upsert_level(conn, dict(
            signal_key=key, coin_symbol="SOL", ticker="KRW-SOL", direction="long",
            entry_usd=100, sl_usd=90, tp_usd=120, rr=2.0, grade="A", score=80,
            author="bob", collected_at=NOW - 200000))
        lid = conn.execute("SELECT id FROM levels WHERE signal_key=?", (key,)).fetchone()["id"]
        db.mark_touched(conn, [(lid, 100.0, NOW - 190000)], NOW - 190000, usdt_krw=1300)
        db.resolve_outcome(conn, lid, "hit", 120.0, "tp_sl", r_multiple=1.0, now=NOW - 180000)
    db.save_author_snapshot(conn, "2026-W29", "bob",
                            {"e_lb": 0.4, "neff_r": 6, "p_hat": 0.8, "neff_win": 6,
                             "wins": 6, "losses": 0}, now=NOW - 7 * 86400)
    db.save_author_snapshot(conn, "2026-W30", "bob",
                            {"e_lb": 0.8, "neff_r": 6, "p_hat": 0.85, "neff_win": 6,
                             "wins": 6, "losses": 0}, now=NOW)
code, out = capture(days=7)
check("S5 bob 랭킹 등재 + E_LB", "bob" in out and "+1.00" in out)
check("S5 스냅샷 추이 상승 화살표", "▲" in out)

# ── S6: 건강 경고 — 가격체크 정체 / 수집 정체 / ambiguous / 발송실패 ─
fresh_db()
with db.connect(TEST_DB) as conn:
    db.set_meta(conn, "last_check_at", str(NOW - 3600))       # 60분 전 -> 정체 경고
    db.set_meta(conn, "last_collect_at", str(NOW - 20 * 3600))  # 20시간 전(주기 4h*3=12h 초과)
    db.bump_daily_stats(conn, "2026-07-26", ambiguous_unresolved=2, ambiguous_magnified=1,
                        ambiguous_skipped=4, suppressed_send_fail=3)
code, out = capture(days=7)
check("S6 가격체크 정체 경고", "가격체크가" in out and "멈춰" in out)
check("S6 수집 정체 경고", "수집이 조용히 멈췄을 수 있습니다" in out)
check("S6 동시터치 판별불가 경고", "동시터치" in out and "판별불가" in out)
check("S6 발송실패 경고", "텔레그램 발송 실패 3건" in out)
# 2026-07-26 감사 minor: '판별불가'(체결내역을 보고도 못 가름)와 '미시도'(예산/OFF/
# 구간초과로 못 봄)를 갈라 표시한다 — 처방이 다르므로 한 숫자로 뭉치면 못 읽는다.
# 분모는 셋의 합(2+4+1=7) = 그 기간 발생한 동시터치 전체.
check("S6b 판별불가/미시도 분리 표시 + 분모는 동시터치 전체",
      "판별불가 2건" in out and "미시도 4건" in out and "/7건" in out)

# ── S6c: 미시도만 있어도 경고 — 예산/스위치 탓에 재검사를 못 한 것도 신뢰도 신호 ──
fresh_db()
with db.connect(TEST_DB) as conn:
    db.set_meta(conn, "last_check_at", str(NOW - 60))
    db.set_meta(conn, "last_collect_at", str(NOW - 3600))
    db.bump_daily_stats(conn, "2026-07-26", ambiguous_skipped=2)
code, out = capture(days=7)
check("S6c 미시도만 발생해도 경고(판별불가 0건이어도 침묵하지 않음)",
      "미시도 2건" in out and "판별불가 0건" in out and "이상 징후 없음" not in out)

# ── S6d: 새 컬럼이 아직 없는 구세대 DB — show_status 는 읽기 전용이라 스스로
# 마이그레이션할 수 없다(ALTER 불가). 값이 없으면 0으로 접히고 깨지지 않아야 한다.
fresh_db()
with db.connect(TEST_DB) as conn:
    conn.execute("DROP TABLE daily_stats")
    conn.execute("CREATE TABLE daily_stats (day_kst TEXT PRIMARY KEY, "
                 "touches_total INTEGER NOT NULL DEFAULT 0, "
                 "ambiguous_unresolved INTEGER NOT NULL DEFAULT 0, updated_at REAL)")
    conn.execute("INSERT INTO daily_stats (day_kst, touches_total, ambiguous_unresolved) "
                 "VALUES ('2026-07-26', 3, 1)")
code, out = capture(days=7)
check("S6d 구세대 DB(새 컬럼 없음)도 읽기전용 조회에서 안 깨짐",
      code == 0 and "판별불가 1건" in out and "미시도" not in out)

# ── S7: 이상 징후 전혀 없을 때 "이상 징후 없음" ──────────────────────
fresh_db()
with db.connect(TEST_DB) as conn:
    db.set_meta(conn, "last_check_at", str(NOW - 60))
    db.set_meta(conn, "last_collect_at", str(NOW - 3600))
code, out = capture(days=7)
check("S7 이상 징후 없음 표시", "이상 징후 없음" in out)

# ── S11: 가격체크 회차 공백 지표 (2026-07-27 기획 카드 #2 과제2) ─────────
# monitor.price_check._check_price_check_gap 이 기록하는 meta 값을 건강 상태
# 섹션에 한 줄 노출하는지 확인(값 자체의 계산 로직은 test_resilience.py 담당,
# 여긴 표시 위치·문구만 검증).
fresh_db()
with db.connect(TEST_DB) as conn:
    db.set_meta(conn, "last_check_at", str(NOW - 60))
    db.set_meta(conn, "last_collect_at", str(NOW - 3600))
    db.set_meta(conn, "last_price_check_gap_min", "3.0")
    db.set_meta(conn, "max_price_check_gap_min", "45.0")
code, out = capture(days=7)
check("S11 가격체크 회차 공백 한 줄 노출(직전/최장 둘 다)",
      "가격체크 회차 공백" in out and "직전 3분" in out and "역대 최장 45분" in out)

# ── S12: 공백 meta 가 아예 없는 구세대 DB — 줄 자체가 안 나오고(값 없음)
#         건강 판정에도 영향 없어야 한다(읽기 전용 조회에서 안 깨짐 확인) ──
fresh_db()
with db.connect(TEST_DB) as conn:
    db.set_meta(conn, "last_check_at", str(NOW - 60))
    db.set_meta(conn, "last_collect_at", str(NOW - 3600))
code, out = capture(days=7)
check("S12 공백 meta 없으면 줄 자체 미출력 + 이상 징후 없음 유지",
      "가격체크 회차 공백" not in out and "이상 징후 없음" in out)

# ── S13: 하트비트 / 스캔 워터마크 분리 (2026-07-27 2차 교차검토 M-A1) ──────
# last_cycle_at(회차가 깨어난 때) 과 last_check_at(어디까지 스캔했나) 이 갈라지면
# 그 자체가 "회차는 도는데 일을 못 하고 있다"는 신호다. 예전엔 한 키가 둘을 겸직해
# 업비트가 몇 시간 죽어도 화면이 '정상'으로 보이는 관측 사각지대였다.
fresh_db()
with db.connect(TEST_DB) as conn:
    db.set_meta(conn, "last_cycle_at", str(NOW - 60))         # 회차는 1분 전에도 돌았다
    db.set_meta(conn, "last_check_at", str(NOW - 3600))       # 스캔은 60분째 정체
    db.set_meta(conn, "last_collect_at", str(NOW - 3600))
code, out = capture(days=7)
check("S13 하트비트/워터마크 두 줄로 분리 표시",
      "마지막 회차" in out and "마지막 가격스캔" in out)
check("S13b 스캔 정체 경고 1줄(회차는 도는데 스캔을 못 한다)",
      "회차는 도는데 가격 스캔이" in out and "59분째 정체" in out)
check("S13c 회차 자체는 살아 있으므로 '멈춰 있습니다' 정지 경고는 뜨지 않는다",
      "멈춰 있습니다" not in out)

# S13d: 두 키가 나란히 신선하면 정체 경고 없음(정상 상태 회귀)
fresh_db()
with db.connect(TEST_DB) as conn:
    db.set_meta(conn, "last_cycle_at", str(NOW - 60))
    db.set_meta(conn, "last_check_at", str(NOW - 60))
    db.set_meta(conn, "last_collect_at", str(NOW - 3600))
code, out = capture(days=7)
check("S13d 두 키가 나란히 신선하면 정체 경고 없음 + 이상 징후 없음",
      "회차는 도는데" not in out and "이상 징후 없음" in out)

# S13e: last_cycle_at 이 없는 구세대/롤백 DB — 워터마크로 폴백하고, 폴백 상태에서는
# 두 키의 차이를 잴 수 없으므로 정체 경고도 내지 않는다(거짓 경고 방지).
fresh_db()
with db.connect(TEST_DB) as conn:
    db.set_meta(conn, "last_check_at", str(NOW - 60))
    db.set_meta(conn, "last_collect_at", str(NOW - 3600))
code, out = capture(days=7)
check("S13e 하트비트 키 부재 시 워터마크 폴백 - 거짓 정체 경고 없음",
      "회차는 도는데" not in out and "이상 징후 없음" in out)

# ── S8: --report 옵션 — 주간 리포트 텍스트가 함께 출력됨 ────────────
fresh_db()
code, out = capture(days=7, report=True)
check("S8 --report 헤더 포함", "주간 성적 리포트" in out)
check("S8 --report 없으면 헤더 미포함", "주간 성적 리포트" not in capture(days=7, report=False)[1])

# ── S9: 읽기 전용 연결 — 실제로 쓰기가 막히는지 sanity check ────────
fresh_db()
conn = show_status._ro_connect(TEST_DB)
try:
    threw = False
    try:
        conn.execute("INSERT INTO meta (key, value) VALUES ('x','y')")
        conn.commit()
    except sqlite3.OperationalError:
        threw = True
    check("S9 읽기전용 연결은 쓰기를 거부", threw)
finally:
    conn.close()

# ── S10: CLI 파서(main) 기본값/파싱 sanity — 실제 실행 없이 인자만 확인 ─
import argparse
check("S10 main 함수 존재", callable(show_status.main))


print()
print("PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
