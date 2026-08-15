# 터치 시점 기록(2026-08-15 Tier1 스프린트) 오프라인 테스트 — 네트워크/텔레그램 없음.
#
# 검증 범위 (test_infra.py 결 유지 — 임시 DB·check()/✅❌·오프라인 원칙,
# 단 파일은 분리한다: test_infra.py 는 다른 세션이 잡고 있어 충돌 방지):
#   1) 신규 컬럼 5종(touch_grade/touch_score/touch_penetration_pct/
#      touch_closed_below/touch_tp_usd) 자동 마이그레이션
#   2) record_touch_snapshot 최초 기록 우선(IS NULL) + 점수 정수 반올림
#   3) _touch_quality 수학 — 꼬리 스침(저가만 관통, 종가 위) → closed_below=0,
#      종가 안착 → 1, 구 4-튜플 캔들(종가 없음) → NULL, 캔들 없음 → 둘 다 NULL
#   4) 캔들 5-튜플 확장 하위호환 — c[3] 소비처(run_once 실루프)가 4-튜플 그대로 동작
#   5) C등급 무음 푸시 결정(_touch_sound_urgency) — C→low, B→high
#   6) 억제된 터치(send_ok=False)에도 스냅샷이 남는다 — 이 기록의 존재 이유
import sys, os, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

import logging
logging.basicConfig(level=logging.WARNING)

from config import settings
from storage import db, alert_ledger
from monitor import price_check, upbit, binance, market_sentiment, token_events
from notify import telegram

TEST_DB = "cache/_test_touch_recording.db"
settings.SETTINGS["db_path"] = TEST_DB
# 오프라인 원칙 — 외부 API 를 때릴 수 있는 부가 기능은 전부 끈다 (test_price_logic 관례)
settings.SETTINGS["announcement_alert_enabled"] = False
settings.SETTINGS["volume_spike_enabled"] = False
settings.SETTINGS["alert_feedback_enabled"] = False
settings.SETTINGS["orderbook_pressure_enabled"] = False
settings.SETTINGS["preview_alert_enabled"] = False
# 등급 필터를 S 로 조여 모든 터치를 '억제' 경로로 보낸다 — 발송 블록(52주·펀딩 등
# 네트워크 결합 다수)을 아예 타지 않으면서, "억제 터치에도 기록된다"를 함께 증명.
settings.SETTINGS["alert_min_grade"] = "S"

if os.path.exists(TEST_DB):
    os.remove(TEST_DB)
if os.path.exists(alert_ledger.ledger_path(TEST_DB)):
    os.remove(alert_ledger.ledger_path(TEST_DB))
db.init_db(TEST_DB)

now = time.time()
RUN_T = now + 60
USDT_KRW = 1000.0

ok = True
def check(name, cond):
    global ok
    print(("✅" if cond else "❌"), name)
    ok = ok and cond

# ── 1) 컬럼 자동 마이그레이션 ─────────────────────────────────────────────
# SCHEMA 의 CREATE TABLE 에는 신규 컬럼이 없다(_OUTCOME_COLUMNS 전용) —
# init_db 후 존재하면 ALTER 마이그레이션이 실제로 동작한 것이다.
_NEW_COLS = ("touch_grade", "touch_score", "touch_penetration_pct",
             "touch_closed_below", "touch_tp_usd")
with db.connect(TEST_DB) as conn:
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(levels)").fetchall()}
check("M1 신규 컬럼 5종 자동 마이그레이션", all(c in cols for c in _NEW_COLS))

# ── 테스트 레벨 셋업 ─────────────────────────────────────────────────────
# AAA: 2-레벨 클러스터(상단 10.0 도달 + 하단 9.85 섀도) — 4-튜플 캔들(하위호환)
# BBB: 단일 — 5-튜플, 종가가 트리거 이하(안착)
# CCC: 단일 — 5-튜플, 저가만 관통하고 종가는 위(꼬리 스침)
_LEVELS = [
    ("AAA", "KRW-AAA", 10.00, 9.00, 11.5, "T1", "ua1"),
    # 9.93 = 상단 대비 0.7% (클러스터 밴드 1% 이내) + 캔들 저가 9950 위(자기
    # 엔트리 9930 미도달) → 섀도 터치 케이스
    ("AAA", "KRW-AAA", 9.93, 9.00, 11.0, "T2", "ua2"),
    ("BBB", "KRW-BBB", 20.00, 18.0, 23.0, "T3", "ub1"),
    ("CCC", "KRW-CCC", 30.00, 27.0, 34.5, "T4", "uc1"),
]
with db.connect(TEST_DB) as conn:
    for coin, ticker, entry, sl, tp, author, url in _LEVELS:
        lv = dict(coin_symbol=coin, ticker=ticker, direction="long",
                  entry_usd=entry, sl_usd=sl, tp_usd=tp, rr=2.0,
                  grade="B", score=60, author=author,
                  post_url=f"https://tv.com/{url}", post_age_minutes=100,
                  collected_at=now - 600)
        lv["signal_key"] = db.make_signal_key(coin, entry, author, url)
        db.upsert_level(conn, lv)
    # OI 스냅샷 60분 게이트를 이번 회차 시각으로 잠가 CoinGecko 호출을 봉쇄
    db.set_meta(conn, "last_oi_snapshot_at", str(RUN_T))

# ── 오프라인 스텁 (test_price_logic 관례) ────────────────────────────────
sent = []
telegram.send = lambda text, urgency="high", reply_markup=None: (sent.append(urgency) or True)

PRICES = {"KRW-AAA": 10050.0, "KRW-BBB": 20100.0, "KRW-CCC": 30100.0,
          "KRW-USDT": USDT_KRW}
upbit.fetch_prices = lambda mkts, t: {m: PRICES.get(m) for m in mkts}

# AAA 는 일부러 구 4-튜플 — c[3] 소비처(터치 검출·판정 루프) 하위호환의 산 증거.
CANDLES = {
    "KRW-AAA": [(RUN_T - 120, RUN_T - 60, 10100.0, 9950.0)],
    "KRW-BBB": [(RUN_T - 120, RUN_T - 60, 20200.0, 19800.0, 19900.0)],
    "KRW-CCC": [(RUN_T - 120, RUN_T - 60, 30200.0, 29850.0, 30100.0)],
}
upbit.fetch_range_since = lambda m, mins, t: CANDLES.get(m)
upbit.fetch_volume_ranks = lambda t: {}
binance.fetch_usdt_price = lambda s, t: None            # 김프 스냅 생략 경로
market_sentiment.get_sentiment = lambda conn: {"fear_greed": 30, "btc_dominance": 50.0}
token_events.fetch_upcoming_unlocks = lambda conn, t: None

# ── 4)+6) 실루프 구동 — 억제 터치 3클러스터, 전부 기록돼야 한다 ─────────────
s = price_check.run_once(RUN_T)
check("R1 터치 3클러스터 전부 등급(S) 억제 — 발송 0건",
      s["touches"] == 0 and s["suppressed"] == 3 and not sent)

with db.connect(TEST_DB) as conn:
    rows = {r["author"]: dict(r) for r in conn.execute(
        "SELECT * FROM levels").fetchall()}
a1, a2, b1, c1 = rows["T1"], rows["T2"], rows["T3"], rows["T4"]

check("R2 억제 터치에도 touch_grade/touch_score 기록 (전 클러스터 멤버)",
      all(r["touch_grade"] in ("S", "A", "B", "C", "D")
          and isinstance(r["touch_score"], int)
          for r in (a1, a2, b1, c1)))
check("R3 섀도 터치(하단 미도달)도 상태 전이 + 스냅샷 기록",
      a2["status"] == "touched" and a2["touched_at"] is None
      and a2["touch_grade"] is not None)
check("R4 touch_tp_usd — 각 레벨 자신의 TP 동결 (대표 TP 아님)",
      a1["touch_tp_usd"] == 11.5 and a2["touch_tp_usd"] == 11.0
      and b1["touch_tp_usd"] == 23.0)

# 관통 깊이: (트리거-저가)/트리거×100, 트리거 = 클러스터 상단 엔트리 KRW
check("R5 관통 깊이 — AAA (10000-9950)/10000 = 0.5%, 클러스터 전 멤버 동일값",
      abs(a1["touch_penetration_pct"] - 0.5) < 1e-9
      and abs(a2["touch_penetration_pct"] - 0.5) < 1e-9)
check("R6 구 4-튜플 캔들(종가 없음) → touch_closed_below NULL (하위호환)",
      a1["touch_closed_below"] is None)
check("R7 종가 안착 캔들(19900 ≤ 20000) → closed_below=1, 관통 1.0%",
      b1["touch_closed_below"] == 1
      and abs(b1["touch_penetration_pct"] - 1.0) < 1e-9)
check("R8 꼬리 스침(저가 29850 관통, 종가 30100 복귀) → closed_below=0, 관통 0.5%",
      c1["touch_closed_below"] == 0
      and abs(c1["touch_penetration_pct"] - 0.5) < 1e-9)

# ── 2) 최초 기록 우선 + 정수 반올림 ──────────────────────────────────────
with db.connect(TEST_DB) as conn:
    # 값이 전부 차 있는 행(BBB)에 다른 값으로 재기록 시도 — 전 컬럼 최초값 유지.
    # (아직 NULL 인 컬럼은 이후 첫 기록이 채울 수 있다 — IS NULL 가드의 정의 그대로)
    db.record_touch_snapshot(conn, [(b1["id"], "D", 1, 99.9)],
                             penetration_pct=55.5, closed_below=0)
    r = dict(conn.execute("SELECT * FROM levels WHERE id=?", (b1["id"],)).fetchone())
check("W1 최초 기록 우선 — 재기록해도 grade/score/tp/펜/종가이탈 전부 첫 값 유지",
      r["touch_grade"] == b1["touch_grade"] and r["touch_score"] == b1["touch_score"]
      and r["touch_tp_usd"] == 23.0
      and abs(r["touch_penetration_pct"] - 1.0) < 1e-9
      and r["touch_closed_below"] == 1)

with db.connect(TEST_DB) as conn:
    # 신선한 행으로 정수 반올림 + None 필드 미기록 확인
    lv = dict(coin_symbol="ZZZ", ticker="KRW-ZZZ", direction="long",
              entry_usd=1.0, collected_at=now,
              signal_key=db.make_signal_key("ZZZ", 1.0, "TZ", "uz"))
    db.upsert_level(conn, lv)
    zid = conn.execute("SELECT id FROM levels WHERE coin_symbol='ZZZ'").fetchone()["id"]
    db.record_touch_snapshot(conn, [(zid, "C", 61.7, None)])
    z = dict(conn.execute("SELECT * FROM levels WHERE id=?", (zid,)).fetchone())
check("W2 점수 61.7 → INTEGER 62 반올림, tp/펜/종가이탈 None → 미기록(NULL)",
      z["touch_score"] == 62 and z["touch_grade"] == "C"
      and z["touch_tp_usd"] is None and z["touch_penetration_pct"] is None
      and z["touch_closed_below"] is None)

# ── 3) _touch_quality 단위 검증 ──────────────────────────────────────────
_tq = price_check._touch_quality
check("Q1 캔들 없음(현재가 단독 감지) → (None, None)",
      _tq(None, 0, 10000.0) == (None, None) and _tq([], 0, 10000.0) == (None, None))
check("Q2 수집 이전 캔들은 무시 — 수집 이후 첫 관통 캔들이 터치 캔들",
      _tq([(90, 150, 101.0, 95.0, 96.0),      # 수집(100) 이전 — 무시해야 한다
           (160, 220, 101.0, 98.0, 99.5)], 100, 100.0) == (2.0, 1))
check("Q3 저가가 트리거 위(미관통) → (None, None)",
      _tq([(160, 220, 105.0, 101.0, 102.0)], 100, 100.0) == (None, None))
check("Q4 4-튜플(종가 없음) → 관통은 계산, closed_below 만 None",
      _tq([(160, 220, 101.0, 99.0)], 100, 100.0) == (1.0, None))
check("Q5 종가 == 트리거(경계) → '이하' 규칙으로 closed_below=1",
      _tq([(160, 220, 101.0, 99.0, 100.0)], 100, 100.0) == (1.0, 1))

# ── 5) C등급 무음 푸시 결정 (alert_sound_min_grade='B', settings.py 기본값) ──
_ug = price_check._touch_sound_urgency
check("S1 무음 결정 — B 이상(S/A/B)은 유음 high",
      _ug("S", settings.get) == "high" and _ug("A", settings.get) == "high"
      and _ug("B", settings.get) == "high")
check("S2 무음 결정 — B 미만(C/D)·등급 없음은 무음 low",
      _ug("C", settings.get) == "low" and _ug("D", settings.get) == "low"
      and _ug(None, settings.get) == "low")

print()
print("전체 결과:", "✅ ALL PASS" if ok else "❌ FAIL")
sys.exit(0 if ok else 1)
