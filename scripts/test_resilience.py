# 철야 수리(2026-07-26) 회귀 테스트 — 수리 1~5·7 검증. 네트워크 호출 없이 전부
# 몽키패치(requests.get/post, 모듈 함수)로 오프라인 검증한다(기존 test_price_logic.py/
# test_cycle.py 스텁 패턴 참고). 다른 test_*.py 와 동일 원칙 — settings.SETTINGS 를
# 전역으로 덮어쓰므로 반드시 독립 프로세스로 실행한다(한 프로세스에 모으면 서로 오염).
import hashlib
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import logging
logging.basicConfig(level=logging.WARNING)

import requests

ok = True
n_checks = 0


def check(name, cond):
    global ok, n_checks
    n_checks += 1
    print(("✅" if cond else "❌"), name)
    ok = ok and cond


class _FakeResp:
    """requests.Response 흉내 - status_code/json()/text/raise_for_status() 만 필요."""

    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"status {self.status_code}")

    def json(self):
        return self._payload


class _ListLogHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record.getMessage())


# ══════════════════════════════════════════════════════════════════
# 수리1: market_sentiment ALT.S — 90d 는 CoinGecko markets 엔드포인트 미지원값이라
# period 튜플에서 제거되고 30d→7d 로만 시도해야 한다.
# ══════════════════════════════════════════════════════════════════
from monitor import market_sentiment

_ms_periods_tried = []


def _ms_get(url, params=None, headers=None, timeout=None):
    if "coins/markets" in url:
        period = (params or {}).get("price_change_percentage")
        _ms_periods_tried.append(period)
        if period == "7d":  # 30d 는 실패, 7d 로만 성공(폴백 경로 검증)
            coins = [{"symbol": "btc", "price_change_percentage_7d_in_currency": 1.0}]
            coins += [{"symbol": f"alt{i}", "price_change_percentage_7d_in_currency": 2.0}
                      for i in range(25)]
            return _FakeResp(200, coins)
        return _FakeResp(500, text="markets fail")
    if "alternative.me" in url:
        return _FakeResp(200, {"data": [{"value": "50", "value_classification": "Neutral"}]})
    if "/global" in url:
        return _FakeResp(200, {"data": {"market_cap_percentage": {"btc": 55.0}}})
    return _FakeResp(404)


requests.get = _ms_get
_ms_result = market_sentiment._fetch_fresh(5.0)
check("수리1: ALT.S 계산 중 90d 요청이 전혀 발생하지 않는다(period 튜플에서 제거됨)",
      "90d" not in _ms_periods_tried)
check("수리1: 시도한 period 는 30d/7d 뿐, 최대 2회(90d 왕복 낭비 제거)",
      set(_ms_periods_tried) <= {"30d", "7d"} and len(_ms_periods_tried) <= 2)
check("수리1: 30d 실패 시 7d 로 폴백해 ALT.S 계산 성공",
      _ms_result.get("altcoin_season_index") is not None)

_ms_src = Path(market_sentiment.__file__).read_text(encoding="utf-8")
check("수리1: 소스 코드에 '90d' 리터럴이 더 이상 없다",
      '"90d"' not in _ms_src and "'90d'" not in _ms_src)


# ══════════════════════════════════════════════════════════════════
# 수리2: collector.coingecko — 예외처리 부재였던 네트워크 실패가 이제 로그+전파되고,
# build_universe() 는 신선 캐시가 없어도 실패 시 스테일 캐시로 폴백한다.
# ══════════════════════════════════════════════════════════════════
from collector import coingecko
from config import settings

_cg_handler = _ListLogHandler()
coingecko.logger.addHandler(_cg_handler)
coingecko.logger.setLevel(logging.DEBUG)


def _cg_boom(*a, **k):
    raise requests.ConnectionError("network down")


requests.get = _cg_boom

_raised_top = _raised_krw = False
try:
    coingecko.fetch_top_coins(10, 5.0)
except requests.RequestException:
    _raised_top = True
try:
    coingecko.fetch_upbit_krw_symbols(5.0)
except requests.RequestException:
    _raised_krw = True

check("수리2a: fetch_top_coins 네트워크 실패 시 예외를 삼키지 않고 전파(상위가 폴백 판단)",
      _raised_top)
check("수리2a: fetch_upbit_krw_symbols 네트워크 실패 시도 동일하게 전파", _raised_krw)
check("수리2a: 두 실패 모두 로그로 남는다(조용히 죽지 않음)",
      any("실패" in r for r in _cg_handler.records))
coingecko.logger.removeHandler(_cg_handler)

# build_universe: 신선 캐시 없음(만료) + fetch 실패 → 스테일 캐시로 폴백
import json

_cache_path = "cache/_test_resilience_universe.json"
os.makedirs("cache", exist_ok=True)
if os.path.exists(_cache_path):
    os.remove(_cache_path)
_stale_universe = [{"symbol": "BTC", "ticker": "KRW-BTC", "rank": 1, "name": "Bitcoin",
                    "price_usd": 60000.0, "tier_icon": "\U0001f48e"}]
with open(_cache_path, "w", encoding="utf-8") as f:
    json.dump({"updated_at": time.time() - 10 * 86400, "universe": _stale_universe}, f)

settings.SETTINGS["universe_cache_path"] = _cache_path
settings.SETTINGS["universe_refresh_hours"] = 24
settings.SETTINGS["universe_top_n"] = 10
settings.SETTINGS["http_timeout_sec"] = 5.0

_uni_fallback = coingecko.build_universe()
check("수리2b: 신선 캐시 없이 fetch 도 실패하면 예외 대신 만료 캐시를 반환",
      _uni_fallback == _stale_universe)

os.remove(_cache_path)
_raised_no_cache = False
try:
    coingecko.build_universe()
except requests.RequestException:
    _raised_no_cache = True
check("수리2b: 캐시조차 없으면 예외를 그대로 전파(호출부가 이번 회차 스킵 판단)",
      _raised_no_cache)


# ══════════════════════════════════════════════════════════════════
# 즉시수리(2026-07-27, 개발자B, 교차감사 A-m5 확정): 빈 유니버스가 캐시·stale
# 폴백에 박혀 24시간짜리 조용한 장애가 되던 문제. _save_cache 는 빈 목록을 저장하지
# 않고, _load_cache/_load_cache_any_age 는 truthy 게이트로 빈 캐시를 미스 취급한다.
# ══════════════════════════════════════════════════════════════════
_cache_path_m5 = "cache/_test_resilience_universe_m5.json"
if os.path.exists(_cache_path_m5):
    os.remove(_cache_path_m5)
settings.SETTINGS["universe_cache_path"] = _cache_path_m5
settings.SETTINGS["universe_refresh_hours"] = 24
settings.SETTINGS["universe_top_n"] = 10
settings.SETTINGS["http_timeout_sec"] = 5.0

_good_universe_m5 = [{"symbol": "ETH", "ticker": "KRW-ETH", "rank": 2, "name": "Ethereum",
                      "price_usd": 3000.0, "tier_icon": "\U0001f947"}]


def _cg_get_empty_markets(url, params=None, headers=None, timeout=None):
    if "coins/markets" in url:
        return _FakeResp(200, [])   # CoinGecko 가 빈 배열로 응답(순간 오류 재현)
    if "market/all" in url:
        return _FakeResp(200, [{"market": "KRW-BTC"}])
    return _FakeResp(404)


# m5-1: 캐시가 아예 없는 상태에서 빈 응답 → build_universe 는 여전히 빈 목록을
# 반환하지만(동작 유지, 예외 아님), 그 빈 목록을 캐시 파일로 저장하지는 않는다 -
# 저장했다면 다음 24시간 동안 "신선한 빈 캐시"로 굳어 재조회 자체가 안 됐을 것.
requests.get = _cg_get_empty_markets
_uni_empty1 = coingecko.build_universe()
check("즉시수리(m5)-1: 빈 응답이면 build_universe 는 빈 목록을 반환(기존 동작 유지)",
      _uni_empty1 == [])
check("즉시수리(m5)-1b: 빈 목록은 캐시 파일로 저장되지 않는다(다음 호출이 재조회 가능)",
      not os.path.exists(_cache_path_m5))

# m5-2: 정상 캐시가 이미 있는데(단, 만료돼 fetch 를 타야 하는 상태) 빈 응답이 오면
# - 반환값은 이번 회차의 빈 결과 그대로지만, 디스크의 기존 정상 캐시는 덮어써지지
# 않고 보존된다(다음 정상 응답이 올 때까지 "어제자 유니버스"를 잃지 않는다).
with open(_cache_path_m5, "w", encoding="utf-8") as f:
    json.dump({"updated_at": time.time() - 25 * 3600, "universe": _good_universe_m5}, f)
_uni_empty2 = coingecko.build_universe()
with open(_cache_path_m5, "r", encoding="utf-8") as f:
    _payload_after_m5 = json.load(f)
check("즉시수리(m5)-2: 정상 캐시가 있어도 빈 응답이면 반환값은 빈 목록(재조회 결과 그대로)",
      _uni_empty2 == [])
check("즉시수리(m5)-2b: 기존 정상 캐시 파일은 빈 응답으로 덮어써지지 않는다(보존)",
      _payload_after_m5.get("universe") == _good_universe_m5)

# m5-3: (구버전 버그로) 캐시에 이미 빈 목록이 "신선하게" 저장돼 있던 경우 -
# truthy 게이트 덕에 24시간 이내여도 미스 취급해 실제로 재조회가 발생해야 한다.
with open(_cache_path_m5, "w", encoding="utf-8") as f:
    json.dump({"updated_at": time.time(), "universe": []}, f)

_fetch_called_m5 = {"n": 0}


def _cg_get_track_calls(url, params=None, headers=None, timeout=None):
    _fetch_called_m5["n"] += 1
    if "coins/markets" in url:
        return _FakeResp(200, [{"symbol": "eth", "market_cap_rank": 2, "name": "Ethereum",
                                "current_price": 3000.0}])
    if "market/all" in url:
        return _FakeResp(200, [{"market": "KRW-ETH"}])
    return _FakeResp(404)


requests.get = _cg_get_track_calls
_uni_m5_3 = coingecko.build_universe()
check("즉시수리(m5)-3: 신선(24h 이내)해도 캐시가 빈 목록이면 미스 취급 - 재조회 발생",
      _fetch_called_m5["n"] > 0)
check("즉시수리(m5)-3b: 재조회 성공 시 정상 유니버스를 반환한다",
      len(_uni_m5_3) == 1 and _uni_m5_3[0]["symbol"] == "ETH")
with open(_cache_path_m5, "r", encoding="utf-8") as f:
    _payload_m5_3 = json.load(f)
check("즉시수리(m5)-3c: 정상 결과는 캐시에 저장된다(빈 목록만 저장 생략 - 회귀 아님)",
      _payload_m5_3.get("universe") == _uni_m5_3)

os.remove(_cache_path_m5)


# ══════════════════════════════════════════════════════════════════
# 수리2(하류) + 수리3 + 수리4: scripts/run_collect.py
# ══════════════════════════════════════════════════════════════════
from scripts import run_collect
from collector import tradingview, watcher_stats
from monitor import upbit as upbit_mod
from storage import db

TEST_DB_RC = "cache/_test_resilience_collect.db"
if os.path.exists(TEST_DB_RC):
    os.remove(TEST_DB_RC)
settings.SETTINGS["db_path"] = TEST_DB_RC
db.init_db(TEST_DB_RC)

# 공통 무해화 스텁 - 실제 네트워크 호출 완전 차단
upbit_mod.fetch_prices = lambda tickers, timeout: {}
tradingview.reset_detail_budget = lambda: None
tradingview.is_blocked = lambda: False
tradingview.hard_block_detected = lambda: None
watcher_stats.load_author_stats = lambda timeout=15.0: {}

_old_argv = sys.argv


def _run_main(argv):
    global _old_argv
    _old_argv = sys.argv
    sys.argv = argv
    try:
        return run_collect.main()
    finally:
        sys.argv = _old_argv


# ── 수리2(하류): build_universe() 예외가 회차 전체를 죽이지 않는다 ──────────
def _boom_universe(force=False):
    raise RuntimeError("coingecko 완전 실패")


coingecko.build_universe = _boom_universe
_rc_boom = _run_main(["run_collect.py"])
check("수리2(run_collect): 유니버스 갱신 실패해도 main() 이 예외 없이 종료(0)",
      _rc_boom == 0)

# ── 수리3: 아이디어 1건(예외 발생)이 나머지 처리/커밋을 막지 않는다 ──────────
_FAKE_UNIVERSE = [{"symbol": "AAA", "ticker": "KRW-AAA", "rank": 5, "name": "AAA Coin",
                   "price_usd": 10.0, "tier_icon": "🥈"}]
_FAKE_IDEAS = [
    {"title": "BOOM idea", "description": "x", "author": "AuthorBad", "url": "u-bad",
     "age_minutes": 5, "author_followers": 100},
    {"title": "Good idea", "description": "y", "author": "AuthorGood", "url": "u-good",
     "age_minutes": 5, "author_followers": 100},
]


def _fake_parse_setup(text, current_price=None):
    if "BOOM" in text:
        raise ValueError("파싱 중 의도적 예외(격리 검증용)")
    return {"direction": "long", "entry": 10.0, "entry_low": 10.0, "entry_high": 10.0,
            "sl": 9.0, "tp": 12.0, "rr": 2.0}


coingecko.build_universe = lambda force=False: _FAKE_UNIVERSE
tradingview.fetch_ideas = lambda symbol, timeout, max_age_hours=None: _FAKE_IDEAS
tradingview.fetch_author_followers = lambda username, timeout: 100
run_collect.parse_setup = _fake_parse_setup
run_collect.calculate_grade = lambda *a, **k: ("B", 60, 2.0)
run_collect.judgment_window_hours = lambda *a, **k: 168.0
run_collect.parse_timeframe_hours = lambda text: None

_rc_isolate = _run_main(["run_collect.py"])
with db.connect(TEST_DB_RC) as conn:
    _authors = {r["author"] for r in conn.execute("SELECT author FROM levels").fetchall()}
check("수리3: main() 이 아이템 예외로 죽지 않고 정상 종료(0)", _rc_isolate == 0)
check("수리3: 불량 아이디어 뒤에 오는 정상 아이디어는 계속 처리·저장된다",
      "AuthorGood" in _authors)
check("수리3: 불량 아이디어 자체는 예외로 스킵되어 저장되지 않는다",
      "AuthorBad" not in _authors)

sys.argv = _old_argv


# ── 수리4: _check_deletions - 차단 break 시 하루 게이트를 소진하지 않는다 ──────
TEST_DB_DEL = "cache/_test_resilience_delgate.db"
if os.path.exists(TEST_DB_DEL):
    os.remove(TEST_DB_DEL)
db.init_db(TEST_DB_DEL)


def _insert_touched(conn, key):
    conn.execute(
        """INSERT INTO levels (signal_key, coin_symbol, ticker, direction, entry_usd,
             author, post_url, status, collected_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (key, "TST", "KRW-TST", "long", 1.0, "Auth", f"https://tv.example/{key}",
         "touched", time.time()))


with db.connect(TEST_DB_DEL) as conn:
    for i in range(3):
        _insert_touched(conn, f"delkey{i}")

settings.SETTINGS["deletion_check_daily_limit"] = 10
settings.SETTINGS["deletion_recheck_after_days"] = 30

# 시나리오 A - 실전 재현: 후보는 있는데 첫 순번부터 차단 감지 → break, 0건 확인
tradingview.is_blocked = lambda: True
with db.connect(TEST_DB_DEL) as conn:
    n_a = run_collect._check_deletions(conn, timeout=5.0)
    gate_after_block = db.get_meta(conn, "last_deletion_check_day")
check("수리4 시나리오A: 차단으로 0건만 확인됨(오늘 실전 재현 조건)", n_a == 0)
check("수리4(핵심 수리) 시나리오A: 차단 break 시 하루 게이트를 소진하지 않는다"
      " - 로그('다음 회차로 연기')와 동작이 일치", gate_after_block is None)

# 시나리오 B - 차단 없이 정상 순회 완료 → 게이트 정상 set(하루 1회 방어 유지)
tradingview.is_blocked = lambda: False
tradingview.check_post_deleted = lambda url, timeout: False  # 전부 생존
with db.connect(TEST_DB_DEL) as conn:
    n_b = run_collect._check_deletions(conn, timeout=5.0)
    gate_after_complete = db.get_meta(conn, "last_deletion_check_day")
check("수리4 시나리오B: 정상 순회 완료 시엔 여전히 게이트가 set된다(회귀 아님)",
      gate_after_complete is not None)
check("수리4 시나리오B: 정상 순회는 0건 삭제(전부 생존) - 확인 자체는 수행됨", n_b == 0)

# 시나리오 C - 후보 자체가 없는 날 → 기존처럼 게이트 set(이 분기는 원래도 정상)
TEST_DB_EMPTY = "cache/_test_resilience_delgate_empty.db"
if os.path.exists(TEST_DB_EMPTY):
    os.remove(TEST_DB_EMPTY)
db.init_db(TEST_DB_EMPTY)
with db.connect(TEST_DB_EMPTY) as conn:
    n_c = run_collect._check_deletions(conn, timeout=5.0)
    gate_empty = db.get_meta(conn, "last_deletion_check_day")
check("수리4 시나리오C: 후보 없음이면 0건 + 게이트 set(회귀 없음 확인)",
      n_c == 0 and gate_empty is not None)

# 시나리오 D (즉시수리, 2026-07-27, 개발자B, 교차감사 A-M2 확정): 차단은 없었지만
# (blocked=False) 후보 전원이 '판정 보류(None)' - deleted_checked_at 이 하나도
# 갱신 안 됐는데 예전엔 여기서도 게이트가 소진돼, 결정적 정렬(ORDER BY (checked_at
# IS NOT NULL), collected_at ASC) 때문에 다음날도 '같은' 후보가 다시 뽑히고 또
# 전부 None 이면 - 게이트만 매일 태워지고 실제 확인은 영원히 0건이었다(실적 3일째
# 0건의 근본 원인). 핵심 수리: 진전(n_checked>0) 없이는 게이트를 태우지 않는다.
TEST_DB_DEL_NONE = "cache/_test_resilience_delgate_none.db"
if os.path.exists(TEST_DB_DEL_NONE):
    os.remove(TEST_DB_DEL_NONE)
db.init_db(TEST_DB_DEL_NONE)
with db.connect(TEST_DB_DEL_NONE) as conn:
    for i in range(3):
        _insert_touched(conn, f"nonekey{i}")

tradingview.is_blocked = lambda: False
tradingview.check_post_deleted = lambda url, timeout: None  # 전원 판정 보류
with db.connect(TEST_DB_DEL_NONE) as conn:
    n_d = run_collect._check_deletions(conn, timeout=5.0)
    gate_after_none = db.get_meta(conn, "last_deletion_check_day")
    n_checked_col_d = conn.execute(
        "SELECT COUNT(*) AS n FROM levels WHERE deleted_checked_at IS NOT NULL"
    ).fetchone()["n"]
check("수리4 시나리오D: 차단 없이 정상 순회했지만 전원 판정 보류 - 삭제 확정 0건",
      n_d == 0)
check("수리4 시나리오D: deleted_checked_at 은 단 한 건도 갱신되지 않는다(전원 보류)",
      n_checked_col_d == 0)
check("수리4(핵심 수리) 시나리오D: 전원 None 이면 진전이 없었으므로 하루 게이트를 "
      "태우지 않는다 - 안 그러면 같은 후보가 매일 재선택만 되고 확인은 영원히 0건",
      gate_after_none is None)

# 다음 "수집 회차"(2분 뒤가 아니라 4시간 뒤 cron)에는 즉시 재시도되고, 하루 최대
# 6회(24h/4h)로 유계된다는 것도 함께 확인 - 게이트가 안 걸렸으니 즉시 재호출해도
# 다시 시도된다(무한 루프가 아니라 "하루 여러 번 재시도 가능"이라는 뜻).
with db.connect(TEST_DB_DEL_NONE) as conn:
    n_d2 = run_collect._check_deletions(conn, timeout=5.0)
check("수리4 시나리오D 후속: 게이트 미소진 덕에 다음 회차(같은 날)도 즉시 재시도된다",
      n_d2 == 0)  # 여전히 전부 None 이라 확인 0건이지만, 재시도 자체는 일어남(예외 없음)

# 시나리오 E (즉시수리): 후보 중 '1건이라도' 실제 판정(True/False)이 나오면 -
# 전원 보류가 아니므로 진전이 있었던 것 - 게이트가 정상적으로 소진돼야 한다
# (매 회차 무한정 재시도되는 것도 낭비이므로, 진전이 있었으면 하루 상한을 지킨다).
TEST_DB_DEL_MIX = "cache/_test_resilience_delgate_mix.db"
if os.path.exists(TEST_DB_DEL_MIX):
    os.remove(TEST_DB_DEL_MIX)
db.init_db(TEST_DB_DEL_MIX)
with db.connect(TEST_DB_DEL_MIX) as conn:
    for i in range(3):
        _insert_touched(conn, f"mixkey{i}")

_mix_results = {"mixkey0": None, "mixkey1": False, "mixkey2": None}


def _mix_check_post_deleted(url, timeout):
    for k, v in _mix_results.items():
        if k in url:
            return v
    return None


tradingview.is_blocked = lambda: False
tradingview.check_post_deleted = _mix_check_post_deleted
with db.connect(TEST_DB_DEL_MIX) as conn:
    n_e = run_collect._check_deletions(conn, timeout=5.0)
    gate_after_mix = db.get_meta(conn, "last_deletion_check_day")
    n_checked_col_e = conn.execute(
        "SELECT COUNT(*) AS n FROM levels WHERE deleted_checked_at IS NOT NULL"
    ).fetchone()["n"]
check("수리4 시나리오E: 3건 중 1건만 실제 판정(나머지 보류) - 확인 1건 기록",
      n_checked_col_e == 1)
check("수리4(핵심 수리) 시나리오E: 1건이라도 판정됐으면(진전 있음) 하루 게이트를 "
      "정상적으로 소진한다(전원 보류였던 D 와 대비 - 회귀 아님)",
      gate_after_mix is not None)
check("수리4 시나리오E: 삭제 확정 0건(그 1건은 생존 판정=False)", n_e == 0)


# ══════════════════════════════════════════════════════════════════
# m-A6(2026-07-27, 개발자B, 교차감사 확정) — 삭제확인 '보류 재시도'가 하루
# 6회차 × 5요청 = 최대 30요청까지 증폭되던 문제. 진전 게이트(위 수리4)는 그대로
# 두고, 완전히 별도 축인 '하루 총 요청 수' 를 모듈 상수 캡으로 별도 제한한다.
# ══════════════════════════════════════════════════════════════════
TEST_DB_DEL_CAP = "cache/_test_resilience_delcap.db"
if os.path.exists(TEST_DB_DEL_CAP):
    os.remove(TEST_DB_DEL_CAP)
db.init_db(TEST_DB_DEL_CAP)
with db.connect(TEST_DB_DEL_CAP) as conn:
    for i in range(30):
        _insert_touched(conn, f"capkey{i}")

settings.SETTINGS["deletion_check_daily_limit"] = 5
settings.SETTINGS["deletion_recheck_after_days"] = 30
tradingview.is_blocked = lambda: False
tradingview.check_post_deleted = lambda url, timeout: None  # 전원 보류 - 게이트 미소진 재현

_cap_day = run_collect._day_kst(time.time())
_cap_req_key = run_collect._DELETION_CHECK_REQ_COUNT_KEY_PREFIX + _cap_day

# F1~F2: 캡(10) 이내에서는 회차를 반복해도(전원 보류라 게이트 미소진) 매번 요청이
# 실제로 나가고, 누적 요청 수가 정확히 쌓인다(5요청 × 2회차 = 10, 캡에 정확히 도달).
with db.connect(TEST_DB_DEL_CAP) as conn:
    run_collect._check_deletions(conn, timeout=5.0)
    used_after_1 = int(db.get_meta(conn, _cap_req_key, "0"))
check("m-A6-F1: 1회차 후 요청 카운터가 5(=deletion_check_daily_limit)로 기록됨",
      used_after_1 == 5)

with db.connect(TEST_DB_DEL_CAP) as conn:
    run_collect._check_deletions(conn, timeout=5.0)
    used_after_2 = int(db.get_meta(conn, _cap_req_key, "0"))
check("m-A6-F2: 2회차(게이트 미소진 - 여전히 전원 보류) 후 누적 10(=캡)에 정확히 도달",
      used_after_2 == 10)

# F3: 캡 소진 이후 3회차는 TradingView 요청을 아예 시도하지 않는다(하루 총량 방어).
_cap_calls = {"n": 0}


def _cap_check_post_deleted(url, timeout):
    _cap_calls["n"] += 1
    return None


tradingview.check_post_deleted = _cap_check_post_deleted
with db.connect(TEST_DB_DEL_CAP) as conn:
    n_cap3 = run_collect._check_deletions(conn, timeout=5.0)
    used_after_3 = int(db.get_meta(conn, _cap_req_key, "0"))
check("m-A6-F3: 캡 도달 후 회차는 check_post_deleted 를 단 한 번도 호출하지 않는다"
      "(요청 자체를 시도 안 함 - 게이트가 아니라 요청 수로 막힘)",
      _cap_calls["n"] == 0)
check("m-A6-F3b: 캡 도달 후엔 확인 0건, 카운터도 그대로(10에서 불변)",
      n_cap3 == 0 and used_after_3 == 10)

# F4: 캡 안에서는(진전 게이트와 무관하게) 정상적으로 판정이 이뤄진다 - 캡 자체가
# 기능을 막는 게 아니라 '보류 재시도의 총량'만 막는다는 것을 확인(회귀 방지).
TEST_DB_DEL_CAP2 = "cache/_test_resilience_delcap2.db"
if os.path.exists(TEST_DB_DEL_CAP2):
    os.remove(TEST_DB_DEL_CAP2)
db.init_db(TEST_DB_DEL_CAP2)
with db.connect(TEST_DB_DEL_CAP2) as conn:
    for i in range(3):
        _insert_touched(conn, f"capok{i}")
tradingview.check_post_deleted = lambda url, timeout: False  # 전부 생존 판정(진전 있음)
_cap4_req_key = (run_collect._DELETION_CHECK_REQ_COUNT_KEY_PREFIX
                 + run_collect._day_kst(time.time()))
with db.connect(TEST_DB_DEL_CAP2) as conn:
    n_cap4 = run_collect._check_deletions(conn, timeout=5.0)
    used_cap4 = db.get_meta(conn, _cap4_req_key, "0")
    gate_cap4 = db.get_meta(conn, "last_deletion_check_day")
check("m-A6-F4: 캡 이내 + 진전 있음 - 기존 게이트 동작(하루 1회 소진)은 그대로",
      gate_cap4 is not None)
check("m-A6-F4b: 요청 카운터도 정상 증가(3건)", used_cap4 == "3")

# F5: 날짜별 요청캡 meta 키가 오래되면 정리된다(무한 증가 방지 - 위 _prune_tv_block_alert_meta
# 패턴과 동일한 SQL 을 공유하는 _prune_daily_counter_meta_by_prefix 로 검증).
TEST_DB_DEL_PRUNE = "cache/_test_resilience_delcap_prune.db"
if os.path.exists(TEST_DB_DEL_PRUNE):
    os.remove(TEST_DB_DEL_PRUNE)
db.init_db(TEST_DB_DEL_PRUNE)
_dcp = run_collect._DELETION_CHECK_REQ_COUNT_KEY_PREFIX
with db.connect(TEST_DB_DEL_PRUNE) as conn:
    db.set_meta(conn, _dcp + "2026-07-01", "5")   # 오래됨 - 삭제 대상
    db.set_meta(conn, _dcp + "2026-07-23", "5")   # cutoff 이전 - 삭제 대상
    db.set_meta(conn, _dcp + "2026-07-24", "5")   # cutoff 당일 - 보존(경계, keep_days=3)
    db.set_meta(conn, _dcp + "2026-07-27", "5")   # 오늘 - 보존
    db.set_meta(conn, "other_unrelated_key2", "keep")
    n_pruned_del = run_collect._prune_daily_counter_meta_by_prefix(
        conn, _dcp, "2026-07-27", run_collect._DELETION_CHECK_REQ_META_KEEP_DAYS)
with db.connect(TEST_DB_DEL_PRUNE) as conn:
    remaining_del = {r["key"] for r in conn.execute(
        "SELECT key FROM meta WHERE key LIKE ?", (_dcp + "%",)).fetchall()}
    other_kept_del = db.get_meta(conn, "other_unrelated_key2")
check("m-A6-F5: keep_days(3) 경계 이전 요청캡 키만 정리(경계일 07-24 는 보존)",
      n_pruned_del == 2)
check("m-A6-F5b: 남은 키는 경계일 이후 + 오늘만, 무관 키는 항상 보존",
      remaining_del == {_dcp + "2026-07-24", _dcp + "2026-07-27"} and other_kept_del == "keep")

os.remove(TEST_DB_DEL_CAP)
os.remove(TEST_DB_DEL_CAP2)
os.remove(TEST_DB_DEL_PRUNE)


# ══════════════════════════════════════════════════════════════════
# 수리5: notify/telegram.send() - 재시도(429/5xx/타임아웃), 토큰 마스킹, urgency
# ══════════════════════════════════════════════════════════════════
from notify import telegram

os.environ["TELEGRAM_BOT_TOKEN"] = "123456:FAKESECRETTOKENVALUEXYZ"
os.environ["TELEGRAM_CHAT_ID"] = "1"
_FAKE_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

_tg_sleep_calls = []
_orig_tg_sleep = telegram.time.sleep
telegram.time.sleep = lambda s: _tg_sleep_calls.append(s)

# 5a: 429 - retry_after 대기 후 1회 재시도로 성공
_posts_a = []


def _post_429_then_ok(url, json=None, timeout=None):
    _posts_a.append(json)
    if len(_posts_a) == 1:
        return _FakeResp(429, payload={"parameters": {"retry_after": 3}}, text="rate limited")
    return _FakeResp(200)


requests.post = _post_429_then_ok
_tg_sleep_calls.clear()
res_a = telegram.send("hello")
check("수리5a: 429 - retry_after 대기 후 재시도로 성공", res_a is True and len(_posts_a) == 2)
check("수리5a: retry_after(3초, 상한 이내) 그대로 대기",
      bool(_tg_sleep_calls) and abs(_tg_sleep_calls[-1] - 3.0) < 0.01)

# 5b: 429 retry_after 가 상한(10초) 초과 → 10초로 캡
_posts_b = []


def _post_429_big(url, json=None, timeout=None):
    _posts_b.append(json)
    if len(_posts_b) == 1:
        return _FakeResp(429, payload={"parameters": {"retry_after": 999}})
    return _FakeResp(200)


requests.post = _post_429_big
_tg_sleep_calls.clear()
telegram.send("hi")
check("수리5b: retry_after 999초 요청도 상한 10초로 캡",
      bool(_tg_sleep_calls) and abs(_tg_sleep_calls[-1] - 10.0) < 0.01)

# 5c: 5xx 계속 실패 → 총 재시도 2회 소진(시도 3회) 후 False, 백오프 1초/2초
_posts_c = []


def _post_500_always(url, json=None, timeout=None):
    _posts_c.append(json)
    return _FakeResp(500, text="server error")


requests.post = _post_500_always
_tg_sleep_calls.clear()
res_c = telegram.send("hi")
check("수리5c: 5xx 계속 실패 - 재시도 소진 후 False(삼키고 False, 잡을 안 막음)",
      res_c is False)
check("수리5c: 총 시도 3회(최초 1 + 재시도 2)", len(_posts_c) == 3)
check("수리5c: 5xx 백오프 1초→2초 순서", _tg_sleep_calls == [1.0, 2.0])

# 5d: 타임아웃/연결오류(RequestException) 도 동일 정책으로 재시도 후 소진
_calls_d = {"n": 0}


def _post_timeout(url, json=None, timeout=None):
    _calls_d["n"] += 1
    raise requests.Timeout("timed out")


requests.post = _post_timeout
_tg_sleep_calls.clear()
res_d = telegram.send("hi")
check("수리5d: 타임아웃 계속 - 재시도 소진 후 False", res_d is False and _calls_d["n"] == 3)

# 5e: 토큰 마스킹 - 예외 문자열에 토큰 URL 이 실려도 로그엔 남지 않는다
_tg_handler = _ListLogHandler()
telegram.logger.addHandler(_tg_handler)
telegram.logger.setLevel(logging.DEBUG)


def _post_exc_leaks_token(url, json=None, timeout=None):
    raise requests.ConnectionError(f"Failed to establish a new connection: {url}")


requests.post = _post_exc_leaks_token
_tg_sleep_calls.clear()
telegram.send("hi")
_joined_e = "\n".join(_tg_handler.records)
check("수리5e: 연결오류 로그에 봇 토큰 원문이 남지 않는다", _FAKE_TOKEN not in _joined_e)
check("수리5e: 마스킹 표식(***)은 로그에 남는다", "***" in _joined_e)
telegram.logger.removeHandler(_tg_handler)

# 5f: status 오류 바디에 토큰이 섞여도 로그는 마스킹된다
_tg_handler2 = _ListLogHandler()
telegram.logger.addHandler(_tg_handler2)


def _post_400_leaks_token(url, json=None, timeout=None):
    return _FakeResp(400, text=f"Bad Request for url: {url}")


requests.post = _post_400_leaks_token
telegram.send("hi")
_joined_f = "\n".join(_tg_handler2.records)
check("수리5f: 4xx 응답 바디 로그도 토큰 마스킹", _FAKE_TOKEN not in _joined_f)
telegram.logger.removeHandler(_tg_handler2)

# 5g: urgency 파라미터 - 기본값(high)은 기존과 동일(무음 플래그 없음), low 만 무음
_last_payload = {}


def _post_capture(url, json=None, timeout=None):
    _last_payload.clear()
    _last_payload.update(json or {})
    return _FakeResp(200)


requests.post = _post_capture
telegram.send("hi")
check("수리5g: urgency 기본값(high)은 disable_notification 미설정(기존 동작 유지)",
      "disable_notification" not in _last_payload)
telegram.send("hi", urgency="low")
check("수리5g: urgency='low' 는 disable_notification=true(무음) 전송",
      _last_payload.get("disable_notification") is True)

telegram.time.sleep = _orig_tg_sleep


# ══════════════════════════════════════════════════════════════════
# 수리6/7 grep 수준 확인 (워크플로 YAML - 실행 자체는 CI 몫, 여기선 텍스트만 검사)
# ══════════════════════════════════════════════════════════════════
_repo_root = Path(__file__).resolve().parent.parent
_tests_yml = (_repo_root / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
check("수리6: tests.yml 이 python 3.12 를 사용(price-check.yml/weekly-report.yml 과 일치)",
      'python-version: "3.12"' in _tests_yml)
check("수리6(등록 확인): tests.yml 테스트 목록에 test_resilience 등록됨",
      "test_resilience" in _tests_yml)

_pc_yml = (_repo_root / ".github" / "workflows" / "price-check.yml").read_text(encoding="utf-8")
check("수리7: push 3회 실패 시 ::warning 대신 ::error 로 실패를 표면화",
      "::warning::상태 커밋 push 3회 실패" not in _pc_yml and "::error::" in _pc_yml)
_after_error = _pc_yml.split("::error::")[-1] if "::error::" in _pc_yml else ""


def _fails_after_error(tail: str) -> bool:
    """마지막 ::error:: 뒤에 exit 1 이 오고, 그 전에 exit 0 이 끼지 않는가.

    2026-07-28: 예전엔 `"exit 1" in tail[:200]` 으로 쟀는데, 그 사이에 코드가 몇 줄만
    늘어도(이번엔 push 실패 시 텔레그램 경보 curl) 불변식은 멀쩡한데 테스트가 깨졌다.
    지켜야 할 성질은 '문구와 exit 1 사이의 물리적 거리'가 아니라 **잡이 실제로
    실패하는 것**이므로 그걸 직접 잰다."""
    if "exit 1" not in tail:
        return False
    return "exit 0" not in tail[:tail.index("exit 1")]


check("수리7: ::error:: 뒤 exit 1 로 잡을 실제로 실패시킨다(중간에 exit 0 없음)",
      _fails_after_error(_after_error))


# ══════════════════════════════════════════════════════════════════
# 수리9(2026-07-27): 수집 순환 — 차단 중도 이탈 시 다음 회차가 이어받는다
# (유니버스가 시총 내림차순 고정이라, 차단이 반복되면 꼬리 심볼이 영원히
#  조회되지 않던 기아 문제. 실측: 쿠키 등록 후에도 61번째에서 403)
# ══════════════════════════════════════════════════════════════════
settings.SETTINGS["db_path"] = TEST_DB_RC
settings.SETTINGS["tv_fetch_sleep_sec"] = 0.0   # 순환 검증에 페이싱 대기는 불필요
db.init_db(TEST_DB_RC)

_ROT_UNIVERSE = [
    {"symbol": f"R{i}", "ticker": f"KRW-R{i}", "rank": i + 1, "name": f"R{i} Coin",
     "price_usd": 10.0, "tier_icon": "🥈"} for i in range(5)
]
_fetched: list = []


def _rot_fetch_ideas(symbol, timeout, max_age_hours=None):
    _fetched.append(symbol)
    return []


coingecko.build_universe = lambda force=False: list(_ROT_UNIVERSE)
tradingview.fetch_ideas = _rot_fetch_ideas
tradingview.hard_block_detected = lambda: None
upbit_mod.fetch_prices = lambda tickers, timeout: {}

# R1: 차단 없이 완주 → offset 은 0 유지(대형주 우선 순서 복원)
tradingview.is_blocked = lambda: False
_fetched.clear()
_run_main(["run_collect.py"])
with db.connect(TEST_DB_RC) as conn:
    _off1 = db.get_meta(conn, run_collect._UNIVERSE_OFFSET_META, "0")
check("수리9-R1: 완주 시 순환 지점 0 (기존 순서 유지)", _off1 == "0")
check("수리9-R1: 전체 5심볼 순서대로 조회", _fetched == ["R0", "R1", "R2", "R3", "R4"])

# R2: 2개 처리 후 차단 → 멈춘 지점(2)이 저장되고 나머지는 이월
tradingview.is_blocked = lambda: len(_fetched) >= 2
_fetched.clear()
_run_main(["run_collect.py"])
with db.connect(TEST_DB_RC) as conn:
    _off2 = db.get_meta(conn, run_collect._UNIVERSE_OFFSET_META, "0")
check("수리9-R2: 차단 이탈 시 멈춘 절대 위치 저장", _off2 == "2")
check("수리9-R2: 차단 전까지만 조회(R0,R1)", _fetched == ["R0", "R1"])

# R3: 다음 회차는 저장 지점(R2)부터 재개, 완주하면 0 으로 리셋
tradingview.is_blocked = lambda: False
_fetched.clear()
_run_main(["run_collect.py"])
with db.connect(TEST_DB_RC) as conn:
    _off3 = db.get_meta(conn, run_collect._UNIVERSE_OFFSET_META, "0")
check("수리9-R3: 재개 회차는 이월 지점부터 회전 순회", _fetched == ["R2", "R3", "R4", "R0", "R1"])
check("수리9-R3: 완주 후 순환 지점 0 리셋", _off3 == "0")

# R4: 연쇄 차단 — 재개 회차도 중도 이탈하면 절대 위치로 환산해 저장된다
with db.connect(TEST_DB_RC) as conn:
    db.set_meta(conn, run_collect._UNIVERSE_OFFSET_META, "3")
tradingview.is_blocked = lambda: len(_fetched) >= 3
_fetched.clear()
_run_main(["run_collect.py"])
with db.connect(TEST_DB_RC) as conn:
    _off4 = db.get_meta(conn, run_collect._UNIVERSE_OFFSET_META, "0")
check("수리9-R4: 재개(R3부터) 후 3개 처리 뒤 차단 → (3+3)%5=1 저장",
      _fetched == ["R3", "R4", "R0"] and _off4 == "1")

# R5: --symbols 수동 실행은 순환을 읽지도 쓰지도 않는다
with db.connect(TEST_DB_RC) as conn:
    db.set_meta(conn, run_collect._UNIVERSE_OFFSET_META, "4")
tradingview.is_blocked = lambda: False
_fetched.clear()
_run_main(["run_collect.py", "--symbols", "R0"])
with db.connect(TEST_DB_RC) as conn:
    _off5 = db.get_meta(conn, run_collect._UNIVERSE_OFFSET_META, "0")
check("수리9-R5: --symbols 는 순환 미적용(전체가 아닌 지정분만, meta 불변)",
      _fetched == ["R0"] and _off5 == "4")

# R6: 페이싱 완충 확대(3.0→5.0)가 설정 소스에 반영돼 있다 — 이 프로세스는 위에서
# SETTINGS 를 0.0 으로 덮었으므로 런타임 값이 아니라 소스 리터럴을 검사한다.
_settings_src = (_repo_root / "config" / "settings.py").read_text(encoding="utf-8")
check("수리9-R6: tv_fetch_sleep_sec 기본값 5.0 (2026-07-27 완충 확대)",
      '"tv_fetch_sleep_sec": 5.0' in _settings_src)


# ══════════════════════════════════════════════════════════════════
# 카드3: 적중 DB 해시체인 무결성 검증 (2026-07-27) — 정상체인/변조탐지/구세대
# 소급 마이그레이션/검증 실패해도 회차 생존, 4가지를 오프라인으로 확인한다.
# ══════════════════════════════════════════════════════════════════
from monitor import price_check as pc

TEST_DB_CHAIN = "cache/_test_resilience_chain.db"
if os.path.exists(TEST_DB_CHAIN):
    os.remove(TEST_DB_CHAIN)
db.init_db(TEST_DB_CHAIN)


def _insert_level_chain(conn, key, **extra):
    cols = dict(signal_key=key, coin_symbol="CHN", ticker="KRW-CHN", direction="long",
                entry_usd=1.0, author="AuthChain", post_url=f"https://tv.example/{key}",
                status="touched", collected_at=time.time())
    cols.update(extra)
    fields = ", ".join(cols.keys())
    qs = ", ".join("?" for _ in cols)
    cur = conn.execute(f"INSERT INTO levels ({fields}) VALUES ({qs})", tuple(cols.values()))
    return cur.lastrowid


# ── 체인1: 정상 체인 — resolve_outcome 으로 여러 건 종결 후 검증 통과 ─────
with db.connect(TEST_DB_CHAIN) as conn:
    id_a = _insert_level_chain(conn, "chain_a")
    id_b = _insert_level_chain(conn, "chain_b")
    id_c = _insert_level_chain(conn, "chain_c")

with db.connect(TEST_DB_CHAIN) as conn:
    db.resolve_outcome(conn, id_a, "hit", 1500.0, "tp_sl", r_multiple=2.0, now=time.time())
    db.resolve_outcome(conn, id_b, "miss", 900.0, "tp_sl", r_multiple=-1.0,
                       ambiguous=True, now=time.time())
    # 같은 호출 안에서 동일 now(동시각 타이) — 체인 tip 이 순차적으로 전진하는지도 겸해 확인
    same_now = time.time()
    db.resolve_outcome(conn, id_c, "timeboxed_win", 1100.0, "timeboxed",
                       r_multiple=0.3, now=same_now)

with db.connect(TEST_DB_CHAIN) as conn:
    rows = {r["id"]: dict(r) for r in conn.execute(
        "SELECT id, outcome_prev_hash, outcome_hash FROM levels "
        "WHERE id IN (?,?,?)", (id_a, id_b, id_c)).fetchall()}
    v1 = db.verify_outcome_chain(conn)

check("체인1: resolve_outcome 3건 모두 outcome_hash 채워짐",
      all(rows[i]["outcome_hash"] for i in (id_a, id_b, id_c)))
check("체인1: 첫 판정의 prev_hash 는 고정 제네시스",
      rows[id_a]["outcome_prev_hash"] == db._OUTCOME_CHAIN_GENESIS)
check("체인1: 두 번째 판정의 prev_hash 는 첫 판정의 hash(체인 연결)",
      rows[id_b]["outcome_prev_hash"] == rows[id_a]["outcome_hash"])
check("체인1: 정상 체인은 verify_outcome_chain 이 None(불일치 없음)", v1 is None)

# 이미 종결된 행에 재호출하면 no-op(불변 스냅샷 원칙) — 체인도 건드리지 않아야 함
with db.connect(TEST_DB_CHAIN) as conn:
    db.resolve_outcome(conn, id_a, "miss", 1.0, "tp_sl", r_multiple=-9.0, now=time.time())
    row_a_after = dict(conn.execute(
        "SELECT outcome, outcome_hash FROM levels WHERE id=?", (id_a,)).fetchone())
check("체인1b: 이미 종결된 행 재호출은 값도 해시도 그대로(불변 원칙 유지)",
      row_a_after["outcome"] == "hit" and row_a_after["outcome_hash"] == rows[id_a]["outcome_hash"])

# ── 체인2: 행 변조 시 검출 — DB 를 직접 고쳐 판정 필드를 바꾸면 잡혀야 한다 ──
with db.connect(TEST_DB_CHAIN) as conn:
    conn.execute("UPDATE levels SET r_multiple=? WHERE id=?", (99.9, id_b))
    v2 = db.verify_outcome_chain(conn)
check("체인2: r_multiple 사후 변조 시 verify_outcome_chain 이 그 행을 지목",
      v2 is not None and v2["level_id"] == id_b and v2["reason"] == "hash_mismatch")

# 원복하면 다시 정상으로 돌아온다(해시 자체는 그대로 저장돼 있으므로)
with db.connect(TEST_DB_CHAIN) as conn:
    conn.execute("UPDATE levels SET r_multiple=? WHERE id=?", (-1.0, id_b))
    v2b = db.verify_outcome_chain(conn)
check("체인2b: 변조를 되돌리면 다시 정상(회귀 확인용)", v2b is None)

# ── 체인3: 구세대 판정 행(outcome 있는데 hash 없음) 소급 마이그레이션 ──────
TEST_DB_LEGACY = "cache/_test_resilience_chain_legacy.db"
if os.path.exists(TEST_DB_LEGACY):
    os.remove(TEST_DB_LEGACY)
db.init_db(TEST_DB_LEGACY)
with db.connect(TEST_DB_LEGACY) as conn:
    # outcome_hash 컬럼 도입 이전에 이미 종결됐던 행처럼 - resolve_outcome 을 거치지
    # 않고 직접 outcome 계열 컬럼만 채운다(체인 컬럼은 NULL 로 남김).
    t0 = time.time() - 3600
    lg1 = _insert_level_chain(conn, "legacy1", outcome="hit", resolved_at=t0,
                              r_multiple=1.5, ambiguous=0)
    lg2 = _insert_level_chain(conn, "legacy2", outcome="miss", resolved_at=t0 - 100,
                              r_multiple=-1.0, ambiguous=0)  # 더 이른 시각(정렬 검증용)
    lg3 = _insert_level_chain(conn, "legacy3", outcome="miss", resolved_at=t0,
                              r_multiple=-1.0, ambiguous=1)  # lg1 과 동시각 - id 로 타이브레이크

with db.connect(TEST_DB_LEGACY) as conn:
    before = conn.execute(
        "SELECT COUNT(*) AS n FROM levels WHERE outcome IS NOT NULL AND outcome_hash IS NULL"
    ).fetchone()["n"]
check("체인3 사전조건: 마이그레이션 전엔 구세대 행 3건 모두 hash 없음", before == 3)

# init_db 재호출 = 운영에서 매 회차 db.init_db 가 하는 것과 동일 경로(_migrate 가 소급 실행)
db.init_db(TEST_DB_LEGACY)
with db.connect(TEST_DB_LEGACY) as conn:
    after = conn.execute(
        "SELECT COUNT(*) AS n FROM levels WHERE outcome IS NOT NULL AND outcome_hash IS NULL"
    ).fetchone()["n"]
    v3 = db.verify_outcome_chain(conn)
    order_rows = conn.execute(
        "SELECT id, outcome_prev_hash FROM levels WHERE id IN (?,?,?) "
        "ORDER BY resolved_at ASC, id ASC", (lg1, lg2, lg3)).fetchall()
check("체인3: 소급 마이그레이션 후 구세대 행 전부 hash 보유", after == 0)
check("체인3: 소급 구축된 체인도 verify_outcome_chain 통과", v3 is None)
check("체인3: 결정적 순서(resolved_at,id) — 가장 이른 legacy2 가 제네시스에서 시작",
      order_rows[0]["id"] == lg2 and order_rows[0]["outcome_prev_hash"] == db._OUTCOME_CHAIN_GENESIS)
check("체인3: 동시각 타이는 id 로 확정 — legacy1(작은 id)이 legacy3 보다 먼저",
      [r["id"] for r in order_rows] == [lg2, lg1, lg3])

# 재실행해도 이미 있는 체인은 건드리지 않는다(멱등성)
with db.connect(TEST_DB_LEGACY) as conn:
    hash_before_2nd = dict(conn.execute(
        "SELECT id, outcome_hash FROM levels WHERE id=?", (lg1,)).fetchone())
db.init_db(TEST_DB_LEGACY)
with db.connect(TEST_DB_LEGACY) as conn:
    hash_after_2nd = dict(conn.execute(
        "SELECT id, outcome_hash FROM levels WHERE id=?", (lg1,)).fetchone())
check("체인3b: 재-init_db 는 이미 체인 있는 행을 재계산하지 않는다(멱등)",
      hash_before_2nd["outcome_hash"] == hash_after_2nd["outcome_hash"])

# ── 체인4: 검증 실패(예외)해도 run_once 회차는 생존해야 한다 ─────────────
# run_once 는 활성/미종결/수익률대기 레벨이 전무하면 조기 반환하며 이 검증 구간
# 자체에 도달하지 않는다(early-return 경로) - 실제로 검증부까지 흐르도록 활성
# 레벨을 하나 심고, 네트워크 의존(가격조회/발송)은 기존 test_price_logic.py 와
# 동일한 방식으로 몽키패치해 오프라인으로 돌린다.
TEST_DB_CHAIN_CYCLE = "cache/_test_resilience_chain_cycle.db"
if os.path.exists(TEST_DB_CHAIN_CYCLE):
    os.remove(TEST_DB_CHAIN_CYCLE)
settings.SETTINGS["db_path"] = TEST_DB_CHAIN_CYCLE
settings.SETTINGS["announcement_alert_enabled"] = False
db.init_db(TEST_DB_CHAIN_CYCLE)
with db.connect(TEST_DB_CHAIN_CYCLE) as conn:
    lvc4 = dict(coin_symbol="CHC", ticker="KRW-CHC", direction="long", entry_usd=100.0,
               sl_usd=90.0, tp_usd=130.0, rr=3.0, grade="B", score=60, author="AuthC4",
               post_url="https://tv.example/c4", collected_at=time.time() - 600)
    lvc4["signal_key"] = db.make_signal_key("CHC", 100.0, "AuthC4", lvc4["post_url"])
    db.upsert_level(conn, lvc4)

_orig_fetch_prices = upbit_mod.fetch_prices
_orig_send = telegram.send
upbit_mod.fetch_prices = lambda mkts, t: {m: 1400.0 for m in mkts}  # 가격이 entry 대비 멀어 터치 없음
telegram.send = lambda text, urgency="high": True

_orig_verify = db.verify_outcome_chain
db.verify_outcome_chain = lambda conn: (_ for _ in ()).throw(RuntimeError("체인4 고의 예외"))

_now_c4 = time.time()
try:
    s_c4 = pc.run_once(_now_c4)
    _c4_survived = True
except Exception as _e_c4:
    _c4_survived = False
check("체인4: verify_outcome_chain 이 예외를 던져도 run_once 는 죽지 않는다", _c4_survived)

with db.connect(TEST_DB_CHAIN_CYCLE) as conn:
    gate_c4 = db.get_meta(conn, pc._OUTCOME_CHAIN_CHECK_META)
check("체인4: 예외가 나도 오늘 게이트는 마감됨(같은 예외로 매 회차 반복되지 않게)",
      gate_c4 is not None)

# 같은 날 두 번째 호출은 verify_outcome_chain 을 아예 다시 부르지 않아야 한다(게이트 재확인)
_verify_calls = {"n": 0}


def _counting_verify(conn):
    _verify_calls["n"] += 1
    raise RuntimeError("호출되면 안 됨")


db.verify_outcome_chain = _counting_verify
try:
    pc.run_once(_now_c4 + 60)
except Exception:
    pass
check("체인4b: 같은 날 재호출은 하루 게이트로 verify 자체를 건너뜀(회귀 없음)",
      _verify_calls["n"] == 0)

db.verify_outcome_chain = _orig_verify
upbit_mod.fetch_prices = _orig_fetch_prices
telegram.send = _orig_send


# ══════════════════════════════════════════════════════════════════
# 카드2 과제1+3(2026-07-27): price-check.yml 백업 트리거 + 커밋백 재시도 완충
# — 실제 실행은 CI/운영 몫, 여기선 YAML 구조·텍스트만 검사한다(수리6/7 패턴과 동일).
# ══════════════════════════════════════════════════════════════════
_pc_yml_card2 = (_repo_root / ".github" / "workflows" / "price-check.yml").read_text(
    encoding="utf-8")

try:
    import yaml as _yaml
    _pc_parsed_card2 = _yaml.safe_load(_pc_yml_card2)
    _pc_yaml_ok = True
except ImportError:
    # requirements.txt 에 pyyaml 이 없어 CI 에 설치 보장이 안 된다 - 설치를 시도하지
    # 않고(요구사항) 구조 검증만 건너뛴다. 아래 텍스트 기반 검사들은 yaml 없이도 돈다.
    _pc_parsed_card2 = None
    _pc_yaml_ok = False

if _pc_yaml_ok:
    _pc_on = _pc_parsed_card2.get(True) or _pc_parsed_card2.get("on") or {}
    check("과제1: price-check.yml 은 여전히 유효한 YAML(파싱 성공)", _pc_parsed_card2 is not None)
    check("과제1: schedule 트리거 1건 등록됨",
          "schedule" in _pc_on and len(_pc_on["schedule"]) == 1)
    _cron_expr = (_pc_on.get("schedule") or [{}])[0].get("cron", "")
    check("과제1: cron 표현식 */30(GH 최소 5분 하한 위반 아님)",
          _cron_expr == "*/30 * * * *")
    check("과제1: workflow_dispatch 입력 2개(force_collect/force_report) 그대로 보존",
          "force_collect" in _pc_on.get("workflow_dispatch", {}).get("inputs", {})
          and "force_report" in _pc_on.get("workflow_dispatch", {}).get("inputs", {}))
else:
    check("과제1: yaml 모듈 미설치 - 구조 검증 skip(pip install 시도 안 함, 요구사항)", True)

check("과제1: cron-job.org 주 경로 서술이 그대로 남아있음(무변경)",
      "cron-job.org" in _pc_yml_card2)
check("과제1: GH schedule 5분 하한/지연 경고 주석이 있어 '더 당기자' 시도를 막음",
      "5분" in _pc_yml_card2 and ("지연" in _pc_yml_card2 or "밀리" in _pc_yml_card2))
check("과제1: concurrency(db-writer) 그룹은 무변경(중복 트리거 직렬화 유지)",
      "group: db-writer" in _pc_yml_card2 and "cancel-in-progress: false" in _pc_yml_card2)
check("과제1: FORCE_COLLECT/FORCE_REPORT 가 schedule 의 null inputs 를 방어(|| false)",
      "inputs.force_collect || false" in _pc_yml_card2
      and "inputs.force_report || false" in _pc_yml_card2)

check("과제3: 커밋백 재시도가 6회(백오프 5단 3→5→8→13→21초 + 마지막 1회, 총 6회 push 시도)",
      "DELAYS=(3 5 8 13 21)" in _pc_yml_card2
      and "for i in 0 1 2 3 4 5; do" in _pc_yml_card2)
check("과제3: 예전 '3회 실패' 문구는 사라지고 '6회 실패'로 갱신됨",
      "push 3회 실패" not in _pc_yml_card2 and "push 6회 실패" in _pc_yml_card2)
# 기존 수리7(::error::+exit 1 표면화)이 문구 변경 후에도 깨지지 않았는지 재확인.
check("과제3(수리7 회귀 확인): ::error:: 로 실패 표면화 유지",
      "::error::" in _pc_yml_card2)
_after_error_card2 = _pc_yml_card2.split("::error::")[-1]
check("과제3(수리7 회귀 확인): ::error:: 뒤 exit 1 여전히 존재",
      _fails_after_error(_after_error_card2))

# ── 2026-07-28 중복알림 사고 후속 3건 (워크플로 쪽 불변식) ────────────────────
# ① 재동기화가 삭제를 전파해야 한다. `git checkout <tree> -- <path>` 는 삭제를
#    전파하지 않으므로(git 고유 동작) data/ 를 먼저 지우고 받아야 한다. clean -fd 는
#    해법이 아니다 — 추적 중인 파일은 안 지운다(실측 확인).
_sync_block = _pc_yml_card2.split("data/ 최신 상태 재동기화")[-1].split("- name:")[0]
check("07-28①: 재동기화가 data/ 를 먼저 지워 삭제를 전파한다",
      "rm -rf data" in _sync_block and "git checkout FETCH_HEAD -- data/" in _sync_block)
check("07-28①: 최초 부팅(원격에 DB 없음)엔 재동기화를 건너뛴다"
      " - 아티팩트 복원분 보호",
      "git cat-file -e FETCH_HEAD:data/levels.db" in _sync_block)
check("07-28①: 체크아웃 실패 시 지운 data/ 를 되돌린다",
      "git checkout -- data/" in _sync_block)

# ② push 최종 실패는 텔레그램으로도 알린다. ::error + 액션 실패 메일만으로는 신호가
#    가지 않는다(07-23~26 사흘 침묵 전력). 이 실패는 '알림은 나갔는데 기록이 유실'
#    이라 다음 회차가 확정적으로 재발송하는, 사람이 즉시 알아야 할 유일한 모드다.
check("07-28②: push 6회 실패 시 텔레그램 경보를 쏜다",
      "api.telegram.org" in _after_error_card2 and "sendMessage" in _after_error_card2)
check("07-28②: 경보 실패가 exit 1 을 가리지 않는다(|| true)",
      "|| true" in _after_error_card2[:_after_error_card2.index("exit 1")])

# ③ 발송 원장은 '덮어쓰기'가 아니라 '합집합'으로 되돌려야 한다 — 경합에서 진 회차의
#    발송 기록이 사라지면 다음 회차가 그걸 모르고 재발송한다(실측: 커밋 28e14a9 의
#    03:53:50 기록이 870e889 스냅샷에 덮여 영구 소실).
_backup_block = _pc_yml_card2.split("상태 커밋 백")[-1]
check("07-28③: 커밋백이 발송 원장을 합집합 병합한다",
      "alert_ledger" in _backup_block and "merge_files" in _backup_block)
check("07-28③: 병합 입력으로 원격 원장을 스냅샷 복원 전에 확보한다",
      "REMOTE_LEDGER" in _backup_block
      and _backup_block.index("REMOTE_LEDGER") < _backup_block.index("shutil.rmtree"))


# ══════════════════════════════════════════════════════════════════
# M3(2026-07-27, 개발자B, 교차감사 확정) — commit-then-rebase 를
# fetch-then-commit 으로 교체. data/levels.db 는 바이너리라 3-way 머지가
# 불가능해서, 로컬 커밋을 먼저 만들고 그 위에서 rebase 하면 origin 이 data/ 를
# 건드린 경우 매 재시도가 같은 지점에서 결정적으로 재충돌했다(경합과 달리
# 충돌은 기다린다고 안 풀린다 - 07-27 07:17~07:18 실사고). 아래는 그 구조
# 전환이 실제로 반영됐는지와, "무조건 ours 승리"가 아니라 사람이 손댄 data/ 는
# 자동 병합하지 않고 사람에게 넘기는 방어 로직이 존재하는지를 검사한다(실행
# 자체는 위 스크래치패드 임시 저장소 시나리오로 별도 검증 완료 - 여기선 텍스트
# 구조만 확인).
# ══════════════════════════════════════════════════════════════════
check("M3: 커밋을 먼저 만들지 않는다 - 스냅샷을 tmp 로 뺀 뒤 매 재시도에서 fetch 한다",
      "SNAPSHOT_DIR=" in _pc_yml_card2 and "git fetch -q origin main" in _pc_yml_card2)
check("M3: 옛 구조(로컬 커밋 먼저 + git pull --rebase 재시도)의 실제 명령이 제거됨"
      "(설명 주석 속 인용은 남아있을 수 있어 실행 명령 형태로만 검사)",
      "git pull --rebase origin main" not in _pc_yml_card2)
check("M3: base 기록 후 매 루프에서 최신 origin과 비교한다(REMOTE/BASE 갱신)",
      "BASE=\"$(git rev-parse HEAD)\"" in _pc_yml_card2
      and 'REMOTE="$(git rev-parse origin/main)"' in _pc_yml_card2)
check("M3: origin의 data/ 가 base 이후 변경됐는지 diff 로 실제 검사한다",
      'git diff --quiet "$BASE" "$REMOTE" -- data/' in _pc_yml_card2)
check("M3: data/ 충돌 감지 시 조용히 덮지 않고 ::error:: 로 사람에게 넘긴다(exit 1)",
      "자동 병합 위험 회피" in _pc_yml_card2 and "사람 확인 필요" in _pc_yml_card2)
check("M3: 충돌 시 어느 커밋이 끼어들었는지 로그에 남긴다(git log BASE..REMOTE -- data/)",
      'git log --oneline "$BASE"..."$REMOTE" -- data/' in _pc_yml_card2
      or 'git log --oneline "$BASE".."$REMOTE" -- data/' in _pc_yml_card2)
# 2026-07-27 저녁 갱신: 가드가 '변경 여부'에서 '변경 주체'로 바뀌면서 주석 문구도
# 바뀌었다. 특정 낱말이 아니라 **판단 근거가 문서화돼 있는지**를 본다.
check("M3: '무조건 ours 승리가 아닌 이유' 주석이 명시적으로 남아있다",
      "무조건 ours" in _pc_yml_card2 and "유실" in _pc_yml_card2)
check("M3: 안전할 때만 reset --hard 로 최신 origin 을 따라간다",
      'git reset -q --hard "$REMOTE"' in _pc_yml_card2)
# 2026-07-27 저녁 수리: 가드가 '변경 여부'가 아니라 '변경 주체'를 본다.
# 최초 설계는 "concurrency 가 직렬화하니 base 이후 data 변경은 사람뿐"이라고
# 전제했는데 실측이 반박했다 — 수집 회차(6~10분) 동안 2분 트리거가 밀려 쌓였다가
# 한꺼번에 풀리며 봇 회차끼리 겹쳤고(19:45:57·19:46:42 커밋 45초 간격),
# 그 구간에서만 실패가 나 4시간마다 실패 메일 + 회차 유실이 생겼다.
check("M3: 끼어든 data 커밋의 '작성자'를 판별한다(봇 경합 vs 사람 개입)",
      "%an" in _pc_yml_card2 and "grep -v '^izrua-bot$'" in _pc_yml_card2)
check("M3: 봇 자신의 선행 회차면 중단하지 않고 진행한다",
      "::notice::" in _pc_yml_card2 and "정상 경합" in _pc_yml_card2)
check("M3: 봇이 아닌 주체면 종전대로 중단한다(사람 개입 방어는 유지)",
      'if [ -n "$OTHERS" ]; then' in _pc_yml_card2
      and "::error::" in _pc_yml_card2 and "exit 1" in _pc_yml_card2)
check("M3: data/ 를 스냅샷과 정확히 동기화한다(삭제 포함 - 감사 덤프 정리 되살아남 방지)",
      "shutil.rmtree(dst)" in _pc_yml_card2 and "shutil.copytree(snap, dst)" in _pc_yml_card2)
check("M3: 여전히 이 잡 하나만 data/ 를 커밋한다(W1 불변식 - git add data/ 유지)",
      "git add data/" in _pc_yml_card2)

# 2026-07-27 수리(개발자B, 교차감사 m-A4 확정) — fetch 실패 가드. set -e 아래에서
# fetch 가 실패(exit 128)하면 재시도 루프 자체가 첫 판에 증발하던 회귀를
# push 와 동일한 방식(백오프 후 continue)으로 흡수했는지 텍스트로 검사한다.
# 실제 "원격 불가 → 재시도 지속 → 복구 후 성공" 시나리오는 스크래치패드 임시
# 저장소(가짜 git fetch 셔틀)로 별도 재현·검증 완료(레포 밖이라 여기선 텍스트만).
check("m-A4: fetch 를 if ! git fetch 로 감싸 실패를 즉사시키지 않는다",
      'if ! git fetch -q origin main; then' in _pc_yml_card2)
check("m-A4: fetch 실패 시 REMOTE 는 그 반복에서 갱신되지 않는다"
      "(REMOTE 대입이 fetch 가드 성공 분기 안에서만 일어남)",
      'fi\n            REMOTE="$(git rev-parse origin/main)"' in _pc_yml_card2)
check("m-A4: fetch 실패 시에도 push 실패와 동일한 백오프 배열(DELAYS)로 sleep 후 재시도",
      _pc_yml_card2.count('sleep "${DELAYS[$i]}"') >= 2)
check("m-A4: fetch 실패는 ::warning:: 으로 남기되(치명은 아님) 잡을 죽이지 않는다",
      "git fetch 실패" in _pc_yml_card2 and "::warning::git fetch 실패" in _pc_yml_card2)
_fetch_guard_block = _pc_yml_card2.split('if ! git fetch -q origin main; then')[1].split(
    'REMOTE="$(git rev-parse origin/main)"')[0]
check("m-A4: fetch 실패 분기는 continue 로 루프 맨 앞(다음 fetch 시도)으로 돌아간다",
      "continue" in _fetch_guard_block)
check("m-A4: fetch 가드는 마지막 시도(i=5)에서 sleep 을 생략하고 그대로 빠져나가"
      " 기존 최종 ::error::+exit 1 경로에 자연 합류한다(별도 종료 코드 없음)",
      "exit 1" not in _fetch_guard_block and "exit 0" not in _fetch_guard_block)


# ══════════════════════════════════════════════════════════════════
# m-A8(2026-07-27, 개발자B, 교차감사 확정) — reset --hard 불변식 주석.
# 코드 변경은 없다(과제 정의상 주석뿐) - 이 reset --hard 가 data/ 밖 추적 파일의
# 회차 중 변경을 무음으로 버린다는 사실이 스냅샷/reset 부근에 명시돼 있는지만
# 확인한다.
# ══════════════════════════════════════════════════════════════════
check("m-A8: reset --hard 직전에 'data/ 밖 추적 파일' 불변식 경고 주석이 있다",
      "data/ 밖" in _pc_yml_card2 and "추적 파일" in _pc_yml_card2)
check("m-A8: '조용히 버린다'는 문구로 무음 손실 위험을 명시한다",
      "조용히 버린다" in _pc_yml_card2)
check("m-A8: '산출물은 반드시 data/ 밑에' 원칙이 문구로 못박혀 있다",
      "산출물은 반드시 data/ 밑에" in _pc_yml_card2)
_reset_hard_idx = _pc_yml_card2.find('git reset -q --hard "$REMOTE"')
_invariant_idx = _pc_yml_card2.find("data/ 밖")
check("m-A8: 불변식 주석이 reset --hard 실행 줄보다 앞(주변)에 위치한다",
      0 <= _invariant_idx < _reset_hard_idx)


# ══════════════════════════════════════════════════════════════════
# 카드2 과제2(2026-07-27): 가격체크 회차 자체 정지(공백) 감시
# monitor.price_check._check_price_check_gap — 회차가 도는 동안은 자기 정지를
# 스스로 감지할 수 없으므로, 다음 회차가 직전 last_check_at 과의 공백을 사후
# 재구성하는 로직. 네트워크 없이 telegram.send 만 몽키패치해 검증한다.
# ══════════════════════════════════════════════════════════════════
TEST_DB_GAP = "cache/_test_resilience_gap.db"
if os.path.exists(TEST_DB_GAP):
    os.remove(TEST_DB_GAP)
db.init_db(TEST_DB_GAP)

_gap_sent = []
_orig_tg_send_gap = telegram.send
telegram.send = lambda text, urgency="high": (_gap_sent.append(text), True)[1]
settings.SETTINGS["price_check_gap_alert_minutes"] = 120

_now_gap = time.time()
# KST 자정 경계 가드 (2026-07-27 저녁 실전 발견 — 오늘 세 번째 같은 유형).
# G1~G4 는 기준 시각에서 최대 +4.2시간(120초 + 130분 + 121분)까지 앞으로 밀며
# 검사하는데, _check_price_check_gap 의 중복 억제는 **KST 날짜** 게이트다.
# 기준이 저녁이면 G3(경보 발송)과 G4(재차 초과) 사이에 날짜가 바뀌어 게이트가
# 풀리고, 억제돼야 할 G4 가 두 번째 경보를 보내 실패한다(19시 통과 → 21시 실패로
# 재현). 오늘 남은 시간이 5시간 미만이면 그만큼 뒤로 당겨 전 구간을 같은 KST
# 날짜에 넣는다 — 검사 대상 로직과 무관한 시계 문제라 기준 시각만 옮긴다.
_KST_TZ = timezone(timedelta(hours=9))
_gap_day_end = datetime.fromtimestamp(_now_gap, _KST_TZ).replace(
    hour=23, minute=59, second=59, microsecond=0).timestamp()
if _gap_day_end - _now_gap < 5 * 3600:
    _now_gap -= 5 * 3600 - (_gap_day_end - _now_gap)

# G1: 최초 회차(직전 기록 없음) - 비교 대상이 없으니 경보도 meta 기록도 없다
with db.connect(TEST_DB_GAP) as conn:
    r_g1 = pc._check_price_check_gap(conn, _now_gap, settings.get)
    meta_g1 = db.get_meta(conn, "last_price_check_gap_min")
check("과제2-G1: 최초 회차(비교 대상 없음) - 경보/기록 없음",
      r_g1 is False and meta_g1 is None)

# G2: 정상 2분 간격 - 임계(120분) 한참 아래라 경보 없음, 마지막/최장 공백만 기록
with db.connect(TEST_DB_GAP) as conn:
    db.set_meta(conn, "last_check_at", str(_now_gap))
    r_g2 = pc._check_price_check_gap(conn, _now_gap + 120, settings.get)
    last_gap_g2 = db.get_meta(conn, "last_price_check_gap_min")
    max_gap_g2 = db.get_meta(conn, "max_price_check_gap_min")
check("과제2-G2: 정상 2분 간격 - 경보 없음", r_g2 is False and not _gap_sent)
check("과제2-G2b: 마지막/최장 공백 둘 다 2.0분으로 기록",
      last_gap_g2 == "2.0" and max_gap_g2 == "2.0")

# G3: 임계(120분) 초과 공백(130분) - 경보 발송 + 최장 공백 갱신 + 하루 게이트 set
with db.connect(TEST_DB_GAP) as conn:
    db.set_meta(conn, "last_check_at", str(_now_gap + 120))
    r_g3 = pc._check_price_check_gap(conn, _now_gap + 120 + 130 * 60, settings.get)
    max_gap_g3 = db.get_meta(conn, "max_price_check_gap_min")
    warned_g3 = db.get_meta(conn, "price_check_gap_warned_date")
check("과제2-G3: 임계 초과 공백 - 경보 발송",
      r_g3 is True and len(_gap_sent) == 1 and "가격체크 회차 정지" in _gap_sent[0])
check("과제2-G3b: 최장 공백이 130.0분으로 갱신", max_gap_g3 == "130.0")
check("과제2-G3c: 하루 경보 게이트 set(중복 방지용)", warned_g3 is not None)

# G4: 같은 날 재차 초과해도 중복 발송 없음(하루 1회, 다른 감시들과 동일 원칙).
# 공백을 G3(130분)보다 작게(121분, 여전히 임계 120분은 넘음) 잡아 "재발송 억제"와
# "최장 공백 갱신"을 서로 다른 축으로 깔끔히 분리 검증한다.
_gap_now_after_g3 = _now_gap + 120 + 130 * 60
with db.connect(TEST_DB_GAP) as conn:
    db.set_meta(conn, "last_check_at", str(_gap_now_after_g3))
    r_g4 = pc._check_price_check_gap(conn, _gap_now_after_g3 + 121 * 60, settings.get)
    max_gap_g4 = db.get_meta(conn, "max_price_check_gap_min")
check("과제2-G4: 같은 날 재차 초과해도 중복 억제", r_g4 is False and len(_gap_sent) == 1)
check("과제2-G4b: 이번 공백(121분)이 직전 최장(130분)보다 작아 최장은 불변",
      max_gap_g4 == "130.0")

# G5: 최장 공백은 '최댓값만' 갱신 - 이후 더 짧은 공백이 와도 줄어들지 않는다
_gap_now_after_g4 = _gap_now_after_g3 + 121 * 60
with db.connect(TEST_DB_GAP) as conn:
    db.set_meta(conn, "last_check_at", str(_gap_now_after_g4))
    pc._check_price_check_gap(conn, _gap_now_after_g4 + 60, settings.get)  # 1분짜리 공백
    max_gap_g5 = db.get_meta(conn, "max_price_check_gap_min")
    last_gap_g5 = db.get_meta(conn, "last_price_check_gap_min")
check("과제2-G5: 최장 공백은 역대 최댓값 유지(짧은 공백에 안 밀림)", max_gap_g5 == "130.0")
check("과제2-G5b: 마지막 공백은 매번 덮어씀(1.0분으로 갱신)", last_gap_g5 == "1.0")

# G6: 시계 역행 방어 - now 가 직전 last_check_at 보다 과거면 무시(음수 공백 계산 안 함)
with db.connect(TEST_DB_GAP) as conn:
    db.set_meta(conn, "last_check_at", str(_now_gap + 999999))
    r_g6 = pc._check_price_check_gap(conn, _now_gap, settings.get)
    max_gap_g6 = db.get_meta(conn, "max_price_check_gap_min")
    last_gap_g6 = db.get_meta(conn, "last_price_check_gap_min")
check("과제2-G6: 시계 역행 - 경보 없이 무시, 최장/마지막 공백 기록도 불변",
      r_g6 is False and max_gap_g6 == "130.0" and last_gap_g6 == "1.0")

telegram.send = _orig_tg_send_gap
os.remove(TEST_DB_GAP)

check("과제2-G7: render_price_check_gap_alert 렌더러 신설(파일 끝 추가 원칙)",
      hasattr(telegram, "render_price_check_gap_alert"))
_gap_render = telegram.render_price_check_gap_alert(130.0, 120.0)
check("과제2-G7b: 렌더 본문에 공백·임계 수치 반영",
      "130" in _gap_render and "120" in _gap_render)


# ══════════════════════════════════════════════════════════════════
# 즉시수리(2026-07-27, 개발자B, 교차감사 A-m2 확정): 워쳐독 3종
# (_check_collect_silence / _check_price_check_gap / _check_outcome_chain) 의
# meta 기록이 telegram.send 호출보다 '먼저' 커밋돼 있는지 검증한다. send 호출
# 시점에 - 원래 커넥션의 아직 열려 있는 트랜잭션과는 무관하게 - 같은 DB 파일에
# 대고 새 커넥션을 열어 meta 를 조회한다. 새 커넥션에서 값이 보인다는 것은 곧
# conn.commit() 이 send 이전에 실제로 디스크까지 반영됐다는 뜻이다(진행 중인
# 트랜잭션은 다른 커넥션에는 보이지 않으므로, 이 검증은 "set_meta 만 호출됐다"가
# 아니라 "commit 까지 됐다"를 잡아낸다).
# ══════════════════════════════════════════════════════════════════
import sqlite3 as _sqlite3

TEST_DB_ORDER = "cache/_test_resilience_watchdog_order.db"
if os.path.exists(TEST_DB_ORDER):
    os.remove(TEST_DB_ORDER)
db.init_db(TEST_DB_ORDER)


def _meta_via_fresh_conn(key):
    c = _sqlite3.connect(TEST_DB_ORDER)
    try:
        row = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None
    finally:
        c.close()


_orig_send_order = telegram.send

# ── _check_collect_silence: send 시점에 별도 커넥션으로 meta 조회 ──
_order_seen_silence = {}


def _send_check_silence(text):
    _order_seen_silence["meta_at_send"] = _meta_via_fresh_conn("collect_silence_warned_date")
    return True


_now_order = time.time()
with db.connect(TEST_DB_ORDER) as conn:
    for i in range(14):  # 직전 7일 이틀에 한 번꼴 수집 → 평균 있음(오탐 방지 통과)
        conn.execute(
            "INSERT INTO levels (signal_key, coin_symbol, ticker, direction, status, collected_at) "
            "VALUES (?, 'X', 'KRW-X', 'long', 'expired', ?)",
            (f"ord_silence_{i}", _now_order - 25 * 3600 - i * 12 * 3600))

telegram.send = _send_check_silence
with db.connect(TEST_DB_ORDER) as conn:
    _r_order_silence = pc._check_collect_silence(conn, _now_order, settings.get)
check("즉시수리(m2)-order-a: _check_collect_silence - send 성공", _r_order_silence is True)
check("즉시수리(m2)-order-a(핵심): send 호출 시점에 이미 meta 가 별도 커넥션에서도 보인다"
      "(set_meta 뿐 아니라 commit 까지 send 이전에 끝났다는 증거)",
      _order_seen_silence.get("meta_at_send") is not None)

# ── _check_price_check_gap: 동일 검증 ──
_order_seen_gap = {}


def _send_check_gap(text):
    _order_seen_gap["meta_at_send"] = _meta_via_fresh_conn("price_check_gap_warned_date")
    return True


settings.SETTINGS["price_check_gap_alert_minutes"] = 10
with db.connect(TEST_DB_ORDER) as conn:
    db.set_meta(conn, "last_check_at", str(_now_order - 20 * 60))

telegram.send = _send_check_gap
with db.connect(TEST_DB_ORDER) as conn:
    _r_order_gap = pc._check_price_check_gap(conn, _now_order, settings.get)
check("즉시수리(m2)-order-b: _check_price_check_gap - send 성공", _r_order_gap is True)
check("즉시수리(m2)-order-b(핵심): send 호출 시점에 이미 meta 가 별도 커넥션에서도 보인다",
      _order_seen_gap.get("meta_at_send") is not None)

# ── _check_outcome_chain: 불일치를 만들어 send 경로를 태운 뒤 동일 검증 ──
_order_seen_chain = {}


def _send_check_chain(text):
    _order_seen_chain["meta_at_send"] = _meta_via_fresh_conn(pc._OUTCOME_CHAIN_CHECK_META)
    return True


with db.connect(TEST_DB_ORDER) as conn:
    id_ord_chain = _insert_level_chain(conn, "ord_chain")
with db.connect(TEST_DB_ORDER) as conn:
    db.resolve_outcome(conn, id_ord_chain, "hit", 1500.0, "tp_sl", r_multiple=2.0,
                       now=time.time())
with db.connect(TEST_DB_ORDER) as conn:  # 사후 변조로 해시 불일치 강제 유발
    conn.execute("UPDATE levels SET r_multiple=? WHERE id=?", (99.9, id_ord_chain))

_now_order_chain = time.time()
_day_order_chain = pc._day_kst(_now_order_chain)
telegram.send = _send_check_chain
with db.connect(TEST_DB_ORDER) as conn:
    _r_order_chain = pc._check_outcome_chain(conn, _now_order_chain, _day_order_chain)
check("즉시수리(m2)-order-c: _check_outcome_chain - 불일치 감지 시 send 경로를 탄다",
      _r_order_chain is True)
check("즉시수리(m2)-order-c(핵심): send 호출 시점에 이미 meta 가 별도 커넥션에서도 보인다",
      _order_seen_chain.get("meta_at_send") is not None)

telegram.send = _orig_send_order
if os.path.exists(TEST_DB_ORDER):
    os.remove(TEST_DB_ORDER)


# ══════════════════════════════════════════════════════════════════
# CTO 지시(2026-07-27): 글 삭제 감지가 구조적으로 영원히 실행되지 않던 기아 수리.
# 수집 루프 도중 TradingView 차단이 걸리면(실측 26~61번째 심볼) 루프 '뒤'에 있던
# _check_deletions 는 첫 후보에서 is_blocked()==True 라 즉시 break → 다음 회차도
# 같은 자리에서 막혀 영원히 0건. 호출을 루프 '앞'으로 옮긴 것을 회귀로 못 박는다.
# ══════════════════════════════════════════════════════════════════
TEST_DB_DELORDER = "cache/_test_resilience_delorder.db"
if os.path.exists(TEST_DB_DELORDER):
    os.remove(TEST_DB_DELORDER)
settings.SETTINGS["db_path"] = TEST_DB_DELORDER
settings.SETTINGS["tv_fetch_sleep_sec"] = 0.0
settings.SETTINGS["deletion_check_daily_limit"] = 5
settings.SETTINGS["deletion_recheck_after_days"] = 30
db.init_db(TEST_DB_DELORDER)

# 종결(touched) 레벨 2건 = 삭제 확인 후보. (3건 이상이면 모듈 페이싱 1초가 누적돼
# 테스트가 느려진다 - 순서 검증에는 2건이면 충분)
with db.connect(TEST_DB_DELORDER) as conn:
    for i in range(2):
        conn.execute(
            """INSERT INTO levels (signal_key, coin_symbol, ticker, direction, entry_usd,
                 author, post_url, status, collected_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (f"ordkey{i}", "TST", "KRW-TST", "long", 1.0, "Auth",
             f"https://tv.example/ord{i}", "touched", time.time()))

_ORD_UNIVERSE = [
    {"symbol": f"O{i}", "ticker": f"KRW-O{i}", "rank": i + 1, "name": f"O{i} Coin",
     "price_usd": 10.0, "tier_icon": "🥈"} for i in range(5)
]
_ord_calls: list = []


def _ord_fetch_ideas(symbol, timeout, max_age_hours=None):
    _ord_calls.append(f"fetch:{symbol}")
    return []


# 실전 재현: 2번째 심볼부터 차단(수집 루프 중간에 403 → 30분 쿨다운)
def _ord_is_blocked():
    return sum(1 for c in _ord_calls if c.startswith("fetch:")) >= 2


_orig_check_deletions = run_collect._check_deletions


def _ord_check_deletions(conn, timeout):
    _ord_calls.append("delcheck")
    return _orig_check_deletions(conn, timeout)


coingecko.build_universe = lambda force=False: list(_ORD_UNIVERSE)
tradingview.fetch_ideas = _ord_fetch_ideas
tradingview.is_blocked = _ord_is_blocked
tradingview.hard_block_detected = lambda: None
tradingview.check_post_deleted = lambda url, timeout: False  # 전부 생존(요청은 스텁)
upbit_mod.fetch_prices = lambda tickers, timeout: {}
run_collect._check_deletions = _ord_check_deletions

_run_main(["run_collect.py"])
run_collect._check_deletions = _orig_check_deletions

with db.connect(TEST_DB_DELORDER) as conn:
    _n_checked_ord = conn.execute(
        "SELECT COUNT(*) AS n FROM levels WHERE deleted_checked_at IS NOT NULL"
    ).fetchone()["n"]
    _gate_ord = db.get_meta(conn, "last_deletion_check_day")

check("CTO수리: 삭제확인이 심볼 수집 루프보다 먼저 호출된다(순서 고정)",
      bool(_ord_calls) and _ord_calls[0] == "delcheck")
check("CTO수리(핵심): 루프 중간에 차단이 나도 삭제확인은 이미 끝난 뒤 - 후보 2건 확인됨",
      _n_checked_ord == 2)
check("CTO수리: 차단은 여전히 수집 루프를 조기 종료시킨다(회귀 아님)",
      sum(1 for c in _ord_calls if c.startswith("fetch:")) == 2)
check("CTO수리: 정상 순회 완료된 삭제확인은 하루 게이트를 소진한다", _gate_ord is not None)

# 회차 시작 시점에 이미 쿨다운이 걸려 있으면(직전 회차 30분 쿨다운 미종료) 기존대로
# break + 게이트 미소진 - 이건 정상 동작이라 바뀌면 안 된다.
TEST_DB_DELORDER2 = "cache/_test_resilience_delorder2.db"
if os.path.exists(TEST_DB_DELORDER2):
    os.remove(TEST_DB_DELORDER2)
settings.SETTINGS["db_path"] = TEST_DB_DELORDER2
db.init_db(TEST_DB_DELORDER2)
with db.connect(TEST_DB_DELORDER2) as conn:
    conn.execute(
        """INSERT INTO levels (signal_key, coin_symbol, ticker, direction, entry_usd,
             author, post_url, status, collected_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        ("ord2key", "TST", "KRW-TST", "long", 1.0, "Auth",
         "https://tv.example/ord2", "touched", time.time()))
tradingview.is_blocked = lambda: True
_run_main(["run_collect.py"])
with db.connect(TEST_DB_DELORDER2) as conn:
    _gate_pre = db.get_meta(conn, "last_deletion_check_day")
check("CTO수리: 회차 시작부터 쿨다운이면 기존대로 게이트 미소진(다음 회차 재시도)",
      _gate_pre is None)


# ══════════════════════════════════════════════════════════════════
# 카드14(2026-07-27): 텔레그램 공개채널 소스 — 인프라만, 기본 OFF·빈 화이트리스트.
# 네트워크 없이 실측 HTML 구조를 재현한 픽스처로 파서를 검증한다
# (t.me/s/durov · t.me/s/telegram 2026-07-27 실측 구조 — telegram_source 모듈
#  docstring 참고. 클래스명/속성은 실제 응답에서 그대로 옮겼다).
# ══════════════════════════════════════════════════════════════════
from collector import telegram_source as tgs
from datetime import datetime as _dt, timezone as _tz

# 픽스처 시각은 '실행 시점 기준 과거'로 만든다 - 고정 문자열로 박으면 시간이 지나며
# 의미가 바뀌고(미래 글 → 음수 age), 연령 필터 검증이 통과/실패를 오간다.
def _iso_ago(seconds: float) -> str:
    return _dt.fromtimestamp(time.time() - seconds, tz=_tz.utc).isoformat()


_TG_T101 = _iso_ago(7200)   # 2시간 전
_TG_T102 = _iso_ago(3600)   # 1시간 전 (더 최신 - 정렬 검증용)

_TG_FIXTURE = """
<main class="tgme_main"><section class="tgme_channel_history js-message_history">
<div class="tgme_widget_message_wrap js-widget_message_wrap">
<div class="tgme_widget_message" data-post="testchan/101">
  <div class="tgme_widget_message_text js-message_text" dir="auto">BTC 진입 60000<br/>TP 70000<br/>목표 도달 &amp; 관찰</div>
  <div class="tgme_widget_message_footer">
    <span class="tgme_widget_message_views">3.46M</span><span class="tgme_widget_message_meta">
    <a class="tgme_widget_message_date" href="https://t.me/testchan/101">
    <time datetime="__T101__" class="time">10:00</time></a></span>
  </div>
</div></div>
<div class="tgme_widget_message_wrap js-widget_message_wrap">
<div class="tgme_widget_message" data-post="testchan/102">
  <div class="tgme_widget_message_text js-message_text" dir="auto">ETH entry 3000 <div>중첩 div 안에 있는 본문</div> 꺼지 target 4000</div>
  <div class="tgme_widget_message_footer">
    <a class="tgme_widget_message_date" href="https://t.me/testchan/102">
    <time datetime="__T102__" class="time">11:30</time></a>
  </div>
</div></div>
<div class="tgme_widget_message_wrap js-widget_message_wrap">
<div class="tgme_widget_message" data-post="testchan/103">
  <div class="tgme_widget_message_footer">사진 전용 글(본문 div 없음)</div>
</div></div>
</section></main>
""".replace("__T101__", _TG_T101).replace("__T102__", _TG_T102)

_tg_orig_get = tgs._get
tgs._MIN_INTERVAL_SEC = 0.0   # 테스트에서 모듈 자체 페이싱 대기 제거


def _tg_stub_get(text, blocked=False):
    return lambda url, timeout, max_retry=2: (text, blocked)


# ── T1: HTML 파싱 (실측 구조) ────────────────────────────────────
tgs._get = _tg_stub_get(_TG_FIXTURE)
_tg_posts = tgs.fetch_posts("testchan", 5.0)
_tg_by_url = {p["url"]: p for p in _tg_posts}
check("카드14-T1: 본문 있는 글만 파싱(미디어 전용 103은 제외)",
      len(_tg_posts) == 2 and "https://t.me/testchan/103" not in _tg_by_url)
_p101 = _tg_by_url.get("https://t.me/testchan/101", {})
check("카드14-T1b: URL 은 data-post 로 조립(https://t.me/{채널}/{번호})", bool(_p101))
check("카드14-T1c: <br> 이 줄바꿈으로 보존되고 HTML 엔티티는 디코드된다",
      "60000\nTP 70000" in (_p101.get("description") or "")
      and "&" in (_p101.get("description") or "")
      and "&amp;" not in (_p101.get("description") or ""))
check("카드14-T1d: ISO8601 datetime → epoch 환산(published_at/age_minutes)",
      isinstance(_p101.get("published_at"), float)
      and _p101.get("age_minutes") is not None)
check("카드14-T1e: 작성자는 채널명(기존 작성자 축 통계가 그대로 흡수)",
      _p101.get("author") == "testchan")
check("카드14-T1f: 조회수 '3.46M' → 3460000 정수 환산", _p101.get("views") == 3460000)
check("카드14-T1g: fetch_ideas 계약 키를 전부 갖춘다(하류 무수정 재사용)",
      set(_p101) >= {"symbol", "title", "description", "author", "author_followers",
                     "url", "published_at", "age_minutes", "direction",
                     "likes_count", "comments_count", "ticker"})
_p102 = _tg_by_url.get("https://t.me/testchan/102", {})
check("카드14-T1h: 본문 div 안에 중첩 div 가 생겨도 본문이 잘리지 않는다(깊이 계산)",
      "target 4000" in (_p102.get("description") or ""))
check("카드14-T1i: 최신 글이 앞에 온다(TradingView 경로와 정렬 일치)",
      _tg_posts[0]["url"].endswith("/102"))

# 연령 필터: 시각을 못 뽑은 글은 관대 통과, 오래된 글은 제외
_tg_old = tgs.fetch_posts("testchan", 5.0, max_age_hours=0.001)
check("카드14-T1j: max_age_hours 초과 글은 제외(수집 단계 연령 게이트)", _tg_old == [])

# ── T2: 채널 상태 구분 (삭제 vs 비공개/미리보기 불가) ─────────────
_tg_handler = _ListLogHandler()
tgs.logger.addHandler(_tg_handler)
tgs.logger.setLevel(logging.DEBUG)

tgs._get = _tg_stub_get(
    '<div class="tgme_page_wrap"><div class="tgme_page_description">If you have '
    'Telegram, you can contact @gone right away.</div></div>')
_tg_gone = tgs.fetch_posts("gonechannel", 5.0)
_log_gone = "\n".join(_tg_handler.records)
check("카드14-T2: 삭제/없는 채널은 빈 목록 + '찾을 수 없음' 로그",
      _tg_gone == [] and "찾을 수 없음" in _log_gone)

_tg_handler.records.clear()
tgs._get = _tg_stub_get(
    '<div class="tgme_page_wrap"><div class="tgme_page_extra">98 700 members</div></div>')
_tg_priv = tgs.fetch_posts("privchannel", 5.0)
check("카드14-T2b: 존재하나 미리보기 불가(비공개 전환/그룹)는 다른 로그로 구분",
      _tg_priv == [] and "미리보기 불가" in "\n".join(_tg_handler.records))

_tg_handler.records.clear()
tgs._get = _tg_stub_get("<html><body>완전히 다른 구조</body></html>")
check("카드14-T2c: 알 수 없는 구조는 '스키마 변경 의심' 로그 + 빈 목록",
      tgs.fetch_posts("weird", 5.0) == []
      and "스키마 변경" in "\n".join(_tg_handler.records))
tgs.logger.removeHandler(_tg_handler)

# ── T3: 조회 실패/차단 격리 ──────────────────────────────────────
tgs._get = _tg_stub_get(None, blocked=True)
check("카드14-T3: 차단 신호면 빈 목록(예외 없음)", tgs.fetch_posts("testchan", 5.0) == [])
tgs._get = _tg_stub_get(None, blocked=False)
check("카드14-T3b: 조회 완전 실패도 빈 목록", tgs.fetch_posts("testchan", 5.0) == [])


def _tg_get_boom(url, timeout, max_retry=2):
    raise RuntimeError("예상외 오류(격리 검증용)")


tgs._get = _tg_get_boom
check("카드14-T3c: 예상외 예외도 삼키고 빈 목록(회차 생존 최우선)",
      tgs.fetch_posts("testchan", 5.0) == [])
tgs._get = _tg_stub_get(_TG_FIXTURE)
check("카드14-T3d: 채널명 형식 오류는 요청 자체를 생략",
      tgs.fetch_posts("https://t.me/s/bad", 5.0) == [] and tgs.fetch_posts("", 5.0) == [])

# ── T4: 심볼 해석 — 모호하면 버린다(엉뚱한 코인에 레벨을 붙이는 사고 방지) ──
_known = ["BTC", "ETH", "SOL", "XRP"]
check("카드14-T4: 심볼 1개만 등장하면 채택($/KRW-/USDT 표기 포함)",
      tgs.match_symbol("BTC 진입 60000", _known) == "BTC"
      and tgs.match_symbol("$SOL long", _known) == "SOL"
      and tgs.match_symbol("KRW-XRP 매수", _known) == "XRP"
      and tgs.match_symbol("ETHUSDT entry", _known) == "ETH")
check("카드14-T4b: 2개 이상 등장(시황글)은 None - 억지로 하나 고르지 않는다",
      tgs.match_symbol("BTC 와 ETH 둘 다 좋다", _known) is None)
check("카드14-T4c: 유니버스 밖/미언급은 None",
      tgs.match_symbol("오늘 시장은 조용합니다", _known) is None)
check("카드14-T4d: 소문자 일반단어는 코인으로 오인하지 않는다(sol/one 등)",
      tgs.match_symbol("solana ecosystem", _known) is None
      and tgs.match_symbol("BTCUSD", _known) == "BTC")
# 교차감사 즉시수리 #1 (2026-07-27): _PAIR 가 "Risk: 100 USD" 의 100 을 base 로
# 오인하면 그 거짓 base 하나가 '페어 있음' 판정을 만들어 전수 스캔 폴백까지 막고,
# 실제 티커(ETC)가 있는 글을 통째로 드롭했다. base 는 영문자 1자 이상을 요구한다.
check("카드14-T4e: 숫자-only 는 base 가 아니다(리스크 문구가 진짜 티커를 못 가림)",
      tgs.match_symbol("Buy ETH now. Risk: 100 USD per trade", _known) == "ETH"
      and tgs.match_symbol("GEM: $4/USDT breakout", _known) is None)

# ── T4f~T4k: 2차 교차검토 확정 _PAIR 통합수리 (2026-07-27) ────────────────
# T4e 의 수리는 "숫자-only base 배제"만 닫았고, 결함 부류 전체는 남아 있었다 —
# 옛 패턴의 `\s*[/\-]?\s*` 가 구분자를 통째로 생략 가능하게 해서 견적통화 바로
# 앞의 **아무 대문자 토큰**이나 base 로 승격됐다. 거짓 base 하나가 '페어 있음'
# 판정을 만들어 전수 스캔 폴백을 봉쇄 → 글이 통째로 드롭된다(손실 100%).
# 확정안: ① `$` 접두는 구분자 주변 공백 허용 ② 무접두는 붙여쓰기/무공백 구분자만
# ③ 레버리지는 정규식이 아니라 후처리(`^[0-9]{1,3}X$`)로 배제.
# 아래 6묶음이 그 계약 전부다. `_known` 을 그대로 쓰지 않는 이유: 실물 유니버스
# 심볼(2Z/USD1/ETC)이 있어야 T4i 의 '영문자 2자' 기각 근거를 증명할 수 있다.
_known_pair = ["BTC", "ETH", "SOL", "XRP", "ETC", "ID", "2Z", "USD1", "AERO"]

check("카드14-T4f: 레버리지 표기는 base 가 아니다(2X/5X/10X/20X - 후처리 배제)",
      all(tgs.match_symbol(t, _known_pair) == "ETH" for t in [
          "ETH LONG 2X USD margin",
          "ETH LONG. Leverage 5X USDT",
          "ETH 10X PERP",
          "Cross 20X USDT / ETH",
          "ETH LONG 10X/USDT",
          "ETH LONG 20XUSDT",
      ]))
check("카드14-T4g: 공백만으로는 페어가 아니다(대문자 상용구가 글을 못 죽인다)",
      all(tgs.match_symbol(t, _known_pair) == "ETH" for t in [
          "ETH LONG\nBALANCE USDT\nEntry 3000",
          "ETH LONG\nTAKE PROFIT IN USDT\nEntry 3000",
          "ETH LONG\nTAKE PROFIT - USDT\nEntry 3000",
          "ETH LONG\nSTOP LOSS / USD 2900",
          "ETH LONG. MARGIN USDT 500",
          "ETH setup. TOTAL PERP exposure",
      ]))
check("카드14-T4h: 진짜 페어 표기는 전부 그대로 통과(회귀)",
      tgs.match_symbol("COIN: $ETC/USDT\nEntry 6.8", _known_pair) == "ETC"
      and tgs.match_symbol("BTCUSDT long", _known_pair) == "BTC"
      and tgs.match_symbol("#BTCUSDT long", _known_pair) == "BTC"
      and tgs.match_symbol("ETH-USDT long", _known_pair) == "ETH"
      and tgs.match_symbol("$SOL / USDT long", _known_pair) == "SOL"
      and tgs.match_symbol("$1INCH/USDT long", _known_pair) is None)
# '영문자 2자 이상' 안을 기각한 증거 — 실물 유니버스(data/universe.json)에 2Z(영문자
# 1자)와 USD1 이 실제로 있다. 이 두 줄이 깨지면 그 안이 다시 들어온 것이다.
check("카드14-T4i: 실물 심볼 2Z/USD1 이 페어 경로를 잃지 않는다('영문자 2자' 안 기각)",
      tgs.match_symbol("$2Z/USDT long", _known_pair) == "2Z"
      and tgs.match_symbol("USD1/USDT long", _known_pair) == "USD1")
# 오귀속 방지 불변식: 미상장 코인의 페어 글은 **버려져야** 한다. 폴백을 허용하면
# 'SIGNAL ID:' 의 ID(업비트 상장)로 오귀속된다 — 07-27 실측 사고 그 자체.
check("카드14-T4j: 미추적 base 는 폴백을 봉쇄한 채 드롭(오귀속 방지 계약 유지)",
      tgs.match_symbol("COIN: $GRAM/USDT\nSIGNAL ID: 44", _known_pair) is None)
check("카드14-T4k: 숫자-only 회귀(T4e 계승) - 100 USD 는 base 가 아니고 $4/USDT 는 드롭",
      tgs.match_symbol("Buy ETH now. Risk: 100 USD per trade", _known_pair) == "ETH"
      and tgs.match_symbol("GEM: $4/USDT breakout", _known_pair) is None)

# ── T5: 기본 OFF·빈 화이트리스트면 아무 일도 일어나지 않는다(핵심 안전장치) ──
TEST_DB_TG = "cache/_test_resilience_telegram.db"
if os.path.exists(TEST_DB_TG):
    os.remove(TEST_DB_TG)
db.init_db(TEST_DB_TG)

_tg_fetch_calls = []


def _tg_fake_fetch(channel, timeout, max_age_hours=None, max_posts=20):
    _tg_fetch_calls.append(channel)
    return [{"title": "BTC 진입", "description": "BTC 진입 60000 손절 55000 목표 70000",
             "author": channel, "url": f"https://t.me/{channel}/1",
             "age_minutes": 5.0, "author_followers": None}]


run_collect.telegram_source.fetch_posts = _tg_fake_fetch
run_collect.telegram_source.is_blocked = lambda: False
_TG_UNIVERSE = [{"symbol": "BTC", "ticker": "KRW-BTC", "rank": 1, "name": "Bitcoin",
                 "price_usd": 60000.0, "tier_icon": "💎"}]

# 소스 코드 설정 검증 - 이 프로세스는 SETTINGS 를 덮어쓰므로 런타임 값이 아니라
# 소스 리터럴을 본다(수리9-R6 과 동일 패턴).
# 2026-07-27: 첫 채널을 실제로 켰다(BitmexSignalsFee — 126개 실사 후 승인). 그래서
# 이 두 검사는 "기본값이 OFF" 고정에서 **"켠 상태가 명시적으로 관리되는가"**로 바꾼다.
# 안전 속성 자체(꺼짐 또는 빈 목록이면 요청 0건)는 아래 T5c·T5d 가 그대로 지킨다.
_settings_src_tg = (_repo_root / "config" / "settings.py").read_text(encoding="utf-8")
_tg_on_src = '"telegram_source_enabled": True' in _settings_src_tg
check("카드14-T5: 켜져 있다면 화이트리스트가 비어 있지 않다(설정 실수 방지)",
      not _tg_on_src or '"telegram_source_channels": []' not in _settings_src_tg)
check("카드14-T5b: 꺼져 있다면 화이트리스트도 비어 있다(잔여 설정 방지)",
      _tg_on_src or '"telegram_source_channels": []' in _settings_src_tg)

settings.SETTINGS["telegram_source_enabled"] = False
settings.SETTINGS["telegram_source_channels"] = ["somechannel"]
settings.SETTINGS["telegram_source_sleep_sec"] = 0.0
settings.SETTINGS["telegram_source_max_posts"] = 20
with db.connect(TEST_DB_TG) as conn:
    _r_off = run_collect._collect_telegram(conn, _TG_UNIVERSE, {}, 5.0, 168)
check("카드14-T5c: enabled=False 면 채널이 있어도 요청 0건",
      _r_off == (0, 0, 0) and not _tg_fetch_calls)

settings.SETTINGS["telegram_source_enabled"] = True
settings.SETTINGS["telegram_source_channels"] = []
with db.connect(TEST_DB_TG) as conn:
    _r_empty = run_collect._collect_telegram(conn, _TG_UNIVERSE, {}, 5.0, 168)
check("카드14-T5d: 화이트리스트가 비면 켜져 있어도 요청 0건",
      _r_empty == (0, 0, 0) and not _tg_fetch_calls)

# ── T6: 켰을 때 정상 유입 + source 컬럼 + signal_key 충돌 방지 ──────────
settings.SETTINGS["telegram_source_channels"] = ["somechannel"]
run_collect.parse_setup = lambda text, current_price=None: {
    "direction": "long", "entry": 60000.0, "sl": 55000.0, "tp": 70000.0}
run_collect.calculate_grade = lambda *a, **k: ("B", 60, 2.0)
run_collect.judgment_window_hours = lambda *a, **k: 168.0
run_collect.parse_timeframe_hours = lambda text: None
with db.connect(TEST_DB_TG) as conn:
    _r_on = run_collect._collect_telegram(conn, _TG_UNIVERSE, {}, 5.0, 168)
with db.connect(TEST_DB_TG) as conn:
    _tg_rows = [dict(r) for r in conn.execute(
        "SELECT author, source, post_url, coin_symbol FROM levels").fetchall()]
check("카드14-T6: 켜면 채널 글이 정상 유입(글1 → 셋업1 → 신규1)", _r_on == (1, 1, 1))
check("카드14-T6b: 작성자는 채널명, 코인은 본문에서 해석된 BTC",
      len(_tg_rows) == 1 and _tg_rows[0]["author"] == "somechannel"
      and _tg_rows[0]["coin_symbol"] == "BTC")
check("카드14-T6c: levels.source 에 'telegram' 이 남는다(사후 소스별 분석 근거)",
      _tg_rows[0]["source"] == "telegram")

# signal_key: 같은 (코인/엔트리/작성자/URL) 이라도 소스가 다르면 다른 키.
# 그리고 기본값(tradingview)은 접두사를 붙이지 않아 **기존 DB 키가 불변**이어야 한다
# (붙이면 이미 쌓인 레벨 전부가 '신규'로 재삽입돼 중복 알림이 터진다).
_k_tv = db.make_signal_key("BTC", 60000.0, "author", "https://x/1")
_k_tv_explicit = db.make_signal_key("BTC", 60000.0, "author", "https://x/1",
                                    source="tradingview")
_k_tg = db.make_signal_key("BTC", 60000.0, "author", "https://x/1", source="telegram")
check("카드14-T6d: 소스가 다르면 signal_key 도 다르다(중복 저장·오알림 방지)",
      _k_tv != _k_tg)
check("카드14-T6e(회귀): 기본값과 명시 'tradingview' 는 같은 키", _k_tv == _k_tv_explicit)
_expected_tv_key = hashlib.sha1(
    "BTC|60000.000000|author|https://x/1".encode("utf-8")).hexdigest()[:16]
check("카드14-T6f(회귀): tradingview 키는 접두사 없는 원문 해시 그대로 "
      "(기존 DB 수천 건이 '신규'로 재삽입되면 중복 알림 폭발)",
      _k_tv == _expected_tv_key)

# 두 번째 호출은 같은 키라 신규가 아니다(멱등 - 회차마다 중복 저장되지 않는다)
with db.connect(TEST_DB_TG) as conn:
    _r_again = run_collect._collect_telegram(conn, _TG_UNIVERSE, {}, 5.0, 168)
check("카드14-T6g: 같은 글 재수집은 신규 0건(업서트 멱등)", _r_again == (1, 1, 0))

# ── T7: source 컬럼 마이그레이션 (기존 DB 무중단) ────────────────────
TEST_DB_TGMIG = "cache/_test_resilience_tg_migrate.db"
if os.path.exists(TEST_DB_TGMIG):
    os.remove(TEST_DB_TGMIG)
# SCHEMA(CREATE TABLE)에는 source 가 없다 - 즉 이 경로가 곧 '구세대 DB' 재현이고,
# init_db 의 PRAGMA table_info → ALTER 마이그레이션이 실제로 컬럼을 붙여야 한다.
with db.connect(TEST_DB_TGMIG) as conn:
    conn.executescript(db.SCHEMA)
    conn.execute(
        """INSERT INTO levels (signal_key, coin_symbol, ticker, direction, entry_usd,
             author, post_url, status, collected_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        ("legacy_src", "BTC", "KRW-BTC", "long", 60000.0, "OldAuthor",
         "https://tv.example/legacy", "watching", time.time()))
with db.connect(TEST_DB_TGMIG) as conn:
    _cols_before = {r["name"] for r in conn.execute("PRAGMA table_info(levels)").fetchall()}
check("카드14-T7 사전조건: 구세대 스키마엔 source 컬럼이 없다",
      "source" not in _cols_before)

db.init_db(TEST_DB_TGMIG)
with db.connect(TEST_DB_TGMIG) as conn:
    _cols_after = {r["name"] for r in conn.execute("PRAGMA table_info(levels)").fetchall()}
    _legacy_src = conn.execute(
        "SELECT source FROM levels WHERE signal_key='legacy_src'").fetchone()["source"]
check("카드14-T7b: init_db 가 source 컬럼을 ALTER 로 추가", "source" in _cols_after)
check("카드14-T7c: 기존 행은 DEFAULT 'tradingview' 로 소급 분류(사후 분석 정확도)",
      _legacy_src == "tradingview")

# 재실행 멱등 - 이미 있는 컬럼을 다시 ALTER 하지 않는다(예외 없이 통과)
db.init_db(TEST_DB_TGMIG)
check("카드14-T7d: init_db 재호출도 예외 없이 통과(마이그레이션 멱등)", True)

tgs._get = _tg_orig_get


# ══════════════════════════════════════════════════════════════════
# 교차감사 M1(2026-07-27, 개발자A): 수집이 회차 전체를 단일 트랜잭션으로 감싸던 문제.
# run_cycle.py 가 수집을 subprocess timeout(12분)으로 **하드킬**하면 열린 트랜잭션이
# 통째로 롤백돼 그 회차 수집분 전량 + 순환 offset 까지 증발했고, 다음 회차가 같은
# 지점을 다시 돌았다(무한 제자리걸음). 지금 안 터지는 유일한 이유가 "매번 차단으로
# 일찍 끊겨서"라 차단이 완화될수록 폭탄에 가까워지는 구조였다.
# 수리: 심볼 N개(collect_commit_every_symbols)마다 중간 커밋 + 그 커밋에 재개 지점을
# 함께 기록. 하드킬은 여기서 '수집 루프 도중 예외'로 재현한다 — 어느 쪽이든 커밋되지
# 않은 트랜잭션이 롤백된다는 점에서 DB 관점의 결과가 같다.
# ══════════════════════════════════════════════════════════════════
TEST_DB_M1 = "cache/_test_resilience_m1_commit.db"
if os.path.exists(TEST_DB_M1):
    os.remove(TEST_DB_M1)
settings.SETTINGS["db_path"] = TEST_DB_M1
settings.SETTINGS["tv_fetch_sleep_sec"] = 0.0
settings.SETTINGS["collect_commit_every_symbols"] = 3
settings.SETTINGS["telegram_source_enabled"] = False   # 이 절은 TradingView 경로만 본다
db.init_db(TEST_DB_M1)

_M1_UNIVERSE = [
    {"symbol": f"M{i}", "ticker": f"KRW-M{i}", "rank": i + 1, "name": f"M{i} Coin",
     "price_usd": 10.0, "tier_icon": "🥈"} for i in range(10)
]
_m1_fetched: list = []
_m1_seen_mid = {}
_M1_KILL_AT = "M7"   # 커밋 경계(3·6·9개째)가 아닌 지점에서 죽여 '마지막 배치' 손실 재현


def _m1_fetch_ideas(symbol, timeout, max_age_hours=None):
    _m1_fetched.append(symbol)
    # 커밋 #2(6개째 처리 직후) 시점에 **별도 커넥션**으로 관측한다. 진행 중인
    # 트랜잭션은 다른 커넥션에 보이지 않으므로, 여기서 6건이 보인다는 것은
    # "set 만 됐다"가 아니라 "commit 까지 디스크에 반영됐다"는 증거다.
    if symbol == "M6" and "rows" not in _m1_seen_mid:
        _c = _sqlite3.connect(TEST_DB_M1, timeout=5)
        try:
            _m1_seen_mid["rows"] = _c.execute("SELECT COUNT(*) FROM levels").fetchone()[0]
            _row = _c.execute("SELECT value FROM meta WHERE key=?",
                              (run_collect._UNIVERSE_OFFSET_META,)).fetchone()
            _m1_seen_mid["offset"] = _row[0] if _row else None
        finally:
            _c.close()
    # _ingest_idea 는 자체 try/except 로 예외를 삼키므로(글 1건 격리 원칙), 격리 밖인
    # fetch 단계에서 던져야 예외가 main() 밖으로 나가 트랜잭션이 롤백된다.
    if symbol == _M1_KILL_AT:
        raise RuntimeError("하드킬 재현(중간 커밋 검증용)")
    return [{"title": f"{symbol} idea", "description": "x", "author": f"A{symbol}",
             "url": f"https://tv.example/{symbol}", "age_minutes": 5.0,
             "author_followers": 100}]


coingecko.build_universe = lambda force=False: list(_M1_UNIVERSE)
tradingview.fetch_ideas = _m1_fetch_ideas
tradingview.is_blocked = lambda: False
tradingview.hard_block_detected = lambda: None
tradingview.fetch_author_followers = lambda username, timeout: 100
upbit_mod.fetch_prices = lambda tickers, timeout: {}
run_collect.parse_setup = lambda text, current_price=None: {
    "direction": "long", "entry": 10.0, "sl": 9.0, "tp": 12.0}
run_collect.calculate_grade = lambda *a, **k: ("B", 60, 2.0)
run_collect.judgment_window_hours = lambda *a, **k: 168.0
run_collect.parse_timeframe_hours = lambda text: None

_m1_raised = False
try:
    _run_main(["run_collect.py"])
except RuntimeError:
    _m1_raised = True

with db.connect(TEST_DB_M1) as conn:
    _m1_rows = [r["coin_symbol"] for r in conn.execute(
        "SELECT coin_symbol FROM levels ORDER BY id").fetchall()]
    _m1_off = db.get_meta(conn, run_collect._UNIVERSE_OFFSET_META, "0")

check("M1-1: 하드킬 재현 - 수집 루프 도중 예외가 main() 밖으로 나간다(미커밋 종료)",
      _m1_raised)
check("M1-2(핵심): 중간 커밋된 6건(M0~M5)이 롤백에서 살아남는다 "
      "- 예전엔 단일 트랜잭션이라 그 회차 수집분 전량이 증발했다",
      _m1_rows == [f"M{i}" for i in range(6)])
check("M1-3: 잃는 건 마지막 배치(커밋 전 M6)뿐 - 손실 범위가 유계",
      "M6" not in _m1_rows)
check("M1-4: 커밋이 루프 '도중' 실제 디스크까지 반영된다(별도 커넥션에서 6건 관측)",
      _m1_seen_mid.get("rows") == 6)
check("M1-5: 재개 지점이 중단 지점(M6 = 원본 기준 절대 인덱스 6)을 가리킨다 "
      "- 살아남은 구간을 다시 돌지도, 못 받은 구간을 건너뛰지도 않는다", _m1_off == "6")
check("M1-5b: 재개 지점도 수집분과 '같은' 중간 커밋에 실린다(둘의 불일치 불가)",
      _m1_seen_mid.get("offset") == "6")

# 다음 회차: 중단 지점부터 이어받고, 완주하면 0 리셋(기존 순환 계약 유지) + 중복 0
_M1_KILL_AT = ""
_m1_fetched.clear()
_run_main(["run_collect.py"])
with db.connect(TEST_DB_M1) as conn:
    _m1_rows2 = [r["coin_symbol"] for r in conn.execute(
        "SELECT coin_symbol FROM levels ORDER BY id").fetchall()]
    _m1_off2 = db.get_meta(conn, run_collect._UNIVERSE_OFFSET_META, "0")
    _m1_dup = conn.execute(
        "SELECT COUNT(*) AS n FROM (SELECT signal_key FROM levels "
        "GROUP BY signal_key HAVING COUNT(*) > 1)").fetchone()["n"]
check("M1-6: 다음 회차는 중단 지점(M6)부터 이어받는다(하드킬로도 순환이 안 끊긴다)",
      _m1_fetched == ["M6", "M7", "M8", "M9", "M0", "M1", "M2", "M3", "M4", "M5"])
check("M1-7: 완주 시 순환 지점 0 리셋 유지(중간 커밋 도입이 기존 계약을 안 깬다)",
      _m1_off2 == "0")
check("M1-8: 살아남은 구간(M0~M5)을 다시 수집해도 중복 삽입 0 - signal_key UNIQUE + "
      "upsert_level 의 키 조회→UPDATE 경로가 멱등성을 보장",
      _m1_dup == 0 and len(_m1_rows2) == 10
      and set(_m1_rows2) == {f"M{i}" for i in range(10)})

# ── 중간 커밋 #1: 삭제확인 결과는 수집 루프가 죽어도 살아남는다 ──────────────
# 삭제확인은 하루 상한 5요청짜리 희소 자원이라, 수집 하드킬에 딸려 롤백되면
# 그날치 확인이 통째로 사라진다. '진전 게이트'(B의 A-M2 수리)와의 무모순도 함께 확인 —
# deleted_checked_at 과 게이트가 같은 커밋에 들어가므로 한쪽만 남을 수 없다.
TEST_DB_M1DEL = "cache/_test_resilience_m1_delcommit.db"
if os.path.exists(TEST_DB_M1DEL):
    os.remove(TEST_DB_M1DEL)
settings.SETTINGS["db_path"] = TEST_DB_M1DEL
db.init_db(TEST_DB_M1DEL)
with db.connect(TEST_DB_M1DEL) as conn:
    conn.execute(
        """INSERT INTO levels (signal_key, coin_symbol, ticker, direction, entry_usd,
             author, post_url, status, collected_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        ("m1delkey", "TST", "KRW-TST", "long", 1.0, "Auth",
         "https://tv.example/m1del", "touched", time.time()))

tradingview.check_post_deleted = lambda url, timeout: False   # 생존 판정 = 진전 있음
_M1_KILL_AT = "M0"      # 첫 심볼부터 하드킬 - 수집분은 0건
_m1_fetched.clear()
try:
    _run_main(["run_collect.py"])
except RuntimeError:
    pass
with db.connect(TEST_DB_M1DEL) as conn:
    _m1_del_checked = conn.execute(
        "SELECT COUNT(*) AS n FROM levels WHERE deleted_checked_at IS NOT NULL"
    ).fetchone()["n"]
    _m1_del_gate = db.get_meta(conn, "last_deletion_check_day")
check("M1-9: 수집 루프가 첫 심볼에서 죽어도 삭제확인 결과는 이미 커밋돼 살아남는다 "
      "(하루 5요청짜리 희소 자원을 롤백으로 날리지 않는다)", _m1_del_checked == 1)
check("M1-9b: 근거(deleted_checked_at)와 하루 게이트가 같은 커밋에 함께 남는다 "
      "- '게이트만 타고 진전은 없음'이 구조적으로 불가(진전 게이트와 무모순)",
      _m1_del_gate is not None)


# ── 정리 ──────────────────────────────────────────────────────────
for _p in (TEST_DB_RC, TEST_DB_DEL, TEST_DB_EMPTY, TEST_DB_CHAIN, TEST_DB_LEGACY,
           TEST_DB_CHAIN_CYCLE, TEST_DB_GAP, TEST_DB_DELORDER, TEST_DB_DELORDER2,
           TEST_DB_TG, TEST_DB_TGMIG, TEST_DB_M1, TEST_DB_M1DEL):
    try:
        if os.path.exists(_p):
            os.remove(_p)
    except Exception:
        pass

print(f"\n{n_checks}건 중 {'전부' if ok else '일부 실패'} 통과")
sys.exit(0 if ok else 1)
