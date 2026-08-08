"""[일회용] 2026-08-08 그룹채팅 폭 축소 대응 수정 4건 확인용 샘플 발송.

DB 를 읽어(SELECT 전용) 실제 레벨(AVAX, id=273)을 그대로 재구성해 본알림 +
부분익절/급증 알림 샘플을 그룹에 발송한다. 확인 항목:
  1. 구분선 17자로 줄내림 없이 한 줄 표시
  2. 목표 행 "원" 뒤 이중 공백 제거
  3. "1/n단계" → "1/n" ("단계" 글자 제거)
  4. 너무 긴 행은 줄내림 대신 내림 직전까지만 표시(표시 없이 절삭)
  5. 부분익절/급증 알림도 마지막 구분선 아래 출처 링크 표기
전부 [샘플] 표식 명시. 확인 후 워크플로와 함께 삭제 예정.
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
    touch_msg = "🧪 <b>[샘플 - 본알림: 17자 구분선/원-괄호 붙임/1·n 표기/행별 절삭]</b>\n" + touch_msg

    tp_msg = tg.render_tp_partial_alert(
        row["coin_symbol"], 1, row["tp_ladder_count"] or 3,
        row["touch_price_krw"] * 1.02, row["touch_price_krw"],
        post_url=row["post_url"])
    tp_msg = "🧪 <b>[샘플 - 부분익절 알림: 출처 링크 표기]</b>\n" + tp_msg

    vs_msg = tg.render_volume_spike_alert(
        row["coin_symbol"], 6.2, 42.3, 6.8,
        next_tp_krw=row["touch_price_krw"] * 1.05, tp_idx=1,
        tp_count=row["tp_ladder_count"] or 3,
        post_urls=[row["post_url"]])
    vs_msg = "🧪 <b>[샘플 - 급증 알림: 출처 링크 표기]</b>\n" + vs_msg

    ok = True
    for msg in (touch_msg, tp_msg, vs_msg):
        ok = tg.send(msg, urgency="low") and ok
    print("발송:", "성공" if ok else "실패")
    return ok


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
