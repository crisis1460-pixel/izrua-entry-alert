"""
StockTwits — 크립토 심볼별 소셜 심리 통계 (무료·무등록·200 req/hr).

Reddit OAuth 등록이 Responsible Builder Policy 로 막힌 후 대체 채택
(2026-08-17). Coin Metrics(138종 커버)와 상호보완: StockTwits는 SOL/SUI/APT/
TAO/WLD/TIA/PEPE/SHIB 등 최근 유행 알트 커버가 오히려 강함.

심볼 관례: 크립토는 접미사 `.X` 필수(BTC.X, ETH.X, SOL.X).
UA 헤더 필수 — 미기입 시 403 반환.

응답 스트림 최신 30건 중 Bullish/Bearish 태그가 있는 것만 집계. 태그율은
30~50% 관례 — 태그된 표본 5건 미만은 노이즈로 판단해 None 반환.

전 실패 허용 — 알림·필터·등급 어느 것도 이 소스 결측으로 죽지 않는다.
"""

import logging
import re
from typing import Optional

import requests

logger = logging.getLogger("alert.stocktwits")

_BASE = "https://api.stocktwits.com/api/2/streams/symbol"
_UA = "Mozilla/5.0 (izrua-bot; +https://github.com/crisis1460-pixel)"
_MIN_TAGGED = 5   # 노이즈 컷 — 태그된 메시지가 이 미만이면 판정 유보


def _normalize_name(name: str) -> str:
    """이름 정규화 — 심볼 충돌 검증(2026-08-17)용. 소문자 + 공백/특수문자 제거만.
    접미('coin'/'token' 등) 제거는 위험(Sky/Skycoin 둘 다 'sky' 로 매칭되는 실측
    오탐이 있어 폐기) — 엄격 완전 일치로 심볼 충돌 방지.
    예: 'Sky' → 'sky', 'Skycoin' → 'skycoin' (다름 → 거부 ✓),
        'Bitcoin' → 'bitcoin', 'USD Coin' → 'usdcoin'"""
    if not name:
        return ""
    return re.sub(r"[\s\-_.]+", "", name.strip().lower())


def _names_match(cg_name: str, stwits_title: str) -> bool:
    """CoinGecko name 과 StockTwits symbol.title 이 같은 프로젝트를 지칭하는지.
    정규화 후 완전 일치. Skycoin(별도 프로젝트) vs Sky(구 MKR) 심볼 충돌 방지."""
    a = _normalize_name(cg_name)
    b = _normalize_name(stwits_title)
    if not a or not b:
        return False
    return a == b


def fetch_sentiment_stats(coin_symbol: str, expected_name: Optional[str] = None,
                          timeout: float = 10.0) -> Optional[dict]:
    """심볼별 최신 30건 스트림에서 감정 태그 집계.
    반환 {
      "total_msgs": int,          # 응답 메시지 수 (최대 30)
      "bullish": int,             # Bullish 태그 수
      "bearish": int,             # Bearish 태그 수
      "bullish_ratio": float|None,# bullish / (bullish + bearish), 없거나 표본<5면 None
    } 또는 None (심볼 미존재·API 실패).

    expected_name (2026-08-17 심볼 충돌 방지): CoinGecko 등 신뢰 소스가 준
    코인 이름(예: SKY → 'Sky'). StockTwits 응답의 symbol.title 과 정규화 후
    비교해 다른 프로젝트(예: SKY.X = 'Skycoin')면 None 반환. None 전달 시
    검증 스킵(구 호출부 호환).

    bullish_ratio 임계 관례 (금융 소셜 심리 표준):
      ≥0.75 강한 매수 심리 (매수 유리 배지)
      ≤0.30 강한 매도 심리 (매수 부담 배지)
      나머지·표본 부족 → 배지 무표기."""
    sym = f"{coin_symbol.upper()}.X"
    try:
        r = requests.get(f"{_BASE}/{sym}.json",
                         headers={"User-Agent": _UA}, timeout=timeout)
        if r.status_code == 404:
            return None  # 심볼 미존재 (WEMIX/KAIA 등 국내 특화 알트)
        if r.status_code != 200:
            logger.warning("[stocktwits] %s HTTP %s", sym, r.status_code)
            return None
        payload = r.json() or {}
        # 심볼 충돌 검증 (2026-08-17) — 실제 SKY 알림에서 발견된 SKY.X=Skycoin 오조회
        # 방지. expected_name 이 None 이면 (구 호출부 호환) 검증 스킵.
        if expected_name:
            st_title = (payload.get("symbol") or {}).get("title") or ""
            if not _names_match(expected_name, st_title):
                logger.warning("[stocktwits] %s 심볼 충돌 (CG=%s vs ST=%s) → 폐기",
                               sym, expected_name, st_title)
                return None
        msgs = payload.get("messages") or []
        if not msgs:
            return None
        bullish = bearish = 0
        for m in msgs:
            tag = ((m.get("entities") or {}).get("sentiment") or {}).get("basic")
            if tag == "Bullish":
                bullish += 1
            elif tag == "Bearish":
                bearish += 1
        tagged = bullish + bearish
        bullish_ratio = None
        if tagged >= _MIN_TAGGED:
            bullish_ratio = round(bullish / tagged, 3)
        return {
            "total_msgs": len(msgs),
            "bullish": bullish,
            "bearish": bearish,
            "bullish_ratio": bullish_ratio,
        }
    except Exception as e:  # noqa: BLE001
        logger.warning("[stocktwits] %s 실패: %s", sym, e)
        return None
