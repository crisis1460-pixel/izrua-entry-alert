"""
바이낸스 공개 시세 — 김프 계산용 해외 USD 가격 (인증 불필요, izrua_watcher 방식).
김프% = (업비트KRW가 ÷ 바이낸스USDT가 − USDT/KRW환율) ÷ USDT/KRW환율 × 100

2026-08-01 (m-7 수리): 프로덕션 touch_kimchi_pct 가 전 행 NULL — GitHub Actions
러너(미국 IP)에서 api.binance.com 이 HTTP 451(지역 차단)을 돌려주는데, 종전
코드는 non-200 을 로그 없이 None 처리해 원인이 로그에도 안 남았다. 공개 시세
미러 data-api.binance.vision(미국 접근 가능한 공식 데이터 엔드포인트로 통용,
ccxt#15891·dev.binance.vision 커뮤니티 답변)을 2차로 시도하고, 상태코드를
로그에 남긴다. 400(Invalid symbol)은 미상장 — 폴백 무의미라 즉시 None.
"""

import logging
from typing import Optional

import requests

logger = logging.getLogger("alert.binance")

_ENDPOINTS = (
    "https://api.binance.com",        # 주 경로 — 미국 외 지역
    "https://data-api.binance.vision",  # 공개 시세 미러 — 미국(Actions 러너) 폴백
)


def fetch_usdt_price(symbol: str, timeout: float) -> Optional[float]:
    """코인의 바이낸스 USDT 페어 현재가. 상장 없음/전 경로 실패 시 None
    (김프 줄만 생략됨 — 알림 발송은 계속된다)."""
    pair = f"{symbol.upper()}USDT"
    for base in _ENDPOINTS:
        try:
            r = requests.get(
                f"{base}/api/v3/ticker/price",
                params={"symbol": pair}, timeout=timeout,
            )
            if r.status_code == 200:
                return float(r.json()["price"])
            if r.status_code == 400:
                return None     # Invalid symbol = 미상장 — 다른 엔드포인트도 동일
            logger.warning("[binance] %s %s HTTP %s - 다음 엔드포인트 시도",
                           base, pair, r.status_code)
        except Exception as e:  # noqa: BLE001
            logger.warning("[binance] %s %s 조회 실패: %s", base, pair, e)
    return None
