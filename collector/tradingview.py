"""
TradingView 심볼별 아이디어(차티스트 글) 수집기 — 2026-07-22 정찰 실측 기반 재작성.

근거 3종:
  (1) 라이브 페이지 실측: 아이디어는 순수 SSR 임베드. HTML 의
      <script type="application/prs.init-data+json"> 안에 목록 전체 JSON 이 있고,
      최상위 키는 로드마다 랜덤이므로 '구조'({"ideas":{"data":{"items":[...]}}})로 찾아야 함.
      목록의 description 은 상세 페이지와 바이트 단위 일치하는 '전문'(절단 0건 실측)
      → 본문 목적의 상세 방문은 원칙적으로 불필요.
  (2) izrua_watcher 검증 소스(수개월 무차단 운영): curl_cffi chrome 지문 위장 +
      requests 폴백, recent 정렬만 사용(popular 는 403 실증), USDT 우선/USD 폴백,
      chart URL 경계(boundary) 파싱(DOM 클래스 변화 면역), 상세 페이지 본문 3단 폴백,
      팔로워는 /u/{username}/ 프로필 정규식.
  (3) OSS(mnwato/tradingview-scraper 0.4.20): ?component-data-only=1 파라미터로
      같은 데이터를 순수 JSON 으로 수신(data.ideas.data.items[]) — 2026-07-22 실측
      재검증 완료(쿠키 불필요, 24건/페이지, description 전문 포함).

수집 경로 (3단 폴백, 신뢰 순):
  1) component-data-only=1 JSON        ← 표준 경로 (파싱 유지보수 최소)
  2) HTML init-data 임베드 JSON        ← 파라미터 무력화 대비
  3) chart URL boundary HTML 파싱      ← JSON 표면 전멸 시 최후 수단
     (3 경로 아이템은 시간 정보가 없어 상세 페이지 1회 방문으로 보강)

차단 대응: 403/429/캡차/1020 즉시 포기(재시도 무의미), 그 외 오류만 지수 백오프.
차단 감지 시 모듈 전역 쿨다운(30분)이 걸려 쿨다운 동안 어떤 HTTP 요청도 나가지 않고,
호출부는 is_blocked() 로 차단 상태를 확인해 남은 심볼 루프를 즉시 중단할 수 있다.
env TV_COOKIE 가 있으면 Cookie 헤더로 부착(캡차 우회용, 선택).
요청 간 페이싱은 호출부 책임 — 모듈 내 sleep 은 '상세 페이지 방문 간 1초'뿐.

계약:
  fetch_ideas(symbol, timeout, max_age_hours=None, max_detail_fetch=5) -> list[dict]
  반환 dict 필수 키: symbol, title, description, author, author_followers, url,
                    published_at(epoch float|None), age_minutes(float|None)
  추가 키: direction("long"/"short"/None), likes_count, comments_count, ticker
  실패/차단 시 예외를 위로 던지지 않고 [] + logger.warning.
  is_blocked() -> bool: 차단 쿨다운 중 여부 — True 면 호출부는 남은 심볼 루프 중단.
  reset_detail_budget(): 주기 전역 상세 방문 예산(총 20회) 리셋 — 주기 시작 시 호출.
  import 시 네트워크 없음. print 금지(logging 만 — Windows cp949 콘솔 대비).
  author_followers 는 목록/상세 어디에도 없어(실측) 기본 None —
  필요 시 호출부가 fetch_author_followers() 로 별도 조회(+캐시 권장).
"""

import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import List, Optional, Tuple

# curl_cffi 가 있으면 Chrome TLS 지문까지 위장 (izrua_watcher 무차단 운영의 1등 공신).
# 미설치 환경에서는 requests + 수동 브라우저 헤더로 강등.
try:
    from curl_cffi import requests as _http
    _IMPERSONATE = "chrome120"
except ImportError:  # pragma: no cover
    import requests as _http
    _IMPERSONATE = None

logger = logging.getLogger("alert.tradingview")

_BASE = "https://www.tradingview.com"
# 정찰(1)에서 4회 연속 통과가 실측된 평범한 Chrome UA (requests 폴백 시에만 사용 —
# curl_cffi 는 impersonate 가 지문과 일치하는 UA 를 스스로 설정하므로 건드리지 않는다).
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
_DETAIL_SLEEP_SEC = 1.0          # 계약: 모듈 내 sleep 은 상세 방문 간 1초만
_MAX_JSON_DEPTH = 8              # 구조 탐색 재귀 상한 (방어)
_QUOTE_SUFFIXES = ("USDT", "USDC", "BUSD", "USD", "KRW", "EUR", "PERP")

_session = None  # 지연 생성 + 재사용 (정찰(1): 세션 재사용 권장)

# 전역 서킷브레이커: 차단(403/429/캡차/1020) 감지 시 쿨다운 동안 모듈 차원에서
# 모든 HTTP 요청을 생략한다(밴 중 82심볼 루프가 주기당 82요청을 계속 쏘는 것 원천 봉쇄).
# 호출부는 is_blocked() 로 확인해 남은 심볼 루프를 즉시 중단할 수 있다.
_BLOCK_COOLDOWN_SEC = 1800.0  # 30분
_blocked_until = 0.0          # epoch. 이 시각 전까지 _get 은 요청 없이 즉시 blocked 반환

# 주기(호출 세션) 전역 상세 방문 예산: 심볼당 예산(max_detail_fetch)만으로는
# 목록 스키마 변경/강등 시 82심볼 × 5회 = 410회 폭주 가능 → 전체 상한을 별도로 둔다.
# 호출부가 수집 주기 시작 시 reset_detail_budget() 호출로 리셋(미호출 시 프로세스 누적 상한).
_CYCLE_DETAIL_BUDGET = 20
_cycle_detail_used = 0

# 소프트 실패 브레이커(2026-07-22 리뷰 [2]): 5xx/타임아웃 같은 '비차단 실패'가 연속되면
# 서버가 거부 중인데도 82심볼 루프가 최대 984요청을 계속 때리는 증폭이 가능했다.
# _get 이 재시도까지 전부 소진한 '완전 실패'가 연속 _SOFT_FAIL_LIMIT 회에 도달하면
# 짧은 전역 쿨다운을 걸어 이번 주기를 사실상 중단시킨다(성공/404 시 카운터 리셋).
_SOFT_FAIL_LIMIT = 3
_SOFT_FAIL_COOLDOWN_SEC = 600.0  # 10분 (진짜 차단 30분보다 짧게)
_consec_fail = 0

# TV_COOKIE 는 헤더가 아니라 세션 쿠키 jar 로 주입한다(2026-07-22 리뷰 [3]:
# 헤더 방식은 requests 폴백에서 두 번째 요청부터 서버 Set-Cookie 가 채운 jar 가
# Cookie 헤더를 통째로 덮어써 침묵 소실). 마지막으로 주입한 원문을 기억해 중복 주입 방지.
_cookie_applied = None

# 프로필(팔로워) 조회 방어선(2026-07-22 리뷰 [4]): 상세 방문과 동일한 위협 모델
# (호출부가 글마다 호출)에 대해 주기 전역 예산 + 최소 페이싱 + TTL 캐시를 모듈측에 강제.
_CYCLE_PROFILE_BUDGET = 10
_cycle_profile_used = 0
_PROFILE_SLEEP_SEC = 1.0
_last_profile_at = 0.0
_FOLLOWERS_TTL_OK_SEC = 7 * 86400   # 성공값 7일 캐시
_FOLLOWERS_TTL_NONE_SEC = 6 * 3600  # 실패(None)는 6시간만 (영구 미스 방지)
_followers_cache: dict = {}         # username → (cached_at, followers|None)


# ── HTTP 계층 ─────────────────────────────────────────────────

def _new_session():
    if _IMPERSONATE:
        return _http.Session(impersonate=_IMPERSONATE)
    s = _http.Session()
    s.headers.update({
        "User-Agent": _UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Upgrade-Insecure-Requests": "1",
        "Referer": _BASE + "/",
    })
    return s


def _get_session():
    global _session
    if _session is None:
        _session = _new_session()
    return _session


def _ensure_cookie(session) -> dict:
    """TV_COOKIE('k=v; k2=v2')를 세션 쿠키 jar 에 주입하고 {} 를 반환.
    jar 주입이 실패하는 구현이면 헤더 방식으로 폴백해 {'Cookie': ...} 를 반환
    (호출부가 headers 로 전달). 값은 절대 로그에 남기지 않는다. 런타임 중 env 가
    바뀌면 재주입한다(원문 비교)."""
    global _cookie_applied
    raw = os.getenv("TV_COOKIE", "").strip()
    if not raw:
        return {}
    if raw == _cookie_applied:
        return {}
    try:
        for part in raw.split(";"):
            part = part.strip()
            if not part or "=" not in part:
                continue
            name, value = part.split("=", 1)
            session.cookies.set(name.strip(), value.strip(), domain=".tradingview.com", path="/")
        _cookie_applied = raw
        return {}
    except Exception as e:
        logger.warning("[tv] 쿠키 jar 주입 실패 - 헤더 방식 폴백: %s", type(e).__name__)
        return {"Cookie": raw}


def _looks_blocked(status: int, text: str) -> bool:
    """차단/캡차/챌린지 신호 판별. mnwato 는 캡차를 <title>Captcha Challenge</title> 로
    감지, Cloudflare 밴은 'error code: 1020', 구형 챌린지는 503 + 'Just a moment'."""
    if status in (403, 429, 503):
        return True
    head = (text or "")[:3000]
    return (
        "Captcha Challenge" in head
        or "error code: 1020" in head
        or "Just a moment" in head
    )


def _get(url: str, timeout: float, max_retry: int = 3) -> Tuple[Optional[str], bool, bool]:
    """GET → (본문 텍스트|None, 차단 여부, 404 여부).
    - 차단 쿨다운 중: HTTP 요청 없이 즉시 (None, True, False) — 전역 서킷브레이커
    - 403/429/캡차/1020: 즉시 포기 + blocked=True + 쿨다운 설정 (재시도는 밴을 키울 뿐)
    - 404: 페어 없음 → 재시도 없이 (None, False, True)
      ('재시도 소진 실패'(None, False, False)와 구분 — 호출부는 같은 ticker 추가 경로 생략)
    - 그 외 오류/네트워크 예외: 지수 백오프 재시도"""
    global _blocked_until, _consec_fail
    now = time.time()
    if now < _blocked_until:
        logger.warning("[tv] 차단 쿨다운 중(%.0f초 남음) - 요청 생략: %s",
                       _blocked_until - now, url)
        return None, True, False
    delay = 2.0
    for attempt in range(1, max_retry + 1):
        try:
            session = _get_session()
            headers = _ensure_cookie(session)  # 기본 jar 주입, 실패 시에만 헤더 폴백
            resp = session.get(url, headers=headers or None, timeout=timeout)
            status = resp.status_code
            text = resp.text
            if status == 200 and not _looks_blocked(status, text):
                _consec_fail = 0
                return text, False, False
            if _looks_blocked(status, text):
                _blocked_until = time.time() + _BLOCK_COOLDOWN_SEC
                logger.warning("[tv] 차단/캡차 신호(status=%s) - 즉시 포기 + %.0f분 쿨다운: %s",
                               status, _BLOCK_COOLDOWN_SEC / 60.0, url)
                return None, True, False
            if status == 404:
                _consec_fail = 0  # 서버가 정상 응답 중이라는 뜻 — 실패 연속으로 안 침
                logger.info("[tv] 404(페어 없음 추정): %s", url)
                return None, False, True
            logger.warning("[tv] status=%s (재시도 %d/%d): %s", status, attempt, max_retry, url)
        except Exception as e:  # curl_cffi/requests 예외 계열이 달라 광범위 캐치
            logger.warning("[tv] 요청 실패(%d/%d) %s: %s", attempt, max_retry, url, e)
        if attempt < max_retry:
            time.sleep(delay)
            delay *= 2
    # 재시도까지 전부 소진한 '완전 실패' — 연속되면 소프트 브레이커 발동 (리뷰 [2])
    _consec_fail += 1
    if _consec_fail >= _SOFT_FAIL_LIMIT:
        _blocked_until = time.time() + _SOFT_FAIL_COOLDOWN_SEC
        _consec_fail = 0
        logger.warning(
            "[tv] 완전 실패 연속 %d회 - 소프트 브레이커 발동, %.0f분 쿨다운 "
            "(장애/소프트 차단 중 요청 증폭 방지)",
            _SOFT_FAIL_LIMIT, _SOFT_FAIL_COOLDOWN_SEC / 60.0)
        return None, True, False
    return None, False, False


# ── JSON 구조 탐색 (최상위 키 랜덤 대응 — 정찰(1) 핵심) ─────────────

def _find_ideas_payload(node, depth: int = 0) -> Optional[dict]:
    """dict 트리에서 {"ideas": {"data": {"items": [...]}}} 구조를 찾아
    {"items": [...], "next": ..., "total": ...} dict 를 반환.
    component-data-only JSON(root.data.ideas...)과 init-data 블롭(랜덤키.data.ideas...)
    양쪽 모두 이 구조 탐색 하나로 커버된다."""
    if depth > _MAX_JSON_DEPTH or not isinstance(node, dict):
        return None
    ideas = node.get("ideas")
    if isinstance(ideas, dict):
        data = ideas.get("data")
        if isinstance(data, dict) and isinstance(data.get("items"), list):
            return data
    for v in node.values():
        if isinstance(v, dict):
            found = _find_ideas_payload(v, depth + 1)
            if found is not None:
                return found
    return None


def _find_key_dict(node, key: str, depth: int = 0) -> Optional[dict]:
    """dict 트리에서 특정 키의 dict 값을 탐색 (상세 페이지 ssrIdeaData 용)."""
    if depth > _MAX_JSON_DEPTH or not isinstance(node, dict):
        return None
    v = node.get(key)
    if isinstance(v, dict):
        return v
    for child in node.values():
        if isinstance(child, dict):
            found = _find_key_dict(child, key, depth + 1)
            if found is not None:
                return found
    return None


_INIT_DATA_RE = re.compile(
    r'<script[^>]*type="application/prs\.init-data\+json"[^>]*>(.*?)</script>', re.S
)


def _init_data_blobs(html_text: str):
    """페이지 내 init-data script 블롭들을 JSON 파싱해 순회 (페이지당 6~7개 실측)."""
    for raw in _INIT_DATA_RE.findall(html_text or ""):
        try:
            yield json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue


# ── 파싱 유틸 ─────────────────────────────────────────────────

def _iso_to_epoch(s) -> Optional[float]:
    """'2026-07-17T07:11:47+00:00' / '...Z' → epoch float. 실패 시 None."""
    if not isinstance(s, str) or not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None


def _decode_json_string(raw: str) -> str:
    """JSON 문자열 리터럴 내용부(이스케이프 포함)를 실제 텍스트로 디코드."""
    try:
        return json.loads('"' + raw + '"')
    except (json.JSONDecodeError, ValueError):
        # izrua_watcher 방식 수동 디코드 폴백
        s = raw.replace("\\n", "\n").replace("\\t", " ").replace('\\"', '"')
        s = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), s)
        return s.replace("\\\\", "\\")


def _strip_html(text: str) -> str:
    """HTML/스크립트/스타일 제거 후 텍스트만 추출 (izrua_watcher 검증 코드 차용)."""
    text = re.sub(r"<script[^>]*>.*?</script>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    text = text.replace("&quot;", '"').replace("&#39;", "'").replace("&nbsp;", " ")
    text = re.sub(r"&[a-z#0-9]+;", " ", text, flags=re.I)
    return re.sub(r"\s+", " ", text).strip()


_DIRECTION_MAP = {1: "long", 2: "short"}  # 정찰(1) 실측: 0=중립/교육, 1=Long, 2=Short


def _as_str(v) -> str:
    """스키마 드리프트 방어(리뷰 [1]): truthy 비문자열(i18n dict 등)이 str 연산을 만나
    예외로 번지거나 계약(str) 위반 값이 하류로 새는 것을 막는다."""
    return v if isinstance(v, str) else ""


def _is_crypto_symbol(sym: dict) -> bool:
    """이 아이디어가 '암호화폐 자산' 글인지 판별(리뷰 [5] — 실측 필드 기반).
    근거: 목록 item.symbol 에 base_currency_logo_id='crypto/XTVCBTC', type='spot',
    exchange='BINANCE' 실측(2026-07-22, cache/recon_btc.html). S&P500 CFD 같은
    비크립토 심볼은 crypto/ 로고가 없다."""
    if not isinstance(sym, dict):
        return False
    if _as_str(sym.get("base_currency_logo_id")).startswith("crypto/"):
        return True
    logo = sym.get("logo")
    if isinstance(logo, dict) and _as_str(logo.get("logoid")).startswith("crypto/"):
        return True
    return sym.get("type") in ("spot", "crypto")


def _item_to_idea(item: dict, symbol: str, now: float,
                  require_crypto: bool = False) -> Optional[dict]:
    """목록 JSON item(정찰(1) 스키마) → 계약 dict.
    require_crypto=True(USD 폴백 ticker 경로)면 암호화폐 자산으로 검증되지 않는
    아이템을 버린다 — 'SPXUSD'가 S&P500 지수로 해석되어 오염된 가격 레벨이
    수집되는 사고 방지(리뷰 [5])."""
    if not isinstance(item, dict):
        return None
    title = _as_str(item.get("name")).strip()
    desc = _as_str(item.get("description"))
    user = item.get("user") if isinstance(item.get("user"), dict) else {}
    sym = item.get("symbol") if isinstance(item.get("symbol"), dict) else {}
    if require_crypto and not _is_crypto_symbol(sym):
        logger.warning("[tv] %s: 비크립토 자산 아이템 제외(ticker=%s)",
                       symbol, _as_str(sym.get("short_name")) or "?")
        return None
    url = _as_str(item.get("chart_url"))
    if url.startswith("/"):
        url = _BASE + url
    ts = item.get("date_timestamp")
    published_at = float(ts) if isinstance(ts, (int, float)) and ts > 0 else None
    if published_at is None:
        published_at = _iso_to_epoch(item.get("created_at"))
    if not (title or desc) or not url:
        return None
    author = user.get("username")
    ticker = sym.get("short_name")
    return {
        "symbol": symbol,
        "title": title,
        "description": desc,
        "author": author if isinstance(author, str) else None,
        "author_followers": None,  # 목록/상세 JSON 에 없음(실측) — 프로필 별도 조회 사항
        "url": url,
        "published_at": published_at,
        "age_minutes": (now - published_at) / 60.0 if published_at else None,
        # 추가 정보 (후단 등급/필터용)
        "direction": _DIRECTION_MAP.get(sym.get("direction")),
        "likes_count": item.get("likes_count"),
        "comments_count": item.get("comments_count"),
        "ticker": ticker if isinstance(ticker, str) else None,
    }


def _items_to_ideas(items: list, symbol: str, now: float,
                    require_crypto: bool = False) -> List[dict]:
    """아이템별 예외 격리(리뷰 [1]): 불량 아이템 1건이 심볼 전체(나머지 23건)와
    폴백 경로까지 무효화하던 문제를 막는다 — 실패 건만 스킵하고 계속."""
    ideas = []
    for it in items:
        try:
            idea = _item_to_idea(it, symbol, now, require_crypto)
            if idea:
                ideas.append(idea)
        except Exception as e:
            logger.warning("[tv] %s: 아이템 1건 파싱 실패 - 스킵: %s", symbol, e)
    return ideas


# ── 경로 3: chart URL boundary 파싱 (최후 수단, izrua_watcher 검증 코드) ──

_CHART_URL_RE = re.compile(
    r"https://www\.tradingview\.com/chart/([A-Z0-9.]+)/([a-zA-Z0-9_]+)-([a-zA-Z0-9_-]+)/"
)
_AUTHOR_IN_SECTION_RE = re.compile(r"/u/([a-zA-Z0-9_.\-]{2,50})/")


def _parse_chart_boundary(html_text: str, symbol: str, now: float) -> List[dict]:
    """각 unique chart URL = 게시물 시작점, 다음 unique chart URL 전까지 = 그 게시물 영역.
    DOM 클래스명 변경에 면역. 시간 정보가 없으므로 published_at=None + _partial 마킹
    → 상세 페이지 보강 대상이 된다."""
    seen, positions = set(), []
    for m in _CHART_URL_RE.finditer(html_text):
        url = m.group(0)
        if url not in seen:
            seen.add(url)
            positions.append((url, m.start()))
    ideas = []
    for i, (url, start) in enumerate(positions):
        end = positions[i + 1][1] if i + 1 < len(positions) else min(start + 10000, len(html_text))
        section = html_text[start:end]
        first_close = section.find(">")  # <a href="URL" ...> 속성 영역 스킵
        if 0 < first_close < 1000:
            section = section[first_close + 1:]
        text = _strip_html(section)
        if len(text) < 100:  # 노이즈 컷
            continue
        head = text[:120]
        title = head
        for sep in (". ", "! ", "? "):
            idx = head.find(sep)
            if idx > 0:
                title = head[: idx + 1].strip()
                break
        author_m = _AUTHOR_IN_SECTION_RE.search(section)
        direction = None
        if re.search(r">\s*Long\s*<", section):
            direction = "long"
        elif re.search(r">\s*Short\s*<", section):
            direction = "short"
        ideas.append({
            "symbol": symbol,
            "title": title,
            "description": text,
            "author": author_m.group(1) if author_m else None,
            "author_followers": None,
            "url": url,
            "published_at": None,
            "age_minutes": None,
            "direction": direction,
            "likes_count": None,
            "comments_count": None,
            "ticker": None,
            "_partial": True,  # 시간/본문 신뢰도 낮음 → 상세 보강 대상
        })
    return ideas


# ── 목록 수집 ─────────────────────────────────────────────────

def _candidate_tickers(symbol: str) -> List[str]:
    """'BTC' → ['BTCUSDT', 'BTCUSD'] (izrua_watcher: USDT 우선, USD 는 0건일 때만).
    'BTCUSD'/'BTCUSDT' 처럼 이미 페어면 그대로. 'KRW-BTC' 업비트 표기도 수용."""
    s = (symbol or "").strip().upper()
    if "-" in s:  # 업비트 'KRW-BTC' 형
        s = s.split("-")[-1]
    for q in _QUOTE_SUFFIXES:
        if s.endswith(q) and len(s) > len(q):
            return [s]
    return [s + "USDT", s + "USD"]


def _collect_for_ticker(ticker: str, symbol: str, timeout: float,
                        require_crypto: bool = False) -> Tuple[List[dict], bool]:
    """한 ticker 의 최신 아이디어 목록. 반환 (ideas, blocked). 3단 폴백.
    require_crypto: USD 폴백 후보처럼 자산 오인 위험이 있는 경로에서 True —
    JSON 아이템은 심볼 메타로 검증하고, 검증 불가능한 boundary 경로는 생략한다."""
    now = time.time()

    # 경로 1: component-data-only JSON (recent 정렬 고정 — popular 는 403 실증)
    json_url = f"{_BASE}/symbols/{ticker}/ideas/?sort=recent&component-data-only=1"
    text, blocked, not_found = _get(json_url, timeout)
    if blocked:
        return [], True
    if not_found:
        # 심볼 라우트 자체가 404 → 같은 ticker 의 HTML 경로도 404 확정.
        # 무의미한 요청(주기마다 반복되는 404 히트 = 크롤러 시그니처) 방지, 즉시 다음 후보로.
        return [], False
    path1_dead = text is None  # 재시도 소진 완전 실패 (차단/404 아님)
    if text:
        try:
            payload = _find_ideas_payload(json.loads(text))
        except (json.JSONDecodeError, ValueError):
            payload = None  # JSON 아님 → 파라미터 무력화/변조 의심 → HTML 폴백
        if payload is not None:
            ideas = _items_to_ideas(payload["items"], symbol, now, require_crypto)
            logger.info("[tv] %s: JSON 경로 %d건", ticker, len(ideas))
            return ideas, False
        logger.warning("[tv] %s: component-data-only 응답 구조 인식 실패 - HTML 폴백", ticker)

    # 경로 2: HTML init-data 임베드. 경로 1이 '완전 실패'였다면 서버 장애 가능성이
    # 높으므로 재시도 1회로 줄인다(리뷰 [2]: 실패 시 요청 증폭 방지).
    html_url = f"{_BASE}/symbols/{ticker}/ideas/?sort=recent"
    html_text, blocked, _ = _get(html_url, timeout, max_retry=1 if path1_dead else 3)
    if blocked or not html_text:
        return [], blocked
    for blob in _init_data_blobs(html_text):
        payload = _find_ideas_payload(blob)
        if payload is not None:
            ideas = _items_to_ideas(payload["items"], symbol, now, require_crypto)
            logger.info("[tv] %s: init-data 경로 %d건", ticker, len(ideas))
            return ideas, False

    # 경로 3: boundary 파싱 (최후 수단). 심볼 메타가 없어 자산 검증이 불가능하므로
    # require_crypto(USD 폴백) 경로에서는 쓰지 않는다(리뷰 [5]).
    if require_crypto:
        logger.warning("[tv] %s: boundary 파싱은 자산 검증 불가 - USD 폴백에서는 생략", ticker)
        return [], False
    ideas = _parse_chart_boundary(html_text, symbol, now)
    logger.warning("[tv] %s: init-data 실패 - boundary 파싱으로 %d건", ticker, len(ideas))
    return ideas, False


# ── 상세 페이지 보강 ───────────────────────────────────────────

def _fetch_detail(url: str, timeout: float) -> Tuple[Optional[dict], bool]:
    """상세 페이지에서 본문/시각/방향 보강. 반환 (부분 dict|None, blocked).
    폴백 사슬: ssrIdeaData 블롭 → JSON-LD articleBody → og:description.
    시각: ssrIdeaData → "created_at" 키 정규식 → JSON-LD datePublished."""
    html_text, blocked, _ = _get(url, timeout, max_retry=2)
    if blocked:
        return None, True
    if not html_text:
        return None, False
    out = {}

    for blob in _init_data_blobs(html_text):
        node = _find_key_dict(blob, "ssrIdeaData")
        if node:
            out["description"] = _as_str(node.get("description")) or None
            out["title"] = _as_str(node.get("name")).strip() or None
            user = node.get("user") if isinstance(node.get("user"), dict) else {}
            author = user.get("username")
            out["author"] = author if isinstance(author, str) else None
            ts = node.get("date_timestamp")
            out["published_at"] = (
                float(ts) if isinstance(ts, (int, float)) and ts > 0
                else _iso_to_epoch(node.get("created_at"))
            )
            out["direction"] = _DIRECTION_MAP.get(node.get("direction"))  # 상세는 최상위
            break

    if not out.get("description"):
        m = re.search(r'"articleBody"\s*:\s*"((?:[^"\\]|\\.)*)"', html_text)
        if m:
            body = _decode_json_string(m.group(1)).strip()
            if len(body) >= 200:  # izrua_watcher 기준: 200자 미만이면 다음 폴백
                out["description"] = body
    if not out.get("description"):
        m = re.search(
            r'<meta[^>]+property="og:description"[^>]+content="([^"]*)"', html_text
        )
        if m:
            out["description"] = _strip_html(m.group(1)) or None

    if not out.get("published_at"):
        m = re.search(r'"created_at"\s*:\s*"([^"]+)"', html_text)
        out["published_at"] = _iso_to_epoch(m.group(1)) if m else None
    if not out.get("published_at"):
        m = re.search(r'"datePublished"\s*:\s*"([^"]+)"', html_text)
        out["published_at"] = _iso_to_epoch(m.group(1)) if m else None

    return (out if any(out.values()) else None), False


def _needs_detail(idea: dict) -> bool:
    """상세 방문 가치 판단. JSON 목록의 description 은 이미 '전문'(실측)이므로
    정상 경로에서는 False — boundary 경로/본문 없음/시각 없음일 때만 True."""
    if not idea.get("url"):
        return False
    return bool(
        idea.get("_partial")
        or not idea.get("description")
        or idea.get("published_at") is None
    )


# ── 공개 API ─────────────────────────────────────────────────

def is_blocked() -> bool:
    """차단 쿨다운 중이면 True. 호출부(오케스트레이터)는 fetch_ideas 호출 후 이 값이
    True 면 남은 심볼 루프를 즉시 중단할 것 — 밴 벽에 주기당 82요청을 때리는 것 방지."""
    return time.time() < _blocked_until


def reset_detail_budget() -> None:
    """주기 전역 예산(상세 방문 + 프로필 조회) 리셋 — 수집 주기 시작 시 호출.
    (이름은 호환성 유지 — 프로필 예산도 함께 리셋한다, 리뷰 [4])"""
    global _cycle_detail_used, _cycle_profile_used
    _cycle_detail_used = 0
    _cycle_profile_used = 0


def fetch_ideas(symbol: str, timeout: float, max_age_hours: Optional[float] = None,
                max_detail_fetch: int = 5) -> List[dict]:
    """한 심볼의 최신 아이디어 목록. 실패/차단 시 [] (예외를 위로 던지지 않음).

    - symbol: 'BTC'(→ USDT 우선/USD 폴백) 또는 'BTCUSD' 같은 완성 ticker.
    - max_age_hours: 지정 시 그보다 오래된 글 제외. 시각을 못 뽑은 글은 관대하게
      통과시킨다(수집 단계 손실 최소화 — 후단에서 검증하는 철학).
    - max_detail_fetch: 본문/시각이 부족한 글에 한해 상세 페이지를 방문하는 심볼당 상한.
      JSON 경로가 살아있는 한 0회가 정상이다(목록 description == 상세 전문 실측).
      별도로 주기 전역 상한(_CYCLE_DETAIL_BUDGET=20, reset_detail_budget() 로 리셋)이
      함께 적용되어 목록 강등 시 82심볼 × 5회 = 410회 폭주를 막는다.
    """
    try:
        return _fetch_ideas_impl(symbol, timeout, max_age_hours, max_detail_fetch)
    except Exception as e:
        logger.warning("[tv] fetch_ideas(%s) 예상외 오류 - 빈 결과 반환: %s", symbol, e)
        return []


def _fetch_ideas_impl(symbol: str, timeout: float, max_age_hours: Optional[float],
                      max_detail_fetch: int) -> List[dict]:
    global _cycle_detail_used
    ideas: List[dict] = []
    for idx, ticker in enumerate(_candidate_tickers(symbol)):
        # 첫 후보(USDT)는 크립토 확정이지만, 폴백 후보(USD)는 전혀 다른 자산
        # (예: SPXUSD=S&P500 CFD)으로 해석될 수 있어 자산 검증을 강제한다(리뷰 [5]).
        ideas, blocked = _collect_for_ticker(ticker, symbol, timeout, require_crypto=idx > 0)
        if blocked:
            return []  # 차단 신호면 폴백 ticker 요청도 금지 (밴 확산 방지)
        if ideas:
            break  # USDT 에서 나오면 USD 는 안 감 (요청 절약)

    if not ideas:
        return []

    # url 기준 중복 제거 (경로 폴백/재수집 대비)
    seen, unique = set(), []
    for i in ideas:
        if i["url"] not in seen:
            seen.add(i["url"])
            unique.append(i)
    ideas = unique

    # 상세 보강 (심볼당 + 주기 전역 이중 예산, 방문 간 1초 — 그 외 페이싱은 호출부 몫)
    budget = max(0, int(max_detail_fetch or 0))
    visited_any = False
    for idea in ideas:
        if budget <= 0:
            break
        if not _needs_detail(idea):
            continue
        if _cycle_detail_used >= _CYCLE_DETAIL_BUDGET:
            logger.warning(
                "[tv] 주기 전역 상세 예산(%d회) 소진 - %s 상세 보강 생략 "
                "(목록 강등 폭주 방어: reset_detail_budget() 전까지 상세 방문 중단)",
                _CYCLE_DETAIL_BUDGET, symbol)
            break
        if _cycle_detail_used == 0:
            # 정상 JSON 경로에서는 상세 방문 0회가 기본 — 0→양수 전환은 목록 스키마
            # 변경/boundary 강등 신호일 수 있으므로 집계 경보를 남긴다.
            logger.warning("[tv] 강등 감지 가능: 이번 주기 첫 상세 방문 발생 - symbol=%s", symbol)
        if visited_any:
            time.sleep(_DETAIL_SLEEP_SEC)
        visited_any = True
        budget -= 1
        _cycle_detail_used += 1
        try:
            detail, blocked = _fetch_detail(idea["url"], timeout)
        except Exception as e:
            # 보강은 '선택적' 단계 — 실패가 수집 완료된 목록을 폐기하면 안 됨(리뷰 [1])
            logger.warning("[tv] 상세 보강 실패 - 해당 글만 생략: %s", e)
            continue
        if blocked:
            logger.warning("[tv] 상세 페이지 차단 신호 - 이번 주기 상세 보강 중단")
            break
        if not detail:
            continue
        d_desc = detail.get("description")
        if d_desc and len(d_desc) > len(idea.get("description") or ""):
            idea["description"] = d_desc
        if idea.get("published_at") is None and detail.get("published_at"):
            idea["published_at"] = detail["published_at"]
        for k in ("author", "title"):
            if not idea.get(k) and detail.get(k):
                idea[k] = detail[k]
        if idea.get("direction") is None and detail.get("direction"):
            idea["direction"] = detail["direction"]
        idea.pop("_partial", None)

    # 시각 재계산 + 내부 마킹 제거
    now = time.time()
    for idea in ideas:
        idea.pop("_partial", None)
        pa = idea.get("published_at")
        idea["age_minutes"] = (now - pa) / 60.0 if pa else None

    # 연령 필터 (시각 미상 글은 관대하게 통과)
    if max_age_hours is not None:
        cutoff_min = max_age_hours * 60.0
        ideas = [i for i in ideas if i["age_minutes"] is None or i["age_minutes"] <= cutoff_min]

    # recent 정렬도 엄밀한 시간 역순이 아님(실측 24건 중 3건 이탈) → 자체 정렬
    ideas.sort(key=lambda i: i["published_at"] or 0.0, reverse=True)
    return ideas


_FOLLOWERS_PATTERNS = (  # izrua_watcher 실전 검증 정규식
    r"Followers[^0-9]{1,200}?([\d,.]+)\s*([KMkm])\b",
    r">\s*Followers\s*</[^>]+>\s*<[^>]+>\s*([\d,.]+)\s*([KMkm]?)",
    r">\s*([\d,.]+)\s*([KMkm]?)\s*</[^>]+>\s*<[^>]+>\s*Followers",
)


def fetch_author_followers(username: str, timeout: float) -> Optional[int]:
    """작성자 프로필 페이지에서 팔로워 수 조회 (목록/상세 JSON 에는 없음 — 실측).
    모듈측 방어선(리뷰 [4] — 상세 방문 예산과 동일한 위협 모델 적용):
    - TTL 캐시: 성공 7일 / 실패 6시간 (호출부 캐시 부재 시에도 폭주 불가)
    - 주기 전역 예산 _CYCLE_PROFILE_BUDGET(10회, reset_detail_budget() 로 리셋)
    - 호출 간 최소 1초 페이싱
    실패/예산 소진 시 None."""
    global _cycle_profile_used, _last_profile_at
    if not username:
        return None
    cached = _followers_cache.get(username)
    if cached is not None:
        cached_at, value = cached
        ttl = _FOLLOWERS_TTL_OK_SEC if value is not None else _FOLLOWERS_TTL_NONE_SEC
        if time.time() - cached_at <= ttl:
            return value
    if _cycle_profile_used >= _CYCLE_PROFILE_BUDGET:
        logger.warning("[tv] 주기 전역 프로필 예산(%d회) 소진 - %s 조회 생략",
                       _CYCLE_PROFILE_BUDGET, username)
        return None
    try:
        wait = _PROFILE_SLEEP_SEC - (time.time() - _last_profile_at)
        if wait > 0:
            time.sleep(wait)
        _cycle_profile_used += 1
        _last_profile_at = time.time()
        html_text, blocked, _ = _get(f"{_BASE}/u/{username}/", timeout, max_retry=1)
        if blocked or not html_text:
            _followers_cache[username] = (time.time(), None)
            return None
        for pat in _FOLLOWERS_PATTERNS:
            m = re.search(pat, html_text)
            if not m:
                continue
            try:
                num = float(m.group(1).replace(",", ""))
            except ValueError:
                continue
            unit = (m.group(2) or "").upper()
            if unit == "K":
                num *= 1_000
            elif unit == "M":
                num *= 1_000_000
            _followers_cache[username] = (time.time(), int(num))
            return int(num)
        _followers_cache[username] = (time.time(), None)
        return None
    except Exception as e:
        logger.warning("[tv] 팔로워 조회 실패(%s): %s", username, e)
        _followers_cache[username] = (time.time(), None)
        return None
