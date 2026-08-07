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
    """선물 펀딩비율(%). Binance Futures → Bybit → OKX 폴백. 인증 불필요.
    양수=롱 과열, 음수=숏 과열. 전 경로 실패 시 None(알림에서 행 생략).

    2026-08-07 OKX 3차 폴백 추가: fapi.binance.com 과 Bybit linear 는 미국 IP
    지역 차단(451/403) — GitHub Actions 러너(미국 Azure)에서 두 경로 모두 막혀
    프로덕션 알림에 펀딩 행이 항상 생략되던 문제. OKX 공개 API 는 미국 접근 가능.
    비-200 응답에도 경고를 남긴다(예전엔 조용히 폴백해 로그 무흔적)."""
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
        else:
            logger.warning("[funding] Binance %s HTTP %s", pair, r.status_code)
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
        else:
            logger.warning("[funding] Bybit %s HTTP %s", pair, r.status_code)
    except Exception as e:  # noqa: BLE001
        logger.warning("[funding] Bybit %s 실패: %s", pair, e)
    # OKX Swap (미상장 심볼은 51001 코드 200 응답 — data 비어 폴백 없이 None)
    try:
        r = requests.get(
            "https://www.okx.com/api/v5/public/funding-rate",
            params={"instId": f"{symbol.upper()}-USDT-SWAP"}, timeout=timeout,
        )
        if r.status_code == 200:
            data = r.json().get("data") or []
            if data:
                rate = data[0].get("fundingRate")
                if rate is not None:
                    return float(rate) * 100
        else:
            logger.warning("[funding] OKX %s HTTP %s", pair, r.status_code)
    except Exception as e:  # noqa: BLE001
        logger.warning("[funding] OKX %s 실패: %s", pair, e)
    return None


def fetch_funding_history(symbol: str, timeout: float,
                          days: int = 32) -> Optional[list]:
    """선물 펀딩비율 히스토리(오래된 순 float 리스트, 단위 % — 원시 decimal *100).
    Binance Futures → Bybit Linear → OKX 폴백. 인증 불필요.
    days: 조회할 일수 (기본 32일 — 30일 지속 음수 감지에 여유 2일).
    반환: None(전 경로 실패) 또는 시간순 정렬된 %값 리스트.
    Binance/Bybit 모두 펀딩 간격 8h → days*3 개 요청, 최대 1000/200 상한.
    OKX 는 페이지당 100 상한 — 기본 32일(99개)은 1페이지로 충분
    (2026-08-07 미국 IP 차단 대응, fetch_funding_rate 주석 참고).
    주의: days>32 를 넘기면 Bybit(66일)/OKX(33일) 폴백 경로는 요청보다 짧은
    리스트를 조용히 반환할 수 있다 — detect_funding_regime_flip 은 표본 부족 시
    None(배지 생략)이라 안전하지만, 새 호출부는 이 상한을 알고 써야 한다."""
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
    # OKX /api/v5/public/funding-rate-history — limit 최대 100 (약 33일치)
    try:
        r = requests.get(
            "https://www.okx.com/api/v5/public/funding-rate-history",
            params={"instId": f"{symbol.upper()}-USDT-SWAP",
                    "limit": min(n_needed, 100)},
            timeout=timeout,
        )
        if r.status_code == 200:
            items = r.json().get("data") or []
            if items:
                # OKX 응답은 최신→과거 순 → 뒤집어서 시간순으로.
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
        else:
            logger.warning("[funding-hist] OKX %s HTTP %s", pair, r.status_code)
    except Exception as e:  # noqa: BLE001
        logger.warning("[funding-hist] OKX %s 실패: %s", pair, e)
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
    # 전부 0(또는 사실상 0)인 구간은 "하락 편향"이 아니라 무편향 — 실제 음수가
    # 있어야 플립으로 인정 (2026-08-04 R2 감사: [0.0]*90+[0.001] false positive)
    if min(window) >= 0:
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
