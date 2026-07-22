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

from config import settings
from monitor import upbit
from notify import telegram
from storage import db

logger = logging.getLogger("alert.price_check")

_KST = timezone(timedelta(hours=9))


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
        if not levels:
            db.set_meta(conn, "last_check_at", str(now))
            logger.info("[체크] 활성 레벨 없음")
            return summary

        # 직전 체크 시각 → 소급 저가 판정 구간
        last = db.get_meta(conn, "last_check_at")
        since_min = int((now - float(last)) / 60) + 2 if last else 12

        by_ticker: dict = {}
        for lv in levels:
            by_ticker.setdefault(lv["ticker"], []).append(lv)

        markets = sorted(by_ticker.keys())
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
        candle_calls = 0

        from collector.grading import meets_min_grade  # 순환 import 방지 지연 로드

        for ticker, tlevels in by_ticker.items():
            current = prices.get(ticker)
            if not current:
                continue
            summary["checked"] += 1
            coin = tlevels[0]["coin_symbol"]

            # 소급 저가: 엔트리가 현재가의 +5% 이내에 있을 때만 캔들 소모 (호출 예산 30)
            need_low = any(
                lv["entry_usd"] * usdt_krw >= current * 0.95 for lv in tlevels if lv.get("entry_usd")
            )
            low = None
            if need_low and candle_calls < 30:
                candle_calls += 1
                low = upbit.fetch_low_since(ticker, since_min, cfg_get("http_timeout_sec"))
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
                send_ok = meets_min_grade(rep.get("grade") or "D", min_grade)
                if send_ok and db.count_alerts_today(conn, coin, day) >= daily_cap:
                    logger.info("[체크] %s 일일 알림 상한 도달 - 억제", coin)
                    send_ok = False

                if send_ok:
                    text = telegram.render_alert(kind, coin, cluster, current, usdt_krw)
                    if telegram.send(text):
                        db.record_alert(conn, coin, kind, ids, day, now)
                        summary["touches" if touched else "previews"] += 1
                    else:
                        summary["suppressed"] += 1
                else:
                    summary["suppressed"] += 1

                if touched:
                    db.mark_touched(conn, ids, now)
                else:
                    for lid in ids:
                        db.mark_previewed(conn, lid, now)

        db.set_meta(conn, "last_check_at", str(now))

    logger.info("[체크] 완료: %s", summary)
    return summary
