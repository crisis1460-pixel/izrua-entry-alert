# -*- coding: utf-8 -*-
"""plan_S10_grade_recalibration.md 수치 재현 스크립트 (2026-07-31).

실행: py -3.14 izrua_company/query_0731_recalib_sim.py  (레포 루트 기준 아무 위치나 무방)

내용:
  1) 종결 74건 저장(수집시점) 등급 캘리브레이션 — 역전 검증
  2) 요소별 TP1 도달률 (rr유무/판정모드/TP거리/팔로워/작성자)
  3) 현행(2026-07-29) 산식 소급 재채점 — 터치 시점 근접도 20점 가정
  4) 재조정안 P0/안1/안2/안3 시뮬 (소급은 작성자 가점 LOO, 감시군은 live)
  5) 감시군 34건 알림량 영향 (min_grade=C 통과 + 마지막 TP<2% 억제 반영)

가정:
  - 소급 재채점의 가격근접도 = 20점 고정 (터치 순간 현재가=entry — 알림 필터가
    실제로 평가하는 조건과 동일. price_check.py 559행 regrade_current 참조)
  - TP1 도달 = outcome='hit' (timeboxed_win 은 분모만 — calibration.py 원칙)
"""
import json
import os
import sqlite3
import sys
from collections import defaultdict

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
from analytics.calibration import calibrate_grades, wilson_interval  # noqa: E402

DB = os.path.join(_ROOT, "data", "levels.db")
COLS = """id, coin_symbol, direction, entry_usd, sl_usd, tp_usd, rr, grade, score,
author, author_followers, mcap_rank, status, outcome, ambiguous, judgment_mode,
tp_ladder_count, tps_usd"""

con = sqlite3.connect(DB)
con.row_factory = sqlite3.Row
closed = [dict(r) for r in con.execute(f"SELECT {COLS} FROM levels WHERE outcome IS NOT NULL")]
watching = [dict(r) for r in con.execute(
    f"SELECT {COLS} FROM levels WHERE status IN ('watching','previewed')")]
con.close()
print(f"종결 {len(closed)}건 / 감시(watching+previewed) {len(watching)}건")


def is_hit(r):
    return r["outcome"] == "hit"


def tp_pct_of(r):
    e, t, d = r["entry_usd"], r["tp_usd"], r["direction"]
    if not (e and t and e > 0):
        return None
    return ((t - e) if d == "long" else (e - t)) / e * 100


def last_tp_pct(r):
    """price_check.py 578-600 의 마지막 TP<2% 억제 판정 재현."""
    e = r["entry_usd"]
    if not e or e <= 0:
        return None
    try:
        tps = json.loads(r["tps_usd"]) if r["tps_usd"] else []
    except (ValueError, TypeError):
        tps = []
    tps = tps if tps else ([r["tp_usd"]] if r["tp_usd"] else [])
    if not tps:
        return None
    last = tps[-1]
    return (last - e) / e * 100 if r["direction"] == "long" else (e - last) / e * 100


def tp_suppressed(r):
    p = last_tp_pct(r)
    return p is not None and 0 < p < 2.0


def show_cal(title, rows):
    cal = calibrate_grades(rows)
    parts = []
    for g in cal["order"]:
        b = cal["buckets"][g]
        rate = f"{b['hits']/b['n']:.0%}" if b["n"] else "-"
        parts.append(f"{g}:{b['hits']}/{b['n']}={rate}")
    print(f"  {title}: " + " ".join(parts))
    print("    위반:", [(v["higher"], v["lower"], "강" if v["significant"] else "약")
                      for v in cal["violations"]] or "없음")


# ── 1) 저장 등급 캘리브레이션 ──
print("\n[1] 저장(수집시점) 등급 캘리브레이션")
show_cal("종결 74", [(r["grade"], r["outcome"], r["ambiguous"]) for r in closed])

# ── 2) 요소별 도달률 ──
def table(title, keyfn, rows=None):
    rows = closed if rows is None else rows
    agg = defaultdict(lambda: [0, 0])
    for r in rows:
        agg[keyfn(r)][0] += 1
        agg[keyfn(r)][1] += is_hit(r)
    print(f"\n[2] {title}")
    for k in sorted(agg, key=lambda x: (x is None, x)):
        n, h = agg[k]
        print(f"    {str(k):20s} {h}/{n} = {h/n:.0%}" if n else k)


def tp_band(r):
    p = tp_pct_of(r)
    if p is None:
        return "TP없음"
    for hi, lab in [(2, "0-2%"), (3, "2-3%"), (5, "3-5%"), (8, "5-8%"),
                    (15, "8-15%"), (25, "15-25%")]:
        if p < hi:
            return lab
    return "25%+"


table("rr(SL) 유무", lambda r: "rr없음(SL미기재)" if r["rr"] is None else "rr있음")
table("판정모드", lambda r: r["judgment_mode"])
table("TP1 거리", tp_band)
sup = [r for r in closed if tp_suppressed(r)]
print(f"\n    마지막 TP<2% 억제 대상: 종결 {len(sup)}/74 (그중 hit {sum(map(is_hit, sup))}건)"
      f" / 감시 {sum(map(tp_suppressed, watching))}/34")
print("\n[2] 작성자별 (n>=3)")
agg = defaultdict(lambda: [0, 0])
for r in closed:
    agg[r["author"]][0] += 1
    agg[r["author"]][1] += is_hit(r)
for a, (n, h) in sorted(agg.items(), key=lambda x: -x[1][0]):
    if n >= 3:
        print(f"    {a:26s} {h}/{n} = {h/n:.0%}")

# ── 3~4) 산식 시뮬 ──
def fol_pts(f):
    f = f or 0
    return 10 if f >= 100000 else 9 if f >= 50000 else 8 if f >= 10000 else \
        5 if f >= 1000 else 3 if f >= 100 else 1


def completeness(r):
    he, hs, ht = bool(r["entry_usd"]), bool(r["sl_usd"]), bool(r["tp_usd"])
    if he and ht:
        return 30 if hs else 20
    return 8 if (he or ht) else 2


CUR = [(2, -6), (3, -4), (5, -2), (8, 12), (15, 20), (25, 25), (40, 18), (60, 9), (1e18, 3)]
FLAT = [(2, -6), (3, -4), (5, -2), (8, 12), (15, 12), (25, 8), (40, 4), (60, 0), (1e18, 0)]


def tp_pts(r, bands):
    p = tp_pct_of(r)
    if p is None:
        return 0.0
    for hi, pts in bands:
        if p < hi:
            return float(pts)
    return 0.0


def author_pts(row, loo):
    """안2/안3 실적 가점. loo=True 면 자기 자신 제외(소급 공정성)."""
    n = h = 0
    for r in closed:
        if r["author"] == row["author"] and (not loo or r["id"] != row["id"]):
            n += 1
            h += is_hit(r)
    if n < 5:
        return 0
    lo, _ = wilson_interval(h, n, z=1.28)
    return 15 if lo >= 0.55 else 10 if lo >= 0.40 else 5 if lo >= 0.25 else 0


def score_v(r, bands, use_author, loo):
    s = fol_pts(r["author_followers"]) + 20 + completeness(r) + tp_pts(r, bands)
    return s + (author_pts(r, loo) if use_author else 0)


def gcut(s):
    return "S" if s >= 85 else "A" if s >= 70 else "B" if s >= 55 else "C" if s >= 40 else "D"


elig = [r for r in closed if not tp_suppressed(r)]
print(f"\n[3~4] 산식 시뮬 (컷 S85/A70/B55/C40 고정, 스윙유효군 n={len(elig)})")
for name, bands, ua in [("P0 현행", CUR, False), ("안1 FLAT", FLAT, False),
                        ("안2 실적가점", CUR, True), ("안3 FLAT+실적(추천)", FLAT, True)]:
    print(f"\n  ── {name} ──")
    show_cal("종결74 소급(LOO)",
             [(gcut(score_v(r, bands, ua, True)), r["outcome"], r["ambiguous"]) for r in closed])
    show_cal("스윙유효군 소급",
             [(gcut(score_v(r, bands, ua, True)), r["outcome"], r["ambiguous"]) for r in elig])
    dist = defaultdict(int)
    eff = 0
    for r in watching:
        g = gcut(score_v(r, bands, ua, False))
        dist[g] += 1
        eff += g in "SABC" and not tp_suppressed(r)
    gpass = sum(dist[g] for g in "SABC")
    print("    감시34 분포:", " ".join(f"{g}:{dist[g]}" for g in "SABCD"),
          f"| >=C {gpass}/34 | TP억제 제외 실효 {eff}/34")

# 39점 군집 확인 (현행 산식이 최우수 군집을 D로 두는 지점)
n39 = [r for r in closed if round(score_v(r, CUR, False, True)) == 39]
print(f"\n[참고] 현행 산식 터치재채점 39점(=C컷 1점 미달) 군집: {len(n39)}건, "
      f"hit {sum(map(is_hit, n39))}건")
diff = sum(1 for r in closed if gcut(score_v(r, CUR, False, True)) != (r["grade"] or "?"))
print(f"[참고] 저장등급 vs 현행산식 터치재채점 불일치: {diff}/74")
