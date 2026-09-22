#!/usr/bin/env python3
"""
주간 성적 리포트 — 로직 본체 + 수동 실행 엔트리포인트.

정기 발송은 2026-07-26 구조 개선으로 **가격체크 회차(scripts/run_cycle.py)가 흡수**했다
(meta 의 last_weekly_report_at 을 보고 7일마다 send_report() 호출). 외부 크론 추가 등록이
필요 없다. 이 파일을 직접 실행하는 건 "지금 당장 리포트를 보고 싶을 때"의 수동 경로다
(주기 meta 를 건드리지 않으므로 정기 발송 일정에는 영향이 없다).

읽기 전용: DB(data/levels.db)를 조회만 하고 쓰지 않는다.

로직: analytics/ranking.py(순수 수학, E_LB/수축 승률) + notify/telegram.py
(render_weekly_report, 톤 렌더링) 참고.
"""

import logging
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

from analytics import calibration, weekly
from config import settings
from notify import telegram
from storage import audit_dump, db

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("alert.weekly_report")

DAY = 86400.0

# 캘리브레이션 표본 우선순위 (2026-09-22 R4): 최신 산식부터 보고, 종결 표본이 0 이면
# 한 단계 아래 버전으로 내려간다. 구 산식 병기는 제거 — 신·구 등급은 의미가 달라
# 한 리포트에 두 표를 놓으면 읽는 사람이 어느 쪽을 보는지 모른다.
_CAL_VER_FALLBACK = ("v6", "v5")


def _calibration_latest(conn):
    """(결과, 사용한 버전). 표본이 있는 첫 버전을 쓴다. 구세대 스키마면 (None, None)."""
    vers = [settings.get("grade_formula_ver")] + [
        v for v in _CAL_VER_FALLBACK if v != settings.get("grade_formula_ver")]
    for ver in vers:
        if not ver:
            continue
        try:
            rows = db.get_weekly_calibration_rows(conn, ver)
        except sqlite3.OperationalError:
            return None, None
        if rows:
            return calibration.calibrate_grades(rows), ver
    return None, None


def _milestones(conn, now: float) -> list:
    """④ 다음 판단 — 표본 도달 마일스톤 2개(고정).

    카운트 정의는 의도적으로 단순하다: `touched_at >= 기준시각 AND outcome IS NOT
    NULL`. "공정 종결"(tp_only 를 TP1 대칭 가상손절로 재판정한 표본)은 정의가
    복잡해 리포트에 싣기 어렵고, 여기서 필요한 건 진행률뿐이다.
    속도는 최근 30일 실적(같은 정의)으로 잡는다."""
    out = []
    for key, label, target in (
        ("grade_v6_since", "v6 지연감점 평가", settings.get("weekly_report_milestone_v6")),
        ("mfe_mae_fixed_since", "MFE/MAE e-ratio", settings.get("weekly_report_milestone_mfe")),
    ):
        since = db.meta_float(conn, key)
        if not since:
            continue
        count = db.count_resolved_touches_since(conn, since)
        recent = db.count_resolved_touches_since(conn, max(since, now - 30 * DAY))
        span_days = max(min(30.0, (now - since) / DAY), 1.0)
        m = weekly.milestone(count, target, per_day=recent / span_days)
        m["label"] = label
        out.append(m)
    return out


def send_report(db_path: str = None, now: float = None) -> bool:
    """리포트를 조립해 1회 발송한다. 반환: 발송 성공 여부.

    run_cycle 이 주기 판정 후 호출하는 진입점이기도 하다. 성공/실패를 bool 로 돌려주는
    이유는, 실패를 '발송 완료'로 기록해 그 주 리포트를 통째로 날리지 않기 위해서다
    (실패 시 run_cycle 이 백오프 후 재시도한다)."""
    db_path = db_path or settings.get("db_path")
    now = time.time() if now is None else now
    db.init_db(db_path)

    text, meta = build_report(db_path, now)
    ok = telegram.send(text)

    logger.info(
        "주간 리포트 %s: 작성자 %d명 / 이번 주 종결 %d건 / 알림 %s건 / 길이 %d자",
        "발송 완료" if ok else "발송 실패(백오프 후 재시도)",
        meta["authors"], meta["closed"], meta["alerts"], len(text),
    )
    return bool(ok)


def _collect(conn, now: float, pool_days: float) -> dict:
    """리포트 조립에 필요한 원천 데이터 일괄 조회 — **SELECT 만 한다**."""
    authors = db.list_authors_with_outcomes(conn)
    wk_start = now - 7 * DAY
    pv_start = now - 14 * DAY
    return {
        "rows_by_author": {a: db.get_author_outcome_rows(conn, a) for a in authors},
        # 등급 캘리브레이션 — grade_ver 최신 표본만(구 산식 병기 제거, 2026-09-22 R4)
        "calibration": _calibration_latest(conn),
        # 역신호 확정 구분(S9, 표시 전용) — run_cycle 스냅샷 훅이 meta 에 기록한
        # 확정 상태를 안내 한 줄로만 병기한다(정렬·수식·필터 불변).
        "reverse_confirmed": db.get_reverse_confirmed_authors(conn),
        # ── v2 창 데이터 ──
        "wk_start": wk_start,
        "cur_rows": db.get_resolved_rows_between(conn, wk_start, now),
        "prev_rows": db.get_resolved_rows_between(conn, pv_start, wk_start),
        "pool_rows": db.get_resolved_rows_between(conn, now - pool_days * DAY, now),
        "cur_alerts": db.count_touch_alerts_between(conn, wk_start, now),
        "prev_alerts": db.count_touch_alerts_between(conn, pv_start, wk_start),
        "milestones": _milestones(conn, now),
    }


def build_report(db_path: str = None, now: float = None, conn=None) -> tuple:
    """(리포트 텍스트, 로그용 메타) — 발송은 하지 않는다. DB 는 **읽기만** 한다.

    send_report 에서 분리한 이유: 샘플 렌더링·미리보기가 send() 를 거치지 않고
    같은 조립 경로를 탈 수 있어야 한다(발송 사고 방지).
    conn 을 주면 그 연결을 그대로 쓴다 — 운영 DB 를 `mode=ro` URI 로 열어 샘플을
    뽑을 때 쓰는 경로다(db.connect 는 WAL 설정 등 쓰기를 동반하므로 부적합)."""
    db_path = db_path or settings.get("db_path")
    now = time.time() if now is None else now
    pool_days = settings.get("weekly_report_pool_days")

    if conn is not None:
        d = _collect(conn, now, pool_days)
    else:
        with db.connect(db_path) as c:
            d = _collect(c, now, pool_days)

    calibration_result, calibration_ver = d["calibration"]
    current = weekly.summary(d["cur_rows"], alerts=d["cur_alerts"])
    previous = weekly.summary(d["prev_rows"], alerts=d["prev_alerts"])
    obs = weekly.observations(d["cur_rows"], d["prev_rows"], d["pool_rows"],
                              limit=settings.get("weekly_report_observations"),
                              min_n=settings.get("weekly_report_min_n"))
    stats = weekly.outcome_stats(d["pool_rows"])

    text = telegram.render_weekly_report(
        d["rows_by_author"], now=now,
        calibration_result=calibration_result, calibration_ver=calibration_ver,
        reverse_confirmed=d["reverse_confirmed"],
        current=current, previous=previous, observations=obs,
        pool_n=stats["total"], pool_days=pool_days,
        milestones=d["milestones"], outcome_stats=stats,
        period=(d["wk_start"], now))
    return text, {"authors": len(d["rows_by_author"]), "closed": current["closed"],
                  "alerts": d["cur_alerts"], "pool_n": stats["total"]}


def main() -> int:
    # 이 잡(.github/workflows/weekly-report.yml)은 **읽기 전용**이라 data/ 를 커밋백하지
    # 않는다. 여기서 주간 감사 덤프(카드 #4)가 돌면 파일은 러너와 함께 사라지는데 주기
    # meta(last_audit_dump_at)만 앞당겨져, 정작 라이터 회차(price-check.yml)가 그 주
    # 덤프를 건너뛴다 = 그 주 감사 기록이 통째로 빈다. 그래서 명시적으로 끈다.
    # (run_cycle 이 send_report() 를 직접 부르는 경로는 이미 회차 시작 시 init_db 에서
    #  덤프 기회를 지났으므로 영향이 없다.)
    audit_dump.SUPPRESSED = True
    send_report()
    return 0  # 수동 실행은 발송 실패해도 잡을 빨갛게 만들지 않는다(로그로 확인)


if __name__ == "__main__":
    sys.exit(main())
