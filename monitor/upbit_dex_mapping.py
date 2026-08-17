"""
Upbit 코인심볼 → DEX 토큰 주소 매핑 (CoinGecko `/coins/{id}` platforms 필드).

Upbit는 KRW 심볼(BTC, ETH, PEPE 등)만 노출하지만, DEX Screener는 온체인
주소로만 조회 가능. CoinGecko 응답의 `platforms` 필드가 chain → contract
address 매핑을 제공하므로 이걸 재이용해 자동 구축한다.

작동:
1. 최초 호출 시 CoinGecko 조회 → 첫 non-null address 저장 (DexScreener는
   주소 하나로 모든 체인 페어를 반환하므로 첫 것만 쓰면 충분)
2. `data/upbit_dex_addr_cache.json` 에 24h TTL 로 파일 캐시
3. 네이티브 코인(XRP/ADA/BTC 등, platforms 빈 dict) → 빈 문자열 저장, 재조회 억제

CoinGecko Demo API 재사용(이미 시장 심리·유니버스에서 활용) — 추가 계정 불필요.
"""

import json
import logging
import os
import time
from typing import Optional

import requests

from config import settings

logger = logging.getLogger("alert.dex_mapping")

_CACHE_PATH = "data/upbit_dex_addr_cache.json"
_CACHE_TTL_SEC = 86400.0     # 24h — Upbit 상장 변동 반영 주기
_NEG_TTL_SEC = 604800.0      # 7일 — 매핑 실패(네이티브 코인)는 더 길게 캐시

# CoinGecko id 캐시 (심볼 → id) — /coins/list 조회 결과 재사용
_ID_CACHE_KEY = "cg_id_cache"


def _load_cache() -> dict:
    try:
        if os.path.exists(_CACHE_PATH):
            with open(_CACHE_PATH, "r", encoding="utf-8") as f:
                return json.load(f) or {}
    except Exception as e:  # noqa: BLE001
        logger.warning("[dex_map] 캐시 로드 실패: %s", e)
    return {}


def _save_cache(data: dict) -> None:
    try:
        os.makedirs(os.path.dirname(_CACHE_PATH), exist_ok=True)
        tmp = _CACHE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, _CACHE_PATH)
    except Exception as e:  # noqa: BLE001
        logger.warning("[dex_map] 캐시 저장 실패: %s", e)


def _cg_headers() -> dict:
    key = settings.secret("COINGECKO_API_KEY")
    return {"x-cg-demo-api-key": key} if key else {}


def _resolve_cg_id(symbol: str, timeout: float) -> Optional[str]:
    """Upbit 심볼 → CoinGecko id (첫 매칭 반환).
    /coins/list?include_platform=true 를 한 번 조회해 인메모리 dict 구성."""
    # 간단히: /search 엔드포인트가 심볼→id 매칭에 최적
    try:
        r = requests.get("https://api.coingecko.com/api/v3/search",
                         params={"query": symbol}, headers=_cg_headers(),
                         timeout=timeout)
        if r.status_code != 200:
            return None
        coins = (r.json() or {}).get("coins") or []
        # 정확히 심볼이 일치하는 첫 항목 (CoinGecko는 시총 순 정렬)
        for c in coins:
            if (c.get("symbol") or "").upper() == symbol.upper():
                return c.get("id")
        return None
    except Exception as e:  # noqa: BLE001
        logger.warning("[dex_map] CG search %s 실패: %s", symbol, e)
        return None


def _fetch_platform_address(cg_id: str, timeout: float) -> Optional[str]:
    """CoinGecko /coins/{id} 의 platforms 필드에서 첫 non-null 주소 반환.
    빈 값('') 이면 네이티브 코인(BTC/XRP/ADA 등) — None 반환."""
    try:
        r = requests.get(f"https://api.coingecko.com/api/v3/coins/{cg_id}",
                         params={"localization": "false", "tickers": "false",
                                 "market_data": "false", "community_data": "false",
                                 "developer_data": "false"},
                         headers=_cg_headers(), timeout=timeout)
        if r.status_code != 200:
            return None
        platforms = (r.json() or {}).get("platforms") or {}
        for chain, addr in platforms.items():
            if addr and addr.strip():
                return addr.strip()
        return None
    except Exception as e:  # noqa: BLE001
        logger.warning("[dex_map] CG /coins/%s 실패: %s", cg_id, e)
        return None


def get_dex_address(coin_symbol: str, timeout: float = 10.0) -> Optional[str]:
    """Upbit 심볼(BTC/ETH/PEPE) → DEX Screener 조회용 컨트랙트 주소.
    24h 파일 캐시(음성은 7일). 네이티브 코인·매핑 실패 → None."""
    cache = _load_cache()
    now = time.time()
    entry = cache.get(coin_symbol.upper()) or {}
    addr = entry.get("addr")
    at = entry.get("at") or 0
    # 캐시 유효성: 성공 24h / 실패("" 저장) 7일
    if entry:
        ttl = _CACHE_TTL_SEC if addr else _NEG_TTL_SEC
        if now - at <= ttl:
            return addr if addr else None

    cg_id = entry.get("cg_id")
    if not cg_id:
        cg_id = _resolve_cg_id(coin_symbol, timeout)
    if not cg_id:
        cache[coin_symbol.upper()] = {"addr": "", "cg_id": "", "at": now}
        _save_cache(cache)
        return None

    addr = _fetch_platform_address(cg_id, timeout)
    cache[coin_symbol.upper()] = {"addr": addr or "", "cg_id": cg_id, "at": now}
    _save_cache(cache)
    return addr if addr else None
