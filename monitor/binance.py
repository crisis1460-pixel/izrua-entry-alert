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


def fetch_funding_rate(symbol: str, timeout: float) -> Optional[float]:
    """선물 펀딩비율(%). Binance Futures → Bybit 폴백. 인증 불필요.
    양수=롱 과열, 음수=숏 과열. 전 경로 실패 시 None(알림에서 행 생략)."""
    pair = f"{symbol.upper()}USDT"
    # Binance Futures
    try:
        r = requests.get(
            "https://fapi.binance.com/fapi/v1/premiumIndex",
            params={"symbol": pair}, timeout=timeout,
        )
        if r.status_code == 200:
            rate = r.json().get("lastFundingRate")
            if rate is not None:
                return float(rate) * 100  # 0.0001 → 0.01%
    except Exception as e:  # noqa: BLE001
        logger.warning("[funding] Binance %s 실패: %s", pair, e)
    # Bybit Linear
    try:
        r = requests.get(
            "https://api.bybit.com/v5/market/tickers",
            params={"category": "linear", "symbol": pair}, timeout=timeout,
        )
        if r.status_code == 200:
            items = r.json().get("result", {}).get("list", [])
            if items:
                rate = items[0].get("fundingRate")
                if rate is not None:
                    return float(rate) * 100
    except Exception as e:  # noqa: BLE001
        logger.warning("[funding] Bybit %s 실패: %s", pair, e)
    return None


def fetch_funding_history(symbol: str, timeout: float,
                          days: int = 32) -> Optional[list]:
    """선물 펀딩비율 히스토리(오래된 순 float 리스트, 단위 % — 원시 decimal *100).
    Binance Futures 우선, 실패 시 Bybit Linear 폴백. 인증 불필요.
    days: 조회할 일수 (기본 32일 — 30일 지속 음수 감지에 여유 2일).
    반환: None(전 경로 실패) 또는 시간순 정렬된 %값 리스트.
    Binance/Bybit 모두 펀딩 간격 8h → days*3 개 요청, 최대 1000/200 상한."""
    pair = f"{symbol.upper()}USDT"
    n_needed = min(days * 3 + 3, 1000)  # 8h 간격 + 여유
    # Binance Futures /fapi/v1/fundingRate 는 limit 최대 1000
    try:
        r = requests.get(
            "https://fapi.binance.com/fapi/v1/fundingRate",
            params={"symbol": pair, "limit": n_needed}, timeout=timeout,
        )
        if r.status_code == 200:
            items = r.json()
            if isinstance(items, list) and items:
                # 응답은 시간순(과거→최근). fundingRate 문자열 원시 decimal.
                out = []
                for it in items:
                    v = it.get("fundingRate")
                    if v is None:
                        continue
                    try:
                        out.append(float(v) * 100)
                    except (ValueError, TypeError):
                        continue
                if out:
                    return out
    except Exception as e:  # noqa: BLE001
        logger.warning("[funding-hist] Binance %s 실패: %s", pair, e)
    # Bybit V5 /v5/market/funding/history — limit 최대 200 (약 66일치)
    try:
        r = requests.get(
            "https://api.bybit.com/v5/market/funding/history",
            params={"category": "linear", "symbol": pair,
                    "limit": min(n_needed, 200)},
            timeout=timeout,
        )
        if r.status_code == 200:
            items = r.json().get("result", {}).get("list", [])
            if items:
                # Bybit 응답은 최신→과거 순 → 뒤집어서 시간순으로.
                out = []
                for it in reversed(items):
                    v = it.get("fundingRate")
                    if v is None:
                        continue
                    try:
                        out.append(float(v) * 100)
                    except (ValueError, TypeError):
                        continue
                if out:
                    return out
    except Exception as e:  # noqa: BLE001
        logger.warning("[funding-hist] Bybit %s 실패: %s", pair, e)
    return None


def detect_funding_regime_flip(history: Optional[list],
                               min_neg_days: int = 30) -> Optional[dict]:
    """펀딩 레짐 전환 감지 — min_neg_days 일 이상 지속 음수 후 최근 양수 플립이면
    {"flipped": True, "neg_days": N, "latest": float} 반환, 아니면 None.

    "지속 음수"는 8h 간격 기준으로 min_neg_days*3 개 연속 <=0 상태를 의미하고
    (경계 0 포함 — 미미한 음수/제로도 롱 편향 없음), 최근 값은 양수(>0)여야 한다.
    사용자 결정(2026-08-03 질문카드): 임계 30일(리서치 정설).

    실패 시(히스토리 부재, 표본 부족) None — 알림 렌더에서 배지 생략."""
    if not history or len(history) < min_neg_days * 3 + 1:
        return None
    latest = history[-1]
    if latest <= 0:
        return None  # 최근이 양수여야 플립
    # 최근값 직전까지 min_neg_days*3 개가 모두 <=0
    window = history[-(min_neg_days * 3 + 1):-1]
    if not all(v <= 0 for v in window):
        return None
    # 플립 확정 — 실제 연속 음수 구간 길이(일수)를 정확히 세어 표기 정확도 향상.
    neg_count = 0
    for v in reversed(history[:-1]):
        if v <= 0:
            neg_count += 1
        else:
            break
    return {"flipped": True, "neg_days": neg_count / 3.0, "latest": latest}


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
