"""
가격체크 잡 — 활성 레벨 vs 업비트 실시간 가격, 예고/터치 판정 후 알림.

확정 설계(ALERT_BOT_PLAN v3):
- 대상: long 레벨만 (하방 터치 = 매수 관점 알림)
- 클러스터: 같은 코인에서 엔트리가 서로 ±cluster_band_pct 이내인 레벨을 병합.
  트리거 기준가는 클러스터 상단 엔트리. 알림은 클러스터당 1회.
- 예고: 위에서 하락해 상단엔트리 +preview_band_pct 이내 진입 시 1회
- 본알림: 상단엔트리 터치/하향돌파 시 1회. 직전 체크 이후 1분봉 저가로 소급 판정
  (스파이크 놓침 방지). 예고와 동시 감지되면 본알림만.
- 알림 필터: 대표(최고점수) 레벨 등급 min_grade 이상 + 코인당 하루 상한.
  필터로 알림이 생략돼도 상태 전이는 수행(재알림 방지 원칙 유지).
- entry 는 USD 저장 → 체크 시점 KRW-USDT 환율로 환산 비교(환율 변동 반영,
  upbit_bot watcher_feed 검증 방식).
"""

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from analytics import clustering, ranking  # 순수 수학 모듈 (프로젝트 import 0 — 순환 없음)
from config import settings
from monitor import announcements, upbit
from notify import telegram
from storage import db

logger = logging.getLogger("alert.price_check")

_KST = timezone(timedelta(hours=9))

# 직전 체크 시각은 DB meta 에 저장한다(2026-07-24 감사 #3). 예전엔 cache/ 임시파일에
# 뒀는데 커밋백 대상이 아니라 러너 재체크아웃마다 소실 → 소급창이 늘 기본값(45분)에
# 고착돼 15분봉 다운타임 폴백이 사문화됐다. meta 는 data/levels.db 안이라 커밋백으로
# 이어달리기된다. 갱신은 어차피 매 회차 다른 상태변화와 함께 커밋되므로 추가 소음 없음.
def _load_last_check(conn):
    try:
        v = db.get_meta(conn, "last_check_at")
        return float(v) if v else None
    except (ValueError, TypeError):
        return None


def _save_last_check(conn, ts: float) -> None:
    db.set_meta(conn, "last_check_at", str(ts))


def _day_kst(now: float) -> str:
    return datetime.fromtimestamp(now, tz=_KST).strftime("%Y-%m-%d")


# ── 수집 급감(조용한 고장) 감지 (2026-07-26) ──────────────────────────
# cron 초록불 + Actions 초록불 + 신규 수집 0건이 3일 지속된 사고를 계기로 추가.
# 수집 잡(run_collect.py) 자체가 죽어도 이 잡(price_check, 2분 주기)이 살아있으면
# 감지되도록 여기 둔다. DB 쓰기는 meta 뿐이라 가볍고, 하루 1회 넘게 보내지 않는다.
def _check_collect_silence(conn, now: float, cfg_get) -> bool:
    day = _day_kst(now)
    if db.get_meta(conn, "collect_silence_warned_date") == day:
        return False  # 오늘 이미 경고 발송함 - 중복 방지

    window_sec = cfg_get("collect_silence_window_hours") * 3600
    baseline_days = cfg_get("collect_silence_baseline_days")
    recent = conn.execute(
        "SELECT COUNT(*) FROM levels WHERE collected_at >= ?", (now - window_sec,)
    ).fetchone()[0]
    if recent > 0:
        return False  # 정상 - 최근 수집 있음

    baseline_start = now - window_sec - baseline_days * 86400
    prior = conn.execute(
        "SELECT COUNT(*) FROM levels WHERE collected_at >= ? AND collected_at < ?",
        (baseline_start, now - window_sec),
    ).fetchone()[0]
    baseline_avg = prior / baseline_days
    if baseline_avg < cfg_get("collect_silence_min_baseline_avg"):
        return False  # 원래도 조용했던 기간(신규 프로젝트 등) - 오탐 방지

    text = telegram.render_collect_silence_alert(
        cfg_get("collect_silence_window_hours"), baseline_avg)
    if telegram.send(text):
        db.set_meta(conn, "collect_silence_warned_date", day)
        logger.warning("[체크] 수집 급감 경고 발송 (직전 %.1f건/일 -> 최근 0건)", baseline_avg)
        return True
    return False


def _build_clusters(levels: list, band_pct: float) -> list:
    """엔트리 내림차순 greedy 병합. 반환: [ [level,...](entry 내림차순), ... ]

    2026-07-26 리팩터: 규칙 본체는 analytics.clustering.build_clusters 로 옮겨
    정본(canonical)을 그쪽 하나로 통일했다(주간 리포트 쪽 confluence 집계와 동일
    정의 보장 — 예전엔 이 함수와 클러스터링 모듈에 같은 규칙이 두 곳 있었다).
    실시간 경로는 window_sec 제약이 필요 없으므로(항상 '지금 동시에 살아있는'
    레벨만 넘어옴) 시간 인자 없이 위임한다 — 동작은 이전과 완전히 동일
    (scripts/test_price_logic.py 클러스터 회귀 테스트로 증명)."""
    return clustering.build_clusters(levels, band_pct)


def _rep(cluster: list) -> dict:
    """대표 레벨 = 등급점수 최고 (필터/표시 기준)."""
    return max(cluster, key=lambda l: l.get("score") or 0)


def _tp_distance_penalty(direction: str, entry, target) -> float:
    """'목표 거리 감점' 폭(양수)만 돌려준다 — 관찰집계의 되돌림 판정 전용.

    2026-07-26 사용자 결정: 감점이 없었으면 알림이 나갔을 억제 건수를 기록한다.
    최초엔 규칙을 여기 복제했으나(당시 grading.py 를 다른 개발자가 편집 중),
    같은 날 배점표가 grading.TP_DISTANCE_BANDS 로 단일화되면서 위임으로 정리했다 —
    출처가 하나뿐이라 드리프트 자체가 발생할 수 없다."""
    from collector.grading import tp_distance_points  # 순환 import 방지 지연 로드
    # has_rr=True → 감점 구간(<5%)만 반환하고 SL 미기재 글의 대체 가점은 제외한다.
    # (이 함수의 용도는 '감점 폭 역산' 하나뿐)
    return -tp_distance_points(direction, entry, target, has_rr=True)


def _magnify_feasible(scan_from: float, c_end: float, cfg_get) -> bool:
    """체결내역 재검사를 '시도할 수 있는' 구간인지 — 예산과 무관한 길이 제약만 본다.

    magnify_order 가 이 함수로 조기 반환하므로 규칙의 출처는 여기 하나다. 호출부는
    같은 판정을 관찰집계 분류(ambiguous_skipped = 체결내역을 아예 못 봄)에만 쓴다."""
    return c_end - scan_from <= cfg_get("bar_magnifier_max_span_sec")


def magnify_order(ticker: str, scan_from: float, c_end: float,
                  tp_krw: float, sl_krw: float, cfg_get) -> Optional[str]:
    """동시터치 캔들을 체결내역으로 '확대'해 TP·SL 도달 순서를 판별 (Bar Magnifier).

    반환 'hit' | 'miss' | None(판별 불가 → 호출부는 기존 보수적 처리를 유지).

    왜 체결내역인가: 업비트 공개 API의 최소 캔들 단위가 1분이라 '하위 봉'이 없다.
    개별 체결 틱이 사실상 최하위 타임프레임이고, 그 안에는 밀리초 시각과 체결
    일련번호가 있어 같은 1분 안의 실제 순서를 그대로 복원할 수 있다.

    비용: ambiguous 가 발생한 순간에만(정상 회차엔 0콜) 최대 max_pages 콜.
    체결내역은 캔들과 레이트리밋 그룹이 분리돼 있어 판정용 캔들 예산과 무관하다.

    scan_from 은 max(캔들 시작, 터치 시각) — 터치 이전 체결이 순서 판정에 섞이지
    않게 한다(캔들 스캔부의 '터치 이후만' 원칙과 동일, 2026-07-26 감사 major2 취지).
    """
    if not _magnify_feasible(scan_from, c_end, cfg_get):
        return None
    trades = upbit.fetch_trades_window(
        ticker, scan_from, c_end, cfg_get("http_timeout_sec"),
        max_pages=cfg_get("bar_magnifier_max_pages"))
    if not trades:
        return None
    for _ts, price in trades:            # 시간 오름차순 — 먼저 닿은 쪽이 승부를 결정
        if tp_krw > 0 and price >= tp_krw:
            return "hit"
        if sl_krw > 0 and price <= sl_krw:
            return "miss"
    # 구간은 덮었는데 어느 쪽에도 안 닿았다 = 캔들 고저와 불일치(경계/데이터 이상).
    # 억지로 결론 내지 않고 보수적 처리로 되돌린다.
    return None


def run_once(now: float = None) -> dict:
    """1회 체크. 반환 요약 dict (테스트/로그용)."""
    now = now or time.time()
    cfg_get = settings.get
    db_path = cfg_get("db_path")
    db.init_db(db_path)

    summary = {"checked": 0, "previews": 0, "touches": 0, "suppressed": 0}

    with db.connect(db_path) as conn:
        # 수집 급감 감시는 활성 레벨 유무와 무관하게 매 회차 수행(조용한 고장 감지가
        # 목적이라, 아래 "대상 없음" 조기 반환보다 먼저 돌아야 한다)
        if _check_collect_silence(conn, now, cfg_get):
            summary["collect_silence_alert"] = True

        # 거래소 리스크 공지 감시 (카드 #5) — 유의종목/거래지원 종료를 감지해 경보 +
        # 해당 코인 대기 레벨 만료. 비문서화 API 를 쓰므로 예외를 여기서 완전히
        # 격리한다: 이 기능이 어떻게 실패하든 가격체크 회차는 계속 돌아야 한다.
        # 위치는 만료·활성조회 '앞' — 이번 회차부터 위험 코인의 알림이 멈추도록.
        try:
            ann = announcements.check_announcements(conn, now, cfg_get)
            if ann.get("alerted"):
                summary["announcement_alerts"] = ann["alerted"]
                summary["announcement_expired"] = ann["expired"]
        except Exception as e:  # noqa: BLE001 - 회차 생존 최우선
            logger.warning("[체크] 공지 감시 실패(무시하고 진행): %s", e)

        expired = db.expire_old(conn, cfg_get("level_expiry_hours") * 3600, now)
        if expired:
            logger.info("[체크] 만료 처리 %d건", expired)

        levels = db.get_active_levels(conn, direction="long")
        unresolved = db.get_unresolved_touched(conn)  # 적중판정 대상 (활성과 별개)
        ret_pending = db.get_ret_pending(conn, now)   # 24/72h 수익률 기록 대상 (종결 무관)
        if not levels and not unresolved and not ret_pending:
            _save_last_check(conn, now)
            logger.info("[체크] 활성/판정/수익률 대상 레벨 없음")
            return summary

        # 직전 체크 시각 → 소급 저가 판정 구간. 2026-07-24 감사 #3: last_check 를
        # data/meta 에 영속화(러너 재체크아웃에도 보존)해 실제 다운타임을 반영 →
        # 200분 초과 시 15분봉 폴백이 실제로 발동한다(예전엔 항상 45분 고정이라 사문화).
        last = _load_last_check(conn)
        since_min = int((now - last) / 60) + 2 if last else 45

        by_ticker: dict = {}
        for lv in levels:
            by_ticker.setdefault(lv["ticker"], []).append(lv)

        # 시세는 활성 + 미종결 + 수익률대기 티커 모두 — 활성 레벨이 사라진 코인의
        # 미종결 건도 판정되고, 조기 종결 건도 24/72h 수익률이 유실되지 않도록
        # (2026-07-24 감사 #1: ret 대기 티커 누락으로 조기종결 건 수익률 영구 NULL)
        markets = sorted(set(by_ticker.keys())
                         | {lv["ticker"] for lv in unresolved}
                         | {lv["ticker"] for lv in ret_pending})
        prices = upbit.fetch_prices(markets + ["KRW-USDT"], cfg_get("http_timeout_sec"))
        usdt_krw = prices.get("KRW-USDT")
        if not usdt_krw:
            logger.warning("[체크] KRW-USDT 환율 조회 실패 - 이번 회차 건너뜀")
            return summary

        preview_band = cfg_get("preview_band_pct") / 100.0
        cluster_band = cfg_get("cluster_band_pct")
        min_grade = cfg_get("alert_min_grade")
        daily_cap = cfg_get("alert_max_per_coin_per_day")
        day = _day_kst(now)
        # 관찰 집계(스프린트5) — 알림 발송과 무관하게 조용히 누적만 한다. 필터로
        # 억제돼도 여기엔 반드시 잡힌다(방금 들어간 TP 근접도 감점의 효과 측정 등).
        obs = {"touches_total": 0, "previews_total": 0, "suppressed_grade": 0,
               "suppressed_cap": 0, "suppressed_dup": 0, "suppressed_send_fail": 0,
               "suppressed_grade_tp_penalty_only": 0, "preview_dwell": 0,
               "ambiguous_magnified": 0, "ambiguous_unresolved": 0,
               "ambiguous_skipped": 0}
        budget = {"calls": 0}   # 캔들 호출 예산 (감시+판정 공유, 2026-07-24 카운터 수정)
        range_cache: dict = {}  # ticker → 캔들목록|False(실패 네거티브캐시) — 1콜 공유

        # 순환 import 방지 지연 로드. grade_from_score 는 grading.py 를 고치지 않고
        # 이미 있는 순수 함수를 읽기 전용으로 재사용하는 용도(TP 감점 되돌림 판정).
        from collector.grading import grade_from_score, meets_min_grade, regrade_current
        from monitor import market_sentiment

        # 시장 심리(BTC.D/ALT.S/F&G)는 실제로 알림을 보낼 때만 1회 지연 조회
        # (1시간 meta 캐시 — 5분 주기 체크가 CoinGecko 한도를 갉아먹지 않게)
        sentiment_cache = {"loaded": False, "data": None}

        def _sentiment():
            if not sentiment_cache["loaded"]:
                sentiment_cache["loaded"] = True
                sentiment_cache["data"] = market_sentiment.get_sentiment(conn)
            return sentiment_cache["data"]

        # 거래량 순위도 발송 시에만 1회 조회해 이번 회차 알림들이 공유 (조회 시점 기준)
        vol_cache = {"loaded": False, "ranks": {}}

        def _volume_ranks():
            if not vol_cache["loaded"]:
                vol_cache["loaded"] = True
                vol_cache["ranks"] = upbit.fetch_volume_ranks(cfg_get("http_timeout_sec"))
            return vol_cache["ranks"]

        def _get_range(ticker, limit):
            """캔들목록 조회 (예산·네거티브캐시 공유). 반환 목록|None."""
            cached = range_cache.get(ticker)
            if cached is not None:
                return cached or None
            if budget["calls"] >= limit:
                return None
            budget["calls"] += 1
            rng = upbit.fetch_range_since(ticker, since_min, cfg_get("http_timeout_sec"))
            range_cache[ticker] = rng if rng else False
            return rng

        # 엔트리 근접 순으로 순회 — 캔들 예산 소진 시 먼 티커부터 생략되게
        # (2026-07-24 감사: 임의 순서면 같은 코인이 반복적으로 밀릴 수 있었음)
        def _proximity(tlevels):
            cur = prices.get(tlevels[0]["ticker"]) or 0
            ents = [lv["entry_usd"] * usdt_krw for lv in tlevels if lv.get("entry_usd")]
            return min((abs(cur - e) / e for e in ents), default=9e9) if cur else 9e9

        for ticker, tlevels in sorted(by_ticker.items(), key=lambda kv: _proximity(kv[1])):
            current = prices.get(ticker)
            if not current:
                continue
            summary["checked"] += 1
            coin = tlevels[0]["coin_symbol"]

            # 소급 저가: 엔트리가 현재가의 +5% 이내에 있을 때만 캔들 소모
            need_low = any(
                lv["entry_usd"] * usdt_krw >= current * 0.95 for lv in tlevels if lv.get("entry_usd")
            )
            candles = _get_range(ticker, 30) if need_low else None

            def _eff_low(lv_):
                """레벨별 유효 저가 — 수집(collected_at) 이후 시작한 캔들만 인정.
                (2026-07-26 감사 major3: 레벨이 존재하기 전 가격으로 터치·종결되던
                문제 — 다운타임 15분봉 폴백(최대 50h 소급) 직후 수집 시 재발 구조)"""
                col = lv_.get("collected_at") or 0
                lows = [c[3] for c in (candles or []) if c[0] >= col]
                return min([current] + lows)

            for cluster in _build_clusters(tlevels, cluster_band):
                top_krw = cluster[0]["entry_usd"] * usdt_krw
                touched = _eff_low(cluster[0]) <= top_krw
                previewing = (not touched) and current <= top_krw * (1 + preview_band)
                if not (touched or previewing):
                    continue

                rep = _rep(cluster)
                ids = [l["id"] for l in cluster]
                kind = "touch" if touched else "preview"

                # 이미 예고한 클러스터는 밴드에 머무는 동안 매 회차 여기 도달한다.
                # 2026-07-26 감사 MAJOR-1: 예전엔 이 판정 '전에' previews_total 을 올려서
                # 밴드 체류 시간이 예고 건수로 둔갑했다(하루 머물면 최대 720배 부풀림).
                # 이제 '새로 발생한' 이벤트만 세고, 체류 회차는 별도 지표로 분리한다.
                dup_preview = kind == "preview" and any(
                    l["status"] == "previewed" for l in cluster)

                if not dup_preview:
                    # 관찰 집계: 필터 통과 여부와 무관한 원(raw) 이벤트 1건
                    obs["touches_total" if touched else "previews_total"] += 1

                if dup_preview:
                    obs["preview_dwell"] += 1   # 억제가 아니라 '밴드 체류 회차'
                    continue

                # 등급 재평가 (2026-07-26 감사: freeze 결함 수정) — calculate_grade 의
                # 가격근접도(최대 20점)는 채점 시점 가격 기준이라, 수집 당시 등급을 그대로
                # 쓰면 이후 가격이 entry 에 근접해 정작 알림이 가장 중요해진 순간에도
                # 여전히 옛 등급(D 등)에 갇혀 필터에서 배제된다(터치 52건 중 18건 사례).
                # DB 원본 grade/score 는 보존한다 — 판정(hit/miss/r_multiple)은 entry/sl/tp
                # 만으로 결정돼 등급과 무관하고, 수집 시점 등급은 그 자체로 사후분석
                # 가치(등급-실제성과 상관관계 검증)가 있어 덮어쓰지 않는 편이 낫다.
                # rep(표시·필터 대표) 하나만 in-memory 로 재계산 - DB 쓰기 없음, 가벼움.
                current_usd = current / usdt_krw
                cur_grade, cur_score, _cur_rr = regrade_current(rep, current_usd)
                rep["grade"], rep["score"] = cur_grade, cur_score

                # 알림 필터 (상태 전이는 필터와 무관하게 수행 — 재알림 방지)
                # 일일 상한은 터치(본알림)에만 적용 (2026-07-24 감사: 예고가 상한을
                # 소진해 정작 본알림이 영구 소실되던 문제 — 예고는 클러스터당 1회라
                # 자체 상한이 이미 있음)
                send_ok = meets_min_grade(rep.get("grade") or "D", min_grade)
                if not send_ok:
                    obs["suppressed_grade"] += 1
                    # suppressed_grade 의 부분집합(중복 카운트 아님) - TP 거리 감점만
                    # 되돌린 점수였다면 min_grade 를 통과했을 건. 방금 들어간 목표거리
                    # 감점의 억제 효과를 분리 측정하려는 관찰 지표(2026-07-26 결정).
                    tp_penalty = _tp_distance_penalty(
                        rep.get("direction"), rep.get("entry_usd"), rep.get("tp_usd"))
                    if tp_penalty > 0:
                        reverted_grade = grade_from_score((rep.get("score") or 0) + tp_penalty)
                        if meets_min_grade(reverted_grade, min_grade):
                            obs["suppressed_grade_tp_penalty_only"] += 1
                if send_ok and kind == "touch" and \
                        db.count_alerts_today(conn, coin, day, kind="touch") >= daily_cap:
                    logger.info("[체크] %s 일일 본알림 상한 도달 - 억제", coin)
                    send_ok = False
                    obs["suppressed_cap"] += 1

                if send_ok:
                    # 자체 적중 성적 주입 (표본 5건↑일 때만 렌더러가 표시 — 2단계 자동 발동)
                    for lv in cluster:
                        st = db.get_author_self_stats(conn, lv.get("author"))
                        lv["author_self_wins"], lv["author_self_losses"] = st["wins"], st["losses"]
                        lv["author_touched_n"] = st["touched"]
                        lv["author_untouched_expired"] = st["untouched_expired"]
                        # 자체 승률 줄 게이트용 n_eff (2026-07-26 카드: raw n≥5 →
                        # n_eff≥5. 최신성 가중 유효표본 — 데이터가 젊은 동안은 raw 동일)
                        hl = cfg_get("rank_half_life_days")
                        lv["author_self_neff"] = ranking.effective_n([
                            ranking.recency_weight(now, r["touched_at"], hl)
                            for r in db.get_author_outcome_rows(conn, lv.get("author"))])
                    # 52주 고저 + 김프는 발송 확정건에만 조회 (회당 업비트 1콜 + 바이낸스 1콜)
                    from monitor import binance
                    week52 = upbit.fetch_week52(ticker, cfg_get("http_timeout_sec"))
                    kimchi = None
                    usd_global = binance.fetch_usdt_price(coin, cfg_get("http_timeout_sec"))
                    if usd_global and usd_global > 0 and usdt_krw:
                        effective = current / usd_global
                        kimchi = (effective - usdt_krw) / usdt_krw * 100
                    text = telegram.render_alert(kind, coin, cluster, current, usdt_krw,
                                                 sentiment=_sentiment(), week52=week52,
                                                 kimchi_pct=kimchi,
                                                 volume_rank=_volume_ranks().get(ticker))
                    if telegram.send(text):
                        db.record_alert(conn, coin, kind, ids, day, now)
                        summary["touches" if touched else "previews"] += 1
                    else:
                        summary["suppressed"] += 1
                        obs["suppressed_send_fail"] += 1
                else:
                    summary["suppressed"] += 1

                if touched:
                    # 자기 엔트리에 실제 도달한 레벨만 판정 대상 터치 (기준가 = 자기
                    # 엔트리, 지정가 체결 모델). 미도달 하단 레벨은 섀도 터치(재알림
                    # 방지만, 통계 제외) — 2026-07-24 감사 수정
                    touches = []
                    for lv in cluster:
                        e_krw = lv["entry_usd"] * usdt_krw if lv.get("entry_usd") else None
                        reached = e_krw is not None and _eff_low(lv) <= e_krw
                        # 터치 앵커 = 실제 도달한 첫 캔들의 종료 시각 (2026-07-26 감사
                        # major2: 감지 시각 앵커면 터치 캔들의 터치 이전 고가가 다음
                        # 회차 판정에 섞이고, 소급 터치 구간이 영구 미판정이었다).
                        # 진행 중 캔들이면 종료가 now 이후일 수 있음 — 판정부가
                        # touched_at > now 인 행을 다음 회차로 미뤄 정합 유지.
                        t_anchor = now
                        if reached:
                            for c in candles or []:
                                if c[0] >= (lv.get("collected_at") or 0) and c[3] <= e_krw:
                                    t_anchor = c[1]
                                    break
                        touches.append((lv["id"], e_krw if reached else None, t_anchor))
                    # 호가 매수/매도 압력 스냅샷 (카드 #19) — 실제 도달 터치가 있을
                    # 때만 1콜. 순수 기록용이라 실패해도 그냥 NULL 로 남기고 진행한다
                    # (알림·필터·판정에는 이 값이 어디에도 쓰이지 않는다).
                    ratio = None
                    if cfg_get("orderbook_pressure_enabled") and \
                            any(p is not None for _id, p, _t in touches):
                        try:
                            ratio = upbit.fetch_orderbook_ratio(
                                ticker, cfg_get("http_timeout_sec"))
                        except Exception as e:  # noqa: BLE001 - 기록 실패 격리
                            logger.warning("[체크] %s 호가 기록 실패(무시): %s", ticker, e)
                    db.mark_touched(conn, touches, now, usdt_krw=usdt_krw,
                                    bid_ask_ratio=ratio)
                else:
                    for lid in ids:
                        db.mark_previewed(conn, lid, now)

                # 발송·상태전이 즉시 확정 (2026-07-24 감사: 이후 크래시/타임아웃 시
                # 롤백돼 같은 알림이 재발송되던 문제 방지)
                conn.commit()

        # ── 적중 판정 (ACCURACY_DB_PLAN 1단계 — 조용한 누적, 표시·필터 무관) ──
        summary["resolved"] = _judge_outcomes(
            conn, prices, usdt_krw, _get_range, now, cfg_get, obs=obs)

        # 관찰 집계 반영 + 보존기간 정리 (스프린트5 — 알림 발송 없음, 조용히 누적만)
        db.bump_daily_stats(conn, day, **obs)
        db.prune_daily_stats(conn, now)

        _save_last_check(conn, now)

    logger.info("[체크] 완료: %s", summary)
    return summary



def _judge_outcomes(conn, prices, usdt_krw, get_range, now, cfg_get, obs=None) -> int:
    """터치됐지만 미종결인 레벨들의 hit/miss 판정 + 24h/72h 수익률 기록.

    확정 규칙(2026-07-23 질문카드 + 2026-07-24 감사 반영):
    - 캔들을 시간순으로 스캔하되 '터치 이후' 캔들만 본다 (터치 이전 가격이 섞여
      급락 관통 시 가짜 hit 이 나던 감사 1번 수정)
    - TP1 도달=hit / SL 도달=miss / '같은 캔들' 안에서 둘 다=체결내역 재검사
      (Bar Magnifier, 2026-07-26) → 순서가 복원되면 그대로, 복원 실패 시에만
      보수적 miss+ambiguous (여러 캔들에 걸친 순서 확정은 감사 2번 수정)
      · 재검사 실패는 관찰집계에서 두 갈래로 나눠 센다 — 체결내역을 보고도 못 가른
        ambiguous_unresolved / 아예 못 본 ambiguous_skipped (판정 동작은 동일)
    - TP 없으면 타임박스(창 만료 시 수익률 부호), SL 터치는 즉시 miss
    - 창 만료 강제 종결 시에도 judgment_mode 는 원래 모드 유지 (정보 보존)
    - 타임박스/수익률 기준가는 터치 시점 환율로 보정 (장기 창의 환율 드리프트 제거)
    - 시세 조회가 계속 불가한 티커(상폐 등)는 창+14일 후 판정불능 제외
    """
    # obs 미주입(테스트에서 단독 호출) 시에도 안전하게 — 집계만 버려진다
    obs = {} if obs is None else obs
    obs.setdefault("ambiguous_magnified", 0)
    obs.setdefault("ambiguous_unresolved", 0)
    obs.setdefault("ambiguous_skipped", 0)
    resolved = 0
    default_window_sec = cfg_get("outcome_window_hours") * 3600
    r_lo, r_hi = cfg_get("r_clip_low"), cfg_get("r_clip_high")
    # 동시터치 재검사 예산 — 회차당 상한. 시장 전체 급변으로 ambiguous 가 한꺼번에
    # 쏟아져도 2분 주기 핫패스가 늘어지지 않게 한다(초과분은 기존 보수적 처리).
    magnify_budget = {"left": cfg_get("bar_magnifier_max_per_cycle")
                      if cfg_get("bar_magnifier_enabled") else 0}

    # ── 24h/72h 수익률 — 종결 여부 무관 + 도과 6시간 허용오차 안에서만 기록
    #    (다운타임 뒤 70시간짜리 값이 '24h'로 오라벨되느니 NULL 이 낫다 — 감사 수정)
    for lv in db.get_ret_pending(conn, now):
        current = prices.get(lv["ticker"])
        base = lv.get("touch_price_krw")
        if not current or not base or not lv.get("touched_at"):
            continue
        t_rate = lv.get("touch_usdt_krw")
        base_eff = base * (usdt_krw / t_rate) if (t_rate and usdt_krw) else base
        elapsed = now - lv["touched_at"]
        ret_pct = (current - base_eff) / base_eff * 100
        if lv.get("ret_24h") is None and 24 * 3600 <= elapsed <= 30 * 3600:
            db.record_ret(conn, lv["id"], "ret_24h", ret_pct)
        if lv.get("ret_72h") is None and 72 * 3600 <= elapsed <= 78 * 3600:
            db.record_ret(conn, lv["id"], "ret_72h", ret_pct)

    # 판정창 만료 임박 순 정렬 — DB 순서(id순) 그대로 돌면 캔들 예산 고갈 시 뒤쪽
    # 티커가 매 회차 반복적으로 밀릴 수 있다(2026-07-26 재감사 minor10). run_once의
    # 터치 탐지가 이미 근접순(_proximity)으로 완화한 것과 같은 취지 — 남은 시간이
    # 적은(곧 타임박스 만료될) 레벨을 먼저 판정해 예산 부족 시의 피해를 최소화한다.
    # 현재 규모(활성+미종결 ~25개)는 예산 내라 실영향은 없음(감사 기록).
    def _urgency(lv_):
        w = (lv_.get("judgment_window_hours") or 0) * 3600 or default_window_sec
        return w - (now - (lv_["touched_at"] or now))

    for lv in sorted(db.get_unresolved_touched(conn), key=_urgency):
        if lv["touched_at"] > now:
            continue  # 터치 캔들이 아직 진행 중 — 완성 후(다음 회차) 판정
        window_sec = (lv.get("judgment_window_hours") or 0) * 3600 or default_window_sec
        elapsed = now - lv["touched_at"]
        ticker = lv["ticker"]
        current = prices.get(ticker)
        entry_krw = (lv.get("entry_usd") or 0) * (usdt_krw or 0)
        if not current or not usdt_krw or entry_krw <= 0:
            if elapsed > window_sec + 14 * 86400:
                conn.execute(
                    "UPDATE levels SET status='expired', expired_at=? "
                    "WHERE id=? AND outcome IS NULL", (now, lv["id"]))
                logger.info("[적중판정] %s 시세 조회 불가 지속 - 판정불능 제외", ticker)
            continue

        base = lv.get("touch_price_krw") or entry_krw
        t_rate = lv.get("touch_usdt_krw")
        base_eff = base * (usdt_krw / t_rate) if t_rate else base

        # 오염 방어선(2026-07-26 감사 major1): 방향·크기 sanity 위반 tp/sl 은 '없음' 취급.
        # raw_text 없는 구세대 행은 reparse/업서트 자동치유가 닿지 않아(ALGO tp=1.0 사례)
        # 서수 오인 값이 판정까지 흘러올 수 있다 — extractor 크기 규칙(0.25x~4x)과 동일 기준.
        entry_usd = lv.get("entry_usd") or 0
        tp_usd = lv.get("tp_usd") or 0
        sl_usd = lv.get("sl_usd") or 0
        if lv.get("direction") == "long" and entry_usd > 0:
            if tp_usd and not (entry_usd < tp_usd <= entry_usd * 4):
                tp_usd = 0
            if sl_usd and not (entry_usd * 0.25 <= sl_usd < entry_usd):
                sl_usd = 0
        tp_krw = tp_usd * usdt_krw
        sl_krw = sl_usd * usdt_krw

        def _r(resolve_krw):
            if sl_krw <= 0 or entry_krw <= sl_krw:
                return None
            return max(r_lo, min(r_hi, (resolve_krw - entry_krw) / (entry_krw - sl_krw)))

        # 캔들 시간순 스캔 (터치 이후 캔들만)
        outcome = None
        resolve_price = None
        ambiguous = False
        candles = get_range(ticker, 40) or []
        for (c_start, c_end, c_high, c_low) in candles:
            # 진행 중 캔들(c_end>now)은 제외 — 터치 이전 가격이 남아 있을 수 있고
            # 15분봉 폴백에선 최대 15분치가 섞인다 (2026-07-26 감사 major2)
            if c_end <= lv["touched_at"] or c_end > now:
                continue
            tp_hit = tp_krw > 0 and c_high >= tp_krw
            sl_hit = sl_krw > 0 and c_low <= sl_krw
            if tp_hit and sl_hit:
                # 같은 캔들 안에서 TP·SL 동시 — 체결내역으로 실제 순서를 복원해 본다.
                # 종결 '이전' 단계라 이미 확정된 판정을 뒤집는 게 아니다(안티게이밍
                # 불변 스냅샷 원칙과 무충돌 — 판정은 여전히 단 한 번만 쓰인다).
                scan_from = max(c_start, lv["touched_at"])
                # '체결내역을 실제로 봤는가' — 판별 실패(unresolved)와 시도조차 못 함
                # (skipped)을 가르는 기준(2026-07-26 감사 minor). 예전엔 한 칸에 합쳐
                # 세서 "판정을 못 가른 것"과 "우리가 안 본 것"이 구분되지 않아
                # 신뢰도 지표로 쓸 수 없었다. 판정 결과는 양쪽 다 예전 그대로
                # (보수적 miss+ambiguous) — 이번 변경은 분류뿐이다.
                attempted = (magnify_budget["left"] > 0
                             and _magnify_feasible(scan_from, c_end, cfg_get))
                refined = None
                if magnify_budget["left"] > 0:
                    # 예산 소모 시점은 기존과 동일하게 둔다(구간 길이 초과로 조회
                    # 없이 끝나는 경우까지 포함) — 판정 경로의 동작 변화 0 이 우선.
                    magnify_budget["left"] -= 1
                    refined = magnify_order(
                        ticker, scan_from, c_end, tp_krw, sl_krw, cfg_get)
                    logger.info("[적중판정] %s 동시터치 재검사 결과: %s",
                                ticker, refined or "판별불가(보수적 miss 유지)")
                if refined == "hit":
                    outcome, resolve_price = "hit", tp_krw
                    obs["ambiguous_magnified"] += 1
                elif refined == "miss":
                    outcome, resolve_price = "miss", sl_krw
                    obs["ambiguous_magnified"] += 1
                else:
                    outcome, resolve_price, ambiguous = "miss", sl_krw, True
                    # 조회 실패·부분커버·캔들 고저 불일치는 '봤지만 못 가름'
                    # (비용을 쓰고도 결론이 안 난 건)이라 unresolved 쪽이다.
                    obs["ambiguous_unresolved" if attempted
                        else "ambiguous_skipped"] += 1
            elif tp_hit:
                outcome, resolve_price = "hit", tp_krw
            elif sl_hit:
                outcome, resolve_price = "miss", sl_krw
            if outcome:
                break
        if not outcome:
            # 캔들 부재(예산/실패) 폴백: 현재가 스냅샷 (다음 회차가 보완)
            if tp_krw > 0 and current >= tp_krw:
                outcome, resolve_price = "hit", tp_krw
            elif sl_krw > 0 and current <= sl_krw:
                outcome, resolve_price = "miss", sl_krw

        mode = ("tp_sl" if (tp_krw > 0 and sl_krw > 0)
                else "tp_only" if tp_krw > 0 else "timeboxed")

        if outcome == "hit":
            db.resolve_outcome(conn, lv["id"], "hit", resolve_price, mode,
                               r_multiple=_r(resolve_price), best_tp_hit=1, now=now)
            resolved += 1
        elif outcome == "miss":
            db.resolve_outcome(conn, lv["id"], "miss", resolve_price, mode,
                               r_multiple=_r(resolve_price), ambiguous=ambiguous, now=now)
            resolved += 1
        elif elapsed >= window_sec:
            oc = "timeboxed_win" if current >= base_eff else "timeboxed_loss"
            db.resolve_outcome(conn, lv["id"], oc, current, mode,
                               r_multiple=_r(current), now=now)
            resolved += 1

    if resolved:
        logger.info("[적중판정] %d건 종결", resolved)
    return resolved
