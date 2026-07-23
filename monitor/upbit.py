"""
업비트 공개(quotation) REST — 인증 불필요. 이 봇이 업비트 개인 API 키를 쓰지 않는 것은
보안 설계다(README) — 여기에 인증을 추가하지 말 것.

한도: 시세 REST 초당 10회(IP). 배치 ticker 는 1콜, 캔들은 마켓당 1콜이라
candle 호출만 페이싱(0.12s)한다.
"""

import logging
import time
from datetime import datetime, timezone
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


def fetch_range_since(market: str, minutes: int, timeout: float) -> Optional[list]:
    """최근 minutes 분간의 분봉 목록 [(시작epoch, 종료epoch, high, low), ...] 시간 오름차순.

    2026-07-24 감사 수정: 예전엔 max(high)/min(low)로 뭉개서 반환했는데, 그러면
    ① 터치 이전 가격이 적중판정에 섞이고(가짜 hit) ② TP→SL 도달 순서를 알 수 있는
    경우까지 전부 '동시터치 miss'로 떨어졌다(승률 하향 편향). 캔들 목록을 그대로
    반환해 호출부가 시간순으로 판정하게 한다.

    소급 창이 200분을 넘으면(봇 다운타임) 15분봉으로 폴백해 최대 50시간까지 커버."""
    unit = 1 if minutes <= 200 else 15
    count = max(1, min(200, (minutes + unit - 1) // unit))
    try:
        resp = requests.get(
            f"{_BASE}/candles/minutes/{unit}",
            params={"market": market, "count": count},
            timeout=timeout,
        )
        resp.raise_for_status()
        raw = resp.json()
        time.sleep(_CANDLE_PACE_SEC)
        if not raw:
            return None
        out = []
        for c in raw:  # 업비트는 최신순 반환 → 오름차순으로 뒤집는다
            start = datetime.fromisoformat(c["candle_date_time_utc"]).replace(
                tzinfo=timezone.utc).timestamp()
            out.append((start, start + unit * 60,
                        float(c["high_price"]), float(c["low_price"])))
        out.sort(key=lambda x: x[0])
        return out
    except Exception as e:  # noqa: BLE001
        logger.warning("[upbit] %s 분봉 조회 실패: %s", market, e)
        time.sleep(_CANDLE_PACE_SEC)
        return None
