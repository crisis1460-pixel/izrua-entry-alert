"""
BTC 청산 클러스터 — ByKaranteli Public API (인증 불필요).

BTC 주변 청산 클러스터 방향을 파악하여 derive_supply_verdict 의 내부 보정
입력으로 제공한다. 알림·필터·등급에 수치가 직접 노출되지 않는다.

ByKaranteli: 6개 거래소(Binance, Bybit, OKX, Gate, HTX, Hyperliquid) 통합,
20개 메이저 종목 커버. 레이트 리밋 20 req/min, 캐시 5분 TTL.

BTC 전용: BTC 청산벽이 전체 시장 방향성에 가장 큰 영향을 미치므로
BTC 결과를 전 코인 알림의 시장 컨텍스트로 활용(옵션 컨텍스트와 동일 성격).
"""

import logging
import time
from typing import Optional

import requests

logger = logging.getLogger("alert.liquidation")

_API_BASE = "https://bykaranteli.com"
_CACHE_TTL_SEC = 300.0  # 5분 — API 캐시와 동기화
_CACHE_STALE_MAX_SEC = 3600.0  # 스테일 캐시 최대 수명
_cache: dict = {"ts": 0.0, "data": None}

_REGIME_MAP = {
    "LONG_CROWDED": "long_heavy",
    "LONG_FLUSH": "long_heavy",
    "SHORT_RAMP": "short_heavy",
    "SHORT_SQUEEZE": "short_heavy",
}


def fetch_btc_liq_context(timeout: float = 10.0) -> Optional[dict]:
    """BTC 청산 클러스터 컨텍스트. TTL 캐시 적용.

    반환: {"pressure": float, "direction": str} 또는 None.
    pressure: 파생상품 압력 점수 (0~100)
    direction: "long_heavy" | "short_heavy" | "neutral"
      - long_heavy: 롱 과밀/플러시 리스크 → 추가 하락 위험 (경고)
      - short_heavy: 숏 과밀/스퀴즈 리스크 → 숏 스퀴즈 가능 (확인)
    """
    now = time.time()
    if _cache["data"] is not None and now - _cache["ts"] < _CACHE_TTL_SEC:
        return _cache["data"]

    result = _fetch_pressure(timeout)
    if result is not None:
        _cache["ts"] = now
        _cache["data"] = result
        return result
    if _cache["data"] is not None and now - _cache["ts"] < _CACHE_STALE_MAX_SEC:
        return _cache["data"]
    return None


def _fetch_pressure(timeout: float) -> Optional[dict]:
    """ByKaranteli 복합 압력 점수 조회."""
    try:
        r = requests.get(
            f"{_API_BASE}/api/public/pressure/BTCUSDT",
            timeout=timeout,
        )
        if r.status_code != 200:
            logger.warning("[liq] ByKaranteli pressure HTTP %s", r.status_code)
            return _try_liqmap_fallback(timeout)
        data = r.json()
        if not data:
            return _try_liqmap_fallback(timeout)

        score = data.get("score")
        if score is None:
            return _try_liqmap_fallback(timeout)

        regime = (data.get("regime") or "").upper()
        direction = _REGIME_MAP.get(regime, "neutral")

        return {"pressure": float(score), "direction": direction}
    except Exception as e:  # noqa: BLE001
        logger.warning("[liq] ByKaranteli pressure 실패: %s", e)
        return _try_liqmap_fallback(timeout)


def _try_liqmap_fallback(timeout: float) -> Optional[dict]:
    """liqmap/public 엔드포인트 폴백 — pressure 실패 시.

    stats.long_short_ratio + stats.bias_label 로 방향 판정."""
    try:
        r = requests.get(
            f"{_API_BASE}/api/liqmap/public",
            params={"symbol": "BTC"},
            timeout=timeout,
        )
        if r.status_code != 200:
            logger.warning("[liq] ByKaranteli liqmap HTTP %s", r.status_code)
            return None
        data = r.json()
        if not data:
            return None

        stats = data.get("stats") or {}
        ls_ratio = stats.get("long_short_ratio")
        if ls_ratio is None:
            return None

        ls_ratio = float(ls_ratio)
        bias = (stats.get("bias_label") or "").lower()

        if ls_ratio >= 2.0 or "long flush" in bias or "long crowded" in bias:
            direction = "long_heavy"
        elif ls_ratio <= 0.5 or "short squeeze" in bias or "short ramp" in bias:
            direction = "short_heavy"
        else:
            direction = "neutral"

        pressure = min(ls_ratio / 3.0 * 100, 100.0)
        return {"pressure": pressure, "direction": direction}
    except Exception as e:  # noqa: BLE001
        logger.warning("[liq] ByKaranteli liqmap 실패: %s", e)
        return None
