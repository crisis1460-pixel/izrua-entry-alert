#!/usr/bin/env python3
"""
수집 잡 엔트리포인트 (4시간마다 — cron-job.org → GitHub Actions).

흐름: 유니버스(top200∩업비트KRW) → 심볼별 TradingView 아이디어 → entry 추출
→ 등급 산정 → 레벨 DB 저장. entry 있는 글은 전부 저장(알림 필터는 가격체크 잡 담당).

사용:
  python scripts/run_collect.py                  # 전체 유니버스
  python scripts/run_collect.py --symbols BTC,LINK   # 지정 심볼만 (스모크 테스트)
  python scripts/run_collect.py --limit 5        # 상위 N개만
"""

import argparse
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

from collector import coingecko, tradingview, watcher_stats
from collector.extractor import judgment_window_hours, parse_setup, parse_timeframe_hours
from collector.grading import calculate_grade
from config import settings
from storage import db

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("alert.collect")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", help="콤마구분 심볼 (지정 시 유니버스 대신 사용)")
    ap.add_argument("--limit", type=int, help="유니버스 상위 N개만")
    args = ap.parse_args()

    t0 = time.time()
    timeout = settings.get("http_timeout_sec")
    db_path = settings.get("db_path")
    db.init_db(db_path)
    tradingview.reset_detail_budget()

    universe = coingecko.build_universe()
    logger.info("유니버스 %d개", len(universe))

    # 심볼 동명이인 가드 (2026-07-24 감사): CoinGecko 코인 A 와 업비트의 같은 심볼
    # 다른 코인 B 가 묶이면 엉뚱한 자산에 레벨을 붙인다 — CG 달러가 × 환율 vs 업비트
    # 원화가가 ±40% 넘게 어긋나면 다른 자산으로 보고 이번 주기 제외.
    try:
        from monitor import upbit as upbit_api
        tickers = [u["ticker"] for u in universe] + ["KRW-USDT"]
        krw_prices = upbit_api.fetch_prices(tickers, timeout)
        usdt_krw = krw_prices.get("KRW-USDT")
        if usdt_krw:
            kept = []
            for u in universe:
                upbit_p, cg_p = krw_prices.get(u["ticker"]), u.get("price_usd")
                if upbit_p and cg_p:
                    expected = cg_p * usdt_krw
                    if abs(upbit_p - expected) / expected > 0.40:
                        logger.warning("동명이인 의심 제외: %s (업비트 %.6g원 vs 예상 %.6g원)",
                                       u["symbol"], upbit_p, expected)
                        continue
                kept.append(u)
            universe = kept
    except Exception as e:  # noqa: BLE001 - 가드 실패가 수집을 막으면 안 됨
        logger.warning("동명이인 가드 생략(오류): %s", e)
    if args.symbols:
        want = {s.strip().upper() for s in args.symbols.split(",")}
        universe = [u for u in universe if u["symbol"] in want]
    if args.limit:
        universe = universe[: args.limit]

    author_stats = watcher_stats.load_author_stats()

    n_posts = n_new = n_setup = 0
    sleep_sec = settings.get("tv_fetch_sleep_sec")
    max_age_h = settings.get("max_post_age_hours")

    with db.connect(db_path) as conn:
        for i, coin in enumerate(universe):
            if tradingview.is_blocked():
                logger.warning("차단 쿨다운 감지 - 남은 %d개 심볼 이번 주기 생략",
                               len(universe) - i)
                break
            ideas = tradingview.fetch_ideas(coin["symbol"], timeout, max_age_hours=max_age_h)
            n_posts += len(ideas)

            for idea in ideas:
                text = f"{idea['title']}\n{idea['description']}"
                setup = parse_setup(text, current_price=coin.get("price_usd"))
                if not setup or not setup.get("entry"):
                    continue
                n_setup += 1
                stats_row = author_stats.get(idea.get("author") or "", {})
                followers = stats_row.get("followers") or idea.get("author_followers")
                if followers is None and idea.get("author"):
                    followers = tradingview.fetch_author_followers(idea["author"], timeout)
                grade, score, rr = calculate_grade(
                    followers, setup["direction"], setup["entry"],
                    setup.get("sl"), setup.get("tp"), coin.get("price_usd"),
                )
                tf_hours = parse_timeframe_hours(text)
                level = {
                    "signal_key": db.make_signal_key(
                        coin["symbol"], setup["entry"], idea.get("author"), idea.get("url")),
                    "judgment_window_hours": judgment_window_hours(
                        tf_hours, setup["entry"], setup.get("tp")),
                    "raw_text": text,  # 원문 저장 → 파서 개선 시 재파싱 치유 (reparse_all)
                    "coin_symbol": coin["symbol"],
                    "ticker": coin["ticker"],
                    "direction": setup["direction"],
                    "entry_usd": setup["entry"],
                    "sl_usd": setup.get("sl"),
                    "tp_usd": setup.get("tp"),
                    "rr": rr,
                    "grade": grade,
                    "score": score,
                    "author": idea.get("author"),
                    "author_followers": followers,
                    "author_hit_rate": stats_row.get("hit_rate"),
                    "author_hit_count": stats_row.get("hit_count"),
                    "author_whitelisted": stats_row.get("whitelisted", False),
                    "mcap_rank": coin.get("rank"),
                    "mcap_tier_icon": coin.get("tier_icon"),
                    "post_url": idea.get("url"),
                    "post_age_minutes": idea.get("age_minutes"),
                    "collected_at": time.time(),
                }
                if db.upsert_level(conn, level):
                    n_new += 1

            if i < len(universe) - 1:
                time.sleep(sleep_sec)

        # 파서 개선 자동 전파: 원문 있는 기존 레벨을 현재 파서로 재파싱해 오염값 치유
        reparsed = db.reparse_all(conn)
        expired = db.expire_old(conn, settings.get("level_expiry_hours") * 3600)
        st = db.stats(conn)

    logger.info(
        "수집 완료(%.0f초): 글 %d건 → 셋업 %d건 → 신규 %d건 / 재파싱치유 %d건 / 만료 %d건 / DB %s",
        time.time() - t0, n_posts, n_setup, n_new, reparsed, expired, st,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
