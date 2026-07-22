"""
텔레그램 알림 — v3 확정 양식 렌더링 + 발송.

발송 원칙(upbit_bot core/notifier.py 검증 패턴 이식):
- 알림 실패는 예외를 삼키고 로그만 남긴다 (알림 때문에 잡이 죽으면 안 됨).
- 토큰/chat_id 는 env 전용(config.settings.secret). 없으면 조용히 비활성화.

렌더링 규칙(2026-07-22 기획 확정):
- 본알림(터치) 5줄 표준형 + 시총등급/순위 + 작성자 적중률 + 출처 최신순.
- 예고(접근)는 같은 틀에 헤더만 ⚠️ [접근].
- 클러스터(±1% 병합)면 "엔트리 존 lo~hi" 표기 + 출처 여러 줄.
"""

import logging

import requests

from config import settings

logger = logging.getLogger("alert.telegram")

_API = "https://api.telegram.org/bot{token}/sendMessage"


def send(text: str) -> bool:
    """알림 발송. 성공 True. 토큰 미설정/실패 시 False (예외 없음)."""
    token = settings.secret("TELEGRAM_BOT_TOKEN")
    chat_id = settings.secret("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        logger.warning("[tg] 토큰/chat_id 미설정 - 발송 생략 (내용 %d자)", len(text))
        return False
    try:
        resp = requests.post(
            _API.format(token=token),
            data={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
            timeout=10,
        )
        if resp.status_code != 200:
            logger.error("[tg] 발송 실패 status=%s", resp.status_code)
            return False
        return True
    except Exception as e:  # noqa: BLE001 - 알림 실패가 잡을 막으면 안 됨
        logger.error("[tg] 발송 실패: %s", e)
        return False


# ── 렌더링 ────────────────────────────────────────────────────

def _fmt_krw(v) -> str:
    """KRW 표시: 1000 이상 정수 콤마, 미만은 유효숫자 유지 (업비트 호가 관례)."""
    if v is None:
        return "?"
    if v >= 1000:
        return f"{v:,.0f}"
    if v >= 1:
        return f"{v:,.2f}"
    return f"{v:.4f}"


def _fmt_age(minutes) -> str:
    if minutes is None:
        return "시각미상"
    if minutes < 60:
        return f"{minutes:.0f}분 전"
    if minutes < 1440:
        return f"{minutes / 60:.0f}시간 전"
    return f"{minutes / 1440:.0f}일 전"


def _author_line(level: dict) -> str:
    """작성자 신뢰정보: 워쳐 적중률 있으면 그것, 없으면 팔로워, 둘 다 없으면 기록없음."""
    author = level.get("author") or "작성자미상"
    parts = [f"✍️ {author}"]
    hit_rate, hit_count = level.get("author_hit_rate"), level.get("author_hit_count")
    if hit_rate is not None and hit_count:
        parts.append(f"적중 {hit_rate * 100:.0f}% ({hit_count}건)")
        if level.get("author_whitelisted"):
            parts.append("✅화이트")
    elif level.get("author_followers"):
        f = level["author_followers"]
        parts.append(f"팔로워 {f / 1000:.1f}k" if f >= 1000 else f"팔로워 {f}")
    else:
        parts.append("기록없음")
    parts.append(_fmt_age(level.get("post_age_minutes")))
    if level.get("grade"):
        parts.append(f"{level['grade']}등급")
    return " · ".join(parts)


def render_alert(kind: str, coin_symbol: str, cluster: list, current_krw: float,
                 usdt_krw: float) -> str:
    """kind: 'touch'|'preview'. cluster: 같은 코인 ±1% 이내 레벨 dict 목록(대표=첫번째,
    entry 높은 순 정렬 가정). USD 저장값은 usdt_krw 로 환산해 표기."""
    rep = cluster[0]

    def krw(usd):
        return usd * usdt_krw if (usd is not None and usdt_krw) else None

    entries = [lv["entry_usd"] for lv in cluster if lv.get("entry_usd")]
    lo, hi = (min(entries), max(entries)) if entries else (None, None)

    tier = rep.get("mcap_tier_icon") or "·"
    rank = f"시총 {rep['mcap_rank']}위" if rep.get("mcap_rank") else "순위미상"
    header_icon = "🎯 [터치]" if kind == "touch" else "⚠️ [접근]"

    lines = [f"{header_icon} {coin_symbol} {tier} {rank} · ₩{_fmt_krw(current_krw)}"]

    if lo is not None and hi is not None and hi > lo:
        zone = f"엔트리 존 {_fmt_krw(krw(lo))}~{_fmt_krw(krw(hi))}"
    else:
        zone = f"엔트리 {_fmt_krw(krw(rep.get('entry_usd')))}"
    lines.append(f"{zone} {'하향 돌파' if kind == 'touch' else '접근 중'}")

    # TP/SL/RR (대표 레벨 기준, 없으면 항목 생략)
    tp_krw, sl_krw = krw(rep.get("tp_usd")), krw(rep.get("sl_usd"))
    entry_rep = krw(rep.get("entry_usd"))
    row = []
    if tp_krw and entry_rep:
        row.append(f"🏁 TP {_fmt_krw(tp_krw)} ({(tp_krw / entry_rep - 1) * 100:+.1f}%)")
    if sl_krw and entry_rep:
        row.append(f"🛑 SL {_fmt_krw(sl_krw)} ({(sl_krw / entry_rep - 1) * 100:+.1f}%)")
    if rep.get("rr"):
        row.append(f"R:R 1:{rep['rr']:.1f}")
    if row:
        lines.append(" · ".join(row))

    lines.append(_author_line(rep))

    # 출처 최신순 (클러스터 전체)
    srcs = sorted(cluster, key=lambda l: l.get("post_age_minutes") or 1e12)
    if len(srcs) == 1:
        lines.append(f"🔗 {srcs[0].get('post_url') or '?'}")
    else:
        lines.append("📎 출처(최신순)")
        for lv in srcs[:5]:
            lines.append(f" · {_fmt_age(lv.get('post_age_minutes'))} "
                         f"{lv.get('author') or '?'} — {lv.get('post_url') or '?'}")
    return "\n".join(lines)
