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

fake = {"price": None, "low": None, "high": None}
upbit.fetch_prices = lambda mkts, t: {m: (USDT_KRW if m == "KRW-USDT" else fake["price"]) for m in mkts}
upbit.fetch_range_since = lambda m, mins, t: (
    None if fake["low"] is None and fake["high"] is None
    else (fake["high"] or fake["price"], fake["low"] or fake["price"]))
upbit.fetch_week52 = lambda m, t: (16000.0, 9000.0)  # 52주 고가/저가 (KRW)
upbit.fetch_volume_ranks = lambda t: {"KRW-LINK": 5}
from monitor import binance
binance.fetch_usdt_price = lambda s, t: (fake["price"] / USDT_KRW) * 0.997  # 김프 +0.3%대

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
check("T2 접근 - 예고 1건", s2["previews"] == 1 and len(sent_messages) == 1 and "진입가 접근" in sent_messages[0])

# T3: 같은 조건 재체크 → 중복 예고 없음
s3 = price_check.run_once(now + 180)
check("T3 중복 예고 억제", s3["previews"] == 0 and len(sent_messages) == 1)

# T4: 저가가 엔트리 하향 터치 → 본알림 1건, 엔트리 존 표기, 출처 하이퍼링크 2개
fake["price"] = 8.30 * USDT_KRW * 1.002
fake["low"] = 8.24 * USDT_KRW
s4 = price_check.run_once(now + 240)
touch_msg = sent_messages[-1]
check("T4 터치 - 본알림 1건", s4["touches"] == 1 and len(sent_messages) == 2)
check("T4 터치 헤더+진입가 표기", "진입가 터치" in touch_msg and "진입:" in touch_msg)
check("T4 출처 링크형(URL 비노출)", touch_msg.count("출처1") == 1 and touch_msg.count("출처2") == 1
      and 'href="https://tv.com' in touch_msg and "🔗 https://" not in touch_msg)
check("T4 적중률 표시", "적중률: 67%" in touch_msg and "⭐⭐" in touch_msg)
check("T4 시장심리 행", "BTC.D: 56.6%" in touch_msg and "ALT.S: 32 (BTC 매수 고려)" in touch_msg
      and "F&G: 31 (공포)" in touch_msg)
check("T4 원단위 반올림", ".00원" not in touch_msg and "원)" in touch_msg)
check("T4 표기수정 1차", "[진입가 터치]" in touch_msg and "손절" not in touch_msg
      and "평균 적중률: 67%" in touch_msg and "작성자 평균" not in touch_msg)
check("T4 표기수정 최종(워쳐식 타점+원화단독)", "타점" in touch_msg and "현재:" in touch_msg
      and "진입:" in touch_msg and "목표:" in touch_msg and "$" not in touch_msg
      and "엔트리" not in touch_msg and "~" in touch_msg)
check("T4 R:R삭제+거래순위+4칸정렬", "R:R" not in touch_msg and "    거래:  5위" in touch_msg
      and "\n    현재:" in touch_msg and "\n    고가" in touch_msg)
check("T4 김프+52주", "김프" in touch_msg and "52주" in touch_msg
      and "고가" in touch_msg and "지점" in touch_msg)

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

# ── 적중판정 엔진 (ACCURACY_DB_PLAN 1단계) ──────────────────────
def add_touched(coin, entry, sl, tp, touched_ago_sec, key, window_h=None):
    with db.connect(TEST_DB) as conn:
        lv = dict(coin_symbol=coin, ticker=f"KRW-{coin}", direction="long",
                  entry_usd=entry, sl_usd=sl, tp_usd=tp, rr=None, grade="B", score=60,
                  author=f"A_{key}", author_followers=100, author_hit_rate=None,
                  author_hit_count=None, author_whitelisted=False, mcap_rank=50,
                  mcap_tier_icon="🥇", post_url=f"https://tv.com/{key}",
                  post_age_minutes=100, collected_at=now, judgment_window_hours=window_h)
        lv["signal_key"] = db.make_signal_key(coin, entry, lv["author"], lv["post_url"])
        db.upsert_level(conn, lv)
        row = conn.execute("SELECT id FROM levels WHERE signal_key=?", (lv["signal_key"],)).fetchone()
        conn.execute("UPDATE levels SET status='touched', touched_at=?, touch_price_krw=? WHERE id=?",
                     (now - touched_ago_sec, entry * USDT_KRW, row["id"]))
        return row["id"]

def outcome_of(lid):
    with db.connect(TEST_DB) as conn:
        r = conn.execute("SELECT outcome, judgment_mode, r_multiple, ambiguous FROM levels WHERE id=?", (lid,)).fetchone()
        return dict(r)

# T8: TP 도달 → hit, R=+2 근처
lid8 = add_touched("LINK", 10.0, 9.0, 12.0, 3600, "t8")
fake["price"] = 12.1 * USDT_KRW; fake["low"] = 11.5 * USDT_KRW; fake["high"] = 12.2 * USDT_KRW
price_check.run_once(now + 420)
o8 = outcome_of(lid8)
check("T8 판정 hit + R기록", o8["outcome"] == "hit" and o8["judgment_mode"] == "tp_sl"
      and o8["r_multiple"] is not None and abs(o8["r_multiple"] - 2.0) < 0.01)

# T9: SL 도달 → miss, R=-1
lid9 = add_touched("LINK", 10.0, 9.0, 12.0, 3600, "t9")
fake["price"] = 9.2 * USDT_KRW; fake["low"] = 8.9 * USDT_KRW; fake["high"] = 9.4 * USDT_KRW
price_check.run_once(now + 540)
o9 = outcome_of(lid9)
check("T9 판정 miss + R=-1", o9["outcome"] == "miss" and abs(o9["r_multiple"] + 1.0) < 0.01)

# T10: 같은 구간 TP·SL 동시 → 보수적 miss + ambiguous
lid10 = add_touched("LINK", 10.0, 9.0, 12.0, 3600, "t10")
fake["price"] = 10.0 * USDT_KRW; fake["low"] = 8.9 * USDT_KRW; fake["high"] = 12.2 * USDT_KRW
price_check.run_once(now + 660)
o10 = outcome_of(lid10)
check("T10 동시터치 - miss+ambiguous", o10["outcome"] == "miss" and o10["ambiguous"] == 1)

# T11: TP 없음 + 7일 경과 → 타임박스 승 판정
lid11 = add_touched("LINK", 10.0, None, None, 8 * 86400, "t11")
fake["price"] = 10.5 * USDT_KRW; fake["low"] = 10.3 * USDT_KRW; fake["high"] = 10.6 * USDT_KRW
price_check.run_once(now + 780)
o11 = outcome_of(lid11)
check("T11 타임박스 7일 - win", o11["outcome"] == "timeboxed_win" and o11["judgment_mode"] == "timeboxed")

# T12: 판정 창 존중 — 1D봉(30일 창) 글은 8일 지나도 강제 종결하지 않음
lid12 = add_touched("LINK", 10.0, 9.0, 14.0, 8 * 86400, "t12", window_h=720.0)
fake["price"] = 10.5 * USDT_KRW; fake["low"] = 10.3 * USDT_KRW; fake["high"] = 10.8 * USDT_KRW
price_check.run_once(now + 900)
o12 = outcome_of(lid12)
check("T12 30일 창 - 8일차 미종결 유지", o12["outcome"] is None)

# T13: 같은 조건이지만 7일 창이면 타임박스 종결됨 (대조군)
lid13 = add_touched("LINK", 10.0, 9.0, 14.0, 8 * 86400, "t13", window_h=168.0)
price_check.run_once(now + 960)
o13 = outcome_of(lid13)
check("T13 7일 창 대조군 - 타임박스 종결", o13["outcome"] == "timeboxed_win")

# T14: 자체 성적 병기 줄 (🏹 별도 줄, 5건 이상 발동)
from notify import telegram as tg
msg_a = tg.render_alert("touch", "LINK", [dict(
    coin_symbol="LINK", entry_usd=8.3, sl_usd=7.8, tp_usd=9.5, rr=2.4, grade="B", score=62,
    author="ProChartist", author_followers=None, author_hit_rate=0.72, author_hit_count=25,
    author_whitelisted=True, mcap_rank=19, mcap_tier_icon="🥇", post_url="https://tv.com/a",
    post_age_minutes=60, collected_at=now, author_self_wins=8, author_self_losses=3)],
    8.35 * USDT_KRW, USDT_KRW)
check("T14 워쳐+자체 병기 (별도줄)", "📊 평균 적중률: 72% (워쳐 25건)" in msg_a
      and "\n🏹 터치후 승률: 73% (8승3패)" in msg_a)
msg_b = tg.render_alert("touch", "LINK", [dict(
    coin_symbol="LINK", entry_usd=8.3, sl_usd=None, tp_usd=None, rr=None, grade="C", score=45,
    author="NewComer", author_followers=2300, author_hit_rate=None, author_hit_count=None,
    author_whitelisted=False, mcap_rank=19, mcap_tier_icon="🥇", post_url="https://tv.com/b",
    post_age_minutes=60, collected_at=now, author_self_wins=4, author_self_losses=2)],
    8.35 * USDT_KRW, USDT_KRW)
check("T14b 자체만 (워쳐없음)", "🏹 터치후 승률: 67% (4승2패)" in msg_b and "기록없음" not in msg_b)

print()
print("── 본알림 실제 렌더링 ──")
print(touch_msg)
os.remove(TEST_DB)
sys.exit(0 if ok else 1)
