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
with patch.object(tg, "send", return_value=True) as mock_send:
    result = tg._split_send(short_msg, "low")
    check("_split_send: 짧은 메시지 → send 1회", mock_send.call_count == 1)
    check("_split_send: 짧은 메시지 → True", result is True)

long_lines = [f"라인{i:04d} " + "x" * 80 for i in range(100)]
long_msg = "\n".join(long_lines)
assert len(long_msg) > 4096
with patch.object(tg, "send", return_value=True) as mock_send:
    with patch("time.sleep"):
        result = tg._split_send(long_msg, "high")
    check("_split_send: 긴 메시지 → 다건 분할", mock_send.call_count >= 2)
    check("_split_send: 전부 성공 → True", result is True)
    for call_args in mock_send.call_args_list:
        chunk = call_args[0][0]
        check(f"_split_send: 청크 ≤4096 ({len(chunk)}자)", len(chunk) <= 4096)

with patch.object(tg, "send", side_effect=[True, False, True]) as mock_send:
    with patch("time.sleep"):
        result = tg._split_send(long_msg, "low")
    check("_split_send: 일부 실패 → False", result is False)


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


# ─── 결과 ────────────────────────────────────────────────────────────

print(f"\n{'='*40}")
print(f"  infra 테스트: {n_checks}건 {'전부 통과 ✅' if ok else '실패 있음 ❌'}")
print(f"{'='*40}")
sys.exit(0 if ok else 1)
