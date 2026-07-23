"""
시장 심리 지표 (BTC.D / ALT.S / F&G) — izrua_watcher coingecko_client.py 검증 로직 이식.

- BTC.D: CoinGecko /global 의 비트코인 시총 점유율
- F&G: alternative.me 공포탐욕지수 (무료, 키 불필요)
- ALT.S: 워쳐 방식 직접 계산 — top-50 알트 중 90d(실패 시 30d→7d) 기간에 BTC 를
  outperform 한 비율(%). 75+ 알트시즌 / 25- BTC 시즌.

호출 비용 관리: 가격체크가 5~10분마다 돌므로 매번 부르면 CoinGecko Demo 한도를
초과한다 → DB meta 에 1시간 TTL 캐시. 알림을 실제로 보낼 때만 조회(지연 로드)하는
것은 호출부(price_check) 책임.

전 항목 실패 허용 — 실패한 지표는 None 으로 남고 알림에서 그 줄만 생략된다.
"""

import json
import logging
import time
from typing import Optional

import requests

from config import settings
from storage import db

logger = logging.getLogger("alert.sentiment")

_CACHE_KEY = "market_sentiment"
_CACHE_TTL_SEC = 3600.0


def _cg_headers() -> dict:
    key = settings.secret("COINGECKO_API_KEY")
    return {"x-cg-demo-api-key": key} if key else {}


def _fetch_fresh(timeout: float) -> dict:
    result = {
        "btc_dominance": None,
        "fear_greed": None,
        "fear_greed_label": None,
        "altcoin_season_index": None,
    }

    try:
        r = requests.get("https://api.coingecko.com/api/v3/global",
                         headers=_cg_headers(), timeout=timeout)
        if r.status_code == 200:
            btc_d = r.json().get("data", {}).get("market_cap_percentage", {}).get("btc")
            if btc_d:
                result["btc_dominance"] = round(btc_d, 1)
    except Exception as e:  # noqa: BLE001
        logger.warning("[sentiment] BTC.D 조회 실패: %s", e)

    try:
        r = requests.get("https://api.alternative.me/fng/?limit=1", timeout=timeout)
        if r.status_code == 200:
            data = r.json().get("data", [])
            if data:
                result["fear_greed"] = int(data[0].get("value", 0))
                result["fear_greed_label"] = data[0].get("value_classification", "")
    except Exception as e:  # noqa: BLE001
        logger.warning("[sentiment] F&G 조회 실패: %s", e)

    # ALT.S — 워쳐와 동일: 90d 우선, 데이터 부족 시 30d→7d 폴백
    for period in ("90d", "30d", "7d"):
        try:
            r = requests.get(
                "https://api.coingecko.com/api/v3/coins/markets",
                params={"vs_currency": "usd", "order": "market_cap_desc",
                        "per_page": 50, "page": 1,
                        "price_change_percentage": period},
                headers=_cg_headers(), timeout=timeout + 5,
            )
            if r.status_code != 200:
                continue
            coins = r.json()
            key = f"price_change_percentage_{period}_in_currency"
            btc_change = next((c.get(key) for c in coins if c.get("symbol") == "btc"), None)
            alt_changes = [c.get(key) for c in coins
                           if c.get("symbol") != "btc" and c.get(key) is not None]
            if btc_change is not None and len(alt_changes) >= 20:
                outperformers = sum(1 for c in alt_changes if c > btc_change)
                result["altcoin_season_index"] = int(outperformers / len(alt_changes) * 100)
                break
        except Exception as e:  # noqa: BLE001
            logger.warning("[sentiment] ALT.S %s 조회 실패: %s", period, e)

    return result


def get_sentiment(conn) -> Optional[dict]:
    """1시간 캐시된 시장 심리 지표. conn 은 levels DB (meta 테이블 캐시용)."""
    try:
        raw = db.get_meta(conn, _CACHE_KEY)
        if raw:
            payload = json.loads(raw)
            if time.time() - payload.get("at", 0) <= _CACHE_TTL_SEC:
                return payload.get("data")
    except Exception:  # noqa: BLE001 - 캐시 문제는 무시하고 새로 조회
        pass

    data = _fetch_fresh(settings.get("http_timeout_sec"))
    try:
        db.set_meta(conn, _CACHE_KEY, json.dumps({"at": time.time(), "data": data}))
    except Exception:  # noqa: BLE001
        pass
    return data
