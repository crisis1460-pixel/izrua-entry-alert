# storage/audit_dump 의 misses 실패 분류 + post-age 버킷 적중률 테스트
# (2026-08-15 Tier2 #9·#10 — 내부 통계 전용, 알림 미출력. 오프라인 임시 DB만 사용.)
import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from storage import audit_dump, db

# init_db 의 주간 감사 훅이 테스트 중 돌지 않게 차단 (test_weekly_report 와 동일 패턴)
audit_dump.SUPPRESSED = True

ok = True


def check(name, cond):
    global ok
    print(("✅" if cond else "❌"), name)
    ok = ok and cond


now = 1_800_000_000.0
DAY = 86400.0
HOUR = 3600.0

M_DB = "cache/_test_audit_misses.db"
M_DIR = Path("cache/_test_audit_misses_out")
P_DB = "cache/_test_audit_postage.db"
R_DB = "cache/_test_audit_rawcols.db"

for _p in (M_DB, P_DB, R_DB, str(M_DIR)):
    if os.path.isdir(_p):
        shutil.rmtree(_p)
    elif os.path.exists(_p):
        os.remove(_p)


def _ins(conn, key, **kw):
    cols = ["signal_key", "coin_symbol", "ticker", "direction", "status", "collected_at"]
    vals = [key, kw.pop("coin_symbol", "SOL"), "KRW-SOL", "long",
            kw.pop("status", "touched"), kw.pop("collected_at", now - 3 * DAY)]
    for k, v in kw.items():
        cols.append(k)
        vals.append(v)
    conn.execute(f"INSERT INTO levels ({','.join(cols)}) VALUES "
                 f"({','.join('?' for _ in cols)})", vals)


# ── M: 실패 분류(_compute_misses_stats) ──────────────────────────────────
db.init_db(M_DB)
with db.connect(M_DB) as conn:
    # m1: mfe 0.9 → 즉시반전. touch_* 스냅샷 우선 확인용(grade B→touch A, score 50→70)
    _ins(conn, "m1", coin_symbol="SOL", author="A1", outcome="miss",
         touched_at=now - 2 * DAY, resolved_at=now - 1 * DAY,
         mfe_pct=0.9, mae_pct=-3.0, grade="B", touch_grade="A",
         score=50, touch_score=70,
         touch_supply_verdict="ok", touch_position_verdict="good")
    # m2: mfe 2.0(경계) → 이익반납. touch_grade 없음 → grade 폴백
    _ins(conn, "m2", coin_symbol="ETH", author="A2", outcome="miss",
         touched_at=now - 3 * DAY, resolved_at=now - 2 * DAY + HOUR,
         mfe_pct=2.0, mae_pct=-2.5, grade="C", score=40)
    # m3: mfe 1.5 → 중간 (timeboxed_loss 도 패배로 포함)
    _ins(conn, "m3", coin_symbol="XRP", author="A1", outcome="timeboxed_loss",
         touched_at=now - 4 * DAY, resolved_at=now - 3 * DAY, mfe_pct=1.5)
    # m4: mfe 없음 → 판정불가. touched_at 도 없음(섀도) → time_to_fail_h None
    _ins(conn, "m4", coin_symbol="ADA", author="A3", outcome="miss",
         resolved_at=now - 4 * DAY)
    # m5: 창 밖 — 8일 전 종결 → 제외
    _ins(conn, "m5", coin_symbol="DOT", author="A1", outcome="miss",
         touched_at=now - 9 * DAY, resolved_at=now - 8 * DAY, mfe_pct=0.5)
    # m6: mfe 1.0(경계) → 중간 (1.0 은 즉시반전이 아니다)
    _ins(conn, "m6", coin_symbol="DOGE", author="A2", outcome="miss",
         touched_at=now - 5 * DAY, resolved_at=now - 5 * DAY + 30 * HOUR, mfe_pct=1.0)
    # w1: 승리건 — misses 에 안 잡혀야 함
    _ins(conn, "w1", coin_symbol="BTC", author="A1", outcome="hit",
         touched_at=now - 2 * DAY, resolved_at=now - 1 * DAY, mfe_pct=5.0)

with db.connect(M_DB) as conn:
    ms = audit_dump._compute_misses_stats(conn, since_epoch=now - 7 * DAY)

check("M1 창 내 패배건만 집계(5건) — 승리·8일 전 종결 제외", ms["total"] == 5)
check("M2 분류 경계: 0.9→즉시반전 / 2.0→이익반납 / 1.5·1.0→중간 / None→판정불가",
      ms["class_counts"] == {"즉시반전": 1, "이익반납": 1, "중간": 2, "판정불가": 1})
by_coin = {s["coin"]: s for s in ms["signals"]}
check("M3 창 밖(m5=DOT)·승리(w1=BTC)는 목록에도 없음",
      "DOT" not in by_coin and "BTC" not in by_coin and len(ms["signals"]) == 5)
check("M4 개별 분류 태깅",
      by_coin["SOL"]["failure_class"] == "즉시반전"
      and by_coin["ETH"]["failure_class"] == "이익반납"
      and by_coin["XRP"]["failure_class"] == "중간"
      and by_coin["DOGE"]["failure_class"] == "중간"
      and by_coin["ADA"]["failure_class"] == "판정불가")
check("M5 touch_* 스냅샷 우선(grade A, score 70) + 폴백(ETH→C)",
      by_coin["SOL"]["grade"] == "A" and by_coin["SOL"]["score"] == 70
      and by_coin["ETH"]["grade"] == "C" and by_coin["ETH"]["score"] == 40)
check("M6 time_to_fail_h 소수 1자리(24.0h·25.0h·30.0h) + 섀도(ADA)는 None",
      by_coin["SOL"]["time_to_fail_h"] == 24.0
      and by_coin["ETH"]["time_to_fail_h"] == 25.0
      and by_coin["DOGE"]["time_to_fail_h"] == 30.0
      and by_coin["ADA"]["time_to_fail_h"] is None)
check("M7 수급/자리 판정 동봉", by_coin["SOL"]["supply_verdict"] == "ok"
      and by_coin["SOL"]["position_verdict"] == "good"
      and by_coin["ETH"]["supply_verdict"] is None)
check("M8 최근 종결 순 정렬(resolved_at 내림차순)",
      [s["coin"] for s in ms["signals"]] == ["SOL", "ETH", "XRP", "DOGE", "ADA"])

# M9: 목록 상한 30 — 창 내 패배 35건 추가 → signals 30건, class_counts 는 전건(40)
with db.connect(M_DB) as conn:
    for i in range(35):
        _ins(conn, f"cap{i}", coin_symbol=f"C{i}", author="A9", outcome="miss",
             touched_at=now - DAY, resolved_at=now - HOUR - i, mfe_pct=0.5)
with db.connect(M_DB) as conn:
    ms2 = audit_dump._compute_misses_stats(conn, since_epoch=now - 7 * DAY)
check("M9 목록은 최근 30건으로 상한, 요약은 전건 집계(총 40)",
      len(ms2["signals"]) == 30 and ms2["total"] == 40
      and sum(ms2["class_counts"].values()) == 40)
check("M10 상한 시 최근 종결건 우선(cap0 포함, 가장 오래된 m4/ADA 탈락)",
      ms2["signals"][0]["coin"] == "C0" and "ADA" not in
      {s["coin"] for s in ms2["signals"]})

# ── P: post-age 버킷(_compute_post_age_stats) ────────────────────────────
db.init_db(P_DB)
with db.connect(P_DB) as conn:
    # p1: 수집→터치 20h + 글나이 300분(5h) = 25h → 24-72h (post_age_minutes 가산 검증)
    _ins(conn, "p1", collected_at=now - 30 * HOUR, touched_at=now - 10 * HOUR,
         post_age_minutes=300.0, outcome="hit", resolved_at=now)
    # p2: 5h, post_age_minutes NULL → <24h (COALESCE 0)
    _ins(conn, "p2", collected_at=now - 10 * HOUR, touched_at=now - 5 * HOUR,
         outcome="miss", resolved_at=now)
    # p3: 23h + 60분 = 24h → 경계는 위 버킷(24-72h)
    _ins(conn, "p3", collected_at=now - 24 * HOUR, touched_at=now - 1 * HOUR,
         post_age_minutes=60.0, outcome="timeboxed_win", resolved_at=now)
    # p4: 100h → 72-120h
    _ins(conn, "p4", collected_at=now - 101 * HOUR, touched_at=now - 1 * HOUR,
         outcome="timeboxed_loss", resolved_at=now)
    # p5: 130h → 120h+
    _ins(conn, "p5", collected_at=now - 131 * HOUR, touched_at=now - 1 * HOUR,
         outcome="hit", resolved_at=now)
    # 진행 중(outcome NULL)·미터치 — 제외 대상
    _ins(conn, "p6", collected_at=now - 5 * HOUR, touched_at=now - 1 * HOUR)
    _ins(conn, "p7", status="watching", collected_at=now - 5 * HOUR)

with db.connect(P_DB) as conn:
    pa = audit_dump._compute_post_age_stats(conn)

check("P1 버킷 4개 모두 존재(빈 버킷도 자기기술)",
      set(pa) == {"<24h", "24-72h", "72-120h", "120h+"})
check("P2 post_age_minutes 가산: 20h+5h=25h → 24-72h / 경계 24.0h 도 24-72h",
      pa["24-72h"]["n"] == 2 and pa["24-72h"]["wins"] == 2
      and pa["24-72h"]["win_rate"] == 1.0)
check("P3 <24h: miss 1건 → win_rate 0.0",
      pa["<24h"] == {"n": 1, "wins": 0, "win_rate": 0.0})
check("P4 72-120h: timeboxed_loss → 0.0 / 120h+: hit → 1.0",
      pa["72-120h"] == {"n": 1, "wins": 0, "win_rate": 0.0}
      and pa["120h+"] == {"n": 1, "wins": 1, "win_rate": 1.0})
check("P5 미종결·미터치는 분모에서 제외(총 n=5)",
      sum(v["n"] for v in pa.values()) == 5)

# ── R: 옵션 컬럼 없는 구버전 스키마에서도 죽지 않는다 ────────────────────
# touch_btc_regime(병행 작업으로 추가 예정)·mfe_pct·touch_* 등이 전혀 없는
# 최소 테이블 — PRAGMA 게이트가 NULL 로 눕혀야 한다.
raw = sqlite3.connect(R_DB)
raw.row_factory = sqlite3.Row
raw.execute("""CREATE TABLE levels (
    id INTEGER PRIMARY KEY, coin_symbol TEXT, author TEXT, outcome TEXT,
    collected_at REAL, touched_at REAL, resolved_at REAL, grade TEXT, score INTEGER)""")
raw.execute("INSERT INTO levels (coin_symbol, author, outcome, collected_at, "
            "touched_at, resolved_at, grade, score) VALUES (?,?,?,?,?,?,?,?)",
            ("SOL", "A1", "miss", now - 25 * HOUR, now - 2 * HOUR, now - HOUR, "B", 55))
try:
    ms_r = audit_dump._compute_misses_stats(raw, since_epoch=now - 7 * DAY)
    pa_r = audit_dump._compute_post_age_stats(raw)
    graceful = True
except Exception as e:
    print("   예외:", type(e).__name__, e)
    graceful = ms_r = pa_r = False
raw.close()
check("R1 옵션 컬럼 전무해도 예외 없이 동작", bool(graceful))
if graceful:
    s = ms_r["signals"][0]
    check("R2 mfe 없음 → 판정불가, grade/score 는 기본 컬럼 폴백",
          s["failure_class"] == "판정불가" and s["grade"] == "B" and s["score"] == 55
          and s["mfe_pct"] is None and s["btc_regime"] is None)
    check("R3 post_age_minutes 없어도 버킷 계산(23h → <24h)",
          pa_r["<24h"]["n"] == 1 and pa_r["<24h"]["win_rate"] == 0.0)

# ── J: run_weekly_audit 통합 — grade_stats JSON 에 두 키가 실린다 ─────────
with db.connect(M_DB) as conn:
    res = audit_dump.run_weekly_audit(conn, M_DB, now=now, out_dir=M_DIR)
stats_files = [f for f in res["files"] if f.startswith("grade_stats_")]
check("J1 grade_stats JSON 파일 생성", len(stats_files) == 1)
payload = json.loads((M_DIR / stats_files[0]).read_text(encoding="utf-8"))
check("J2 payload 에 misses·post_age_stats 키 + 기존 키 유지",
      "misses" in payload and "post_age_stats" in payload
      and "grade_hit_rates" in payload and "touch_time_analysis" in payload)
check("J3 misses 내용이 헬퍼 결과와 일치(총 40, 목록 30 상한)",
      payload["misses"]["total"] == 40 and len(payload["misses"]["signals"]) == 30
      and payload["misses"]["class_counts"]["즉시반전"] == 36)
check("J4 post_age_stats 버킷 4개 + 표준 JSON(json.loads 통과로 확인됨)",
      set(payload["post_age_stats"]) == {"<24h", "24-72h", "72-120h", "120h+"})

for _p in (M_DB, P_DB, R_DB):
    if os.path.exists(_p):
        os.remove(_p)
shutil.rmtree(M_DIR, ignore_errors=True)

print()
n_checks = 22
print(f"{'전체 통과' if ok else '실패 있음'} ({n_checks}개 체크)")
sys.exit(0 if ok else 1)
