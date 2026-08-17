"""
Coin Metrics Community API — 온체인 활성 주소(30일 백분위).

무료·무키·1.6 rps (10 req / 6s sliding window)·**CC 비상업 라이선스**
(개인 알림봇은 문제 없음, 상업화 시 재검토 필요).

커버 자산(2026-08-17 실측, 무료 티어, 총 43종):
  L1: BTC/ETH/XRP/ADA/DOGE/LTC/BCH/ETC/BNB/TRX/DASH/ZEC/XTZ/EOS/XLM/ALGO/ICP
  DeFi(ERC-20): LINK/UNI/AAVE/MKR/SNX/CRV/LDO/1INCH/YFI/SUSHI/COMP/BAT/OMG/
                MANA/ZRX/LPT/REN/KNC/BAL/UMA/FTT/POWR
  스테이블: USDT/USDC/BUSD/TUSD (알림 대상 아님, 화이트리스트만)
Upbit KRW 유니버스 300 대비 실질 커버율 ~10% — 나머지 알트는 배지·등급 자연
스킵. 미커버 자산(SOL/SUI/APT/TON/ARB/OP/RENDER/SEI/TIA 등)은 유료 티어 필요.

지표: **AdrActCnt (활성 주소 수, 일봉)** 30일 창의 현재 값 백분위.
- 백분위 ≤20 저조: 네트워크 관심 이탈 = 매수 부담 (-1점, 배지 표시)
- 백분위 ≥80 활발: 네트워크 관심 유입 = 매수 유리 (+1점, 배지 표시)
- 20~80 중립: 무표기·무가감

임계 근거: 30일 창 20/80 백분위는 통계 관례(양 극단 20% = 유의미 편차).
가중치가 낮은(±1) 이유는 커버율 낮아 유니버스 형평성 유지.
"""

import json
import logging
import time
from typing import Optional

import requests

from storage import db

logger = logging.getLogger("alert.coinmetrics")

_BASE = "https://community-api.coinmetrics.io/v4"
_CACHE_TTL_SEC = 86400.0     # 24h — 일봉 지표라 하루 1회면 충분
_NEG_TTL_SEC = 604800.0      # 7일 — 미커버 자산은 오래 캐시(재조회 억제)
_META_PREFIX = "cm_addr_pct_"

# 무료 티어 확인된 자산 (2026-08-17 실측, 총 43종). 나머지는 API 콜 자체 생략.
# 유료 티어 필요 자산(SOL/SUI/APT/ARB/OP/RENDER 등)은 여기 없음 → None 반환.
COVERED = {
    # L1 (17)
    "btc", "eth", "xrp", "ada", "doge", "ltc", "bch", "etc", "bnb", "trx",
    "dash", "zec", "xtz", "eos", "xlm", "algo", "icp",
    # ERC-20 DeFi/유틸 (22)
    "link", "uni", "aave", "mkr", "snx", "crv", "ldo", "1inch", "yfi", "sushi",
    "comp", "bat", "omg", "mana", "zrx", "lpt", "ren", "knc", "bal", "uma",
    "ftt", "powr",
    # 스테이블(알림 대상 아님, 화이트리스트만) (4)
    "usdt", "usdc", "busd", "tusd",
}


def _fetch_series(asset: str, timeout: float, days: int = 30) -> Optional[list]:
    """AdrActCnt 최근 N일 리스트(오래된 순)."""
    try:
        r = requests.get(
            f"{_BASE}/timeseries/asset-metrics",
            params={"assets": asset, "metrics": "AdrActCnt",
                    "frequency": "1d", "page_size": days},
            timeout=timeout,
        )
        if r.status_code != 200:
            logger.warning("[cm] %s HTTP %s", asset, r.status_code)
            return None
        data = (r.json() or {}).get("data") or []
        values = []
        for row in data:
            v = row.get("AdrActCnt")
            if v is not None:
                try:
                    values.append(float(v))
                except (ValueError, TypeError):
                    continue
        if len(values) < 5:
            return None
        return values
    except Exception as e:  # noqa: BLE001
        logger.warning("[cm] %s 조회 실패: %s", asset, e)
        return None


def fetch_active_addr_percentile(coin_symbol: str, conn=None,
                                 timeout: float = 10.0) -> Optional[float]:
    """활성 주소 수의 30일 백분위(0~100). 24h DB 캐시.
    미커버 자산·조회 실패 → None (배지·등급 자연 스킵).

    백분위 = (현재값 이하 관측치 수) / 전체 × 100. 극단 20/80 임계로 배지·등급
    반영. conn 은 DB 캐시용 — None 이면 매번 API 콜(테스트 편의)."""
    asset = coin_symbol.lower()
    if asset not in COVERED:
        return None  # 미커버 자산 — API 콜 자체 생략
    key = _META_PREFIX + asset

    # DB 캐시 조회 (성공/실패 별 TTL)
    if conn is not None:
        try:
            raw = db.get_meta(conn, key)
            if raw:
                payload = json.loads(raw)
                age = time.time() - payload.get("at", 0)
                # value=null (미커버·실패) → 7일 캐시
                ttl = _CACHE_TTL_SEC if payload.get("value") is not None else _NEG_TTL_SEC
                if age <= ttl:
                    return payload.get("value")
        except Exception:  # noqa: BLE001
            pass

    values = _fetch_series(asset, timeout, days=30)
    result = None
    if values and len(values) >= 5:
        current = values[-1]
        rank = sum(1 for v in values if v <= current)
        result = round(rank / len(values) * 100, 1)

    if conn is not None:
        try:
            db.set_meta(conn, key,
                        json.dumps({"at": time.time(), "value": result}))
        except Exception:  # noqa: BLE001
            pass
    return result
