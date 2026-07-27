"""
텔레그램 공개채널 수집기 — 두 번째 입력원 (2026-07-27 기획 카드 #14 인프라).

왜 필요한가: 지금 유일한 입력원이 TradingView 하나뿐이라 403 차단 한 번이면 그
회차 수집이 통째로 0건이 된다(2026-07-26/27 실측 반복). 텔레그램 공개채널은
`https://t.me/s/{채널명}` 으로 **로그인·API키·토큰 없이** 최근 글 HTML 이 그대로
열려 있어(아래 실측 참고) 완전히 독립된 경로로 같은 성격의 글을 받아올 수 있다.

⚠️ 기본 OFF·빈 화이트리스트: 이 모듈이 배포돼도 config/settings.py 의
`telegram_source_enabled=False` + `telegram_source_channels=[]` 인 한 호출부는
아무 요청도 보내지 않는다. 채널 목록은 사장님 승인 사항이다.

── HTML 구조 실측 (2026-07-27, t.me/s/durov · t.me/s/telegram) ────────────────
  · 정상 채널: 200 + `<section class="tgme_channel_history js-message_history">`
    안에 글 20건이 오래된 순서로 들어있다. 글 1건의 형태:
      <div class="tgme_widget_message_wrap js-widget_message_wrap">
        <div class="tgme_widget_message ..." data-post="durov/514" ...>
          <div class="tgme_widget_message_text js-message_text" dir="auto">본문</div>
          <span class="tgme_widget_message_views">3.46M</span>
          <a class="tgme_widget_message_date" href="https://t.me/durov/514">
            <time datetime="2026-05-16T13:57:49+00:00" class="time">13:57</time></a>
      → 글 URL 은 data-post 로 조립(https://t.me/{채널}/{번호}), 시각은 ISO8601
        (타임존 포함)이라 추측 없이 정확히 epoch 로 환산된다.
  · 삭제/존재하지 않는 채널: 200(404 아님!) + `tgme_page_wrap` 안내 페이지,
    <title>Telegram: Contact @xxx</title>, 멤버수(`tgme_page_extra`) 없음.
  · 존재하지만 웹 미리보기 불가(비공개 전환·그룹): 200 + `tgme_page_wrap`,
    <title>Telegram: View @xxx</title>, `tgme_page_extra`("98 700 members")가 있음.
  → 상태코드로는 구분이 안 되므로 위 두 신호로 갈라 로그를 남긴다(요구사항).

계약 (collector/tradingview.py fetch_ideas 와 동일 — 하류 무수정 재사용 목적):
  fetch_posts(channel, timeout, max_age_hours=None, max_posts=20) -> list[dict]
  반환 dict 키: symbol, title, description, author, author_followers, url,
                published_at(epoch float|None), age_minutes(float|None),
                direction, likes_count, comments_count, ticker (+ views, channel)
  ※ symbol/ticker 만 계약과 의미가 다르다: TradingView 는 '심볼별' 조회라 호출
    시점에 심볼이 정해지지만, 채널 글은 어느 코인 얘기인지 본문을 봐야 안다.
    그래서 여기서는 None 으로 두고 호출부가 match_symbol() 로 해석한다.
  실패·차단·스키마 변화는 전부 조용히 [] + logger.warning (회차 생존 최우선).
  import 시 네트워크 없음. print 금지(logging 만 — Windows cp949 콘솔 대비).

의존성: **표준 라이브러리만** 으로 파싱한다(bs4 등 추가 금지 — requirements.txt 불변).
페이싱: 비공식 경로라 공격적으로 긁지 않는다. 채널 간 간격은 호출부 책임이고,
모듈은 같은 채널 재조회 최소 간격(_MIN_INTERVAL_SEC)만 스스로 강제한다.
"""

import html as _html
import logging
import re
import time
from datetime import datetime, timezone
from typing import List, Optional, Tuple

# TradingView 수집기와 같은 이유(데이터센터 IP 차단 완화)로 curl_cffi 가 있으면 쓴다.
# 없으면 requests + 평범한 Chrome 헤더로 강등 — requirements.txt 는 건드리지 않는다.
try:
    from curl_cffi import requests as _http
    _IMPERSONATE = "chrome120"
except ImportError:  # pragma: no cover
    import requests as _http
    _IMPERSONATE = None

logger = logging.getLogger("alert.telegram_source")

_BASE = "https://t.me/s/"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# 채널명 허용 문자 — 텔레그램 규칙(영문 시작, 영숫자/밑줄, 5~32자)을 그대로 강제한다.
# 설정 파일에 오타나 URL 전체가 들어와도 여기서 걸러 이상한 경로로 요청이 나가지 않게.
_CHANNEL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")

# 차단 대응: TradingView 모듈과 같은 철학(즉시 포기 + 전역 쿨다운). 텔레그램은
# 비공식 경로라 더 보수적으로 — 차단 신호가 보이면 이번 회차는 통째로 접는다.
_BLOCK_COOLDOWN_SEC = 1800.0   # 30분
_blocked_until = 0.0

# 같은 채널을 짧은 간격으로 다시 긁지 않게 하는 모듈측 최소 방어선(호출부가 페이싱을
# 잊어도 폭주가 불가능하도록). 설정값(telegram_source_sleep_sec)과 별개의 하한이다.
_MIN_INTERVAL_SEC = 3.0
_last_fetch_at: dict = {}   # channel -> epoch

_MAX_POSTS_HARD_CAP = 100   # 스키마 변화로 파싱이 폭주해도 하류로 새지 않게 하는 상한


# ── HTTP 계층 ─────────────────────────────────────────────────

_session = None


def _get_session():
    global _session
    if _session is None:
        if _IMPERSONATE:
            _session = _http.Session(impersonate=_IMPERSONATE)
        else:
            _session = _http.Session()
            _session.headers.update({
                "User-Agent": _UA,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            })
    return _session


def is_blocked() -> bool:
    """차단 쿨다운 중이면 True — 호출부는 남은 채널 루프를 즉시 중단할 것."""
    return time.time() < _blocked_until


def _get(url: str, timeout: float, max_retry: int = 2) -> Tuple[Optional[str], bool]:
    """GET → (본문|None, 차단 여부). 403/429/503 은 재시도 없이 즉시 포기 + 쿨다운
    (비공식 경로에서 재시도는 차단을 키울 뿐이다). 그 외 오류만 짧게 재시도."""
    global _blocked_until
    now = time.time()
    if now < _blocked_until:
        logger.warning("[tg] 차단 쿨다운 중(%.0f초 남음) - 요청 생략", _blocked_until - now)
        return None, True
    delay = 2.0
    for attempt in range(1, max_retry + 1):
        try:
            resp = _get_session().get(url, timeout=timeout)
            status = resp.status_code
            if status == 200:
                return resp.text, False
            if status in (403, 429, 503):
                _blocked_until = time.time() + _BLOCK_COOLDOWN_SEC
                logger.warning("[tg] 차단 신호(status=%s) - 즉시 포기 + %.0f분 쿨다운",
                               status, _BLOCK_COOLDOWN_SEC / 60.0)
                return None, True
            logger.warning("[tg] status=%s (시도 %d/%d): %s", status, attempt, max_retry, url)
        except Exception as e:  # curl_cffi/requests 예외 계열이 달라 광범위 캐치
            logger.warning("[tg] 요청 실패(%d/%d) %s: %s", attempt, max_retry, url, e)
        if attempt < max_retry:
            time.sleep(delay)
            delay *= 2
    return None, False


# ── HTML 파싱 유틸 (표준 라이브러리만) ──────────────────────────

def _extract_balanced_div(text: str, open_end: int) -> str:
    """여는 <div ...> 태그가 끝난 위치(open_end)부터 짝이 맞는 </div> 직전까지 반환.

    단순 non-greedy(.*?</div>)로 자르지 않는 이유: 지금 실측 HTML 의 본문 div 안에는
    중첩 div 가 없지만(2026-07-27), 텔레그램이 본문 안에 래퍼 div 를 하나만 넣어도
    본문이 조용히 잘려나간다. 깊이를 세면 그 변화에 면역이다."""
    depth = 1
    i = open_end
    n = len(text)
    while i < n and depth > 0:
        nxt = text.find("<div", i)
        end = text.find("</div", i)
        if end < 0:
            return text[open_end:]           # 닫는 태그 소실 — 남은 전부(방어)
        if 0 <= nxt < end:
            depth += 1
            i = nxt + 4
        else:
            depth -= 1
            if depth == 0:
                return text[open_end:end]
            i = end + 5
    return text[open_end:]


def _strip_html(fragment: str) -> str:
    """텔레그램 본문 조각 → 평문. <br> 은 줄바꿈으로 살린다(작성자가 'Entry: ...'
    같은 항목을 줄 단위로 쓰기 때문 — 한 줄로 뭉치면 추출기가 라벨 뒤 30자 창에서
    엉뚱한 숫자를 끌어올 수 있다)."""
    fragment = re.sub(r"<script[^>]*>.*?</script>", " ", fragment, flags=re.S | re.I)
    fragment = re.sub(r"<style[^>]*>.*?</style>", " ", fragment, flags=re.S | re.I)
    fragment = re.sub(r"<br\s*/?>", "\n", fragment, flags=re.I)
    fragment = re.sub(r"</(p|div|li)\s*>", "\n", fragment, flags=re.I)
    fragment = re.sub(r"<[^>]+>", "", fragment)
    text = _html.unescape(fragment)
    text = text.replace(" ", " ").replace("\r", "")
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.split("\n")]
    return "\n".join(ln for ln in lines if ln).strip()


def _iso_to_epoch(s: str) -> Optional[float]:
    """'2026-05-16T13:57:49+00:00' → epoch float. 실패 시 None."""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (ValueError, TypeError):
        return None


_VIEWS_RE = re.compile(r'tgme_widget_message_views"[^>]*>([^<]+)<')
_DATE_RE = re.compile(
    r'tgme_widget_message_date"[^>]*>\s*<time datetime="([^"]+)"', re.I)
_ANY_TIME_RE = re.compile(r'<time datetime="([^"]+)"', re.I)
_DATA_POST_RE = re.compile(r'data-post="([A-Za-z0-9_]+)/(\d+)"')
_TEXT_DIV_RE = re.compile(r'<div class="[^"]*js-message_text[^"]*"[^>]*>')


def _parse_views(raw: str) -> Optional[int]:
    """'3.46M' / '12.3K' / '842' → int. 표기 변화 시 조용히 None."""
    m = re.match(r"^\s*([\d.,]+)\s*([KMkm]?)\s*$", raw or "")
    if not m:
        return None
    try:
        num = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    unit = m.group(2).upper()
    if unit == "K":
        num *= 1_000
    elif unit == "M":
        num *= 1_000_000
    return int(num)


def _block_to_post(block: str, channel: str, now: float) -> Optional[dict]:
    """글 블록 HTML → 계약 dict. 본문·URL 중 하나라도 못 뽑으면 None(그 글만 스킵)."""
    m_post = _DATA_POST_RE.search(block)
    if not m_post:
        return None
    post_id = m_post.group(2)
    # URL 은 설정에 적힌 채널명으로 조립한다 — data-post 의 채널명은 t.me 가 대소문자를
    # 정규화해 돌려주는 값이라 설정값과 다를 수 있고, signal_key 정체성이 흔들리면
    # 같은 글이 중복 레벨로 저장된다.
    url = f"https://t.me/{channel}/{post_id}"

    m_text = _TEXT_DIV_RE.search(block)
    if not m_text:
        return None  # 미디어 전용 글(사진/영상만) — 셋업 텍스트가 없으니 버린다
    body = _strip_html(_extract_balanced_div(block, m_text.end()))
    if not body:
        return None

    m_date = _DATE_RE.search(block) or _ANY_TIME_RE.search(block)
    published_at = _iso_to_epoch(m_date.group(1)) if m_date else None

    m_views = _VIEWS_RE.search(block)
    views = _parse_views(m_views.group(1)) if m_views else None

    # 제목이 따로 없는 매체라 본문 첫 줄을 제목으로 쓴다(하류는 title+description 을
    # 이어붙여 파싱하므로 중복돼도 결과가 달라지지 않는다).
    title = body.split("\n", 1)[0][:120]

    return {
        # ⚠️ symbol/ticker 는 호출부가 match_symbol() 로 채운다(모듈 docstring 참고).
        "symbol": None,
        "ticker": None,
        "title": title,
        "description": body,
        # 작성자 = 채널명. 기존 E_LB·적중DB 가 '작성자' 축으로 그대로 흡수한다.
        "author": channel,
        "author_followers": None,   # 공개 미리보기에 구독자 수가 없다(실측)
        "url": url,
        "published_at": published_at,
        "age_minutes": (now - published_at) / 60.0 if published_at else None,
        "direction": None,          # 본문에서 추출기(parse_setup)가 판단한다
        "likes_count": None,        # 반응 수는 이모지별로 쪼개져 있어 단일값 의미가 없다
        "comments_count": None,
        # 아래는 계약 밖 부가 정보 — 하류는 읽지 않는다(사후 분석용).
        "views": views,
        "channel": channel,
    }


def _diagnose_no_history(html_text: str, channel: str) -> None:
    """글 목록이 없는 응답의 원인을 구분해 로그로 남긴다(요구사항: 채널 삭제/비공개
    전환을 구분할 것). 실측 근거는 모듈 docstring 참고 — 둘 다 200 이라 상태코드로는
    갈리지 않는다."""
    if "tgme_page_extra" in html_text:
        # 멤버/구독자 수가 렌더된다 = 대상은 살아있는데 웹 미리보기만 막힌 상태
        # (비공개 전환, 또는 채널이 아니라 그룹). 목록에서 빼야 할 후보.
        logger.warning("[tg] %s: 채널은 존재하나 웹 미리보기 불가(비공개 전환/그룹 추정) "
                       "- 화이트리스트에서 제외 검토", channel)
    elif "tgme_page_wrap" in html_text:
        logger.warning("[tg] %s: 채널을 찾을 수 없음(삭제/이름변경/오타 추정)", channel)
    else:
        # 안내 페이지도 목록도 아님 = 페이지 구조가 바뀐 것. 파서 점검 신호.
        logger.warning("[tg] %s: 알 수 없는 응답 구조 - 파싱 스키마 변경 의심", channel)


# ── 공개 API ─────────────────────────────────────────────────

def fetch_posts(channel: str, timeout: float, max_age_hours: Optional[float] = None,
                max_posts: int = 20) -> List[dict]:
    """공개채널 최근 글 목록. 실패/차단/구조변화 시 [] (예외를 위로 던지지 않는다)."""
    try:
        return _fetch_posts_impl(channel, timeout, max_age_hours, max_posts)
    except Exception as e:  # noqa: BLE001 - 수집 1채널 오류가 회차를 죽이면 안 된다
        logger.warning("[tg] fetch_posts(%s) 예상외 오류 - 빈 결과 반환: %s", channel, e)
        return []


def _fetch_posts_impl(channel: str, timeout: float, max_age_hours: Optional[float],
                      max_posts: int) -> List[dict]:
    channel = (channel or "").strip().lstrip("@")
    if not _CHANNEL_RE.match(channel):
        logger.warning("[tg] 채널명 형식 오류 - 요청 생략: %r", channel)
        return []

    wait = _MIN_INTERVAL_SEC - (time.time() - _last_fetch_at.get(channel, 0.0))
    if wait > 0:
        time.sleep(wait)
    _last_fetch_at[channel] = time.time()

    html_text, blocked = _get(_BASE + channel, timeout)
    if blocked or not html_text:
        return []

    if "js-widget_message_wrap" not in html_text:
        _diagnose_no_history(html_text, channel)
        return []

    now = time.time()
    posts: List[dict] = []
    # 글 단위 분리: 래퍼 클래스로 자른다. 첫 조각은 헤더/네비라 버려진다(data-post 없음).
    for block in html_text.split("js-widget_message_wrap")[1:]:
        try:
            post = _block_to_post(block, channel, now)
        except Exception as e:  # noqa: BLE001 - 글 1건 파싱 실패가 채널 전체를 버리면 안 됨
            logger.warning("[tg] %s: 글 1건 파싱 실패 - 스킵: %s", channel, e)
            continue
        if post:
            posts.append(post)
        if len(posts) >= _MAX_POSTS_HARD_CAP:
            break

    if not posts:
        logger.warning("[tg] %s: 글 목록은 있으나 파싱 결과 0건 - 스키마 변경 의심", channel)
        return []

    # url 기준 중복 제거 (핀 고정 글이 목록에 두 번 실리는 경우 대비)
    seen, unique = set(), []
    for p in posts:
        if p["url"] not in seen:
            seen.add(p["url"])
            unique.append(p)
    posts = unique

    # 연령 필터 — 시각을 못 뽑은 글은 관대하게 통과(tradingview.py 와 같은 철학:
    # 수집 단계 손실을 최소화하고 판단은 후단에 맡긴다).
    if max_age_hours is not None:
        cutoff_min = max_age_hours * 60.0
        posts = [p for p in posts
                 if p["age_minutes"] is None or p["age_minutes"] <= cutoff_min]

    # 페이지는 오래된 순이라 최신 우선으로 뒤집는다(TradingView 경로와 정렬 일치).
    posts.sort(key=lambda p: p["published_at"] or 0.0, reverse=True)
    limit = max(1, int(max_posts or 20))
    posts = posts[:limit]
    logger.info("[tg] %s: %d건 수집", channel, len(posts))
    return posts


# ── 심볼 해석 ─────────────────────────────────────────────────
# TradingView 는 '심볼 페이지'를 긁으므로 코인이 자명하지만, 채널 글은 본문을 봐야
# 어느 코인 얘기인지 안다. 오분류는 엉뚱한 코인에 레벨을 붙이는 사고(= 잘못된 알림)라
# **모호하면 버린다**: 유니버스 심볼이 정확히 하나만 등장할 때만 채택한다.

def _symbol_pattern(symbol: str) -> "re.Pattern":
    """'BTC' → $BTC / BTC / BTCUSDT / BTC/USDT / KRW-BTC 를 잡는 경계 패턴.
    대소문자를 구분한다(의도): 'sol'(태양)·'one'·'arb' 같은 일반 단어가 소문자로
    쓰였을 때 코인으로 오인되는 것을 막는다. 티커는 관례상 대문자로 쓴다."""
    s = re.escape(symbol.upper())
    return re.compile(
        r"(?<![A-Za-z0-9])(?:KRW-|\$)?" + s + r"(?:/?USDT|/?USD|/?KRW|PERP)?(?![A-Za-z0-9])"
    )


_pattern_cache: dict = {}

# 페어 표기의 '기준통화'(base). "$ETC/USDT", "BTCUSDT", "SOL/USD" 의 앞부분을 잡는다.
# 2026-07-27 실전 발견: 전수 스캔만 하면 시그널 채널의 상용구가 진짜 티커와 충돌한다 —
# "COIN: $ETC/USDT" 한 줄에서 ETC 말고도 견적통화 **USDT** 와, 바로 위 "SIGNAL ID:" 의
# **ID**(Space ID, 업비트 상장)까지 잡혀 3개 = '모호'로 글 전체가 버려졌다(8건 중 7건).
# 페어 표기가 있으면 그게 글의 주제이므로 전수 스캔보다 우선한다.
#
# 2026-07-27 2차 교차검토 확정안. 이전 판(즉시수리 #1)은 "숫자-only base 배제"만
# 했는데, **결함의 본질은 숫자가 아니라 구분자였다**: 옛 패턴의 `\s*[/\-]?\s*` 는
# 구분자를 통째로 생략할 수 있어서, 견적통화 바로 앞의 아무 대문자 토큰이나 base 로
# 승격됐다 — `BALANCE USDT`·`TAKE PROFIT IN USDT`·`MARGIN USDT 500`·`TOTAL PERP`,
# 그리고 레버리지 표기 `2X USD`/`5X USDT`/`10X PERP` 가 전부 같은 구멍으로 들어왔다.
#
# 왜 정밀도 우선인가(설계 원칙 — 비용이 비대칭이다):
#   · 진짜 페어를 놓치면(FN) 아래 전수 스캔 폴백이 받아준다 → 손실 ≈ 0.
#   · 가짜 페어를 잡으면(FP) `if bases:` 가 참이 되어 폴백이 봉쇄되고 글이 통째로
#     버려진다 → 손실 100%.
#   따라서 애매하면 페어가 아닌 것으로 본다.
#
# 두 갈래로 나눈 이유:
#   ① `$` 접두 = 사람이 손으로 붙인 티커 표식이라 신뢰할 수 있다 → 구분자 주변
#      공백을 허용한다(`$SOL / USDT` 는 실전 표기).
#   ② 무접두 = 붙여쓰기(`BTCUSDT`) 또는 공백 없는 구분자(`ETH-USDT`)만 인정한다.
#      공백을 허용하면 `TAKE PROFIT - USDT`(base=PROFIT)·`STOP LOSS / USD`(base=LOSS)
#      가 그대로 통과한다(재현 확인). 대가로 `SOL USDT`·`SOL / USDT` 같은 무접두
#      공백 페어는 페어 경로를 잃지만, 위 비대칭 비용상 폴백으로 내려가는 게 옳다.
#
# ⚠️ "base 는 영문자 2자 이상" 안은 **기각**한다(2차 검토에서 반박됨).
#    실물 유니버스(data/universe.json 81심볼)에 **`2Z`(영문자 1자)** 가 있고 `USD1`
#    도 있다. 이 규칙을 넣으면 `$2Z/USDT` 가 페어 경로를 잃고 SIGNAL ID 류 오귀속에
#    노출된다. 숫자-only 배제(영문자 **1자** 이상)가 상한이다 — 100·4 는 탈락하고
#    1INCH·2Z·USD1 은 살아남는다.
_PAIR_BASE = r"(?=[0-9]*[A-Z])[A-Z0-9]{2,10}"
_PAIR_QUOTE = r"(?:USDT|USDC|BUSD|USD|PERP)"
_PAIR = re.compile(
    r"(?<![A-Za-z0-9])(?:"
    r"\$(?P<sb>" + _PAIR_BASE + r")\s*[/\-]\s*"     # ① $ 접두 → 구분자 주변 공백 허용
    r"|(?P<nb>" + _PAIR_BASE + r")(?:[/\-]|)"       # ② 무접두 → 붙여쓰기/무공백 구분자만
    r")" + _PAIR_QUOTE + r"(?![A-Za-z0-9])"
)
# 레버리지 토큰 배제는 **정규식이 아니라 후처리**로 한다. 정규식에 lookahead 로
# 밀어넣으면 `20XUSDT`(무구분) 때문에 패턴이 난해해지고, 어휘 판단이 '모양' 판단에
# 섞인다 — 정규식은 모양만, 어휘는 이름 붙은 술어로. `X` 한정이라 실물 심볼 `2Z` 는
# 안 다친다(`2X` 라는 티커가 상장되는 날엔 이 줄을 다시 볼 것).
_LEVERAGE_BASE = re.compile(r"^[0-9]{1,3}X$")
# 견적통화·상용구는 그 자체로 티커여도 '이 글의 주제'가 아니다. 전수 스캔 폴백에서만
# 쓰는 최소 목록 — 여기에 더 채워 넣기 시작하면 곧 단어 블랙리스트가 되므로,
# 진짜 견적통화만 둔다.
_QUOTE_ONLY = {"USDT", "USDC", "BUSD", "USD"}


def match_symbol(text: str, symbols) -> Optional[str]:
    """본문에서 유니버스 심볼을 찾아 '정확히 하나'면 그 심볼을, 아니면 None 을 반환.

    ① 페어 표기($ETC/USDT 등)가 있으면 그 base 를 우선 채택한다 — 서로 다른 base 가
       둘 이상이면 모호로 버린다.
    ② 없으면 전수 스캔 폴백. 0개(코인 얘기가 아닌 글)와 2개 이상(한 글에 여러 코인)은
       모두 버린다 — 억지로 하나 고르면 엔트리가 엉뚱한 코인에 붙는다.
    심볼 1글자는 오탐이 압도적이라 아예 제외한다."""
    if not text:
        return None
    upper = {s.upper() for s in (symbols or ()) if s and len(s) >= 2}
    if not upper:
        return None

    # 페어 표기는 '이 글의 주제'에 대해 권위가 있다. 하나라도 있으면 전수 스캔으로
    # 넘어가지 않는다 — 넘어가면 추적 대상이 아닌 코인의 글이 상용구에 걸린 엉뚱한
    # 티커로 귀속된다(실측: "$GRAM/USDT" 글이 "SIGNAL ID:" 의 ID 코인에 붙었다.
    # GRAM 은 업비트 미상장이라 그냥 버려야 하는 글이다). 오귀속은 잘못된 코인에
    # 잘못된 진입가로 알림이 나가는 사고라, 버리는 쪽이 항상 낫다.
    bases = set()
    for m in _PAIR.finditer(text):
        b = (m.group("sb") or m.group("nb")).upper()
        if b in _QUOTE_ONLY or _LEVERAGE_BASE.match(b):
            continue   # 견적통화 자기자신 / 레버리지 표기는 base 가 아니다
        bases.add(b)
    if bases:
        tracked = bases & upper
        return next(iter(tracked)) if len(tracked) == 1 else None

    found = set()
    for sym in upper:
        if sym in _QUOTE_ONLY:
            continue
        pat = _pattern_cache.get(sym)
        if pat is None:
            pat = _pattern_cache[sym] = _symbol_pattern(sym)
        if pat.search(text):
            found.add(sym)
            if len(found) > 1:
                return None   # 모호 — 조기 탈출
    return next(iter(found)) if len(found) == 1 else None
