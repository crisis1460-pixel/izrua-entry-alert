# Hash Ribbons 지표(2026-08-15) 오프라인 단위 테스트.
# 네트워크 호출 없이 requests.get 몽키패치 + 인메모리 DB 캐시로 검증.
import json
import sqlite3
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import logging
logging.basicConfig(level=logging.ERROR)

ok = True
n_checks = 0


def check(name, cond):
    global ok, n_checks
    n_checks += 1
    print(("✅" if cond else "❌"), name)
    if not cond:
        ok = False


from monitor import hash_ribbons as hr
from monitor.binance import derive_supply_verdict


def make_response(values, status=200):
    """values 리스트 → mempool.space 실제 응답 형태 mock."""
    resp = MagicMock()
    resp.status_code = status
    base_ts = 1755000000 - 86400 * len(values)
    resp.json.return_value = {
        "hashrates": [
            {"timestamp": base_ts + 86400 * i, "avgHashrate": v}
            for i, v in enumerate(values)
        ],
        "difficulty": [],
        "currentHashrate": values[-1] if values else 0,
    }
    return resp


def fresh_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    return conn


# ─── _fetch_fresh 상태 판정 ──────────────────────────────────────────

# 1) 지속 하락 → SMA30 < SMA60 = capitulation
declining = [1000.0 - i * 5 for i in range(90)]
with patch.object(hr.requests, "get", return_value=make_response(declining)):
    d = hr._fetch_fresh(10.0)
check("항복: 하락 시계열 → capitulation",
      d is not None and d["state"] == "capitulation")
check("항복: sma30/sma60 비율 < 1",
      d is not None and d["sma30_over_sma60"] < 1.0)

# 2) 급락 후 최근 급회복 (크로스가 14포인트 이내) → recovery
#    0~59일 100, 60~79일 40(항복), 80~89일 300(급회복)
#    현재: sma30=126.7 > sma60=113.3, shift 5 시점엔 sma30<sma60 → 최근 크로스
recovery = [100.0] * 60 + [40.0] * 20 + [300.0] * 10
with patch.object(hr.requests, "get", return_value=make_response(recovery)):
    d = hr._fetch_fresh(10.0)
check("회복: 14일 내 상향 크로스 → recovery",
      d is not None and d["state"] == "recovery")
check("회복: sma30/sma60 비율 > 1",
      d is not None and d["sma30_over_sma60"] > 1.0)

# 3) 계속 상승(크로스 없음/오래전) → normal
rising = [float(i) for i in range(90)]
with patch.object(hr.requests, "get", return_value=make_response(rising)):
    d = hr._fetch_fresh(10.0)
check("정상: 꾸준한 상승(최근 크로스 없음) → normal",
      d is not None and d["state"] == "normal")

# 4) 포인트 부족 (<60) → None
short = [100.0] * 50
with patch.object(hr.requests, "get", return_value=make_response(short)):
    d = hr._fetch_fresh(10.0)
check("실패허용: 60포인트 미만 → None", d is None)

# 5) HTTP 500 → None
with patch.object(hr.requests, "get",
                  return_value=make_response(declining, status=500)):
    d = hr._fetch_fresh(10.0)
check("실패허용: HTTP 500 → None", d is None)

# 6) 예외 (네트워크 오류) → None
with patch.object(hr.requests, "get", side_effect=Exception("boom")):
    d = hr._fetch_fresh(10.0)
check("실패허용: 요청 예외 → None", d is None)


# ─── 캐시 (meta key=hash_ribbons, TTL 6h) ────────────────────────────

conn = fresh_conn()
mock_get = MagicMock(return_value=make_response(declining))
with patch.object(hr.requests, "get", mock_get):
    d1 = hr.fetch_hash_ribbons(conn)
    d2 = hr.fetch_hash_ribbons(conn)
check("캐시: 첫 호출 결과 정상", d1 is not None and d1["state"] == "capitulation")
check("캐시: TTL 내 재호출 = 캐시 반환 (요청 1회)", mock_get.call_count == 1)
check("캐시: 두 호출 결과 동일", d1 == d2)

# TTL 만료 시 재요청
raw = json.loads(conn.execute(
    "SELECT value FROM meta WHERE key='hash_ribbons'").fetchone()["value"])
raw["at"] = time.time() - 7 * 3600  # 7시간 전으로 조작 (TTL 6h 초과)
conn.execute("UPDATE meta SET value=? WHERE key='hash_ribbons'",
             (json.dumps(raw),))
with patch.object(hr.requests, "get", mock_get):
    hr.fetch_hash_ribbons(conn)
check("캐시: TTL 만료 후 재요청", mock_get.call_count == 2)

# 조회 실패 시 None (캐시 없음 + 500)
conn2 = fresh_conn()
with patch.object(hr.requests, "get",
                  return_value=make_response(declining, status=500)):
    d = hr.fetch_hash_ribbons(conn2)
check("캐시: 신규조회 실패 → None (캐시 미저장)",
      d is None and conn2.execute(
          "SELECT COUNT(*) c FROM meta").fetchone()["c"] == 0)


# ─── derive_supply_verdict 연동 ──────────────────────────────────────

# 기준: hash_ribbons 없으면 종전 판정 그대로 (우호)
v = derive_supply_verdict(0.005, 5.0, 2.0)
check("수급연동: 기본(자금 유입=우호)", v == ("우호", "자금 유입"))

# 우호 + capitulation(warn 1) → 중립 강등, reason 유지
v = derive_supply_verdict(0.005, 5.0, 2.0,
                          hash_ribbons={"state": "capitulation",
                                        "sma30_over_sma60": 0.95})
check("수급연동: 우호+항복 → 중립", v == ("중립", "자금 유입"))

# normal 은 보정 없음
v = derive_supply_verdict(0.005, 5.0, 2.0,
                          hash_ribbons={"state": "normal",
                                        "sma30_over_sma60": 1.1})
check("수급연동: normal → 우호 유지", v == ("우호", "자금 유입"))

# 중립 + recovery(confirm) + CVD 확인 = 확인 2개 → 우호 상향
v = derive_supply_verdict(0.005, None, None, cvd_ratio=0.2,
                          hash_ribbons={"state": "recovery",
                                        "sma30_over_sma60": 1.05})
check("수급연동: 중립+회복+CVD확인 → 우호", v[0] == "우호")

# 중립 + recovery 단독(confirm 1) → 상향 없음 (보수 원칙)
v = derive_supply_verdict(0.005, None, None,
                          hash_ribbons={"state": "recovery",
                                        "sma30_over_sma60": 1.05})
check("수급연동: 중립+회복 단독 → 유지", v[0] == "중립")

# hash_ribbons=None → 종전과 동일 (하위 호환)
v = derive_supply_verdict(0.005, 5.0, 2.0, hash_ribbons=None)
check("수급연동: None 전달 = 미전달과 동일", v == ("우호", "자금 유입"))


print()
print(f"{'모두 통과' if ok else '실패 있음'} ({n_checks} checks)")
sys.exit(0 if ok else 1)
