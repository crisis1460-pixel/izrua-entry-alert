"""
모닝 브리핑 — 하루 1회, KST 아침 시간창에 시장환경 요약 1통 (2026-08-15).

주간 리포트(run_cycle.report_due)와 같은 meta 주기 판정 패턴 — 외부 크론 없이
2분 회차가 "오늘 아직 안 보냈고 지금이 발송 창"이면 1통 보낸다. 엔트리 알림
양식(render_alert)과 무관한 별도 메시지 종류이며, 데이터는 전부 기존 캐시
페처(market_sentiment 1h / options 5min / DXY 1h / 업비트 배치 ticker)를
재사용한다 — 신규 API·유료 API 없음.

발송 원칙(render_alert 와 동일): 각 데이터가 None 이면 그 행만 조용히 생략,
브리핑 자체는 항상 발송된다. 실패는 삼키고 로그만(회차를 죽이지 않는다).

meta 기록 순서: **발송 성공 후에만** 날짜를 기록한다 — 발송 실패 시 날짜가
남지 않아 다음 회차(2분 뒤)가 창 안에서 자연 재시도한다. 주간 리포트의
meta 선기록(중복 방지 우선)과 반대인 이유: 브리핑은 하루 창이 2시간뿐이라
유실되면 그날 통째로 못 보고, 만에 하나 중복돼도 요약 1통이라 무해하다.
"""

import html
import json
import logging
import re
import time
import unicodedata
from datetime import datetime

from config import settings
from notify import news_parse, telegram
from storage import db
from utils.time_kst import KST, day_kst

logger = logging.getLogger("alert.morning_brief")

META_LAST_BRIEF_DATE = "last_morning_brief_date"
# 직전 브리핑 **발송 시각**(epoch). 하루 1회 게이트는 위 date 키가 그대로 맡고,
# 이 키는 "🏁 목표 도달" 블록의 조회 창 시작점 전용이다(2026-09-14 수리).
# date 만으로는 창을 만들 수 없다 — 브리핑이 아침 8~10시에 나가므로 '어제 하루'로
# 자르면 오늘 0~8시 적중이 매번 빠지고, '어제+오늘'로 넓히면 어제 이미 보여준 걸
# 또 보여준다. 발송 시각을 남겨야 누락도 중복도 없다.
META_LAST_BRIEF_AT = "last_morning_brief_at"
# 분할 발송 중 **뉴스 메시지만** 실패한 큐 id 목록(JSON). 날짜는 이미 마킹돼 재시도가
# 다음 날 브리핑이 되는데, 그때 48h 신선도 가드가 이 행들을 조용히 소비하면 한 번도
# 못 본 뉴스가 사라진다(2026-09-27 리뷰 RV2-N7, 09-14 유실 사고의 변형). 이 목록의
# 행은 가드를 면제한다. 다음 브리핑 발송 뒤 그 회차의 실패분으로 덮어쓴다.
META_NEWS_RETRY_IDS = "morning_brief_news_retry_ids"
# 위 키가 아직 없을 때(최초 1회, 또는 옛 DB) 쓰는 폴백 창. 브리핑 주기가 하루라
# 24시간이면 직전 회차를 충분히 덮는다.
_BRIEF_FALLBACK_WINDOW_SEC = 86400.0

# 구분선·F&G 한국어 라벨은 알림과 동일 표기 유지(같은 채팅방에 섞여 보인다).
_SEP = telegram.SEP
_FNG_KR = telegram.FNG_KR

# 캘린더 줄 포맷: display width 32 초과 시 kst 부분을 다음 줄로 분리
_MACRO_LINE_MAX_W = 32
_MACRO_INDENT = "   "  # 📅 + 공백 너비 보정(이모지=2, 공백=1 → 3칸)


def _display_width(text: str) -> int:
    """한글·CJK·이모지 = 2, 나머지 = 1 로 환산한 표시 너비."""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def _wrap_indented(text: str, max_w: int, indent: str) -> list:
    """표시 너비 기준으로 줄을 나누고 **모든 줄에 같은 들여쓰기**를 붙인다
    (2026-09-16 사용자 요청 — 행잉 인덴트).

    왜 직접 나누나: 텔레그램에는 CSS 가 없어서, 한 줄이 화면 폭을 넘으면
    클라이언트가 알아서 접고 **접힌 줄은 왼쪽 끝(0열)에 붙는다**. 그러면
    항목 들여쓰기가 무너져 어디까지가 한 항목인지 읽기 어렵다. 우리가 미리
    폭에 맞춰 나누고 각 줄 머리에 같은 indent 를 넣으면 접힘이 일어나지
    않으므로 시작 열이 유지된다.

    어절 경계로 나누되, 한 어절이 폭보다 길면(긴 URL·붙여쓴 영문) 그 어절만
    글자 단위로 쪼갠다 — 안 그러면 그 줄이 폭을 넘어 다시 클라이언트 접힘이
    일어나 목적을 잃는다."""
    avail = max_w - _display_width(indent)
    if avail <= 0 or not text:
        return [indent + text] if text else []

    def _split_long(word: str) -> list:
        """한 어절이 avail 을 넘으면 글자 단위로 조각낸다."""
        out, cur, w = [], "", 0
        for ch in word:
            cw = _display_width(ch)
            if cur and w + cw > avail:
                out.append(cur)
                cur, w = ch, cw
            else:
                cur += ch
                w += cw
        if cur:
            out.append(cur)
        return out

    # 줄바꿈 단위(unit) — 2026-09-27 사용자 요청 "줄내림 후 시작줄 고아단어 안 나오게".
    # 조사·기호만 줄 머리로 넘어가지 않도록 **앞 어절에 붙여** 한 덩어리로 다룬다
    # ("를"·"%"·"·"·")" 등). 화살표·여는 괄호는 뒤 어절에 붙인다("↑ 2,540").
    units = []
    for u in _wrap_units(text):
        if _display_width(u) <= avail:
            units.append(u)
            continue
        # 덩어리가 폭을 넘으면 원래 어절로 풀고, 그래도 넘는 어절만 글자 단위로 쪼갠다.
        for raw in u.split():
            units.extend(_split_long(raw) if _display_width(raw) > avail else [raw])

    rows, cur, cur_w = [], [], 0
    for word in units:
        ww = _display_width(word)
        if cur and cur_w + 1 + ww > avail:
            rows.append(cur)
            cur, cur_w = [word], ww
        else:
            cur_w += (1 if cur else 0) + ww
            cur.append(word)
    if cur:
        rows.append(cur)
    _balance_orphans(rows, avail)
    return [indent + " ".join(r) for r in rows]


# 앞 어절에 붙는 토큰(줄 머리 금지): 문장부호·단위·가운뎃점·흔한 조사 단독 어절.
# 조사 단독 어절은 번역 아티팩트("$ 9,000,000 를")에서 실제로 나온다. "이"·"가"·"의"
# 처럼 관형사·명사로도 쓰이는 글자("이 법안")는 넣지 않는다.
_TRAIL_TOKEN_RX = re.compile(
    r"^(?:[%.,;:!?)\]}…'\"’”·|/]+|은|는|을|를|에|에서|로|으로|와|과|까지|부터)$")
# 뒤 어절에 붙는 토큰(줄 끝 금지): 화살표·여는 괄호·따옴표.
_LEAD_TOKENS = frozenset({"↑", "↓", "→", "(", "[", "“", "‘", "\"", "—", "–"})
# 고아 줄 판정 폭 — 이 이하(또는 덩어리 1개)면 '한 단어만 덩그러니' 남은 줄로 본다.
_ORPHAN_MAX_W = 6


def _wrap_units(text: str) -> list:
    units, glue_next = [], False
    for tok in (text or "").split():
        if units and (glue_next or _TRAIL_TOKEN_RX.match(tok)):
            units[-1] = units[-1] + " " + tok
        else:
            units.append(tok)
        glue_next = tok in _LEAD_TOKENS
    return units


_WORD_SPLIT_RX = re.compile("[ ]+")


def _n_words(units: list) -> int:
    """덩어리 목록의 '단어' 수 — 조사·기호 토큰은 단어로 세지 않는다("9,000,000 를" = 1).
    조각 보호 문자(\\ue000)로 묶인 요약줄 조각은 안의 단어를 각각 센다."""
    return sum(1 for u in units for w in _WORD_SPLIT_RX.split(u)
               if w and not _TRAIL_TOKEN_RX.match(w))


def _balance_orphans(rows: list, avail: int) -> None:
    """줄 끝에서부터 고아 줄(단어 1개 또는 표시폭 ≤6)을 찾아 직전 줄의 마지막
    덩어리를 끌어내린다. 직전 줄이 1단어가 되지 않는 선에서만, 그리고 끌어내린
    결과가 폭 안에 들어갈 때만. 제자리 변형."""
    def _w(ws):
        return sum(_display_width(x) for x in ws) + max(0, len(ws) - 1)

    for i in range(len(rows) - 1, 0, -1):
        cur, prev = rows[i], rows[i - 1]
        while (_n_words(cur) <= 1 or _w(cur) <= _ORPHAN_MAX_W) \
                and len(prev) >= 2 and _n_words(prev[:-1]) >= 2:
            mv = prev[-1]
            if _w([mv] + cur) > avail:
                break
            prev.pop()
            cur.insert(0, mv)

# 매크로 이벤트 예고 범위(일). get_nearby_macro_event 는 24h 창이라 브리핑용
# 7일 예고는 get_macro_events(conn) 자동 캘린더를 사용한다.
_MACRO_LOOKAHEAD_DAYS = 7

# ── 알림량 A안 (2026-09-13) 브리핑 흡수 블록 상한 ────────────────────
# 실시간 발송을 끈 TP 적중·뉴스를 다음 날 아침 브리핑 1통이 대신 전달한다.
_TP_BLOCK_MAX_LINES = 8      # 🏁 목표 도달 — 초과분은 "외 N건"
_NO_NEWS_LINE = "📰 새 뉴스 없음 (조건 통과 0건)"   # 32칸 — 뉴스 0건인 날 첫 통 끝 안내(09-29)
# 📰 주요 뉴스 — 뉴스 상한(15/일)과 동수. 09-28 5→12, 10-09 12→15(대표 피드백 "25개는 너무 많으니
# 건당 내용 보강으로 줄여도 됨" — 압축 항목 4~5줄. 한도를 넘으면 build_brief_messages 가 항목
# 경계에서 나눈다).
_NEWS_BLOCK_MAX = 15
# 큐에서 꺼낼 배수 (2026-09-17). 렌더 직전 2차 필터(_is_queued_noise)가 걸러내는
# 만큼을 채우려면 상한보다 넉넉히 꺼내야 한다 — 딱 5건만 꺼내면 그중 3건이
# 노이즈일 때 2건만 실린다. 3배면 실측 노이즈 비율(약 절반)을 충분히 흡수한다.
_NEWS_FETCH_MULT = 3
# 뉴스 요약 줄의 표시 너비 상한과 들여쓰기 (2026-09-16 행잉 인덴트).
# 매크로 캘린더 줄(_MACRO_LINE_MAX_W=32)과 같은 계열의 값 — 모바일 텔레그램에서
# 한 줄에 무리 없이 들어가는 폭이다. 들여쓰기를 포함한 전체 너비 기준.
# 2026-09-28: 36 → 32. 실측 대표 폰에서 35칸 줄("82,000을 지키면 86,000까지 상승을")이
# 화면 폭을 넘어 '을'이 들여쓰기 없이 다음 줄로 떨어졌다(숫자·쉼표가 반각보다 넓게 렌더).
# 진입 알림과 같은 32칸.
_NEWS_WRAP_W = 32
_NEWS_INDENT = "   "         # 코인·채널 줄과 요약 줄이 같은 열에서 시작한다
# 요약 첫 문장 컷. 2026-09-16 사용자 요청("핵심주제만 두괄식으로, 내용이 계속
# 잘린다")으로 80 → 55. 줄이는 게 곧 개선인 이유: 채널 원문은 이미 두괄식이라
# (첫 줄이 제목) 그 제목만 온전히 실으면 되고, 80자는 제목을 넘어 본문 중간까지
# 물어 와서 "…" 로 끊기기만 했다. 제목이 무의미한 글은 _is_generic_title 이
# 걷어내고 본문 첫 문장을 올린다.
_NEWS_SUMMARY_MAX_CHARS = 55
# 텔레그램 메시지 하드 리밋 4096자. 여유를 두고 이 값을 넘으면 뉴스 줄부터 줄인다
# (뉴스는 다음 날 큐에 남지 않고 소비되므로 '줄이는' 게 아니라 '요약을 자르는' 쪽).
_TELEGRAM_MAX_CHARS = 3900


def brief_due(conn, now: float) -> tuple:
    """발송 판정. 반환 (실행여부, 사유).

    하루 1회 게이트는 타임스탬프 간격이 아니라 KST 날짜 문자열 비교다 —
    "오늘 이미 보냈는가"가 정확히 하루 1회의 의미이고, 실패 시 날짜가 안
    남으므로 같은 키가 재시도 게이트도 겸한다(별도 fail 키 불필요)."""
    if not settings.get("morning_brief_enabled"):
        return False, "비활성(morning_brief_enabled=False)"
    today = day_kst(now)
    last = db.get_meta(conn, META_LAST_BRIEF_DATE)
    if last == today:
        return False, f"오늘({today}) 이미 발송"
    hour_from = settings.get("morning_brief_kst_hour_from")
    hour_to = settings.get("morning_brief_kst_hour_to")
    kst_hour = datetime.fromtimestamp(now, KST).hour
    if not (hour_from <= kst_hour < hour_to):
        return False, f"발송 시간대 대기(KST {kst_hour}시, 창 {hour_from}~{hour_to}시)"
    return True, f"발송 창 도래({today} KST {kst_hour}시)"


# ── 데이터 수집 (전 항목 실패 허용 — None 이면 해당 행 생략) ────────────


def _btc_block(timeout: float) -> tuple:
    """(btc_krw, kimchi_pct). 업비트 배치 ticker 1콜 + 바이낸스 BTC 시세.
    ticker 에는 현재가만 쓴다(fetch_prices 반환이 {market: trade_price})."""
    from monitor import binance, upbit

    btc_krw = usdt_krw = None
    try:
        prices = upbit.fetch_prices(["KRW-BTC", "KRW-USDT"], timeout)
        btc_krw = prices.get("KRW-BTC")
        usdt_krw = prices.get("KRW-USDT")
    except Exception as e:  # noqa: BLE001 - 행 생략으로 강등
        logger.warning("[brief] 업비트 시세 실패: %s", e)

    kimchi = None
    try:
        btc_usd = binance.fetch_usdt_price("BTC", timeout)
        # 김프 산식은 price_check.run_once 와 동일: 실효환율 vs USDT/KRW.
        if btc_krw and usdt_krw and btc_usd and btc_usd > 0:
            kimchi = (btc_krw / btc_usd - usdt_krw) / usdt_krw * 100
    except Exception as e:  # noqa: BLE001 - 행 생략으로 강등
        logger.warning("[brief] 김프 계산 실패: %s", e)
    return btc_krw, kimchi


def _macro_event_lines(now: float, conn) -> list:
    """향후 7일 내 매크로 이벤트 전부 — 날짜순, 한국 발표시각 표기."""
    from monitor import macro

    today = datetime.fromtimestamp(now, KST).date()
    upcoming = []
    for ev in macro.get_macro_events(conn):
        try:
            ev_date = datetime.strptime(ev["date"], "%Y-%m-%d").date()
        except (ValueError, TypeError, KeyError):
            continue
        d = (ev_date - today).days
        if 0 <= d <= _MACRO_LOOKAHEAD_DAYS:
            tag = "D-DAY" if d == 0 else f"D-{d}"
            kst = ev.get("kst_time", "")
            kst_part = f" ({kst})" if kst else ""
            label = ev.get("label", "")
            if not label:
                continue
            base = f"📅 {label} {tag}"
            # 10-09 대표 캡처: "PPI 생산자물가 D-4" / "(한국 21:30)" 두 줄 — 넘치면 먼저 '한국'을 빼
            # 한 줄("(21:30)")에 맞추고, 그래도 넘칠 때만 다음 줄로.
            _short = kst_part.replace("한국 ", "")
            if kst_part and _display_width(base + kst_part) > _MACRO_LINE_MAX_W \
                    and _display_width(base + _short) <= _MACRO_LINE_MAX_W:
                line = base + _short
            elif kst_part and _display_width(base + kst_part) > _MACRO_LINE_MAX_W:
                line = f"{base}\n{_MACRO_INDENT}{kst_part.lstrip()}"
            else:
                line = base + kst_part
            upcoming.append((d, line))
    upcoming.sort(key=lambda x: x[0])
    return [line for _, line in upcoming]


def _tp_hit_lines(conn, now: float) -> list:
    """"🏁 목표 도달" 블록 (2026-09-13 A안). 없으면 빈 리스트(블록 생략).

    tp_alert_send_enabled=False 로 실시간 발송을 끈 TP 적중을 **직전 브리핑 이후**
    치로 모아 전달한다. alerts_log 에는 kind='tpN' 이 종전대로 남으므로 스위치를
    다시 켜도 이 블록은 그대로 동작한다(이중 통지가 되는 건 사용자 선택).

    2026-09-14 수리 — 창을 '어제 하루'에서 '직전 브리핑 이후'로 바꿨다. 브리핑이
    아침 8~10시에 나가므로 어제로 자르면 **오늘 0~8시 적중이 매번 누락**된다
    (실측: 09-14 새벽 00:20~06:12 적중 4건이 당일 브리핑에서 빠짐 → 다음 날에야
    표시, 최대 32시간 지연). TP 실시간 발송을 끈 대가가 "다음 날 아침 확인"인데
    그보다 하루 더 밀리면 맞바꾼 값을 못 받는다. meta.last_morning_brief_at 을
    창 시작점으로 쓰고, 없으면 24시간 폴백.

    진입 대비 %는 render_tp_partial_alert 와 같은 계산식 — (도달 TP − 진입) /
    진입 × 100. 단 저장 단위가 USD(levels.entry_usd/tps_usd)라 환율 없이 비율만
    쓴다(비율은 환율 불변이라 KRW 환산과 동일한 값이 나온다)."""
    since = now - _BRIEF_FALLBACK_WINDOW_SEC
    raw = db.get_meta(conn, META_LAST_BRIEF_AT)
    if raw:
        try:
            prev = float(raw)
            # 시계 역행·수동 편집 방어: 미래이거나 폴백 창보다 과거면 폴백을 쓴다
            # (다른 due 판정과 같은 원칙 — 신뢰할 수 없는 값은 안전한 기본으로).
            if since < prev <= now:
                since = prev
        except (TypeError, ValueError):
            pass
    rows = db.get_tp_hits_since(conn, since)
    if not rows:
        return []

    # 코인당 1행으로 접는다 (2026-09-13 CTO 검토). 같은 코인의 클러스터 형제
    # 레벨이 각각 적중하면 원본은 "AUCTION TP2/3" + "AUCTION TP1/3" 두 행이 되는데,
    # 진입 알림 자체가 클러스터당 1회만 나가므로(cluster_band_pct 병합) 사용자가
    # 본 사건은 하나다. 표시 단위를 알림 단위와 맞춘다 — 코인별로 **가장 멀리 간
    # 단계**만 남기고, 진입 대비 %도 그 단계 기준으로 계산한다.
    best_by_coin: dict = {}
    for r in rows:
        coin = str(r.get("coin") or "?")
        cur = best_by_coin.get(coin)
        if cur is None or (r.get("best_tp") or 0) > (cur.get("best_tp") or 0):
            best_by_coin[coin] = r
    folded = list(best_by_coin.values())

    lines = [f"🏁 <b>목표 도달</b> {len(folded)}건"]
    for r in folded[:_TP_BLOCK_MAX_LINES]:
        coin = html.escape(str(r.get("coin") or "?"))
        best = r.get("best_tp") or 0
        total = r.get("tp_total") or 0
        step = f"TP{best}/{total}" if total else f"TP{best}"
        entry = r.get("entry_usd")
        tps = r.get("tps_usd") or []
        pct = None
        if entry and entry > 0 and 0 < best <= len(tps):
            pct = (tps[best - 1] - entry) / entry * 100
        if pct is not None:
            lines.append(f"   {coin} {step} (진입 {pct:+.1f}%)")
        else:
            lines.append(f"   {coin} {step}")
    if len(folded) > _TP_BLOCK_MAX_LINES:
        lines.append(f"   외 {len(folded) - _TP_BLOCK_MAX_LINES}건")
    return lines


def _news_body(text: str) -> str:
    """뉴스 요약에서 **알맹이만** 남긴다 (2026-09-14).

    채널 원문에는 본문 앞뒤로 정보가 없는 장식이 붙는다. 종전엔 개행만 공백으로
    바꿔 이어 붙였더니 첫 문장 자리를 장식이 차지해 정작 내용이 안 보였다 —
    실측: "# FIL 시장 분석 FIL은 6시간 동안 0.9363으로 폭발하여…" 처럼 제목이
    80자 컷의 절반을 먹었고, "❤️❤️❤️무료 신호!❤️❤️❤️" 로 시작하는 글은 아예
    본문이 한 글자도 안 나왔다.

    걷어내는 것:
      · 선행 마크다운 헤더 줄("# 제목") — 심볼·채널은 바로 윗줄에 이미 있다
      · 장식만 있는 줄(이모지·기호만, 글자 없음)
      · 채널 꼬리말 — 3자 이상 반복되는 구분 기호(➖➖➖, ---) **이후** 전부
    """
    return _news_body_impl(text)


# 무의미한 제목 판정용 (2026-09-16). 채널 템플릿의 첫 줄이 "# FIL 시장 분석",
# "$BTCUSDT 중요 업데이트" 처럼 **심볼 + 일반명사**뿐이면 정보가 0이다. 심볼과
# 채널명은 브리핑에서 바로 윗줄에 이미 찍히므로 이런 제목은 자리만 차지한다.
# 실측 4건(BitcoinBullets FIL·XRP·AAVE·ETH)이 전부 이 형태였고, 80자 컷의 절반을
# 제목이 먹어 정작 "황소/베어 케이스" 분기점이 밀려났다.
_GENERIC_TITLE_WORDS = (
    "시장 분석", "시장분석", "중요 업데이트", "업데이트", "분석", "시황", "차트",
    "전망", "리포트", "뉴스", "소식",
    "market analysis", "market update", "price analysis", "update", "analysis",
    "chart", "outlook", "report", "news",
)
# 제목에서 심볼·장식을 걷어낼 때 쓰는 패턴 — 티커 표기($BTC, BTCUSDT, #ETH)와
# 구두점·이모지를 지운 뒤 남는 게 일반명사뿐인지 본다.
_TITLE_STRIP_RX = re.compile(
    r"[#$*_~`\[\](){}:：,.\-—–·•!?]|"
    r"\b[A-Z0-9]{2,10}(?:USDT|USD|KRW|PERP)?\b", re.I)


def _is_generic_title(line: str) -> bool:
    """제목 줄이 '심볼 + 일반명사'뿐이라 정보가 없는가 (2026-09-16)."""
    t = _TITLE_STRIP_RX.sub(" ", line or "")
    # 타임프레임·기간 표기는 제목의 정보량에 보태지 않는다 — 실측
    # "$NEOUSDT 업데이트: 30분" 은 숫자가 있다는 이유만으로 통과했지만
    # 읽는 사람이 얻는 건 없다.
    t = re.sub(r"\d+\s*(?:분|시간|일|주|개월|m|h|d|w)\b", " ", t, flags=re.I)
    t = " ".join(t.split()).strip().lower()
    if not t:
        return True                     # 심볼·기호만 남은 줄 = 정보 0
    return any(t == w or t.replace(" ", "") == w.replace(" ", "")
               for w in _GENERIC_TITLE_WORDS)


def _news_body_impl(text: str) -> str:
    raw = (text or "").replace("\r", "")
    # 꼬리말 절단: 같은 기호가 3번 이상 연속되면 그 뒤는 채널 서명·홍보다.
    cut = re.search(r"([-=~_➖—–·•*]{3,})", raw)
    if cut:
        raw = raw[:cut.start()]
    kept = []
    for line in raw.split("\n"):
        ln = line.strip()
        if not ln:
            continue
        if not re.search(r"[0-9A-Za-z가-힣]", ln):
            continue                      # 글자가 없는 장식 줄
        if not kept:
            # 선행 제목 줄 처리 (2026-09-16). 마크다운 헤더든 아니든, **정보가
            # 없는 제목**이면 버리고 다음 줄을 머리로 올린다. 종전엔 '#' 로
            # 시작하는 줄만 스킵해서 "$BTCUSDT 중요 업데이트" 같은 무기호 제목은
            # 그대로 남았다. 반대로 "XRP는 엄청난 성장 잠재력을 보여줍니다" 처럼
            # 내용이 있는 제목은 그 자체가 두괄식 요지이므로 반드시 살린다.
            if _is_generic_title(ln.lstrip("#").strip()):
                continue
            kept.append(ln.lstrip("#").strip())
            continue
        kept.append(ln)
    # 불릿 기호는 한 줄로 이어 붙일 때 의미가 없다 — 앞머리 기호만 걷는다.
    kept = [re.sub(r"^[*\-•]\s*", "", k) for k in kept]
    # 줄 구조를 보존해 반환한다 (2026-09-16) — 호출부가 제목 줄과 본문
    # 문장을 구분해야 하기 때문. 종전엔 여기서 공백으로 이어 붙여
    # 그 구분이 사라졌고, 그래서 제목 뒤에 본문이 따라붙어 잘렸다.
    return "\n".join(k for k in kept if k)


# 시나리오 분기 템플릿 (2026-09-16). 실측 15건 중 4건(@BitcoinBullets)이 전부
# 이 꼴이고, 정보량이 가장 많은 글들이다:
#   "# FIL 시장 분석 / FIL은 6시간 동안 … / 황소 케이스: 0.9000을 초과하여 유지…
#    / 베어 케이스: 0.8600 아래로 페이드백… / {수사적 질문} / ➖➖➖ / 채널 서명"
# 이 글의 핵심은 **위아래 분기 가격** 하나뿐인데, 서술을 그대로 실으면 장황해서
# 반드시 잘린다(사용자 지적 "내용이 계속 잘리더라"). 두 숫자만 뽑아 한 줄로 만든다.
# 실패하면(패턴 불일치) 아래 일반 경로로 자연스럽게 떨어진다 — 채널이 포맷을
# 바꿔도 조용히 종전 동작으로 돌아갈 뿐 깨지지 않는다.
# 2026-09-17 확대 — 무료 번역기가 같은 원문(bull case / bear case)을 회차마다
# 다르게 옮긴다. 실측만 해도 "황소 케이스:" · "강세 사례:" · "약세:" 세 가지가
# 나왔고, 마지막은 케이스/사례라는 말 자체가 없다. 그래서 **머리말(케이스·사례·
# 시나리오)은 선택**으로 두고 "강세/약세 + 콜론 + 숫자"를 골격으로 삼는다.
# 콜론과 숫자가 붙어 있어야 하므로 산문에서 우발적으로 걸릴 여지는 작다.
_BULL_RX = re.compile(
    r"(?:황소|불리시|강세|상승)\s*(?:케이스|사례|시나리오)?\s*[:：]\s*([\d,]+(?:\.\d+)?)"
    r"|bull(?:ish)?\s*(?:case|scenario)?\s*[:：]\s*([\d,]+(?:\.\d+)?)", re.I)
_BEAR_RX = re.compile(
    r"(?:베어|곰|약세|하락)\s*(?:케이스|사례|시나리오)?\s*[:：]\s*([\d,]+(?:\.\d+)?)"
    r"|bear(?:ish)?\s*(?:case|scenario)?\s*[:：]\s*([\d,]+(?:\.\d+)?)", re.I)


def _scenario_line(text: str) -> str:
    """'황소/베어 케이스' 템플릿에서 분기 가격 두 개만 뽑아 한 줄로. 없으면 ""."""
    b = _BULL_RX.search(text or "")
    r = _BEAR_RX.search(text or "")
    if not b or not r:
        return ""
    up = b.group(1) or b.group(2)
    dn = r.group(1) or r.group(2)
    if not up or not dn:
        return ""
    # 두 시나리오가 같은 숫자를 기준으로 갈리는 경우가 흔하다(실측 AAVE: 황소
    # "122.00 이상 유지" / 베어 "122.00을 잃고"). 그대로 쓰면 "↑122 · ↓122" 라
    # 오히려 헷갈리므로 하나의 분기선으로 표현한다.
    if up == dn:
        return f"{up} 지키면 강세 · 잃으면 약세"
    # "위/아래"는 화살표와 중복이라 뺀다 — 한 줄(표시 너비 36)에 들어가야
    # 행잉 인덴트로 접히지 않고 한눈에 읽힌다.
    return f"↑ {up} 강세 · ↓ {dn} 약세"


# 촉매(catalyst) 패턴 (2026-09-16 사용자 요청) — "보는 사람이 이걸 왜 사야
# 하는가에 포커스를 맞춰 핵심 이슈를 먼저 언급".
#
# 왜 제목만으론 부족한가: 채널 제목은 대개 "XRP는 엄청난 성장 잠재력을
# 보여줍니다" 같은 수사(修辭)라 읽어도 살 이유를 모른다. 정작 이유는 본문에
# 있다 — 같은 글의 "명확성 법은 상원의 공개 투표를 위해 마련되었으며, 이는
# XRP 가격을 촉매할 수 있습니다" 가 그것이다. 그래서 **문장 단위로 점수를
# 매겨 가장 '살 이유'에 가까운 문장**을 머리에 올린다.
#
# 배점은 행동 근거의 강도 순이다: 외부 사건(규제·자금 유입) > 예정된 이벤트
# (상장·업그레이드) > 차트 신호. 차트 신호를 낮게 둔 이유는 그것만으론
# "왜 지금"에 답하지 못해서다.
_CATALYST_PATTERNS = (
    # ── 외부 사건: 가장 강한 매수 근거 ──
    (3, re.compile(r"법안?\b|상원|하원|의회|규제|승인|ETF|SEC\b|소송|판결|"
                   r"\bbill\b|\bsenate\b|\bapproval\b|\bruling\b|\blawsuit\b", re.I)),
    (3, re.compile(r"고래|기관|큰손|\bwhale\b|\binstitution|"
                   r"[\$￦]\s?[\d,]+(?:\.\d+)?\s*(?:억|만|[MBK]\b|백만|천만)|"
                   r"순유입|자금\s*유입|\binflow", re.I)),
    # ── 예정 이벤트: 날짜가 있어 '왜 지금'에 답한다 ──
    (2, re.compile(r"상장|메인넷|업그레이드|하드포크|파트너십|제휴|에어드랍|"
                   r"언락|해제|\blisting\b|\bmainnet\b|\bupgrade\b|\bpartnership\b", re.I)),
    (2, re.compile(r"신고가|사상\s*최고|역대\s*최고|\ball[- ]?time high\b|\bATH\b", re.I)),
    # ── 차트 신호: 보조 ──
    (1, re.compile(r"과매도|초과\s*판매|과매수|골든\s*크로스|데드\s*크로스|"
                   r"돌파|브레이크아웃|\boversold\b|\bbreakout\b|\bgolden cross\b", re.I)),
    (1, re.compile(r"목표가?|저항선?|지지선?|\btarget\b|\bresistance\b|\bsupport\b", re.I)),
)
_SENT_SPLIT_RX = re.compile(r"(?<=[.。!?])\s+|\n+")

# 촉매 문장을 본문 중간에서 뽑으면 접속사로 시작하는 일이 잦다(실측 XRP:
# "또한, 명확성 법은 상원의…"). 머리에 올릴 문장이라 앞의 연결어는 군더더기다.
_LEAD_CONJ_RX = re.compile(
    r"^(?:또한|그리고|그러나|하지만|한편|게다가|더욱이|아울러|따라서|그래서|"
    r"however|moreover|also|additionally|furthermore|meanwhile|but|and)"
    r"\s*[,，]?\s*", re.I)
# 번역 아티팩트: "# Sol", "$ ZRX" 처럼 기호와 티커 사이에 공백이 끼어 들어온다.
_SYMBOL_GAP_RX = re.compile(r"([#$￦])\s+(?=[A-Za-z0-9])")
# 한 문장이 상한을 넘을 때 "…"로 뭉개는 대신 **절 경계**에서 끊는다. 실측 XRP
# 규제 문장이 "…가격 움직임을 촉매할…" 처럼 동사 중간에서 잘려 뜻이 끊겼다.
_CLAUSE_END_RX = re.compile(r"(?<=[,，])\s*|(?<=며)\s+|(?<=고)\s+|(?<=만)\s+|(?<=서)\s+")


def _strip_lead(s: str) -> str:
    """머리에 올릴 문장 다듬기 — 선행 접속사 제거 + 기호-티커 공백 정리."""
    out = _LEAD_CONJ_RX.sub("", (s or "").strip())
    out = _SYMBOL_GAP_RX.sub(r"\1", out)
    return out.strip()


def _clip_clause(s: str, max_chars: int) -> str:
    """max_chars 안에서 **절 경계**까지만 남긴다. 경계가 없으면 길이로 자른다."""
    if len(s) <= max_chars:
        return s
    cut = s[:max_chars]
    best = 0
    for m in _CLAUSE_END_RX.finditer(cut):
        if m.start() > max_chars // 2:      # 너무 앞에서 끊지 않는다
            best = m.start()
    if best:
        return cut[:best].rstrip(" ,，") + "…"
    return cut.rstrip() + "…"


def _catalyst_best(body: str) -> tuple:
    """(최고 점수 문장, 점수). _catalyst_sentence·_catalyst_score 공용 루프
    (2026-09-22 P5 — 뉴스 랭킹에 점수만 필요한 호출부가 생겨 분리).

    동점이면 **앞선 문장**이 이긴다(원문의 두괄식 의도를 존중)."""
    best, best_score = "", 0
    for raw in _SENT_SPLIT_RX.split(body):
        s = raw.strip()
        if len(s) < 8:                      # 조각·머리말은 후보 제외
            continue
        score = sum(pts for pts, rx in _CATALYST_PATTERNS if rx.search(s))
        if score > best_score:
            best, best_score = s, score
    return best, best_score


def _catalyst_score(body: str) -> int:
    """본문 전체에서 촉매 패턴에 걸리는 **최고 문장 점수**(2026-09-22 P5).

    표시용 _catalyst_sentence 는 2점 미만이면 ""를 돌려주지만(차트 신호 단독
    불채택), 뉴스 랭킹은 1점짜리 차이도 순서에 반영해야 하므로 문턱 없는
    원점수가 따로 필요하다. 같은 루프(_catalyst_best)를 재사용해 두 곳의
    '가장 중요한 문장이 무엇인가' 판단이 갈리지 않게 한다."""
    return _catalyst_best(body)[1]


def _catalyst_sentence(body: str, max_chars: int) -> str:
    """본문에서 '살 이유'에 가장 가까운 문장 1개. 근거가 없으면 "".

    뽑은 문장이 길면 호출부가 자르되, 자르기 전에 문장 자체가 핵심이라는
    점은 지켜진다."""
    best, best_score = _catalyst_best(body)
    return best if best_score >= 2 else ""  # 차트 신호 1점짜리 단독은 채택 안 함


def _first_sentence(text: str, max_chars: int = _NEWS_SUMMARY_MAX_CHARS) -> str:
    """브리핑에 실을 **핵심 한 줄**. 없으면 "".

    우선순위 (2026-09-16 사용자 요청 "왜 사야 하는가에 포커스"):
      ① 촉매 문장 — 규제·자금 유입·예정 이벤트처럼 **행동 근거**가 담긴 문장
      ② 시나리오 분기 템플릿이면 분기 가격 한 줄 (_scenario_line)
      ③ 정보 있는 제목 줄이 있으면 **그 줄에서 끝낸다** — 채널 원문은 대개
         첫 줄이 제목이라 이미 두괄식이다. 종전엔 모든 줄을 공백으로 이어 붙여
         제목 뒤에 본문이 따라붙었고, 그래서 제목이 멀쩡한데도 "…"로 잘렸다.
      ④ 그 외에는 첫 문장을 max_chars 안에서.
    ①이 ②보다 앞서는 이유: 분기 가격은 "어디서 사라"이지 "왜 사라"가 아니다.
    """
    body = _news_body(text)
    if not body:
        return ""

    cat = _catalyst_sentence(body, max_chars)
    if cat:
        return _clip_clause(_strip_lead(cat), max_chars)

    scen = _scenario_line(text)
    if scen:
        return scen

    head = body.split("\n", 1)[0].strip() if "\n" in body else ""
    if head and len(head) <= max_chars:
        return head

    t = _strip_lead(" ".join(body.split()))
    if not t:
        return ""
    for sep in (". ", "。", "! ", "? "):
        idx = t.find(sep)
        if 0 < idx <= max_chars:
            return t[:idx + 1]
    if len(t) <= max_chars:
        return t
    return t[:max_chars].rstrip() + "…"


def _is_queued_noise(symbol: str, summary: str, summary_en: str = "") -> bool:
    """큐에 **이미 들어간** 항목을 렌더 직전에 한 번 더 거른다 (2026-09-17).

    왜 두 번 거르나: 수집 단계 필터(news_brief)는 큐 적재 **전에만** 돈다. 그래서
    ① 필터를 고쳐도 이미 쌓인 항목에는 소급되지 않고 ② 새 노이즈 유형이 나타나면
    고치기 전에 들어온 것들이 그대로 나간다. 실제로 09-17 아침 브리핑에 시그널
    카드가 실렸다 — 큐 적재는 03:23 수집 회차, 필터 수정 배포는 07:04 였다.
    진입가 sanity 를 수집·감시 두 곳에서 보는 것과 같은 이유다(마지막 방어선).

    판정 기준은 news_brief 와 **같은 것을 재사용**한다 — 두 곳의 기준이 갈리면
    어느 쪽이 정본인지 알 수 없게 된다. 지연 import 는 순환 방지용.

    2026-09-27 S0 — 원문(summary_en)이 있으면 **수치·리캡·시그널 판정은 원문으로**
    한다. 실측: "$1.69B BTC ETF Net-flow" 가 "16억 9천만 개" 로 번역돼 정보 밀도
    게이트(가격다운 수치)에 걸렸고, 그 기간 유일한 ETF 자금 뉴스가 버려졌다.
    광고 키워드는 한·영 어느 쪽에 있어도 광고라 양쪽을 본다."""
    from notify import news_brief as nb

    if (symbol or "").upper() in nb._AMBIGUOUS_SYMBOLS:
        return True
    ko = summary or ""
    en = summary_en or ""
    if any(k in (ko + "\n" + en).lower() for k in nb._PROMO_KEYWORDS):
        return True
    text = en or ko
    if nb._is_trade_result(text) or nb._is_trade_setup(text):
        return True
    if len(text) < nb._DENSITY_MIN_LEN and not nb._NUMBER_RX.search(text):
        return True
    return False


def _news_importance(text: str) -> int:
    """뉴스 1건의 중요도 점수(2026-09-22 P5 — 도착순 대신 중요도순 정렬).

    ① _catalyst_score — 본문 전체(장식 제거본)에서 가장 강한 문장 1개의 점수
       (외부 사건 3 > 예정 이벤트 2 > 차트 신호 1).
    ② _scenario_line 이 성립하면(황소/베어 분기 가격) +1 — 매매 판단에 바로
       쓸 수 있는 숫자라 촉매 없는 글보다는 위에 둘 가치가 있다.
    시나리오 판정은 원문(text) 기준 — _first_sentence 가 그 문장을 뽑을 때와
    같은 입력을 써야 "이 글이 왜 분기선을 얻었는가"가 어긋나지 않는다."""
    score = _catalyst_score(_news_body(text))
    if _scenario_line(text):
        score += 1
    return score


def _fetch_tickers(markets: list, timeout: float) -> dict:
    """업비트 공개 ticker **1콜**(배치) → {market: {"trade_price", "signed_change_rate"}}.

    뉴스 v2 가격 맥락 전용(2026-09-27). 브리핑 회차에서 코인당 1콜 이내 — 배치 1콜이
    기본이고, 상폐 종목이 섞여 400/404 면 그때만 마켓별 1콜로 구제한다. 실패는 빈
    dict(해당 줄 생략). 테스트는 이 함수를 몽키패치한다(네트워크 없음)."""
    import requests

    out: dict = {}
    if not markets:
        return out
    url = "https://api.upbit.com/v1/ticker"
    try:
        r = requests.get(url, params={"markets": ",".join(markets)}, timeout=timeout)
        if r.status_code == 200:
            for t in r.json():
                out[t["market"]] = {"trade_price": float(t["trade_price"]),
                                    "signed_change_rate": float(t.get("signed_change_rate") or 0.0)}
            return out
        if r.status_code not in (400, 404):
            logger.warning("[brief] 뉴스 가격 맥락 ticker HTTP %s", r.status_code)
            return out
    except Exception as e:  # noqa: BLE001 - 맥락 줄 생략으로 강등
        logger.warning("[brief] 뉴스 가격 맥락 ticker 실패: %s", e)
        return out
    for m in markets:
        try:
            time.sleep(0.12)
            r = requests.get(url, params={"markets": m}, timeout=timeout)
            if r.status_code == 200 and r.json():
                t = r.json()[0]
                out[m] = {"trade_price": float(t["trade_price"]),
                          "signed_change_rate": float(t.get("signed_change_rate") or 0.0)}
        except Exception as e:  # noqa: BLE001
            logger.warning("[brief] %s ticker 개별 조회 실패: %s", m, e)
    return out


def _price_ctx(symbols: list, timeout: float, kimchi=None) -> dict:
    """{sym: {"chg24": %, "cur_usd": 추정 USD 가}} — 뉴스 항목 ④ 가격 맥락줄 재료.

    cur_usd = KRW 현재가 / 실효환율. 실효환율 = USDT/KRW × (1 + 김프) — 브리핑 상단
    김프 계산(업비트 BTC ÷ 바이낸스 BTC)과 같은 환율이라 채널이 적은 USDT 가격과 같은
    단위가 된다. 김프를 모르면 USDT/KRW 만 쓴다(오차 = 김프 %). MARKET(🌐)은 BTC 로 본다."""
    syms = sorted({("BTC" if s == news_parse.MARKET_SYMBOL else s) for s in symbols if s})
    if not syms:
        return {}
    markets = [f"KRW-{s}" for s in syms] + ["KRW-USDT"]
    tick = _fetch_tickers(markets, timeout)
    usdt = (tick.get("KRW-USDT") or {}).get("trade_price")
    fx = usdt * (1 + kimchi / 100.0) if (usdt and kimchi is not None) else usdt
    out = {}
    for s in syms:
        t = tick.get(f"KRW-{s}")
        if not t:
            continue
        out[s] = {"chg24": t["signed_change_rate"] * 100.0,
                  "cur_usd": (t["trade_price"] / fx) if fx else None}
    if "BTC" in out:
        out[news_parse.MARKET_SYMBOL] = {"chg24": out["BTC"]["chg24"], "cur_usd": None}
    return out


def _is_feed(ch: str) -> bool:
    """RSS 피드 이름인가(09-29 신설) — 텔레그램 채널이 아니라 '@' 없이 매체명만 쓴다."""
    return bool(ch) and ch in {str(f[0]) for f in (settings.get("rss_news_feeds") or []) if f}


def _source_label(ch: str) -> str:
    """출처 표기(escape 전 평문) — 텔레그램 "@채널", RSS "CoinDesk", 없으면 ""."""
    if not ch:
        return ""
    return ch if _is_feed(ch) else f"@{ch}"


# 압축 항목 하위 줄 들여쓰기(2026-10-09 대표 피드백 "기존처럼 행별 시작줄 맞추고 하위내용만
# 들여쓰기") — 진입 알림("타점" / "  현재: …")과 같은 모양: 머리줄 0열, 하위 줄 2칸.
_CMP_SUB = "  "
_CMP_SUMMARY_MAX_LINES = 2


def _compact_comp(parsed: dict, sym: str, row: dict, ctx: dict):
    """news_parse.compact 호출(폭·출처·번역문을 브리핑 규격으로 채워서)."""
    ch = str(row.get("channel") or "")
    return news_parse.compact(parsed, sym, row.get("summary_en") or "", ctx,
                              source=_source_label(ch), is_feed=_is_feed(ch),
                              avail=_NEWS_WRAP_W - _display_width(_CMP_SUB),
                              head_avail=_NEWS_WRAP_W, summary_ko=row.get("summary") or "")


def _compact_empty(comp) -> bool:
    """내용 없는 의견 항목("💬 분석 기사 / → 기사: 방향 단정 없음", 숫자 없음)인가 — 싣지 않는다
    (10-09 대표 요청 "진짜 시세와 관련된 최고 중요내용만"). 후보 단계에서 걸러 블록 상한 자리를
    차지하지 않게 한다(리뷰 10-09). 판정은 했으니 소비는 본문 메시지와 함께."""
    if not comp or comp.get("kind") == "fact":
        return False                       # 사실형은 건너뛰지 않는다
    call = comp.get("call") or ""
    summ = " ".join(_compact_summary_lines(comp.get("summary")))
    if summ.startswith("확인된 사건이 아닌"):
        # 전망·가정 기사인데 실을 수 있는 요약이 일반 안내문뿐(제목도 2줄에 안 들어감) — 정보 0
        # (최종 리뷰 10-09: 큐 89·102·125 "전망·가정 기사 / 확인된 사건이 아닌 예상·가정 기사").
        return True
    blob = (comp.get("kw") or "") + call + summ
    # 숫자(레벨·목표·수치)가 하나도 없이 "관망"·"방향 단정 없음"뿐인 의견(큐 119·132 "박스권 / 기사:
    # 박스권 관망")은 판단 재료가 없다.
    return not re.search(r"\d", blob) and ("관망" in blob or "방향 단정 없음" in blob)


def _compact_summary_lines(cands: list) -> list:
    """요약 후보 중 하위 들여쓰기(2칸)·32칸으로 접어 **2줄 이내·고아 줄 0** 인 첫 후보의 줄들
    (escape 전). 하나도 안 맞으면 [] — 자르거나 "…" 를 붙이지 않는다."""
    avail = _NEWS_WRAP_W - _display_width(_CMP_SUB)
    for s in cands or []:
        s = " ".join((s or "").split())
        if not s:
            continue
        # 폭보다 긴 한 어절(글자 단위로 쪼개짐)은 중간 절단과 같아 후보에서 뺀다.
        if any(_display_width(w) > avail for w in s.split(" ")):
            continue
        wrapped = _wrap_indented(s, _NEWS_WRAP_W, _CMP_SUB)
        if 1 <= len(wrapped) <= _CMP_SUMMARY_MAX_LINES and orphan_lines(wrapped, _CMP_SUB) == 0:
            return [ln[len(_CMP_SUB):] for ln in wrapped]
    return []


def _rank_tag(sym: str, rank) -> str:
    """머리줄 순위 표기 — "[시총 N위]" / 🌐 "[전체]" / 순위 모름(fail-open)이면 ""."""
    if sym == news_parse.MARKET_SYMBOL:
        return "[전체]"
    return f"[시총 {int(rank)}위]" if rank else ""


def _compact_item_lines(parsed: dict, sym: str, row: dict, ctx: dict, rank=None):
    """압축 항목 — 2026-10-09 대표 결정 **레이아웃 C**. 4~5줄, 줄마다 32칸 이내.

    ① "<b>SYM</b> [시총 N위] 🟢 상승 재료"  — 0열. 의견은 "💬 채널 의견"/"💬 기사 의견".
                                             32칸을 넘으면 판정 글자를 빼고 칩만(접지 않는다).
    ② "  핵심 키워드( · 짧은 이유)"          — 사실은 이유가 들어가면 붙인다. 차트는 셋업 키워드만.
    ③ "  판단 보강 요약 1~2줄"               — 의견은 "채널:"/"기사:" 인용. 사실은 요약 후보가 하나도
                                             안 들어가면 이유(_fact_why)로 채워 **항목 모양을 통일**.
    ④ "  24h ±x% · 출처"
    폭 맞춤은 평문 기준, escape 는 **맞춘 뒤** 줄마다. 반환 None = 내용 없는 의견(건너뜀),
    [] = 압축 불가(종전 형식으로 강등)."""
    comp = _compact_comp(parsed, sym, row, ctx)
    if not comp:
        return []
    if _compact_empty(comp):
        return None
    sub_w = _NEWS_WRAP_W - _display_width(_CMP_SUB)
    # ① 머리줄 — 라벨 + 순위 + 칩 + 판정. 넘치면 칩만.
    tag = _rank_tag(sym, rank)
    base = " ".join(x for x in (comp["label"], tag) if x)
    verdict_full = f"{comp['chip']} {comp['verdict']}"
    vtxt = verdict_full if _display_width(f"{base} {verdict_full}") <= _NEWS_WRAP_W else comp["chip"]
    head = f"<b>{html.escape(comp['label'])}</b>" + (f" {html.escape(tag)}" if tag else "") \
        + f" {html.escape(vtxt)}"
    # ② 키워드 줄 · ③ 요약
    summ = _compact_summary_lines(comp.get("summary"))
    kw = comp["kw"]
    if comp["kind"] == "fact":
        whys = comp.get("whys") or []
        kws0 = (comp.get("kws") or [kw])[0]
        # 키워드에 이미 든 말은 이유로 다시 붙이지 않는다("연준 금리 동결 · 금리 동결" 방지).
        whys_all = list(whys)
        whys = [w for w in whys if w.split(",")[0].strip() not in kws0]
        if summ:
            with_why = [f"{kws0} · {w}" for w in whys] + [f"{kw} · {w}" for w in whys]
            l2 = next((c for c in with_why if _display_width(c) <= sub_w), kw)
        else:
            # 요약 후보가 하나도 안 들어가면 이유를 요약 자리로 — 키워드 줄과 중복되지 않게
            # 키워드 줄엔 이유를 붙이지 않는다(대표 요구: 코인마다 같은 모양).
            l2 = kw
            summ = _compact_summary_lines([comp.get("why_full")] + whys + whys_all
                                          + [comp.get("verdict")])
    else:
        l2 = kw
    subs = [l2] + [s for s in summ if s.strip() != (l2 or "").strip()] + [comp["tail"]]
    return [head] + [_CMP_SUB + html.escape(ln) for ln in subs if ln]


def _is_item_head(line: str) -> bool:
    """뉴스 블록 안에서 항목 머리줄인가 — 종전 긴 형식("   <b>SYM</b> · @ch")과 압축 형식
    ("<b>SYM</b> 칩 …", 0열) 둘 다. 하위 줄은 각각 3칸 평문·2칸 들여쓰기라 '<b>' 로 시작하지
    않는다. 항목 구조((줄들, id) 목록)를 넘길 수 없는 호출(종전 테스트 등)의 폴백 판정용."""
    return line.startswith(_NEWS_INDENT + "<b>") or line.startswith("<b>")


def _item_header(sym: str, ch: str) -> str:
    """항목 머리줄(종전 긴 형식). 항목 경계는 (줄들, id) 구조로 넘기고, 구조가 없는 호출만
    _is_item_head 가 이 줄의 머리("   <b>")로 센다."""
    # RSS 피드(09-29 신설)는 텔레그램 채널이 아니라 '@' 없이 매체명만("· CoinDesk").
    ch_part = (f" · {html.escape(ch)}" if _is_feed(ch) else f" · @{html.escape(ch)}") if ch else ""
    label = "🌐 시장" if sym == news_parse.MARKET_SYMBOL else html.escape(sym or "?")
    return f"{_NEWS_INDENT}<b>{label}</b>{ch_part}"


_SEG_GLUE = "\ue000"   # 조각 안 공백 보호용 사용자 정의 문자(표시폭 1 = 공백과 같다)


def _wrap_segments(src: str) -> tuple:
    """segments 모드 접기 — (줄 목록, 고아 없음 여부).

    2026-09-28 대표 캡처("💬 차트 의견(8시간봉) ·" / "박스권 · 단기", "24h -0.4% · 분기" /
    "↑82,000(-2.7%)"): 조각 경계에서 줄이 바뀌면 줄 끝에 '·' 가 매달리고 '분기' 라벨이 값과
    떨어졌다. 이제 **조각 단위로 채우고, 줄이 바뀌는 경계의 ' · ' 는 지운다**. 폭보다 긴
    조각만 어절 단위로 접고(라벨과 첫 값은 함께), 마지막 줄이 짧은 조각 하나뿐이면 직전 줄의
    마지막 조각을 끌어내려 균형을 맞춘다."""
    avail = _NEWS_WRAP_W - _display_width(_NEWS_INDENT)
    segs = [sg for sg in src.split(" · ") if sg]
    rows: list = []      # 줄마다 조각 목록
    cur: list = []
    for sg in segs:
        w = _display_width(sg)
        if w > avail:
            # 긴 조각: 어절 단위로 접어 앞 줄들은 확정, 마지막 줄은 이어 붙일 수 있게 둔다.
            if cur:
                rows.append(cur)
                cur = []
            parts = [ln[len(_NEWS_INDENT):] for ln in _wrap_indented(sg, _NEWS_WRAP_W, _NEWS_INDENT)]
            rows.extend([p] for p in parts[:-1])
            cur = [parts[-1]] if parts else []
            continue
        cand = cur + [sg]
        if cur and _display_width(" · ".join(cand)) > avail:
            rows.append(cur)
            cur = [sg]
        else:
            cur = cand
    if cur:
        rows.append(cur)
    # 균형: 마지막 줄이 짧은 조각 하나면 직전 줄(조각 2개 이상)의 마지막 조각을 끌어내린다.
    if len(rows) >= 2 and len(rows[-1]) == 1 and len(rows[-2]) >= 2 \
            and _display_width(rows[-1][0]) <= _ORPHAN_MAX_W + 2:
        moved = rows[-2][-1]
        if _display_width(" · ".join([moved] + rows[-1])) <= avail:
            rows[-2] = rows[-2][:-1]
            rows[-1] = [moved] + rows[-1]
    elif len(rows) >= 2 and len(rows[-1]) == 1 and len(rows[-2]) == 1 \
            and _display_width(rows[-1][0]) <= _ORPHAN_MAX_W + 2:
        # 직전 줄이 긴 조각 하나("🔴 악재 연준 금리 인상 25bp" / "단기")면 그 조각의 마지막
        # 어절을 끌어내린다 → "🔴 악재 연준 금리 인상" / "25bp · 단기".
        words = rows[-2][0].split(" ")
        if len(words) >= 3:
            cand = words[-1] + " · " + rows[-1][0]
            if _display_width(cand) <= avail:
                rows[-2] = [" ".join(words[:-1])]
                rows[-1] = [cand]
    lines = [_NEWS_INDENT + " · ".join(r) for r in rows]
    return lines, True


def _wrap_segments_legacy(src: str) -> tuple:
    """종전(09-27) segments 접기 — 조각 보호 + 어절 균형. 참고용으로만 남김."""
    avail = _NEWS_WRAP_W - _display_width(_NEWS_INDENT)
    segs = src.split(" · ")
    protect = [_display_width(sg) <= avail - 2 for sg in segs]
    # 조각 보호가 오히려 고아 줄을 만들면("… 25bp ·" / "단기") 뒤쪽 조각부터 보호를
    # 풀어 어절 단위 균형 맞추기에 맡긴다.
    for k in range(len(segs), -1, -1):
        cand_src = " · ".join(sg.replace(" ", _SEG_GLUE) if (protect[i] and i < k) else sg
                              for i, sg in enumerate(segs))
        cand = _wrap_indented(cand_src, _NEWS_WRAP_W, _NEWS_INDENT)
        if orphan_lines(cand, _NEWS_INDENT, strict=True) == 0:
            return cand, True
    return _wrap_indented(src, _NEWS_WRAP_W, _NEWS_INDENT), False


def _wrap_escaped(text: str, segments: bool = False, reorder: bool = False) -> list:
    """행잉 인덴트로 접고 줄마다 HTML escape.

    segments=True(요약줄·맥락줄): " · " 로 나뉜 조각을 **한 덩어리**로 접는다 —
    "저항 / 시험" 처럼 한 항목이 줄 경계에서 갈라지지 않게(2026-09-27 고아단어
    요청). 폭보다 긴 조각만 예외로 종전처럼 어절 단위로 접힌다.
    escape 는 접은 **뒤에** 각 줄에 적용한다 — 먼저 escape 하면 `&amp;` 같은
    엔티티가 줄 경계에서 쪼개져 깨진 문자로 보인다.

    reorder=True(가격 맥락줄 전용 — 조각 순서에 의미가 없는 줄): 보호를 다 풀어도 고아가
    남으면 첫 조각("24h ±x%")을 맨 뒤로 돌려 한 번 더 접는다(2026-09-27 리뷰 RV2-W1:
    극소가 코인 "분기 ↑0.00000596(+2.2%) ↓0.00000570(-2.2%)" 의 마지막 덩어리가 혼자
    남았다 — 두 덩어리가 한 줄에 안 들어가 순서를 지키면 해가 없다). 요약줄은 칩이
    맨 앞이어야 하므로 쓰지 않는다."""
    src = text or ""
    wrapped = _wrap_indented(src, _NEWS_WRAP_W, _NEWS_INDENT)
    if segments and " · " in src:
        wrapped, clean = _wrap_segments(src)
        if not clean and reorder:
            segs = src.split(" · ")
            alt, alt_clean = _wrap_segments(" · ".join(segs[1:] + segs[:1]))
            if alt_clean:
                wrapped = alt
    return [_NEWS_INDENT + html.escape(ln[len(_NEWS_INDENT):].replace(_SEG_GLUE, " "))
            for ln in wrapped]


def orphan_lines(lines: list, indent: str = _NEWS_INDENT, strict: bool = False) -> int:
    """고아 줄 수 — 둘째 줄부터, 단어 1개이거나 표시폭 ≤6 인 줄. 단, 직전 줄이
    2단어 이하라 끌어내릴 수 없는 경우는 세지 않는다(_balance_orphans 와 같은 기준).
    샘플 검사·테스트(NEWS-ORPHAN)·요약줄 조각 보호 판단용."""
    body = [ln[len(indent):] if ln.startswith(indent) else ln for ln in lines]
    bad = 0
    for i in range(1, len(body)):
        cur = _wrap_units(body[i])
        prev = _wrap_units(body[i - 1])
        # strict: 끌어내릴 수 있든 없든 고아 모양이면 센다(요약줄 조각 보호 해제 판단용).
        if (_n_words(cur) <= 1 or _display_width(body[i].replace(_SEG_GLUE, " ")) <= _ORPHAN_MAX_W) \
                and (strict or (len(prev) >= 2 and _n_words(prev[:-1]) >= 2)):
            bad += 1
    return bad


def _item_ctx(ctx_map: dict, sym: str, row: dict, now) -> dict:
    """항목별 맥락 — 가격(ctx_map) + 게시 경과 시간(age_h)."""
    ctx = dict(ctx_map.get(sym) or {})
    posted = row.get("posted_at")
    if posted:
        ref = now if now is not None else time.time()
        ctx["age_h"] = max(0.0, (ref - float(posted)) / 3600.0)
    return ctx


def _retry_ids(conn) -> set:
    """전날 분할 뉴스 메시지 발송 실패로 남은 큐 id(META_NEWS_RETRY_IDS). 실패는 빈 집합."""
    try:
        raw = db.get_meta(conn, META_NEWS_RETRY_IDS) or ""
        return {int(x) for x in json.loads(raw)} if raw else set()
    except Exception as e:  # noqa: BLE001 - 면제 없이 종전 동작으로 강등
        logger.warning("[brief] 재시도 id meta 읽기 실패: %s", e)
        return set()


def _news_items(conn, consumed_ids: list, kimchi=None, timeout: float = 5.0,
                now: float = None):
    """📰 블록 재료. 반환 (헤더 줄, [(항목 줄 목록, 큐 id), ...]) 또는 None.

    2026-09-13 A안 → 2026-09-22 중요도순 → 2026-09-27 뉴스 v2(구조화)로 확장.
    소비 계약(09-22)은 그대로다 — **판정한 후보는 전부** consumed_ids 에 넣고, 꺼내지
    않은 잔여분만 다음 날 후보로 남는다. 실제 소비(DB)는 발송 성공 후 호출부가 한다.

    v2(news_structured_enabled=True + 큐 행에 영문 원문 summary_en 있음):
      · 판정: news_parse.parse(원문) — 노이즈(지난 예측 자찬·유형/금액 없는 일반론) 제외
      · 랭킹: 정보 유형 등급(사실 H > 사실 M > 🌐시장 > 💬차트 > 💬의견) → 게시 최신순
        (기획안 §4-4 + 사용자 결정 Q1~Q3). 코인당 1건·채널 다양성은 종전과 같다.
      · 렌더: 머리줄 / 요약줄 / 설명 2~3문장 / 가격 맥락줄(전부 행잉 인덴트).
    원문이 없는 과거 행·스위치 OFF 는 종전(번역문 첫 문장) 경로 그대로다."""
    rows = db.get_news_digest(conn, limit=_NEWS_BLOCK_MAX * _NEWS_FETCH_MULT)
    if not rows:
        return None
    from notify import news_brief as nb   # 시총 순위 게이트(수집 단계와 같은 함수)
    structured = bool(settings.get("news_structured_enabled"))
    compact = bool(settings.get("news_compact_enabled"))
    market_ok = bool(settings.get("news_market_enabled"))
    try:
        n_sent = int(settings.get("news_detail_sentences") or 3)
    except (TypeError, ValueError):
        n_sent = 3

    cands = []   # {"row", "v2", "parsed"|"summ", "key"}
    retry_ids = _retry_ids(conn)
    for r in rows:
        # 꺼낸 이상 **전부** 소비 처리한다 — 안 그러면 밀려난 저점수 뉴스가
        # 큐 머리에 남아 다음 날 후보 창을 막는다(get_news_digest 는 오래된
        # 순이라 머리에서 막히면 그 뒤가 계속 굶는다).
        consumed_ids.append(r["id"])
        # 신선도 가드 (2026-09-27 대표 결정 "48시간 이내") — 큐에 이미 남아 있던 옛 글도
        # 렌더에서 제외(소비는 위에서 처리해 큐 머리를 막지 않는다). 게시 시각이 없는
        # 구세대 행은 적재 시각으로 본다.
        # 예외(2026-09-27 리뷰 RV2-N7): 전날 분할 뉴스 메시지 발송이 실패해 **재시도로
        # 남은 행**은 가드를 면제한다. 안 그러면 게시 24h 가 넘은 뉴스가 사용자에게 한 번도
        # 안 나간 채 이 가드에서 조용히 소비된다(09-14 유실 사고의 변형).
        try:
            _max_h = float(settings.get("news_max_age_hours") or 0)
        except (TypeError, ValueError):
            _max_h = 0.0
        _pub = r.get("posted_at") or r.get("created_at")
        _ref = now if now is not None else time.time()
        if _max_h > 0 and _pub and _ref - float(_pub) > _max_h * 3600 \
                and r["id"] not in retry_ids:
            logger.info("[brief] 게시 %.0fh 경과 — 제외: %s", (_ref - float(_pub)) / 3600,
                        r.get("symbol"))
            continue
        sym = r.get("symbol") or ""
        # 시총 순위 게이트 (2026-10-09 대표 요청 "시총 200위 안쪽만") — 수집 단계(news_brief)와
        # 같은 판정. 게이트 신설 전에 적재된 큐 잔존분·순위가 밀려난 코인을 여기서 거른다.
        # 판정한 후보라 소비는 위에서 처리됐다(본문 메시지와 함께 소비 — build_brief_messages).
        if not nb.mcap_rank_ok(sym):
            logger.info("[brief] 시총 순위 %s위 밖 — 제외: %s", settings.get("news_max_mcap_rank"), sym)
            continue
        summary = r.get("summary") or ""
        en = (r.get("summary_en") or "") if structured else ""
        if structured and not en:
            # v2 가 켜진 뒤 원문(summary_en) 없는 **레거시 큐 행**은 싣지 않고 소비만 한다
            # (RV2-N8). 종전 경로(_first_sentence)는 번역문을 55자에서 "…"로 잘라 실어
            # "…·중간 절단 금지" 계약을 어긴다(09-28 브리핑 XRP/17290 재적재 행).
            logger.info("[brief] 원문 없는 레거시 행 — 소비만: %s (id=%s)", sym, r["id"])
            continue
        if _is_queued_noise(sym, summary, en):
            logger.info("[brief] 큐 노이즈 제외: %s (%s)", sym, (en or summary)[:40])
            continue
        if en:
            if sym == news_parse.MARKET_SYMBOL and not market_ok:
                continue
            p = news_parse.parse(en)
            if p.get("kind") == "noise":
                logger.info("[brief] 원문 노이즈 제외: %s (%s)", sym, p.get("why"))
                continue
            if sym == news_parse.MARKET_SYMBOL and not news_parse.is_market_news(p):
                # 🌐 시장 항목은 확인된 시장 사실만(RV2-N3·N5) — 적재 뒤 판정 규칙이 바뀌어
                # 가정·예상 기사로 풀린 옛 MARKET 행은 코인이 없어 실을 자리가 없다.
                logger.info("[brief] 🌐 시장 사실 아님 — 제외: %s", p.get("title", "")[:40])
                continue
            if compact and _compact_empty(_compact_comp(p, sym, r, {})):
                # 내용 없는 의견 — 블록 상한 자리를 차지하지 않게 후보 단계에서 거른다(리뷰 10-09).
                logger.info("[brief] 내용 없는 의견 — 제외: %s (%s)", sym, p.get("title", "")[:40])
                continue
            tier = news_parse.compose(p, sym, en, "", {}, n_sent)["tier"]
            fresh = r.get("posted_at") or r.get("created_at") or 0
            cands.append({"row": r, "v2": True, "parsed": p, "key": (tier, 0, -fresh)})
            continue
        if sym == news_parse.MARKET_SYMBOL:
            continue            # 🌐 항목은 원문 기반 렌더 전용(번역문 경로엔 코인이 없다)
        summ = _first_sentence(summary)
        if not summ:
            continue            # 정제 후 남는 게 없으면 실을 가치도 없다
        score = _news_importance(summary)
        cands.append({"row": r, "v2": False, "summ": summ,
                      "key": (news_parse.TIER_LEGACY, -score, -(r.get("created_at") or 0))})

    if not cands:
        return None

    # 같은 코인은 최상위 1건만 (실측 XRP·BTC 가 같은 CLARITY 법안 뉴스로 중복).
    # 🌐 시장 항목은 코인이 아니라 서로 다른 사건이라 합치지 않는다.
    # 종전 경로(v2 아님)는 동점 시 **먼저 본 것**이 남는 종전 규칙을 지킨다.
    best_by_symbol: dict = {}
    keep = []
    for c in cands:
        sym = str(c["row"].get("symbol") or "").upper()
        if sym == news_parse.MARKET_SYMBOL:
            keep.append(c)
            continue
        cur = best_by_symbol.get(sym)
        ck = c["key"] if c["v2"] else c["key"][:2]
        if cur is None or ck < (cur["key"] if cur["v2"] else cur["key"][:2]):
            best_by_symbol[sym] = c
    deduped = list(best_by_symbol.values()) + keep

    # 등급 → 최신순(1차 정렬) → 선택 중 **이미 뽑힌 채널과 다른 채널 우선**(그리디).
    pool = sorted(deduped, key=lambda c: c["key"])
    picked, used_channels = [], set()
    while pool and len(picked) < _NEWS_BLOCK_MAX:
        top = pool[0]["key"][:2]
        tier_grp = [c for c in pool if c["key"][:2] == top]
        novel = [c for c in tier_grp if (c["row"].get("channel") or "") not in used_channels]
        chosen = (novel or tier_grp)[0]
        picked.append(chosen)
        used_channels.add(chosen["row"].get("channel") or "")
        pool.remove(chosen)

    ctx_map = {}
    v2_syms = [c["row"].get("symbol") for c in picked if c["v2"]]
    if v2_syms:
        try:
            ctx_map = _price_ctx(v2_syms, timeout, kimchi)
        except Exception as e:  # noqa: BLE001 - 맥락 줄 생략으로 강등
            logger.warning("[brief] 뉴스 가격 맥락 실패: %s", e)

    ranks = {}
    if compact:
        # 레이아웃 C(10-09 대표 결정): **어떤 항목을 실을지**는 위 중요도 선택이 정하고, 고른 뒤의
        # **표시 순서만** 바꾼다: 🌐 시장(전체 공통) 맨 위(10-10 대표 수정) → 코인 시총 순위 오름차순
        # (1위 먼저) → 순위 모름(fail-open). 같은 그룹 안에서는 선택 순서(중요도)를 유지한다.
        ranks = nb.load_mcap_ranks() or {}

        def _disp_key(ic):
            i, c = ic
            s = str(c["row"].get("symbol") or "").upper()
            if s == news_parse.MARKET_SYMBOL:
                return (0, 0, i)
            rk = ranks.get(s)
            return (1, rk, i) if rk else (2, 0, i)

        picked = [c for _i, c in sorted(enumerate(picked), key=_disp_key)]

    items = []
    for c in picked:
        r = c["row"]
        sym = str(r.get("symbol") or "?")
        if c["v2"] and compact:
            # 압축 항목(2026-10-09 레이아웃 C) — 4~5줄. [] 면 아래 종전 긴 형식으로 강등.
            lines = _compact_item_lines(c["parsed"], sym, r, _item_ctx(ctx_map, sym, r, now),
                                        rank=ranks.get(sym.upper()))
            if lines is None:
                continue   # 내용 없는 의견 — 건너뜀(소비는 본문 메시지와 함께)
            if lines:
                items.append((lines, r["id"]))
                continue
        lines = [_item_header(sym, str(r.get("channel") or ""))]
        if c["v2"]:
            comp = news_parse.compose(c["parsed"], sym, r.get("summary_en") or "",
                                      r.get("summary") or "", _item_ctx(ctx_map, sym, r, now), n_sent)
            lines += _wrap_escaped(comp["summary"], segments=True)
            if comp["detail"]:
                lines += _wrap_escaped(" ".join(comp["detail"]))
            if comp["context"]:
                lines += _wrap_escaped(comp["context"], segments=True, reorder=True)
        else:
            lines += _wrap_escaped(c["summ"])
        items.append((lines, r["id"]))

    # "외 N건" 은 **아직 안 본 잔여분** 기준. 위에서 소비한 건 이미 처리된 것이라
    # 세면 안 된다(노이즈·컷 탈락까지 '남았다'고 표시되면 숫자가 거짓이 된다).
    if not items:
        # 뽑힌 항목이 전부 건너뛰어졌으면 블록 없음(None) — 호출부가 "📰 새 뉴스 없음" 안내를
        # 붙이고 판정 id 는 본문 메시지와 함께 소비한다(리뷰 10-09: (헤더, []) 면 안내도 뉴스도 없었다).
        return None
    remain = max(0, db.count_news_digest(conn) - len(consumed_ids))
    head = "📰 <b>주요 뉴스</b>"
    if compact:
        # 레이아웃 C — 표시 순서가 시총순임을 헤더에 밝힌다(잔여 건수는 같은 괄호에).
        head += f" (시총순 · 외 {remain}건)" if remain else " (시총순)"
    elif remain:
        head += f" (외 {remain}건)"
    return head, items


def _news_lines(conn, consumed_ids: list, kimchi=None, timeout: float = 5.0) -> list:
    """"📰 주요 뉴스" 블록 줄 목록(헤더 + 항목들). 없으면 빈 리스트(블록 생략).
    _news_items 의 평탄화 래퍼 — 종전 호출부·테스트와 같은 반환 형태."""
    got = _news_items(conn, consumed_ids, kimchi=kimchi, timeout=timeout)
    if not got:
        return []
    head, items = got
    out = [head]
    for lines, _rid in items:
        out.extend(lines)
    return out


def _fit_telegram(lines: list, news_start: int, item_lens: list = None) -> list:
    """전체 길이가 텔레그램 한도를 넘으면 뉴스 줄부터 줄인다 (2026-09-13 A안).

    news_start: lines 안에서 뉴스 블록이 시작하는 인덱스(-1 이면 뉴스 없음).
    뉴스를 먼저 줄이는 이유: 시장환경·TP 도달은 하루 한 번뿐인 확정 정보인데
    뉴스는 원문이 채널에 그대로 남아 있어 손실이 가장 작다. 뉴스를 다 걷어내도
    한도를 넘으면 마지막 수단으로 통째 절단한다(발송 실패보다 낫다).

    2026-09-14 수리(감사 F1): **입력을 제자리 변형하지 않는다.** 종전엔 인자
    리스트에 직접 pop() 을 걸고 같은 객체를 돌려줬는데, 호출부가 반환값과 원본의
    길이를 비교해 "잘렸는가"를 판정하고 있어서 두 값이 언제나 같아 그 판정이
    영구히 False 였다(= 안 실린 뉴스가 소비 처리돼 영구 소실). 복사해서 다루면
    호출부가 어떤 방식으로 비교하든 안전하다."""
    lines = list(lines)
    if sum(len(x) + 1 for x in lines) <= _TELEGRAM_MAX_CHARS:
        return lines
    if news_start >= 0:
        trimmed = False
        while len(lines) > news_start + 1 and \
                sum(len(x) + 1 for x in lines) > _TELEGRAM_MAX_CHARS:
            lines.pop()          # 뉴스 블록 끝줄부터 제거
            trimmed = True
        # 요약 줄만 잘리고 항목 헤더("   <b>SYM</b> · @ch")가 남으면 그 뉴스는
        # 코인 이름만 덩그러니 실린다 — 내용을 못 봤는데 소비 처리돼 다시는 안
        # 나온다(호출부는 헤더 줄 수로 '실린 건수'를 센다). 그런 반쪽 항목은
        # 통째로 빼서 다음 브리핑에 온전히 나오게 한다. 잘림이 실제로 일어난
        # 경우에만 적용한다 — 원래 요약이 비어 헤더만 있는 정상 항목은 건드리지
        # 않는다. (2026-09-14 감사 F1 후속)
        if trimmed and item_lens:
            # 항목 구조가 있으면(10-09) **항목 경계**까지 되돌린다 — 압축 항목은 머리줄 뒤에도
            # 하위 줄이 여럿이라, 머리줄 문자열만 보면 반쪽 항목이 남을 수 있다.
            end, keep_to = news_start + 1, news_start + 1
            for n in item_lens:
                end += n
                if end <= len(lines):
                    keep_to = end
            lines = lines[:keep_to]
        elif trimmed:
            while len(lines) > news_start + 1 and _is_item_head(lines[-1]):
                lines.pop()
        if len(lines) == news_start + 1:
            # 헤더만 남으면 블록 통째 제거. 헤더 **앞의 구분선**까지 걷어낸다 —
            # 안 그러면 본문 없는 ━━━ 한 줄이 브리핑 끝에 덩그러니 남는다
            # (2026-09-14 감사 F6). news_start 는 호출부가 _SEP 를 넣은 직후의
            # 인덱스라 news_start-1 이 그 구분선이다.
            cut = news_start
            if cut > 0 and lines[cut - 1] == _SEP:
                cut -= 1
            lines = lines[:cut]
        if sum(len(x) + 1 for x in lines) <= _TELEGRAM_MAX_CHARS:
            return lines
    text = "\n".join(lines)
    if len(text) > _TELEGRAM_MAX_CHARS:
        logger.warning("[brief] 길이 초과 %d자 — 절단", len(text))
        return text[:_TELEGRAM_MAX_CHARS].split("\n")
    return lines


# ── 렌더링 ──────────────────────────────────────────────────────────


def _assemble(conn, now: float, timeout: float, consumed_ids: list) -> tuple:
    """브리핑 줄 조립(2026-09-27 build_brief 에서 분리). 반환 (lines, news_start, items).

    items = [(항목 줄 목록, 큐 id), ...] — 분할 발송이 항목 경계와 소비 id 를 정확히
    맞추려고 필요하다. consumed_ids 에는 **판정한 후보 전부**가 들어간다(09-22 계약).
    어떤 데이터가 죽어도 줄은 항상 나온다. 한 화면 상한(~15행) — 행 추가 시 기존 행
    삭제를 먼저 검토할 것(뉴스 블록은 예외: 길어지면 두 번째 메시지로 분할)."""
    from monitor import market_sentiment, options
    from monitor import macro as macro_mod

    lines = [f"🌅 <b>모닝 브리핑</b> · {day_kst(now)}", _SEP]

    # BTC 현재가 + 김프 (표기·이모지 분기는 render_alert 와 동일)
    btc_krw, kimchi = _btc_block(timeout)
    if btc_krw:
        lines.append(f"₿ BTC {btc_krw:,.0f}원")
    if kimchi is not None:
        if abs(kimchi) < 0.01:
            lines.append(f"⚖️ 김프 거의 0% ({kimchi:+.3f}%)")
        elif kimchi > 0:
            lines.append(f"🌶️ 김프 {kimchi:+.2f}%")
        else:
            lines.append(f"❄️ 김프 {kimchi:+.2f}%")

    # 시장 심리 (1h 캐시) — 어휘는 2026-08-14 헤더 개편 표기를 따른다
    sent = None
    try:
        sent = market_sentiment.get_sentiment(conn)
    except Exception as e:  # noqa: BLE001 - 행 생략으로 강등
        logger.warning("[brief] 시장심리 실패: %s", e)
    if sent:
        fng = sent.get("fear_greed")
        if fng is not None:
            label = _FNG_KR.get(sent.get("fear_greed_label", ""),
                                sent.get("fear_greed_label", ""))
            # 2026-09-27 감사 D18: 값과 무관한 😨 고정 → 구간별 이모지(알림과 공용 함수)
            lines.append(f"{telegram.fng_emoji(fng)} 시장심리 {fng} ({label})")
        if sent.get("btc_dominance") is not None:
            lines.append(f"🌍 비트 점유율 {sent['btc_dominance']}%")
        if sent.get("eth_dominance") is not None:
            lines.append(f"💠 이더 점유율 {sent['eth_dominance']}%")
        if sent.get("usdt_dominance") is not None:
            lines.append(f"🪙 USDT 점유율 {sent['usdt_dominance']}%")
        alt_s = sent.get("altcoin_season_index")
        if alt_s is not None:
            if alt_s >= 75:
                alt_tag = "알트시즌"
            elif alt_s <= 25:
                alt_tag = "비트시즌"
            else:
                alt_tag = "중립"
            lines.append(f"🌊 알트시즌 {alt_s} ({alt_tag})")
        stable = sent.get("stablecoin_mcap_b")
        stable_chg = sent.get("stablecoin_mcap_change_7d_pct")
        if stable is not None:
            if stable_chg is not None:
                # '/7d' 는 텔레그램이 봇 명령어 링크(파란 글씨)로 바꿨다(09-28 대표 캡처).
                lines.append(f"💵 스테이블 {stable}B (7일 {stable_chg:+.2f}%)")
            else:
                lines.append(f"💵 스테이블 {stable}B")

    # 달러지수 (1h 캐시) — 강달러=위험자산 이탈, 약달러=위험자산 유리
    # 임계 100/105 는 최근 5년 분포 중앙~상위 근처(DXY 기준).
    try:
        dxy = macro_mod.fetch_dxy(conn, timeout)
        if dxy is not None:
            if dxy <= 100:
                dxy_tag = "매수 유리"
            elif dxy >= 105:
                dxy_tag = "매수 부담"
            else:
                dxy_tag = "중립"
            lines.append(f"💵 달러지수 {dxy:.2f} ({dxy_tag})")
    except Exception as e:  # noqa: BLE001 - 행 생략으로 강등
        logger.warning("[brief] DXY 실패: %s", e)

    # BTC 변동성 (5min 캐시)
    try:
        opt = options.fetch_btc_options_context(timeout)
        dvol = opt.get("dvol") if opt else None
        if dvol is not None:
            lines.append(f"📊 BTC변동성 {dvol:.0f}")
    except Exception as e:  # noqa: BLE001 - 행 생략으로 강등
        logger.warning("[brief] 옵션 컨텍스트 실패: %s", e)

    # VIX (S&P 500 변동성 지수, FRED 무료) — 저=평온·위험자산 유리, 고=공포
    # 임계 20/30 은 VIX 업계 관례(20 미만 저변동, 30 이상 고공포).
    try:
        vix = macro_mod.fetch_vix(conn, timeout)
        if vix is not None:
            if vix < 20:
                vix_tag = "매수 유리"
            elif vix >= 30:
                vix_tag = "매수 부담"
            else:
                vix_tag = "중립"
            lines.append(f"😱 VIX {vix:.1f} ({vix_tag})")
    except Exception as e:  # noqa: BLE001 - 행 생략으로 강등
        logger.warning("[brief] VIX 실패: %s", e)

    # 미 10년 국채 수익률 (FRED 무료) — 위험자산 밸류에이션 벤치마크
    # 라벨은 코인 관점 관례: 금리 낮으면 위험자산 유리, 높으면 안전자산 대체수단
    # 매력↑ → 코인 매수세 약화. 임계 3.5%/4.5%는 최근 5년 분포 중앙~상위 근처.
    # 라벨 "미국채 10Y"는 좁은 화면(모바일 32자 폭) 줄바꿈 회피용 압축 표기.
    try:
        y10 = macro_mod.fetch_ust_10y(conn, timeout)
        if y10 is not None:
            if y10 <= 3.5:
                y10_tag = "매수 유리"
            elif y10 >= 4.5:
                y10_tag = "매수 부담"
            else:
                y10_tag = "중립"
            lines.append(f"🏦 미국채 10Y {y10:.2f}% ({y10_tag})")
    except Exception as e:  # noqa: BLE001 - 행 생략으로 강등
        logger.warning("[brief] 10Y 국채 실패: %s", e)

    # 미국 증시 전일 등락 (1h 캐시, Yahoo Finance 무료)
    try:
        us = macro_mod.fetch_us_indices(conn, timeout)
        if us:
            sp = us.get("sp500")
            nq = us.get("nasdaq")
            if sp is not None:
                lines.append(f"🇺🇸 S&P500 {sp:+.2f}%")
            if nq is not None:
                lines.append(f"🇺🇸 나스닥 {nq:+.2f}%")
    except Exception as e:  # noqa: BLE001 - 행 생략으로 강등
        logger.warning("[brief] 미국 증시 실패: %s", e)

    # 워쳐 시장 전체 SL률 (최근 7일, 표본 5건 이상일 때만 표시)
    try:
        from collector import watcher_stats
        wd = watcher_stats.load_watcher_data(timeout)
        msl = wd.get("market_sl")
        if msl and msl.get("total", 0) >= 5 and msl.get("sl_rate") is not None:
            lines.append(f"📉 워쳐 SL률 {msl['sl_rate']*100:.0f}%"
                         f" (7일 {msl['misses']}/{msl['total']}건)")
    except Exception as e:  # noqa: BLE001 - 행 생략으로 강등
        logger.warning("[brief] 워쳐 SL률 실패: %s", e)

    # 매크로 이벤트 7일 예고 (전부 표시)
    try:
        ev_lines = _macro_event_lines(now, conn)
        if ev_lines:
            lines.extend(ev_lines)
    except Exception as e:  # noqa: BLE001 - 행 생략으로 강등
        logger.warning("[brief] 매크로 이벤트 실패: %s", e)

    # 어제 성과 + 대기 레벨 (로컬 DB 조회 — 실패 시 행 생략)
    yesterday = day_kst(now - 86400.0)
    tail = []
    try:
        n_touch = db.count_all_alerts_today(conn, yesterday, kind="touch")
        tail.append(f"🎯 어제 터치 알림 {n_touch}건")
    except Exception as e:  # noqa: BLE001 - 행 생략으로 강등
        logger.warning("[brief] 어제 성과 조회 실패: %s", e)
    # 🏁 목표 도달 (2026-09-13 A안) — TP 실시간 발송을 끈 대신 여기서 요약.
    # 순서: 어제 일어난 일(터치 → 목표 도달)을 먼저 묶고, 앞으로 볼 것(대기 레벨)을
    # 뒤에 둔다. 과거·현재가 뒤섞이면 읽는 순서가 끊긴다.
    try:
        tail.extend(_tp_hit_lines(conn, now))
    except Exception as e:  # noqa: BLE001 - 블록 생략으로 강등
        logger.warning("[brief] TP 도달 블록 실패: %s", e)
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n, COUNT(DISTINCT coin_symbol) AS c FROM levels "
            "WHERE status IN ('watching','previewed')").fetchone()
        if row is not None:
            tail.append(f"⏳ 대기 레벨 {row['n']}개 ({row['c']}개 코인)")
    except Exception as e:  # noqa: BLE001 - 행 생략으로 강등
        logger.warning("[brief] 대기 레벨 조회 실패: %s", e)
    if tail:
        lines.append(_SEP)
        lines.extend(tail)

    # 📰 주요 뉴스 (2026-09-13 A안) — 뉴스 실시간 발송을 끈 대신 여기서 요약.
    news_start = -1
    items: list = []
    try:
        got = _news_items(conn, consumed_ids, kimchi=kimchi, timeout=timeout, now=now)
        if got:
            head, items = got
            news_block = [head]
            for item_lines, _rid in items:
                news_block.extend(item_lines)
            lines.append(_SEP)
            news_start = len(lines)
            lines.extend(news_block)
    except Exception as e:  # noqa: BLE001 - 블록 생략으로 강등
        logger.warning("[brief] 뉴스 블록 실패: %s", e)
        del consumed_ids[:]   # 실려 나가지 않았으면 소비 처리도 안 한다
        items = []
    return lines, news_start, items


def build_brief(conn, now: float, timeout: float,
                consumed_ids: list = None) -> str:
    """텔레그램 HTML 브리핑 **한 통** 문자열(종전 계약 — 조회·테스트용).

    실제 발송(maybe_send_brief)은 2026-09-27 부터 build_brief_messages 를 쓴다 —
    한도를 넘으면 뉴스를 자르지 않고 두 번째 메시지로 나눈다. 이 함수는 한 통에
    담아야 하는 호출부를 위해 종전 _fit_telegram 절단 경로를 유지한다.

    consumed_ids (2026-09-13 A안): 리스트를 넘기면 브리핑에 실린 news_digest_queue
    행 id 를 채워 준다. 호출부가 **발송 성공 후** db.consume_news_digest 로 소비
    처리한다 — 실패 시 재시도에서 같은 뉴스가 다시 실리게(유실 방지)."""
    consumed_ids = consumed_ids if consumed_ids is not None else []
    lines, news_start, _items = _assemble(conn, now, timeout, consumed_ids)
    item_lens = [len(ls) for ls, _rid in _items]
    fitted = _fit_telegram(lines, news_start, item_lens)
    if consumed_ids:
        # 길이 방어로 뉴스 줄이 잘려 나갔으면 그만큼 소비 처리도 취소한다 —
        # 안 실린 뉴스를 consumed 로 찍으면 영원히 못 본다. 항목 헤더 줄
        # ("   <b>SYM</b>…") 수 = 실제로 실린 뉴스 건수.
        #
        # 2026-09-14 수리(감사 F1): 종전엔 `len(fitted) < len(lines)` 로 "잘렸는가"를
        # 먼저 물었는데, _fit_telegram 이 인자를 **제자리 변형**하고 같은 객체를
        # 돌려주므로 두 길이가 언제나 같아 이 가드가 **항상 False** 였다 — 즉 절단이
        # 실제로 일어나도 소비 취소가 한 번도 실행되지 않아 안 실린 뉴스가
        # consumed=1 로 영구 소실된다. 가드를 걷고 실린 줄을 무조건 세어 맞춘다
        # (잘리지 않았으면 kept == len(consumed_ids) 라 del 이 no-op).
        # 10-09: 항목 수는 (줄들, id) 구조로 센다 — 끝까지 온전히 남은 항목만(압축 항목은 머리줄
        # 뒤 하위 줄이 여럿이라 머리줄 문자열 세기로는 반쪽 항목도 '실림'이 된다).
        kept = 0
        if news_start >= 0:
            end = news_start + 1
            for n in item_lens:
                end += n
                if end > len(fitted):
                    break
                kept += 1
        del consumed_ids[kept:]
    return "\n".join(fitted)


def _tg_len(text: str) -> int:
    """텔레그램 길이 단위(UTF-16 코드 유닛). 이모지 🟢·🌐 는 2로 센다 — len() 은 1로
    세서 이모지가 많은 v2 항목에서 한도를 과소평가한다. HTML 태그까지 세므로 보수적."""
    return len((text or "").encode("utf-16-le")) // 2


def build_brief_messages(conn, now: float, timeout: float) -> list:
    """발송 단위 메시지 목록 [(text, consume_ids), ...] (2026-09-27 사용자 요청).

    "브리핑이 길어져도 되니" — 뉴스 설명이 길어지면서 한 통 한도(_TELEGRAM_MAX_CHARS)를
    넘을 수 있다. 그때 **뉴스를 자르지 않고** 뉴스 블록을 두 번째 메시지로 나눈다
    (09-14 사고: 잘린 뉴스가 소비 처리돼 영구 소실). 뉴스 블록 자체도 한도를 넘으면
    **항목 경계**에서 세 번째 이후 메시지로 더 나눈다 — 항목 중간 절단은 없다.

    소비 계약: 각 메시지의 consume_ids 는 **그 메시지가 발송에 성공했을 때만** 소비할
    id 다. 실린 항목의 id 는 그 항목이 들어간 메시지에, 판정만 하고 싣지 않은 후보
    (노이즈·중복·48h 초과·상한 컷)의 id 는 **본문(첫) 메시지**에 붙는다(09-29 — 뉴스 메시지 실패 시
    재시도 목록의 신선도 면제로 이미 걸러진 글이 실리던 문제 방지).
    시장환경 본문이 단독으로 한도를 넘는 극단은 종전 _fit_telegram 절단(마지막 수단)."""
    consumed_ids: list = []
    lines, news_start, items = _assemble(conn, now, timeout, consumed_ids)
    whole = "\n".join(lines)
    if news_start < 0:
        # 뉴스 블록이 없어도 **판정한 후보**(원문 없는 레거시·48h 초과·노이즈)는 본문과 함께
        # 소비한다(RV2-N8 "싣지 않고 소비만"). 종전엔 [] 로 버려 이런 행이 큐 머리에 영구히
        # 남았다 — 꺼낼 수 있는 15건이 전부 그런 행이면 새 뉴스가 영영 안 꺼내진다.
        # 뉴스 블록 조립이 예외로 죽은 경우는 _assemble 이 consumed_ids 를 비운다.
        # 2026-09-29 대표 결정: 뉴스 0건인 날은 첫 메시지 끝에 한 줄 안내 — 두 번째 메시지가
        # 없는 게 오류로 보이지 않게(09-29 "왜 뉴스 메시지는 안 와?").
        lines = list(lines) + [_SEP, _NO_NEWS_LINE]
        return [("\n".join(_fit_telegram(lines, -1)), list(consumed_ids))]
    # 2026-09-28 대표 요청: 뉴스는 **항상** 두 번째 메시지로 뺀다(한 통에 들어가도) — 시장환경
    # 요약과 코인별 뉴스를 한 말풍선에 몰지 않고, 뉴스 항목 수를 늘려도 첫 통이 길어지지 않게.
    # 분할: 본문(뉴스 앞 구분선까지 제외) + 뉴스 메시지(들)
    cut = news_start
    if cut > 0 and lines[cut - 1] == _SEP:
        cut -= 1
    main_lines = lines[:cut]
    if _tg_len("\n".join(main_lines)) > _TELEGRAM_MAX_CHARS:
        main_lines = _fit_telegram(main_lines, -1)
    msgs = [("\n".join(main_lines), [])]

    head = lines[news_start]
    shown_ids = {rid for _l, rid in items}
    extra_ids = [i for i in consumed_ids if i not in shown_ids]
    cont_head = "📰 <b>주요 뉴스</b> (이어서)"
    cur_lines, cur_ids = [head], []
    for item_lines, rid in items:
        # 항목 사이 빈 줄(09-28 대표 요청 "시각적으로 눈에 잘 들어오게") — 코인 경계가 보이게.
        cand = cur_lines + ([""] if cur_ids else []) + item_lines
        if cur_ids and _tg_len("\n".join(cand)) > _TELEGRAM_MAX_CHARS:
            msgs.append(("\n".join(cur_lines), cur_ids))
            cur_lines, cur_ids = [cont_head] + list(item_lines), [rid]
        else:
            cur_lines, cur_ids = cand, cur_ids + [rid]
    if cur_ids:
        msgs.append(("\n".join(cur_lines), cur_ids))
    # 판정만 한 후보 id 는 첫 뉴스 메시지에 붙인다.
    # 판정만 하고 싣지 않은 후보(48h 초과·노이즈·레거시)는 **본문 메시지**에 붙인다(09-29 리뷰).
    # 종전엔 첫 뉴스 메시지에 붙어, 그 메시지가 실패하면 재시도 목록(신선도 면제)에 들어가
    # 이미 '오래됨'으로 걸러진 글이 다음 날 실렸다. 싣지 않을 글이라 본문과 함께 소비해도 된다.
    t0, ids0 = msgs[0]
    msgs[0] = (t0, ids0 + extra_ids)
    for t, _ids in msgs:
        if _tg_len(t) > 4096:
            # 항목 1건이 4096 을 넘는 일은 설계상 없다(설명 3문장). 넘으면 텔레그램
            # send 의 구분선 분할이 받아주지만, 로그로 남겨 원인을 본다.
            logger.warning("[brief] 분할 메시지가 4096 초과(%d) — 항목 길이 점검 필요", _tg_len(t))
    return msgs


# ── 회차 훅 (run_cycle 의 maybe_* 패턴) ─────────────────────────────


def maybe_send_brief(db_path: str, now: float = None) -> str:
    """조건이 맞으면 모닝 브리핑 발송(보통 1통, 뉴스가 길면 2통 이상 — 2026-09-27). 반환 "skipped" | "ok" | "failed".
    어떤 실패도 예외를 밖으로 던지지 않는다(가격체크 보호 — maybe_collect 동일).

    날짜 마킹은 **발송 성공 후에만** 한다(모듈 docstring 참고) — 실패 시 다음
    회차가 창 안에서 재시도하고, 창(hour_to)을 넘기면 그날은 자연 생략된다."""
    now = time.time() if now is None else now

    try:
        with db.connect(db_path) as conn:
            due, reason = brief_due(conn, now)
            if not due:
                logger.debug("모닝 브리핑 생략: %s", reason)
                return "skipped"

            logger.info("모닝 브리핑 발송: %s", reason)
            # 2026-09-27: 한도 초과 시 뉴스를 자르지 않고 메시지를 나눈다
            # (build_brief_messages docstring). 메시지마다 소비할 id 가 따로 있다.
            msgs = build_brief_messages(conn, now, settings.get("http_timeout_sec"))
            text = msgs[0][0]
    except BaseException as e:  # noqa: BLE001 - 브리핑 실패가 회차를 죽이면 안 된다
        if isinstance(e, (KeyboardInterrupt, SystemExit)):
            raise
        logger.error("모닝 브리핑 판정/조립 실패: %s: %s", type(e).__name__, e, exc_info=True)
        print(f"::warning::모닝 브리핑 판정/조립 실패 - {type(e).__name__}")
        return "failed"

    try:
        sent = telegram.send(text)
    except BaseException as e:  # noqa: BLE001 - 발송 실패가 회차를 죽이면 안 된다
        if isinstance(e, (KeyboardInterrupt, SystemExit)):
            raise
        logger.error("모닝 브리핑 발송 예외: %s: %s", type(e).__name__, e, exc_info=True)
        sent = False
    if not sent:
        print("::warning::모닝 브리핑 발송 실패 - 다음 회차 재시도(창 내)")
        return "failed"

    # 첫 통(시장환경 본문)이 나갔으면 그날 브리핑은 '발송됨'이다. 이어지는 뉴스
    # 메시지는 **각자 성공한 것만** 소비 처리한다 — 실패한 메시지의 뉴스는 큐에
    # 남아 다음 브리핑에 다시 실린다(09-14 사고: 안 실린 뉴스의 영구 소실 방지).
    consumed_ids: list = list(msgs[0][1])
    failed_ids: list = []
    for extra_text, extra_ids in msgs[1:]:
        try:
            ok = telegram.send(extra_text)
        except BaseException as e:  # noqa: BLE001 - 발송 실패가 회차를 죽이면 안 된다
            if isinstance(e, (KeyboardInterrupt, SystemExit)):
                raise
            logger.error("모닝 브리핑 뉴스 메시지 발송 예외: %s: %s", type(e).__name__, e)
            ok = False
        if ok:
            consumed_ids.extend(extra_ids)
        else:
            failed_ids.extend(extra_ids)
            logger.warning("[brief] 뉴스 분할 메시지 발송 실패 — %d건 미소비(다음 브리핑 재시도)",
                           len(extra_ids))
            print("::warning::모닝 브리핑 뉴스 분할 메시지 발송 실패 - 해당 뉴스는 다음 브리핑으로")

    try:
        with db.connect(db_path) as conn:
            db.set_meta(conn, META_LAST_BRIEF_DATE, day_kst(now))
            # 다음 브리핑의 🏁 조회 창 시작점 (2026-09-14 수리). date 와 함께 찍어야
            # 다음 회차가 "이 시각 이후 적중"만 실어 누락·중복을 둘 다 피한다.
            db.set_meta(conn, META_LAST_BRIEF_AT, str(now))
            # 뉴스 큐 소비는 **발송 성공 후**에만 (2026-09-13 A안) — 실패 시
            # 다음 회차 재시도가 같은 뉴스를 다시 싣는다.
            if consumed_ids:
                db.consume_news_digest(conn, consumed_ids)
            # 뉴스 메시지 실패분은 다음 브리핑에서 신선도 가드 면제(RV2-N7). 이번 회차에
            # 다시 실려 성공한 id 는 빠지고, 또 실패하면 다시 남는다(덮어쓰기).
            db.set_meta(conn, META_NEWS_RETRY_IDS, json.dumps(sorted(set(failed_ids))))
    except BaseException as e:  # noqa: BLE001 - meta 기록 실패로 회차를 죽이면 안 된다
        if isinstance(e, (KeyboardInterrupt, SystemExit)):
            raise
        logger.error("모닝 브리핑 meta 기록 실패(발송은 완료): %s: %s",
                     type(e).__name__, e, exc_info=True)
        print(f"::warning::모닝 브리핑 meta 기록 실패 - {type(e).__name__}")

    return "ok"
