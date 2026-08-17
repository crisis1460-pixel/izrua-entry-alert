"""
뉴스·시황 요약 알림 (2026-08-17) — 사용자 요청.

배경: 저자 채널(cryptosignals0rg/wolfoftrading/BitcoinBullets 등)에는 매매 시그널
(Entry+TP+SL) 외에도 코인별 시황·뉴스 게시글이 많다. extractor.parse_setup 이
실패한 게시글 중 Upbit 상장 심볼이 언급된 것을 원문 요약으로 별도 알림.

설계:
- 트리거: extractor.parse_setup 실패 + telegram_source.match_symbol 성공
- 요약: 원문 앞 N자 그대로 (LLM 요약 없음 — 무료·즉시)
- kind='news' (매매 알림과 분리) 로 alerts_log 기록, 무음 발송
- 상한: 하루 5건 / 채널당 3건 / 코인당 24h 1건
- 최소 길이: 60자 미만은 노이즈로 판단해 스킵

전 실패 격리 — 이 모듈 실패가 수집·매매 알림을 죽이면 안 된다.
"""

import logging
import time
from typing import Optional

from config import settings
from notify import telegram
from storage import db
from utils.time_kst import day_kst

logger = logging.getLogger("alert.news_brief")

_NEWS_KIND = "news"
_TEXT_MAX = 250   # 원문 요약 상한(자)


def _rate_limit_ok(conn, coin: str, channel: str, now: float) -> tuple:
    """(허용여부, 사유) — 하루 상한·채널 상한·코인 24h 상한 3중 게이트."""
    today = day_kst(now)
    max_global = settings.get("news_alert_max_global_per_day") or 5
    max_ch = settings.get("news_alert_max_per_channel_per_day") or 3
    coin_cooldown_h = settings.get("news_alert_coin_cooldown_hours") or 24

    n_global = db.count_all_alerts_today(conn, today, kind=_NEWS_KIND)
    if n_global >= max_global:
        return False, f"글로벌 상한 {max_global}건 도달({n_global})"

    # 채널당 상한 — level_ids 에 채널명을 문자열로 저장하는 관례 이용
    n_ch = conn.execute(
        "SELECT COUNT(*) n FROM alerts_log WHERE day_kst=? AND kind=? AND level_ids=?",
        (today, _NEWS_KIND, channel)
    ).fetchone()["n"]
    if n_ch >= max_ch:
        return False, f"채널({channel}) 상한 {max_ch}건 도달"

    # 코인당 24h 쿨다운
    since = now - coin_cooldown_h * 3600
    n_coin = conn.execute(
        "SELECT COUNT(*) n FROM alerts_log WHERE coin_symbol=? AND kind=? AND sent_at >= ?",
        (coin, _NEWS_KIND, since)
    ).fetchone()["n"]
    if n_coin >= 1:
        return False, f"코인({coin}) 24h 쿨다운 중"

    return True, "OK"


def _summary(text: str) -> str:
    """원문 요약 — 앞 N자 클리핑 + 뒤 마감. 이미 짧으면 그대로."""
    if not text:
        return ""
    t = text.strip()
    if len(t) <= _TEXT_MAX:
        return t
    # 문장 끊김 완화: N자 앞 마지막 개행/마침표까지 자르기
    cut = t[:_TEXT_MAX]
    for sep in ("\n\n", "\n", ". ", "。"):
        idx = cut.rfind(sep)
        if idx > _TEXT_MAX // 2:
            return cut[:idx + len(sep)].rstrip() + " …"
    return cut.rstrip() + " …"


def maybe_send_news_brief(conn, post: dict, symbol: str, channel: str,
                          now: Optional[float] = None) -> str:
    """뉴스 요약 알림 발송 시도. 반환 "skipped"|"ok"|"failed".

    호출부(run_collect)는 심볼 매칭 성공 + parse_setup 실패인 게시글만 여기 넘긴다.
    상한·쿨다운·최소 길이 미달·설정 OFF 는 조용히 skipped."""
    if not settings.get("news_alert_enabled"):
        return "skipped"

    text = post.get("description") or post.get("title") or ""
    min_len = settings.get("news_alert_min_length") or 60
    if len(text.strip()) < min_len:
        return "skipped"

    now = now if now is not None else time.time()
    try:
        ok, reason = _rate_limit_ok(conn, symbol, channel, now)
    except Exception as e:  # noqa: BLE001 — 회차 생존 최우선
        logger.warning("[news] %s 상한 판정 실패(스킵): %s", symbol, e)
        return "failed"
    if not ok:
        logger.debug("[news] %s 스킵: %s", symbol, reason)
        return "skipped"

    summary = _summary(text)
    url = post.get("url") or ""
    try:
        text_out = telegram.render_news_brief(symbol, channel, summary, url)
    except Exception as e:  # noqa: BLE001
        logger.warning("[news] %s render 실패: %s", symbol, e)
        return "failed"

    try:
        sent_mid = telegram.send(text_out, urgency="low")
    except Exception as e:  # noqa: BLE001
        logger.warning("[news] %s 발송 예외: %s", symbol, e)
        return "failed"
    if not sent_mid:
        return "failed"

    try:
        today = day_kst(now)
        # kind='news' + level_ids 필드에 채널명 저장 (뉴스는 레벨과 무관해 재활용).
        db.record_alert(conn, symbol, _NEWS_KIND, [channel], today, now)
    except Exception as e:  # noqa: BLE001 — 발송은 성공, 기록만 실패
        logger.warning("[news] %s 기록 실패(발송 완료): %s", symbol, e)
    return "ok"
