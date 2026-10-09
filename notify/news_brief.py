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

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Optional

from config import settings
from notify import news_parse, telegram
from notify.translator import translate_en_ko
from storage import db
from utils.time_kst import day_kst

logger = logging.getLogger("alert.news_brief")

_NEWS_KIND = "news"
_TEXT_MAX = 500   # 원문 요약 상한(자) 폴백 — settings.news_alert_summary_max_chars 우선
# 큐에 보관할 영문 원문 상한(2026-09-27 뉴스 v2). 번역 요약(500자)과 별개 — 구조 추출은
# 본문 뒤쪽의 분기선·금액까지 봐야 한다. 채널 글은 대부분 1,000자 안쪽.
_EN_KEEP_MAX = 2000

# 일반 영단어와 겹치는 심볼 — 뉴스 본문에서 코인이 아닌 맥락으로 자주 등장해 오탐.
# 매매 시그널(parse_setup 성공)에는 영향 없음 — 뉴스 경로에서만 차단.
_AMBIGUOUS_SYMBOLS = frozenset({
    "OPEN", "SIGN", "GAS", "ID", "T", "W", "F", "G", "A",
    "RE", "LA", "ME", "AI", "IO", "OP",
    "HOME", "SUPER", "PUMP", "RED", "SKY", "TREE",
    "MASK", "SUN", "MOVE", "ERA",
    # 2026-09-16 추가 — 브리핑 실물에서 잡힌 오탐. "IN" 은 영어 전치사라
    # 트레이딩 교육 글("...5 losses limited to -5R, a 6-trade sequence...")이
    # IN 코인 뉴스로 실렸다. 나머지도 같은 부류의 흔한 영단어.
    "IN", "ON", "AT", "IT", "BY", "SO", "UP", "OG", "WIN", "BEST", "NEXT",
})

# 광고·프로모션 키워드 — 이 패턴이 본문에 있으면 뉴스가 아니라 홍보글.
_PROMO_KEYWORDS = (
    "bonus", "airdrop", "에어드롭", "보너스", "giveaway", "referral",
    "join now", "sign up", "register now", "limited offer", "exclusive offer",
    "mt5", "mt4", "자동화 시스템", "vip channel", "premium channel",
    "free trial", "discount code", "promo code",
    # 2026-09-16 추가 — 브리핑 실물에서 잡힌 종목 추천 광고.
    # 실측(@wolfoftrading): "투자하기 좋은 코인을 찾고 계신가요? $ZRX는 좋은 선택입니다"
    # — 근거 숫자가 하나도 없는 순수 추천문이다.
    "찾고 계신가요", "좋은 선택", "looking for a coin", "looking for a good coin",
    "great pick", "good pick", "best coin to",
)

# 정보 밀도 게이트 (2026-09-16) — "짧은데 숫자 하나 없는" 글은 알맹이가 없다.
# 실측 노이즈: "$BTCUSDT 중요 업데이트: 현재 보유하고 있는 이 수준을 잃으면
# 비트코인이 하락할 것으로 예상할 수 있습니다"(65자) — 그 '수준'이 얼마인지
# 끝내 말하지 않아 읽어도 할 수 있는 게 없다.
# 가격·비율 같은 **수치가 하나라도 있으면** 통과시킨다(고래 매수액·목표가·
# 지지선 등 구체 정보가 있다는 뜻). 길면 서술형 분석일 수 있으니 역시 통과.
_DENSITY_MIN_LEN = 100          # 이 길이 이상이면 수치가 없어도 통과(서술형 분석)
# "수치"는 **가격다운 수치**여야 한다. 그냥 \d 로 보면 타임프레임 하나로 통과해
# 버린다 — 실측 "$NEOUSDT 업데이트: 30분 / 이 추세선은 곧 새로운 기회를 창출할
# 것입니다"(85자)가 '30분' 때문에 살아남아 브리핑에 실렸는데, 읽어도 가격이
# 얼마인지·무엇을 하라는지가 없다. 달러 표기·소수점 가격·천단위 수만 인정한다.
_NUMBER_RX = re.compile(
    r"[\$￦]\s?[\d,]+(?:\.\d+)?"                  # $9,000,000 · ￦1,200
    r"|\d+\.\d+"                                 # 0.9363 · 249.2 (소수 = 가격/비율)
    r"|\d[\d,]{3,}"                              # 9,000 · 2540 (천단위)
    r"|\d+(?:\.\d+)?\s*(?:%|퍼센트|percent)"      # 249.2 percent
    # 2026-09-27 S0 — 금액 단위 표기. 영문 "$1.69B"/"1.69B BTC" 와, 번역기가 만든
    # 한글 수 표기 "16억 9천만" 을 수치로 인정한다(실측: 유일한 ETF 자금 뉴스가
    # "16억 9천만 개" 로 번역돼 이 게이트에 걸려 버려졌다).
    r"|\d+(?:\.\d+)?\s?(?:[BM]\b|billion|million)"
    r"|\d+\s?(?:억|천만|백만)"
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


# 진입 시그널 글 필터 (2026-09-14) — 위 _RESULT_PATTERNS 가 '지난 거래 자랑'을
# 막는다면 이쪽은 '앞으로의 매매 지시'를 막는다. 둘 다 뉴스가 아니다.
#
# 왜 필요했나: extractor.parse_setup 이 포맷을 못 읽으면 그 글은 "셋업 아님"으로
# 뉴스 경로에 떨어진다. 실측(09-14 브리핑 큐 id=1)에서 아래 글이 시황 뉴스로
# 실렸다 — "❤️❤️❤️FREE SIGNAL!❤️❤️❤️ / Instrument: LSKUSDT / My opinion: BUY /
# Entry: $0.83 / Target: $0.842 / RRR: 1:3". 파싱 실패한 시그널이 뉴스로 둔갑한
# 전형이다. 실시간 발송 때는 하루에 흩어져 눈에 덜 띄었는데 브리핑 5줄에 모으니
# 노이즈가 그대로 드러났다.
#
# 점수제인 이유는 _RESULT_PATTERNS 와 같다 — 단독 단어로 끊으면 정상 시황글이
# 함께 죽는다("entry point for buyers", "price target of $5" 는 뉴스에도 흔하다).
# 그래서 **라벨 형태(콜론이 붙은 지시문)** 와 시그널 전용 어휘만 본다.
# 번역 전 원문에 적용되지만, 한글 채널·번역본 재검사까지 커버하도록 양쪽을 넣었다.
_SETUP_PATTERNS = (
    # ── 고정밀(2점): 시그널 카드에만 나오는 어휘 ──
    (2, re.compile(r"\bfree\s+signals?\b|무료\s*신호", re.I)),
    (2, re.compile(r"\binstrument\s*:|기기\s*:", re.I)),
    (2, re.compile(r"\brrr\s*[:=]|\br\s*:\s*r\s*[:=]|위험\s*보상\s*비", re.I)),
    (2, re.compile(r"\bmy\s+opinion\s*:|내\s*의견\s*:", re.I)),
    (2, re.compile(r"\b(?:buy|sell)\s+(?:zone|setup)\s*:", re.I)),
    # ── 보조(1점): 라벨 형태일 때만 — 본문 속 같은 단어와 구분된다 ──
    (1, re.compile(r"\bentry\s*(?:price|point)?\s*:|진입가?\s*:|입장료\s*:", re.I)),
    (1, re.compile(r"\btargets?\s*\d*\s*:|목표가?\s*:|경유지\s*:", re.I)),
    (1, re.compile(r"\b(?:stop\s*-?\s*loss|sl)\s*:|손절가?\s*:", re.I)),
    (1, re.compile(r"\btake\s*-?\s*profits?\s*\d*\s*:|\btp\s*\d*\s*:", re.I)),
    (1, re.compile(r"\bleverage\s*:|레버리지\s*:", re.I)),
    # 2026-09-17 추가 — 실측(@BitmexSignalsFee)에서 **번역된 시그널 카드**가
    # 그대로 통과했다: "📍신호 ID: #2227📍 / 코인: $JUP/USDT (2-5X) / 방향: 긴 /
    # 정지 손실: 0.2160 / 🚫20% 손실(2x)🚫". 무료 번역기가 Stop Loss 를 "정지
    # 손실", Long 을 "긴"으로 옮기는 바람에 기존 한글 라벨(`손절가:`)이 전부
    # 빗나갔다. 번역 변형까지 라벨로 잡는다.
    (2, re.compile(r"신호\s*ID|\bsignal\s*id\b", re.I)),          # 시그널 채널 전용 메타
    (1, re.compile(r"정지\s*손실\s*[:：]|스탑\s*로스\s*[:：]", re.I)),
    (1, re.compile(r"^\s*코인\s*[:：]|\n\s*코인\s*[:：]|^\s*방향\s*[:：]|\n\s*방향\s*[:：]", re.I)),
    (1, re.compile(r"\(\s*\d+\s*-\s*\d+\s*[xX]\s*\)|\(\s*\d+\s*[xX]\s*\)"), ),  # (2-5X)·(2x)
)
_SETUP_SCORE_MIN = 3


def _is_trade_setup(text: str) -> bool:
    """진입 시그널 글 판정 — 점수 3 이상이면 True (뉴스 경로에서 제외).

    문턱이 리캡(2)보다 높은 이유: 보조 패턴이 라벨 형태라도 분석글에 한둘은
    섞일 수 있다("Key levels — Support: 0.79"). 고정밀 1개 + 보조 1개,
    또는 보조 3개가 모여야 시그널 카드로 본다."""
    score = 0
    for pts, rx in _SETUP_PATTERNS:
        if rx.search(text):
            score += pts
            if score >= _SETUP_SCORE_MIN:
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


# ── 시총 순위 게이트 (2026-10-09 대표 요청 "시총 200위 안쪽 애들만") ─────────
# 순위 출처 = 유니버스 캐시(collector/coingecko.build_universe 가 쓰는 universe_cache_path,
# {updated_at, universe: [{symbol, rank, ...}]}). rank 는 CoinGecko 시총 순위, top-N 밖이면 None.
# 수집(maybe_send_news_brief)과 브리핑 렌더(morning_brief._news_items)가 같은 함수를 쓴다.
_ROOT = Path(__file__).resolve().parent.parent
_RANK_CACHE: dict = {"key": None, "ranks": None}


def load_mcap_ranks() -> Optional[dict]:
    """{SYMBOL: rank|None}. 캐시 파일이 없거나 읽기 실패면 None(= 판정 불가).
    파일 경로·수정 시각이 같으면 다시 읽지 않는다(회차당 수십 건 호출)."""
    raw = settings.get("universe_cache_path") or ""
    if not raw:
        return None
    path = Path(raw)
    if not path.is_absolute() and not path.exists():
        path = _ROOT / raw          # 작업 디렉터리가 저장소 루트가 아닐 때
    try:
        key = (str(path), os.stat(path).st_mtime)
    except OSError:
        return None
    if _RANK_CACHE["key"] == key:
        return _RANK_CACHE["ranks"]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        uni = data.get("universe") if isinstance(data, dict) else data
        ranks = {}
        for c in uni or []:
            s = str(c.get("symbol") or "").upper()
            if s:
                rk = c.get("rank")
                ranks[s] = int(rk) if isinstance(rk, (int, float)) else None
    except Exception as e:  # noqa: BLE001 — 순위 판정 불가 → 호출부 fail-open
        logger.warning("[news] 유니버스 순위 읽기 실패(제한 없이 진행): %s", e)
        return None
    _RANK_CACHE["key"], _RANK_CACHE["ranks"] = key, ranks
    return ranks


_STABLECOINS = frozenset({"USDT", "USDC", "USDG", "USD1", "USDE", "USDS", "DAI", "FDUSD", "PYUSD",
                          "TUSD", "RLUSD", "USDD", "USDP", "GUSD", "EURC", "USDF", "USDX"})


def mcap_rank_ok(symbol: str) -> bool:
    """뉴스 대상 코인인가 — 시총 순위 ≤ news_max_mcap_rank.

    · 🌐 시장(MARKET)·설정 0 → 항상 True.
    · 캐시 파일 없음/읽기 실패 → True(fail-open — 순위를 모른다고 뉴스를 끊지 않는다).
    · 캐시에 **없는** 심볼(캐시 갱신 전 신규 상장·테스트 가짜 심볼) → True(판정 불가).
    · 캐시에 있는데 rank 가 None(CoinGecko top-N 밖) 또는 상한 초과 → False."""
    try:
        max_rank = int(settings.get("news_max_mcap_rank") or 0)
    except (TypeError, ValueError):
        max_rank = 0
    sym = (symbol or "").upper()
    # 스테이블코인은 시세가 움직이지 않아 "상승/하락 재료"가 의미 없다(10-09 미리보기: USDG 🟢 상승 재료).
    if sym in _STABLECOINS:
        return False
    if max_rank <= 0 or not sym or sym == news_parse.MARKET_SYMBOL:
        return True
    ranks = load_mcap_ranks()
    if ranks is None or sym not in ranks:
        return True
    rk = ranks[sym]
    return rk is not None and rk <= max_rank


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
    """뉴스 요약 알림 발송 시도. 반환 "skipped"|"ok"|"queued"|"failed".

    호출부(run_collect)는 심볼 매칭 성공 + parse_setup 실패인 게시글만 여기 넘긴다.
    상한·쿨다운·최소 길이 미달·설정 OFF 는 조용히 skipped.
    "queued" (2026-09-13 A안): news_alert_send_enabled=False — 실시간 발송 대신
    news_digest_queue 적재. 상한 카운트(record_alert)는 종전대로 남는다."""
    if not settings.get("news_alert_enabled"):
        return "skipped"

    if symbol.upper() in _AMBIGUOUS_SYMBOLS:
        # 2026-09-27 S3: 모호 심볼로 잡힌 글은 사실상 '코인 미해석' 글이다 — 🌐 시장
        # 뉴스 판정만 한 번 더 본다. 실측: Bitget $351.6M 해킹 글(wolfoftrading/6448)이
        # 본문의 "AI likely identified the flaw" 때문에 AI 코인으로 잡혀 버려졌다.
        logger.debug("[news] %s 모호 심볼 — 코인 뉴스로는 스킵, 시장 뉴스 판정", symbol)
        return _maybe_market_news(conn, post, channel, now)

    # 신선도 가드 (2026-09-27 대표 결정 "48시간 이내") — 티커 경로도 게시 경과 상한.
    if _older_than(post, now, "news_max_age_hours"):
        logger.debug("[news] %s 게시 경과 초과 — 스킵", symbol)
        return "skipped"

    # 시총 순위 게이트 (2026-10-09 대표 요청 "시총 200위 안쪽만") — 상한 판정보다 **앞**에서
    # 걸러 순위 밖 코인 글이 하루 상한(전체·채널)과 코인 쿨다운을 먹지 않게 한다.
    if not mcap_rank_ok(symbol):
        logger.debug("[news] %s 시총 순위 %s위 밖 — 스킵", symbol,
                     settings.get("news_max_mcap_rank"))
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

    if _is_trade_setup(text):
        logger.debug("[news] %s 진입 시그널 글 스킵(파싱 실패한 시그널)", symbol)
        return "skipped"

    # 정보 밀도 게이트 (2026-09-16) — 위 상수 주석 참고. 판정은 **번역 전 원문**
    # (text)으로 한다(2026-09-27 S0 재확인 — 번역은 아래에서 게이트 통과 후에만).
    if len(text) < _DENSITY_MIN_LEN and not _NUMBER_RX.search(text):
        logger.debug("[news] %s 정보 밀도 미달 스킵(%d자, 수치 없음)", symbol, len(text))
        return "skipped"

    # 원문 자찬·홍보 노이즈 (2026-09-27 뉴스 v2) — "Perfectly followed our prediction",
    # "going for it … Enjoy." 류. 정보가 0인데 종전엔 큐에 들어가 하루 5건 쿼터를
    # 먹었다. 수집 단계에선 **명시적 자찬·홍보 패턴만** 뺀다 — "유형·금액 없는 일반론"
    # 판정은 렌더 단계(_news_items)가 맡는다(여기서 빼면 v2 스위치를 끈 날 종전
    # 렌더가 볼 후보까지 사라진다). v2 스위치 OFF 면 종전 동작 유지.
    if settings.get("news_structured_enabled"):
        try:
            if news_parse.NOISE_RX.search(news_parse.clean(text)):
                logger.debug("[news] %s 원문 자찬·홍보 노이즈 스킵", symbol)
                return "skipped"
        except Exception as e:  # noqa: BLE001 — 파서 실패는 종전 경로로
            logger.warning("[news] %s 원문 파싱 실패(무시): %s", symbol, e)

    now = now if now is not None else time.time()
    url = post.get("url") or ""

    # 재적재 차단 (2026-09-27 S0) — 이미 큐에 들어갔거나 소비(브리핑·실시간 발송)된
    # 글은 다시 넣지 않는다. db.news_url_seen docstring 참고(09-21~27 큐 47% 재적재).
    # 상한 판정보다 앞에 둔다 — 중복 글이 쿨다운·채널 상한 카운트를 먹지 않게.
    try:
        if db.news_url_seen(conn, url, symbol):
            logger.debug("[news] %s 이미 적재·발송된 글 스킵: %s", symbol, url)
            return "skipped"
    except Exception as e:  # noqa: BLE001 — 조회 실패는 종전 동작(쿨다운)에 맡긴다
        logger.warning("[news] %s URL 중복 조회 실패(무시): %s", symbol, e)
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
    try:
        label = "🌐시장" if symbol == news_parse.MARKET_SYMBOL else symbol
        text_out = telegram.render_news_brief(label, channel, summary, url)
    except Exception as e:  # noqa: BLE001
        logger.warning("[news] %s render 실패: %s", symbol, e)
        return "failed"

    today = day_kst(now)

    # 발송 스위치 (2026-09-13 A안) — OFF 면 telegram.send 대신 대기열 적재.
    # 상한·쿨다운·필터·요약·번역은 위에서 전부 종전대로 돌았으므로 큐에 들어온
    # 건은 "종전이라면 실시간 발송됐을 건" 그 자체다. record_alert 도 종전대로
    # 남겨 상한 카운트(글로벌 5/일·채널 2/일·코인 24h)를 그대로 유지한다 —
    # 큐가 상한을 넘어 불어나면 브리핑 5줄 컷이 무의미해진다.
    if not settings.get("news_alert_send_enabled"):
        try:
            db.queue_news_digest(conn, symbol, channel, summary, url, today, now,
                                 summary_en=text[:_EN_KEEP_MAX],
                                 posted_at=post.get("published_at"))
        except Exception as e:  # noqa: BLE001 — 회차 생존 최우선
            logger.warning("[news] %s 큐 적재 실패: %s", symbol, e)
            return "failed"
        _record(conn, symbol, channel, today, now, sent=0)
        logger.debug("[news] %s 발송 스위치 OFF — 브리핑 큐 적재", symbol)
        return "queued"

    try:
        sent_mid = telegram.send(text_out, urgency="low")
    except Exception as e:  # noqa: BLE001
        logger.warning("[news] %s 발송 예외: %s", symbol, e)
        return "failed"
    if not sent_mid:
        return "failed"

    _record(conn, symbol, channel, today, now, sent=1)
    # 실시간 발송분도 URL 원장에 남긴다(consumed=1 — 브리핑 후보에는 안 섞인다).
    # 발송 스위치를 켠 기간에도 같은 글 재발송을 막기 위해서다(2026-09-27 S0).
    try:
        db.queue_news_digest(conn, symbol, channel, summary, url, today, now,
                             summary_en=text[:_EN_KEEP_MAX],
                             posted_at=post.get("published_at"), consumed=1)
    except Exception as e:  # noqa: BLE001 — 발송은 성공, 원장만 실패
        logger.warning("[news] %s URL 원장 기록 실패: %s", symbol, e)
    return "ok"


# ── S3: 티커 없는 글 — 코인 이름 매칭 · 🌐 시장 뉴스 (2026-09-27) ─────────
# run_collect 의 "심볼 미해석" 분기(n_unmatched)에서 호출한다. 종전엔 그 글들이 전부
# 버려졌다 — 2개월 389건 중 사실형 18건(Fed 25bp 인상·CLARITY 법안 불발·ETF 주간
# 자금·"Cronos" 이름만 있는 $74M 해킹 등, 기획안 §1-6).
_NAME_INDEX_CACHE: dict = {"key": None, "idx": {}}


def _name_index(universe) -> dict:
    key = id(universe), len(universe or ())
    if _NAME_INDEX_CACHE["key"] != key:
        _NAME_INDEX_CACHE["key"] = key
        _NAME_INDEX_CACHE["idx"] = news_parse.build_name_index(universe)
    return _NAME_INDEX_CACHE["idx"]


def maybe_send_unmatched_news(conn, post: dict, channel: str, universe,
                              now: Optional[float] = None) -> str:
    """심볼 미해석 글의 뉴스 경로. 반환 "skipped"|"ok"|"queued"|"failed".

    ① 코인 **이름** 매칭(Cronos→CRO 등, news_parse.match_coin_name 오탐 규칙) 성공 →
       일반 뉴스 경로(maybe_send_news_brief)와 **같은 게이트·상한**으로 처리.
    ② 아니면 news_market_enabled 일 때 🌐 시장 뉴스 판정(연준·ETF 자금·규제·거래소
       해킹, news_parse.is_market_news) → 심볼 MARKET 으로 같은 경로에 태운다.
       상한도 그대로다 — 글로벌 5/일·채널 2/일, 코인 쿨다운은 MARKET 1개 키로 걸려
       🌐 항목은 하루 1건이 된다(블록 5건 안에서 코인 뉴스를 밀어내지 않게).
    모든 실패 격리 — 호출부는 예외를 로그만 남긴다."""
    if not settings.get("news_alert_enabled"):
        return "skipped"
    text = _post_text(post)
    if not text:
        return "skipped"
    if _too_old_for_unmatched(post, now):
        return "skipped"
    sym = news_parse.match_coin_name(text, _name_index(universe))
    if sym and sym.upper() not in _AMBIGUOUS_SYMBOLS:
        return maybe_send_news_brief(conn, post, sym, channel, now=now)
    return _maybe_market_news(conn, post, channel, now)


def _older_than(post: dict, now: Optional[float], key: str) -> bool:
    """게시 경과가 settings[key] 시간을 넘으면 True (0/미설정이면 제한 없음)."""
    try:
        max_age_h = float(settings.get(key) or 0)
    except (TypeError, ValueError):
        max_age_h = 0.0
    pub = post.get("published_at")
    ref = now if now is not None else time.time()
    return bool(max_age_h > 0 and pub and ref - float(pub) > max_age_h * 3600)


def _too_old_for_unmatched(post: dict, now: Optional[float]) -> bool:
    """신선도 가드 — 티커 없는 글 경로(이름 매칭·🌐 시장)는 2026-09-27 신설이라 배포 첫
    회차에 수집 창(7일) 안의 옛 글이 한꺼번에 들어올 수 있다(실측 재생: 2.6일 지난
    Bitget 해킹 글이 '단기' 재료로 실림). news_unmatched_max_age_hours 안의 글만."""
    try:
        max_age_h = float(settings.get("news_unmatched_max_age_hours") or 0)
    except (TypeError, ValueError):
        max_age_h = 0.0
    pub = post.get("published_at")
    ref = now if now is not None else time.time()
    return bool(max_age_h > 0 and pub and ref - float(pub) > max_age_h * 3600)


def _post_text(post: dict) -> str:
    title = (post.get("title") or "").strip()
    desc = (post.get("description") or "").strip()
    if title and desc:
        return desc if desc.startswith(title) else (title + "\n" + desc).strip()
    return title or desc


def _maybe_market_news(conn, post: dict, channel: str, now: Optional[float] = None) -> str:
    """🌐 시장 뉴스 판정 → 맞으면 심볼 MARKET 으로 일반 뉴스 경로(같은 게이트·상한)."""
    if not (settings.get("news_market_enabled") and settings.get("news_structured_enabled")):
        return "skipped"
    if _too_old_for_unmatched(post, now):
        return "skipped"
    text = _post_text(post)
    if not text:
        return "skipped"
    # 광고·리캡·시그널 글은 시장 뉴스 판정 전에 버린다(아래 경로가 다시 보긴 하지만
    # 파서가 광고의 "$500 airdrop" 을 금액으로 보는 일을 원천 차단).
    low = text.lower()
    if any(kw in low for kw in _PROMO_KEYWORDS) or _is_trade_result(text) or _is_trade_setup(text):
        return "skipped"
    try:
        parsed = news_parse.parse(text)
    except Exception as e:  # noqa: BLE001
        logger.warning("[news] 시장 뉴스 파싱 실패(무시): %s", e)
        return "skipped"
    if not news_parse.is_market_news(parsed):
        return "skipped"
    return maybe_send_news_brief(conn, post, news_parse.MARKET_SYMBOL, channel, now=now)


def _record(conn, symbol: str, channel: str, today: str, now: float,
            sent: int) -> None:
    """alerts_log 기록 — 기록 실패가 발송/적재를 되돌리지는 않는다.
    kind='news' + level_ids 필드에 채널명 저장 (뉴스는 레벨과 무관해 재활용)."""
    try:
        db.record_alert(conn, symbol, _NEWS_KIND, [channel], today, now, sent=sent)
    except Exception as e:  # noqa: BLE001 — 발송/적재는 성공, 기록만 실패
        logger.warning("[news] %s 기록 실패: %s", symbol, e)
