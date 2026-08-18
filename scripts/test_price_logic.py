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
# 2026-07-31 예고 발송 스위치 — 운영 기본값이 False(예고 완전 제거)가 됐지만
# T1~T35 회귀는 예고 발송을 전제로 짜여 있다. 여기서 True 로 되돌려 기존 검증
# (True 면 기존 동작 그대로임의 증명이기도 하다)을 보존하고, False 동작은 전용
# 블록(PV*)에서 검증한다.
settings.SETTINGS["preview_alert_enabled"] = True
# 2026-08-15 v5 사다리 감점 중립화 — 이 파일의 T1~T35 손계산(점수 절대값·억제
# 카운터 연쇄)은 전부 v4 배점 기준으로 짜여 있고, 검증 대상은 파이프라인 역학
# (클러스터·소급·상한·재발송·집계)이지 배점표가 아니다. v5 의 -3 이 켜지면 경계
# (40점) 근처 픽스처가 D 로 밀려 suppressed_* 손계산이 연쇄로 어긋난다(실측 7건:
# T22·T26d/e·T27b/c/e·T28c). 사다리 감점 자체의 검증은 test_grading.py G14~G14f
# 가 전담한다. LADDER_PENALTY 는 함수 본문에서 모듈 전역으로 참조되므로 여기서
# 0 으로 덮으면 이 프로세스 안에서만 무효화된다(운영 코드 불변).
from collector import grading as _grading_neutralize  # noqa: E402
_grading_neutralize.LADDER_PENALTY = 0
if os.path.exists(TEST_DB):
    os.remove(TEST_DB)
# 발송 원장(2026-07-28)은 DB 밖 파일이라 DB 를 지워도 남는다 — 안 지우면 직전
# 실행이 남긴 발송 이력이 이번 실행의 알림을 재발송으로 오인해 막는다(실측: 두 번째
# 실행부터 T2 부터 무너짐). 테스트 시작 시 함께 초기화한다.
from storage import alert_ledger as _alert_ledger   # noqa: E402
if os.path.exists(_alert_ledger.ledger_path(TEST_DB)):
    os.remove(_alert_ledger.ledger_path(TEST_DB))
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
sent_urgency = []   # 무음/유음 분리 검증용 (2026-07-27): 터치=high, 예고=low


def _stub_send(text, urgency="high", reply_to_message_id=None):
    sent_messages.append(text)
    sent_urgency.append(urgency)
    # 반환 타입 (2026-08-17 #6): Optional[int]. 성공 시 message_id (양수 정수).
    return 1


telegram.send = _stub_send

fake = {"price": None, "low": None, "high": None, "candles": None}
upbit.fetch_prices = lambda mkts, t: {m: (USDT_KRW if m == "KRW-USDT" else fake["price"]) for m in mkts}

def _fake_range(m, mins, t):
    if fake["candles"] is not None:
        return fake["candles"]
    if fake["low"] is None and fake["high"] is None:
        return None
    # 기본: 직전 1~2분 사이의 캔들 1개 (end 가 최근이라 터치 이후 판정에 포함됨)
    # 5-튜플(2026-08-15 종가 확장) — 실물 fetch_range_since 와 형태 동기화.
    # 종가=현재가(꼬리 스침 모델). fake["candles"] 로 넘기는 명시 목록은 일부러
    # 4-튜플을 유지한다 — 구 형태 하위호환의 살아있는 회귀 증거.
    return [(now - 120, now - 60,
             fake["high"] or fake["price"], fake["low"] or fake["price"],
             fake["price"])]
upbit.fetch_range_since = _fake_range
_real_fetch_trades_window = upbit.fetch_trades_window  # BM-U1~ 에서 실물 로직 검증용

# 동시터치 재검사(Bar Magnifier)용 체결내역 — 기본값 None = "판별 불가"라
# 기존 T10(보수적 miss+ambiguous) 동작이 그대로 유지된다. BM 테스트가 값을 바꾼다.
fake_trades = {"list": None}
upbit.fetch_trades_window = lambda m, s, e, t, max_pages=4: fake_trades["list"]
_real_fetch_orderbook = upbit.fetch_orderbook_ratio  # OB2~OB4 에서 실물 로직 검증용
upbit.fetch_orderbook_ratio = lambda m, t: None  # 호가 스냅샷 기본 스텁 (OB* 에서 교체)
_real_fetch_week52 = upbit.fetch_week52  # WK1 에서 실물 로직(페이싱) 검증용
upbit.fetch_week52 = lambda m, t: (16000.0, 9000.0)  # 52주 고가/저가 (KRW)
upbit.fetch_volume_ranks = lambda t: {"KRW-LINK": 5}
# 거래량 급증 감시(Feature 4) — 2026-07-31 스텁 추가. 구 fetch_volume_data 시절엔
# 스텁이 빠져 있어 run_once 가 터치 후 매 회차 실제 업비트 API 를 때리는 잠복
# 네트워크 결합이 있었다("네트워크 없이 도는 것이 원칙"과 모순). None = 조회 실패
# 취급이라 감시 로직이 조용히 skip — RVOL 실물 로직은 RV*, 판정은 VS* 블록에서 검증.
_real_fetch_rvol_1h = upbit.fetch_rvol_1h
upbit.fetch_rvol_1h = lambda m, t: None
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
    # trade_price(종가) 포함 — 2026-08-15 5-튜플 확장 후 실물 파서가 읽는 필드.
    # U1~U4 검증 대상(시간창·페이지네이션)과는 무관, 실응답 형태만 맞춘다.
    return {"candle_date_time_utc": _iso(ts), "high_price": high, "low_price": low,
            "trade_price": (high + low) / 2}


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
# 무음/유음 분리(기획 카드 #6, 2026-07-27 승인): 예고는 아직 행동할 시점이 아니라 무음.
check("T2b 예고는 무음(disable_notification)", sent_urgency[-1] == "low")

# T3: 같은 조건 재체크 → 중복 예고 없음
s3 = price_check.run_once(now + 180)
check("T3 중복 예고 억제", s3["previews"] == 0 and len(sent_messages) == 1)

# T4: 저가가 엔트리 하향 터치 → 본알림 1건, 엔트리 존 표기, 출처 하이퍼링크 2개
fake["price"] = 8.30 * USDT_KRW * 1.002
fake["low"] = 8.24 * USDT_KRW
s4 = price_check.run_once(now + 240)
touch_msg = sent_messages[-1]
check("T4 터치 - 본알림 1건", s4["touches"] == 1 and len(sent_messages) == 2)
# 터치 본알림만 소리를 낸다 — "지금 매수를 판단하라"는 유일한 신호이기 때문.
check("T4b 터치 본알림은 유음", sent_urgency[-1] == "high")
check("T4 터치 헤더+진입가 표기", "진입가 터치" in touch_msg and "진입:" in touch_msg)
check("T4 출처 링크형(URL 비노출)", touch_msg.count("출처1") == 1 and touch_msg.count("출처2") == 1
      and 'href="https://tv.com' in touch_msg and "🔗 https://" not in touch_msg)
# 2026-08-08 최종 결정: 화이트리스트 ⭐⭐ 표시 숨김(거추장스럽다 - 다른 지표로 판단)
check("T4 적중률 표시", "적중률: 67%" in touch_msg and "⭐⭐" not in touch_msg)
check("T4 시장심리 행", "비트 점유율: 56.6%" in touch_msg and "알트장: 32 (BTC 매수 고려)" in touch_msg
      and "시장심리: 31 (공포)" in touch_msg)
check("T4 원단위 반올림", ".00원" not in touch_msg and "원)" in touch_msg)
check("T4 표기수정 1차", "[진입가 터치]" in touch_msg and "손절" not in touch_msg
      and "평균 적중률: 67%" in touch_msg and "작성자 평균" not in touch_msg)
check("T4 표기수정 최종(워쳐식 타점+원화단독)", "타점" in touch_msg and "현재:" in touch_msg
      and "진입:" in touch_msg and "목표:" in touch_msg and "$" not in touch_msg
      and "엔트리" not in touch_msg and "~" in touch_msg)
# 2026-08-03 사용자 결정: 📐 SL 행 삭제. SL 은 판정 엔진 내부에서만 사용.
check("T4 거래순위+4칸정렬+SL행 삭제", "    거래:  5위" in touch_msg
      and "\n    현재:" in touch_msg and "\n    고가" in touch_msg
      and "📐 SL" not in touch_msg and "R:R 1:" not in touch_msg)
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
# 2026-08-08 2차: 행별 자동 절삭 예산 안에서만 원문과 비교(절삭 자체는 TR
# 섹션에서 별도 검증 - 여기선 절삭 후에도 올바른 수치가 담기는지만 본다).
check("T14 워쳐+자체 병기 (별도줄)",
      tg._truncate_line("📊 평균 적중률: 72% (워쳐 25건)") in msg_a
      and "\n🏹 승률73% (8승3패)" in msg_a and "✍️ @" in msg_a)
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
check("T14g2 승률만 빠지고 작성자 줄은 정상 렌더", "✍️ @NoStopAuthor" in msg_g)
msg_h = tg.render_alert("touch", "LINK",
                        [dict(_asym, author_self_neff=12.0, author_self_neff_r=5.0)],
                        8.35 * USDT_KRW, USDT_KRW)
check("T14h R 표본이 게이트를 넘기면 정상 표시(과잉 차단 아님)",
      "🏹 승률100% (12승0패)" in msg_h)
msg_i = tg.render_alert("touch", "LINK", [dict(_asym)], 8.35 * USDT_KRW, USDT_KRW)
check("T14i 지표 미주입(구버전 경로)은 보수적 미표시 - 예전 raw 폴백이 그 구멍이었다",
      "승률" not in msg_i)

# T14L~T14N: SL 행 완전 삭제 회귀 방어 (2026-08-03 사용자 결정) — SL 은 판정
# 엔진 내부 기준선으로만 사용, 알림 화면에는 절대 노출하지 않는다.
# 예전엔 "📐 SL -5.2% · R:R 1:5.3" 행이 표시됐으나 사용자가 SL 을 매매에 참고하지
# 않아 화면 공간만 차지했음. rep 에 sl_usd/rr 이 채워져 있어도 렌더에 나오면 안 됨.
_sl_long = dict(coin_symbol="BTC", entry_usd=100.0, sl_usd=95.0, tp_usd=115.0, rr=3.0,
                direction="long", grade="B", score=60, author="LongAuth",
                author_followers=1000, author_hit_rate=None, author_hit_count=None,
                author_whitelisted=False, mcap_rank=1, mcap_tier_icon="🥇",
                post_url="https://tv.com/L", post_age_minutes=10, collected_at=now)
msg_long_sl = tg.render_alert("touch", "BTC", [_sl_long], 100.0 * USDT_KRW, USDT_KRW)
check("T14L 롱 SL 데이터 있어도 📐 SL 행 미표시",
      "📐" not in msg_long_sl and "R:R" not in msg_long_sl)
_sl_short = dict(_sl_long, direction="short", sl_usd=105.0, tp_usd=90.0)
msg_short_sl = tg.render_alert("touch", "BTC", [_sl_short], 100.0 * USDT_KRW, USDT_KRW)
check("T14M 숏 SL 데이터 있어도 📐 SL 행 미표시",
      "📐" not in msg_short_sl and "R:R" not in msg_short_sl)
# T14N: 펀딩 레짐 전환 배지 (스프린트08, 2026-08-14 어휘 개편) — 감지 시
# "🔥 N일만에 매수세 복귀", 미감지 시 무.
_fund_lv = dict(_sl_long, author="FundAuth")
_flip = {"flipped": True, "neg_days": 32.3, "latest": 0.0012}
msg_flip = tg.render_alert("touch", "BTC", [_fund_lv], 100.0 * USDT_KRW, USDT_KRW,
                           funding_rate=0.0012, funding_regime_flip=_flip)
msg_noflip = tg.render_alert("touch", "BTC", [_fund_lv], 100.0 * USDT_KRW, USDT_KRW,
                             funding_rate=0.0012, funding_regime_flip=None)
check("T14N 레짐 전환 감지 시 '🔥 32일만에 매수세 복귀' 표시, 미감지 시 무",
      "🔥 32일만에 매수세 복귀" in msg_flip and "🔥" not in msg_noflip)
# T14N2 (2026-08-03 R1 감사): 다른 시장심리 지표(sentiment/kimchi/funding_rate)가
# 모두 없어도 레짐 배지만 있으면 세퍼레이터가 붙어야 한다 — 예전엔 배지가
# 세퍼레이터 없이 목표가 행 바로 아래에 뜨는 렌더 이슈가 있었다.
_msg_only_flip = tg.render_alert("touch", "BTC", [_fund_lv], 100.0 * USDT_KRW, USDT_KRW,
                                 sentiment=None, kimchi_pct=None,
                                 funding_rate=None, funding_regime_flip=_flip)
check("T14N2 레짐 배지 단독 - 세퍼레이터 이후 렌더",
      "🔥 32일만에 매수세 복귀" in _msg_only_flip
      and _msg_only_flip.find(tg._SEP + "\n🔥") >= 0)
# T14O (2026-08-07 개편): 종전 "💰 펀딩 수치+라벨" 줄 → "🧭 수급" 판정 한 줄.
# supply 미전달 구 호출부는 funding_rate 단독 폴백으로 같은 줄이 나와야 한다.
msg_hot = tg.render_alert("touch", "BTC", [_fund_lv], 100.0 * USDT_KRW, USDT_KRW,
                          funding_rate=0.05)
msg_cold = tg.render_alert("touch", "BTC", [_fund_lv], 100.0 * USDT_KRW, USDT_KRW,
                           funding_rate=-0.05)
msg_neutral = tg.render_alert("touch", "BTC", [_fund_lv], 100.0 * USDT_KRW, USDT_KRW,
                              funding_rate=0.001)
check("T14O 수급 폴백: 롱과열→주의(추격 위험) / 숏과열→우호(반등 여지) / 그 외→중립",
      "🧭 돈 흐름: 주의 (추격 위험)" in msg_hot
      and "🧭 돈 흐름: 우호 (반등 여지)" in msg_cold
      and "🧭 돈 흐름: 중립" in msg_neutral and "💰 펀딩" not in msg_neutral)
# T14O2: supply 명시 전달 시 그대로 렌더 + 원시 펀딩 수치는 미노출.
msg_sup = tg.render_alert("touch", "BTC", [_fund_lv], 100.0 * USDT_KRW, USDT_KRW,
                          funding_rate=-0.05, supply=("우호", "반등 연료"))
check("T14O2 수급 명시 전달 - 합성 판정 렌더·펀딩 수치 미노출",
      "🧭 돈 흐름: 우호 (반등 연료)" in msg_sup and "-0.05" not in msg_sup)

# ── SV1~SV5: derive_supply_verdict 판정 매트릭스 (2026-08-07) ──────────────
from monitor.binance import derive_supply_verdict as _sv
check("SV1 숏 과열+신규 숏 유입 → 우호(반등 연료) / 중립 펀딩이면 중립(하락 베팅)",
      _sv(-0.02, +8.0, -3.0) == ("우호", "반등 연료")
      and _sv(0.0, +8.0, -3.0) == ("중립", "하락 베팅"))
check("SV2 신규 매수 유입 → 롱 과열이면 주의, 아니면 우호(자금 유입)",
      _sv(+0.02, +8.0, +3.0) == ("주의", "추격 위험")
      and _sv(0.0, +8.0, +3.0) == ("우호", "자금 유입"))
check("SV3 OI 감소+가격 상승 → 주의(속임 반등) / +하락 → 중립(투매 진행)",
      _sv(0.0, -8.0, +3.0) == ("주의", "속임 반등")
      and _sv(-0.02, -8.0, -3.0) == ("중립", "투매 진행"))
check("SV4 OI 보합(<3%)/미확보 → 펀딩 단독 폴백",
      _sv(-0.02, +1.0, -3.0) == ("우호", "반등 여지")
      and _sv(+0.02, None, None) == ("주의", "추격 위험")
      and _sv(0.001, None, None) == ("중립", None))
check("SV5 전부 없음 → (None, None) - 행 생략",
      _sv(None, None, None) == (None, None))

# ── RS-RSI/PV: Wilder RSI 계산 + 자리 판정 (2026-08-07) ────────────────────
from monitor.upbit import _wilder_rsi as _rsi, derive_position_verdict as _pv
# 검증 벡터: Wilder 원저 방식 손계산 대조 (period=14, 상승분·하락분 평활).
# 전형 시퀀스 — 연속 상승만 있으면 100, 연속 하락만 있으면 0 근접.
check("RSI1 단조 상승 → 100 / 단조 하락 → 0",
      _rsi([float(i) for i in range(1, 31)]) == 100.0
      and _rsi([float(i) for i in range(30, 0, -1)]) < 1e-9)
# 표본 부족(period+1 미만) → None
check("RSI2 표본 부족 → None", _rsi([1.0] * 14) is None and _rsi([]) is None)
# 상승·하락 균등 교대(±1 반복) → RS≈1 → RSI≈50 (Wilder 평활 수렴 성질)
_alt = [100.0 + (i % 2) for i in range(100)]
_rsi_alt = _rsi(_alt)
check("RSI3 등폭 교대 시퀀스 → 50 부근 수렴", 45.0 < _rsi_alt < 55.0)
# 변동 없음(전부 동일 종가) → avg_loss 0 → 100 (0 나눗셈 없이)
check("RSI4 무변동 → 100 (division-safe)", _rsi([5.0] * 30) == 100.0)

# RPV — 5단계 개편 (2026-08-08 MA 확장 + 기준값 검토):
# 최적/우호/중립/주의/위험. RSI 단독 눌림목은 중립(Cardwell 정설 — 확인 부재),
# 바닥권(<=30) 단독은 우호 유지. MA 미전달은 RSI 단독 폴백.
check("RPV1 RSI 단독(MA 폴백): 바닥권 우호 / 조정중 중립 강등 / 과열 주의",
      _pv(27.0, 50.0) == ("우호", "바닥권·RSI27")
      and _pv(38.0, 50.0) == ("중립", "조정중·RSI38")
      and _pv(48.0, 50.0) == ("중립", "조정중·RSI48")  # 상한 45→50 확대
      and _pv(52.0, 50.0) == ("중립", "RSI52")
      and _pv(64.0, 50.0) == ("중립", "상승중·RSI64")
      and _pv(74.0, 50.0) == ("주의", "과열·RSI74"))
check("RPV2 주봉 극단: 주>=70 위험(일봉 무관) / 주<=30+일<=50 장기바닥 우호",
      _pv(38.0, 72.0) == ("위험", "장기과열·주RSI72")
      and _pv(38.0, 28.0) == ("우호", "장기바닥·주RSI28")
      and _pv(60.0, 28.0) == ("중립", "상승중·RSI60"))
check("RPV3 결측: 일봉 없으면 주봉 극단만 판정, 전부 없으면 (None,None)",
      _pv(None, 72.0) == ("위험", "장기과열·주RSI72")
      and _pv(None, 50.0) == (None, None)
      and _pv(None, None) == (None, None))
# MA 케이스 — price/ma20/ma60/ma120 전달 시
_MAS_UP = dict(ma20=110.0, ma60=100.0, ma120=90.0)     # 정배열
_MAS_DOWN = dict(ma20=90.0, ma60=100.0, ma120=110.0)   # 역배열
_MAS_MIX = dict(ma20=100.0, ma60=110.0, ma120=90.0)    # 혼조
# 2026-08-14 어휘 개편 + 2토큰 상한: 정배열→상승세, 역배열→하락세,
# 눌림목→조정중, 일NN→RSINN, 주NN→주RSINN. 3박자 최적은 지지·추세만
# 표기(RSI 생략), 4h 태그는 마지막 토큰을 밀어내고 붙는다 — 종전 3~4토큰이
# 32칼럼을 넘어 프로덕션에서 잘리던 버그의 근본 수정.
check("RPV5 최적 = 3박자(지지+상승세+조정권): 60일선 +2% 이내 터치",
      _pv(38.0, 50.0, price=101.0, **_MAS_UP) == ("최적", "60일지지·상승세"))
check("RPV5b 지지 밴드: 상단 +3% 이내 인정 / +3% 초과·하단 -1% 초과는 미인정",
      _pv(38.0, 50.0, price=102.9, **_MAS_UP)[0] == "최적"
      and _pv(38.0, 50.0, price=103.2, **_MAS_UP) == ("우호", "상승세·RSI38")
      and _pv(38.0, 50.0, price=98.9, **_MAS_UP) == ("우호", "상승세·RSI38"))
check("RPV6 우호 = 2박자: 지지+조정권(혼조) / 상승세+조정권(지지 없음)",
      _pv(38.0, 50.0, price=110.5, **_MAS_MIX) == ("우호", "60일지지·RSI38")
      and _pv(44.0, 50.0, price=150.0, **_MAS_UP) == ("우호", "상승세·RSI44"))
check("RPV7 하락세 강등: 조정권 주의(함정) / 과열 위험 / 그 외 중립",
      _pv(38.0, 50.0, price=150.0, **_MAS_DOWN) == ("주의", "하락세·RSI38")
      and _pv(74.0, 50.0, price=150.0, **_MAS_DOWN) == ("위험", "하락세·RSI74")
      and _pv(64.0, 50.0, price=150.0, **_MAS_DOWN) == ("중립", "하락세·RSI64"))
check("RPV8 중립대(50~60) 지지 근접은 정보만: 중립 (60일지지·RSI52)",
      _pv(52.0, 50.0, price=110.5, **_MAS_MIX) == ("중립", "60일지지·RSI52")
      and _pv(64.0, 50.0, price=150.0, **_MAS_UP) == ("중립", "상승세·RSI64"))
check("RPV9 가장 가까운 지지선 선택: 20일선이 60일선보다 가까우면 20일지지",
      _pv(44.0, 50.0, price=110.5, **_MAS_UP) == ("최적", "20일지지·상승세"))

# ── RPV10~13: 4h RSI 극단 경고 오버레이 (2026-08-08 사용자 결정 - 극단값만 개입) ──
check("RPV10 4h 과열(>=70) - 최적/우호 각각 한 단계 강등 + 태그 병기",
      _pv(38.0, 50.0, price=101.0, rsi_4h=74.0, **_MAS_UP)
      == ("우호", "60일지지·4h과열")
      and _pv(38.0, 50.0, price=110.5, rsi_4h=74.0, **_MAS_MIX)
      == ("중립", "60일지지·4h과열"))
check("RPV11 4h 과열이어도 이미 중립 이하면 등급 불변, 태그만 병기",
      _pv(52.0, 50.0, price=150.0, rsi_4h=74.0, **_MAS_MIX) == ("중립", "RSI52·4h과열")
      and _pv(38.0, 50.0, price=150.0, rsi_4h=74.0, **_MAS_DOWN)
      == ("주의", "하락세·4h과열"))
check("RPV12 4h 급락(<=30) - 등급은 그대로, 정보 태그만 추가",
      _pv(38.0, 50.0, price=101.0, rsi_4h=25.0, **_MAS_UP)
      == ("최적", "60일지지·4h급락"))
check("RPV13 4h 평범(30<x<70) 또는 미전달 - 태그 없음(기존 동작 그대로)",
      _pv(38.0, 50.0, price=101.0, rsi_4h=50.0, **_MAS_UP)
      == ("최적", "60일지지·상승세")
      and _pv(38.0, 50.0, price=101.0, **_MAS_UP)
      == ("최적", "60일지지·상승세"))
check("RPV14 base 판정이 (None,None)이면 4h 극단이어도 등급 생성 안 함",
      _pv(None, None, rsi_4h=74.0) == (None, None))

# 렌더: 52주 블록 아래 자리 줄
_msg_pos = tg.render_alert("touch", "BTC", [_fund_lv], 100.0 * USDT_KRW, USDT_KRW,
                           week52=(200.0 * USDT_KRW, 50.0 * USDT_KRW),
                           position=("우호", "조정중·RSI38"))
check("RPV4 렌더: 🌡️ 자리 줄이 52주 블록 뒤에 표시",
      "🌡️ 자리: 우호 (조정중·RSI38)" in _msg_pos
      and _msg_pos.find("현재") < _msg_pos.find("🌡️"))

# ── VR1~VR3: 판정 로깅 + 자가검증 집계 (2026-08-07) ─────────────────────────
_VR_DB = "cache/_test_verdict.db"
if os.path.exists(_VR_DB):
    os.remove(_VR_DB)
db.init_db(_VR_DB)
with db.connect(_VR_DB) as conn:
    _vr_ids = []
    for i, (ret24, ret72) in enumerate([(3.0, 5.0), (1.0, 2.0), (-2.0, -1.0)]):
        conn.execute(
            "INSERT INTO levels (signal_key, coin_symbol, ticker, direction, "
            "entry_usd, status, collected_at, ret_24h, ret_72h) "
            "VALUES (?,?,?,'long',1.0,'touched',?,?,?)",
            (f"vr-{i}", "VRC", "KRW-VRC", now - 1000, ret24, ret72))
        _vr_ids.append(conn.execute("SELECT last_insert_rowid() AS i").fetchone()[0])
    # 앞 2건 우호, 뒤 1건 주의로 기록
    db.record_touch_verdicts(conn, _vr_ids[:2], ("우호", "반등 연료"), ("우호", "조정중·RSI38"))
    db.record_touch_verdicts(conn, _vr_ids[2:], ("주의", "추격 위험"), None)
    # 재기록 시도(재발송 재현) — 최초 기록이 보존돼야 한다
    db.record_touch_verdicts(conn, _vr_ids[:1], ("중립", ""), ("중립", ""))
    _vr_first = conn.execute(
        "SELECT touch_supply_verdict AS s, touch_position_verdict AS p "
        "FROM levels WHERE id=?", (_vr_ids[0],)).fetchone()
    _vs = db.get_verdict_stats(conn)
check("VR1 판정 기록 - 최초 기록 우선(재발송이 덮어쓰지 않음)",
      _vr_first["s"] == "우호|반등 연료" and _vr_first["p"] == "우호|조정중·RSI38")
check("VR2 집계 - 라벨별 n·평균 24h/72h (position 미기록 건은 해당 축 제외)",
      _vs["supply"]["우호"]["n"] == 2 and abs(_vs["supply"]["우호"]["avg24"] - 2.0) < 1e-9
      and abs(_vs["supply"]["우호"]["avg72"] - 3.5) < 1e-9
      and _vs["supply"]["주의"]["n"] == 1
      and "주의" not in _vs["position"] and _vs["position"]["우호"]["n"] == 2)
# VR3 (2026-08-07 사용자 확정): 판정 데이터는 **내부 축적 전용** — 알림·주간
# 리포트 어디에도 노출하지 않는다(기획/후속 개발 참고용 원천 데이터).
# 렌더러에 노출 경로가 다시 생기면 이 체크가 깨진다.
check("VR3 내부 전용 - 렌더러에 verdict 노출 경로 없음",
      not hasattr(tg, "_verdict_section")
      and "verdict_stats" not in tg.render_weekly_report.__code__.co_varnames)
if os.path.exists(_VR_DB):
    os.remove(_VR_DB)

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

# T14j~T14n: 소스 표기 + 다단계 목표 (2026-07-27 사용자 승인 A안).
# 워쳐는 TradingView 작성자만 추적하므로 텔레그램 채널은 **영원히** "워쳐 미추적"
# 이다 — 늘 참인 문구는 정보가 0이라 그 자리에 출처를 넣는다. 목표는 소스가 8단계
# 사다리를 줘도 우리는 TP1 만 쓰므로(판정·배점 축) "1/8단계"로 위에 더 있음만
# 알린다. 판정 로직은 건드리지 않는다.
_src = dict(coin_symbol="LINK", entry_usd=8.3, sl_usd=7.8, tp_usd=9.5, rr=2.4,
            grade="C", score=45, author="SomeChannel", author_followers=None,
            author_hit_rate=None, author_hit_count=None, author_whitelisted=False,
            mcap_rank=19, mcap_tier_icon="🥇", post_url="https://t.me/x/1",
            post_age_minutes=60, collected_at=now)
msg_j = tg.render_alert("touch", "LINK", [dict(_src, source="telegram")],
                        8.35 * USDT_KRW, USDT_KRW)
check("T14j 텔레그램 소스는 '워쳐 미추적' 대신 출처를 표기",
      "📡 텔레그램 채널 · 적중률 미집계" in msg_j and "워쳐 미추적" not in msg_j)
msg_k = tg.render_alert("touch", "LINK", [dict(_src, source="tradingview")],
                        8.35 * USDT_KRW, USDT_KRW)
# 2026-07-28 사장님 지시로 뒤집힌 결정: TradingView 인데 워쳐가 안 따라가는 작성자도
# 출처를 적는다(실측 ENA/@Elephantun). "워쳐 미추적"은 봇 내부 사정일 뿐이고, 읽는
# 사람에게 쓸모 있는 정보는 '어디서 온 신호인가'다.
check("T14k TradingView 미추적 작성자도 '워쳐 미추적' 대신 출처를 표기",
      "📡 트레이딩뷰 · 적중률 미집계" in msg_k and "워쳐 미추적" not in msg_k)
msg_k2 = tg.render_alert("touch", "LINK", [dict(_src, source=None)],
                         8.35 * USDT_KRW, USDT_KRW)
check("T14k2 source 가 빈 초기 수집분만 종전 문구로 남는다",
      # 2026-08-08: 좁아진 그룹채팅 폭에 맞춰 행별 자동 절삭이 적용되어
      # "...작성자)" 꼬리가 잘려나간다 — 잘리기 전 접두부만 확인.
      "👥 적중률 기록없음 (워쳐 미추적" in msg_k2)
msg_l = tg.render_alert("touch", "LINK",
                        [dict(_src, source="telegram", tp_ladder_count=8)],
                        8.35 * USDT_KRW, USDT_KRW)
check("T14l 다단계 목표는 '1/8' 병기 (2026-08-08: '단계' 글씨 제거)",
      "1/8" in msg_l)
check("T14m 단계 표기로 줄 수가 늘지 않는다",
      msg_l.count("\n") == msg_j.count("\n"))
msg_n = tg.render_alert("touch", "LINK",
                        [dict(_src, source="telegram", tp_ladder_count=1)],
                        8.35 * USDT_KRW, USDT_KRW)
check("T14n 단계가 1개(또는 미상)면 꼬리표 없음 - 기존 알림과 동일",
      "단계" not in msg_n)

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
# v4 손계산: 팔로워100(+3) + 근접(+20) + TP+30%(+4) + 완결성 entry/TP/SL(+23) = 50 → C
check("T22 regrade_current 함수 자체 계산 정합", g22 == "C" and abs(s22score - 50) < 1e-9)

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
# previews_total: T2(LINK 예고) + T22(EGLD 재채점 예고) = 2
# (T3 은 dup_preview → preview_dwell +1, MAJOR-1 수정으로 previews_total 미포함)
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
_t27_saved_min_grade = settings.SETTINGS["alert_min_grade"]
settings.SETTINGS["alert_min_grade"] = "C"
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
telegram.send = lambda text, urgency="high", reply_to_message_id=None: None
sent_before28 = len(sent_messages)
price_check.run_once(now + 1320)
telegram.send = _prev_send
row28 = _obs_row()
check("T28 발송실패 - 메시지 미기록", len(sent_messages) == sent_before28)
check("T28b 관찰집계 - 터치 raw +1(7→8), 발송실패 억제 +1", row28["touches_total"] == 8
      and row28["suppressed_send_fail"] == 1)
check("T28c 발송실패는 등급미달과 별도 집계(등급미달/TP감점 카운트 불변)",
      row28["suppressed_grade"] == 2 and row28["suppressed_grade_tp_penalty_only"] == 1)

settings.SETTINGS["alert_min_grade"] = _t27_saved_min_grade

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
    db.bump_daily_stats(conn, "2026-07-20", touches_total=3, suppressed_grade=1,
                        suppressed_tp_gate=4)
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
# T29e2 (2026-08-01 검토 발견): suppressed_tp_gate 는 컬럼·bump 는 있었지만 이
# 조회함수 딕셔너리에 빠져 있어 show_status/리포트 어디서도 볼 수 없었다 — 수정 고정.
check("T29e2 suppressed_tp_gate 조회함수 노출", rep20["suppressed_tp_gate"] == 4)

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

# ── T32: TP 거리 감점 폭 — grading.tp_distance_points 직접 교차검증 ──
# _tp_distance_penalty 는 grading.tp_distance_points(has_rr=True) 에 직접 위임한다.
# 2026-07-29 R:R 제거로 calculate_grade 가 has_rr=False 로 전환되어,
# 종전의 'SL 고정 score 차이' 방식(기준선 8%도 TP 보너스 포함으로 변함)은 폐기.
# tp_distance_points(has_rr=True) 를 직접 비교해 드리프트를 잡는다.
from collector.grading import tp_distance_points as _tdp32  # noqa: E402

for _tp_pct32 in (0.5, 1.99, 2.0, 2.5, 2.99, 3.0, 4.0, 4.99, 5.0, 8.0):
    _target32 = 100.0 * (1 + _tp_pct32 / 100.0)
    _want32 = -_tdp32("long", 100.0, _target32, has_rr=True)  # 감점 폭(양수)
    _got32 = price_check._tp_distance_penalty("long", 100.0, _target32)
    check(f"T32 TP거리감점 로컬재현 일치 (tp_pct={_tp_pct32}%)", abs(_got32 - _want32) < 1e-9)

check("T32b entry/target 없으면 0 (되돌림 판정 스킵 조건)",
      price_check._tp_distance_penalty("long", None, 105.0) == 0
      and price_check._tp_distance_penalty("long", 100.0, None) == 0)
check("T32c 숏 방향도 동일 규칙", abs(price_check._tp_distance_penalty("short", 100.0, 98.5)
      - (-_tdp32("short", 100.0, 98.5, has_rr=True))) < 1e-9)

# ── T33~T34: B안 TP 스윙 미달 억제 (2026-07-29 판정, 07-30 5%→2%,
#    2026-08-03 설정키 alert_min_last_tp_pct 분리 후 5% 복원 — 사용자 결정) ─────
# 마지막(가장 먼) TP 가 진입가 대비 5% 미만이면 레버리지(선물)용 설계로 보아
# 알림을 억제한다. 마지막이 5%+ 면 TP1 이 가깝더라도 허용(스윙 사다리).
# tps_usd 가 NULL 인 구버전 레벨은 tp_usd 단일 값으로 폴백한다.
#
# 점수 계산: followers=5000(+5) / sl=95(rr=0.3<1, +0) / proximity≈0%(<2%, +20) /
# TP 1.5%(0~2% 감점 -6) / 완결성 entry+target+stop(+20+10=+30) = 49점 → C ≥ 'C' 기준
import json as _json33

# T33: 전체 TP 목표가가 2% 이내 → 억제
with db.connect(TEST_DB) as conn:
    lv33 = dict(coin_symbol="ZTPNR", ticker="KRW-ZTPNR", direction="long",
                entry_usd=100.0, sl_usd=95.0, tp_usd=101.5, rr=0.3, grade="B", score=62,
                author="Auth33", author_followers=5000, author_hit_rate=None,
                author_hit_count=None, author_whitelisted=False, mcap_rank=19,
                mcap_tier_icon="🥇", post_url="https://tv.com/u33", post_age_minutes=10,
                collected_at=now - 600,
                tps_usd=_json33.dumps([100.5, 101.0, 101.5]))  # all < 2% (last=1.5%)
    lv33["signal_key"] = db.make_signal_key("ZTPNR", 100.0, "Auth33", "u33")
    db.upsert_level(conn, lv33)
fake["low"] = fake["high"] = fake["candles"] = None
fake["price"] = 100.0 * USDT_KRW * 0.999   # 엔트리 터치 + 등급 C 통과권
# T33-pre: B안 필터가 실행되려면 grade 필터를 먼저 통과해야 한다.
# grading 로직이 변경돼 이 픽스처가 D 등급을 받으면 T33 는 grade 차단으로도
# 무알림이 돼 T33b(suppressed_tp_too_close +1)가 실패 원인 불명으로 보인다.
from collector.grading import meets_min_grade as _mgcheck33
check("T33-pre grade B ≥ min_grade 통과(B안 필터 도달 전제)",
      _mgcheck33(lv33.get("grade"), settings.get("alert_min_grade")))
_tp_before33 = (_obs_row() or {}).get("suppressed_tp_too_close", 0)
sent_before33 = len(sent_messages)
price_check.run_once(now + 1400)
row33 = _obs_row()
check("T33 TP 스윙 미달(all TPs ≤1.5%) - 무알림",
      len(sent_messages) == sent_before33)
check("T33b 관찰집계 - suppressed_tp_too_close +1",
      row33 is not None and row33["suppressed_tp_too_close"] == _tp_before33 + 1)

# T34: 다중 TP 에서 마지막 TP 가 5%+ → 통과 (TP1 이 가깝더라도 허용)
with db.connect(TEST_DB) as conn:
    lv34 = dict(coin_symbol="ZTPLAD", ticker="KRW-ZTPLAD", direction="long",
                entry_usd=100.0, sl_usd=95.0, tp_usd=106.0, rr=1.2, grade="B", score=62,
                author="Auth34", author_followers=5000, author_hit_rate=None,
                author_hit_count=None, author_whitelisted=False, mcap_rank=19,
                mcap_tier_icon="🥇", post_url="https://tv.com/u34", post_age_minutes=10,
                collected_at=now - 600,
                tps_usd=_json33.dumps([101.5, 106.0, 113.0]))  # last=13% ≥ 5%
    lv34["signal_key"] = db.make_signal_key("ZTPLAD", 100.0, "Auth34", "u34")
    db.upsert_level(conn, lv34)
fake["price"] = 100.0 * USDT_KRW * 0.999
sent_before34 = len(sent_messages)
price_check.run_once(now + 1460)
check("T34 다중 TP 마지막 13% - 알림 발송됨",
      len(sent_messages) == sent_before34 + 1)
check("T34b suppressed_tp_too_close 불변(T33b 이후 그대로)",
      (_obs_row() or {})["suppressed_tp_too_close"] == _tp_before33 + 1)

# T34c: _judge_outcomes tps_usd 방어 (2026-08-03 감사) — 리스트에 문자열이 섞여도
# TypeError 로 판정 사이클 전체가 멎지 않는다. 저장된 tps_usd 를 오염시켜 놓고
# 그 레벨을 활성화 → run_once 가 예외 없이 완주하는지만 본다.
_lv34c_key = db.make_signal_key("ZTPBAD", 100.0, "Auth34c", "u34c")
with db.connect(TEST_DB) as conn:
    _lv34c = dict(coin_symbol="ZTPBAD", ticker="KRW-ZTPBAD", direction="long",
                  entry_usd=100.0, sl_usd=95.0, tp_usd=110.0, rr=2.0,
                  grade="B", score=62, author="Auth34c", author_followers=5000,
                  author_hit_rate=None, author_hit_count=None,
                  author_whitelisted=False, mcap_rank=19, mcap_tier_icon="🥇",
                  post_url="https://tv.com/u34c", post_age_minutes=10,
                  collected_at=now - 1200, signal_key=_lv34c_key,
                  tps_usd='[105.0, "bad", 115.0]')  # 파서 밖에서 손상된 사례 시뮬
    db.upsert_level(conn, _lv34c)
    # 강제 touched 로 만들어 _judge_outcomes 진입 대상으로
    conn.execute("UPDATE levels SET status='touched', touched_at=?, "
                 "touch_price_krw=? WHERE signal_key=?",
                 (now - 1000, 100.0 * USDT_KRW, _lv34c_key))
    conn.commit()
fake["price"] = 100.0 * USDT_KRW
_no_crash = True
try:
    price_check.run_once(now + 1500)
except Exception:  # noqa: BLE001
    _no_crash = False
check("T34c 오염된 tps_usd 원소 - _judge_outcomes 크래시 없이 완주",
      _no_crash)

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
    # m-6 (2026-08-01 재검토): 리스크 공지 코인은 거래량 급증 감시도 즉시 종료
    # — 안 끄면 최대 72h 상폐성 펌핑에 🔥 알림이 나간다. 무관 코인 감시는 보존.
    db.add_volume_watch(conn, "KRW-ZIL", "ZIL", now,
                        band_low_krw=1.0, band_high_krw=2.0)
    db.add_volume_watch(conn, "KRW-LINK", "LINK", now)
    conn.commit()
    r5 = announcements.check_announcements(conn, now, settings.get)
    _vw_zil = conn.execute(
        "SELECT 1 FROM volume_watch WHERE ticker='KRW-ZIL'").fetchone()
    _vw_link = conn.execute(
        "SELECT 1 FROM volume_watch WHERE ticker='KRW-LINK'").fetchone()
    db.remove_volume_watch(conn, "KRW-LINK")   # 이후 케이스 오염 방지
    conn.commit()
check("AN5 경보 1회 발송", r5["alerted"] == 1 and len(sent_messages) == 1)
check("AN5b 리스크 공지 코인 - 거래량 감시 즉시 제거(무관 코인은 보존)",
      _vw_zil is None and _vw_link is not None)
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
telegram.send = lambda text, urgency="high", reply_to_message_id=None: None  # 텔레그램 일시 장애
with db.connect(TEST_DB) as conn:
    r11 = announcements.check_announcements(conn, now, settings.get)
    _an_pending_raw = db.get_meta(conn, "announcement_pending_alerts")
check("AN11 발송 실패 - 레벨은 만료되지만 경보는 대기 큐에 보존",
      r11["alerted"] == 0 and r11["expired"] == 1 and r11["pending"] == 1
      and _row(_zil_f)["status"] == "expired"
      and _an_pending_raw and "ZIL" in _an_pending_raw)

# 텔레그램 복구 → 다음 회차에 재시도 발송. (이 시점 ZIL 은 이미 만료돼 활성 목록에
# 없다 = 대기 큐가 없었다면 경보가 영원히 안 나가는 상황)
telegram.send = _stub_send
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

# OB4b (2026-08-08 재검토): 예외 경로도 페이싱을 지키는지 — fetch_rvol_1h 관례
# (두 except 경로 모두 슬립)와 통일. time.sleep 을 스텁으로 바꿔 호출 여부만 확인.
_slept_ob = []
_orig_sleep_ob = time.sleep
upbit.time.sleep = lambda s: _slept_ob.append(s)
_requests_mod.get = _an_boom
_real_fetch_orderbook("KRW-T", 5.0)
upbit.time.sleep = _orig_sleep_ob
_requests_mod.get = _orig_requests_get
check("OB4b 조회 실패(예외) 경로도 페이싱 슬립을 지킨다", len(_slept_ob) == 1)

# WK/FC (2026-08-08 재검토): fetch_week52·_fetch_closes 도 같은 비대칭 수리 —
# 예외 경로에서 페이싱을 건너뛰던 것을 fetch_rvol_1h 관례로 통일.
_slept_wk = []
upbit.time.sleep = lambda s: _slept_wk.append(s)
_requests_mod.get = _an_boom
_real_fetch_week52("KRW-T", 5.0)
upbit._fetch_closes("KRW-T", "days", 200, 5.0)
upbit.time.sleep = _orig_sleep_ob
_requests_mod.get = _orig_requests_get
check("WK1/FC1 fetch_week52·_fetch_closes 예외 경로도 페이싱 슬립을 지킨다(2회)",
      len(_slept_wk) == 2)

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
# EX4b (2026-08-07 재검토 I2 회귀): 섀도 터치 만료는 expired_reason='shadow_touch'
# 마커가 남아야 한다 — get_author_self_stats 의 untouched_expired 집계에서 이 행을
# 제외하는 근거 컬럼. 일반 만료(EXSTALE)는 마커 없이(NULL) 남는다.
with db.connect(TEST_DB) as conn:
    _ex_reasons = {r[0]: r[1] for r in conn.execute(
        "SELECT id, expired_reason FROM levels WHERE id IN (?,?)",
        (_ex_stale, _ex_shadow)).fetchall()}
check("EX4b 섀도 만료엔 shadow_touch 마커, 일반 만료엔 NULL",
      _ex_reasons[_ex_shadow] == "shadow_touch" and _ex_reasons[_ex_stale] is None)

# ── SS1~SS6: 자체 승률의 분자·분모는 R 트랙 한정 (2026-07-27 교차감사 B-m1) ──
# 같은 날 오전 알림의 승률 '표시 게이트'가 neff_win → neff_r 로 옮겨졌는데
# get_author_self_stats 의 wins/losses 는 판정 방식 무관 전체 합산으로 남아 있었다.
# 혼합형(tp_only+tp_sl) 작성자가 등장하는 순간 "게이트는 R 트랙인데 화면 숫자는
# 전체 표본"이 되어, SL 없는 글의 사실상 자동승이 승률 줄로 새어나간다.
# 프로덕션에 혼합형이 0명인 지금은 표시값이 안 변한다(수리 시점 실측: 작성자 39명
# 전원 화면 변동 0, 내부값 변동은 tp_only 전용 2명뿐 — 둘 다 neff_r=0 이라 미표시).
# 터치율 축(touched/untouched)은 선택편향 처방이라 전체 표본 그대로여야 한다.
_SS_DB = "cache/_test_selfstats.db"
if os.path.exists(_SS_DB):
    os.remove(_SS_DB)
db.init_db(_SS_DB)
with db.connect(_SS_DB) as conn:
    _ss_seq = [0]

    def _ss(author, outcome, r_mult, mode, touched=True, status="touched"):
        _ss_seq[0] += 1
        conn.execute(
            "INSERT INTO levels (signal_key, coin_symbol, ticker, direction, entry_usd,"
            " status, collected_at, author, outcome, r_multiple, judgment_mode,"
            " touched_at, resolved_at) VALUES (?,?,?,'long',1.0,?,?,?,?,?,?,?,?)",
            (f"ss-{_ss_seq[0]}", "SSX", "KRW-SSX", status, now - 3600, author,
             outcome, r_mult, mode, now - 1800 if touched else None, now - 900))

    # (A) SL 미기재 작성자 — 전부 tp_only 라 r_multiple 이 없다(=R 트랙 표본 0)
    for _ in range(3):
        _ss("SSOnlyTP", "hit", None, "tp_only")
    # (B) 혼합형 — tp_only 2건(승) + tp_sl 3건(1승 2패). 전체 합산이면 3승2패(60%),
    #     R 트랙 한정이면 1승2패(33%)
    _ss("SSMixed", "hit", None, "tp_only")
    _ss("SSMixed", "hit", None, "tp_only")
    _ss("SSMixed", "hit", 2.0, "tp_sl")
    _ss("SSMixed", "miss", -1.0, "tp_sl")
    _ss("SSMixed", "miss", -1.0, "tp_sl")
    # 미터치 만료 1건 — 터치율 분모(선택편향 축)가 살아있는지 확인용
    _ss("SSMixed", None, None, None, touched=False, status="expired")
    # 섀도 터치(touched_at NULL)에 R 이 남은 이례 행 — E_LB 원천과 같은 기준으로 제외
    _ss("SSShadow", "hit", 2.0, "tp_sl", touched=False)

    _ss_tp = db.get_author_self_stats(conn, "SSOnlyTP")
    _ss_mix = db.get_author_self_stats(conn, "SSMixed")
    _ss_sh = db.get_author_self_stats(conn, "SSShadow")

check("SS1 tp_only 전용 작성자는 wins/losses 0 - 승률 줄이 자연히 비표시",
      _ss_tp["wins"] == 0 and _ss_tp["losses"] == 0)
check("SS2 tp_only 전용이어도 터치율 축(전체 표본)은 그대로 3건",
      _ss_tp["touched"] == 3 and _ss_tp["untouched_expired"] == 0)
check("SS3 혼합형은 R 트랙 건만 카운트 - 전체합산 3승2패가 아니라 1승2패",
      _ss_mix["wins"] == 1 and _ss_mix["losses"] == 2)
check("SS4 혼합형 터치율 축은 전체 표본 유지(터치 5 / 미터치만료 1)",
      _ss_mix["touched"] == 5 and _ss_mix["untouched_expired"] == 1)
check("SS5 섀도 터치는 R 이 있어도 제외(E_LB 원천 get_author_outcome_rows 와 동일 기준)",
      _ss_sh["wins"] == 0 and _ss_sh["losses"] == 0)

# SS6: 화면 정합 — tp_only 전용 작성자는 게이트(neff_r=0)와 숫자(0승0패)가 동시에
# 승률 줄을 막는다. 게이트만 고쳐두면 숫자는 여전히 3승0패(100%)로 남아 있어,
# 나중에 게이트를 완화하는 순간 그 값이 그대로 노출된다.
_ss_rep = dict(author="SSOnlyTP", author_self_wins=_ss_tp["wins"],
               author_self_losses=_ss_tp["losses"], author_touched_n=_ss_tp["touched"],
               author_untouched_expired=_ss_tp["untouched_expired"],
               author_self_neff_r=0.0, author_rank_min_neff=5.0)
check("SS6 tp_only 전용 작성자 렌더에 🏹 승률 줄 없음",
      not any(l.startswith("🏹") for l in telegram._author_block(_ss_rep)))
os.remove(_SS_DB)

# ── 체인(카드3): 이 파일에서 그동안 resolve_outcome 을 거쳐 쌓인 실제 판정
#    (T8/T9/T10/T11/T13/T15/BM1~BM8 등, run_once/_judge_outcomes 정상 경로로
#    종결된 전체 표본)이 통째로 하나의 유효한 해시체인을 이루는지 확인한다 —
#    개별 단위테스트(test_resilience.py)와 달리 실제 운영 경로 산출물 검증.

# ── T-m3a~e: 하트비트 / 스캔 워터마크 분리 (2026-07-27 2차 교차검토 M-A1) ──────
# last_check_at 한 키가 두 의미를 겸직하고 있었다 — (a) 소급 판정창의 워터마크
# ("어디까지 스캔했나", run_once 의 since_min)와 (b) 기동 하트비트("회차가 깨어났나",
# 공백 감시·show_status). 겸직 때문에, 터치를 하나도 검출하지 않고 반환하는 환율
# 실패 회차가 워터마크를 전진시켜 **그 구간이 영원히 스캔되지 않았다**.
# 수리: last_check_at = 워터마크(성공 스캔에서만 전진), 신규 last_cycle_at = 하트비트.
_M3_DB = "cache/_test_m3_watermark.db"
if os.path.exists(_M3_DB):
    os.remove(_M3_DB)
db.init_db(_M3_DB)
_m3_prev_db_path = settings.SETTINGS["db_path"]
_m3_prev_gap_min = settings.SETTINGS.get("price_check_gap_alert_minutes")
settings.SETTINGS["db_path"] = _M3_DB
settings.SETTINGS["price_check_gap_alert_minutes"] = 120

_m3_t0 = now + 20000
with db.connect(_M3_DB) as conn:
    _m3_lv = dict(coin_symbol="LINK", ticker="KRW-LINK", direction="long",
                  entry_usd=8.0, sl_usd=7.5, tp_usd=9.2, rr=2.0, grade="B", score=62,
                  author="M3", post_url="https://tv.com/m3",
                  collected_at=_m3_t0 - 7200)
    _m3_lv["signal_key"] = db.make_signal_key("LINK", 8.0, "M3", "m3")
    db.upsert_level(conn, _m3_lv)

# fetch_range_since 가 실제로 받은 since_min 을 그대로 관측한다(간접 지표가 아니라
# 소급 판정창 그 자체를 본다). 캔들은 None 을 돌려 터치/판정 경로를 건드리지 않는다.
_m3_since = []
_m3_real_range = upbit.fetch_range_since
_m3_real_prices = upbit.fetch_prices


def _m3_range(market, mins, timeout):
    _m3_since.append(mins)
    return None


# 엔트리(8.0 USD × 1400 = 11,200원)가 현재가의 -5% 안쪽이라 need_low 가 참 →
# 캔들 조회가 실제로 일어난다. 동시에 현재가가 엔트리보다 위라 터치는 안 난다.
_m3_fx = {"ok": True}


def _m3_prices(markets, timeout):
    out = {m: 11500.0 for m in markets}
    if _m3_fx["ok"]:
        out["KRW-USDT"] = USDT_KRW
    else:
        out.pop("KRW-USDT", None)   # 환율 조회 실패 = 업비트 API 가 흔들리는 상황
    return out


upbit.fetch_range_since = _m3_range
upbit.fetch_prices = _m3_prices

price_check.run_once(_m3_t0)                       # 정상 회차 — 워터마크 t0 확정
_m3_since.clear()
_m3_fx["ok"] = False
for _i in range(1, 4):                             # 환율 실패 3회차 (2분 간격)
    price_check.run_once(_m3_t0 + _i * 120)
with db.connect(_M3_DB) as conn:
    _m3_check_during = db.get_meta(conn, "last_check_at")
    _m3_cycle_during = db.get_meta(conn, "last_cycle_at")
    _m3_gap_during = db.get_meta(conn, "last_price_check_gap_min")

check("T-m3e 환율 실패 회차는 하트비트만 전진시킨다(워터마크는 t0 에 고정)",
      _m3_since == []
      and abs(float(_m3_check_during) - _m3_t0) < 1
      and abs(float(_m3_cycle_during) - (_m3_t0 + 360)) < 1)
# b965926(거짓 공백 경보 제거)이 달성했던 것을 되돌리지 않았음을 못박는다 —
# 환율이 계속 실패해도 하트비트가 매 회차 찍히므로 공백은 2분으로 유지된다.
check("T-m3b 환율 실패가 이어져도 회차 공백은 2분(거짓 정지 경보 없음)",
      _m3_gap_during == "2.0")

_m3_fx["ok"] = True
price_check.run_once(_m3_t0 + 480)                 # 복구 회차
# 수리 전이라면 직전 실패 회차가 워터마크를 전진시켜 since_min = int(120/60)+2 = 4분
# → 장애 8분 중 6분이 영구 미스캔. 수리 후엔 t0 부터 전 구간(480초)을 덮는다.
check("T-m3a(핵심) 복구 회차의 소급 판정창이 장애 구간 전체를 덮는다",
      len(_m3_since) == 1 and _m3_since[0] == int(480 / 60) + 2)
with db.connect(_M3_DB) as conn:
    _m3_check_after = db.get_meta(conn, "last_check_at")
check("T-m3a2 성공 회차는 워터마크를 전진시킨다",
      abs(float(_m3_check_after) - (_m3_t0 + 480)) < 1)

# T-m3d: '대상 없음' 조기 반환은 **여전히** 워터마크를 전진시킨다(의도적 예외).
# 환율 실패와 겉모습만 같고 성질이 다르다 — 스캔 대상이 0건이라 놓칠 터치가 없고,
# 이후 수집되는 레벨은 _eff_low 가 collected_at 이후 캔들만 인정한다.
_M3_DB2 = "cache/_test_m3_empty.db"
if os.path.exists(_M3_DB2):
    os.remove(_M3_DB2)
db.init_db(_M3_DB2)
settings.SETTINGS["db_path"] = _M3_DB2
price_check.run_once(_m3_t0 + 600)
with db.connect(_M3_DB2) as conn:
    _m3_empty_check = db.get_meta(conn, "last_check_at")
    _m3_empty_cycle = db.get_meta(conn, "last_cycle_at")
check("T-m3d '대상 없음' 조기 반환은 워터마크도 전진(의도적 예외 — 계약 고정)",
      abs(float(_m3_empty_check) - (_m3_t0 + 600)) < 1
      and abs(float(_m3_empty_cycle) - (_m3_t0 + 600)) < 1)

# T-m3c: last_cycle_at 이 없는 구세대 DB(또는 커밋백 롤백으로 옛 스냅샷이 돌아온
# DB)에서도 last_check_at 폴백으로 공백 판정이 정상 동작한다. 폴백이 없으면 그런
# 회차마다 공백 감시가 1회 침묵한다.
_M3_DB3 = "cache/_test_m3_fallback.db"
if os.path.exists(_M3_DB3):
    os.remove(_M3_DB3)
db.init_db(_M3_DB3)
_m3_sent_before = len(sent_messages)
with db.connect(_M3_DB3) as conn:
    db.set_meta(conn, "last_check_at", str(_m3_t0))      # 하트비트 키는 일부러 없음
    _m3_fallback = price_check._check_price_check_gap(
        conn, _m3_t0 + 200 * 60, settings.get)
    _m3_fallback_gap = db.get_meta(conn, "last_price_check_gap_min")
check("T-m3c last_cycle_at 부재 시 last_check_at 폴백으로 공백 판정(200분 > 임계 120분)",
      _m3_fallback is True and _m3_fallback_gap == "200.0"
      and len(sent_messages) == _m3_sent_before + 1)

upbit.fetch_range_since = _m3_real_range
upbit.fetch_prices = _m3_real_prices
settings.SETTINGS["db_path"] = _m3_prev_db_path
if _m3_prev_gap_min is not None:
    settings.SETTINGS["price_check_gap_alert_minutes"] = _m3_prev_gap_min
for _p in (_M3_DB, _M3_DB2, _M3_DB3):
    if os.path.exists(_p):
        os.remove(_p)


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

# ── RS: 회차 경합 재발송 차단 (2026-07-28 실사고 회귀) ───────────────────────
# 실사고: 03:53·03:54 에 DOT·ENA 터치와 AAVE 예고가 각각 두 번 나갔다. 뒤 회차가
# 앞 회차의 커밋 '이전' DB로 돌아 상태 전이(status='touched')를 못 봤기 때문이다.
# 여기서는 그 상황을 그대로 재현한다 — 발송 후 status 만 되돌리고(=전이 유실),
# append-only 인 alerts_log 는 남긴다. 그 한 겹으로 재발송이 막혀야 한다.
with db.connect(TEST_DB) as conn:
    lv_rs = dict(coin_symbol="RSND", ticker="KRW-RSND", direction="long",
                 entry_usd=10.0, sl_usd=9.5, tp_usd=11.0, rr=2.0, grade="B", score=70,
                 author="AuthRS", author_followers=50000, author_hit_rate=None,
                 author_hit_count=None, author_whitelisted=False, mcap_rank=20,
                 mcap_tier_icon="🥇", post_url="https://tv.com/urs", post_age_minutes=10,
                 collected_at=now - 600)
    lv_rs["signal_key"] = db.make_signal_key("RSND", 10.0, "AuthRS", "urs")
    db.upsert_level(conn, lv_rs)
fake["low"] = fake["high"] = fake["candles"] = None
fake["price"] = 10.0 * USDT_KRW * 0.999   # entry 대비 -0.1% → 터치
_before_rs = len(sent_messages)
price_check.run_once(now + 2000)
check("RS1 정상 첫 발송은 나간다", len(sent_messages) == _before_rs + 1)

_dup_before = (_obs_row() or {})["suppressed_dup"]
with db.connect(TEST_DB) as conn:   # 앞 회차 상태 전이만 유실된 DB 재현
    conn.execute("UPDATE levels SET status='watching', touched_at=NULL "
                 "WHERE coin_symbol='RSND'")
    conn.commit()
_before_rs2 = len(sent_messages)
price_check.run_once(now + 2060)    # 60초 뒤 = 실사고와 같은 간격
check("RS2 상태 전이가 유실돼도 재발송하지 않는다(alerts_log 방어선)",
      len(sent_messages) == _before_rs2)
check("RS3 재발송 차단이 관찰지표(suppressed_dup)에 잡힌다",
      (_obs_row() or {})["suppressed_dup"] == _dup_before + 1)

# 차단 창(_RESEND_BLOCK_SEC)을 넘기면 다시 보낼 수 있어야 한다 - 영구 봉인이 아니라
# '경합 구간만' 막는 장치임을 고정한다.
with db.connect(TEST_DB) as conn:
    conn.execute("UPDATE levels SET status='watching', touched_at=NULL "
                 "WHERE coin_symbol='RSND'")
    conn.commit()
_before_rs3 = len(sent_messages)
price_check.run_once(now + 2000 + price_check._RESEND_BLOCK_SEC + 60)
check("RS4 차단 창을 넘기면 다시 발송된다(영구 봉인 아님)",
      len(sent_messages) == _before_rs3 + 1)

# ── UPS1/UPS2/RPA1: upsert_level UPDATE 비대칭 수리 회귀 (2026-08-03 R1 감사) ──
# 재수집 UPDATE 가 작성자 지표 3종·timeframe_hours 를 COALESCE 없이 덮어써
# fetch 실패(None)가 기존 실측값을 지워버리던 비대칭을 수리했다. 여기서는
# UPDATE 경로가 실제로 보호하는지, reparse_all 은 반대로(진짜 재파싱 결과이므로)
# 명시적으로 덮어쓰는지를 함께 고정한다.
_UPS_DB = "cache/_test_ups_upsert.db"
if os.path.exists(_UPS_DB):
    os.remove(_UPS_DB)
db.init_db(_UPS_DB)
with db.connect(_UPS_DB) as conn:
    _lvups = dict(
        coin_symbol="ZUPSX", ticker="KRW-ZUPSX", direction="long",
        entry_usd=50.0, sl_usd=45.0, tp_usd=60.0, rr=2.0,
        grade="B", score=58, author="AuthUps",
        author_followers=8000, author_hit_rate=0.65, author_hit_count=17,
        author_whitelisted=False, mcap_rank=80, mcap_tier_icon="🥈",
        post_url="https://tv.com/ups", post_age_minutes=5,
        collected_at=now - 100, timeframe_hours=4.0,
    )
    _lvups["signal_key"] = db.make_signal_key("ZUPSX", 50.0, "AuthUps", "ups")
    db.upsert_level(conn, _lvups)
    _idups = conn.execute(
        "SELECT id FROM levels WHERE signal_key=?", (_lvups["signal_key"],)
    ).fetchone()["id"]

    # 재수집 재현: fetch 실패로 작성자 지표·timeframe_hours 가 전부 None 으로
    # 온 재수집 결과를 그대로 upsert — COALESCE 로 기존 실측값이 보존돼야 한다.
    _lvups_refetch = dict(_lvups)
    _lvups_refetch.update(
        author_followers=None, author_hit_rate=None, author_hit_count=None,
        timeframe_hours=None, grade="C", score=40,
    )
    db.upsert_level(conn, _lvups_refetch)
    _rups = conn.execute(
        "SELECT author_followers, author_hit_rate, author_hit_count, "
        "timeframe_hours, grade FROM levels WHERE id=?", (_idups,)
    ).fetchone()

check("UPS1 재수집 fetch 실패(None)에도 작성자 지표 3종 보존(COALESCE)",
      _rups["author_followers"] == 8000 and _rups["author_hit_rate"] == 0.65
      and _rups["author_hit_count"] == 17)
check("UPS2 재수집 timeframe_hours=None 도 기존 4.0 보존(COALESCE)",
      _rups["timeframe_hours"] == 4.0)
check("UPS1b COALESCE 대상 아닌 필드(grade)는 정상 갱신(비대칭 수리가 전체 동결은 아님)",
      _rups["grade"] == "C")

# RPA1: reparse_all 은 진짜 재파싱 결과이므로 COALESCE 가 아니라 명시적으로 덮어써야
# 한다 — 08-03 이전 파서가 '0h' 를 timeframe_hours=0 으로 저장한 구세대 행이 있으면,
# 현재 파서가 재파싱해 None(무의미값 정리)으로 치유해야 한다.
with db.connect(_UPS_DB) as conn:
    conn.execute(
        "UPDATE levels SET raw_text=?, timeframe_hours=0 WHERE id=?",
        ("진입가 50 손절 45 목표 60", _idups))
    conn.commit()
    _changed_rpa1 = db.reparse_all(conn)
    _rrpa1 = conn.execute(
        "SELECT timeframe_hours FROM levels WHERE id=?", (_idups,)
    ).fetchone()["timeframe_hours"]
check("RPA1 reparse_all 이 구세대 timeframe_hours=0 을 재파싱 결과(None)로 치유",
      _rrpa1 is None)

# ── UPS3: author_whitelisted 3상 보존 회귀 (2026-08-07 재검토 C1 재수정) ──
# 종전 `1 if ... else 0` 바인딩은 NULL 을 만들 수 없어 COALESCE 가 죽은 코드 —
# 워쳐 통계 로드 실패(None=알 수 없음)마다 whitelist 1→0 으로 덮어썼다.
# 3상 의미론 고정: True→1 확정, None→기존 보존, False→0 확정(진짜 비화이트).
with db.connect(_UPS_DB) as conn:
    _lvwl = dict(_lvups)
    _lvwl.update(coin_symbol="ZWLX", ticker="KRW-ZWLX", author="AuthWl",
                 author_whitelisted=True)
    _lvwl["signal_key"] = db.make_signal_key("ZWLX", 50.0, "AuthWl", "wl")
    db.upsert_level(conn, _lvwl)
    # 재수집 1: 통계 미로드(None) → 기존 1 보존돼야 한다
    _lvwl_unknown = dict(_lvwl, author_whitelisted=None)
    db.upsert_level(conn, _lvwl_unknown)
    _wl1 = conn.execute("SELECT author_whitelisted FROM levels WHERE signal_key=?",
                        (_lvwl["signal_key"],)).fetchone()["author_whitelisted"]
    # 재수집 2: 통계 로드됨 + 진짜 비화이트(False) → 0 확정 덮어쓰기
    _lvwl_false = dict(_lvwl, author_whitelisted=False)
    db.upsert_level(conn, _lvwl_false)
    _wl2 = conn.execute("SELECT author_whitelisted FROM levels WHERE signal_key=?",
                        (_lvwl["signal_key"],)).fetchone()["author_whitelisted"]
check("UPS3 whitelist 3상: None(미로드) 재수집은 1 보존, False(확정)는 0 덮어쓰기",
      _wl1 == 1 and _wl2 == 0)

# UPS4 (2026-08-08 재검토): tp_ladder_count 도 다른 선택필드처럼 COALESCE 보호
# — 종전엔 `or 0` 바인딩이라 키 부재(None)까지 0 으로 강제 덮어썼다(tps_usd
# 는 COALESCE 인데 짝인 단계수만 예외였음). 키 부재는 보존, 명시적 0(사다리
# 아님으로 치유)은 확정 덮어써야 한다.
with db.connect(_UPS_DB) as conn:
    _lvlad = dict(_lvups, coin_symbol="ZLADX", author="AuthLad",
                 tp_ladder_count=3, tps_usd="[11,12,13]")
    _lvlad["signal_key"] = db.make_signal_key("ZLADX", 50.0, "AuthLad", "lad")
    db.upsert_level(conn, _lvlad)
    _lvlad_missing = {k: v for k, v in _lvlad.items() if k != "tp_ladder_count"}
    db.upsert_level(conn, _lvlad_missing)
    _lad1 = conn.execute("SELECT tp_ladder_count FROM levels WHERE signal_key=?",
                         (_lvlad["signal_key"],)).fetchone()["tp_ladder_count"]
    db.upsert_level(conn, dict(_lvlad, tp_ladder_count=0))
    _lad2 = conn.execute("SELECT tp_ladder_count FROM levels WHERE signal_key=?",
                         (_lvlad["signal_key"],)).fetchone()["tp_ladder_count"]
check("UPS4 tp_ladder_count 3상: 키 부재는 3 보존, 명시적 0 은 확정 덮어쓰기",
      _lad1 == 3 and _lad2 == 0)

if os.path.exists(_UPS_DB):
    os.remove(_UPS_DB)

# ── LG: 발송 원장이 DB 유실을 견디는가 (2026-07-28) ─────────────────────────
# 위 RS 는 DB(alerts_log) 만으로도 통과한다. 그런데 그 표는 **경합에서 지면 통째로
# 사라진다** — levels.db 가 바이너리라 커밋백이 전체 파일 교체밖에 못 하기 때문이다
# (실측: 커밋 28e14a9 와 870e889 의 id 68~70 이 같은 번호에 다른 내용). 그래서 원장을
# DB 밖 NDJSON 으로 뺐다. 여기서는 그 목적이 실제로 달성됐는지만 본다.
from storage import alert_ledger as _AL   # noqa: E402

with db.connect(TEST_DB) as conn:
    _rs_id = conn.execute(
        "SELECT id FROM levels WHERE coin_symbol='RSND'").fetchone()["id"]
check("LG1 발송이 DB 와 별개인 원장 파일에도 남는다",
      _AL.recent_exists(TEST_DB, "RSND", "touch", [_rs_id], 0))

# 사고 재현 — 커밋백이 DB 를 덮어써 alerts_log 행이 사라진 상태.
# 원장은 합집합 병합으로 살아남으므로, 그것만으로 재발송이 막혀야 한다.
with db.connect(TEST_DB) as conn:
    conn.execute("DELETE FROM alerts_log WHERE coin_symbol='RSND'")
    conn.execute("UPDATE levels SET status='watching', touched_at=NULL "
                 "WHERE coin_symbol='RSND'")
    conn.commit()
_before_lg = len(sent_messages)
price_check.run_once(now + 2000 + price_check._RESEND_BLOCK_SEC + 120)
check("LG2 DB 기록이 통째로 사라져도 원장만으로 재발송이 막힌다",
      len(sent_messages) == _before_lg)

# ── MG: merge_files 합집합 동작 단위 검증 (2026-07-28) ───────────────────
# 역회귀 리뷰 4-B: merge_files 자체에 대한 단위 테스트 없음(MEDIUM). 여기서 검증한다.
# 커밋백 실제 호출 패턴 포함 — merge_files(REMOTE, LOCAL, out_path=LOCAL).
import tempfile as _tempfile, shutil as _shutil_mg, json as _json_mg

_tmp_mg = _tempfile.mkdtemp()
_mg_now = time.time()

def _wl(path, rows):
    with open(path, "w", encoding="utf-8") as fh:
        for k, t in rows:
            fh.write(_json_mg.dumps({"k": k, "t": t}, sort_keys=True) + "\n")

_fA = os.path.join(_tmp_mg, "A.ndjson")
_fB = os.path.join(_tmp_mg, "B.ndjson")
_fo = os.path.join(_tmp_mg, "out.ndjson")

_wl(_fA, [("K1", _mg_now - 10), ("K2", _mg_now - 20)])
_wl(_fB, [("K1", _mg_now - 10), ("K3", _mg_now - 30)])
check("MG1 합집합·중복 제거: K1 중복 1건·K2·K3 고유항목 유지 → 3줄",
      _AL.merge_files(_fA, _fB, out_path=_fo) == 3)

_wl(_fA, [("K_old", _mg_now - 10000), ("K_new", _mg_now - 10)])
check("MG2 keep_sec 프루닝: 10000초 전 항목 제거·최근 항목만 유지 → 1줄",
      _AL.merge_files(_fA, out_path=_fo, keep_sec=1000) == 1)

_wl(_fB, [("K_only", _mg_now - 5)])
check("MG3 한쪽 파일 없음: OSError 없이 나머지 파일 항목만 유지 → 1줄",
      _AL.merge_files(os.path.join(_tmp_mg, "missing.ndjson"), _fB, out_path=_fo) == 1)

_wl(_fA, [("KA", _mg_now - 10)])
_wl(_fB, [("KB", _mg_now - 20)])
_shutil_mg.copy2(_fB, _fo)   # out_path = _fB 내용으로 초기화
check("MG4 out_path=입력파일(커밋백 패턴): 자기 자신에 써도 데이터 손실 없음 → 2줄",
      _AL.merge_files(_fA, _fo, out_path=_fo) == 2)

_shutil_mg.rmtree(_tmp_mg, ignore_errors=True)

# ── T35~T35b: TP 클러스터 중복 차단 (2026-07-30) ────────────────────────────
# 같은 코인 ±1% 엔트리 레벨 2건이 동시에 TP1 도달해도 알림은 1건만 발송돼야 한다.
# 재현 사고: AERO TP1 적중 2건(levels 110·111 — BitmexSignalsFee 가 동일 신호를
# URL 달리해 두 번 게시 → 별개 signal_key 로 DB 2행 → TP 알림 2건 발송).
import json as _json35

_T35_DB = "cache/_test_t35_tpdup.db"
if os.path.exists(_T35_DB):
    os.remove(_T35_DB)
_t35_ledger = _alert_ledger.ledger_path(_T35_DB)
if os.path.exists(_t35_ledger):
    os.remove(_t35_ledger)
db.init_db(_T35_DB)
_t35_prev_db = settings.SETTINGS["db_path"]
settings.SETTINGS["db_path"] = _T35_DB

_t35_now = now + 50000
with db.connect(_T35_DB) as conn:
    # Level A: entry=100.0, tps=[103.0, 108.0]
    _lv35a = dict(
        coin_symbol="ZDUPX", ticker="KRW-ZDUPX", direction="long",
        entry_usd=100.0, sl_usd=95.0, tp_usd=108.0, rr=1.5,
        grade="B", score=62, author="AuthDupA", author_followers=5000,
        author_hit_rate=None, author_hit_count=None, author_whitelisted=False,
        mcap_rank=50, mcap_tier_icon="🥇",
        post_url="https://tv.com/dupa", post_age_minutes=10,
        collected_at=_t35_now - 3600,
        tps_usd=_json35.dumps([103.0, 108.0]),
    )
    _lv35a["signal_key"] = db.make_signal_key("ZDUPX", 100.0, "AuthDupA", "dupa")
    db.upsert_level(conn, _lv35a)
    _id35a = conn.execute(
        "SELECT id FROM levels WHERE signal_key=?", (_lv35a["signal_key"],)
    ).fetchone()["id"]
    conn.execute(
        "UPDATE levels SET status='touched', touched_at=?, touch_price_krw=? WHERE id=?",
        (_t35_now - 1800, 100.0 * USDT_KRW, _id35a))

    # Level B: entry=100.3 (0.3% 차이, ±1% 클러스터 내)
    _lv35b = dict(
        coin_symbol="ZDUPX", ticker="KRW-ZDUPX", direction="long",
        entry_usd=100.3, sl_usd=95.3, tp_usd=108.3, rr=1.5,
        grade="B", score=60, author="AuthDupB", author_followers=5000,
        author_hit_rate=None, author_hit_count=None, author_whitelisted=False,
        mcap_rank=50, mcap_tier_icon="🥇",
        post_url="https://tv.com/dupb", post_age_minutes=10,
        collected_at=_t35_now - 3600,
        tps_usd=_json35.dumps([103.3, 108.3]),
    )
    _lv35b["signal_key"] = db.make_signal_key("ZDUPX", 100.3, "AuthDupB", "dupb")
    db.upsert_level(conn, _lv35b)
    _id35b = conn.execute(
        "SELECT id FROM levels WHERE signal_key=?", (_lv35b["signal_key"],)
    ).fetchone()["id"]
    conn.execute(
        "UPDATE levels SET status='touched', touched_at=?, touch_price_krw=? WHERE id=?",
        (_t35_now - 1800, 100.3 * USDT_KRW, _id35b))
    # M-2 게이트(본알림 발송된 신호만 TP 알림) 이후에도 T35 의 원래 목적(클러스터
    # 중복 차단)을 검증하려면 터치 본알림 발송 이력이 있어야 한다 — 클러스터 터치
    # 1건으로 기록(실운영과 동일한 형태: 병합 발송 시 ids 에 두 레벨 모두 포함)
    db.record_alert(conn, "ZDUPX", "touch", [_id35a, _id35b],
                    "2026-07-30", _t35_now - 1800)

# 현재가 = TP1 초과(104.0), TP2 미달(108.0): 양쪽 TP1 모두 도달
_t35_price_saved = fake["price"]
_t35_candles_saved = fake["candles"]
_t35_high_saved = fake["high"]
_t35_low_saved = fake["low"]
fake["price"] = 104.0 * USDT_KRW
fake["candles"] = [(_t35_now - 1801, _t35_now - 10,
                    104.0 * USDT_KRW, 98.0 * USDT_KRW)]
fake["high"] = None
fake["low"] = None

_t35_before = len(sent_messages)
price_check.run_once(_t35_now)
_t35_sent = len(sent_messages) - _t35_before

fake["price"] = _t35_price_saved
fake["candles"] = _t35_candles_saved
fake["high"] = _t35_high_saved
fake["low"] = _t35_low_saved

check("T35 TP 클러스터 중복 차단 — ±1% 엔트리 2건 TP1 동시 도달, 알림 1건만 발송",
      _t35_sent == 1)

with db.connect(_T35_DB) as conn:
    _r35a = conn.execute(
        "SELECT tp_alert_idx FROM levels WHERE id=?", (_id35a,)
    ).fetchone()["tp_alert_idx"]
    _r35b = conn.execute(
        "SELECT tp_alert_idx FROM levels WHERE id=?", (_id35b,)
    ).fetchone()["tp_alert_idx"]
check("T35b 두 레벨 모두 tp_alert_idx=1 전진(차단된 레벨도 다음 TP 감시 이어감)",
      _r35a == 1 and _r35b == 1)

settings.SETTINGS["db_path"] = _t35_prev_db
if os.path.exists(_T35_DB):
    os.remove(_T35_DB)
if os.path.exists(_t35_ledger):
    os.remove(_t35_ledger)

# ── PTP1: pending_tp_kind idx 어긋남 stale clear (2026-08-03 감사 회귀) ─────
# 다중 TP + 롤백 CAS 실패로 pending_tp_kind 가 현재 tp_alert_idx 와 어긋난
# 채 남으면, 다음 회차가 그걸 그대로 force-hit 해 도달 안 한 TP 를 오판정한다.
# idx 불일치면 force 대신 clear 만 하고 판정은 자연 스캔 결과(미도달=None)를
# 따라야 한다.
_PTP_DB = "cache/_test_ptp_pending.db"
if os.path.exists(_PTP_DB):
    os.remove(_PTP_DB)
_ptp_ledger = _alert_ledger.ledger_path(_PTP_DB)
if os.path.exists(_ptp_ledger):
    os.remove(_ptp_ledger)
db.init_db(_PTP_DB)
_ptp_prev_db = settings.SETTINGS["db_path"]
settings.SETTINGS["db_path"] = _PTP_DB

_ptp_now = now + 60000
with db.connect(_PTP_DB) as conn:
    # entry=100, tps=[103, 108]. tp_alert_idx=1 → TP1 이미 적중, TP2(108) 감시 중.
    # 정상 pending 형식은 f"tp{idx+1}"="tp2" 인데, 여기 저장값은 "tp1" — 어긋남.
    _lvptp = dict(
        coin_symbol="ZPTPX", ticker="KRW-ZPTPX", direction="long",
        entry_usd=100.0, sl_usd=90.0, tp_usd=108.0, rr=1.8,
        grade="B", score=62, author="AuthPTP", author_followers=5000,
        author_hit_rate=None, author_hit_count=None, author_whitelisted=False,
        mcap_rank=50, mcap_tier_icon="🥇",
        post_url="https://tv.com/ptp", post_age_minutes=10,
        collected_at=_ptp_now - 3600,
        tps_usd=_json35.dumps([103.0, 108.0]),
    )
    _lvptp["signal_key"] = db.make_signal_key("ZPTPX", 100.0, "AuthPTP", "ptp")
    db.upsert_level(conn, _lvptp)
    _idptp = conn.execute(
        "SELECT id FROM levels WHERE signal_key=?", (_lvptp["signal_key"],)
    ).fetchone()["id"]
    conn.execute(
        "UPDATE levels SET status='touched', touched_at=?, touch_price_krw=?, "
        "tp_alert_idx=1 WHERE id=?",
        (_ptp_now - 1800, 100.0 * USDT_KRW, _idptp))
    db.record_alert(conn, "ZPTPX", "touch", [_idptp], "2026-08-04", _ptp_now - 1800)
    db.set_pending_tp(conn, _idptp, "tp1")

# 현재가 = TP2(108) 미달(105) — 자연 스캔으로도 force 로도 TP2 는 도달하면 안 된다.
_ptp_price_saved = fake["price"]
_ptp_candles_saved = fake["candles"]
_ptp_high_saved = fake["high"]
_ptp_low_saved = fake["low"]
fake["price"] = 105.0 * USDT_KRW
fake["candles"] = [(_ptp_now - 1801, _ptp_now - 10,
                    105.0 * USDT_KRW, 99.0 * USDT_KRW)]
fake["high"] = None
fake["low"] = None

price_check.run_once(_ptp_now)

fake["price"] = _ptp_price_saved
fake["candles"] = _ptp_candles_saved
fake["high"] = _ptp_high_saved
fake["low"] = _ptp_low_saved

with db.connect(_PTP_DB) as conn:
    _rptp = conn.execute(
        "SELECT status, tp_alert_idx, pending_tp_kind FROM levels WHERE id=?",
        (_idptp,)
    ).fetchone()
check("PTP1 idx 어긋난 stale pending 은 force-hit 대신 clear 된다",
      _rptp["pending_tp_kind"] is None)
check("PTP1b clear 후에도 미도달 TP2 는 오판정되지 않는다(idx·상태 불변)",
      _rptp["tp_alert_idx"] == 1 and _rptp["status"] == "touched")

settings.SETTINGS["db_path"] = _ptp_prev_db
if os.path.exists(_PTP_DB):
    os.remove(_PTP_DB)
if os.path.exists(_ptp_ledger):
    os.remove(_ptp_ledger)

# ── PV1~PV7: 접근 예고 발송 스위치 (2026-07-31 사용자 결정 — 예고 완전 제거) ──
# preview_alert_enabled=False 면 예고는 발송·기록(alerts_log/원장) 없이 상태 전이
# (previewed)와 관찰 집계(previews_total/preview_dwell)만 수행하고, 이후 터치
# 본알림은 정상 발송돼야 한다. True 면 기존 동작(T2/T3 가 증명) 그대로.
_PV_DB = "cache/_test_pv_preview.db"
if os.path.exists(_PV_DB):
    os.remove(_PV_DB)
_pv_ledger = _alert_ledger.ledger_path(_PV_DB)
if os.path.exists(_pv_ledger):
    os.remove(_pv_ledger)
db.init_db(_PV_DB)
_pv_prev_db = settings.SETTINGS["db_path"]
settings.SETTINGS["db_path"] = _PV_DB
settings.SETTINGS["preview_alert_enabled"] = False


def _pv_level(coin, entry, key, tp=None):
    with db.connect(_PV_DB) as conn:
        lv = dict(coin_symbol=coin, ticker=f"KRW-{coin}", direction="long",
                  entry_usd=entry, sl_usd=entry * 0.94, tp_usd=tp or entry * 1.15,
                  rr=2.4, grade="B", score=62, author=f"PV_{key}",
                  author_followers=5000, author_hit_rate=0.67, author_hit_count=12,
                  author_whitelisted=False, mcap_rank=19, mcap_tier_icon="🥇",
                  post_url=f"https://tv.com/{key}", post_age_minutes=100,
                  collected_at=now - 600)
        lv["signal_key"] = db.make_signal_key(coin, entry, lv["author"], lv["post_url"])
        db.upsert_level(conn, lv)
        return conn.execute("SELECT id FROM levels WHERE signal_key=?",
                            (lv["signal_key"],)).fetchone()["id"]


def _pv_stats(key):
    """PV DB daily_stats 를 전 행 합산(자정 경계 무관) — _amb_totals 와 같은 방식."""
    with db.connect(_PV_DB) as conn:
        rows = db.get_daily_stats(conn, days=60)
    return sum(r.get(key, 0) or 0 for r in rows)


_pvx = _pv_level("PVX", 10.0, "pv_x", tp=11.5)
fake["low"] = fake["high"] = fake["candles"] = None
fake["price"] = 10.0 * USDT_KRW * 1.006   # +0.6% - 예고 밴드 진입
sent_before_pv = len(sent_messages)
s_pv1 = price_check.run_once(now + 3000)
with db.connect(_PV_DB) as conn:
    _pv_status = conn.execute("SELECT status FROM levels WHERE id=?", (_pvx,)).fetchone()[0]
    _pv_alerts = conn.execute("SELECT COUNT(*) FROM alerts_log").fetchone()[0]
check("PV1 예고 OFF - 무발송·발송기록(alerts_log/원장) 없음",
      len(sent_messages) == sent_before_pv and s_pv1["previews"] == 0
      and _pv_alerts == 0 and not os.path.exists(_pv_ledger)
      and s_pv1.get("preview_disabled") == 1)
check("PV1b 예고 OFF 는 suppressed 에 안 섞임(억제 지표 오염 방지)",
      s_pv1.get("suppressed") == 0)
check("PV2 예고 OFF 여도 상태 전이(mark_previewed)는 수행", _pv_status == "previewed")
check("PV2b 관찰집계 - previews_total 은 그대로 +1(시계열 연속)",
      _pv_stats("previews_total") == 1)

# 밴드 체류 회차 — dup_preview 경로는 스위치와 무관하게 동일해야 한다
s_pv2 = price_check.run_once(now + 3120)
check("PV3 예고 OFF 상태의 밴드 체류 - preview_dwell 로 집계(무발송)",
      len(sent_messages) == sent_before_pv and _pv_stats("preview_dwell") == 1
      and s_pv2.get("preview_disabled") is None)

# 이어지는 터치 - 본알림은 정상 발송 (첫 알림 = 터치 본알림)
fake["price"] = 10.0 * USDT_KRW * 1.002
fake["low"] = 9.90 * USDT_KRW
s_pv3 = price_check.run_once(now + 3240)
check("PV4 예고 OFF 이후 터치 본알림 정상 발송(유음)",
      s_pv3["touches"] == 1 and len(sent_messages) == sent_before_pv + 1
      and "진입가 터치" in sent_messages[-1] and sent_urgency[-1] == "high")

# 터치 시 거래량 감시 등록에 밴드가 함께 저장된다 (대표 레벨 entry 10.0 / TP1 11.5,
# 터치 시점 환율 1400 고정 → band_low 10×0.9×1400=12600, band_high 11.5×1400=16100)
with db.connect(_PV_DB) as conn:
    _pv_vw = conn.execute(
        "SELECT band_low_krw, band_high_krw, alerted, tp1_krw, tp_count, tps_krw "
        "FROM volume_watch WHERE ticker='KRW-PVX'").fetchone()
check("PV5 터치 등록 시 감시 밴드 저장 [entry-10%, 마지막TP] (터치 환율 고정)",
      _pv_vw is not None and abs(_pv_vw["band_low_krw"] - 12600.0) < 1e-6
      and abs(_pv_vw["band_high_krw"] - 16100.0) < 1e-6 and _pv_vw["alerted"] == 0)
check("PV5b 급증 알림 TP 표시용 스냅샷 저장 (tp_usd 단일 폴백 → 1단계)",
      abs(_pv_vw["tp1_krw"] - 16100.0) < 1e-6 and _pv_vw["tp_count"] == 1
      and _json.loads(_pv_vw["tps_krw"]) == [16100.0])

# 스위치를 되돌리면(True) 예고가 즉시 재개된다 — 새 코인으로 기존 동작 확인
settings.SETTINGS["preview_alert_enabled"] = True
_pvy = _pv_level("PVY", 20.0, "pv_y")
fake["low"] = fake["high"] = fake["candles"] = None
fake["price"] = 20.0 * USDT_KRW * 1.006
s_pv4 = price_check.run_once(now + 3360)
check("PV6 스위치 True 복귀 - 예고 발송 즉시 재개(무음)",
      s_pv4["previews"] == 1 and len(sent_messages) == sent_before_pv + 2
      and "진입가 접근" in sent_messages[-1] and sent_urgency[-1] == "low")
check("PV7 재개 후에도 관찰집계는 연속(previews_total 누적 2)",
      _pv_stats("previews_total") == 2)

# PV9: 멀티TP 사다리 셋업 — 밴드 상단은 마지막 유효 TP (2026-07-31 2차 결정.
# 백테스트: TP1 상단이면 짧은 TP1 셋업의 TP2~N 구간이 급증 감시 사각지대).
# entry 30.0, tps [31.0, 33.0, 36.0] → 상단 36×1400=50400, tps_krw 3단계 전부 저장
with db.connect(_PV_DB) as conn:
    lv9 = dict(coin_symbol="PVZ", ticker="KRW-PVZ", direction="long",
               entry_usd=30.0, sl_usd=28.0, tp_usd=31.0,
               tps_usd="[31.0, 33.0, 36.0]",
               rr=2.4, grade="B", score=62, author="PV_pv_z",
               author_followers=5000, author_hit_rate=0.67, author_hit_count=12,
               author_whitelisted=False, mcap_rank=19, mcap_tier_icon="🥇",
               post_url="https://tv.com/pv_z", post_age_minutes=100,
               collected_at=now - 600)
    lv9["signal_key"] = db.make_signal_key("PVZ", 30.0, lv9["author"], lv9["post_url"])
    db.upsert_level(conn, lv9)
fake["price"] = 30.0 * USDT_KRW * 1.002
fake["low"] = 29.7 * USDT_KRW
s_pv9 = price_check.run_once(now + 3480)
with db.connect(_PV_DB) as conn:
    _pv9_vw = conn.execute(
        "SELECT band_high_krw, tp1_krw, tp_count, tps_krw FROM volume_watch "
        "WHERE ticker='KRW-PVZ'").fetchone()
check("PV9 멀티TP - 밴드 상단 = 마지막 TP(50400), tps_krw 3단계 저장",
      s_pv9["touches"] == 1 and _pv9_vw is not None
      and abs(_pv9_vw["band_high_krw"] - 50400.0) < 1e-6
      and abs(_pv9_vw["tp1_krw"] - 43400.0) < 1e-6 and _pv9_vw["tp_count"] == 3
      and _json.loads(_pv9_vw["tps_krw"]) == [43400.0, 46200.0, 50400.0])

settings.SETTINGS["db_path"] = _pv_prev_db
os.remove(_PV_DB)
if os.path.exists(_pv_ledger):
    os.remove(_pv_ledger)

# ── PV8: _volume_band_tp1 오염 방어선 직접 검증 (2026-07-31 감사 minor — 이
# 헬퍼의 존재 이유인 sanity 필터가 스위트에서 한 번도 안 돌던 커버리지 공백) ──
_bt = price_check._volume_band_tp1
check("PV8 서수 오염 tps [1.0, 11.5] - 1.0 걸러지고 11.5",
      _bt({"entry_usd": 10.0, "tps_usd": "[1.0, 11.5]"}) == 11.5)
check("PV8b 전부 무효(상한 4x 밖) + tp_usd 도 무효 - None(+10% 폴백行)",
      _bt({"entry_usd": 10.0, "tps_usd": "[50.0]", "tp_usd": 999.0}) is None)
check("PV8c 비수치 원소 혼입 - 조용히 스킵하고 유효값 채택",
      _bt({"entry_usd": 10.0, "tps_usd": "[\"a\", 11.5]"}) == 11.5)
check("PV8d tp == entry (미만 경계) - None",
      _bt({"entry_usd": 10.0, "tps_usd": "[10.0]", "tp_usd": 10.0}) is None)
check("PV8e tps 없음 - tp_usd 단일값 폴백",
      _bt({"entry_usd": 10.0, "tps_usd": None, "tp_usd": 11.0}) == 11.0)
# PV8f/g: 2인검토 A-1 — tp_usd(대표 TP) 합집합 + 정렬 재보증. 실데이터 ETH
# (tp=2000/tps=[1975]) 유형에서 터치 알림과 급증 알림의 TP 가 갈리지 않게.
_bts = price_check._volume_band_tps
check("PV8f 대표 TP 합집합 - tps 에 없는 tp_usd 흡수 (중복은 미추가)",
      _bts({"entry_usd": 10.0, "tps_usd": "[11.0]", "tp_usd": 12.0}) == [11.0, 12.0]
      and _bts({"entry_usd": 10.0, "tps_usd": "[11.0, 12.0]", "tp_usd": 12.0})
      == [11.0, 12.0])
check("PV8g 정렬 재보증 - 비정렬 입력도 마지막 원소 = 최고 TP",
      _bts({"entry_usd": 10.0, "tps_usd": "[12.0, 11.0]"}) == [11.0, 12.0])

# ── RV1~RV5: upbit.fetch_rvol_1h 실물 로직 (가짜 requests.get, HTTP 없음) ──────
# 2026-07-31 Feature 4 판정 지표 교체(24h/7일평균 → 최근 1h/20h 완결 60분봉 평균).
# fetch_range_since 와 같은 함정을 공유한다 — 업비트는 무거래 분에 캔들을 만들지
# 않아 count 기반 요청이 의도 구간보다 먼 과거를 덮는다 → 시간창 필터 필수.


def _vcandle(ts, acc_price):
    return {"candle_date_time_utc": _iso(ts), "candle_acc_trade_price": acc_price}


def _rv_get_factory(minute_candles, hour_candles):
    def _get(url, params=None, timeout=None):
        if url.endswith("/candles/minutes/1"):
            # 2026-08-01 재검토: count 회귀 방어 — 61 미만이면 분 경계에서 최고령
            # 캔들이 절단된다(B-3). 팩토리가 params 를 무시해 뮤테이션을 못 잡던
            # 공백을 assert 로 봉인.
            assert (params or {}).get("count", 0) >= 61, "minutes/1 count>=61 필요"
            return _FakeResp(minute_candles)
        if url.endswith("/candles/minutes/60"):
            assert (params or {}).get("count", 0) >= 22, "minutes/60 count>=22 필요"
            return _FakeResp(hour_candles)
        raise AssertionError(f"예상 밖 URL: {url}")
    return _get


_rv_now = time.time()
# RV1 정상 케이스: 1분봉 60개 전부 최근 60분 이내(각 5백만) + 60분봉은 [0] 진행 중
# (30분 전 시작, 완결 판정에서 제외돼야) + 완결 20개(각 5천만). 이 중 최신 완결봉
# (-1.5h 시작)은 분모 자기희석 제거 필터(종료 1h 경과)에 걸려 제외되고 19개 평균
# — 전부 같은 값이라 avg 는 동일 (희석 제거 자체는 RV6 이 검증).
# +30초 오프셋: 함수 내부의 now 는 _rv_now 보다 약간 뒤라, 정확히 경계(-3600)에
# 걸친 캔들은 실행 지연에 따라 포함/제외가 흔들린다 — 경계 밖 판정은 RV3 몫.
_rv1_min = [_vcandle(_rv_now - (i + 1) * 60 + 30, 5e6) for i in range(60)]
_rv1_hr = ([_vcandle(_rv_now - 1800, 9e9)]            # 진행 중 — 평균에 섞이면 안 됨
           + [_vcandle(_rv_now - 1800 - (i + 1) * 3600, 5e7) for i in range(20)])
_requests_mod.get = _rv_get_factory(_rv1_min, _rv1_hr)
_rv1 = _real_fetch_rvol_1h("KRW-RV", 5.0)
check("RV1 최근 1h 합산 + 완결 60분봉 평균(진행 중 [0] 제외)",
      _rv1 is not None and abs(_rv1["last_60m"] - 60 * 5e6) < 1
      and abs(_rv1["avg_20h"] - 5e7) < 1)

# RV2 신규상장 가드: 완결 60분봉 12개 미만이면 None
_rv2_hr = [_vcandle(_rv_now - (i + 1) * 3600, 5e7) for i in range(11)]
_requests_mod.get = _rv_get_factory(_rv1_min, _rv2_hr)
check("RV2 완결 60분봉 12개 미만(신규상장) - None", _real_fetch_rvol_1h("KRW-RV", 5.0) is None)

# RV3 저유동성: 1분봉 60개 중 60분 밖 캔들은 합산 제외(무거래 분 캔들 미생성 함정)
_rv3_min = ([_vcandle(_rv_now - (i + 1) * 60, 1e6) for i in range(10)]        # 최근 10분
            + [_vcandle(_rv_now - 3600 - (i + 1) * 600, 1e6) for i in range(50)])  # 60분 밖
_requests_mod.get = _rv_get_factory(_rv3_min, _rv1_hr)
_rv3 = _real_fetch_rvol_1h("KRW-RV", 5.0)
check("RV3 60분 시간창 필터 - 구간 밖 캔들 미합산", abs(_rv3["last_60m"] - 10 * 1e6) < 1)

# RV4 낡은 60분봉(21h 밖)은 평균에서 제외 — 완결 12개 규칙도 시간창 안에서만 센다
_rv4_hr = ([_vcandle(_rv_now - 1800 - (i + 1) * 3600, 5e7) for i in range(5)]      # 창 안 5개
           + [_vcandle(_rv_now - 40 * 3600 - i * 3600, 9e9) for i in range(16)])   # 창 밖 16개
_requests_mod.get = _rv_get_factory(_rv1_min, _rv4_hr)
check("RV4 21h 시간창 - 낡은 캔들 제외로 완결 12개 미달 → None",
      _real_fetch_rvol_1h("KRW-RV", 5.0) is None)

# RV5 조회 실패는 예외 없이 None (핫패스 격리 관례)
_requests_mod.get = _u4_get
check("RV5 조회 실패 - None", _real_fetch_rvol_1h("KRW-RV", 5.0) is None)

# RV6 분모 자기희석 제거 (2026-07-31 감사 minor): 직전 완결 60분봉은 분자(최근
# 60분 롤링)와 최대 59분 겹치므로 분모에서 제외 — 정각 직후 급증 봉이 완결되며
# 스스로 평균을 끌어올려 ratio 를 임계 아래로 희석하던 창을 없앤다. 직전 완결봉에
# 90억을 넣어도 평균은 나머지 19개(각 5천만)로만 계산돼야 한다.
_rv6_hr = ([_vcandle(_rv_now - 1800, 9e9),          # 진행 중 — 제외
            _vcandle(_rv_now - 5400, 9e9)]          # 직전 완결(분자와 중첩) — 제외
           + [_vcandle(_rv_now - 5400 - (i + 1) * 3600, 5e7) for i in range(19)])
_requests_mod.get = _rv_get_factory(_rv1_min, _rv6_hr)
_rv6 = _real_fetch_rvol_1h("KRW-RV", 5.0)
check("RV6 직전 완결봉 분모 제외 - 급증 자기희석 없음",
      _rv6 is not None and abs(_rv6["avg_20h"] - 5e7) < 1)
_requests_mod.get = _orig_requests_get

# ── VS1~VS10: _check_volume_spikes 판정 (RVOL ×5 + 절대 하한 + 감시 제외 밴드) ──
_VS_DB = "cache/_test_volspike.db"
if os.path.exists(_VS_DB):
    os.remove(_VS_DB)
db.init_db(_VS_DB)

_vs_vol = {"val": None}
_vs_calls = []


def _vs_rvol(m, t):
    _vs_calls.append(m)
    return _vs_vol["val"]


upbit.fetch_rvol_1h = _vs_rvol
_vs_now = time.time()


def _vs_row(ticker):
    with db.connect(_VS_DB) as conn:
        r = conn.execute("SELECT * FROM volume_watch WHERE ticker=?", (ticker,)).fetchone()
        return dict(r) if r else None


def _vs_run(prices):
    with db.connect(_VS_DB) as conn:
        price_check._check_volume_spikes(conn, _vs_now + 60, settings.get, prices)


# VS1: add_volume_watch 가 밴드를 저장한다
with db.connect(_VS_DB) as conn:
    db.add_volume_watch(conn, "KRW-VSA", "VSA", _vs_now,
                        band_low_krw=9000.0, band_high_krw=12000.0)
    conn.commit()
_vsa = _vs_row("KRW-VSA")
check("VS1 add_volume_watch 밴드 저장",
      _vsa["band_low_krw"] == 9000.0 and _vsa["band_high_krw"] == 12000.0)

# VS2: 활성 감시(alerted=0) 중 재터치 — 타이머(added_at)는 유지, 밴드는 합집합 확장
# (2026-07-31 감사 major 수정: 첫 터치 밴드 고정이면 아래쪽 클러스터 재터치 직후
# 같은 회차 밴드 이탈 판정이 방금 유효해진 감시를 삭제하는 무성 커버리지 소실)
with db.connect(_VS_DB) as conn:
    db.add_volume_watch(conn, "KRW-VSA", "VSA", _vs_now + 30,
                        band_low_krw=7650.0, band_high_krw=9350.0)
    conn.commit()
_vsa2 = _vs_row("KRW-VSA")
check("VS2 활성 행 재터치 - 타이머 유지 + 밴드 합집합 [7650, 12000]",
      _vsa2["added_at"] == _vs_now and _vsa2["band_low_krw"] == 7650.0
      and _vsa2["band_high_krw"] == 12000.0)

# VS2b: 합집합에서 NULL(무경계)은 우선한다 — 재터치 밴드 계산 실패(None) 시
# 좁은 옛 밴드가 새 셋업을 자르지 않도록 양쪽 다 무경계로 넓어져야 한다
with db.connect(_VS_DB) as conn:
    db.add_volume_watch(conn, "KRW-VSN", "VSN", _vs_now,
                        band_low_krw=900.0, band_high_krw=1200.0)
    db.add_volume_watch(conn, "KRW-VSN", "VSN", _vs_now + 30)   # 밴드 None 재터치
    conn.commit()
_vsn = _vs_row("KRW-VSN")
check("VS2b 합집합의 NULL 우선 - 무경계로 확장",
      _vsn["band_low_krw"] is None and _vsn["band_high_krw"] is None)
with db.connect(_VS_DB) as conn:
    db.remove_volume_watch(conn, "KRW-VSN")
    conn.commit()

# VS3: 발송 완료(alerted=1) 행은 새 터치가 리셋하며 밴드도 새 값으로 교체
with db.connect(_VS_DB) as conn:
    db.mark_volume_alerted(conn, "KRW-VSA", _vs_now + 40)
    db.add_volume_watch(conn, "KRW-VSA", "VSA", _vs_now + 50,
                        band_low_krw=9500.0, band_high_krw=13000.0)
    conn.commit()
_vsa3 = _vs_row("KRW-VSA")
check("VS3 발송완료 행 리셋 시 밴드 교체",
      _vsa3["alerted"] == 0 and _vsa3["band_low_krw"] == 9500.0
      and _vsa3["band_high_krw"] == 13000.0)

# VS4: 밴드 내 + ratio ≥5 + 절대 하한 통과 → 알림 발송 + alerted=1
_vs_vol["val"] = {"last_60m": 9e8, "avg_20h": 1e8}   # 9.0x, 9억 ≥ 하한 5억
sent_before_vs = len(sent_messages)
_vs_run({"KRW-VSA": 10000.0})
_vs_msg = sent_messages[-1]
check("VS4 밴드 내 + 9.0x 급증 - 알림 발송·발송표식",
      len(sent_messages) == sent_before_vs + 1 and _vs_row("KRW-VSA")["alerted"] == 1
      and "거래량 급증" in _vs_msg and "VSA" in _vs_msg)
check("VS4b 새 지표 렌더 - 최근 1시간/20시간 평균 라벨",
      tg._truncate_line("    최근 1시간:  9.0억  (9.0x 급증)") in _vs_msg
      and "20시간 평균:  1.0억" in _vs_msg
      and "24h" not in _vs_msg and "7일" not in _vs_msg)
check("VS4c TP 스냅샷 없는 행(NULL) - TP 행 생략", "TP:" not in _vs_msg)

# VS5: ratio < multiplier 는 미발송 (경계 미만).
# 2026-08-17 리뷰 대응: 종전 4.9x/5.0x 하드코딩은 완화(3.0) 후 stale — 실제 SETTINGS
# 값을 override 해 임계 재조정(2.0/3.0)에도 회귀가 잡히게 한다.
_vs_saved_mult = settings.SETTINGS["volume_spike_multiplier"]
_vs_saved_min = settings.SETTINGS["volume_spike_min_krw_60m"]
settings.SETTINGS["volume_spike_multiplier"] = 5.0   # VS5/VS5b 는 5.0 임계 전제
settings.SETTINGS["volume_spike_min_krw_60m"] = 200_000_000
with db.connect(_VS_DB) as conn:
    db.add_volume_watch(conn, "KRW-VSB", "VSB", _vs_now,
                        band_low_krw=900.0, band_high_krw=1200.0)
    conn.commit()
_vs_vol["val"] = {"last_60m": 4.9e8, "avg_20h": 1e8}   # 4.9x < 5.0
_vs_run({"KRW-VSB": 1000.0})
check("VS5 ×5 미만(4.9x) - 미발송", len(sent_messages) == sent_before_vs + 1
      and _vs_row("KRW-VSB")["alerted"] == 0)

# VS5b: 정확히 ×5 는 발송 (이상 경계)
_vs_vol["val"] = {"last_60m": 5e8, "avg_20h": 1e8}
_vs_run({"KRW-VSB": 1000.0})
check("VS5b 정확히 ×5 - 발송(이상 경계)", len(sent_messages) == sent_before_vs + 2
      and _vs_row("KRW-VSB")["alerted"] == 1)

# VS5c: mark_volume_alerted CAS (2026-08-03 감사) — alerted=1 인 행에 재호출하면
# False 반환(경합 사이클이 이미 잡음 시나리오). rowcount=0.
with db.connect(_VS_DB) as conn:
    _cas_first = db.mark_volume_alerted(conn, "KRW-VSB", _vs_now + 90)
    _cas_second = db.mark_volume_alerted(conn, "KRW-VSB", _vs_now + 91)
    conn.commit()
check("VS5c mark_volume_alerted CAS - 이미 alerted=1 이면 False (동시 사이클 중복 차단)",
      _cas_first is False and _cas_second is False)

# VS6: ratio 는 넘어도 최근 1h 절대금액 < volume_spike_min_krw_60m 이면 미발송
with db.connect(_VS_DB) as conn:
    db.add_volume_watch(conn, "KRW-VSC", "VSC", _vs_now,
                        band_low_krw=90.0, band_high_krw=120.0)
    conn.commit()
_vs_vol["val"] = {"last_60m": 3e7, "avg_20h": 5e6}    # 6.0x 지만 0.3억 < 하한 2억(override)
_vs_run({"KRW-VSC": 100.0})
check("VS6 저유동 절대 하한 가드 - 6.0x 여도 미발송",
      len(sent_messages) == sent_before_vs + 2 and _vs_row("KRW-VSC")["alerted"] == 0)
with db.connect(_VS_DB) as conn:   # 다음 케이스 오염 방지 - 활성 잔여 행 정리
    db.remove_volume_watch(conn, "KRW-VSC")
    conn.commit()
# VS5~VS6 임계 override 복원 — 이후 VS7+ 는 SETTINGS 원값 사용
settings.SETTINGS["volume_spike_multiplier"] = _vs_saved_mult
settings.SETTINGS["volume_spike_min_krw_60m"] = _vs_saved_min

# VS7: TP1 위 이탈(현재가 > band_high) - 행 삭제·무알림·거래량 API 미호출
with db.connect(_VS_DB) as conn:
    db.add_volume_watch(conn, "KRW-VSD", "VSD", _vs_now,
                        band_low_krw=900.0, band_high_krw=1200.0)
    conn.commit()
_vs_vol["val"] = {"last_60m": 9e8, "avg_20h": 1e8}    # 급증 중이어도
_vs_calls.clear()
_vs_run({"KRW-VSD": 1300.0})                          # band_high 1200 위로 이탈
check("VS7 TP1 위 이탈 - 감시 행 삭제·무알림·API 미호출",
      _vs_row("KRW-VSD") is None and len(sent_messages) == sent_before_vs + 2
      and "KRW-VSD" not in _vs_calls)

# VS8: 진입가 -10% 아래 이탈(현재가 < band_low) - 동일하게 즉시 종료
with db.connect(_VS_DB) as conn:
    db.add_volume_watch(conn, "KRW-VSE", "VSE", _vs_now,
                        band_low_krw=900.0, band_high_krw=1200.0)
    conn.commit()
_vs_run({"KRW-VSE": 850.0})
check("VS8 -10% 아래 이탈 - 감시 행 삭제", _vs_row("KRW-VSE") is None)

# VS9: 레거시 행(밴드 NULL)은 밴드 체크 미적용 - 가격이 어디 있든 감시 유지·판정 수행
with db.connect(_VS_DB) as conn:
    conn.execute("INSERT INTO volume_watch (ticker, coin_symbol, added_at) "
                 "VALUES ('KRW-VSL', 'VSL', ?)", (_vs_now,))
    conn.commit()
_vs_vol["val"] = {"last_60m": 9e8, "avg_20h": 1e8}
_vs_run({"KRW-VSL": 999999.0})                        # 밴드가 있었다면 명백한 이탈 가격
check("VS9 레거시 NULL 밴드 - 이탈 판정 없이 감시 지속 + 급증 알림 정상",
      len(sent_messages) == sent_before_vs + 3 and _vs_row("KRW-VSL")["alerted"] == 1)

# VS9b: 밴드 있는 행이 prices 에 현재가가 없으면 그 회차 판정 전체를 유예한다
# — 급증 값이어도 발송·API 호출 없이 감시만 유지 (2026-07-31 감사 minor: 밴드가
# 무력화된 채 셋업 밖 가격대의 급증 알림이 나가는 경로 봉쇄)
with db.connect(_VS_DB) as conn:
    db.add_volume_watch(conn, "KRW-VSF", "VSF", _vs_now,
                        band_low_krw=900.0, band_high_krw=1200.0)
    conn.commit()
_vs_vol["val"] = {"last_60m": 9e8, "avg_20h": 1e8}    # 9.0x 급증 값이어도
_vs_calls.clear()
_vs_run({})
check("VS9b 현재가 미보유+밴드 행 - 판정 유예(무발송·API 미호출·감시 유지)",
      _vs_row("KRW-VSF") is not None and _vs_row("KRW-VSF")["alerted"] == 0
      and len(sent_messages) == sent_before_vs + 3 and "KRW-VSF" not in _vs_calls)

# VS10: 시간 만료(72h)는 밴드와 무관하게 그대로 - 레거시 행도 prune 으로 정리
with db.connect(_VS_DB) as conn:
    conn.execute("INSERT INTO volume_watch (ticker, coin_symbol, added_at) "
                 "VALUES ('KRW-VSO', 'VSO', ?)", (_vs_now - 73 * 3600,))
    conn.commit()
_vs_vol["val"] = None
_vs_run({})
check("VS10 72h 시간 만료 유지(레거시 행 prune)", _vs_row("KRW-VSO") is None)

# avg_20h=0(상장 직후 무거래)·조회 None 도 조용히 skip — 예외 없이 통과하면 성공
_vs_vol["val"] = {"last_60m": 1e8, "avg_20h": 0.0}
_vs_run({"KRW-VSF": 1000.0})
check("VS11 avg=0/조회 None 가드 - 예외 없이 skip", _vs_row("KRW-VSF")["alerted"] == 0)

# VS12: 감사 major 재현 시나리오 — 아래쪽 클러스터 재터치 직후 같은 회차 판정.
# 첫 터치 밴드 [9000,12000] 활성 감시 중 8,500원에서 새 클러스터 터치(밴드
# [7650,9350])가 등록되면, 합집합 [7650,12000] 덕에 같은 회차의 밴드 판정에서
# 삭제되지 않고 급증 판정까지 진행돼야 한다 (수정 전: 옛 밴드 유지 → 8500 <
# 9000 이탈 판정 → 방금 유효해진 감시가 통째로 삭제, 급증 알림 영구 소실)
with db.connect(_VS_DB) as conn:
    db.add_volume_watch(conn, "KRW-VSR", "VSR", _vs_now,
                        band_low_krw=9000.0, band_high_krw=12000.0,
                        tp1_krw=11500.0, tp_count=3)
    db.add_volume_watch(conn, "KRW-VSR", "VSR", _vs_now + 30,
                        band_low_krw=7650.0, band_high_krw=9350.0)
    conn.commit()
_vs_vol["val"] = {"last_60m": 8e8, "avg_20h": 1e8}    # 8.0x 급증
_vs_calls.clear()
_vs_run({"KRW-VSR": 8500.0})
check("VS12 재터치 합집합 밴드 - 같은 회차 삭제 없이 급증 알림 발송(감사 major 재현)",
      _vs_row("KRW-VSR") is not None and _vs_row("KRW-VSR")["alerted"] == 1
      and "KRW-VSR" in _vs_calls and "거래량 급증" in sent_messages[-1])
check("VS12b TP 스냅샷 - 재터치 합집합(과도기: tps_krw 없음→scalar 보존) TP1 폴백 렌더",
      "다음 TP:  11,500원 (1/3단계)" in sent_messages[-1])

# VS13: 다음 TP 동적 선정 (2026-07-31 2차) — tps_krw 스냅샷이 있으면 알림 시점
# 현재가 바로 위의 TP 를 (k/N단계)로 표시. cur 10,500 → 2단계 TP 11,000 선택
with db.connect(_VS_DB) as conn:
    db.add_volume_watch(conn, "KRW-VST", "VST", _vs_now,
                        band_low_krw=9000.0, band_high_krw=13000.0,
                        tp1_krw=10000.0, tp_count=3,
                        tps_krw="[10000.0, 11000.0, 13000.0]")
    conn.commit()
_vs_vol["val"] = {"last_60m": 9e8, "avg_20h": 1e8}
_vs_run({"KRW-VST": 10500.0})
check("VS13 다음 TP 동적 선정 - 현재가 위 2단계 TP 표시",
      _vs_row("KRW-VST")["alerted"] == 1
      and "다음 TP:  11,000원 (2/3단계)" in sent_messages[-1])

# VS14: 2인검토 B-1 — 감시창(72h)을 넘겼지만 아직 prune 전인 미발송 행에 재터치
# 가 오면 활성 분기(타이머 비갱신)가 아니라 전면 리셋이어야 한다. 안 그러면
# 같은 회차 말미 prune 이 방금 유효해진 감시를 삭제(무성 소실 잔존 변종).
with db.connect(_VS_DB) as conn:
    conn.execute("INSERT INTO volume_watch (ticker, coin_symbol, added_at, "
                 "band_low_krw, band_high_krw) VALUES ('KRW-VSS', 'VSS', ?, 1.0, 2.0)",
                 (_vs_now - 73 * 3600,))
    db.add_volume_watch(conn, "KRW-VSS", "VSS", _vs_now,
                        band_low_krw=900.0, band_high_krw=1200.0,
                        tp1_krw=1100.0, tp_count=1, tps_krw="[1100.0]",
                        max_age_sec=72 * 3600)
    conn.commit()
_vss = _vs_row("KRW-VSS")
check("VS14 만료행 재터치 - 전면 리셋(타이머·밴드·TP 교체, prune race 봉쇄)",
      _vss["added_at"] == _vs_now and _vss["band_low_krw"] == 900.0
      and _vss["band_high_krw"] == 1200.0 and _vss["tp1_krw"] == 1100.0)
# 창 안(신선) 활성 행은 max_age_sec 를 줘도 종전 합집합 의미론 유지
with db.connect(_VS_DB) as conn:
    db.add_volume_watch(conn, "KRW-VSS", "VSS", _vs_now + 60,
                        band_low_krw=800.0, band_high_krw=1000.0,
                        max_age_sec=72 * 3600)
    conn.commit()
_vss2 = _vs_row("KRW-VSS")
check("VS14b 신선 활성 행 - 합집합·타이머 유지 불변",
      _vss2["added_at"] == _vs_now and _vss2["band_low_krw"] == 800.0
      and _vss2["band_high_krw"] == 1200.0)

# VS15/VS15b: 하한 2억 상향 (2026-08-01 사용자 확정 — 첫날 실사례 ETHFI 02:04,
# 최근 1h 5,437만·평균 790만/h·6.9x 가 구 하한 5천만을 9% 차로 통과한 새벽
# 저유동 위양성) — 실사례 수치 차단 + 정확 경계(2억) 통과를 고정
with db.connect(_VS_DB) as conn:
    db.add_volume_watch(conn, "KRW-VSE2", "VSE2", _vs_now,
                        band_low_krw=90.0, band_high_krw=120.0)
    conn.commit()
_vs_vol["val"] = {"last_60m": 5.437e7, "avg_20h": 7.9e6}   # ETHFI 실측값
_vs_before15 = len(sent_messages)
_vs_run({"KRW-VSE2": 100.0})
check("VS15 ETHFI 위양성 실사례(0.54억, 6.9x) - 5억 하한이 차단",
      len(sent_messages) == _vs_before15 and _vs_row("KRW-VSE2")["alerted"] == 0)
_vs_vol["val"] = {"last_60m": 5e8, "avg_20h": 5e7}         # 10.0x, 정확히 5억
_vs_run({"KRW-VSE2": 100.0})
check("VS15b 하한 정확 경계(5억) - 발송(이상 경계)",
      len(sent_messages) == _vs_before15 + 1 and _vs_row("KRW-VSE2")["alerted"] == 1)

upbit.fetch_rvol_1h = lambda m, t: None   # 기본 스텁 복원
os.remove(_VS_DB)

# ── SA1~SA4: S9 통합감사 수정분 (2026-07-31 카드 확정 — M-2 게이트 / m-4 승계 /
# M-1 표시 대표 일치) — run_once 실물 경로 검증 ──────────────────────────────
_SA_DB = "cache/_test_s9audit.db"
if os.path.exists(_SA_DB):
    os.remove(_SA_DB)
_sa_ledger = _alert_ledger.ledger_path(_SA_DB)
if os.path.exists(_sa_ledger):
    os.remove(_sa_ledger)
db.init_db(_SA_DB)
_sa_prev_db = settings.SETTINGS["db_path"]
settings.SETTINGS["db_path"] = _SA_DB
_sa_now = time.time()


def _sa_level(coin, entry, key, tps, tp, followers=5000, touched=False):
    with db.connect(_SA_DB) as conn:
        lv = dict(coin_symbol=coin, ticker=f"KRW-{coin}", direction="long",
                  entry_usd=entry, sl_usd=entry * 0.94, tp_usd=tp, rr=1.5,
                  grade="B", score=60, author=f"SA_{key}",
                  author_followers=followers, author_hit_rate=None,
                  author_hit_count=None, author_whitelisted=False,
                  mcap_rank=50, mcap_tier_icon="🥇",
                  post_url=f"https://tv.com/{key}", post_age_minutes=10,
                  collected_at=_sa_now - 3600,
                  tps_usd=_json.dumps(tps) if tps is not None else None)
        lv["signal_key"] = db.make_signal_key(coin, entry, lv["author"], key)
        db.upsert_level(conn, lv)
        _id = conn.execute("SELECT id FROM levels WHERE signal_key=?",
                           (lv["signal_key"],)).fetchone()["id"]
        if touched:  # 본알림 없이 터치 상태만 (억제됐던 신호 재현)
            conn.execute("UPDATE levels SET status='touched', touched_at=?, "
                         "touch_price_krw=? WHERE id=?",
                         (_sa_now - 1800, entry * USDT_KRW, _id))
        return _id


# SA1 (M-2): 본알림이 발송된 적 없는 터치 신호의 TP 적중 — 알림 0건, 인덱스는 전진
_sa1 = _sa_level("SAG", 100.0, "sa_gate", [103.0, 108.0], 103.0, touched=True)
fake["price"] = 104.0 * USDT_KRW
fake["candles"] = [(_sa_now - 1801, _sa_now - 10, 104.0 * USDT_KRW, 98.0 * USDT_KRW)]
fake["high"] = fake["low"] = None
_sa_before = len(sent_messages)
price_check.run_once(_sa_now)
with db.connect(_SA_DB) as conn:
    _sa1_idx = conn.execute("SELECT tp_alert_idx FROM levels WHERE id=?",
                            (_sa1,)).fetchone()["tp_alert_idx"]
check("SA1 M-2 게이트 - 본알림 무발송 신호의 TP 적중은 무알림·인덱스만 전진",
      len(sent_messages) == _sa_before and _sa1_idx == 1)

# SA2 (m-4 승계 + M-1 표시 일치): 대표(고팔로워, last TP 1.5% 미달)가 B안에 걸리고
# 형제(저팔로워, last TP 3.2%)가 통과 → 형제 승계 발송 + 알림 작성자 = 형제.
# 팔로워 배점 차(10 vs 1)가 목표거리 차(-6 vs -1)를 눌러 대표 선정이 결정적.
_sa2a = _sa_level("SAH", 200.0, "sa_short", [203.0], 203.0, followers=100000)
_sa2b = _sa_level("SAH", 200.6, "sa_swing", [207.0, 216.0], 207.0, followers=100)
fake["price"] = 200.0 * USDT_KRW * 1.001
fake["low"] = 199.0 * USDT_KRW
fake["candles"] = None
_sa_before = len(sent_messages)
s_sa2 = price_check.run_once(_sa_now + 120)
check("SA2 m-4 형제 승계 - 대표 스윙 미달이어도 통과 형제로 발송",
      s_sa2["touches"] == 1 and len(sent_messages) == _sa_before + 1
      and "진입가 터치" in sent_messages[-1])
check("SA2b M-1 표시 대표 = 승계 대표 (작성자·수집점수 역전에도 일치)",
      "SA_sa_swing" in sent_messages[-1] and "SA_sa_short" not in sent_messages[-1])
with db.connect(_SA_DB) as conn:
    _sa2_vw = conn.execute("SELECT band_high_krw FROM volume_watch "
                           "WHERE ticker='KRW-SAH'").fetchone()
check("SA2c 감시 밴드도 승계 대표 기준 (마지막 TP 216)",
      _sa2_vw is not None and abs(_sa2_vw["band_high_krw"] - 216.0 * USDT_KRW) < 1e-6)

# SA4 (M-2 양성 경로): SA2 에서 본알림 나간 SAH 의 TP1(207) 적중 → TP 알림 정상
# 발송. (SA3 보다 먼저 — 픽스처 가격이 전 마켓 공유라 순서를 바꾸면 SAH TP 가
# 다른 케이스 회차에 섞여 터진다)
fake["price"] = 208.0 * USDT_KRW
fake["candles"] = [(_sa_now + 121, _sa_now + 250, 208.0 * USDT_KRW, 200.0 * USDT_KRW)]
fake["low"] = None
_sa_before = len(sent_messages)
price_check.run_once(_sa_now + 240)
check("SA4 M-2 양성 - 본알림 나간 신호의 TP 알림은 정상 발송",
      len(sent_messages) > _sa_before
      and any("TP" in m for m in sent_messages[_sa_before:]))

# SA3 (m-4): 클러스터 전원이 스윙 미달이면 종전대로 억제 + 카운터.
# 엔트리를 저가대(50)로 잡아 위 SAH 잔여 레벨(TP2 216)이 이 회차 가격에서
# 조용히 miss 종결만 되고 알림이 섞이지 않게 한다.
_sa3a = _sa_level("SAI", 50.0, "sa_all1", [50.7], 50.7)
_sa3b = _sa_level("SAI", 50.15, "sa_all2", [50.9], 50.9)
fake["price"] = 50.0 * USDT_KRW * 1.001
fake["low"] = 49.7 * USDT_KRW
fake["candles"] = None
_sa_before = len(sent_messages)
s_sa3 = price_check.run_once(_sa_now + 360)
check("SA3 클러스터 전원 미달 - 억제 유지", len(sent_messages) == _sa_before
      and s_sa3["suppressed"] >= 1)

fake["price"] = fake["candles"] = fake["high"] = fake["low"] = None
settings.SETTINGS["db_path"] = _sa_prev_db
os.remove(_SA_DB)
if os.path.exists(_sa_ledger):
    os.remove(_sa_ledger)

# ── SB1~SB4: 2026-08-01 재검토 후속 (R-1 원장 폴백 / R-2 승계 TP 게이트 /
# M-1 역전 픽스처 / B-2 감시 티커 시세 공급) ─────────────────────────────
_SB_DB = "cache/_test_sb_rereview.db"
if os.path.exists(_SB_DB):
    os.remove(_SB_DB)
_sb_ledger = _alert_ledger.ledger_path(_SB_DB)
if os.path.exists(_sb_ledger):
    os.remove(_sb_ledger)
db.init_db(_SB_DB)
_sb_prev_db = settings.SETTINGS["db_path"]
settings.SETTINGS["db_path"] = _SB_DB
_sb_now = time.time()


def _sb_level(coin, entry, key, tps, tp, followers=5000, grade="B", score=60,
              touched=False):
    with db.connect(_SB_DB) as conn:
        lv = dict(coin_symbol=coin, ticker=f"KRW-{coin}", direction="long",
                  entry_usd=entry, sl_usd=entry * 0.94, tp_usd=tp, rr=1.5,
                  grade=grade, score=score, author=f"SB_{key}",
                  author_followers=followers, author_hit_rate=None,
                  author_hit_count=None, author_whitelisted=False,
                  mcap_rank=50, mcap_tier_icon="🥇",
                  post_url=f"https://tv.com/{key}", post_age_minutes=10,
                  collected_at=_sb_now - 3600,
                  tps_usd=_json.dumps(tps) if tps is not None else None)
        lv["signal_key"] = db.make_signal_key(coin, entry, lv["author"], key)
        db.upsert_level(conn, lv)
        _id = conn.execute("SELECT id FROM levels WHERE signal_key=?",
                           (lv["signal_key"],)).fetchone()["id"]
        if touched:
            conn.execute("UPDATE levels SET status='touched', touched_at=?, "
                         "touch_price_krw=? WHERE id=?",
                         (_sb_now - 1800, entry * USDT_KRW, _id))
        return _id


# SB1 (R-1): alerts_log 유실(커밋백 경합 재현 — DB 에 touch 행 없음) 시에도
# NDJSON 원장의 터치 기록이 폴백으로 잡혀 TP 알림이 나간다
_sb1 = _sb_level("SBA", 100.0, "sb_ledger", [103.0, 108.0], 103.0, touched=True)
_alert_ledger.append(_SB_DB, "SBA", "touch", [_sb1], _sb_now - 1800)
fake["price"] = 104.0 * USDT_KRW
fake["candles"] = [(_sb_now - 1801, _sb_now - 10, 104.0 * USDT_KRW, 98.0 * USDT_KRW)]
fake["high"] = fake["low"] = None
_sb_before = len(sent_messages)
price_check.run_once(_sb_now)
check("SB1 R-1 원장 폴백 - DB 유실이어도 원장 터치 기록으로 TP 알림 발송",
      len(sent_messages) == _sb_before + 1 and "TP1" in sent_messages[-1])
with db.connect(_SB_DB) as conn:   # 잔여 TP2 가 이후 케이스에 섞이지 않게 종결
    conn.execute("UPDATE levels SET outcome='hit' WHERE id=?", (_sb1,))
    conn.commit()

# SB2 (R-2): 승계 발송 클러스터의 초단타 형제 — 터치 기록(ids 포함)이 있어도
# 자신이 스윙 미달이면 TP 알림 차단(판정·인덱스 전진은 유지)
_sb2a = _sb_level("SBH", 400.0, "sb_scalp", [402.0, 404.0], 402.0, followers=100000)
_sb2b = _sb_level("SBH", 401.5, "sb_swing", [423.0], 423.0, followers=100)  # 최종 TP 5.35% (5% 필터 통과)
fake["price"] = 400.2 * USDT_KRW
fake["low"] = None
# _fake_range 기본 캔들은 모듈 now 기반 타임스탬프라 _sb_now 기반 collected_at 보다
# 이전 → _eff_low 가 필터링해 low 무시. 명시적 캔들로 양 레벨 실도달 보장.
fake["candles"] = [(_sb_now - 300, _sb_now + 60,
                    400.5 * USDT_KRW, 399.0 * USDT_KRW)]
_sb_before = len(sent_messages)
s_sb2 = price_check.run_once(_sb_now + 120)
check("SB2 사전조건 - 초단타 대표는 승계, 스윙 형제 명의로 본알림 1건",
      s_sb2["touches"] == 1 and len(sent_messages) == _sb_before + 1
      and "SB_sb_swing" in sent_messages[-1])
fake["price"] = 405.0 * USDT_KRW
fake["candles"] = [(_sb_now + 121, _sb_now + 250, 405.0 * USDT_KRW, 400.0 * USDT_KRW)]
fake["low"] = None
_sb_before = len(sent_messages)
price_check.run_once(_sb_now + 360)
with db.connect(_SB_DB) as conn:
    _sb2_idx = conn.execute("SELECT tp_alert_idx FROM levels WHERE id=?",
                            (_sb2a,)).fetchone()["tp_alert_idx"]
check("SB2b R-2 스윙 게이트 - 초단타 형제의 TP 적중은 무알림·인덱스만 전진",
      len(sent_messages) == _sb_before and _sb2_idx == 1)

# SB3 (M-1 역전 픽스처): 형제의 수집 등급이 D(30점, stale)여도 재채점으로 C 가
# 되면 승계된다 — 전 멤버 재채점(M-1)을 rep 단독으로 되돌리면 이 케이스가 잡는다
_sb3a = _sb_level("SBI", 500.0, "sb_short2", [507.5], 507.5, followers=100000)
_sb3b = _sb_level("SBI", 501.0, "sb_stale", [530.0], 530.0, followers=100,  # 최종 TP 5.79%
                  grade="D", score=30)
fake["price"] = 500.2 * USDT_KRW
fake["low"] = 499.0 * USDT_KRW
fake["candles"] = None
_sb_before = len(sent_messages)
s_sb3 = price_check.run_once(_sb_now + 480)
check("SB3 M-1 전멤버 재채점 - 수집 D 형제가 재채점 C 로 승계 발송",
      s_sb3["touches"] == 1 and len(sent_messages) == _sb_before + 1
      and "SB_sb_stale" in sent_messages[-1])

# SB4 (B-2): 감시 전용 티커(활성 레벨 없음)가 시세 조회 markets 에 합류하는지 —
# 빠지면 밴드 판정이 '현재가 미보유 유예'로 상시 표류한다
with db.connect(_SB_DB) as conn:
    db.add_volume_watch(conn, "KRW-SBW", "SBW", _sb_now + 500,
                        band_low_krw=100.0, band_high_krw=200.0)
    conn.commit()
_sb_markets = {}
_orig_sb_fp = upbit.fetch_prices


def _cap_fp(markets, timeout):
    _sb_markets["m"] = list(markets)
    return _orig_sb_fp(markets, timeout)


upbit.fetch_prices = _cap_fp
price_check.run_once(_sb_now + 600)
upbit.fetch_prices = _orig_sb_fp
check("SB4 B-2 - 감시 전용 티커가 시세 조회 markets 에 포함",
      "KRW-SBW" in _sb_markets.get("m", []))

fake["price"] = fake["candles"] = fake["high"] = fake["low"] = None
settings.SETTINGS["db_path"] = _sb_prev_db
os.remove(_SB_DB)
if os.path.exists(_sb_ledger):
    os.remove(_sb_ledger)

# ── BN1~BN3: binance 김프 시세 451 폴백 (2026-08-01 m-7 수리 — Actions 미국
# 러너에서 api.binance.com 이 451 지역차단, 종전 코드는 로그 없이 None) ────────
# 하네스가 파일 초입(라인 111)에서 fetch_usdt_price 를 가짜로 바꿔두므로,
# 실물 로직 검증을 위해 reload 로 원본을 복원한다(이후 run_once 호출 없음).
import importlib  # noqa: E402

from monitor import binance as _binance  # noqa: E402

importlib.reload(_binance)


class _BnResp:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload


def _bn_get_factory(by_host):
    calls = []

    def _get(url, params=None, timeout=None, **_kw):
        # **_kw (2026-08-08): CoinGecko 경로가 headers= 를 추가로 넘긴다 —
        # 나머지 경로와 시그니처를 공유하기 위해 무시하고 받기만 한다.
        calls.append(url)
        for host, resp in by_host.items():
            if host in url:
                return resp
        raise AssertionError(f"예상 밖 URL: {url}")
    _get.calls = calls
    return _get


# BN1: 주 경로 451(미국 차단) → data-api.binance.vision 폴백으로 가격 획득
_requests_mod.get = _bn_get_factory({
    "api.binance.com": _BnResp(451),
    "data-api.binance.vision": _BnResp(200, {"price": "63000.5"}),
})
check("BN1 451 지역차단 - vision 미러 폴백으로 가격 획득",
      _binance.fetch_usdt_price("BTC", 5.0) == 63000.5)

# BN2: 400(Invalid symbol = Binance 미상장) → Binance 미러 생략, Bybit 시도도 미상장 → None
_bn2 = _bn_get_factory({
    "api.binance.com": _BnResp(400),
    "data-api.binance.vision": _BnResp(200, {"price": "1"}),
    "api.bybit.com": _BnResp(200, {"result": {"list": []}}),
})
_requests_mod.get = _bn2
check("BN2 미상장(400) - Binance 미러 생략·Bybit도 미상장 → None",
      _binance.fetch_usdt_price("NOPE", 5.0) is None
      and not any("binance.vision" in c for c in _bn2.calls))


# BN3: 전 경로 실패(451+예외)도 예외 없이 None (김프 줄만 생략, 발송은 계속)
def _bn3_get(url, params=None, timeout=None):
    if "api.binance.com" in url:
        return _BnResp(451)
    raise RuntimeError("network down")


_requests_mod.get = _bn3_get
check("BN3 전 경로 실패 - 예외 없이 None", _binance.fetch_usdt_price("BTC", 5.0) is None)

# BN4: Binance 전면 차단(451) + Bybit 폴백 성공 → 가격 획득
_requests_mod.get = _bn_get_factory({
    "api.binance.com": _BnResp(451),
    "data-api.binance.vision": _BnResp(451),
    "api.bybit.com": _BnResp(200, {"result": {"list": [{"lastPrice": "62500.0"}]}}),
})
check("BN4 Binance 전면 차단 - Bybit 폴백 성공",
      _binance.fetch_usdt_price("BTC", 5.0) == 62500.0)
_requests_mod.get = _orig_requests_get

# ── FR1~FR3: 펀딩 레짐 전환 감지 (2026-08-03 스프린트08) ───────────────
# 30일 지속 음수 후 최근 양수 플립이면 flipped=True, 그 외 None.
# 히스토리 없음/부족은 None (안전).
from monitor.binance import detect_funding_regime_flip as _reg
# 91개(=30일*3+1) 미만 → None
check("FR1 히스토리 부족 → None", _reg([-0.01]*30) is None)
# 30일치 전부 음수 + 최근 양수 1개 = flipped=True
_hist_flip = [-0.02] * (30 * 3) + [0.0015]
_r = _reg(_hist_flip, min_neg_days=30)
check("FR2 30일 음수 후 양수 플립 → flipped=True", _r is not None and _r["flipped"] is True
      and abs(_r["neg_days"] - 30.0) < 0.34)
# 최근 값이 음수면 플립 아님
check("FR2b 최근값 음수 → None", _reg([-0.02]*(30*3) + [-0.001]) is None)
# 30일 창 안에 양수 하나라도 있으면 플립 아님
_hist_partial = [-0.02]*(30*3 - 5) + [0.005] + [-0.02]*4 + [0.001]
check("FR2c 30일 창 내 양수 있으면 → None (스트릭 미충족)",
      _reg(_hist_partial) is None)
# min_neg_days 커스텀
check("FR3 min_neg_days=14 로 낮추면 통과",
      _reg([-0.02]*(14*3) + [0.0015], min_neg_days=14) is not None)
# 전부 0(무편향)인 창 + 미세 양수 latest → False positive 방지 (2026-08-04 R2 감사)
check("FR4 전부 0 창은 하락 편향 아님 → None",
      _reg([0.0]*(30*3) + [0.001], min_neg_days=30) is None)

# ── OI1~OI5 / CG1~CG2: 2026-08-08 재검토 커버리지 공백 메우기 ─────────────
# (교차감사 발견: OI 스냅샷 게이트·get_oi_baseline 경계·CoinGecko 캐시 히트·
#  record_touch_verdicts 의 ma200_above 파라미터가 전부 무테스트였음)

# get_oi_baseline 경계값 — 18h/30h 창 정확히 걸치는 지점 + 창 밖은 None
_OI_DB = "cache/_test_oi_baseline.db"
if os.path.exists(_OI_DB):
    os.remove(_OI_DB)
db.init_db(_OI_DB)
_oi_now = now
with db.connect(_OI_DB) as conn:
    db.record_oi_snapshots(conn, [("OIC", 1000.0)], _oi_now - 24 * 3600)   # 정확히 24h 전
    db.record_oi_snapshots(conn, [("OIC", 1100.0)], _oi_now - 18 * 3600)   # 창 상단 경계(포함)
    db.record_oi_snapshots(conn, [("OIC", 900.0)], _oi_now - 30 * 3600)    # 창 하단 경계(포함)
    db.record_oi_snapshots(conn, [("OIC", 500.0)], _oi_now - 31 * 3600)    # 창 밖(30h 초과)
    db.record_oi_snapshots(conn, [("OIC", 1200.0)], _oi_now - 17 * 3600)   # 창 밖(18h 미만)
    _base_exact = db.get_oi_baseline(conn, "OIC", _oi_now)
    _base_none = db.get_oi_baseline(conn, "OINONE", _oi_now)
check("OI1 get_oi_baseline: 24h 정확히 일치하는 스냅샷을 최우선 선택",
      _base_exact == 1000.0)
check("OI2 get_oi_baseline: 스냅샷 자체가 없는 코인은 None",
      _base_none is None)
with db.connect(_OI_DB) as conn:
    conn.execute("DELETE FROM oi_history")
    db.record_oi_snapshots(conn, [("OIC2", 700.0)], _oi_now - 18 * 3600)   # 경계 포함 확인
    _base_edge_hi = db.get_oi_baseline(conn, "OIC2", _oi_now)
    conn.execute("DELETE FROM oi_history")
    db.record_oi_snapshots(conn, [("OIC3", 800.0)], _oi_now - 30 * 3600)   # 경계 포함 확인
    _base_edge_lo = db.get_oi_baseline(conn, "OIC3", _oi_now)
check("OI3 창 경계(정확히 18h/30h)는 포함(BETWEEN 양끝단)",
      _base_edge_hi == 700.0 and _base_edge_lo == 800.0)
if os.path.exists(_OI_DB):
    os.remove(_OI_DB)

# record_oi_snapshots 프룬 — 48h 보존기간 밖은 삭제
_OI_DB2 = "cache/_test_oi_prune.db"
if os.path.exists(_OI_DB2):
    os.remove(_OI_DB2)
db.init_db(_OI_DB2)
with db.connect(_OI_DB2) as conn:
    db.record_oi_snapshots(conn, [("OIP", 1.0)], now - 50 * 3600)  # 48h 보존 밖 → 프룬 대상
    db.record_oi_snapshots(conn, [("OIP", 2.0)], now)  # 이 호출이 프룬을 트리거
    _remaining = conn.execute("SELECT COUNT(*) AS n FROM oi_history").fetchone()["n"]
check("OI4 48h 보존기간 밖 스냅샷은 다음 적재 시 프룬", _remaining == 1)
if os.path.exists(_OI_DB2):
    os.remove(_OI_DB2)

# _snapshot_oi 60분 게이트 — 재호출 시 실제 적재 스킵 + 실패 시 게이트 미갱신(재시도)
_OI_DB3 = "cache/_test_oi_gate.db"
if os.path.exists(_OI_DB3):
    os.remove(_OI_DB3)
db.init_db(_OI_DB3)
with db.connect(_OI_DB3) as conn:
    conn.execute(
        "INSERT INTO levels (signal_key, coin_symbol, ticker, direction, entry_usd, status, collected_at) "
        "VALUES ('oig-1','OIG','KRW-OIG','long',1.0,'watching',?)", (now,))
    conn.commit()
_binance._cg_funding_cache.update(ts=0.0, map=None)
_requests_mod.get = _bn_get_factory({
    "coingecko.com": _BnResp(200, {"tickers": [
        {"symbol": "OIGUSDT", "open_interest_usd": 12345.0}]}),
})
with db.connect(_OI_DB3) as conn:
    price_check._snapshot_oi(conn, now)  # 최초 호출 — 적재 + 게이트 기록
    _n1 = conn.execute("SELECT COUNT(*) AS n FROM oi_history").fetchone()["n"]
    price_check._snapshot_oi(conn, now + 600)  # 10분 후 재호출 — 게이트 안에 있어 스킵
    _n2 = conn.execute("SELECT COUNT(*) AS n FROM oi_history").fetchone()["n"]
check("OI5a 60분 게이트 - 재호출이 게이트 안이면 추가 적재 없음", _n1 == 1 and _n2 == 1)
# CoinGecko 맵 자체가 실패(None)하면 게이트를 갱신하지 않아 다음 회차 재시도돼야 한다
_binance._cg_funding_cache.update(ts=0.0, map=None)
_requests_mod.get = _bn_get_factory({"coingecko.com": _BnResp(500)})
with db.connect(_OI_DB3) as conn:
    db.set_meta(conn, price_check._META_LAST_OI_SNAP, str(now + 601))  # 게이트 강제 통과 위치로
    price_check._snapshot_oi(conn, now + 4000)  # 게이트 밖 + 맵 실패
    _gate_after_fail = db.get_meta(conn, price_check._META_LAST_OI_SNAP)
check("OI5b CoinGecko 맵 실패 시 게이트 미갱신(다음 회차 재시도 보장)",
      _gate_after_fail == str(now + 601))
if os.path.exists(_OI_DB3):
    os.remove(_OI_DB3)

# CoinGecko TTL 캐시 — TTL 이내 재호출은 실제 HTTP 콜 없이 캐시 반환
_binance._cg_funding_cache.update(ts=0.0, map=None)
_cg_calls = _bn_get_factory({
    "coingecko.com": _BnResp(200, {"tickers": [
        {"symbol": "CGCUSDT", "funding_rate": 0.01, "open_interest_usd": 999.0}]}),
})
_requests_mod.get = _cg_calls
_m1 = _binance._coingecko_binance_funding_map(5.0)
_calls_after_first = len(_cg_calls.calls)
_m2 = _binance._coingecko_binance_funding_map(5.0)  # TTL(180s) 이내 — 캐시 히트
_calls_after_second = len(_cg_calls.calls)
check("CG1 TTL 캐시 - TTL 이내 재호출은 실제 HTTP 콜 없이 동일 데이터 반환",
      _calls_after_first == 1 and _calls_after_second == 1 and _m1 == _m2)
# TTL 만료 후에는 재호출되어야 한다(캐시 타임스탬프를 과거로 되돌려 시뮬레이션)
_binance._cg_funding_cache["ts"] -= (_binance._CG_FUNDING_TTL_SEC + 1)
_m3 = _binance._coingecko_binance_funding_map(5.0)
check("CG2 TTL 만료 후 재호출 - 새 HTTP 콜 발생", len(_cg_calls.calls) == 2 and _m3 is not None)
_binance._cg_funding_cache.update(ts=0.0, map=None)  # 다음 테스트 오염 방지

# record_touch_verdicts 의 ma200_above 파라미터 — VR1/VR2 는 이 인자 없이 호출했었다
_MA200_DB = "cache/_test_ma200_verdict.db"
if os.path.exists(_MA200_DB):
    os.remove(_MA200_DB)
db.init_db(_MA200_DB)
with db.connect(_MA200_DB) as conn:
    conn.execute(
        "INSERT INTO levels (signal_key, coin_symbol, ticker, direction, entry_usd, status, collected_at) "
        "VALUES ('m200-1','M2C','KRW-M2C','long',1.0,'touched',?)", (now,))
    _m200_id = conn.execute("SELECT last_insert_rowid() AS i").fetchone()[0]
    db.record_touch_verdicts(conn, [_m200_id], None, None, ma200_above=1)
    _m200_val = conn.execute(
        "SELECT touch_ma200_above FROM levels WHERE id=?", (_m200_id,)).fetchone()[0]
    # 최초 기록 우선 — 재기록 시도(0)는 무시되고 1 그대로 보존
    db.record_touch_verdicts(conn, [_m200_id], None, None, ma200_above=0)
    _m200_after_retry = conn.execute(
        "SELECT touch_ma200_above FROM levels WHERE id=?", (_m200_id,)).fetchone()[0]
check("MA200-1 record_touch_verdicts ma200_above 기록 + 최초값 보존",
      _m200_val == 1 and _m200_after_retry == 1)
if os.path.exists(_MA200_DB):
    os.remove(_MA200_DB)

# ── OS1~OS7: OI 급증 알림 (2026-08-08 사용자 결정) ─────────────────────────
# 진입가 터치와 무관한 별도 알림 — _snapshot_oi 의 시간당 사이클에 얹혀 직전
# 스냅샷 대비 변화율을 판정한다. 거래량 급증 알림과 같은 격식, 코인당 쿨다운.
_OS_DB = "cache/_test_oi_spike.db"
if os.path.exists(_OS_DB):
    os.remove(_OS_DB)
db.init_db(_OS_DB)
with db.connect(_OS_DB) as conn:
    conn.execute(
        "INSERT INTO levels (signal_key, coin_symbol, ticker, direction, "
        "entry_usd, status, collected_at) VALUES "
        "('os-1','OSC','KRW-OSC','long',1.0,'watching',?)", (now,))
    conn.commit()


def _os_map(oi_value):
    return {"tickers": [{"symbol": "OSCUSDT", "funding_rate": 0.0,
                         "open_interest_usd": oi_value}]}


_os_prices = {"KRW-OSC": 5000.0}

# OS1: 직전 스냅샷(1h 전, 1000) → 현재 1200 = +20% (임계 15% 초과) → 발동
_binance._cg_funding_cache.update(ts=0.0, map=None)
_requests_mod.get = _bn_get_factory({"coingecko.com": _BnResp(200, _os_map(1_200_000.0))})
with db.connect(_OS_DB) as conn:
    db.record_oi_snapshots(conn, [("OSC", 1_000_000.0)], now - 3600)
    sent_messages.clear()
    price_check._snapshot_oi(conn, now, settings.get, _os_prices)
check("OS1 직전 대비 +20% 급증 → 알림 발동 + 렌더 포맷(단위·부호)",
      len(sent_messages) == 1 and "[OI 급증]" in sent_messages[0]
      and "+20.0%" in sent_messages[0] and "$1.2M" in sent_messages[0]
      and "$1.0M" in sent_messages[0] and "5,000원" in sent_messages[0])

# OS2: 쿨다운(기본 6h) 내 재급증 → 재발동 안 함
_binance._cg_funding_cache.update(ts=0.0, map=None)
_requests_mod.get = _bn_get_factory({"coingecko.com": _BnResp(200, _os_map(1_500_000.0))})
with db.connect(_OS_DB) as conn:
    db.set_meta(conn, price_check._META_LAST_OI_SNAP, str(now - 4000))  # 게이트 통과
    db.record_oi_snapshots(conn, [("OSC", 1_200_000.0)], now - 100)  # 새 '직전' 값
    sent_messages.clear()
    price_check._snapshot_oi(conn, now + 100, settings.get, _os_prices)
check("OS2 쿨다운 내 재급증 - 재발동 안 함", len(sent_messages) == 0)

# OS2b (2026-08-08 재검토): 쿨다운 정확히 경계(now-last_alert == cooldown_sec)
# — 코드는 `< cooldown_sec` 로 스킵 판정하므로 경계값 자체는 스킵 없이
# 발동해야 한다(< 이 아니라 <= 였다면 이 케이스가 막혔을 것).
_OS_DB3 = "cache/_test_oi_spike_boundary.db"
if os.path.exists(_OS_DB3):
    os.remove(_OS_DB3)
db.init_db(_OS_DB3)
with db.connect(_OS_DB3) as conn:
    conn.execute(
        "INSERT INTO levels (signal_key, coin_symbol, ticker, direction, "
        "entry_usd, status, collected_at) VALUES "
        "('os-b','OSB','KRW-OSB','long',1.0,'watching',?)", (now,))
    db.record_oi_spike_alert(conn, "OSB", now)  # 마지막 발동 = now
    conn.commit()
_binance._cg_funding_cache.update(ts=0.0, map=None)
_requests_mod.get = _bn_get_factory(
    {"coingecko.com": _BnResp(200, {"tickers": [
        {"symbol": "OSBUSDT", "funding_rate": 0.0, "open_interest_usd": 2_000_000.0}]})})
_os_cd = settings.get("oi_spike_cooldown_hours") * 3600
with db.connect(_OS_DB3) as conn:
    _tb = now + _os_cd  # 정확히 경계
    db.set_meta(conn, price_check._META_LAST_OI_SNAP, str(_tb - 3700))
    db.record_oi_snapshots(conn, [("OSB", 1_000_000.0)], _tb - 100)
    sent_messages.clear()
    price_check._snapshot_oi(conn, _tb, settings.get, {"KRW-OSB": 5000.0})
check("OS2b 쿨다운 정확히 경계(==) - 스킵 아니고 발동", len(sent_messages) == 1)
if os.path.exists(_OS_DB3):
    os.remove(_OS_DB3)

# OS3: 쿨다운 경과 후 재급증 → 다시 발동
_binance._cg_funding_cache.update(ts=0.0, map=None)
_requests_mod.get = _bn_get_factory({"coingecko.com": _BnResp(200, _os_map(2_000_000.0))})
_os_cooldown = settings.get("oi_spike_cooldown_hours") * 3600
with db.connect(_OS_DB) as conn:
    _t2 = now + 100 + _os_cooldown + 3700
    db.set_meta(conn, price_check._META_LAST_OI_SNAP, str(_t2 - 3700))
    db.record_oi_snapshots(conn, [("OSC", 1_500_000.0)], _t2 - 100)
    sent_messages.clear()
    price_check._snapshot_oi(conn, _t2, settings.get, _os_prices)
check("OS3 쿨다운 경과 후 재급증 - 다시 발동", len(sent_messages) == 1)

# OS4: 임계 미만 변화 → 발동 안 함
_binance._cg_funding_cache.update(ts=0.0, map=None)
_requests_mod.get = _bn_get_factory({"coingecko.com": _BnResp(200, _os_map(1_050_000.0))})
with db.connect(_OS_DB) as conn:
    _t3 = now + 900000
    db.set_meta(conn, price_check._META_LAST_OI_SNAP, str(_t3 - 3700))
    db.record_oi_snapshots(conn, [("OSC", 1_000_000.0)], _t3 - 100)
    sent_messages.clear()
    price_check._snapshot_oi(conn, _t3, settings.get, _os_prices)
check("OS4 임계(15%) 미만 변화 - 발동 안 함", len(sent_messages) == 0)

# OS5: 직전 스냅샷 없음(신규 감시 코인 첫 적재) → 비교 기준 없어 판정 보류,
# 그래도 이번 값은 정상 적재(다음 회차 비교용 기준이 된다)
_binance._cg_funding_cache.update(ts=0.0, map=None)
_requests_mod.get = _bn_get_factory({"coingecko.com": _BnResp(200, _os_map(999_000.0))})
_OS_DB2 = "cache/_test_oi_spike_fresh.db"
if os.path.exists(_OS_DB2):
    os.remove(_OS_DB2)
db.init_db(_OS_DB2)
with db.connect(_OS_DB2) as conn:
    conn.execute(
        "INSERT INTO levels (signal_key, coin_symbol, ticker, direction, "
        "entry_usd, status, collected_at) VALUES "
        "('os-2','OSC','KRW-OSC','long',1.0,'watching',?)", (now,))
    conn.commit()
    sent_messages.clear()
    price_check._snapshot_oi(conn, now, settings.get, _os_prices)
    _os5_n = conn.execute("SELECT COUNT(*) AS n FROM oi_history").fetchone()["n"]
check("OS5 직전 스냅샷 없음(첫 적재) - 알림 보류하되 값은 적재",
      len(sent_messages) == 0 and _os5_n == 1)
if os.path.exists(_OS_DB2):
    os.remove(_OS_DB2)

# OS6: oi_spike_enabled=False → 임계 초과해도 무발동(적재는 계속)
_saved_oi_spike = settings.SETTINGS["oi_spike_enabled"]
settings.SETTINGS["oi_spike_enabled"] = False
_binance._cg_funding_cache.update(ts=0.0, map=None)
_requests_mod.get = _bn_get_factory({"coingecko.com": _BnResp(200, _os_map(3_000_000.0))})
with db.connect(_OS_DB) as conn:
    _t4 = now + 2000000
    db.set_meta(conn, price_check._META_LAST_OI_SNAP, str(_t4 - 3700))
    db.record_oi_snapshots(conn, [("OSC", 2_000_000.0)], _t4 - 100)
    sent_messages.clear()
    price_check._snapshot_oi(conn, _t4, settings.get, _os_prices)
    _os6_n = conn.execute("SELECT COUNT(*) AS n FROM oi_history WHERE ts=?", (_t4,)).fetchone()["n"]
check("OS6 설정 OFF - 급증해도 무발동하되 적재는 계속",
      len(sent_messages) == 0 and _os6_n == 1)
settings.SETTINGS["oi_spike_enabled"] = _saved_oi_spike

# OS7: prices 미전달(구 호출부 호환) → 급증 검사 자체를 건너뜀(예외 없이)
_binance._cg_funding_cache.update(ts=0.0, map=None)
_requests_mod.get = _bn_get_factory({"coingecko.com": _BnResp(200, _os_map(5_000_000.0))})
with db.connect(_OS_DB) as conn:
    _t5 = now + 3000000
    db.set_meta(conn, price_check._META_LAST_OI_SNAP, str(_t5 - 3700))
    db.record_oi_snapshots(conn, [("OSC", 3_000_000.0)], _t5 - 100)
    sent_messages.clear()
    price_check._snapshot_oi(conn, _t5, settings.get, None)
check("OS7 prices 미전달 - 예외 없이 급증검사만 생략(적재는 정상)",
      len(sent_messages) == 0)

# OS8 (2026-08-08 재검토): _fmt_usd_notional 직접 검증 - M/B 단위 전환 +
# 반올림 경계(999,999,999.9 처럼 반올림하면 1000.0M 이 될 값은 B 로 승격).
from notify.telegram import _fmt_usd_notional as _fmt_usd
check("OS8 M 단위 (1e9 미만)", _fmt_usd(210_500_000.0) == "$210.5M")
check("OS8b B 단위 (1e9 이상)", _fmt_usd(4_830_000_000.0) == "$4.83B")
check("OS8c B 경계 정확히 1e9", _fmt_usd(1_000_000_000.0) == "$1.00B")
check("OS8d 반올림 경계 - 999,999,999.9 는 M 반올림 시 1000.0M 이 아니라 B 로",
      _fmt_usd(999_999_999.9) == "$1.00B")
check("OS8e 경계 바로 아래(999,949,999.0)는 여전히 M", _fmt_usd(999_949_999.0) == "$999.9M")

_binance._cg_funding_cache.update(ts=0.0, map=None)
if os.path.exists(_OS_DB):
    os.remove(_OS_DB)

# ── TR: 행별 자동 절삭 / 구분선 폭 (2026-08-08 그룹채팅 폭 축소 대응) ────────
# _display_width 는 East Asian Width 기반 표시폭 추정 - ━ 는 유니코드 공식
# 분류(Ambiguous=1)와 달리 텔레그램 실사용 폰트에서 2칸으로 렌더돼 특례 처리.
check("TR1 ━ 는 표시폭 2 (유니코드 Ambiguous 분류와 다른 실측 특례)",
      telegram._display_width("━") == 2)
check("TR2 한글은 표시폭 2 (East Asian Width W)", telegram._display_width("가") == 2)
check("TR3 영숫자는 표시폭 1", telegram._display_width("a") == 1)
check("TR4 _SEP 는 17자", len(telegram._SEP) == 17)

check("TR5 예산 이내 행은 그대로 통과",
      telegram._truncate_line("짧은 한 줄") == "짧은 한 줄")
_long_plain = "가" * 30  # 표시폭 60 > 예산 36
_cut_plain = telegram._truncate_line(_long_plain)
check("TR6 예산 초과 행은 절삭되고 원문 접두부와 일치",
      _long_plain.startswith(_cut_plain) and len(_cut_plain) < len(_long_plain))
check("TR7 절삭 시 말줄임표 등 표시 없음 - 접두부만 남고 덧붙는 문자 없음",
      "…" not in _cut_plain and "..." not in _cut_plain)

_long_html = "<b>" + "가" * 30 + "</b>"
_cut_html = telegram._truncate_line(_long_html)
check("TR8 여는 태그 중간에서 잘려도 HTML 이 열린 채로 남지 않는다",
      _cut_html.count("<b>") == _cut_html.count("</b>"))
check("TR9 _SEP 자체는 절삭 대상에서 제외(길이 그대로)",
      telegram._truncate_line(telegram._SEP) == telegram._SEP)

# TR10 (2026-08-08 최종 결정): "작성자:" 라벨 삭제 + 화이트리스트 ⭐⭐
# 표시 숨김(거추장스럽다 - 다른 지표로 판단) - 화이트리스트 여부와 무관하게
# ⭐ 는 어디에도 나오지 않는다.
_ab_star = telegram._author_block(dict(author="mastercrypto2020", author_whitelisted=True))
check("TR10 화이트리스트여도 ⭐ 표시 자체가 없다",
      _ab_star[0] == "✍️ @mastercrypto2020" and not any("⭐" in ln for ln in _ab_star))
_ab_nostar = telegram._author_block(dict(author="mastercrypto2020", author_whitelisted=False))
check("TR11 비화이트리스트도 동일하게 ⭐ 없음",
      not any("⭐" in ln for ln in _ab_nostar))

# ── SL: 별도알림 출처 링크 렌더러 (2026-08-08 사용자 결정) ──────────────────
_sl2 = telegram._source_line(["https://x.example/1", "https://x.example/2"])
check("SL1 두 출처는 · 로 이어 붙인다",
      '<a href="https://x.example/1">출처1</a>' in _sl2
      and '<a href="https://x.example/2">출처2</a>' in _sl2
      and " · " in _sl2)
_sl6 = telegram._source_line([f"https://x.example/{i}" for i in range(6)])
check("SL2 6건 이상이면 '외 N건' 을 덧붙이고 6번째부터는 생략",
      "외 1건" in _sl6 and "출처6" not in _sl6)

# ── TG: 타점/목표 행 포맷 (2026-08-08: 원-괄호 사이 공백 제거) ──────────────
check("TG1 목표 행 '원' 바로 뒤에 괄호 - 이중 공백 없음",
      "원(" in touch_msg and "원  (" not in touch_msg)

# ── PU: volume_watch.post_urls 합집합 병합 + 급증/부분익절 알림 출처 표기 ───
import json as _json_pu  # noqa: E402
_PU_DB = "cache/_test_post_urls.db"
if os.path.exists(_PU_DB):
    os.remove(_PU_DB)
db.init_db(_PU_DB)
with db.connect(_PU_DB) as conn:
    _pu_now = now
    db.add_volume_watch(conn, "KRW-PUC", "PUC", _pu_now,
                         post_urls=_json_pu.dumps(["https://a.example/1"]))
    db.add_volume_watch(conn, "KRW-PUC", "PUC", _pu_now + 30,
                         post_urls=_json_pu.dumps(["https://a.example/2"]))
    _pu_row = db.get_volume_watch_active(conn, _pu_now + 60, 3600)[0]
    check("PU1 재터치 시 post_urls 는 합집합으로 병합",
          sorted(db.json_str_list(_pu_row["post_urls"]))
          == ["https://a.example/1", "https://a.example/2"])
os.remove(_PU_DB)

check("PU2 급증 알림 - post_urls 없으면 출처 줄 생략",
      "🔗" not in telegram.render_volume_spike_alert("PUD", 5.0, 10.0, 2.0))
_pu_spike = telegram.render_volume_spike_alert("PUD", 5.0, 10.0, 2.0,
                                                post_urls=["https://a.example/3"])
check("PU3 급증 알림 - post_urls 있으면 마지막 구분선 아래 출처 표기",
      _pu_spike.rstrip().endswith(telegram._source_line(["https://a.example/3"])))

check("PU4 부분익절 알림 - post_url 없으면 출처 줄 생략",
      "🔗" not in telegram.render_tp_partial_alert("PUE", 1, 3, 100.0, 90.0))
_pu_tp = telegram.render_tp_partial_alert("PUE", 1, 3, 100.0, 90.0,
                                          post_url="https://a.example/4")
check("PU5 부분익절 알림 - post_url 있으면 마지막 구분선 아래 출처 표기",
      _pu_tp.rstrip().endswith(telegram._source_line(["https://a.example/4"])))

check("PU6 OI 급증 알림 - post_urls 없으면 출처 줄 생략",
      "🔗" not in telegram.render_oi_spike_alert("PUF", 1e9, 1.2e9, 20.0))
_pu_oi = telegram.render_oi_spike_alert("PUF", 1e9, 1.2e9, 20.0,
                                         post_urls=["https://a.example/5"])
check("PU7 OI 급증 알림 - post_urls 있으면 마지막 구분선 아래 출처 표기",
      _pu_oi.rstrip().endswith(telegram._source_line(["https://a.example/5"])))

print()
print("── 본알림 실제 렌더링 ──")
print(touch_msg)
os.remove(TEST_DB)
if os.path.exists(_alert_ledger.ledger_path(TEST_DB)):
    os.remove(_alert_ledger.ledger_path(TEST_DB))   # 다음 실행 오염 방지(위 주석 참고)
sys.exit(0 if ok else 1)