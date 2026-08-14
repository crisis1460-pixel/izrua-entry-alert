"""
토큰 이벤트 모니터 — 대량 언락 경고.

DeFiLlama /unlocks 엔드포인트 (무료, 인증 불필요).
대량 토큰 언락(유통량 대비 5%+) 7일 내 예정인 코인에 경고 태그.
전 항목 실패 허용 — None 반환 시 경고 생략.
"""

import json
import logging
import time
from datetime import datetime, timezone as tz
from typing import Optional

import requests

from storage import db

logger = logging.getLogger("alert.token_events")

_UNLOCK_CACHE_KEY = "token_unlocks"
_UNLOCK_CACHE_TTL_SEC = 21600.0  # 6시간 캐시 — 언락 일정은 느리게 변함


def fetch_upcoming_unlocks(conn, timeout: float = 10.0) -> Optional[dict]:
    """향후 7일 내 대량 언락 예정 코인 맵.

    반환: {symbol_upper: {"pct": float, "date": str, "value_usd": float}}
    또는 None (조회 실패).
    pct: 유통량 대비 언락 비율(%). 5% 이상만 포함."""
    try:
        raw = db.get_meta(conn, _UNLOCK_CACHE_KEY)
        if raw:
            payload = json.loads(raw)
            if time.time() - payload.get("at", 0) <= _UNLOCK_CACHE_TTL_SEC:
                return payload.get("data")
    except Exception:  # noqa: BLE001 - 캐시 문제는 무시하고 새로 조회
        pass

    data = _fetch_unlocks_fresh(timeout)
    if data is not None:
        try:
            db.set_meta(conn, _UNLOCK_CACHE_KEY,
                        json.dumps({"at": time.time(), "data": data}))
        except Exception:  # noqa: BLE001
            pass
    return data


def _fetch_unlocks_fresh(timeout: float) -> Optional[dict]:
    """DeFiLlama 토큰 언락 데이터 조회."""
    try:
        r = requests.get("https://api.llama.fi/unlocks/upcoming",
                         timeout=timeout)
        if r.status_code != 200:
            logger.warning("[token_events] DeFiLlama HTTP %s", r.status_code)
            return None

        result = {}
        now = time.time()
        week_later = now + 7 * 86400

        for item in r.json() or []:
            symbol = (item.get("symbol") or "").upper()
            if not symbol:
                continue

            # 향후 7일 내 이벤트만
            events = item.get("events") or []
            for ev in events:
                ts = ev.get("timestamp")
                if ts is None:
                    continue
                if not (now <= ts <= week_later):
                    continue

                # 유통량 대비 비율 계산
                unlock_pct = ev.get("unlockPercentage")
                if unlock_pct is None:
                    noOfTokens = ev.get("noOfTokens", 0)
                    maxSupply = item.get("maxSupply", 0)
                    if maxSupply > 0 and noOfTokens > 0:
                        unlock_pct = (noOfTokens / maxSupply) * 100

                if unlock_pct is not None and unlock_pct >= 5.0:
                    date_str = datetime.fromtimestamp(ts, tz=tz.utc).strftime("%m/%d")
                    existing = result.get(symbol)
                    if existing is None or unlock_pct > existing["pct"]:
                        result[symbol] = {
                            "pct": round(unlock_pct, 1),
                            "date": date_str,
                            "value_usd": ev.get("valueUSD", 0),
                        }

        return result
    except Exception as e:  # noqa: BLE001
        logger.warning("[token_events] 언락 조회 실패: %s", e)
        return None


def get_unlock_warning(conn, symbol: str, timeout: float = 10.0) -> Optional[dict]:
    """특정 코인의 대량 언락 경고. 5%+ 언락 예정이면 dict, 아니면 None."""
    unlocks = fetch_upcoming_unlocks(conn, timeout)
    if not unlocks:
        return None
    return unlocks.get(symbol.upper())
