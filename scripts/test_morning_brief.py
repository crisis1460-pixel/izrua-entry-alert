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
# 뉴스 압축 항목(10-09, 기본 ON) — 이 파일의 분할·소비 픽스처는 종전 긴 형식 줄 수 기준이라
# OFF 로 고정한다. 압축 형식은 끝의 NEWS-CMP-MB* 블록에서 켜서 검증한다.
settings.SETTINGS["news_compact_enabled"] = False
# 시총 순위 게이트(10-09)도 끈다 — 실물 유니버스 캐시가 매일 바뀌어 결과가 실행일에 따라 흔들린다.
# 게이트는 NEWS-CMP-MB* 블록에서 고정 픽스처 유니버스로 켜서 검증한다.
settings.SETTINGS["news_max_mcap_rank"] = 0
# 순위 캐시도 없는 파일로 고정(최종 리뷰 10-09) — 실물 data/universe.json 순위에 결과가 기대지 않게.
settings.SETTINGS["universe_cache_path"] = "cache/_test_mb_no_universe.json"
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
check("B3b 달러지수 단독 줄(DXY 아님, 매수영향 라벨)",
      "달러지수 103.45 (중립)" in text_full and "DXY" not in text_full)
check("B3c BTC변동성 단독 줄(DVOL 아님)", "BTC변동성 52" in text_full and "DVOL" not in text_full)
check("B3d S&P500 단독 줄", "S&P500 +0.87%" in text_full)
check("B3e 나스닥 단독 줄", "나스닥 -0.34%" in text_full)

# FRED VIX / 10Y 국채 (2026-08-17) — 데이터 있을 때 표시, 결측이면 생략
macro_mod.fetch_vix = lambda conn, timeout=10.0: 18.4
macro_mod.fetch_ust_10y = lambda conn, timeout=10.0: 4.23
with db.connect(TEST_DB) as conn:
    text_fred = morning_brief.build_brief(conn, AT_9, timeout=1.0)
check("B3m VIX 단독 줄(매수영향 라벨)", "VIX 18.4 (매수 유리)" in text_fred)
check("B3n 미국채 10Y 단독 줄(매수영향 라벨)", "미국채 10Y 4.23% (중립)" in text_fred)
# 결측 시 행 생략
macro_mod.fetch_vix = lambda conn, timeout=10.0: None
macro_mod.fetch_ust_10y = lambda conn, timeout=10.0: None
with db.connect(TEST_DB) as conn:
    text_no_fred = morning_brief.build_brief(conn, AT_9, timeout=1.0)
check("B3o VIX None 이면 행 생략", "VIX" not in text_no_fred)
check("B3p 10Y None 이면 행 생략", "미국채 10Y" not in text_no_fred)
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
# 10-09: 32칸을 넘는 줄은 '한국'을 빼고 한 줄로("(21:30)") — 시각 자체는 항상 표기.
check("B3k 한국 시간 표기", "21:30" in text_ev and all(
    morning_brief._display_width(x) <= 32 for x in text_ev.splitlines() if x.startswith("📅")))
check("B3l FOMC 익일 표기", "익일03:00" in text_ev)

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
check("M1c 뉴스 0건인 날은 첫 통 끝에 안내 줄(09-29 대표 결정)",
      sent_log and "새 뉴스 없음" in sent_log[0])

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

# ── NEWS-SPLIT1~7: 한도 초과 시 뉴스 분할 발송 + 소비 가드 (2026-09-27) ─────
# 사용자 요청 "브리핑이 길어져도 되니 내용별 문장을 좀 길게" → 한 통 한도를 넘을 수
# 있다. 뉴스는 **자르지 않고** 두 번째 메시지로 나눈다. 09-14 사고(잘린 뉴스가 소비
# 처리돼 영구 소실)가 분할에서도 재발하지 않는지 — 메시지별 소비를 검증한다.
morning_brief._fetch_tickers = lambda markets, timeout: {}   # 가격 맥락 네트워크 차단
_SPLIT_LIMIT = 900                                              # 실한도 3900 대신 축소 재현


def _fill_news(tag, n=5, base=AT_9):
    with db.connect(TEST_DB) as conn:
        for i in range(n):
            sym = f"Q{tag}{i}"
            en = (f"#{sym} Market Analysis\n{sym} is at 1.2345 on the 4h, pulling back from the "
                  f"1.4000 highs and holding above the demand zone.\nBull case: hold above 1.2000 "
                  f"and resume the push toward 1.4000.\nBear case: lose 1.1500 and slide toward 1.0500.")
            db.queue_news_digest(conn, sym, f"ch{i}", f"{sym} 는 1.2345 부근입니다.",
                                 f"https://t.me/split/{tag}{i}", TODAY, base - 600 + i,
                                 summary_en=en, posted_at=base - 3600 + i)


def _unconsumed():
    with db.connect(TEST_DB) as conn:
        return db.count_news_digest(conn)


with db.connect(TEST_DB) as conn:
    conn.execute("UPDATE news_digest_queue SET consumed=1")
_fill_news("a")
_orig_max = morning_brief._TELEGRAM_MAX_CHARS
morning_brief._TELEGRAM_MAX_CHARS = _SPLIT_LIMIT
with db.connect(TEST_DB) as conn:
    _msgs = morning_brief.build_brief_messages(conn, AT_9, timeout=1.0)
    _qids = [r[0] for r in conn.execute(
        "SELECT id FROM news_digest_queue WHERE consumed=0").fetchall()]
_news_msgs = _msgs[1:]
check("NEWS-SPLIT1 한도 초과 → 본문 1통 + 뉴스 메시지(들)로 분할(≥2통)",
      len(_msgs) >= 3 and "주요 뉴스" not in _msgs[0][0] and "모닝 브리핑" in _msgs[0][0])
check("NEWS-SPLIT2 모든 메시지가 한도 이내(UTF-16 기준)",
      all(morning_brief._tg_len(t) <= _SPLIT_LIMIT for t, _ids in _msgs))
_all_heads = [ln for t, _ids in _news_msgs for ln in t.split("\n") if ln.startswith("   <b>")]
check("NEWS-SPLIT3 뉴스 5건 전부 실림(자르지 않음) — 항목은 메시지 경계에서 쪼개지지 않는다",
      len(_all_heads) == 5 and all(t.split("\n")[0].startswith("📰") for t, _ids in _news_msgs)
      and all(not t.split("\n")[-1].startswith("   <b>") for t, _ids in _news_msgs))
check("NEWS-SPLIT4 소비 id 는 메시지별로 정확히 나뉜다(본문 0 · 뉴스 합계 = 판정 후보 전부)",
      _msgs[0][1] == [] and sorted(i for _t, ids in _news_msgs for i in ids) == sorted(_qids)
      and all(len(ids) == sum(1 for ln in t.split("\n") if ln.startswith("   <b>"))
              for t, ids in _news_msgs))

# 뉴스 메시지 발송 실패 → 브리핑은 ok(날짜 마킹) · 뉴스는 **미소비**로 남아 다음 브리핑에
set_brief_meta("")
sent_log.clear()
_fail_news = {"on": True}


def _send_fail_news(text, urgency="high", reply_to_message_id=None):
    sent_log.append(text)
    if _fail_news["on"] and "주요 뉴스" in text:
        return None
    return 1


telegram.send = _send_fail_news
check("NEWS-SPLIT5 뉴스 분할 메시지 실패 → 브리핑 ok · 날짜 마킹 · 뉴스 5건 전부 미소비",
      morning_brief.maybe_send_brief(TEST_DB, now=AT_9) == "ok"
      and get_brief_meta() == TODAY and _unconsumed() == 5)
# 다음 날: 전부 성공 → 그제서야 소비
set_brief_meta("")
sent_log.clear()
_fail_news["on"] = False
check("NEWS-SPLIT6 재시도(다음 브리핑) 성공 시 분할 메시지 모두 발송 · 전부 소비",
      morning_brief.maybe_send_brief(TEST_DB, now=AT_9 + 86400) == "ok"
      and len(sent_log) >= 3 and _unconsumed() == 0)
# 첫 통(본문) 실패 → failed · 아무것도 소비·마킹 안 함
_fill_news("b", base=AT_9 + 2 * 86400)
set_brief_meta("")
sent_log.clear()
_send_result["ok"] = False
telegram.send = _fake_send
check("NEWS-SPLIT7 본문 발송 실패 → failed · 뉴스 미소비 · 날짜 미기록",
      morning_brief.maybe_send_brief(TEST_DB, now=AT_9 + 2 * 86400) == "failed"
      and _unconsumed() == 5 and get_brief_meta() != "2026-08-17")
_send_result["ok"] = True
morning_brief._TELEGRAM_MAX_CHARS = _orig_max
set_brief_meta("")
sent_log.clear()
# 2026-09-28 대표 요청: 한도 이내여도 뉴스는 항상 두 번째 메시지(본문 1통 + 뉴스 1통).
check("NEWS-SPLIT8 한도 이내여도 뉴스는 두 번째 메시지 · 뉴스 전부 소비",
      morning_brief.maybe_send_brief(TEST_DB, now=AT_9 + 2 * 86400) == "ok"
      and len(sent_log) == 2 and "주요 뉴스" not in sent_log[0]
      and "주요 뉴스" in sent_log[1] and _unconsumed() == 0)
_news_msg = sent_log[-1] if sent_log else ""
check("NEWS-SPLIT9 v2 항목 형태(💬 차트 의견 · 분기선 · 설명 문장)가 실린다",
      "💬 차트 의견(4시간봉)" in _news_msg and "↑1.2000" in _news_msg and "↓1.1500" in _news_msg
      and "강세 시나리오" in " ".join(l.strip() for l in _news_msg.split("\n")))
check("NEWS-SPLIT10 뉴스 항목 사이 빈 줄(코인 경계)", "\n\n" in _news_msg)

# ── NEWS-AGE1~4 (2026-09-27 대표 결정 "뉴스 48시간 이내") ────────────────
# 렌더 단계: 게시 72h 전 큐 행은 빠지고(소비는 됨), 게시 1h 전 행은 실린다.
_en_age = ("#XRP Market Analysis\nXRP is at 1.4944 on the 4H, pulling back from 1.6500 highs.\n"
           "Bull case: hold above 1.4500 and push toward 1.6500.\n"
           "Bear case: lose 1.4500 and slide toward 1.3100.")
with db.connect(TEST_DB) as conn:
    conn.execute("UPDATE news_digest_queue SET consumed=1")
    db.queue_news_digest(conn, "XRP", "old", "XRP 는 1.4944 부근입니다.", "https://t.me/age/old",
                         TODAY, AT_9 - 600, summary_en=_en_age, posted_at=AT_9 - 72 * 3600)
    db.queue_news_digest(conn, "ADA", "new", "ADA 는 0.8123 부근입니다.", "https://t.me/age/new",
                         TODAY, AT_9 - 600, summary_en=_en_age.replace("XRP", "ADA"),
                         posted_at=AT_9 - 3600)
    conn.commit()
    _age_ids = []
    _age_items = morning_brief._news_items(conn, _age_ids, timeout=1.0, now=AT_9)
_age_txt = "\n".join(str(x) for x in (_age_items or []))
check("NEWS-AGE1 게시 72h 전 큐 행은 브리핑에서 제외", "@old" not in _age_txt and "XRP" not in _age_txt)
check("NEWS-AGE2 게시 1h 전 행은 실림", "ADA" in _age_txt)
check("NEWS-AGE3 제외된 행도 소비 처리(큐 머리 막힘 방지)", len(_age_ids) == 2)
# 수집 단계: 티커 경로도 48h 초과 글은 적재 거부
from notify import news_brief as _nb_age
_old_post = {"title": "ADA Market Analysis", "description": _en_age.replace("XRP", "ADA") * 2,
             "url": "https://t.me/age/collect", "published_at": AT_9 - 50 * 3600}
check("NEWS-AGE4 수집 단계: 티커 경로 게시 50h 전 글은 skipped",
      _nb_age.maybe_send_news_brief(None, _old_post, "ADA", "age", now=AT_9) == "skipped")

# ── RV2-N7: 분할 뉴스 메시지 실패분은 다음 날 48h 가드에 조용히 소비되지 않는다 ──────
# (2026-09-27 코드 리뷰 — 09-14 유실 사고의 변형). 게시 30h 전 글 5건 → 1일차 뉴스
# 메시지 실패 → 2일차(게시 54h)에 **실려서** 발송된 뒤 소비돼야 한다.
with db.connect(TEST_DB) as conn:
    conn.execute("UPDATE news_digest_queue SET consumed=1")
    for i in range(5):
        sym = f"R7{i}"
        en = (f"#{sym} Market Analysis\n{sym} is at 1.2345 on the 4h, pulling back from the "
              f"1.4000 highs and holding above the demand zone.\nBull case: hold above 1.2000 "
              f"and resume the push toward 1.4000.\nBear case: lose 1.1500 and slide toward 1.0500.")
        db.queue_news_digest(conn, sym, f"r7ch{i}", f"{sym} 는 1.2345 부근입니다.",
                             f"https://t.me/rv2n7/{i}", TODAY, AT_9 - 30 * 3600 + i,
                             summary_en=en, posted_at=AT_9 - 30 * 3600 + i)
    conn.commit()
morning_brief._TELEGRAM_MAX_CHARS = _SPLIT_LIMIT
set_brief_meta("")
sent_log.clear()
_fail_news["on"] = True
telegram.send = _send_fail_news
_n7_day1 = morning_brief.maybe_send_brief(TEST_DB, now=AT_9)
_n7_unc1 = _unconsumed()
set_brief_meta("")
sent_log.clear()
_fail_news["on"] = False
_n7_day2 = morning_brief.maybe_send_brief(TEST_DB, now=AT_9 + 86400)
_n7_txt2 = "\n".join(sent_log)
check("RV2-N7 뉴스 메시지 실패분(게시 30h)은 다음 날(게시 54h) 가드 면제로 실리고 그 뒤 소비",
      _n7_day1 == "ok" and _n7_unc1 == 5 and _n7_day2 == "ok"
      and all(f"R7{i}" in _n7_txt2 for i in range(5)) and _unconsumed() == 0)
with db.connect(TEST_DB) as conn:
    _n7_meta = db.get_meta(conn, morning_brief.META_NEWS_RETRY_IDS)
check("RV2-N7b 재시도 성공 뒤 면제 목록은 비워진다", _n7_meta == "[]")
morning_brief._TELEGRAM_MAX_CHARS = _orig_max
telegram.send = _fake_send

# ── RV2-N8: v2 스위치 ON 이면 원문(summary_en) 없는 레거시 큐 행은 싣지 않고 소비만 ──
# 운영 큐의 XRP/17290 재적재 행이 09-28 브리핑에 "…동안 건설된 1.…" 로 잘려 실릴 뻔했다.
with db.connect(TEST_DB) as conn:
    conn.execute("UPDATE news_digest_queue SET consumed=1")
    db.queue_news_digest(conn, "XRP", "BitcoinBullets",
                         "# XRP 시장 분석\nXRP는 4시간에 1.4944이며, 1.6500 고점에서 물러나고 9월 통합 기간 "
                         "동안 건설된 1.4500 근처의 수요 구역에 착륙했습니다.",
                         "https://t.me/BitcoinBullets/17290", TODAY, AT_9 - 600)
    conn.commit()
    _n8_ids = []
    _n8 = morning_brief._news_items(conn, _n8_ids, timeout=1.0, now=AT_9)
_n8_txt = "\n".join(str(x) for x in (_n8 or []))
check("RV2-N8 v2 ON: 원문 없는 레거시 행은 실리지 않고('…' 절단 없음) 소비 id 에는 들어간다",
      settings.get("news_structured_enabled") is True and _n8 is None and "…" not in _n8_txt
      and len(_n8_ids) == 1)
set_brief_meta("")
sent_log.clear()
check("RV2-N8b 뉴스 블록이 없어도 판정한 레거시 행은 발송 성공 후 소비(큐 머리 영구 잔류 방지)",
      morning_brief.maybe_send_brief(TEST_DB, now=AT_9) == "ok" and len(sent_log) == 1
      and "…" not in sent_log[0] and "주요 뉴스" not in sent_log[0] and _unconsumed() == 0)

# ── NEWS-CMP-MB: 압축 항목 + 시총 게이트 + 분할 (2026-10-09 대표 요청) ─────────
# 30건(200위 안 26 · 250위 2 · rank 없음 2) → 압축 4~5줄 항목 15건(블록 상한) · 순위 밖 4건은 판정만
# (본문 메시지와 함께 소비) · 한도를 줄여도 항목 경계에서만 나뉜다.
import json as _json_cmp
import re as _re_cmp
import tempfile as _tmp_cmp

_cmp_dir = _tmp_cmp.mkdtemp()
_cmp_uni = os.path.join(_cmp_dir, "universe.json")
_cmp_syms = [(f"C{i:02d}", 10 + i) for i in range(26)] + [("OUT1", 250), ("OUT2", 251),
                                                          ("NONE1", None), ("NONE2", None)]
with open(_cmp_uni, "w", encoding="utf-8") as _f:
    _json_cmp.dump({"updated_at": AT_9, "universe": [{"symbol": s, "rank": r} for s, r in _cmp_syms]}, _f)
_cmp_orig = {k: settings.SETTINGS[k] for k in ("universe_cache_path", "news_max_mcap_rank",
                                                "news_compact_enabled")}
settings.SETTINGS.update(universe_cache_path=_cmp_uni, news_max_mcap_rank=200, news_compact_enabled=True)
with db.connect(TEST_DB) as conn:
    conn.execute("UPDATE news_digest_queue SET consumed=1")
    for i, (sym, _r) in enumerate(_cmp_syms):
        en = (f"#{sym} Market Analysis\n{sym} is at 1.2345 on the 4h, pulling back from the "
              f"1.4000 highs and holding above the demand zone.\nBull case: hold above 1.2000 "
              f"and resume the push toward 1.4000.\nBear case: lose 1.1500 and slide toward 1.0500.")
        db.queue_news_digest(conn, sym, f"cmpch{i % 7}", f"{sym} 는 1.2345 부근입니다.",
                             f"https://t.me/cmp/{i}", TODAY, AT_9 - 600 + i,
                             summary_en=en, posted_at=AT_9 - 3600 + i)
    conn.commit()
    _cmp_out_ids = {r[0] for r in conn.execute(
        "SELECT id FROM news_digest_queue WHERE consumed=0 AND symbol IN ('OUT1','OUT2','NONE1','NONE2')")}
_orig_max2 = morning_brief._TELEGRAM_MAX_CHARS
morning_brief._TELEGRAM_MAX_CHARS = 1200
with db.connect(TEST_DB) as conn:
    _cmp_msgs = morning_brief.build_brief_messages(conn, AT_9, timeout=1.0)
morning_brief._TELEGRAM_MAX_CHARS = _orig_max2
_cmp_news = _cmp_msgs[1:]
_cmp_items = []           # 메시지별 항목(빈 줄 경계) 목록
for _t, _ids in _cmp_news:
    _hl = _t.split("\n")
    _k = 1                # 헤더 보조 줄(3칸 들여, 10-10)까지 건너뛴다
    while _k < len(_hl) and _hl[_k].startswith("   "):
        _k += 1
    _body = _hl[_k:]
    _blk = "\n".join(_body).split("\n\n")
    _cmp_items.append([b.split("\n") for b in _blk if b])
_cmp_flat = [it for m in _cmp_items for it in m]
_cmp_txt = "\n".join(t for t, _ in _cmp_news)


def _cmp_w(ln):
    import html as _h
    return morning_brief._display_width(_h.unescape(_re_cmp.sub(r"</?b>", "", ln)))


check("NEWS-CMP-MB1 압축 ON: 항목 15건(블록 상한) · 항목 2~5줄 · 머리 0열 '<b>' · 하위 정확히 2칸 · 32칸",
      len(_cmp_flat) == 15 and all(4 <= len(it) <= 5 and it[0].startswith("<b>") for it in _cmp_flat)
      and all(ln.startswith("  ") and not ln.startswith("   ") for it in _cmp_flat for ln in it[1:])
      and all(_cmp_w(ln) <= 32 for it in _cmp_flat for ln in it)
      and all(t.split("\n")[0].startswith("📰 <b>주요 뉴스</b>") for t, _ in _cmp_news))
check("NEWS-CMP-MB1b 차트 분기 항목은 요약(목표 1.4000·1.0500) 1~2줄 보강 → 4~5줄",
      all(4 <= len(it) <= 5 and "1.4000" in "".join(it[2:-1]) for it in _cmp_flat))
check("NEWS-CMP-MB2 시총 게이트: 250위·rank 없음 4건은 안 실리고 본문 메시지 소비 id 로(판정 계약)",
      all(s not in _cmp_txt for s in ("OUT1", "OUT2", "NONE1", "NONE2"))
      and _cmp_out_ids <= set(_cmp_msgs[0][1]))
check("NEWS-CMP-MB3 한도 축소(1200) → 항목 경계에서만 분할 · 메시지마다 한도 이내 · id 수 = 항목 수",
      len(_cmp_news) >= 2 and all(morning_brief._tg_len(t) <= 1200 for t, _ in _cmp_news)
      and all(len(ids) == len(its) for (_t, ids), its in zip(_cmp_news, _cmp_items))
      and all(t.split("\n")[0].startswith("📰") for t, _ in _cmp_news))
check("NEWS-CMP-MB4 레이아웃 C 차트: 머리 '[시총 N위] 💬 채널 의견' · 키워드는 셋업만 · 요약 '채널:' 인용 · 색 칩 없음",
      all(_re_cmp.match(r"<b>C\d\d</b> \[시총 \d+위\] 💬 채널 의견$", it[0]) for it in _cmp_flat)
      and all("위↑" not in "".join(it) and it[2].startswith("  채널") for it in _cmp_flat)
      and "🟢" not in _cmp_txt and "🔴" not in _cmp_txt)
_cmp_ranks = [int(_re_cmp.search(r"시총 (\d+)위", it[0]).group(1)) for it in _cmp_flat]
check("NEWS-CMP-MB6 표시 순서 = 시총 오름차순(메시지 경계를 넘어서도) · 헤더 '(시총순'",
      _cmp_ranks == sorted(_cmp_ranks) and _cmp_news[0][0].startswith("📰 <b>주요 뉴스</b>\n   시총순"))
# 한 통 경로(build_brief → _fit_telegram): 압축 항목은 머리줄 뒤 하위 줄이 여럿 — 길이 방어가
# **항목 경계**까지 되돌려 반쪽 항목이 남지 않고, 소비 id 수 = 온전히 실린 항목 수(구조 기반 경계).
with db.connect(TEST_DB) as conn:
    _cmp_full = morning_brief.build_brief(conn, AT_9, timeout=1.0)   # 한도 안: 전부 실림
_cmp_body_len = len(_cmp_full.split("📰")[0])
morning_brief._TELEGRAM_MAX_CHARS = _cmp_body_len + 700
_cmp_ids1: list = []
with db.connect(TEST_DB) as conn:
    _cmp_one = morning_brief.build_brief(conn, AT_9, timeout=1.0, consumed_ids=_cmp_ids1)
morning_brief._TELEGRAM_MAX_CHARS = _orig_max2
_cmp_one_news = _cmp_one.split("📰", 1)[1].split("\n")[1:] if "📰" in _cmp_one else []
_cmp_heads1 = [ln for ln in _cmp_one_news if ln.startswith("<b>")]
check("NEWS-CMP-MB5 한 통 절단: 항목 경계까지만 남음(마지막 줄 = 꼬리 '24h'/출처) · 소비 id 수 = 실린 항목 수",
      0 < len(_cmp_heads1) < 15 and _cmp_one_news[-1].startswith("  ") and "@cmpch" in _cmp_one_news[-1]
      and len(_cmp_ids1) == len(_cmp_heads1))
settings.SETTINGS.update(_cmp_orig)
with db.connect(TEST_DB) as conn:
    conn.execute("UPDATE news_digest_queue SET consumed=1")
    conn.commit()

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
