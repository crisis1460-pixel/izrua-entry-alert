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
from pathlib import Path

from config import settings
from monitor import upbit
from notify import telegram
from storage import db

logger = logging.getLogger("alert.price_check")

_KST = timezone(timedelta(hours=9))

# 직전 체크 시각은 DB가 아니라 임시 파일에 둔다(2026-07-23): DB에 넣으면 매 실행마다
# DB가 바뀌어 커밋백이 2분마다 커밋을 쌓는다(하루 ~720개). Actions 러너는 매번 새
# 체크아웃이라 이 파일이 없고 → 기본 12분 소급 창을 쓰는데, 2분 주기 + 1분봉 소급
# 판정은 멱등이라(이미 터치된 레벨 재알림 없음) 겹침 창은 무해하다.
_LAST_CHECK_FILE = Path("cache/last_check.txt")


def _load_last_check():
    try:
        return float(_LAST_CHECK_FILE.read_text().strip())
    except (OSError, ValueError):
        return None


def _save_last_check(ts: float) -> None:
    try:
        _LAST_CHECK_FILE.parent.mkdir(parents=True, exist_ok=True)
        _LAST_CHECK_FILE.write_text(str(ts))
    except OSError:
        pass


def _day_kst(now: float) -> str:
    return datetime.fromtimestamp(now, tz=_KST).strftime("%Y-%m-%d")


def _build_clusters(levels: list, band_pct: float) -> list:
    """엔트리 내림차순 greedy 병합. 반환: [ [level,...](entry 내림차순), ... ]"""
    with_entry = [l for l in levels if l.get("entry_usd")]
    with_entry.sort(key=lambda l: l["entry_usd"], reverse=True)
    clusters, used = [], set()
    for lv in with_entry:
        if lv["id"] in used:
            continue
        top = lv["entry_usd"]
        group = [l for l in with_entry
                 if l["id"] not in used and l["entry_usd"] >= top * (1 - band_pct / 100.0)]
        for g in group:
            used.add(g["id"])
        clusters.append(group)
    return clusters


def _rep(cluster: list) -> dict:
    """대표 레벨 = 등급점수 최고 (필터/표시 기준)."""
    return max(cluster, key=lambda l: l.get("score") or 0)


def run_once(now: float = None) -> dict:
    """1회 체크. 반환 요약 dict (테스트/로그용)."""
    now = now or time.time()
    cfg_get = settings.get
    db_path = cfg_get("db_path")
    db.init_db(db_path)

    summary = {"checked": 0, "previews": 0, "touches": 0, "suppressed": 0}

    with db.connect(db_path) as conn:
        expired = db.expire_old(conn, cfg_get("level_expiry_hours") * 3600, now)
        if expired:
            logger.info("[체크] 만료 처리 %d건", expired)

        levels = db.get_active_levels(conn, direction="long")
        unresolved = db.get_unresolved_touched(conn)  # 적중판정 대상 (활성과 별개)
        if not levels and not unresolved:
            _save_last_check(now)
            logger.info("[체크] 활성/판정 대상 레벨 없음")
            return summary

        # 직전 체크 시각 → 소급 저가 판정 구간
        last = _load_last_check()
        since_min = int((now - last) / 60) + 2 if last else 45

        by_ticker: dict = {}
        for lv in levels:
            by_ticker.setdefault(lv["ticker"], []).append(lv)

        # 시세는 활성 + 판정대상 티커 모두 — 활성 레벨이 사라진 코인의 미종결 건도
        # 판정이 계속되도록 (2026-07-23 자체 발견 버그 수정)
        markets = sorted(set(by_ticker.keys()) | {lv["ticker"] for lv in unresolved})
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
        budget = {"calls": 0}   # 캔들 호출 예산 (감시+판정 공유, 2026-07-24 카운터 수정)
        range_cache: dict = {}  # ticker → 캔들목록|False(실패 네거티브캐시) — 1콜 공유

        from collector.grading import meets_min_grade  # 순환 import 방지 지연 로드
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
            low = min((c[3] for c in candles), default=None) if candles else None
            eff_low = min(current, low) if low else current

            for cluster in _build_clusters(tlevels, cluster_band):
                top_krw = cluster[0]["entry_usd"] * usdt_krw
                touched = eff_low <= top_krw
                previewing = (not touched) and current <= top_krw * (1 + preview_band)
                if not (touched or previewing):
                    continue

                rep = _rep(cluster)
                ids = [l["id"] for l in cluster]
                kind = "touch" if touched else "preview"

                if kind == "preview" and any(l["status"] == "previewed" for l in cluster):
                    continue  # 이미 예고한 클러스터

                # 알림 필터 (상태 전이는 필터와 무관하게 수행 — 재알림 방지)
                # 일일 상한은 터치(본알림)에만 적용 (2026-07-24 감사: 예고가 상한을
                # 소진해 정작 본알림이 영구 소실되던 문제 — 예고는 클러스터당 1회라
                # 자체 상한이 이미 있음)
                send_ok = meets_min_grade(rep.get("grade") or "D", min_grade)
                if send_ok and kind == "touch" and \
                        db.count_alerts_today(conn, coin, day, kind="touch") >= daily_cap:
                    logger.info("[체크] %s 일일 본알림 상한 도달 - 억제", coin)
                    send_ok = False

                if send_ok:
                    # 자체 적중 성적 주입 (표본 5건↑일 때만 렌더러가 표시 — 2단계 자동 발동)
                    for lv in cluster:
                        st = db.get_author_self_stats(conn, lv.get("author"))
                        lv["author_self_wins"], lv["author_self_losses"] = st["wins"], st["losses"]
                        lv["author_touched_n"] = st["touched"]
                        lv["author_untouched_expired"] = st["untouched_expired"]
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
                else:
                    summary["suppressed"] += 1

                if touched:
                    # 자기 엔트리에 실제 도달한 레벨만 판정 대상 터치 (기준가 = 자기
                    # 엔트리, 지정가 체결 모델). 미도달 하단 레벨은 섀도 터치(재알림
                    # 방지만, 통계 제외) — 2026-07-24 감사 수정
                    touches = []
                    for lv in cluster:
                        e_krw = lv["entry_usd"] * usdt_krw if lv.get("entry_usd") else None
                        reached = e_krw is not None and eff_low <= e_krw
                        touches.append((lv["id"], e_krw if reached else None))
                    db.mark_touched(conn, touches, now, usdt_krw=usdt_krw)
                else:
                    for lid in ids:
                        db.mark_previewed(conn, lid, now)

                # 발송·상태전이 즉시 확정 (2026-07-24 감사: 이후 크래시/타임아웃 시
                # 롤백돼 같은 알림이 재발송되던 문제 방지)
                conn.commit()

        # ── 적중 판정 (ACCURACY_DB_PLAN 1단계 — 조용한 누적, 표시·필터 무관) ──
        summary["resolved"] = _judge_outcomes(
            conn, prices, usdt_krw, _get_range, now, cfg_get)

        _save_last_check(now)

    logger.info("[체크] 완료: %s", summary)
    return summary



def _judge_outcomes(conn, prices, usdt_krw, get_range, now, cfg_get) -> int:
    """터치됐지만 미종결인 레벨들의 hit/miss 판정 + 24h/72h 수익률 기록.

    확정 규칙(2026-07-23 질문카드 + 2026-07-24 감사 반영):
    - 캔들을 시간순으로 스캔하되 '터치 이후' 캔들만 본다 (터치 이전 가격이 섞여
      급락 관통 시 가짜 hit 이 나던 감사 1번 수정)
    - TP1 도달=hit / SL 도달=miss / '같은 캔들' 안에서 둘 다=보수적 miss+ambiguous
      (여러 캔들에 걸쳐 순서가 확정되면 그 순서대로 — 감사 2번 수정)
    - TP 없으면 타임박스(창 만료 시 수익률 부호), SL 터치는 즉시 miss
    - 창 만료 강제 종결 시에도 judgment_mode 는 원래 모드 유지 (정보 보존)
    - 타임박스/수익률 기준가는 터치 시점 환율로 보정 (장기 창의 환율 드리프트 제거)
    - 시세 조회가 계속 불가한 티커(상폐 등)는 창+14일 후 판정불능 제외
    """
    resolved = 0
    default_window_sec = cfg_get("outcome_window_hours") * 3600
    r_lo, r_hi = cfg_get("r_clip_low"), cfg_get("r_clip_high")

    # ── 24h/72h 수익률 — 종결 여부 무관 + 도과 6시간 허용오차 안에서만 기록
    #    (다운타임 뒤 70시간짜리 값이 '24h'로 오라벨되느니 NULL 이 낫다 — 감사 수정)
    for lv in db.get_ret_pending(conn):
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

    for lv in db.get_unresolved_touched(conn):
        window_sec = (lv.get("judgment_window_hours") or 0) * 3600 or default_window_sec
        elapsed = now - lv["touched_at"]
        ticker = lv["ticker"]
        current = prices.get(ticker)
        entry_krw = (lv.get("entry_usd") or 0) * (usdt_krw or 0)
        if not current or not usdt_krw or entry_krw <= 0:
            if elapsed > window_sec + 14 * 86400:
                conn.execute(
                    "UPDATE levels SET status='expired' WHERE id=? AND outcome IS NULL",
                    (lv["id"],))
                logger.info("[적중판정] %s 시세 조회 불가 지속 - 판정불능 제외", ticker)
            continue

        base = lv.get("touch_price_krw") or entry_krw
        t_rate = lv.get("touch_usdt_krw")
        base_eff = base * (usdt_krw / t_rate) if t_rate else base

        tp_krw = (lv.get("tp_usd") or 0) * usdt_krw
        sl_krw = (lv.get("sl_usd") or 0) * usdt_krw

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
            if c_end <= lv["touched_at"]:
                continue
            tp_hit = tp_krw > 0 and c_high >= tp_krw
            sl_hit = sl_krw > 0 and c_low <= sl_krw
            if tp_hit and sl_hit:
                outcome, resolve_price, ambiguous = "miss", sl_krw, True
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
