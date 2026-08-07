"""[일회용] 풀옵션 샘플 알림 발송 — 2026-08-08 사용자 요청.

어제(08-07~08) 배포한 🌡️ 자리(MA 5단계)·🧭 수급(펀딩×OI) 줄을 포함해
선택 요소가 전부 켜진 메시지를 실제 텔레그램으로 1회 발송해 본다.
DB 를 읽지도 쓰지도 않는 순수 렌더+발송 — 실제 레벨/판정과 무관한 가상
데이터이며 헤더에 [샘플] 를 명시한다. 확인 후 워크플로와 함께 삭제 예정.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from notify import telegram as tg  # noqa: E402

USDT = 1406.0
lv1 = dict(coin_symbol="SOL", direction="long", entry_usd=73.42, sl_usd=69.0,
           tp_usd=78.5, rr=2.1, grade="S", score=91, author="MasterAnanda",
           author_whitelisted=1, author_followers=52000,
           author_hit_rate=0.70, author_hit_count=60,
           author_self_wins=4, author_self_losses=2,
           author_self_neff_r=6.2, author_self_e_lb=0.15,
           author_touched_n=9, author_untouched_expired=3,
           author_rank_min_neff=5,
           author_avg_holding_days=3.2,
           mcap_rank=7, mcap_tier_icon="🥇",
           tp_ladder_count=3, tps_usd="[74.9, 78.5, 84.0]",
           post_url="https://tv.com/a", post_age_minutes=180,
           collected_at=1786100000)
lv2 = dict(lv1, entry_usd=73.20, author="OtherChartist", author_whitelisted=0,
           post_url="https://tv.com/b", grade="B", score=60)

msg = tg.render_alert(
    "touch", "SOL", [lv1, lv2],
    current_krw=103500.0, usdt_krw=USDT,
    sentiment={"btc_dominance": 56.8, "altcoin_season_index": 24,
               "fear_greed": 25, "fear_greed_label": "Extreme Fear"},
    week52=(350800.0, 91800.0), kimchi_pct=0.10, volume_rank=17,
    rep=lv1,
    funding_rate=0.0031,
    funding_regime_flip={"flipped": True, "neg_days": 34.0, "latest": 0.0031},
    supply=("우호", "숏 몰림"),
    position=("최적", "60일지지·정배열·일38"))

# 실알림 오인 방지 — 헤더 바로 위에 샘플 표식
msg = "🧪 <b>[샘플 — 실거래 신호 아님]</b> 풀옵션 렌더링 데모\n" + msg

ok = tg.send(msg, urgency="low")  # 무음 발송
print("발송:", "성공" if ok else "실패")
sys.exit(0 if ok else 1)
