"""
워쳐(izrua_watcher) DB 아티팩트에서 작성자 신뢰 데이터를 읽는다 — 선택 기능.

upbit_bot signals/watcher_feed.py 의 아티팩트 다운로드 경로를 이식(간소화).
WATCHER_GITHUB_TOKEN 이 없으면 조용히 빈 결과 반환 → 알림에는 '기록없음'/팔로워로 표시.
가져오는 것: chartist_stats(적중률·건수), chartist_whitelist, author_cache(팔로워).
"""

import io
import logging
import sqlite3
import tempfile
import zipfile
from pathlib import Path
from typing import Optional

import requests

from config import settings

logger = logging.getLogger("alert.watcher_stats")

_GITHUB_API = "https://api.github.com"
_OWNER = "crisis1460-pixel"
_REPO = "izrua_watcher"
_ARTIFACT_NAME = "watcher-db"


def _headers(token: str) -> dict:
    return {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Authorization": f"Bearer {token}",
    }


def load_author_stats(timeout: float = 15.0) -> dict:
    """반환: {username: {hit_rate, hit_count, whitelisted, followers}}. 실패 시 {}."""
    token = settings.secret("WATCHER_GITHUB_TOKEN")
    if not token:
        logger.info("[watcher] 토큰 없음 - 작성자 통계 비활성화 (알림엔 기록없음 표시)")
        return {}
    try:
        url = f"{_GITHUB_API}/repos/{_OWNER}/{_REPO}/actions/artifacts"
        resp = requests.get(url, headers=_headers(token), params={"per_page": 50}, timeout=timeout)
        resp.raise_for_status()
        arts = [a for a in resp.json().get("artifacts", [])
                if a.get("name") == _ARTIFACT_NAME and not a.get("expired", True)]
        if not arts:
            logger.warning("[watcher] 아티팩트 없음")
            return {}
        arts.sort(key=lambda a: a.get("created_at", ""), reverse=True)
        art_id = arts[0]["id"]

        resp = requests.get(f"{url}/{art_id}/zip", headers=_headers(token), timeout=timeout * 2)
        resp.raise_for_status()
        db_bytes = None
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            for name in zf.namelist():
                if name.endswith(".db"):
                    db_bytes = zf.read(name)
                    break
        if not db_bytes:
            return {}
        return _parse_db(db_bytes)
    except Exception as e:  # noqa: BLE001 - 선택 기능 실패가 수집을 막으면 안 됨
        logger.warning("[watcher] 작성자 통계 로드 실패(계속 진행): %s", e)
        return {}


def _parse_db(db_bytes: bytes) -> dict:
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            f.write(db_bytes)
            tmp_path = f.name
        conn = sqlite3.connect(tmp_path)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()

        out: dict = {}
        try:
            for r in c.execute(
                "SELECT username, hits, total FROM chartist_stats"
            ).fetchall():
                total = r["total"] or 0
                out[r["username"]] = {
                    "hit_rate": (r["hits"] / total) if total else None,
                    "hit_count": total,
                    "whitelisted": False,
                    "followers": None,
                }
        except sqlite3.OperationalError:
            # 스키마가 다르면(컬럼명 변화) 통계 없이 진행 — 치명 아님
            logger.warning("[watcher] chartist_stats 스키마 인식 실패 - 적중률 생략")

        try:
            for r in c.execute("SELECT username FROM chartist_whitelist").fetchall():
                out.setdefault(r["username"], {"hit_rate": None, "hit_count": 0,
                                               "whitelisted": False, "followers": None})
                out[r["username"]]["whitelisted"] = True
        except sqlite3.OperationalError:
            pass

        try:
            for r in c.execute("SELECT username, followers FROM author_cache").fetchall():
                out.setdefault(r["username"], {"hit_rate": None, "hit_count": 0,
                                               "whitelisted": False, "followers": None})
                out[r["username"]]["followers"] = r["followers"]
        except sqlite3.OperationalError:
            pass

        conn.close()
        logger.info("[watcher] 작성자 통계 %d명 로드", len(out))
        return out
    except Exception as e:  # noqa: BLE001
        logger.warning("[watcher] DB 파싱 실패: %s", e)
        return {}
    finally:
        if tmp_path and Path(tmp_path).exists():
            Path(tmp_path).unlink()
