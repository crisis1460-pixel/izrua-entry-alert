#!/usr/bin/env python3
"""
단일 DB 라이터 엔트리포인트 (2분마다 — cron-job.org → GitHub Actions).
한 회차 = 가격체크(매번) + 수집(4시간마다) + 주간 리포트(7일마다).

## 왜 하나로 합쳤나 (2026-07-26 구조 개선)
이전엔 collect(4시간)와 price-check(2분)가 **각자** `data/levels.db` 를 커밋했다.
바이너리 파일은 3-way 머지가 불가능해 두 잡의 커밋은 항상 충돌했고, rebase 전략
(`-X ours` / `-X theirs`)으로 "누가 양보하나"만 정해줄 뿐 한쪽 결과는 반드시 버려졌다.
실제로 2026-07-23~26 사이 모든 수집분이 조용히 폐기돼 신규 레벨이 0건이었다(커밋 26ac522).

근본 해법은 **라이터를 1개로 만드는 것**이다. 가격체크가 2분마다 도는 김에,
"마지막 수집 후 4시간이 지났으면" 그 회차에 수집도 같이 수행한다.
이제 `data/levels.db` 를 커밋하는 워크플로는 price-check.yml 하나뿐이므로
충돌 자체가 발생하지 않는다(= 유실 경로 제거).
주간 리포트도 같은 패턴으로 흡수해, 외부 크론 등록을 추가로 요구하지 않는다.

## 주기 판정 (meta 테이블 — 기존 `last_check_at` 과 동일 패턴)
  - `last_collect_at`      / `last_collect_fail_at`
  - `last_weekly_report_at`/ `last_weekly_report_fail_at`
실패 직후 2분 뒤 회차가 곧바로 재시도하면 무거운 작업이 가격체크를 계속 지연시키므로,
실패 시에는 각각 `collect_retry_minutes` / `weekly_report_retry_minutes` 만큼 쉰다.

## 부분 실패 격리
- 가격체크(알림)를 **가장 먼저** 수행한다 → 무거운 수집이 알림 지연을 만들지 않는다.
- 수집은 **별도 프로세스(subprocess)** 로 돌린다. 예외로 죽든, 멈춰서 타임아웃되든,
  Cloudflare 에 차단되든 이 프로세스는 영향받지 않는다. 수집이 중간까지 쓴 레벨은
  SQLite 에 이미 커밋돼 있어 살아남는다.
- 주간 리포트 실패도 삼켜서 로그/경고로만 남긴다.
- 프로세스 종료코드는 **가격체크 성공 여부만** 반영한다(나머지는 ::warning::).

사용:
  python scripts/run_cycle.py                    # 정상 회차
  python scripts/run_cycle.py --force-collect     # 이번 회차에 수집 강제
  python scripts/run_cycle.py --force-report      # 이번 회차에 주간 리포트 강제
  python scripts/run_cycle.py --no-collect        # 가격체크만
  FORCE_COLLECT=true python scripts/run_cycle.py  # 워크플로 입력에서 주입
"""

import argparse
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

from config import settings
from storage import db

logger = logging.getLogger("alert.cycle")

META_LAST_COLLECT = "last_collect_at"
META_LAST_COLLECT_FAIL = "last_collect_fail_at"
META_LAST_REPORT = "last_weekly_report_at"
META_LAST_REPORT_FAIL = "last_weekly_report_fail_at"

KST = timezone(timedelta(hours=9))

# 수집 하드 타임아웃 — 잡 timeout(20분)보다 넉넉히 짧게 잡아, 수집이 멈춰도
# 커밋백 단계가 반드시 실행되도록 한다.
COLLECT_TIMEOUT_SEC = 12 * 60


def _meta_float(conn, key: str) -> float:
    try:
        return float(db.get_meta(conn, key) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _due(conn, now: float, ok_key: str, fail_key: str,
         interval_sec: float, retry_sec: float) -> tuple:
    """주기 판정 공통부. 반환 (실행여부, 사유)."""
    last_ok = _meta_float(conn, ok_key)
    last_fail = _meta_float(conn, fail_key)
    # 미래 시각(시계 역행/수동 편집)은 신뢰하지 않는다 — 영구 굶주림 방지.
    if last_ok > now:
        last_ok = 0.0
    if last_fail > now:
        last_fail = 0.0

    elapsed = now - last_ok
    if last_ok and elapsed < interval_sec:
        return False, f"주기 미도래({elapsed / 3600:.1f}h < {interval_sec / 3600:.1f}h)"
    if last_fail and (now - last_fail) < retry_sec:
        return False, f"직전 실패 백오프({(now - last_fail) / 60:.0f}분 < {retry_sec / 60:.0f}분)"
    if not last_ok:
        return True, "이력 없음(최초)"
    return True, f"주기 도래({elapsed / 3600:.1f}h 경과)"


def collect_due(conn, now: float, interval_sec: float, retry_sec: float) -> tuple:
    return _due(conn, now, META_LAST_COLLECT, META_LAST_COLLECT_FAIL, interval_sec, retry_sec)


def report_due(conn, now: float, interval_sec: float, retry_sec: float,
               hour_from: int = None, hour_to: int = None) -> tuple:
    """주간 리포트 판정. 주기 도래에 더해 '사람이 깨어있는 KST 시간대'만 발송한다
    (새벽 3시 리포트 방지 — 첫 발송도 이 창 안에서 일어난다)."""
    due, reason = _due(conn, now, META_LAST_REPORT, META_LAST_REPORT_FAIL,
                       interval_sec, retry_sec)
    if not due:
        return False, reason
    if hour_from is None:
        hour_from = settings.get("weekly_report_kst_hour_from")
    if hour_to is None:
        hour_to = settings.get("weekly_report_kst_hour_to")
    kst_hour = datetime.fromtimestamp(now, KST).hour
    if not (hour_from <= kst_hour < hour_to):
        return False, f"발송 시간대 대기(KST {kst_hour}시, 창 {hour_from}~{hour_to}시)"
    return True, reason


# ── 수집 ────────────────────────────────────────────────────────────

def _subprocess_collect(timeout_sec: float) -> None:
    """수집을 별도 프로세스로 실행. 실패/타임아웃은 예외로 올라온다."""
    subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "run_collect.py")],
        cwd=str(REPO_ROOT),
        check=True,
        timeout=timeout_sec,
    )


def _mark(db_path: str, ok_key: str, fail_key: str, now: float, success: bool) -> None:
    with db.connect(db_path) as conn:
        if success:
            db.set_meta(conn, ok_key, str(now))
            db.set_meta(conn, fail_key, "0")
        else:
            db.set_meta(conn, fail_key, str(now))


def maybe_collect(db_path: str, now: float = None, force: bool = False,
                  enabled: bool = True, collect_runner=None,
                  timeout_sec: float = COLLECT_TIMEOUT_SEC) -> str:
    """조건이 맞으면 수집을 1회 수행. 반환 "skipped" | "ok" | "failed".
    수집이 어떤 식으로 실패해도 예외를 밖으로 던지지 않는다(가격체크 보호)."""
    now = time.time() if now is None else now
    runner = collect_runner or _subprocess_collect

    if not enabled:
        logger.info("수집 생략: --no-collect")
        return "skipped"

    with db.connect(db_path) as conn:
        due, reason = collect_due(conn, now,
                                  settings.get("collect_interval_hours") * 3600,
                                  settings.get("collect_retry_minutes") * 60)
    if force:
        due, reason = True, "강제 실행(force)"
    if not due:
        logger.info("수집 생략: %s", reason)
        return "skipped"

    logger.info("수집 편입: %s", reason)
    t0 = time.time()
    try:
        runner(timeout_sec)
    except BaseException as e:  # noqa: BLE001 - 수집 실패가 회차를 죽이면 안 된다
        if isinstance(e, KeyboardInterrupt):
            raise
        logger.error("수집 실패(%.0f초, 나머지 단계는 계속): %s: %s",
                     time.time() - t0, type(e).__name__, e)
        _mark(db_path, META_LAST_COLLECT, META_LAST_COLLECT_FAIL, now, success=False)
        print(f"::warning::수집 실패 - {type(e).__name__} "
              f"({settings.get('collect_retry_minutes')}분 후 재시도, 알림은 정상 동작)")
        return "failed"

    _mark(db_path, META_LAST_COLLECT, META_LAST_COLLECT_FAIL, now, success=True)
    logger.info("수집 성공(%.0f초)", time.time() - t0)
    return "ok"


# ── 주간 리포트 ──────────────────────────────────────────────────────

def _default_report_runner():
    from scripts.run_weekly_report import send_report
    return send_report()


def maybe_weekly_report(db_path: str, now: float = None, force: bool = False,
                        enabled: bool = True, report_runner=None) -> str:
    """조건이 맞으면 주간 성적 리포트를 1회 발송. 반환 "skipped" | "ok" | "failed".
    발송 실패(텔레그램 오류/False 반환)는 성공으로 기록하지 않아 백오프 후 재시도된다."""
    now = time.time() if now is None else now
    if not enabled:
        return "skipped"

    with db.connect(db_path) as conn:
        due, reason = report_due(conn, now,
                                 settings.get("weekly_report_interval_hours") * 3600,
                                 settings.get("weekly_report_retry_minutes") * 60)
    if force:
        due, reason = True, "강제 발송(force)"
    if not due:
        logger.debug("주간 리포트 생략: %s", reason)
        return "skipped"

    logger.info("주간 리포트 발송: %s", reason)
    try:
        sent = (report_runner or _default_report_runner)()
    except BaseException as e:  # noqa: BLE001 - 리포트 실패가 회차를 죽이면 안 된다
        if isinstance(e, KeyboardInterrupt):
            raise
        logger.error("주간 리포트 실패: %s: %s", type(e).__name__, e)
        sent = False
    if not sent:
        _mark(db_path, META_LAST_REPORT, META_LAST_REPORT_FAIL, now, success=False)
        print(f"::warning::주간 리포트 발송 실패 - "
              f"{settings.get('weekly_report_retry_minutes')}분 후 재시도")
        return "failed"

    _mark(db_path, META_LAST_REPORT, META_LAST_REPORT_FAIL, now, success=True)
    return "ok"


# ── 회차 ────────────────────────────────────────────────────────────

def _default_price_runner():
    from monitor.price_check import run_once
    return run_once()


def run_cycle(now: float = None, force_collect: bool = False, force_report: bool = False,
              collect_enabled: bool = True, report_enabled: bool = True,
              collect_runner=None, price_runner=None, report_runner=None) -> dict:
    """1회차 = 가격체크 → (조건부 수집) → (조건부 주간 리포트). 반환 요약 dict.

    가격체크를 맨 앞에 두는 이유: 수집이 편입된 회차는 5~8분 걸리는데, 뒤에 두면
    그만큼 터치 알림이 늦어진다. 새로 수집된 레벨은 2분 뒤 다음 회차가 검사한다.
    """
    now = time.time() if now is None else now
    db_path = settings.get("db_path")
    db.init_db(db_path)

    price_status, price_summary = "ok", None
    try:
        price_summary = (price_runner or _default_price_runner)()
    except Exception as e:  # noqa: BLE001 - 요약을 남기고 종료코드로만 실패를 알린다
        price_status = "failed"
        logger.exception("가격체크 실패: %s", e)
        print(f"::error::가격체크 실패 - {type(e).__name__}")

    collect_status = maybe_collect(db_path, now=now, force=force_collect,
                                   enabled=collect_enabled, collect_runner=collect_runner)
    report_status = maybe_weekly_report(db_path, now=now, force=force_report,
                                        enabled=report_enabled, report_runner=report_runner)

    logger.info("회차 완료: 가격체크=%s 수집=%s 주간리포트=%s",
                price_status, collect_status, report_status)
    return {"price_check": price_status, "collect": collect_status,
            "weekly_report": report_status, "summary": price_summary}


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes", "on")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force-collect", action="store_true", help="주기와 무관하게 이번 회차 수집")
    ap.add_argument("--force-report", action="store_true", help="주기와 무관하게 주간 리포트 발송")
    ap.add_argument("--no-collect", action="store_true", help="수집 금지(가격체크만)")
    ap.add_argument("--no-report", action="store_true", help="주간 리포트 금지")
    args = ap.parse_args(argv)

    result = run_cycle(
        force_collect=args.force_collect or _env_flag("FORCE_COLLECT"),
        force_report=args.force_report or _env_flag("FORCE_REPORT"),
        collect_enabled=not args.no_collect,
        report_enabled=not args.no_report,
    )
    # 종료코드는 가격체크(=알림 파이프라인)만 반영한다. 수집/리포트 실패는 경고로 남기고
    # 잡을 성공 처리해야, 커밋백 단계가 정상 진행되고 알림이 멈추지 않는다.
    return 1 if result["price_check"] == "failed" else 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    sys.exit(main())
