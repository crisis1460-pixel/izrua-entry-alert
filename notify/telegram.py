"""
텔레그램 알림 — izrua_watcher notifier.py 양식 이식판 (2026-07-23 사용자 지시).

워쳐에서 그대로 가져온 것:
- ━━━ 구분선으로 섹션 나누기, parse_mode=HTML
- 타점 블록: 4칸 들여쓰기, "$USD (KRW원)" 병기, 손절/목표에 엔트리 대비 ±%
- KRW 는 1원 이상이면 반올림 정수(콤마), 1원 미만 소수 유지 (사용자 확정: 원단위 반올림)
- 작성자 평균 적중률 라인 (📊 ... % (총 N건 기반))
- 🌍 BTC.D / 🪙 ALT.S(매수 라벨) / 😨 F&G(한국어 라벨) 행
- 출처는 URL 노출 없이 <a>출처1</a> · <a>출처2</a> 하이퍼링크

발송 원칙: 실패는 삼키고 로그만 (알림 실패가 잡을 막으면 안 됨). 토큰은 env 전용.

2026-07-26 수리 3건:
- 재시도: 429(레이트리밋)는 응답의 retry_after(상한 10초) 대기 후 1회, 5xx/타임아웃은
  1~2초 대기 후 재시도. 총 재시도 최대 _RETRY_MAX 회 - 소진 후에만 기존처럼 "삼키고
  False". 정책 상수는 아래 모듈 상수로 고정.
- 토큰 마스킹: requests 예외 문자열엔 요청 URL(봇 토큰 포함)이 그대로 실려있을 수
  있어, 그걸 그대로 logger.error 에 넘기면 공개 레포 Actions 로그에 토큰이 노출된다
  (collector/tradingview.py 의 '값은 절대 로그에 남기지 않는다' 원칙과 동일 취지) -
  로그 직전 토큰 문자열을 '***' 로 치환한다.
- urgency 파라미터(인프라만 신설, 2026-07-26): send(text, urgency="high"|"low").
  "low" 면 disable_notification=true 로 무음 전송. 기본값 "high" 는 현행(유음)과
  동일 - 호출부는 아직 수정하지 않는다(활성화는 사용자 확정 후 별도 작업).
"""

import html
import json
import logging
import re
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional

import requests

from analytics import calibration, ranking, weekly
from config import settings

logger = logging.getLogger("alert.telegram")

_API = "https://api.telegram.org/bot{token}/sendMessage"
# 결과 이모지 반응 (2026-09-22 Q4) — 기존 봇 토큰 그대로, 추가 인증 없음.
_REACTION_API = "https://api.telegram.org/bot{token}/setMessageReaction"
# 2026-08-08 사용자 결정: 그룹 채팅 전환 후 폭이 좁아져 20자 구분선이
# 자동 줄바꿈되던 것을 축소. 실측 역산(스크린샷상 "    목표:  18,265원
# (+15.6%)  1/2단" 까지 표시되고 "계" 만 다음 줄로 넘어감 — East Asian
# Width 기준 그 문자열 표시폭 36칼럼, ━ 는 유니코드 공식 분류(1칸)와 달리
# 이 폰트에서 와이드(2칸)로 렌더링) 로 이론상 안전선은 18개. 17개로 설정
# (사용자 재조정 — 16에서 여유 있음을 확인 후 1개 상향).
SEP = "━━━━━━━━━━━━━━━━━"
_SEP = SEP

# 그룹 채팅 폭 기준 한 줄 최대 표시폭(칼럼). 2026-08-08 사용자 결정: 이 폭을
# 넘는 행은 줄바꿈하지 말고 넘는 지점 "직전까지"만 표시하고 나머지는 표기
# 없이 자른다(말줄임표 "…" 도 안 붙임 — 사용자가 명시적으로 표기하지 말라고
# 확정).
#
# 2026-08-08 재조정(실사고): 위 _SEP 산정 때는 36칼럼까지 안전하다고
# 봤지만, 실제 샘플 발송('✍️ 작성자: @mastercrypto2020 ⭐⭐' — 이모지가
# 몇 개 섞인 줄)은 계산상 표시폭 33인데도 실제로 줄바꿈됐다. 이모지 조합·
# 폰트에 따라 칼럼 근사가 완벽하지 않다는 뜻 — 사용자가 "무조건 줄내림
# 금지"를 우선하므로, 알려진 실패 사례(33)보다 낮게 32로 낮춰 안전 여유를
# 둔다. _SEP(34칼럼) 자체는 이 예산보다 넓지만 절삭 대상에서 제외한다
# (아래 _truncate_line 참고) — 구분선은 고정 상수라 절삭하면 오히려
# 짧아진 구분선이라는 새 버그가 된다.
_MAX_LINE_COLS = 32


def _display_width(ch: str) -> int:
    """문자 1개의 표시폭(칼럼). East Asian Width 'W'/'F' 는 2칸.
    2026-08-08 1차: ━(U+2501) 는 공식 분류가 Ambiguous(1칸)지만 실측상 이
    폰트에서 와이드로 렌더링돼(_SEP 주석 참고) 예외 처리했었다.
    2026-08-08 2차(실사고): ✍/❄ 같은 이모지도 문자 자체 EAW 분류는 Neutral
    이지만 화면엔 다른 이모지처럼 2칸 폭으로 렌더된다 — 실측(샘플 알림에서
    '✍️ 작성자: @x ⭐⭐' 줄이 예상 표시폭 33 < 예산 36 인데도 줄내림됨) 근거로
    Unicode 카테고리 'So'(Symbol,other, 이모지 대부분이 속함) 전체를 2칸으로
    승격 — 개별 이모지 화이트리스트보다 안정적이다. 가변폭 변형 선택자
    U+FE0F 는 화면에 아무것도 추가하지 않으므로 0칸."""
    if ch == "️":  # VS-16(이모지 스타일 강제) - 화면 폭 없음
        return 0
    if unicodedata.category(ch) == "So":
        return 2
    w = unicodedata.east_asian_width(ch)
    return 2 if w in ("W", "F") else 1


_TAG_RE = re.compile(r"<[^>]+>")
_TAG_NAME_RE = re.compile(r"</?([a-zA-Z][a-zA-Z0-9]*)")


def _truncate_line(line: str, max_cols: int = _MAX_LINE_COLS) -> str:
    """HTML(parse_mode) 태그를 보존하면서 표시폭 max_cols 를 넘는 지점에서
    잘라낸다(말줄임표 없음 — 사용자 결정). 태그 자체는 폭 계산에서 제외하고
    항상 통째로 통과시키며, 절단 시점에 열려 있던 태그는 닫아 HTML 이 깨지지
    않게 한다. 2026-08-08 사용자 결정: 모든 알림 행에 공통 적용.

    _SEP 는 예외 — 구분선은 이 예산(32)보다 넓게(34) 고정 산정된 상수라
    이 함수를 그대로 태우면 짧아진 구분선이 나가는 새 버그가 된다."""
    if line == _SEP:
        return line
    total = 0
    out = []
    open_tags: list = []
    pos = 0
    for m in _TAG_RE.finditer(line):
        for ch in line[pos:m.start()]:
            cw = _display_width(ch)
            if total + cw > max_cols:
                out.extend(f"</{t}>" for t in reversed(open_tags))
                return "".join(out)
            out.append(ch)
            total += cw
        tag = m.group(0)
        out.append(tag)
        nm = _TAG_NAME_RE.match(tag)
        if nm:
            name = nm.group(1)
            if tag.startswith("</"):
                if open_tags and open_tags[-1] == name:
                    open_tags.pop()
            elif not tag.endswith("/>"):
                open_tags.append(name)
        pos = m.end()
    for ch in line[pos:]:
        cw = _display_width(ch)
        if total + cw > max_cols:
            out.extend(f"</{t}>" for t in reversed(open_tags))
            return "".join(out)
        out.append(ch)
        total += cw
    out.extend(f"</{t}>" for t in reversed(open_tags))
    return "".join(out)


def _cur_price_lines(krw: float, change_rate_24h: float = None) -> list:
    """'현재가:' 행을 폭에 따라 1-2행으로 반환. 32칼럼 초과 시 (전일 ...) 분리.
    거래량 급증·OI 급증 알림 공통 헬퍼 — 두 곳 동일 룰 유지."""
    _cp = f"{krw:,.0f}" if krw >= 1 else f"{krw:.4f}"
    if change_rate_24h is None:
        return [f"    현재가:  {_cp}원"]
    _sign = "+" if change_rate_24h >= 0 else ""
    _pct = f"(전일 {_sign}{change_rate_24h*100:.2f}%)"
    _single = f"    현재가:  {_cp}원  {_pct}"
    if sum(_display_width(c) for c in _single) > _MAX_LINE_COLS:
        return [f"    현재가:  {_cp}원", f"    {_pct}"]
    return [_single]


def _finalize(lines: list) -> str:
    """모든 렌더러의 최종 join 지점 — 각 행을 _truncate_line 으로 다듬은 뒤
    개행 결합한다(2026-08-08 사용자 결정: 전체 행 공통 적용).
    출처(🔗) 행과 구분선(_SEP)은 잘림 면제 — 하이퍼링크 태그 중간 절단 방지."""
    _PRICE_PREFIXES = ("현재:", "진입:", "목표:", "손절:",
                       "현재가:", "달성가:", "다음 목표:", "다음 TP:",
                       "최근 1시간:", "20시간 평균:", "1시간 전:", "지금:",
                       "TP", "(전일 ")
    result = []
    for ln in lines:
        if ln.startswith("🔗 ") or ln == _SEP:
            result.append(ln)
        elif ln.lstrip().startswith(_PRICE_PREFIXES):
            result.append(_truncate_line(ln, max_cols=50))
        else:
            result.append(_truncate_line(ln))
    return "\n".join(result)


def _source_line(post_urls) -> str:
    """출처 라인 (URL 노출 없이 하이퍼링크, 최대 5, 원 render_alert 로직을
    2026-08-08 재사용화 — TP 적중·거래량 급증 알림에도 같은 방식으로 적용).
    post_urls: URL 문자열(또는 None) 이터러블. None/빈 값은 링크 없이
    "출처N" 텍스트만 표시(전체 URL 결측이어도 행 자체는 항상 나온다)."""
    urls = list(post_urls)
    links = []
    for i, url in enumerate(urls[:5], 1):
        u = html.escape(url or "", quote=True)
        links.append(f'<a href="{u}">출처{i}</a>' if u else f"출처{i}")
    line = "🔗 " + " · ".join(links)
    if len(urls) > 5:
        line += f" · 외 {len(urls) - 5}건"
    return line

# ── 발송 레이트리밋 (2026-08-13) ────────────────────────────────────
_SEND_MIN_INTERVAL_SEC = settings.get("telegram_send_min_interval_sec")
_last_send_at = 0.0

# ── 재시도 정책 상수 (2026-07-26 수리) ──────────────────────────────
_RETRY_MAX = 2                  # 총 재시도 최대 횟수(최초 시도 제외)
_RETRY_429_MAX_WAIT_SEC = 10.0  # 429 retry_after 상한
_RETRY_BACKOFF_SEC = (1.0, 2.0)  # 5xx/타임아웃 - 재시도 회차별 대기(1회차 1초, 2회차 2초)

FNG_KR = {
    "Extreme Fear": "극공포",
    "Fear": "공포",
    "Neutral": "중립",
    "Greed": "탐욕",
    "Extreme Greed": "극탐욕",
}
_FNG_KR = FNG_KR


def _redact(msg: str, token: str) -> str:
    """토큰 문자열을 로그에서 지운다(2026-07-26 수리) - requests 예외/응답 문자열엔
    요청 URL(bot{token}/...)이 그대로 실려있을 수 있어 무심코 로그에 남기면 공개
    레포 Actions 로그에 봇 토큰이 노출된다."""
    if not token or not msg:
        return msg
    return msg.replace(token, "***")


def _retry_after_sec(resp) -> float:
    """429 응답 바디의 retry_after(초). 파싱 실패/미포함 시 상한값으로 보수적 대체."""
    try:
        val = resp.json().get("parameters", {}).get("retry_after")
        if val is not None:
            return min(float(val), _RETRY_429_MAX_WAIT_SEC)
    except Exception:  # noqa: BLE001 - 파싱 실패는 재시도 자체를 막지 않는다
        pass
    return _RETRY_429_MAX_WAIT_SEC


_TG_MAX_LEN = 4096

# 김프 급변 화살표 임계 (%p, ~6h 대비) — 2026-08-14. 0.5%p 는 평시 김프
# 변동폭(시간당 0.1%p 미만) 대비 뚜렷한 이동으로, 국내 FOMO/리스크오프
# 전환 감지 관례 수준.
_KIMCHI_DELTA_TH = 0.5


def send(text: str, urgency: Literal["high", "low"] = "high",
         reply_to_message_id: Optional[int] = None) -> Optional[int]:
    """HTML 모드 발송. 성공 시 telegram message_id(int>0), 실패 시 None.

    반환 타입 변경 이력(2026-08-17 #6 스레딩): 종전 bool 반환 → Optional[int].
    기존 호출부 `if telegram.send(...)` 는 int>0/None 모두 bool 컨텍스트에서 동일
    하게 동작(양수 truthy)하므로 하위 호환 유지. message_id 를 사용하려는 신규
    호출부만 정수로 저장(preview→touch 스레딩용, storage/db.py preview_message_id).

    reply_to_message_id: 원 메시지에 답글로 붙여 발송 — preview 알림의 message_id
    를 넘기면 touch 알림이 UI 에서 스레드로 연결(사용자 UX 개선). None(기본) 은
    최상위 메시지로 발송.

    4096자 초과 시 구분선(_SEP) 기준으로 자동 분할 전송한다.

    urgency: 'high'(기본, 유음) | 'low'(disable_notification=true, 무음).

    재시도(2026-07-26): 429 는 retry_after(상한 10초) 대기 후, 5xx/타임아웃·연결
    오류는 1~2초 대기 후 재시도. 총 재시도 최대 _RETRY_MAX 회, 소진하면 기존처럼
    조용히 None."""
    if len(text) > _TG_MAX_LEN:
        # 분할 발송은 스레딩 대상 아님(첫 chunk 만 답글로 붙이면 나머지 어긋난다).
        return _split_send(text, urgency)

    global _last_send_at
    elapsed = time.time() - _last_send_at
    if elapsed < _SEND_MIN_INTERVAL_SEC:
        logger.debug("[tg] 속도제한 대기 %.1f초", _SEND_MIN_INTERVAL_SEC - elapsed)
        time.sleep(_SEND_MIN_INTERVAL_SEC - elapsed)

    token = settings.secret("TELEGRAM_BOT_TOKEN")
    chat_id = settings.secret("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        logger.warning("[tg] 토큰/chat_id 미설정 - 발송 생략 (내용 %d자)", len(text))
        return None

    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
               "disable_web_page_preview": True}
    if urgency == "low":
        payload["disable_notification"] = True
    if reply_to_message_id:
        payload["reply_to_message_id"] = reply_to_message_id
        # 원 메시지가 삭제됐어도 답글 자체는 발송 (Telegram 관례) — 원장 스레드는
        # 끊어져도 알림 자체는 잃지 않는 게 우선.
        payload["allow_sending_without_reply"] = True

    retry = 0
    while True:
        try:
            resp = requests.post(_API.format(token=token), json=payload,
                                  timeout=settings.get("http_timeout_sec"))
        except requests.RequestException as e:
            if retry < _RETRY_MAX:
                wait = _RETRY_BACKOFF_SEC[min(retry, len(_RETRY_BACKOFF_SEC) - 1)]
                logger.warning("[tg] 발송 실패(%s) - %.0f초 후 재시도(%d/%d)",
                               type(e).__name__, wait, retry + 1, _RETRY_MAX)
                time.sleep(wait)
                retry += 1
                continue
            logger.error("[tg] 발송 실패(재시도 소진): %s", _redact(str(e), token))
            return None

        if resp.status_code == 200:
            _last_send_at = time.time()
            # message_id 파싱 실패 시에도 발송은 성공했으므로 truthy 값 반환 (>0 보장 위해 -1 회피)
            try:
                mid = resp.json().get("result", {}).get("message_id")
                return int(mid) if mid else -1
            except (ValueError, TypeError, KeyError):
                return -1

        if resp.status_code == 429 and retry < _RETRY_MAX:
            wait = _retry_after_sec(resp)
            logger.warning("[tg] 429 레이트리밋 - %.1f초 후 재시도(%d/%d)",
                           wait, retry + 1, _RETRY_MAX)
            time.sleep(wait)
            retry += 1
            continue

        if resp.status_code >= 500 and retry < _RETRY_MAX:
            wait = _RETRY_BACKOFF_SEC[min(retry, len(_RETRY_BACKOFF_SEC) - 1)]
            logger.warning("[tg] %s - %.0f초 후 재시도(%d/%d)",
                           resp.status_code, wait, retry + 1, _RETRY_MAX)
            time.sleep(wait)
            retry += 1
            continue

        logger.error("[tg] 발송 실패 status=%s body=%s", resp.status_code,
                     _redact(resp.text[:200], token))
        return None


def set_reaction(message_id: int, emoji: str) -> bool:
    """이미 보낸 메시지에 이모지 반응 1개를 단다 (2026-09-22 Q4). 성공 True.

    Telegram Bot API `setMessageReaction` — 기존 봇 토큰·chat_id 를 그대로 쓰고
    추가 인증이 없다. 봇은 메시지당 반응 1개라 **새 반응이 이전 것을 교체**한다
    (👍 → 🏆). is_big=False (큰 애니메이션 없이 조용히).

    ⚠️ 이모지는 Telegram 이 정한 허용 목록(ReactionTypeEmoji)에서만 고를 수 있다 —
    ✅·❌ 는 목록에 없다. 우리 매핑은 settings.result_reaction_emoji 참고.

    **모든 실패는 False 로 삼킨다**(예외·비200·설정 미비 전부). 반응은 순수
    장식이라 실패가 판정·회차·발송을 막으면 절대 안 된다. 재시도도 하지 않는다
    (send 와 달리 다음 판정 이벤트에서 자연히 다시 시도된다)."""
    try:
        if not message_id or int(message_id) <= 0 or not emoji:
            return False
        token = settings.secret("TELEGRAM_BOT_TOKEN")
        chat_id = settings.secret("TELEGRAM_CHAT_ID")
        if not token or not chat_id:
            logger.warning("[tg] 토큰/chat_id 미설정 - 반응 생략")
            return False
        payload = {"chat_id": chat_id, "message_id": int(message_id),
                   "reaction": [{"type": "emoji", "emoji": emoji}],
                   "is_big": False}
        resp = requests.post(_REACTION_API.format(token=token), json=payload,
                             timeout=settings.get("http_timeout_sec"))
        if resp.status_code == 200:
            return True
        logger.warning("[tg] 반응 실패 status=%s body=%s", resp.status_code,
                       _redact(resp.text[:200], token))
        return False
    except Exception as e:  # noqa: BLE001 - 반응 실패는 무조건 무해해야 한다
        logger.warning("[tg] 반응 실패(무시): %s", type(e).__name__)
        return False


def _split_send(text: str, urgency: str) -> Optional[int]:
    """_SEP 경계에서 분할하여 다건 전송. 첫 chunk 의 message_id 반환(전부 성공 시),
    하나라도 실패하면 None (2026-08-17 #6: 반환 타입 확장). 스레딩 대상은 아님 —
    긴 메시지는 대부분 리포트/주간 통계로, 답글 관계를 걸 후속이 없다."""
    logger.info("[tg] %d자 → 분할 발송 시작", len(text))
    chunks, current = [], []
    for line in text.split("\n"):
        candidate = "\n".join(current + [line])
        if len(candidate) > _TG_MAX_LEN and current:
            chunks.append("\n".join(current))
            current = [line]
        else:
            current.append(line)
    if current:
        chunks.append("\n".join(current))
    first_mid = None
    ok = True
    for i, chunk in enumerate(chunks):
        if len(chunk) > _TG_MAX_LEN:
            cut = _TG_MAX_LEN - 20
            last_open = chunk.rfind('<', 0, cut)
            if last_open != -1 and '>' not in chunk[last_open:cut]:
                cut = last_open
            chunk = _TAG_RE.sub("", chunk[:cut]) + "\n… (truncated)"
        if i > 0:
            time.sleep(1.0)
        mid = send(chunk, urgency)
        if not mid:
            ok = False
            logger.error("[tg] 분할 발송 %d/%d 실패", i + 1, len(chunks))
        elif i == 0:
            first_mid = mid
    return first_mid if ok else None



# ── 포맷 유틸 (워쳐 notifier.py 이식) ─────────────────────────────

def _fmt_age(minutes) -> str:
    if minutes is None or minutes < 0:
        return ""
    if minutes < 60:
        return f"{minutes:.0f}분 전"
    if minutes < 1440:
        return f"{minutes // 60:.0f}시간 전"
    return f"{minutes // 1440:.0f}일 전"


def _fresh_age_min(level: dict):
    """알림 시점 기준 글 나이(분). DB의 post_age_minutes 는 '수집 당시' 나이라
    그대로 쓰면 낡는다(2026-07-23 WLD 사고: TV는 2일 전인데 알림은 1일 전) —
    수집 시각(collected_at)에서 게시 시각을 역산해 지금 기준으로 재계산한다."""
    age = level.get("post_age_minutes")
    collected = level.get("collected_at")
    if age is None:
        return None
    if not collected:
        return age
    published_epoch = collected - age * 60.0
    return (time.time() - published_epoch) / 60.0


def _fmt_followers(count) -> str:
    if not count or count <= 0:
        return ""
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    if count >= 1_000:
        return f"{count / 1_000:.1f}K"
    return str(int(count))


_SELF_STATS_MIN_N = 5  # 자체 표본이 이 이상일 때만 병기 (ACCURACY_DB_PLAN 2단계 발동 조건)

# 소스 표시 문구 (2026-07-27). levels.source 값 → 알림에 쓸 한국어 라벨.
# 등록되지 않은 소스는 값 그대로 노출한다(빈칸보다 낫고, 새 소스 추가 시 눈에 띈다).
_SOURCE_LABEL = {"telegram": "텔레그램 채널", "tradingview": "트레이딩뷰"}


def _author_block(rep: dict) -> list:
    """작성자 라인 + 적중률 라인 (워쳐 스타일 + 자체 적중 병기).
    2026-07-23 카드4 확정: 워쳐(글 시점 기준)와 자체(터치 시점 기준)는 측정 기준이
    달라 섞지 않고 병기. 자체 표본은 R 트랙 유효표본(neff_r) 5건 이상일 때만 표시
    (2026-07-27 판정 비대칭 대응 — 아래 게이트 주석 참고)."""
    author = html.escape(rep.get("author") or "?")
    # ✍️ 로 아래 📊/🏹/📎 행들과 시작 칸 정렬 (2026-07-24 사용자 확정)
    # 2026-08-08 사용자 결정: "작성자:" 라벨 삭제 + 화이트리스트 ⭐⭐ 표시
    # 숨김(거추장스럽다 - 다른 지표로 판단) - author_whitelisted 필드 자체는
    # 그대로 두되(다른 곳에서 쓸 수 있으니) 렌더링만 하지 않는다.
    lines = [f"✍️ @{author}"]

    wins = rep.get("author_self_wins") or 0
    losses = rep.get("author_self_losses") or 0
    # 자체 성적은 별도 줄 (이모지로 윗줄과 시작 위치 정렬). C안 함축 표기로 한 줄 유지
    # (2026-07-24 사용자 확정: "승률67% (4승2패) 터치율67%").
    self_line = None
    # 게이트: R 트랙 유효표본(neff_r). 2026-07-27 사장님 확정으로 승률축(neff_win)에서
    # 옮겼다 — 두 지표가 서로 다른 축을 보던 구멍을 막는다.
    #   · 예전 승률 표시는 neff_win(판정 방식 무관 전체 종결) 기준 → SL 미기재 작성자도 통과
    #   · 역신호 경고는 neff_r(r_multiple 필요 = SL 있어야 계산) 기준 → 그 작성자는 면제
    #   ⇒ "SL 을 안 쓰면 승률 100% 로 표시되면서 경고는 절대 안 붙는" 구조였다.
    # 실측 근거: tp_only(SL 미기재) 13건 중 13건 hit(100%) vs tp_sl 21건 중 4건(19%).
    # SL 이 없으면 지는 경로가 거의 없다 — CryptoAnalystSignal 은 종결 12건 전부 tp_only 라
    # "승률100% (12승0패)" 로 나갈 참이었다(활성 5건 대기 중이었음).
    # 이제 표시와 경고가 같은 축을 쓴다 = 승률이 보이면 역신호 판정도 받은 표본이다.
    # 미주입(렌더러 단독 호출·구버전 경로)이면 표시하지 않는다 — 예전의 raw 폴백이
    # 바로 그 구멍이었으므로 보수적으로 숨긴다.
    _gate_n = rep.get("author_self_neff_r") or 0.0
    if _gate_n >= _SELF_STATS_MIN_N and (wins + losses) > 0:
        rate = wins / (wins + losses) * 100
        self_line = f"🏹 승률{rate:.0f}% ({wins}승{losses}패)"
        # 터치율 병기 (선택편향 처방, ACCURACY_DB_PLAN — 표본 5건↑일 때만)
        touched_n = rep.get("author_touched_n") or 0
        untouched = rep.get("author_untouched_expired") or 0
        if touched_n + untouched >= _SELF_STATS_MIN_N:
            self_line += f" 터치율{touched_n / (touched_n + untouched) * 100:.0f}%"

    hit_rate, hit_count = rep.get("author_hit_rate"), rep.get("author_hit_count")
    if hit_rate is not None and hit_count:
        lines.append(f"📊 평균 적중률: {hit_rate * 100:.0f}% (워쳐 {hit_count}건)")
    elif not self_line:
        if rep.get("author_followers"):
            lines.append(f"👥 팔로워 {_fmt_followers(rep['author_followers'])} · 적중률 기록없음")
        elif rep.get("source"):
            # 소스별 문구 분기 (2026-07-27 사용자 지시). 워쳐는 TradingView 작성자만
            # 추적하므로 그 밖의 소스는 **영원히** "워쳐 미추적"이다 — 늘 참인 문구는
            # 아무 정보도 주지 않는다. 그 자리에 '어디서 온 신호인지'를 대신 넣는다.
            #
            # 2026-07-28 확대: TradingView 인데 워쳐에 없는 작성자도 여기로 보낸다
            # (실측 ENA/@Elephantun). "워쳐 미추적 작성자"는 사장님 관점에서 봇 내부
            # 사정일 뿐 — 읽는 사람에게 쓸모 있는 정보는 '어디서 온 신호인가'다.
            lines.append(f"📡 {_SOURCE_LABEL.get(rep['source'], rep['source'])} · 적중률 미집계")
        else:
            # source 가 비어 있는 초기 수집분(컬럼 도입 전)만 여기로 온다.
            lines.append("👥 적중률 기록없음 (워쳐 미추적 작성자)")
    if self_line:
        lines.append(self_line)

    # 평균 보유기간 (표본 3건↑, 스윙 정렬 참고용)
    # 데이터 이상(resolved_at < touched_at 등)으로 음수가 나오면 표기 생략
    _hold = rep.get("author_avg_holding_days")
    if _hold is not None and _hold > 0:
        if _hold < 1:
            lines.append(f"⏱ 평균 {_hold * 24:.0f}시간 보유")
        else:
            lines.append(f"⏱ 평균 {_hold:.1f}일 보유")

    # 역신호 경고 줄은 여기서 렌더하지 않는다 (2026-07-27 사용자 결정).
    # 07-27 오전에 "🔻 역신호 후보 — 자체 기대손익 -0.92R (표본 8)" 한 줄을 넣었다가
    # 같은 날 뺐다. 이유: 알림 한 건이 이미 폰 화면을 넘길 만큼 길고, 행이 하나 늘 때마다
    # 정작 봐야 할 타점·가격이 밀린다. 지표 자체는 계속 쌓이고 `scripts/show_status.py`
    # 작성자 성적 섹션(🔻역신호후보 태그)과 주간 리포트에서 볼 수 있다 — 알림은 "지금
    # 무엇을 할지", 리포트는 "이 소스를 계속 쓸지"를 답하는 화면이라 역할이 다르다.
    # rep 에 주입되는 author_self_e_lb 는 그대로 둔다(같은 author_metrics 호출에서
    # 함께 나오므로 비용 0이고, 억제 필터로 승격할 때 바로 쓸 수 있다). 위 자체 승률
    # 게이트가 neff_r 을 쓰는 것도 이 판정과 같은 축을 유지하기 위함이다.
    return lines


def render_alert(kind: str, coin_symbol: str, cluster: list, current_krw: float,
                 usdt_krw: float, sentiment: dict = None, week52: tuple = None,
                 kimchi_pct: float = None, volume_rank: int = None,
                 rep: dict = None, funding_rate: float = None,
                 funding_regime_flip: dict = None, supply: tuple = None,
                 position: tuple = None, kimchi_delta: float = None,
                 adx14: float = None, dex_stats: dict = None,
                 active_addr_pctile: float = None,
                 stwits_bullish_ratio: float = None,
                 watcher_coin_sl: dict = None) -> str:
    """kind: 'touch'|'preview'. cluster: 같은 코인 ±1% 레벨 dict 목록(entry 내림차순).
    sentiment: {btc_dominance, fear_greed, ...}|None. week52: (고가KRW, 저가KRW)|None.
    kimchi_pct: 김프 %|None. volume_rank: 업비트 KRW 거래대금 순위(조회 시점)|None.
    rep: 호출부가 확정한 대표 레벨 (S9 통합감사 M-1, 2026-07-31) — run_once 는
    재채점·B안 승계까지 반영해 대표를 고르는데, 여기서 수집 score 로 재선정하면
    표시 대표(작성자·등급)와 필터·밴드 대표가 갈릴 수 있다. 미전달 시(직접 호출·
    구버전 경로) 종전대로 score 최대 멤버 폴백."""
    if rep is None:
        rep = max(cluster, key=lambda l: l.get("score") or 0)
    current_usd = (current_krw / usdt_krw) if (current_krw and usdt_krw) else None

    entries = [lv["entry_usd"] for lv in cluster if lv.get("entry_usd")]
    lo, hi = (min(entries), max(entries)) if entries else (None, None)
    entry_rep = hi  # 트리거 기준 = 클러스터 상단

    tier = rep.get("mcap_tier_icon") or ""
    rank = rep.get("mcap_rank")
    rank_part = f"{tier} 시총 {rank}위" if rank else ""
    kind_kr = "🎯 <b>[진입가 터치]</b>" if kind == "touch" else "⚠️ <b>[진입가 접근]</b>"
    grade = f"{rep['grade']}등급" if rep.get("grade") else ""

    head_meta = " · ".join(x for x in [rank_part, grade,
                                       _fmt_age(_fresh_age_min(rep))] if x)

    lines = [
        _SEP,
        f"{kind_kr} <b>{html.escape(coin_symbol)}</b>",
        head_meta,
    ]
    # TP 도달 훈장 — 작성자의 자체 적중 1회 이상이면 상단에 배지 표시.
    # author_self_wins 는 price_check.py 가 db.get_author_self_stats 로 주입한다.
    _tp_wins = rep.get("author_self_wins") or 0
    if _tp_wins >= 1:
        lines.append(f"🏅 TP도달: {_tp_wins}회")
    lines.extend(_author_block(rep))
    lines.append(_SEP)

    # ── 타점 (워쳐식 복귀 + 원화 단독 표기, 2026-07-23 사용자 최종 확정:
    #    터치 시점엔 어차피 현재가≈진입가라 달러 병기가 불필요 — 원화만 한 줄씩) ──
    def _krw(usd_value):
        if not usd_value or not usdt_krw:
            return None
        v = usd_value * usdt_krw
        return f"{v:,.0f}" if v >= 1 else f"{v:.4f}"

    # 들여쓰기 4칸 = 52주 블록의 고가/저가 행과 시작 위치 정렬 (2026-07-23 사용자 지시).
    # R:R 행은 삭제(사용자가 직접 판단) — 그 자리에 거래량 순위.
    lines.append("타점")
    if current_usd and _krw(current_usd):
        lines.append(f"    현재:  {_krw(current_usd)}원")
    if lo is not None and hi is not None and hi > lo and _krw(lo):
        lines.append(f"    진입:  {_krw(lo)}~{_krw(hi)}원")
    elif entry_rep and _krw(entry_rep):
        lines.append(f"    진입:  {_krw(entry_rep)}원")
    # 손절 행은 표시하지 않는다(사용자 결정 - 데이터는 저장·등급 계산에 계속 사용)
    # 표시 직전 최종 가드(2026-07-23 SOL 실전 사고): 파서 수정 '이전'에 수집돼 DB에
    # 남아있는 오염값(서수 오인 tp=1.0 등)이 다음 수집의 자동 치유 전까지 알림에
    # 노출되는 걸 막는다 - 진입가 대비 4배/0.25배 밖 목표는 '데이터 없음' 처리.
    tp = rep.get("tp_usd")
    if tp and entry_rep and not (entry_rep * 0.25 <= tp <= entry_rep * 4):
        tp = None
    if tp and entry_rep:
        pct = (tp - entry_rep) / entry_rep * 100
        # 다단계 목표 표기 (2026-07-27 사용자 승인, A안): 소스가 "TARGETS: 0.059 -
        # 0.0615 - … - 0.085" 처럼 사다리로 주면 우리는 **첫 목표(TP1)만** 쓴다 —
        # 적중 판정("TP1 도달=승")과 TP 거리 배점이 그 축이라 바꾸면 기존 표본과
        # 축이 어긋난다. 그래서 판정은 그대로 두고 "1/8단계"만 덧붙여, 위로 더
        # 있다는 사실을 알린다(정확한 상단은 출처 링크의 원문에 있다).
        # 단계가 1개뿐이거나 미상이면 종전과 완전히 동일한 한 줄이 나간다.
        n_tp = rep.get("tp_ladder_count") or 0
        # 2026-08-08 사용자 결정: "단계" 글자 삭제(그룹 채팅 폭 절약).
        # 2026-08-15: 표시 상한 12 를 추출기(_LADDER_MAX_STEPS)에서 이리로 이동 —
        # 실측(원문 103건) 분포는 8단에서 끝나 13단+ 는 산문 오염 가능성이 크다.
        # '틀린 단계 수를 보여주느니 안 보여준다'는 컷은 유지하되, 저장값은 v5
        # 등급 산식이 쓰므로 참 개수를 유지하고 표시만 자른다. 12 이하 사다리의
        # 알림 양식은 종전과 바이트 단위로 동일(13단+ 는 종전에도 0 저장 → 무표기).
        step = f"  1/{n_tp}" if 1 < n_tp <= 12 else ""
        _tp_krw = _krw(tp)
        if _tp_krw:
            # 2026-08-08 사용자 결정: 원 이후 띄어쓰기 삭제(그룹 채팅 폭 절약).
            lines.append(f"    목표:  {_tp_krw}원({pct:+.1f}%){step}")
        else:
            lines.append(f"    목표:  ({pct:+.1f}%){step}")
    else:
        lines.append("    목표:  데이터 없음")
    if volume_rank:
        lines.append(f"    거래:  {volume_rank}위")

    # ── 52주 고저 + 현재 위치 바 (워쳐 notifier.py 표기 그대로, 2026-07-23 #9) ──
    # 2026-08-17 UX: (1) 타점 블록 직후 빈줄 제거 (52주 바로 붙임), (2) 저가↔바
    # 사이 빈줄 → _SEP 로 승격해 다른 블록 간 구분선과 시각적 일관성.
    if week52 and current_krw:
        high52, low52 = week52
        if high52 and low52 and high52 > 0 and low52 > 0:
            from_high = (current_krw - high52) / high52 * 100
            from_low = (current_krw - low52) / low52 * 100
            lines.append("52주")
            lines.append(f"    고가  {from_high:+.1f}% ({_fmt_krw(high52)}원)")
            lines.append(f"    저가  {from_low:+.1f}% ({_fmt_krw(low52)}원)")
            if high52 > low52:
                pos = max(0, min(100, (current_krw - low52) / (high52 - low52) * 100))
                filled = max(0, min(10, round(pos / 10)))
                lines.append(_SEP)
                lines.append("    " + "🟩" * filled + "⬜" * (10 - filled))
                lines.append(f"    └ 현재 {pos:.0f}% 지점")

    # Zone 1 배지 재배치 (2026-08-17 E1, 사용자 결정 — 리스크 우선):
    # 세 그룹으로 버퍼링 후 순서대로 렌더 (라인 수 불변, 순서만 변경).
    #   [1] risk_badges (매수 부담·매수 주의) — 스윙 진입 판단 시 위험 신호 우선 스캔
    #   [2] info_badges (라벨 없음: 자리·추세장) — 판단 근거 데이터
    #   [3] positive_badges (매수 유리) — 확증 신호
    # 각 그룹 내 순서는 데이터 소스별 자연 순서(DEX → 온체인 등) 유지.
    risk_badges, info_badges, positive_badges = [], [], []

    # DEX 저유동 (매수 주의) — 러그·exit 위험. 임계 <100k$ (grading._dex_points 와 동일).
    if dex_stats:
        _liq = dex_stats.get("liquidity_usd")
        _bratio = dex_stats.get("buy_ratio_24h")
        if _liq is not None and _liq < 100_000:
            risk_badges.append(f"💧 DEX 저유동 {_liq/1000:.0f}k$ (매수 주의)")
        # DEX 매수세/매도세 — 배타적 임계 (≥0.65 / ≤0.35). 사이(중립)는 무표기.
        if _bratio is not None:
            if _bratio >= 0.65:
                positive_badges.append(f"🟢 DEX 매수세 {_bratio*100:.0f}% (매수 유리)")
            elif _bratio <= 0.35:
                risk_badges.append(f"🔴 DEX 매도세 {(1-_bratio)*100:.0f}% (매수 부담)")

    # Coin Metrics 활성주소 30d 백분위 (2026-08-17). 무료 커버 138종만 값.
    # ≥80 활발 (매수 유리) / ≤20 저조 (매수 부담) / 중립 무표기.
    if active_addr_pctile is not None:
        if active_addr_pctile >= 80:
            positive_badges.append(f"⛓ 온체인 활발 {active_addr_pctile:.0f}위 (매수 유리)")
        elif active_addr_pctile <= 20:
            risk_badges.append(f"⛓ 온체인 저조 {active_addr_pctile:.0f}위 (매수 부담)")

    # StockTwits 소셜 심리 (2026-08-17). SOL/SUI/APT/TAO/WLD/TIA 등 최근 유행 알트
    # 커버 (Coin Metrics 미커버 자산 상당수 보완). 태그된 표본 <5는 이미 fetch
    # 단계에서 None 반환. ≥0.75 강한 매수 심리 / ≤0.30 강한 매도 심리.
    if stwits_bullish_ratio is not None:
        if stwits_bullish_ratio >= 0.75:
            positive_badges.append(
                f"💬 소셜 매수세 {stwits_bullish_ratio*100:.0f}% (매수 유리)")
        elif stwits_bullish_ratio <= 0.30:
            risk_badges.append(
                f"💬 소셜 매도세 {(1-stwits_bullish_ratio)*100:.0f}% (매수 부담)")

    # 추세장 (2026-08-17 F3): ADX(14) ≥25 일 때만 표시. 라벨 없는 정보 배지.
    if adx14 is not None and adx14 >= 25:
        info_badges.append(f"📈 추세장 (ADX {adx14:.0f})")

    # RSI 자리 판정 (2026-08-07). 라벨 없는 정보 배지.
    if position and position[0]:
        _pv, _pr = position
        info_badges.append(f"🌡️ 자리: {_pv} ({_pr})" if _pr else f"🌡️ 자리: {_pv}")

    # 워쳐 코인별 SL률 (최근 7일, 표본 2건 이상일 때만 표시)
    if watcher_coin_sl and watcher_coin_sl.get("total", 0) >= 2:
        _sl_r = watcher_coin_sl["sl_rate"]
        _sl_t = watcher_coin_sl["total"]
        _sl_m = watcher_coin_sl["misses"]
        if _sl_r is not None:
            if _sl_r >= 0.6:
                risk_badges.append(
                    f"📉 워쳐 SL률 {_sl_r*100:.0f}% (7일 {_sl_m}/{_sl_t}건)")
            else:
                info_badges.append(
                    f"📉 워쳐 SL률 {_sl_r*100:.0f}% (7일 {_sl_m}/{_sl_t}건)")

    # 렌더 순서: 리스크 → 정보 → 긍정 (사용자 결정, E1 옵션 2).
    lines.extend(risk_badges)
    lines.extend(info_badges)
    lines.extend(positive_badges)

    # 포지션 참고(📐 SL / R:R) 행은 삭제됨 (2026-08-03 사용자 결정) — SL 은 판정
    # 엔진 내부 기준선으로만 사용, 알림 화면에는 노출하지 않는다. rep.sl_usd/rr 는
    # 등급·판정에는 계속 반영.

    # ── 시장 심리 (워쳐 표기 그대로: 김프 행 → BTC.D 행 / ALT.S 행 / F&G 행) ──
    # funding_regime_flip 도 세퍼레이터 조건에 포함 (2026-08-03 R1 감사): 다른
    # 세 지표가 다 실패한 상태에서 레짐 배지만 있으면 세퍼레이터가 안 붙어
    # 목표가 행에 바로 이어지는 렌더 이슈가 있었다.
    if (sentiment or kimchi_pct is not None or funding_rate is not None
            or (funding_regime_flip and funding_regime_flip.get("flipped"))
            or supply):
        lines.append(_SEP)
    # 수급 판정 한 줄 (2026-08-07 사용자 결정): 종전 "💰 펀딩 수치+라벨" 줄을
    # 펀딩×OI 합성 결론으로 대체 — 원시 수치 대신 매수 관점 판정만 짧게.
    # supply 미전달(구 호출부·테스트)이면 funding_rate 단독으로 같은 형식을
    # 만들어 렌더 경로를 하나로 유지한다. 모바일 한 줄 상한: 최장 14자.
    _supply = supply
    if _supply is None and funding_rate is not None:
        from monitor.binance import derive_supply_verdict
        _supply = derive_supply_verdict(funding_rate, None, None)
    # 헤더 어휘 개편 (2026-08-14 사용자 확정): 수급→돈 흐름 — 초보자 직관.
    if _supply and _supply[0]:
        _sv, _sr = _supply
        lines.append(f"🧭 돈 흐름: {_sv} ({_sr})" if _sr else f"🧭 돈 흐름: {_sv}")
    # 펀딩 레짐 전환 (2026-08-03 스프린트08 사용자 결정): 30일+ 지속 음수 → 양수
    # 플립 감지 시 🔥 강조 배지. 등급 산식엔 영향 없음(배지만).
    # 표기 개편 (2026-08-14 사용자 확정 A안): "N일 음수→양수"는 방향성이
    # 안 읽힌다는 피드백 → 펀딩이 실제로 말해주는 사실(매수 수요 복귀)을
    # 그대로 — 과장("바닥 탈출") 없이 호재로 읽히는 표현.
    if funding_regime_flip and funding_regime_flip.get("flipped"):
        _nd = funding_regime_flip.get("neg_days") or 0
        lines.append(f"🔥 {_nd:.0f}일만에 매수세 복귀")
    if kimchi_pct is not None:
        # 급변 화살표 (2026-08-14 사용자 확정): ~6h 대비 ±0.5%p 이상 움직였을
        # 때만 화살표 1글자 — 평시 표기는 종전과 완전 동일(행 폭 유지).
        _arrow = ""
        if kimchi_delta is not None and abs(kimchi_delta) >= _KIMCHI_DELTA_TH:
            _arrow = " ▲" if kimchi_delta > 0 else " ▼"
        if abs(kimchi_pct) < 0.01:
            lines.append(f"⚖️ 김프 거의 0% ({kimchi_pct:+.3f}%){_arrow}")
        elif kimchi_pct > 0:
            lines.append(f"🌶️ 김프 {kimchi_pct:+.2f}%{_arrow}")
        else:
            lines.append(f"❄️ 김프 {kimchi_pct:+.2f}%{_arrow}")
    if sentiment:
        btc_d = sentiment.get("btc_dominance")
        alt_s = sentiment.get("altcoin_season_index")
        fng = sentiment.get("fear_greed")
        # 헤더 어휘 개편 (2026-08-14 사용자 확정): BTC.D→비트 점유율,
        # ALT.S→알트장, F&G→시장심리 — 영문 약어를 우리말로.
        if btc_d is not None:
            lines.append(f"🌍 비트 점유율: {btc_d}%")
        if alt_s is not None:
            if alt_s >= 75:
                alt_label = "알트 매수 권장"
            elif alt_s >= 50:
                alt_label = "알트 매수 고려"
            elif alt_s >= 25:
                alt_label = "BTC 매수 고려"
            else:
                alt_label = "BTC 매수 권장"
            lines.append(f"🪙 알트장: {alt_s} ({alt_label})")
        if fng is not None:
            label_kr = _FNG_KR.get(sentiment.get("fear_greed_label", ""),
                                   sentiment.get("fear_greed_label", ""))
            if label_kr:
                lines.append(f"😨 시장심리: {fng} ({label_kr})")
            else:
                lines.append(f"😨 시장심리: {fng}")

    # ── 출처 (URL 노출 없이 하이퍼링크, 최신순, 최대 5) ──
    lines.append(_SEP)
    srcs = sorted(cluster, key=lambda l: x if (x := _fresh_age_min(l)) is not None else 1e12)
    lines.append(_source_line([lv.get("post_url") for lv in srcs]))

    return _finalize(lines)


# ── 수집 급감(조용한 고장) 경고 (2026-07-26) ──────────────────────────
# 기존 render_alert 와 명확히 구분되는 별도 렌더러 - render_alert 는 변경하지 않는다.


def render_collect_silence_alert(window_hours: float, baseline_avg_per_day: float) -> str:
    """cron/Actions 는 정상(초록불)인데 신규 수집이 끊긴 '조용한 고장' 경고.
    일반 알림과 헷갈리지 않게 🚨 헤더로 시작 - 하루 1회 상한은 호출부(price_check)의
    meta 중복방지가 담당하고, 이 함수는 순수 렌더링만 한다."""
    lines = [
        _SEP,
        "🚨 <b>[수집 급감 경고]</b>",
        f"최근 {window_hours:.0f}시간 신규 수집 0건",
        f"(직전 {baseline_avg_per_day:.1f}건/일 대비)",
        _SEP,
    ]
    return "\n".join(lines)


def render_collect_stale_alert(elapsed_hours: float, threshold_hours: float) -> str:
    """meta.last_collect_at 이 임계시간 이상 갱신되지 않은 '수집 단계 정지' 경고
    (2026-07-26 과제3, scripts/run_cycle.py 신설). render_collect_silence_alert(신규
    수집 0건 - 결과 신호, 수집기가 살아있어도 새 글이 없으면 울릴 수 있음)와 잡는
    고장 원인이 다르다 - 이건 '수집 단계 자체가 실행/성공했는가'를 보는 구조적 신호.
    2026-07-23~26 rebase 오류로 수집분이 조용히 전량 폐기된 3일 장애가 계기(당시
    last_collect_at 이 갱신되지 않은 채 멈춰 있었을 것). 하루 1회 중복방지는
    호출부(run_cycle.py)의 meta 가 담당 - 이 함수는 순수 렌더링만."""
    lines = [
        _SEP,
        "🚨 <b>[수집 단계 정지 경고]</b>",
        f"마지막 수집 성공 후 {elapsed_hours:.1f}시간 경과 (임계 {threshold_hours:.0f}시간)",
        "run_cycle.py 의 수집 단계가 계속 실패했거나 회차 자체가 멈췄을 수 있습니다.",
        _SEP,
    ]
    return "\n".join(lines)


def render_tv_block_alert(reason) -> str:
    """TradingView 확정 차단(403/429/캡차/1020) 감지 즉시 경고 (2026-07-26 과제2).
    수집 급감 경고(24시간 0건 지속)보다 먼저 울리는 빠른 신호 - render_alert 와
    무관한 별도 렌더러. 하루 1회 상한은 호출부(scripts/run_collect.py)의 meta
    중복방지가 담당한다(이 함수는 순수 렌더링만)."""
    reason_kr = "캡차/챌린지" if reason == "captcha" else f"HTTP {reason}"
    lines = [
        _SEP,
        "🚫 <b>[TradingView 차단 감지]</b>",
        f"신호: {reason_kr}",
        "봇이 30분 쿨다운 후 자동 재시도합니다. 3단계 폴백으로 수집은 계속됩니다 - "
        "장기 반복 시 TV_COOKIE 갱신(재로그인 쿠키 재등록)을 검토하세요.",
        _SEP,
    ]
    return "\n".join(lines)


# ── 주간 성적 리포트 (ACCURACY_DB_PLAN 2단계 후속, 2026-07-26) ────────────
#
# 랭킹 수학은 analytics.ranking 그대로 사용(재설계 금지) — 여기선 톤을 맞춘 렌더링만.
# analytics.ranking.py 는 "프로젝트 모듈 import 0" 원칙(순환 import 차단 + DB 없는
# 손계산 단위테스트)이라, DB 연동이 필요한 텍스트 조립은 이미 db 를 참조하는
# notify 쪽에 둔다 (기존 render_alert 도 rep dict 를 받아 조립하는 동일 위치).


def _weekly_rank_line(rank: int, author: str, met: dict) -> tuple:
    """랭킹 한 줄 + 역신호 후보 여부(E_LB<=0, 게이트는 통과) 반환."""
    e_lb = met.get("e_lb")
    e_lb_display = met.get("e_lb_display")
    neff_r = met.get("neff_r") or 0.0
    is_anti = e_lb is not None and e_lb <= 0
    mark = " 🔻" if is_anti else ""
    pct = f", 승률{met['p_hat'] * 100:.0f}%" if met.get("p_hat") is not None else ""
    e_txt = f"{e_lb_display:+.2f}" if e_lb_display is not None else "-"
    line = (f"  {rank}. @{html.escape(author)}{mark}  "
            f"E_LB {e_txt} (n_eff {neff_r:.1f}{pct})")
    return line, is_anti


def _pct(v) -> str:
    return f"{v * 100:.0f}%" if v is not None else "-"


# ── 주간 리포트 v2 (2026-09-22 R4) ───────────────────────────────────────
#
# "지표 나열형 → 의사결정형" 개편. 근거: plan_2026-09-22_최종버전_종합검토 §3 R4 /
# research_2026-09-22_external_final_review §3(TradeZella 30분 주간 리뷰 6단계 —
# 개요→베이스라인 대비→관찰 3개→조정 1개, freqtrade `/stats` exit-reason 집계).
#
# 수학은 전부 analytics/weekly.py(순수 함수, DB·프로젝트 모듈 import 0)가 맡고
# 여기서는 문구만 만든다 — calibration/ranking 섹션과 같은 역할 분담이다.
#
# 제거된 섹션과 이유(2026-09-22):
#   🎲 초과 적중률   — 판정 기준이 서로 다른 두 축의 뺄셈이라 매주 caveat 2줄이
#                      따라붙었다. "기준선 대비"는 ②의 지난주 대비로 대체.
#   📊 R-멀티플 분포 — 막대 그래프가 길이 예산의 15%를 먹는데, 의사결정에 쓰인
#                      정보는 평균 R 한 줄뿐이었다(→ ② 로 승격).
#   ⏱️ 보유기간 분포 — ⑤ 판정 사유별 집계의 '평균 보유시간' 칼럼이 상위 호환.
#   🌡️ 등급×장세 히트맵 — BTC 레짐 below 표본이 8월 이후 0건 증가(db_final_review
#                      §요약3). 표본 도달이 무기한이라 상시 침묵 섹션이었다.
#   🤝 합의 참여     — 데이터가 가설을 기각(다작성자 33.3% vs 단독 38.2%).
#   구 산식 병기     — grade_ver 최신 표본만 보기로 정리(⑦).

_WEEKLY_KST = timezone(timedelta(hours=9))

# 각주 (plan §3 R5 / db_final_review §2-1) — 숫자는 실측 누적 종결률.
_WEEKLY_FOOTNOTE = "ℹ️ 결과 확인 기준: 터치 후 7일 (168h 내 종결 57%)"


def _kst_md(ts: float) -> str:
    return datetime.fromtimestamp(float(ts), _WEEKLY_KST).strftime("%m-%d")


def _metric_row(label: str, cur, prev, fmt, n=None, min_n: int = 10) -> str:
    """'  라벨  값 (지난주값 → ▲)' 한 줄.

    n 이 주어지고 min_n 미만이면 **화살표를 생략**하고 '(n=…, 참고)' 만 붙인다 —
    소표본에서 화살표는 노이즈를 방향으로 착각하게 만든다(사용자 결정 관례:
    표본 미달 지표는 판단 재료가 아니라 관찰 재료)."""
    if cur is None:
        return f"  {label}  -"
    cur_txt = fmt(cur)
    if n is not None and n < min_n:
        return f"  {label}  {cur_txt} (n={n}, 참고)"
    ar = weekly.arrow(cur, prev)
    if ar is None:
        return f"  {label}  {cur_txt} (지난주 없음)"
    return f"  {label}  {cur_txt} ({fmt(prev)} → {ar})"


def _overview_section(cur: dict, prev: dict, min_n: int) -> list:
    """📌 이번 주 한눈에 — 지난주 대비. cur 미주입이면 섹션 생략(하위호환 경로)."""
    if not cur:
        return []
    prev = prev or {}
    lines = [_SEP, "📌 <b>이번 주 한눈에</b> — 지난주 대비"]
    lines.append(_metric_row("알림 수", cur.get("alerts"), prev.get("alerts"),
                             lambda v: f"{v:.0f}건"))
    lines.append(_metric_row("종결 수", cur.get("closed"), prev.get("closed"),
                             lambda v: f"{v:.0f}건"))
    lines.append(_metric_row("승률  ", cur.get("win_rate"), prev.get("win_rate"),
                             lambda v: f"{v * 100:.0f}%",
                             n=cur.get("win_n"), min_n=min_n))
    lines.append(_metric_row("PF    ", cur.get("pf"), prev.get("pf"),
                             lambda v: f"{v:.2f}", n=cur.get("r_n"), min_n=min_n))
    lines.append(_metric_row("평균 R", cur.get("avg_r"), prev.get("avg_r"),
                             lambda v: f"{v:+.2f}", n=cur.get("r_n"), min_n=min_n))
    lines.append(f"  (PF·평균 R 은 R 산출 가능 표본 {cur.get('r_n') or 0}건 기준)")
    return lines


def _observation_line(c: dict) -> str:
    """관찰 후보(analytics.weekly.observations 의 dict) 1건 → 한 줄. n 을 반드시 병기."""
    k = c.get("kind")
    if k == "outcome_mix":
        return (f"  · {c['label']} 비중 {c['cur'] * 100:.0f}% "
                f"({c['delta_pp']:+.0f}%p, 이번주 n={c['n_cur']}/지난주 n={c['n_prev']})")
    if k in ("delay", "volume_rank"):
        return (f"  · {c['label_a']} 승률 {c['rate_a'] * 100:.0f}%(n={c['n_a']}) vs "
                f"{c['label_b']} {c['rate_b'] * 100:.0f}%(n={c['n_b']}) "
                f"— 격차 {c['gap']:.0f}%p")
    if k == "authors":
        return (f"  · 최고 @{html.escape(c['top'])} {c['top_rate'] * 100:.0f}%"
                f"(n={c['top_n']}) / 최저 @{html.escape(c['low'])} "
                f"{c['low_rate'] * 100:.0f}%(n={c['low_n']})")
    if k == "hold_outlier":
        return (f"  · {c['label']} 평균 보유 {c['hold_h']:.0f}h — 전체 평균 "
                f"{c['overall_h']:.0f}h 대비 {c['dev_pct']:+.0f}% (n={c['n']})")
    return f"  · {c}"


def _observations_section(obs: list, pool_n, pool_days: int) -> list:
    """🔎 관찰 3줄 — 규칙 기반 자동 생성. 후보가 없으면 '특이 관찰 없음(표본 n)'.

    후보 선정·격차 계산은 analytics.weekly.observations 가 전담한다(양쪽 n≥10,
    작성자 극단만 n≥5). 여기서는 고른 결과를 문장으로 옮길 뿐이다."""
    lines = [_SEP, f"🔎 <b>관찰 3줄</b> (최근 {pool_days}일 누적, 데이터가 지지하는 것만)"]
    if not obs:
        lines.append(f"  · 특이 관찰 없음 (표본 n={pool_n if pool_n is not None else 0})")
        return lines
    lines.extend(_observation_line(c) for c in obs)
    return lines


def _milestones_section(milestones: list) -> list:
    """⏳ 다음 판단 — 표본 도달 마일스톤. 진행률 + 최근 30일 속도 기반 예상일만.

    **여기서 자동으로 바뀌는 것은 아무것도 없다** — 표본이 차면 사람이 조정을
    결정하는 자리라는 뜻으로 읽혀야 한다(TradeZella '주당 조정 1개' 프레임)."""
    if not milestones:
        return []
    lines = [_SEP, "⏳ <b>다음 판단</b> — 표본 도달 시 사람이 결정"]
    for m in milestones:
        if m.get("done"):
            tail = "도달 ✅"
        elif m.get("eta_days"):
            tail = f"예상 {m['eta_days']}일"
        else:
            tail = "속도 산출 불가"
        lines.append(f"  · {m.get('label', '?')}: {m.get('count', 0)}/"
                     f"{m.get('target', 0)}건 ({tail})")
    lines.append("  (종결 = 기준시각 이후 터치 + outcome 기록 · 예상일은 최근 30일 속도)")
    return lines


def _outcome_stats_section(stats: dict, pool_days: int) -> list:
    """📋 판정 사유별 집계 (freqtrade `/stats` 형). 미주입/표본 0 이면 빈 목록."""
    if not stats or not stats.get("total"):
        return []
    lines = [_SEP,
             f"📋 <b>판정 사유별</b> (최근 {pool_days}일, 종결 {stats['total']}건)"]
    for r in stats.get("rows") or []:
        hold = f"{r['hold_h']:.0f}h" if r.get("hold_h") is not None else "-"
        ret = f"{r['ret_pct']:+.1f}%" if r.get("ret_pct") is not None else "-"
        lines.append(f"  {r['label']} {r['n']}건({r['share'] * 100:.0f}%) · "
                     f"보유 {hold} · 실현 {ret}")
    lines.append("  (실현% = 종결가÷터치가, |50%| 초과 제외 · 숏 부호 반전)")
    return lines


def _calibration_compact(cal: dict, ver: str = None) -> list:
    """🎚️ 등급 캘리브레이션 — 수학은 종전(analytics.calibration) 그대로, 표시만
    1줄/등급으로 압축한 판. 구 산식 병기는 제거했다(grade_ver 최신 표본만 본다).

    **표기 전용** — 배점·알림 필터·알림 양식 어디에도 되먹임되지 않는다."""
    if not cal or not cal.get("buckets"):
        return []
    pooled = cal.get("pooled") or {}
    if not pooled.get("n"):
        return []  # 종결 표본 0 — 빈 표를 띄우느니 섹션 통째 생략
    min_n = cal.get("min_n", calibration.DEFAULT_MIN_N)
    vtxt = f"{ver} 표본, " if ver else ""
    lines = [_SEP, f"🎚️ <b>등급 캘리브레이션</b> ({vtxt}TP1 도달률, "
                   f"종결 {pooled['n']}건)"]
    for g in cal.get("order") or ():
        b = (cal.get("buckets") or {}).get(g) or {}
        if not b.get("n"):
            continue
        # '<' 는 반드시 &lt; (parse_mode=HTML — 날 '<' 는 400 Can't parse entities)
        note = "" if b.get("enough") else f" ⚠️n&lt;{min_n:g}"
        lines.append(f"  {g}  {_pct(b['rate'])} ({b['hits']}/{b['n']})  "
                     f"CI {_pct(b['ci_low'])}~{_pct(b['ci_high'])}{note}")
    violations = cal.get("violations") or []
    if violations:
        v = violations[0]
        mark = "CI 비겹침" if v["significant"] else "CI 겹침"
        lines.append(f"  ⚠️ 단조성 위반 {len(violations)}건 "
                     f"({v['lower']} {_pct(v['lower_rate'])} &gt; "
                     f"{v['higher']} {_pct(v['higher_rate'])}, {mark}) — 표기 전용")
    elif cal.get("eligible", 0) < 2:
        lines.append(f"  ℹ️ 단조성 판정 보류 (표본 n≥{min_n:g} 등급 2개 미만)")
    else:
        lines.append("  ✅ 단조성 유지 — 표기 전용(산식·필터 불변)")
    return lines


def _author_section(rows_by_author: dict, now: float, rank_kw: dict,
                    min_neff: float, top_n: int, reverse_confirmed) -> list:
    """🏆 작성자 랭킹 — 랭킹 수학(analytics.ranking)은 **그대로**, 표시만 상위 top_n.

    2026-09-22: 합의(🤝) 줄 제거(데이터가 가설을 기각). 역신호 후보(🔻)/확정 구분은
    유지한다 — 확정은 2주 연속 판정이라 주간 리포트가 유일한 노출 지점이다."""
    lines = [_SEP]
    if not rows_by_author:
        lines.append("🏆 <b>작성자 랭킹</b>")
        lines.append("  아직 표본 부족합니다 — 터치 후 종결된 레벨이 쌓이면 "
                     "다음 리포트부터 순위가 표시됩니다.")
        if reverse_confirmed:
            names = " · ".join(f"@{html.escape(a)}" for a in sorted(reverse_confirmed))
            lines.append(f"🔻 역신호 확정 {len(reverse_confirmed)}명: {names} — "
                         f"2주 연속 E_LB&lt;0 (알림 필터 무변경)")
        return lines

    ranked = ranking.rank_authors(rows_by_author, now, min_neff=min_neff, **rank_kw)
    ranked_authors = {a for a, _ in ranked}
    lines.append(f"🏆 <b>작성자 랭킹</b> (E_LB, R 트랙 n_eff≥{min_neff:g})")
    n_anti = 0
    if ranked:
        for i, (author, met) in enumerate(ranked, 1):
            line, is_anti = _weekly_rank_line(i, author, met)
            n_anti += is_anti
            if i <= top_n:
                lines.append(line)
        if len(ranked) > top_n:
            lines.append(f"  · 외 {len(ranked) - top_n}명 (상위 {top_n}만 표시)")
    else:
        lines.append("  게이트 통과 작성자 없음 (계속 관찰 중)")

    # R NULL(2트랙) — 랭킹엔 미등재, 승률축만 게이트 통과
    win_only = []
    under_sample = []
    for author, rows in rows_by_author.items():
        if author in ranked_authors:
            continue
        met = ranking.author_metrics(rows, now, **rank_kw)
        if met["neff_r"] == 0.0 and met["neff_win"] >= min_neff:
            wins = sum(1 for r in rows if r.get("outcome") in ranking.WIN_OUTCOMES)
            losses = sum(1 for r in rows if r.get("outcome") in ranking.LOSS_OUTCOMES)
            win_only.append((author, met, wins, losses))
        else:
            under_sample.append((author, len(rows)))

    if win_only:
        win_only.sort(key=lambda x: x[1]["p_hat"] or 0.0, reverse=True)
        lines.append("🎯 승률만 확정 (R 미보유 표본, 랭킹 미등재)")
        for author, met, wins, losses in win_only[:top_n]:
            lines.append(f"  @{html.escape(author)}  승률{met['p_hat'] * 100:.0f}% "
                         f"({wins}승{losses}패, n_eff {met['neff_win']:.1f})")

    if under_sample:
        under_sample.sort(key=lambda x: x[1], reverse=True)
        names = " · ".join(f"@{html.escape(a)}({n}건)" for a, n in under_sample[:5])
        more = f" · 외 {len(under_sample) - 5}명" if len(under_sample) > 5 else ""
        lines.append(f"📋 표본 부족(n_eff&lt;{min_neff:g}): {names}{more}")
    if n_anti:
        lines.append(f"⚠️ 역신호 후보 {n_anti}명 — 게이트 통과 + E_LB≤0 (🔻, 관찰용)")
    if reverse_confirmed:
        names = " · ".join(f"@{html.escape(a)}" for a in sorted(reverse_confirmed))
        lines.append(f"🔻 역신호 확정 {len(reverse_confirmed)}명: {names} — "
                     f"2주 연속 E_LB&lt;0 (확정 경보 발송됨, 알림 필터 무변경)")
    return lines


def _fit_weekly(text: str, max_chars) -> str:
    """길이 예산 가드 — 목표 3,500자(설정 weekly_report_max_chars).

    텔레그램 4096 분할 발송(_split_send)이 이미 있지만, **두 통으로 쪼개진 주간
    리포트는 읽히지 않는다**. 그래서 발송 레이어에 가기 전에 리포트 스스로 예산을
    지킨다. 절단은 반드시 줄 경계에서 한다 — HTML 태그 중간을 자르면 parse_mode
    =HTML 발송이 400 으로 실패한다(태그는 한 줄 안에서 닫힌다)."""
    if not max_chars or len(text) <= max_chars:
        return text
    notice = f"\n… (길이 제한으로 이하 생략)\n{_SEP}"
    budget = max(int(max_chars) - len(notice), 0)
    cut = text[:budget]
    nl = cut.rfind("\n")
    if nl > 0:
        cut = cut[:nl]
    return cut + notice


# (2026-09-22 R4) 이 섹션은 주간 리포트에서 **제거**됐다 — BTC 레짐 below
# 표본이 8월 이후 0건 증가라 표본 도달이 무기한이다(db_final_review).
# 함수 자체는 scripts/test_infra.py 의 렌더 격리 검증이 참조하므로 남긴다.
def _regime_heatmap_section(heatmap: dict, min_cell_n: int = 5) -> list:
    """등급×장세 히트맵 섹션 (2026-08-17 #5).

    heatmap 스키마: {"cells": {(grade, regime): {"n": int, "hit": float, "mfe": float}}}
    - grade: 'S'|'A'|'B'|'C'|'D'
    - regime: 'trend'(ADX>=25) | 'neutral'(20<=ADX<25) | 'range'(ADX<20) | 'squeeze'(BB pctile<=20)
    - 각 셀에 표본 n 이 min_cell_n 미만이면 셀 자체 생략. 전체 셀이 부족하면 섹션 통째 스킵.

    표시 전용 — 알림 필터·등급 산식에 영향 없음. 표본 도달(각 셀 최소 5건) 전까지는
    자동으로 섹션이 출력되지 않아 리포트 노이즈 0."""
    if not heatmap or not heatmap.get("cells"):
        return []
    cells = heatmap["cells"]
    valid = {k: v for k, v in cells.items() if v.get("n", 0) >= min_cell_n}
    if not valid:
        return []
    lines = [_SEP, f"🌡️ 등급×장세 히트맵 (터치 후 TP1 도달률, 각 셀 n≥{min_cell_n})"]
    regime_order = [("trend", "추세"), ("neutral", "중립"), ("range", "횡보"),
                    ("squeeze", "압축")]
    regime_has = {r: any((g, r) in valid for g in ("S", "A", "B", "C", "D"))
                  for r, _ in regime_order}
    header_cols = [lbl for r, lbl in regime_order if regime_has.get(r)]
    lines.append("  등급 | " + " | ".join(f"{c:^6}" for c in header_cols))
    for grade in ("S", "A", "B", "C"):  # D 는 필터에서 걸러져 표본 없음이 대부분
        cols = []
        has_row = False
        for r, _ in regime_order:
            if not regime_has.get(r):
                continue
            cell = valid.get((grade, r))
            if cell:
                cols.append(f"{cell['hit'] * 100:>4.0f}%")
                has_row = True
            else:
                cols.append("  -   ")
        if has_row:
            lines.append(f"  {grade:>4s}  | " + " | ".join(cols))
    lines.append("ℹ️ 표시 전용 — 알림 필터·등급 산식 무영향")
    return lines


def render_weekly_report(rows_by_author: dict = None, now: float = None,
                         half_life_days: float = None, z: float = None,
                         prior_m: int = None, min_neff: float = None,
                         confluence: dict = None, baseline: dict = None,
                         raw_records: dict = None, baseline_min_n: int = None,
                         confluence_min_clusters: int = None,
                         calibration_result: dict = None,
                         calibration_legacy: dict = None,
                         reverse_confirmed: set = None,
                         r_distribution: dict = None,
                         r_distribution_by_grade: dict = None,
                         holding_period: dict = None,
                         regime_heatmap: dict = None,
                         current: dict = None, previous: dict = None,
                         observations: list = None, pool_n: int = None,
                         pool_days: int = None, milestones: list = None,
                         outcome_stats: dict = None, calibration_ver: str = None,
                         period: tuple = None, min_n: int = None,
                         top_authors: int = None, max_chars: int = None) -> str:
    """주간 성적 리포트 (2026-09-22 v2 — 지표 나열형 → 의사결정형).

    구성 9부:
      ① 헤더(주차 KST 기간 · 표본 알림/종결)
      ② 📌 이번 주 한눈에 — 지난주 대비 (알림/종결/승률/PF/평균R, 화살표 ▲▼→)
      ③ 🔎 관찰 3줄 (규칙 기반 자동 선택, 양쪽 n≥10 · 최근 4주 누적)
      ④ ⏳ 다음 판단 (v6 평가 150건 · MFE/MAE 50건 진행률 — 사람이 결정)
      ⑤ 📋 판정 사유별 집계 (freqtrade /stats 형: n·비중·보유시간·실현%)
      ⑥ 🏆 작성자 랭킹 (E_LB 수학 불변, 표시만 상위 N + 역신호 확정)
      ⑦ 🎚️ 등급 캘리브레이션 (grade_ver 최신 표본만, 1줄/등급)
      ⑨ 각주 1줄 (결과 확인 기준: 터치 후 7일)

    v2 인자(전부 선택):
      current / previous  — analytics.weekly.summary(...) 결과 + {"alerts": N}
      observations        — analytics.weekly.observations(...) 결과
      pool_n / pool_days  — 관찰·집계 누적 창의 표본 수 / 일수
      milestones          — [{label, count, target, done, eta_days}, ...]
      outcome_stats       — analytics.weekly.outcome_stats(...) 결과
      calibration_ver     — 캘리브레이션 표본의 grade_ver (예: 'v6')
      period              — (start_ts, end_ts). 미지정이면 now 기준 7일 창
      max_chars           — 길이 예산(설정 weekly_report_max_chars)

    하위호환: baseline / raw_records / confluence / calibration_legacy /
    r_distribution / r_distribution_by_grade / holding_period / regime_heatmap 은
    **받되 무시한다**(해당 섹션이 2026-09-22 개편에서 제거됨 — 이유는 위
    '제거된 섹션과 이유' 주석). 기존 호출부(scripts/show_status.py 등)를 고치지
    않아도 되도록 시그니처만 유지한다."""
    now = time.time() if now is None else now
    half_life_days = settings.get("rank_half_life_days") if half_life_days is None else half_life_days
    z = settings.get("rank_z") if z is None else z
    prior_m = settings.get("rank_prior_m") if prior_m is None else prior_m
    min_neff = settings.get("rank_min_neff") if min_neff is None else min_neff
    min_n = settings.get("weekly_report_min_n") if min_n is None else min_n
    top_authors = settings.get("weekly_report_top_authors") if top_authors is None else top_authors
    pool_days = settings.get("weekly_report_pool_days") if pool_days is None else pool_days
    max_chars = settings.get("weekly_report_max_chars") if max_chars is None else max_chars
    rank_kw = dict(half_life_days=half_life_days, z=z, m=prior_m)

    rows_by_author = rows_by_author or {}
    total_rows = sum(len(rows) for rows in rows_by_author.values())

    # ① 헤더
    start_ts, end_ts = period if period else (now - 7 * 86400.0, now)
    lines = [_SEP, "📈 <b>주간 성적 리포트</b>",
             f"🗓 {_kst_md(start_ts)}~{_kst_md(end_ts)} (KST, 7일)"]
    if current:
        alerts = current.get("alerts")
        head = f"알림 {alerts}건 · " if alerts is not None else ""
        lines.append(f"📦 표본: {head}종결 {current.get('closed', 0)}건")
    else:
        lines.append(f"📦 표본: 작성자 {len(rows_by_author)}명 · 종결 {total_rows}건")

    # ② 이번 주 한눈에 / ③ 관찰 3줄 — v2 데이터가 주입된 경로에서만
    lines.extend(_overview_section(current, previous, min_n))
    if current is not None:
        lines.extend(_observations_section(observations, pool_n, pool_days))

    # ④ 다음 판단 / ⑤ 판정 사유별
    lines.extend(_milestones_section(milestones))
    lines.extend(_outcome_stats_section(outcome_stats, pool_days))

    # ⑥ 작성자 랭킹 (수학 불변 — analytics.ranking 그대로)
    lines.extend(_author_section(rows_by_author, now, rank_kw, min_neff,
                                 top_authors, reverse_confirmed))

    # ⑦ 등급 캘리브레이션 (최신 grade_ver 표본만, 1줄/등급)
    lines.extend(_calibration_compact(calibration_result, calibration_ver))

    # ⑨ 각주
    lines.append(_SEP)
    lines.append(_WEEKLY_FOOTNOTE)
    lines.append(_SEP)
    return _fit_weekly("\n".join(lines), max_chars)


# ── 적중 DB 해시체인 무결성 경보 (2026-07-27 기획 카드 #3) ────────────────
# 기존 렌더러(render_alert 등)와 무관한 별도 함수 - 하루 1회 중복방지는 호출부
# (monitor.price_check) 의 meta 게이트가 담당하고, 이 함수는 순수 렌더링만 한다.
# 정상 상황에선 절대 울리지 않는 안티게이밍 경보 - 판정 기록의 사후 변조·유실
# 자가 감지 결과를 사람이 읽을 수 있게 옮길 뿐이다.


def render_outcome_chain_alert(mismatch: dict) -> str:
    """storage.db.verify_outcome_chain() 의 첫 불일치 지점을 경보 텍스트로 변환.
    mismatch: {"level_id": int|None, "reason": str}."""
    lines = [
        _SEP,
        "🚨 <b>[적중 DB 무결성 경고]</b>",
        f"판정 해시체인 불일치 감지 (level_id={mismatch.get('level_id')}, "
        f"사유={html.escape(str(mismatch.get('reason')))})",
        "판정 기록이 사후에 변조되었거나 유실되었을 수 있습니다 - DB를 확인하세요.",
        _SEP,
    ]
    return "\n".join(lines)


# ── 가격체크 회차 정지(공백) 경고 (2026-07-27 기획 카드 #2 과제2) ──────────
# 기존 렌더러와 무관한 별도 함수 - 파일 끝에 추가만(개발자B 작업 경계). 하루 1회
# 중복방지는 호출부(monitor.price_check) 의 meta 게이트가 담당하고, 여긴 순수
# 렌더링만 한다. render_collect_stale_alert(수집 단계 정지)와 잡는 대상이 다르다 -
# 이건 2분 주기 회차(가격체크) 자체가 멈춘 것을 본다.


def render_tp_partial_alert(coin: str, tp_n: int, tp_total: int,
                            tp_krw: float, entry_krw: float,
                            post_url: str = None,
                            next_tp_krw: float = None) -> str:
    """다단계 TP 중간·최종 도달 알림.

    tp_n: 방금 도달한 TP 번호(1-indexed). tp_total: 전체 TP 수.
    tp_krw: 도달가(KRW). entry_krw: 진입가(KRW, 대비 % 계산용).
    post_url (2026-08-08 사용자 결정): 해당 레벨의 원문 글 — 있으면 본알림과
    같은 형식의 출처 링크를 마지막 구분선 아래에 추가."""
    is_last = (tp_n >= tp_total)
    step_label = "🏁 최종목표 달성" if is_last else f"({tp_n}/{tp_total}단계)"
    if entry_krw and entry_krw > 0:
        pct = (tp_krw - entry_krw) / entry_krw * 100
        price_line = f"    달성가:  {_fmt_krw(tp_krw)}원  (진입 {pct:+.1f}%)"
    else:
        price_line = f"    달성가:  {_fmt_krw(tp_krw)}원"
    lines = [
        _SEP,
        f"✅ <b>[TP{tp_n} 적중]</b> <b>{html.escape(coin)}</b>  {step_label}",
        price_line,
    ]
    if not is_last:
        if next_tp_krw is not None:
            _n = _fmt_krw(next_tp_krw)
            if entry_krw and entry_krw > 0:
                _n_pct = (next_tp_krw - entry_krw) / entry_krw * 100
                _tp_part = f"TP{tp_n + 1}  {_n}원  (진입 {_n_pct:+.1f}%)"
            else:
                _tp_part = f"TP{tp_n + 1}  {_n}원"
            _single = f"    다음 목표:  {_tp_part}"
            # 폰 화면 32칼럼에서 줄내림되는 케이스는 미리 2줄로 분리하고
            # 두 번째 줄은 같은 4스페이스 들여쓰기로 정렬(사용자 요청).
            if sum(_display_width(c) for c in _single) > _MAX_LINE_COLS:
                lines.append("    다음 목표:")
                lines.append(f"    {_tp_part}")
            else:
                lines.append(_single)
        else:
            lines.append(f"    다음 목표:  TP{tp_n + 1} 계속 모니터링 중")
    lines.append(_SEP)
    if post_url:
        lines.append(_source_line([post_url]))
    return _finalize(lines)


def render_volume_spike_alert(coin: str, multiplier: float,
                               current_bil: float, avg_bil: float,
                               next_tp_krw=None, tp_idx=None,
                               tp_count=None, post_urls=None,
                               cur_price_krw=None, change_rate_24h=None) -> str:
    """거래량 급증 알림 (Feature 4 — 진입가 터치 후 2단계 알림).
    2026-07-31 지표 교체: "현재 24h vs 7일 평균" → "최근 1시간 vs 직전 20시간
    (완결 60분봉) 평균"(RVOL 관례). 숫자만 갈면 오독하므로 라벨을 함께 교체.
    coin: 코인 심볼, multiplier: 최근1h/20h평균 배수, current_bil/avg_bil: 단위 억원.
    next_tp_krw/tp_idx/tp_count (2026-07-31 사용자 요청, 2차에서 동적 선정):
    현재가 바로 위의 TP 원화가와 그 단계 (k/N) — "지금 급증 중인데 다음 목표까지
    얼마 남았나"를 알림 안에서 바로 보게. 유효 TP 가 없던 셋업(+10% 폴백 등록)
    이나 이미 전 TP 위면 None 으로 들어와 행 자체를 생략한다.
    post_urls (2026-08-08 사용자 결정): 이 감시를 등록시킨 원문 글들 — 있으면
    본알림과 같은 형식의 출처 링크를 마지막 구분선 아래에 추가(재터치로 여러
    글이 밴드에 합쳐졌으면 여러 개일 수 있음)."""
    lines = [
        _SEP,
        f"🔥 <b>[거래량 급증]</b> <b>{html.escape(coin)}</b>",
        f"    최근 1시간:  {current_bil:.1f}억  ({multiplier:.1f}x 급증)",
        f"    20시간 평균:  {avg_bil:.1f}억",
    ]
    if cur_price_krw is not None:
        lines.extend(_cur_price_lines(cur_price_krw, change_rate_24h))
    if next_tp_krw:
        # 원화 표기는 타점 블록 _krw 관례와 동일 (1원 미만 소수 4자리)
        _p = f"{next_tp_krw:,.0f}" if next_tp_krw >= 1 else f"{next_tp_krw:.4f}"
        _n = f" ({tp_idx}/{tp_count}단계)" if tp_idx and tp_count else ""
        lines.append(f"    다음 TP:  {_p}원{_n}")
    lines.append(_SEP)
    _urls = [u for u in (post_urls or []) if u]
    if _urls:
        lines.append(_source_line(_urls))
    return _finalize(lines)


def _fmt_krw(v: float) -> str:
    """1원 미만(SHIB 등)은 소수 표기, 이상은 정수 콤마."""
    return f"{v:,.0f}" if v >= 1 else f"{v:.4f}"


def _fmt_usd_notional(v: float) -> str:
    """OI 명목가액(USD) 표기 — 억 단위(원화 전용 어휘)와 섞이지 않게
    B(십억)/M(백만) 그대로 사용. 예: 4.83B / 210.5M.
    2026-08-08 재검토: 1e9 미만이어도 반올림 결과가 M 표시로 1000.0 을
    찍는 경계(예: 999,999,999.9 → "$1000.0M")를 B 로 승격해 값이 실제
    자릿수와 어긋나 보이는 걸 막는다."""
    if v >= 1e9 or round(v / 1e6, 1) >= 1000.0:
        return f"${v / 1e9:.2f}B"
    return f"${v / 1e6:.1f}M"


def render_oi_spike_alert(coin: str, prev_oi_usd: float, cur_oi_usd: float,
                          pct: float, current_krw: float = None,
                          post_urls=None, change_rate_24h: float = None) -> str:
    """OI(미결제약정) 급증 알림 (2026-08-08 사용자 결정) — 진입가 터치와
    무관한 별도 이벤트. 거래량 급증 알림(render_volume_spike_alert)과 같은
    격식의 독립 카드. OI 는 CoinGecko 가 이미 USD 로 주는 값이라 원화 환산
    없이 그대로 표시(B/M 단위 — "억"은 원화 전용 어휘라 혼동 방지).
    current_krw 는 참고용 현재가 한 줄, 없으면 생략.
    post_urls (2026-08-10): 이 코인의 활성 레벨 원문 글들 — 거래량급증과
    동일한 형식의 출처 링크를 마지막 구분선 아래에 추가."""
    lines = [
        _SEP,
        f"📈 <b>[선물 유입 급증]</b>",
        f"    <b>{html.escape(coin)}</b>",
        f"    1시간 전:  {_fmt_usd_notional(prev_oi_usd)}",
        f"    지금:  {_fmt_usd_notional(cur_oi_usd)}  (1h {pct:+.1f}%)",
    ]
    if current_krw:
        lines.extend(_cur_price_lines(current_krw, change_rate_24h))
    lines.append(_SEP)
    _urls = [u for u in (post_urls or []) if u]
    if _urls:
        lines.append(_source_line(_urls))
    return _finalize(lines)


# ── 역신호 확정/해제 경보 (S9, 2026-08-01 사용자 결정 Q1=B안/Q2=해제 있음) ──────
# 기존 렌더러와 무관한 별도 함수 — 본알림(render_alert) 양식은 §4 불변. 중복 방지는
# 호출부(scripts/run_cycle.py 스냅샷 훅)의 meta 확정 기록이 담당하고, 여긴 순수
# 렌더링만 한다. 알림 필터·발송량에는 영향 없음(확정돼도 그 작성자의 본알림은
# 계속 나간다 — B안 원칙).


def _reverse_snap_lines(snaps: list) -> list:
    """주차별 'W31: E_LB -0.78 (n_eff 7.4)' 행 — 오래된 주 → 최신 주 순.
    snaps 는 db.get_author_last_n_snapshots() 반환(최신순)을 그대로 받는다."""
    lines = []
    for s in reversed(snaps[:2]):
        wk = (s.get("week_kst") or "?").split("-")[-1]  # "2026-W31" → "W31"
        e = s.get("e_lb")
        n = s.get("neff_r") or 0.0
        e_txt = f"{e:+.2f}" if e is not None else "-"
        lines.append(f"  {wk}: E_LB {e_txt} (n_eff {n:.1f})")
    return lines


def render_reverse_confirm_alert(author: str, snaps: list) -> str:
    """역신호 확정 경보 (확정 시점 1회 발송). snaps: 최근 2주 스냅샷(최신순).
    '<' 는 반드시 &lt; (parse_mode=HTML — 날 '<' 는 400 Can't parse entities)."""
    lines = [
        _SEP,
        f"🔻 <b>[역신호 확정]</b> @{html.escape(author)}",
        _SEP,
        "2주 연속 E_LB &lt; 0 확인",
    ]
    lines.extend(_reverse_snap_lines(snaps))
    lines.append("이 작성자의 신호는 계속 알림드리지만")
    lines.append("참고용으로 활용하세요.")
    lines.append(_SEP)
    return "\n".join(lines)


def render_reverse_release_alert(author: str, snaps: list) -> str:
    """역신호 해제 알림 (2주 연속 회복 시 1회 발송) — 확정 경보의 대칭."""
    lines = [
        _SEP,
        f"✅ <b>[역신호 해제]</b> @{html.escape(author)}",
        _SEP,
        "2주 연속 E_LB ≥ 0 회복 확인",
    ]
    lines.extend(_reverse_snap_lines(snaps))
    lines.append("역신호 확정 표시를 해제합니다.")
    lines.append("(알림 발송은 확정 중에도 그대로였습니다)")
    lines.append(_SEP)
    return "\n".join(lines)


def render_news_brief(coin_symbol: str, channel: str, summary: str,
                      url: str = "") -> str:
    """뉴스·시황 요약 알림 (2026-08-17). 매매 시그널이 아닌 코인별 시황·뉴스
    게시글의 원문 요약. `notify/news_brief.py::maybe_send_news_brief` 가 유일 호출부.

    - 헤더: `📰 [뉴스·시황] SYMBOL`
    - 메타: 채널 핸들
    - 본문: 원문 앞 N자 (호출부에서 요약·클리핑 완료)
    - 하단: 원문 링크 (있으면)"""
    safe_sym = html.escape(coin_symbol or "?")
    safe_ch = html.escape(channel or "?")
    safe_sum = html.escape(summary or "")
    lines = [
        _SEP,
        f"📰 <b>[뉴스·시황] {safe_sym}</b>",
        f"채널: @{safe_ch}",
        _SEP,
        safe_sum,
    ]
    if url:
        lines.append(_SEP)
        lines.append(f'🔗 <a href="{html.escape(url)}">원문</a>')
    return "\n".join(lines)


# 러너 배정 대기가 이 이상이면 정지의 원인을 'GitHub 측 지연'으로 지목한다. 정상
# 회차의 대기는 0~1분(체크아웃·pip 포함)이라 10분이면 이미 비정상이고, 트리거 유실과는
# 구분된다(트리거가 안 왔으면 대기 자체가 없다).
_QUEUE_WAIT_CAUSE_MIN = 10.0


def render_price_check_gap_alert(gap_minutes: float, threshold_minutes: float,
                                 queue_wait_min: Optional[float] = None) -> str:
    """직전 회차와의 공백이 임계를 넘었을 때의 경고. gap_minutes: 감지된 공백(분),
    threshold_minutes: 임계값(분, config.settings.price_check_gap_alert_minutes),
    queue_wait_min: 이 회차가 GitHub 러너를 배정받기까지 대기열에 있던 시간(분,
    price-check.yml 이 측정해 env 로 전달; 로컬/미측정이면 None).

    2026-09-13 291분 정지: cron-job.org 는 4분마다 정상 디스패치했는데 GitHub 가
    러너를 4시간 47분 배정하지 않았다(당일 GitHub Actions 공식 장애). 종전 문구는
    이 경우에도 "cron-job.org 와 schedule 이 모두 실패했을 수 있다"고 엉뚱한 곳을
    지목했다 - 대기 시간이 있으면 원인을 가려 말한다."""
    if queue_wait_min is not None and queue_wait_min >= _QUEUE_WAIT_CAUSE_MIN:
        cause = (f"원인: GitHub 러너 배정 대기 {queue_wait_min:.0f}분 - 트리거(cron-job.org)는 "
                 "도달했고 GitHub Actions 측 지연입니다. githubstatus.com 을 확인하세요.")
    else:
        cause = ("cron-job.org 주 경로와 GitHub schedule 백업이 모두 실패했을 수 있습니다 - "
                 "Actions 실행 이력을 확인하세요.")
    lines = [
        _SEP,
        "🚨 <b>[가격체크 회차 정지 경고]</b>",
        f"직전 회차 이후 공백 {gap_minutes:.0f}분 (임계 {threshold_minutes:.0f}분)",
        cause,
        _SEP,
    ]
    return "\n".join(lines)
