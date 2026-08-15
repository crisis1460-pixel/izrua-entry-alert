# scripts.show_status 피드백 Wilson 집계 + scripts.analyze_touch_quality 회귀 테스트
# (2026-08-15 Tier2 #11·#12). 오프라인 — 네트워크/텔레그램 호출 없음, 임시 DB만
# 사용(프로덕션 data/levels.db 는 절대 건드리지 않는다). test_show_status.py 관례.
import io
import math
import os
import sys
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import logging
logging.basicConfig(level=logging.CRITICAL)

from analytics import calibration
from storage import db
from scripts import analyze_touch_quality as atq
from scripts import show_status

TEST_DB = "cache/_test_feedback_stats.db"
NOW = 1_800_000_000.0

ok = True


def check(name, cond):
    global ok
    print(("✅" if cond else "❌"), name)
    ok = ok and cond


def fresh_db():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    db.init_db(TEST_DB)


def add_level(conn, key, grade="B", author="alice", touch_grade=None):
    """레벨 1건 삽입 후 id 반환. touch_grade 는 직접 UPDATE(기록 전용 컬럼)."""
    db.upsert_level(conn, dict(
        signal_key=key, coin_symbol="ETH", ticker="KRW-ETH", direction="long",
        entry_usd=100, sl_usd=90, tp_usd=120, rr=2.0, grade=grade, score=70,
        author=author, collected_at=NOW - 1000))
    lid = conn.execute("SELECT id FROM levels WHERE signal_key=?", (key,)).fetchone()["id"]
    if touch_grade is not None:
        conn.execute("UPDATE levels SET touch_grade=? WHERE id=?", (touch_grade, lid))
    return lid


def capture_feedback():
    """읽기 전용 연결로 피드백 섹션만 캡처."""
    conn = show_status._ro_connect(TEST_DB)
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            show_status.print_alert_feedback(conn)
    finally:
        conn.close()
    return buf.getvalue()


# ── W: Wilson 수학 손계산 대조 ────────────────────────────────────────
# 10표 중 8👍, z=1.28 의 Wilson 단측 하한 — 공식을 여기 그대로 다시 써서
# (라이브러리 결과와 독립으로) 손계산 기대값을 만든다:
#   center = (p + z²/2n) / (1 + z²/n),  half = z/(1+z²/n)·√(p(1−p)/n + z²/4n²)
def _hand_wilson_lo(h, n, z):
    p = h / n
    zz = z * z
    denom = 1.0 + zz / n
    center = (p + zz / (2 * n)) / denom
    half = (z / denom) * math.sqrt(p * (1 - p) / n + zz / (4 * n * n))
    return center - half

hand = _hand_wilson_lo(8, 10, 1.28)
check("W1 손계산 공식 자체 sanity (8/10, z=1.28 → ≈0.6019)", abs(hand - 0.6018584) < 1e-4)
lib_lo, _ = calibration.wilson_interval(8, 10, z=show_status.FEEDBACK_Z)
check("W2 show_status 가 쓰는 정본(wilson_interval, z=1.28) = 손계산", abs(lib_lo - hand) < 1e-9)
check("W3 z 관례 = 1.28 (rank_z·작성자 실적 게이트와 동일)", show_status.FEEDBACK_Z == 1.28)

# ── F1: 피드백 0건 — 단독 안내 문구 ──────────────────────────────────
fresh_db()
out = capture_feedback()
check("F1 피드백 없음 문구", "피드백 없음 (버튼 도입 08-15)" in out)
check("F1b 0건이면 버킷 표 없음", "등급별" not in out)

# ── F2: 10표 버킷 — 판단 표기 + Wilson LB 값 일치 + touch_grade 우선 ──
# 레벨은 grade='C' 지만 touch_grade='B' → 표는 전부 B 버킷으로 잡혀야 한다
# (피드백은 '그 알림'에 대한 반응이라 터치 시점 등급이 맞는 축).
fresh_db()
with db.connect(TEST_DB) as conn:
    lid = add_level(conn, "fb1", grade="C", author="alice", touch_grade="B")
    for i in range(8):
        db.record_feedback(conn, str(lid), "up", f"u{i}", now=NOW)
    for i in range(8, 10):
        db.record_feedback(conn, str(lid), "down", f"u{i}", now=NOW)
out = capture_feedback()
check("F2 전체 집계(10표·👍8)", "전체 10표" in out and "👍 8표 (80%)" in out)
check("F2b 전체 Wilson 80% 하한 = 손계산 0.60", f"Wilson 80% 하한 {hand:.2f}" in out)
_b_line = [ln for ln in out.splitlines() if ln.strip().startswith("B")]
check("F2c B 버킷(=touch_grade)에 10표 판단 표기",
      len(_b_line) == 1 and "10표" in _b_line[0] and f"Wilson LB {hand:.2f}" in _b_line[0])
check("F2d 수집 시점 grade('C') 버킷은 없음 — touch_grade 우선 조인",
      not any(ln.strip().startswith("C") for ln in out.splitlines()))
check("F2e 작성자 버킷(alice)도 판단 표기", "alice" in out and "👍 80%(8/10)" in out)
check("F2f 30표+ 자동조치 정책 캡션(표기 전용)", "30표+" in out and "표기 전용" in out)

# ── F3: 10표 미만 버킷 — 판단 유보 ───────────────────────────────────
fresh_db()
with db.connect(TEST_DB) as conn:
    lid = add_level(conn, "fb2", grade="A", author="bob", touch_grade="A")
    for i in range(3):
        db.record_feedback(conn, str(lid), "up", f"v{i}", now=NOW)
out = capture_feedback()
check("F3 10표 미만은 표본 부족(n=3) — LB 미표기",
      "표본 부족(n=3)" in out and "Wilson LB" not in out)

# ── F4: 레벨과 조인 안 되는 ref — 전체 집계엔 포함, (미상) 버킷 ───────
fresh_db()
with db.connect(TEST_DB) as conn:
    db.record_feedback(conn, "99999", "up", "w1", now=NOW)
out = capture_feedback()
check("F4 조인 불가 ref 도 전체 1표 + (미상) 버킷", "전체 1표" in out and "(미상)" in out)

# ── F5: show_status.run 전체 경로에 섹션 등장 + 다른 섹션 안 깨짐 ─────
fresh_db()
buf = io.StringIO()
with redirect_stdout(buf):
    code = show_status.run(TEST_DB, 7, False, now=NOW)
out = buf.getvalue()
check("F5 run() 경로에 피드백 섹션 + 정상 종료", code == 0 and "알림 피드백 (시험)" in out)


# ═════════ analyze_touch_quality ═════════

def add_touch_row(conn, key, closed_below, outcome, pen=None,
                  ret_24h=None, mfe=None, mae=None):
    """터치 품질 기록이 있는 종결 표본 1건. 판정·스냅샷 파이프라인을 거치지 않고
    분석 대상 컬럼만 직접 UPDATE — 이 스크립트가 읽는 축만 검증한다."""
    lid = add_level(conn, key)
    conn.execute(
        "UPDATE levels SET touched_at=?, outcome=?, touch_closed_below=?, "
        "touch_penetration_pct=?, ret_24h=?, mfe_pct=?, mae_pct=? WHERE id=?",
        (NOW - 90000, outcome, closed_below, pen, ret_24h, mfe, mae, lid))
    return lid


def run_analyzer(db_path):
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = atq.main(["--db", db_path])
    return code, buf.getvalue()


# ── A1: 버킷 경계 — 0.5/1.5 는 가운데 구간, NULL 은 구간 없음 ─────────
check("A1 관통 깊이 버킷 경계",
      atq.penetration_bucket(0.4) == "<0.5%"
      and atq.penetration_bucket(0.5) == "0.5~1.5%"
      and atq.penetration_bucket(1.5) == "0.5~1.5%"
      and atq.penetration_bucket(1.6) == ">1.5%"
      and atq.penetration_bucket(None) is None)

# ── A2: 그룹 분리 + 통계 손계산 ──────────────────────────────────────
# 꼬리(0): hit, miss, hit → 2/3승, ret24h [1,-2,3] 중앙값 1.0
# 종가(1): timeboxed_win, timeboxed_loss → 1/2승
fresh_db()
with db.connect(TEST_DB) as conn:
    add_touch_row(conn, "t1", 0, "hit", pen=0.2, ret_24h=1.0, mfe=2.0, mae=-1.0)
    add_touch_row(conn, "t2", 0, "miss", pen=0.7, ret_24h=-2.0, mfe=0.5, mae=-3.0)
    add_touch_row(conn, "t3", 0, "hit", pen=2.0, ret_24h=3.0, mfe=4.0, mae=-0.5)
    add_touch_row(conn, "t4", 1, "timeboxed_win", pen=1.0, ret_24h=0.5)
    add_touch_row(conn, "t5", 1, "timeboxed_loss", pen=None, ret_24h=-1.0)
    # 분석 제외 대상 2종: 미종결 / 터치 품질 미기록(closed_below NULL)
    add_touch_row(conn, "t6", 0, None, ret_24h=9.9)
    add_touch_row(conn, "t7", None, "hit", ret_24h=9.9)
conn = atq.connect_ro(TEST_DB)
rows = atq.load_rows(conn)
conn.close()
check("A2 표본 선별 — 종결 + closed_below 기록분만 5건", len(rows) == 5)
g = atq.split_groups(rows)
check("A2b 그룹 분리 3/2건", len(g["wick"]) == 3 and len(g["closed"]) == 2)
sw = atq.group_stats(g["wick"])
sc = atq.group_stats(g["closed"])
check("A2c 꼬리 그룹 손계산 — 2/3승·ret24h 중앙값 1.0·MFE 중앙값 2.0",
      sw["wins"] == 2 and abs(sw["rate"] - 2 / 3) < 1e-9
      and sw["med_ret_24h"] == 1.0 and sw["med_mfe"] == 2.0 and sw["med_mae"] == -1.0)
check("A2d 종가 그룹 손계산 — timeboxed_win 은 승 (1/2)",
      sc["wins"] == 1 and sc["rate"] == 0.5)
check("A2e 그룹 Wilson LB = 정본 wilson_interval(z=1.28)",
      abs(sw["wilson_lo"] - calibration.wilson_interval(2, 3, z=1.28)[0]) < 1e-12)
check("A2f MFE/MAE 전무 그룹 중앙값 None (0 으로 뭉개지 않음)",
      sc["med_mfe"] is None and sc["med_mae"] is None)

# ── A3: n<20/그룹 — 판정 유보 문구 + --db 플래그 경로 ────────────────
code, out = run_analyzer(TEST_DB)
check("A3 --db 플래그로 임시 DB 분석 + 정상 종료",
      code == 0 and TEST_DB.replace("/", os.sep) in out and "읽기 전용" in out)
check("A3b 표본 부족 판정 유보 문구",
      "표본 부족 — 수집 시작 2026-08-16, 판정 대기 (필요 n=20/그룹" in out)
check("A3c 그룹·버킷 표는 소표본에도 표기(관찰용)",
      "꼬리 스침(0)" in out and "종가 이탈(1)" in out and "0.5~1.5%" in out)
check("A3d 깊이 미기록 건수 별도 표기", "깊이 미기록 1건" in out)

# ── A4: 양 그룹 n>=20 — 판정 문구 발동 ───────────────────────────────
fresh_db()
with db.connect(TEST_DB) as conn:
    for i in range(20):  # 꼬리: 8/20 승
        add_touch_row(conn, f"w{i}", 0, "hit" if i < 8 else "miss", pen=0.3)
    for i in range(20):  # 종가: 16/20 승
        add_touch_row(conn, f"c{i}", 1, "hit" if i < 16 else "miss", pen=1.0)
code, out = run_analyzer(TEST_DB)
# 손계산: 종가 LB = wilson(16,20,1.28) 하한 ≈ 0.655 > 꼬리 승률 0.40 → 종가 우위
_lo_c = calibration.wilson_interval(16, 20, z=1.28)[0]
check("A4 판정 발동 전제 손계산 sanity (종가 LB > 꼬리 승률)", _lo_c > 0.40)
check("A4b n>=20/그룹이면 판정 문구 — 종가 이탈 우위 + 승률차 +40.0%p",
      "판정: 종가 이탈(재확인 대기) 우위" in out and "+40.0%p" in out
      and "표본 부족" not in out)
check("A4c 내부 전용 캡션(알림·필터·등급 미반영)", "알림·필터·등급 미반영" in out)

# ── A5: DB 없음 — 에러 코드 1 ────────────────────────────────────────
missing = "cache/_test_feedback_stats_missing.db"
if os.path.exists(missing):
    os.remove(missing)
buf = io.StringIO()
with redirect_stdout(buf):
    code = atq.main(["--db", missing])
check("A5 DB 없음 → 종료코드 1 + 메시지", code == 1 and "DB 없음" in buf.getvalue())

# ── A6: 읽기 전용 연결 — 실제로 쓰기가 막히는지 sanity check ─────────
fresh_db()
conn = atq.connect_ro(TEST_DB)
try:
    import sqlite3
    threw = False
    try:
        conn.execute("INSERT INTO meta (key, value) VALUES ('x','y')")
        conn.commit()
    except sqlite3.OperationalError:
        threw = True
    check("A6 읽기전용 연결은 쓰기를 거부", threw)
finally:
    conn.close()


print()
print("PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
