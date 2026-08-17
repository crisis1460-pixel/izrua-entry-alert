"""
Coin Metrics Community API — 온체인 활성 주소(30일 백분위).

무료·무키·1.6 rps (10 req / 6s sliding window)·**CC 비상업 라이선스**
(개인 알림봇은 문제 없음, 상업화 시 재검토 필요).

커버 자산 (2026-08-17 카탈로그 전수 실측, 총 138종):
- 카탈로그 API(`/catalog/assets`) 로 AdrActCnt·1d 지원 자산 138개 확보, 개별
  호출 검증 138/138 통과. 무료 티어 완전 접근.
- **여전히 미커버(카탈로그 자체에 없음)**: SOL, SUI, APT, TON, NEAR, ARB, OP,
  RENDER, SEI, TIA, JUP, WLD, TAO, PEPE, ORDI 등 — 유료 티어 전용.

자산 ID 매핑 (`_cm_asset_id`): Upbit 심볼(대문자) → Coin Metrics 소문자 자산
ID. 우선순위: (1) bare form (btc/eth/xrp) → (2) *_eth 접미(shib_eth,
matic_eth 등) → (3) *_native 접미(flow_native 등). 미커버는 None.

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

# 무료 티어 확인된 자산 (2026-08-17 카탈로그 전수 실측, 총 138종).
# 나머지 자산(SOL/SUI/APT 등)은 카탈로그 자체에 없음 → 유료 티어 전용.
COVERED = {
    "1inch", "aave", "ada", "ae_eth", "aion_eth", "algo", "alpha", "ant",
    "avaxc", "avaxp", "avaxx", "bal", "bat", "bch", "bnb", "bnb_eth", "bsv",
    "btc", "btg", "btm_eth", "buidl_eth", "busd", "comp", "cro", "crv",
    "crvusd_eth", "cvc", "dai", "dash", "dcr", "dgb", "doge", "dot", "drgn",
    "elf", "eos", "eos_eth", "etc", "eth", "eurc_eth", "eurcv_eth", "fdusd_eth",
    "flow_native", "frax_eth", "ftt", "fun", "fxc_eth", "gas", "gno", "gnt",
    "gusd", "hbtc", "hedg", "ht", "husd", "icp", "icx_eth", "kcs", "knc", "ldo",
    "lend", "leo_eos", "leo_eth", "link", "lpt", "lrc_eth", "ltc", "lusd_eth",
    "maid", "mana", "matic_eth", "mkr", "nas_eth", "neo", "nxm", "omg", "pax",
    "paxg", "pay", "perp", "pol_eth", "poly", "powr", "ppt", "pyusd_eth",
    "qash", "qnt", "qtum_eth", "ren", "renbtc", "rep", "rev_eth", "sai",
    "sdai_eth", "shib_eth", "snt", "snx", "srm", "steth_lido", "susde_eth",
    "sushi", "swrv", "trx", "trx_eth", "tusd", "tusd_eth", "tusd_trx", "uma",
    "uni", "usdc", "usdc_avaxc", "usdc_eth", "usdc_trx", "usdd_eth", "usde_eth",
    "usdk", "usdm_eth", "usdt", "usdt_avaxc", "usdt_eth", "usdt_omni",
    "usdt_trx", "vet_eth", "vtc", "wbtc", "weth", "wnxm", "wtc", "xaut", "xem",
    "xlm", "xrp", "xtz", "xvg", "yfi", "zec", "zil_eth", "zrx",
}


def _cm_asset_id(coin_symbol: str):
    """Upbit KRW 심볼 → Coin Metrics 자산 ID (bare → *_eth → *_native 순).
    None 반환 = 미커버(예: SOL/SUI/APT/PEPE 등 유료 티어 전용).

    예:
      BTC → 'btc' (bare)
      SHIB → 'shib_eth' (ERC-20 wrapped only)
      MATIC → 'matic_eth' (구 폴리곤 ERC-20)
      POL → 'pol_eth' (신 폴리곤 ERC-20)
      FLOW → 'flow_native' (네이티브 체인)
      SOL → None (카탈로그 자체에 없음)"""
    s = coin_symbol.lower()
    if s in COVERED:
        return s
    if f"{s}_eth" in COVERED:
        return f"{s}_eth"
    if f"{s}_native" in COVERED:
        return f"{s}_native"
    return None


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

    Upbit 심볼 → Coin Metrics 자산 ID 매핑은 `_cm_asset_id()` 참고
    (SHIB → shib_eth, MATIC → matic_eth, FLOW → flow_native 등 자동 별칭).
    백분위 = (현재값 이하 관측치 수) / 전체 × 100. 극단 20/80 임계로 배지·등급
    반영. conn 은 DB 캐시용 — None 이면 매번 API 콜(테스트 편의)."""
    asset = _cm_asset_id(coin_symbol)
    if asset is None:
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
