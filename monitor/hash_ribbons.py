"""Hash Ribbons 지표 — BTC 채굴자 항복/회복 감지 (2026-08-15).

Charles Edwards 의 Hash Ribbons: 비트코인 해시레이트의 SMA30/SMA60 교차로
채굴자 상태를 판정한다.
  · SMA30 < SMA60 = 항복(capitulation) — 채굴자 스트레스 구간 (경고)
  · SMA30 이 SMA60 을 최근(14일 이내) 상향 돌파 = 회복(recovery)
    — 역사적으로 강한 매집 구간 (확인)
  · 그 외 = normal (신호 없음)

데이터: mempool.space 공개 API (무료, 인증 불필요)
  GET https://mempool.space/api/v1/mining/hashrate/3m
  → {"hashrates": [{"timestamp": epoch, "avgHashrate": float}, ...]} 일 단위
  ~90일치. 90개 포인트면 최근 ~30일 구간의 SMA60 계산이 가능해
  14일 크로스 룩백에 충분하다.

내부 보정 전용 — 알림 문구에 노출하지 않고 수급 판정 라벨 이동에만 사용.
전 구간 실패 허용: 오류 시 경고 로그 + None (호출부가 보정 생략).
"""

import json
import logging
import time
from typing import Optional

import requests

from storage import db

logger = logging.getLogger("alert.hash_ribbons")

_CACHE_KEY = "hash_ribbons"
_CACHE_TTL_SEC = 21600.0  # 6시간 — 해시레이트 SMA 는 일 단위로 움직임

_API_URL = "https://mempool.space/api/v1/mining/hashrate/3m"
_CROSS_LOOKBACK = 14  # 회복 크로스 인정 기간 (일 단위 포인트 수)


def fetch_hash_ribbons(conn, timeout: float = 10.0) -> Optional[dict]:
    """Hash Ribbons 상태 (DB meta 캐시, TTL 6h).

    반환: {"state": "capitulation"|"recovery"|"normal",
           "sma30_over_sma60": float}  또는 None (조회 실패)."""
    try:
        raw = db.get_meta(conn, _CACHE_KEY)
        if raw:
            payload = json.loads(raw)
            if time.time() - payload.get("at", 0) <= _CACHE_TTL_SEC:
                return payload.get("data")
    except Exception:  # noqa: BLE001 - 캐시 문제는 무시하고 새로 조회
        pass

    data = _fetch_fresh(timeout)
    if data is not None:
        try:
            db.set_meta(conn, _CACHE_KEY,
                        json.dumps({"at": time.time(), "data": data}))
        except Exception:  # noqa: BLE001
            pass
    return data


def _sma(values, n, shift=0):
    """뒤에서 shift 만큼 물러난 시점 기준 최근 n개 평균. 부족하면 None."""
    if len(values) < n + shift:
        return None
    window = values[-(n + shift):len(values) - shift or None]
    return sum(window) / n


def _fetch_fresh(timeout: float) -> Optional[dict]:
    """mempool.space 3m 해시레이트 시계열 → SMA30/SMA60 상태 계산."""
    try:
        r = requests.get(_API_URL, timeout=timeout)
        if r.status_code != 200:
            logger.warning("[hash_ribbons] mempool.space HTTP %s", r.status_code)
            return None

        body = r.json()
        points = body.get("hashrates") if isinstance(body, dict) else body
        if not points:
            logger.warning("[hash_ribbons] 해시레이트 포인트 없음")
            return None

        points = sorted(points, key=lambda p: p.get("timestamp", 0))
        values = [float(p["avgHashrate"]) for p in points
                  if p.get("avgHashrate") is not None]
        if len(values) < 60:
            logger.warning("[hash_ribbons] 포인트 부족: %d < 60", len(values))
            return None

        sma30 = _sma(values, 30)
        sma60 = _sma(values, 60)
        if not sma60:  # None 또는 0 — 비율 계산 불가
            return None

        if sma30 < sma60:
            state = "capitulation"
        else:
            # 최근 14포인트 내에 sma30 < sma60 이었던 적이 있으면
            # 갓 상향 돌파한 회복 크로스 = 역사적 매집 구간
            state = "normal"
            for k in range(1, _CROSS_LOOKBACK + 1):
                s30 = _sma(values, 30, shift=k)
                s60 = _sma(values, 60, shift=k)
                if s30 is None or s60 is None:
                    break
                if s30 < s60:
                    state = "recovery"
                    break

        return {"state": state,
                "sma30_over_sma60": round(sma30 / sma60, 4)}
    except Exception as e:  # noqa: BLE001
        logger.warning("[hash_ribbons] 조회 실패: %s", e)
        return None
