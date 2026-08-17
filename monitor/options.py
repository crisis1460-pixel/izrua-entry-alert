"""
BTC 옵션 시장 컨텍스트 — Deribit Public API (인증 불필요).

P/C Ratio(풋/콜 미결제약정 비율)와 Max Pain(최대 고통가)을 수집하여
derive_supply_verdict 의 내부 보정 입력으로 제공한다.
알림·필터·등급에 수치가 직접 노출되지 않는다(CVD/OBI 보정과 동일 원칙).

Deribit 시장 점유율 ~90% — BTC/ETH 옵션의 사실상 단일 출처.
BTC 전용: 알트 옵션 유동성은 사실상 없어 데이터가 무의미하므로
BTC 결과를 전 코인 알림의 시장 컨텍스트로 활용(BTC.D/F&G 와 동일 성격).
"""

import logging
import time
from typing import Optional

import requests

logger = logging.getLogger("alert.options")

_DERIBIT_BASE = "https://www.deribit.com/api/v2"
_CACHE_TTL_SEC = 300.0  # 5분 — 옵션 OI는 느리게 변함
_CACHE_STALE_MAX_SEC = 3600.0  # 스테일 캐시 최대 수명
_cache: dict = {"ts": 0.0, "data": None}


def _fetch_raw(timeout: float) -> Optional[list]:
    """Deribit BTC 옵션 전 종목 요약 — 1 API 콜로 전체 OI·IV 수집."""
    try:
        r = requests.get(
            f"{_DERIBIT_BASE}/public/get_book_summary_by_currency",
            params={"currency": "BTC", "kind": "option"},
            timeout=timeout,
        )
        if r.status_code != 200:
            logger.warning("[options] Deribit HTTP %s", r.status_code)
            return None
        data = r.json().get("result")
        if not data:
            logger.warning("[options] Deribit 빈 응답")
            return None
        return data
    except Exception as e:  # noqa: BLE001
        logger.warning("[options] Deribit 요청 실패: %s", e)
        return None


def _calc_pc_ratio(instruments: list) -> Optional[float]:
    """전 만기 합산 Put/Call OI 비율. >1 = 풋 우위(약세 헤지), <1 = 콜 우위."""
    call_oi = put_oi = 0.0
    for inst in instruments:
        name = inst.get("instrument_name", "")
        oi = inst.get("open_interest", 0) or 0
        if name.endswith("-C"):
            call_oi += oi
        elif name.endswith("-P"):
            put_oi += oi
    if call_oi <= 0:
        return None
    return put_oi / call_oi


def _calc_max_pain(instruments: list) -> Optional[float]:
    """Max Pain = 행사가별 총 내재가치 합이 최소인 행사가(USD).

    옵션 만기 시 매도자(마켓메이커) 손실이 최소화되는 가격 →
    만기 접근 시 가격이 이 지점으로 수렴하는 경향(max pain theory)."""
    strikes: dict = {}  # {strike: {"call_oi": float, "put_oi": float}}
    for inst in instruments:
        name = inst.get("instrument_name", "")
        oi = inst.get("open_interest", 0) or 0
        if oi <= 0:
            continue
        parts = name.split("-")
        if len(parts) < 4:
            continue
        try:
            strike = float(parts[2])
        except (ValueError, IndexError):
            continue
        if strike not in strikes:
            strikes[strike] = {"call_oi": 0.0, "put_oi": 0.0}
        if name.endswith("-C"):
            strikes[strike]["call_oi"] += oi
        elif name.endswith("-P"):
            strikes[strike]["put_oi"] += oi

    if not strikes:
        return None

    all_strikes = sorted(strikes.keys())
    min_pain = float("inf")
    max_pain_strike = None

    for test_price in all_strikes:
        pain = 0.0
        for strike, ois in strikes.items():
            if test_price > strike:
                pain += (test_price - strike) * ois["call_oi"]
            elif test_price < strike:
                pain += (strike - test_price) * ois["put_oi"]
        if pain < min_pain:
            min_pain = pain
            max_pain_strike = test_price

    return max_pain_strike


def _fetch_dvol(timeout: float) -> Optional[float]:
    """Deribit DVOL(30일 내재변동성 지수). 무인증 공개 API.
    40 이하=평상시, 60~80=경계, 80+=위기."""
    try:
        r = requests.get(
            f"{_DERIBIT_BASE}/public/get_volatility_index_data",
            params={"currency": "BTC", "resolution": 3600,
                    "start_timestamp": int((time.time() - 7200) * 1000),
                    "end_timestamp": int(time.time() * 1000)},
            timeout=timeout,
        )
        if r.status_code != 200:
            logger.warning("[options] DVOL HTTP %s", r.status_code)
            return None
        data = r.json().get("result", {}).get("data", [])
        if data and len(data[-1]) > 4:
            return data[-1][4]  # index 4 = close
        return None
    except Exception as e:  # noqa: BLE001
        logger.warning("[options] DVOL 요청 실패: %s", e)
        return None


def fetch_btc_options_context(timeout: float = 10.0) -> Optional[dict]:
    """BTC 옵션 컨텍스트 반환. TTL 캐시 적용.

    반환: {"pc_ratio": float, "max_pain": float, "dvol": float|None} 또는 None.
    pc_ratio: Put OI / Call OI (전 만기 합산)
    max_pain: Max Pain 행사가(USD)
    dvol: DVOL 30일 내재변동성 지수
    """
    now = time.time()
    if _cache["data"] is not None and now - _cache["ts"] < _CACHE_TTL_SEC:
        return _cache["data"]

    instruments = _fetch_raw(timeout)
    dvol = _fetch_dvol(timeout)

    if not instruments:
        if _cache["data"] is not None and now - _cache["ts"] < _CACHE_STALE_MAX_SEC:
            return _cache["data"]
        return None

    pc = _calc_pc_ratio(instruments)
    mp = _calc_max_pain(instruments)
    if pc is None:
        if _cache["data"] is not None and now - _cache["ts"] < _CACHE_STALE_MAX_SEC:
            return _cache["data"]
        return None

    result = {"pc_ratio": pc, "max_pain": mp, "dvol": dvol}
    _cache["ts"] = now
    _cache["data"] = result
    return result
