"""[일회용] 구분선 16자 축소 확인용 샘플 알림 발송 - 2026-08-08.

DB 를 읽어(SELECT 전용) VVV 터치 실제 알림을 그대로 재구성해 그룹에
발송 - 새 구분선(16자)이 좁아진 말풍선 폭에서 줄바꿈 없이 한 줄로
표시되는지 확인. [샘플] 표식 명시. 확인 후 워크플로와 함께 삭제 예정.
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
        vvv = dict(conn.execute("SELECT * FROM levels WHERE id=256").fetchone())

    sv, sr = (vvv["touch_supply_verdict"] or "|").split("|", 1)
    pv, pr = (vvv["touch_position_verdict"] or "|").split("|", 1)
    lv = dict(coin_symbol="VVV", direction="long", entry_usd=vvv["entry_usd"],
              sl_usd=vvv["sl_usd"], tp_usd=vvv["tp_usd"], grade=vvv["grade"],
              score=vvv["score"], author=vvv["author"],
              author_whitelisted=vvv["author_whitelisted"],
              author_followers=vvv["author_followers"],
              author_hit_rate=vvv["author_hit_rate"],
              author_hit_count=vvv["author_hit_count"],
              mcap_rank=vvv["mcap_rank"], mcap_tier_icon=vvv["mcap_tier_icon"],
              tp_ladder_count=vvv["tp_ladder_count"], tps_usd=vvv["tps_usd"],
              post_url=vvv["post_url"], post_age_minutes=vvv["post_age_minutes"],
              collected_at=vvv["collected_at"])
    fng = int(vvv["touch_fear_greed"]) if vvv["touch_fear_greed"] is not None else None
    msg = tg.render_alert(
        "touch", "VVV", [lv], vvv["touch_price_krw"], vvv["touch_usdt_krw"],
        sentiment={"btc_dominance": vvv["touch_btc_dominance"], "fear_greed": fng,
                   "fear_greed_label": "Extreme Fear" if fng and fng <= 25 else "Fear"},
        kimchi_pct=vvv["touch_kimchi_pct"], volume_rank=vvv["touch_volume_rank"],
        rep=lv, supply=(sv, sr or None), position=(pv, pr or None))
    msg = "🧪 <b>[샘플 - 구분선 16자 축소 확인용]</b>\n" + msg

    ok = tg.send(msg, urgency="low")
    print("발송:", "성공" if ok else "실패")
    return ok


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
