"""고변동 경제지표 발표 30분 전 개별 알림 (2026-10-09 대표 요청).

"모닝 브리핑의 PPI·CPI 등 발표 일정 중 변동성 폭이 아주 높은 CPI 와 비슷한 지표가 발표하는
날에는 발표 30분 전에 별도 메시지를 중요 알림으로 하나 보내줘."

대상(settings.macro_prealert_types, 기본 CPI·FOMC 금리결정·비농업 고용): 암호화폐 시장에서
발표 직후 변동성이 가장 큰 미국 지표 3종. PPI·PCE·ISM·소매판매·GDP·FOMC 의사록은 브리핑
일정 줄로만 안내한다(설정에 타입을 넣으면 같은 경로로 알림).

발송 창: 발표까지 남은 시간이 (0, macro_prealert_window_minutes] 일 때 1회 — 회차가 약 4분
간격이라 기본 35분 창이면 30~35분 전 회차가 보낸다(러너 지연 시 더 늦게라도 발표 전이면 보냄).
중복 방지: meta macro_prealert_sent = ["TYPE|YYYY-MM-DD", …](최근 30개). 소리 있는 발송.
어떤 실패도 회차를 죽이지 않는다(run_cycle 의 다른 maybe_* 와 동일 격리).
"""

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from config import settings
from storage import db

logger = logging.getLogger("alert.macro_alert")

META_SENT = "macro_prealert_sent"
KST = timezone(timedelta(hours=9))
_SEP = "━━━━━━━━━━━━━━━━━"

# 지표별 한 줄 설명(32칸 이내) — 무엇이 나오는지·왜 크게 움직이는지.
_WHY = {
    "CPI": "물가 → 금리 인하 기대를 좌우",
    "FOMC": "기준금리 결정 + 의장 회견",
    "NFP": "고용 → 금리 경로 기대를 좌우",
    "PCE": "연준이 보는 물가 지표",
    "PPI": "생산자 물가 → CPI 선행",
}


def _cfg(key, default):
    try:
        v = settings.get(key)
        return default if v is None else v
    except KeyError:
        return default


def build_message(ev: dict, ev_utc: datetime, now: float) -> str:
    """알림 본문(HTML). 모든 줄 32칸 이내."""
    mins = max(1, int(round((ev_utc.timestamp() - now) / 60.0)))
    kst = ev_utc.astimezone(KST)
    day = "오늘" if kst.date() == datetime.fromtimestamp(now, KST).date() else "내일"
    label = ev.get("label") or ev.get("type") or "경제지표"
    lines = [
        _SEP,
        f"⏰ <b>[{mins}분 후 발표]</b>",
        f"🇺🇸 <b>{label}</b>",
        f"🕘 {day} {kst:%H:%M} (한국)",
    ]
    why = _WHY.get(ev.get("type"))
    if why:
        lines.append(f"📌 {why}")
    lines += [
        _SEP,
        "⚡ 발표 직후 급등락이 잦은 지표",
        "   · 직전 신규 진입은 신중히",
        "   · 방향 확인 후 대응",
        _SEP,
    ]
    return "\n".join(lines)


def due_events(events: list, now: float, types, window_min: float, sent: set) -> list:
    """지금 보내야 할 (ev, ev_utc) 목록 — 대상 타입 · 발표 전 window 분 이내 · 미발송."""
    out = []
    for ev in events or []:
        if ev.get("type") not in types:
            continue
        key = f"{ev.get('type')}|{ev.get('date')}"
        if key in sent:
            continue
        from monitor import macro
        ev_utc = macro.event_datetime_utc(ev)
        if ev_utc is None:
            continue
        lead = ev_utc.timestamp() - now
        if 0 < lead <= window_min * 60:
            out.append((ev, ev_utc))
    return out


def maybe_send_macro_prealert(db_path: str, now: Optional[float] = None) -> str:
    """반환 "disabled"|"skipped"|"ok"|"failed"."""
    if not _cfg("macro_prealert_enabled", True):
        return "disabled"
    now = time.time() if now is None else now
    try:
        from monitor import macro
        from notify import telegram
        types = set(_cfg("macro_prealert_types", ["CPI", "FOMC", "NFP"]) or [])
        window = float(_cfg("macro_prealert_window_minutes", 35))
        with db.connect(db_path) as conn:
            try:
                sent = set(json.loads(db.get_meta(conn, META_SENT) or "[]"))
            except (TypeError, ValueError):
                sent = set()
            todo = due_events(macro.get_macro_events(conn), now, types, window, sent)
            if not todo:
                return "skipped"
            status = "ok"
            for ev, ev_utc in todo:
                ok = telegram.send(build_message(ev, ev_utc, now), urgency="high")
                if ok:
                    sent.add(f"{ev.get('type')}|{ev.get('date')}")
                    logger.info("[macro] 발표 30분 전 알림 발송: %s %s", ev.get("type"), ev.get("date"))
                else:
                    status = "failed"
                    logger.warning("[macro] 발표 전 알림 발송 실패: %s %s", ev.get("type"), ev.get("date"))
            db.set_meta(conn, META_SENT, json.dumps(sorted(sent)[-30:]))
            conn.commit()
            return status
    except BaseException as e:  # noqa: BLE001 - 회차 격리
        if isinstance(e, (KeyboardInterrupt, SystemExit)):
            raise
        logger.warning("[macro] 발표 전 알림 실패(무시): %s: %s", type(e).__name__, e)
        return "failed"
