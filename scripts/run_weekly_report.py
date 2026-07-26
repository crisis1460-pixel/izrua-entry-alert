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

from config import settings
from notify import telegram
from storage import db

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

    total_rows = sum(len(rows) for rows in rows_by_author.values())
    text = telegram.render_weekly_report(rows_by_author, now=now)
    ok = telegram.send(text)

    logger.info(
        "주간 리포트 %s: 작성자 %d명 / 종결 표본 %d건",
        "발송 완료" if ok else "발송 실패(백오프 후 재시도)",
        len(rows_by_author), total_rows,
    )
    return bool(ok)


def main() -> int:
    send_report()
    return 0  # 수동 실행은 발송 실패해도 잡을 빨갛게 만들지 않는다(로그로 확인)


if __name__ == "__main__":
    sys.exit(main())
