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


# 같은 프로젝트인데 표기만 다른 StockTwits title 별칭 (2026-09-27 알림 항목 v3).
# 엄격 완전 일치 규칙 때문에 XRP(CG 'XRP' vs ST 'Ripple')가 **매 발송 폐기**됐다
# (운영 DB: XRP 터치 18회 전부 소셜 NULL). 09-27 StockTwits 실측으로 소셜이 한 번도
# 안 채워진 코인을 전수 대조해 **title 은 다르지만 external_id(=CoinGecko id)가
# 일치하고 이름도 같은 프로젝트로 확인된 것만** 등록했다.
# 등록하지 않은 것(= 계속 폐기, 다른 프로젝트 이름): GMT(ST 'Mercury Protocol'),
# TRUMP('Trump For President'), ESP('Spain Coin'), VANA('Nirvana'), ZORA('Zoracles').
# 키 = 업비트 심볼(대문자), 값 = 허용 StockTwits title 목록.
TITLE_ALIASES = {
    "XRP": ("Ripple",),
    "HBAR": ("Hedera Hashgraph",),
    "XLM": ("Stellar Lumens",),
    "IOST": ("IOStoken",),
    "OP": ("Optimism Coin",),
    "ZRX": ("0x",),
    "ATOM": ("Cosmos",),
    "NEAR": ("Near",),
    "POL": ("Polygon",),
    "IMX": ("Immutable X",),
}


def _names_match(cg_name: str, stwits_title: str, symbol: Optional[str] = None) -> bool:
    """CoinGecko name 과 StockTwits symbol.title 이 같은 프로젝트를 지칭하는지.
    정규화 후 완전 일치. Skycoin(별도 프로젝트) vs Sky(구 MKR) 심볼 충돌 방지.
    symbol 이 주어지면 TITLE_ALIASES 의 확인된 별칭 title 도 일치로 본다."""
    a = _normalize_name(cg_name)
    b = _normalize_name(stwits_title)
    if not a or not b:
        return False
    if a == b:
        return True
    if symbol:
        for alias in TITLE_ALIASES.get(symbol.upper(), ()):
            if _normalize_name(alias) == b:
                return True
    return False


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

    알림 표시(2026-09-27 v3 재보정): '매수 유리' 배지는 폐지 — 크립토 스트림은
    평소 매수 비중이 높다(운영 DB 중앙값 90%). 평소보다 크게 낮을 때만
    (settings.social_warn_max_ratio 이하) 경고 배지 — 표시부는 소스 중립
    telegram.format_social({"bull_ratio", "n"}). 등급 산식(grading)은 별개."""
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
            if not _names_match(expected_name, st_title, symbol=coin_symbol):
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
