"""[일회용] 그룹 전환 확인용 샘플 알림 발송 — 2026-08-08 사용자 요청.

TELEGRAM_CHAT_ID 를 새 그룹(izrua_group)으로 교체한 뒤 실제로 그 방에
알림이 도착하는지 확인하기 위한 1회성 스크립트. DB 를 읽지도 쓰지도 않는
순수 렌더+발송이며, 실제 레벨/판정과 무관한 가상 데이터. 헤더에
[샘플] 을 명시한다. 확인 후 워크플로와 함께 삭제 예정.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from notify import telegram as tg  # noqa: E402

USDT = 1407.0
lv = dict(coin_symbol="BTC", direction="long", entry_usd=62500.0, sl_usd=59000.0,
          tp_usd=68000.0, rr=2.0, grade="B", score=58, author="GroupTestAuthor",
          author_whitelisted=0, author_followers=3000,
          mcap_rank=1, mcap_tier_icon="💎",
          post_url="https://tv.com/grouptest", post_age_minutes=60,
          collected_at=1786100000)

msg = tg.render_alert(
    "touch", "BTC", [lv],
    current_krw=87500000.0, usdt_krw=USDT,
    sentiment={"btc_dominance": 56.8, "fear_greed": 30,
               "fear_greed_label": "Fear"},
    week52=(102000000.0, 55000000.0), kimchi_pct=0.05, volume_rank=1,
    rep=lv,
    supply=("중립", None),
    position=("중립", "일50"))

msg = ("🧪 <b>[샘플 — 그룹 전환 확인용, 실거래 신호 아님]</b>\n"
       "이 메시지가 보이면 새 그룹 chat_id 전환이 정상 작동한 것입니다.\n" + msg)

ok = tg.send(msg, urgency="high")  # 유음 발송 - 도착 확인 쉽게
print("발송:", "성공" if ok else "실패")
sys.exit(0 if ok else 1)
