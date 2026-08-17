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


def _resolve_cg_id(symbol: str, timeout: float):
    """Upbit 심볼 → (cg_id, cg_name) 튜플. 실패 시 (None, None).
    cg_name 은 StockTwits 등 다른 소스의 심볼 충돌 검증(2026-08-17) 원천 데이터."""
    try:
        r = requests.get("https://api.coingecko.com/api/v3/search",
                         params={"query": symbol}, headers=_cg_headers(),
                         timeout=timeout)
        if r.status_code != 200:
            return None, None
        coins = (r.json() or {}).get("coins") or []
        # 정확히 심볼이 일치하는 첫 항목 (CoinGecko는 시총 순 정렬)
        for c in coins:
            if (c.get("symbol") or "").upper() == symbol.upper():
                return c.get("id"), c.get("name")
        return None, None
    except Exception as e:  # noqa: BLE001
        logger.warning("[dex_map] CG search %s 실패: %s", symbol, e)
        return None, None


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
    cg_name = entry.get("cg_name")
    if not cg_id:
        cg_id, cg_name = _resolve_cg_id(coin_symbol, timeout)
    if not cg_id:
        cache[coin_symbol.upper()] = {"addr": "", "cg_id": "", "cg_name": "", "at": now}
        _save_cache(cache)
        return None

    addr = _fetch_platform_address(cg_id, timeout)
    cache[coin_symbol.upper()] = {"addr": addr or "", "cg_id": cg_id,
                                  "cg_name": cg_name or "", "at": now}
    _save_cache(cache)
    return addr if addr else None


def get_cg_name(coin_symbol: str, timeout: float = 10.0) -> Optional[str]:
    """Upbit 심볼 → CoinGecko 코인 name (StockTwits 심볼 충돌 검증용).
    캐시 우선(get_dex_address 와 동일 캐시 재사용) → 없으면 CG search 1콜.
    반환 예: SKY → 'Sky', BTC → 'Bitcoin', PEPE → 'Pepe'. 매칭 실패 시 None."""
    cache = _load_cache()
    entry = cache.get(coin_symbol.upper()) or {}
    now = time.time()
    at = entry.get("at") or 0
    # 캐시 히트 조건: cg_name 이 필드에 있고(구형식 호환) TTL 안이면 반환
    if "cg_name" in entry:
        ttl = _CACHE_TTL_SEC if entry.get("cg_id") else _NEG_TTL_SEC
        if now - at <= ttl:
            return entry.get("cg_name") or None

    # 캐시 미스·구형식 → 신선 조회 후 캐시 업데이트 (get_dex_address 와 동일 경로)
    cg_id, cg_name = _resolve_cg_id(coin_symbol, timeout)
    if not cg_id:
        cache[coin_symbol.upper()] = {"addr": entry.get("addr", ""), "cg_id": "",
                                       "cg_name": "", "at": now}
        _save_cache(cache)
        return None
    # 기존 addr 는 보존(별개 조회 결과) — 다음 get_dex_address 호출까지 유효
    cache[coin_symbol.upper()] = {**entry, "cg_id": cg_id, "cg_name": cg_name or "",
                                   "at": entry.get("at", now)}
    _save_cache(cache)
    return cg_name or None
