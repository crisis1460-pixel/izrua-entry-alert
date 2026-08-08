"""[일회용] 2026-08-08 행별 절삭 예산 재보정(32) 확인용 샘플 재발송.

이전 샘플(AVAX, id=273)에서 '✍️ 작성자: @mastercrypto2020 ⭐⭐' 줄이 계산상
예산(당시 36) 이내인데도 실제로 줄내림되는 것을 스크린샷으로 확인 - 이모지
폭 계산을 보정하고 예산을 32로 낮춰 재발송, 해당 줄이 줄내림 없이 절삭되는지
확인한다. [샘플] 표식 명시. 확인 후 워크플로와 함께 삭제 예정.
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
    touch_msg = ("🧪 <b>[샘플 재검증 - ⭐⭐ 줄내림 수정 확인 (예산 32)]</b>\n"
                 + touch_msg)

    ok = tg.send(touch_msg, urgency="low")
    print("발송:", "성공" if ok else "실패")
    return ok


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
