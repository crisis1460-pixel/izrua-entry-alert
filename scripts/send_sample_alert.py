"""[일회용] 신규 참여자 안내 + 최근 실제 알림 3건 재전송 - 2026-08-08.

DB 를 실제로 읽어 최근 발송된 진짜 알림 3건(CFX 터치/CFX TP3 최종/VVV
터치)의 원본 텍스트를 그대로 재구성해 그룹에 다시 보낸다 - 새 참여자가
"어떤 알림이 오는지" 실물로 볼 수 있게. 앞에 안내 프롬프트 1건을 먼저
보내고, 재전송분에는 [예시-과거 발송분] 표식을 붙여 실거래 신호로
오인하지 않게 한다. 확인 후 워크플로와 함께 삭제 예정.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from notify import telegram as tg  # noqa: E402
from storage import db  # noqa: E402
from config import settings  # noqa: E402

GUIDE = """👋 <b>izrua_group 알림봇 안내</b>

이 방은 TradingView 차티스트 글에서 뽑은 <b>매수 진입가</b>가 실제로
가격에 도달하면 알림을 보내는 봇입니다. 자동매매가 아니라
<b>참고용 알림</b>이며, 매매 판단은 본인 책임입니다.

<b>알림 종류</b>
🎯 [진입가 터치] — 가격이 등록된 진입가에 실제로 도달
✅ [TPn 적중] — 목표가 단계 도달(다단계 목표가 있는 경우)
📈 [OI 급증] — 특정 코인 선물 미결제약정 급변 (터치와 무관한 별도 정보)

<b>알림 안 읽는 법</b>
- 등급(S~D): 팔로워·가격근접도·목표거리 등 종합 점수. C 이상만 발송
- 🌡️ 자리: RSI+이동평균 기반 매수 자리 판정(최적/우호/중립/주의/위험)
- 🧭 수급: 선물 펀딩비+미결제약정 조합 판정(우호/중립/주의)
- 나머지(김프·BTC.D·F&G 등)는 시장 전반 참고 지표

<b>발송 빈도</b>: 실시간 조건 충족 시에만, 하루 몇 건 수준(장에 따라
변동). 아래는 최근 실제 발송된 알림 3건을 그대로 다시 보여드립니다."""


def main():
    ok_all = True
    ok_all &= tg.send(GUIDE, urgency="low")

    db_path = settings.get("db_path")
    with db.connect(db_path) as conn:
        cfx = dict(conn.execute("SELECT * FROM levels WHERE id=248").fetchone())
        vvv = dict(conn.execute("SELECT * FROM levels WHERE id=256").fetchone())

    def _fng_label(v):
        return "Extreme Fear" if v is not None and v <= 25 else "Fear"

    lv1 = dict(coin_symbol="CFX", direction="long", entry_usd=cfx["entry_usd"],
               sl_usd=cfx["sl_usd"], tp_usd=cfx["tp_usd"], grade=cfx["grade"],
               score=cfx["score"], author=cfx["author"],
               author_whitelisted=cfx["author_whitelisted"],
               author_followers=cfx["author_followers"],
               author_hit_rate=cfx["author_hit_rate"],
               author_hit_count=cfx["author_hit_count"],
               mcap_rank=cfx["mcap_rank"], mcap_tier_icon=cfx["mcap_tier_icon"],
               tp_ladder_count=cfx["tp_ladder_count"], tps_usd=cfx["tps_usd"],
               post_url=cfx["post_url"], post_age_minutes=cfx["post_age_minutes"],
               collected_at=cfx["collected_at"])
    msg1 = tg.render_alert(
        "touch", "CFX", [lv1], cfx["touch_price_krw"], cfx["touch_usdt_krw"],
        sentiment={"btc_dominance": cfx["touch_btc_dominance"],
                   "fear_greed": int(cfx["touch_fear_greed"]),
                   "fear_greed_label": _fng_label(cfx["touch_fear_greed"])},
        kimchi_pct=cfx["touch_kimchi_pct"], volume_rank=cfx["touch_volume_rank"],
        rep=lv1)
    msg1 = "📋 <b>[예시 - 과거 발송분 재전송]</b>\n" + msg1
    ok_all &= tg.send(msg1, urgency="low")

    msg2 = tg.render_tp_partial_alert(
        "CFX", 3, 3, cfx["resolve_price_krw"], cfx["touch_price_krw"])
    msg2 = "📋 <b>[예시 - 과거 발송분 재전송]</b>\n" + msg2
    ok_all &= tg.send(msg2, urgency="low")

    sv, sr = (vvv["touch_supply_verdict"] or "|").split("|", 1)
    pv, pr = (vvv["touch_position_verdict"] or "|").split("|", 1)
    lv3 = dict(coin_symbol="VVV", direction="long", entry_usd=vvv["entry_usd"],
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
    msg3 = tg.render_alert(
        "touch", "VVV", [lv3], vvv["touch_price_krw"], vvv["touch_usdt_krw"],
        sentiment={"btc_dominance": vvv["touch_btc_dominance"],
                   "fear_greed": int(vvv["touch_fear_greed"]),
                   "fear_greed_label": _fng_label(vvv["touch_fear_greed"])},
        kimchi_pct=vvv["touch_kimchi_pct"], volume_rank=vvv["touch_volume_rank"],
        rep=lv3, supply=(sv, sr or None), position=(pv, pr or None))
    msg3 = "📋 <b>[예시 - 과거 발송분 재전송]</b>\n" + msg3
    ok_all &= tg.send(msg3, urgency="low")

    print("발송:", "전부 성공" if ok_all else "일부 실패")
    return ok_all


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
