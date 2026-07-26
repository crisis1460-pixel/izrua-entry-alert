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
import logging
import time

import requests

from analytics import calibration, clustering, ranking
from config import settings

logger = logging.getLogger("alert.telegram")

_API = "https://api.telegram.org/bot{token}/sendMessage"
_SEP = "━━━━━━━━━━━━━━━━━━━━"

# ── 재시도 정책 상수 (2026-07-26 수리) ──────────────────────────────
_RETRY_MAX = 2                  # 총 재시도 최대 횟수(최초 시도 제외)
_RETRY_429_MAX_WAIT_SEC = 10.0  # 429 retry_after 상한
_RETRY_BACKOFF_SEC = (1.0, 2.0)  # 5xx/타임아웃 - 재시도 회차별 대기(1회차 1초, 2회차 2초)

_FNG_KR = {
    "Extreme Fear": "극공포",
    "Fear": "공포",
    "Neutral": "중립",
    "Greed": "탐욕",
    "Extreme Greed": "극탐욕",
}


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


def send(text: str, urgency: str = "high") -> bool:
    """HTML 모드 발송. 성공 True. 토큰 미설정/재시도 소진 후 실패 시 False (예외 없음).

    urgency: 'high'(기본, 유음) | 'low'(disable_notification=true, 무음). 기본값은
    기존 동작과 동일 - 활성화는 호출부 수정 후(사용자 확정 대기 중).

    재시도(2026-07-26): 429 는 retry_after(상한 10초) 대기 후, 5xx/타임아웃·연결
    오류는 1~2초 대기 후 재시도. 총 재시도 최대 _RETRY_MAX 회, 소진하면 기존처럼
    조용히 False."""
    token = settings.secret("TELEGRAM_BOT_TOKEN")
    chat_id = settings.secret("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        logger.warning("[tg] 토큰/chat_id 미설정 - 발송 생략 (내용 %d자)", len(text))
        return False

    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
               "disable_web_page_preview": True}
    if urgency == "low":
        payload["disable_notification"] = True

    retry = 0
    while True:
        try:
            resp = requests.post(_API.format(token=token), json=payload, timeout=10)
        except requests.RequestException as e:
            if retry < _RETRY_MAX:
                wait = _RETRY_BACKOFF_SEC[min(retry, len(_RETRY_BACKOFF_SEC) - 1)]
                logger.warning("[tg] 발송 실패(%s) - %.0f초 후 재시도(%d/%d)",
                               type(e).__name__, wait, retry + 1, _RETRY_MAX)
                time.sleep(wait)
                retry += 1
                continue
            logger.error("[tg] 발송 실패(재시도 소진): %s", _redact(str(e), token))
            return False

        if resp.status_code == 200:
            return True

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
    # ✍️ 로 아래 📊/🏹/📎 행들과 시작 칸 정렬 (2026-07-24 사용자 확정)
    lines = [f"✍️ 작성자: @{author}{star}"]

    wins = rep.get("author_self_wins") or 0
    losses = rep.get("author_self_losses") or 0
    # 자체 성적은 별도 줄 (이모지로 윗줄과 시작 위치 정렬). C안 함축 표기로 한 줄 유지
    # (2026-07-24 사용자 확정: "승률67% (4승2패) 터치율67%").
    self_line = None
    # 게이트: 주입된 n_eff(최신성 가중 유효표본, 2026-07-26 카드 확정) 우선,
    # 미주입이면 raw 폴백 (렌더러 단독 호출 테스트 호환)
    _neff = rep.get("author_self_neff")
    _gate_n = _neff if _neff is not None else wins + losses
    if _gate_n >= _SELF_STATS_MIN_N:
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
        else:
            lines.append("👥 적중률 기록없음 (워쳐 미추적 작성자)")
    if self_line:
        lines.append(self_line)

    # 역신호 경고 (2026-07-27 사용자 지시) — 워쳐 적중률·⭐⭐ 화이트리스트가 "믿을 만한
    # 작성자"로 읽히는데 자체 표본은 정반대인 경우가 실제로 나왔다(@mastercrypto2020:
    # 워쳐 66%/64건 · ⭐⭐ 인데 자체 승률 25%(2승6패), E_LB 음수로 이미 역신호 후보 태깅).
    # 두 숫자는 측정 기준이 달라(워쳐=글 시점, 자체=터치 시점) 어느 쪽도 틀리진 않지만,
    # 사용자는 위쪽 큰 숫자만 보고 신뢰하기 쉽다. show_status·주간 리포트에만 있던
    # 역신호 판정을 정작 판단이 필요한 알림 시점에 노출한다 — 판정식은 재구현하지 않고
    # 같은 게이트(neff_r >= rank_min_neff, E_LB <= 0)를 쓴다. 억제가 아니라 표기다.
    e_lb = rep.get("author_self_e_lb")
    neff_r = rep.get("author_self_neff_r") or 0.0
    min_neff = rep.get("author_rank_min_neff")
    if (e_lb is not None and e_lb <= 0 and min_neff is not None
            and neff_r >= min_neff):
        lines.append(f"🔻 역신호 후보 — 자체 기대손익 {e_lb:+.2f}R (표본 {neff_r:.0f})")
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
        "봇이 30분 쿨다운 후 자동 재시도합니다. 반복되면 TV_COOKIE 등록을 검토하세요.",
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
    is_anti = met["e_lb"] is not None and met["e_lb"] <= 0
    mark = " 🔻" if is_anti else ""
    pct = f", 승률{met['p_hat'] * 100:.0f}%" if met.get("p_hat") is not None else ""
    line = (f"  {rank}. @{html.escape(author)}{mark}  "
            f"E_LB {met['e_lb_display']:+.2f} (n_eff {met['neff_r']:.1f}{pct})")
    return line, is_anti


def _confluence_line(confluence: dict, author: str, min_clusters: int) -> str:
    """'🤝 합의 참여 X/Y회(Z%)' — 표시 전용(정렬·E_LB 미반영). 클러스터 수가
    min_clusters 미만이면 None(1회짜리 0% / 100% 는 정보가 아니라 잡음)."""
    if not confluence:
        return None
    s = confluence.get(author)
    if not s or s.get("total", 0) < min_clusters:
        return None
    return (f"     🤝 합의 참여 {s['multi']}/{s['total']}회"
            f"({s['cr'] * 100:.0f}%)")


def _baseline_section(rows_by_author: dict, order: list, baseline: dict,
                      raw_records: dict, min_n: int) -> list:
    """🎲 초과 적중률 섹션. pooled 표본 미달이면 빈 목록(그 주 통째로 생략).

    비교 대상은 '원시' 승률이다 — 베이스라인(ret_24h 양수 비율)이 가중 없는 단순
    비율이라 축을 맞춘다. 랭킹의 수축·가중 승률(p_hat)과는 다른 숫자이며, 이 섹션은
    랭킹과 무관한 별도 축이다."""
    if not baseline or baseline.get("rate") is None:
        return []
    if baseline.get("n", 0) < min_n:
        return []  # 표본 미달 — 기준선 자체가 흔들려 비교가 무의미
    rate = baseline["rate"]
    lines = [_SEP,
             f"🎲 초과 적중률 (베이스라인: 터치 후 24h 보유 시 수익권 "
             f"{rate * 100:.0f}%, n={baseline['n']})"]
    shown = 0
    for author in order:
        rec = (raw_records or {}).get(author)
        if rec is None:  # 호출부 미주입 — rows 에서 직접 센다(렌더러 단독 테스트 호환)
            rows = rows_by_author.get(author) or []
            rec = {
                "wins": sum(1 for r in rows if r.get("outcome") in ranking.WIN_OUTCOMES),
                "losses": sum(1 for r in rows if r.get("outcome") in ranking.LOSS_OUTCOMES),
            }
        ex = clustering.excess_hit_rate(rec["wins"], rec["losses"], rate)
        if ex["excess"] is None:
            continue
        lines.append(f"  @{html.escape(author)}  원시승률 {ex['raw'] * 100:.0f}%"
                     f" → 베이스라인 대비 {ex['excess'] * 100:+.0f}%p")
        shown += 1
    if not shown:
        return []
    lines.append("⚠️ 판정 기준이 서로 다릅니다(작성자=TP 도달 / 기준선=단순 수익 여부). "
                 "하락장 구간에선 기준선이 낮아져 초과치가 과장돼 보입니다 — 방향 참고용.")
    return lines


def _pct(v) -> str:
    return f"{v * 100:.0f}%" if v is not None else "-"


def _calibration_section(cal: dict) -> list:
    """🎚️ 등급 캘리브레이션 섹션 (기획 카드 #26). 미주입/표본 0 이면 빈 목록.

    수학은 analytics.calibration 이 전부 담당하고 여기선 문구만 만든다.
    **표기 전용** — 이 섹션은 배점(collector/grading.py)·알림 필터·엔트리 알림
    양식 어디에도 되먹임되지 않는다. 문구에서도 그 점을 매번 명시한다(읽는 사람이
    '봇이 알아서 등급을 고쳤겠거니' 오해하면 안 된다).
    """
    if not cal or not cal.get("buckets"):
        return []
    pooled = cal.get("pooled") or {}
    if not pooled.get("n"):
        return []  # 종결 표본 0 — 섹션 통째로 생략(빈 표를 띄우느니 안 띄운다)

    min_n = cal.get("min_n", calibration.DEFAULT_MIN_N)
    z = cal.get("z", calibration.DEFAULT_Z)
    ci_label = f"CI = {calibration.confidence_pct(z):.0f}% Wilson score 구간"
    lines = [_SEP,
             f"🎚️ 등급 캘리브레이션 (수집 시점 등급별 TP1 도달률, 종결 {pooled['n']}건)"]
    for g in cal["order"]:
        b = cal["buckets"].get(g) or {}
        if not b.get("n"):
            continue  # 그 등급의 종결 표본이 아직 없음 — 행 자체를 만들지 않는다
        amb = f" · 판별불가 {b['ambiguous']}건" if b.get("ambiguous") else ""
        # '<' 는 반드시 &lt; (parse_mode=HTML — 날 '<' 는 400 Can't parse entities)
        note = "" if b.get("enough") else f" · ⚠️표본 부족(n&lt;{min_n:g}), 참고용"
        lines.append(f"  {g}  {_pct(b['rate'])} ({b['hits']}/{b['n']})  "
                     f"CI {_pct(b['ci_low'])}~{_pct(b['ci_high'])}{amb}{note}")
    lines.append(f"  ({ci_label}. 도달률 분자 = TP1 실제 도달 건만 — "
                 f"판정창 만료 종결은 분모에만 들어갑니다)")

    violations = cal.get("violations") or []
    if violations:
        lines.append(f"⚠️ 단조성 위반 {len(violations)}건 — 하위 등급이 상위 등급보다 "
                     f"실측이 높습니다")
        for v in violations[:5]:
            mark = ("CI 비겹침 — 강한 신호" if v["significant"]
                    else "CI 겹침 — 약한 신호")
            lines.append(f"  {v['lower']} {_pct(v['lower_rate'])} &gt; "
                         f"{v['higher']} {_pct(v['higher_rate'])} ({mark})")
        if len(violations) > 5:
            lines.append(f"  · 외 {len(violations) - 5}쌍")
        lines.append("→ 배점 재검토 '신호'로만 표기합니다. 등급 산식·알림 필터는 "
                     "이 리포트로 바뀌지 않습니다(사람이 판단할 몫).")
    elif cal.get("eligible", 0) < 2:
        lines.append(f"ℹ️ 단조성 판정 보류 — 표본 n≥{min_n:g} 등급이 2개 미만입니다 "
                     f"(계속 관찰 중)")
    else:
        lines.append("✅ 단조성 유지 — 하위 등급이 상위 등급을 앞지른 쌍 없음")

    lines.append("ℹ️ E_LB(작성자 실력 축)와는 <b>다른 축</b>입니다 — 이 섹션은 "
                 "'등급 산식이 실제 결과와 맞는가'만 봅니다.")
    return lines


def render_weekly_report(rows_by_author: dict, now: float = None,
                         half_life_days: float = None, z: float = None,
                         prior_m: int = None, min_neff: float = None,
                         confluence: dict = None, baseline: dict = None,
                         raw_records: dict = None, baseline_min_n: int = None,
                         confluence_min_clusters: int = None,
                         calibration_result: dict = None) -> str:
    """작성자별 종결 표본({author: rows}, storage.db.get_author_outcome_rows 행 형식)
    → 텔레그램 HTML 주간 리포트. 파라미터 미지정 시 config.settings 의 rank_* 사용.

    3부 구성(2026-07-26 카드): ① E_LB 게이트(n_eff≥min) 통과 작성자 순위,
    ② R NULL(전부 tp_only 등) 이면서 승률 게이트만 통과한 작성자 별도(2트랙 확정,
    랭킹엔 미등재), ③ 표본부족 안내 + 역신호 후보(①에서 게이트는 통과했지만
    E_LB≤0) 안내. 표본이 전혀 없으면 그 상태도 우아하게 표시.

    2026-07-26 신규 2건(둘 다 **표시 전용** — 랭킹 수식·정렬 키는 불변):
    - confluence: {author: {multi, total, cr}} (analytics.clustering.confluence_by_author)
      → 랭킹/승률 행 아래 '🤝 합의 참여 X/Y회'. 미주입이면 그 줄만 빠진다.
    - baseline: {n, positive, rate} (analytics.clustering.baseline_positive_rate)
      + raw_records: {author: {wins, losses}} → '🎲 초과 적중률' 섹션.
      pooled n 이 baseline_min_n 미만이면 섹션 자체를 생략한다.

    2026-07-27 신규(기획 카드 #26):
    - calibration_result: analytics.calibration.calibrate_grades(...) 결과
      → '🎚️ 등급 캘리브레이션' 섹션(등급별 TP1 도달률·Wilson CI·단조성 신호).
      미주입이면 섹션만 빠지고 나머지 출력은 완전히 동일하다. 표시 전용이라
      랭킹 수식·정렬 키·알림 필터·등급 산식 어디에도 영향이 없다."""
    now = time.time() if now is None else now
    half_life_days = settings.get("rank_half_life_days") if half_life_days is None else half_life_days
    z = settings.get("rank_z") if z is None else z
    prior_m = settings.get("rank_prior_m") if prior_m is None else prior_m
    min_neff = settings.get("rank_min_neff") if min_neff is None else min_neff
    baseline_min_n = settings.get("baseline_min_n") if baseline_min_n is None else baseline_min_n
    if confluence_min_clusters is None:
        confluence_min_clusters = settings.get("confluence_min_clusters")
    rank_kw = dict(half_life_days=half_life_days, z=z, m=prior_m)

    lines = [_SEP, "📈 <b>주간 성적 리포트</b>"]

    rows_by_author = rows_by_author or {}
    total_rows = sum(len(rows) for rows in rows_by_author.values())
    if not rows_by_author or total_rows == 0:
        lines.append(_SEP)
        lines.append("아직 표본 부족합니다 — 터치 후 종결된 레벨이 쌓이면 다음 리포트부터 "
                      "순위가 표시됩니다.")
        # 등급 캘리브레이션은 작성자 축과 독립이다 — 작성자 미상(author NULL) 종결
        # 표본만 있는 경우에도 등급 축은 볼 수 있으므로 이 경로에서도 붙인다.
        lines.extend(_calibration_section(calibration_result))
        lines.append(_SEP)
        return "\n".join(lines)

    lines.append(f"✍️ 작성자 {len(rows_by_author)}명 · 종결 표본 {total_rows}건")
    lines.append(_SEP)

    ranked = ranking.rank_authors(rows_by_author, now, min_neff=min_neff, **rank_kw)
    ranked_authors = {author for author, _ in ranked}

    # ① E_LB 게이트 통과 랭킹
    lines.append(f"🏆 E_LB 랭킹 (R 트랙, n_eff≥{min_neff:g})")
    n_anti = 0
    if ranked:
        for i, (author, met) in enumerate(ranked, 1):
            line, is_anti = _weekly_rank_line(i, author, met)
            lines.append(line)
            conf = _confluence_line(confluence, author, confluence_min_clusters)
            if conf:
                lines.append(conf)
            n_anti += is_anti
    else:
        lines.append("  게이트 통과 작성자 없음 (계속 관찰 중)")

    # ② R NULL(2트랙) — 랭킹엔 미등재, 승률축만 게이트 통과
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
        lines.append(_SEP)
        lines.append("🎯 승률만 확정 (R 미보유 표본, 2트랙 — 랭킹 미등재)")
        for author, met, wins, losses in win_only:
            lines.append(f"  @{html.escape(author)}  승률{met['p_hat'] * 100:.0f}% "
                         f"({wins}승{losses}패, n_eff {met['neff_win']:.1f})")
            conf = _confluence_line(confluence, author, confluence_min_clusters)
            if conf:
                lines.append(conf)

    # ③ 초과 적중률 베이스라인 (표시 대상 = 위 두 섹션에 노출된 작성자)
    baseline_order = [a for a, _ in ranked] + [a for a, _, _, _ in win_only]
    lines.extend(_baseline_section(rows_by_author, baseline_order, baseline,
                                   raw_records, baseline_min_n))

    # ④ 등급 캘리브레이션 (기획 카드 #26) — 작성자 축과 무관한 별도 축, 표시 전용
    lines.extend(_calibration_section(calibration_result))

    # ⑤ 안내: 표본부족 + 역신호 후보
    if under_sample or n_anti:
        lines.append(_SEP)
    if under_sample:
        under_sample.sort(key=lambda x: x[1], reverse=True)
        names = " · ".join(f"@{html.escape(a)}({n}건)" for a, n in under_sample[:10])
        more = f" · 외 {len(under_sample) - 10}명" if len(under_sample) > 10 else ""
        # '<' 는 반드시 &lt; 로 (parse_mode=HTML 이라 날 '<' 가 태그 시작으로 파싱돼
        # 400 Can't parse entities 로 발송 자체가 실패한다 — 2026-07-26 첫 발송 실패 원인)
        lines.append(f"📋 표본 부족(n_eff&lt;{min_neff:g}, 계속 관찰 중): {names}{more}")
    if n_anti:
        lines.append(f"⚠️ 역신호 후보 {n_anti}명 — 게이트는 통과했지만 E_LB≤0 (🔻 표시, "
                     f"자동 필터·태깅 아님, 관찰 참고용)")

    lines.append(_SEP)
    return "\n".join(lines)


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
