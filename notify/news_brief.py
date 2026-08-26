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
import re
import time
from typing import Optional

from config import settings
from notify import telegram
from notify.translator import translate_en_ko
from storage import db
from utils.time_kst import day_kst

logger = logging.getLogger("alert.news_brief")

_NEWS_KIND = "news"
_TEXT_MAX = 500   # 원문 요약 상한(자) 폴백 — settings.news_alert_summary_max_chars 우선

# 일반 영단어와 겹치는 심볼 — 뉴스 본문에서 코인이 아닌 맥락으로 자주 등장해 오탐.
# 매매 시그널(parse_setup 성공)에는 영향 없음 — 뉴스 경로에서만 차단.
_AMBIGUOUS_SYMBOLS = frozenset({
    "OPEN", "SIGN", "GAS", "ID", "T", "W", "F", "G", "A",
    "RE", "LA", "ME", "AI", "IO", "OP",
    "HOME", "SUPER", "PUMP", "RED", "SKY", "TREE",
    "MASK", "SUN", "MOVE", "ERA",
})

# 광고·프로모션 키워드 — 이 패턴이 본문에 있으면 뉴스가 아니라 홍보글.
_PROMO_KEYWORDS = (
    "bonus", "airdrop", "에어드롭", "보너스", "giveaway", "referral",
    "join now", "sign up", "register now", "limited offer", "exclusive offer",
    "mt5", "mt4", "자동화 시스템", "vip channel", "premium channel",
    "free trial", "discount code", "promo code",
)

# 매매 결과 리캡 필터 (2026-08-21 사용자 요청): "manually closed. +929.8 pips.
# profits secured. well played" 류 청산 결과 자랑 글 — 뉴스가 아니라 지난
# 시그널 성적 보고라 정보가치 없음. 오탐 방지 점수제: 고정밀 패턴 2점,
# 보조 패턴 1점, 합산 2점 이상일 때만 스킵. 일반 시황 글의 profit/close
# 단어 단독(예: "profit-taking pressure", "closed above resistance")으로는
# 절대 안 걸리게 구두 표현·숫자 결합 패턴만 사용.
_RESULT_PATTERNS = (
    # ── 고정밀(2점): 뉴스·분석 글에는 등장하지 않는 결과 보고 전용 어휘 ──
    (2, re.compile(r"[+-]?\d[\d,]*(?:\.\d+)?\s*(?:pips?\b|핍)", re.I)),      # +929.8 pips
    (2, re.compile(r"\bmanually\s+clos(?:ed|ing)\b|수동으로\s*닫", re.I)),
    (2, re.compile(r"\bprofits?\s+(?:secured|booked|locked)\b", re.I)),
    (2, re.compile(r"\bwell\s+played\b|잘\s*연주했", re.I)),
    (2, re.compile(r"\btp\s*\d*\s+hit\b", re.I)),          # "TP2 hit" — 시그널 채널 전용 어휘
    (2, re.compile(r"\bsl\s+hit\b", re.I)),
    (2, re.compile(r"\bclos(?:ed|ing)\s+in\s+profit\b", re.I)),
    (2, re.compile(r"\bclean\s+win\b|\brunning\s+in\s+profit\b", re.I)),
    (2, re.compile(r"익절\s*완료|이익을\s*챙|수익\s*확정|마감되었습니다", re.I)),
    # ── 보조(1점): 단독으론 스킵 안 됨 — 고정밀과 결합돼야 트립 ──
    # 2026-08-21 검토 강등 3건: 일반 뉴스에도 나올 수 있는 표현이라 단독 차단 금지 —
    #   "Bitcoin closed at 100,000"(시세 마감), "$73K target hit"(애널 목표가),
    #   "stopped out"(청산 캐스케이드 기사). 리캡 글은 pips/well played 등
    #   고정밀 어휘를 함께 쓰므로 결합 시엔 여전히 차단된다.
    (1, re.compile(r"\bclos(?:ed|ing)\s+at\s+[\d.,]+", re.I)),
    (1, re.compile(r"\btargets?\s+(?:hit|smashed|reached)\b", re.I)),
    (1, re.compile(r"\bstop(?:\s|-)?loss\s+hit\b|\bstopped\s+out\b", re.I)),
    (1, re.compile(r"\btrade\s+update\b|거래\s*업데이트", re.I)),
    (1, re.compile(r"\bbreak\s*-?\s*even\b|본절", re.I)),
)
_RESULT_SCORE_MIN = 2


def _is_trade_result(text: str) -> bool:
    """청산 결과 리캡 판정 — 점수 2 이상이면 True (뉴스 경로에서 제외)."""
    score = 0
    for pts, rx in _RESULT_PATTERNS:
        if rx.search(text):
            score += pts
            if score >= _RESULT_SCORE_MIN:
                return True
    return False

# level_ids 필드 재사용 계약 (2026-08-17): news 알림은 매매 레벨 개념이 없어
# alerts_log.level_ids 에 '단일 원소 = 채널명 문자열' 로 저장한다. record_alert
# 는 정렬+CSV join 하므로 실제 저장값은 그대로 채널명. _rate_limit_ok 의 채널당
# 상한 SELECT 도 `level_ids=? (channel)` 로 raw equality 매칭. 이 계약을 깨는
# 리팩터(JSON 배열 저장, 다중 채널 aggregation 등)는 여기와 storage/db.py
# record_alert 두 파일 동시 수정 필요. 별도 news_log 테이블 신설이 정공법이나
# 4개 필드 알림 규모라 재사용 유지.

# 회차 단위 module-level short-circuit (2026-08-17 효율 개선): 상한 도달 후
# 남은 후보 전부 skip 하면서도 매번 3 SELECT 를 태우던 문제 방지. 회차마다
# maybe_send_news_brief 첫 호출 시 오늘 카운트 1회 조회 후 캐시.
_SESSION_STATE = {"day": None, "global_reached": False, "ch_reached": set()}


def _reset_session_state(today: str) -> None:
    """새 KST 일 진입 시 세션 캐시 리셋."""
    if _SESSION_STATE["day"] != today:
        _SESSION_STATE["day"] = today
        _SESSION_STATE["global_reached"] = False
        _SESSION_STATE["ch_reached"] = set()


def _rate_limit_ok(conn, coin: str, channel: str, now: float) -> tuple:
    """(허용여부, 사유) — 3중 게이트. 순서: 코인 24h → 채널 → 글로벌 (효율).
    가장 자주 트립되는 게이트(같은 코인 재게시)를 먼저 짚어 short-circuit."""
    today = day_kst(now)
    _reset_session_state(today)
    max_global = settings.get("news_alert_max_global_per_day")
    max_ch = settings.get("news_alert_max_per_channel_per_day")
    coin_cooldown_h = settings.get("news_alert_coin_cooldown_hours")

    # 회차 단위 도달 플래그 — 상한 도달 후 남은 후보에 3 SELECT 반복 방지
    if _SESSION_STATE["global_reached"]:
        return False, "글로벌 상한(세션 캐시)"
    if channel in _SESSION_STATE["ch_reached"]:
        return False, f"채널({channel}) 상한(세션 캐시)"

    # 1) 코인 24h 쿨다운 — 뉴스 채널 특성상 같은 코인 반복 게시가 가장 흔한 트립
    since = now - coin_cooldown_h * 3600
    n_coin = conn.execute(
        "SELECT COUNT(*) n FROM alerts_log WHERE coin_symbol=? AND kind=? AND sent_at >= ?",
        (coin, _NEWS_KIND, since)
    ).fetchone()["n"]
    if n_coin >= 1:
        return False, f"코인({coin}) 24h 쿨다운 중"

    # 2) 채널당 하루 상한 — level_ids 필드 raw string 매칭(위 계약 참고)
    n_ch = conn.execute(
        "SELECT COUNT(*) n FROM alerts_log WHERE day_kst=? AND kind=? AND level_ids=?",
        (today, _NEWS_KIND, channel)
    ).fetchone()["n"]
    if n_ch >= max_ch:
        _SESSION_STATE["ch_reached"].add(channel)
        return False, f"채널({channel}) 상한 {max_ch}건 도달"

    # 3) 글로벌 하루 상한
    n_global = db.count_all_alerts_today(conn, today, kind=_NEWS_KIND)
    if n_global >= max_global:
        _SESSION_STATE["global_reached"] = True
        return False, f"글로벌 상한 {max_global}건 도달({n_global})"

    return True, "OK"


def _summary(text: str) -> str:
    """원문 요약 — 앞 N자 클리핑 + 뒤 마감. 이미 짧으면 그대로.
    상한은 settings.news_alert_summary_max_chars (2026-08-27 사용자 요청: 250→500,
    MyMemory 번역 폴백의 500자 제한이 사실상 상한이라 그 이상 금지)."""
    if not text:
        return ""
    try:
        text_max = int(settings.get("news_alert_summary_max_chars"))
    except Exception:  # noqa: BLE001 — 설정 누락 시 폴백
        text_max = _TEXT_MAX
    t = text.strip()
    if len(t) <= text_max:
        return t
    # 문장 끊김 완화: N자 앞 마지막 개행/마침표까지 자르기
    cut = t[:text_max]
    for sep in ("\n\n", "\n", ". ", "。"):
        idx = cut.rfind(sep)
        if idx > text_max // 2:
            return cut[:idx + len(sep)].rstrip() + " …"
    return cut.rstrip() + " …"


def maybe_send_news_brief(conn, post: dict, symbol: str, channel: str,
                          now: Optional[float] = None) -> str:
    """뉴스 요약 알림 발송 시도. 반환 "skipped"|"ok"|"failed".

    호출부(run_collect)는 심볼 매칭 성공 + parse_setup 실패인 게시글만 여기 넘긴다.
    상한·쿨다운·최소 길이 미달·설정 OFF 는 조용히 skipped."""
    if not settings.get("news_alert_enabled"):
        return "skipped"

    if symbol.upper() in _AMBIGUOUS_SYMBOLS:
        logger.debug("[news] %s 모호 심볼 스킵", symbol)
        return "skipped"

    # title+description 결합 (2026-08-17 리뷰): 종전 description 우선 단일 선택은
    # description 이 짧고 title 이 헤드라인인 채널(BitcoinBullets 등)에서 조용히
    # 스킵. 심볼 매칭도 run_collect 에서 두 필드 결합으로 하므로 여기도 통일.
    # 첫 줄 중복 제거 (2026-08-27 사용자 요청): telegram_source 는 title 을 본문
    # 첫 줄에서 잘라 만들므로(desc 가 title 로 시작) 결합하면 같은 줄이 두 번 나간다.
    title = (post.get("title") or "").strip()
    desc = (post.get("description") or "").strip()
    if title and desc:
        text = desc if desc.startswith(title) else (title + "\n" + desc).strip()
    else:
        text = title or desc
    min_len = settings.get("news_alert_min_length")
    if len(text) < min_len:
        return "skipped"

    text_lower = text.lower()
    if any(kw in text_lower for kw in _PROMO_KEYWORDS):
        logger.debug("[news] %s 프로모션 콘텐츠 스킵", symbol)
        return "skipped"

    if _is_trade_result(text):
        logger.debug("[news] %s 매매 결과 리캡 스킵", symbol)
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
    if settings.get("news_translate_enabled"):
        try:
            summary = translate_en_ko(summary, timeout=settings.get("http_timeout_sec"))
        except Exception as e:
            logger.warning("[news] %s 번역 실패(원문 유지): %s", symbol, e)
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
