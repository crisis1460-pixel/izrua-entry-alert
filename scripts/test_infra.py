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


# ─── 결과 ────────────────────────────────────────────────────────────

print(f"\n{'='*40}")
print(f"  infra 테스트: {n_checks}건 {'전부 통과 ✅' if ok else '실패 있음 ❌'}")
print(f"{'='*40}")
sys.exit(0 if ok else 1)
