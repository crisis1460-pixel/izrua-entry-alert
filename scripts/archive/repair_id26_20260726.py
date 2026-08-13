"""일회성 수리 — id 26 AVAX 허구 outcome 재판정 (2026-07-26 감사 major3).

id 26은 레벨 수집(07-23 07:10) 43분 전 가격으로 터치·miss 종결돼 outcome 전체가
허구였다(구독자가 진입 불가능한 시점). 수집 시각 이후 캔들만으로 터치를 재탐색해
재앵커·재판정한다. 수집 후 한 번도 도달이 없으면 터치 무효(previewed 복귀 — 봇이
남은 유효기간 동안 이어서 감시).

사용: python scripts/repair_id26_20260726.py [--apply] [--cache <dir>]
멱등: repair_rejudge_20260726 의 판정 함수 재사용, 고정 id.
"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import settings  # noqa: E402
from storage import db  # noqa: E402
from repair_rejudge_20260726 import (  # noqa: E402
    APPLY_SQL, VOID_SQL, FxSeries, _RETRO_SEC, fetch_candles, rejudge_row)

TARGET_ID = 26


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--cache")
    args = ap.parse_args()
    cache_dir = Path(args.cache) if args.cache else None
    if cache_dir:
        cache_dir.mkdir(parents=True, exist_ok=True)

    now = int(time.time() // 600) * 600
    timeout = settings.get("http_timeout_sec")
    db.init_db(settings.get("db_path"))
    with db.connect(settings.get("db_path")) as conn:
        lv = dict(conn.execute("SELECT * FROM levels WHERE id=?", (TARGET_ID,)).fetchone())
        # rejudge_row 는 touched_at−_RETRO_SEC 부터 탐색 → collected_at 부터 탐색되게 보정
        lv["touched_at"] = lv["collected_at"] + _RETRO_SEC
        start = lv["collected_at"] - 120
        fx = FxSeries(fetch_candles("KRW-USDT", start, now, timeout, cache_dir))
        candles = fetch_candles(lv["ticker"], start, now, timeout, cache_dir)
        upd = rejudge_row(lv, candles, fx, now, settings.get)
        if upd["void"]:
            print(f"id {TARGET_ID}: 수집 후 도달 없음 → 터치 무효(previewed 복귀)")
        else:
            import datetime
            print(f"id {TARGET_ID}: outcome={upd['outcome']} R={upd['r_multiple']} "
                  f"터치 재앵커={datetime.datetime.fromtimestamp(upd['touched_at'])} "
                  f"ret24={upd['ret_24h']} ret72={upd['ret_72h']}")
        if args.apply:
            if upd["void"]:
                conn.execute(VOID_SQL, (TARGET_ID,))
            else:
                conn.execute(APPLY_SQL, (
                    upd["status"], upd["touched_at"], upd["touch_price_krw"],
                    upd["touch_usdt_krw"], upd["outcome"], upd["resolved_at"],
                    upd["resolve_price_krw"], upd["r_multiple"], upd["best_tp_hit"],
                    upd["ambiguous"], upd["judgment_mode"], upd["ret_24h"],
                    upd["ret_72h"], TARGET_ID))
            conn.commit()
            print("반영 완료.")
        else:
            print("드라이런 — 반영하려면 --apply")


if __name__ == "__main__":
    main()
