"""
Coinalyze — 파생 데이터 통합 API (무료 40 req/min, 이메일 가입만).

기존 `binance.py`의 펀딩/OI 조회가 Binance 중심(+CoinGecko/Bybit/OKX 폴백)이라
소형 알트·다중거래소 상장 알트에서 결측이 잦은 문제를 완화한다. Coinalyze는
30+ 파생 거래소(Binance/Bybit/OKX/Bitget/MEXC/BitMEX/KuCoin/…)를 통합 색인해
`{BASE}USDT_PERP.A` (A=Binance) 심볼 하나로 대부분 알트 커버.

키는 `.env` COINALYZE_API_KEY. 미설정·API 실패 시 조용히 None → 기존 폴백
체인의 마지막 단계로 붙어 다른 소스가 이미 성공했으면 호출조차 하지 않는다.

값 단위 검증(2026-08-17 실측): `value`는 이미 percentage number (BTC ~0.003,
XRP ~-0.012 등 -1%~+1% 범위). 다른 소스(Binance API 원시 decimal `*100`)와
동일한 %단위로 반환한다.
"""

import logging
import time
from typing import Optional

import requests

from config import settings

logger = logging.getLogger("alert.coinalyze")

_BASE = "https://api.coinalyze.net/v1"


def _perp_symbol(coin_symbol: str) -> str:
    """Upbit 코인심볼(BTC/ETH/…) → Coinalyze Binance perp 심볼.
    `.A` 접미는 Binance. 미상장 알트는 상위 폴백에서 실패로 처리됨."""
    return f"{coin_symbol.upper()}USDT_PERP.A"


def _key() -> Optional[str]:
    val = settings.secret("COINALYZE_API_KEY")
    return val if val else None


def fetch_funding_rate(coin_symbol: str, timeout: float = 10.0) -> Optional[float]:
    """선물 펀딩비율(%). 성공 시 이미 percentage 형태(예: 0.008 = 0.008%).
    미상장·키 미설정·API 실패 → None. 호출부는 다른 폴백으로 계속 진행."""
    key = _key()
    if not key:
        return None
    try:
        r = requests.get(
            f"{_BASE}/funding-rate",
            params={"symbols": _perp_symbol(coin_symbol), "api_key": key},
            timeout=timeout,
        )
        if r.status_code != 200:
            logger.warning("[coinalyze] funding %s HTTP %s", coin_symbol, r.status_code)
            return None
        data = r.json()
        if isinstance(data, list) and data:
            v = data[0].get("value")
            if v is not None:
                return float(v)
        return None
    except Exception as e:  # noqa: BLE001
        logger.warning("[coinalyze] funding %s 실패: %s", coin_symbol, e)
        return None


def fetch_open_interest(coin_symbol: str, timeout: float = 10.0) -> Optional[float]:
    """미결제약정 현재값(계약 수). 미상장·실패 → None.
    호출부가 자체 스냅샷(oi_history) 대비 % 변화를 계산한다."""
    key = _key()
    if not key:
        return None
    try:
        r = requests.get(
            f"{_BASE}/open-interest",
            params={"symbols": _perp_symbol(coin_symbol), "api_key": key},
            timeout=timeout,
        )
        if r.status_code != 200:
            logger.warning("[coinalyze] OI %s HTTP %s", coin_symbol, r.status_code)
            return None
        data = r.json()
        if isinstance(data, list) and data:
            v = data[0].get("value")
            if v is not None:
                return float(v)
        return None
    except Exception as e:  # noqa: BLE001
        logger.warning("[coinalyze] OI %s 실패: %s", coin_symbol, e)
        return None


def fetch_oi_change_24h(coin_symbol: str, timeout: float = 10.0) -> Optional[float]:
    """OI 24시간 변화율(%). Binance/Bybit/OKX 미상장 알트용 폴백 경로.
    /open-interest-history 로 최근 24개 1h 시계열 → 첫/마지막 종가 대비 %.
    성공 시 float(예: +18.4). 실패·표본부족 → None."""
    key = _key()
    if not key:
        return None
    try:
        now_ts = int(time.time())
        r = requests.get(
            f"{_BASE}/open-interest-history",
            params={"symbols": _perp_symbol(coin_symbol), "interval": "1hour",
                    "from": now_ts - 86400, "to": now_ts, "api_key": key},
            timeout=timeout,
        )
        if r.status_code != 200:
            logger.warning("[coinalyze] OI-history %s HTTP %s",
                           coin_symbol, r.status_code)
            return None
        data = r.json()
        if not (isinstance(data, list) and data):
            return None
        hist = data[0].get("history") or []
        if len(hist) < 2:
            return None
        first_c = hist[0].get("c")
        last_c = hist[-1].get("c")
        if not first_c or not last_c or first_c <= 0:
            return None
        return round((last_c - first_c) / first_c * 100, 2)
    except Exception as e:  # noqa: BLE001
        logger.warning("[coinalyze] OI-history %s 실패: %s", coin_symbol, e)
        return None
