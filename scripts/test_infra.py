# 인프라 개선(2026-08-13 commit f7716471) 유틸 함수 단위 테스트.
# 네트워크 호출 없이 몽키패치/인메모리 DB 로 오프라인 검증.
import json
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import logging
logging.basicConfig(level=logging.WARNING)

ok = True
n_checks = 0


def check(name, cond):
    global ok, n_checks
    n_checks += 1
    print(("✅" if cond else "❌"), name)
    if not cond:
        ok = False


# ─── iso_to_epoch ────────────────────────────────────────────────────

from utils.time_kst import iso_to_epoch

check("iso_to_epoch: UTC Z-suffix",
      abs(iso_to_epoch("2026-01-01T00:00:00Z") - 1767225600.0) < 1)

check("iso_to_epoch: +00:00 suffix",
      abs(iso_to_epoch("2026-01-01T00:00:00+00:00") - 1767225600.0) < 1)

check("iso_to_epoch: +09:00 KST",
      abs(iso_to_epoch("2026-01-01T09:00:00+09:00") - 1767225600.0) < 1)

check("iso_to_epoch: None input → None",
      iso_to_epoch(None) is None)

check("iso_to_epoch: empty string → None",
      iso_to_epoch("") is None)

check("iso_to_epoch: garbage → None",
      iso_to_epoch("not-a-date") is None)

check("iso_to_epoch: int input → None",
      iso_to_epoch(12345) is None)


# ─── prune_alerts_log ────────────────────────────────────────────────

from storage import db

def _make_test_db():
    conn = sqlite3.connect(":memory:")
    conn.execute("""
        CREATE TABLE alerts_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            coin_symbol TEXT NOT NULL,
            kind TEXT NOT NULL,
            level_ids TEXT,
            sent_at REAL NOT NULL,
            day_kst TEXT NOT NULL
        )
    """)
    return conn

now = time.time()

conn = _make_test_db()
conn.execute(
    "INSERT INTO alerts_log (coin_symbol,kind,level_ids,sent_at,day_kst) VALUES (?,?,?,?,?)",
    ("BTC", "touch", "1", now - 86400 * 40, "2026-07-01"),
)
conn.execute(
    "INSERT INTO alerts_log (coin_symbol,kind,level_ids,sent_at,day_kst) VALUES (?,?,?,?,?)",
    ("ETH", "preview", "2", now - 86400 * 10, "2026-08-01"),
)
conn.commit()

deleted = db.prune_alerts_log(conn, keep_days=30)
check("prune_alerts_log: 40일 전 행 삭제됨", deleted == 1)

remaining = conn.execute("SELECT COUNT(*) FROM alerts_log").fetchone()[0]
check("prune_alerts_log: 10일 전 행 보존됨", remaining == 1)

conn2 = _make_test_db()
deleted2 = db.prune_alerts_log(conn2)
check("prune_alerts_log: 빈 테이블 → 0건", deleted2 == 0)

conn.close()
conn2.close()


# ─── _split_send (telegram) ──────────────────────────────────────────

import notify.telegram as tg

short_msg = "짧은 메시지"
# 반환 타입 (2026-08-17 #6): Optional[int]. 성공=message_id(양수), 실패=None.
with patch.object(tg, "send", return_value=1) as mock_send:
    result = tg._split_send(short_msg, "low")
    check("_split_send: 짧은 메시지 → send 1회", mock_send.call_count == 1)
    check("_split_send: 짧은 메시지 → 첫 msg_id 반환", result == 1)

long_lines = [f"라인{i:04d} " + "x" * 80 for i in range(100)]
long_msg = "\n".join(long_lines)
assert len(long_msg) > 4096
with patch.object(tg, "send", return_value=42) as mock_send:
    with patch("time.sleep"):
        result = tg._split_send(long_msg, "high")
    check("_split_send: 긴 메시지 → 다건 분할", mock_send.call_count >= 2)
    check("_split_send: 전부 성공 → 첫 msg_id 반환", result == 42)
    for call_args in mock_send.call_args_list:
        chunk = call_args[0][0]
        check(f"_split_send: 청크 ≤4096 ({len(chunk)}자)", len(chunk) <= 4096)

with patch.object(tg, "send", side_effect=[10, None, 30]) as mock_send:
    with patch("time.sleep"):
        result = tg._split_send(long_msg, "low")
    check("_split_send: 일부 실패 → None", result is None)


# ─── #6 preview→touch 스레딩 (2026-08-17) ─────────────────────────────
# preview_message_id 저장/조회 함수 격리 검증. run_once 통합 테스트는
# test_price_logic 의 다른 경로가 커버(스텁 send 가 reply_to_message_id 수용).
import tempfile, os
from storage import db as _dbmod
_thread_db = tempfile.NamedTemporaryFile(delete=False, suffix=".db").name
try:
    _dbmod.init_db(_thread_db)
    with _dbmod.connect(_thread_db) as conn:
        cur = conn.execute(
            "INSERT INTO levels (coin_symbol, ticker, entry_usd, direction, status, "
            "signal_key, collected_at) VALUES ('X','KRW-X',1.0,'long','watching','k',900)")
        _lid = cur.lastrowid
        conn.commit()
        check("#6a get_preview_message_id — 미저장 상태 None",
              _dbmod.get_preview_message_id(conn, [_lid]) is None)
        _dbmod.set_preview_message_id(conn, [_lid], 55555)
        conn.commit()
        check("#6b set/get preview_message_id — 저장 후 정확 조회",
              _dbmod.get_preview_message_id(conn, [_lid]) == 55555)
        _dbmod.set_preview_message_id(conn, [_lid], 99999)
        conn.commit()
        check("#6c IS NULL 가드 — 재저장 무시(첫 값 유지, 재발송 경합 대비)",
              _dbmod.get_preview_message_id(conn, [_lid]) == 55555)
finally:
    os.unlink(_thread_db)


# ─── #5 등급×장세 히트맵 (2026-08-17) ─────────────────────────────────
# 렌더 격리(표본 부족 자동 스킵) + DB 집계(regime 분류) 검증.
_hm_small = {"cells": {("A", "trend"): {"n": 3, "hit": 0.5, "mfe": 5.0}}}
check("#5a 셀 n<5 이면 섹션 통째 스킵(표본 도달 전 자동 침묵)",
      tg._regime_heatmap_section(_hm_small) == [])
_hm_ok = {"cells": {
    ("S", "trend"): {"n": 8, "hit": 0.75, "mfe": 12.0},
    ("A", "range"): {"n": 5, "hit": 0.20, "mfe": 3.0},
}}
_hm_lines = tg._regime_heatmap_section(_hm_ok)
check("#5b n>=5 셀만 표시 (S/trend + A/range 표시)",
      any("75%" in l for l in _hm_lines) and any("20%" in l for l in _hm_lines))

_hm_db = tempfile.NamedTemporaryFile(delete=False, suffix=".db").name
try:
    _dbmod.init_db(_hm_db)
    with _dbmod.connect(_hm_db) as conn:
        # A/trend(ADX 30) 승·패 각 1건, A/squeeze(BB 15) 승 1건
        for _adx, _bbp, _out, _mfe in [(30, 15, "hit", 10), (30, 50, "miss", -2)]:
            conn.execute(
                "INSERT INTO levels (coin_symbol, ticker, entry_usd, direction, status, "
                "signal_key, collected_at, touch_grade, touch_adx14, "
                "touch_bb_width_pctile, outcome, resolved_at, mfe_pct) "
                "VALUES ('X','KRW-X',1.0,'long','touched',?,900,'A',?,?,?,1000,?)",
                (f"k{_adx}{_bbp}{_out}", _adx, _bbp, _out, _mfe))
        conn.commit()
        _hm = _dbmod.get_regime_heatmap(conn)
        check("#5c DB 집계 — ADX>=25 → trend 셀, ADX<20 → range, BB<=20 → squeeze 병립",
              ("A", "trend") in _hm["cells"] and _hm["cells"][("A", "trend")]["n"] == 2
              and _hm["cells"][("A", "trend")]["hit"] == 0.5
              and ("A", "squeeze") in _hm["cells"]
              and _hm["cells"][("A", "squeeze")]["n"] == 1)
finally:
    os.unlink(_hm_db)


# ─── _fetch_fapi_ratio (binance) ─────────────────────────────────────

from monitor.binance import _fetch_fapi_ratio

mock_resp = MagicMock()
mock_resp.status_code = 200
mock_resp.json.return_value = [{"longShortRatio": "1.25"}]

with patch("requests.get", return_value=mock_resp):
    val = _fetch_fapi_ratio("globalLongShortAccountRatio", "BTC",
                            "longShortRatio", 5.0, "LS비율")
    check("_fetch_fapi_ratio: 정상 응답 → float", abs(val - 1.25) < 0.001)

mock_resp_404 = MagicMock()
mock_resp_404.status_code = 404
with patch("requests.get", return_value=mock_resp_404):
    val = _fetch_fapi_ratio("globalLongShortAccountRatio", "NOPE",
                            "longShortRatio", 5.0, "LS비율")
    check("_fetch_fapi_ratio: 404 → None", val is None)

mock_resp_empty = MagicMock()
mock_resp_empty.status_code = 200
mock_resp_empty.json.return_value = []
with patch("requests.get", return_value=mock_resp_empty):
    val = _fetch_fapi_ratio("globalLongShortAccountRatio", "BTC",
                            "longShortRatio", 5.0, "LS비율")
    check("_fetch_fapi_ratio: 빈 응답 → None", val is None)

with patch("requests.get", side_effect=Exception("timeout")):
    val = _fetch_fapi_ratio("globalLongShortAccountRatio", "BTC",
                            "longShortRatio", 5.0, "LS비율")
    check("_fetch_fapi_ratio: 예외 → None", val is None)


# ─── _save_json_cache (coingecko atomic write) ───────────────────────

from collector.coingecko import _save_json_cache, _save_cache

with tempfile.TemporaryDirectory() as td:
    path = os.path.join(td, "sub", "cache.json")
    data = {"key": "value", "num": 42}
    _save_json_cache(path, data)
    check("_save_json_cache: 파일 생성됨", os.path.exists(path))
    with open(path, encoding="utf-8") as f:
        loaded = json.load(f)
    check("_save_json_cache: 내용 일치", loaded == data)
    check("_save_json_cache: tmp 파일 없음",
          not os.path.exists(path.replace(".json", ".tmp")))


# ─── _save_cache (empty guard) ───────────────────────────────────────

with tempfile.TemporaryDirectory() as td:
    path = os.path.join(td, "universe.json")
    _save_cache(path, [])
    check("_save_cache: 빈 리스트 → 파일 미생성", not os.path.exists(path))

    universe = [{"symbol": "BTC"}, {"symbol": "ETH"}]
    _save_cache(path, universe)
    check("_save_cache: 정상 리스트 → 파일 생성됨", os.path.exists(path))
    with open(path, encoding="utf-8") as f:
        loaded = json.load(f)
    check("_save_cache: universe 키 존재", "universe" in loaded)
    check("_save_cache: 내용 일치", loaded["universe"] == universe)


# ─── derive_supply_verdict CVD·호가 보정 (2026-08-14) ────────────────

from monitor.binance import derive_supply_verdict

# 기준: 보정 입력 없으면 종전 판정 그대로
v = derive_supply_verdict(0.005, 5.0, 2.0)
check("수급보정: 기본(자금 유입=우호)", v == ("우호", "자금 유입"))

# 우호 + CVD 매도 우위 → 중립 강등 (reason 유지)
v = derive_supply_verdict(0.005, 5.0, 2.0, cvd_ratio=-0.2)
check("수급보정: 우호+CVD매도 → 중립", v == ("중립", "자금 유입"))

# 우호 + 매도벽 → 중립 강등
v = derive_supply_verdict(0.005, 5.0, 2.0, bid_ask_ratio=0.5)
check("수급보정: 우호+매도벽 → 중립", v == ("중립", "자금 유입"))

# 중립 + 경고 2개 → 주의 강등
v = derive_supply_verdict(0.005, 5.0, -2.0, cvd_ratio=-0.2, bid_ask_ratio=0.5)
check("수급보정: 중립+경고2 → 주의", v[0] == "주의")

# 중립 + 확인 2개 → 우호 상향 (둘 다 필요)
v = derive_supply_verdict(0.005, None, None, cvd_ratio=0.2, bid_ask_ratio=2.0)
check("수급보정: 중립+확인2 → 우호", v[0] == "우호")

# 중립 + 확인 1개만 → 상향 없음 (보수 원칙)
v = derive_supply_verdict(0.005, None, None, cvd_ratio=0.2)
check("수급보정: 중립+확인1 → 유지", v[0] == "중립")

# 주의는 보정으로 좋아지지 않음
v = derive_supply_verdict(0.02, 5.0, 2.0, cvd_ratio=0.5, bid_ask_ratio=3.0)
check("수급보정: 주의는 상향 불가", v == ("주의", "추격 위험"))

# 임계 미만 보정값은 무시 (경계 안쪽)
v = derive_supply_verdict(0.005, 5.0, 2.0, cvd_ratio=-0.1, bid_ask_ratio=0.8)
check("수급보정: 임계 미만은 무시", v == ("우호", "자금 유입"))

# 전부 None 이면 종전대로 (None, None)
v = derive_supply_verdict(None, None, None, cvd_ratio=0.5, bid_ask_ratio=2.0)
check("수급보정: 본판정 없으면 보정도 없음", v == (None, None))


# ─── push_kimchi_history (2026-08-14) ────────────────────────────────

_kc = sqlite3.connect(":memory:")
_kc.row_factory = sqlite3.Row
_kc.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
_now0 = time.time()

d = db.push_kimchi_history(_kc, _now0 - 6 * 3600, 2.0)
check("김프이력: 첫 기록 → 델타 None", d is None)

d = db.push_kimchi_history(_kc, _now0, 2.8)
check("김프이력: 6h 전 대비 +0.8 델타", d is not None and abs(d - 0.8) < 0.001)

# 창 밖(13h 전) 기록만 있으면 델타 없음 + prune 확인
_kc2 = sqlite3.connect(":memory:")
_kc2.row_factory = sqlite3.Row
_kc2.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
db.push_kimchi_history(_kc2, _now0 - 13 * 3600, 1.0)
d = db.push_kimchi_history(_kc2, _now0, 3.0)
check("김프이력: 13h 전 기록은 창 밖 → None", d is None)
hist = json.loads(db.get_meta(_kc2, "kimchi_hist"))
check("김프이력: 12h 초과분 prune", len(hist) == 1)
_kc.close()
_kc2.close()


# ─── render_alert 김프 화살표 (2026-08-14) ──────────────────────────

_cluster = [{"coin_symbol": "BTC", "entry_usd": 100.0, "score": 50,
             "grade": "B", "author": "tester"}]
_txt = tg.render_alert("touch", "BTC", _cluster, 140000.0, 1400.0,
                       kimchi_pct=2.15, kimchi_delta=0.8)
check("김프화살표: 급변 시 ▲ 표시", "김프 +2.15% ▲" in _txt)

_txt = tg.render_alert("touch", "BTC", _cluster, 140000.0, 1400.0,
                       kimchi_pct=2.15, kimchi_delta=-0.7)
check("김프화살표: 급락 시 ▼ 표시", "김프 +2.15% ▼" in _txt)

_txt = tg.render_alert("touch", "BTC", _cluster, 140000.0, 1400.0,
                       kimchi_pct=2.15, kimchi_delta=0.3)
check("김프화살표: 임계 미만 → 없음", "김프 +2.15%\n" in _txt or _txt.rstrip().endswith("김프 +2.15%") or ("김프 +2.15%" in _txt and "▲" not in _txt))

_txt = tg.render_alert("touch", "BTC", _cluster, 140000.0, 1400.0,
                       kimchi_pct=2.15)
check("김프화살표: 델타 미전달 → 종전 표기", "김프 +2.15%" in _txt and "▲" not in _txt and "▼" not in _txt)


# ─── derive_supply_verdict 옵션·청산 보정 (2026-08-14) ───────────────

# 옵션 P/C 극단 HIGH (≥1.0) → 경고
v = derive_supply_verdict(0.005, 5.0, 2.0,
                          options_ctx={"pc_ratio": 1.2, "max_pain": 100000})
check("옵션보정: P/C 극단HIGH(1.2) → 우호가 중립으로", v[0] == "중립")

# 옵션 P/C 극단 LOW (≤0.30) → 경고
v = derive_supply_verdict(0.005, 5.0, 2.0,
                          options_ctx={"pc_ratio": 0.25, "max_pain": 100000})
check("옵션보정: P/C 극단LOW(0.25) → 우호가 중립으로", v[0] == "중립")

# 옵션 P/C 정상 범위 (0.55) → 보정 없음
v = derive_supply_verdict(0.005, 5.0, 2.0,
                          options_ctx={"pc_ratio": 0.55, "max_pain": 100000})
check("옵션보정: P/C 정상(0.55) → 우호 유지", v[0] == "우호")

# 청산 long_heavy → 경고
v = derive_supply_verdict(0.005, 5.0, 2.0,
                          liq_ctx={"pressure": 70, "direction": "long_heavy"})
check("청산보정: long_heavy → 우호가 중립으로", v[0] == "중립")

# 청산 short_heavy → 확인 (중립 + 확인2개 시 상향)
v = derive_supply_verdict(0.005, None, None, cvd_ratio=0.2,
                          liq_ctx={"pressure": 30, "direction": "short_heavy"})
check("청산보정: short_heavy+CVD확인 → 중립이 우호로", v[0] == "우호")

# 청산 neutral → 보정 없음
v = derive_supply_verdict(0.005, 5.0, 2.0,
                          liq_ctx={"pressure": 50, "direction": "neutral"})
check("청산보정: neutral → 우호 유지", v[0] == "우호")

# 옵션+청산 동시 경고 → 중립도 주의로
v = derive_supply_verdict(0.005, None, None,
                          options_ctx={"pc_ratio": 0.2, "max_pain": 100000},
                          liq_ctx={"pressure": 70, "direction": "long_heavy"})
check("옵션+청산 동시경고: 중립 → 주의", v[0] == "주의")

# None 컨텍스트 → 기존 판정 유지
v = derive_supply_verdict(0.005, 5.0, 2.0, options_ctx=None, liq_ctx=None)
check("옵션·청산 None → 기존 판정 유지", v == ("우호", "자금 유입"))


# ─── options.py 단위 테스트 (2026-08-14) ─────────────────────────────

from monitor.options import _calc_pc_ratio, _calc_max_pain

_mock_instruments = [
    {"instrument_name": "BTC-28MAR26-50000-C", "open_interest": 1000},
    {"instrument_name": "BTC-28MAR26-50000-P", "open_interest": 500},
    {"instrument_name": "BTC-28MAR26-60000-C", "open_interest": 2000},
    {"instrument_name": "BTC-28MAR26-60000-P", "open_interest": 1500},
    {"instrument_name": "BTC-28MAR26-70000-C", "open_interest": 800},
    {"instrument_name": "BTC-28MAR26-70000-P", "open_interest": 2000},
]

_pc = _calc_pc_ratio(_mock_instruments)
check("P/C Ratio 계산: (500+1500+2000)/(1000+2000+800)", _pc is not None and abs(_pc - 4000/3800) < 0.01)

_mp = _calc_max_pain(_mock_instruments)
check("Max Pain 계산: 유효한 행사가 반환", _mp is not None and _mp in (50000, 60000, 70000))

check("P/C Ratio: 빈 리스트 → None", _calc_pc_ratio([]) is None)
check("Max Pain: 빈 리스트 → None", _calc_max_pain([]) is None)


# ─── record_ret 확장 (ret_4h/ret_12h) ───────────────────────────────

_rc = sqlite3.connect(":memory:")
_rc.row_factory = sqlite3.Row
_rc.execute("""
    CREATE TABLE levels (
        id INTEGER PRIMARY KEY, ret_4h REAL, ret_12h REAL, ret_24h REAL, ret_72h REAL
    )
""")
_rc.execute("INSERT INTO levels (id) VALUES (1)")
_rc.commit()

db.record_ret(_rc, 1, "ret_4h", 2.5)
check("record_ret: ret_4h 기록", _rc.execute("SELECT ret_4h FROM levels WHERE id=1").fetchone()[0] == 2.5)

db.record_ret(_rc, 1, "ret_4h", 9.9)
check("record_ret: ret_4h 재기록 방지", _rc.execute("SELECT ret_4h FROM levels WHERE id=1").fetchone()[0] == 2.5)

db.record_ret(_rc, 1, "ret_12h", -1.3)
check("record_ret: ret_12h 기록", _rc.execute("SELECT ret_12h FROM levels WHERE id=1").fetchone()[0] == -1.3)

_bad_field = False
try:
    db.record_ret(_rc, 1, "ret_1h", 0.5)
except ValueError:
    _bad_field = True
check("record_ret: 미허용 필드 거부", _bad_field)
_rc.close()


# ─── record_mfe_mae (2026-08-14) ────────────────────────────────────

_mc = sqlite3.connect(":memory:")
_mc.row_factory = sqlite3.Row
_mc.execute(
    "CREATE TABLE levels "
    "(id INTEGER PRIMARY KEY, mfe_pct REAL, mae_pct REAL, "
    "touch_atr_pct REAL, touch_mfe_atr_ratio REAL)"
)
_mc.execute("INSERT INTO levels (id) VALUES (1)")
_mc.commit()

db.record_mfe_mae(_mc, 1, 5.2, -3.1)
_row = _mc.execute("SELECT mfe_pct, mae_pct FROM levels WHERE id=1").fetchone()
check("MFE/MAE: 최초 기록", abs(_row[0] - 5.2) < 0.01 and abs(_row[1] - (-3.1)) < 0.01)

db.record_mfe_mae(_mc, 1, 99.0, -99.0)
_row = _mc.execute("SELECT mfe_pct, mae_pct FROM levels WHERE id=1").fetchone()
check("MFE/MAE: 재기록 방지", abs(_row[0] - 5.2) < 0.01)
_mc.close()


# ─── Coinalyze 폴백 (2026-08-17) ─────────────────────────────────────
# monitor/coinalyze.py 단위 + binance.fetch_funding_rate 폴백 체인 결합.
# 실제 API 콜 없이 requests.get 을 몽키패치해 응답만 시뮬.

from monitor import coinalyze as _coin
from monitor import binance as _bin

# 원본 백업
_orig_get = _coin.requests.get
_orig_bin_get = _bin.requests.get
_orig_secret = _coin.settings.secret


def _restore():
    _coin.requests.get = _orig_get
    _bin.requests.get = _orig_bin_get
    _coin.settings.secret = _orig_secret


class _R:
    def __init__(self, status, body):
        self.status_code = status; self._body = body
    def json(self): return self._body


# CA1: 키 미설정 시 조용히 None
_coin.settings.secret = lambda name: ""
check("CA1 키 미설정 시 funding None", _coin.fetch_funding_rate("BTC") is None)
check("CA1b 키 미설정 시 OI None", _coin.fetch_open_interest("BTC") is None)
check("CA1c 키 미설정 시 OI change None", _coin.fetch_oi_change_24h("BTC") is None)

# CA2: 키 있고 정상 응답
_coin.settings.secret = lambda name: "test_key"
_coin.requests.get = lambda *a, **k: _R(200, [{"symbol": "BTCUSDT_PERP.A",
                                               "value": 0.008282, "update": 1}])
check("CA2 funding 정상 파싱", abs(_coin.fetch_funding_rate("BTC") - 0.008282) < 1e-6)

_coin.requests.get = lambda *a, **k: _R(200, [{"symbol": "BTCUSDT_PERP.A",
                                               "value": 110322.228, "update": 1}])
check("CA2b OI 정상 파싱", abs(_coin.fetch_open_interest("BTC") - 110322.228) < 0.01)

# CA3: OI history 24h 변화율 계산
_coin.requests.get = lambda *a, **k: _R(200, [{"symbol": "BTCUSDT_PERP.A",
    "history": [{"t": 1, "o": 100, "h": 100, "l": 100, "c": 100.0},
                {"t": 2, "o": 100, "h": 100, "l": 100, "c": 118.4}]}])
check("CA3 OI 24h 변화율(+18.4%)",
      abs(_coin.fetch_oi_change_24h("BTC") - 18.4) < 0.01)

# CA3b: history 표본 부족 시 None
_coin.requests.get = lambda *a, **k: _R(200, [{"symbol": "BTCUSDT_PERP.A",
                                               "history": [{"t": 1, "c": 100}]}])
check("CA3b history 1건 뿐이면 None", _coin.fetch_oi_change_24h("BTC") is None)

# CA4: HTTP 오류 시 None
_coin.requests.get = lambda *a, **k: _R(429, {"message": "rate limit"})
check("CA4 HTTP 429 → None", _coin.fetch_funding_rate("BTC") is None)

# CA5: binance.fetch_funding_rate 최종 폴백 — 위 4개(Binance/CG/Bybit/OKX)
# 모두 실패해도 Coinalyze 성공하면 반환. Binance 경로들은 requests.get 을 다 실패로.
_bin.requests.get = lambda *a, **k: _R(500, {})  # Binance/Bybit/OKX 전 경로 실패
# CoinGecko 캐시는 별도 함수 — 강제로 None 반환
_orig_cg_map = _bin._coingecko_binance_funding_map
_bin._coingecko_binance_funding_map = lambda t: None
# Coinalyze만 성공
_coin.requests.get = lambda *a, **k: _R(200, [{"symbol": "BTCUSDT_PERP.A",
                                               "value": 0.005, "update": 1}])
_rate = _bin.fetch_funding_rate("BTC", 5.0)
check("CA5 위 4개 폴백 실패 → Coinalyze 최종 폴백 성공",
      _rate is not None and abs(_rate - 0.005) < 1e-6)
_bin._coingecko_binance_funding_map = _orig_cg_map

_restore()


# ─── DEX Screener + 매핑 (2026-08-17) ────────────────────────────────
# monitor/dexscreener.py 응답 집계 + upbit_dex_mapping.py 캐시 로직 +
# telegram.render_alert dex_stats 배지 3종. 실 API 콜 없음.

from monitor import dexscreener as _dex
from monitor import upbit_dex_mapping as _dxmap

_orig_dex_get = _dex.requests.get
_orig_dxmap_get = _dxmap.requests.get


class _RD:
    def __init__(self, status, body):
        self.status_code = status; self._body = body
    def json(self): return self._body


# DX1: 정상 응답 집계 (2 페어 유동성/볼륨/tx 합산 + buy_ratio 계산)
_dex.requests.get = lambda *a, **k: _RD(200, {"pairs": [
    {"chainId": "ethereum", "liquidity": {"usd": 1_000_000},
     "volume": {"h24": 500_000},
     "txns": {"h24": {"buys": 130, "sells": 70}}},
    {"chainId": "bsc", "liquidity": {"usd": 500_000},
     "volume": {"h24": 200_000},
     "txns": {"h24": {"buys": 60, "sells": 40}}},
]})
_r = _dex.fetch_token_stats("0xabc")
check("DX1 페어 합산 유동성", _r["liquidity_usd"] == 1_500_000)
check("DX1b 페어 합산 24h 볼륨", _r["volume_24h_usd"] == 700_000)
check("DX1c buy_ratio 정확도(3자리 반올림)",
      abs(_r["buy_ratio_24h"] - 0.633) < 0.001)
check("DX1d top_chain 선정", _r["top_chain"] == "ethereum")

# DX2: pairs 비어있으면 None
_dex.requests.get = lambda *a, **k: _RD(200, {"pairs": []})
check("DX2 페어 없음 → None", _dex.fetch_token_stats("0xabc") is None)

# DX3: HTTP 오류 → None
_dex.requests.get = lambda *a, **k: _RD(429, {})
check("DX3 HTTP 429 → None", _dex.fetch_token_stats("0xabc") is None)

_dex.requests.get = _orig_dex_get

# DX4: telegram.render_alert 배지 3종 (저유동/매수/매도 임계)
from notify import telegram as _tg
_base_cluster = [{"entry_usd": 100.0, "score": 50, "grade": "B", "author": "x",
                  "author_followers": 1000, "tp_usd": 110, "direction": "long"}]
_base_rep = _base_cluster[0]
# 저유동
_txt = _tg.render_alert("touch", "TEST", _base_cluster, 100000.0, 1300.0, rep=_base_rep,
                       dex_stats={"liquidity_usd": 50_000, "buy_ratio_24h": 0.5})
check("DX4 저유동성 <100k$ 배지 표시(매수 주의 라벨)",
      "DEX 저유동" in _txt and "50k$" in _txt and "매수 주의" in _txt)
# 매수세 강함
_txt = _tg.render_alert("touch", "TEST", _base_cluster, 100000.0, 1300.0, rep=_base_rep,
                       dex_stats={"liquidity_usd": 500_000, "buy_ratio_24h": 0.70})
check("DX4b 매수세 ≥65% 배지 표시(매수 유리 라벨)",
      "DEX 매수세" in _txt and "70%" in _txt and "매수 유리" in _txt)
# 매도세 강함
_txt = _tg.render_alert("touch", "TEST", _base_cluster, 100000.0, 1300.0, rep=_base_rep,
                       dex_stats={"liquidity_usd": 500_000, "buy_ratio_24h": 0.30})
check("DX4c 매도세 (buy_ratio ≤35%) 배지 표시(매수 부담 라벨)",
      "DEX 매도세" in _txt and "70%" in _txt and "매수 부담" in _txt)  # (1-0.30)*100 = 70
# 중립 = 배지 없음
_txt = _tg.render_alert("touch", "TEST", _base_cluster, 100000.0, 1300.0, rep=_base_rep,
                       dex_stats={"liquidity_usd": 500_000, "buy_ratio_24h": 0.50})
check("DX4d 중립(매수세 미달·유동성 충분) → DEX 배지 없음",
      "DEX 매수세" not in _txt and "DEX 매도세" not in _txt
      and "DEX 저유동" not in _txt)
# dex_stats=None → 배지 미표시
_txt = _tg.render_alert("touch", "TEST", _base_cluster, 100000.0, 1300.0, rep=_base_rep,
                       dex_stats=None)
check("DX4e dex_stats=None → 배지 미표시(매핑 없는 코인 자연 처리)",
      "DEX" not in _txt)


# ─── Coin Metrics 활성주소 백분위 (2026-08-17) ──────────────────────
from monitor import coinmetrics as _cm
_orig_cm_get = _cm.requests.get


# CM1: 미커버 자산은 API 콜 없이 즉시 None
_cm.requests.get = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("must not call"))
check("CM1 미커버 자산은 API 콜 없이 None", _cm.fetch_active_addr_percentile("SHIB") is None)

# CM2: 커버 자산 정상 응답 → 백분위 계산 (30개 중 마지막 값이 최소)
_cm.requests.get = lambda *a, **k: _R(200, {"data": [
    {"asset": "btc", "time": f"2026-08-{i:02d}T00:00:00.000000000Z",
     "AdrActCnt": str(1000000 - i * 1000)}
    for i in range(1, 31)  # 999000 ~ 970000, 마지막(30번째)이 최소
]})
_pct = _cm.fetch_active_addr_percentile("btc", conn=None)
# 마지막값 970000 이하인 관측치 수 = 1 (자기 자신) / 30 → 3.3%
check("CM2 마지막이 최소면 백분위 매우 낮음", _pct is not None and _pct <= 5)

# CM3: 마지막이 최대면 백분위 100
_cm.requests.get = lambda *a, **k: _R(200, {"data": [
    {"asset": "btc", "time": f"2026-08-{i:02d}T00:00:00.000000000Z",
     "AdrActCnt": str(500000 + i * 1000)}
    for i in range(1, 31)  # 501000 ~ 530000, 마지막이 최대
]})
_pct = _cm.fetch_active_addr_percentile("eth", conn=None)
check("CM3 마지막이 최대면 백분위 100", _pct == 100.0)

# CM4: 응답 표본 부족(<5개) → None
_cm.requests.get = lambda *a, **k: _R(200, {"data": [
    {"asset": "xrp", "time": "2026-08-16T00:00:00.000000000Z", "AdrActCnt": "100"}
]})
check("CM4 표본 부족(<5) → None", _cm.fetch_active_addr_percentile("xrp", conn=None) is None)

# CM5: HTTP 오류 → None
_cm.requests.get = lambda *a, **k: _R(500, {})
check("CM5 HTTP 500 → None", _cm.fetch_active_addr_percentile("ada", conn=None) is None)

# CM6: 등급 반영 — 백분위 극단 ±1
from collector import grading as _gr
_base_pts = _gr._onchain_activity_points(50)   # 중간 → 0
_hi_pts = _gr._onchain_activity_points(85)     # ≥80 → +1
_lo_pts = _gr._onchain_activity_points(15)     # ≤20 → -1
_none_pts = _gr._onchain_activity_points(None) # None → 0
check("CM6 활성주소 백분위 ≥80 +1 / ≤20 -1 / 중간·None 0",
      _base_pts == 0.0 and _hi_pts == 1.0 and _lo_pts == -1.0 and _none_pts == 0.0)

# CM7: telegram 배지 — 극단만 노출
_txt = _tg.render_alert("touch", "TEST", _base_cluster, 100000.0, 1300.0, rep=_base_rep,
                       active_addr_pctile=85.5)
check("CM7 활발(≥80) 배지 표시(매수 유리)",
      "온체인 활발" in _txt and "매수 유리" in _txt)
_txt = _tg.render_alert("touch", "TEST", _base_cluster, 100000.0, 1300.0, rep=_base_rep,
                       active_addr_pctile=12.0)
check("CM7b 저조(≤20) 배지 표시(매수 부담)",
      "온체인 저조" in _txt and "매수 부담" in _txt)
_txt = _tg.render_alert("touch", "TEST", _base_cluster, 100000.0, 1300.0, rep=_base_rep,
                       active_addr_pctile=50.0)
check("CM7c 중립(20~80) → 배지 없음", "온체인" not in _txt)

_cm.requests.get = _orig_cm_get


# ─── StockTwits 소셜 심리 (2026-08-17) ──────────────────────────────
from monitor import stocktwits as _st
_orig_st_get = _st.requests.get

# ST1: 정상 응답 집계 (Bullish 10 / Bearish 5 → ratio 10/15 = 0.667)
_st.requests.get = lambda *a, **k: _R(200, {"messages": [
    {"entities": {"sentiment": {"basic": "Bullish"}}} for _ in range(10)
] + [
    {"entities": {"sentiment": {"basic": "Bearish"}}} for _ in range(5)
] + [
    {"entities": None} for _ in range(5)
]})
_r = _st.fetch_sentiment_stats("BTC")
check("ST1 정상 파싱 (Bullish 10 · Bearish 5)",
      _r["bullish"] == 10 and _r["bearish"] == 5
      and abs(_r["bullish_ratio"] - 0.667) < 0.001)

# ST2: 태그 표본 <5 → bullish_ratio None
_st.requests.get = lambda *a, **k: _R(200, {"messages": [
    {"entities": {"sentiment": {"basic": "Bullish"}}} for _ in range(2)
] + [
    {"entities": None} for _ in range(28)
]})
_r = _st.fetch_sentiment_stats("XYZ")
check("ST2 태그 표본 <5 → bullish_ratio None (판정 유보)",
      _r["bullish"] == 2 and _r["bullish_ratio"] is None)

# ST3: 404 심볼 미존재 (WEMIX/KAIA 등) → None
_st.requests.get = lambda *a, **k: _R(404, {})
check("ST3 404 심볼 미존재 → None", _st.fetch_sentiment_stats("KAIA") is None)

# ST4: 빈 messages → None
_st.requests.get = lambda *a, **k: _R(200, {"messages": []})
check("ST4 빈 messages → None", _st.fetch_sentiment_stats("BTC") is None)

# ST5: 등급 반영
_hi = _gr._social_sentiment_points(0.80)
_lo = _gr._social_sentiment_points(0.20)
_mid = _gr._social_sentiment_points(0.50)
_none = _gr._social_sentiment_points(None)
check("ST5 소셜 극단 ±1 (≥0.75 +1 / ≤0.30 -1 / 중간·None 0)",
      _hi == 1.0 and _lo == -1.0 and _mid == 0.0 and _none == 0.0)

# ST6: telegram 배지 극단만 노출
_txt = _tg.render_alert("touch", "TEST", _base_cluster, 100000.0, 1300.0, rep=_base_rep,
                       stwits_bullish_ratio=0.85)
check("ST6 매수세 강함(≥0.75) 배지 표시(매수 유리)",
      "소셜 매수세" in _txt and "매수 유리" in _txt)
_txt = _tg.render_alert("touch", "TEST", _base_cluster, 100000.0, 1300.0, rep=_base_rep,
                       stwits_bullish_ratio=0.20)
check("ST6b 매도세 강함(≤0.30) 배지 표시(매수 부담)",
      "소셜 매도세" in _txt and "매수 부담" in _txt)
_txt = _tg.render_alert("touch", "TEST", _base_cluster, 100000.0, 1300.0, rep=_base_rep,
                       stwits_bullish_ratio=0.50)
check("ST6c 중립(0.30~0.75) → 배지 없음", "소셜" not in _txt)

# ST7: 심볼 충돌 검증 (2026-08-17 실사고 대응) — expected_name != symbol.title
# 정규화 후 불일치 시 응답 폐기. Sky(구 MKR) 알림이 SKY.X=Skycoin 데이터를 96%
# Bullish 로 오라벨했던 사건.
_st.requests.get = lambda *a, **k: _R(200, {
    "symbol": {"title": "Skycoin"},
    "messages": [{"entities": {"sentiment": {"basic": "Bullish"}}} for _ in range(10)]
        + [{"entities": {"sentiment": {"basic": "Bearish"}}} for _ in range(1)]
})
check("ST7 심볼 충돌(CG=Sky vs ST=Skycoin) → None",
      _st.fetch_sentiment_stats("SKY", expected_name="Sky") is None)
# 정상 일치 케이스 — 값 반환
_st.requests.get = lambda *a, **k: _R(200, {
    "symbol": {"title": "Bitcoin"},
    "messages": [{"entities": {"sentiment": {"basic": "Bullish"}}} for _ in range(10)]
        + [{"entities": {"sentiment": {"basic": "Bearish"}}} for _ in range(2)]
})
_r = _st.fetch_sentiment_stats("BTC", expected_name="Bitcoin")
check("ST7b 정상 일치(Bitcoin=Bitcoin) → 값 반환",
      _r is not None and _r["bullish"] == 10)
# expected_name=None → 검증 스킵 (구 호출부 호환)
_r = _st.fetch_sentiment_stats("BTC")
check("ST7c expected_name=None → 검증 스킵(구 호출부 호환)",
      _r is not None and _r["bullish"] == 10)
# 접미 유사 케이스 정확 처리 (Sky vs Skycoin 정규화 후 다름)
check("ST7d 접미 유사(Sky vs Skycoin) 오탐 없음 — 접미 제거 로직 없음",
      _st._normalize_name("Sky") == "sky"
      and _st._normalize_name("Skycoin") == "skycoin"
      and not _st._names_match("Sky", "Skycoin"))

_st.requests.get = _orig_st_get


# ─── 뉴스·시황 요약 알림 (2026-08-17) ────────────────────────────────
from notify import news_brief as _nb, telegram as _tg2
from config import settings as _st_cfg
from utils.time_kst import day_kst as _day_kst_util

_orig_send = _tg2.send
_sent_log = []
def _fake_send(text, urgency="high", reply_to_message_id=None):
    _sent_log.append((text, urgency))
    return 1
_tg2.send = _fake_send

# 임시 DB
import tempfile
_nb_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
db.init_db(_nb_db)
import sqlite3 as _sq
_nbc = _sq.connect(_nb_db)
_nbc.row_factory = _sq.Row

# 2026-09-13 A안: 운영 기본값이 news_alert_send_enabled=False(브리핑 흡수)로
# 바뀌었지만 NB1~NB10 회귀는 실시간 발송 경로를 전제로 짜여 있다. price_logic 의
# preview/TP 스위치와 같은 취급 — 여기서 True 로 되돌려 종전 동작을 보존하고,
# False 동작은 전용 블록(NBQ*)에서 검증한다.
_st_cfg.SETTINGS["news_alert_send_enabled"] = True
# 채널당 상한도 A안에서 3→2 로 조였다. NB3/NB4 는 '상한 도달' 자체를 재는
# 테스트라 설정값을 읽어 기대치를 맞춘다(숫자 하드코딩 제거 — 다음 조정에도 안 깨짐).
_NB_MAX_CH = _st_cfg.SETTINGS["news_alert_max_per_channel_per_day"]

# NB1: 정상 발송
# 2026-09-16: 픽스처에 가격을 넣었다. 정보 밀도 게이트(짧은데 가격 수치가 하나도
# 없는 글은 알맹이가 없다고 보고 스킵)가 생기면서, 종전의 더미 문장은 "정상 뉴스"
# 역할을 못 한다 — 실제 시황 뉴스라면 가격·수치가 있는 게 정상이므로 픽스처를
# 현실에 맞춘다(테스트 의도인 '정상 뉴스는 발송된다'는 그대로).
_p = {"description": "AAVE holds near 126.19 after pulling back from the 140.00 "
                     "high, keeping the multi-month uptrend intact.",
      "url": "https://t.me/x/1"}
r = _nb.maybe_send_news_brief(_nbc, _p, "AAVE", "ch1", now=1786900000)
_nbc.commit()
check("NB1 정상 뉴스 알림 발송(ok)", r == "ok" and len(_sent_log) == 1)
check("NB1b 렌더에 심볼·채널·요약 포함",
      "[뉴스·시황] AAVE" in _sent_log[0][0] and "@ch1" in _sent_log[0][0]
      and "urgency='low'" == f"urgency={_sent_log[0][1]!r}")

# NB2: 코인당 24h 쿨다운 - 같은 코인 재발송 스킵
_sent_log.clear()
r = _nb.maybe_send_news_brief(_nbc, _p, "AAVE", "ch1", now=1786900000 + 60)
check("NB2 같은 코인 24h 쿨다운(스킵)", r == "skipped" and len(_sent_log) == 0)

# NB3: 다른 코인은 발송 가능 (같은 채널 계속) — NB1 이 이미 ch1 1건을 썼다.
r = _nb.maybe_send_news_brief(_nbc, _p, "LINK", "ch1", now=1786900000 + 60)
_nbc.commit()
_nb3_ok = (r == "ok") if _NB_MAX_CH >= 2 else (r == "skipped")
check("NB3 다른 코인은 상한 안에서 정상 발송", _nb3_ok)
# 상한을 채울 때까지 같은 채널로 더 밀어넣는다(상한값 변화에 무관하게 동작).
_nb_syms = ["UNI", "DOT", "ATOM", "NEAR"]
_nb_t = 1786900000 + 120
while len(_sent_log) < _NB_MAX_CH - 1 and _nb_syms:
    _nb.maybe_send_news_brief(_nbc, _p, _nb_syms.pop(0), "ch1", now=_nb_t)
    _nbc.commit()
    _nb_t += 60
check(f"NB3b 채널 상한({_NB_MAX_CH})까지 발송 누적",
      len(_sent_log) == _NB_MAX_CH - 1)
# 상한 초과분: 채널당 하루 상한 도달
r = _nb.maybe_send_news_brief(_nbc, _p, "ETH", "ch1", now=_nb_t + 60)
check(f"NB4 채널당 하루 상한 {_NB_MAX_CH}건 도달 → 스킵", r == "skipped")

# NB5: 짧은 원문 스킵 (60자 미만)
_sent_log.clear()
_p_short = {"description": "짧은 글", "url": ""}
r = _nb.maybe_send_news_brief(_nbc, _p_short, "BTC", "ch2", now=1786900000 + 300)
check("NB5 원문 60자 미만 스킵", r == "skipped" and len(_sent_log) == 0)

# NB6: enabled=False 시 발송 안 함
_st_cfg.SETTINGS["news_alert_enabled"] = False
r = _nb.maybe_send_news_brief(_nbc, _p, "SOL", "ch3", now=1786900000 + 400)
check("NB6 news_alert_enabled=False 시 스킵", r == "skipped")
_st_cfg.SETTINGS["news_alert_enabled"] = True

# NB7: 요약 함수 — 상한(500자) 초과 시 클리핑 + "…" (2026-08-27 250→500)
long_text = "A" * 800
s = _nb._summary(long_text)
check("NB7 요약 500자 이내 + … 마감", len(s) <= 510 and s.endswith("…"))
check("NB7b 상한 이내 원문은 그대로", _nb._summary("B" * 400) == "B" * 400)

# NB8: 매매 결과 리캡 필터 (2026-08-21) — 청산 자랑 글 스킵
_sent_log.clear()
_p_recap1 = {"description": "BCH trade update\nmanually closed. +929.8 pips. "
                            "profits secured. well played everyone.", "url": ""}
r = _nb.maybe_send_news_brief(_nbc, _p_recap1, "BCH", "ch4", now=1786900000 + 500)
check("NB8 결과 리캡(pips+manually closed) 스킵", r == "skipped" and not _sent_log)
_p_recap2 = {"description": "BAT trade update\nclosed at 0.05742. +145 pips. "
                            "clean win. well played, take profits.", "url": ""}
r = _nb.maybe_send_news_brief(_nbc, _p_recap2, "BAT", "ch4", now=1786900000 + 510)
check("NB8b 결과 리캡(closed at+pips) 스킵", r == "skipped" and not _sent_log)

# NB9: 일반 시황 글은 profit/close 단어가 있어도 통과 (오탐 방지 확인)
_p_legit = {"description": "Avalanche momentum builds as investors engage in "
                           "profit-taking after the rally. Price closed above key "
                           "resistance and analysts see higher upside potential.",
            "url": "https://t.me/x/9"}
r = _nb.maybe_send_news_brief(_nbc, _p_legit, "AVAX", "ch5", now=1786900000 + 520)
_nbc.commit()
check("NB9 일반 시황(profit-taking/closed above 포함) 정상 발송", r == "ok" and len(_sent_log) == 1)

# NB10: 첫 줄 중복 제거 (2026-08-27 사용자 요청) — TG 수집부는 title 을 본문
# 첫 줄에서 잘라 만들므로(desc 가 title 로 시작) 결합 시 같은 줄이 두 번 나가던 것
_st_cfg.SETTINGS["news_translate_enabled"] = False
_sent_log.clear()
_p_dup = {"title": "ETH/USDT Take-Profit target 2",
          "description": "ETH/USDT Take-Profit target 2\nProfit reached 249.2 "
                         "percent over one month and nine days of holding.",
          "url": ""}
r = _nb.maybe_send_news_brief(_nbc, _p_dup, "ETH", "ch6", now=1786900000 + 530)
_nbc.commit()
check("NB10 title=첫줄 중복 제거(1회만 표기)",
      r == "ok" and _sent_log[0][0].count("Take-Profit target 2") == 1)
# NB10b: 독립 title(desc 와 다름)은 종전대로 결합 유지 — 다음 날로 넘겨 상한 회피
_sent_log.clear()
_p_sep = {"title": "Headline about Cardano outlook",
          "description": "Body text differs from the headline: ADA trades near "
                         "0.8420 after reclaiming the 0.8000 support zone.",
          "url": ""}
r = _nb.maybe_send_news_brief(_nbc, _p_sep, "ADA", "ch6", now=1786900000 + 86400 + 600)
_nbc.commit()
check("NB10b 독립 title 은 결합 유지",
      r == "ok" and "Headline about Cardano" in _sent_log[0][0]
      and "Body text differs" in _sent_log[0][0])
_st_cfg.SETTINGS["news_translate_enabled"] = True

# ─── NBS1~NBS8: 진입 시그널 글 필터 (2026-09-14) ──────────────────────────
# 사고: extractor.parse_setup 이 포맷을 못 읽은 시그널 글이 "셋업 아님"으로 뉴스
# 경로에 떨어져 시황 뉴스인 척 실렸다. 실측(브리핑 큐 id=1) —
# "❤️❤️❤️FREE SIGNAL!❤️❤️❤️ / Instrument: LSKUSDT / My opinion: BUY /
#  Entry: $0.83 / Target: $0.842 / RRR: 1:3".
# 아래 후반부(정상 뉴스)가 이 필터의 진짜 계약이다 — 시황글을 잡으면 안 된다.
_nbs = _nb._is_trade_setup
for _d, _t, _want in [
    ("NBS1 FREE SIGNAL 카드(실측 원문)",
     "❤️❤️❤️FREE SIGNAL!❤️❤️❤️\nInstrument: LSKUSDT\nMy opinion: BUY\n"
     "Entry: $0.83\nTarget: $0.842\nRRR: 1:3", True),
    ("NBS2 같은 글의 한글 번역본(번역 후 재검사 대비)",
     "❤️❤️❤️무료 신호!❤️❤️❤️\n기기: LSKUSDT\n내 의견: 구매\n입장료: $ 0.83\n"
     "경유지: $ 0.826\n목표: $ 0.842\nRRR: 1: 3", True),
    ("NBS3 전형적 시그널 카드(Entry/TP/SL/Leverage)",
     "BTC/USDT LONG\nEntry: 62000\nTP1: 64000\nSL: 60000\nLeverage: 10x", True),
    ("NBS4 정상 시장분석은 통과(실측 FIL)",
     "# FIL Market Analysis\nFIL exploded to 0.9363 over six hours, cleanly "
     "leaving the demand zone near 0.7900.\nBull case: hold above 0.9000.", False),
    ("NBS5 고래 매수 뉴스는 통과(실측 SOL)",
     "A whale bought $9,000,000 of #SOL this week. Smart money is now "
     "focusing more on alts.", False),
    ("NBS6 차트 코멘트는 통과(실측 NEO)",
     "$NEOUSDT update: 30m\nThis trendline will soon create a new opportunity.", False),
    ("NBS7 'entry point'·'price target' 산문은 통과(과필터 방지)",
     "Bitcoin found a good entry point for buyers near support. Analysts see "
     "a price target of $120,000 this quarter.", False),
    ("NBS8 'Support:'·'Resistance:' 라벨만으론 통과(보조 2점 < 문턱 3점)",
     "Key levels to watch — Support: 0.79, Resistance: 0.95. The market "
     "remains range-bound.", False),
]:
    check(_d, _nbs(_t) is _want)

# 파이프라인 통과 검증 — 필터가 실제 발송 경로에서 동작하는가
_sent_log.clear()
_p_sig = {"title": "", "description":
          "FREE SIGNAL! Instrument: ADAUSDT My opinion: BUY Entry: $0.83 "
          "Target: $0.842 RRR: 1:3 — plenty long to clear the min length gate.",
          "url": ""}
r = _nb.maybe_send_news_brief(_nbc, _p_sig, "ADA", "chsig",
                              now=1786900000 + 86400 * 9)
check("NBS9 시그널 글은 발송·큐 적재 둘 다 안 된다(skipped)",
      r == "skipped" and len(_sent_log) == 0)

# ─── NBQ1~NBQ4: 뉴스 발송 스위치 OFF → 브리핑 대기열 (2026-09-13 A안) ────
# 계약: OFF 면 telegram.send 만 생략하고 상한·쿨다운·필터·요약·번역은 종전대로
# 수행한다. 결과물은 news_digest_queue 에 적재되고 record_alert(kind='news')도
# 그대로 남아 상한 카운트가 유지된다(= 큐가 상한 위로 불어나지 않는다).
_st_cfg.SETTINGS["news_alert_send_enabled"] = False
_st_cfg.SETTINGS["news_translate_enabled"] = False
_sent_log.clear()
_NBQ_DAY_T = 1786900000 + 86400 * 3          # 상한·쿨다운 충돌 없는 새 KST 일자
_p_q = {"description": "XRP ledger activity climbs to a new high this quarter. "
                       "Analysts point to steady settlement volume growth.",
        "url": "https://t.me/x/42"}
r = _nb.maybe_send_news_brief(_nbc, _p_q, "XRP", "chq", now=_NBQ_DAY_T)
_nbc.commit()
check("NBQ1 스위치 OFF — 발송 0건 · 반환 queued",
      r == "queued" and len(_sent_log) == 0)
_nbq_day = _day_kst_util(_NBQ_DAY_T)
_nbq_rows = db.get_news_digest(_nbc, limit=5)
check("NBQ2 news_digest_queue 적재(심볼·채널·요약)",
      len(_nbq_rows) == 1 and _nbq_rows[0]["symbol"] == "XRP"
      and _nbq_rows[0]["channel"] == "chq"
      and "XRP ledger activity" in (_nbq_rows[0]["summary"] or ""))
_nbq_log = _nbc.execute(
    "SELECT kind, sent FROM alerts_log WHERE coin_symbol='XRP' AND kind='news'"
).fetchall()
check("NBQ3 record_alert(kind='news') 기록 유지 · sent=0 으로 구분(상한 카운트 유지)",
      len(_nbq_log) == 1 and _nbq_log[0]["sent"] == 0)
# 같은 코인 24h 쿨다운이 큐 경로에서도 그대로 작동해야 한다(상한 무손상 증명)
r = _nb.maybe_send_news_brief(_nbc, _p_q, "XRP", "chq", now=_NBQ_DAY_T + 60)
check("NBQ4 큐 경로에서도 코인 24h 쿨다운 유지(큐 무한증식 방지)", r == "skipped")
_st_cfg.SETTINGS["news_translate_enabled"] = True

_nbc.close()
os.unlink(_nb_db)
_tg2.send = _orig_send


# ─── 결과 ────────────────────────────────────────────────────────────

# ─── 회차 정지 경고 원인 표시 (2026-09-13 러너 대기 사고) ───────────────────

from monitor import price_check as _pc_rq
from notify import telegram as _tg_rq

with patch.dict(os.environ, {"RUNNER_QUEUE_WAIT_MIN": ""}):
    check("RQ1: env 비어있음 → None", _pc_rq._runner_queue_wait_min() is None)
with patch.dict(os.environ, {"RUNNER_QUEUE_WAIT_MIN": "287"}):
    check("RQ2: env '287' → 287.0", _pc_rq._runner_queue_wait_min() == 287.0)
with patch.dict(os.environ, {"RUNNER_QUEUE_WAIT_MIN": "abc"}):
    check("RQ3: env 이상값 → None", _pc_rq._runner_queue_wait_min() is None)
with patch.dict(os.environ, {"RUNNER_QUEUE_WAIT_MIN": "-5"}):
    check("RQ4: env 음수 → None", _pc_rq._runner_queue_wait_min() is None)

_rq_old = _tg_rq.render_price_check_gap_alert(291, 120)
check("RQ5: 대기 미측정 → 종전 문구(cron-job.org 의심)",
      "cron-job.org 주 경로" in _rq_old and "러너 배정 대기" not in _rq_old)
_rq_new = _tg_rq.render_price_check_gap_alert(291, 120, queue_wait_min=287)
check("RQ6: 대기 287분 → GitHub 러너 지연 원인 문구, cron-job.org 의심 문구 없음",
      "러너 배정 대기 287분" in _rq_new and "githubstatus.com" in _rq_new
      and "cron-job.org 주 경로" not in _rq_new)
_rq_small = _tg_rq.render_price_check_gap_alert(291, 120, queue_wait_min=3)
check("RQ7: 대기 3분(정상 범위) → 종전 문구 유지", "cron-job.org 주 경로" in _rq_small)

# ─── MB1~MB8: 모닝 브리핑 흡수 블록 (2026-09-13 A안) ─────────────────────
# 실시간 발송을 끈 TP 적중·뉴스를 다음 날 아침 브리핑이 대신 전달한다.
# 여기서는 블록 렌더 함수만 검증한다(build_brief 전체는 test_morning_brief.py).
from notify import morning_brief as _mb

_MB_DB = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
db.init_db(_MB_DB)
_mbc = sqlite3.connect(_MB_DB)
_mbc.row_factory = sqlite3.Row
_MB_DAY = "2026-09-12"
_MB_NOW = 1789000000.0

# MB1: 빈 큐/빈 적중 → 블록 통째 생략
check("MB1 빈 TP 적중 → 🏁 블록 생략", _mb._tp_hit_lines(_mbc, _MB_NOW) == [])
_mb_ids = []
check("MB2 빈 뉴스 큐 → 📰 블록 생략",
      _mb._news_lines(_mbc, _mb_ids) == [] and _mb_ids == [])

# MB3: 뉴스 5건 컷 + "외 N건" + consumed 처리
for i in range(7):
    db.queue_news_digest(_mbc, f"SYM{i}", f"chan{i}",
                         f"SYM{i} trades near 1,2{i}0 after reclaiming support. "
                         f"Second sentence is dropped.",
                         f"https://t.me/x/{i}", _MB_DAY, _MB_NOW + i)
_mbc.commit()
_mb_ids = []
_mb_news = _mb._news_lines(_mbc, _mb_ids)
check("MB3 뉴스 큐 7건 → 최대 5건만 렌더 + 헤더에 '외 2건'",
      len(_mb_ids) == 5 and "외 2건" in _mb_news[0])
check("MB3b 항목 줄에 코인·채널·요약 첫 문장(원문 링크 없음)",
      any("SYM0" in x and "@chan0" in x for x in _mb_news)
      and any("SYM0 trades near 1,200" in x for x in _mb_news)
      and not any("https://" in x for x in _mb_news))
check("MB3c 요약은 첫 문장만 (둘째 문장 제외)",
      not any("Second sentence" in x for x in _mb_news))
db.consume_news_digest(_mbc, _mb_ids)
_mbc.commit()
check("MB4 consumed=1 처리 후 남은 미소비 2건", db.count_news_digest(_mbc) == 2)
_mb_ids2 = []
_mb_news2 = _mb._news_lines(_mbc, _mb_ids2)
check("MB4b 소비된 건은 다음 브리핑에 다시 안 나온다",
      len(_mb_ids2) == 2 and all(i not in _mb_ids for i in _mb_ids2))

# ── MBQ1~MBQ5: 큐 노이즈 2차 필터 (2026-09-17 실사고) ────────────────────
# 사고: 09-17 아침 브리핑에 시그널 카드("📍신호 ID: #2227📍 / 코인: $JUP/USDT
# (2-5X) / 방향: 긴 / 정지 손실: 0.2160")가 실렸다. 수집 단계 필터는 큐 적재
# **전에만** 돌기 때문에, 큐 적재(03:23)가 필터 수정 배포(07:04)보다 빨랐던 것.
# 필터를 고쳐도 이미 쌓인 항목에는 소급되지 않는다 → 렌더 직전에 한 번 더 본다.
# 전용 DB 를 쓴다 — 큐는 오래된 순으로 나가므로 다른 블록의 잔여 미소비분이
# 섞이면 "무엇이 실렸는가" 검증이 흔들리고, 반대로 여기서 큐를 비우면 뒤쪽
# 테스트(MB12)의 픽스처가 사라진다. 서로 건드리지 않는 게 맞다.
_MBQ_DAY = "2026-09-17"
_MBQ_NOW = 1789500000.0
_MBQ_DB = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
db.init_db(_MBQ_DB)
if True:
    _mbqc = sqlite3.connect(_MBQ_DB)
    _mbqc.row_factory = sqlite3.Row
    # 노이즈 3종 + 정상 2건을 오래된 순으로 심는다(get_news_digest 가 오래된 순).
    for _sym, _ch, _sum in [
        ("JUP", "sig", "📍신호 ID: #2227📍\n코인: $JUP/USDT (2-5X)\n방향: 긴\n정지 손실: 0.2160"),
        ("ZRX", "wolf", "투자하기 좋은 코인을 찾고 계신가요?\n$ZRX는 좋은 선택입니다."),
        ("IN", "cs", "트레이딩 이론 설명이 길게 이어지는 교육용 글이며 코인과는 무관한 내용이다."),
        ("XRP", "cs", "XRP는 엄청난 성장 잠재력을 보여줍니다\n"
                      "XRP 시장이 상승세를 보일 가능성이 있다는 것을 쉽게 인식할 수 "
                      "있습니다.\n또한 명확성 법은 상원의 공개 투표를 위해 "
                      "마련되었으며 이는 XRP의 상당한 가격 변동을 촉진할 수 있습니다."),
        ("ETH", "bb", "강세 사례: 2,540을 클리어합니다.\n약세: 2,460 부근에서 무너집니다."),
    ]:
        db.queue_news_digest(_mbqc, _sym, _ch, _sum, "", _MBQ_DAY, _MBQ_NOW)
    _mbqc.commit()
    _mbq_ids = []
    _mbq_lines = _mb._news_lines(_mbqc, _mbq_ids)
_mbq_body = "\n".join(_mbq_lines)
check("MBQ1 큐에 이미 들어간 시그널 카드는 표시에서 제외(실사고 재현)",
      "신호 ID" not in _mbq_body and "JUP" not in _mbq_body)
check("MBQ2 광고·모호심볼도 같은 기준으로 제외(news_brief 기준 재사용)",
      "ZRX" not in _mbq_body and "교육용" not in _mbq_body)
check("MBQ3 정상 뉴스는 그대로 실린다(과필터 방지)",
      "XRP" in _mbq_body and "ETH" in _mbq_body
      and "↑ 2,540 강세" in _mbq_body)
check("MBQ4 걸러진 항목도 **소비 처리**한다 — 큐에 남아 뒤를 굶기면 안 된다",
      len(_mbq_ids) == 5)
_mbq_left = db.count_news_digest(_mbqc)
check("MBQ5 노이즈를 제외하고도 정상분을 채우려 상한보다 넉넉히 꺼낸다",
      _mbq_left == 5 and _mb._NEWS_FETCH_MULT >= 2)

# ── MBW1~MBW7: 요약 줄 행잉 인덴트 (2026-09-16 사용자 요청) ───────────────
# "줄내림 발생시 줄내림만 들어가는 게 아니고, 줄내림 직전 텍스트 시작열과
#  동일한 위치에서 시작되게" — 텔레그램에는 CSS 가 없어 한 줄이 화면 폭을
# 넘으면 클라이언트가 접고 **접힌 줄은 0열에 붙는다**. 미리 폭에 맞춰 나누고
# 각 줄에 같은 들여쓰기를 넣어 접힘 자체를 없앤다.
_W, _IND = _mb._NEWS_WRAP_W, _mb._NEWS_INDENT
_mbw = _mb._wrap_indented("명확성법(Clarity Act)은 상원의 공개 투표로 예정되어 있으며…",
                          _W, _IND)
check("MBW1 긴 요약은 여러 줄로 나뉜다", len(_mbw) >= 2)
check("MBW2 **모든 줄**이 같은 들여쓰기로 시작한다(핵심 계약)",
      all(x.startswith(_IND) and not x[len(_IND):].startswith(" ") for x in _mbw))
check("MBW3 어느 줄도 표시 너비 상한을 넘지 않는다(넘으면 클라이언트가 다시 접는다)",
      all(_mb._display_width(x) <= _W for x in _mbw))
check("MBW4 원문 어절이 유실되지 않는다",
      "".join(x[len(_IND):] for x in _mbw).replace(" ", "")
      == "명확성법(ClarityAct)은상원의공개투표로예정되어있으며…".replace(" ", ""))
check("MBW5 짧은 줄은 접지 않는다(불필요한 줄바꿈 금지)",
      len(_mb._wrap_indented("↑ 2,540 강세 · ↓ 2,460 약세", _W, _IND)) == 1)
_mbw_long = _mb._wrap_indented("a" * 80, _W, _IND)
check("MBW6 공백 없는 긴 덩어리(URL 등)는 글자 단위로 쪼갠다",
      len(_mbw_long) >= 2 and all(_mb._display_width(x) <= _W for x in _mbw_long))
check("MBW7 빈 문자열은 줄을 만들지 않는다", _mb._wrap_indented("", _W, _IND) == [])

# MB5~MB6: 🏁 어제 목표 도달 — 진입 대비 % 계산 + 8줄 컷
_mb_lids = []
for i in range(10):
    _mbc.execute(
        "INSERT INTO levels (signal_key, coin_symbol, ticker, direction, entry_usd,"
        " tps_usd, status, collected_at) VALUES (?,?,?,?,?,?,'touched',?)",
        (f"mbk{i}", f"MBC{i}", f"KRW-MBC{i}", "long", 100.0,
         json.dumps([110.0, 125.0]), _MB_NOW))
    _mb_lids.append(_mbc.execute("SELECT last_insert_rowid() AS r").fetchone()["r"])
for idx, lid in enumerate(_mb_lids):
    db.record_alert(_mbc, f"MBC{idx}", "tp1", [lid], _MB_DAY, _MB_NOW, sent=0)
db.record_alert(_mbc, "MBC0", "tp2", [_mb_lids[0]], _MB_DAY, _MB_NOW + 1, sent=0)
_mbc.commit()
_mb_tp = _mb._tp_hit_lines(_mbc, _MB_NOW + 3600)
check("MB5 헤더에 총 건수 10건 · 본문은 8줄 컷 + '외 2건'",
      "10건" in _mb_tp[0] and len(_mb_tp) == 1 + 8 + 1 and "외 2건" in _mb_tp[-1])
check("MB5b 같은 레벨 TP1·TP2 는 최고 단계 1행으로 접힘(TP2/2)",
      any("MBC0 TP2/2" in x for x in _mb_tp)
      and not any("MBC0 TP1/2" in x for x in _mb_tp))
check("MB6 진입 대비 % = (TP − 진입)/진입 (TP2 125 vs 진입 100 → +25.0%)",
      any("MBC0 TP2/2 (진입 +25.0%)" in x for x in _mb_tp))
check("MB6b 중간 단계는 해당 TP 기준 (TP1 110 → +10.0%)",
      any("TP1/2 (진입 +10.0%)" in x for x in _mb_tp))

# MB7: level_ids 계약 재사용 행(kind='news' 의 채널명)은 TP 집계에 섞이지 않는다
db.record_alert(_mbc, "ZZZ", "news", ["some_channel"], _MB_DAY, _MB_NOW, sent=0)
_mbc.commit()
check("MB7 level_ids 가 정수가 아닌 행(news)은 TP 집계에서 제외",
      len(db.get_tp_hits_since(_mbc, _MB_NOW - 86400)) == 10)

# MB6c~MB6e: 코인당 1행 접기 (2026-09-13 CTO 검토). 같은 코인의 **다른 레벨**
# (클러스터 형제)이 각각 적중하면 원본 rows 는 2행인데, 진입 알림 자체가 클러스터당
# 1회만 나가므로 표시도 코인 1행이어야 한다 — 최고 단계만 남고 헤더 건수도 접은 뒤
# 기준으로 세어야 한다("11건"이라 써놓고 10줄만 보이면 안 된다).
# 위치가 MB7 뒤인 이유: 여기서 레벨을 하나 더 심으므로, 앞에 두면 MB7 의
# "정확히 10건" 기대값이 11 로 흔들린다(집계 대상 자체를 바꾸는 픽스처다).
_mbc.execute(
    "INSERT INTO levels (signal_key, coin_symbol, ticker, direction, entry_usd,"
    " tps_usd, status, collected_at) VALUES (?,?,?,?,?,?,'touched',?)",
    ("mbk_sib", "MBC0", "KRW-MBC0", "long", 200.0,
     json.dumps([210.0, 260.0]), _MB_NOW))
_mb_sib_id = _mbc.execute("SELECT last_insert_rowid() AS r").fetchone()["r"]
db.record_alert(_mbc, "MBC0", "tp1", [_mb_sib_id], _MB_DAY, _MB_NOW + 2, sent=0)
_mbc.commit()
check("MB6c 형제 레벨이 늘어 원본 집계는 11건이 된다(접기 전 기준선)",
      len(db.get_tp_hits_since(_mbc, _MB_NOW - 86400)) == 11)
_mb_tp2 = _mb._tp_hit_lines(_mbc, _MB_NOW + 3600)
check("MB6d 같은 코인의 형제 레벨 적중은 1행으로 접힌다(MBC0 두 번 안 나옴)",
      sum(1 for x in _mb_tp2 if "MBC0 " in x) == 1)
check("MB6e 접힌 뒤에도 최고 단계가 남는다(형제의 TP1 이 아니라 TP2/2)",
      any("MBC0 TP2/2" in x for x in _mb_tp2))
check("MB6f 헤더 건수는 접은 뒤 기준 — 원본 11건이어도 코인 수 10건으로 표기",
      "10건" in _mb_tp2[0])

# MB8: 텔레그램 4096자 방어 — 뉴스 줄부터 줄인다
_mb_head = ["헤더", _mb.telegram.SEP, "시장환경 A", "시장환경 B"]
# ── MB9~MB12: 조회 창 회귀 (2026-09-14 실사고) ───────────────────────────
# 사고: 두 블록이 `day_kst='어제'` 로 조회했다. 그런데 브리핑은 **아침 8~10시**에
# 나가므로 "오늘 0~8시"에 생긴 적중·뉴스는 day_kst 가 '오늘'이라 그날 브리핑에서
# 통째로 빠지고 **다음 날**에야 실렸다(실측 09-14: 새벽 00:20~06:12 TP 적중 4건
# 누락, 뉴스 큐 5건이 전부 '오늘' 날짜라 브리핑 뉴스 0건). TP 실시간 발송을 끄고
# 맞바꾼 게 "다음 날 아침 확인"인데 실제로는 이틀 뒤가 되던 셈이다.
# 수리: TP 는 meta.last_morning_brief_at 이후, 뉴스는 날짜 무관 미소비 전부.
_MB2_DB = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
db.init_db(_MB2_DB)
_mb2 = sqlite3.connect(_MB2_DB)
_mb2.row_factory = sqlite3.Row
# 브리핑 발송 시각 = '오늘 09:00', 직전 브리핑 = '어제 09:00'
_MB2_NOW = 1789000000.0
_MB2_PREV = _MB2_NOW - 86400.0
_mb2.execute("INSERT INTO levels (signal_key, coin_symbol, ticker, direction,"
             " entry_usd, tps_usd, status, collected_at) VALUES"
             " (?,?,?,?,?,?,'touched',?)",
             ("mb2k", "DAWN", "KRW-DAWN", "long", 100.0,
              json.dumps([110.0, 120.0]), _MB2_NOW - 200000))
_mb2_lid = _mb2.execute("SELECT last_insert_rowid() AS r").fetchone()["r"]
db.set_meta(_mb2, _mb.META_LAST_BRIEF_AT, str(_MB2_PREV))
# ① 직전 브리핑 '이전'(=이미 보여준 것) ② 새벽 적중(어제 날짜 아님, 창 안)
db.record_alert(_mb2, "DAWN", "tp1", [_mb2_lid], "2026-09-12", _MB2_PREV - 3600, sent=0)
db.record_alert(_mb2, "DAWN", "tp2", [_mb2_lid], "2026-09-13", _MB2_NOW - 7200, sent=0)
_mb2.commit()
_mb2_tp = _mb._tp_hit_lines(_mb2, _MB2_NOW)
check("MB9 브리핑 2시간 전(같은 날 새벽) 적중이 당일 브리핑에 포함된다 — 사고 재발 방지",
      any("DAWN TP2/2" in x for x in _mb2_tp))
check("MB10 직전 브리핑 이전 적중은 제외(이미 보여준 것을 또 싣지 않는다)",
      not any("TP1/2" in x for x in _mb2_tp))
# meta 가 없으면(최초 1회·옛 DB) 24시간 폴백으로 동작해야 한다
_mb2.execute("DELETE FROM meta WHERE key=?", (_mb.META_LAST_BRIEF_AT,))
_mb2.commit()
check("MB11 last_morning_brief_at 부재 시 24시간 폴백으로 새벽 적중 포함",
      any("DAWN TP2/2" in x for x in _mb._tp_hit_lines(_mb2, _MB2_NOW)))
# 시계 역행 방어: meta 가 미래면 폴백을 쓴다(창이 음수가 되어 전부 누락되는 것 방지)
db.set_meta(_mb2, _mb.META_LAST_BRIEF_AT, str(_MB2_NOW + 99999))
_mb2.commit()
check("MB11b meta 가 미래 시각이면 신뢰하지 않고 24시간 폴백",
      any("DAWN TP2/2" in x for x in _mb._tp_hit_lines(_mb2, _MB2_NOW)))
# 뉴스: '오늘' 날짜로 적재된 건도 당일 브리핑에 실려야 한다(종전엔 0건이었다)
# 픽스처에 가격을 넣는다 — 2026-09-17 정보 밀도 게이트(짧은데 가격 수치가 없는
# 글은 알맹이 없음)가 렌더 단계에도 걸리므로, 더미 문장은 "정상 뉴스" 역할을 못 한다.
db.queue_news_digest(_mb2, "DAWN", "chan",
                     "DAWN trades near 1,250 after reclaiming the support zone.",
                     "", "2026-09-13", _MB2_NOW - 7200)
_mb2.commit()
_mb2_ids = []
_mb2_news = _mb._news_lines(_mb2, _mb2_ids)
check("MB12 적재 날짜와 무관하게 미소비 뉴스는 당일 브리핑에 실린다 — 사고 재발 방지",
      len(_mb2_ids) == 1 and any("DAWN" in x for x in _mb2_news))
_mb2.close()

# ── NBB1~NBB5: 뉴스 본문 정제 (2026-09-14) ───────────────────────────────
# 브리핑은 요약 첫 문장 80자만 싣는데, 채널 원문의 장식이 그 자리를 차지해
# 정작 내용이 안 보였다(실측: "# FIL 시장 분석 FIL은 6시간 동안…" / 본문이 한
# 글자도 안 나온 "❤️❤️❤️무료 신호!❤️❤️❤️" 글).
check("NBB1 선행 마크다운 헤더 줄은 걷어내고 본문부터 싣는다(실측 FIL)",
      _mb._first_sentence("# FIL 시장 분석\nFIL은 6시간 동안 급등했다. 다음 문장.")
      .startswith("FIL은 6시간"))
check("NBB2 본문 중간의 #(해시태그)은 보존 — 헤더만 스킵한다(실측 SOL)",
      "#SOL" in _mb._first_sentence("고래가 이번 주 #SOL 을 샀다."))
check("NBB3 반복 기호 이후 채널 꼬리말은 잘라낸다",
      "Bitcoin Bullets" not in
      _mb._first_sentence("XRP 가 지지선을 시험했다.\n➖➖➖➖➖➖➖\nBitcoin Bullets ® 거래"))
check("NBB4 글자 없는 장식 줄은 건너뛴다",
      _mb._first_sentence("🔥🔥🔥\n\n실제 본문이 여기 있다.").startswith("실제 본문"))
check("NBB5 장식만 있는 글은 빈 문자열(요약 줄 자체가 생략된다)",
      _mb._first_sentence("🔥🔥🔥\n➖➖➖➖") == "")

# ── NBC1~NBC12: "왜 사야 하는가" 우선 추출 (2026-09-16 사용자 요청) ────────
# "결국 뉴스 첫줄에는 보는 사람이 이걸 왜 사야하는가에 포커스를 맞춰 핵심 이슈를
# 먼저 언급" — 제목은 대개 수사(修辭)라 살 이유를 담지 못한다. 아래 케이스는
# 전부 실제 발송분(news_digest_queue 15건)에서 가져왔다.
check("NBC1 규제 촉매가 수사적 제목을 제친다(실측 XRP — 제목 '엄청난 성장 잠재력')",
      _mb._first_sentence(
          "XRP는 엄청난 성장 잠재력을 보여줍니다\nXRP 시장이 거대한 움직임을 위해 "
          "자리를 잡았을 수 있습니다.\n또한, 명확성 법은 상원의 공개 투표를 위해 "
          "마련되었으며, 이는 XRP 가격을 촉매할 수 있습니다."
      ).startswith("명확성 법은 상원"))
check("NBC2 선행 접속사('또한,')는 떼어낸다",
      not _mb._first_sentence("제목입니다\n또한, 고래가 $9,000,000를 샀습니다.")
      .startswith("또한"))
check("NBC3 자금 유입이 촉매로 잡힌다(실측 SOL)",
      "9,000,000" in _mb._first_sentence("고래가 이번 주 #Sol에서 $ 9,000,000를 샀습니다."))
check("NBC4 기호-티커 사이 번역 공백 정리($ 9,000 → $9,000 / # Sol → #Sol)",
      "$9,000,000" in _mb._first_sentence("고래가 #Sol 에서 $ 9,000,000를 샀습니다.")
      and "# " not in _mb._first_sentence("고래가 # Sol에서 $ 9,000,000를 샀습니다."))
# 시나리오 분기 템플릿 — 실측 @BitcoinBullets 4건이 전부 이 꼴이고 서술이 길어
# 반드시 잘렸다. 위아래 분기 가격만 뽑으면 30자 안에 들어간다.
_nbc_tpl = ("# FIL 시장 분석\nFIL은 6시간 동안 0.9363으로 폭발하여 0.7900 근처의 "
            "수요 구역을 깨끗하게 벗어났습니다.\n황소 케이스: 0.9000을 초과하여 유지하고 "
            "계속 밀어붙입니다.\n베어 케이스: 0.8600 아래로 페이드백합니다.\n"
            "➖➖➖➖➖➖➖\nBitcoin Bullets ® 거래")
check("NBC5 황소/베어 케이스에서 분기 가격만 뽑아 한 줄로",
      _mb._first_sentence(_nbc_tpl) == "↑ 0.9000 강세 · ↓ 0.8600 약세")
check("NBC6 분기 가격이 같으면 하나의 기준선으로 표현(실측 AAVE)",
      _mb._first_sentence("황소 케이스: 122.00 이상으로 유지합니다.\n"
                          "베어 케이스: 122.00을 잃고 98.50으로 미끄러집니다.")
      == "122.00 지키면 강세 · 잃으면 약세")
check("NBC7 bull/bear case 영문 표기도 인식",
      _mb._first_sentence("Bull case: 0.9000 hold.\nBear case: 0.8600 breakdown.")
      == "↑ 0.9000 강세 · ↓ 0.8600 약세")
# 무의미한 제목 — 심볼 + 일반명사뿐이라 정보가 0이다
check("NBC8 '# FIL 시장 분석' 류 제목은 버리고 본문을 올린다",
      _mb._is_generic_title("# FIL 시장 분석") is True
      and _mb._is_generic_title("$BTCUSDT 중요 업데이트") is True)
check("NBC9 타임프레임만 붙은 제목도 무의미로 본다(실측 NEO '업데이트: 30분')",
      _mb._is_generic_title("$ NEOUSDT 업데이트: 30분") is True)
check("NBC10 내용 있는 제목은 살린다 — 두괄식 요지이므로",
      _mb._is_generic_title("XRP는 엄청난 성장 잠재력을 보여줍니다") is False)
check("NBC11 긴 촉매 문장은 절 경계에서 끊는다(동사 중간 절단 금지)",
      _mb._first_sentence(
          "제목\n명확성 법은 상원의 공개 투표를 위해 마련되었으며, 이는 XRP에 대한 "
          "상당한 가격 움직임을 촉매할 수 있습니다.").endswith("…")
      and "마련되었으며" in _mb._first_sentence(
          "제목\n명확성 법은 상원의 공개 투표를 위해 마련되었으며, 이는 XRP에 대한 "
          "상당한 가격 움직임을 촉매할 수 있습니다."))
check("NBC12 차트 용어 1점짜리 단독은 촉매로 채택하지 않는다(과추출 방지)",
      _mb._catalyst_sentence("이 저항선을 지켜보고 있습니다.", 55) == "")

# ── NBC13~NBC16 · NBS10: 번역 변형 대응 (2026-09-17 실물 브리핑에서 발견) ──
# 무료 번역기는 같은 원문을 회차마다 다르게 옮긴다. 09-17 큐에서 실제로 새다:
#   · bull/bear case → "강세 사례:" · "약세:"(머리말 자체가 없음)
#   · Stop Loss → "정지 손실", Long → "긴", Signal ID → "신호 ID"
# 그래서 시나리오 패턴은 머리말(케이스·사례·시나리오)을 **선택**으로 두고,
# 시그널 필터는 번역 변형 라벨까지 본다.
check("NBC13 '강세 사례:' / '약세:' 조합도 분기로 인식(실측 ETH)",
      _mb._first_sentence(
          "#ETH 시장 분석\nETH는 4시간째 2,500.42에 있습니다.\n"
          "강세 사례: 2,540을 클리어합니다.\n약세: 2,460 부근에서 무너집니다."
      ) == "↑ 2,540 강세 · ↓ 2,460 약세")
check("NBC14 종전 '황소/베어 케이스' 표기도 계속 인식(회귀 보호)",
      _mb._first_sentence("황소 케이스: 0.9000 유지.\n베어 케이스: 0.8600 이탈.")
      == "↑ 0.9000 강세 · ↓ 0.8600 약세")
check("NBC15 한쪽만 있으면 분기 압축 안 하고 촉매로 넘어간다(실측 BTC)",
      _mb._first_sentence(
          "#BTC 시장분석\nBTC는 일일 76,155에 있습니다.\n"
          "강세 사례: 82,000을 클리어합니다. CLARITY 법안에 대한 절차적 상원 "
          "표결이 오늘 예정되어 있습니다."
      ).startswith("CLARITY 법안"))
check("NBC16 산문 속 '강세'는 분기로 오인하지 않는다(콜론+숫자 필요)",
      _mb._scenario_line("시장이 강세를 보이며 2,540까지 올랐고 약세 전환은 없었다.") == "")
check("NBS10 번역된 시그널 카드도 차단(실측 JUP — 신호 ID·정지 손실·방향)",
      _nb._is_trade_setup(
          "📍신호 ID: #2227📍\n코인: $JUP/USDT (2-5X)\n방향: 긴\n"
          "정지 손실: 0.2160\n🚫20% 손실(2x)🚫") is True)
check("NBS11 '코인:'·'방향:' 라벨이 없는 정상 분석은 통과(과필터 방지)",
      _nb._is_trade_setup(
          "BTC는 일일 76,155에 있으며 74,000~80,000 범위를 유지하고 있습니다. "
          "강세 사례: 82,000을 클리어하고 추세선을 돌파합니다.") is False)

# ── MB13~MB15: 잘린 뉴스의 소비 취소 (2026-09-14 감사 F1) ────────────────
# 사고: _fit_telegram 이 인자를 제자리 변형하고 같은 객체를 반환해, 호출부의
# `len(fitted) < len(lines)` 가드가 **영구히 False** 였다 → 길이 방어로 잘려 나간
# 뉴스까지 consumed=1 로 찍혀 영영 못 보게 된다. 아래가 그 계약을 못 박는다.
_mb3_in = ["헤더", _mb.telegram.SEP, "시장 A"]
_mb3_news_start = len(_mb3_in)
_mb3_full = _mb3_in + ["📰 <b>주요 뉴스</b>",
                       "   <b>AAA</b> · @c", "   " + "가" * 1500,
                       "   <b>BBB</b> · @c", "   " + "나" * 1500,
                       "   <b>CCC</b> · @c", "   " + "다" * 1500]
_mb3_snapshot = list(_mb3_full)
_mb3_fit = _mb._fit_telegram(_mb3_full, _mb3_news_start)
check("MB13 _fit_telegram 은 입력 리스트를 변형하지 않는다(F1 근본 원인)",
      _mb3_full == _mb3_snapshot)
check("MB13b 잘린 결과는 원본보다 짧다(비교 가드가 실제로 동작할 수 있다)",
      len(_mb3_fit) < len(_mb3_full))
_mb3_kept = sum(1 for x in _mb3_fit[_mb3_news_start:] if x.startswith("   <b>"))
check("MB14 실린 뉴스 건수를 헤더 줄로 셀 수 있다 — 3건 중 일부만 남는다",
      0 < _mb3_kept < 3)
check("MB14b 요약이 잘려 헤더만 남은 항목은 통째로 빠진다(내용 못 본 뉴스를 "
      "소비 처리하지 않는다)",
      not _mb3_fit[-1].startswith("   <b>"))
# 뉴스가 전량 제거되면 그 앞 구분선도 함께 걷힌다 (감사 F6)
_mb3_tiny = ["헤더", _mb.telegram.SEP, "시장 A", _mb.telegram.SEP]
_mb3_ns2 = len(_mb3_tiny)
_mb3_big = _mb3_tiny + ["📰 <b>주요 뉴스</b>", "   " + "라" * 5000]
_mb3_fit2 = _mb._fit_telegram(_mb3_big, _mb3_ns2)
check("MB15 뉴스 전량 제거 시 헤더 앞 구분선도 함께 제거(빈 ━━━ 잔류 없음)",
      _mb3_fit2 == ["헤더", _mb.telegram.SEP, "시장 A"])

_mb_tail = ["   <b>SYM</b> · @ch", "   " + "가" * 200]
_mb_lines = _mb_head + ["📰 <b>어제의 뉴스</b>"] + _mb_tail * 40
_mb_fit = _mb._fit_telegram(list(_mb_lines), news_start=len(_mb_head))
check("MB8 길이 초과 시 뉴스 줄부터 제거 — 한도 이내로 축소",
      sum(len(x) + 1 for x in _mb_fit) <= _mb._TELEGRAM_MAX_CHARS
      and len(_mb_fit) < len(_mb_lines))
check("MB8b 시장환경 머리 블록은 보존",
      _mb_fit[:len(_mb_head)] == _mb_head)
_mb_small = ["가" * 10, "나" * 10]
check("MB8c 한도 이내면 무변경", _mb._fit_telegram(list(_mb_small), -1) == _mb_small)

_mbc.close()
os.unlink(_MB_DB)

# ─── GG1: 등급 게이트 상향 (2026-09-13 A안, D → C) ───────────────────────
from collector.grading import meets_min_grade as _mmg
check("GG1 alert_min_grade 기본값 'C'", _st_cfg.get("alert_min_grade") == "C")
check("GG1b D 등급은 게이트 탈락(최하위 승률 22.7% · TP1 도달 0건)",
      not _mmg("D", _st_cfg.get("alert_min_grade")))
check("GG1c C 이상은 그대로 통과(상위 표본 무손상)",
      all(_mmg(g, _st_cfg.get("alert_min_grade")) for g in ("C", "B", "A", "S")))

print(f"\n{'='*40}")
print(f"  infra 테스트: {n_checks}건 {'전부 통과 ✅' if ok else '실패 있음 ❌'}")
print(f"{'='*40}")
sys.exit(0 if ok else 1)
