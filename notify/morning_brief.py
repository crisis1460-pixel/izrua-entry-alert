"""
모닝 브리핑 — 하루 1회, KST 아침 시간창에 시장환경 요약 1통 (2026-08-15).

주간 리포트(run_cycle.report_due)와 같은 meta 주기 판정 패턴 — 외부 크론 없이
2분 회차가 "오늘 아직 안 보냈고 지금이 발송 창"이면 1통 보낸다. 엔트리 알림
양식(render_alert)과 무관한 별도 메시지 종류이며, 데이터는 전부 기존 캐시
페처(market_sentiment 1h / options 5min / DXY 1h / 업비트 배치 ticker)를
재사용한다 — 신규 API·유료 API 없음.

발송 원칙(render_alert 와 동일): 각 데이터가 None 이면 그 행만 조용히 생략,
브리핑 자체는 항상 발송된다. 실패는 삼키고 로그만(회차를 죽이지 않는다).

meta 기록 순서: **발송 성공 후에만** 날짜를 기록한다 — 발송 실패 시 날짜가
남지 않아 다음 회차(2분 뒤)가 창 안에서 자연 재시도한다. 주간 리포트의
meta 선기록(중복 방지 우선)과 반대인 이유: 브리핑은 하루 창이 2시간뿐이라
유실되면 그날 통째로 못 보고, 만에 하나 중복돼도 요약 1통이라 무해하다.
"""

import logging
import time
from datetime import datetime

from config import settings
from notify import telegram
from storage import db
from utils.time_kst import KST, day_kst

logger = logging.getLogger("alert.morning_brief")

META_LAST_BRIEF_DATE = "last_morning_brief_date"

# 구분선·F&G 한국어 라벨은 알림과 동일 표기 유지(같은 채팅방에 섞여 보인다).
_SEP = telegram._SEP
_FNG_KR = telegram._FNG_KR

# 매크로 이벤트 예고 범위(일). get_nearby_macro_event 는 24h 창이라 브리핑용
# 7일 예고는 MACRO_EVENTS 를 직접 읽어 계산한다.
_MACRO_LOOKAHEAD_DAYS = 7


def brief_due(conn, now: float) -> tuple:
    """발송 판정. 반환 (실행여부, 사유).

    하루 1회 게이트는 타임스탬프 간격이 아니라 KST 날짜 문자열 비교다 —
    "오늘 이미 보냈는가"가 정확히 하루 1회의 의미이고, 실패 시 날짜가 안
    남으므로 같은 키가 재시도 게이트도 겸한다(별도 fail 키 불필요)."""
    if not settings.get("morning_brief_enabled"):
        return False, "비활성(morning_brief_enabled=False)"
    today = day_kst(now)
    last = db.get_meta(conn, META_LAST_BRIEF_DATE)
    if last == today:
        return False, f"오늘({today}) 이미 발송"
    hour_from = settings.get("morning_brief_kst_hour_from")
    hour_to = settings.get("morning_brief_kst_hour_to")
    kst_hour = datetime.fromtimestamp(now, KST).hour
    if not (hour_from <= kst_hour < hour_to):
        return False, f"발송 시간대 대기(KST {kst_hour}시, 창 {hour_from}~{hour_to}시)"
    return True, f"발송 창 도래({today} KST {kst_hour}시)"


# ── 데이터 수집 (전 항목 실패 허용 — None 이면 해당 행 생략) ────────────


def _btc_block(timeout: float) -> tuple:
    """(btc_krw, kimchi_pct). 업비트 배치 ticker 1콜 + 바이낸스 BTC 시세.
    ticker 에는 현재가만 쓴다(fetch_prices 반환이 {market: trade_price})."""
    from monitor import binance, upbit

    btc_krw = usdt_krw = None
    try:
        prices = upbit.fetch_prices(["KRW-BTC", "KRW-USDT"], timeout)
        btc_krw = prices.get("KRW-BTC")
        usdt_krw = prices.get("KRW-USDT")
    except Exception as e:  # noqa: BLE001 - 행 생략으로 강등
        logger.warning("[brief] 업비트 시세 실패: %s", e)

    kimchi = None
    try:
        btc_usd = binance.fetch_usdt_price("BTC", timeout)
        # 김프 산식은 price_check.run_once 와 동일: 실효환율 vs USDT/KRW.
        if btc_krw and usdt_krw and btc_usd and btc_usd > 0:
            kimchi = (btc_krw / btc_usd - usdt_krw) / usdt_krw * 100
    except Exception as e:  # noqa: BLE001 - 행 생략으로 강등
        logger.warning("[brief] 김프 계산 실패: %s", e)
    return btc_krw, kimchi


def _macro_event_line(now: float):
    """향후 7일 내 FOMC/CPI 예고 한 줄("📅 CPI 발표 D-2") 또는 None."""
    from monitor import macro

    today = datetime.fromtimestamp(now, KST).date()
    best = None
    for ev in macro.MACRO_EVENTS:
        try:
            ev_date = datetime.strptime(ev["date"], "%Y-%m-%d").date()
        except (ValueError, TypeError):
            continue
        d = (ev_date - today).days
        if 0 <= d <= _MACRO_LOOKAHEAD_DAYS and (best is None or d < best[0]):
            best = (d, ev)
    if best is None:
        return None
    d, ev = best
    return f"📅 {ev['label']} {'D-DAY' if d == 0 else f'D-{d}'}"


# ── 렌더링 ──────────────────────────────────────────────────────────


def build_brief(conn, now: float, timeout: float) -> str:
    """텔레그램 HTML 브리핑 조립. 어떤 데이터가 죽어도 문자열은 항상 나온다.
    한 화면 상한(~15행) — 행 추가 시 기존 행 삭제를 먼저 검토할 것."""
    from monitor import market_sentiment, options
    from monitor import macro as macro_mod

    lines = [f"🌅 <b>모닝 브리핑</b> · {day_kst(now)}", _SEP]

    # BTC 현재가 + 김프 (표기·이모지 분기는 render_alert 와 동일)
    btc_krw, kimchi = _btc_block(timeout)
    if btc_krw:
        lines.append(f"₿ BTC {btc_krw:,.0f}원")
    if kimchi is not None:
        if abs(kimchi) < 0.01:
            lines.append(f"⚖️ 김프 거의 0% ({kimchi:+.3f}%)")
        elif kimchi > 0:
            lines.append(f"🌶️ 김프 {kimchi:+.2f}%")
        else:
            lines.append(f"❄️ 김프 {kimchi:+.2f}%")

    # 시장 심리 (1h 캐시) — 어휘는 2026-08-14 헤더 개편 표기를 따른다
    sent = None
    try:
        sent = market_sentiment.get_sentiment(conn)
    except Exception as e:  # noqa: BLE001 - 행 생략으로 강등
        logger.warning("[brief] 시장심리 실패: %s", e)
    if sent:
        fng = sent.get("fear_greed")
        if fng is not None:
            label = _FNG_KR.get(sent.get("fear_greed_label", ""),
                                sent.get("fear_greed_label", ""))
            lines.append(f"😨 시장심리 {fng} ({label})")
        if sent.get("btc_dominance") is not None:
            lines.append(f"🌍 비트 점유율 {sent['btc_dominance']}%")
        if sent.get("usdt_dominance") is not None:
            lines.append(f"🪙 USDT 점유율 {sent['usdt_dominance']}%")

    # 달러지수 (1h 캐시)
    try:
        dxy = macro_mod.fetch_dxy(conn, timeout)
        if dxy is not None:
            lines.append(f"💵 달러지수 {dxy:.2f}")
    except Exception as e:  # noqa: BLE001 - 행 생략으로 강등
        logger.warning("[brief] DXY 실패: %s", e)

    # BTC 변동성 (5min 캐시)
    try:
        opt = options.fetch_btc_options_context(timeout)
        dvol = opt.get("dvol") if opt else None
        if dvol is not None:
            lines.append(f"📊 BTC변동성 {dvol:.0f}")
    except Exception as e:  # noqa: BLE001 - 행 생략으로 강등
        logger.warning("[brief] 옵션 컨텍스트 실패: %s", e)

    # 미국 증시 전일 등락 (1h 캐시, Yahoo Finance 무료)
    try:
        us = macro_mod.fetch_us_indices(conn, timeout)
        if us:
            sp = us.get("sp500")
            nq = us.get("nasdaq")
            if sp is not None:
                lines.append(f"🇺🇸 S&P500 {sp:+.2f}%")
            if nq is not None:
                lines.append(f"🇺🇸 나스닥 {nq:+.2f}%")
    except Exception as e:  # noqa: BLE001 - 행 생략으로 강등
        logger.warning("[brief] 미국 증시 실패: %s", e)

    # 매크로 이벤트 7일 예고
    try:
        ev_line = _macro_event_line(now)
        if ev_line:
            lines.append(ev_line)
    except Exception as e:  # noqa: BLE001 - 행 생략으로 강등
        logger.warning("[brief] 매크로 이벤트 실패: %s", e)

    # 어제 성과 + 대기 레벨 (로컬 DB 조회 — 실패 시 행 생략)
    tail = []
    try:
        yesterday = day_kst(now - 86400.0)
        n_touch = db.count_all_alerts_today(conn, yesterday, kind="touch")
        tail.append(f"🎯 어제 터치 알림 {n_touch}건")
    except Exception as e:  # noqa: BLE001 - 행 생략으로 강등
        logger.warning("[brief] 어제 성과 조회 실패: %s", e)
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n, COUNT(DISTINCT coin_symbol) AS c FROM levels "
            "WHERE status IN ('watching','previewed')").fetchone()
        if row is not None:
            tail.append(f"⏳ 대기 레벨 {row['n']}개 ({row['c']}개 코인)")
    except Exception as e:  # noqa: BLE001 - 행 생략으로 강등
        logger.warning("[brief] 대기 레벨 조회 실패: %s", e)
    if tail:
        lines.append(_SEP)
        lines.extend(tail)

    return "\n".join(lines)


# ── 회차 훅 (run_cycle 의 maybe_* 패턴) ─────────────────────────────


def maybe_send_brief(db_path: str, now: float = None) -> str:
    """조건이 맞으면 모닝 브리핑 1통 발송. 반환 "skipped" | "ok" | "failed".
    어떤 실패도 예외를 밖으로 던지지 않는다(가격체크 보호 — maybe_collect 동일).

    날짜 마킹은 **발송 성공 후에만** 한다(모듈 docstring 참고) — 실패 시 다음
    회차가 창 안에서 재시도하고, 창(hour_to)을 넘기면 그날은 자연 생략된다."""
    now = time.time() if now is None else now

    try:
        with db.connect(db_path) as conn:
            due, reason = brief_due(conn, now)
    except BaseException as e:  # noqa: BLE001 - 브리핑 실패가 회차를 죽이면 안 된다
        if isinstance(e, KeyboardInterrupt):
            raise
        logger.error("모닝 브리핑 판정 실패: %s: %s", type(e).__name__, e, exc_info=True)
        print(f"::warning::모닝 브리핑 판정 실패 - {type(e).__name__}")
        return "failed"

    if not due:
        logger.debug("모닝 브리핑 생략: %s", reason)
        return "skipped"

    logger.info("모닝 브리핑 발송: %s", reason)
    try:
        with db.connect(db_path) as conn:
            text = build_brief(conn, now, settings.get("http_timeout_sec"))
    except BaseException as e:  # noqa: BLE001 - 브리핑 실패가 회차를 죽이면 안 된다
        if isinstance(e, KeyboardInterrupt):
            raise
        logger.error("모닝 브리핑 조립 실패: %s: %s", type(e).__name__, e, exc_info=True)
        print(f"::warning::모닝 브리핑 조립 실패 - {type(e).__name__}")
        return "failed"

    try:
        sent = telegram.send(text)
    except BaseException as e:  # noqa: BLE001 - 발송 실패가 회차를 죽이면 안 된다
        if isinstance(e, KeyboardInterrupt):
            raise
        logger.error("모닝 브리핑 발송 예외: %s: %s", type(e).__name__, e, exc_info=True)
        sent = False
    if not sent:
        # 날짜 미기록 — 다음 회차(2분 뒤)가 발송 창 안에서 자동 재시도한다.
        print("::warning::모닝 브리핑 발송 실패 - 다음 회차 재시도(창 내)")
        return "failed"

    try:
        with db.connect(db_path) as conn:
            db.set_meta(conn, META_LAST_BRIEF_DATE, day_kst(now))
    except BaseException as e:  # noqa: BLE001 - meta 기록 실패로 회차를 죽이면 안 된다
        if isinstance(e, KeyboardInterrupt):
            raise
        # 발송 자체는 성공 — 날짜만 못 남겼으니 다음 회차가 창 내 중복 발송할 수
        # 있다(요약 1통이라 무해, 유실보다 낫다). ok 로 취급하되 경고는 남긴다.
        logger.error("모닝 브리핑 meta 기록 실패(발송은 완료): %s: %s",
                     type(e).__name__, e, exc_info=True)
        print(f"::warning::모닝 브리핑 meta 기록 실패 - {type(e).__name__}")

    return "ok"
