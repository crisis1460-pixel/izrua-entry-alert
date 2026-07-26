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

# T24: 목표 거리 감점 (2026-07-26 A안) — 초근접 TP 는 감점돼 알림에서 빠진다
from collector.grading import calculate_grade as _cg  # noqa: E402
_g_close, _s_close, _ = _cg(500, "long", 100.0, None, 101.5, 100.0)   # TP +1.5%
_g_far, _s_far, _ = _cg(500, "long", 100.0, None, 110.0, 100.0)       # TP +10%
check("T24 초근접 TP 감점 (-6점)", abs((_s_far - _s_close) - 6) < 1e-9)
check("T24b 감점으로 등급 하락", _g_close == "D" and _g_far == "C")
# 중간 구간(2~3%: -4 / 3~5%: -2)과 5%↑ 무감점
_, _s_25, _ = _cg(500, "long", 100.0, None, 102.5, 100.0)
_, _s_40, _ = _cg(500, "long", 100.0, None, 104.0, 100.0)
check("T24c 구간별 감점", abs((_s_far - _s_25) - 4) < 1e-9 and abs((_s_far - _s_40) - 2) < 1e-9)
# 숏 방향도 대칭 적용 (long 전용 버그 방지)
_, _s_short_close, _ = _cg(500, "short", 100.0, None, 98.5, 100.0)
_, _s_short_far, _ = _cg(500, "short", 100.0, None, 90.0, 100.0)
check("T24d 숏 대칭", abs((_s_short_far - _s_short_close) - 6) < 1e-9)
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

print()
print("── 본알림 실제 렌더링 ──")
print(touch_msg)
os.remove(TEST_DB)
sys.exit(0 if ok else 1)