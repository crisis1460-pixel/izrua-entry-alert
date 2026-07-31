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
from notify import telegram
from storage import db

logger = logging.getLogger("alert.cycle")

META_LAST_COLLECT = "last_collect_at"
META_LAST_COLLECT_FAIL = "last_collect_fail_at"
META_LAST_REPORT = "last_weekly_report_at"
META_LAST_REPORT_FAIL = "last_weekly_report_fail_at"

KST = timezone(timedelta(hours=9))


def _day_kst(now: float) -> str:
    return datetime.fromtimestamp(now, KST).strftime("%Y-%m-%d")


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

    # 2026-07-26 감사 minor 후속: 3·4단계에서 고친 것과 동일 결함류 - 주기 판정 조회도
    # DB 접근이라 잠금/디스크 오류로 실패할 수 있다. try 밖이면 예외가 run_cycle 전체
    # (1단계 가격체크 결과의 커밋백 포함)까지 끌고 내려가므로 동일하게 격리한다.
    try:
        with db.connect(db_path) as conn:
            due, reason = collect_due(conn, now,
                                      settings.get("collect_interval_hours") * 3600,
                                      settings.get("collect_retry_minutes") * 60)
    except BaseException as e:  # noqa: BLE001 - 수집 실패가 회차를 죽이면 안 된다
        if isinstance(e, KeyboardInterrupt):
            raise
        logger.error("수집 주기 판정 실패: %s: %s", type(e).__name__, e)
        print(f"::warning::수집 주기 판정 실패 - {type(e).__name__}")
        return "failed"

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
        # 실패 마킹도 DB 접근 - 여기서마저 실패하면 백오프가 기록되지 않아 다음 회차가
        # 즉시 재시도하게 될 뿐이니, 로그만 남기고 원래의 failed 반환을 계속 진행한다.
        try:
            _mark(db_path, META_LAST_COLLECT, META_LAST_COLLECT_FAIL, now, success=False)
        except BaseException as e2:  # noqa: BLE001 - meta 기록 실패로 회차를 죽이면 안 된다
            if isinstance(e2, KeyboardInterrupt):
                raise
            logger.error("수집 실패 meta 기록 실패(백오프 미기록): %s: %s",
                         type(e2).__name__, e2)
        print(f"::warning::수집 실패 - {type(e).__name__} "
              f"({settings.get('collect_retry_minutes')}분 후 재시도, 알림은 정상 동작)")
        return "failed"

    # 성공 마킹도 DB 접근 - 여기서 실패해도(잠금/디스크 등) 수집된 레벨은 이미 SQLite 에
    # 커밋돼 살아 있으니, 회차를 죽이지 않고 failed 로만 남긴다(다음 회차가 주기 재판정).
    try:
        _mark(db_path, META_LAST_COLLECT, META_LAST_COLLECT_FAIL, now, success=True)
    except BaseException as e:  # noqa: BLE001 - meta 기록 실패로 회차를 죽이면 안 된다
        if isinstance(e, KeyboardInterrupt):
            raise
        logger.error("수집 meta 기록 실패(수집 자체는 완료): %s: %s", type(e).__name__, e)
        print(f"::warning::수집 meta 기록 실패 - {type(e).__name__}")
        return "failed"

    logger.info("수집 성공(%.0f초)", time.time() - t0)
    return "ok"


# ── 수집 단계 정지(last_collect_at staleness) 감시 (2026-07-26 과제3 — 감사 minor) ──
#
# 기존 monitor/price_check.py 의 _check_collect_silence(읽기 전용, 이 파일에서 수정
# 안 함)는 "최근 collect_silence_window_hours 동안 신규 수집(levels.collected_at) 0건"을
# 본다 - 이건 결과물 신호라 '수집기는 살아있는데 마침 새 글이 없는' 정상 상황에도
# 위양성으로 울릴 수 있고, 반대로 '수집 단계 자체가 실행되지 않는'(meta.last_collect_at
# 이 갱신되지 않는) 고장은 못 잡는다 - collected_at 이 예전 값 그대로여도 창 밖이면
# 그냥 조용하다. 실제로 2026-07-23~26 rebase 오류로 수집분이 조용히 전량 폐기되는
# 3일 장애가 있었다 - 그 동안 last_collect_at 자체가 정체돼 있었을 것이므로, 이
# 감시는 결과물이 아니라 '수집 단계가 실행/성공했는가'를 직접 보는 구조적 신호다.
# 서로 다른 실패 모드를 잡으므로 기존 감시를 대체하지 않고 나란히 둔다.

META_COLLECT_STALE_WARNED = "collect_stale_warned_date"


def maybe_alert_collect_stale(db_path: str, now: float = None) -> str:
    """meta.last_collect_at 이 last_collect_stale_hours 이상 갱신되지 않으면 하루
    1회 경고. 반환 "skipped" | "ok" | "failed".

    신규 DB(수집을 한 번도 안 함 - last_collect_at 미존재)에서는 울리지 않는다:
    최초 수집 전은 고장이 아니라 정상 초기 상태라 여기서 경보를 내면 첫 회차부터
    오탐이 된다. 이 체크 자체도 다른 단계와 동일한 부분 실패 격리 원칙을 따른다 -
    DB 조회/발송/meta 기록 중 무엇이 실패해도 예외를 밖으로 던지지 않는다."""
    now = time.time() if now is None else now
    try:
        with db.connect(db_path) as conn:
            last = db.get_meta(conn, META_LAST_COLLECT)
            warned_day = db.get_meta(conn, META_COLLECT_STALE_WARNED)
    except BaseException as e:  # noqa: BLE001 - 감시 자체가 회차를 죽이면 안 된다
        if isinstance(e, KeyboardInterrupt):
            raise
        logger.error("수집 정체 감시 조회 실패: %s: %s", type(e).__name__, e)
        print(f"::warning::수집 정체 감시 조회 실패 - {type(e).__name__}")
        return "failed"

    # "0" 문자열(테스트 reset_meta 등의 리셋 센티널)도 "이력 없음"과 동일 취급해야
    # 하므로, 존재 여부가 아니라 다른 due 판정(_meta_float)과 동일하게 float 변환
    # 결과의 참거짓으로 판단한다 - 문자열 "0"은 참(존재)이지만 수치 0.0은 거짓이다.
    try:
        last_ts = float(last) if last else 0.0
    except (TypeError, ValueError):
        last_ts = 0.0  # 손상값도 "이력 없음"과 동일 취급 - 다음 성공 수집이 자연 치유
    if not last_ts:
        return "skipped"  # 신규 DB - 최초 수집 전 정상 상태(고장 아님)

    day = _day_kst(now)
    if warned_day == day:
        return "skipped"  # 오늘 이미 경고함 - 중복 방지(collect_silence 와 동일 패턴)

    threshold_hours = settings.get("last_collect_stale_hours")
    elapsed = now - last_ts
    if elapsed < threshold_hours * 3600:
        return "skipped"

    text = telegram.render_collect_stale_alert(elapsed / 3600, threshold_hours)
    # 2026-07-28 수리: meta→commit→send 순서로 통일(price_check.py 패턴 승계).
    # meta 를 먼저 쓰면 send 실패 시 오늘 경보를 한 번 건너뛰더라도 중복 발송이 없다.
    # send→meta 역순이면 meta 기록 실패 시 다음 회차에도 같은 경보가 반복된다.
    try:
        with db.connect(db_path) as conn:
            db.set_meta(conn, META_COLLECT_STALE_WARNED, day)
    except BaseException as e:  # noqa: BLE001 - meta 기록 실패로 회차를 죽이면 안 된다
        if isinstance(e, KeyboardInterrupt):
            raise
        logger.error("수집 정체 경고 meta 기록 실패(발송 생략): %s: %s", type(e).__name__, e)
        return "failed"

    try:
        sent = telegram.send(text)
    except BaseException as e:  # noqa: BLE001 - 발송 실패가 회차를 죽이면 안 된다
        if isinstance(e, KeyboardInterrupt):
            raise
        logger.error("수집 정체 경고 발송 실패(meta는 기록됨): %s: %s", type(e).__name__, e)
        sent = False
    if not sent:
        print("::warning::수집 정체 경고 발송 실패")
        return "failed"

    logger.warning("[정체감시] 수집 정체 경고 발송(%.1fh 미갱신)", elapsed / 3600)
    return "ok"


# ── 작성자 주간 스냅샷 (2026-07-26 사용자 결정 — 역신호 태깅 선행 인프라) ──
#
# 왜 별도 훅인가: 역신호 판정 규칙이 "n_eff>=5 이면서 E_LB<0 이 2주 연속"인데,
# 지금은 '이번 주 값'만 계산 가능하고 지난주를 알 방법이 없어 연속 판정이 불가능하다.
# 주 1회 찍어두면 나중에 판정 로직만 얹으면 된다. 주간 리포트 발송은 꺼졌지만(사용자
# 결정) 이 축적은 리포트와 독립적으로 계속 돌아야 하므로 별도 meta 키를 쓴다.
# **저장 전용** — 알림·필터·등급 어디에도 영향을 주지 않는다(관찰기 안전).

META_LAST_SNAPSHOT = "last_author_snapshot_at"


def take_author_snapshot(db_path: str, now: float = None) -> int:
    """작성자별 현재 지표를 그 주 스냅샷으로 저장. 반환: 저장한 작성자 수."""
    from analytics import ranking

    now = time.time() if now is None else now
    week = db.week_kst(now)
    saved = 0
    db.init_db(db_path)  # 단독 호출(수동 실행·테스트)에서도 테이블이 보장되게
    with db.connect(db_path) as conn:
        raw = db.get_author_raw_record(conn)  # {author: {"wins": w, "losses": l}}
        for author in db.list_authors_with_outcomes(conn):
            rows = db.get_author_outcome_rows(conn, author)
            if not rows:
                continue
            met = dict(ranking.author_metrics(rows, now))
            rec = raw.get(author) or {}
            met["wins"], met["losses"] = rec.get("wins"), rec.get("losses")
            db.save_author_snapshot(conn, week, author, met, now=now)
            saved += 1
    return saved


def maybe_author_snapshot(db_path: str, now: float = None, force: bool = False,
                          enabled: bool = True, snapshot_runner=None) -> str:
    """주 1회 작성자 스냅샷. 반환 "skipped" | "ok" | "failed".

    실패해도 회차를 죽이지 않는다(수집·리포트와 동일한 부분 실패 격리 원칙).
    리포트와 달리 발송이 없어 조용하다 — 사용자에게 어떤 알림도 가지 않는다."""
    now = time.time() if now is None else now
    if not enabled:
        return "skipped"

    # 2026-07-26 감사 minor 조치(과제1): meta 조회도 DB 접근이라 잠금/디스크 오류로
    # 실패할 수 있다 - try 밖에 있으면 여기서 난 예외가 run_cycle 전체(1단계 가격체크
    # 결과의 커밋백 포함)까지 끌고 내려간다. 다른 DB 접근과 동일하게 격리한다.
    try:
        with db.connect(db_path) as conn:
            last = db.get_meta(conn, META_LAST_SNAPSHOT)
    except BaseException as e:  # noqa: BLE001 - 스냅샷 실패가 회차를 죽이면 안 된다
        if isinstance(e, KeyboardInterrupt):
            raise
        logger.error("작성자 스냅샷 주기 판정 실패: %s: %s", type(e).__name__, e)
        print(f"::warning::작성자 스냅샷 주기 판정 실패 - {type(e).__name__}")
        return "failed"

    try:
        last_ts = float(last) if last else 0.0
    except (TypeError, ValueError):
        last_ts = 0.0
    if last_ts > now:          # 시계 역행 방어 (수집·리포트와 동일)
        last_ts = 0.0
    interval = settings.get("author_snapshot_interval_hours") * 3600
    if not force and last_ts and now - last_ts < interval:
        return "skipped"

    try:
        n = (snapshot_runner or take_author_snapshot)(db_path, now)
    except BaseException as e:  # noqa: BLE001 - 스냅샷 실패가 회차를 죽이면 안 된다
        if isinstance(e, KeyboardInterrupt):
            raise
        logger.error("작성자 스냅샷 실패: %s: %s", type(e).__name__, e)
        print(f"::warning::작성자 스냅샷 실패 - {type(e).__name__}")
        return "failed"

    # 스냅샷 저장(save_author_snapshot)은 이미 끝났다 - 여기서 meta 기록만 실패해도
    # 스냅샷 자체를 무효로 되돌리지 않는다(다음 회차가 재시도해도 덮어쓰기라 무해).
    try:
        with db.connect(db_path) as conn:
            db.set_meta(conn, META_LAST_SNAPSHOT, str(now))
    except BaseException as e:  # noqa: BLE001 - meta 기록 실패로 회차를 죽이면 안 된다
        if isinstance(e, KeyboardInterrupt):
            raise
        logger.error("작성자 스냅샷 meta 기록 실패(스냅샷 %d명 저장은 완료): %s: %s",
                     n, type(e).__name__, e)
        print(f"::warning::작성자 스냅샷 meta 기록 실패 - {type(e).__name__}")
        return "failed"

    logger.info("작성자 주간 스냅샷 저장: %d명 (%s)", n, db.week_kst(now))
    return "ok"


# ── 역신호 최종 판정 (S9, 2026-08-01 사용자 결정 Q1=B안/Q2=해제 있음) ──────
#
# 판정 규칙: 최근 2주 스냅샷이 모두 neff_r>=rank_min_neff 이고 E_LB<0 → 역신호 확정.
# 확정 시 텔레그램 경보 **1회**만 발송하고 알림 필터·발송량은 건드리지 않는다(B안).
# 2주 연속 회복(neff_r 게이트 통과 + E_LB>=0)이면 해제 + 해제 알림 1회(Q2).
# 표본 부족·게이트 미달이면 판정 불가 = 확정 상태 보수적 유지.
#
# 왜 스냅샷 훅 직후인가: 판정 원천이 주간 스냅샷이라, 스냅샷을 실제로 찍은 회차에만
# 돌면 부하가 없고(주 1회) 핫패스(2분 가격체크)와 완전히 분리된다(§7-2 예외 격리).
# 별도 잡·별도 라이터를 만들지 않는다 — 라이터는 여전히 run_cycle 단일 진입점(W1).
#
# 중복 방지(§7-2 M-A2): 확정 기록(meta reverse_confirmed_{author}=확정 주차)을
# **발송 전에 commit** 한다. 발송 실패는 로그만 남기고 유실 허용 — 역순이면 발송 후
# 크래시 시 다음 회차가 같은 경보를 중복 발송한다(중복 방지 > 유실 방지).


def _maybe_reverse_check(conn, now: float) -> dict:
    """스냅샷 보유 작성자 전체를 순회해 확정/해제를 판정·기록·발송.
    반환 {"confirmed": [...], "released": [...]} (로그·테스트용)."""
    from analytics import ranking

    min_neff = float(settings.get("rank_min_neff"))
    confirmed, released = [], []
    for author in db.list_snapshot_authors(conn):
        snaps = db.get_author_last_n_snapshots(conn, author, 2)
        key = db.reverse_confirm_key(author)
        already = db.get_meta(conn, key)  # 확정 주차 문자열, ""/None = 미확정
        if not already and ranking.is_confirmed_reverse(snaps, min_neff=min_neff):
            db.set_meta(conn, key, snaps[0]["week_kst"])
            conn.commit()  # 발송 전 확정 — 크래시해도 재발송 없음 (M-A2)
            _send_reverse_alert(telegram.render_reverse_confirm_alert(author, snaps),
                                "확정", author)
            confirmed.append(author)
        elif already and ranking.is_recovered_reverse(snaps, min_neff=min_neff):
            db.set_meta(conn, key, "")  # 빈 값 = 해제 (키 잔존은 이력 흔적)
            conn.commit()
            _send_reverse_alert(telegram.render_reverse_release_alert(author, snaps),
                                "해제", author)
            released.append(author)
    return {"confirmed": confirmed, "released": released}


def _send_reverse_alert(text: str, kind: str, author: str) -> None:
    """경보 1회 발송 — 실패는 로그만(유실 허용, 상태 기록은 이미 commit 됨)."""
    try:
        sent = telegram.send(text)
    except BaseException as e:  # noqa: BLE001 - 발송 실패가 회차를 죽이면 안 된다
        if isinstance(e, KeyboardInterrupt):
            raise
        logger.error("역신호 %s 경보 발송 예외(@%s, 기록은 유지): %s: %s",
                     kind, author, type(e).__name__, e)
        return
    if sent:
        logger.warning("[역신호] %s 경보 발송: @%s", kind, author)
    else:
        logger.error("역신호 %s 경보 발송 실패(@%s, 기록은 유지 — 유실 허용)",
                     kind, author)


def maybe_reverse_check(db_path: str, now: float = None, enabled: bool = True) -> str:
    """스냅샷을 실제로 찍은 회차에만(enabled=True) 역신호 확정/해제 검사.
    반환 "skipped" | "ok" | "failed". 어떤 실패도 회차를 죽이지 않는다."""
    now = time.time() if now is None else now
    if not enabled:
        return "skipped"
    try:
        with db.connect(db_path) as conn:
            res = _maybe_reverse_check(conn, now)
    except BaseException as e:  # noqa: BLE001 - 역신호 검사 실패가 회차를 죽이면 안 된다
        if isinstance(e, KeyboardInterrupt):
            raise
        logger.error("역신호 판정 실패: %s: %s", type(e).__name__, e)
        print(f"::warning::역신호 판정 실패 - {type(e).__name__}")
        return "failed"
    if res["confirmed"] or res["released"]:
        logger.info("역신호 판정: 확정 %s / 해제 %s", res["confirmed"], res["released"])
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

    # 2026-07-26 감사 minor 조치(과제1): 주기 판정 DB 조회도 try 안으로 - 아래
    # 발송/meta 기록과 동일한 격리 원칙(예외가 run_cycle 까지 전파되면 1단계
    # 가격체크 결과의 커밋백까지 연쇄로 죽을 수 있다).
    try:
        with db.connect(db_path) as conn:
            due, reason = report_due(conn, now,
                                     settings.get("weekly_report_interval_hours") * 3600,
                                     settings.get("weekly_report_retry_minutes") * 60)
    except BaseException as e:  # noqa: BLE001 - 리포트 실패가 회차를 죽이면 안 된다
        if isinstance(e, KeyboardInterrupt):
            raise
        logger.error("주간 리포트 주기 판정 실패: %s: %s", type(e).__name__, e)
        print(f"::warning::주간 리포트 주기 판정 실패 - {type(e).__name__}")
        return "failed"

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
        print(f"::warning::주간 리포트 발송 실패 - "
              f"{settings.get('weekly_report_retry_minutes')}분 후 재시도")

    # meta 기록도 DB 접근 - 여기서 실패해도(잠금/디스크 등) 위 발송 결과 자체는 이미
    # 확정됐으니 사이클을 죽이지 않고 failed 로만 남긴다(다음 회차가 재시도).
    try:
        _mark(db_path, META_LAST_REPORT, META_LAST_REPORT_FAIL, now, success=sent)
    except BaseException as e:  # noqa: BLE001 - meta 기록 실패로 회차를 죽이면 안 된다
        if isinstance(e, KeyboardInterrupt):
            raise
        logger.error("주간 리포트 meta 기록 실패: %s: %s", type(e).__name__, e)
        print(f"::warning::주간 리포트 meta 기록 실패 - {type(e).__name__}")
        return "failed"

    return "ok" if sent else "failed"


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
    except BaseException as e:  # noqa: BLE001 - 요약을 남기고 종료코드로만 실패를 알린다
        if isinstance(e, (KeyboardInterrupt, SystemExit)):
            raise
        price_status = "failed"
        logger.exception("가격체크 실패: %s", e)
        print(f"::error::가격체크 실패 - {type(e).__name__}")

    collect_status = maybe_collect(db_path, now=now, force=force_collect,
                                   enabled=collect_enabled, collect_runner=collect_runner)
    # 수집 정체(last_collect_at staleness) 감시(과제3) — maybe_collect 직후에 둬서
    # 이번 회차의 갱신 결과를 바로 반영한 상태로 판정한다.
    collect_stale_status = maybe_alert_collect_stale(db_path, now=now)
    # 작성자 주간 스냅샷 — 발송이 없어 조용하고, 리포트 ON/OFF 와 무관하게 계속 쌓인다
    snapshot_status = maybe_author_snapshot(db_path, now=now)
    # 역신호 확정/해제 판정(S9) — 스냅샷을 실제로 찍은 회차에만(주 1회) 돈다.
    # 평회차·스냅샷 실패 회차에는 검사 자체를 하지 않는다(원천이 안 갱신됐으므로).
    reverse_status = maybe_reverse_check(db_path, now=now,
                                         enabled=(snapshot_status == "ok"))

    # 자동 발송 스위치(settings.weekly_report_auto_send)가 꺼져 있으면 정기 발송을
    # 하지 않는다 — 2026-07-26 사용자 결정. --force-report 는 여전히 동작한다
    # (수동으로 지금 보고 싶을 때). 리포트가 쓰는 데이터·계산은 전부 그대로 쌓인다.
    report_status = maybe_weekly_report(
        db_path, now=now, force=force_report,
        enabled=report_enabled and (settings.get("weekly_report_auto_send") or force_report),
        report_runner=report_runner)

    logger.info("회차 완료: 가격체크=%s 수집=%s 수집정체감시=%s 스냅샷=%s 역신호=%s 주간리포트=%s",
                price_status, collect_status, collect_stale_status, snapshot_status,
                reverse_status, report_status)
    return {"price_check": price_status, "collect": collect_status,
            "collect_stale_alert": collect_stale_status,
            "author_snapshot": snapshot_status,
            "reverse_check": reverse_status,
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
