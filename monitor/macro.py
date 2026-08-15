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


def _kst_days_inclusive(since: str, today: str) -> int:
    """후보 시작일~오늘의 KST 날짜 수(양끝 포함). 파싱 실패 시 1(카운트 재시작
    취급 — 오염된 날짜로 조기 전환하는 것보다 보수적)."""
    try:
        d0 = datetime.strptime(since, "%Y-%m-%d").date()
        d1 = datetime.strptime(today, "%Y-%m-%d").date()
        return max((d1 - d0).days, 0) + 1
    except (ValueError, TypeError):
        return 1


def get_btc_regime(conn, timeout: float = 10.0,
                   now: Optional[float] = None) -> Optional[str]:
    """BTC 레짐 도장 — "above"|"below"(BTC vs 200일선), 3일 히스테리시스.

    히스테리시스: 원시 조건이 저장 상태와 어긋나면 즉시 뒤집지 않고 후보(cand)로
    두고, 같은 반대 조건이 3 KST일 연속(양끝 포함) 관측돼야 상태를 전환한다 —
    200일선 걸침 구간의 일중 왕복이 레짐 라벨을 매일 뒤집는 노이즈 제거
    (Golden Cross 류 지표의 확인 대기 관례). 조건이 상태와 재일치하면 후보 리셋
    (연속성 요건). 최초 호출은 원시 조건으로 즉시 초기화(히스테리시스 없음 —
    비교할 저장 상태가 없다).

    상태는 meta JSON 하나에 영속: {"state","cand","cand_since","at"}.
    "at" 이 1시간 캐시를 겸해 시간당 업비트 일봉 1콜이 상한(사용자 승인 예산).
    전 경로 실패 허용 — 원시 조건 조회 실패 시 저장 상태(스테일)를 그대로
    반환하고 "at" 은 갱신하지 않아 다음 회차가 재시도한다. now 인자는 테스트
    주입용(기본 현재 시각)."""
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
    if state is None:
        # 최초 호출(또는 오염 복구) — 원시 조건으로 즉시 초기화
        new = {"state": raw, "cand": None, "cand_since": None, "at": now}
    elif raw == state:
        new = {"state": state, "cand": None, "cand_since": None, "at": now}  # 후보 리셋
    else:
        since = st.get("cand_since")
        if st.get("cand") != raw or not since:
            since = today  # 새 반대 조건 관측 시작 — 카운트 재시작
        if _kst_days_inclusive(since, today) >= _REGIME_HYST_DAYS:
            new = {"state": raw, "cand": None, "cand_since": None, "at": now}  # 전환
        else:
            new = {"state": state, "cand": raw, "cand_since": since, "at": now}
    try:
        db.set_meta(conn, _BTC_REGIME_KEY, json.dumps(new))
    except Exception:  # noqa: BLE001 — 메타 기록 실패해도 이번 회차 값은 반환
        pass
    return new["state"]


# ── 매크로 이벤트 캘린더 ─────────────────────────────────────────────────
MACRO_EVENTS = [
    # 2026 FOMC
    {"date": "2026-09-16", "type": "FOMC", "label": "FOMC 금리결정"},
    {"date": "2026-11-04", "type": "FOMC", "label": "FOMC 금리결정"},
    {"date": "2026-12-16", "type": "FOMC", "label": "FOMC 금리결정"},
    # 2026 CPI
    {"date": "2026-09-10", "type": "CPI", "label": "CPI 발표"},
    {"date": "2026-10-14", "type": "CPI", "label": "CPI 발표"},
    {"date": "2026-11-12", "type": "CPI", "label": "CPI 발표"},
    {"date": "2026-12-10", "type": "CPI", "label": "CPI 발표"},
    # 2027 Q1 FOMC
    {"date": "2027-01-27", "type": "FOMC", "label": "FOMC 금리결정"},
    {"date": "2027-03-17", "type": "FOMC", "label": "FOMC 금리결정"},
    # 2027 Q1 CPI
    {"date": "2027-01-14", "type": "CPI", "label": "CPI 발표"},
    {"date": "2027-02-12", "type": "CPI", "label": "CPI 발표"},
    {"date": "2027-03-12", "type": "CPI", "label": "CPI 발표"},
]


def get_nearby_macro_event(hours_before: int = 24,
                           hours_after: int = 2) -> Optional[dict]:
    """현재 시각 기준 +-N시간 내 매크로 이벤트.

    반환: {"type": "FOMC"|"CPI", "label": str, "date": str, "hours_until": float}
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
