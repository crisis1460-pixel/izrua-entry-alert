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
import time

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
    """달러 표기 - 소수 1자리 (2026-07-23 사용자: 소수점 너무 길다, 한자리까지만).
    단 $1 미만 코인은 1자리로 반올림하면 값 자체가 뭉개져($0.0888→$0.1) 유효숫자
    3개만 남긴다 - '짧게'라는 취지 유지."""
    if value is None or value == 0:
        return "N/A"
    v = abs(value)
    if v >= 1:
        return f"{value:,.1f}"
    return f"{value:.3g}"


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


def _author_block(rep: dict) -> list:
    """작성자 라인 + 적중률 라인 (워쳐 스타일 + 자체 적중 병기).
    2026-07-23 카드4 확정: 워쳐(글 시점 기준)와 자체(터치 시점 기준)는 측정 기준이
    달라 섞지 않고 병기. 자체 표본 5건 미만이면 표시하지 않음(조용한 누적)."""
    author = html.escape(rep.get("author") or "?")
    star = " ⭐⭐" if rep.get("author_whitelisted") else ""
    lines = [f"작성자: @{author}{star}"]

    wins = rep.get("author_self_wins") or 0
    losses = rep.get("author_self_losses") or 0
    self_part = ""
    if wins + losses >= _SELF_STATS_MIN_N:
        self_part = f" · 자체 터치후 {wins}승{losses}패"

    hit_rate, hit_count = rep.get("author_hit_rate"), rep.get("author_hit_count")
    if hit_rate is not None and hit_count:
        lines.append(f"📊 평균 적중률: {hit_rate * 100:.0f}% (워쳐 {hit_count}건){self_part}")
    elif self_part:
        rate = wins / (wins + losses) * 100
        lines.append(f"📊 자체 적중률(터치후): {wins}승{losses}패 ({rate:.0f}%)")
    elif rep.get("author_followers"):
        lines.append(f"👥 팔로워 {_fmt_followers(rep['author_followers'])} · 적중률 기록없음")
    else:
        lines.append("👥 적중률 기록없음 (워쳐 미추적 작성자)")
    return lines


def _vwidth(s: str) -> int:
    """대략적 시각 폭 — 한글/CJK 는 2, 그 외 1 (모바일 한 줄 초과 판정용)."""
    return sum(2 if ord(ch) > 0x1100 else 1 for ch in s)


def _price_row(label: str, value: str, wrap_limit: int = 34) -> list:
    """'라벨: 값' 한 줄 — 폭 초과가 예상되면 라벨 줄 아래 4칸 들여쓰기로 값 줄을
    내린다(2026-07-23 사용자 지시 #8: 진입가 범위처럼 긴 값이 중간에서 꺾이는 것 방지)."""
    one = f"{label} {value}"
    if _vwidth(one) <= wrap_limit:
        return [one]
    return [label, f"    {value}"]


def render_alert(kind: str, coin_symbol: str, cluster: list, current_krw: float,
                 usdt_krw: float, sentiment: dict = None, week52: tuple = None,
                 kimchi_pct: float = None, volume_rank: int = None) -> str:
    """kind: 'touch'|'preview'. cluster: 같은 코인 ±1% 레벨 dict 목록(entry 내림차순).
    sentiment: {btc_dominance, fear_greed, ...}|None. week52: (고가KRW, 저가KRW)|None.
    kimchi_pct: 김프 %|None. volume_rank: 업비트 KRW 거래대금 순위(조회 시점)|None."""
    rep = max(cluster, key=lambda l: l.get("score") or 0)
    current_usd = (current_krw / usdt_krw) if (current_krw and usdt_krw) else None

    entries = [lv["entry_usd"] for lv in cluster if lv.get("entry_usd")]
    lo, hi = (min(entries), max(entries)) if entries else (None, None)
    entry_rep = hi  # 트리거 기준 = 클러스터 상단

    tier = rep.get("mcap_tier_icon") or ""
    rank = f"시총 {rep['mcap_rank']}위" if rep.get("mcap_rank") else ""
    kind_kr = "🎯 <b>[진입가 터치]</b>" if kind == "touch" else "⚠️ <b>[진입가 접근]</b>"
    grade = f"{rep['grade']}등급" if rep.get("grade") else ""

    head_meta = " · ".join(x for x in [f"{tier} {rank}".strip(), grade,
                                       _fmt_age(_fresh_age_min(rep))] if x)

    lines = [
        _SEP,
        f"{kind_kr} <b>{html.escape(coin_symbol)}</b>",
        head_meta,
    ]
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
    if current_usd:
        lines.append(f"    현재:  {_krw(current_usd)}원")
    if lo is not None and hi is not None and hi > lo:
        lines.append(f"    진입:  {_krw(lo)}~{_krw(hi)}원")
    elif entry_rep:
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
        lines.append(f"    목표:  {_krw(tp)}원  ({pct:+.1f}%)")
    else:
        lines.append("    목표:  데이터 없음")
    if volume_rank:
        lines.append(f"    거래:  {volume_rank}위")

    # ── 52주 고저 + 현재 위치 바 (워쳐 notifier.py 표기 그대로, 2026-07-23 #9) ──
    if week52 and current_krw:
        high52, low52 = week52
        if high52 and low52 and high52 > 0 and low52 > 0:
            from_high = (current_krw - high52) / high52 * 100
            from_low = (current_krw - low52) / low52 * 100
            lines.append("")
            lines.append("52주")
            lines.append(f"    고가  {from_high:+.1f}% ({high52:,.0f}원)")
            lines.append(f"    저가  {from_low:+.1f}% ({low52:,.0f}원)")
            if high52 > low52:
                pos = max(0, min(100, (current_krw - low52) / (high52 - low52) * 100))
                filled = max(1, min(10, round(pos / 10)))
                lines.append("")
                lines.append("    " + "🟩" * filled + "⬜" * (10 - filled))
                lines.append(f"    └ 현재 {pos:.0f}% 지점")

    # ── 시장 심리 (워쳐 표기 그대로: 김프 행 → BTC.D 행 / ALT.S 행 / F&G 행) ──
    if sentiment or kimchi_pct is not None:
        lines.append(_SEP)
    if kimchi_pct is not None:
        if abs(kimchi_pct) < 0.01:
            lines.append(f"⚖️ 김프 거의 0% ({kimchi_pct:+.3f}%)")
        elif kimchi_pct > 0:
            lines.append(f"🌶️ 김프 {kimchi_pct:+.2f}%")
        else:
            lines.append(f"❄️ 김프 {kimchi_pct:+.2f}%")
    if sentiment:
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
    srcs = sorted(cluster, key=lambda l: _fresh_age_min(l) or 1e12)
    links = []
    for i, lv in enumerate(srcs[:5], 1):
        url = html.escape(lv.get("post_url") or "", quote=True)
        links.append(f'<a href="{url}">출처{i}</a>' if url else f"출처{i}")
    source_line = "🔗 " + " · ".join(links)
    if len(srcs) > 5:
        source_line += f" · 외 {len(srcs) - 5}건"
    lines.append(source_line)

    return "\n".join(lines)
