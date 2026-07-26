"""일회성 수리 — 구세대(raw_text 부재) 오염 tp/sl 5행 무효화 (2026-07-26 감사 major1).

대상: id 42/43(ALGO tp=1.0 서수 오인 잔존 — 방치 시 07-30 20:18 tp_only 오판정),
id 8(ETH sl>entry), id 12(XRP sl>entry), id 47(INJ short, tp 크기 위반).
5행 모두 raw_text 가 없어 reparse_all/업서트 자동치유가 닿지 않는다(원문저장 도입 전 수집).
extractor 와 같은 방향·크기 sanity 로 위반 값만 NULL 처리, rr 은 완비 시에만 재계산.
상태·터치·판정 필드는 건드리지 않는다. 멱등(고정 id 목록 + 규칙 기반).

사용: python scripts/repair_sanity_20260726.py [--apply]   (기본 드라이런)
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import settings  # noqa: E402
from storage import db  # noqa: E402

TARGET_IDS = [8, 12, 42, 43, 47]


def sanitize(direction, entry, tp, sl):
    """extractor 방향·크기 규칙(0.25x~4x) 위반 값을 None 으로. 반환 (tp, sl)."""
    if not entry or entry <= 0:
        return tp, sl
    if direction == "long":
        if tp is not None and not (entry < tp <= entry * 4):
            tp = None
        if sl is not None and not (entry * 0.25 <= sl < entry):
            sl = None
    else:
        if tp is not None and not (entry * 0.25 <= tp < entry):
            tp = None
        if sl is not None and not (entry < sl <= entry * 4):
            sl = None
    return tp, sl


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    db.init_db(settings.get("db_path"))
    changed = 0
    with db.connect(settings.get("db_path")) as conn:
        rows = conn.execute(
            f"SELECT id, coin_symbol, direction, entry_usd, sl_usd, tp_usd, rr, status "
            f"FROM levels WHERE id IN ({','.join('?' * len(TARGET_IDS))})",
            TARGET_IDS).fetchall()
        for r in rows:
            tp, sl = sanitize(r["direction"], r["entry_usd"], r["tp_usd"], r["sl_usd"])
            if tp == r["tp_usd"] and sl == r["sl_usd"]:
                print(f"id {r['id']} {r['coin_symbol']}: 정상 - 무변경")
                continue
            rr = None
            if tp and sl and r["direction"] == "long" and r["entry_usd"] > sl:
                rr = round((tp - r["entry_usd"]) / (r["entry_usd"] - sl), 2)
            print(f"id {r['id']} {r['coin_symbol']}({r['direction']},{r['status']}): "
                  f"tp {r['tp_usd']}→{tp}, sl {r['sl_usd']}→{sl}, rr {r['rr']}→{rr}")
            changed += 1
            if args.apply:
                conn.execute("UPDATE levels SET tp_usd=?, sl_usd=?, rr=? WHERE id=?",
                             (tp, sl, rr, r["id"]))
        if args.apply:
            conn.commit()
    print(f"{'반영' if args.apply else '드라이런'} 완료: 변경 {changed}건")


if __name__ == "__main__":
    main()
