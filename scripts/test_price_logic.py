# 가격체크 상태머신 오프라인 테스트 — 네트워크/텔레그램 없이 몽키패치로 검증.
import sys, time, os
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import logging
logging.basicConfig(level=logging.WARNING)

from config import settings
from storage import db
from monitor import price_check, upbit
from notify import telegram

TEST_DB = "cache/_test_price.db"
settings.SETTINGS["db_path"] = TEST_DB
if os.path.exists(TEST_DB):
    os.remove(TEST_DB)
db.init_db(TEST_DB)

now = time.time()
USDT_KRW = 1400.0

# 레벨 3개: LINK 엔트리 8.30/8.25(±1% 클러스터) + 7.50(별개), 등급 B — 필터 통과
with db.connect(TEST_DB) as conn:
    for entry, author, url in [(8.30, "AuthA", "u1"), (8.25, "AuthB", "u2"), (7.50, "AuthC", "u3")]:
        lv = dict(coin_symbol="LINK", ticker="KRW-LINK", direction="long",
                  entry_usd=entry, sl_usd=entry * 0.94, tp_usd=entry * 1.15, rr=2.4,
                  grade="B", score=62, author=author, author_followers=5000,
                  author_hit_rate=0.67, author_hit_count=12, author_whitelisted=(author == "AuthA"),
                  mcap_rank=19, mcap_tier_icon="🥇",
                  post_url=f"https://tv.com/{url}", post_age_minutes=2000, collected_at=now)
        lv["signal_key"] = db.make_signal_key("LINK", entry, author, url)
        db.upsert_level(conn, lv)

sent_messages = []
telegram.send = lambda text: sent_messages.append(text) or True

fake = {"price": None, "low": None}
upbit.fetch_prices = lambda mkts, t: {m: (USDT_KRW if m == "KRW-USDT" else fake["price"]) for m in mkts}
upbit.fetch_low_since = lambda m, mins, t: fake["low"]

# 시장심리는 네트워크 없이 고정값 (렌더링 검증 겸용)
from monitor import market_sentiment
market_sentiment.get_sentiment = lambda conn: {
    "btc_dominance": 56.6, "fear_greed": 31, "fear_greed_label": "Fear",
    "altcoin_season_index": 32,
}

ok = True
def check(name, cond):
    global ok
    print(("✅" if cond else "❌"), name)
    ok = ok and cond

# T1: 가격이 멀면(엔트리 +10%) 아무 알림 없음
fake["price"] = 8.30 * USDT_KRW * 1.10
s1 = price_check.run_once(now + 60)
check("T1 원거리 - 무알림", s1["previews"] == 0 and s1["touches"] == 0 and not sent_messages)

# T2: +0.6% 접근 → 예고 1건 (클러스터 상단 8.30 기준)
fake["price"] = 8.30 * USDT_KRW * 1.006
s2 = price_check.run_once(now + 120)
check("T2 접근 - 예고 1건", s2["previews"] == 1 and len(sent_messages) == 1 and "엔트리 접근" in sent_messages[0])

# T3: 같은 조건 재체크 → 중복 예고 없음
s3 = price_check.run_once(now + 180)
check("T3 중복 예고 억제", s3["previews"] == 0 and len(sent_messages) == 1)

# T4: 저가가 엔트리 하향 터치 → 본알림 1건, 엔트리 존 표기, 출처 하이퍼링크 2개
fake["price"] = 8.30 * USDT_KRW * 1.002
fake["low"] = 8.24 * USDT_KRW
s4 = price_check.run_once(now + 240)
touch_msg = sent_messages[-1]
check("T4 터치 - 본알림 1건", s4["touches"] == 1 and len(sent_messages) == 2)
check("T4 터치 헤더+존 표기", "엔트리 터치" in touch_msg and "엔트리 존" in touch_msg)
check("T4 출처 링크형(URL 비노출)", touch_msg.count("출처1") == 1 and touch_msg.count("출처2") == 1
      and 'href="https://tv.com' in touch_msg and "🔗 https://" not in touch_msg)
check("T4 적중률 표시", "적중률: 67%" in touch_msg and "⭐⭐" in touch_msg)
check("T4 시장심리 행", "BTC.D: 56.6%" in touch_msg and "ALT.S: 32 (BTC 매수 고려)" in touch_msg
      and "F&G: 31 (공포)" in touch_msg)
check("T4 원단위 반올림", ".00원" not in touch_msg and "원)" in touch_msg)

# T5: 터치된 클러스터는 재알림 없음, 7.50 별개 레벨은 아직 활성
with db.connect(TEST_DB) as conn:
    active = db.get_active_levels(conn)
check("T5 잔여 활성 = 7.50 하나", len(active) == 1 and abs(active[0]["entry_usd"] - 7.50) < 1e-9)

# T6: 7.50까지 급락(예고 없이) → 본알림만 1건 (동시감지=본알림만 규칙)
fake["price"] = 7.49 * USDT_KRW
fake["low"] = 7.45 * USDT_KRW
s6 = price_check.run_once(now + 300)
check("T6 급락 직터치 - 본알림만", s6["touches"] == 1 and s6["previews"] == 0)

# T7: 일일 상한 — 새 레벨 넣고 cap=2 상태에서 알림 억제되지만 상태전이는 수행
with db.connect(TEST_DB) as conn:
    lv = dict(coin_symbol="LINK", ticker="KRW-LINK", direction="long", entry_usd=7.00,
              sl_usd=6.5, tp_usd=8.0, rr=2.0, grade="B", score=60, author="AuthD",
              author_followers=100, author_hit_rate=None, author_hit_count=None,
              author_whitelisted=False, mcap_rank=19, mcap_tier_icon="🥇",
              post_url="https://tv.com/u4", post_age_minutes=100, collected_at=now)
    lv["signal_key"] = db.make_signal_key("LINK", 7.00, "AuthD", "u4")
    db.upsert_level(conn, lv)
settings.SETTINGS["alert_max_per_coin_per_day"] = 2  # 이미 2건 발송됨
fake["price"] = 6.99 * USDT_KRW
fake["low"] = 6.95 * USDT_KRW
s7 = price_check.run_once(now + 360)
with db.connect(TEST_DB) as conn:
    remaining = db.get_active_levels(conn)
check("T7 상한 억제 + 상태전이 수행", s7["suppressed"] == 1 and s7["touches"] == 0 and len(remaining) == 0)

print()
print("── 본알림 실제 렌더링 ──")
print(touch_msg)
os.remove(TEST_DB)
sys.exit(0 if ok else 1)
