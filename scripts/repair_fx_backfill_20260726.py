"""일회성 수리 — touch_usdt_krw만 NULL인 중간세대 5행 백필 (2026-07-26 재감사 minor6).

대상: id 13,50,58,60,61 — 07-24 00:06~03:46 터치, touch_usdt_krw 컬럼 도입 이전
(v1~2차감사 사이) 세대라 NULL. _judge_outcomes 의 환율 드리프트 보정 분기
(base_eff = base*(usdt_krw/t_rate))가 t_rate(=touch_usdt_krw) 없이는 동작 못 해
장기 판정·ret_24h/72h 기준가가 터치 시점 환율로 보정되지 못한 채 방치된다.

주의: touch_price_krw 는 그 시절 코드가 클러스터 상단가(min(current, top_krw))를
클러스터 전원에게 공유 저장하던 버그의 영향을 받아, 자기 entry_usd 로 역산한
환율과 실제 환율이 어긋나는 행이 섞여 있다(id 58/60 실측 확인 — id13/50/61 은
우연히 근접). 따라서 touch_price_krw/entry_usd 역산이 아니라, repair_rejudge_
20260726 의 FxSeries 패턴대로 그 시각의 실제 KRW-USDT 1분봉 종가를 조회해 채운다.

이미 종결(outcome IS NOT NULL)된 행(id 13,50 — outcome=miss)은 outcome/
touch_price_krw 등 다른 필드를 전혀 건드리지 않고 touch_usdt_krw만 채운다
(과거 판정 재현 목적, 결론 변경 없음). 미종결 행(58,60,61)은 앞으로 봇이
스스로 판정할 때 이 값을 드리프트 보정에 사용하게 된다.

사용: python scripts/repair_fx_backfill_20260726.py [--apply] [--cache <dir>]
멱등: WHERE touch_usdt_krw IS NULL 가드 + 고정 id 목록.
"""

import argparse
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import settings  # noqa: E402
from storage import db  # noqa: E402
from repair_rejudge_20260726 import FxSeries, fetch_candles  # noqa: E402

TARGET_IDS = [13, 50, 58, 60, 61]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--cache")
    args = ap.parse_args()
    cache_dir = Path(args.cache) if args.cache else None
    if cache_dir:
        cache_dir.mkdir(parents=True, exist_ok=True)

    timeout = settings.get("http_timeout_sec")
    db.init_db(settings.get("db_path"))
    with db.connect(settings.get("db_path")) as conn:
        rows = [dict(r) for r in conn.execute(
            f"SELECT id, coin_symbol, touched_at, touch_price_krw, touch_usdt_krw, "
            f"entry_usd, outcome, status FROM levels WHERE id IN "
            f"({','.join('?' * len(TARGET_IDS))})", TARGET_IDS).fetchall()]
        rows.sort(key=lambda r: r["id"])
        missing = set(TARGET_IDS) - {r["id"] for r in rows}
        if missing:
            print(f"[경고] 대상 id 미존재: {sorted(missing)}")
        already = [r["id"] for r in rows if r["touch_usdt_krw"] is not None]
        if already:
            print(f"[정보] 이미 touch_usdt_krw 채워진 id (건너뜀): {already}")
        rows = [r for r in rows if r["touch_usdt_krw"] is None and r["touched_at"]]
        if not rows:
            print("대상 없음 - 종료")
            return

        start = min(r["touched_at"] for r in rows) - 300
        end = max(r["touched_at"] for r in rows) + 60
        print(f"[1/2] KRW-USDT 캔들 조회 {datetime.fromtimestamp(start)} ~ {datetime.fromtimestamp(end)}")
        fx = FxSeries(fetch_candles("KRW-USDT", start, end, timeout, cache_dir))

        print("\n[2/2] 백필 결과 (id, 코인, 상태/outcome, touched_at, 신규 usdt_krw, 참고: 역산비율)")
        updates = []
        for r in rows:
            rate = fx.at(r["touched_at"])
            naive = (r["touch_price_krw"] / r["entry_usd"]) if r["touch_price_krw"] and r["entry_usd"] else None
            naive_s = f"{naive:.1f}" if naive else "N/A"
            print(f"  id={r['id']:>3} {r['coin_symbol']:6} {r['status']}/{r['outcome']} "
                  f"{datetime.fromtimestamp(r['touched_at'])} -> usdt_krw={rate:.2f} "
                  f"(역산비율={naive_s})")
            updates.append((rate, r["id"]))

        if args.apply:
            conn.executemany(
                "UPDATE levels SET touch_usdt_krw=? WHERE id=? AND touch_usdt_krw IS NULL",
                updates)
            conn.commit()
            print(f"\n반영 완료: {len(updates)}건.")
        else:
            print(f"\n드라이런 — 반영하려면 --apply ({len(updates)}건 예정)")


if __name__ == "__main__":
    main()
