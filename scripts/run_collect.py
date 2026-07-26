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
import logging
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
def _check_deletions(conn, timeout: float) -> int:
    """종결된 레벨의 post_url 생존을 하루 상한만큼 확인. 반환: 삭제 확정 건수.
    하루 1회만 수행(수집이 4시간마다 도는 것과 별개로 - meta 로 날짜 게이트),
    확인 건수는 settings.deletion_check_daily_limit 로 제한한다(비용 방어)."""
    now = time.time()
    day = _day_kst(now)
    if db.get_meta(conn, "last_deletion_check_day") == day:
        return 0
    limit = settings.get("deletion_check_daily_limit")
    recheck_sec = settings.get("deletion_recheck_after_days") * 86400
    candidates = db.get_deletion_check_candidates(conn, limit, recheck_sec)
    if not candidates:
        db.set_meta(conn, "last_deletion_check_day", day)
        return 0
    n_deleted = n_checked = 0
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
        if result is None:
            continue  # 판정 보류 - deleted_checked_at 갱신 안 함, 다음 순번에 재확인
        db.mark_deletion_checked(conn, cand["id"], result, now)
        n_checked += 1
        if result:
            n_deleted += 1
            logger.info("[삭제확인] 삭제 확정: id=%s author=%s", cand["id"], cand.get("author"))
    # 2026-07-26 실전 버그 수리: 차단으로 중도 break 했을 때도 게이트를 set 해버려서,
    # "0건 확인"인데 하루 게이트가 소진되고 후보(그날 26건)가 통째로 다음날로
    # 밀리는 사고가 있었다. 로그 문구("다음 회차로 연기")대로, 차단 시엔 게이트를
    # set 하지 않는다 - 정상 순회 완료(또는 애초에 후보 없음)일 때만 하루 게이트 소진.
    if not blocked:
        db.set_meta(conn, "last_deletion_check_day", day)
    if n_checked:
        logger.info("[삭제확인] %d건 확인 (삭제 %d건)", n_checked, n_deleted)
    return n_deleted


# ── TradingView 확정 차단 즉시 알림 (2026-07-26 과제2) ─────────────────
# 날짜별 카운터 meta 키 접두사 - 경보 발송(_maybe_alert_block)과 정리
# (_prune_tv_block_alert_meta) 양쪽에서 공유해 접두사가 어긋날 일이 없게 한다.
_TV_BLOCK_ALERT_KEY_PREFIX = "tv_block_alert_count_"

# 수집 순환 재개 지점(원본 유니버스 기준 절대 인덱스) — 차단 기아 방지 (2026-07-27)
_UNIVERSE_OFFSET_META = "collect_universe_offset"


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


def _prune_tv_block_alert_meta(conn, day: str, keep_days: int) -> int:
    """날짜별 차단경보 카운터(tv_block_alert_count_YYYY-MM-DD) meta 키 정리
    (2026-07-26 과제2 - 감사 minor). 날짜가 지나도 옛 키가 안 지워지면 meta
    테이블이 무한 증가한다. storage/db.py 에 범용 prune 헬퍼를 새로 얹지 않고
    (개발자 A 병렬 작업 중 - 파일 경계 원칙) 이 파일 안에서 직접 SQL로 해결한다.

    LIKE 대신 substr 두 번으로 접두사를 자른다 - 접두사 안에 '_' 가 있어 LIKE
    패턴으로 쓰면 SQL 와일드카드(임의 1글자)로 해석돼 의도보다 헐겁게 매칭된다.
    남은 부분은 ISO 날짜(YYYY-MM-DD)라 사전식 문자열 비교가 곧 시간순 비교와
    같다(storage.db.prune_daily_stats 와 동일한 컷오프 계산 방식). 매 수집
    회차 가볍게 수행. 반환: 삭제한 키 개수."""
    cutoff = (datetime.fromisoformat(day) - timedelta(days=keep_days)).strftime("%Y-%m-%d")
    prefix_len = len(_TV_BLOCK_ALERT_KEY_PREFIX)
    cur = conn.execute(
        "DELETE FROM meta WHERE substr(key, 1, ?) = ? AND substr(key, ?) < ?",
        (prefix_len, _TV_BLOCK_ALERT_KEY_PREFIX, prefix_len + 1, cutoff),
    )
    return cur.rowcount


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
        grade, score, rr = calculate_grade(
            followers, setup["direction"], setup["entry"],
            setup.get("sl"), setup.get("tp"), coin.get("price_usd"),
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
            "rr": rr,
            "grade": grade,
            "score": score,
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
    max_age_h = settings.get("max_post_age_hours")

    with db.connect(db_path) as conn:
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

            if i < len(universe) - 1:
                time.sleep(sleep_sec)

        # 순환 지점 저장 — 완주면 0(평상시 대형주 우선 복원), 차단 이탈이면 그 지점.
        # (offset + stopped_at) 은 회전 전 원본 목록 기준 절대 위치로 환산한 값.
        if rotate:
            if stopped_at is None:
                db.set_meta(conn, _UNIVERSE_OFFSET_META, "0")
            else:
                db.set_meta(conn, _UNIVERSE_OFFSET_META,
                            str((offset + stopped_at) % len(universe)))

        # ── 두 번째 입력원: 텔레그램 공개채널 (2026-07-27 카드 #14) ─────────
        # TradingView 루프 뒤에 붙인다. 기본 OFF·빈 화이트리스트라 사장님이 채널을
        # 넣기 전까지는 이 호출이 요청 없이 즉시 0 을 반환한다(동작 변화 0).
        tg_posts, tg_setup, tg_new = _collect_telegram(
            conn, universe, author_stats, timeout, max_age_h)
        n_posts += tg_posts
        n_setup += tg_setup
        n_new += tg_new

        # 파서 개선 자동 전파: 원문 있는 기존 레벨을 현재 파서로 재파싱해 오염값 치유
        reparsed = db.reparse_all(conn)
        expired = db.expire_old(conn, settings.get("level_expiry_hours") * 3600)
        st = db.stats(conn)

        # 확정 차단 감지 시 즉시 경보(과제2) - 위 심볼 루프 도중 차단으로 조기 종료
        # 됐어도 hard_block_detected() 는 주기 내내 유지되므로 여기서 잡힌다.
        now_block = time.time()
        _maybe_alert_block(conn, now_block)

        # 차단경보 meta 키 정리(과제2, 2026-07-26 감사 minor) - 날짜별 카운터가 영영
        # 안 지워지는 문제 방지. 매 수집 회차 가볍게 수행(비용 무시할 수준).
        _prune_tv_block_alert_meta(conn, _day_kst(now_block),
                                   settings.get("tv_block_alert_meta_keep_days"))

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
