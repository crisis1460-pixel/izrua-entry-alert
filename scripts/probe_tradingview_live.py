"""
TradingView 수집기 라이브 검증 스크립트.

사용:
  PYTHONIOENCODING=utf-8 python scripts/test_tradingview_live.py BTCUSD LINKUSD
옵션:
  --sleep 3.0        심볼 간 sleep (기본 3초)
  --timeout 15.0     요청 timeout
  --max-age-hours N  연령 필터 (기본 없음)
  --detail N         심볼당 앞 N건의 상세 페이지를 강제 방문해 상세 파싱 경로 검증
                     (정상 운영에서는 목록 description 이 전문이라 상세 방문 0회가 기본이므로,
                      상세 코드 경로는 이 프로브로만 증명 가능. 방문 간 3초 sleep)
  --show-example     parse_setup 성공 사례 1건 출력

요청 예산 주의: 심볼당 목록 1회 + (--detail N)회. 차단 방지를 위해 남발 금지.
차단 신호(blocked) 감지 시 남은 프로브/심볼을 즉시 전부 중단하고 부분 리포트만 출력한다
(모듈의 '즉시 포기' 정책 준수 — 차단 후 추가 요청은 밴을 확정·연장시킬 뿐).
"""

import argparse
import logging
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from collector import tradingview
from collector.extractor import parse_setup

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


def pct(n: int, total: int) -> str:
    return f"{100.0 * n / total:5.1f}%" if total else "  n/a"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("symbols", nargs="+", help="예: BTCUSD LINKUSD 또는 BTC LINK")
    ap.add_argument("--sleep", type=float, default=3.0)
    ap.add_argument("--timeout", type=float, default=15.0)
    ap.add_argument("--max-age-hours", type=float, default=None)
    ap.add_argument("--detail", type=int, default=0)
    ap.add_argument("--show-example", action="store_true")
    args = ap.parse_args()

    rows = []
    setup_examples = []
    detail_reports = []

    for si, symbol in enumerate(args.symbols):
        if si > 0:
            time.sleep(args.sleep)
        t0 = time.time()
        ideas = tradingview.fetch_ideas(symbol, timeout=args.timeout,
                                        max_age_hours=args.max_age_hours,
                                        max_detail_fetch=5)
        elapsed = time.time() - t0
        n = len(ideas)

        def filled(key):
            return sum(1 for i in ideas if i.get(key))

        desc_lens = [len(i["description"]) for i in ideas if i.get("description")]
        setups = 0
        for i in ideas:
            s = parse_setup(i.get("description") or "")
            if s:
                setups += 1
                if len(setup_examples) < 3:
                    setup_examples.append((symbol, i.get("title", "")[:60], s))

        rows.append({
            "symbol": symbol, "count": n, "sec": elapsed,
            "title": filled("title"), "desc": filled("description"),
            "author": filled("author"), "url": filled("url"),
            "time": sum(1 for i in ideas if i.get("published_at") is not None),
            "direction": filled("direction"),
            "desc_med": int(statistics.median(desc_lens)) if desc_lens else 0,
            "setups": setups,
        })

        # 목록 수집 중 차단 감지 → 남은 심볼로 요청을 더 쏘지 않고 즉시 중단
        if tradingview.is_blocked():
            print(f"\n[차단] {symbol} 수집 중 차단 신호 감지 - 남은 프로브/심볼 즉시 중단 "
                  f"(즉시 포기 정책, 부분 리포트 출력)")
            break

        # 상세 파싱 경로 강제 검증 (목록 description 과 대조)
        blocked_abort = False
        for idea in ideas[: max(0, args.detail)]:
            time.sleep(args.sleep)
            detail, blocked = tradingview._fetch_detail(idea["url"], args.timeout)
            d_desc = (detail or {}).get("description") or ""
            d_time = (detail or {}).get("published_at")
            match = "EXACT" if d_desc == idea["description"] else (
                "DIFF" if d_desc else "MISS")
            detail_reports.append({
                "symbol": symbol, "url": idea["url"][-40:], "blocked": blocked,
                "desc_len": len(d_desc), "desc_vs_list": match,
                "time_ok": d_time is not None,
                # 상세의 date_timestamp 는 소수점 초 포함 float(실측) → 1초 허용오차 비교
                "time_match": (
                    abs(d_time - idea["published_at"]) < 1.0
                    if d_time and idea.get("published_at") else False
                ),
            })
            if blocked:
                # 차단 신호 후 추가 요청 금지: 남은 프로브 + 이후 심볼 전체 즉시 중단
                blocked_abort = True
                break
        if blocked_abort:
            print("\n[차단] 상세 프로브에서 차단 신호 감지 - 남은 프로브/심볼 즉시 중단 "
                  "(즉시 포기 정책, 부분 리포트 출력)")
            break

    print()
    print("=== fetch_ideas 라이브 결과 ===")
    hdr = (f"{'symbol':<10}{'건수':>5}{'초':>7}{'title':>8}{'desc':>8}{'author':>8}"
           f"{'url':>8}{'시각':>8}{'방향':>8}{'desc중앙값':>10}{'setup':>7}")
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        c = r["count"]
        print(f"{r['symbol']:<10}{c:>5}{r['sec']:>7.1f}"
              f"{pct(r['title'], c):>8}{pct(r['desc'], c):>8}{pct(r['author'], c):>8}"
              f"{pct(r['url'], c):>8}{pct(r['time'], c):>8}{pct(r['direction'], c):>8}"
              f"{r['desc_med']:>9}자{r['setups']:>6}건")

    if detail_reports:
        print()
        print("=== 상세 페이지 파싱 검증 (강제 프로브) ===")
        for d in detail_reports:
            print(f"  {d['symbol']:<8} ...{d['url']:<42} blocked={d['blocked']} "
                  f"desc={d['desc_len']}자({d['desc_vs_list']}) "
                  f"time_ok={d['time_ok']} time_match={d['time_match']}")

    if args.show_example and setup_examples:
        print()
        print("=== parse_setup 성공 사례 ===")
        for sym, title, s in setup_examples:
            print(f"  [{sym}] {title}")
            print(f"    direction={s['direction']} entry={s['entry']} "
                  f"(range {s['entry_low']}~{s['entry_high']}) sl={s['sl']} tp={s['tp']} rr={s['rr']}")

    total = sum(r["count"] for r in rows)
    print()
    print(f"총 {total}건 수집. 0건 심볼: {[r['symbol'] for r in rows if r['count'] == 0] or '없음'}")
    return 0 if total > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
