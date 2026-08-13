"""KST 타임존 상수 및 날짜 유틸리티 — 프로젝트 전역 단일 정의."""

from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))


def day_kst(now: float) -> str:
    return datetime.fromtimestamp(now, tz=KST).strftime("%Y-%m-%d")
