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
from analytics import clustering

_real_fetch_range_since = upbit.fetch_range_since  # 아래에서 price_check 테스트용으로
                                                    # upbit.fetch_range_since 를 몽키패치
                                                    # 하기 전에 실물 함수를 보관해둔다
                                                    # (U1~U4 는 실물 로직을 검증한다)

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
                  post_url=f"https://tv.com/{url}", post_age_minutes=2000,
                  collected_at=now - 600)  # 수집은 캔들(now-120~) 이전 — major3 클립과 정합
        lv["signal_key"] = db.make_signal_key("LINK", entry, author, url)
        db.upsert_level(conn, lv)

sent_messages = []
telegram.send = lambda text: sent_messages.append(text) or True

fake = {"price": None, "low": None, "high": None, "candles": None}
upbit.fetch_prices = lambda mkts, t: {m: (USDT_KRW if m == "KRW-USDT" else fake["price"]) for m in mkts}

def _fake_range(m, mins, t):
    if fake["candles"] is not None:
        return fake["candles"]
    if fake["low"] is None and fake["high"] is None:
        return None
    # 기본: 직전 1~2분 사이의 캔들 1개 (end 가 최근이라 터치 이후 판정에 포함됨)
    return [(now - 120, now - 60,
             fake["high"] or fake["price"], fake["low"] or fake["price"])]
upbit.fetch_range_since = _fake_range
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

# ── U1~U4: fetch_range_since 시간 기반 재설계 회귀 테스트 (2026-07-26 감사 #9) ──
# 업비트 캔들 API를 흉내 낸 가짜 requests.get 으로, HTTP 호출 없이 검증한다.
import requests as _requests_mod
from datetime import datetime as _dt, timezone as _tz

_orig_requests_get = _requests_mod.get


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _iso(ts):
    return _dt.fromtimestamp(ts, tz=_tz.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _candle(ts, unit, high, low):
    return {"candle_date_time_utc": _iso(ts), "high_price": high, "low_price": low}


upbit._CANDLE_PACE_SEC = 0.0  # 테스트 속도 - 실제 페이싱 로직 검증과 무관

# U1: 정상 유동성(매 분마다 거래) - count 만큼 1콜에 다 들어오고, 요청 구간을
#     벗어나지 않는다.
_u1_calls = []
def _u1_get(url, params=None, timeout=None):
    _u1_calls.append(params)
    now_ts = time.time()
    unit = 1
    n = params["count"]
    to_ts = now_ts if "to" not in params else \
        _dt.strptime(params["to"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=_tz.utc).timestamp()
    payload = [_candle(to_ts - (i + 1) * 60, unit, 100.0, 90.0) for i in range(n)]
    return _FakeResp(payload)

_requests_mod.get = _u1_get
rng_u1 = _real_fetch_range_since("KRW-TEST", 45, 5.0)
check("U1 정상유동성 - 1콜로 종결", len(_u1_calls) == 1)
check("U1 정상유동성 - 캔들 반환됨", bool(rng_u1) and len(rng_u1) >= 40)
if rng_u1:
    span_min = (rng_u1[-1][1] - rng_u1[0][0]) / 60.0
    check("U1 정상유동성 - 의도한 45분 구간 이내", span_min <= 46)

# U2: 저유동성(45분 요청인데 실제 거래는 드문드문 - count개가 훨씬 먼 과거까지
#     뭉쳐서 온다) - 시간 필터로 target_start 이전 캔들은 전부 제거되어야 한다
#     (예전 버그: 이 필터가 없어 SUN 31시간 확대 같은 사례가 나왔다).
_u2_calls = []
def _u2_get(url, params=None, timeout=None):
    _u2_calls.append(params)
    now_ts = time.time()
    n = params["count"]
    # 실제 거래가 45분에 1번씩만 있었다고 가정 -> count(45)개면 총 45*45분 과거까지 확대
    payload = [_candle(now_ts - (i + 1) * 45 * 60, 1, 100.0, 90.0) for i in range(n)]
    return _FakeResp(payload)

_requests_mod.get = _u2_get
rng_u2 = _real_fetch_range_since("KRW-SPARSE", 45, 5.0)
target_start_u2 = time.time() - 45 * 60
check("U2 저유동성 - 목표 시각 이전 캔들 없음",
      all(c[0] >= target_start_u2 - 1 for c in (rng_u2 or [])))
check("U2 저유동성 - 여전히 1콜(추가페이지 불필요, count 자체가 이론상 상한)",
      len(_u2_calls) == 1)

# U3: count 가 부족한 극단 상황(방어적 안전판) - 1페이지가 목표 시각까지 못 닿으면
#     'to' 를 당겨 추가 페이지를 조회하되, 최종 결과는 여전히 목표 시각을 넘지 않는다.
_u3_calls = []
def _u3_get(url, params=None, timeout=None):
    _u3_calls.append(dict(params))
    now_ts = time.time()
    n = params["count"]
    if "to" not in params:
        # 1페이지: 목표 시각(45분 전)에 훨씬 못 미치는 최근 5분치만 반환
        payload = [_candle(now_ts - (i + 1) * 60, 1, 100.0, 90.0) for i in range(min(n, 5))]
    else:
        to_ts = _dt.strptime(params["to"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=_tz.utc).timestamp()
        payload = [_candle(to_ts - (i + 1) * 60, 1, 100.0, 90.0) for i in range(n)]
    return _FakeResp(payload)

_requests_mod.get = _u3_get
rng_u3 = _real_fetch_range_since("KRW-THIN", 45, 5.0)
target_start_u3 = time.time() - 45 * 60
check("U3 방어적 페이지네이션 - 여러 페이지 조회됨", len(_u3_calls) > 1)
check("U3 방어적 페이지네이션 - 최대 페이지 상한 준수", len(_u3_calls) <= upbit._RANGE_MAX_PAGES)
check("U3 방어적 페이지네이션 - 그래도 목표 시각 이내",
      all(c[0] >= target_start_u3 - 1 for c in (rng_u3 or [])))

# U4: 조회 실패는 예전과 동일하게 None
def _u4_get(url, params=None, timeout=None):
    raise Exception("network down")

_requests_mod.get = _u4_get
rng_u4 = _real_fetch_range_since("KRW-FAIL", 45, 5.0)
check("U4 조회 실패 - None 반환(기존과 동일)", rng_u4 is None)

_requests_mod.get = _orig_requests_get

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
      and "\n🏹 승률73% (8승3패)" in msg_a and "✍️ 작성자:" in msg_a)
msg_b = tg.render_alert("touch", "LINK", [dict(
    coin_symbol="LINK", entry_usd=8.3, sl_usd=None, tp_usd=None, rr=None, grade="C", score=45,
    author="NewComer", author_followers=2300, author_hit_rate=None, author_hit_count=None,
    author_whitelisted=False, mcap_rank=19, mcap_tier_icon="🥇", post_url="https://tv.com/b",
    post_age_minutes=60, collected_at=now, author_self_wins=4, author_self_losses=2)],
    8.35 * USDT_KRW, USDT_KRW)
check("T14b 자체만 (워쳐없음)", "🏹 승률67% (4승2패)" in msg_b and "기록없음" not in msg_b)

# ── 2026-07-24 감사 수정 검증 ──────────────────────────────────
# T15: 여러 캔들에 걸쳐 TP 먼저 → SL 나중이면 순서대로 hit (뭉개면 가짜 ambiguous였음)
lid15 = add_touched("LINK", 10.0, 9.0, 11.0, 7200, "t15")
fake["price"] = 9.1 * USDT_KRW
fake["candles"] = [
    (now - 600, now - 540, 11.2 * USDT_KRW, 10.8 * USDT_KRW),  # TP 도달 캔들
    (now - 540, now - 480, 10.9 * USDT_KRW, 8.9 * USDT_KRW),   # 이후 SL 캔들
]
price_check.run_once(now + 1020)
o15 = outcome_of(lid15)
check("T15 순서 확정 - TP 먼저는 hit", o15["outcome"] == "hit" and o15["ambiguous"] == 0)

# T16: 터치 '이전' 캔들의 고가는 판정에서 제외 (가짜 hit 방지)
lid16 = add_touched("LINK", 10.0, 9.0, 10.5, 30, "t16")  # 30초 전 터치
fake["price"] = 9.9 * USDT_KRW
fake["candles"] = [(now - 600, now - 300, 12.0 * USDT_KRW, 9.8 * USDT_KRW)]  # 전부 터치 이전
price_check.run_once(now + 1080)
o16 = outcome_of(lid16)
check("T16 터치이전 캔들 제외 - 미종결", o16["outcome"] is None)
fake["candles"] = None

# T17: 불변 스냅샷 - touched 레벨은 재수집 upsert 로 sl/tp 가 안 바뀜
with db.connect(TEST_DB) as conn:
    row = conn.execute("SELECT signal_key, tp_usd FROM levels WHERE id=?", (lid12,)).fetchone()
    lv = dict(coin_symbol="LINK", ticker="KRW-LINK", direction="long", entry_usd=10.0,
              sl_usd=8.0, tp_usd=99.0, rr=1.0, grade="A", score=80, author="A_t12",
              author_followers=1, author_hit_rate=None, author_hit_count=None,
              author_whitelisted=False, mcap_rank=1, mcap_tier_icon="💎",
              post_url="https://tv.com/t12", post_age_minutes=1, collected_at=now,
              signal_key=row["signal_key"])
    db.upsert_level(conn, lv)
    after = conn.execute("SELECT tp_usd FROM levels WHERE id=?", (lid12,)).fetchone()
check("T17 불변스냅샷 - touched 레벨 tp 유지", abs(after["tp_usd"] - 14.0) < 1e-9)

# T18: 오염 방어선 - 서수 오인 tp(=1.0)·엔트리 위 sl 은 '없음' 취급 → 즉시 가짜 판정 방지
#      (방어선 없으면 tp_krw=1400 < 현재가라 스냅샷 폴백이 즉시 가짜 hit 을 기록한다)
lid18 = add_touched("LINK", 10.0, 15.0, 1.0, 3600, "t18")
fake["price"] = 10.5 * USDT_KRW; fake["low"] = 10.2 * USDT_KRW; fake["high"] = 10.8 * USDT_KRW
price_check.run_once(now + 700)
o18 = outcome_of(lid18)
check("T18 오염 방어선 - 불량 tp/sl 무시·미종결", o18["outcome"] is None)

# T19: 터치 앵커 - TP 스윕과 터치가 같은(진행 중) 캔들에서 난 급락에서, 캔들 고가
#      (터치 이전 가격)가 판정에 못 섞이고 touched_at 이 캔들 종료시각으로 앵커됨
with db.connect(TEST_DB) as conn:
    lv19 = dict(coin_symbol="LINK", ticker="KRW-LINK", direction="long", entry_usd=10.0,
                sl_usd=9.0, tp_usd=12.0, rr=2.0, grade="B", score=60, author="A_t19",
                author_followers=1, author_hit_rate=None, author_hit_count=None,
                author_whitelisted=False, mcap_rank=50, mcap_tier_icon="🥇",
                post_url="https://tv.com/t19", post_age_minutes=10, collected_at=now)
    lv19["signal_key"] = db.make_signal_key("LINK", 10.0, "A_t19", lv19["post_url"])
    db.upsert_level(conn, lv19)
fake["price"] = 10.1 * USDT_KRW
fake["candles"] = [(now + 740, now + 800, 12.5 * USDT_KRW, 9.9 * USDT_KRW)]
price_check.run_once(now + 780)   # 캔들 진행 중(end 800 > 780)에 터치 감지
price_check.run_once(now + 900)   # 캔들 완성 후에도 터치 캔들은 판정 제외
with db.connect(TEST_DB) as conn:
    r19 = conn.execute("SELECT outcome, touched_at FROM levels WHERE signal_key=?",
                       (lv19["signal_key"],)).fetchone()
check("T19 터치캔들 고가 미오염 + 종료시각 앵커",
      r19["outcome"] is None and abs(r19["touched_at"] - (now + 800)) < 1e-6)
fake["candles"] = None

# T20: 수집 전 캔들 차단 - 수집 이전 저가(9.0)로는 터치 안 되고, 수집 이후 캔들(9.4)로
#      터치되며 앵커도 그 캔들 종료시각 (감사 major3: 레벨 존재 전 가격 판정 차단)
with db.connect(TEST_DB) as conn:
    lv20 = dict(coin_symbol="LINK", ticker="KRW-LINK", direction="long", entry_usd=9.5,
                sl_usd=8.5, tp_usd=12.0, rr=2.5, grade="B", score=60, author="A_t20",
                author_followers=1, author_hit_rate=None, author_hit_count=None,
                author_whitelisted=False, mcap_rank=50, mcap_tier_icon="🥇",
                post_url="https://tv.com/t20", post_age_minutes=10,
                collected_at=now + 1000)
    lv20["signal_key"] = db.make_signal_key("LINK", 9.5, "A_t20", lv20["post_url"])
    db.upsert_level(conn, lv20)
fake["price"] = 9.8 * USDT_KRW
fake["candles"] = [(now + 840, now + 900, 9.6 * USDT_KRW, 9.0 * USDT_KRW),
                   (now + 1020, now + 1080, 9.6 * USDT_KRW, 9.4 * USDT_KRW)]
price_check.run_once(now + 1100)
with db.connect(TEST_DB) as conn:
    r20 = conn.execute("SELECT status, touched_at FROM levels WHERE signal_key=?",
                       (lv20["signal_key"],)).fetchone()
check("T20 수집 전 캔들 차단 + 수집 후 캔들 앵커",
      r20["status"] == "touched" and abs(r20["touched_at"] - (now + 1080)) < 1e-6)
fake["candles"] = None

# T21: 2026-07-26 재감사 minor10 - 판정 루프가 DB순(id순)이 아니라 창 만료
#      임박순으로 순회해야, 캔들 예산 고갈 시 특정 티커가 계속 뒤로 밀리지 않는다.
#      남은시간이 다른 세 티커를 두고 get_range 호출 순서가 임박순(오름차순)인지 확인.
add_touched("URGB", 10.0, None, None, 1000, "t21b")                  # 기본창(168h) - 여유 큼
add_touched("URGA", 10.0, None, None, 3500, "t21a", window_h=1.0)    # 1h창 - 곧 만료(가장 급함)
add_touched("URGC", 10.0, None, None, 1000, "t21c", window_h=2.0)    # 2h창 - 중간
call_order = []
def _stub_range(ticker, limit):
    call_order.append(ticker)
    return None
test_prices = {"KRW-URGA": 10.0 * USDT_KRW, "KRW-URGB": 10.0 * USDT_KRW, "KRW-URGC": 10.0 * USDT_KRW}
with db.connect(TEST_DB) as conn:
    price_check._judge_outcomes(conn, test_prices, USDT_KRW, _stub_range, now, settings.get)
order = [t for t in call_order if t in ("KRW-URGA", "KRW-URGB", "KRW-URGC")]
check("T21 판정 루프 임박순 정렬", order == ["KRW-URGA", "KRW-URGC", "KRW-URGB"])

# ── T22: 등급 재평가(freeze 결함 수정) ──────────────────────────
# 수집 시 가격이 멀어 D등급(근접도 0점)으로 저장된 레벨이, 체크 시점 가격이
# entry 에 근접하면 재채점돼 필터를 통과해야 한다(2026-07-26 감사: 터치 52건 중
# 18건이 이 결함으로 억제됨). alert_min_grade 는 기본 'C'.
from collector import grading as _grading
with db.connect(TEST_DB) as conn:
    lv22 = dict(coin_symbol="EGLD", ticker="KRW-EGLD", direction="long",
                entry_usd=20.0, sl_usd=18.0, tp_usd=26.0, rr=3.0,
                grade="D", score=13,  # 수집 당시(가격 멂) D등급으로 저장됨
                author="AuthE", author_followers=100,
                author_hit_rate=None, author_hit_count=None, author_whitelisted=False,
                mcap_rank=80, mcap_tier_icon="🥉", post_url="https://tv.com/u22",
                post_age_minutes=500, collected_at=now - 600)
    lv22["signal_key"] = db.make_signal_key("EGLD", 20.0, "AuthE", "u22")
    db.upsert_level(conn, lv22)
fake["low"] = fake["high"] = fake["candles"] = None  # 이전 테스트 잔여값 초기화
fake["price"] = 20.0 * USDT_KRW * 1.006  # entry 대비 +0.6% - 예고 밴드 이내, 근접도 20점권
sent_messages.clear()
s22 = price_check.run_once(now + 1200)
check("T22 재채점 - 원거리땐 D였던 레벨이 근접 시 알림 통과(예고/터치 무관)",
      (s22["previews"] + s22["touches"]) == 1 and len(sent_messages) == 1
      and "D등급" not in sent_messages[0])
with db.connect(TEST_DB) as conn:
    row22 = conn.execute("SELECT grade, score FROM levels WHERE signal_key=?",
                         (lv22["signal_key"],)).fetchone()
check("T22 DB 원본 등급/점수는 보존(불변) - 필터용 재계산은 in-memory만",
      row22["grade"] == "D" and abs(row22["score"] - 13) < 1e-9)
# regrade_current 단위 계산 검증: entry=20, current=20.12 -> diff 0.6% (<2%) -> +20점
g22, s22score, _ = _grading.regrade_current(lv22, fake["price"] / USDT_KRW)
check("T22 regrade_current 함수 자체 계산 정합", g22 in ("S", "A", "B") and s22score > 13)

# ── T23: 수집 급감 경고 (조용한 고장 감지) ───────────────────────
with db.connect(TEST_DB) as conn:
    conn.execute("DELETE FROM meta WHERE key='collect_silence_warned_date'")
    conn.execute("DELETE FROM levels")
    base_collect = now - 25 * 3600  # 24h 감시창 밖(=25h 전)부터 과거로 7일 평균 채움
    for i in range(14):  # 직전 7일 동안 이틀에 한 번꼴 수집 -> 평균 2건/일
        conn.execute(
            "INSERT INTO levels (signal_key, coin_symbol, ticker, direction, status, collected_at) "
            "VALUES (?, 'X', 'KRW-X', 'long', 'expired', ?)",
            (f"silence_seed_{i}", base_collect - i * 12 * 3600))
sent_messages.clear()
with db.connect(TEST_DB) as conn:
    s23 = price_check._check_collect_silence(conn, now, settings.get)
check("T23 24h 무수집+평년 수집있음 - 경고 발송", s23 is True and len(sent_messages) == 1
      and "수집 급감" in sent_messages[0])

with db.connect(TEST_DB) as conn:
    s23b = price_check._check_collect_silence(conn, now + 30, settings.get)
check("T23b 같은 날 재호출 - 중복 억제(하루 1회)", s23b is False and len(sent_messages) == 1)

with db.connect(TEST_DB) as conn:
    conn.execute("DELETE FROM meta WHERE key='collect_silence_warned_date'")
    conn.execute("DELETE FROM levels")  # 원래도 조용했던 기간(신규 프로젝트) - 오탐 방지
sent_messages.clear()
with db.connect(TEST_DB) as conn:
    s23c = price_check._check_collect_silence(conn, now, settings.get)
check("T23c 원래 조용한 기간(직전 평균도 0) - 오탐 없음", s23c is False and not sent_messages)

with db.connect(TEST_DB) as conn:
    conn.execute("DELETE FROM meta WHERE key='collect_silence_warned_date'")
    conn.execute(
        "INSERT INTO levels (signal_key, coin_symbol, ticker, direction, status, collected_at) "
        "VALUES ('silence_recent', 'X', 'KRW-X', 'long', 'expired', ?)", (now - 3600,))
sent_messages.clear()
with db.connect(TEST_DB) as conn:
    s23d = price_check._check_collect_silence(conn, now, settings.get)
check("T23d 최근 24h 내 수집 있음 - 정상(무경고)", s23d is False and not sent_messages)

# T24(목표 거리 감점 단독 검증)는 scripts/test_grading.py 로 이관됐다 —
# 2026-07-26 등급 배점 재조정으로 "SL 없음 + TP +10%" 픽스처가 더 이상 '감점 0
# 기준선'이 아니게 됐고(대체 가점 +20이 붙음), T24b 의 기대값("SL 없고 TP +10% 인
# 글은 C 가 한계")은 이번 결정으로 없애기로 한 규칙 그 자체라 무효가 됐다.
# 배점표 검증은 test_grading.py G2/G6/G7c 가 담당한다.

# T25: 텔레그램 HTML 안전성 — 렌더 결과에 이스케이프 안 된 '<' 가 있으면
#      parse_mode=HTML 발송이 400 으로 실패한다(2026-07-26 주간리포트 첫 발송 실패 원인)
import re as _re  # noqa: E402
def _unescaped_lt(text):
    """허용 태그(<b> </b> <a href=...> </a>)를 제거한 뒤 남은 '<' 를 찾는다."""
    stripped = _re.sub(r'</?(?:b|i|u|s|code|pre|a)(?:\s[^<>]*)?>', '', text)
    return '<' in stripped
_rep25 = dict(coin_symbol="LINK", ticker="KRW-LINK", direction="long", entry_usd=10.0,
              sl_usd=9.0, tp_usd=12.0, grade="B", score=60, author="A<script>",
              author_followers=10, author_hit_rate=0.7, author_hit_count=9,
              author_whitelisted=0, mcap_rank=19, mcap_tier_icon="🥇",
              post_url="https://tv.com/x", post_age_minutes=100)
check("T25 알림 렌더 HTML 안전",
      not _unescaped_lt(telegram.render_alert("touch", "LINK", [_rep25], 14000.0, 1400)))
check("T25b 급감경고 HTML 안전",
      not _unescaped_lt(telegram.render_collect_silence_alert(24, 9.0)))

# ── T26~T28: 관찰 집계 (스프린트5 "알림량 관찰기") ────────────────────
# 지금까지(T1~T22) 실제로 발생한 원시 이벤트를 손계산해 절대값으로 검증한다.
# touches_total: T4(LINK 8.30 클러스터 터치) + T6(7.50 직터치) + T7(7.00, 상한억제돼도
#                raw 이벤트로는 터치) + T19(캔들 앵커 검증용 신규 LINK 10.0, 상한억제)
#                + T20(수집전 캔들 차단 검증용 신규 LINK 9.5, 상한억제) = 5
#                (T8~T18/T21은 add_touched() 로 곧장 'touched' 상태로 꽂아 활성
#                레벨 루프를 타지 않는다 — 재채점/집계 대상이 아니라 raw 카운트 무관)
# previews_total: T2(LINK 예고) + T3(중복 예고 시도) + T22(EGLD 재채점 예고) = 3
# suppressed_dup: T3 하나(이미 예고된 클러스터 재시도)
# suppressed_cap: T7 + T19 + T20 = 3건(모두 일일상한 2건을 이미 채운 LINK)
# suppressed_grade / suppressed_send_fail 은 이 구간엔 발생 안 함(T27/T28에서 별도 검증)
obs_day = price_check._day_kst(now)


def _obs_row(day=None):
    with db.connect(TEST_DB) as conn:
        rows = db.get_daily_stats(conn, days=60)
    return next((r for r in rows if r["day_kst"] == (day or obs_day)), None)


row26 = _obs_row()
check("T26 관찰집계 - 터치 raw 5건(필터 무관)", row26 is not None and row26["touches_total"] == 5)
check("T26b 관찰집계 - 예고 raw 3건(필터 무관)", row26["previews_total"] == 3)
check("T26c 관찰집계 - 중복예고 억제 1건", row26["suppressed_dup"] == 1)
check("T26d 관찰집계 - 일일상한 억제 3건", row26["suppressed_cap"] == 3)
check("T26e 관찰집계 - 등급미달/발송실패는 아직 0건(T27/T28에서 검증)",
      row26["suppressed_grade"] == 0 and row26["suppressed_send_fail"] == 0
      and row26["suppressed_grade_tp_penalty_only"] == 0)

# T27: 등급 미달(재채점해도 D)로 억제되는 경우 - 방금 들어간 TP 근접도 감점 효과를
# 재는 핵심 지표라 별도로 정확히 검증한다. followers=1(+1) / SL 없음(rr 미계산,+0) /
# 근접도 abs<2%(+20) / TP+0.5%로 초근접 감점(-6) / 완결성 has_entry+target(+20, has_stop
# 없어 +10 없음) = 35점 -> D. min_grade='C' 라 필터 탈락.
# 이 케이스는 감점(-6)을 되돌리면 41점 -> C가 돼 min_grade('C')를 통과한다 - 즉
# "TP 감점 때문에 억제된" 표본이라 suppressed_grade_tp_penalty_only 도 함께 +1돼야 함
# (2026-07-26 사용자 결정: 감점 효과를 분리 측정).
with db.connect(TEST_DB) as conn:
    lv27 = dict(coin_symbol="ZGRD", ticker="KRW-ZGRD", direction="long",
                entry_usd=5.0, sl_usd=None, tp_usd=5.025, rr=None, grade="B", score=60,
                author="Auth27", author_followers=1, author_hit_rate=None,
                author_hit_count=None, author_whitelisted=False, mcap_rank=190,
                mcap_tier_icon="🥉", post_url="https://tv.com/u27", post_age_minutes=10,
                collected_at=now - 600)
    lv27["signal_key"] = db.make_signal_key("ZGRD", 5.0, "Auth27", "u27")
    db.upsert_level(conn, lv27)
fake["low"] = fake["high"] = fake["candles"] = None
fake["price"] = 5.0 * USDT_KRW * 0.999  # entry 대비 -0.1% - 터치 + 근접도 만점권
sent_before27 = len(sent_messages)
price_check.run_once(now + 1310)
row27 = _obs_row()
check("T27 등급미달 - 무알림", len(sent_messages) == sent_before27)
check("T27b 관찰집계 - 터치 raw +1(5→6), 등급미달 억제 +1",
      row27["touches_total"] == 6 and row27["suppressed_grade"] == 1)
check("T27c 관찰집계 - TP감점 되돌리면 통과했을 건 +1 (suppressed_grade 의 부분집합)",
      row27["suppressed_grade_tp_penalty_only"] == 1)

# T27d: 대조군 - TP 자체가 없어(has_target 없음) 애초에 목표거리 감점이 적용되지
# 않은 등급미달 건. suppressed_grade 는 늘지만 tp_penalty_only 는 늘면 안 된다
# (부분집합이지 suppressed_grade 와 항상 같이 움직이는 게 아님을 증명).
# followers=1(+1) / rr 없음(target 없어 계산불가,+0) / 근접도 abs<2%(+20) / TP감점
# 없음(target 없어 스킵,+0) / 완결성 has_entry만(+8) = 29점 -> D, 되돌릴 감점이 없다.
with db.connect(TEST_DB) as conn:
    lv27d = dict(coin_symbol="ZBAD", ticker="KRW-ZBAD", direction="long",
                 entry_usd=3.0, sl_usd=2.8, tp_usd=None, rr=None, grade="B", score=60,
                 author="Auth27d", author_followers=1, author_hit_rate=None,
                 author_hit_count=None, author_whitelisted=False, mcap_rank=190,
                 mcap_tier_icon="🥉", post_url="https://tv.com/u27d", post_age_minutes=10,
                 collected_at=now - 600)
    lv27d["signal_key"] = db.make_signal_key("ZBAD", 3.0, "Auth27d", "u27d")
    db.upsert_level(conn, lv27d)
fake["price"] = 3.0 * USDT_KRW * 0.999
price_check.run_once(now + 1315)
row27d = _obs_row()
check("T27e 대조군 - TP 없는 등급미달은 suppressed_grade 만 +1, tp_penalty_only 불변",
      row27d["suppressed_grade"] == 2 and row27d["suppressed_grade_tp_penalty_only"] == 1)

# T28: 필터는 통과했지만 텔레그램 발송 자체가 실패하는 경우 - 등급미달과는 다른
# 사유로 별도 집계돼야 한다(둘 다 합치면 "왜 안 갔는지"를 못 가른다).
with db.connect(TEST_DB) as conn:
    lv28 = dict(coin_symbol="ZSND", ticker="KRW-ZSND", direction="long",
                entry_usd=10.0, sl_usd=9.4, tp_usd=11.5, rr=2.4, grade="B", score=62,
                author="Auth28", author_followers=5000, author_hit_rate=0.67,
                author_hit_count=12, author_whitelisted=False, mcap_rank=19,
                mcap_tier_icon="🥇", post_url="https://tv.com/u28", post_age_minutes=2000,
                collected_at=now - 600)
    lv28["signal_key"] = db.make_signal_key("ZSND", 10.0, "Auth28", "u28")
    db.upsert_level(conn, lv28)
fake["low"] = fake["high"] = fake["candles"] = None
fake["price"] = 10.0 * USDT_KRW * 0.999  # 터치 + 등급 통과권(S) - 발송만 실패시킴
_prev_send = telegram.send
telegram.send = lambda text: False
sent_before28 = len(sent_messages)
price_check.run_once(now + 1320)
telegram.send = _prev_send
row28 = _obs_row()
check("T28 발송실패 - 메시지 미기록", len(sent_messages) == sent_before28)
check("T28b 관찰집계 - 터치 raw +1(7→8), 발송실패 억제 +1", row28["touches_total"] == 8
      and row28["suppressed_send_fail"] == 1)
check("T28c 발송실패는 등급미달과 별도 집계(등급미달/TP감점 카운트 불변)",
      row28["suppressed_grade"] == 2 and row28["suppressed_grade_tp_penalty_only"] == 1)

# ── T29: 관찰 집계 DB 함수 단위 검증 (임시 DB, 손계산·프로덕션 DB 미접근) ──────
TEST_DB29 = "cache/_test_obs.db"
if os.path.exists(TEST_DB29):
    os.remove(TEST_DB29)
db.init_db(TEST_DB29)
from datetime import datetime as _dt29, timezone as _tz29, timedelta as _td29  # noqa: E402
_KST29 = _tz29(_td29(hours=9))


def _kst_ts(y, m, d, hh=12):
    return _dt29(y, m, d, hh, 0, tzinfo=_KST29).timestamp()


with db.connect(TEST_DB29) as conn:
    db.bump_daily_stats(conn, "2026-07-20", touches_total=3, suppressed_grade=1)
    db.bump_daily_stats(conn, "2026-07-20", previews_total=2, suppressed_grade=1)  # 누적 확인
    db.bump_daily_stats(conn, "2026-07-19", touches_total=1)
    rows29 = db.get_daily_stats(conn, days=60)
r20 = next(r for r in rows29 if r["day_kst"] == "2026-07-20")
check("T29 관찰DB - 같은 날 재호출은 누적(교체 아님)", r20["touches_total"] == 3
      and r20["suppressed_grade"] == 2 and r20["previews_total"] == 2)

with db.connect(TEST_DB29) as conn:
    db.bump_daily_stats(conn, "2026-07-18")  # 델타 전부 0
    rows29b = db.get_daily_stats(conn, days=60)
check("T29b 전부 0인 델타는 행을 만들지 않음(쓰기 생략)",
      not any(r["day_kst"] == "2026-07-18" for r in rows29b))

with db.connect(TEST_DB29) as conn:
    conn.execute("INSERT INTO levels (signal_key, coin_symbol, ticker, direction, status, "
                 "collected_at) VALUES ('c1','X','KRW-X','long','watching', ?)",
                 (_kst_ts(2026, 7, 20),))
    conn.execute("INSERT INTO levels (signal_key, coin_symbol, ticker, direction, status, "
                 "collected_at) VALUES ('c2','X','KRW-X','long','watching', ?)",
                 (_kst_ts(2026, 7, 20, 23),))
    conn.execute("INSERT INTO levels (signal_key, coin_symbol, ticker, direction, status, "
                 "collected_at) VALUES ('c3','X','KRW-X','long','watching', ?)",
                 (_kst_ts(2026, 7, 19),))
    db.record_alert(conn, "X", "touch", [1], "2026-07-20", now=_kst_ts(2026, 7, 20))
    db.record_alert(conn, "X", "preview", [1], "2026-07-20", now=_kst_ts(2026, 7, 20))
    db.record_alert(conn, "X", "touch", [1], "2026-07-19", now=_kst_ts(2026, 7, 19))
    collected29 = db.get_collected_counts_by_day(conn, days=60)
    sent29 = db.get_alerts_sent_by_day(conn, days=60)
check("T29c 일자별 신규수집 건수(KST 경계, 원본 재사용)",
      collected29.get("2026-07-20") == 2 and collected29.get("2026-07-19") == 1)
check("T29d 일자별 발송 건수(alerts_log 재사용, 중복저장 없음)",
      sent29.get("2026-07-20") == 2 and sent29.get("2026-07-19") == 1)

with db.connect(TEST_DB29) as conn:
    report29 = db.get_observation_report(conn, days=60)
rep20 = next(r for r in report29 if r["day_kst"] == "2026-07-20")
check("T29e 통합 조회함수 - 수집/발송/집계 필드 결합 정합",
      rep20["collected"] == 2 and rep20["alerts_sent"] == 2
      and rep20["touches_total"] == 3 and rep20["suppressed_grade"] == 2)

with db.connect(TEST_DB29) as conn:
    db.bump_daily_stats(conn, "2020-01-01", touches_total=9)
    removed29 = db.prune_daily_stats(conn, now=time.time(), keep_days=60)
    remaining29 = {r["day_kst"] for r in db.get_daily_stats(conn, days=999)}
check("T29f 보존기간(60일) 초과분 삭제", removed29 >= 1 and "2020-01-01" not in remaining29)
check("T29g 보존기간 이내 최근 데이터는 유지", "2026-07-20" in remaining29)
os.remove(TEST_DB29)

# ── T30~T31: 클러스터 로직 리팩터 동등성 (monitor.price_check → analytics.clustering) ──
# 개발자A가 analytics/clustering.py 에 build_clusters() 를 만들며 같은 병합 규칙이
# 두 곳에 생겼다 — price_check._build_clusters 가 그쪽으로 위임하도록 리팩터하고,
# "리팩터 전 알고리즘의 스냅샷"과 결과가 100% 같은지 여러 케이스로 증명한다.


def _golden_build_clusters(levels, band_pct):
    """리팩터 전 monitor/price_check.py `_build_clusters` 원본 그대로의 스냅샷(회귀
    기준선). 리팩터 후 구현이 이 결과와 한 글자도 다르면 안 된다(알림 트리거 로직)."""
    with_entry = [l for l in levels if l.get("entry_usd")]
    with_entry.sort(key=lambda l: l["entry_usd"], reverse=True)
    clusters, used = [], set()
    for lv in with_entry:
        if lv["id"] in used:
            continue
        top = lv["entry_usd"]
        group = [l for l in with_entry
                 if l["id"] not in used and l["entry_usd"] >= top * (1 - band_pct / 100.0)]
        for g in group:
            used.add(g["id"])
        clusters.append(group)
    return clusters


def _lv30(i, entry):
    return dict(id=i, entry_usd=entry)


_cases30 = [
    ([], 1.0),
    ([_lv30(1, 100.0)], 1.0),
    ([_lv30(1, 100.0), _lv30(2, 99.5), _lv30(3, 98.0), _lv30(4, 97.9), _lv30(5, None)], 1.0),
    ([_lv30(1, 100.0), _lv30(2, 99.0), _lv30(3, 98.0)], 0.5),   # 전부 별개 클러스터
    ([_lv30(1, 100.0), _lv30(2, 99.0), _lv30(3, 98.0)], 2.5),   # 전부 한 클러스터
    ([_lv30(1, 100.0), _lv30(2, 99.0)], 1.0),                    # 정확히 경계값(99.0=100*0.99)
    ([_lv30(1, 50.0), _lv30(2, 50.0), _lv30(3, 50.0)], 0.01),    # 동일 entry 다건
    ([_lv30(i, 100.0 - i * 0.3) for i in range(1, 21)], 1.2),    # 20건 연쇄 병합
]
_all_match30 = True
for _lv_list, _band in _cases30:
    _got = [[l["id"] for l in c] for c in price_check._build_clusters(_lv_list, _band)]
    _want = [[l["id"] for l in c] for c in _golden_build_clusters(_lv_list, _band)]
    if _got != _want:
        _all_match30 = False
check("T30 클러스터 리팩터 - 모든 케이스에서 원본 알고리즘과 완전 동일", _all_match30)

_calls31 = []
_orig_bc = clustering.build_clusters
clustering.build_clusters = lambda *a, **kw: (_calls31.append(1) or _orig_bc(*a, **kw))
price_check._build_clusters([_lv30(1, 10.0)], 1.0)
clustering.build_clusters = _orig_bc
check("T31 price_check._build_clusters 가 analytics.clustering.build_clusters 로 실제 위임"
      "(재구현 아님)", len(_calls31) == 1)

# ── T32: TP 거리 감점 폭 로컬 재현 함수가 실제 grading.py 와 계속 일치하는지 교차검증 ──
# price_check._tp_distance_penalty 는 collector/grading.py 를 건드리지 않으려고
# 감점 규칙(2%/-6, 3%/-4, 5%/-2)을 로컬에 복제했다(2026-07-26). 개발자A 가 같은 날
# grading.py 의 등급 배점을 재조정 중이라 드리프트(값이 벌어짐) 위험이 있는데,
# 이 테스트가 실제 calculate_grade() 결과와 매번 대조해 드리프트를 즉시 잡아준다
# (거리별 점수차 = target 을 아주 멀리(감점 0) 뒀을 때와의 점수 차이).
from collector.grading import calculate_grade as _cg32  # noqa: E402


def _score32(tp_pct):
    """감점 폭만 순수하게 분리하기 위한 픽스처 — SL 을 반드시 둔다.

    2026-07-26 등급 배점 재조정 후, SL 없는 글에는 목표거리 '대체 가점'이 붙어
    (5%↑ 구간) 점수차로 감점만 역산할 수 없게 됐다. SL 을 목표거리의 1/10 로 두면
    rr=10 으로 고정돼 R:R·완결성·근접도 항이 전부 불변 → 점수차 = 감점 폭뿐."""
    target = 100.0 * (1 + tp_pct / 100.0)
    sl = 100.0 - (target - 100.0) / 10.0
    return _cg32(500, "long", 100.0, sl, target, 100.0)[1]


_far_score32 = _score32(8.0)  # 감점 0 기준선 (5%↑ 이라 감점 없음)


def _actual_penalty32(entry, target):
    tp_pct = (target - entry) / entry * 100.0
    return _far_score32 - _score32(tp_pct)


for _tp_pct32 in (0.5, 1.99, 2.0, 2.5, 2.99, 3.0, 4.0, 4.99, 5.0, 8.0):
    _target32 = 100.0 * (1 + _tp_pct32 / 100.0)
    _want32 = _actual_penalty32(100.0, _target32)
    _got32 = price_check._tp_distance_penalty("long", 100.0, _target32)
    check(f"T32 TP거리감점 로컬재현 일치 (tp_pct={_tp_pct32}%)", abs(_got32 - _want32) < 1e-9)

check("T32b entry/target 없으면 0 (되돌림 판정 스킵 조건)",
      price_check._tp_distance_penalty("long", None, 105.0) == 0
      and price_check._tp_distance_penalty("long", 100.0, None) == 0)
check("T32c 숏 방향도 동일 규칙", abs(price_check._tp_distance_penalty("short", 100.0, 98.5)
      - _actual_penalty32(100.0, 101.5)) < 1e-9)  # 숏 -1.5% == 롱 +1.5% 와 감점 대칭

print()
print("── 본알림 실제 렌더링 ──")
print(touch_msg)
os.remove(TEST_DB)
sys.exit(0 if ok else 1)