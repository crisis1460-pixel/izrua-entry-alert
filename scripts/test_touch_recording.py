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
#
# 2026-08-16 Tier2 확장 (research_2026-08-15_sharpening_synthesis.md #7·8·10):
#   7) 신규 컬럼 4종(touch_atr_pct/touch_btc_regime/touch_dvol/
#      touch_post_age_hours) 마이그레이션 + 최초 기록 우선
#   8) ATR20% 수학(atr20_pct) — 21캔들 손계산 픽스처, 갭 TR 분기, 표본부족 None
#   9) BTC 레짐 히스테리시스(get_btc_regime) — 초기화 무히스테리시스, 1h 캐시,
#      반대 조건 2일 유지 후 3일째 전환, 조건 복귀 시 후보 리셋, 실패 스테일 폴백
#  10) 글나이 수학(_post_age_hours) — (터치-수집)/3600 + post_age_minutes/60
#
# 2026-08-16 리뷰 수정분 확장 (Fix1~6):
#  11) 진행 중 터치 캔들 → 품질 (None,None) + 다음 회차 백필이 완성 캔들로 소급
#  12) 관통/종가이탈 **레벨별** 계산 — 형제 꼬리/안착 갈림(top closed=1, sib=0),
#      섀도 멤버는 NULL
#  13) 글나이 앵커 = 각 레벨 t_anchor(터치 캔들 종료) — 다운타임 소급 케이스
#  14) touch_grade_ver 도장(v5) + 최초 기록 우선
#  15) 레짐 히스테리시스 = **관측된 KST일** 카운트 — 달력 경과일 아님(구형식
#      메타 무시, 관측 2회/달력 4일 무전환, 같은 날 반복 1회, 관측 3일 전환)
import sys, os, time, json
from datetime import datetime, timezone
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
from monitor import price_check, upbit, binance, market_sentiment, token_events, macro
from notify import telegram

TEST_DB = "cache/_test_touch_recording.db"
settings.SETTINGS["db_path"] = TEST_DB
# 오프라인 원칙 — 외부 API 를 때릴 수 있는 부가 기능은 전부 끈다 (test_price_logic 관례)
settings.SETTINGS["announcement_alert_enabled"] = False
settings.SETTINGS["volume_spike_enabled"] = False
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
# Tier2(08-16) 4종 + 리뷰 Fix5(touch_grade_ver) 포함 총 10종.
_NEW_COLS = ("touch_grade", "touch_score", "touch_penetration_pct",
             "touch_closed_below", "touch_tp_usd",
             "touch_atr_pct", "touch_btc_regime", "touch_dvol",
             "touch_post_age_hours", "touch_grade_ver")
with db.connect(TEST_DB) as conn:
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(levels)").fetchall()}
check("M1 신규 컬럼 10종(Tier1 5 + Tier2 4 + grade_ver) 자동 마이그레이션",
      all(c in cols for c in _NEW_COLS))

# ── 테스트 레벨 셋업 ─────────────────────────────────────────────────────
# AAA: 2-레벨 클러스터(상단 10.0 도달 + 하단 9.85 섀도) — 4-튜플 캔들(하위호환)
# BBB: 단일 — 5-튜플, 종가가 트리거 이하(안착)
# CCC: 단일 — 5-튜플, 저가만 관통하고 종가는 위(꼬리 스침)
# DDD: 2-레벨 클러스터 **양쪽 도달** (Fix2 형제 케이스) — 저가 19800 이 상단
#      20.00·하단 19.82 둘 다 관통, 종가 19900 은 상단 이하·하단 위 →
#      top closed=1 / sib closed=0 로 갈려야 한다(종전엔 둘 다 상단 기준 1).
# EEE: 단일 — 터치 캔들이 회차 시각(RUN_T)에 **진행 중**(종료 RUN_T+30) →
#      품질 NULL(Fix1), 다음 회차 백필이 완성 캔들로 소급 기록(Fix1b).
# FFF: 단일 — 다운타임 소급 터치(캔들이 1시간 전) → 글나이 앵커가 회차
#      시각이 아니라 터치 캔들 종료 시각이어야 한다(Fix3).
_LEVELS = [
    ("AAA", "KRW-AAA", 10.00, 9.00, 11.5, "T1", "ua1"),
    # 9.93 = 상단 대비 0.7% (클러스터 밴드 1% 이내) + 캔들 저가 9950 위(자기
    # 엔트리 9930 미도달) → 섀도 터치 케이스
    ("AAA", "KRW-AAA", 9.93, 9.00, 11.0, "T2", "ua2"),
    ("BBB", "KRW-BBB", 20.00, 18.0, 23.0, "T3", "ub1"),
    ("CCC", "KRW-CCC", 30.00, 27.0, 34.5, "T4", "uc1"),
    ("DDD", "KRW-DDD", 20.00, 18.0, 23.0, "T5", "ud1"),
    ("DDD", "KRW-DDD", 19.82, 18.0, 22.8, "T6", "ud2"),  # 0.9% — 밴드 내 형제
    ("EEE", "KRW-EEE", 50.00, 45.0, 57.5, "T7", "ue1"),
    ("FFF", "KRW-FFF", 40.00, 36.0, 46.0, "T8", "uf1"),
]
with db.connect(TEST_DB) as conn:
    for coin, ticker, entry, sl, tp, author, url in _LEVELS:
        lv = dict(coin_symbol=coin, ticker=ticker, direction="long",
                  entry_usd=entry, sl_usd=sl, tp_usd=tp, rr=2.0,
                  grade="B", score=60, author=author,
                  post_url=f"https://tv.com/{url}", post_age_minutes=100,
                  # FFF 만 2시간 전 수집 — 1시간 전 캔들(_eff_low 의 수집 이후
                  # 원칙)로 소급 터치가 성립하려면 수집이 캔들보다 앞서야 한다
                  collected_at=(now - 7200) if coin == "FFF" else (now - 600))
        lv["signal_key"] = db.make_signal_key(coin, entry, author, url)
        db.upsert_level(conn, lv)
    # OI 스냅샷 60분 게이트를 이번 회차 시각으로 잠가 CoinGecko 호출을 봉쇄
    db.set_meta(conn, "last_oi_snapshot_at", str(RUN_T))

# ── 오프라인 스텁 (test_price_logic 관례) ────────────────────────────────
sent = []
telegram.send = lambda text, urgency="high", reply_markup=None: (sent.append(urgency) or True)

PRICES = {"KRW-AAA": 10050.0, "KRW-BBB": 20100.0, "KRW-CCC": 30100.0,
          "KRW-DDD": 20100.0, "KRW-EEE": 50100.0, "KRW-FFF": 40200.0,
          "KRW-USDT": USDT_KRW}
upbit.fetch_prices = lambda mkts, t: {m: PRICES.get(m) for m in mkts}

# AAA 는 일부러 구 4-튜플 — c[3] 소비처(터치 검출·판정 루프) 하위호환의 산 증거.
# EEE 캔들은 RUN_T 시점 진행 중(종료 RUN_T+30) — Fix1 검증용.
CANDLES = {
    "KRW-AAA": [(RUN_T - 120, RUN_T - 60, 10100.0, 9950.0)],
    "KRW-BBB": [(RUN_T - 120, RUN_T - 60, 20200.0, 19800.0, 19900.0)],
    "KRW-CCC": [(RUN_T - 120, RUN_T - 60, 30200.0, 29850.0, 30100.0)],
    "KRW-DDD": [(RUN_T - 120, RUN_T - 60, 20200.0, 19800.0, 19900.0)],
    "KRW-EEE": [(RUN_T - 30, RUN_T + 30, 50100.0, 49900.0, 49950.0)],
    "KRW-FFF": [(RUN_T - 3600, RUN_T - 3540, 40100.0, 39900.0, 40050.0)],
}
upbit.fetch_range_since = lambda m, mins, t: CANDLES.get(m)
upbit.fetch_volume_ranks = lambda t: {}
binance.fetch_usdt_price = lambda s, t: None            # 김프 스냅 생략 경로
market_sentiment.get_sentiment = lambda conn: {"fear_greed": 30, "btc_dominance": 50.0}
token_events.fetch_upcoming_unlocks = lambda conn, t: None
# BTC 레짐 (2026-08-16 Tier2) — 억제 터치 스냅샷도 _btc_regime() 을 부르므로
# 오프라인 스텁 필수(안 그러면 업비트 BTC 일봉을 실제로 때린다). 실물
# get_btc_regime 로직은 아래 H* 블록이 원시 조건 스텁으로 따로 검증한다.
_orig_get_btc_regime = macro.get_btc_regime
macro.get_btc_regime = lambda conn, t: "above"

# ── 4)+6) 실루프 구동 — 억제 터치 6클러스터, 전부 기록돼야 한다 ─────────────
s = price_check.run_once(RUN_T)
check("R1 터치 6클러스터 전부 등급(S) 억제 — 발송 0건",
      s["touches"] == 0 and s["suppressed"] == 6 and not sent)

with db.connect(TEST_DB) as conn:
    rows = {r["author"]: dict(r) for r in conn.execute(
        "SELECT * FROM levels").fetchall()}
a1, a2, b1, c1 = rows["T1"], rows["T2"], rows["T3"], rows["T4"]
d1, d2, e1, f1 = rows["T5"], rows["T6"], rows["T7"], rows["T8"]

check("R2 억제 터치에도 touch_grade/touch_score 기록 (전 클러스터 멤버)",
      all(r["touch_grade"] in ("S", "A", "B", "C", "D")
          and isinstance(r["touch_score"], int)
          for r in (a1, a2, b1, c1, d1, d2, e1, f1)))
check("R3 섀도 터치(하단 미도달)도 상태 전이 + 스냅샷 기록",
      a2["status"] == "touched" and a2["touched_at"] is None
      and a2["touch_grade"] is not None)
check("R4 touch_tp_usd — 각 레벨 자신의 TP 동결 (대표 TP 아님)",
      a1["touch_tp_usd"] == 11.5 and a2["touch_tp_usd"] == 11.0
      and b1["touch_tp_usd"] == 23.0)

# 관통 깊이: (자기 엔트리-저가)/자기 엔트리×100 — **레벨별** (2026-08-16 Fix2).
# 섀도 멤버(자기 엔트리 미도달)는 품질 자체가 무의미 → NULL.
check("R5 관통 깊이 — a1 (10000-9950)/10000=0.5%, 섀도 a2 는 NULL (레벨별)",
      abs(a1["touch_penetration_pct"] - 0.5) < 1e-9
      and a2["touch_penetration_pct"] is None
      and a2["touch_closed_below"] is None)
check("R6 구 4-튜플 캔들(종가 없음) → touch_closed_below NULL (하위호환)",
      a1["touch_closed_below"] is None)
check("R7 종가 안착 캔들(19900 ≤ 20000) → closed_below=1, 관통 1.0%",
      b1["touch_closed_below"] == 1
      and abs(b1["touch_penetration_pct"] - 1.0) < 1e-9)
check("R8 꼬리 스침(저가 29850 관통, 종가 30100 복귀) → closed_below=0, 관통 0.5%",
      c1["touch_closed_below"] == 0
      and abs(c1["touch_penetration_pct"] - 0.5) < 1e-9)
# Fix2 형제 케이스(리뷰 원문 그대로): top 20000/sib 19820, 저가 19800, 종가 19900
# → top 은 종가 안착(closed=1), sib 은 자기 엔트리 위 복귀(closed=0) + 관통도
# 각자 기준 — 종전엔 둘 다 상단 기준 (1.0%, 1) 복제였다.
check("R12 형제 레벨별 품질 — top closed=1/pen 1.0%, sib closed=0/pen 0.1009%",
      d1["touch_closed_below"] == 1
      and abs(d1["touch_penetration_pct"] - 1.0) < 1e-9
      and d2["touch_closed_below"] == 0
      and abs(d2["touch_penetration_pct"] - (20.0 / 19820.0 * 100)) < 1e-9)
# Fix1: 터치 캔들이 회차 시각에 진행 중(종료 RUN_T+30 > RUN_T) → 잠정 종가로
# 기록하지 않는다 — 품질 둘 다 NULL (다음 회차 백필 대상).
check("R13 진행 중 터치 캔들 → 품질 NULL + touched_at 은 미래 앵커(RUN_T+30)",
      e1["touch_penetration_pct"] is None and e1["touch_closed_below"] is None
      and abs(e1["touched_at"] - (RUN_T + 30)) < 1e-6)

# ── Tier2(08-16) 실루프 기록 — 레짐·글나이는 억제 터치에도 남고, ATR/DVOL 은
# 발송 경로(fetch_position_data/옵션 컨텍스트) 전용이라 억제 회차엔 NULL 이어야
# 한다("이미 로드된 것만 재사용, 추가 API 콜 0" 원칙의 살아있는 증명).
check("R9 억제 터치에도 touch_btc_regime 도장 (전 클러스터 멤버)",
      all(r["touch_btc_regime"] == "above"
          for r in (a1, a2, b1, c1, d1, d2, e1, f1)))
check("R10 억제 터치는 ATR/DVOL 미조회 → NULL (추가 API 콜 0 원칙)",
      all(r["touch_atr_pct"] is None and r["touch_dvol"] is None
          for r in (a1, a2, b1, c1, d1, d2, e1, f1)))
# 글나이 앵커 = t_anchor(터치 캔들 종료, 2026-08-16 Fix3) — 회차 시각(now)이
# 아니다. 도달 멤버(a1/b1/c1): (RUN_T-60 - (now-600))/3600 + 100/60
# = 600/3600 + 100/60 = 1.833…h. 섀도 a2 는 앵커가 없어 now 폴백 = 1.85h.
check("R11 touch_post_age_hours — 도달 멤버는 캔들 앵커(1.8333h), 섀도는 now(1.85h)",
      all(abs(r["touch_post_age_hours"] - (600 / 3600 + 100 / 60)) < 1e-6
          for r in (a1, b1, c1))
      and abs(a2["touch_post_age_hours"] - 1.85) < 1e-6)
# 다운타임 소급 케이스: FFF 터치 캔들 종료 = RUN_T-3540 (1시간 전 소급 검출).
# 앵커 기준 = (RUN_T-3540 - (now-7200))/3600 + 100/60 = 3720/3600 + 5/3 = 2.7h.
# 구 코드(now 앵커)라면 7260/3600 + 5/3 ≈ 3.683h 로 1시간 부풀었다.
check("R14 다운타임 소급 터치 — 글나이가 실제 터치 시각 기준(2.7h)",
      abs(f1["touch_post_age_hours"] - 2.7) < 1e-6)
# Fix5: 터치 재채점 산식 버전 도장 — 클러스터 공통(settings.grade_formula_ver).
check("R15 touch_grade_ver = 현행 산식 버전(v5) 도장 (전 멤버)",
      all(r["touch_grade_ver"] == settings.get("grade_formula_ver")
          for r in (a1, a2, b1, c1, d1, d2, e1, f1)))

# ── 11) 백필 — 다음 회차(RUN_T2)에 완성된 EEE 터치 캔들로 품질 소급 기록 ────
RUN_T2 = RUN_T + 180   # EEE 캔들 종료(RUN_T+30) + 60초 여유가 지난 시각
s2 = price_check.run_once(RUN_T2)
with db.connect(TEST_DB) as conn:
    e1b = dict(conn.execute("SELECT * FROM levels WHERE id=?",
                            (e1["id"],)).fetchone())
    d1b = dict(conn.execute("SELECT * FROM levels WHERE id=?",
                            (d1["id"],)).fetchone())
check("B1 백필 — 완성 캔들로 pen=(50000-49900)/50000=0.2%, closed=1(49950≤50000)",
      abs(e1b["touch_penetration_pct"] - 0.2) < 1e-9
      and e1b["touch_closed_below"] == 1)
check("B2 백필은 양쪽 NULL 행만 — 이미 기록된 행(d1)은 불변(최초 기록 우선)",
      d1b["touch_penetration_pct"] == d1["touch_penetration_pct"]
      and d1b["touch_closed_below"] == d1["touch_closed_below"])

# ── 2) 최초 기록 우선 + 정수 반올림 ──────────────────────────────────────
with db.connect(TEST_DB) as conn:
    # 값이 전부 차 있는 행(BBB)에 다른 값으로 재기록 시도 — 전 컬럼 최초값 유지.
    # (아직 NULL 인 컬럼은 이후 첫 기록이 채울 수 있다 — IS NULL 가드의 정의 그대로)
    # 행 형태는 7-튜플 (2026-08-16 Fix2): 관통/종가이탈이 레벨별 원소로 이동.
    db.record_touch_snapshot(conn, [(b1["id"], "D", 1, 99.9, 77.7, 55.5, 0)],
                             btc_regime="below", grade_ver="v99")
    r = dict(conn.execute("SELECT * FROM levels WHERE id=?", (b1["id"],)).fetchone())
check("W1 최초 기록 우선 — 재기록해도 grade/score/tp/펜/종가이탈 전부 첫 값 유지",
      r["touch_grade"] == b1["touch_grade"] and r["touch_score"] == b1["touch_score"]
      and r["touch_tp_usd"] == 23.0
      and abs(r["touch_penetration_pct"] - 1.0) < 1e-9
      and r["touch_closed_below"] == 1)
check("W1b Tier2 도 최초 기록 우선 — 레짐 above 유지, 글나이 유지",
      r["touch_btc_regime"] == "above"
      and abs(r["touch_post_age_hours"] - (600 / 3600 + 100 / 60)) < 1e-6)
check("W1d touch_grade_ver 도 최초 기록 우선 — v99 재기록해도 v5 유지 (Fix5)",
      r["touch_grade_ver"] == settings.get("grade_formula_ver"))

with db.connect(TEST_DB) as conn:
    # 실루프에서 NULL 로 남은 ATR/DVOL — 이후 '첫' 기록이 채우고(IS NULL 가드의
    # 정의), 그 다음 재기록은 무시된다(첫 값 승리).
    db.record_touch_snapshot(conn, [(b1["id"], None, None, None, None, None, None)],
                             atr_pct=5.5, dvol=60.0)
    db.record_touch_snapshot(conn, [(b1["id"], None, None, None, None, None, None)],
                             atr_pct=7.7, dvol=80.0, btc_regime="below")
    r = dict(conn.execute("SELECT * FROM levels WHERE id=?", (b1["id"],)).fetchone())
check("W1c NULL 컬럼은 첫 기록이 채우고(atr 5.5/dvol 60.0), 재기록은 무시",
      r["touch_atr_pct"] == 5.5 and r["touch_dvol"] == 60.0
      and r["touch_btc_regime"] == "above")

with db.connect(TEST_DB) as conn:
    # 신선한 행으로 정수 반올림 + None 필드 미기록 확인
    lv = dict(coin_symbol="ZZZ", ticker="KRW-ZZZ", direction="long",
              entry_usd=1.0, collected_at=now,
              signal_key=db.make_signal_key("ZZZ", 1.0, "TZ", "uz"))
    db.upsert_level(conn, lv)
    zid = conn.execute("SELECT id FROM levels WHERE coin_symbol='ZZZ'").fetchone()["id"]
    db.record_touch_snapshot(conn, [(zid, "C", 61.7, None, None, None, None)])
    z = dict(conn.execute("SELECT * FROM levels WHERE id=?", (zid,)).fetchone())
check("W2 점수 61.7 → INTEGER 62 반올림, tp/펜/종가이탈 None → 미기록(NULL)",
      z["touch_score"] == 62 and z["touch_grade"] == "C"
      and z["touch_tp_usd"] is None and z["touch_penetration_pct"] is None
      and z["touch_closed_below"] is None)
check("W2b Tier2 None 필드 미기록 — atr/레짐/dvol/글나이/산식버전 전부 NULL",
      z["touch_atr_pct"] is None and z["touch_btc_regime"] is None
      and z["touch_dvol"] is None and z["touch_post_age_hours"] is None
      and z["touch_grade_ver"] is None)

# ── 3) _touch_quality 단위 검증 (2026-08-16 Fix1: now 인자 — 완성 캔들만) ──
_tq = price_check._touch_quality
check("Q1 캔들 없음(현재가 단독 감지) → (None, None)",
      _tq(None, 0, 10000.0, 1000) == (None, None)
      and _tq([], 0, 10000.0, 1000) == (None, None))
check("Q2 수집 이전 캔들은 무시 — 수집 이후 첫 관통 캔들이 터치 캔들",
      _tq([(90, 150, 101.0, 95.0, 96.0),      # 수집(100) 이전 — 무시해야 한다
           (160, 220, 101.0, 98.0, 99.5)], 100, 100.0, 1000) == (2.0, 1))
check("Q3 저가가 트리거 위(미관통) → (None, None)",
      _tq([(160, 220, 105.0, 101.0, 102.0)], 100, 100.0, 1000) == (None, None))
check("Q4 4-튜플(종가 없음) → 관통은 계산, closed_below 만 None",
      _tq([(160, 220, 101.0, 99.0)], 100, 100.0, 1000) == (1.0, None))
check("Q5 종가 == 트리거(경계) → '이하' 규칙으로 closed_below=1",
      _tq([(160, 220, 101.0, 99.0, 100.0)], 100, 100.0, 1000) == (1.0, 1))
# Fix1: 첫 터치 캔들이 진행 중(종료 220 > now 200)이면 (None, None) — 그리고
# 뒤쪽의 (완성된) 관통 캔들로 **넘어가지 않는다**(첫 터치 캔들이 아닌 캔들의
# 품질은 이 지표의 정의가 아님 — 넘어갔다면 (3.0, 1) 이 나왔을 것).
check("Q6 진행 중 터치 캔들 → (None, None), 뒤 캔들로 폴스루 금지",
      _tq([(160, 220, 101.0, 99.0, 99.5),
           (130, 190, 101.0, 97.0, 97.0)], 100, 100.0, 200) == (None, None))

# ── 5) C등급 무음 푸시 결정 (alert_sound_min_grade='B', settings.py 기본값) ──
_ug = price_check._touch_sound_urgency
check("S1 무음 결정 — B 이상(S/A/B)은 유음 high",
      _ug("S", settings.get) == "high" and _ug("A", settings.get) == "high"
      and _ug("B", settings.get) == "high")
check("S2 무음 결정 — B 미만(C/D)·등급 없음은 무음 low",
      _ug("C", settings.get) == "low" and _ug("D", settings.get) == "low"
      and _ug(None, settings.get) == "low")

# ── 8) ATR20% 수학 (upbit.atr20_pct — 손계산 픽스처) ─────────────────────
# 21캔들 동형(고105/저95/종100): TR 20개 전부 max(10, 5, 5)=10 → ATR=10
# → 마지막 종가 100 대비 10.0%
_flat = [(105.0, 95.0, 100.0)] * 21
check("A1 동형 21캔들 — ATR=10, 종가 100 대비 10.0%",
      abs(upbit.atr20_pct(_flat) - 10.0) < 1e-9)
# 22번째 갭 캔들(고130/저120/종125, 전일 종가 100): TR=max(10,30,20)=30 —
# |고-전종가| 분기의 산 증거. Wilder 평활: (10×19+30)/20 = 11.0 → 11/125 = 8.8%
_gap = _flat + [(130.0, 120.0, 125.0)]
check("A2 갭 TR 분기 + Wilder 평활 — (10×19+30)/20=11 → 종가 125 대비 8.8%",
      abs(upbit.atr20_pct(_gap) - 8.8) < 1e-9)
check("A3 표본 부족(20캔들 = TR 19개)·빈 입력·None → 전부 None (fail-safe)",
      upbit.atr20_pct(_flat[:20]) is None and upbit.atr20_pct(None) is None
      and upbit.atr20_pct([]) is None)

# ── 9) BTC 레짐 히스테리시스 (실물 get_btc_regime + 원시 조건 스텁) ─────────
macro.get_btc_regime = _orig_get_btc_regime   # 실루프용 스텁 해제 — 실물 검증
_raw = {"v": "above", "calls": 0}
def _stub_raw(t):
    _raw["calls"] += 1
    return _raw["v"]
macro._btc_ma200_raw = _stub_raw

T0 = RUN_T
D = 86400.0
with db.connect(TEST_DB) as conn:
    r1 = macro.get_btc_regime(conn, 5.0, now=T0)
    check("H1 최초 호출 — 원시 조건으로 즉시 초기화(above), 히스테리시스 없음",
          r1 == "above" and _raw["calls"] == 1)
    _raw["v"] = "below"
    r2 = macro.get_btc_regime(conn, 5.0, now=T0 + 60)
    check("H2 1h 캐시 — 조건이 바뀌어도 TTL 내에는 조회 없이 저장 상태 반환",
          r2 == "above" and _raw["calls"] == 1)
    r3 = macro.get_btc_regime(conn, 5.0, now=T0 + D)
    r4 = macro.get_btc_regime(conn, 5.0, now=T0 + 2 * D)
    check("H3 반대 조건 1·2일째 — 상태 유지(above)",
          r3 == "above" and r4 == "above")
    r5 = macro.get_btc_regime(conn, 5.0, now=T0 + 3 * D)
    check("H4 반대 조건 3 KST일 연속 — 전환(below)", r5 == "below")
    # 후보 리셋: above 후보 1일 → 조건이 below 로 복귀(리셋) → 다시 above.
    # 리셋이 없었다면 T0+7D 시점에 첫 후보일(T0+4D)부터 4일이 차서 이미
    # 조기 전환됐을 것 — r6 가 below 로 남는 것이 리셋의 증명이다.
    _raw["v"] = "above"
    macro.get_btc_regime(conn, 5.0, now=T0 + 4 * D)       # 후보 1일째
    _raw["v"] = "below"
    macro.get_btc_regime(conn, 5.0, now=T0 + 5 * D)       # 조건 복귀 — 후보 리셋
    _raw["v"] = "above"
    macro.get_btc_regime(conn, 5.0, now=T0 + 6 * D)       # 새 후보 1일째
    r6 = macro.get_btc_regime(conn, 5.0, now=T0 + 7 * D)  # 2일째 — 유지
    r7 = macro.get_btc_regime(conn, 5.0, now=T0 + 8 * D)  # 3일째 — 전환
    check("H5 조건 복귀 시 후보 리셋 — 카운트 재시작(2일째 유지, 3일째 전환)",
          r6 == "below" and r7 == "above")
    # 실패 폴백: 원시 조건 None → 스테일 상태 반환 + meta 미갱신 → 다음 호출이
    # TTL 재계산 없이 곧바로 재시도한다(호출 수 +2 로 증명).
    _raw["v"] = None
    _calls_before = _raw["calls"]
    r8 = macro.get_btc_regime(conn, 5.0, now=T0 + 9 * D)
    r9 = macro.get_btc_regime(conn, 5.0, now=T0 + 9 * D + 1)
    check("H6 조회 실패 — 스테일 상태(above) 반환 + 캐시 미갱신(즉시 재시도)",
          r8 == "above" and r9 == "above" and _raw["calls"] == _calls_before + 2)

# ── 15) 관측일 카운트 히스테리시스 (2026-08-16 리뷰 Fix6) ────────────────
# 구 규칙(cand_since 달력 경과일)은 중간 날 관측이 없어도 달력만 3일 지나면
# 뒤집었다 — 이제 "서로 다른 KST일의 관측 3회"가 필요하다. KST 날짜 경계
# 오염을 피하려고 고정 시각(09:00 KST = 00:00 UTC) 기준으로 돌린다.
B = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc).timestamp()  # 09:00 KST
with db.connect(TEST_DB) as conn:
    # 구형식 메타(cand_since, cand_days 없음) 심기 — 후보 없음으로 재구축돼야
    # 한다. cand_since 가 아무리 오래됐어도 그 나이로 전환하면 안 된다.
    db.set_meta(conn, "btc_regime_state", json.dumps(
        {"state": "above", "cand": "below", "cand_since": "2026-08-20", "at": 0}))
    _raw["v"] = "below"
    r10 = macro.get_btc_regime(conn, 5.0, now=B)
    check("H7 구형식 메타(cand_since) — 후보 재구축(관측 1일째), 달력 나이 무시",
          r10 == "above")
    # 관측 2회가 달력 4일 떨어져 있어도 '관측일 2일' — 전환 없음
    # (구 규칙이라면 양끝 포함 5일로 이미 뒤집었을 지점 — 다운타임 오전환 재현)
    r11 = macro.get_btc_regime(conn, 5.0, now=B + 4 * D)
    check("H8 관측 2회/달력 5일 — 관측일 기준 2일이라 전환 없음", r11 == "above")
    # 같은 KST일 두 번째 관측(2h 뒤, TTL 1h 경과) — 카운트 불변(여전히 2일)
    r12 = macro.get_btc_regime(conn, 5.0, now=B + 4 * D + 2 * 3600)
    check("H9 같은 KST일 반복 관측은 1회 — 여전히 전환 없음", r12 == "above")
    # 세 번째 '새로운' 관측일 — 3일 충족, 전환
    r13 = macro.get_btc_regime(conn, 5.0, now=B + 6 * D)
    check("H10 서로 다른 3 KST일 관측 — 전환(below)", r13 == "below")

# ── 10) 글나이 수학 (price_check._post_age_hours) ────────────────────────
_pa = price_check._post_age_hours
check("P1 글나이 — 3600초/3600 + 90분/60 = 2.5h",
      abs(_pa({"collected_at": 1000.0, "post_age_minutes": 90}, 4600.0) - 2.5) < 1e-9)
check("P2 post_age_minutes 없음(None) → 수집→터치 경과분만",
      abs(_pa({"collected_at": 1000.0, "post_age_minutes": None}, 4600.0) - 1.0) < 1e-9)
check("P3 수집 시각 없음 → None (fail-safe)",
      _pa({"collected_at": None, "post_age_minutes": 90}, 4600.0) is None
      and _pa({}, 4600.0) is None)

print()
print("전체 결과:", "✅ ALL PASS" if ok else "❌ FAIL")
sys.exit(0 if ok else 1)
