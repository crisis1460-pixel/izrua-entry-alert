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
import logging
import re
import time
import unicodedata
from datetime import datetime

from config import settings
from notify import telegram
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

# 매크로 이벤트 예고 범위(일). get_nearby_macro_event 는 24h 창이라 브리핑용
# 7일 예고는 get_macro_events(conn) 자동 캘린더를 사용한다.
_MACRO_LOOKAHEAD_DAYS = 7

# ── 알림량 A안 (2026-09-13) 브리핑 흡수 블록 상한 ────────────────────
# 실시간 발송을 끈 TP 적중·뉴스를 다음 날 아침 브리핑 1통이 대신 전달한다.
_TP_BLOCK_MAX_LINES = 8      # 🏁 목표 도달 — 초과분은 "외 N건"
_NEWS_BLOCK_MAX = 5          # 📰 주요 뉴스 — 뉴스 상한(5/일)과 동수
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
            if kst_part and _display_width(base + kst_part) > _MACRO_LINE_MAX_W:
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
_BULL_RX = re.compile(r"(?:황소|불|강세)\s*케이스\s*[:：]\s*([\d,]+(?:\.\d+)?)|"
                      r"bull\s*case\s*[:：]\s*([\d,]+(?:\.\d+)?)", re.I)
_BEAR_RX = re.compile(r"(?:베어|곰|약세)\s*케이스\s*[:：]\s*([\d,]+(?:\.\d+)?)|"
                      r"bear\s*case\s*[:：]\s*([\d,]+(?:\.\d+)?)", re.I)


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
    return f"↑ {up} 위 강세 · ↓ {dn} 아래 약세"


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


def _catalyst_sentence(body: str, max_chars: int) -> str:
    """본문에서 '살 이유'에 가장 가까운 문장 1개. 근거가 없으면 "".

    동점이면 **앞선 문장**이 이긴다(원문의 두괄식 의도를 존중). 뽑은 문장이
    길면 호출부가 자르되, 자르기 전에 문장 자체가 핵심이라는 점은 지켜진다."""
    best, best_score = "", 0
    for raw in _SENT_SPLIT_RX.split(body):
        s = raw.strip()
        if len(s) < 8:                      # 조각·머리말은 후보 제외
            continue
        score = sum(pts for pts, rx in _CATALYST_PATTERNS if rx.search(s))
        if score > best_score:
            best, best_score = s, score
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


def _news_lines(conn, consumed_ids: list) -> list:
    """"📰 주요 뉴스" 블록 (2026-09-13 A안). 없으면 빈 리스트(블록 생략).

    news_alert_send_enabled=False 로 실시간 발송을 끈 뉴스를 news_digest_queue
    에서 최대 5건 꺼내 요약 1~2줄로 전달한다. 원문 링크는 생략(브리핑 길이 제한).
    소비 처리(consumed=1)는 **발송 성공 후** maybe_send_brief 가 한다 — 여기서
    바로 찍으면 발송 실패 시 그날 뉴스가 통째로 증발한다. 그래서 id 만 모아
    호출부에 넘긴다.

    2026-09-14 수리 — 날짜 인자를 없앴다. 종전엔 `day_kst='어제'` 로 걸러서 오늘
    새벽에 쌓인 뉴스가 당일 브리핑에서 빠지고 다음 날에야 나갔다(실측: 배포
    다음 날 브리핑의 뉴스 0건, 큐에는 5건이 '오늘' 날짜로 대기). consumed 플래그가
    이미 중복을 막으므로 "아직 안 보여준 것을 오래된 순으로"면 충분하다."""
    rows = db.get_news_digest(conn, limit=_NEWS_BLOCK_MAX)
    if not rows:
        return []
    total = db.count_news_digest(conn)
    head = "📰 <b>주요 뉴스</b>"
    if total > len(rows):
        head += f" (외 {total - len(rows)}건)"
    lines = [head]
    for r in rows:
        consumed_ids.append(r["id"])
        sym = html.escape(str(r.get("symbol") or "?"))
        ch = html.escape(str(r.get("channel") or ""))
        summ = html.escape(_first_sentence(r.get("summary") or ""))
        ch_part = f" · @{ch}" if ch else ""
        lines.append(f"   <b>{sym}</b>{ch_part}")
        if summ:
            lines.append(f"   {summ}")
    return lines


def _fit_telegram(lines: list, news_start: int) -> list:
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
        if trimmed:
            while len(lines) > news_start + 1 and lines[-1].startswith("   <b>"):
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


def build_brief(conn, now: float, timeout: float,
                consumed_ids: list = None) -> str:
    """텔레그램 HTML 브리핑 조립. 어떤 데이터가 죽어도 문자열은 항상 나온다.
    한 화면 상한(~15행) — 행 추가 시 기존 행 삭제를 먼저 검토할 것.

    consumed_ids (2026-09-13 A안): 리스트를 넘기면 브리핑에 실린 news_digest_queue
    행 id 를 채워 준다. 호출부가 **발송 성공 후** db.consume_news_digest 로 소비
    처리한다 — 실패 시 재시도에서 같은 뉴스가 다시 실리게(유실 방지)."""
    consumed_ids = consumed_ids if consumed_ids is not None else []
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
            lines.append(f"😨 시장심리 {fng} ({label})")
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
                lines.append(f"💵 스테이블 {stable}B ({stable_chg:+.2f}%/7d)")
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
    try:
        news = _news_lines(conn, consumed_ids)
        if news:
            lines.append(_SEP)
            news_start = len(lines)
            lines.extend(news)
    except Exception as e:  # noqa: BLE001 - 블록 생략으로 강등
        logger.warning("[brief] 뉴스 블록 실패: %s", e)
        del consumed_ids[:]   # 실려 나가지 않았으면 소비 처리도 안 한다

    fitted = _fit_telegram(lines, news_start)
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
        kept = sum(1 for x in fitted[news_start:] if x.startswith("   <b>")) \
            if news_start >= 0 else 0
        del consumed_ids[kept:]
    return "\n".join(fitted)


# ── 회차 훅 (run_cycle 의 maybe_* 패턴) ─────────────────────────────


def maybe_send_brief(db_path: str, now: float = None) -> str:
    """조건이 맞으면 모닝 브리핑 1통 발송. 반환 "skipped" | "ok" | "failed".
    어떤 실패도 예외를 밖으로 던지지 않는다(가격체크 보호 — maybe_collect 동일).

    날짜 마킹은 **발송 성공 후에만** 한다(모듈 docstring 참고) — 실패 시 다음
    회차가 창 안에서 재시도하고, 창(hour_to)을 넘기면 그날은 자연 생략된다."""
    now = time.time() if now is None else now
    consumed_ids: list = []

    try:
        with db.connect(db_path) as conn:
            due, reason = brief_due(conn, now)
            if not due:
                logger.debug("모닝 브리핑 생략: %s", reason)
                return "skipped"

            logger.info("모닝 브리핑 발송: %s", reason)
            text = build_brief(conn, now, settings.get("http_timeout_sec"),
                               consumed_ids)
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
    except BaseException as e:  # noqa: BLE001 - meta 기록 실패로 회차를 죽이면 안 된다
        if isinstance(e, (KeyboardInterrupt, SystemExit)):
            raise
        logger.error("모닝 브리핑 meta 기록 실패(발송은 완료): %s: %s",
                     type(e).__name__, e, exc_info=True)
        print(f"::warning::모닝 브리핑 meta 기록 실패 - {type(e).__name__}")

    return "ok"
