"""알림 반응 피드백 폴링 (2026-08-15 시험 운용).

터치 본알림에 붙는 👍/👎 인라인 버튼(notify/telegram.py feedback_keyboard)의
콜백을 수거한다. 서버리스(GitHub Actions 2~4분 회차)라 웹훅 서버를 둘 수 없어
회차당 1회 getUpdates 폴링으로 대체한다 — allowed_updates=["callback_query"] 로
버튼 콜백만 받고, 처리한 최대 update_id 는 meta 에 저장해 다음 회차가 이어받는다
(매칭 안 되는 잡음 업데이트도 오프셋은 전진 — 같은 잡음이 매 회차 도는 걸 방지).

**전체 fail-safe**: 이 기능의 어떤 실패도 가격체크 핫패스를 죽이면 안 된다 —
poll_feedback 은 어떤 예외도 삼키고 0 을 돌려준다. getUpdates 가 409(웹훅 설정
상태) 등 비 200 을 돌려줘도 경고 로그만 남기고 넘어간다. answerCallbackQuery
실패도 무시한다(기록은 이미 됐고, 버튼의 로딩 스피너가 잠시 돌다 말 뿐).
"""

import json
import logging
import re
import time

import requests

from config import settings
from storage import db

logger = logging.getLogger("alert.feedback_poll")

_API = "https://api.telegram.org/bot{token}/{method}"
_META_OFFSET = "feedback_update_offset"
# callback_data 규약: "fb:<ref>:<up|down>" (telegram.feedback_keyboard 와 단일 출처)
_DATA_RE = re.compile(r"^fb:(.+):(up|down)$")


def _answer(token: str, callback_query_id, text: str, timeout) -> None:
    """answerCallbackQuery — 버튼 로딩 스피너 종료 + 토스트 표시. 실패는 무시
    (기록이 본체, 응답은 UX 장식). 토큰이 URL 에 실리므로 예외 문자열을 그대로
    로그에 남기지 않는다(telegram.py _redact 원칙과 동일 취지 — 타입명만)."""
    if not callback_query_id:
        return
    try:
        requests.post(_API.format(token=token, method="answerCallbackQuery"),
                      json={"callback_query_id": callback_query_id, "text": text},
                      timeout=timeout)
    except Exception as e:  # noqa: BLE001 - 응답 실패가 수거를 막으면 안 됨
        logger.warning("[피드백] answerCallbackQuery 실패(무시): %s", type(e).__name__)


def poll_feedback(conn, timeout) -> int:
    """getUpdates 1콜로 버튼 콜백을 수거해 DB 기록. 처리한 콜백 수 반환.

    어떤 예외도 밖으로 던지지 않는다(0 반환) — 호출부(price_check.run_once)의
    가드와 이중 방어."""
    try:
        return _poll(conn, timeout)
    except Exception as e:  # noqa: BLE001 - 핫패스 생존 최우선
        logger.warning("[피드백] 폴링 실패(무시): %s: %s", type(e).__name__, e)
        return 0


def _poll(conn, timeout) -> int:
    token = settings.secret("TELEGRAM_BOT_TOKEN")
    if not token:
        return 0  # 토큰 미설정(로컬 등) — 조용히 생략
    try:
        offset = int(db.get_meta(conn, _META_OFFSET) or 0)
    except (ValueError, TypeError):
        offset = 0
    resp = requests.get(
        _API.format(token=token, method="getUpdates"),
        params={"offset": offset + 1, "timeout": 0,
                "allowed_updates": json.dumps(["callback_query"])},
        timeout=timeout)
    if resp.status_code != 200:
        # 409 = 웹훅이 설정된 상태(getUpdates 와 상호배타) 등 — 절대 크래시 금지
        logger.warning("[피드백] getUpdates status=%s (무시)", resp.status_code)
        return 0

    updates = (resp.json() or {}).get("result") or []
    processed = 0
    max_id = None
    for up in updates:
        uid = up.get("update_id")
        if uid is not None and (max_id is None or uid > max_id):
            max_id = uid
        cq = up.get("callback_query")
        if not cq:
            continue  # 잡음 업데이트 — 오프셋만 전진
        m = _DATA_RE.match(cq.get("data") or "")
        if not m:
            continue  # 우리 버튼이 아닌 콜백 — 오프셋만 전진
        ref, vote = m.group(1), m.group(2)
        _from_id = (cq.get("from") or {}).get("id")
        tg_user = str(_from_id) if _from_id is not None else None
        db.record_feedback(conn, ref, vote, tg_user, time.time())
        processed += 1
        _answer(token, cq.get("id"),
                "기록됨 👍" if vote == "up" else "기록됨 👎", timeout)

    # 잡음 포함 전체의 최대 update_id 를 저장 — 매칭 실패 건이 다음 회차에
    # 또 내려와 무한 루프하는 걸 막는다.
    if max_id is not None:
        db.set_meta(conn, _META_OFFSET, str(max_id))
    if processed:
        logger.info("[피드백] 콜백 %d건 수거", processed)
    return processed
