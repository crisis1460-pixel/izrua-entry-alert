"""KST 타임존 상수 및 날짜 유틸리티 — 프로젝트 전역 단일 정의."""

from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))


def day_kst(now: float) -> str:
    return datetime.fromtimestamp(now, tz=KST).strftime("%Y-%m-%d")


def iso_to_epoch(s) -> float | None:
    """'2026-07-17T07:11:47+00:00' / '...Z' → epoch float. 실패 시 None."""
    if not isinstance(s, str) or not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (ValueError, TypeError):
        return None
