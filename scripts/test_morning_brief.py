# 모닝 브리핑(notify/morning_brief.py) 오프라인 테스트 — 네트워크·텔레그램 없이
# 몽키패치로 검증. 커버: 하루 1회/시간창 게이트, 발송 실패 시 재시도 가능(날짜
# 미기록), 전 데이터 None 이어도 조립이 죽지 않음, run_cycle 결과 dict 편입.
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import logging
logging.basicConfig(level=logging.CRITICAL)

from config import settings
from monitor import binance, market_sentiment, options, upbit
from monitor import macro as macro_mod
from notify import morning_brief, telegram
from storage import db

TEST_DB = "cache/_test_morning_brief.db"
settings.SETTINGS["db_path"] = TEST_DB
if os.path.exists(TEST_DB):
    os.remove(TEST_DB)
db.init_db(TEST_DB)

ok = True
KST = timezone(timedelta(hours=9))


def check(name, cond):
    global ok
    print(("✅" if cond else "❌"), name)
    ok = ok and cond


# ── 오프라인 강제: 데이터 페처 전부 무력화(전 항목 None 경로) ─────────────
upbit.fetch_prices = lambda markets, timeout: {}
binance.fetch_usdt_price = lambda symbol, timeout: None
market_sentiment.get_sentiment = lambda conn: None
options.fetch_btc_options_context = lambda timeout=10.0: None
macro_mod.fetch_dxy = lambda conn, timeout=10.0: None
macro_mod.fetch_us_indices = lambda conn, timeout=10.0: None
macro_mod.fetch_vix = lambda conn, timeout=10.0: None
macro_mod.fetch_ust_10y = lambda conn, timeout=10.0: None

# 텔레그램 발송 목(mock) — 발송문을 기록하고 성공/실패를 전환할 수 있다
sent_log = []
_send_result = {"ok": True}


def _fake_send(text, urgency="high", reply_to_message_id=None):
    sent_log.append(text)
    # 반환 타입 (2026-08-17 #6): Optional[int]. 성공 시 정수, 실패 시 None.
    return 1 if _send_result["ok"] else None


telegram.send = _fake_send


def set_brief_meta(val):
    with db.connect(TEST_DB) as conn:
        db.set_meta(conn, morning_brief.META_LAST_BRIEF_DATE, val)


def get_brief_meta():
    with db.connect(TEST_DB) as conn:
        return db.get_meta(conn, morning_brief.META_LAST_BRIEF_DATE)


def due(now):
    with db.connect(TEST_DB) as conn:
        return morning_brief.brief_due(conn, now)


# 발송 창 기본값(8~10시) 기준 고정 시각 — 로컬 시간대와 무관하게 KST 로 고정
AT_9 = datetime(2026, 8, 15, 9, 0, tzinfo=KST).timestamp()    # 창 안
AT_7 = datetime(2026, 8, 15, 7, 59, tzinfo=KST).timestamp()   # 창 전
AT_10 = datetime(2026, 8, 15, 10, 0, tzinfo=KST).timestamp()  # 창 끝(미만이라 제외)
TODAY = "2026-08-15"

# ── G1~G6: 발송 게이트 ────────────────────────────────────────────────
set_brief_meta("")
check("G1 창 안 + 미발송이면 due", due(AT_9)[0] is True)
check("G2 창 시작 전(KST 7:59)은 대기", due(AT_7)[0] is False)
check("G3 창 끝(KST 10:00, hour_to 미만)은 제외", due(AT_10)[0] is False)

set_brief_meta(TODAY)
check("G4 오늘 이미 발송했으면 스킵", due(AT_9)[0] is False)
check("G5 다음날 아침엔 다시 due", due(AT_9 + 86400.0)[0] is True)

settings.SETTINGS["morning_brief_enabled"] = False
check("G6 스위치 OFF 면 스킵", due(AT_9 + 86400.0)[0] is False)
settings.SETTINGS["morning_brief_enabled"] = True

# ── B1~B3: 전 데이터 None 이어도 조립이 죽지 않는다 ──────────────────────
with db.connect(TEST_DB) as conn:
    text = morning_brief.build_brief(conn, AT_9, timeout=1.0)
check("B1 전 항목 None 이어도 문자열 반환", isinstance(text, str) and len(text) > 0)
check("B2 헤더(제목+날짜)는 항상 포함", "모닝 브리핑" in text and TODAY in text)
check("B3 결측 행은 생략(김프/달러지수 미노출)", "김프" not in text and "달러지수" not in text)

# B3b: 데이터 있을 때 한국어 라벨 확인 + 미국 증시 행
macro_mod.fetch_dxy = lambda conn, timeout=10.0: 103.45
options.fetch_btc_options_context = lambda timeout=10.0: {"dvol": 52.3}
macro_mod.fetch_us_indices = lambda conn, timeout=10.0: {"sp500": 0.87, "nasdaq": -0.34}
with db.connect(TEST_DB) as conn:
    text_full = morning_brief.build_brief(conn, AT_9, timeout=1.0)
check("B3b 달러지수 단독 줄(DXY 아님)", "달러지수 103.45" in text_full and "DXY" not in text_full)
check("B3c BTC변동성 단독 줄(DVOL 아님)", "BTC변동성 52" in text_full and "DVOL" not in text_full)
check("B3d S&P500 단독 줄", "S&P500 +0.87%" in text_full)
check("B3e 나스닥 단독 줄", "나스닥 -0.34%" in text_full)

# FRED VIX / 10Y 국채 (2026-08-17) — 데이터 있을 때 표시, 결측이면 생략
macro_mod.fetch_vix = lambda conn, timeout=10.0: 18.4
macro_mod.fetch_ust_10y = lambda conn, timeout=10.0: 4.23
with db.connect(TEST_DB) as conn:
    text_fred = morning_brief.build_brief(conn, AT_9, timeout=1.0)
check("B3m VIX 단독 줄", "VIX 18.4" in text_fred)
check("B3n 미국 10년 국채금리 단독 줄", "미국 10년 국채금리 4.23%" in text_fred)
# 결측 시 행 생략
macro_mod.fetch_vix = lambda conn, timeout=10.0: None
macro_mod.fetch_ust_10y = lambda conn, timeout=10.0: None
with db.connect(TEST_DB) as conn:
    text_no_fred = morning_brief.build_brief(conn, AT_9, timeout=1.0)
check("B3o VIX None 이면 행 생략", "VIX" not in text_no_fred)
check("B3p 10년물 None 이면 행 생략", "미국 10년 국채금리" not in text_no_fred)
# 한 줄에 두 항목이 섞이지 않는다 (· 합침 금지)
for ln in text_full.split("\n"):
    if "달러지수" in ln:
        check("B3f 달러지수 줄에 BTC변동성 미합침", "BTC변동성" not in ln)
    if "S&P500" in ln:
        check("B3g S&P500 줄에 나스닥 미합침", "나스닥" not in ln)
# 원복 — 이후 테스트는 None 경로
macro_mod.fetch_dxy = lambda conn, timeout=10.0: None
options.fetch_btc_options_context = lambda timeout=10.0: None
macro_mod.fetch_us_indices = lambda conn, timeout=10.0: None

# B3h: 매크로 이벤트 복수 표시 + 한국 시간 표기
# 고정 이벤트 목 — 자동 캘린더와 무관하게 표시 로직 검증
_mock_events = [
    {"date": "2026-09-09", "type": "PPI", "label": "PPI 생산자물가", "kst_time": "한국 21:30"},
    {"date": "2026-09-10", "type": "CPI", "label": "CPI 소비자물가", "kst_time": "한국 21:30"},
    {"date": "2026-09-14", "type": "FOMC", "label": "FOMC 금리결정", "kst_time": "한국 익일03:00"},
    {"date": "2026-09-04", "type": "NFP", "label": "비농업 고용", "kst_time": "한국 21:30"},
]
macro_mod.get_macro_events = lambda conn=None: _mock_events
AT_SEP8 = datetime(2026, 9, 8, 9, 0, tzinfo=KST).timestamp()
with db.connect(TEST_DB) as conn:
    text_ev = morning_brief.build_brief(conn, AT_SEP8, timeout=1.0)
check("B3h PPI D-1 표시", "PPI 생산자물가 D-1" in text_ev)
check("B3i CPI D-2 표시", "CPI 소비자물가 D-2" in text_ev)
check("B3j 이벤트가 1개가 아닌 복수", text_ev.count("📅") >= 2)
check("B3k 한국 시간 표기", "한국 21:30" in text_ev)
check("B3l FOMC 익일 표기", "한국 익일03:00" in text_ev)

# B4: 어제 성과·대기 레벨은 로컬 DB 원천 — 데이터가 있으면 행이 나온다
YESTERDAY = "2026-08-14"
with db.connect(TEST_DB) as conn:
    db.record_alert(conn, "BTC", "touch", [1], YESTERDAY)
    db.record_alert(conn, "ETH", "touch", [2], YESTERDAY)
    db.record_alert(conn, "XRP", "preview", [3], YESTERDAY)  # touch 만 세야 함
    text = morning_brief.build_brief(conn, AT_9, timeout=1.0)
check("B4 어제 터치 알림 수(touch 만 2건) 표기", "어제 터치 알림 2건" in text)
check("B5 대기 레벨 행 표기(0개도 데이터)", "대기 레벨 0개" in text)

# ── M1~M5: maybe_send_brief — 발송/마킹/재시도 ──────────────────────────
set_brief_meta("")
sent_log.clear()
check("M1 창 안 첫 회차는 발송 ok",
      morning_brief.maybe_send_brief(TEST_DB, now=AT_9) == "ok" and len(sent_log) == 1)
check("M1b 성공 시 오늘 날짜 마킹", get_brief_meta() == TODAY)

check("M2 같은 날 두 번째 회차는 skipped(재발송 없음)",
      morning_brief.maybe_send_brief(TEST_DB, now=AT_9 + 120) == "skipped"
      and len(sent_log) == 1)

check("M3 창 밖(KST 7:59)은 skipped(발송 없음)",
      morning_brief.maybe_send_brief(TEST_DB, now=AT_7 + 86400.0) == "skipped"
      and len(sent_log) == 1)

# M4: 발송 실패는 날짜를 마킹하지 않는다 → 다음 회차가 창 안에서 재시도 가능
set_brief_meta("")
_send_result["ok"] = False
check("M4 발송 실패는 failed", morning_brief.maybe_send_brief(TEST_DB, now=AT_9) == "failed")
check("M4b 실패 시 날짜 미기록(재시도 가능)", get_brief_meta() != TODAY)

_send_result["ok"] = True
check("M5 다음 회차(2분 뒤) 재시도 성공",
      morning_brief.maybe_send_brief(TEST_DB, now=AT_9 + 120) == "ok"
      and get_brief_meta() == TODAY)

# ── X1: run_cycle 편입 — 결과 dict 에 morning_brief 키가 들어간다 ─────────
from scripts import run_cycle

set_brief_meta("")
sent_log.clear()
res = run_cycle.run_cycle(now=AT_9, collect_enabled=False, report_enabled=False,
                          price_runner=lambda: {"checked": 0})
check("X1 run_cycle 결과에 morning_brief=ok", res.get("morning_brief") == "ok")
check("X1b 회차에서 브리핑 1통 발송", len(sent_log) == 1)
check("X1c 가격체크는 정상(격리 확인)", res.get("price_check") == "ok")

res = run_cycle.run_cycle(now=AT_9 + 120, collect_enabled=False, report_enabled=False,
                          price_runner=lambda: {"checked": 0})
check("X2 같은 날 재회차는 skipped", res.get("morning_brief") == "skipped")

print("\n" + ("모든 테스트 통과 ✅" if ok else "실패한 테스트 있음 ❌"))
sys.exit(0 if ok else 1)
