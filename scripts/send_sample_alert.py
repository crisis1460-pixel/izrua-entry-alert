"""[일회용] 2026-08-08 화이트리스트 ⭐⭐ 복원(원래 기준 그대로, 라벨 없이
별도 줄) 확인용 샘플 발송.

star 를 통째로 지웠던 것을 정정 - author_whitelisted 기준 그대로 복원하되
작성자 줄과는 분리된 별도 줄(라벨 문구 없음)로 표시한다. [샘플] 표식 명시.
확인 후 워크플로와 함께 삭제 예정.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from notify import telegram as tg  # noqa: E402
from storage import db  # noqa: E402
from config import settings  # noqa: E402


def main():
    db_path = settings.get("db_path")
    with db.connect(db_path) as conn:
        row = dict(conn.execute("SELECT * FROM levels WHERE id=273").fetchone())

    lv = dict(coin_symbol=row["coin_symbol"], direction=row["direction"],
              entry_usd=row["entry_usd"], sl_usd=row["sl_usd"], tp_usd=row["tp_usd"],
              grade=row["grade"], score=row["score"], author=row["author"],
              author_whitelisted=row["author_whitelisted"],
              author_followers=row["author_followers"],
              author_hit_rate=row["author_hit_rate"],
              author_hit_count=row["author_hit_count"],
              mcap_rank=row["mcap_rank"], mcap_tier_icon=row["mcap_tier_icon"],
              tp_ladder_count=row["tp_ladder_count"], tps_usd=row["tps_usd"],
              post_url=row["post_url"], post_age_minutes=row["post_age_minutes"],
              collected_at=row["collected_at"])
    fng = int(row["touch_fear_greed"]) if row["touch_fear_greed"] is not None else None
    touch_msg = tg.render_alert(
        "touch", row["coin_symbol"], [lv], row["touch_price_krw"], row["touch_usdt_krw"],
        sentiment={"btc_dominance": row["touch_btc_dominance"], "fear_greed": fng,
                   "fear_greed_label": "Extreme Fear" if fng and fng <= 25 else "Fear"},
        kimchi_pct=row["touch_kimchi_pct"], volume_rank=row["touch_volume_rank"],
        rep=lv, supply=(None, None), position=(None, None))
    touch_msg = ("🧪 <b>[샘플 - ⭐⭐ 복원(원래 기준, 라벨 없이 별도 줄) 확인]</b>\n"
                 + touch_msg)

    ok = tg.send(touch_msg, urgency="low")
    print("발송:", "성공" if ok else "실패")
    return ok


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
