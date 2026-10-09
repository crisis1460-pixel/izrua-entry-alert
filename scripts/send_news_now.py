"""지금 시점 뉴스 메시지 수동 발송 (2026-09-29 대표 요청 "오늘 브리핑 기준 뉴스 있다면 작성해서 보내줘").

운영 DB(data/levels.db)를 **임시 복사본**으로 열어, 모닝 브리핑과 같은 경로로
  ① 텔레그램 채널 뉴스(run_collect 의 뉴스 분기와 같은 판정) + ② 영문 RSS 뉴스(_collect_rss_news)
를 복사본 큐에 넣고, build_brief_messages 의 뉴스 메시지만 발송한다. 운영 DB·커밋백 없음 —
내일 아침 브리핑의 소비·중복 판정에는 영향이 없다(같은 기사가 내일 다시 실릴 수 있음).

기본은 dry-run(출력만). --send 일 때만 텔레그램 발송(워크플로 news-now.yml).
"""

import argparse
import json
import logging
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import settings  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("news_now")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--send", action="store_true")
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp()) / "levels.db"
    shutil.copyfile(ROOT / "data" / "levels.db", tmp)
    settings.SETTINGS["db_path"] = str(tmp)

    from collector import telegram_source
    from collector.extractor import parse_setup
    from notify import morning_brief, news_brief, telegram
    from scripts import run_collect

    uni = json.loads((ROOT / "data" / "universe.json").read_text(encoding="utf-8"))["universe"]
    known = [u["symbol"] for u in uni]
    conn = sqlite3.connect(str(tmp))
    conn.row_factory = sqlite3.Row
    timeout = float(settings.get("http_timeout_sec") or 10.0)

    # ① 텔레그램 채널 — run_collect._collect_telegram 의 뉴스 분기와 같은 판정(셋업 수집은 안 함)
    for i, ch in enumerate(settings.get("telegram_source_channels") or []):
        if i:
            time.sleep(settings.get("telegram_source_sleep_sec") or 5.0)
        for post in telegram_source.fetch_posts(ch, timeout,
                                                max_age_hours=settings.get("max_post_age_hours"),
                                                max_posts=settings.get("telegram_source_max_posts")):
            try:
                text = f"{post.get('title') or ''}\n{post.get('description') or ''}"
                sym = telegram_source.match_symbol(text, known)
                if not sym:
                    news_brief.maybe_send_unmatched_news(conn, post, ch, uni)
                elif not parse_setup(text, current_price=None):
                    news_brief.maybe_send_news_brief(conn, post, sym, ch)
            except Exception as e:  # noqa: BLE001
                logger.warning("[news_now] %s 글 처리 실패(무시): %s", ch, e)
        conn.commit()
    # ② 영문 RSS
    run_collect._collect_rss_news(conn, uni, timeout)
    conn.commit()

    msgs = morning_brief.build_brief_messages(conn, time.time(), timeout)
    news = [t for t, _ids in msgs if "주요 뉴스" in t]
    if not news:
        logger.info("[news_now] 조건을 통과한 뉴스 없음 — 발송 없음")
        print("NO_NEWS")
        return 0
    stamp = time.strftime("%m-%d %H:%M", time.gmtime(time.time() + 9 * 3600))
    # 수시 시각은 헤더 보조 줄 맨 앞 조각으로(10-10: '주요 뉴스' 뒤 줄내림·열 맞춤, 고아 단어 없음).
    old_parts = morning_brief.news_head_parts(news[0])
    old_head = morning_brief.news_head(old_parts)
    news[0] = news[0].replace(old_head, morning_brief.news_head([f"{stamp} 수시"] + old_parts), 1)
    for t in news:
        print(t)
        print("-----")
    if not args.send:
        return 0
    ok = True
    for t in news:
        ok = bool(telegram.send(t, urgency="low")) and ok
    print("발송 결과:", "성공" if ok else "실패")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
