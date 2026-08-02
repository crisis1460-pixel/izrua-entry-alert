#!/usr/bin/env python3
"""
수집 잡 엔트리포인트 (4시간마다 — cron-job.org → GitHub Actions).

흐름: 유니버스(top200∩업비트KRW) → 심볼별 TradingView 아이디어 → entry 추출
→ 등급 산정 → 레벨 DB 저장. entry 있는 글은 전부 저장(알림 필터는 가격체크 잡 담당).

사용:
  python scripts/run_collect.py                  # 전체 유니버스
  python scripts/run_collect.py --symbols BTC,LINK   # 지정 심볼만 (스모크 테스트)
  python scripts/run_collect.py --limit 5        # 상위 N개만
"""

import argparse
import json
import logging
import random
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

from collector import coingecko, telegram_source, tradingview, watcher_stats
from collector.extractor import judgment_window_hours, parse_setup, parse_timeframe_hours
from collector.grading import calculate_grade
from config import settings
from notify import telegram
from storage import db

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("alert.collect")

_KST = timezone(timedelta(hours=9))


def _day_kst(now: float) -> str:
    return datetime.fromtimestamp(now, tz=_KST).strftime("%Y-%m-%d")


# ── 글 삭제 감지 (2026-07-26, ACCURACY_DB_PLAN 안티게이밍) ──────────────
# 하루 총 요청 캡 (2026-07-27, 개발자B, 교차감사 m-A6 확정) ───────────────
# 왜 '게이트'가 아니라 '요청 수'를 세는가:
#   진전 게이트(last_deletion_check_day - n_checked>0 일 때만 set, 아래 함수 하단
#   주석 참고)는 "그날 최소 1건이라도 확정 판정이 나왔는가"만 본다. 전원 판정
#   보류(None)면 게이트가 안 걸려 다음 "수집 회차"(4시간마다, 하루 최대 6회)마다
#   재시도한다 - 그런데 그 재시도 한 번마다 최대 deletion_check_daily_limit(5)건의
#   실제 TradingView 요청을 태운다. 하루 예산 산식이 원래 "1회·최대 5요청"이었던
#   전제가 "6회차 × 5요청 = 최대 30요청/일"로 불어난다(수집 루프 앞단에 있어 403
#   기아를 앞당길 수 있다는 것도 양측 확증). 게이트를 "요청 성공 여부"가 아니라
#   "요청을 시도했는가"로 되돌리면 m-A2(원래 회귀 - 전원 보류 시 영구 0건)가
#   재발하므로, 게이트는 그대로 두고 완전히 별도 축인 '요청 총량'만 하루 단위로
#   하드 캡한다(설정값이 아니라 모듈 상수 - 사용자가 설정을 올려도 이 캡은
#   못 넘게 하는 것이 목적이라 settings 로 노출하지 않는다).
_DELETION_CHECK_REQ_COUNT_KEY_PREFIX = "deletion_check_req_count_"
_DELETION_CHECK_DAILY_REQUEST_CAP = 10  # 6회차×5요청(=30) 시나리오를 이 아래로 강제
_DELETION_CHECK_REQ_META_KEEP_DAYS = 3  # 이 캡은 '오늘'만 보므로 며칠치만 남기면 충분


def _check_deletions(conn, timeout: float) -> int:
    """종결된 레벨의 post_url 생존을 하루 상한만큼 확인. 반환: 삭제 확정 건수.
    하루 1회만 수행(수집이 4시간마다 도는 것과 별개로 - meta 로 날짜 게이트),
    확인 건수는 settings.deletion_check_daily_limit 로 제한한다(비용 방어).

    2026-07-27 수리(개발자B, 교차감사 m-A6 확정): 위 '진전 게이트'만으로는 전원
    판정 보류(None)가 이어지는 날 하루 최대 6회차 × 5요청 = 30요청까지 재시도가
    증폭될 수 있다(모듈 상수 설명 참고). 여기서는 그 재시도들이 하루 총
    _DELETION_CHECK_DAILY_REQUEST_CAP 건을 넘지 않도록 별도로 카운트·차단한다."""
    now = time.time()
    day = _day_kst(now)
    if db.get_meta(conn, "last_deletion_check_day") == day:
        return 0

    req_count_key = _DELETION_CHECK_REQ_COUNT_KEY_PREFIX + day
    used_today = int(db.get_meta(conn, req_count_key, "0") or "0")
    remaining_budget = _DELETION_CHECK_DAILY_REQUEST_CAP - used_today
    if remaining_budget <= 0:
        logger.info("[삭제확인] 하루 요청 캡(%d) 소진 - 오늘은 더 시도하지 않음(진전 게이트와 별개)",
                    _DELETION_CHECK_DAILY_REQUEST_CAP)
        return 0

    limit = min(settings.get("deletion_check_daily_limit"), remaining_budget)
    recheck_sec = settings.get("deletion_recheck_after_days") * 86400
    candidates = db.get_deletion_check_candidates(conn, limit, recheck_sec)
    if not candidates:
        db.set_meta(conn, "last_deletion_check_day", day)
        return 0
    n_deleted = n_checked = n_requests = 0
    blocked = False
    for i, cand in enumerate(candidates):
        if tradingview.is_blocked():
            logger.warning("[삭제확인] 차단 쿨다운 감지 - 남은 %d건 다음 회차로 연기",
                           len(candidates) - i)
            blocked = True
            break
        if i > 0:
            time.sleep(1.0)  # 상세 방문과 동일한 페이싱 원칙(모듈 sleep 계약 준수)
        result = tradingview.check_post_deleted(cand["post_url"], timeout)
        n_requests += 1  # 결과가 None 이어도 실제 요청은 나갔으므로 캡에는 반영한다
        if result is None:
            continue  # 판정 보류 - deleted_checked_at 갱신 안 함, 다음 순번에 재확인
        db.mark_deletion_checked(conn, cand["id"], result, now)
        n_checked += 1
        if result:
            n_deleted += 1
            logger.info("[삭제확인] 삭제 확정: id=%s author=%s", cand["id"], cand.get("author"))
    if n_requests:
        db.set_meta(conn, req_count_key, str(used_today + n_requests))
    # 2026-07-26 실전 버그 수리: 차단으로 중도 break 했을 때도 게이트를 set 해버려서,
    # "0건 확인"인데 하루 게이트가 소진되고 후보(그날 26건)가 통째로 다음날로
    # 밀리는 사고가 있었다. 로그 문구("다음 회차로 연기")대로, 차단 시엔 게이트를
    # set 하지 않는다 - 정상 순회 완료(또는 애초에 후보 없음)일 때만 하루 게이트 소진.
    #
    # 2026-07-27 수리(개발자B, 교차감사 A-M2 확정): 위 수정만으로는 부족했다 - 결과가
    # 전부 None(판정 보류, tradingview.check_post_deleted 가 "모르겠다"고 응답)이면
    # 차단이 아니어도(blocked=False) 정상 순회를 '완료'한 것으로 처리돼 게이트가
    # 소진됐다. 그런데 None 은 deleted_checked_at 을 갱신 안 하므로(위 continue 참고)
    # 후보 정렬(get_deletion_check_candidates 의 "미확인 우선, collected_at ASC")이
    # 결정적이라 다음날도 '같은' 후보 5건이 다시 뽑히고 또 전부 None 이면 - 게이트만
    # 매일 태워지고 실제 확인은 영원히 0건이었다(3일째 실적 0건의 근본 원인).
    # 이제 "진전이 있었을 때만"(n_checked > 0) 게이트를 태운다 - 후보가 아예 없던
    # 경우는 위에서 이미 별도로 처리·반환했으므로 여기 도달했다는 것 자체가
    # "후보는 있었다"는 뜻이라 원래 기준(n_checked > 0 or not candidates)이 자연히
    # n_checked > 0 하나로 좁혀진다.
    # 전원 None 이어도 게이트를 안 태우면 다음 "수집 회차"(cron 4시간 간격)가 바로
    # 재시도한다 - 무한 재시도가 아니라 하루 최대 6회(24h / 4h)로 유계돼 있다.
    if not blocked and n_checked > 0:
        db.set_meta(conn, "last_deletion_check_day", day)
    if n_checked:
        logger.info("[삭제확인] %d건 확인 (삭제 %d건)", n_checked, n_deleted)
    return n_deleted


# ── TradingView 확정 차단 즉시 알림 (2026-07-26 과제2) ─────────────────
# 날짜별 카운터 meta 키 접두사 - 경보 발송(_maybe_alert_block)과 정리
# (_prune_tv_block_alert_meta) 양쪽에서 공유해 접두사가 어긋날 일이 없게 한다.
_TV_BLOCK_ALERT_KEY_PREFIX = "tv_block_alert_count_"

# 차단 쿨다운 만료 epoch 영속화 키 (2026-07-28) — 모듈 전역 변수는 회차(프로세스)를
# 못 넘어 "30분 쿨다운"이 지켜지지 않았다. collector.tradingview.restore_block_state 참고.
_TV_BLOCKED_UNTIL_KEY = "tv_blocked_until"
_TG_BLOCKED_UNTIL_KEY = "tg_blocked_until"  # 2026-07-28 수리: telegram_source 대칭 영속화

# 수집 순환 재개 지점(원본 유니버스 기준 절대 인덱스) — 차단 기아 방지 (2026-07-27)
_UNIVERSE_OFFSET_META = "collect_universe_offset"


def _save_universe_offset(conn, offset: int, done_i: int, total: int) -> None:
    """수집 루프의 재개 지점을 '원본 유니버스 기준 절대 인덱스'로 meta 에 기록한다.

    2026-07-27 교차감사 M1: 예전엔 이 저장이 루프 '뒤'에만 있어서, 하드킬
    (run_cycle.py 의 subprocess timeout 12분)로 프로세스가 죽으면 저장 자체가
    실행되지 않았고 - 설령 실행됐어도 회차 전체가 한 트랜잭션이라 함께 롤백됐다.
    그래서 다음 회차가 같은 지점을 또 돌았다. 이제 중간 커밋마다 함께 기록한다.

    done_i = 회전된 목록에서 '방금 처리를 마친' 심볼의 인덱스 → 재개는 그 다음부터.
    한 바퀴를 다 돌았으면(done_i+1 == total) 0 — 루프 뒤의 완주 처리와 같은 값이라
    "중간 커밋이 마지막 심볼에 걸린 회차"만 재개 지점이 달라지는 모순이 안 생긴다.
    """
    nxt = done_i + 1
    value = "0" if nxt >= total else str((offset + nxt) % total)
    db.set_meta(conn, _UNIVERSE_OFFSET_META, value)


def _maybe_alert_block(conn, now: float) -> None:
    """이번 수집 주기 중 확정 차단(403/429/캡차/1020)이 감지됐으면 하루 상한 내에서
    즉시 경고. 수집 급감 경고(24시간 뒤에야 울림)보다 빠른 신호 - 단 사용자가
    알림 과다를 싫어하므로 하루 1회(tv_block_alert_daily_limit=1)로 강하게 억제."""
    reason = tradingview.hard_block_detected()
    if reason is None:
        return
    day = _day_kst(now)
    limit = settings.get("tv_block_alert_daily_limit")
    if limit <= 0:
        return
    sent_today = int(db.get_meta(conn, _TV_BLOCK_ALERT_KEY_PREFIX + day, "0") or "0")
    if sent_today >= limit:
        return
    text = telegram.render_tv_block_alert(reason)
    if telegram.send(text):
        db.set_meta(conn, _TV_BLOCK_ALERT_KEY_PREFIX + day, str(sent_today + 1))
        logger.warning("[차단경보] 확정 차단(%s) 감지 - 경고 발송(오늘 %d/%d회)",
                       reason, sent_today + 1, limit)


def _prune_daily_counter_meta_by_prefix(conn, prefix: str, day: str, keep_days: int) -> int:
    """접두사_YYYY-MM-DD 형태 날짜별 카운터 meta 키 정리 공통 구현(내부 헬퍼).

    LIKE 대신 substr 두 번으로 접두사를 자른다 - 접두사 안에 '_' 가 있어 LIKE
    패턴으로 쓰면 SQL 와일드카드(임의 1글자)로 해석돼 의도보다 헐겁게 매칭된다.
    남은 부분은 ISO 날짜(YYYY-MM-DD)라 사전식 문자열 비교가 곧 시간순 비교와
    같다(storage.db.prune_daily_stats 와 동일한 컷오프 계산 방식). 반환: 삭제한
    키 개수."""
    cutoff = (datetime.fromisoformat(day) - timedelta(days=keep_days)).strftime("%Y-%m-%d")
    prefix_len = len(prefix)
    cur = conn.execute(
        "DELETE FROM meta WHERE substr(key, 1, ?) = ? AND substr(key, ?) < ?",
        (prefix_len, prefix, prefix_len + 1, cutoff),
    )
    return cur.rowcount


def _prune_tv_block_alert_meta(conn, day: str, keep_days: int) -> int:
    """날짜별 차단경보 카운터(tv_block_alert_count_YYYY-MM-DD) meta 키 정리
    (2026-07-26 과제2 - 감사 minor). 날짜가 지나도 옛 키가 안 지워지면 meta
    테이블이 무한 증가한다. storage/db.py 에 범용 prune 헬퍼를 새로 얹지 않고
    (개발자 A 병렬 작업 중 - 파일 경계 원칙) 이 파일 안에서 직접 SQL로 해결한다.

    2026-07-27 - 삭제확인 요청캡(_DELETION_CHECK_REQ_COUNT_KEY_PREFIX) 정리에도
    같은 SQL 이 필요해져 공통부를 _prune_daily_counter_meta_by_prefix 로 뽑았다.
    이 함수는 그 얇은 래퍼로 남긴다 - scripts/test_cycle.py(다른 세션 소유)가 이
    이름·시그니처(conn, day, keep_days)를 직접 호출하므로 변경 금지. 매 수집
    회차 가볍게 수행. 반환: 삭제한 키 개수."""
    return _prune_daily_counter_meta_by_prefix(conn, _TV_BLOCK_ALERT_KEY_PREFIX, day, keep_days)


# ── 글 1건 → 레벨 저장 (입력원 공통 경로) ──────────────────────────────
def _ingest_idea(conn, coin: dict, idea: dict, author_stats: dict, timeout: float,
                 source: str = "tradingview", lookup_followers: bool = True):
    """글 1건을 파싱→등급→저장까지 처리. 반환 (셋업 있었나, 신규 저장인가).

    2026-07-26 수리: 글 1건의 파싱/등급/저장 오류가 사이클 전체 커밋을 굴리지 못하게
    격리 - collector/tradingview.py _items_to_ideas 의 아이템별 격리 원칙을 하류
    (파싱~저장)까지 확장한다. 실패 건은 로그만 남기고 다음 아이디어로 계속.

    2026-07-27 카드 #14: 텔레그램 소스가 같은 하류(파서→등급→클러스터→적중DB)를
    무수정 재사용하도록 이 함수로 뽑았다. 입력원별로 갈리는 건 딱 둘 —
      · source: levels.source 에 남겨 사후에 "어느 소스가 잘 맞나"를 가른다.
      · lookup_followers: 팔로워 조회는 TradingView 프로필 페이지 전용이라
        텔레그램 채널 작성자에 대고 부르면 무의미한 TV 요청(=차단 위험)만 늘어난다.
    """
    try:
        text = f"{idea['title']}\n{idea['description']}"
        setup = parse_setup(text, current_price=coin.get("price_usd"))
        if not setup or not setup.get("entry"):
            return False, False
        stats_row = author_stats.get(idea.get("author") or "", {})
        followers = stats_row.get("followers") or idea.get("author_followers")
        if followers is None and lookup_followers and idea.get("author"):
            followers = tradingview.fetch_author_followers(idea["author"], timeout)
        # 작성자 실적 가점 (2026-08-01 S10 v3 안2) — 자기 DB 종결 실적을 채점 직전
        # 조회해 전달. 콜드스타트(n<5)는 grading 쪽 게이트가 0점(중립) 처리.
        closed_n, closed_hits = db.author_closed_stats(conn, idea.get("author"))
        grade, score, rr = calculate_grade(
            followers, setup["direction"], setup["entry"],
            setup.get("sl"), setup.get("tp"), coin.get("price_usd"),
            author_closed_n=closed_n, author_closed_hits=closed_hits,
        )
        tf_hours = parse_timeframe_hours(text)
        level = {
            "signal_key": db.make_signal_key(
                coin["symbol"], setup["entry"], idea.get("author"), idea.get("url"),
                source=source),
            "judgment_window_hours": judgment_window_hours(
                tf_hours, setup["entry"], setup.get("tp")),
            "raw_text": text,  # 원문 저장 → 파서 개선 시 재파싱 치유 (reparse_all)
            "coin_symbol": coin["symbol"],
            "ticker": coin["ticker"],
            "direction": setup["direction"],
            "entry_usd": setup["entry"],
            "sl_usd": setup.get("sl"),
            "tp_usd": setup.get("tp"),
            "tp_ladder_count": setup.get("tp_ladder_count") or 0,  # 표시 전용(1/N단계)
            "tps_usd": json.dumps(setup.get("tps_all") or []),     # 전체 TP 목록 JSON
            "rr": round(rr, 2) if rr is not None else None,
            "grade": grade,
            "score": score,
            # 산식 버전 태그 (D4) — 이 행이 어느 산식으로 채점됐는지. 과거 행 소급 없음.
            "grade_ver": settings.get("grade_formula_ver"),
            "author": idea.get("author"),
            "author_followers": followers,
            "author_hit_rate": stats_row.get("hit_rate"),
            "author_hit_count": stats_row.get("hit_count"),
            "author_whitelisted": stats_row.get("whitelisted", False),
            "mcap_rank": coin.get("rank"),
            "mcap_tier_icon": coin.get("tier_icon"),
            "post_url": idea.get("url"),
            "post_age_minutes": idea.get("age_minutes"),
            "collected_at": time.time(),
            "source": source,
        }
        return True, bool(db.upsert_level(conn, level))
    except Exception as e:  # noqa: BLE001 - 글 1건 오류가 사이클 전체를 막으면 안 됨
        logger.warning("[%s] 아이디어 1건 처리 실패 - 스킵: %s", coin.get("symbol"), e)
        return False, False


# ── 텔레그램 공개채널 수집 (2026-07-27 기획 카드 #14) ──────────────────
def _collect_telegram(conn, universe: list, author_stats: dict, timeout: float,
                      max_age_hours):
    """공개채널 화이트리스트를 돌며 글을 수집·저장. 반환 (글수, 셋업수, 신규수).

    ⚠️ 기본 OFF·빈 화이트리스트가 이 기능의 안전장치다 — settings 의
    telegram_source_enabled 가 False 이거나 telegram_source_channels 가 비어 있으면
    **요청 한 건도 나가지 않고 즉시 0 을 반환한다**. 배포만으로는 동작이 전혀
    변하지 않아야 한다(채널 목록은 사장님 승인 사항).

    TradingView 수집 루프 '뒤에' 둔다: 주 입력원이 예산·시간을 먼저 쓰고, 보조
    입력원이 남은 시간에 붙는 구조. 텔레그램 차단은 TradingView 와 완전히 독립이라
    (다른 호스트) 한쪽 차단이 다른 쪽을 막지 않는다.
    """
    if not settings.get("telegram_source_enabled"):
        return 0, 0, 0
    channels = [c for c in (settings.get("telegram_source_channels") or []) if c]
    if not channels:
        logger.info("[tg] 채널 화이트리스트가 비어 있음 - 수집 생략")
        return 0, 0, 0

    # 심볼 해석용 인덱스. 채널 글은 어느 코인 얘기인지 본문을 봐야 알기 때문에
    # 유니버스(= 업비트 KRW 상장 ∩ 시총 상위) 안에서만 찾는다 — 추적 대상이 아닌
    # 코인의 레벨을 만들어 봐야 가격체크가 조회할 티커가 없다.
    by_symbol = {u["symbol"]: u for u in universe}
    known = list(by_symbol)

    sleep_sec = settings.get("telegram_source_sleep_sec")
    max_posts = settings.get("telegram_source_max_posts")
    n_posts = n_setup = n_new = n_unmatched = 0

    for i, channel in enumerate(channels):
        if telegram_source.is_blocked():
            logger.warning("[tg] 차단 쿨다운 감지 - 남은 %d개 채널 다음 회차로 이월",
                           len(channels) - i)
            break
        if i > 0:
            time.sleep(sleep_sec)   # 비공식 경로 - 공격적으로 긁지 않는다
        posts = telegram_source.fetch_posts(
            channel, timeout, max_age_hours=max_age_hours, max_posts=max_posts)
        n_posts += len(posts)
        for post in posts:
            # 글 1건 격리는 TradingView 루프와 동일 원칙 — 심볼 해석 실패는 예외가
            # 아니라 '해당 없음'이라 조용히 넘긴다(시황글·잡담이 대부분일 것).
            try:
                symbol = telegram_source.match_symbol(
                    f"{post.get('title') or ''}\n{post.get('description') or ''}", known)
            except Exception as e:  # noqa: BLE001
                logger.warning("[tg] %s: 심볼 해석 실패 - 스킵: %s", channel, e)
                continue
            if not symbol:
                n_unmatched += 1
                continue
            had_setup, is_new = _ingest_idea(
                conn, by_symbol[symbol], post, author_stats, timeout,
                source="telegram", lookup_followers=False)
            n_setup += 1 if had_setup else 0
            n_new += 1 if is_new else 0

        # 채널 1개 끝날 때마다 확정 (2026-07-27 교차감사 M1). TradingView 루프와 같은
        # 이유 — 여기도 하드킬 사정권이고(채널당 5초 페이싱 × 채널 수), 채널 목록이
        # 늘어날수록 한 트랜잭션에 묶인 구간이 길어진다. 채널이 1개면 사실상 무비용.
        conn.commit()

    if n_posts:
        logger.info("[tg] 채널 %d개: 글 %d건(심볼 미해석 %d) → 셋업 %d건 → 신규 %d건",
                    len(channels), n_posts, n_unmatched, n_setup, n_new)
    return n_posts, n_setup, n_new


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", help="콤마구분 심볼 (지정 시 유니버스 대신 사용)")
    ap.add_argument("--limit", type=int, help="유니버스 상위 N개만")
    args = ap.parse_args()

    t0 = time.time()
    timeout = settings.get("http_timeout_sec")
    db_path = settings.get("db_path")
    db.init_db(db_path)
    tradingview.reset_detail_budget()

    # 2026-07-26 수리: build_universe() 가 신선 캐시도 없이 네트워크 예외를 던지면
    # (coingecko 쪽 예외처리 부재였음) 이 회차 전체가 죽는다 - 실패는 로그만 남기고
    # 이번 회차 수집을 스킵(빈 유니버스), 다음 회차(4h 뒤)에 재시도.
    try:
        universe = coingecko.build_universe()
    except Exception as e:  # noqa: BLE001 - 유니버스 실패가 프로세스를 죽이면 안 됨
        logger.warning("유니버스 갱신 실패 - 이번 수집 회차 스킵: %s", e)
        return 0
    logger.info("유니버스 %d개", len(universe))

    if args.symbols:
        want = {s.strip().upper() for s in args.symbols.split(",")}
        universe = [u for u in universe if u["symbol"] in want]
    if args.limit:
        universe = universe[: args.limit]

    # 심볼 동명이인 가드 (2026-07-24 감사): CoinGecko 코인 A 와 업비트의 같은 심볼
    # 다른 코인 B 가 묶이면 엉뚱한 자산에 레벨을 붙인다 — CG 달러가 × 환율 vs 업비트
    # 원화가가 ±40% 넘게 어긋나면 다른 자산으로 보고 이번 주기 제외.
    # (필터 이후에 두어 --symbols 스모크 시 전체 마켓 조회 낭비를 막음, 감사 #7)
    try:
        from monitor import upbit as upbit_api
        tickers = [u["ticker"] for u in universe] + ["KRW-USDT"]
        krw_prices = upbit_api.fetch_prices(tickers, timeout)
        usdt_krw = krw_prices.get("KRW-USDT")
        if usdt_krw:
            kept = []
            for u in universe:
                upbit_p, cg_p = krw_prices.get(u["ticker"]), u.get("price_usd")
                if upbit_p and cg_p:
                    expected = cg_p * usdt_krw
                    if abs(upbit_p - expected) / expected > 0.40:
                        logger.warning("동명이인 의심 제외: %s (업비트 %.6g원 vs 예상 %.6g원)",
                                       u["symbol"], upbit_p, expected)
                        continue
                kept.append(u)
            universe = kept
    except Exception as e:  # noqa: BLE001 - 가드 실패가 수집을 막으면 안 됨
        logger.warning("동명이인 가드 생략(오류): %s", e)

    author_stats = watcher_stats.load_author_stats()

    n_posts = n_new = n_setup = 0
    sleep_sec = settings.get("tv_fetch_sleep_sec")
    # 랜덤 지터 상한 (2026-08-02) — 고정 간격 봇 패턴 희석. min>max 설정 실수는
    # 지터 없는 고정 간격으로 강등(방어적). 테스트는 양쪽 다 0 으로 덮어쓴다.
    sleep_max = max(sleep_sec, settings.get("tv_fetch_sleep_max_sec") or 0.0)
    max_age_h = settings.get("max_post_age_hours")

    with db.connect(db_path) as conn:
        # ── 차단 쿨다운 복원 (2026-07-28 수리) ──────────────────────────────
        # 회차마다 새 프로세스라 tradingview._blocked_until 이 0 으로 리셋된다 →
        # "30분 쿨다운"이 사실상 한 회차 안에서만 유효했다. 여기서 DB 값을 실어
        # 회차 경계를 넘긴다. 반드시 삭제감지·수집 루프보다 **앞**이어야 한다 —
        # 뒤에 두면 이번 회차가 이미 밴 벽에 요청을 쏜 뒤라 복원의 의미가 없다.
        _saved_block = db.get_meta(conn, _TV_BLOCKED_UNTIL_KEY, "0")
        try:
            tradingview.restore_block_state(float(_saved_block or 0))
        except (TypeError, ValueError):
            logger.warning("[차단쿨다운] 저장값 해석 실패(무시): %r", _saved_block)
        if tradingview.is_blocked():
            logger.warning("[차단쿨다운] 이전 회차 차단 유효 - %.0f초 남음",
                           tradingview.blocked_until() - time.time())
        # 2026-07-28 수리: telegram_source 도 동일한 패턴으로 복원(tradingview 와 대칭).
        _saved_tg_block = db.get_meta(conn, _TG_BLOCKED_UNTIL_KEY, "0")
        try:
            telegram_source.restore_block_state(float(_saved_tg_block or 0))
        except (TypeError, ValueError):
            logger.warning("[TG차단쿨다운] 저장값 해석 실패(무시): %r", _saved_tg_block)
        if telegram_source.is_blocked():
            logger.warning("[TG차단쿨다운] 이전 회차 차단 유효 - %.0f초 남음",
                           telegram_source.blocked_until() - time.time())

        # ── 글 삭제 감지(과제1) — 하루 1회, 종결 레벨만, 상한 건수만 순환 확인.
        #
        # ⚠️ 이 호출은 반드시 심볼 수집 루프보다 **앞**에 있어야 한다. 뒤로 옮기면
        # 기능이 구조적으로 영원히 실행되지 않는다 — 2026-07-27 프로덕션 실증:
        #   ① 수집 루프 도중 TradingView 가 차단을 건다(실측 26~61번째 심볼에서 403,
        #      → 모듈 전역 30분 쿨다운).
        #   ② 루프 뒤에 있던 _check_deletions 는 첫 후보에서 is_blocked()==True 라
        #      즉시 break → 0건 확인.
        #   ③ 하루 게이트를 소진하지 않는 수정(2026-07-26) 덕에 다음 회차에 재시도
        #      하지만, 다음 회차도 똑같이 루프에서 먼저 차단돼 **같은 자리에서 영원히
        #      기아**. 실제로 deleted_checked_at 이 배포 후 이틀째 0건이었다.
        # 비용 대비도 명확하다: 삭제 확인은 하루 1회·최대 5요청(deletion_check_daily_limit)
        # 이라 81심볼 수집에 비하면 무시할 수준이고, 그 5요청 때문에 수집이 조금 일찍
        # 차단될 위험보다 안티게이밍 기능이 아예 안 도는 손실이 훨씬 크다.
        # (직전 회차의 쿨다운이 아직 안 끝난 채로 회차가 시작된 경우엔 여기서도 그대로
        #  break 하고 게이트를 소진하지 않는다 — 그건 정상 동작이다.)
        n_deleted = _check_deletions(conn, timeout)

        # ── 중간 커밋 #1: 삭제확인 결과 확정 (2026-07-27 교차감사 M1) ─────────
        # 여기 아래(수집 루프)는 run_cycle.py 가 subprocess timeout 12분으로 **하드킬**
        # 할 수 있는 구간이고, 하드킬은 열린 트랜잭션을 통째로 롤백시킨다. 예전엔 실제
        # TradingView 요청(하루 최대 5건)을 써서 얻은 deleted_checked_at 갱신이 수집분과
        # 함께 증발했다 — 하루 상한이 있어 그 손실은 곧 '그날 삭제확인 0건'이다.
        #
        # '진전 게이트'(n_checked>0 일 때만 하루 게이트 set — 2026-07-27 개발자B 수리)와
        # 모순되지 않는다: 게이트와 그 근거인 deleted_checked_at 이 **같은 커밋**에 함께
        # 들어가므로 "게이트만 타고 진전은 없는" 상태가 생길 수 없다. 반대로 뒤이은
        # 수집 루프가 죽어도 이미 확인한 사실을 되돌릴 이유가 없다(글 생존 여부 확인은
        # 수집 성패와 독립적인 사실이다).
        conn.commit()

        # ── 수집 순환 (2026-07-27): 유니버스는 시총 내림차순 '고정'이라, 차단으로
        # 중도 이탈하면 매번 앞쪽 대형주만 수집되고 꼬리는 영원히 조회되지 않는
        # 기아가 생긴다(실측: 쿠키 등록 후에도 61번째에서 403 → 하위 21개 0회 조회
        # 확정 경로). 멈춘 지점을 meta 에 남겨 다음 회차에 그 지점부터 이어받는다 —
        # 차단이 반복돼도 두세 회차(8~12h)면 전체가 한 바퀴 돈다. 완주하면 0 으로
        # 리셋해 평상시엔 기존 순서(대형주 우선) 그대로. --symbols/--limit(수동
        # 진단)는 순환을 읽지도 쓰지도 않는다. 차단을 '유발한' 심볼은 다음 재개
        # 지점에서 한 칸 앞이라 이번 바퀴를 건너뛸 수 있는데, 판별이 불가능하므로
        # (fetch_ideas 는 차단이어도 빈 목록) 다음 바퀴에 자연 회복되게 둔다.
        rotate = not args.symbols and not args.limit and len(universe) > 1
        offset = 0
        if rotate:
            try:
                offset = int(float(db.get_meta(conn, _UNIVERSE_OFFSET_META, "0") or 0))
            except (TypeError, ValueError):
                offset = 0
            offset %= len(universe)
            if offset:
                universe = universe[offset:] + universe[:offset]
                logger.info("수집 순환 재개: %d번째(%s)부터 (직전 회차 차단 이월분)",
                            offset + 1, universe[0]["symbol"])

        # 중간 커밋 주기 (2026-07-27 교차감사 M1). 0/음수 설정도 최소 1로 막아
        # "커밋이 영영 안 도는" 설정 실수를 구조적으로 불가능하게 한다.
        commit_every = max(1, int(settings.get("collect_commit_every_symbols")))

        stopped_at = None
        for i, coin in enumerate(universe):
            if tradingview.is_blocked():
                stopped_at = i
                logger.warning("차단 쿨다운 감지 - 남은 %d개 심볼 다음 회차로 이월",
                               len(universe) - i)
                break
            ideas = tradingview.fetch_ideas(coin["symbol"], timeout, max_age_hours=max_age_h)
            n_posts += len(ideas)

            for idea in ideas:
                had_setup, is_new = _ingest_idea(conn, coin, idea, author_stats, timeout)
                n_setup += 1 if had_setup else 0
                n_new += 1 if is_new else 0

            # ── 중간 커밋 #2: 심볼 N개마다 수집분 + 재개 지점을 함께 확정 ────────
            # 하드킬(12분 timeout)로 죽어도 잃는 건 '마지막 배치'뿐이다. 재개 지점을
            # 커밋과 같은 시점에 쓰는 게 핵심 — 수집분만 살고 offset 이 뒤처지면 다음
            # 회차가 이미 받은 구간을 다시 돌고(시간 낭비), 반대면 구간을 건너뛴다.
            # 재실행 안전성(멱등): 같은 글은 db.make_signal_key 로 결정적인 키를 만들고
            # levels.signal_key 는 스키마상 UNIQUE, upsert_level 은 그 키로 먼저 조회해
            # 있으면 UPDATE 한다 — 중간 커밋 뒤 같은 구간을 재수집해도 중복 행은
            # 구조적으로 생길 수 없다(DB 제약 + 코드 양쪽에서 보장).
            if (i + 1) % commit_every == 0:
                if rotate:
                    _save_universe_offset(conn, offset, i, len(universe))
                conn.commit()

            if i < len(universe) - 1:
                time.sleep(random.uniform(sleep_sec, sleep_max))

        # 순환 지점 저장 — 완주면 0(평상시 대형주 우선 복원), 차단 이탈이면 그 지점.
        # (offset + stopped_at) 은 회전 전 원본 목록 기준 절대 위치로 환산한 값.
        # 중간 커밋이 이미 써둔 값보다 항상 앞서거나 같으므로(마지막 배치 이후 진행분을
        # 반영) 여기서 덮어쓰는 게 맞다. 차단 이탈은 하드킬과 달리 정상 흐름이라
        # 이 지점까지 도달한다.
        if rotate:
            if stopped_at is None:
                db.set_meta(conn, _UNIVERSE_OFFSET_META, "0")
            else:
                db.set_meta(conn, _UNIVERSE_OFFSET_META,
                            str((offset + stopped_at) % len(universe)))
        conn.commit()

        # ── 두 번째 입력원: 텔레그램 공개채널 (2026-07-27 카드 #14) ─────────
        # TradingView 루프 뒤에 붙인다. 기본 OFF·빈 화이트리스트라 사장님이 채널을
        # 넣기 전까지는 이 호출이 요청 없이 즉시 0 을 반환한다(동작 변화 0).
        tg_posts, tg_setup, tg_new = _collect_telegram(
            conn, universe, author_stats, timeout, max_age_h)
        n_posts += tg_posts
        n_setup += tg_setup
        n_new += tg_new

        # ── 뒷정리 구간 ─────────────────────────────────────────────────
        # ⚠️ 아래 셋은 하드킬로 아예 실행되지 않아도 데이터 정합이 깨지지 않는다
        # (2026-07-27 M1 경계 검토):
        #  · reparse_all — 활성 레벨을 '현재 파서'로 다시 계산하는 치유라 멱등이고,
        #    다음 회차가 같은 행들을 그대로 다시 훑는다(밀린 만큼만 늦어짐).
        #  · expire_old — 만료는 collected_at 경과 시간 기준이라 늦게 돌아도 결과가
        #    같다(시각이 아니라 조건으로 판정). 4시간 늦게 만료되는 것뿐.
        #  · stats — 로그용 읽기 전용.
        # 즉 중간 커밋으로 "수집분만 살고 뒷정리는 안 된" 상태가 남아도 무해하다.
        reparsed = db.reparse_all(conn)
        expired = db.expire_old(conn, settings.get("level_expiry_hours") * 3600)
        st = db.stats(conn)

        # 확정 차단 감지 시 즉시 경보(과제2) - 위 심볼 루프 도중 차단으로 조기 종료
        # 됐어도 hard_block_detected() 는 주기 내내 유지되므로 여기서 잡힌다.
        now_block = time.time()
        _maybe_alert_block(conn, now_block)

        # 이번 회차에 걸린 쿨다운을 다음 회차로 넘긴다(2026-07-28 수리). 위
        # _maybe_alert_block 뒤에 두는 이유는 없고 순서 무관하지만, 차단 관련
        # 처리를 한자리에 모아 읽기 쉽게 뒀다. 만료 시각이 과거면 저장해도 무해하다
        # (is_blocked() 가 현재시각과 비교하므로 자동으로 '차단 아님'이 된다).
        db.set_meta(conn, _TV_BLOCKED_UNTIL_KEY, str(tradingview.blocked_until()))
        db.set_meta(conn, _TG_BLOCKED_UNTIL_KEY, str(telegram_source.blocked_until()))

        # 차단경보 meta 키 정리(과제2, 2026-07-26 감사 minor) - 날짜별 카운터가 영영
        # 안 지워지는 문제 방지. 매 수집 회차 가볍게 수행(비용 무시할 수준).
        _prune_tv_block_alert_meta(conn, _day_kst(now_block),
                                   settings.get("tv_block_alert_meta_keep_days"))

        # 삭제확인 요청캡 meta 키 정리(2026-07-27, 개발자B, 교차감사 m-A6) - 위와
        # 동일한 이유로 날짜별 카운터가 무한 증가하지 않게 함께 정리한다(공통 SQL은
        # _prune_daily_counter_meta_by_prefix 로 공유 - 위 함수 docstring 참고).
        _prune_daily_counter_meta_by_prefix(conn, _DELETION_CHECK_REQ_COUNT_KEY_PREFIX,
                                            _day_kst(now_block),
                                            _DELETION_CHECK_REQ_META_KEEP_DAYS)

        # 마지막 확정 (2026-07-27 M1). 컨텍스트 종료 시에도 커밋되지만 명시한다 —
        # _maybe_alert_block 은 '발송 성공 후' 카운터를 올리므로, 발송과 커밋 사이에
        # 죽으면 다음 회차가 같은 차단 경보를 한 번 더 보낸다. 그 창을 최소화한다.
        conn.commit()

        # (글 삭제 감지 _check_deletions 는 이 자리에 있었다 → 2026-07-27 수집 루프
        #  '앞'으로 이동. 이유는 위 호출부 주석 참고 — 되돌리면 다시 기아가 된다.)

    logger.info(
        "수집 완료(%.0f초): 글 %d건 → 셋업 %d건 → 신규 %d건 / 재파싱치유 %d건 / 만료 %d건 / "
        "삭제감지 %d건 / DB %s",
        time.time() - t0, n_posts, n_setup, n_new, reparsed, expired, n_deleted, st,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
