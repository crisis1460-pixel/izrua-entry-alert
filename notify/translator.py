"""
뉴스 브리프 한글 번역 — Google Translate (비공식) + MyMemory 폴백.

외부 패키지 불필요 (requests 만 사용). 둘 다 키 불요·무료.
전 경로 실패 시 원문 그대로 반환 — 번역 실패가 알림을 죽이면 안 된다.
"""

import logging
import time
from typing import Optional

import requests

logger = logging.getLogger("alert.translator")

_GOOGLE_URL = "https://translate.googleapis.com/translate_a/single"
_MYMEMORY_URL = "https://api.mymemory.translated.net/get"

_cache: dict[str, tuple[str, float]] = {}
_CACHE_TTL = 86400.0


def _google(text: str, timeout: float) -> Optional[str]:
    r = requests.get(
        _GOOGLE_URL,
        params={
            "client": "gtx",
            "sl": "en",
            "tl": "ko",
            "dt": "t",
            "q": text,
        },
        timeout=timeout,
    )
    if r.status_code != 200:
        logger.warning("[번역] Google HTTP %s", r.status_code)
        return None
    data = r.json()
    if not data or not data[0]:
        return None
    return "".join(seg[0] for seg in data[0] if seg and seg[0])


def _mymemory(text: str, timeout: float) -> Optional[str]:
    r = requests.get(
        _MYMEMORY_URL,
        params={"q": text, "langpair": "en|ko"},
        timeout=timeout,
    )
    if r.status_code != 200:
        logger.warning("[번역] MyMemory HTTP %s", r.status_code)
        return None
    data = r.json()
    if data.get("responseStatus") != 200:
        logger.warning("[번역] MyMemory status %s", data.get("responseStatus"))
        return None
    return data["responseData"]["translatedText"]


def translate_en_ko(text: str, timeout: float = 5.0) -> str:
    """영→한 번역. 실패 시 원문 그대로 반환."""
    if not text or not text.strip():
        return text

    now = time.time()
    cached = _cache.get(text)
    if cached and now - cached[1] < _CACHE_TTL:
        return cached[0]

    result = None
    try:
        result = _google(text, timeout)
    except Exception as e:
        logger.warning("[번역] Google 예외: %s", e)

    if result is None:
        try:
            result = _mymemory(text, timeout)
        except Exception as e:
            logger.warning("[번역] MyMemory 폴백 예외: %s", e)

    if result is None:
        logger.warning("[번역] 전 경로 실패 — 원문 유지")
        return text

    _cache[text] = (result, now)
    return result
