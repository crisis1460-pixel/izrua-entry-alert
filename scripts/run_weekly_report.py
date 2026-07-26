#!/usr/bin/env python3
"""
주간 성적 리포트 잡 엔트리포인트 (주 1회 예정 — cron-job.org → GitHub Actions,
다른 잡과 동일 패턴). 읽기 전용: DB(data/levels.db)를 조회만 하고 쓰지 않는다.

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


def main() -> int:
    db_path = settings.get("db_path")
    db.init_db(db_path)

    with db.connect(db_path) as conn:
        authors = db.list_authors_with_outcomes(conn)
        rows_by_author = {a: db.get_author_outcome_rows(conn, a) for a in authors}

    total_rows = sum(len(rows) for rows in rows_by_author.values())
    text = telegram.render_weekly_report(rows_by_author, now=time.time())
    ok = telegram.send(text)

    logger.info(
        "주간 리포트 %s: 작성자 %d명 / 종결 표본 %d건",
        "발송 완료" if ok else "발송 실패(로그만, 잡은 정상 종료)",
        len(rows_by_author), total_rows,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
