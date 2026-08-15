# 알림 반응 피드백(2026-08-15 시험 운용) 오프라인 테스트.
# 네트워크 호출 없이 몽키패치/임시 DB(cache/) 로 검증 — test_infra.py 양식.
import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import logging
logging.basicConfig(level=logging.WARNING)

ok = True
n_checks = 0


def check(name, cond):
    global ok, n_checks
    n_checks += 1
    print(("✅" if cond else "❌"), name)
    if not cond:
        ok = False


from storage import db
import notify.telegram as tg
from notify import feedback_poll


# ─── feedback_keyboard 구조 + callback_data 64바이트 상한 ─────────────

kb = tg.feedback_keyboard("123")
check("keyboard: inline_keyboard 키 존재", "inline_keyboard" in kb)
_row = kb["inline_keyboard"][0]
check("keyboard: 버튼 2개 (👍/👎)", len(_row) == 2)
check("keyboard: up callback_data", _row[0]["callback_data"] == "fb:123:up")
check("keyboard: down callback_data", _row[1]["callback_data"] == "fb:123:down")
for btn in _row:
    cb = btn["callback_data"]
    check(f"keyboard: callback_data <64바이트 ({cb})",
          len(cb.encode("utf-8")) < 64)


# ─── 임시 DB 준비 (cache/) ───────────────────────────────────────────

_db_path = str(Path(__file__).resolve().parent.parent / "cache" / "test_feedback.db")
for suffix in ("", "-wal", "-shm"):
    try:
        os.remove(_db_path + suffix)
    except OSError:
        pass
db.init_db(_db_path)
now = time.time()


# ─── record_feedback: 삽입 + 같은 유저 재투표는 UPDATE ────────────────

with db.connect(_db_path) as conn:
    db.record_feedback(conn, "123", "up", "999", now)
    rows = conn.execute("SELECT * FROM alert_feedback").fetchall()
    check("record: 1건 삽입", len(rows) == 1)
    check("record: vote=up", rows[0]["vote"] == "up")

    db.record_feedback(conn, "123", "down", "999", now + 10)
    rows = conn.execute("SELECT * FROM alert_feedback").fetchall()
    check("record: 재투표 → 행 수 그대로(중복 없음)", len(rows) == 1)
    check("record: 재투표 → vote 갱신(down)", rows[0]["vote"] == "down")
    check("record: 재투표 → created_at 갱신",
          abs(rows[0]["created_at"] - (now + 10)) < 0.001)

    # 다른 유저는 별도 행
    db.record_feedback(conn, "123", "up", "1000", now)
    rows = conn.execute("SELECT * FROM alert_feedback").fetchall()
    check("record: 다른 유저 → 별도 행", len(rows) == 2)


# ─── poll_feedback: 콜백 2건(up/down) + 잡음 1건 → 2건 기록, 오프셋 전진 ──

def _mk_resp(status=200, payload=None):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = payload if payload is not None else {}
    return r


_updates = {
    "ok": True,
    "result": [
        {"update_id": 10,
         "callback_query": {"id": "cbq-1", "data": "fb:55:up",
                            "from": {"id": 111}}},
        {"update_id": 11,
         "callback_query": {"id": "cbq-2", "data": "fb:56:down",
                            "from": {"id": 222}}},
        # 잡음: callback_query 아님(일반 메시지) — 기록 없이 오프셋만 전진해야 함
        {"update_id": 12, "message": {"text": "hello"}},
    ],
}

with db.connect(_db_path) as conn:
    conn.execute("DELETE FROM alert_feedback")
    with patch("requests.get", return_value=_mk_resp(200, _updates)) as mock_get, \
         patch("requests.post", return_value=_mk_resp(200, {"ok": True})) as mock_post, \
         patch.object(feedback_poll.settings, "secret", return_value="TESTTOKEN"):
        n = feedback_poll.poll_feedback(conn, 5.0)
    check("poll: 처리 건수 2", n == 2)
    rows = conn.execute("SELECT ref, vote, tg_user_id FROM alert_feedback "
                        "ORDER BY ref").fetchall()
    check("poll: DB 2건 기록", len(rows) == 2)
    check("poll: up 기록 (ref=55, user=111)",
          (rows[0]["ref"], rows[0]["vote"], rows[0]["tg_user_id"]) == ("55", "up", "111"))
    check("poll: down 기록 (ref=56, user=222)",
          (rows[1]["ref"], rows[1]["vote"], rows[1]["tg_user_id"]) == ("56", "down", "222"))
    check("poll: 오프셋 = 잡음 포함 최대 update_id(12)",
          db.get_meta(conn, "feedback_update_offset") == "12")
    # getUpdates 파라미터 확인: 첫 폴링은 offset=0+1, 콜백만 구독
    _params = mock_get.call_args[1]["params"]
    check("poll: getUpdates offset=stored+1", _params["offset"] == 1)
    check("poll: allowed_updates=callback_query만",
          json.loads(_params["allowed_updates"]) == ["callback_query"])
    check("poll: answerCallbackQuery 2회", mock_post.call_count == 2)
    _ans = mock_post.call_args_list[0][1]["json"]
    check("poll: answer 에 callback_query_id 포함",
          _ans["callback_query_id"] == "cbq-1" and _ans["text"] == "기록됨 👍")

    # 다음 폴링은 저장된 오프셋 +1 부터
    with patch("requests.get",
               return_value=_mk_resp(200, {"ok": True, "result": []})) as mock_get2, \
         patch.object(feedback_poll.settings, "secret", return_value="TESTTOKEN"):
        n = feedback_poll.poll_feedback(conn, 5.0)
    check("poll: 빈 결과 → 0건", n == 0)
    check("poll: 다음 폴링 offset=13",
          mock_get2.call_args[1]["params"]["offset"] == 13)


# ─── poll_feedback: HTTP 409(웹훅 설정 상태) → 0 반환, 크래시 없음 ────

with db.connect(_db_path) as conn:
    _before = db.get_meta(conn, "feedback_update_offset")
    with patch("requests.get", return_value=_mk_resp(409, {"ok": False})), \
         patch.object(feedback_poll.settings, "secret", return_value="TESTTOKEN"):
        n = feedback_poll.poll_feedback(conn, 5.0)
    check("poll 409: 0 반환(크래시 없음)", n == 0)
    check("poll 409: 오프셋 불변", db.get_meta(conn, "feedback_update_offset") == _before)

    # 네트워크 예외도 삼킨다
    with patch("requests.get", side_effect=Exception("boom")), \
         patch.object(feedback_poll.settings, "secret", return_value="TESTTOKEN"):
        n = feedback_poll.poll_feedback(conn, 5.0)
    check("poll 예외: 0 반환(fail-safe)", n == 0)


# ─── send(): reply_markup 유/무에 따른 payload ───────────────────────

_markup = tg.feedback_keyboard("77")

with patch("requests.post", return_value=_mk_resp(200, {"ok": True})) as mock_post, \
     patch.object(tg.settings, "secret",
                  side_effect=lambda name: {"TELEGRAM_BOT_TOKEN": "T",
                                            "TELEGRAM_CHAT_ID": "C"}.get(name, "")), \
     patch("time.sleep"):
    sent = tg.send("본문 테스트", urgency="high", reply_markup=_markup)
    check("send+markup: 발송 성공", sent is True)
    _payload = mock_post.call_args[1]["json"]
    check("send+markup: payload 에 reply_markup 포함", "reply_markup" in _payload)
    check("send+markup: reply_markup 은 JSON 직렬화 문자열",
          json.loads(_payload["reply_markup"]) == _markup)
    check("send+markup: 본문 텍스트 불변", _payload["text"] == "본문 테스트")

with patch("requests.post", return_value=_mk_resp(200, {"ok": True})) as mock_post, \
     patch.object(tg.settings, "secret",
                  side_effect=lambda name: {"TELEGRAM_BOT_TOKEN": "T",
                                            "TELEGRAM_CHAT_ID": "C"}.get(name, "")), \
     patch("time.sleep"):
    sent = tg.send("본문 테스트", urgency="low")
    check("send 기본: 발송 성공", sent is True)
    _payload = mock_post.call_args[1]["json"]
    check("send 기본: reply_markup 키 없음(하위호환)", "reply_markup" not in _payload)


# ─── 정리 + 결과 ─────────────────────────────────────────────────────

for suffix in ("", "-wal", "-shm"):
    try:
        os.remove(_db_path + suffix)
    except OSError:
        pass

print(f"\n{'='*40}")
print(f"  feedback 테스트: {n_checks}건 {'전부 통과 ✅' if ok else '실패 있음 ❌'}")
print(f"{'='*40}")
sys.exit(0 if ok else 1)
