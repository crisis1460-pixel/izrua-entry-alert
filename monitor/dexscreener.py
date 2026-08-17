"""
DEX Screener — 토큰별 온체인 DEX 통계 (완전 무료·무키·300 req/min).

Upbit KRW 알트 대부분이 여러 DEX 페어에 상장돼 있어, CEX 알림 시점의
- 유동성 절대값
- 24h 볼륨 (관심 유입 강도)
- buys/sells 비율 (매수 압력)
- 회전율 (volume/liquidity)
를 알림 배지·등급 축으로 활용한다.

무키·무제한이지만 소형 알트(유동성 <100k$) 응답은 노이즈 심함 →
호출부가 임계값으로 필터링. 매핑 없는 네이티브 알트(XRP/ADA 등)는
빈 dict 반환해 배지 미표시로 자연 처리.

전 실패 허용 — 실패한 지표는 None 이면 배지 생략, 알림 자체는 항상 발송.
"""

import logging
from typing import Optional

import requests

logger = logging.getLogger("alert.dexscreener")

_BASE = "https://api.dexscreener.com/latest/dex"


def fetch_token_stats(token_address: str, timeout: float = 10.0) -> Optional[dict]:
    """토큰 주소(EVM/Solana)로 DEX 페어 전체 집계 반환.
    반환 {
      "liquidity_usd": float,        # 전 체인·전 페어 합산 유동성(USD)
      "volume_24h_usd": float,       # 전 페어 합산 24h 거래대금(USD)
      "buys_24h": int,               # 전 페어 합산 매수 tx 수
      "sells_24h": int,              # 전 페어 합산 매도 tx 수
      "buy_ratio_24h": float,        # buys / (buys+sells), 0~1
      "top_chain": str,              # 유동성 최대 체인(예: 'ethereum')
      "pair_count": int,
    } 또는 None (실패·페어 없음).

    Rate limit 300 req/min이지만 알림당 1콜(15/일)이라 여유 만배."""
    try:
        r = requests.get(f"{_BASE}/tokens/{token_address}", timeout=timeout)
        if r.status_code != 200:
            logger.warning("[dex] %s HTTP %s", token_address, r.status_code)
            return None
        pairs = (r.json() or {}).get("pairs") or []
        if not pairs:
            return None
        liq_by_chain = {}   # chain → 유동성 합
        tot_liq = tot_vol = tot_buys = tot_sells = 0.0
        for p in pairs:
            liq = ((p.get("liquidity") or {}).get("usd")) or 0
            vol = ((p.get("volume") or {}).get("h24")) or 0
            txns = (p.get("txns") or {}).get("h24") or {}
            b = txns.get("buys") or 0
            s = txns.get("sells") or 0
            chain = p.get("chainId") or "unknown"
            tot_liq += liq
            tot_vol += vol
            tot_buys += b
            tot_sells += s
            liq_by_chain[chain] = liq_by_chain.get(chain, 0) + liq
        if tot_liq <= 0 and tot_vol <= 0:
            return None
        tot_tx = tot_buys + tot_sells
        buy_ratio = (tot_buys / tot_tx) if tot_tx > 0 else None
        top_chain = max(liq_by_chain.items(), key=lambda x: x[1])[0] if liq_by_chain else None
        return {
            "liquidity_usd": round(tot_liq, 2),
            "volume_24h_usd": round(tot_vol, 2),
            "buys_24h": int(tot_buys),
            "sells_24h": int(tot_sells),
            "buy_ratio_24h": round(buy_ratio, 3) if buy_ratio is not None else None,
            "top_chain": top_chain,
            "pair_count": len(pairs),
        }
    except Exception as e:  # noqa: BLE001
        logger.warning("[dex] %s 조회 실패: %s", token_address, e)
        return None
