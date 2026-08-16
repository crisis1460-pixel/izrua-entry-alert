"""
매크로 경제 지표 — DXY(달러 인덱스), FOMC/CPI 이벤트 캘린더.

DXY: 달러 강세 시 코인 약세 경향 (상관관계 −0.72~−0.90).
     Yahoo Finance 비공식 API — 무인증, 무료. 1시간 DB 캐시.
FOMC/CPI: 고영향 이벤트 24h 전~2h 후 경고.
          정적 JSON — API 호출 0, 수동 갱신.

전 항목 실패 허용 — None 반환 시 호출부가 해당 데이터를 무시한다.
"""

import json
import logging
import time
from datetime import datetime, timezone
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


# ── 매크로 이벤트 캘린더 ─────────────────────────────────────────────────
# 미국 주요 경제지표 — 대부분 미국 동부 08:30(한국시간 21:30 여름/22:30 겨울)
# 발표 직후 코인 시장 변동성 급등. 정적 리스트 — 수동 갱신(분기 1회).
MACRO_EVENTS = [
    # ── 2026 Q3~Q4 ──────────────────────────────────────────────────
    # FOMC 금리결정 (연 8회)
    {"date": "2026-09-16", "type": "FOMC", "label": "FOMC 금리결정"},
    {"date": "2026-11-04", "type": "FOMC", "label": "FOMC 금리결정"},
    {"date": "2026-12-16", "type": "FOMC", "label": "FOMC 금리결정"},
    # FOMC 의사록 (금리결정 ~3주 후)
    {"date": "2026-10-07", "type": "FOMC_MIN", "label": "FOMC 의사록"},
    {"date": "2026-11-25", "type": "FOMC_MIN", "label": "FOMC 의사록"},
    # CPI 소비자물가 (매월 중순)
    {"date": "2026-09-10", "type": "CPI", "label": "CPI 소비자물가"},
    {"date": "2026-10-14", "type": "CPI", "label": "CPI 소비자물가"},
    {"date": "2026-11-12", "type": "CPI", "label": "CPI 소비자물가"},
    {"date": "2026-12-10", "type": "CPI", "label": "CPI 소비자물가"},
    # PPI 생산자물가 (CPI 전일 또는 근접)
    {"date": "2026-09-09", "type": "PPI", "label": "PPI 생산자물가"},
    {"date": "2026-10-13", "type": "PPI", "label": "PPI 생산자물가"},
    {"date": "2026-11-10", "type": "PPI", "label": "PPI 생산자물가"},
    {"date": "2026-12-09", "type": "PPI", "label": "PPI 생산자물가"},
    # 비농업 고용 NFP (매월 첫째 금요일)
    {"date": "2026-09-04", "type": "NFP", "label": "비농업 고용"},
    {"date": "2026-10-02", "type": "NFP", "label": "비농업 고용"},
    {"date": "2026-11-06", "type": "NFP", "label": "비농업 고용"},
    {"date": "2026-12-04", "type": "NFP", "label": "비농업 고용"},
    # PCE 개인소비지출물가 (연준 선호 물가지표, 월말)
    {"date": "2026-09-25", "type": "PCE", "label": "PCE 물가"},
    {"date": "2026-10-30", "type": "PCE", "label": "PCE 물가"},
    {"date": "2026-11-25", "type": "PCE", "label": "PCE 물가"},
    {"date": "2026-12-23", "type": "PCE", "label": "PCE 물가"},
    # GDP 경제성장률 (분기별, 속보/수정/확정)
    {"date": "2026-09-30", "type": "GDP", "label": "GDP 성장률"},
    {"date": "2026-10-29", "type": "GDP", "label": "GDP 성장률"},
    {"date": "2026-12-22", "type": "GDP", "label": "GDP 성장률"},
    # 소매판매 (매월 중순)
    {"date": "2026-09-16", "type": "RETAIL", "label": "소매판매"},
    {"date": "2026-10-16", "type": "RETAIL", "label": "소매판매"},
    {"date": "2026-11-17", "type": "RETAIL", "label": "소매판매"},
    {"date": "2026-12-16", "type": "RETAIL", "label": "소매판매"},
    # ISM 제조업 (매월 첫 영업일)
    {"date": "2026-09-01", "type": "ISM", "label": "ISM 제조업"},
    {"date": "2026-10-01", "type": "ISM", "label": "ISM 제조업"},
    {"date": "2026-11-02", "type": "ISM", "label": "ISM 제조업"},
    {"date": "2026-12-01", "type": "ISM", "label": "ISM 제조업"},
    # ── 2027 Q1 ─────────────────────────────────────────────────────
    # FOMC 금리결정
    {"date": "2027-01-27", "type": "FOMC", "label": "FOMC 금리결정"},
    {"date": "2027-03-17", "type": "FOMC", "label": "FOMC 금리결정"},
    # FOMC 의사록
    {"date": "2027-01-06", "type": "FOMC_MIN", "label": "FOMC 의사록"},
    {"date": "2027-02-17", "type": "FOMC_MIN", "label": "FOMC 의사록"},
    # CPI 소비자물가
    {"date": "2027-01-14", "type": "CPI", "label": "CPI 소비자물가"},
    {"date": "2027-02-12", "type": "CPI", "label": "CPI 소비자물가"},
    {"date": "2027-03-12", "type": "CPI", "label": "CPI 소비자물가"},
    # PPI 생산자물가
    {"date": "2027-01-13", "type": "PPI", "label": "PPI 생산자물가"},
    {"date": "2027-02-11", "type": "PPI", "label": "PPI 생산자물가"},
    {"date": "2027-03-11", "type": "PPI", "label": "PPI 생산자물가"},
    # 비농업 고용
    {"date": "2027-01-08", "type": "NFP", "label": "비농업 고용"},
    {"date": "2027-02-05", "type": "NFP", "label": "비농업 고용"},
    {"date": "2027-03-05", "type": "NFP", "label": "비농업 고용"},
    # PCE 물가
    {"date": "2027-01-29", "type": "PCE", "label": "PCE 물가"},
    {"date": "2027-02-26", "type": "PCE", "label": "PCE 물가"},
    {"date": "2027-03-26", "type": "PCE", "label": "PCE 물가"},
    # GDP 성장률
    {"date": "2027-01-28", "type": "GDP", "label": "GDP 성장률"},
    {"date": "2027-02-25", "type": "GDP", "label": "GDP 성장률"},
    # 소매판매
    {"date": "2027-01-15", "type": "RETAIL", "label": "소매판매"},
    {"date": "2027-02-17", "type": "RETAIL", "label": "소매판매"},
    {"date": "2027-03-16", "type": "RETAIL", "label": "소매판매"},
    # ISM 제조업
    {"date": "2027-01-04", "type": "ISM", "label": "ISM 제조업"},
    {"date": "2027-02-01", "type": "ISM", "label": "ISM 제조업"},
    {"date": "2027-03-01", "type": "ISM", "label": "ISM 제조업"},
]


def get_nearby_macro_event(hours_before: int = 24,
                           hours_after: int = 2) -> Optional[dict]:
    """현재 시각 기준 +-N시간 내 매크로 이벤트(가장 가까운 1건).

    반환: {"type": str, "label": str, "date": str, "hours_until": float}
    또는 None (근접 이벤트 없음).
    hours_until: 음수 = 이미 지남, 양수 = 아직 안 옴."""
    now = datetime.now(timezone.utc)
    closest = None
    min_dist = float("inf")

    for ev in MACRO_EVENTS:
        try:
            # 이벤트 시각: 날짜 기준 UTC 13:30 (미국 동부 08:30 = 주요 발표 시각)
            ev_dt = datetime.strptime(ev["date"], "%Y-%m-%d").replace(
                hour=13, minute=30, tzinfo=timezone.utc)
            diff_hours = (ev_dt - now).total_seconds() / 3600
            if -hours_after <= diff_hours <= hours_before:
                if abs(diff_hours) < min_dist:
                    min_dist = abs(diff_hours)
                    closest = {**ev, "hours_until": round(diff_hours, 1)}
        except (ValueError, TypeError):  # noqa: BLE001
            continue

    return closest
