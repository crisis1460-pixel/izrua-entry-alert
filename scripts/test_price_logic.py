# 가격체크 상태머신 오프라인 테스트 — 네트워크/텔레그램 없이 몽키패치로 검증.
import sys, time, os
from datetime import datetime, timezone, timedelta
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
# 2026-07-26 신규 외부 호출 2종(공지 폴링·호가 스냅샷)은 기본 OFF/스텁으로 두고
# 전용 블록(AN*/OB*)에서만 켠다 — 안 그러면 아래 T1~T23 의 run_once 가 업비트
# 공지·호가 API 를 실제로 때린다(이 테스트는 네트워크 없이 도는 것이 원칙).
settings.SETTINGS["announcement_alert_enabled"] = False
if os.path.exists(TEST_DB):
    os.remove(TEST_DB)
db.init_db(TEST_DB)

now = time.time()
# 자정 경계 가드(2026-07-27 실전 발견): 이 파일은 run_once(now+60 ~ now+1310)로
# 최대 22분 '미래' 시각을 쓰는데, KST 자정 직전(23:38~)에 실행되면 그 이벤트들이
# daily_stats 의 다음 날 행으로 갈라져 T26~T28 손계산 절대값이 어긋난다(검증 대상
# 로직과 무관한 시계 문제 — 실제로 23:39 통과 → 23:45 실패로 재현). 경계 30분
# 이내면 1시간 물러나 모든 상대 시각이 같은 KST 날짜 안에 머물게 한다.
_KST = timezone(timedelta(hours=9))
if datetime.fromtimestamp(now, _KST).date() != datetime.fromtimestamp(now + 1800, _KST).date():
    now -= 3600
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
_real_fetch_trades_window = upbit.fetch_trades_window  # BM-U1~ 에서 실물 로직 검증용

# 동시터치 재검사(Bar Magnifier)용 체결내역 — 기본값 None = "판별 불가"라
# 기존 T10(보수적 miss+ambiguous) 동작이 그대로 유지된다. BM 테스트가 값을 바꾼다.
fake_trades = {"list": None}
upbit.fetch_trades_window = lambda m, s, e, t, max_pages=4: fake_trades["list"]
_real_fetch_orderbook = upbit.fetch_orderbook_ratio  # OB2~OB4 에서 실물 로직 검증용
upbit.fetch_orderbook_ratio = lambda m, t: None  # 호가 스냅샷 기본 스텁 (OB* 에서 교체)
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

# T14: 자체 성적 병기 줄 (🏹 별도 줄) — 게이트는 R 트랙 유효표본 neff_r ≥ 5
# (2026-07-27 변경: 예전엔 neff_win/raw 폴백이라 SL 미기재 작성자도 통과했다 — T14g 참고)
from notify import telegram as tg
msg_a = tg.render_alert("touch", "LINK", [dict(
    coin_symbol="LINK", entry_usd=8.3, sl_usd=7.8, tp_usd=9.5, rr=2.4, grade="B", score=62,
    author="ProChartist", author_followers=None, author_hit_rate=0.72, author_hit_count=25,
    author_whitelisted=True, mcap_rank=19, mcap_tier_icon="🥇", post_url="https://tv.com/a",
    post_age_minutes=60, collected_at=now, author_self_wins=8, author_self_losses=3,
    author_self_neff_r=11.0, author_rank_min_neff=5.0)],
    8.35 * USDT_KRW, USDT_KRW)
check("T14 워쳐+자체 병기 (별도줄)", "📊 평균 적중률: 72% (워쳐 25건)" in msg_a
      and "\n🏹 승률73% (8승3패)" in msg_a and "✍️ 작성자:" in msg_a)
msg_b = tg.render_alert("touch", "LINK", [dict(
    coin_symbol="LINK", entry_usd=8.3, sl_usd=None, tp_usd=None, rr=None, grade="C", score=45,
    author="NewComer", author_followers=2300, author_hit_rate=None, author_hit_count=None,
    author_whitelisted=False, mcap_rank=19, mcap_tier_icon="🥇", post_url="https://tv.com/b",
    post_age_minutes=60, collected_at=now, author_self_wins=4, author_self_losses=2,
    author_self_neff_r=6.0, author_rank_min_neff=5.0)],
    8.35 * USDT_KRW, USDT_KRW)
check("T14b 자체만 (워쳐없음)", "🏹 승률67% (4승2패)" in msg_b and "기록없음" not in msg_b)

# T14g~T14i: 판정 비대칭 차단 (2026-07-27 사장님 확정)
# SL 미기재 글은 r_multiple 이 없어 neff_r=0 → 승률을 표시하지 않는다. 실측 근거:
# tp_only 13건 전승(100%) vs tp_sl 21건 중 4건(19%) — SL 이 없으면 지는 경로가 거의 없다.
# CryptoAnalystSignal 이 종결 12건 전부 tp_only 라 "승률100% (12승0패)" 로 나갈 참이었고
# (활성 5건 대기), 정작 역신호 경고는 neff_r 게이트라 그 작성자만 면제였다.
_asym = dict(coin_symbol="LINK", entry_usd=8.3, sl_usd=None, tp_usd=9.5, rr=None,
             grade="C", score=45, author="NoStopAuthor", author_followers=None,
             author_hit_rate=None, author_hit_count=None, author_whitelisted=True,
             mcap_rank=19, mcap_tier_icon="🥇", post_url="https://tv.com/g",
             post_age_minutes=60, collected_at=now,
             author_self_wins=12, author_self_losses=0, author_touched_n=12,
             author_untouched_expired=0, author_rank_min_neff=5.0)
msg_g = tg.render_alert("touch", "LINK",
                        [dict(_asym, author_self_neff=12.0, author_self_neff_r=0.0)],
                        8.35 * USDT_KRW, USDT_KRW)
check("T14g SL 미기재 작성자(neff_r=0)는 승률 미표시 - 12승0패가 새어나가지 않는다",
      "승률" not in msg_g and "12승0패" not in msg_g)
check("T14g2 승률만 빠지고 작성자 줄은 정상 렌더", "✍️ 작성자: @NoStopAuthor" in msg_g)
msg_h = tg.render_alert("touch", "LINK",
                        [dict(_asym, author_self_neff=12.0, author_self_neff_r=5.0)],
                        8.35 * USDT_KRW, USDT_KRW)
check("T14h R 표본이 게이트를 넘기면 정상 표시(과잉 차단 아님)",
      "🏹 승률100% (12승0패)" in msg_h)
msg_i = tg.render_alert("touch", "LINK", [dict(_asym)], 8.35 * USDT_KRW, USDT_KRW)
check("T14i 지표 미주입(구버전 경로)은 보수적 미표시 - 예전 raw 폴백이 그 구멍이었다",
      "승률" not in msg_i)

# T14c~T14f: 역신호 지표는 알림에 렌더하지 않는다 (2026-07-27 사용자 결정으로 되돌림).
# 같은 날 오전에 "🔻 역신호 후보 — …" 줄을 넣었다가 뺐다 — 알림 한 건이 이미 폰 화면을
# 넘겨서, 행이 늘면 정작 봐야 할 타점·가격이 밀린다. 지표는 계속 쌓이고 show_status
# 작성자 성적 섹션과 주간 리포트에서 본다. 여기서는 "지표를 주입해도 알림 본문은
# 불변"임을 못 박아, 나중에 누가 무심코 되살리지 않게 한다.
_anti = dict(coin_symbol="LINK", entry_usd=8.3, sl_usd=7.8, tp_usd=9.5, rr=2.4,
             grade="B", score=62, author="AntiSignal", author_followers=None,
             author_hit_rate=0.66, author_hit_count=64, author_whitelisted=True,
             mcap_rank=19, mcap_tier_icon="🥇", post_url="https://tv.com/c",
             post_age_minutes=60, collected_at=now,
             author_self_wins=2, author_self_losses=6, author_self_neff=8.0,
             author_self_neff_r=8.0, author_rank_min_neff=5.0)
msg_c = tg.render_alert("touch", "LINK", [dict(_anti, author_self_e_lb=-0.92)],
                        8.35 * USDT_KRW, USDT_KRW)
check("T14c 역신호 지표를 주입해도 알림 본문엔 안 나온다",
      "역신호" not in msg_c and "-0.92R" not in msg_c)
check("T14c2 확정 양식(워쳐 적중률·자체 승률)은 그대로 유지",
      "📊 평균 적중률: 66% (워쳐 64건)" in msg_c and "🏹 승률25% (2승6패)" in msg_c)

msg_d = tg.render_alert("touch", "LINK", [dict(_anti, author_self_e_lb=0.85)],
                        8.35 * USDT_KRW, USDT_KRW)
check("T14d E_LB 부호와 무관하게 본문 불변", "역신호" not in msg_d)
check("T14e 지표 유무로 줄 수가 달라지지 않는다",
      msg_c.count("\n") == msg_d.count("\n") == msg_a.count("\n"))
check("T14f 지표 미주입 경로도 동일", "역신호" not in msg_a)

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

# ── 체인(카드3): 이 시점까지 T8/T9/T10/T11/T13/T15/T19~T21 등 run_once/
#    _judge_outcomes 실경로로 쌓인 실제 판정 전체가 하나의 유효한 해시체인을
#    이루는지 확인한다(개별 단위테스트는 test_resilience.py 쪽, 여긴 실운영
#    경로 산출물 검증). T23부터는 테스트가 levels 테이블을 통째로 비우므로
#    그 전인 지금 확인해야 한다.
with db.connect(TEST_DB) as conn:
    _chain_v = db.verify_outcome_chain(conn)
    _chain_n = conn.execute(
        "SELECT COUNT(*) AS n FROM levels WHERE outcome IS NOT NULL"
    ).fetchone()["n"]
    _chain_hashed = conn.execute(
        "SELECT COUNT(*) AS n FROM levels WHERE outcome IS NOT NULL AND outcome_hash IS NOT NULL"
    ).fetchone()["n"]
check("체인: run_once/_judge_outcomes 실경로로 쌓인 전체 판정이 유효한 체인을 이룸",
      _chain_v is None)
check("체인: 종결된 판정 전부(N건) outcome_hash 보유(누락 없음)",
      _chain_n > 0 and _chain_n == _chain_hashed)

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
# 2026-07-26 감사 MAJOR-1 수정: previews_total 은 '새로 발생한 예고'만 센다.
# 예전엔 이미 예고된 클러스터가 밴드에 머무는 동안 매 회차 +1 이라, 체류 시간이
# 예고 건수로 둔갑했다(하루 머물면 최대 720배). 체류 회차는 preview_dwell 로 분리.
check("T26b 관찰집계 - 예고 raw 2건(중복 회차 제외)", row26["previews_total"] == 2)
check("T26c 관찰집계 - 밴드 체류 1회차(억제 아님)", row26["preview_dwell"] == 1)
check("T26c2 폐기된 suppressed_dup 은 더 이상 증가하지 않음", row26["suppressed_dup"] == 0)
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

# T29h: 구세대 DB 마이그레이션 — daily_stats 테이블이 이미 있는 DB에는
# CREATE TABLE IF NOT EXISTS 가 새 컬럼을 붙여주지 않아, _migrate 의
# PRAGMA table_info + ALTER 경로가 반드시 태워져야 한다(2026-07-26 ambiguous_*
# 추가 때 겪은 조용한 누락). ambiguous_skipped 분리도 같은 경로를 탄다.
TEST_DB29H = "cache/_test_migrate.db"
if os.path.exists(TEST_DB29H):
    os.remove(TEST_DB29H)
import sqlite3 as _sq29  # noqa: E402
_conn29h = _sq29.connect(TEST_DB29H)
_conn29h.execute("CREATE TABLE daily_stats (day_kst TEXT PRIMARY KEY, "
                 "touches_total INTEGER NOT NULL DEFAULT 0, updated_at REAL)")
_conn29h.commit()
_conn29h.close()
db.init_db(TEST_DB29H)
with db.connect(TEST_DB29H) as conn:
    cols29h = {r["name"] for r in conn.execute("PRAGMA table_info(daily_stats)").fetchall()}
    db.bump_daily_stats(conn, "2026-07-26", ambiguous_skipped=2, ambiguous_unresolved=1)
    row29h = db.get_daily_stats(conn, days=1)[0]
check("T29h 구세대 daily_stats 에 새 카운터 컬럼이 ALTER 로 추가됨",
      set(db._DAILY_STATS_COLS) <= cols29h)
check("T29h2 마이그레이션 직후 새 컬럼에 정상 누적",
      row29h["ambiguous_skipped"] == 2 and row29h["ambiguous_unresolved"] == 1)
os.remove(TEST_DB29H)

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

# ── BM: 동시터치 하위 타임프레임 재검사 (Bar Magnifier, 2026-07-26) ──────────
# 한 캔들 안에서 TP·SL 이 둘 다 닿으면 예전엔 무조건 보수적 miss+ambiguous 였다.
# 이제 그 구간의 체결내역(틱)으로 실제 도달 순서를 복원한다. 복원 실패 시에만
# 예전 동작(miss+ambiguous)으로 되돌아간다 = 안전한 실패(fail-safe).
_BM_C = (now - 600, now - 540)   # 동시터치가 일어난 캔들 구간
_bm_candles = [(_BM_C[0], _BM_C[1], 12.2 * USDT_KRW, 8.9 * USDT_KRW)]  # TP·SL 둘 다 관통


def _amb_totals():
    """동시터치 집계 3종의 현재 누계 — 회차 델타 측정용. 날짜 키로 찾지 않고 전 행을
    합산한다(테스트 실행이 KST 자정을 걸쳐도 흔들리지 않게)."""
    with db.connect(TEST_DB) as conn:
        rows = db.get_daily_stats(conn, days=60)
    return {k: sum(r.get(k, 0) or 0 for r in rows)
            for k in ("ambiguous_magnified", "ambiguous_unresolved", "ambiguous_skipped")}


_bm_delta = {}


def _bm_run(key, trades, offset, candles=None):
    """entry 10 / SL 9 / TP 12 짜리 터치 레벨 1건을 주어진 체결내역으로 판정.
    이 회차에 늘어난 동시터치 집계 델타를 _bm_delta 에 담아둔다(호출 직후 읽기)."""
    before = _amb_totals()
    lid = add_touched("LINK", 10.0, 9.0, 12.0, 7200, key)
    fake["price"] = 10.0 * USDT_KRW
    fake["candles"] = candles or _bm_candles
    fake_trades["list"] = trades
    price_check.run_once(now + offset)
    fake["candles"] = None
    fake_trades["list"] = None
    after = _amb_totals()
    _bm_delta.clear()
    _bm_delta.update({k: after[k] - before[k] for k in after})
    return outcome_of(lid)


def _amb_is(**want):
    """직전 회차 델타가 정확히 기대대로인지 — 지정하지 않은 카운터는 0 이어야 한다
    (한 사건이 두 칸에 동시에 잡히는 이중계산을 잡는다)."""
    return all(_bm_delta.get(k, 0) == want.get(k, 0) for k in _bm_delta)


_TP_K, _SL_K = 12.0 * USDT_KRW, 9.0 * USDT_KRW
_mid = _BM_C[0] + 10


def _amb_flagged():
    """보수적 처리로 ambiguous 표식이 붙은 레벨 수 — 집계 정합 대조군(BM9)."""
    with db.connect(TEST_DB) as conn:
        return conn.execute("SELECT COUNT(*) AS n FROM levels WHERE ambiguous=1").fetchone()["n"]


# BM 구간 진입 시점의 기준선. 누적 절대값이 아니라 이 구간의 '증가분'으로 대조한다
# (T23 이 levels 테이블을 통째로 비우는 구간이 앞에 있어 절대값끼리는 못 맞춘다).
_bm_base, _bm_flagged_base = _amb_totals(), _amb_flagged()

# BM1: 체결 순서상 TP 가 먼저 → hit 으로 정정되고 ambiguous 플래그도 안 선다
o_bm1 = _bm_run("bm1", [(_mid, 10.0 * USDT_KRW), (_mid + 5, _TP_K + 1),
                        (_mid + 20, _SL_K - 1)], 5000)
check("BM1 체결 재검사 - TP 선도달은 hit", o_bm1["outcome"] == "hit" and o_bm1["ambiguous"] == 0)
check("BM1b hit 정정 시 R-멀티플도 TP 기준(+2)",
      o_bm1["r_multiple"] is not None and abs(o_bm1["r_multiple"] - 2.0) < 0.01)
check("BM1-obs 집계 - 확정 건은 magnified 에만", _amb_is(ambiguous_magnified=1))

# BM2: SL 이 먼저 → 결론은 예전과 같은 miss 지만 '확정된' miss 라 ambiguous=0
o_bm2 = _bm_run("bm2", [(_mid, 10.0 * USDT_KRW), (_mid + 5, _SL_K - 1),
                        (_mid + 20, _TP_K + 1)], 5060)
check("BM2 체결 재검사 - SL 선도달은 확정 miss(ambiguous 해제)",
      o_bm2["outcome"] == "miss" and o_bm2["ambiguous"] == 0)
check("BM2-obs 집계 - miss 확정도 magnified", _amb_is(ambiguous_magnified=1))

# ── BM3~BM8 집계 분리 (2026-07-26 감사 minor): 결과는 모두 같은 보수적
# miss+ambiguous 지만, '체결내역을 보고도 못 가름'(unresolved)과 '아예 못
# 봄'(skipped)은 처방이 다르다 — 전자는 데이터 한계, 후자는 예산·스위치 설정
# 문제라 손댈 수 있다. 예전엔 둘이 unresolved 한 칸에 합쳐져 신뢰도 지표로
# 쓸 수 없었다. 판정 동작(outcome/ambiguous)은 이번 변경으로 바뀌지 않는다.

# BM3: 체결내역을 못 받으면(범위 밖·조회 실패·부분커버) 예전 보수적 처리 그대로
o_bm3 = _bm_run("bm3", None, 5120)
check("BM3 판별 불가 시 fail-safe - miss+ambiguous 유지",
      o_bm3["outcome"] == "miss" and o_bm3["ambiguous"] == 1)
check("BM3-obs 집계 - 조회는 시도했으므로 unresolved(비용 쓰고도 결론 없음)",
      _amb_is(ambiguous_unresolved=1))

# BM3b: 구간은 받았는데 TP·SL 어디에도 안 닿는 체결뿐이면(캔들 고저와 불일치)
#       억지로 결론 내지 않는다
o_bm3b = _bm_run("bm3b", [(_mid, 10.0 * USDT_KRW), (_mid + 5, 10.1 * USDT_KRW)], 5180)
check("BM3b 캔들 고저와 불일치하면 결론 보류(ambiguous 유지)",
      o_bm3b["outcome"] == "miss" and o_bm3b["ambiguous"] == 1)
check("BM3b-obs 집계 - 체결을 봤으나 못 가름 → unresolved",
      _amb_is(ambiguous_unresolved=1))

# BM4: 회차당 재검사 예산 — 2분 핫패스 보호. 예산 0이면 재검사 자체를 안 한다.
_bm_saved_budget = settings.SETTINGS["bar_magnifier_max_per_cycle"]
settings.SETTINGS["bar_magnifier_max_per_cycle"] = 0
o_bm4 = _bm_run("bm4", [(_mid + 5, _TP_K + 1)], 5240)
settings.SETTINGS["bar_magnifier_max_per_cycle"] = _bm_saved_budget
check("BM4 회차 예산 소진 시 재검사 생략(보수적 처리)",
      o_bm4["outcome"] == "miss" and o_bm4["ambiguous"] == 1)
check("BM4-obs 집계 - 예산 소진은 skipped(unresolved 아님)",
      _amb_is(ambiguous_skipped=1))

# BM5: 스위치 OFF 면 기능 전체가 비활성 (되돌리기 경로 보장)
settings.SETTINGS["bar_magnifier_enabled"] = False
o_bm5 = _bm_run("bm5", [(_mid + 5, _TP_K + 1)], 5300)
settings.SETTINGS["bar_magnifier_enabled"] = True
check("BM5 스위치 OFF - 예전 동작으로 완전 복귀",
      o_bm5["outcome"] == "miss" and o_bm5["ambiguous"] == 1)
check("BM5-obs 집계 - 기능 OFF 도 skipped(판별 실패로 오염시키지 않음)",
      _amb_is(ambiguous_skipped=1))

# BM6: 캔들이 너무 길면(15분봉 폴백 상한 초과) 재검사하지 않는다 — 체결량 폭주 방어
check("BM6 구간 길이 상한 초과 시 재검사 생략",
      price_check.magnify_order("KRW-LINK", now - 3600, now, _TP_K, _SL_K,
                                settings.get) is None)
check("BM6b 길이 제약 판정의 출처가 하나(_magnify_feasible) - 상한 경계 기준",
      price_check._magnify_feasible(now - 900, now, settings.get)
      and not price_check._magnify_feasible(now - 901, now, settings.get))

# BM7: 터치 이전 체결은 순서 판정에서 제외 (캔들 스캔부의 '터치 이후만' 원칙과 동일).
#      magnify_order 에 넘기는 scan_from 이 max(캔들시작, 터치시각) 인지 직접 검증.
_bm7_args = {}
_saved_ftw = upbit.fetch_trades_window
def _bm7_ftw(m, s, e, t, max_pages=4):
    _bm7_args.update(market=m, start=s, end=e)
    return [(s + 1, _TP_K + 1)]
upbit.fetch_trades_window = _bm7_ftw
_bm7_touch = _BM_C[0] + 30   # 터치가 캔들 '중간'에 일어난 경우
_bm7_res = price_check.magnify_order("KRW-LINK", max(_BM_C[0], _bm7_touch), _BM_C[1],
                                     _TP_K, _SL_K, settings.get)
upbit.fetch_trades_window = _saved_ftw
check("BM7 스캔 시작점 = max(캔들시작, 터치시각)",
      _bm7_res == "hit" and abs(_bm7_args["start"] - _bm7_touch) < 1e-6)

# BM8: 구간 길이 상한 초과(15분봉 폴백보다 긴 캔들)도 '체결내역을 못 본' 경우라
#      skipped 로 센다 — 예산은 남아 있고 기능도 켜져 있지만 조회를 안 했다.
o_bm8 = _bm_run("bm8", [(now - 1500, _TP_K + 1)], 5360,
                candles=[(now - 2000, now - 1000, 12.2 * USDT_KRW, 8.9 * USDT_KRW)])
check("BM8 긴 구간은 재검사 없이 보수적 처리",
      o_bm8["outcome"] == "miss" and o_bm8["ambiguous"] == 1)
check("BM8-obs 집계 - 구간 길이 초과도 skipped", _amb_is(ambiguous_skipped=1))

# BM9: 세 카운터의 합 = 이 구간에서 발생한 동시터치 전체(이중계산·누락 방지).
#      unresolved+skipped 는 전부 보수적 miss+ambiguous 로 표식되니 ambiguous=1 행
#      증가분과 정확히 같아야 하고, magnified 건은 표식이 붙지 않아야 한다.
_amb_d = {k: v - _bm_base[k] for k, v in _amb_totals().items()}
check("BM9 집계 정합 - unresolved+skipped == ambiguous 표식 행 증가분",
      _amb_d["ambiguous_unresolved"] + _amb_d["ambiguous_skipped"]
      == _amb_flagged() - _bm_flagged_base)
check("BM9b 분리 후 각 칸의 내역이 손계산과 일치 "
      "(magnified=BM1,BM2 / unresolved=BM3,BM3b / skipped=BM4,BM5,BM8)",
      _amb_d["ambiguous_magnified"] == 2 and _amb_d["ambiguous_unresolved"] == 2
      and _amb_d["ambiguous_skipped"] == 3)

# ── BM-U: upbit.fetch_trades_window 실물 로직 (가짜 requests.get, HTTP 없음) ──
# 2026-07-26 실측 기반: count 상한 500, `to`=HH:MM:SS(UTC)+`daysAgo`(최대 7),
# 페이지 이어붙이기는 `cursor`(직전 페이지 최고(最古) sequential_id).
upbit._TRADE_PACE_SEC = 0.0  # 테스트 속도 - 페이싱은 검증 대상 아님


def _tick(ts, price, sid):
    return {"timestamp": int(ts * 1000), "trade_price": price, "sequential_id": sid}


_bmu_now = time.time()
_bmu_win = (_bmu_now - 300, _bmu_now - 240)   # 5분 전의 1분 구간

# BM-U1: 1페이지가 구간 시작보다 과거까지 닿으면 그대로 커버 완료.
#        구간 밖 체결은 걸러지고, 결과는 시간 오름차순이어야 한다.
_bmu1_calls = []
def _bmu1_get(url, params=None, timeout=None):
    _bmu1_calls.append(params)
    return _FakeResp([
        _tick(_bmu_win[1] + 0.5, 999.0, 105),      # 구간 밖(뒤) - 제외돼야
        _tick(_bmu_win[0] + 40, 300.0, 104),
        _tick(_bmu_win[0] + 10, 100.0, 102),
        _tick(_bmu_win[0] + 10, 200.0, 103),       # 같은 시각 - sequential_id 로 순서
        _tick(_bmu_win[0] - 30, 50.0, 101),        # 구간 밖(앞) = 커버 증거
    ])
_requests_mod.get = _bmu1_get
_bmu1 = _real_fetch_trades_window("KRW-T", _bmu_win[0], _bmu_win[1], 5.0)
_requests_mod.get = _orig_requests_get
check("BM-U1 구간 필터 + 시간 오름차순 + 동시각 일련번호 순",
      _bmu1 is not None and [p for _t, p in _bmu1] == [100.0, 200.0, 300.0])
check("BM-U1b 1페이지로 끝 + count 상한 500", len(_bmu1_calls) == 1
      and _bmu1_calls[0]["count"] == 500 and "to" in _bmu1_calls[0])

# BM-U2: 500건 꽉 찬 페이지가 계속 구간을 못 덮으면 → 페이지 상한까지만 시도 후 None
#        (부분 데이터로 순서를 단정하지 않는다 = 이 함수의 핵심 안전장치)
_bmu2_calls = []
def _bmu2_get(url, params=None, timeout=None):
    _bmu2_calls.append(params)
    base = _bmu_win[1] - 0.001 * len(_bmu2_calls) * 500
    return _FakeResp([_tick(base - i * 0.001, 100.0, 900000 - len(_bmu2_calls) * 500 - i)
                      for i in range(500)])
_requests_mod.get = _bmu2_get
_bmu2 = _real_fetch_trades_window("KRW-T", _bmu_win[0], _bmu_win[1], 5.0, max_pages=3)
_requests_mod.get = _orig_requests_get
check("BM-U2 구간 미커버 시 None(부분데이터로 단정 금지)", _bmu2 is None)
check("BM-U2b 페이지 상한 준수 + 2페이지부터 cursor 사용", len(_bmu2_calls) == 3
      and "cursor" in _bmu2_calls[1] and "to" not in _bmu2_calls[1])

# BM-U3: 저유동성 — 페이지가 500건 미만이면 더 오래된 체결이 없다는 뜻이라 커버 완료
_requests_mod.get = lambda url, params=None, timeout=None: _FakeResp(
    [_tick(_bmu_win[0] + 5, 111.0, 7)])
_bmu3 = _real_fetch_trades_window("KRW-T", _bmu_win[0], _bmu_win[1], 5.0)
_requests_mod.get = _orig_requests_get
check("BM-U3 저유동성(짧은 페이지)은 커버 완료로 인정",
      _bmu3 is not None and len(_bmu3) == 1 and _bmu3[0][1] == 111.0
      and abs(_bmu3[0][0] - (_bmu_win[0] + 5)) < 0.01)  # ms 정밀도 왕복 오차 허용

# BM-U4: 조회 가능 범위(7일) 밖이면 HTTP 를 아예 쏘지 않고 None
_bmu4_calls = []
_requests_mod.get = lambda url, params=None, timeout=None: (
    _bmu4_calls.append(params), _FakeResp([]))[1]
_bmu4 = _real_fetch_trades_window("KRW-T", _bmu_now - 9 * 86400,
                                  _bmu_now - 9 * 86400 + 60, 5.0)
_requests_mod.get = _orig_requests_get
check("BM-U4 7일 초과 과거는 호출 없이 None", _bmu4 is None and not _bmu4_calls)

# BM-U5: 어제 구간은 daysAgo 파라미터가 붙는다(당일 구간엔 안 붙음).
# 두 구간을 '지금으로부터 N초 전'이 아니라 **UTC 날짜에서 직접** 구성한다
# (2026-07-27 실전 발견): daysAgo 는 호출 시점의 `datetime.now(UTC).date()` 로
# 계산되는데, 상대 시각을 쓰면 UTC 자정 부근에서 판정이 뒤집힌다 —
#   · 자정 직전 실행: _bmu_now 캡처 후 자정을 넘겨 '어제'가 이틀 전(daysAgo=2)
#   · 자정 직후 실행: '5분 전'이 어제로 넘어가 당일 구간에 daysAgo 가 붙음
# 둘 다 재현했다. 날짜에서 구성하면 실행 시각과 무관하게 결정적이다.
_bmu_utc_midnight = datetime.fromtimestamp(_bmu_now, timezone.utc).replace(
    hour=0, minute=0, second=0, microsecond=0).timestamp()
_bmu_yesterday = _bmu_utc_midnight - 3600          # 어제 23:00 UTC — 항상 하루 전
_bmu_today = min(_bmu_utc_midnight + 1, _bmu_now - 5)  # 오늘 00:00:01 UTC 이후
_bmu5_calls = []
def _bmu5_get(url, params=None, timeout=None):
    _bmu5_calls.append(params)
    return _FakeResp([])
_requests_mod.get = _bmu5_get
_real_fetch_trades_window("KRW-T", _bmu_yesterday, _bmu_yesterday + 60, 5.0)
_real_fetch_trades_window("KRW-T", _bmu_today, _bmu_today + 1, 5.0)
_requests_mod.get = _orig_requests_get
check("BM-U5 과거일엔 daysAgo, 당일엔 미부착",
      _bmu5_calls[0].get("daysAgo") == 1 and "daysAgo" not in _bmu5_calls[1])

# BM-U6: 네트워크 실패는 예외 전파 없이 None (핫패스가 죽으면 안 됨)
def _bmu6_get(url, params=None, timeout=None):
    raise RuntimeError("boom")
_requests_mod.get = _bmu6_get
_bmu6 = _real_fetch_trades_window("KRW-T", _bmu_win[0], _bmu_win[1], 5.0)
_requests_mod.get = _orig_requests_get
check("BM-U6 조회 실패는 예외 없이 None", _bmu6 is None)

# ── AN1~AN10: 업비트 거래소 리스크 공지 즉시경보 (2026-07-26 카드 #5) ──────────
# 네트워크 없이 검증한다 — announcements._fetch 를 가짜로 갈아끼우거나(엔드투엔드),
# requests.get 을 가짜로 두고 실물 _fetch 를 돌린다(실패 격리).
from monitor import announcements
from datetime import timedelta as _td

_AN_KST = _tz(_td(hours=9))
_AN_KEYWORDS = settings.get("announcement_risk_keywords")
_AN_EXCLUDE = settings.get("announcement_exclude_keywords")
_real_an_fetch = announcements._fetch   # AN9 에서 실물 조회 로직을 검증하려고 보관


def _notice(nid, title, ago_sec=600):
    """업비트 공지 1건 흉내 (실측 응답 필드 그대로)."""
    at = _dt.fromtimestamp(now - ago_sec, tz=_AN_KST).isoformat()
    return {"id": nid, "uuid": str(nid), "category": "거래", "title": title,
            "listed_at": at, "first_listed_at": at}


def _an_level(coin, entry, key, status="watching", touched=False):
    with db.connect(TEST_DB) as conn:
        lv = dict(coin_symbol=coin, ticker=f"KRW-{coin}", direction="long",
                  entry_usd=entry, sl_usd=None, tp_usd=None, rr=None, grade="B",
                  score=60, author=f"AN_{key}", author_followers=100,
                  author_hit_rate=None, author_hit_count=None, author_whitelisted=False,
                  mcap_rank=50, mcap_tier_icon="🥇", post_url=f"https://tv.com/{key}",
                  post_age_minutes=100, collected_at=now - 600)
        lv["signal_key"] = db.make_signal_key(coin, entry, lv["author"], lv["post_url"])
        db.upsert_level(conn, lv)
        row = conn.execute("SELECT id FROM levels WHERE signal_key=?",
                           (lv["signal_key"],)).fetchone()
        if status != "watching":
            conn.execute("UPDATE levels SET status=?, touched_at=? WHERE id=?",
                         (status, (now - 300) if touched else None, row["id"]))
        return row["id"]


def _row(lid):
    with db.connect(TEST_DB) as conn:
        return dict(conn.execute(
            "SELECT status, expired_reason, outcome FROM levels WHERE id=?",
            (lid,)).fetchone())


# AN1: 제목에서 심볼 추출 — 괄호 안 티커만, 날짜/한글 괄호는 무시
check("AN1 심볼 추출(단일)",
      announcements.extract_symbols("질리카(ZIL) 거래 유의 종목 지정 안내") == ["ZIL"])
check("AN1 심볼 추출(날짜 괄호 무시)",
      announcements.extract_symbols(
          "토트넘홋스퍼(SPURS) 거래지원 종료 안내 (8/18 15:00)") == ["SPURS"])
check("AN1 숫자 시작 심볼 허용 / 숫자만은 제외",
      announcements.extract_symbols("원인치(1INCH) 거래 유의 종목 지정 안내 (2026)") == ["1INCH"])

# AN2: 리스크 판정 — '지정 해제'(위험 해소)는 반드시 제외돼야 한다
_is = lambda t: announcements.is_risk_title(t, _AN_KEYWORDS, _AN_EXCLUDE)
check("AN2 유의종목 지정 = 리스크", _is("질리카(ZIL) 거래 유의 종목 지정 안내"))
check("AN2 거래지원 종료 = 리스크", _is("알파쿼크(AQT) 거래지원 종료 안내 (8/3 15:00)"))
check("AN2 '지정 해제'는 리스크 아님(오탐 방지 핵심)",
      not _is("타이코(TAIKO) 거래 유의 종목 지정 해제 안내"))
check("AN2 이벤트 종료 공지는 리스크 아님",
      not _is("솔스티스(SLX) 거래지원 기념, 총 상금 이벤트! (이벤트 종료)"))
check("AN2 일반 상장 공지는 리스크 아님", not _is("모포(MORPHO) KRW 마켓 디지털 자산 추가"))

# AN3: 매칭 = 리스크 제목 × 추적 중 심볼 교집합
_an_notices = [
    _notice(9001, "질리카(ZIL) 거래 유의 종목 지정 안내"),
    _notice(9002, "알파쿼크(AQT) 거래지원 종료 안내 (8/3 15:00)"),
    _notice(9003, "타이코(TAIKO) 거래 유의 종목 지정 해제 안내"),
    _notice(9004, "모포(MORPHO) KRW 마켓 디지털 자산 추가"),
]
_m = announcements.match_notices(_an_notices, ["ZIL", "TAIKO", "MORPHO"],
                                 _AN_KEYWORDS, _AN_EXCLUDE)
check("AN3 추적 심볼만 매칭(해제·상장 공지 제외)",
      len(_m) == 1 and _m[0][1] == ["ZIL"])
check("AN3 미추적 코인은 매칭 안 됨",
      announcements.match_notices(_an_notices, ["BTC"], _AN_KEYWORDS, _AN_EXCLUDE) == [])

# AN4: 오래된 공지는 제외 (기능 첫 가동 시 과거 공지 경보 폭탄 방지)
_old = [_notice(9005, "질리카(ZIL) 거래 유의 종목 지정 안내", ago_sec=10 * 86400)]
check("AN4 max_age 초과 공지 제외",
      announcements.match_notices(_old, ["ZIL"], _AN_KEYWORDS, _AN_EXCLUDE,
                                  max_age_sec=72 * 3600, now=now) == [])
check("AN4 날짜 불명 공지도 제외(스키마 변경 시 폭탄 방지)",
      announcements.match_notices([{"id": 9006, "title": "질리카(ZIL) 거래 유의 종목 지정 안내"}],
                                  ["ZIL"], _AN_KEYWORDS, _AN_EXCLUDE,
                                  max_age_sec=72 * 3600, now=now) == [])

# AN5: 엔드투엔드 — 경보 1회 + 해당 코인 대기 레벨 즉시 만료(사유 기록)
settings.SETTINGS["announcement_alert_enabled"] = True
settings.SETTINGS["announcement_poll_interval_minutes"] = 0  # TTL 무시 - 매 호출 폴링
with db.connect(TEST_DB) as conn:
    conn.execute("DELETE FROM levels")
    conn.execute("DELETE FROM meta WHERE key LIKE 'announcement%'")
_zil_a = _an_level("ZIL", 0.012, "an_a")
_zil_b = _an_level("ZIL", 0.011, "an_b", status="previewed")
_keep = _an_level("LINK", 8.0, "an_keep")   # 무관한 코인은 그대로 살아 있어야 한다
announcements._fetch = lambda timeout, per_page: _an_notices
sent_messages.clear()
with db.connect(TEST_DB) as conn:
    r5 = announcements.check_announcements(conn, now, settings.get)
check("AN5 경보 1회 발송", r5["alerted"] == 1 and len(sent_messages) == 1)
check("AN5 경보 본문(대상·제목·만료건수)",
      "업비트 리스크 공지" in sent_messages[0] and "ZIL" in sent_messages[0]
      and "유의 종목 지정" in sent_messages[0] and "2건" in sent_messages[0])
check("AN5 대기 레벨 즉시 만료 + 사유 기록",
      r5["expired"] == 2
      and _row(_zil_a)["status"] == "expired" and _row(_zil_b)["status"] == "expired"
      and _row(_zil_a)["expired_reason"] == "upbit_notice:9001")
check("AN5 무관한 코인 레벨은 보존", _row(_keep)["status"] == "watching")

# AN6: 같은 공지 재폴링 → 중복 발송 없음 (meta 이력 기반)
sent_messages.clear()
with db.connect(TEST_DB) as conn:
    r6 = announcements.check_announcements(conn, now + 60, settings.get)
check("AN6 공지ID 기반 중복 발송 방지",
      r6["alerted"] == 0 and not sent_messages and r6["expired"] == 0)

# AN7: 미종결 터치 레벨은 만료하지 않는다(판정 표본 유실 방지) — 단 경보 대상엔 포함
sent_messages.clear()
_aqt_w = _an_level("AQT", 0.5, "an_w")                      # 대기 - 만료 대상
_aqt_t = _an_level("AQT", 0.4, "an_t", status="touched", touched=True)  # 판정 중 - 보존
with db.connect(TEST_DB) as conn:
    r7 = announcements.check_announcements(conn, now + 120, settings.get)
check("AN7 거래지원 종료 경보 발송", r7["alerted"] == 1 and "AQT" in sent_messages[0])
check("AN7 대기 레벨만 만료, 판정 중 터치는 보존",
      r7["expired"] == 1 and _row(_aqt_w)["status"] == "expired"
      and _row(_aqt_t)["status"] == "touched" and _row(_aqt_t)["outcome"] is None)

# AN8: 폴링 주기(meta TTL) — 주기 내에는 호출조차 하지 않는다
settings.SETTINGS["announcement_poll_interval_minutes"] = 20
_an_calls = []
announcements._fetch = lambda timeout, per_page: (_an_calls.append(1), _an_notices)[1]
with db.connect(TEST_DB) as conn:
    db.set_meta(conn, "announcement_last_poll_at", str(now))
    r8a = announcements.check_announcements(conn, now + 300, settings.get)      # 5분 뒤
    r8b = announcements.check_announcements(conn, now + 21 * 60, settings.get)  # 21분 뒤
check("AN8 TTL 내 미호출 / TTL 경과 후 호출",
      r8a["polled"] is False and r8b["polled"] is True and len(_an_calls) == 1)

# AN9: 조회 실패·이상 스키마 격리 — 예외 없이 조용히 0건 (실물 _fetch 로직 검증)
announcements._fetch = _real_an_fetch   # 가짜 제거 → 실물 조회 로직으로 복귀
def _an_boom(url, params=None, timeout=None):
    raise RuntimeError("network down")
_requests_mod.get = _an_boom
_an_fail = announcements._fetch(5.0, 30)
_requests_mod.get = _orig_requests_get
check("AN9 조회 실패는 예외 없이 빈 목록", _an_fail == [])
check("AN9 예상 밖 응답 스키마도 예외 없이 0건",
      announcements._extract_notices({"success": False}) == []
      and announcements._extract_notices(None) == []
      and announcements._extract_notices("nope") == [])
sent_messages.clear()
_requests_mod.get = _an_boom
with db.connect(TEST_DB) as conn:
    db.set_meta(conn, "announcement_last_poll_at", "0")
    r9 = announcements.check_announcements(conn, now + 10000, settings.get)
_requests_mod.get = _orig_requests_get
check("AN9 조회 실패 회차는 경보 없이 통과",
      r9["alerted"] == 0 and not sent_messages)

# AN10: run_once 훅 예외 격리 — 공지 감시가 어떻게 터지든 가격체크는 살아야 한다
_an_real_check = announcements.check_announcements
def _an_explode(conn, now_, cfg_get):
    raise RuntimeError("공지 모듈 폭발")
announcements.check_announcements = _an_explode
fake["price"] = fake["low"] = fake["high"] = fake["candles"] = None
try:
    s10 = price_check.run_once(now + 11000)
    _an_survived = True
except Exception:
    _an_survived = False
announcements.check_announcements = _an_real_check
settings.SETTINGS["announcement_alert_enabled"] = False
check("AN10 공지 감시 예외가 회차를 죽이지 않음", _an_survived and isinstance(s10, dict))

# AN11~AN13: 텔레그램 장애 시 경보 유실 방지 (2026-07-26 리뷰 지적)
# 만료가 발송보다 먼저이므로, 발송이 실패하면 그 코인은 다음 폴링에서 활성 목록에
# 없어 재매칭이 불가능하다 → 대기 큐(meta)가 유일한 복구 경로. 여기서 못박는다.
import json as _json  # noqa: E402 - 대기 큐(meta) 원문 검증용
settings.SETTINGS["announcement_alert_enabled"] = True
settings.SETTINGS["announcement_poll_interval_minutes"] = 0
announcements._fetch = lambda timeout, per_page: _an_notices
with db.connect(TEST_DB) as conn:
    conn.execute("DELETE FROM levels")
    conn.execute("DELETE FROM meta WHERE key LIKE 'announcement%'")
_zil_f = _an_level("ZIL", 0.013, "an_fail")   # watching 만 보유 - 가장 흔한 형태
sent_messages.clear()
telegram.send = lambda text: False            # 텔레그램 일시 장애
with db.connect(TEST_DB) as conn:
    r11 = announcements.check_announcements(conn, now, settings.get)
    _an_pending_raw = db.get_meta(conn, "announcement_pending_alerts")
check("AN11 발송 실패 - 레벨은 만료되지만 경보는 대기 큐에 보존",
      r11["alerted"] == 0 and r11["expired"] == 1 and r11["pending"] == 1
      and _row(_zil_f)["status"] == "expired"
      and _an_pending_raw and "ZIL" in _an_pending_raw)

# 텔레그램 복구 → 다음 회차에 재시도 발송. (이 시점 ZIL 은 이미 만료돼 활성 목록에
# 없다 = 대기 큐가 없었다면 경보가 영원히 안 나가는 상황)
telegram.send = lambda text: sent_messages.append(text) or True
with db.connect(TEST_DB) as conn:
    _an_no_active = "ZIL" not in announcements._active_symbols(conn)
    r12 = announcements.check_announcements(conn, now + 120, settings.get)
    _an_pending_after = db.get_meta(conn, "announcement_pending_alerts")
check("AN12 복구 후 재시도 발송(비활성이라 재매칭은 불가한 상태)",
      _an_no_active and r12["alerted"] == 1 and len(sent_messages) == 1
      and "ZIL" in sent_messages[0] and _json.loads(_an_pending_after) == [])

# AN13: 재시도로 나간 경보도 발송 이력에 적립된다 — 같은 코인 레벨이 새로 수집돼
# 다시 활성이 돼도 같은 공지로 두 번 알리지 않는다.
sent_messages.clear()
_zil_new = _an_level("ZIL", 0.014, "an_again")
with db.connect(TEST_DB) as conn:
    r13a = announcements.check_announcements(conn, now + 240, settings.get)
check("AN13 재시도 발송분도 중복 방지 이력에 적립",
      r13a["alerted"] == 0 and not sent_messages and r13a["matched"] == 1)

# AN14: 재시도 기한(TTL)이 지난 대기분은 폐기 — 무한 재시도로 남지 않는다
with db.connect(TEST_DB) as conn:
    db.set_meta(conn, "announcement_pending_alerts", _json.dumps(
        [{"nid": 9099, "title": "낡은(OLD) 거래 유의 종목 지정 안내",
          "symbols": ["OLD"], "expired": 1, "first_at": now}]))
    r14 = announcements.check_announcements(
        conn, now + settings.get("announcement_pending_ttl_hours") * 3600 + 60,
        settings.get)
    _an_pending_ttl = db.get_meta(conn, "announcement_pending_alerts")
check("AN14 기한 초과 대기분 폐기(발송도 재시도도 없음)",
      r14["alerted"] == 0 and not sent_messages
      and _json.loads(_an_pending_ttl) == [])
settings.SETTINGS["announcement_alert_enabled"] = False
settings.SETTINGS["announcement_poll_interval_minutes"] = 20


# ── OB1~OB6: 터치 시점 호가 매수/매도 압력 기록 (2026-07-26 카드 #19) ──────────
# 카드 #18(REST ticker 의 acc_bid_volume/acc_ask_volume)은 2026-07-26 무인증 실측에서
# 해당 필드가 응답에 없음을 확인해 폐기 → 호가 스냅샷 폴백을 채택. 아래 OB1 은 그
# 실증 사실 자체를 회귀 테스트로 못박는다(다시 #18 로 돌아가려는 시도를 막는 기록).
_REST_TICKER_KEYS = {
    "market", "trade_date", "trade_time", "trade_date_kst", "trade_time_kst",
    "trade_timestamp", "opening_price", "high_price", "low_price", "trade_price",
    "prev_closing_price", "change", "change_price", "change_rate",
    "signed_change_price", "signed_change_rate", "trade_volume", "acc_trade_price",
    "acc_trade_price_24h", "acc_trade_volume", "acc_trade_volume_24h",
    "highest_52_week_price", "highest_52_week_date", "lowest_52_week_price",
    "lowest_52_week_date", "timestamp",
}
check("OB1 REST ticker 에는 매수/매도 분리 필드가 없다(카드 #18 폐기 근거)",
      not {"acc_bid_volume", "acc_ask_volume"} & _REST_TICKER_KEYS)

# OB2~OB4: fetch_orderbook_ratio 실물 로직 (가짜 requests.get, HTTP 없음)

def _ob_resp(bid, ask):
    return _FakeResp([{"market": "KRW-T", "total_bid_size": bid,
                       "total_ask_size": ask, "orderbook_units": []}])

_requests_mod.get = lambda url, params=None, timeout=None: _ob_resp(3.0, 1.5)
_ob2 = _real_fetch_orderbook("KRW-T", 5.0)
_requests_mod.get = lambda url, params=None, timeout=None: _ob_resp(0.0, 1.5)
_ob3 = _real_fetch_orderbook("KRW-T", 5.0)
_requests_mod.get = _an_boom
_ob4 = _real_fetch_orderbook("KRW-T", 5.0)
_requests_mod.get = _orig_requests_get
check("OB2 잔량비 = total_bid_size / total_ask_size", _ob2 == 2.0)
check("OB3 한쪽 잔량 0 이면 None(무의미·0나눗셈 방지)", _ob3 is None)
check("OB4 조회 실패는 예외 없이 None", _ob4 is None)

# OB5: 터치 확정 시 레벨 행에 기록된다 (예고 단계에서는 호출조차 없다)
with db.connect(TEST_DB) as conn:
    conn.execute("DELETE FROM levels")
settings.SETTINGS["orderbook_pressure_enabled"] = True
_ob_calls = []
upbit.fetch_orderbook_ratio = lambda m, t: (_ob_calls.append(m), 1.75)[1]
_obk = _an_level("OBK", 10.0, "ob_k")
fake["candles"] = None
fake["high"] = None
fake["price"] = 10.0 * USDT_KRW * 1.005   # 예고 밴드 이내, 아직 미터치
fake["low"] = None
sent_messages.clear()
price_check.run_once(now + 12000)
_ob_preview_calls = len(_ob_calls)
fake["price"] = 10.0 * USDT_KRW * 1.002
fake["low"] = 9.90 * USDT_KRW             # 소급 저가가 엔트리 하향 터치
price_check.run_once(now + 12100)
with db.connect(TEST_DB) as conn:
    _obrow = dict(conn.execute(
        "SELECT status, touch_bid_ask_ratio FROM levels WHERE id=?", (_obk,)).fetchone())
check("OB5 예고 단계에선 호가 호출 없음(비용 0)", _ob_preview_calls == 0)
check("OB5 터치 확정 시 잔량비 기록",
      _obrow["status"] == "touched" and _obrow["touch_bid_ask_ratio"] == 1.75
      and _ob_calls == ["KRW-OBK"])

# OB6: 호가 조회가 터져도 터치 처리는 정상 진행 (기록은 NULL) — 순수 로깅의 격리
with db.connect(TEST_DB) as conn:
    conn.execute("DELETE FROM levels")
def _ob_explode(m, t):
    raise RuntimeError("호가 폭발")
upbit.fetch_orderbook_ratio = _ob_explode
_obx = _an_level("OBX", 10.0, "ob_x")
fake["price"] = 10.0 * USDT_KRW * 1.002
fake["low"] = 9.90 * USDT_KRW
price_check.run_once(now + 12200)
with db.connect(TEST_DB) as conn:
    _obxrow = dict(conn.execute(
        "SELECT status, touched_at, touch_bid_ask_ratio FROM levels WHERE id=?",
        (_obx,)).fetchone())
upbit.fetch_orderbook_ratio = lambda m, t: None
check("OB6 호가 실패해도 터치 처리 정상 + 기록만 NULL",
      _obxrow["status"] == "touched" and _obxrow["touched_at"] is not None
      and _obxrow["touch_bid_ask_ratio"] is None)

# OB7: 관찰 조회 함수 (show_status 표시용) — 기록 전용 경로 확인
with db.connect(TEST_DB) as conn:
    conn.execute("UPDATE levels SET touch_bid_ask_ratio=2.5 WHERE id=?", (_obx,))
    _obrecent = db.get_recent_bid_ask_ratios(conn, limit=5)
check("OB7 최근 잔량비 조회",
      len(_obrecent) == 1 and _obrecent[0]["coin_symbol"] == "OBX"
      and _obrecent[0]["touch_bid_ask_ratio"] == 2.5)

# ── EX1~EX4: 만료 기준은 '수집 시각'이다 (2026-07-27 실측 확정 — 게시 기준 금지) ──
# HANDOFF 가 오래 "게시 7일 만료"로 적어와 실제로 게시 기준으로 바꿔봤다가 되돌렸다.
# 실측: 터치 67건 중 9건(13.4%)이 게시 7일을 넘겨 터치됐는데 그 전부가 수집 당시
# 이미 5.4~6.6일 된 글이었다 — '묵은 셋업'이 아니라 우리가 늦게 주운 글이고,
# 수집 시점부터는 0.5~4.0일 만에 터치했다. 성적도 나쁘지 않다(게시 7일+ 승률 50%
# ·평균 -0.21R vs 0~3일 48%·-0.69R). 게시 기준이면 수집이 늦을수록 감시창이 깎여
# (5~7일에 수집된 17.5% 는 하루도 안 남는다) 수집 지연이 곧 기회 상실이 된다.
# 절대 상한은 수집 게이트(max_post_age_hours=168)가 자동으로 준다 = 게시 후 14일.
# 이 테스트는 '버그처럼 보이는 의도된 동작'을 못 박아 같은 재변경을 막는다.
_EXPIRY = 168 * 3600
with db.connect(TEST_DB) as conn:
    conn.execute("DELETE FROM levels")

    def _add_exp(sym, age_min, collected_ago_h, status="watching"):
        conn.execute(
            "INSERT INTO levels (signal_key, coin_symbol, ticker, direction, entry_usd,"
            " status, collected_at, post_age_minutes) VALUES (?,?,?,'long',1.0,?,?,?)",
            (f"exp-{sym}", sym, f"KRW-{sym}", status,
             now - collected_ago_h * 3600, age_min))
        return conn.execute("SELECT last_insert_rowid() AS id").fetchone()[0]

    _ex_late = _add_exp("EXLATE", 6.6 * 1440, 4 * 24)   # 게시 10.6일·수집 4일 → 유지
    _ex_stale = _add_exp("EXSTALE", 0, 8 * 24)          # 수집 8일 → 만료
    _ex_null = _add_exp("EXNULL", None, 4 * 24)         # 게시 시각 결측 → 동일 판단
    _ex_shadow = _add_exp("EXSHADOW", 0, 8 * 24, status="touched")

    db.expire_old(conn, _EXPIRY, now)
    _ex_st = {r[0]: r[1] for r in conn.execute(
        "SELECT id, status FROM levels WHERE id IN (?,?,?,?)",
        (_ex_late, _ex_stale, _ex_null, _ex_shadow)).fetchall()}

check("EX1 늦게 주운 글(게시 10.6일)도 수집 4일이면 감시 유지 - 게시 기준이면 잘렸을 건",
      _ex_st[_ex_late] == "watching")
check("EX2 수집 후 8일 지나면 만료", _ex_st[_ex_stale] == "expired")
check("EX3 게시 시각 결측이어도 수집 기준으로 동일 판단", _ex_st[_ex_null] == "watching")
check("EX4 섀도 터치도 수집 기준으로 만료", _ex_st[_ex_shadow] == "expired")

# ── 체인(카드3): 이 파일에서 그동안 resolve_outcome 을 거쳐 쌓인 실제 판정
#    (T8/T9/T10/T11/T13/T15/BM1~BM8 등, run_once/_judge_outcomes 정상 경로로
#    종결된 전체 표본)이 통째로 하나의 유효한 해시체인을 이루는지 확인한다 —
#    개별 단위테스트(test_resilience.py)와 달리 실제 운영 경로 산출물 검증.

# ── AD: 주간 감사 덤프가 운영 배선(init_db 훅)으로 실제로 도는가 (카드 #4) ──
# 단위 검증은 scripts/test_weekly_report.py(A 섹션) 담당. 여기서는 "회차가 부르는
# db.init_db 만으로 배선이 완결되는가"만 본다 — 회차 엔트리포인트(run_cycle/
# price_check)는 매 회차 init_db 를 부르고, 덤프는 DB 옆(data/audit)에 떨어져
# price-check.yml 의 `git add data/` 에 그대로 실린다(워크플로 무수정).
import json as _json  # noqa: E402
import shutil as _shutil  # noqa: E402
from pathlib import Path as _Path  # noqa: E402

from storage import audit_dump as _audit  # noqa: E402

_AD_ROOT = _Path("cache/_ad_wiring")
_shutil.rmtree(_AD_ROOT, ignore_errors=True)
_AD_DB = str(_AD_ROOT / "levels.db")
db.init_db(_AD_DB)   # 1회차 — 이력 없음 → 덤프가 돌아야 한다

_AD_DIR = _AD_ROOT / "audit"
_AD_WEEK = db.week_kst()
check("AD1 init_db 훅만으로 DB 옆(<db 폴더>/audit)에 주차 덤프 생성 — 워크플로 무수정 배선",
      _audit.audit_dir_for(_AD_DB) == _AD_DIR.resolve()
      and (_AD_DIR / f"levels_{_AD_WEEK}.ndjson").exists()
      and (_AD_DIR / f"daily_stats_{_AD_WEEK}.ndjson").exists())

# 2회차 이후는 주기 게이트가 막는다 — 2분마다 도는 핫패스에 파일 IO 를 얹으면 안 된다
with db.connect(_AD_DB) as _c:
    _c.execute(
        "INSERT INTO levels (signal_key, coin_symbol, ticker, direction, status,"
        " collected_at, raw_text) VALUES ('ad1','SOL','KRW-SOL','long','watching',?,?)",
        (time.time(), "원문"))
db.init_db(_AD_DB)
_ad_meta = _json.loads((_AD_DIR / f"levels_{_AD_WEEK}.ndjson").read_text(
    encoding="utf-8").splitlines()[0])
check("AD2 같은 주 안의 다음 회차는 게이트로 생략(핫패스 비용 = meta 조회 1건)",
      _ad_meta["_rows"] == 0)

# 감사 훅이 붙어도 가격체크가 쓰는 활성 레벨의 원문은 그대로다(재파싱 자가치유 보호)
with db.connect(_AD_DB) as _c:
    _ad_raw = _c.execute("SELECT raw_text FROM levels").fetchone()["raw_text"]
check("AD3 감사 훅이 활성 레벨 원문을 건드리지 않음(reparse_all 대상 보존)",
      _ad_raw == "원문")
_shutil.rmtree(_AD_ROOT, ignore_errors=True)

print()
print("── 본알림 실제 렌더링 ──")
print(touch_msg)
os.remove(TEST_DB)
sys.exit(0 if ok else 1)