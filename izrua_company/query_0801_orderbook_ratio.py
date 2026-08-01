# -*- coding: utf-8 -*-
"""오더북 매수/매도 잔량비(touch_bid_ask_ratio) vs 적중(outcome) 상관관계 사후검증
(research_2026-08-01_internal_features.md 영역 2, 우선순위 1위).

실행: py -3.14 izrua_company/query_0801_orderbook_ratio.py

배경: 카드 #19(2026-07-26)로 터치 시점 호가 매수/매도 잔량비를 기록만 해왔고
(코드 주석: "순수 로깅 컬럼 — 알림·필터·판정 어디에도 쓰이지 않는다"), 실제로
분석에 쓴 적이 없다. 이 스크립트는 그 데이터를 처음으로 조회해 "ratio가 높을수록
(매수잔량 우위) 실제로 hit율이 높은가"를 확인한다. 순수 조회 — DB 쓰기 없음.
"""
import os
import sqlite3
import sys
from collections import defaultdict

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(_ROOT, "data", "levels.db")

# 프로덕션 DB 읽기 전용 원칙 — mode=ro + backup 인메모리 사본만 조회 (0731 스크립트와 동일 패턴)
_src = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
con = sqlite3.connect(":memory:")
_src.backup(con)
_src.close()
con.row_factory = sqlite3.Row

rows = [dict(r) for r in con.execute(
    "SELECT id, coin_symbol, touched_at, touch_bid_ask_ratio, outcome, r_multiple, "
    "ambiguous, grade FROM levels WHERE touch_bid_ask_ratio IS NOT NULL "
    "ORDER BY touched_at"
)]
con.close()

print(f"오더북 비율 기록 보유 터치: 총 {len(rows)}건")
closed = [r for r in rows if r["outcome"] is not None]
unresolved = [r for r in rows if r["outcome"] is None]
print(f"  종결 {len(closed)}건 / 미종결(아직 판정 대기) {len(unresolved)}건\n")

print("── 전체 목록 ──")
for r in rows:
    ratio = r["touch_bid_ask_ratio"]
    status = r["outcome"] or "미종결"
    rm = f"R={r['r_multiple']:.2f}" if r["r_multiple"] is not None else ""
    print(f"  {r['coin_symbol']:6s} ratio={ratio:5.2f}  {status:8s} {rm}  등급={r['grade']}")

if len(closed) < 10:
    print(f"\n⚠️ 종결 표본 {len(closed)}건 — 상관관계를 판단하기엔 아직 이르다. "
          f"research_2026-08-01_internal_features.md 권장대로 20건+ 쌓인 뒤 재실행 권장.")
else:
    def is_hit(r):
        return r["outcome"] == "hit"

    # 경험칙(리서치 영역2): ratio>=1.5 매수우위 강, 0.8~1.5 균형, <0.8 매도우위 강
    def bucket(r):
        v = r["touch_bid_ask_ratio"]
        return "매도우위(<0.8)" if v < 0.8 else "균형(0.8~1.5)" if v < 1.5 else "매수우위(1.5+)"

    print("\n── 구간별 hit율 (종결 표본만) ──")
    agg = defaultdict(lambda: [0, 0])
    for r in closed:
        agg[bucket(r)][0] += 1
        agg[bucket(r)][1] += is_hit(r)
    for k in ["매도우위(<0.8)", "균형(0.8~1.5)", "매수우위(1.5+)"]:
        n, h = agg[k]
        print(f"    {k:16s} {h}/{n} = {h/n:.0%}" if n else f"    {k:16s} 표본없음")

print(f"\n오더북 기록 보유 {len(rows)}건 중 미종결 {len(unresolved)}건 — "
      f"판정창(기본 168h=7일)이 아직 안 찼을 가능성이 높다. 결론은 재실행 시점 재확인.")
