"""
유니버스 = CoinGecko 시총 top-N ∩ 업비트 KRW 마켓.

- CoinGecko /coins/markets 1콜로 top-200 + 순위 + 심볼 확보 (per_page=250).
- 업비트 KRW 마켓 목록(pyupbit 없이 공개 REST) 과 교집합.
- 시총 순위 → 등급 아이콘(💎🥇🥈🥉) 매핑.
- 일 1회 캐시(cache/universe.json) — 재실행 시 24h 이내면 API 안 부름.

CoinGecko Demo 키는 env(COINGECKO_API_KEY)에서 읽는다. 키가 없으면 keyless public
엔드포인트로 폴백(속도제한 빡세지만 일 1회라 대개 통과).

2026-07-26 수리: fetch_top_coins/fetch_upbit_krw_symbols 가 raise_for_status() 만
믿고 예외처리가 전혀 없어, 네트워크 오류·레이트리밋 한 번이면 build_universe() 가
예외를 던지며 수집 회차 전체를 죽였다. 이제 요청 단계 예외는 로그로 남기고 상위
(build_universe)로 전파해, 신선 캐시가 없어도 만료된 캐시가 있으면 그걸로 폴백한다
(완전 실패보다 낡은 유니버스가 낫다 - 어차피 다음 회차에 다시 시도됨).
"""

import json
import logging
import time
from pathlib import Path
from typing import Optional

import requests

from config import settings

logger = logging.getLogger("alert.coingecko")

CG_MARKETS_URL = "https://api.coingecko.com/api/v3/coins/markets"
UPBIT_MARKET_URL = "https://api.upbit.com/v1/market/all"

# 스테이블코인은 $1 고정이라 엔트리 터치 알림이 무의미 → 유니버스에서 제외.
STABLECOINS = {
    "USDT", "USDC", "USDS", "DAI", "TUSD", "USDD", "FDUSD", "PYUSD", "BUSD",
    "GUSD", "USDP", "FRAX", "USDe", "USDE", "LUSD", "SUSD", "USDL", "RLUSD",
}


def _mcap_tier(rank: int) -> tuple:
    """순위 → (아이콘, 라벨). 경계는 settings.mcap_tiers."""
    for upper, icon, label in settings.get("mcap_tiers"):
        if rank <= upper:
            return icon, label
    return "·", "순위밖"


def fetch_top_coins(top_n: int, timeout: float) -> list:
    """CoinGecko 시총 상위 코인. 반환: [{symbol, rank, name, price_usd, tier_icon}, ...]"""
    key = settings.secret("COINGECKO_API_KEY")
    params = {
        "vs_currency": "usd",
        "order": "market_cap_desc",
        "per_page": min(250, top_n),
        "page": 1,
        "sparkline": "false",
    }
    headers = {}
    if key:
        # Demo 키는 헤더로 전달 (공식 권장). Pro 키와 엔드포인트가 다르지만 Demo 는 이 헤더 사용.
        headers["x-cg-demo-api-key"] = key
    try:
        resp = requests.get(CG_MARKETS_URL, params=params, headers=headers, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        logger.warning("[cg] top coins 조회 실패: %s", e)
        raise
    coins = []
    for c in data[:top_n]:
        rank = c.get("market_cap_rank")
        if rank is None:
            continue
        icon, _label = _mcap_tier(rank)
        coins.append({
            "symbol": (c.get("symbol") or "").upper(),
            "rank": rank,
            "name": c.get("name"),
            "price_usd": c.get("current_price"),
            "tier_icon": icon,
        })
    return coins


def fetch_upbit_krw_symbols(timeout: float) -> set:
    """업비트 KRW 마켓의 코인 심볼 집합. 예: {'BTC','ETH','LINK',...} (인증 불필요)."""
    try:
        resp = requests.get(UPBIT_MARKET_URL, params={"isDetails": "false"}, timeout=timeout)
        resp.raise_for_status()
        payload = resp.json()
    except requests.RequestException as e:
        logger.warning("[cg] 업비트 마켓 조회 실패: %s", e)
        raise
    symbols = set()
    for m in payload:
        market = m.get("market", "")
        if market.startswith("KRW-"):
            symbols.add(market.split("-", 1)[1])
    return symbols


def build_universe(force: bool = False) -> list:
    """top-N ∩ 업비트 KRW. 캐시가 신선하면 그대로 반환.
    반환 항목: {symbol, ticker, rank, name, price_usd, tier_icon}"""
    cache_path = settings.get("universe_cache_path")
    max_age = settings.get("universe_refresh_hours") * 3600
    timeout = settings.get("http_timeout_sec")

    if not force:
        cached = _load_cache(cache_path, max_age)
        if cached is not None:
            return cached

    try:
        top = fetch_top_coins(settings.get("universe_top_n"), timeout)
        krw = fetch_upbit_krw_symbols(timeout)
    except requests.RequestException:
        # 2026-07-26 수리: 신선 캐시가 없어도(24h 지남/force) 완전 실패보다는 낡은
        # 유니버스가 낫다 - 수집 스킵보다 폐기된 코인 몇 개 섞이는 편이 안전.
        # 캐시조차 없으면(첫 실행) 원래 예외를 그대로 전파해 호출부가 이번 회차를
        # 스킵하게 한다(run_collect.py 책임).
        stale = _load_cache_any_age(cache_path)
        if stale is not None:
            logger.warning("[cg] 유니버스 갱신 실패 - 만료된 캐시로 폴백(%d개)", len(stale))
            return stale
        raise

    universe = []
    for c in top:
        if c["symbol"] in STABLECOINS:
            continue
        if c["symbol"] in krw:
            universe.append({
                "symbol": c["symbol"],
                "ticker": f"KRW-{c['symbol']}",
                "rank": c["rank"],
                "name": c["name"],
                "price_usd": c["price_usd"],
                "tier_icon": c["tier_icon"],
            })
    _save_cache(cache_path, universe)
    return universe


def _load_cache(path: str, max_age_sec: float) -> Optional[list]:
    p = Path(path)
    if not p.exists():
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            payload = json.load(f)
        if time.time() - payload.get("updated_at", 0) > max_age_sec:
            return None
        return payload.get("universe")
    except Exception:
        return None


def _load_cache_any_age(path: str) -> Optional[list]:
    """신선도 무시하고 캐시가 존재하기만 하면 반환 - 실패 시 폴백 전용(2026-07-26)."""
    p = Path(path)
    if not p.exists():
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            payload = json.load(f)
        return payload.get("universe")
    except Exception:
        return None


def _save_cache(path: str, universe: list) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"updated_at": time.time(), "universe": universe}, f, ensure_ascii=False, indent=2)
