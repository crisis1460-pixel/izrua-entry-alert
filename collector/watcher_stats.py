"""
워쳐(izrua_watcher) DB 아티팩트에서 작성자 신뢰 데이터를 읽는다 — 선택 기능.

upbit_bot signals/watcher_feed.py 의 아티팩트 다운로드 경로를 이식(간소화).
WATCHER_GITHUB_TOKEN 이 없으면 조용히 빈 결과 반환 → 알림에는 '기록없음'/팔로워로 표시.
가져오는 것: chartist_stats(적중률·건수), chartist_whitelist, author_cache(팔로워),
signal_tracking(코인별·전체 SL률).
"""

import io
import logging
import sqlite3
import tempfile
import time
import zipfile
from pathlib import Path
import requests

from config import settings

logger = logging.getLogger("alert.watcher_stats")

_GITHUB_API = "https://api.github.com"
_OWNER = "crisis1460-pixel"
_REPO = "izrua_watcher"
_ARTIFACT_NAME = "crypto-db"

_cache: dict = {"data": None, "ts": 0}
_CACHE_TTL = 3600 * 4


def _headers(token: str) -> dict:
    return {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Authorization": f"Bearer {token}",
    }


def _download_artifact(timeout: float = 15.0) -> bytes | None:
    token = settings.secret("WATCHER_GITHUB_TOKEN")
    if not token:
        logger.info("[watcher] 토큰 없음 - 작성자 통계 비활성화 (알림엔 기록없음 표시)")
        return None
    try:
        url = f"{_GITHUB_API}/repos/{_OWNER}/{_REPO}/actions/artifacts"
        resp = requests.get(url, headers=_headers(token), params={"per_page": 50}, timeout=timeout)
        resp.raise_for_status()
        arts = [a for a in resp.json().get("artifacts", [])
                if a.get("name") == _ARTIFACT_NAME and not a.get("expired", True)]
        if not arts:
            logger.warning("[watcher] 아티팩트 없음")
            return None
        arts.sort(key=lambda a: a.get("created_at", ""), reverse=True)
        art_id = arts[0]["id"]

        resp = requests.get(f"{url}/{art_id}/zip", headers=_headers(token), timeout=timeout * 2)
        resp.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            for name in zf.namelist():
                if name.endswith(".db"):
                    return zf.read(name)
        return None
    except Exception as e:  # noqa: BLE001
        logger.warning("[watcher] 아티팩트 다운로드 실패(계속 진행): %s", e)
        return None


def _parse_authors(c: sqlite3.Cursor) -> dict:
    out: dict = {}
    try:
        hit_miss: dict = {}
        for username, outcome, cnt in c.execute(
            "SELECT username, outcome, COUNT(*) FROM chartist_stats "
            "WHERE outcome IN ('hit','miss') GROUP BY username, outcome"
        ).fetchall():
            hit_miss.setdefault(username, {"hit": 0, "miss": 0})[outcome] = cnt
        for username, hm in hit_miss.items():
            total = hm["hit"] + hm["miss"]
            out[username] = {
                "hit_rate": (hm["hit"] / total) if total else None,
                "hit_count": total,
                "whitelisted": False,
                "followers": None,
            }
    except sqlite3.OperationalError:
        logger.warning("[watcher] chartist_stats 스키마 인식 실패 - 적중률 생략")

    try:
        for r in c.execute("SELECT username FROM chartist_whitelist").fetchall():
            out.setdefault(r["username"], {"hit_rate": None, "hit_count": 0,
                                           "whitelisted": False, "followers": None})
            out[r["username"]]["whitelisted"] = True
    except sqlite3.OperationalError:
        logger.warning("[watcher] chartist_whitelist 스키마 인식 실패 - 화이트리스트 생략")

    try:
        for r in c.execute("SELECT username, followers FROM author_cache").fetchall():
            out.setdefault(r["username"], {"hit_rate": None, "hit_count": 0,
                                           "whitelisted": False, "followers": None})
            out[r["username"]]["followers"] = r["followers"]
    except sqlite3.OperationalError:
        logger.warning("[watcher] author_cache 스키마 인식 실패 - 팔로워 생략")

    return out


def _parse_sl_rates(c: sqlite3.Cursor, days: int = 7) -> tuple:
    """코인별 SL률 + 시장 전체 SL률.

    Returns (coin_sl_rates, market_sl):
        coin_sl_rates: {symbol: {hits, misses, total, sl_rate}}
        market_sl: {hits, misses, total, sl_rate} or None
    """
    coin_sl: dict = {}
    market_hits = market_misses = 0
    try:
        cutoff = time.strftime("%Y-%m-%dT%H:%M:%S",
                               time.gmtime(time.time() - 86400 * days))
        rows = c.execute(
            "SELECT st.coin_symbol, sub.outcome "
            "FROM signal_tracking st "
            "JOIN ("
            "  SELECT signal_hash, MAX(outcome) AS outcome "
            "  FROM chartist_stats "
            "  WHERE outcome IN ('hit','miss') "
            "  GROUP BY signal_hash"
            ") sub ON st.signal_hash = sub.signal_hash "
            "WHERE st.alerted_at >= ?",
            (cutoff,),
        ).fetchall()
        for row in rows:
            sym = row["coin_symbol"]
            outcome = row["outcome"]
            coin_sl.setdefault(sym, {"hits": 0, "misses": 0})
            if outcome == "hit":
                coin_sl[sym]["hits"] += 1
                market_hits += 1
            else:
                coin_sl[sym]["misses"] += 1
                market_misses += 1
        for sym, d in coin_sl.items():
            d["total"] = d["hits"] + d["misses"]
            d["sl_rate"] = d["misses"] / d["total"] if d["total"] else None
    except sqlite3.OperationalError:
        logger.warning("[watcher] signal_tracking 스키마 인식 실패 - SL률 생략")
        return {}, None

    market_total = market_hits + market_misses
    market_sl = {
        "hits": market_hits,
        "misses": market_misses,
        "total": market_total,
        "sl_rate": market_misses / market_total if market_total else None,
    } if market_total else None

    return coin_sl, market_sl


def _parse_all(db_bytes: bytes) -> dict:
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            f.write(db_bytes)
            tmp_path = f.name
        conn = sqlite3.connect(tmp_path)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        try:
            authors = _parse_authors(c)
            coin_sl_rates, market_sl = _parse_sl_rates(c)
            logger.info("[watcher] 작성자 %d명, 코인 SL %d종목 로드",
                        len(authors), len(coin_sl_rates))
            return {
                "authors": authors,
                "coin_sl_rates": coin_sl_rates,
                "market_sl": market_sl,
            }
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001
        logger.warning("[watcher] DB 파싱 실패: %s", e)
        return {"authors": {}, "coin_sl_rates": {}, "market_sl": None}
    finally:
        if tmp_path and Path(tmp_path).exists():
            Path(tmp_path).unlink()


def load_watcher_data(timeout: float = 15.0) -> dict:
    """통합 로드. 4h 인프로세스 캐시.

    Returns {authors: {}, coin_sl_rates: {}, market_sl: {} or None}.
    """
    now = time.time()
    if _cache["data"] and (now - _cache["ts"]) < _CACHE_TTL:
        return _cache["data"]
    db_bytes = _download_artifact(timeout)
    if not db_bytes:
        if _cache["data"]:
            logger.info("[watcher] 다운로드 실패 - 기존 캐시 폴백")
            return _cache["data"]
        return {"authors": {}, "coin_sl_rates": {}, "market_sl": None}
    result = _parse_all(db_bytes)
    # 다운로드는 성공했으므로 결과가 비어있어도 캐시 갱신 (TTL 재출발 → 매콜 재다운 방지).
    # 진짜 파싱 실패는 _parse_all 이 예외를 로그하며 빈 dict 반환 — stale 캐시 유지 정책은
    # 다운로드 실패에만 적용(위 분기). 여기서 stale 유지하면 실제 빈 DB 상태를 영원히
    # 못 반영해 자기치유 불가.
    _cache["data"] = result
    _cache["ts"] = now
    return result


def load_author_stats(timeout: float = 15.0) -> dict:
    """하위 호환 — 기존 호출부용."""
    return load_watcher_data(timeout).get("authors", {})
