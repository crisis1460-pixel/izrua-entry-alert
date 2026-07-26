# 철야 수리(2026-07-26) 회귀 테스트 — 수리 1~5·7 검증. 네트워크 호출 없이 전부
# 몽키패치(requests.get/post, 모듈 함수)로 오프라인 검증한다(기존 test_price_logic.py/
# test_cycle.py 스텁 패턴 참고). 다른 test_*.py 와 동일 원칙 — settings.SETTINGS 를
# 전역으로 덮어쓰므로 반드시 독립 프로세스로 실행한다(한 프로세스에 모으면 서로 오염).
import os
import sys
import time
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
check("수리7: ::error:: 직후 exit 1 로 잡을 실제로 실패시킨다", "exit 1" in _after_error[:200])


# ── 정리 ──────────────────────────────────────────────────────────
for _p in (TEST_DB_RC, TEST_DB_DEL, TEST_DB_EMPTY):
    try:
        if os.path.exists(_p):
            os.remove(_p)
    except Exception:
        pass

print(f"\n{n_checks}건 중 {'전부' if ok else '일부 실패'} 통과")
sys.exit(0 if ok else 1)
