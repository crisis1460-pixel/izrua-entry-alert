"""
리스크 체크 -- 섹터 집중도, 상관관계 등 포트폴리오 수준 위험 감지.
"""

import logging
from typing import Optional

logger = logging.getLogger("alert.risk")


def check_sector_concentration(conn, coin_symbol: str,
                                max_same_sector: int = 3) -> Optional[str]:
    """같은 섹터 코인 알림이 오늘 max_same_sector 건 이상이면 경고 문자열 반환.

    현재는 CoinGecko 카테고리 데이터가 없으므로 placeholder.
    향후 universe.json 에 카테고리 필드 추가 후 활성화.

    반환: 경고 메시지 문자열 또는 None (정상).

    TODO: 활성화 순서
      1. collector/coingecko.py 의 /coins/markets 응답에서 categories 필드 파싱
      2. data/universe.json 에 심볼별 카테고리 목록 저장
      3. 이 함수에서 오늘 발송된 알림의 카테고리를 집계해 집중도 판정
      4. 경고 시 알림 본문에 참고 태그 추가 (알림 억제는 하지 않음 -- 정보 제공만)
    """
    # Phase 1: placeholder -- 카테고리 데이터 축적 후 활성화
    return None
