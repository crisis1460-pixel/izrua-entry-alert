"""
텔레그램 알림 — izrua_watcher notifier.py 양식 이식판 (2026-07-23 사용자 지시).

워쳐에서 그대로 가져온 것:
- ━━━ 구분선으로 섹션 나누기, parse_mode=HTML
- 타점 블록: 4칸 들여쓰기, "$USD (KRW원)" 병기, 손절/목표에 엔트리 대비 ±%
- KRW 는 1원 이상이면 반올림 정수(콤마), 1원 미만 소수 유지 (사용자 확정: 원단위 반올림)
- 작성자 평균 적중률 라인 (📊 ... % (총 N건 기반)), 화이트리스트 ⭐⭐
- 🌍 BTC.D / 🪙 ALT.S(매수 라벨) / 😨 F&G(한국어 라벨) 행
- 출처는 URL 노출 없이 <a>출처1</a> · <a>출처2</a> 하이퍼링크

발송 원칙: 실패는 삼키고 로그만 (알림 실패가 잡을 막으면 안 됨). 토큰은 env 전용.
"""

import html
import logging

import requests

from config import settings

logger = logging.getLogger("alert.telegram")

_API = "https://api.telegram.org/bot{token}/sendMessage"
_SEP = "━━━━━━━━━━━━━━━━━━━━"

_FNG_KR = {
    "Extreme Fear": "극공포",
    "Fear": "공포",
    "Neutral": "중립",
    "Greed": "탐욕",
    "Extreme Greed": "극탐욕",
}


def send(text: str) -> bool:
    """HTML 모드 발송. 성공 True. 토큰 미설정/실패 시 False (예외 없음)."""
    token = settings.secret("TELEGRAM_BOT_TOKEN")
    chat_id = settings.secret("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        logger.warning("[tg] 토큰/chat_id 미설정 - 발송 생략 (내용 %d자)", len(text))
        return False
    try:
        resp = requests.post(
            _API.format(token=token),
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": True},
            timeout=10,
        )
        if resp.status_code != 200:
            logger.error("[tg] 발송 실패 status=%s body=%s", resp.status_code, resp.text[:200])
            return False
        return True
    except Exception as e:  # noqa: BLE001 - 알림 실패가 잡을 막으면 안 됨
        logger.error("[tg] 발송 실패: %s", e)
        return False


# ── 포맷 유틸 (워쳐 notifier.py 이식) ─────────────────────────────

def _fmt_usd(value) -> str:
    """가격대별 소수 자릿수 (워쳐 _format_price 동일)."""
    if value is None or value == 0:
        return "N/A"
    v = abs(value)
    if v >= 100:
        return f"{value:,.2f}"
    if v >= 1:
        return f"{value:,.4f}"
    if v >= 0.01:
        return f"{value:.5f}"
    if v >= 0.0001:
        return f"{value:.7f}"
    return f"{value:.9f}"


def _fmt_krw_paren(usd_value, usdt_krw) -> str:
    """'(1,234원)' — 1원 이상은 반올림 정수(사용자 확정), 1원 미만은 소수 4자리."""
    if not usd_value or not usdt_krw:
        return ""
    krw = usd_value * usdt_krw
    if krw >= 1:
        return f"({krw:,.0f}원)"
    return f"({krw:.4f}원)"


def _fmt_age(minutes) -> str:
    if minutes is None or minutes < 0:
        return ""
    if minutes < 60:
        return f"{minutes:.0f}분 전"
    if minutes < 1440:
        return f"{minutes // 60:.0f}시간 전"
    return f"{minutes // 1440:.0f}일 전"


def _fmt_followers(count) -> str:
    if not count or count <= 0:
        return ""
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    if count >= 1_000:
        return f"{count / 1_000:.1f}K"
    return str(int(count))


def _author_block(rep: dict) -> list:
    """작성자 라인 + 적중률 라인 (워쳐 스타일). 적중률 없으면 팔로워로 대체."""
    author = html.escape(rep.get("author") or "?")
    star = " ⭐⭐" if rep.get("author_whitelisted") else ""
    lines = [f"작성자: @{author}{star}"]
    hit_rate, hit_count = rep.get("author_hit_rate"), rep.get("author_hit_count")
    if hit_rate is not None and hit_count:
        lines.append(f"📊 작성자 평균 적중률: {hit_rate * 100:.0f}% (총 {hit_count}건 기반)")
    elif rep.get("author_followers"):
        lines.append(f"👥 팔로워 {_fmt_followers(rep['author_followers'])} · 적중률 기록없음")
    else:
        lines.append("👥 적중률 기록없음 (워쳐 미추적 작성자)")
    return lines


def render_alert(kind: str, coin_symbol: str, cluster: list, current_krw: float,
                 usdt_krw: float, sentiment: dict = None) -> str:
    """kind: 'touch'|'preview'. cluster: 같은 코인 ±1% 레벨 dict 목록(entry 내림차순).
    sentiment: {btc_dominance, fear_greed, fear_greed_label, altcoin_season_index}|None"""
    rep = max(cluster, key=lambda l: l.get("score") or 0)
    current_usd = (current_krw / usdt_krw) if (current_krw and usdt_krw) else None

    entries = [lv["entry_usd"] for lv in cluster if lv.get("entry_usd")]
    lo, hi = (min(entries), max(entries)) if entries else (None, None)
    entry_rep = hi  # 트리거 기준 = 클러스터 상단

    tier = rep.get("mcap_tier_icon") or ""
    rank = f"시총 {rep['mcap_rank']}위" if rep.get("mcap_rank") else ""
    kind_kr = "🎯 <b>엔트리 터치</b>" if kind == "touch" else "⚠️ <b>엔트리 접근</b>"
    grade = f"{rep['grade']}등급" if rep.get("grade") else ""

    head_meta = " · ".join(x for x in [f"{tier} {rank}".strip(), grade,
                                       _fmt_age(rep.get("post_age_minutes"))] if x)

    lines = [
        _SEP,
        f"{kind_kr} <b>{html.escape(coin_symbol)}</b>",
        head_meta,
    ]
    lines.extend(_author_block(rep))
    lines.append(_SEP)

    # ── 타점 (워쳐 동일: 현재 → 엔트리 → 손절 → 목표, 들여쓰기 4칸) ──
    lines.append("타점")
    if current_usd:
        lines.append(f"    현재  ${_fmt_usd(current_usd)} {_fmt_krw_paren(current_usd, usdt_krw)}")
    if lo is not None and hi is not None and hi > lo:
        lines.append(
            f"    엔트리 존  ${_fmt_usd(lo)}~${_fmt_usd(hi)} "
            f"{_fmt_krw_paren(lo, usdt_krw)}~{_fmt_krw_paren(hi, usdt_krw)}"
        )
    elif entry_rep:
        lines.append(f"    엔트리  ${_fmt_usd(entry_rep)} {_fmt_krw_paren(entry_rep, usdt_krw)}")

    sl, tp = rep.get("sl_usd"), rep.get("tp_usd")
    if sl and entry_rep:
        pct = (sl - entry_rep) / entry_rep * 100
        lines.append(f"    손절  ${_fmt_usd(sl)} {_fmt_krw_paren(sl, usdt_krw)}  {pct:+.1f}%")
    else:
        lines.append("    손절  데이터 없음")
    if tp and entry_rep:
        pct = (tp - entry_rep) / entry_rep * 100
        lines.append(f"    목표  ${_fmt_usd(tp)} {_fmt_krw_paren(tp, usdt_krw)}  {pct:+.1f}%")
    else:
        lines.append("    목표  데이터 없음")

    # R:R (워쳐 라벨 체계)
    rr = rep.get("rr")
    if rr and rr > 0:
        if rr >= 5:
            rr_label = "🔥 (매우 좋음)"
        elif rr >= 3:
            rr_label = "✅ (좋음)"
        elif rr >= 2:
            rr_label = "✅ (권장)"
        elif rr >= 1.5:
            rr_label = "(보통)"
        elif rr >= 1:
            rr_label = "⚠️ (위험)"
        else:
            rr_label = "🚫 (매매 비추)"
        lines.append(f"📊 R:R 1:{rr:.2f} {rr_label}")
    else:
        lines.append("📊 R:R 데이터 부족")

    # ── 시장 심리 (워쳐 표기 그대로: BTC.D 행 / ALT.S 행 / F&G 행) ──
    if sentiment:
        lines.append(_SEP)
        btc_d = sentiment.get("btc_dominance")
        alt_s = sentiment.get("altcoin_season_index")
        fng = sentiment.get("fear_greed")
        if btc_d is not None:
            lines.append(f"🌍 BTC.D: {btc_d}%")
        if alt_s is not None:
            if alt_s >= 75:
                alt_label = "알트 매수 권장"
            elif alt_s >= 50:
                alt_label = "알트 매수 고려"
            elif alt_s >= 25:
                alt_label = "BTC 매수 고려"
            else:
                alt_label = "BTC 매수 권장"
            lines.append(f"🪙 ALT.S: {alt_s} ({alt_label})")
        if fng is not None:
            label_kr = _FNG_KR.get(sentiment.get("fear_greed_label", ""),
                                   sentiment.get("fear_greed_label", ""))
            lines.append(f"😨 F&G: {fng} ({label_kr})")

    # ── 출처 (URL 노출 없이 하이퍼링크, 최신순, 최대 5) ──
    lines.append(_SEP)
    srcs = sorted(cluster, key=lambda l: l.get("post_age_minutes") or 1e12)
    links = []
    for i, lv in enumerate(srcs[:5], 1):
        url = html.escape(lv.get("post_url") or "", quote=True)
        links.append(f'<a href="{url}">출처{i}</a>' if url else f"출처{i}")
    source_line = "🔗 " + " · ".join(links)
    if len(srcs) > 5:
        source_line += f" · 외 {len(srcs) - 5}건"
    lines.append(source_line)

    return "\n".join(lines)
