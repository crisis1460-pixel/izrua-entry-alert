"""
바이낸스 공개 시세 — 김프 계산용 해외 USD 가격 (인증 불필요, izrua_watcher 방식).
김프% = (업비트KRW가 ÷ 바이낸스USDT가 − USDT/KRW환율) ÷ USDT/KRW환율 × 100
"""

import logging
from typing import Optional

import requests

logger = logging.getLogger("alert.binance")


def fetch_usdt_price(symbol: str, timeout: float) -> Optional[float]:
    """코인의 바이낸스 USDT 페어 현재가. 상장 없음/실패 시 None (김프 줄만 생략됨)."""
    try:
        r = requests.get(
            "https://api.binance.com/api/v3/ticker/price",
            params={"symbol": f"{symbol.upper()}USDT"}, timeout=timeout,
        )
        if r.status_code != 200:
            return None
        return float(r.json()["price"])
    except Exception as e:  # noqa: BLE001
        logger.warning("[binance] %s 시세 조회 실패: %s", symbol, e)
        return None
