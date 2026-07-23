"""
업비트 공개(quotation) REST — 인증 불필요. 이 봇이 업비트 개인 API 키를 쓰지 않는 것은
보안 설계다(README) — 여기에 인증을 추가하지 말 것.

한도: 시세 REST 초당 10회(IP). 배치 ticker 는 1콜, 캔들은 마켓당 1콜이라
candle 호출만 페이싱(0.12s)한다.
"""

import logging
import time
from typing import Optional

import requests

logger = logging.getLogger("alert.upbit")

_BASE = "https://api.upbit.com/v1"
_CANDLE_PACE_SEC = 0.12  # 초당 ~8콜 (한도 10의 80%)


def fetch_prices(markets: list, timeout: float) -> dict:
    """여러 마켓 현재가를 1콜로. 반환 {market: price}. 실패 시 {}."""
    if not markets:
        return {}
    try:
        resp = requests.get(
            f"{_BASE}/ticker", params={"markets": ",".join(markets)}, timeout=timeout
        )
        resp.raise_for_status()
        return {t["market"]: float(t["trade_price"]) for t in resp.json()}
    except Exception as e:  # noqa: BLE001
        logger.warning("[upbit] 현재가 조회 실패: %s", e)
        return {}


def fetch_volume_ranks(timeout: float) -> dict:
    """업비트 KRW 전 마켓의 24h 거래대금 순위. 반환 {market: rank(1부터)}. 실패 시 {}.
    알림 발송 시점에만 호출(2콜: 마켓목록 + 배치 ticker) — 조회 시점 기준 순위."""
    try:
        resp = requests.get(f"{_BASE}/market/all", params={"isDetails": "false"}, timeout=timeout)
        resp.raise_for_status()
        markets = [m["market"] for m in resp.json() if m["market"].startswith("KRW-")]
        resp = requests.get(f"{_BASE}/ticker", params={"markets": ",".join(markets)}, timeout=timeout)
        resp.raise_for_status()
        vols = [(t["market"], float(t.get("acc_trade_price_24h") or 0)) for t in resp.json()]
        vols.sort(key=lambda x: x[1], reverse=True)
        return {market: i + 1 for i, (market, _) in enumerate(vols)}
    except Exception as e:  # noqa: BLE001
        logger.warning("[upbit] 거래량 순위 조회 실패: %s", e)
        return {}


def fetch_week52(market: str, timeout: float) -> Optional[tuple]:
    """52주 고가/저가 (KRW) — 주봉 52개의 최고 high / 최저 low. 실패 시 None.
    알림 발송 시에만 호출(회당 1콜)되므로 한도 부담 없음."""
    try:
        resp = requests.get(
            f"{_BASE}/candles/weeks",
            params={"market": market, "count": 52},
            timeout=timeout,
        )
        resp.raise_for_status()
        candles = resp.json()
        if not candles:
            return None
        high = max(float(c["high_price"]) for c in candles)
        low = min(float(c["low_price"]) for c in candles)
        time.sleep(_CANDLE_PACE_SEC)
        return (high, low)
    except Exception as e:  # noqa: BLE001
        logger.warning("[upbit] %s 52주 조회 실패: %s", market, e)
        return None


def fetch_low_since(market: str, minutes: int, timeout: float) -> Optional[float]:
    """최근 minutes 분간 1분봉 저가의 최솟값 — 체크 사이 스파이크 터치 소급 감지용.
    (1분봉 최대 200개 = 200분. 그 이상은 200분까지만 본다 — 가격체크가 5~10분
    주기이므로 실운영에서 잘릴 일은 사실상 없음)"""
    count = max(1, min(200, minutes))
    try:
        resp = requests.get(
            f"{_BASE}/candles/minutes/1",
            params={"market": market, "count": count},
            timeout=timeout,
        )
        resp.raise_for_status()
        lows = [float(c["low_price"]) for c in resp.json()]
        time.sleep(_CANDLE_PACE_SEC)
        return min(lows) if lows else None
    except Exception as e:  # noqa: BLE001
        logger.warning("[upbit] %s 1분봉 조회 실패: %s", market, e)
        time.sleep(_CANDLE_PACE_SEC)
        return None
