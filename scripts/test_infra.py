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

# NB1: 정상 발송
_p = {"description": "AAVE testing this text of 60+ chars long enough for min "
                     "length filter passthrough here", "url": "https://t.me/x/1"}
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

# NB3: 다른 코인은 발송 가능 (같은 채널 계속)
r = _nb.maybe_send_news_brief(_nbc, _p, "LINK", "ch1", now=1786900000 + 60)
_nbc.commit()
check("NB3 다른 코인은 정상 발송", r == "ok" and len(_sent_log) == 1)
r = _nb.maybe_send_news_brief(_nbc, _p, "UNI", "ch1", now=1786900000 + 120)
_nbc.commit()
check("NB3b 3번째 발송 정상", r == "ok" and len(_sent_log) == 2)
# 4번째: 채널당 상한 3건 도달
r = _nb.maybe_send_news_brief(_nbc, _p, "ETH", "ch1", now=1786900000 + 180)
check("NB4 채널당 하루 상한 3건 도달 → 스킵", r == "skipped")

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
          "description": "Body text differs from the headline and is long "
                         "enough to pass the minimum length filter easily.",
          "url": ""}
r = _nb.maybe_send_news_brief(_nbc, _p_sep, "ADA", "ch6", now=1786900000 + 86400 + 600)
_nbc.commit()
check("NB10b 독립 title 은 결합 유지",
      r == "ok" and "Headline about Cardano" in _sent_log[0][0]
      and "Body text differs" in _sent_log[0][0])
_st_cfg.SETTINGS["news_translate_enabled"] = True

_nbc.close()
os.unlink(_nb_db)
_tg2.send = _orig_send


# ─── 결과 ────────────────────────────────────────────────────────────

print(f"\n{'='*40}")
print(f"  infra 테스트: {n_checks}건 {'전부 통과 ✅' if ok else '실패 있음 ❌'}")
print(f"{'='*40}")
sys.exit(0 if ok else 1)
