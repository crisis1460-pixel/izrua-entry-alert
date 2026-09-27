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
# 2026-09-27 CI 시한폭탄 수리: 무장 관용 기준시각을 테스트 전역에서 먼 미래로 고정
# (운영 기본값 09-27 10:48 KST 는 "수집=지금−Nh" 픽스처를 실행 시각에 따라 신규/레거시로 뒤바꾼다).
from config import settings as _cfg_arm  # noqa: E402
_cfg_arm.SETTINGS["watch_arming_since_ts"] = 9e12
# 뉴스 신선도 기준(news_max_age_hours=48, 09-27)도 이 파일에선 끈다 — 여기 뉴스 픽스처는
# 순위·렌더 검증용 절대 시각(09-10~09-27)이라 실행일에 따라 전부 "오래된 글"이 된다.
# 신선도 규칙 자체는 test_morning_brief NEWS-AGE1~4 가 검증한다.
_cfg_arm.SETTINGS["news_max_age_hours"] = 0

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

# 우호 + CVD 매도 우위 → 중립 강등. 2026-09-27 S2 D8: 근거도 보정 사유로 교체
# (종전 "중립 (자금 유입)" 은 라벨-근거 역전 — 감사 D 38/150)
v = derive_supply_verdict(0.005, 5.0, 2.0, cvd_ratio=-0.2)
check("수급보정: 우호+CVD매도 → 중립(매도 우위)", v == ("중립", "매도 우위"))

# 우호 + 매도벽 → 중립 강등
v = derive_supply_verdict(0.005, 5.0, 2.0, bid_ask_ratio=0.5)
check("수급보정: 우호+매도벽 → 중립(매도벽)", v == ("중립", "매도벽"))

# 중립 + 경고 2개 → 주의 강등
v = derive_supply_verdict(0.005, 5.0, -2.0, cvd_ratio=-0.2, bid_ask_ratio=0.5)
check("수급보정: 중립+경고2 → 주의(첫 경고 사유)", v == ("주의", "매도 우위"))

# 중립 + 확인 2개 → 우호 상향 (둘 다 필요)
v = derive_supply_verdict(0.005, None, None, cvd_ratio=0.2, bid_ask_ratio=2.0)
check("수급보정: 중립+확인2 → 우호(첫 확인 사유)", v == ("우호", "매수 우위"))

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
# 2026-09-27 v3: |김프| ≥ 3% 일 때만 표시 → 화살표 검증 값을 2.15 → 3.15 로 올림.

_cluster = [{"coin_symbol": "BTC", "entry_usd": 100.0, "score": 50,
             "grade": "B", "author": "tester"}]
_txt = tg.render_alert("touch", "BTC", _cluster, 140000.0, 1400.0,
                       kimchi_pct=3.15, kimchi_delta=0.8)
check("김프화살표: 급변 시 ▲ 표시", "김프: +3.15% ▲" in _txt)

_txt = tg.render_alert("touch", "BTC", _cluster, 140000.0, 1400.0,
                       kimchi_pct=3.15, kimchi_delta=-0.7)
check("김프화살표: 급락 시 ▼ 표시", "김프: +3.15% ▼" in _txt)

_txt = tg.render_alert("touch", "BTC", _cluster, 140000.0, 1400.0,
                       kimchi_pct=3.15, kimchi_delta=0.3)
check("김프화살표: 임계 미만 → 없음", "김프: +3.15%\n" in _txt or _txt.rstrip().endswith("김프: +3.15%") or ("김프: +3.15%" in _txt and "▲" not in _txt))

_txt = tg.render_alert("touch", "BTC", _cluster, 140000.0, 1400.0,
                       kimchi_pct=3.15)
check("김프화살표: 델타 미전달 → 종전 표기", "김프: +3.15%" in _txt and "▲" not in _txt and "▼" not in _txt)


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

# 2026-09-22 P1 수리로 계약 변경: 종전 `WHERE mfe_pct IS NULL`("최초 1회만")
# 가드는 **단조 병합(MAX/MIN)** 으로 대체됐다. 이유는 그 가드 때문에 누적값이
# 확정값을 영원히 막았기 때문이다 — `update_mfe_mae_running` 이 미종결 구간 내내
# 값을 채워 두므로, IS NULL 가드가 남아 있으면 종결 확정이 아예 기록되지 않는다.
# 검증 의도("값이 조용히 나빠지지 않는다 · 재호출이 안전하다")는 그대로 지킨다.
db.record_mfe_mae(_mc, 1, 99.0, -99.0)
_row = _mc.execute("SELECT mfe_pct, mae_pct FROM levels WHERE id=1").fetchone()
check("MFE/MAE: 더 극단값으로 단조 갱신 (99.0 / -99.0)",
      abs(_row[0] - 99.0) < 0.01 and abs(_row[1] - (-99.0)) < 0.01)

db.record_mfe_mae(_mc, 1, 1.0, -1.0)
_row = _mc.execute("SELECT mfe_pct, mae_pct FROM levels WHERE id=1").fetchone()
check("MFE/MAE: 덜 극단값은 무시 — 재호출이 값을 되돌리지 않는다(멱등)",
      abs(_row[0] - 99.0) < 0.01 and abs(_row[1] - (-99.0)) < 0.01)
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
# 2026-09-27 v3: 💧 저유동·🔴 매도세·⛓ 활성주소는 표시 스위치(기본 OFF) 뒤로 —
# 종전 렌더 경로는 스위치 ON 으로 계속 검증하고, 기본값 숨김은 ITEMS-X 에서 확인.
from notify import telegram as _tg
from config import settings as _set_v3
_v3_saved = {k: _set_v3.SETTINGS[k] for k in
             ("alert_show_dex_liq", "alert_show_dex_sell", "alert_show_active_addr")}
_txt = _tg.render_alert("touch", "TEST", [{"entry_usd": 100.0, "score": 50, "grade": "B",
                        "author": "x", "tp_usd": 110}], 100000.0, 1300.0,
                        dex_stats={"liquidity_usd": 50_000, "buy_ratio_24h": 0.30},
                        active_addr_pctile=12.0)
check("ITEMS-X1 기본값: DEX 저유동·DEX 매도세·활성주소 줄 없음",
      "DEX 저유동" not in _txt and "DEX 매도세" not in _txt and "활성주소" not in _txt)
for _k in _v3_saved:
    _set_v3.SETTINGS[_k] = True
_base_cluster = [{"entry_usd": 100.0, "score": 50, "grade": "B", "author": "x",
                  "author_followers": 1000, "tp_usd": 110, "direction": "long"}]
_base_rep = _base_cluster[0]
# 저유동
_txt = _tg.render_alert("touch", "TEST", _base_cluster, 100000.0, 1300.0, rep=_base_rep,
                       dex_stats={"liquidity_usd": 50_000, "buy_ratio_24h": 0.5})
check("DX4 저유동성 <100k$ 배지 표시(매수 주의 라벨)",
      "DEX 저유동" in _txt and "50k$" in _txt and "· 주의" in _txt)
# 매수세 강함
_txt = _tg.render_alert("touch", "TEST", _base_cluster, 100000.0, 1300.0, rep=_base_rep,
                       dex_stats={"liquidity_usd": 500_000, "buy_ratio_24h": 0.70})
check("DX4b 매수세 ≥65% 배지 표시(매수 유리 라벨)",
      "DEX 매수세" in _txt and "70%" in _txt and "· 매수 우호" in _txt)
# 매도세 강함
_txt = _tg.render_alert("touch", "TEST", _base_cluster, 100000.0, 1300.0, rep=_base_rep,
                       dex_stats={"liquidity_usd": 500_000, "buy_ratio_24h": 0.30})
check("DX4c 매도세 (buy_ratio ≤35%) 배지 표시(매수 부담 라벨)",
      "DEX 매도세" in _txt and "70%" in _txt and "· 주의" in _txt)  # (1-0.30)*100 = 70
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
      "활성주소 상위 14%" in _txt and "· 매수 우호" in _txt)
_txt = _tg.render_alert("touch", "TEST", _base_cluster, 100000.0, 1300.0, rep=_base_rep,
                       active_addr_pctile=12.0)
check("CM7b 저조(≤20) 배지 표시(매수 부담)",
      "활성주소 하위 12%" in _txt and "· 주의" in _txt)

# 2026-09-27 사용자 제보: 백분위를 "7위"(순위)로 표기하던 오류 → "하위 7%"·"상위 12%", 32칼럼 이내
from notify import telegram as _tg_onc
for _p, _want in ((6.7, "⛓ 활성주소 하위 7% · 주의"), (0.0, "하위 1%"),
                  (88.0, "⛓ 활성주소 상위 12% · 매수 우호"), (100.0, "상위 1%")):
    _m_onc = _tg_onc.render_alert("touch", "XRP", [dict(coin_symbol="XRP", entry_usd=1.5225,
                                   tp_usd=1.6149, tps_usd="[1.6149, 1.7704]", grade="A",
                                   score=56, author="a", author_followers=2)],
                                  2071.0, 1360.0, active_addr_pctile=_p)
    _onc = [ln for ln in _m_onc.split(chr(10)) if "활성주소" in ln]
    check(f"ONC {_p}% → '{_want}' · '위' 표기 없음 · 32칼럼 이내",
          _onc and _want in _onc[0] and "위 (" not in _onc[0]
          and _tg_onc._line_width(_onc[0]) <= _tg_onc._MAX_LINE_COLS)
_txt = _tg.render_alert("touch", "TEST", _base_cluster, 100000.0, 1300.0, rep=_base_rep,
                       active_addr_pctile=50.0)
check("CM7c 중립(20~80) → 배지 없음", "온체인" not in _txt)
for _k, _v in _v3_saved.items():
    _set_v3.SETTINGS[_k] = _v

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

# ST6: telegram 소셜 배지 (2026-09-27 v3 재보정: '매수 유리' 폐지, 평소보다 크게
# 낮을 때만 경고 — stwits_warn_max_ratio 0.65·표본 10 이상)
_txt = _tg.render_alert("touch", "TEST", _base_cluster, 100000.0, 1300.0, rep=_base_rep,
                       stwits_bullish_ratio=0.85, stwits_n=20)
check("ST6 매수 비중 높음(0.85) → 배지 없음('매수 유리' 폐지)",
      "소셜" not in _txt and "매수 유리" not in _txt)
_txt = _tg.render_alert("touch", "TEST", _base_cluster, 100000.0, 1300.0, rep=_base_rep,
                       stwits_bullish_ratio=0.20, stwits_n=10)
check("ST6b 매수 비중 낮음(0.20, n=10) → 경고 배지 '💬 소셜: 매도 우세 · 주의'",
      "💬 소셜: 매도 우세 · 주의" in _txt and "매수 부담" not in _txt)
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
# MB·MBR 픽스처는 블록 상한 5(09-13 A안) 기준으로 짜여 있다 — 상한 **메커니즘**(컷·소비·잔여)
# 검증이라 값은 고정해 두고 끝에 되돌린다(09-28 운영값 12).
_MB_ORIG_BLOCK_MAX = _mb._NEWS_BLOCK_MAX
_mb._NEWS_BLOCK_MAX = 5

# MB 블록 픽스처는 원문(summary_en) 없는 **번역문 경로**(종전 렌더) 검증이다. 2026-09-27
# 리뷰 RV2-N8 부터 v2 스위치가 켜져 있으면 원문 없는 행은 싣지 않고 소비만 하므로,
# 종전 경로 계약은 스위치 OFF(롤백 경로)에서 검증한다. 블록 끝(_mbc.close)에서 복구.
_st_cfg.SETTINGS["news_structured_enabled"] = False

_MB_DB = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
db.init_db(_MB_DB)
_mbc = sqlite3.connect(_MB_DB)
_mbc.row_factory = sqlite3.Row
_MB_DAY = "2026-09-12"
_MB_NOW = 1789000000.0
# 뉴스 큐 적재 시각은 **실행 시각 기준 상대값**(2026-09-27): 브리핑이 게시 48h 초과 행을
# 제외하므로(news_max_age_hours) 절대 시각 픽스처는 시한폭탄이 된다.
_MB_QNOW = time.time() - 120

# MB1: 빈 큐/빈 적중 → 블록 통째 생략
check("MB1 빈 TP 적중 → 🏁 블록 생략", _mb._tp_hit_lines(_mbc, _MB_NOW) == [])
_mb_ids = []
check("MB2 빈 뉴스 큐 → 📰 블록 생략",
      _mb._news_lines(_mbc, _mb_ids) == [] and _mb_ids == [])

# MB3: 후보 판정 + 중요도 정렬 + 5건 컷 + "외 N건" + consumed 처리
# (2026-09-13 A안 → 2026-09-22 중요도순 랭킹 리팩터로 픽스처 갱신)
# 종전엔 큐 앞에서부터 5건만 판정해 그 5건이 그대로 실렸다(도착순 = 게재순).
# 지금은 상한(_NEWS_FETCH_MULT=3배 = 15건)만큼 넉넉히 꺼낸 후보 **전부**를
# 촉매 점수로 매겨 상위 5건을 고른다 — 그래서 "판정한 후보는 전부 소비"까지
# 검증하려면 15건 넘게 채워야 한다. SYM0~4 는 촉매(support, 1점)가 있어
# SYM5~14(무촉매, 0점)보다 항상 위고, SYM15~17 은 상한 밖이라 아예 안 꺼내진다
# — 그 3건이 "외 3건"의 잔여다.
for i in range(15):
    tail = "reclaiming support" if i < 5 else "a routine session"
    db.queue_news_digest(_mbc, f"SYM{i}", f"chan{i}",
                         f"SYM{i} trades near 1,2{i}0 after {tail}. "
                         f"Second sentence is dropped.",
                         f"https://t.me/x/{i}", _MB_DAY, _MB_QNOW + i)
for i in range(15, 18):
    db.queue_news_digest(_mbc, f"SYM{i}", f"chan{i}",
                         f"SYM{i} trades near 1,2{i}0 after a routine session.",
                         f"https://t.me/x/{i}", _MB_DAY, _MB_QNOW + i)
_mbc.commit()
_mb_ids = []
_mb_news = _mb._news_lines(_mbc, _mb_ids)
check("MB3 후보 15건(상한) 전부 판정 + 상위 5건만 렌더 + 헤더에 '외 3건'",
      len(_mb_ids) == 15 and "외 3건" in _mb_news[0])
check("MB3b 항목 줄에 코인·채널·요약 첫 문장(원문 링크 없음)",
      any("SYM0" in x and "@chan0" in x for x in _mb_news)
      and any("SYM0 trades near 1,200" in x for x in _mb_news)
      and not any("https://" in x for x in _mb_news))
check("MB3c 요약은 첫 문장만 (둘째 문장 제외)",
      not any("Second sentence" in x for x in _mb_news))
check("MB3d 촉매 없는 나머지 후보(SYM5~14)는 5건 컷에서 밀려난다",
      not any(f"SYM{i}" in x for i in range(5, 15) for x in _mb_news))
db.consume_news_digest(_mbc, _mb_ids)
_mbc.commit()
check("MB4 consumed=1 처리 후 남은 미소비 3건(상한 밖이라 아예 안 꺼내진 SYM15~17)",
      db.count_news_digest(_mbc) == 3)
_mb_ids2 = []
_mb_news2 = _mb._news_lines(_mbc, _mb_ids2)
check("MB4b 소비된 건은 다음 브리핑에 다시 안 나온다(잔여 3건만 후보)",
      len(_mb_ids2) == 3 and all(i not in _mb_ids for i in _mb_ids2))

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

# ── MBR1~MBR8: 뉴스 중요도 랭킹 (2026-09-22 P5) ───────────────────────────
# 실측: 뉴스가 00~04시 수집 회차에 몰려 들어와 쿼터(5건/일)를 도착순으로
# 채운다 — "먼저 온 5건" ≠ "중요한 5건". 전용 임시 DB(MBQ 블록과 같은 패턴).
_MBR_DAY = "2026-09-22"
_MBR_NOW = 1790000000.0
_MBR_DB = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
db.init_db(_MBR_DB)
_mbr = sqlite3.connect(_MBR_DB)
_mbr.row_factory = sqlite3.Row

# MBR1~2: 늦게 온 규제 촉매 뉴스가 먼저 온 무촉매 뉴스를 순위에서 제친다
db.queue_news_digest(_mbr, "EARLY", "cha",
                     "EARLY trades near 1,000 after a routine session.",
                     "", _MBR_DAY, _MBR_NOW)              # 먼저 도착, 무촉매
db.queue_news_digest(_mbr, "LATE", "chb",
                     "상원은 LATE 관련 법안 표결을 예정하고 있으며 가격 3,000대에 "
                     "거래되고 있습니다.",
                     "", _MBR_DAY, _MBR_NOW + 100)         # 늦게 도착, 규제 촉매
_mbr.commit()
_mbr_ids1 = []
_mbr_lines1 = _mb._news_lines(_mbr, _mbr_ids1)
_early_pos = next((i for i, x in enumerate(_mbr_lines1) if "EARLY" in x), None)
_late_pos = next((i for i, x in enumerate(_mbr_lines1) if "LATE" in x), None)
check("MBR1 늦게 온 규제 촉매 뉴스도 실린다", _late_pos is not None)
check("MBR2 촉매 있는 뉴스가 먼저 온 무촉매 뉴스보다 위에 실린다(도착순 아님)",
      _early_pos is not None and _late_pos < _early_pos)
_mbr.execute("DELETE FROM news_digest_queue")
_mbr.commit()

# MBR3~3b: 6건 이상일 때 저점수 후보는 5건 컷에서 탈락하지만 소비는 전부 된다
db.queue_news_digest(_mbr, "HI", "chc",
                     "규제 승인 소식으로 HI 가 4,500 부근까지 상승했습니다.",
                     "", _MBR_DAY, _MBR_NOW)
for i in range(6):
    db.queue_news_digest(_mbr, f"LO{i}", f"chd{i}",
                         f"LO{i} trades near 2,{i}00 after a routine session.",
                         "", _MBR_DAY, _MBR_NOW + 10 + i)
_mbr.commit()
_mbr_ids2 = []
_mbr_lines2 = _mb._news_lines(_mbr, _mbr_ids2)
_mbr_body2 = "\n".join(_mbr_lines2)
check("MBR3 6건 이상일 때 저점수 후보는 5건 컷에서 탈락(고점수 HI 는 남음)",
      "HI" in _mbr_body2 and sum(1 for i in range(6) if f"LO{i}" in _mbr_body2) == 4)
check("MBR3b 후보 7건 전부 소비 처리(컷에서 탈락한 것도 포함)", len(_mbr_ids2) == 7)
_mbr.execute("DELETE FROM news_digest_queue")
_mbr.commit()
_mb._NEWS_BLOCK_MAX = _MB_ORIG_BLOCK_MAX
check("MB-CAP 운영 뉴스 블록 상한 12 · 수집 상한 12/일 · 채널 4/일(09-28 대표 요청)",
      _mb._NEWS_BLOCK_MAX == 12 and _cfg_arm.get("news_alert_max_global_per_day") == 12
      and _cfg_arm.get("news_alert_max_per_channel_per_day") == 4)

# MBR4~4c: 같은 코인 중복은 점수 최고 1건만 (실측 XRP·BTC CLARITY 법안 중복)
db.queue_news_digest(_mbr, "XRP", "cha",
                     "XRP는 1,200에서 상승세를 보이고 있습니다.",  # 촉매 없음 → 낮은 점수
                     "", _MBR_DAY, _MBR_NOW)
db.queue_news_digest(_mbr, "XRP", "chb",
                     "명확성 법안이 상원 표결을 앞두고 있어 XRP 가격을 촉매할 "
                     "수 있으며 XRP는 1,500에 거래되고 있습니다.",
                     "", _MBR_DAY, _MBR_NOW + 10)
_mbr.commit()
_mbr_ids3 = []
_mbr_lines3 = _mb._news_lines(_mbr, _mbr_ids3)
check("MBR4 같은 코인 중복은 점수 최고 1건만 렌더",
      sum(1 for x in _mbr_lines3 if "<b>XRP</b>" in x) == 1)
check("MBR4b 소비 처리는 판정한 후보 둘 다(중복이어도 큐에서는 둘 다 뺀다)",
      len(_mbr_ids3) == 2)
check("MBR4c 남는 건 높은 점수 쪽(법안 촉매, @chb)",
      "@chb" in "\n".join(_mbr_lines3) and "@cha" not in "\n".join(_mbr_lines3))
_mbr.execute("DELETE FROM news_digest_queue")
_mbr.commit()

# MBR5~6: 동점이면 채널 다양성이 먼저, 그다음 최신순 — 같은 채널 두 건 중
# 더 최근 것(AAA)이 먼저 뽑히고 나면, 그다음은 "다른 채널"(CCC)이 같은 채널의
# 나머지(BBB, 더 최근인데도)보다 우선한다.
db.queue_news_digest(_mbr, "AAA", "cha",
                     "AAA trades near 1,100 after a routine session.",
                     "", _MBR_DAY, _MBR_NOW + 20)          # 채널 cha, 최신
db.queue_news_digest(_mbr, "BBB", "cha",
                     "BBB trades near 1,200 after a routine session.",
                     "", _MBR_DAY, _MBR_NOW + 10)          # 채널 cha, 중간
db.queue_news_digest(_mbr, "CCC", "chb",
                     "CCC trades near 1,300 after a routine session.",
                     "", _MBR_DAY, _MBR_NOW)                # 채널 chb, 가장 오래됨
_mbr.commit()
_mbr_ids4 = []
_mbr_lines4 = _mb._news_lines(_mbr, _mbr_ids4)
check("MBR5 세 건 모두 5건 컷 안이라 전부 실린다(채널 다양성은 순서에만 영향)",
      all(s in "\n".join(_mbr_lines4) for s in ("AAA", "BBB", "CCC")))
_aaa_pos = next(i for i, x in enumerate(_mbr_lines4) if "AAA" in x)
_bbb_pos = next(i for i, x in enumerate(_mbr_lines4) if "BBB" in x)
_ccc_pos = next(i for i, x in enumerate(_mbr_lines4) if "CCC" in x)
check("MBR6 동점 시 채널 다양성이 먼저(다른 채널 CCC 가 같은 채널 BBB 보다 위) "
      "→ 그다음 최신순(AAA 가 가장 먼저)",
      _aaa_pos < _ccc_pos < _bbb_pos)
_mbr.close()
os.unlink(_MBR_DB)

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
_st_cfg.SETTINGS["news_structured_enabled"] = True

# ═══ NEWS-*: 뉴스 v2 (2026-09-27 사용자 결정 Q1~Q3 + "내용별 문장 길게" + 고아단어) ═══
# 기획안 plan_2026-09-27_뉴스분석_고도화 추천안 A. 픽스처는 기획 샘플 13건의 **채널
# 원문(영문)** 을 그대로 쓴다(링크 안내 꼬리만 제거, 700자 컷).
from notify import news_parse as _np
from notify import news_brief as _nb2
from notify import morning_brief as _mb4
import re as _re4

_NEWS_FX = {
    'wolfoftrading/6443': '🚨Trading Data\nWe are almost reaching the peak of yearly high.\nAnd we have over 1.69B BTC ETF Net-flow 👀💹',
    'cryptosignals0rg/19331': 'Solana Treasury Giant Teams Up With Kraken — Is Institutional Demand Entering A New Phase?\nA validator partnership doesn’t sound dramatic on its own. But when the company signing it holds over 1.2 million SOL and just raised $300 million to build institutional-grade Solana infrastructure, the terms of that partnership start to matter a lot more.\nSolanaFloor reported on July 30, 2026 that Solana treasury company Solmate, which holds over 1.2 million SOL, has partnered with Kraken Institutional to support its Solana validator infrastructure and enhance its staking economics. SOL trades at $74.84, up 1.0% over the past 24 hours.',
    'cryptosignals0rg/19523': 'Cronos’ $74M Tectonic Exploit Exposes a Risk Investors Can’t Ignore\nA $74 million DeFi exploit has turned the spotlight on an uncomfortable question for Cronos investors: how much risk is hiding beneath the surface of a blockchain ecosystem that can be brought to a halt by an attack on a single lending protocol?\nOn August 30, Tectonic, the largest lending platform on Cronos, was exploited after an attacker manipulated the price of its native TONIC token and used the inflated value as collateral to borrow other crypto assets.',
    'wolfoftrading/6447': '🇮🇷 IRAN–US TENSIONS ESCALATE\nIran has responded to Trump’s latest threats, warning that any further attacks could trigger “more severe, crushing and unpredictable blows.”\n⚠️ With the Strait of Hormuz and regional energy infrastructure already under pressure, markets are pricing in higher geopolitical and oil risks.\nMeanwhile, US–Iran talks are still ongoing meaning headline-driven volatility remains extremely high.\n📉 Risk-off pressure = bearish for BTC in the short term.\n📈 Any credible de-escalation/Hormuz deal could quickly reverse the move.',
    'wolfoftrading/6408': 'A whale has bought $9,000,000 in #SOL this week.\nSmart money is more focused on alts now.',
    'cryptosignals0rg/19608': "Stellar (XLM) Support at $0.1747 Holds; a Strong Rebound May Have Started\nThe Stellar token continues to benefit from significant inflows of tokenized funds, claiming a massive share of over $2.5B. This has translated into some traction in the token's market. Even technical indicators are aligning to hint at a possible significant upward rebound.",
    'BitcoinBullets/17281': "#AVAX Market Analysis\nAVAX is at 8.261 on the 4h, pressing right into the resistance zone near 8.300 that's capped price since the mid August spike, with the ascending trendline from mid August still holding beneath.\nBull case: clear 8.300 and break the resistance, opening a path toward fresh highs.\nBear case: reject here and fade back toward the trendline near 7.200.\nMulti week resistance finally breaks, or another rejection here?\n➖➖➖➖➖➖➖\nBitcoin Bullets® Trading",
    'BitcoinBullets/17290': '#XRP Market Analysis\nXRP is at 1.4944 on the 4h, pulling back from the 1.6500 highs and landing right on the demand zone near 1.4500 built during the September consolidation.\nBull case: hold above 1.4500 and resume the push back toward 1.5500 and 1.6500.\nBear case: lose 1.4500 and slide back toward 1.3100.\nDemand zone holds, or does the pullback deepen?\n➖➖➖➖➖➖➖\nBitcoin Bullets® Trading',
    'wolfoftrading/6440': '$ETHUSDT Update: 1D\nExpecting for Ethereum to be bullish within the next few days.\nWent steadily to the upside after breaking from above of the major short term resistance.\nHopefully everything will keep on running smoothly. 💹',
    'wolfoftrading/6432': 'Besides that\n$INJ is forming a very clean cup and handle pattern, we are expecting x2 spot on the mid-term.',
    'cryptosignals0rg/19634': 'The Reason Why the Four Seasons of Crypto Are Important to Investors\nThe headlines are dominated by crypto. However, following a strong bullish correction that brought prices to new highs, the latest BTC pullback is raising familiar questions for investors.',
    'wolfoftrading/6456': '$CHZUSDT Update:\nOur Adam & Eve is going for it, already went 8% from where we posted.\nEnjoy. We still expect it to go up. $0.01928 - $0.02 is the second target',
    'wolfoftrading/6419': 'SUMMARY OF FED DECISION (9/16/2026):\n1. Fed hikes interest rates by 25 bps for first time since July 2023\n2. The decision was made in a 12-0 unanimous vote\n3. Fed says the decision will support a "timelier" return to 2% inflation\n4. Median Fed forecast shows one more 25 basis point rate hike in 2026\n5. Fed says job gains are strong and the unemployment rate has "changed little"\n6. "The Committee will deliver price stability," the Fed\'s statement says\nHigher for longer is back.',
    'wolfoftrading/6417': "CRYPTO HAS FAILED ITS FIRST MAJOR TEST.\nThe Crypto Clarity Act failed to advance in the US Senate today.\nThat doesn't mean it's all over, but now the chances of approval in 2026 are very low.\nNow, there are two big events to look forward to tomorrow.\nThe Fed's interest rate decision with a 94% chance of a rate hike.\nAlong with that, the House Committee will vote on advancing the Strategic Bitcoin Reserve Bill.\nA rate hike is certain, so it won't impact the markets much.\nBut if Kevin Warsh hints of more hikes, it could nuke the market.\nAlong with that, if SBR advances tomorrow, it could be a good sign.\nFailing to advance means two consecutive crypto bill failures, and markets will most likely",
    'wolfoftrading/6448': '🚨UPDATE: $351.6M drained from Bitget.\nPrivate keys were not compromised. Attackers exploited a wallet backend vulnerability to spoof transaction data and bypass authorization. This supports my thesis that AI likely identified the flaw. Malicious actors use AI agents 24/7 for scanning, pen testing, reverse engineering, and finding missed exploits. Anthropic documents automated AI "exploit foundries," and Google reports attackers adopting agentic workflows. If holding significant assets, consider self-custody and multisig.',
    'cryptosignals0rg/19462': 'How the Term Labs Exploit Drained $8.5M Through a Governance Attack\nA DeFi exploit at Term Labs shows that an attacker may not always need to break a smart contract directly to steal millions of dollars.\nSometimes, the most valuable target is the system that decides who has permission to move the money.',
    'cryptosignals0rg/19370': 'Can Bybit Recover Funds From the $1.5 Billion Lazarus Hack?\nSuing a nation-state rarely produces a check in the mail. But eighteen months after crypto’s largest heist on record, Bybit is betting that a US courtroom can accomplish something blockchain forensics alone couldn’t.\nBybit is suing North Korea over the $1.5 billion Lazarus Group crypto heist, with a US federal court issuing a preliminary injunction freezing identified assets linked to the hack as the civil case moves forward.',
    'wolfoftrading/6415': "JUST IN: AN ANONYMOUS WHALE JUST BOUGHT OVER 1,000 #BITCOIN WORTH $82,000,000 OVER THE LAST 4 DAYS\nTHAT'S 250 BTC PER DAY. 10 BTC AN HOUR.\nSMART MONEY KNOWS. WE'RE GOING HIGHER 🚀",
    'wolfoftrading/6425': 'After two days of the most significant net outflows seen in the past four months, ETFs turned back to buying yesterday.\nTotal crypto ETF netflows stand at +$119M. Bitcoin captured the bulk of the flows with +$159M. Ethereum, on the other hand, saw $39M in net outflows.\nWorth noting: $4.3M in inflows for Hype.\nThis end of week appears calmer on the ETF front.',
}

# ── NEWS-DUP: 같은 글 재적재 차단 (S0) ─────────────────────────────────
# 실측 09-21~27 news_digest_queue 34행(고유 URL 18건)을 **그대로 재생**한다. 종전엔
# 코인 24h 쿨다운만 있어 16행이 재적재됐다(wolfoftrading/6440 ETH 6일 연속).
_NEWS_ROWS34 = [
    ('NEAR', 'cryptosignals0rg', 'https://t.me/cryptosignals0rg/19611', 1789917759.8170037),
    ('XLM', 'cryptosignals0rg', 'https://t.me/cryptosignals0rg/19608', 1789917759.9131966),
    ('XEC', 'wolfoftrading', 'https://t.me/wolfoftrading/6422', 1789917766.4401255),
    ('ETH', 'BitcoinBullets', 'https://t.me/BitcoinBullets/17283', 1789917771.8071363),
    ('AVAX', 'BitcoinBullets', 'https://t.me/BitcoinBullets/17281', 1789917773.122607),
    ('UNI', 'cryptosignals0rg', 'https://t.me/cryptosignals0rg/19615', 1790004866.6422014),
    ('NEAR', 'cryptosignals0rg', 'https://t.me/cryptosignals0rg/19611', 1790004869.2305193),
    ('ETH', 'wolfoftrading', 'https://t.me/wolfoftrading/6440', 1790004875.9102437),
    ('BTC', 'wolfoftrading', 'https://t.me/wolfoftrading/6439', 1790004877.293524),
    ('AVAX', 'BitcoinBullets', 'https://t.me/BitcoinBullets/17281', 1790004884.0770476),
    ('UNI', 'cryptosignals0rg', 'https://t.me/cryptosignals0rg/19615', 1790091757.0618758),
    ('BTC', 'wolfoftrading', 'https://t.me/wolfoftrading/6443', 1790091763.6052155),
    ('ETH', 'wolfoftrading', 'https://t.me/wolfoftrading/6440', 1790091764.8781223),
    ('TAO', 'BitcoinBullets', 'https://t.me/BitcoinBullets/17286', 1790091771.2359695),
    ('AVAX', 'BitcoinBullets', 'https://t.me/BitcoinBullets/17281', 1790091772.6046033),
    ('BTC', 'cryptosignals0rg', 'https://t.me/cryptosignals0rg/19634', 1790179373.6561387),
    ('ETH', 'wolfoftrading', 'https://t.me/wolfoftrading/6440', 1790179380.7319105),
    ('INJ', 'wolfoftrading', 'https://t.me/wolfoftrading/6432', 1790179382.0064828),
    ('SOL', 'BitcoinBullets', 'https://t.me/BitcoinBullets/17288', 1790179388.4417865),
    ('TAO', 'BitcoinBullets', 'https://t.me/BitcoinBullets/17286', 1790179389.976084),
    ('BTC', 'cryptosignals0rg', 'https://t.me/cryptosignals0rg/19634', 1790266726.7456489),
    ('ETH', 'wolfoftrading', 'https://t.me/wolfoftrading/6440', 1790266733.9682267),
    ('INJ', 'wolfoftrading', 'https://t.me/wolfoftrading/6432', 1790266735.9229422),
    ('XRP', 'BitcoinBullets', 'https://t.me/BitcoinBullets/17290', 1790266742.539864),
    ('BCH', 'BitcoinBullets', 'https://t.me/BitcoinBullets/17289', 1790266744.2791471),
    ('BTC', 'cryptosignals0rg', 'https://t.me/cryptosignals0rg/19634', 1790354076.269112),
    ('ETH', 'wolfoftrading', 'https://t.me/wolfoftrading/6440', 1790354083.1365957),
    ('TAO', 'BitcoinBullets', 'https://t.me/BitcoinBullets/17292', 1790354089.8988764),
    ('XRP', 'BitcoinBullets', 'https://t.me/BitcoinBullets/17290', 1790354091.4203422),
    ('CHZ', 'wolfoftrading', 'https://t.me/wolfoftrading/6456', 1790426569.6949127),
    ('BTC', 'wolfoftrading', 'https://t.me/wolfoftrading/6447', 1790440998.996615),
    ('ETH', 'wolfoftrading', 'https://t.me/wolfoftrading/6440', 1790441000.4928064),
    ('TAO', 'BitcoinBullets', 'https://t.me/BitcoinBullets/17292', 1790441007.0217144),
    ('XRP', 'BitcoinBullets', 'https://t.me/BitcoinBullets/17290', 1790441008.5645652),
]
_nd_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
db.init_db(_nd_db)
_ndc = sqlite3.connect(_nd_db)
_ndc.row_factory = sqlite3.Row
_st_cfg.SETTINGS["news_alert_send_enabled"] = False
_st_cfg.SETTINGS["news_translate_enabled"] = False
_nd_res = []
for _sym, _ch, _url, _ts in _NEWS_ROWS34:
    _p = {"title": f"{_sym} update", "description":
          f"{_sym} update\n{_sym} trades near 1,234.5 after testing support; "
          f"analysts watch the 1,300.0 resistance next.", "url": _url,
          "published_at": _ts - 3600}
    _nd_res.append(_nb2.maybe_send_news_brief(_ndc, _p, _sym, _ch, now=_ts))
    _ndc.commit()
_nd_q = [dict(r) for r in _ndc.execute("SELECT url FROM news_digest_queue")]
check("NEWS-DUP1 실측 34행 재생 → 고유 18건만 적재(재적재 16행 차단)",
      len(_nd_q) == 18 and len({r["url"] for r in _nd_q}) == 18)
check("NEWS-DUP2 wolfoftrading/6440(ETH) — 6일 연속 → 1회만",
      sum(1 for r in _nd_q if r["url"].endswith("/6440")) == 1)
check("NEWS-DUP3 차단된 16행은 skipped(쿨다운·상한 카운트도 안 먹는다)",
      _nd_res.count("queued") == 18 and _nd_res.count("skipped") == 16
      and _ndc.execute("SELECT COUNT(*) n FROM alerts_log WHERE kind='news'").fetchone()["n"] == 18)
# 소비된 뒤에도 같은 글은 다시 안 들어온다(브리핑에 한 번 나간 글 = 영구 차단)
_ndc.execute("UPDATE news_digest_queue SET consumed=1")
_ndc.commit()
_p = {"description": "ETH trades near 2,645.86 after the breakout above the major resistance.",
      "url": "https://t.me/wolfoftrading/6440"}
check("NEWS-DUP4 소비(consumed=1)된 URL 도 재적재 금지",
      _nb2.maybe_send_news_brief(_ndc, _p, "ETH", "wolfoftrading",
                                 now=1790441000 + 86400 * 3) == "skipped")
check("NEWS-DUP5 원문(summary_en)·게시시각(posted_at) 보관(v2 렌더 재료)",
      _ndc.execute("SELECT summary_en, posted_at FROM news_digest_queue LIMIT 1").fetchone()[0]
      .startswith("NEAR update"))
# 실시간 발송 경로도 URL 원장을 남겨 같은 글을 두 번 보내지 않는다
_st_cfg.SETTINGS["news_alert_send_enabled"] = True
_orig_send_nd = _tg2.send
_nd_sent = []
_tg2.send = lambda text, urgency="high", reply_to_message_id=None: (_nd_sent.append(text) or 1)
_p = {"description": "LINK trades near 12.345 after reclaiming the 12.000 support zone today.",
      "url": "https://t.me/x/rt1"}
_r1 = _nb2.maybe_send_news_brief(_ndc, _p, "LINK", "chrt", now=1790900000)
_ndc.commit()
_r2 = _nb2.maybe_send_news_brief(_ndc, _p, "LINK", "chrt", now=1790900000 + 86400 * 2)
check("NEWS-DUP6 실시간 발송분도 원장(consumed=1) 기록 → 이틀 뒤 재발송 차단",
      _r1 == "ok" and _r2 == "skipped" and len(_nd_sent) == 1
      and db.count_news_digest(_ndc) == 0)
_tg2.send = _orig_send_nd
_st_cfg.SETTINGS["news_alert_send_enabled"] = False
_ndc.close()
os.unlink(_nd_db)

# ── NEWS-EN: 수치 판정은 영문 원문 기준 (S0 — "$1.69B" → "16억 9천만 개" 사고) ──
_etf_ko = "🚨거래 데이터\n연간 최고치에 거의 도달했습니다.\n그리고 우리는 16억 9천만 개가 넘는 BTC ETF Net-flow를 보유하고 있습니다"
_etf_en = _NEWS_FX["wolfoftrading/6443"]
check("NEWS-EN1 원문이 있으면 원문으로 판정 → ETF 자금 뉴스 통과(실사고 재현)",
      _mb4._is_queued_noise("BTC", _etf_ko, _etf_en) is False)
check("NEWS-EN2 원문 없는 과거 행도 한글 수 표기(억·천만)를 수치로 인정",
      _mb4._is_queued_noise("BTC", _etf_ko) is False)
check("NEWS-EN3 원문 금액 추출 — 단위 금액만($1.69B), 가격 수준은 제외",
      [a[0] for a in _np.amounts(_etf_en)] == ["$1.69B"]
      and _np.amounts("SOL trades at $74.84 and BTC at $65,000") == [])

# ── NEWS-P: 파서 유형별 (기획 샘플 원문) ───────────────────────────────
_P = {k: _np.parse(v) for k, v in _NEWS_FX.items()}
check("NEWS-P1 ETF 순유입 → 사실형 etf · 호재 · $1.69B",
      _P["wolfoftrading/6443"]["kind"] == "fact" and _P["wolfoftrading/6443"]["type"] == "etf"
      and _P["wolfoftrading/6443"]["pol"] == 1)
check("NEWS-P2 해킹(Cronos $74M) → 사실형 hack · 악재 · H등급",
      _P["cryptosignals0rg/19523"]["type"] == "hack" and _P["cryptosignals0rg/19523"]["pol"] == -1
      and _P["cryptosignals0rg/19523"]["amounts"][0][0] == "$74M"
      and _P["cryptosignals0rg/19523"]["tier"] == "H")
check("NEWS-P3 매크로(이란-미국 긴장) → macro · 악재",
      _P["wolfoftrading/6447"]["type"] == "macro" and _P["wolfoftrading/6447"]["pol"] == -1)
check("NEWS-P4 고래 매수 $9M → whale · 호재",
      _P["wolfoftrading/6408"]["type"] == "whale" and _P["wolfoftrading/6408"]["pol"] == 1
      and _P["wolfoftrading/6408"]["amounts"][0][0] == "$9M")
check("NEWS-P5 자금 유입 $2.5B + 지지 $0.1747",
      _P["cryptosignals0rg/19608"]["type"] == "flow"
      and _P["cryptosignals0rg/19608"]["levels"].get("support") == "$0.1747")
check("NEWS-P6 제휴(Solmate × Kraken) → partner · 호재 · $300M",
      _P["cryptosignals0rg/19331"]["type"] == "partner"
      and _P["cryptosignals0rg/19331"]["amounts"][0][0] == "$300M")
_sc = _P["BitcoinBullets/17281"]
check("NEWS-P7 차트 시나리오(AVAX) — 콜론 뒤 숫자가 바로 안 와도 분기선 추출(종전 실패 사례)",
      _sc["kind"] == "scenario" and _sc["bull"]["trigger"] == "8.300"
      and _sc["bear"]["trigger"] == "7.200" and _sc["tf"] == "4시간봉" and _sc["price"] == "8.261")
_sc2 = _P["BitcoinBullets/17290"]
check("NEWS-P8 같은 기준선 분기(XRP 1.4500) + 목표(1.6500/1.3100)",
      _sc2["bull"]["trigger"] == _sc2["bear"]["trigger"] == "1.4500"
      and _sc2["bull"]["target"] == "1.6500" and _sc2["bear"]["target"] == "1.3100")
check("NEWS-P9 방향 콜(ETH Update: 1D) → call · 강세 · 일봉",
      _P["wolfoftrading/6440"]["kind"] == "call" and _P["wolfoftrading/6440"]["stance"] == "강세"
      and _P["wolfoftrading/6440"]["tf"] == "일봉")
check("NEWS-P10 방향 콜(INJ 컵앤핸들 x2) — 번역문에선 '그 외에도'만 남던 글",
      _P["wolfoftrading/6432"]["pattern"] == "컵앤핸들" and _P["wolfoftrading/6432"]["target"] == "2배")
check("NEWS-P11 노이즈 — 수치 없는 일반론(Four Seasons)·지난 예측 자찬(CHZ Adam & Eve)",
      _P["cryptosignals0rg/19634"]["kind"] == "noise" and _P["wolfoftrading/6456"]["kind"] == "noise")
check("NEWS-P12 언락(합성 문장) → unlock · 악재 · H등급",
      _np.parse("ARB token unlock of $45M scheduled next week\nTeam and investor tokens "
                "worth $45 million will be released.")["type"] == "unlock"
      and _np.parse("ARB token unlock of $45M scheduled next week")["pol"] == -1)
check("NEWS-P13 연준 25bp 인상 → macro · 악재",
      _P["wolfoftrading/6419"]["type"] == "macro" and _P["wolfoftrading/6419"]["pol"] == -1)
check("NEWS-P14 교양 기사의 '인플레이션' 한 단어로 매크로 판정하지 않음(과추출 방지)",
      _np.parse("Sam Altman's $1 Trillion Problem\nNVIDIA announced the H100 GPU in March 2022.\n"
                "At the time, inflation was skyrocketing and the Fed started hiking rates.")
      .get("type") != "macro")

# ── NEWS-CHIP: 🟢/🔴/⚪ 는 사실형에만, 의견은 💬 (Q1·Q3) ─────────────────
_chip_rx = _re4.compile("🟢|🔴|⚪")
_comp = {k: _np.compose(v, "BTC", _NEWS_FX[k], "", {}, 3) for k, v in _P.items()}
_facts = [k for k, v in _P.items() if v["kind"] == "fact"]
_ops = [k for k, v in _P.items() if v["kind"] in ("scenario", "call")]
check("NEWS-CHIP1 사실형 요약줄은 🟢/🔴/⚪ 로 시작",
      all(_chip_rx.match(_comp[k]["summary"]) for k in _facts) and len(_facts) >= 8)
check("NEWS-CHIP2 의견·차트는 💬 로 시작하고 색 칩이 없다",
      all(_comp[k]["summary"].startswith("💬") and not _chip_rx.search(_comp[k]["summary"])
          for k in _ops) and len(_ops) >= 4)
check("NEWS-CHIP3 노이즈는 항목 자체가 없다(compose → None)",
      _comp["cryptosignals0rg/19634"] is None and _comp["wolfoftrading/6456"] is None)
check("NEWS-CHIP4 호재/악재 방향은 이벤트 유형 prior — 해킹=🔴, ETF 순유입=🟢",
      _comp["cryptosignals0rg/19523"]["summary"].startswith("🔴 악재 해킹")
      and _comp["wolfoftrading/6443"]["summary"].startswith("🟢 호재 ETF 순유입 $1.69B"))

# ── NEWS-DETAIL: 설명 2~3문장 · 절단 없음 (사용자 요청) ─────────────────
_all_det = [(k, c) for k, c in _comp.items() if c]
check("NEWS-DETAIL1 모든 항목 설명 2~3문장",
      all(2 <= len(c["detail"]) <= 3 for _k, c in _all_det))
check("NEWS-DETAIL2 설명 문장은 전부 완결(…·... 없음, 마침표로 끝남)",
      all(("…" not in d and "..." not in d and d.rstrip().endswith(".")) for _k, c in _all_det
          for d in c["detail"]))
check("NEWS-DETAIL3 news_detail_sentences=2 면 2문장으로 줄어든다(스위치 동작)",
      all(len(_np.compose(_P[k], "BTC", _NEWS_FX[k], "", {}, 2)["detail"]) == 2 for k in _facts))
_ko_cut = "제목 줄\n첫 문장은 완결됩니다. 둘째 문장은 중간에서 잘린 …"
check("NEWS-DETAIL4 번역문 보조는 완결 문장만(잘린 꼬리 문장 제외)",
      _np.ko_sentences(_ko_cut) == ["첫 문장은 완결됩니다."])
check("NEWS-DETAIL5 시나리오 설명에 분기선·목표·게시 시점 거리가 숫자로 들어간다",
      "1.4500" in " ".join(_comp["BitcoinBullets/17290"]["detail"])
      and "1.3100" in " ".join(_comp["BitcoinBullets/17290"]["detail"])
      and "-3.0%" in " ".join(_comp["BitcoinBullets/17290"]["detail"]))
check("NEWS-DETAIL6 조사 — 받침 유무(8.300을/7.200을, XRP는, Kraken과, BlackRock이)",
      _np.josa("7.200", "을/를") == "7.200을" and _np.josa("XRP", "은/는") == "XRP는"
      and _np.josa("Kraken", "과/와") == "Kraken과" and _np.josa("BlackRock", "이/가") == "BlackRock이"
      and _np.josa("1.4500", "을/를") == "1.4500을" and _np.josa("Coinbase Prime", "으로/로") == "Coinbase Prime으로")

# ── NEWS-CTX: 가격 맥락줄 ──────────────────────────────────────────────
_ctx_s = _np.compose(_sc, "AVAX", _NEWS_FX["BitcoinBullets/17281"], "",
                     {"chg24": 7.1, "cur_usd": 8.2177}, 3)["context"]
check("NEWS-CTX1 24h 등락 + 분기선까지 현재가 거리(↑+1.0% ↓-12.4%)",
      _ctx_s == "24h +7.1% · 분기 ↑8.300(+1.0%) ↓7.200(-12.4%)")
check("NEWS-CTX2 가격 데이터가 없으면 거리·등락 없이 레벨만(줄이 죽지 않는다)",
      _np.compose(_sc, "AVAX", _NEWS_FX["BitcoinBullets/17281"], "", {}, 3)["context"]
      == "분기 ↑8.300 ↓7.200")
_orig_ft = _mb4._fetch_tickers
_ft_calls = []
_mb4._fetch_tickers = lambda markets, timeout: (_ft_calls.append(list(markets)) or {
    "KRW-AVAX": {"trade_price": 11200.0, "signed_change_rate": 0.071},
    "KRW-BTC": {"trade_price": 1.3e8, "signed_change_rate": -0.01},
    "KRW-USDT": {"trade_price": 1363.0, "signed_change_rate": 0.0}})
_pc = _mb4._price_ctx(["AVAX", "MARKET", "AVAX"], 1.0, kimchi=0.0)
check("NEWS-CTX3 브리핑 회차 ticker 는 배치 1콜(코인당 1콜 이내) · 🌐 는 BTC 로",
      len(_ft_calls) == 1 and sorted(_ft_calls[0]) == ["KRW-AVAX", "KRW-BTC", "KRW-USDT"]
      and abs(_pc["AVAX"]["cur_usd"] - 11200 / 1363) < 1e-9 and _pc["MARKET"]["chg24"] == -1.0)
_mb4._fetch_tickers = lambda markets, timeout: {}
check("NEWS-CTX4 ticker 실패 → 빈 맥락(해당 줄 생략)", _mb4._price_ctx(["AVAX"], 1.0) == {})
_mb4._fetch_tickers = _orig_ft
check("NEWS-CTX5 게시 후 하루 이상 지난 글은 경과를 사실로 표기('게시 2일 전'), 하루 미만은 생략",
      _np.compose(_P["wolfoftrading/6443"], "BTC", _NEWS_FX["wolfoftrading/6443"], "",
                  {"chg24": -0.7, "age_h": 50.0}, 3)["context"] == "24h -0.7% · 게시 2일 전"
      and _np.compose(_P["wolfoftrading/6443"], "BTC", _NEWS_FX["wolfoftrading/6443"], "",
                      {"chg24": -0.7, "age_h": 5.0}, 3)["context"] == "24h -0.7%")
_old_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
db.init_db(_old_db)
_oldc = sqlite3.connect(_old_db)
_oldc.row_factory = sqlite3.Row
check("NEWS-MKT0 티커 없는 글 경로는 48h 넘은 옛 글을 받지 않는다(신설 경로 배포 첫 회차 적체 방지)",
      _nb2.maybe_send_unmatched_news(
          _oldc, {"description": _NEWS_FX["wolfoftrading/6448"], "url": "https://t.me/wolfoftrading/6448",
                  "published_at": 1790000000}, "wolfoftrading", [], now=1790000000 + 62 * 3600) == "skipped"
      and _nb2.maybe_send_unmatched_news(
          _oldc, {"description": _NEWS_FX["wolfoftrading/6448"], "url": "https://t.me/wolfoftrading/6448",
                  "published_at": 1790000000}, "wolfoftrading", [], now=1790000000 + 3 * 3600) == "queued")
_oldc.close()
os.unlink(_old_db)

# ── NEWS-MKT: 🌐 시장 뉴스 (Q2) ────────────────────────────────────────
check("NEWS-MKT1 연준 인상·CLARITY 불발·거래소(Bitget) 해킹은 시장 뉴스",
      all(_np.is_market_news(_P[k]) for k in
          ("wolfoftrading/6419", "wolfoftrading/6417", "wolfoftrading/6448")))
check("NEWS-MKT2 거래소 아닌 DeFi 해킹(Term Labs)·회고 기사(Bybit 18개월 전)는 제외",
      not _np.is_market_news(_P["cryptosignals0rg/19462"])
      and not _np.is_market_news(_P["cryptosignals0rg/19370"]))
_nm_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
db.init_db(_nm_db)
_nmc = sqlite3.connect(_nm_db)
_nmc.row_factory = sqlite3.Row
_UNI = [{"symbol": "BTC", "name": "Bitcoin"}, {"symbol": "CRO", "name": "Cronos"},
        {"symbol": "SOL", "name": "Solana"}, {"symbol": "XLM", "name": "Stellar"},
        {"symbol": "BCH", "name": "Bitcoin Cash"}, {"symbol": "AVAX", "name": "Avalanche"}]
_r_fed = _nb2.maybe_send_unmatched_news(
    _nmc, {"description": _NEWS_FX["wolfoftrading/6419"], "url": "https://t.me/wolfoftrading/6419"},
    "wolfoftrading", _UNI, now=1789600000)
_r_cro = _nb2.maybe_send_unmatched_news(
    _nmc, {"description": _NEWS_FX["cryptosignals0rg/19523"], "url": "https://t.me/cryptosignals0rg/19523"},
    "cryptosignals0rg", _UNI, now=1789600100)
_r_edu = _nb2.maybe_send_unmatched_news(
    _nmc, {"description": _NEWS_FX["cryptosignals0rg/19634"].replace("BTC", "the"),
           "url": "https://t.me/x/edu"}, "cryptosignals0rg", _UNI, now=1789600200)
_nmc.commit()
_nm_rows = {r["url"]: r["symbol"] for r in _nmc.execute("SELECT url, symbol FROM news_digest_queue")}
check("NEWS-MKT3 티커 없는 연준 뉴스 → 심볼 MARKET 으로 큐 적재",
      _r_fed == "queued" and _nm_rows.get("https://t.me/wolfoftrading/6419") == "MARKET")
check("NEWS-MKT4 티커 없이 'Cronos'만 있는 해킹 글 → 이름 매칭으로 CRO 적재(S3)",
      _r_cro == "queued" and _nm_rows.get("https://t.me/cryptosignals0rg/19523") == "CRO")
check("NEWS-MKT5 코인·시장 어느 쪽도 아닌 교양 글은 버린다", _r_edu == "skipped")
_st_cfg.SETTINGS["news_market_enabled"] = False
_r_off = _nb2.maybe_send_unmatched_news(
    _nmc, {"description": _NEWS_FX["wolfoftrading/6417"], "url": "https://t.me/wolfoftrading/6417"},
    "wolfoftrading", _UNI, now=1789700000)
check("NEWS-MKT6 news_market_enabled=False 면 🌐 수집 안 함", _r_off == "skipped")
_st_cfg.SETTINGS["news_market_enabled"] = True
_mb4._fetch_tickers = lambda markets, timeout: {}
_nm_ids = []
_nm_lines = _mb4._news_lines(_nmc, _nm_ids)
_nm_body = "\n".join(_nm_lines)
check("NEWS-MKT7 브리핑 블록에 '🌐 시장' 머리줄 + 사실형 칩(🔴 연준 금리 인상 25bp)",
      "<b>🌐 시장</b> · @wolfoftrading" in _nm_body
      and "🔴 악재 연준 금리 인상 25bp" in _nm_body.replace("\n   ", " "))
check("NEWS-MKT8 순위: 코인 사실형(CRO 해킹) > 🌐 시장(사용자 결정 순서)",
      _nm_body.index("<b>CRO</b>") < _nm_body.index("🌐 시장"))
_r_ai = _nb2.maybe_send_news_brief(
    _nmc, {"description": _NEWS_FX["wolfoftrading/6448"], "url": "https://t.me/wolfoftrading/6448"},
    "AI", "wolfoftrading", now=1789600300 + 86400 * 2)   # 🌐 쿨다운(MARKET 24h) 밖
_nmc.commit()
check("NEWS-MKT9 모호 심볼(AI)로 잡힌 거래소 해킹 글 → 코인 뉴스 대신 🌐 시장으로 구제(실측 Bitget)",
      _r_ai == "queued" and _nmc.execute(
          "SELECT symbol FROM news_digest_queue WHERE url='https://t.me/wolfoftrading/6448'"
      ).fetchone()["symbol"] == "MARKET")
_mb4._fetch_tickers = _orig_ft
_nmc.close()
os.unlink(_nm_db)
_sui = _np.parse("Sui Moderate Bearish Correction May End Shortly\nThe recent Bitcoin price surge "
                 "past $80,000 has triggered a strong upward movement in many altcoins, including Sui.")
check("NEWS-P15 제목에 방향 단서가 있는 코인 분석 기사 → 💬 분석 기사(가격 수준 없어도)",
      _sui["kind"] == "call" and _sui["source"] == "기사"
      and _np.compose(_sui, "SUI", "Sui Moderate Bearish Correction May End Shortly",
                      "수이 완만한 약세 조정이 곧 끝날 수 있습니다\n본문.", {}, 3)["detail"][:2]
      == ["분석 기사가 수이의 가격 흐름을 다뤘습니다.",
          "기사 제목은 “수이 완만한 약세 조정이 곧 끝날 수 있습니다”입니다."]
      and _sui["stance"] == "관망")   # 'Bearish … May End' — 단어로 방향을 매기면 뒤집힌다

# ── NEWS-NAME: 이름 매칭 오탐 (S3) ─────────────────────────────────────
_NI = _np.build_name_index(_UNI)
check("NEWS-NAME1 제목의 코인 이름 → 심볼(Cronos → CRO)",
      _np.match_coin_name("Cronos’ $74M Tectonic Exploit Exposes a Risk", _NI) == "CRO")
check("NEWS-NAME2 'Solana-based' 는 그 체인 위 다른 프로젝트 — 매칭 안 함",
      _np.match_coin_name("New Solana-based lending app raises $20M\nThe Solana-based "
                          "protocol launched today.", _NI) is None)
check("NEWS-NAME3 일반명사 충돌('an avalanche of', 'stellar results') — 매칭 안 함",
      _np.match_coin_name("Markets face an avalanche of liquidations\nStellar results "
                          "from tech earnings lifted stocks.", _NI) is None)
check("NEWS-NAME4 긴 이름 우선 — 'Bitcoin Cash' 는 BCH (BTC 아님)",
      _np.match_coin_name("Bitcoin Cash hard fork scheduled for November", _NI) == "BCH")
check("NEWS-NAME5 제목에 서로 다른 코인 둘 → 모호로 버림",
      _np.match_coin_name("Bitcoin and Solana lead weekly inflows", _NI) is None)
check("NEWS-NAME6 본문 지나가는 언급 1회는 주제로 보지 않음",
      _np.match_coin_name("Perps explained\nA perp is a bet on price. Bitcoin, oil, the S&P.", _NI)
      is None)
check("NEWS-NAME7 대문자 해시태그(#BITCOIN) 고래 글 → BTC",
      _np.match_coin_name(_NEWS_FX["wolfoftrading/6415"], _NI) == "BTC")

# ── NEWS-RANK: 사실 > 🌐시장 > 💬차트 > 💬의견, 같은 등급은 게시 최신순 ──────
_nr_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
db.init_db(_nr_db)
_nrc = sqlite3.connect(_nr_db)
_nrc.row_factory = sqlite3.Row
for _k, _sym, _ch, _post in [
    ("wolfoftrading/6440", "ETH", "wolfoftrading", 1790000900),   # 💬 의견 (가장 최신)
    ("BitcoinBullets/17281", "AVAX", "BitcoinBullets", 1790000800),  # 💬 차트
    ("wolfoftrading/6419", "MARKET", "wolfoftrading", 1790000700),   # 🌐 시장
    ("wolfoftrading/6408", "SOL", "wolfoftrading", 1790000600),      # 사실 M(고래)
    ("cryptosignals0rg/19523", "CRO", "cryptosignals0rg", 1790000100),  # 사실 H(해킹, 가장 오래됨)
    ("cryptosignals0rg/19634", "BTC", "cryptosignals0rg", 1790000950),  # 노이즈
]:
    db.queue_news_digest(_nrc, _sym, _ch, f"{_sym} 번역 요약은 1,234.5 부근에서 거래된다는 내용입니다.",
                         f"https://t.me/{_k}", "2026-09-27",
                         1790001000, summary_en=_NEWS_FX[_k], posted_at=_post)
_nrc.commit()
_mb4._fetch_tickers = lambda markets, timeout: {}
_nr_ids = []
_nr_lines = _mb4._news_lines(_nrc, _nr_ids)
_heads = [x for x in _nr_lines if x.startswith("   <b>")]
check("NEWS-RANK1 등급순: 해킹(H) > 고래(M) > 🌐 시장 > 💬 차트 > 💬 의견 (도착·게시순 아님)",
      [h.split("</b>")[0].split("<b>")[1] for h in _heads] == ["CRO", "SOL", "🌐 시장", "AVAX", "ETH"])
check("NEWS-RANK2 노이즈(Four Seasons)는 제외돼도 소비 처리(6건 전부)",
      "BTC" not in "\n".join(_heads) and len(_nr_ids) == 6)
import html as _html4
check("NEWS-RANK3 모든 줄 행잉 인덴트 + 표시폭 36 이내",
      all(x.startswith(_mb4._NEWS_INDENT)
          and _mb4._display_width(_html4.unescape(x.replace("<b>", "").replace("</b>", ""))) <= 36
          for x in _nr_lines[1:]))
check("NEWS-RANK4 렌더된 블록 전체에 고아 줄 0",
      _mb4.orphan_lines([_html4.unescape(x) for x in _nr_lines[1:] if not x.startswith("   <b>")]) == 0)
_st_cfg.SETTINGS["news_structured_enabled"] = False
_nr_ids2 = []
_nrc.execute("UPDATE news_digest_queue SET consumed=0")
_nr_lines2 = _mb4._news_lines(_nrc, _nr_ids2)
check("NEWS-SW1 news_structured_enabled=False → 종전 렌더(💬·칩 없음, 🌐 항목 없음)",
      _nr_lines2 and not any(("💬" in x or "🟢" in x or "🔴" in x or "🌐" in x) for x in _nr_lines2))
_st_cfg.SETTINGS["news_structured_enabled"] = True
_mb4._fetch_tickers = _orig_ft
_nrc.close()
os.unlink(_nr_db)

# ── NEWS-ORPHAN: 줄내림 고아단어 방지 (2026-09-27 사용자 요청) ──────────────
# "기존 브리핑 양식 줄내림 후 시작줄 고아단어 안 나오게, 기존에 세팅값은 유지"
_W4, _I4 = _mb4._NEWS_WRAP_W, _mb4._NEWS_INDENT
check("NEWS-ORPHAN0 세팅값 — 폭 32(09-28 대표 폰 실측으로 36→32) · 들여쓰기 3칸",
      _W4 == 32 and _I4 == "   ")


def _orphans(lines):
    return _mb4.orphan_lines(lines, _I4)


_o1 = _mb4._wrap_indented("비트코인 현물 ETF로 $1.69B 규모의 순유입이 집계됐습니다. ETF 순유입은 "
                          "기관의 현물 매수 수요로 해석돼 통상 단기 호재로 받아들여집니다.", _W4, _I4)
check("NEWS-ORPHAN1 마지막 줄에 단어 1개만 남지 않는다(직전 줄에서 끌어내림)",
      len(_mb4._wrap_units(_o1[-1][len(_I4):])) >= 2 and _orphans(_o1) == 0)
_o2 = _mb4._wrap_indented("고래가 이번 주 #SOL 에서 $ 9,000,000 를 샀고 시장은 이를 호재로 봤다 %", _W4, _I4)
check("NEWS-ORPHAN2 조사·기호 단독 어절('를'·'%')은 줄 머리로 넘어가지 않는다",
      not any(_re4.match(r"^(를|을|은|는|%|·|,|\.)(\s|$)", x[len(_I4):]) for x in _o2))
_o3 = _mb4._wrap_indented("24h +7.1% · 분기 ↑8.300(+1.0%) ↓7.200(-12.4%) · 목표 가격대 9.000 부근", _W4, _I4)
check("NEWS-ORPHAN3 '·' 는 줄 머리에 오지 않는다", not any(x[len(_I4):].startswith("·") for x in _o3))
check("NEWS-ORPHAN4 어절 유실·중복 없음(재조립 동일)",
      "".join(x[len(_I4):] for x in _o1).replace(" ", "")
      == ("비트코인 현물 ETF로 $1.69B 규모의 순유입이 집계됐습니다. ETF 순유입은 기관의 현물 매수 "
          "수요로 해석돼 통상 단기 호재로 받아들여집니다.").replace(" ", ""))
check("NEWS-ORPHAN5 직전 줄이 1단어가 되면서까지 끌어내리지 않는다(2덩어리 줄은 보존)",
      _mb4._wrap_indented("A" * 16 + " " + "B" * 11 + " C", _W4, _I4)
      == [_I4 + "A" * 16 + " " + "B" * 11, _I4 + "C"])
_o7 = _mb4._wrap_escaped("🔴 악재 연준 금리 인상 25bp · 단기", segments=True)
check("NEWS-ORPHAN7 조각 보호가 고아를 만들면 보호를 풀어 균형('단기' 단독 줄 없음)",
      _mb4.orphan_lines(_o7) == 0 and not any(x.strip() == "단기" for x in _o7))
_seg = _mb4._wrap_escaped("💬 차트 의견(4시간봉) · 저항 시험 · 단기", segments=True)
check("NEWS-ORPHAN6 요약줄은 ' · ' 조각 단위로 접힌다('저항 / 시험' 분리 없음)",
      any("저항 시험" in x for x in _seg) and all(_mb4._display_width(x) <= _W4 for x in _seg))

# ── RV2-N*·W1: 2026-09-27 코드 리뷰 확정 결함 회귀 (뉴스 v2) ──────────────────
# 원칙: 🟢/🔴/⚪ 칩은 확인된 사실에만 · 원문에 없는 내용을 사실처럼 쓰지 않는다.


def _rv2(sym, en, ko="", ctx=None):
    p = _np.parse(en)
    c = _np.compose(p, sym, en, ko, ctx or {})
    return p, c, ((c["summary"] + " " + " ".join(c["detail"]) + " " + c["context"]) if c else "")


_CHIPS = ("🟢", "🔴", "⚪")
_p, _c, _t = _rv2("BTC", "$BTCUSDT Update: 15m\nBTC broke out on the 15m in the morning. Whales bought the dip.")
check("RV2-N1 소문자 타임프레임 '15m' 은 금액 아님 — '$15M 고래 매수' 사실형 없음",
      "$15M" not in _t and not any(ch in _t for ch in _CHIPS)
      and _np.amounts("broke out on the 15m in the morning") == []
      and _np.amounts("5m ETH chart") == [])
check("RV2-N2 코인 수량('1.2M ETH'·'1.5M of BTC')은 달러 금액 아님, 달러 표기가 대표 금액",
      _np.amounts("1.2M ETH moved to Binance") == [] and _np.amounts("whales hold 1.5M of BTC") == []
      and _np.amounts("A whale moved 1.2M ETH worth $4.1B into Binance")[0][0] == "$4.1B"
      and _np.amounts("Spot ETFs saw 500M in net inflows")[0][0] == "$500M")
_p, _c, _t = _rv2("MARKET", "Fed expected to hike rates? Markets price in cut\n"
                            "Traders expect the Fed decision next week.")
check("RV2-N3 예상·질문형 연준 기사 → 사실형 아님('인상했습니다' 없음, 🌐 시장 아님)",
      _p["kind"] != "fact" and "인상했습니다" not in _t and not any(ch in _t for ch in _CHIPS)
      and not _np.is_market_news(_p)
      and _np._fed_move("Fed expected to hike rates? Markets price in cut") == ""
      and _np._fed_move("Fed holds rates steady\nTraders expect a rate cut in December") == "동결"
      and _np._fed_move("Fed raises rates by 25 bps as inflation persists") == "인상")
_p, _c, _t = _rv2("SOL", "SEC approves spot Solana ETF\nThe SEC approved the first spot Solana ETF on Tuesday.")
check("RV2-N4 ETF 승인은 '순유입'이 아니라 'ETF 승인'(원문에 없는 자금 흐름 서술 없음)",
      "순유입" not in _t and "ETF 승인" in _c["summary"] and "승인했습니다" in _t)
_p, _c, _t = _rv2("MARKET", "Bitcoin could drop to $50K if ETF inflows stall, analyst warns\nAnalyst opinion.")
check("RV2-N5 가정·의견 기사(could/if/analyst warns) → 칩 없음 · 🌐 시장 아님",
      _p["kind"] != "fact" and not any(ch in _t for ch in _CHIPS) and not _np.is_market_news(_p)
      and "순유입" not in _t)
_p, _c, _t = _rv2("SOL", _NEWS_FX["cryptosignals0rg/19331"])
check("RV2-N5b 사실 구절 뒤 수사 질문이 붙은 제목은 사실형 유지(질문 구절만 본다)",
      _p["kind"] == "fact" and _p["type"] == "partner" and not _p.get("spec"))
_p1, _c1, _t1 = _rv2("ETH", "ETH sees $1.2B in liquidations as price nears support\nTraders were liquidated.")
_p2, _c2, _t2 = _rv2("BTC", "Why Bitcoin is heading toward $150K\nAnalyst piece about bitcoin rally.",
                     ctx={"cur_usd": 80000.0})
check("RV2-N6 단위 금액을 가격 수준으로 자르지 않음('$1.2' 없음, $150K → $150,000)",
      "$1.2 " not in _t1 + " " and "지지 $1.2" not in _t1 and "$150 " not in _t2 + " "
      and "(-99.8%)" not in _t2 and "$150,000" in _t2)
_p, _c, _t = _rv2("ETH", "$ETHUSDT Update: 4h\nWe expect ETH to go up, bullish momentum. Rise is coming.")
check("RV2-N9 방향 콜 글 설명은 번역 보조문 없이도 2문장 이상", _c and len(_c["detail"]) >= 2)
_p, _c, _t = _rv2("ETH", "BREAKING: Trend Research deposits 50,000 ETH into Binance\n"
                         "Trend Research deposited 50,000 ETH ($120M) into Binance.")
check("RV2-N10 두 단어 주체명 보존('Trend Research가') · 자산명 중복 없음",
      "Trend Research가" in _t and "이더리움을" not in _t and "ETH 50,000개" in _t)
_p1, _c1, _t1 = _rv2("MARKET", "Kraken hack rumors denied\nKraken said no funds were stolen.")
_p2, _c2, _t2 = _rv2("MARKET", "Hackers target Coinbase users in phishing scam\n"
                               "No funds were stolen from Coinbase, the exchange said.")
check("RV2-N11 부인·루머·피싱 기사를 해킹 사실로 쓰지 않음(🔴 해킹·🌐 시장 없음)",
      "해킹 피해" not in _t1 + _t2 and not _np.is_market_news(_p1) and not _np.is_market_news(_p2)
      and _np.parse("Bybit hacked for $1.5B\nBybit was exploited for $1.5 billion in ETH.")["type"] == "hack")
_idx = _np.build_name_index([{"symbol": "FF", "name": "Falcon Finance"},
                             {"symbol": "NEAR", "name": "NEAR Protocol"}])
check("RV2-N12 불용어 이름은 접미사 뗀 별칭도 막는다('falcon' → FF 없음)",
      "falcon" not in _idx and _np.match_coin_name("Falcon Heavy launch delayed\nSpaceX Falcon rocket.",
                                                   _idx) is None)
_w1_bad = 0
for _cur, _up, _dn in [(0.00000583, "0.00000596", "0.00000570"), (81000.0, "82,000", "80,000")]:
    for _chg in (-12.34, 0.5):
        _s = _np.context_line({"kind": "scenario", "bull": {"trigger": _up}, "bear": {"trigger": _dn}},
                              "HBAR", {"chg24": _chg, "cur_usd": _cur})
        _out = _mb4._wrap_escaped(_s, segments=True, reorder=True)
        # 09-28: 분기선 값 하나("↓80,000(-1.2%)")가 한 줄인 건 고아가 아니다(값 단위) — 줄 끝 '·'·
        # 폭 초과·값 아닌 1단어 줄만 결함으로 센다.
        _bad_line = [x for x in _out[1:] if len(x.split()) <= 1 and not x.strip()[:1] in ("↑", "↓")]
        if _bad_line or any(_mb4._display_width(x) > _W4 or x.rstrip().endswith("·") for x in _out):
            _w1_bad += 1
check("RV2-W1 극소가 분기선 맥락줄도 고아 줄 없음(폭 32)", _w1_bad == 0)
# 09-28 대표 캡처: 조각 경계에서 줄이 바뀌면 줄 끝 '·' 가 매달리고 '분기' 라벨이 값과 떨어졌다.
_w2 = [_mb4._wrap_escaped(_s, segments=True, reorder=True) for _s in (
    "💬 차트 의견(8시간봉) · 박스권 · 단기",
    "24h -0.4% · 분기 ↑82,000(-2.7%) ↓80,000(-5.1%) · 게시 1일 전",
    "🔴 악재 연준 금리 인상 25bp · 단기")]
check("NEWS-WRAP2 줄 끝 '·' 없음 · '분기'는 첫 값과 같은 줄 · 폭 32 · 짧은 끝 조각 균형",
      all(not x.rstrip().endswith("·") and _mb4._display_width(x) <= _W4 for o in _w2 for x in o)
      and any("분기 ↑82,000" in x for x in _w2[1])
      and _w2[0][-1].strip() == "박스권 · 단기" and _w2[2][-1].strip() == "25bp · 단기")
_st_cfg.SETTINGS["news_translate_enabled"] = True


# ─── GG1: 등급 게이트 상향 (2026-09-13 A안, D → C) ───────────────────────
from collector.grading import meets_min_grade as _mmg
check("GG1 alert_min_grade 기본값 'C'", _st_cfg.get("alert_min_grade") == "C")
check("GG1b D 등급은 게이트 탈락(최하위 승률 22.7% · TP1 도달 0건)",
      not _mmg("D", _st_cfg.get("alert_min_grade")))
check("GG1c C 이상은 그대로 통과(상위 표본 무손상)",
      all(_mmg(g, _st_cfg.get("alert_min_grade")) for g in ("C", "B", "A", "S")))

# ─── ITEMS: 알림 항목 v3 (2026-09-27 대표 최종 확정) ──────────────────────
# 제외 5행(표시 스위치 OFF) · 추가 4행(판정 병기) · 판정 어휘 통일 · 소셜/김프 재보정.
# 캔들 테스트는 **고정 픽스처 + 인자 now** — 현재 시각에 의존하지 않는다.
from datetime import datetime as _dt_i, timezone as _tz_i
import itertools as _it_i
from monitor import upbit as _up_i
from monitor import stocktwits as _st_i
from notify import telegram as _tg_i
from config import settings as _cfg_i
from storage import db as _db_i

_D0 = _dt_i(2026, 9, 27, tzinfo=_tz_i.utc).timestamp()   # 09-27 00:00 UTC = KST 09:00
_DAY = 86400


def _day_candle(k, high=100.0, low=80.0, close=90.0, acc=100.0):
    """k = 09-27 기준 일수 오프셋(0 = 09-27 봉, -1 = 09-26 봉)."""
    return (high, low, close, acc, _D0 + k * _DAY)


# 09-27 봉(k=0)은 진행 중(거래대금 999·저가 10 — 쓰이면 값이 튄다), 09-26(k=-1) 거래대금 120·
# 저가 50, 09-25(k=-2) 거래대금 150, 나머지 100/80.
_FIX = []
for _k in range(-40, 1):
    if _k == 0:
        _FIX.append(_day_candle(_k, low=10.0, acc=999.0))
    elif _k == -1:
        _FIX.append(_day_candle(_k, low=50.0, acc=120.0))
    elif _k == -2:
        _FIX.append(_day_candle(_k, acc=150.0))
    else:
        _FIX.append(_day_candle(_k))

_t_before = _D0 - 60      # 09-27 KST 08:59 — 09-26 봉 진행 중, 전일 완성봉 = 09-25
_t_edge = _D0             # KST 09:00 정각 — 09-26 봉 완성(시작+24h == now)
_t_after = _D0 + 60       # KST 09:01
_rv_b = _up_i.rvol_d20(_FIX, _t_before)
_rv_e = _up_i.rvol_d20(_FIX, _t_edge)
_rv_a = _up_i.rvol_d20(_FIX, _t_after)
check("ITEMS-C1 경계 전(KST 08:59): 전일 완성봉=09-25(150) ÷ 20일 평균 100 = 1.5, 진행봉 제외",
      _rv_b is not None and abs(_rv_b - 1.5) < 1e-9
      and _up_i.completed_daily(_FIX, _t_before)[-1][4] == _D0 - 2 * _DAY)
check("ITEMS-C2 경계 정각(KST 09:00): 09-26 봉 완성 → 120 ÷ (19×100+150)/20 = 1.1707",
      _rv_e is not None and abs(_rv_e - 120 / 102.5) < 1e-9
      and _up_i.completed_daily(_FIX, _t_edge)[-1][4] == _D0 - _DAY)
check("ITEMS-C3 경계 후(KST 09:01): 정각과 동일, 09-27 진행봉(999) 미사용",
      _rv_a is not None and abs(_rv_a - _rv_e) < 1e-12)
check("ITEMS-C4 30일 저점: 경계 전 저점 80(+10%) / 정각·후 09-26 저가 50 반영(+76%) / 진행봉 저가 10 미사용",
      abs(_up_i.low30_pct(_FIX, _t_before, 88.0) - 10.0) < 1e-9
      and abs(_up_i.low30_pct(_FIX, _t_edge, 88.0) - 76.0) < 1e-9
      and abs(_up_i.low30_pct(_FIX, _t_after, 88.0) - 76.0) < 1e-9)
check("ITEMS-C5 표본 부족(완성봉 20개) → rvol None / 29개 → low30 None / 3-튜플(구 스텁) → None",
      _up_i.rvol_d20(_FIX[-21:], _t_after) is None
      and _up_i.low30_pct(_FIX[-30:], _t_after, 88.0) is None
      and _up_i.rvol_d20([c[:3] for c in _FIX], _t_after) is None
      and _up_i.daily_items(None, _t_after, 88.0) == {"rvol_d20": None, "low30_pct": None})

# _fetch_ohlc 튜플 끝 확장 — 앞 3원소·ATR·ADX 불변
_raw_days = [{"high_price": 100 + i % 7, "low_price": 90 - i % 5, "trade_price": 95 + i % 3,
              "candle_acc_trade_price": 1000.0 + i,
              "candle_date_time_utc": _dt_i.fromtimestamp(_D0 - i * _DAY, _tz_i.utc)
              .strftime("%Y-%m-%dT%H:%M:%S")} for i in range(60)]   # 최신→과거(업비트 순)
_orig_up_get = _up_i.requests.get
_orig_up_sleep = _up_i.time.sleep
_up_i.time.sleep = lambda s: None


class _RU:
    def __init__(self, body, status=200):
        self._b = body; self.status_code = status
    def json(self): return self._b
    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


_up_i.requests.get = lambda *a, **k: _RU(_raw_days)
_oh = _up_i._fetch_ohlc("KRW-T", "days", 60, 5.0)
_oh3 = [c[:3] for c in _oh]
check("ITEMS-C6 _fetch_ohlc = (고,저,종,거래대금,시작epoch) 끝 확장 · 과거→최신 · 시작시각 파싱",
      len(_oh[0]) == 5 and _oh[-1][4] == _D0 and _oh[-1][3] == 1000.0
      and _oh[0][4] == _D0 - 59 * _DAY)
check("ITEMS-C7 ATR20·ADX14 는 5-튜플/3-튜플 결과 동일(인덱스 보존)",
      _up_i.atr20_pct(_oh) == _up_i.atr20_pct(_oh3)
      and _up_i.adx14(_oh) == _up_i.adx14(_oh3) and _up_i.adx14(_oh) is not None)

# 업비트 시장경보 — market/all?isDetails=true 실측 형태(2026-09-27)
_CK = _up_i.UPBIT_CAUTION_KEYS


def _mk(market, warning=False, **caution):
    return {"market": market, "korean_name": "x", "english_name": "x",
            "market_event": {"warning": warning,
                             "caution": {k: bool(caution.get(k)) for k in _CK}}}


_mall = [_mk("KRW-BTC"), _mk("KRW-EGLD", warning=True),
         _mk("KRW-FIL", TRADING_VOLUME_SOARING=True),
         _mk("KRW-2Z", TRADING_VOLUME_SOARING=True, DEPOSIT_AMOUNT_SOARING=True),
         _mk("BTC-FIL", TRADING_VOLUME_SOARING=True)]
_seen_params = {}


def _fake_get(url, params=None, timeout=None, **k):
    if "market/all" in url:
        _seen_params.update(params or {})
        return _RU(_mall)
    return _RU([{"market": m["market"], "acc_trade_price_24h": 1.0,
                 "signed_change_rate": _rates_i.get(m["market"])}
                for m in _mall if m["market"].startswith("KRW-")])


# BTC +5%(제외) · EGLD +1% · FIL 0%(상승 아님) · 2Z -2% → 알트 3종 중 1종 상승 = 33.3%
_rates_i = {"KRW-BTC": 0.05, "KRW-EGLD": 0.01, "KRW-FIL": 0.0, "KRW-2Z": -0.02}


_up_i.requests.get = _fake_get
_ranks = _up_i.fetch_volume_ranks(5.0)
_warn = _up_i.last_market_warnings()
check("ITEMS-W1 market/all 은 isDetails=true 1콜 재사용 — 순위 반환 형태 불변 + 경보 부산물",
      _seen_params.get("isDetails") == "true" and set(_ranks) == {"KRW-BTC", "KRW-EGLD", "KRW-FIL", "KRW-2Z"}
      and _warn == {"KRW-EGLD": ["WARNING"], "KRW-FIL": ["TRADING_VOLUME_SOARING"],
                    "KRW-2Z": ["TRADING_VOLUME_SOARING", "DEPOSIT_AMOUNT_SOARING"]})


def _boom(*a, **k):
    raise RuntimeError("down")


check("ITEMS-B1 알트 상승 비율 = 같은 ticker 응답에서 BTC 제외·등락률>0 비율(0% 는 상승 아님)",
      abs(_up_i.last_alt_breadth() - 100 / 3) < 1e-9
      and _up_i.alt_breadth([{"market": "BTC-ETH", "signed_change_rate": 0.1}]) is None)
_up_i.requests.get = _boom
check("ITEMS-B2 조회 실패 → 알트 상승 비율 None(꼬리 생략, fail-open)",
      _up_i.fetch_volume_ranks(5.0) == {} and _up_i.last_alt_breadth() is None)
check("ITEMS-W2 조회 실패 → 순위 {} · 경보 None('경고 없음' [] 과 구분)",
      _up_i.fetch_volume_ranks(5.0) == {} and _up_i.last_market_warnings() is None)
check("ITEMS-W3 구 필드 market_warning='CAUTION' 도 유의로 인정",
      _up_i.parse_market_warning({"market": "KRW-X", "market_warning": "CAUTION"}) == ["WARNING"])
_up_i.requests.get = _orig_up_get
_up_i.time.sleep = _orig_up_sleep

# 업비트 투자주의 → 헤더 끝 꼬리표 (v3b): 전 사유 조합 × 코인명 1~12자 × 접근/터치
_hdr_cases = 0
_hdr_ok = True
_hdr_extra = 0
for _r in range(1, len(_CK) + 1):
    for _combo in _it_i.combinations(_CK, _r):
        for _len in range(1, 13):
            for _kind in ("touch", "preview"):
                _sym = "Q" * _len
                _m = _tg_i.render_alert(_kind, _sym, [dict(coin_symbol=_sym, entry_usd=1.5,
                                        tp_usd=1.65, score=50, grade="B", author="a")],
                                        2050.0, 1360.0,
                                        items={"upbit_warning": list(_combo)})
                _ls = _m.split(chr(10))
                _hdr_cases += 1
                _hdr_ok = _hdr_ok and all(_tg_i._line_width(x) <= 32 for x in _ls
                                          if x != _tg_i._SEP and not x.startswith("🔗"))
                _hdr_ok = _hdr_ok and "⚠️" in (_ls[1] + (_ls[2] if len(_ls) > 2 else ""))
                if _len <= 8 and "⚠️" not in _ls[1]:
                    _hdr_extra += 1
check(f"ITEMS-H1 주의 꼬리표 {_hdr_cases}케이스(31조합×코인명 1~12자×터치/접근) 전 줄 32칼럼 · "
      f"업비트 최장(8자)까지 새 줄 0",
      _hdr_ok and _hdr_extra == 0)
_H = _tg_i.upbit_caution_header
_hx = "🎯 <b>[진입가 터치]</b> <b>XRP</b>"
check("ITEMS-H2 사유 우선순위: 위험(입금급증>소수계정집중>해외가괴리) 우선 1개 · 관심(거래량급등·가격급등락)",
      _tg_i.upbit_caution_pick(["TRADING_VOLUME_SOARING", "DEPOSIT_AMOUNT_SOARING"])
      == ("입금급증", "입금", "주의")
      and _tg_i.upbit_caution_pick(["PRICE_FLUCTUATIONS", "GLOBAL_PRICE_DIFFERENCES"])[2] == "주의"
      and _tg_i.upbit_caution_pick(["CONCENTRATION_OF_SMALL_ACCOUNTS",
                                    "GLOBAL_PRICE_DIFFERENCES"])[0] == "소수계정집중"
      and _tg_i.upbit_caution_pick(["TRADING_VOLUME_SOARING"])[2] == "관심"
      and _tg_i.upbit_caution_pick(["PRICE_FLUCTUATIONS"])[2] == "관심"
      and _tg_i.upbit_caution_pick([]) is None)
check("ITEMS-H3 축약 단계: 짧은 헤더 전체 문구 · XRP 거래량 '⚠️급증·관심' · 입금 '⚠️입금·주의' · 긴 코인 '⚠️관심'",
      _H("🎯 X", ["TRADING_VOLUME_SOARING"]) == ("🎯 X · ⚠️주의 거래량급증 · 관심", None)
      and _H(_hx, ["TRADING_VOLUME_SOARING"])[0].endswith("XRP</b> ⚠️급증·관심")
      and _H(_hx, ["DEPOSIT_AMOUNT_SOARING"])[0].endswith("XRP</b> ⚠️입금·주의")
      and _H(_hx, ["CONCENTRATION_OF_SMALL_ACCOUNTS"])[0].endswith("XRP</b> ⚠️집중·주의")
      and _H("🎯 <b>[진입가 터치]</b> <b>PIEVERSE</b>", ["PRICE_FLUCTUATIONS"])[0]
      .endswith("PIEVERSE</b> ⚠️관심")
      and _H(_hx, None) == (_hx, None))
_m_nw = _tg_i.render_alert("touch", "XRP", [dict(coin_symbol="XRP", entry_usd=1.5, tp_usd=1.65,
                           score=50, grade="B", author="a")], 2050.0, 1360.0)
_m_cw = _tg_i.render_alert("touch", "XRP", [dict(coin_symbol="XRP", entry_usd=1.5, tp_usd=1.65,
                           score=50, grade="B", author="a")], 2050.0, 1360.0,
                           items={"upbit_warning": ["TRADING_VOLUME_SOARING"]})
check("ITEMS-H4 주의 표시는 헤더 끝 — 줄 수 불변 · 구 '⚠️ 업비트 주의종목 · 매수 보류' 줄 없음",
      len(_m_nw.split(chr(10))) == len(_m_cw.split(chr(10)))
      and "업비트 주의종목" not in _m_cw and "매수 보류" not in _m_cw)

# 판정 경계 (표시 반올림 값 기준)
_F = _tg_i
check("ITEMS-R1 거래량: 0.94→0.9 관망 · 0.96→1.0 매수 우호 · 1.44→1.4 매수 우호 · 1.46→1.5 과열 주의",
      _F.format_rvol_d20(0.94) == "🔊 거래량: 0.9배 · 관망"
      and _F.format_rvol_d20(0.96) == "🔊 거래량: 1.0배 · 매수 우호"
      and _F.format_rvol_d20(1.44) == "🔊 거래량: 1.4배 · 매수 우호"
      and _F.format_rvol_d20(1.46) == "🔊 거래량: 1.5배 · 과열 주의"
      and _F.format_rvol_d20(None) is None)
check("ITEMS-R2 30일 저점: +4 관망 · +5 매수 우호 · +14 매수 우호 · +15 추격 주의 · -3 관망",
      _F.format_low30(4.4) == "📏 30일 저점: +4% · 관망"
      and _F.format_low30(4.6) == "📏 30일 저점: +5% · 매수 우호"
      and _F.format_low30(14.4) == "📏 30일 저점: +14% · 매수 우호"
      and _F.format_low30(14.6) == "📏 30일 저점: +15% · 추격 주의"
      and _F.format_low30(-3.2) == "📏 30일 저점: -3% · 관망")
check("ITEMS-R3 글 이후: +5 매수 가능 · -5 매수 가능 · -6 관망 · +7 관망 · +10 추격 주의",
      _F.format_post_move(5.4) == "⏱ 글 이후: +5% · 매수 가능"
      and _F.format_post_move(-5.4) == "⏱ 글 이후: -5% · 매수 가능"
      and _F.format_post_move(-5.6) == "⏱ 글 이후: -6% · 관망"
      and _F.format_post_move(7.0) == "⏱ 글 이후: +7% · 관망"
      and _F.format_post_move(9.6) == "⏱ 글 이후: +10% · 추격 주의")
check("ITEMS-R4 시장심리: 64 매수 우호 · 65 관망 (판정 병기, 괄호 라벨 대체)",
      _F.format_fng(64).endswith("시장심리: 64 · 매수 우호")
      and _F.format_fng(65).endswith("시장심리: 65 · 관망") and _F.format_fng(None) is None)
_wide = ([_F.format_rvol_d20(x / 10) for x in range(0, 1000)]
         + [_F.format_low30(x) for x in range(-99, 1000)]
         + [_F.format_post_move(x) for x in range(-99, 1000)]
         + [_F.format_fng(x) for x in range(0, 101)])
check(f"ITEMS-L1 새 행 {len(_wide)}개 값 전수(거래량 0~99.9배·저점 -99~+999%·글 이후·심리) 32칼럼 이내",
      all(_F._line_width(x) <= 32 for x in _wide))
_reasons = ["추격 위험", "반등 여지", "자금 유입", "속임 반등", "투매 진행", "롱 청산 위험",
            "숏 청산 연료", "변동성 위기", "채굴자 항복", "장기바닥·주RSI28", "장기과열·주RSI75",
            "20일지지·상승세", "120일지지·RSI47", "역배열·RSI37", "상승세·RSI64·4h과열"]
_vl = ([_F.format_supply(v, r) for v in ("우호", "중립", "주의") for r in _reasons]
       + [_F.format_position(v, r) for v in ("최적", "우호", "중립", "주의", "위험")
          for r in _reasons])
check("ITEMS-L2 돈 흐름·자리 판정 줄 — 긴 근거도 32칼럼 이내(근거부터 축약), 판정은 항상 남음",
      all(_F._line_width(x) <= 32 for x in _vl)
      # '{항목}: {근거} · {판정}' 또는 넘치면 '{항목}: {판정}' — 판정은 항상 줄 끝
      and all(x.endswith((" 매수 우호", " 관망", " 주의", " 매수 보류")) and ": " in x
              for x in _vl))
check("ITEMS-L3 판정 어휘 매핑: 우호→매수 우호 / 중립→관망 / 주의→주의 / 위험→매수 보류",
      _F.format_supply("중립", None) == "🧭 돈 흐름: 중립 · 관망"
      and _F.format_position("위험", "장기과열·주RSI75") == "🌡️ 자리: 장기과열 · 매수 보류"
      and _F.format_position("중립", "상승세·RSI64") == "🌡️ 자리: 상승세·RSI64 · 관망")

# 렌더 통합 — 제외 5행 없음(기본값), 비트 점유율 유지, 새 행 표시·그룹
_cl_i = [{"coin_symbol": "XRP", "entry_usd": 1.50, "tp_usd": 1.65, "score": 50, "grade": "B",
          "author": "a", "author_followers": 10}]
_m_i = _tg_i.render_alert(
    "touch", "XRP", _cl_i, 2050.0, 1360.0,
    sentiment={"btc_dominance": 58.5, "fear_greed": 60, "altcoin_season_index": 30},
    dex_stats={"liquidity_usd": 50_000, "buy_ratio_24h": 0.30},
    active_addr_pctile=5.0, watcher_coin_sl={"total": 5, "misses": 4, "sl_rate": 0.8},
    kimchi_pct=1.0,
    items={"rvol_d20": 1.2, "low30_pct": 8.0, "upbit_warning": ["TRADING_VOLUME_SOARING"],
           "post_move_pct": 3.0})
_ml_i = _m_i.split("\n")
check("ITEMS-X2 기본값: 🔴 DEX 매도세·💧 DEX 저유동·⛓ 활성주소·📉 워쳐 SL률·🪙 알트장 없음",
      all(t not in _m_i for t in ("DEX 매도세", "DEX 저유동", "활성주소", "워쳐 SL률", "알트장")))
check("ITEMS-X3 🌍 비트 점유율 유지(판정 없음) · 시장심리 판정 · 김프 1% 숨김",
      "🌍 비트 점유율: 58.5%" in _ml_i and any(ln.endswith("시장심리: 60 · 매수 우호") for ln in _ml_i)
      and "김프" not in _m_i)
check("ITEMS-X4 헤더 주의 꼬리표·거래량·30일 저점 표시 / ⏱ 글 이후 기본 OFF (전부 32칼럼)",
      _ml_i[1].endswith("⚠️급증·관심") and "🔊 거래량: 1.2배 · 매수 우호" in _ml_i
      and "📏 30일 저점: +8% · 매수 우호" in _ml_i and "글 이후" not in _m_i
      and _cfg_i.get("alert_show_post_move") is False
      and all(_tg_i._line_width(ln) <= 32 for ln in _ml_i
              if ln != _tg_i._SEP and not ln.startswith("🔗")))
_saved_sw = {k: _cfg_i.SETTINGS[k] for k in ("alert_show_rvol_d20", "alert_show_low30",
                                              "alert_show_upbit_warning", "alert_show_post_move",
                                              "alert_show_btc_dom", "alert_show_altseason",
                                              "alert_show_watcher_sl")}
for _k in ("alert_show_rvol_d20", "alert_show_low30", "alert_show_upbit_warning",
           "alert_show_post_move", "alert_show_btc_dom"):
    _cfg_i.SETTINGS[_k] = False
_cfg_i.SETTINGS["alert_show_altseason"] = True
_cfg_i.SETTINGS["alert_show_watcher_sl"] = True
_m_off = _tg_i.render_alert(
    "touch", "XRP", _cl_i, 2050.0, 1360.0,
    sentiment={"btc_dominance": 58.5, "fear_greed": 60, "altcoin_season_index": 30},
    watcher_coin_sl={"total": 5, "misses": 4, "sl_rate": 0.8},
    items={"rvol_d20": 1.2, "low30_pct": 8.0, "upbit_warning": ["WARNING"], "post_move_pct": 3.0})
check("ITEMS-X5 행별 스위치: 새 4행·비트 점유율 OFF 가능 / 알트장·워쳐 SL률 ON 복귀 가능",
      all(t not in _m_off for t in ("업비트", "거래량", "30일 저점", "글 이후", "비트 점유율"))
      and "🪙 알트장: 30" in _m_off and "워쳐 SL률 80%" in _m_off)
check("ITEMS-X6 알트장 렌더는 값·판정 인자 구조(소스 교체 대비)",
      _tg_i.format_altseason(72, "매수 우호") == "🪙 알트장: 72 · 매수 우호"
      and _tg_i.format_altseason(None) is None)
for _k, _v in _saved_sw.items():
    _cfg_i.SETTINGS[_k] = _v

# 🌍 비트 점유율 + 알트 상승 꼬리(새 줄 없음, 판정 없음)
_bd = [_tg_i.format_btc_dom(b, a) for b in (58.3, 100.0, 9.99, 58.33) for a in range(0, 101)]
check("ITEMS-B3 '🌍 비트 점유율 58.3% · 알트↑55%'(전체 문구 36칼럼 → 축약) · 전 조합 32칼럼 · 판정 없음",
      _tg_i.format_btc_dom(58.3, 55.2) == "🌍 비트 점유율: 58.3% · 알트↑55%"
      and _tg_i.format_btc_dom(58.3, None) == "🌍 비트 점유율: 58.3%"
      and all(_tg_i._line_width(x) <= 32 for x in _bd)
      and not any(v in x for x in _bd for v in ("우호", "관망", "주의", "보류")))
check("ITEMS-B4 점유율 소수 1자리 고정(58.33→58.3) · 알트↑99% 까지 꼬리 유지 · 100%(33칼럼)는 꼬리만 생략",
      _tg_i.format_btc_dom(58.33, 99) == "🌍 비트 점유율: 58.3% · 알트↑99%"
      and _tg_i.format_btc_dom(58.33, 100) == "🌍 비트 점유율: 58.3%"
      and _tg_i.format_btc_dom(99.9, 100) == "🌍 비트 점유율: 99.9%")
_mb_on = _tg_i.render_alert("touch", "XRP", _cl_i, 2050.0, 1360.0,
                            sentiment={"btc_dominance": 58.3}, items={"alt_breadth": 55.0})
_cfg_i.SETTINGS["alert_show_alt_breadth"] = False
_mb_off = _tg_i.render_alert("touch", "XRP", _cl_i, 2050.0, 1360.0,
                             sentiment={"btc_dominance": 58.3}, items={"alt_breadth": 55.0})
_cfg_i.SETTINGS["alert_show_alt_breadth"] = True
check("ITEMS-B5 렌더: 🌍 줄 끝에 붙고 새 줄 없음 / 스위치 OFF 면 꼬리만 생략",
      "🌍 비트 점유율: 58.3% · 알트↑55%" in _mb_on.split(chr(10))
      and "🌍 비트 점유율: 58.3%" in _mb_off.split(chr(10))
      and len(_mb_on.split(chr(10))) == len(_mb_off.split(chr(10))))

# 52주 한 줄 병합 옵션(기본 OFF)
_w52_off = _tg_i.render_alert("touch", "XRP", _cl_i, 2071.0, 1360.0, week52=(4379.0, 1392.0, 52))
_cfg_i.SETTINGS["alert_week52_compact"] = True
_w52_on = _tg_i.render_alert("touch", "XRP", _cl_i, 2071.0, 1360.0, week52=(4379.0, 1392.0, 52))
_w52_x = [_tg_i.render_alert("touch", "XRP", _cl_i, _c, 1360.0, week52=(_h, _l, _n))
          for _c, _h, _l, _n in ((2071.0, 4379.0, 1392.0, 20), (1.0, 999999.0, 0.5, 52),
                                 (5000.0, 4379.0, 1392.0, 52))]
_cfg_i.SETTINGS["alert_week52_compact"] = False
check("ITEMS-X7 52주 병합 스위치: 기본 OFF(5줄 블록 유지) / ON 이면 '📍 52주 위치 23% · 고가 -53%' 1줄",
      "  └ 52주 범위 중 23% 위치" in _w52_off.split(chr(10))
      and "📍 52주 위치 23% · 고가 -53%" in _w52_on.split(chr(10))
      and "  고가: " not in _w52_on and "🟩" not in _w52_on
      and all(_tg_i._line_width(ln) <= 32 for _m in _w52_x for ln in _m.split(chr(10))
              if ln.startswith("📍"))
      and "📍 상장후 위치" in _w52_x[0])

# ITEMS-W6 줄넘김 0 격자 (v3c 대표 확정): 가격대 0.00001원~2억원 × 사다리(없음/1/2/12)
# × 장기(≥+50%) 여부 × 이탈(현재 -18%) 여부 × 진입 범위 여부 × 헤더 주의(8자·12자 코인).
_wg_n = 0
_wg_bad = []
for _px in (0.00001, 0.0123, 0.5, 1, 9.9, 123, 1234, 12345, 123456, 1234567,
            12345678, 123456789, 200000000):
    _e = _px / 1360.0
    for _lad in (0, 1, 2, 12):
        for _long in (False, True):
            for _gap in (False, True):
                for _rng in (False, True):
                    _tps = [_e * (1.9 if _long else 1.12) * (1 + 0.01 * _i)
                            for _i in range(max(_lad, 1))]
                    _cl = [dict(coin_symbol="PIEVERSE", entry_usd=_e, tp_usd=_tps[0],
                                tps_usd=str(_tps) if _lad else None, score=50, grade="B",
                                author="a")]
                    if _rng:
                        _cl.append(dict(_cl[0], entry_usd=_e * 0.992, score=40))
                    for _sym, _codes in (("PIEVERSE", ["CONCENTRATION_OF_SMALL_ACCOUNTS"]),
                                         ("ABCDEFGHIJKL", ["TRADING_VOLUME_SOARING"])):
                        _mw = _tg_i.render_alert("touch", _sym, _cl, _px * (0.82 if _gap else 1.0),
                                                 1360.0, week52=(_px * 3, _px / 3, 52),
                                                 volume_rank=123, rep=_cl[0],
                                                 items={"upbit_warning": _codes})
                        _wg_n += 1
                        for _ln in _mw.split(chr(10)):
                            if _ln == _tg_i._SEP or _ln.startswith("🔗"):
                                continue
                            if (_tg_i._line_width(_ln) > 32 or _ln.startswith(" " * 11)
                                    or _ln.startswith("⚠️주의")):
                                _wg_bad.append((_px, _lad, _long, _gap, _rng, _sym, _ln))
check(f"ITEMS-W6 줄넘김 0 격자 {_wg_n}개 알림(가격 13단×사다리 4×장기×이탈×범위×코인 2) — 전 줄 ≤32 · 연속 줄 0",
      _wg_n == 13 * 4 * 2 * 2 * 2 * 2 and not _wg_bad)
_w12 = _tg_i.render_alert("touch", "ABCDEFGHIJKL", _cl_i, 2050.0, 1360.0,
                          items={"upbit_warning": ["TRADING_VOLUME_SOARING"]}).split(chr(10))[1]
check("ITEMS-W7 헤더 주의 꼬리 한 줄 유지 — 사유 보존 우선(PIEVERSE·12자는 '[터치]' 축약, XRP 는 원 라벨)",
      "[터치]" in _w12 and "⚠️" in _w12 and _tg_i._line_width(_w12) <= 32
      and _tg_i._TAG_RE.sub("", _tg_i.render_alert("touch", "PIEVERSE", _cl_i, 2050.0, 1360.0,
          items={"upbit_warning": ["TRADING_VOLUME_SOARING"]}).split(chr(10))[1])
      == "🎯 [터치] PIEVERSE ⚠️급증·관심"
      and _tg_i._TAG_RE.sub("", _tg_i.render_alert("touch", "XRP", _cl_i, 2050.0, 1360.0,
          items={"upbit_warning": ["DEPOSIT_AMOUNT_SOARING"]}).split(chr(10))[1])
      == "🎯 [진입가 터치] XRP ⚠️입금·주의")
_wl = _tg_i.render_alert("touch", "SUI", [dict(coin_symbol="SUI", entry_usd=1.42, tp_usd=2.65,
                         tps_usd="[2.65, 5.36]", score=50, grade="B", author="a")],
                         1590.0, 1361.0).split(chr(10))
check("ITEMS-W8 '(장기)' 꼬리 폐지 → '  목표: 3,607원 장기+86.6% 1/2' · 가격 줄 값 칼럼 정렬(8)",
      "  목표: 3,607원 장기+86.6% 1/2" in _wl and "(장기)" not in chr(10).join(_wl)
      and "  진입: 1,933원 (현재 -17.7%)" in _wl and "  현재: 1,590원" in _wl)
check("ITEMS-W9 30일 저점 극단값(+632059%)도 32칼럼 — '+999%↑' 상한 표기",
      _tg_i.format_low30(632059.0) == "📏 30일 저점: +999%↑ · 추격 주의")

# 김프 경계
def _kim(v):
    return _tg_i.render_alert("touch", "XRP", _cl_i, 2050.0, 1360.0, kimchi_pct=v)


check("ITEMS-K1 김프 |x|<3 숨김(2.99/-2.99) · 3.0/-3.0 표시 · 판정 미부착",
      "김프" not in _kim(2.99) and "김프" not in _kim(-2.99)
      and "🌶️ 김프: +3.00%" in _kim(3.0).split("\n")
      and "❄️ 김프: -3.00%" in _kim(-3.0).split("\n"))
check("ITEMS-K2 15% 상한 가드와 공존 — -97% 숨김",
      "김프" not in _kim(-97.4))

# 소셜 — 소스 중립 표시부 + 새 기준
def _soc_i(r, n, via_items=False):
    if via_items:
        return _tg_i.render_alert("touch", "XRP", _cl_i, 2050.0, 1360.0,
                                  items={"social": {"bull_ratio": r, "n": n, "source": "other"}})
    return _tg_i.render_alert("touch", "XRP", _cl_i, 2050.0, 1360.0,
                              stwits_bullish_ratio=r, stwits_n=n)


check("ITEMS-S1 소셜 평소(0.9·n=20) 숨김 · '매수 유리' 폐지",
      "소셜" not in _soc_i(0.9, 20) and "소셜" not in _soc_i(1.0, 30))
check("ITEMS-S2 소셜 경계: 0.50·n=10 경고 / 0.51 숨김 / 0.2·n=9 숨김(표본 부족)",
      "💬 소셜: 매도 우세 · 주의" in _soc_i(0.5, 10).split("\n")
      and "소셜" not in _soc_i(0.51, 10) and "소셜" not in _soc_i(0.2, 9))
check("ITEMS-S3 소스 중립 dict(items.social) 로도 같은 판정 — 수집부 교체 가능",
      "💬 소셜: 매도 우세 · 주의" in _soc_i(0.3, 12, via_items=True).split("\n")
      and "소셜" not in _soc_i(0.9, 12, via_items=True))

# XRP 별칭
check("ITEMS-A1 XRP↔Ripple 별칭 일치 · 심볼 미지정/다른 코인엔 미적용 · Sky/Skycoin 여전히 거부",
      _st_i._names_match("XRP", "Ripple", symbol="XRP")
      and not _st_i._names_match("XRP", "Ripple")
      and not _st_i._names_match("Bitcoin", "Ripple", symbol="BTC")
      and not _st_i._names_match("Sky", "Skycoin", symbol="SKY")
      and not _st_i._names_match("GMT", "Mercury Protocol", symbol="GMT"))
_orig_st_get_i = _st_i.requests.get
_st_i.requests.get = lambda *a, **k: _R(200, {
    "symbol": {"title": "Ripple"},
    "messages": [{"entities": {"sentiment": {"basic": "Bullish"}}}] * 8
    + [{"entities": {"sentiment": {"basic": "Bearish"}}}] * 4})
_xr = _st_i.fetch_sentiment_stats("XRP", expected_name="XRP")
_st_i.requests.get = _orig_st_get_i
check("ITEMS-A2 XRP 조회(CG 'XRP' vs ST 'Ripple') 더 이상 폐기 안 됨 → 8/12",
      _xr is not None and _xr["bullish"] == 8 and _xr["bearish"] == 4)

# 글 이후 가격 이동 — 4시간봉 근사
_H4 = 4 * 3600
_h4 = [(0, 0, 100.0 + i, None, _D0 - (50 - i) * _H4) for i in range(51)]  # 마지막 봉 시작 = _D0
_pub = _D0 - 10 * _H4 + 3600       # 09-25 16:00 UTC 봉(시작 _D0-10*4h) 안 1시간 지점
_base_i = _up_i.price_at_post(_h4, _pub)
check("ITEMS-P1 게시 시각 가격 = 직전 완성 4시간봉 종가(=게시 봉 시가)",
      _base_i == 100.0 + 39)
check("ITEMS-P2 글 이후 % = 현재가/기준-1 · 7일 초과·미래·커버리지 밖 → None",
      abs(_up_i.post_move_pct(_h4, _pub, _D0 + 60, 139.0 * 1.03) - 3.0) < 1e-9
      and _up_i.post_move_pct(_h4, _D0 - 8 * _DAY, _D0, 100.0) is None
      and _up_i.post_move_pct(_h4, _D0 + 999, _D0, 100.0) is None
      and _up_i.post_move_pct(_h4[-3:], _pub, _D0 + 60, 100.0) is None
      and _up_i.post_move_pct(None, _pub, _D0, 100.0) is None)

# 스냅샷 컬럼 — 마이그레이션 + 최초 기록 우선 + '' = 경보 없음
_items_db = tempfile.NamedTemporaryFile(delete=False, suffix=".db").name
try:
    _db_i.init_db(_items_db)
    with _db_i.connect(_items_db) as conn:
        _cols = {r["name"] for r in conn.execute("PRAGMA table_info(levels)").fetchall()}
        check("ITEMS-D1 마이그레이션: touch_rvol_d20·touch_low30_pct·touch_upbit_warning·"
              "touch_post_move_pct·touch_stwits_n 컬럼",
              {"touch_rvol_d20", "touch_low30_pct", "touch_upbit_warning",
               "touch_post_move_pct", "touch_stwits_n", "touch_alt_breadth"} <= _cols)
        _ids = []
        for _sk in ("i1", "i2"):
            _ids.append(conn.execute(
                "INSERT INTO levels (coin_symbol, ticker, entry_usd, direction, status, "
                "signal_key, collected_at) VALUES ('X','KRW-X',1.0,'long','touched',?,900)",
                (_sk,)).lastrowid)
        _db_i.record_touch_snapshot(conn, [(_ids[0], None, None, None, None, None, None)],
                                    rvol_d20=1.23, low30_pct=8.5,
                                    upbit_warning="WARNING,TRADING_VOLUME_SOARING",
                                    post_move_pct=-2.5, stwits_n=12, alt_breadth=55.5)
        _db_i.record_touch_snapshot(conn, [(_ids[1], None, None, None, None, None, None)],
                                    upbit_warning="")
        _db_i.record_touch_snapshot(conn, [(_ids[0], None, None, None, None, None, None)],
                                    rvol_d20=9.9, upbit_warning="")
        _r0 = dict(conn.execute("SELECT * FROM levels WHERE id=?", (_ids[0],)).fetchone())
        _r1 = dict(conn.execute("SELECT * FROM levels WHERE id=?", (_ids[1],)).fetchone())
        check("ITEMS-D2 스냅샷 기록·최초 기록 우선·'' = 경보 없음(NULL = 미조회와 구분)",
              _r0["touch_rvol_d20"] == 1.23 and _r0["touch_low30_pct"] == 8.5
              and _r0["touch_upbit_warning"] == "WARNING,TRADING_VOLUME_SOARING"
              and _r0["touch_post_move_pct"] == -2.5 and _r0["touch_stwits_n"] == 12
              and _r0["touch_alt_breadth"] == 55.5
              and _r1["touch_upbit_warning"] == "" and _r1["touch_rvol_d20"] is None)
finally:
    try:
        os.remove(_items_db)
    except OSError:
        pass


# ─── OKX 롱숏 3종 폴백 (2026-09-27, 대표 승인 "OKX 롱숏 DB 축적") ───────────
# 미국 러너 Binance /futures/data/* 451 → OKX rubik 폴백. 네트워크는 전부 목.
from monitor import binance as _okb


class _OkR:
    def __init__(self, status, body):
        self.status_code = status; self._body = body
    def json(self): return self._body


def _ok_reset():
    _okb._RATIO_WARNED.clear(); _okb._RATIO_BLOCKED.clear()


def _ok_router(okx_status=200, okx_body=None, bin_status=451, bin_body=None, calls=None):
    def _get(url, params=None, timeout=None, **k):
        if calls is not None:
            calls.append((url, dict(params or {}), timeout))
        if "fapi.binance.com" in url:
            return _OkR(bin_status, bin_body if bin_body is not None else {})
        if "okx.com" in url:
            if isinstance(okx_status, Exception):
                raise okx_status
            return _OkR(okx_status, okx_body)
        raise AssertionError(url)
    return _get


_now_ms = int(time.time() * 1000)
_cur4h = _now_ms - 3600 * 1000          # 진행 중 버킷(시작 1h 전)
_prev4h = _cur4h - 4 * 3600 * 1000      # 완결 버킷
# 진행 중 구간(cur)은 1.4, 완성 구간(prev)은 1.5615 — 완성 구간을 써야 한다(09-27 디버깅).
_ok_ls_body = {"code": "0", "msg": "", "data": [[str(_cur4h), "1.4"], [str(_prev4h), "1.5615"]]}
_ok_tk_body = {"code": "0", "msg": "", "data": [[str(_cur4h), "100", "300"],
                                                [str(_prev4h), "729492.21", "942633.70"]]}

# OKX-1: 451 → OKX 계정 롱/숏 비율 1.5615 → 롱 비중 0.6096 (바이낸스 longAccount 와 같은 의미)
_ok_reset(); _calls = []
with patch("requests.get", side_effect=_ok_router(okx_body=_ok_ls_body, calls=_calls)):
    _v = _okb.fetch_long_short_ratio("SOL", 10.0)
check("OKX-1 Binance 451 → OKX 폴백 롱 비중 r/(1+r)", _v is not None and abs(_v - 0.6096) < 1e-3)
check("OKX-1b OKX 계정 롱숏 엔드포인트·instId·4H·짧은 타임아웃",
      any("long-short-account-ratio-contract" in u and p.get("instId") == "SOL-USDT-SWAP"
          and p.get("period") == "4H" and t <= 5.0 for u, p, t in _calls))

# OKX-2: 451 이 확인되면 같은 회차 나머지 호출은 Binance 생략(헛콜 방지)
_calls = []
with patch("requests.get", side_effect=_ok_router(okx_body=_ok_ls_body, calls=_calls)):
    _v2 = _okb.fetch_top_trader_position_ratio("SOL", 10.0)
check("OKX-2 상위 트레이더도 OKX 포지션 비율로 폴백",
      _v2 is not None and abs(_v2 - 0.6096) < 1e-3
      and any("long-short-position-ratio-contract-top-trader" in u for u, _, _ in _calls))
check("OKX-2b 차단 확인 후 Binance 재호출 없음",
      not any("fapi.binance.com" in u for u, _, _ in _calls))

# OKX-3: 테이커 — [ts, sellVol, buyVol], 진행 중 버킷 건너뛰고 완결 버킷 buy/sell
_ok_reset()
with patch("requests.get", side_effect=_ok_router(okx_body=_ok_tk_body)):
    _v3 = _okb.fetch_taker_buy_sell_ratio("SOL", 10.0)
check("OKX-3 테이커 완결 버킷 buyVol/sellVol", _v3 is not None and abs(_v3 - 942633.70 / 729492.21) < 1e-6)

# OKX-4: OKX 실패(500) → None + 경고는 소스별 1회
_ok_reset()
with patch("requests.get", side_effect=_ok_router(okx_status=500, okx_body={})), \
        patch.object(_okb.logger, "warning") as _w:
    _r4 = [_okb.fetch_long_short_ratio("SOL", 10.0),
           _okb.fetch_top_trader_position_ratio("SOL", 10.0),
           _okb.fetch_taker_buy_sell_ratio("ETH", 10.0)]
    _okx_warns = [c for c in _w.call_args_list if "[okx]" in str(c.args[0])]
    _bin_warns = [c for c in _w.call_args_list if "[binance]" in str(c.args[0])]
check("OKX-4 OKX 실패 → 전부 None(fail-open)", _r4 == [None, None, None])
check("OKX-4b 경고 로그 소스별 1줄(okx 1 · binance 1)", len(_okx_warns) == 1 and len(_bin_warns) == 1)

# OKX-5: OKX 예외(타임아웃) → None
_ok_reset()
with patch("requests.get", side_effect=_ok_router(okx_status=TimeoutError("t/o"))):
    check("OKX-5 OKX 타임아웃 → None", _okb.fetch_long_short_ratio("SOL", 10.0) is None)
_ok_reset(); _calls = []
import requests as _okreq
with patch("requests.get", side_effect=_ok_router(okx_status=_okreq.Timeout("t/o"), calls=_calls)):
    _v5b = [_okb.fetch_long_short_ratio("SOL", 10.0), _okb.fetch_taker_buy_sell_ratio("SOL", 10.0)]
check("OKX-5b requests 타임아웃 → 회차 내 OKX 재호출 생략(지연 누적 방지)",
      _v5b == [None, None] and sum("okx.com" in u for u, _, _ in _calls) == 1)

# OKX-6: 미상장(code 51001, HTTP 200) → 조용히 None
_ok_reset()
with patch("requests.get", side_effect=_ok_router(
        okx_body={"code": "51001", "data": [], "msg": "Instrument ID doesn't exist."})), \
        patch.object(_okb.logger, "warning") as _w6:
    _v6 = _okb.fetch_long_short_ratio("FOO", 10.0)
    _okx_w6 = [c for c in _w6.call_args_list if "[okx]" in str(c.args[0])]
check("OKX-6 OKX 미상장 51001 → None, okx 경고 없음", _v6 is None and not _okx_w6)

# OKX-7: 1000배수 계약 매핑 — Binance 는 1000PEPEUSDT, OKX 는 PEPE-USDT-SWAP
_ok_reset(); _calls = []
with patch("requests.get", side_effect=_ok_router(
        bin_status=200, bin_body=[{"longAccount": "0.6728"}], calls=_calls)):
    _v7 = _okb.fetch_long_short_ratio("pepe", 10.0)
check("OKX-7 Binance 1000배수 매핑(PEPE → 1000PEPEUSDT)",
      abs(_v7 - 0.6728) < 1e-9 and _calls[0][1].get("symbol") == "1000PEPEUSDT")
check("OKX-7b 매핑 표(SHIB·XEC·BONK 1000, BABYDOGE 1M, 일반 SOL 그대로)",
      _okb._binance_futures_pair("SHIB") == "1000SHIBUSDT"
      and _okb._binance_futures_pair("XEC") == "1000XECUSDT"
      and _okb._binance_futures_pair("BONK") == "1000BONKUSDT"
      and _okb._binance_futures_pair("BABYDOGE") == "1MBABYDOGEUSDT"
      and _okb._binance_futures_pair("SOL") == "SOLUSDT")
_ok_reset(); _calls = []
with patch("requests.get", side_effect=_ok_router(okx_body=_ok_ls_body, calls=_calls)):
    _okb.fetch_long_short_ratio("PEPE", 10.0)
check("OKX-7c OKX 는 접두사 없이 PEPE-USDT-SWAP",
      any("okx.com" in u and p.get("instId") == "PEPE-USDT-SWAP" for u, p, _ in _calls))

# OKX-8: Binance 200 + [](미상장) → OKX 폴백, Binance 정상값은 OKX 호출 없음
_ok_reset(); _calls = []
with patch("requests.get", side_effect=_ok_router(bin_status=200, bin_body=[], okx_body=_ok_ls_body,
                                                  calls=_calls)):
    _v8 = _okb.fetch_long_short_ratio("SOL", 10.0)
check("OKX-8 Binance 미상장([]) → OKX 폴백", _v8 is not None and abs(_v8 - 0.6096) < 1e-3)
_ok_reset(); _calls = []
with patch("requests.get", side_effect=_ok_router(bin_status=200, bin_body=[{"buySellRatio": "1.2051"}],
                                                  calls=_calls)):
    _v8b = _okb.fetch_taker_buy_sell_ratio("SOL", 10.0)
check("OKX-8b Binance 정상 → 그 값, OKX 호출 0", abs(_v8b - 1.2051) < 1e-9
      and not any("okx.com" in u for u, _, _ in _calls))

# OKX-9: 동명 다른 코인 차단(EDGE·META) → 호출 자체 없음
_ok_reset(); _calls = []
with patch("requests.get", side_effect=_ok_router(okx_body=_ok_ls_body, calls=_calls)):
    _v9 = [_okb.fetch_long_short_ratio("EDGE", 10.0), _okb.fetch_taker_buy_sell_ratio("META", 10.0)]
check("OKX-9 동명 다른 코인 차단 목록 → None·호출 0", _v9 == [None, None] and not _calls)

# OKX-10: 파싱 방어 — 이상 응답(code 50011 레이트리밋, 행 형식 깨짐) → None
_ok_reset()
with patch("requests.get", side_effect=_ok_router(okx_body={"code": "50011", "msg": "Too Many Requests"})):
    _v10a = _okb.fetch_long_short_ratio("SOL", 10.0)
_ok_reset()
with patch("requests.get", side_effect=_ok_router(okx_body={"code": "0", "data": [["123"]]})):
    _v10b = _okb.fetch_taker_buy_sell_ratio("SOL", 10.0)
check("OKX-10 레이트리밋 코드·깨진 행 → None", _v10a is None and _v10b is None)

# OKX-11: 설정 끔 → OKX 호출 없음
_ok_reset(); _calls = []
_cfg_arm.SETTINGS["okx_ratio_fallback_enabled"] = False
try:
    with patch("requests.get", side_effect=_ok_router(okx_body=_ok_ls_body, calls=_calls)):
        _v11 = _okb.fetch_long_short_ratio("SOL", 10.0)
finally:
    _cfg_arm.SETTINGS["okx_ratio_fallback_enabled"] = True
check("OKX-11 okx_ratio_fallback_enabled=False → None·OKX 호출 0",
      _v11 is None and not any("okx.com" in u for u, _, _ in _calls))
_ok_reset()

# ── 2026-09-27 금일 개발분 디버깅 회귀 (DBG-*) ──
from collector.extractor import parse_setup as _dbg_ps
from notify import news_parse as _dbg_np
_r = _dbg_ps("Entry: 2.10\nSL: 1.95\nTarget 1 (2.40)\nTarget 2 (2.60)", current_price=None)
check("DBG-1 'Target 1 (2.40)' 괄호 가격 → long·SL 유지·TP 2.40",
      _r and _r["direction"] == "long" and _r["sl"] == 1.95 and _r["tp"] == 2.4)
_r = _dbg_ps("Entry 45 (support zone)\nTarget 60", current_price=None)
check("DBG-1b 'Entry 45 (support zone)' 가격 보존(RV2-E1 회귀 없음)", _r and _r["entry"] == 45.0)
check("DBG-2 연준 결정 = 가장 앞 동사(holds … cuts → 동결)",
      _dbg_np._fed_move("Fed holds rates steady, signals two cuts later this year") == "동결"
      and _dbg_np._fed_move("Fed holds rates, rules out hikes for now") == "동결"
      and _dbg_np._fed_move("Fed cuts rates by 25 bps") == "인하")
_e = _dbg_np.classify_event("Hackers drain $12M from DeFi protocol using fake token contract")
check("DBG-3 공격 수법 'fake token' 이 있어도 실제 해킹은 해킹", _e and _e.get("label") == "해킹")
_e = _dbg_np.classify_event("Spot Bitcoin ETFs log $1B inflows, analysts say rally could extend")
check("DBG-4 ', analysts say' 뒤 논평은 사실 구절과 분리 → ETF 호재 유지",
      _e and _e.get("pol") == 1 and _e.get("kind") != "call")
_e = _dbg_np.classify_event("Bitcoin ETFs see $500M in inflows as SEC approves in-kind redemptions")
check("DBG-4b 'in-kind redemptions' 는 유출 아님 → 호재", _e and _e.get("pol") == 1)
_txt = tg.render_alert("touch", "ETH", [
    {"direction": "long", "entry_usd": 1234.5, "entry_low_usd": 1230.0, "entry_high_usd": 1234.5,
     "tp_usd": 1400.0, "score": 60, "grade": "A", "author": "x", "collected_at": 0},
    {"direction": "long", "entry_usd": 1230.0, "tp_usd": 1400.0, "score": 50, "grade": "B",
     "author": "y", "collected_at": 0}],
    1234.5 * 0.8 * 1360.0, 1360.0)
_gap_line = [l for l in _txt.split("\n") if "진입:" in l]
check("DBG-5 범위 진입(고가 코인)도 '(현재 -x%)' 꼬리 유지·32칸 이내",
      _gap_line and "~" in _gap_line[0] and "(현재-20%)" in _gap_line[0]
      and tg._line_width(_gap_line[0]) <= 32)

# ── 2026-09-28 디버깅 회귀 (DBG2-*) ──
from notify import news_parse as _n2
def _sl2(t):
    return _n2.summary_line(_n2.parse(t), t)
check("DBG2-1 금리 기대·부정 문맥은 연준 결정 아님(⚪ 매크로 변수)",
      all("⚪" in _sl2(t) and "연준 금리" not in _sl2(t) for t in (
          "Bitcoin slips as rate cut hopes fade", "Traders price out September rate cut",
          "Fed signals no rate cut in September", "Fed rules out rate hikes")))
check("DBG2-1b 확정 결정은 그대로(인하 🟢·인상 🔴·동결 ⚪)",
      "🟢" in _sl2("Fed cuts rates by 25 bps") and "🔴" in _sl2("Fed raises rates by 50bps")
      and "동결" in _sl2("Fed leaves rates unchanged, pushes back on cut bets"))
check("DBG2-2 SEC 소송 취하·승소는 🟢, 제소는 🔴",
      "🟢" in _sl2("SEC drops lawsuit against Coinbase")
      and "🟢" in _sl2("Judge rules in favor of Coinbase in SEC lawsuit")
      and "🔴" in _sl2("SEC sues Binance over unregistered securities"))
check("DBG2-3 해킹 부인(어순 반전)은 해킹 사실 아님",
      (_n2.classify_event("Kraken says reports of a hack are false") or {}).get("label") != "해킹"
      and (_n2.classify_event("Binance says hack claims are fake") or {}).get("label") != "해킹")
_p = _n2.parse("Crypto funds see $2B outflows, ending three-week inflow streak")
check("DBG2-4 끝난 유입 연속 기록은 방향 아님 → 유출", "🔴" in _n2.summary_line(_p, "x"))
check("DBG2-5 상장 예정(to/will list)도 상장 이벤트",
      (_n2.classify_event("Upbit to list Sui (SUI)") or {}).get("label") == "상장"
      and (_n2.classify_event("Binance will list Plasma (XPL)") or {}).get("label") == "상장")
check("DBG2-6 _krw_fmt: NaN·inf 는 '–', 반올림 자릿수 올림(99.96→100, 9.999→10.0)",
      tg._krw_fmt(float("nan")) == "–" and tg._krw_fmt(99.96) == "100"
      and tg._krw_fmt(9.999) == "10.0" and tg._krw_fmt(0.99999) == "1.00")
_txt2 = tg.render_alert("touch", "ABC", [
    {"direction": "long", "entry_usd": 123.4 / 1360, "tp_usd": 140 / 1360, "score": 60,
     "grade": "A", "author": "x", "collected_at": 0},
    {"direction": "long", "entry_usd": 123.0 / 1360, "tp_usd": 140 / 1360, "score": 50,
     "grade": "B", "author": "y", "collected_at": 0}], 124.0, 1360.0)
_el = [l for l in _txt2.split("\n") if "진입:" in l]
check("DBG2-7 범위가 같은 호가 문자열이면 '123~123원' 대신 단일 값", _el and "~" not in _el[0])

print(f"\n{'='*40}")
print(f"  infra 테스트: {n_checks}건 {'전부 통과 ✅' if ok else '실패 있음 ❌'}")
print(f"{'='*40}")
sys.exit(0 if ok else 1)
