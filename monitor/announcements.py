"""
업비트 거래소 리스크 공지 즉시경보 (2026-07-26 기획 카드 #5).

유의 종목 지정 / 거래지원 종료(상장폐지) 공지를 감지해, 추적 중인 코인이면
① 경보 1회 발송 ② 그 코인의 '알림 대기' 레벨을 즉시 만료한다. 상장폐지를 앞둔
코인의 엔트리 터치 알림이 아무 일 없다는 듯 계속 나가는 상황을 막는 것이 목적.

엔드포인트 (2026-07-26 실측 — 무인증·무쿠키 200 확인):
  GET https://api-manager.upbit.com/api/v1/announcements
      ?os=web&page=1&per_page=N&category=all
  → {"success": true,
     "data": {"total_pages": 1134, "total_count": 5667,
              "notices": [{"id": 6403, "uuid": "…", "category": "거래",
                           "title": "질리카(ZIL) 거래 유의 종목 지정 안내",
                           "listed_at": "2026-07-26T11:48:18+09:00",
                           "first_listed_at": "…"}, …]}}
  (참고: /api/v1/notices 는 404 — announcements 가 현행 경로다)

**비문서화 API** 라 언제 형태가 바뀌어도 이상하지 않다. 그래서
- 파싱은 전부 방어적으로(키가 없으면 그 건만 건너뜀, 리스트 위치도 여러 후보를 탐색)
- 네트워크/스키마 실패는 조용히 스킵 — 2분 주기 회차 생존이 이 기능보다 우선이다.

만료를 발송보다 먼저 하되, 발송 실패분은 meta 대기 큐에 남겨 회차마다 재시도한다.
만료된 레벨은 _active_symbols() 에서 빠져 공지 재매칭이 불가능하므로, 이 큐가
텔레그램 장애 시의 유일한 복구 경로다(2026-07-26 리뷰 지적).

호출 비용: 2분마다 부르지 않는다. meta TTL(announcement_poll_interval_minutes,
기본 20분)로 회차당 최대 1콜(하루 ~72콜) — market_sentiment.py 의 1시간 meta
캐시와 같은 패턴이다. 추적 중인 코인이 하나도 없으면 호출조차 하지 않는다.
"""

import html
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

from notify import telegram
from storage import db

logger = logging.getLogger("alert.announcements")

_URL = "https://api-manager.upbit.com/api/v1/announcements"
_NOTICE_WEB_URL = "https://upbit.com/service_center/notice?id={id}"
_SEP = "━━━━━━━━━━━━━━━━━━━━"   # notify/telegram.py 와 같은 구분선(개발자 B 작업 중이라
                                 # 그쪽 상수를 import 하지 않고 값만 맞춘다)
_KST = timezone(timedelta(hours=9))

_POLL_META = "announcement_last_poll_at"
_SENT_META = "announcement_notified_keys"
_SENT_KEEP = 300     # 발송 이력 meta 상한 — 안 자르면 meta 행이 무한 증가한다
_PENDING_META = "announcement_pending_alerts"
_PENDING_KEEP = 20   # 미발송 대기 큐 상한(정상 운영에선 0건, 텔레그램 장애 때만 쌓인다)


# ── 응답 파싱 (전부 방어적) ──────────────────────────────────────────

def _extract_notices(payload) -> list:
    """비문서화 응답에서 공지 배열만 뽑아낸다. 못 찾으면 빈 리스트.

    최상위가 배열인 형태, data 가 바로 배열인 형태, data 안에 notices/list/items
    로 들어있는 형태를 모두 받아준다 — 스키마가 바뀌어도 조용히 0건이 되게(예외로
    회차를 죽이지 않게) 하려는 것."""
    if isinstance(payload, list):
        return [n for n in payload if isinstance(n, dict)]
    if not isinstance(payload, dict):
        return []
    data = payload.get("data", payload)
    if isinstance(data, list):
        return [n for n in data if isinstance(n, dict)]
    if isinstance(data, dict):
        for key in ("notices", "list", "items", "announcements", "results"):
            v = data.get(key)
            if isinstance(v, list):
                return [n for n in v if isinstance(n, dict)]
    return []


def _notice_id(notice: dict):
    """공지 식별자 — id 우선, 없으면 uuid. 둘 다 없으면 None(그 건은 버린다)."""
    for key in ("id", "uuid", "notice_id"):
        v = notice.get(key)
        if v not in (None, ""):
            return v
    return None


def _notice_title(notice: dict) -> str:
    for key in ("title", "subject", "name"):
        v = notice.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _notice_ts(notice: dict) -> Optional[float]:
    """공지 게시 시각(epoch). 날짜를 못 읽으면 None.

    None 은 호출부에서 '신선도 미상 → 제외'로 다룬다. 스키마가 바뀌어 날짜가
    사라졌을 때 수천 건의 과거 공지가 한꺼번에 매칭돼 경보 폭탄이 터지는 것보다,
    기능이 조용히 멈추는 쪽이 안전하기 때문."""
    for key in ("first_listed_at", "listed_at", "created_at", "updated_at"):
        raw = notice.get(key)
        if not isinstance(raw, str) or not raw.strip():
            continue
        try:
            dt = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_KST)   # 업비트 공지는 KST 표기
        return dt.timestamp()
    return None


# 제목 속 심볼 표기: "질리카(ZIL) 거래 유의 종목 지정 안내" 처럼 괄호 안 대문자 티커.
# 날짜/한글 괄호("(8/18 15:00)", "(완료)", "(이벤트 종료)")는 문자 구성상 자연히 걸러진다.
_SYMBOL_RE = re.compile(r"\(([A-Z0-9][A-Z0-9,\s]{0,60})\)")


def extract_symbols(title: str) -> list:
    """제목에서 코인 심볼 후보를 뽑는다(중복 제거, 등장 순서 유지).

    제목 전체를 부분문자열로 훑지 않고 '괄호 안 티커'만 보는 이유: 부분문자열
    매칭이면 ETH 가 'ETHFI' 공지에, BTC 가 문장 아무 데나 걸려 오탐이 난다."""
    out = []
    for group in _SYMBOL_RE.findall(title or ""):
        for token in re.split(r"[,\s]+", group):
            token = token.strip()
            # 숫자만 있는 토큰(연도·수량)은 심볼이 아니다. 1INCH 처럼 숫자로 시작하는
            # 진짜 심볼은 영문자가 섞여 있어 통과한다.
            if 2 <= len(token) <= 15 and re.search(r"[A-Z]", token) and token not in out:
                out.append(token)
    return out


def is_risk_title(title: str, keywords, exclude_keywords) -> bool:
    """리스크 공지인가 — 키워드 포함 AND 제외어 미포함.

    제외어가 반드시 필요한 이유(실측): "타이코(TAIKO) 거래 유의 종목 지정 **해제**
    안내" 처럼 지정이 풀렸다는 좋은 소식도 "유의 종목" 을 포함한다. 이걸 거르지
    않으면 위험이 해소된 코인의 레벨을 도리어 만료시킨다."""
    if not title:
        return False
    if not any(k in title for k in keywords):
        return False
    return not any(x in title for x in (exclude_keywords or ()))


def match_notices(notices: list, active_symbols, keywords, exclude_keywords,
                  max_age_sec: Optional[float] = None,
                  now: Optional[float] = None) -> list:
    """리스크 공지 × 추적 중 심볼 교집합. 반환 [(notice, [심볼…]), …].

    max_age_sec 를 주면 그보다 오래된 공지는 제외한다 — 기능을 처음 켜는 회차에
    수천 건의 과거 공지가 한꺼번에 매칭돼 경보가 쏟아지는 것을 막는 안전장치."""
    active = {s.upper() for s in active_symbols if s}
    if not active:
        return []
    out = []
    for notice in notices:
        title = _notice_title(notice)
        if not is_risk_title(title, keywords, exclude_keywords):
            continue
        if _notice_id(notice) is None:
            continue      # 중복방지 키를 만들 수 없는 건은 다루지 않는다
        if max_age_sec is not None:
            ts = _notice_ts(notice)
            if ts is None or (now or 0) - ts > max_age_sec:
                continue
        hits = [s for s in extract_symbols(title) if s in active]
        if hits:
            out.append((notice, hits))
    return out


def _fetch(timeout: float, per_page: int) -> list:
    """공지 목록 1페이지. 실패·이상 응답은 조용히 빈 리스트(회차 생존 최우선)."""
    try:
        resp = requests.get(
            _URL,
            params={"os": "web", "page": 1, "per_page": per_page, "category": "all"},
            timeout=timeout,
        )
        resp.raise_for_status()
        return _extract_notices(resp.json())
    except Exception as e:  # noqa: BLE001 - 비문서화 API, 어떤 실패도 회차를 막으면 안 됨
        logger.warning("[공지] 조회 실패 - 이번 주기 건너뜀: %s", e)
        return []


# ── 발송 중복 방지 (meta) ────────────────────────────────────────────
# 키는 "공지ID:심볼" — 같은 공지라도 나중에 새로 수집된 다른 코인이 걸리면 그 코인엔
# 알려야 하고, 이미 알린 (공지,코인) 조합은 다시 알리지 않는다.

def _load_sent_keys(conn) -> list:
    try:
        raw = db.get_meta(conn, _SENT_META)
        if raw:
            keys = json.loads(raw)
            if isinstance(keys, list):
                return [str(k) for k in keys]
    except Exception:  # noqa: BLE001 - 이력이 깨졌으면 빈 이력으로 재시작
        logger.warning("[공지] 발송 이력 파싱 실패 - 초기화")
    return []


def _save_sent_keys(conn, keys: list) -> None:
    db.set_meta(conn, _SENT_META, json.dumps(keys[-_SENT_KEEP:], ensure_ascii=False))


# ── 미발송 경보 대기 큐 (meta) ──────────────────────────────────────
# 왜 필요한가(2026-07-26 리뷰 지적): 만료를 발송보다 먼저 하는 설계라, 텔레그램이
# 일시 장애면 레벨은 이미 expired 로 바뀌어 _active_symbols() 에서 빠진다. 그러면
# 다음 폴링에서 그 코인이 매칭조차 되지 않아 경보가 영구 유실된다. 그래서 발송에
# 실패한 (공지, 코인) 조합을 여기에 적어두고, 이후 회차에서 재시도한다.
# 재시도는 네트워크 조회(TTL 게이트)와 무관하게 매 회차 수행한다 — 텔레그램이
# 살아나는 즉시 나가야 하고, 공지 API 를 더 부르는 것도 아니기 때문.

def _load_pending(conn) -> list:
    try:
        raw = db.get_meta(conn, _PENDING_META)
        if raw:
            items = json.loads(raw)
            if isinstance(items, list):
                return [i for i in items if isinstance(i, dict) and i.get("symbols")]
    except Exception:  # noqa: BLE001 - 큐가 깨졌으면 빈 큐로 재시작(회차 생존 우선)
        logger.warning("[공지] 미발송 대기 큐 파싱 실패 - 초기화")
    return []


def _save_pending(conn, items: list) -> None:
    db.set_meta(conn, _PENDING_META,
                json.dumps(items[-_PENDING_KEEP:], ensure_ascii=False))


def render_risk_alert(title: str, symbols: list, expired: int,
                      notice_id=None) -> str:
    """리스크 공지 경보 렌더링 (render_tv_block_alert 와 같은 계열의 시스템 경보).

    기존 엔트리 알림 양식(render_alert)은 건드리지 않는 별도 렌더러이고, 이 함수는
    순수 조립만 한다 — 발송 중복 방지는 호출부의 meta 이력이 담당."""
    lines = [
        _SEP,
        "⚠️ <b>[업비트 리스크 공지]</b>",
        f"대상: <b>{html.escape(' · '.join(symbols))}</b>",
        html.escape(title),
    ]
    if expired:
        lines.append(f"→ 추적 중이던 레벨 {expired}건을 즉시 만료 처리했습니다.")
    else:
        lines.append("→ 만료할 대기 레벨은 없습니다(이미 터치·종결된 건만 보유).")
    if notice_id is not None:
        url = html.escape(_NOTICE_WEB_URL.format(id=notice_id), quote=True)
        lines.append(f'<a href="{url}">공지 원문</a>')
    lines.append(_SEP)
    return "\n".join(lines)


def _dispatch(title: str, symbols: list, expired: int, nid,
              sent_keys: list, sent_set: set) -> bool:
    """경보 1건 발송 + 성공 시에만 발송 이력 적립. 반환: 발송 성공 여부."""
    if not telegram.send(render_risk_alert(title, symbols, expired, notice_id=nid)):
        return False
    for symbol in symbols:
        key = f"{nid}:{symbol}"
        if key not in sent_set:
            sent_keys.append(key)
            sent_set.add(key)
    return True


def _retry_pending(conn, now: float, cfg_get, sent_keys: list, sent_set: set) -> int:
    """미발송 대기 큐 재시도. 반환: 발송 성공 건수(이력·큐 저장까지 여기서 끝낸다 —
    호출부에 조기 반환 경로가 여럿이라 저장을 미루면 유실되기 쉽다).

    TTL 이 지나 만료된 항목은 버린다 — 며칠 지난 리스크 공지를 뒤늦게 알려봐야
    레벨은 이미 사라진 뒤라 소음일 뿐이다(사실은 로그로 남긴다)."""
    pending = _load_pending(conn)
    if not pending:
        return 0

    ttl_sec = cfg_get("announcement_pending_ttl_hours") * 3600
    kept, sent = [], 0
    for item in pending:
        nid = item.get("nid")
        symbols = [str(s) for s in item.get("symbols") or []]
        if not symbols:
            continue
        if now - float(item.get("first_at") or 0) > ttl_sec:
            logger.warning("[공지] 미발송 경보 폐기(재시도 기한 초과): %s / %s",
                           " · ".join(symbols), str(item.get("title"))[:60])
            continue
        if _dispatch(str(item.get("title") or ""), symbols,
                     int(item.get("expired") or 0), nid, sent_keys, sent_set):
            sent += 1
            logger.warning("[공지] 미발송 경보 재시도 성공: %s", " · ".join(symbols))
        else:
            kept.append(item)

    if len(kept) != len(pending):
        _save_pending(conn, kept)
    if sent:
        _save_sent_keys(conn, sent_keys)
    return sent


def check_announcements(conn, now: float, cfg_get) -> dict:
    """업비트 리스크 공지 폴링 → 경보 + 레벨 만료. 반환 요약 dict(테스트/로그용).

    호출부(price_check.run_once)는 이 함수를 예외 격리해 부른다. 여기서도 개별
    실패는 삼키지만, 방어선을 한 겹 더 두는 편이 2분 주기 잡에 맞다."""
    # pending = 이번 회차에 '새로' 대기 큐에 넣은 건수(누적 큐 길이가 아니다)
    result = {"polled": False, "matched": 0, "alerted": 0, "expired": 0, "pending": 0}
    if not cfg_get("announcement_alert_enabled"):
        return result

    # ① 지난 회차에 발송하지 못한 경보부터 재시도한다. TTL 게이트보다 앞에 두는
    #    이유는 위 _load_pending 주석 참고(레벨이 이미 만료돼 재매칭이 불가능하므로
    #    이 큐가 유일한 복구 경로다). 큐가 비어 있으면 meta 조회 1회로 끝난다.
    sent_keys = _load_sent_keys(conn)
    sent_set = set(sent_keys)
    result["alerted"] += _retry_pending(conn, now, cfg_get, sent_keys, sent_set)

    interval_sec = cfg_get("announcement_poll_interval_minutes") * 60
    try:
        last = float(db.get_meta(conn, _POLL_META) or 0)
    except (TypeError, ValueError):
        last = 0.0
    if last and now - last < interval_sec:
        return result   # TTL 내 - 이번 회차는 호출하지 않는다

    # 추적 중인 코인이 없으면 공지를 볼 이유가 없다 → 네트워크 호출 자체를 생략.
    # (TTL 도 소모하지 않는다 — 값싼 SQL 한 번이라 매 회차 확인해도 부담 없음)
    active_symbols = _active_symbols(conn)
    if not active_symbols:
        return result

    # '시도했다'는 사실을 조회 전에 기록한다. 실패 시 timestamp 를 안 남기면 장애가
    # 이어지는 동안 2분마다 재시도하게 되어 비문서화 API 를 두드리는 꼴이 된다 —
    # 실패해도 다음 주기까지 쉬는 쪽이 무료/무인증 운영 원칙에 맞다.
    db.set_meta(conn, _POLL_META, str(now))
    result["polled"] = True

    notices = _fetch(cfg_get("http_timeout_sec"), cfg_get("announcement_page_size"))
    if not notices:
        return result

    matches = match_notices(
        notices, active_symbols,
        cfg_get("announcement_risk_keywords"),
        cfg_get("announcement_exclude_keywords"),
        max_age_sec=cfg_get("announcement_max_age_hours") * 3600,
        now=now,
    )
    result["matched"] = len(matches)
    if not matches:
        return result

    changed = False
    queued = []

    for notice, symbols in matches:
        nid = _notice_id(notice)
        fresh = [s for s in symbols if f"{nid}:{s}" not in sent_set]
        if not fresh:
            continue
        title = _notice_title(notice)

        try:
            # 만료를 발송보다 먼저 한다: 텔레그램이 죽어 있어도 위험 코인의 알림은
            # 이번 회차부터 멈춰야 한다. 다만 만료하면 그 코인이 _active_symbols() 에서
            # 빠져 다음 폴링에 재매칭되지 않으므로, 발송 실패분은 반드시 대기 큐에
            # 넣어 재시도 경로를 남긴다(안 그러면 경보가 조용히 영구 유실).
            expired = 0
            for symbol in fresh:
                expired += db.expire_levels_for_coin(
                    conn, symbol, reason=f"upbit_notice:{nid}", now=now)
                # 거래량 급증 감시도 즉시 종료 (S9 통합감사 m-6, 2026-07-31):
                # 레벨만 만료하면 volume_watch 가 최대 72h 살아남아 상폐성 펌핑에
                # 🔥 고음량 급증 알림이 나갈 수 있다 — 공지 경보와 정반대 신호.
                db.remove_volume_watch(conn, f"KRW-{symbol}")
            result["expired"] += expired

            if _dispatch(title, fresh, expired, nid, sent_keys, sent_set):
                changed = True
                result["alerted"] += 1
                logger.warning("[공지] 리스크 경보 발송: %s / 레벨 %d건 만료 (%s)",
                               " · ".join(fresh), expired, title[:60])
            else:
                queued.append({"nid": nid, "title": title, "symbols": fresh,
                               "expired": expired, "first_at": now})
                logger.warning("[공지] 경보 발송 실패 - 다음 회차 재시도 대기: %s (%s)",
                               " · ".join(fresh), title[:60])
        except Exception as e:  # noqa: BLE001 — 한 공지 실패가 나머지를 유실시키면 안 된다
            logger.error("[공지] %s 처리 중 예외 → 재시도큐 보존: %s", nid, e)
            queued.append({"nid": nid, "title": title, "symbols": fresh,
                           "expired": 0, "first_at": now})

    if changed:
        _save_sent_keys(conn, sent_keys)
    if queued:
        _save_pending(conn, _load_pending(conn) + queued)
        result["pending"] = len(queued)
    return result


def _active_symbols(conn) -> list:
    """추적 중인 코인 심볼 — 감시/예고 중이거나, 터치됐지만 아직 미종결인 건.

    미종결 터치까지 포함하는 이유: 판정 진행 중인 포지션이 있는 코인이 상장폐지
    수순에 들어갔다면 그 사실 자체를 알려야 한다(만료 대상은 아니지만 경보 대상)."""
    return [r["coin_symbol"] for r in conn.execute(
        "SELECT DISTINCT coin_symbol FROM levels "
        "WHERE status IN ('watching','previewed') "
        "   OR (status='touched' AND outcome IS NULL)"
    ).fetchall()]
