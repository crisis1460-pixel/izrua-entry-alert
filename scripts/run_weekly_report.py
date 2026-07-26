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
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

from analytics import calibration, clustering
from config import settings
from notify import telegram
from scripts.show_status import fetch_calibration_rows
from storage import audit_dump, db

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("alert.weekly_report")


def send_report(db_path: str = None, now: float = None) -> bool:
    """리포트를 조립해 1회 발송한다. 반환: 발송 성공 여부.

    run_cycle 이 주기 판정 후 호출하는 진입점이기도 하다. 성공/실패를 bool 로 돌려주는
    이유는, 실패를 '발송 완료'로 기록해 그 주 리포트를 통째로 날리지 않기 위해서다
    (실패 시 run_cycle 이 백오프 후 재시도한다)."""
    db_path = db_path or settings.get("db_path")
    now = time.time() if now is None else now
    db.init_db(db_path)

    with db.connect(db_path) as conn:
        authors = db.list_authors_with_outcomes(conn)
        rows_by_author = {a: db.get_author_outcome_rows(conn, a) for a in authors}
        # 초과 적중률 베이스라인 (2026-07-26 카드 B안): 이미 기록 중인 ret_24h 재사용
        # — 추가 API 호출 0. 표본 미달이면 렌더러가 섹션을 통째로 생략한다.
        baseline = clustering.baseline_positive_rate(db.get_ret24_values(conn))
        raw_records = db.get_author_raw_record(conn)
        # 합의(confluence): 터치 이력 전체에 price_check 와 동일한 병합 규칙을 재적용.
        # 표시 전용 — E_LB·정렬 키에는 들어가지 않는다.
        confluence = clustering.confluence_by_author(
            db.get_touched_levels_for_clusters(conn),
            settings.get("cluster_band_pct"),
            window_sec=settings.get("confluence_window_hours") * 3600,
        )
        # 등급 캘리브레이션(기획 카드 #26): 등급별 실측 TP1 도달률 + Wilson CI.
        # 순수 로컬 연산(외부 API 0). SQL 은 show_status 에 한 벌만 둔다 —
        # storage/db.py 는 동시 작업 중이라 손대지 않는다.
        calibration_result = calibration.calibrate_grades(fetch_calibration_rows(conn))

    total_rows = sum(len(rows) for rows in rows_by_author.values())
    text = telegram.render_weekly_report(rows_by_author, now=now, baseline=baseline,
                                         raw_records=raw_records, confluence=confluence,
                                         calibration_result=calibration_result)
    ok = telegram.send(text)

    logger.info(
        "주간 리포트 %s: 작성자 %d명 / 종결 표본 %d건 / 베이스라인 %s / 합의 작성자 %d명",
        "발송 완료" if ok else "발송 실패(백오프 후 재시도)",
        len(rows_by_author), total_rows,
        f"{baseline['rate'] * 100:.0f}%(n={baseline['n']})" if baseline["rate"] is not None
        else "표본없음",
        len(confluence),
    )
    return bool(ok)


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
