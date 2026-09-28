"""
영문 암호화폐 뉴스 RSS 수집기 — 모닝 브리핑 뉴스 전용 입력원 (2026-09-29 대표 승인).

왜: 뉴스 입력원이 텔레그램 채널 4곳뿐이라 글 대부분이 시그널·홍보·업비트 미상장 코인이어서
조건을 통과하는 뉴스가 하루 0~5건이었다(09-29 브리핑 뉴스 0건). 공개 RSS 3곳(CoinDesk·
Cointelegraph·Decrypt, 가입·키 없음)을 더해 사실형 뉴스(상장·해킹·ETF·규제 등) 공급을 늘린다.

이 입력원은 **뉴스 전용**이다 — 셋업(진입가) 수집에는 쓰지 않는다. 하류는 텔레그램 뉴스와
같은 news_brief 경로(48h 신선도·업비트 유니버스 매칭·상한·쿨다운·사실만 🟢/🔴)를 탄다.

계약: fetch_items(name, url, timeout, max_age_hours=None, max_items=30) -> list[dict]
  반환 dict 키(텔레그램 post 와 같은 모양): title, description, url, published_at(epoch|None),
  age_minutes, channel(=피드 이름), author(=피드 이름)
  실패·스키마 변화는 조용히 [] + logger.warning (회차 생존 최우선). import 시 네트워크 없음.
"""

import email.utils
import html
import logging
import re
import time
import xml.etree.ElementTree as ET
from typing import Optional

import requests

logger = logging.getLogger("alert.rss_source")

_UA = {"User-Agent": "Mozilla/5.0 (compatible; izrua-entry-alert news reader)"}
_TAG_RX = re.compile(r"<[^>]+>")
_WS_RX = re.compile(r"[ \t\r\f\v]+")


def _clean(s: Optional[str]) -> str:
    """HTML 태그·엔티티 제거, 공백 정리."""
    t = html.unescape(_TAG_RX.sub(" ", s or ""))
    return "\n".join(_WS_RX.sub(" ", ln).strip() for ln in t.splitlines() if ln.strip())


def _pub_ts(item) -> Optional[float]:
    for tag in ("pubDate", "{http://purl.org/dc/elements/1.1/}date", "published", "updated"):
        v = item.findtext(tag)
        if not v:
            continue
        try:
            return email.utils.parsedate_to_datetime(v.strip()).timestamp()
        except (TypeError, ValueError, IndexError):
            pass
        try:
            from datetime import datetime
            return datetime.fromisoformat(v.strip().replace("Z", "+00:00")).timestamp()
        except ValueError:
            continue
    return None


def fetch_items(name: str, url: str, timeout: float, max_age_hours: Optional[float] = None,
                max_items: int = 30, now: Optional[float] = None) -> list:
    """RSS 2.0 피드 1개 → 뉴스 항목 목록(최신순 상위 max_items, max_age_hours 초과 제외)."""
    try:
        r = requests.get(url, headers=_UA, timeout=timeout)
    except Exception as e:  # noqa: BLE001 - 회차 생존 최우선
        logger.warning("[rss] %s 요청 실패: %s", name, e)
        return []
    if r.status_code != 200:
        logger.warning("[rss] %s HTTP %s", name, r.status_code)
        return []
    try:
        root = ET.fromstring(r.content)
    except ET.ParseError as e:
        logger.warning("[rss] %s XML 파싱 실패: %s", name, e)
        return []
    ref = now if now is not None else time.time()
    out = []
    for it in root.findall(".//item"):
        title = _clean(it.findtext("title"))
        if not title:
            continue
        desc = _clean(it.findtext("description"))
        link = (it.findtext("link") or "").strip()
        ts = _pub_ts(it)
        age_min = (ref - ts) / 60.0 if ts else None
        if max_age_hours and age_min is not None and age_min > max_age_hours * 60:
            continue
        out.append({"title": title, "description": desc, "url": link,
                    "published_at": ts, "age_minutes": age_min,
                    "channel": name, "author": name})
        if len(out) >= max_items:
            break
    logger.info("[rss] %s: %d건 수집", name, len(out))
    return out
