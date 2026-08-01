"""
해외 공개 시세 — 김프 계산용 USD 가격 (인증 불필요, izrua_watcher 방식).
김프% = (업비트KRW가 ÷ 해외USDT가 − USDT/KRW환율) ÷ USDT/KRW환율 × 100

2026-08-01 (m-7 수리): Binance 두 엔드포인트(api.binance.com + data-api.binance.vision)
모두 HTTP 451(지역 차단)로 실패 → touch_kimchi_pct 전 행 NULL.
Bybit 공개 스팟 시세를 3차 폴백으로 추가 — 인증 불필요, 한국/미국 접근 가능.
"""

import logging
from typing import Optional

import requests

logger = logging.getLogger("alert.binance")

_BINANCE_ENDPOINTS = (
    "https://api.binance.com",
    "https://data-api.binance.vision",
)


def _try_binance(pair: str, timeout: float) -> Optional[float]:
    """Binance 엔드포인트 순회. 200 → 가격, 400(미상장) → None, 기타 → None(폴백)."""
    for base in _BINANCE_ENDPOINTS:
        try:
            r = requests.get(
                f"{base}/api/v3/ticker/price",
                params={"symbol": pair}, timeout=timeout,
            )
            if r.status_code == 200:
                return float(r.json()["price"])
            if r.status_code == 400:
                return None
            logger.warning("[binance] %s %s HTTP %s", base, pair, r.status_code)
        except Exception as e:  # noqa: BLE001
            logger.warning("[binance] %s %s 실패: %s", base, pair, e)
    return None


def _try_bybit(pair: str, timeout: float) -> Optional[float]:
    """Bybit 공개 스팟 시세. 한국/미국 접근 가능."""
    try:
        r = requests.get(
            "https://api.bybit.com/v5/market/tickers",
            params={"category": "spot", "symbol": pair}, timeout=timeout,
        )
        if r.status_code != 200:
            logger.warning("[bybit] %s HTTP %s", pair, r.status_code)
            return None
        items = r.json().get("result", {}).get("list", [])
        if items:
            return float(items[0]["lastPrice"])
    except Exception as e:  # noqa: BLE001
        logger.warning("[bybit] %s 실패: %s", pair, e)
    return None


def fetch_usdt_price(symbol: str, timeout: float) -> Optional[float]:
    """코인의 USDT 페어 현재가. 전 경로 실패 시 None
    (김프 줄만 생략됨 — 알림 발송은 계속된다)."""
    pair = f"{symbol.upper()}USDT"
    price = _try_binance(pair, timeout)
    if price is not None:
        return price
    price = _try_bybit(pair, timeout)
    if price is not None:
        logger.info("[bybit] %s 폴백 성공: %s", pair, price)
    return price
