"""
매크로 경제 지표 — DXY(달러 인덱스), 경제일정 자동 캘린더, BTC 레짐.

DXY: 달러 강세 시 코인 약세 경향 (상관관계 −0.72~−0.90).
     Yahoo Finance 비공식 API — 무인증, 무료. 1시간 DB 캐시.
경제일정: FOMC는 the-calendar.net JSON 자동 수집(무인증, 무료).
          NFP·ISM 등은 규칙 기반 자동 생성. 7일 DB 캐시 + 정적 폴백.
          한국 발표시각 자동 계산(서머/윈터타임 반영).

전 항목 실패 허용 — None 반환 시 호출부가 해당 데이터를 무시한다.
"""

import calendar as _cal
import json
import logging
import time
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import requests

from storage import db
from utils.time_kst import day_kst

logger = logging.getLogger("alert.macro")

_DXY_CACHE_KEY = "macro_dxy"
_DXY_CACHE_TTL_SEC = 3600.0  # 1시간 캐시 — DXY는 느리게 변함


# ── DXY (달러 인덱스) ────────────────────────────────────────────────────

def fetch_dxy(conn, timeout: float = 10.0) -> Optional[float]:
    """DXY(달러 인덱스) 현재값. 1시간 DB 캐시.
    Yahoo Finance 비공식 API — 무인증, 무료."""
    try:
        raw = db.get_meta(conn, _DXY_CACHE_KEY)
        if raw:
            payload = json.loads(raw)
            if time.time() - payload.get("at", 0) <= _DXY_CACHE_TTL_SEC:
                return payload.get("value")
    except Exception:  # noqa: BLE001
        pass

    value = _fetch_dxy_fresh(timeout)
    if value is not None:
        try:
            db.set_meta(conn, _DXY_CACHE_KEY,
                        json.dumps({"at": time.time(), "value": value}))
        except Exception:  # noqa: BLE001
            pass
    return value


def _fetch_dxy_fresh(timeout: float) -> Optional[float]:
    """Yahoo Finance에서 DXY 현재값 조회."""
    try:
        r = requests.get(
            "https://query1.finance.yahoo.com/v8/finance/chart/DX-Y.NYB",
            params={"interval": "1d", "range": "5d"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=timeout,
        )
        if r.status_code != 200:
            logger.warning("[macro] DXY Yahoo HTTP %s", r.status_code)
            return None
        data = r.json()
        meta = data.get("chart", {}).get("result", [{}])[0].get("meta", {})
        price = meta.get("regularMarketPrice")
        if price:
            return round(float(price), 2)
        return None
    except Exception as e:  # noqa: BLE001
        logger.warning("[macro] DXY 조회 실패: %s", e)
        return None

# ── 미국 증시 (S&P 500 / 나스닥) ─────────────────────────────────────────
_US_CACHE_KEY = "macro_us_indices"
_US_CACHE_TTL_SEC = 3600.0  # 1시간 캐시


def fetch_us_indices(conn, timeout: float = 10.0) -> Optional[dict]:
    """미국 증시 전일 등락률. Yahoo Finance 무료 — DXY와 동일 패턴.
    반환 {"sp500": float(%), "nasdaq": float(%)} 또는 None.
    모닝 브리핑 전용 — 알림·필터·등급 어디에도 관여하지 않는다."""
    try:
        raw = db.get_meta(conn, _US_CACHE_KEY)
        if raw:
            payload = json.loads(raw)
            if time.time() - payload.get("at", 0) <= _US_CACHE_TTL_SEC:
                return payload.get("value")
    except Exception:  # noqa: BLE001
        pass

    value = _fetch_us_indices_fresh(timeout)
    if value is not None:
        try:
            db.set_meta(conn, _US_CACHE_KEY,
                        json.dumps({"at": time.time(), "value": value}))
        except Exception:  # noqa: BLE001
            pass
    return value


def _fetch_us_indices_fresh(timeout: float) -> Optional[dict]:
    """Yahoo Finance에서 S&P 500·나스닥 전일 등락률 조회."""
    symbols = {"sp500": "^GSPC", "nasdaq": "^IXIC"}
    result = {}
    for key, ticker in symbols.items():
        try:
            r = requests.get(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}",
                params={"interval": "1d", "range": "5d"},
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=timeout,
            )
            if r.status_code != 200:
                continue
            meta = r.json().get("chart", {}).get("result", [{}])[0].get("meta", {})
            price = meta.get("regularMarketPrice")
            prev = meta.get("chartPreviousClose") or meta.get("previousClose")
            if price and prev and prev > 0:
                result[key] = round((float(price) / float(prev) - 1) * 100, 2)
        except Exception:  # noqa: BLE001
            continue
    return result if result else None


# ── BTC 레짐 (200일선 ± 3일 히스테리시스) ────────────────────────────────
# (2026-08-16 Tier2 스프린트 — research_2026-08-15_sharpening_synthesis.md #8)
# **기록 전용** — 터치 스냅샷 도장(touch_btc_regime)에만 쓰이고 알림·필터·등급·
# 판정 어디에도 관여하지 않는다. 레짐별 게이팅은 표본 n≈100 도달 후 별도 결정.
_BTC_REGIME_KEY = "btc_regime_state"
_BTC_REGIME_TTL_SEC = 3600.0   # 1시간 캐시 — 시간당 업비트 BTC 일봉 1콜이 상한
_REGIME_HYST_DAYS = 3          # 반대 조건이 3 KST일 연속이어야 상태 전환


def _btc_ma200_raw(timeout: float) -> Optional[str]:
    """원시 조건 1회 판정 — KRW-BTC 일봉 200개 1콜, 마지막 종가 vs SMA200.
    "above"|"below", 실패/표본부족 시 None."""
    try:
        from monitor import upbit  # 지연 로드 — macro 는 다른 잡도 임포트한다
        closes = upbit._fetch_closes("KRW-BTC", "days", 200, timeout)
        if not closes or len(closes) < 200:
            return None
        sma = sum(closes[-200:]) / 200
        return "above" if closes[-1] >= sma else "below"
    except Exception as e:  # noqa: BLE001
        logger.warning("[macro] BTC 레짐 원시 조건 조회 실패: %s", e)
        return None


def get_btc_regime(conn, timeout: float = 10.0,
                   now: Optional[float] = None) -> Optional[str]:
    """BTC 레짐 도장 — "above"|"below"(BTC vs 200일선), 관측일 3일 히스테리시스.

    히스테리시스 (2026-08-16 리뷰 Fix6 — **관측된 KST일** 기준으로 수정):
    원시 조건이 저장 상태와 어긋나면 즉시 뒤집지 않고 후보(cand)로 두고, 같은
    반대 조건이 **서로 다른 KST일에 3회 관측**돼야 상태를 전환한다 — 200일선
    걸침 구간의 일중 왕복이 레짐 라벨을 매일 뒤집는 노이즈 제거(Golden Cross
    류 지표의 확인 대기 관례). 종전 규칙(달력 경과일 cand_since 기준)은 중간
    날에 관측이 하나도 없어도(봇 다운타임·조회 실패 연속) 달력만 3일 지나면
    뒤집었다 — "3일 연속 확인"이 아니라 "달력 3일 전 1회 확인"으로 퇴화하는
    버그. 이제 같은 날의 반복 관측은 1회로 세고(cand_last_day 게이트), 관측이
    없던 날은 카운트에 기여하지 않는다. 조건이 상태와 재일치하면 후보 리셋
    (연속성 요건). 최초 호출은 원시 조건으로 즉시 초기화(히스테리시스 없음 —
    비교할 저장 상태가 없다).

    상태는 meta JSON 하나에 영속: {"state","cand","cand_days","cand_last_day",
    "at"}. 구형식({"cand_since"})은 후보 없음으로 취급해 재구축한다 — 배포 수
    시간 뒤의 형식 교체라 후보 카운트 손실은 무해(보수 방향: 전환이 늦어질 뿐).
    "at" 이 1시간 캐시를 겸해 시간당 업비트 일봉 1콜이 상한(사용자 승인 예산).
    전 경로 실패 허용 — 원시 조건 조회 실패 시 저장 상태(스테일)를 그대로
    반환하고 "at" 은 갱신하지 않아 다음 회차가 재시도한다(실패한 날은 관측일로
    세지 않는다). now 인자는 테스트 주입용(기본 현재 시각)."""
    now = now if now is not None else time.time()
    st = {}
    try:
        raw_meta = db.get_meta(conn, _BTC_REGIME_KEY)
        if raw_meta:
            st = json.loads(raw_meta) or {}
    except Exception:  # noqa: BLE001 — 오염 메타는 미초기화 취급
        st = {}
    state = st.get("state") if st.get("state") in ("above", "below") else None
    if state and now - (st.get("at") or 0) <= _BTC_REGIME_TTL_SEC:
        return state  # 1h 캐시 히트 — API 콜 0

    raw = _btc_ma200_raw(timeout)
    if raw is None:
        return state  # fail-safe: 스테일 상태 반환, 메타 미갱신(다음 회차 재시도)

    today = day_kst(now)
    _reset = {"cand": None, "cand_days": 0, "cand_last_day": None}
    if state is None:
        # 최초 호출(또는 오염 복구) — 원시 조건으로 즉시 초기화
        new = {"state": raw, "at": now, **_reset}
    elif raw == state:
        new = {"state": state, "at": now, **_reset}  # 재일치 — 후보 리셋
    else:
        cand = st.get("cand")
        try:
            cand_days = int(st.get("cand_days") or 0)
        except (ValueError, TypeError):
            cand_days = 0
        cand_last_day = st.get("cand_last_day")
        # 구형식(cand_since 만 있음) 또는 오염 → cand_days=0 → 아래에서 재시작
        if cand != raw or cand_days < 1 or not cand_last_day:
            cand_days, cand_last_day = 1, today  # 새 반대 조건 — 관측일 1일째
        elif today != cand_last_day:
            cand_days, cand_last_day = cand_days + 1, today  # 새 관측일 +1
        # else: 같은 날 반복 관측 — 1회로 유지(카운트 불변)
        if cand_days >= _REGIME_HYST_DAYS:
            new = {"state": raw, "at": now, **_reset}  # 관측일 3일 충족 — 전환
        else:
            new = {"state": state, "cand": raw, "cand_days": cand_days,
                   "cand_last_day": cand_last_day, "at": now}
    try:
        db.set_meta(conn, _BTC_REGIME_KEY, json.dumps(new))
    except Exception:  # noqa: BLE001 — 메타 기록 실패해도 이번 회차 값은 반환
        pass
    return new["state"]


# ── 매크로 이벤트 캘린더 (자동 생성 + 정적 폴백) ────────────────────────
# FOMC: the-calendar.net JSON 자동 수집(무인증, 무료). CC BY 4.0.
# NFP·ISM: 규칙 기반 자동 계산(첫째 금요일·첫 영업일 — 정확).
# CPI·PPI·PCE·GDP·소매판매: 규칙 기반 근사(±1~2일 오차 가능).
# 전 소스 실패 시 정적 리스트로 폴백. 7일 DB 캐시.
# 한국 발표시각은 서머타임(EDT→KST +13h)/윈터타임(EST→KST +14h) 자동 반영.

_RELEASE_TIMES_ET = {
    "FOMC": (14, 0),       # 2:00 PM ET
    "FOMC_MIN": (14, 0),
    "CPI": (8, 30),        # 8:30 AM ET
    "PPI": (8, 30),
    "NFP": (8, 30),
    "PCE": (8, 30),
    "GDP": (8, 30),
    "RETAIL": (8, 30),
    "ISM": (10, 0),        # 10:00 AM ET
}


def _is_us_dst(d) -> bool:
    """미국 동부 DST 여부 (3월 둘째 일요일 ~ 11월 첫째 일요일)."""
    y = d.year
    mar1 = date(y, 3, 1)
    first_sun_mar = mar1 + timedelta(days=(6 - mar1.weekday()) % 7)
    dst_start = first_sun_mar + timedelta(days=7)
    nov1 = date(y, 11, 1)
    dst_end = nov1 + timedelta(days=(6 - nov1.weekday()) % 7)
    return dst_start <= d < dst_end


def _kst_release_label(ev_type: str, ev_date) -> str:
    """이벤트 발표시각→한국시간 문자열. 서머/윈터타임 자동 반영."""
    h_et, m_et = _RELEASE_TIMES_ET.get(ev_type, (8, 30))
    offset = 13 if _is_us_dst(ev_date) else 14
    h_kst = h_et + offset
    next_day = h_kst >= 24
    h_kst %= 24
    prefix = "익일" if next_day else ""
    return f"한국 {prefix}{h_kst:02d}:{m_et:02d}"


def _first_weekday_of(year, month, weekday):
    """월의 첫 번째 특정 요일 (Monday=0 … Sunday=6)."""
    d = date(year, month, 1)
    return d + timedelta(days=(weekday - d.weekday()) % 7)


def _first_biz_day(year, month):
    """월의 첫 영업일."""
    d = date(year, month, 1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def _ev(d, ev_type, label):
    return {"date": d.isoformat(), "type": ev_type, "label": label,
            "kst_time": _kst_release_label(ev_type, d)}


def _generate_rule_events(start, months=8):
    """규칙 기반 경제일정 자동 생성 (FOMC 제외)."""
    events = []
    for off in range(months):
        m = start.month + off
        y = start.year + (m - 1) // 12
        m = (m - 1) % 12 + 1
        events.append(_ev(_first_weekday_of(y, m, 4), "NFP", "비농업 고용"))
        events.append(_ev(_first_biz_day(y, m), "ISM", "ISM 제조업"))
        second_tue = _first_weekday_of(y, m, 1) + timedelta(days=7)
        cpi_d = second_tue + timedelta(days=1)
        events.append(_ev(cpi_d, "CPI", "CPI 소비자물가"))
        events.append(_ev(cpi_d - timedelta(days=1), "PPI", "PPI 생산자물가"))
        last = date(y, m, _cal.monthrange(y, m)[1])
        while last.weekday() != 4:
            last -= timedelta(days=1)
        events.append(_ev(last, "PCE", "PCE 물가"))
        retail = date(y, m, 15)
        while retail.weekday() >= 5:
            retail += timedelta(days=1)
        events.append(_ev(retail, "RETAIL", "소매판매"))
        if m in (1, 4, 7, 10):
            gdp = date(y, m, _cal.monthrange(y, m)[1])
            while gdp.weekday() >= 5:
                gdp -= timedelta(days=1)
            events.append(_ev(gdp, "GDP", "GDP 성장률"))
    return events


def _fetch_fomc_calendar(timeout=10.0):
    """the-calendar.net에서 FOMC 회의일정 자동 수집 — 현재+내년."""
    today = date.today()
    events, seen = [], set()
    for year in (today.year, today.year + 1):
        try:
            r = requests.get(
                f"https://the-calendar.net/api/finance/fomc/{year}.json",
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=timeout,
            )
            if r.status_code != 200:
                continue
            data = r.json()
            meetings = data.get("meetings") if isinstance(data, dict) else data
            if not isinstance(meetings, list):
                continue
            raw = []
            for entry in meetings:
                try:
                    raw.append(date.fromisoformat(str(entry.get("date", ""))[:10]))
                except (ValueError, TypeError):
                    continue
            raw.sort()
            i = 0
            while i < len(raw):
                if i + 1 < len(raw) and (raw[i + 1] - raw[i]).days == 1:
                    decision = raw[i + 1]
                    i += 2
                else:
                    decision = raw[i]
                    i += 1
                if decision in seen:
                    continue
                seen.add(decision)
                events.append(_ev(decision, "FOMC", "FOMC 금리결정"))
                events.append(_ev(decision + timedelta(days=21),
                                  "FOMC_MIN", "FOMC 의사록"))
        except Exception as e:  # noqa: BLE001
            logger.warning("[macro] FOMC 캘린더 수집 실패(year=%d): %s", year, e)
    return events or None


# ── 캘린더 캐시 ──────────────────────────────────────────────────────────
_CAL_CACHE_KEY = "macro_calendar_v2"
_CAL_CACHE_TTL = 604800.0   # 7일
_mem_cal = None              # type: list | None
_mem_cal_ts = 0.0


def refresh_macro_calendar(conn, timeout=10.0):
    """경제일정 자동 갱신 — FOMC 수집 + 규칙 생성 + DB 캐시."""
    global _mem_cal, _mem_cal_ts
    today = date.today()
    events = _generate_rule_events(today, months=8)
    fomc = _fetch_fomc_calendar(timeout)
    if fomc:
        events = [e for e in events if e["type"] not in ("FOMC", "FOMC_MIN")]
        events.extend(fomc)
    events.sort(key=lambda e: e.get("date", ""))
    now = time.time()
    try:
        db.set_meta(conn, _CAL_CACHE_KEY,
                    json.dumps({"at": now, "events": events}))
    except Exception:  # noqa: BLE001
        pass
    _mem_cal, _mem_cal_ts = events, now
    return events


def get_macro_events(conn=None):
    """캐시된 경제일정 반환. 7일 캐시, 실패 시 정적 폴백."""
    global _mem_cal, _mem_cal_ts
    now = time.time()
    if _mem_cal and now - _mem_cal_ts < 86400:
        return _mem_cal
    if conn:
        try:
            raw = db.get_meta(conn, _CAL_CACHE_KEY)
            if raw:
                payload = json.loads(raw)
                if now - payload.get("at", 0) <= _CAL_CACHE_TTL:
                    _mem_cal, _mem_cal_ts = payload["events"], now
                    return _mem_cal
        except Exception:  # noqa: BLE001
            pass
        try:
            return refresh_macro_calendar(conn)
        except Exception as e:  # noqa: BLE001
            logger.warning("[macro] 캘린더 갱신 실패(폴백): %s", e)
    for ev in _STATIC_EVENTS:
        if "kst_time" not in ev:
            try:
                ev["kst_time"] = _kst_release_label(
                    ev["type"], date.fromisoformat(ev["date"]))
            except (ValueError, TypeError):
                pass
    return _STATIC_EVENTS


# 정적 폴백 리스트 (자동 캘린더 전 소스 실패 시 사용)
_STATIC_EVENTS = [
    {"date": "2026-09-16", "type": "FOMC", "label": "FOMC 금리결정"},
    {"date": "2026-11-04", "type": "FOMC", "label": "FOMC 금리결정"},
    {"date": "2026-12-16", "type": "FOMC", "label": "FOMC 금리결정"},
    {"date": "2027-01-27", "type": "FOMC", "label": "FOMC 금리결정"},
    {"date": "2027-03-17", "type": "FOMC", "label": "FOMC 금리결정"},
    {"date": "2026-09-10", "type": "CPI", "label": "CPI 소비자물가"},
    {"date": "2026-10-14", "type": "CPI", "label": "CPI 소비자물가"},
    {"date": "2026-11-12", "type": "CPI", "label": "CPI 소비자물가"},
    {"date": "2026-12-10", "type": "CPI", "label": "CPI 소비자물가"},
    {"date": "2026-09-09", "type": "PPI", "label": "PPI 생산자물가"},
    {"date": "2026-09-04", "type": "NFP", "label": "비농업 고용"},
    {"date": "2026-10-02", "type": "NFP", "label": "비농업 고용"},
    {"date": "2026-11-06", "type": "NFP", "label": "비농업 고용"},
    {"date": "2026-12-04", "type": "NFP", "label": "비농업 고용"},
]

# 모듈 레벨 호환 참조 — get_macro_events(conn) 사용 권장
MACRO_EVENTS = _STATIC_EVENTS


def get_nearby_macro_event(hours_before: int = 24,
                           hours_after: int = 2,
                           conn=None) -> Optional[dict]:
    """현재 시각 기준 +-N시간 내 매크로 이벤트(가장 가까운 1건).
    이벤트 시각은 타입별 미국 동부 발표시각 + DST 반영."""
    now_dt = datetime.now(timezone.utc)
    events = get_macro_events(conn)
    closest = None
    min_dist = float("inf")
    for ev in events:
        try:
            h, m = _RELEASE_TIMES_ET.get(ev.get("type"), (8, 30))
            ev_d = date.fromisoformat(ev["date"])
            utc_off = -4 if _is_us_dst(ev_d) else -5
            ev_dt = datetime(ev_d.year, ev_d.month, ev_d.day, h, m,
                             tzinfo=timezone(timedelta(hours=utc_off)))
            diff_h = (ev_dt - now_dt).total_seconds() / 3600
            if -hours_after <= diff_h <= hours_before:
                if abs(diff_h) < min_dist:
                    min_dist = abs(diff_h)
                    closest = {**ev, "hours_until": round(diff_h, 1)}
        except (ValueError, TypeError):  # noqa: BLE001
            continue
    return closest
