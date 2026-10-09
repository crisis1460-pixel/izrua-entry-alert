"""고변동 경제지표 발표 30분 전 개별 알림 (2026-10-09 대표 요청).

"모닝 브리핑의 PPI·CPI 등 발표 일정 중 변동성 폭이 아주 높은 CPI 와 비슷한 지표가 발표하는
날에는 발표 30분 전에 별도 메시지를 중요 알림으로 하나 보내줘."

대상(settings.macro_prealert_types, 기본 CPI·FOMC 금리결정·비농업 고용): 암호화폐 시장에서
발표 직후 변동성이 가장 큰 미국 지표 3종. PPI·PCE·ISM·소매판매·GDP·FOMC 의사록은 브리핑
일정 줄로만 안내한다(설정에 타입을 넣으면 같은 경로로 알림).

발송 창: 발표까지 남은 시간이 (0, macro_prealert_window_minutes] 일 때 1회 — 회차가 약 4분
간격이라 기본 35분 창이면 30~35분 전 회차가 보낸다(러너 지연 시 더 늦게라도 발표 전이면 보냄).
중복 방지: meta macro_prealert_sent = ["TYPE|YYYY-MM-DD", …](최근 30개). 소리 있는 발송.
어떤 실패도 회차를 죽이지 않는다(run_cycle 의 다른 maybe_* 와 동일 격리).
"""

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from config import settings
from storage import db

logger = logging.getLogger("alert.macro_alert")

META_SENT = "macro_prealert_sent"
KST = timezone(timedelta(hours=9))
_SEP = "━━━━━━━━━━━━━━━━━"

# 지표별 한 줄 설명(32칸 이내) — 무엇이 나오는지·왜 크게 움직이는지.
_WHY = {
    "CPI": "물가 → 금리 인하 기대를 좌우",
    "FOMC": "기준금리 결정 + 의장 회견",
    "NFP": "고용 → 금리 경로 기대를 좌우",
    "PCE": "연준이 보는 물가 지표",
    "PPI": "생산자 물가 → CPI 선행",
}


def _cfg(key, default):
    try:
        v = settings.get(key)
        return default if v is None else v
    except KeyError:
        return default


# 이번 발표의 예상치·직전치 — ForexFactory 공개 주간 캘린더 JSON(가입·키 없음, 2026-10-09 실측 200).
# 발송 시점(발표 30분 전)에 1회만 조회한다. 타입별로 보여 줄 지표 제목(FF title)과 짧은 라벨.
_FF_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
_FF_ITEMS = {
    "CPI": [("CPI y/y", "CPI(연)"), ("Core CPI m/m", "근원(월)")],
    "NFP": [("Non-Farm Employment Change", "고용"), ("Unemployment Rate", "실업률")],
    "FOMC": [("Federal Funds Rate", "금리")],
    "PPI": [("PPI m/m", "PPI(월)"), ("Core PPI m/m", "근원(월)")],
    "PCE": [("Core PCE Price Index m/m", "근원(월)")],
}


def _width(s: str) -> int:
    import unicodedata
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in s)


def fetch_release_rows(timeout: float = 8.0) -> list:
    """FF 주간 캘린더(미국·이번 주). 실패 시 [] — 본문은 예상치 줄 없이 나간다."""
    try:
        import requests
        r = requests.get(_FF_URL, headers={"User-Agent": "Mozilla/5.0 (izrua-entry-alert)"},
                         timeout=timeout)
        if r.status_code != 200:
            logger.warning("[macro] 예상치 캘린더 HTTP %s", r.status_code)
            return []
        rows = [e for e in (r.json() or []) if e.get("country") == "USD"]
        logger.info("[macro] 예상치 캘린더(FF) %d행 수신", len(rows))   # 러너 도달 확인용
        return rows
    except Exception as e:  # noqa: BLE001
        logger.warning("[macro] 예상치 캘린더 조회 실패: %s", e)
        return []


def release_values(ev: dict, ev_utc: datetime, rows: list) -> list:
    """이번 발표의 [(라벨, 예상, 직전), …] 최대 2건 — 같은 제목·발표 시각 ±6시간 행만."""
    out = []
    for title, lab in _FF_ITEMS.get(ev.get("type"), []):
        for e in rows or []:
            if (e.get("title") or "").strip().lower() != title.lower():
                continue
            try:
                d = datetime.fromisoformat(str(e.get("date")))
            except ValueError:
                continue
            if abs((d.astimezone(timezone.utc) - ev_utc).total_seconds()) > 6 * 3600:
                continue
            f, p = (e.get("forecast") or "").strip(), (e.get("previous") or "").strip()
            if f or p:
                out.append((lab, f, p))
            break
        if len(out) >= 2:
            break
    return out


def release_lines(ev: dict, ev_utc: datetime, rows: list) -> list:
    """'예상·직전' 섹션 하위 줄(들여쓰기 2칸, 각 32칸 이내)."""
    vals = release_values(ev, ev_utc, rows)
    # 섹션 안 줄 모양 통일 — 모든 줄이 들어가는 첫 양식을 고른다(한 줄만 붙여 쓰는 들쭉날쭉 방지).
    templates = ["  {lab}: 예상 {f} · 직전 {p}", "  {lab}: 예상 {f}·직전 {p}", "  {lab}: {f} (직전 {p})"]
    for t in templates:
        lines = []
        for lab, f, p in vals:
            if f and p:
                lines.append(t.format(lab=lab, f=f, p=p))
            elif f:
                lines.append(f"  {lab}: 예상 {f}")
            else:
                lines.append(f"  {lab}: 직전 {p}")
        if all(_width(x) <= 32 for x in lines):
            return lines
    return [x for x in lines if _width(x) <= 32]


def _num(v: str) -> Optional[float]:
    """'2.9%'·'125K'·'-0.1%'·'3.75%' → float. 실패 None."""
    import re as _re
    m = _re.search(r"-?\d+(?:\.\d+)?", (v or "").replace(",", ""))
    return float(m.group(0)) if m else None


# 코인 시장 영향 — 발표치가 '예상보다' 어느 쪽이면 위험자산(코인)에 통상 어느 쪽인가.
# 물가·고용이 예상보다 낮으면 금리 인하 기대 ↑ → 코인 상승 쪽, 높으면 하락 쪽. 금리는 예상보다
# 많이 내리면 상승 쪽. (해외 거시·암호화폐 리서치의 통상 반응 — 실제 방향은 발표 수치가 정한다.)
_IMPACT = {
    # (낮을 때 문구, 높을 때 문구, 예상 둔화 문구, 예상 상승 문구)
    "CPI": ("예상보다 낮으면", "예상보다 높으면", "물가 둔화", "물가 반등"),
    "PPI": ("예상보다 낮으면", "예상보다 높으면", "물가 둔화", "물가 반등"),
    "PCE": ("예상보다 낮으면", "예상보다 높으면", "물가 둔화", "물가 반등"),
    "NFP": ("예상보다 약하면", "예상보다 강하면", "고용 둔화", "고용 개선"),
    "FOMC": ("더 크게 내리면", "동결·덜 내리면", "", ""),
}


def impact_lines(ev: dict, ev_utc: datetime, rows: list) -> list:
    """'코인 시장 영향' 섹션 하위 줄(들여쓰기 2칸, 32칸 이내).

    ① 발표치가 예상보다 낮/높을 때 상승·하락 가능성 ② 시장 예상(예상 vs 직전)이 이미 어느 쪽인지."""
    tp = ev.get("type")
    rule = _IMPACT.get(tp)
    if not rule:
        return []
    low, high, down_w, up_w = rule
    out = [f"  {low} → 상승 가능성↑", f"  {high} → 하락 가능성↑"]
    vals = release_values(ev, ev_utc, rows)
    if vals:
        _lab, f, p = vals[0]
        fv, pv = _num(f), _num(p)
        if fv is not None and pv is not None:
            if tp == "FOMC":
                exp = ("인하 예상 → 대부분 선반영" if fv < pv else
                       ("동결 예상" if fv == pv else "인상 예상 → 부담"))
            elif fv < pv:
                exp = f"{down_w} 예상 → 우호적"
            elif fv > pv:
                exp = f"{up_w} 예상 → 부담"
            else:
                exp = "직전과 같은 수준 예상"
            cand = f"  시장 예상: {exp}"
            out.append(cand if _width(cand) <= 32 else f"  {exp}")
    return [x for x in out if _width(x) <= 32]


def build_message(ev: dict, ev_utc: datetime, now: float, rows: Optional[list] = None) -> str:
    """알림 본문(HTML). 모든 줄 32칸 이내, 다른 알림처럼 줄머리 정렬 + 하위 내용만 2칸 들여쓰기.

    10-09 대표 수정 ①: "[nn분 후 발표]" 는 발송·도달 지연으로 어긋난다 → "미국 {지표} 지표 발표"
    + 한국시간. ② 이번 발표의 예상·직전(FF 캘린더). ③ 일반 주의 문구 대신 '코인 시장 영향' —
    발표치가 예상보다 낮/높을 때 상승·하락 가능성과 시장 예상의 방향."""
    kst = ev_utc.astimezone(KST)
    day = "오늘" if kst.date() == datetime.fromtimestamp(now, KST).date() else "내일"
    label = ev.get("label") or ev.get("type") or "경제지표"
    title = f"⏰ <b>미국 {label} 지표 발표</b>"
    if _width(f"⏰ 미국 {label} 지표 발표") > 32:
        title = f"⏰ <b>미국 {label} 발표</b>"
    lines = [_SEP, title, f"🕘 {day} {kst:%H:%M} (한국시간)"]
    data = release_lines(ev, ev_utc, rows or [])
    if data:
        lines += [_SEP, "📊 예상·직전"] + data
    else:
        why = _WHY.get(ev.get("type"))
        if why:
            lines.append(f"📌 {why}")
    imp = impact_lines(ev, ev_utc, rows or [])
    if imp:
        lines += [_SEP, "📈 코인 시장 영향"] + imp
    lines.append(_SEP)
    return "\n".join(lines)


def due_events(events: list, now: float, types, window_min: float, sent: set) -> list:
    """지금 보내야 할 (ev, ev_utc) 목록 — 대상 타입 · 발표 전 window 분 이내 · 미발송."""
    out = []
    for ev in events or []:
        if ev.get("type") not in types:
            continue
        key = f"{ev.get('type')}|{ev.get('date')}"
        if key in sent:
            continue
        from monitor import macro
        ev_utc = macro.event_datetime_utc(ev)
        if ev_utc is None:
            continue
        lead = ev_utc.timestamp() - now
        if 0 < lead <= window_min * 60:
            out.append((ev, ev_utc))
    return out


# 발표일의 기준 = ForexFactory 실제 일정(10-09 리뷰: monitor/macro 규칙 일정 "둘째 화요일+1일" 이
# 정적 일정표와 매달 하루씩 어긋남 — CPI 11-11 vs 11-12, NFP 2027-01-01(공휴일) vs 01-08 → 중요 알림이
# 하루 일찍 나갈 수 있었다). FF 의 '대표 행' 제목으로 타입·시각을 정한다. FF 를 못 받으면 규칙 일정 폴백.
_FF_ANCHOR = {"CPI y/y": "CPI", "Non-Farm Employment Change": "NFP", "Federal Funds Rate": "FOMC",
              "PPI m/m": "PPI", "Core PCE Price Index m/m": "PCE"}
_LABEL = {"CPI": "CPI 소비자물가", "NFP": "비농업 고용", "FOMC": "FOMC 금리결정",
          "PPI": "PPI 생산자물가", "PCE": "PCE 물가"}
META_FF_CACHE = "macro_ff_week_cache"
_FF_TTL_SEC = 6 * 3600


def ff_events(rows: list) -> list:
    """FF 행 → 알림용 이벤트 [{type, date(ET 날짜), label, _utc}] (대표 제목 행만)."""
    out = []
    for e in rows or []:
        tp = _FF_ANCHOR.get((e.get("title") or "").strip())
        if not tp:
            continue
        try:
            d = datetime.fromisoformat(str(e.get("date")))
        except ValueError:
            continue
        out.append({"type": tp, "date": d.date().isoformat(), "label": _LABEL.get(tp, tp),
                    "_utc": d.astimezone(timezone.utc)})
    return out


def _cached_rows(conn, now: float) -> Optional[list]:
    """FF 주간 행 — meta 캐시(6시간). 조회 실패면 None(규칙 일정 폴백 신호)."""
    try:
        cached = json.loads(db.get_meta(conn, META_FF_CACHE) or "null")
    except (TypeError, ValueError):
        cached = None
    if isinstance(cached, dict) and now - float(cached.get("at") or 0) < _FF_TTL_SEC \
            and isinstance(cached.get("rows"), list):
        return cached["rows"]
    rows = fetch_release_rows()
    if not rows:
        return cached.get("rows") if isinstance(cached, dict) and isinstance(cached.get("rows"), list) \
            and now - float(cached.get("at") or 0) < 7 * 86400 else None
    db.set_meta(conn, META_FF_CACHE, json.dumps({"at": now, "rows": rows}))
    return rows


def maybe_send_macro_prealert(db_path: str, now: Optional[float] = None) -> str:
    """반환 "disabled"|"skipped"|"ok"|"failed"."""
    if not _cfg("macro_prealert_enabled", True):
        return "disabled"
    now = time.time() if now is None else now
    try:
        from monitor import macro
        from notify import telegram
        types = set(_cfg("macro_prealert_types", ["CPI", "FOMC", "NFP"]) or [])
        window = float(_cfg("macro_prealert_window_minutes", 35))
        with db.connect(db_path) as conn:
            try:
                sent = set(json.loads(db.get_meta(conn, META_SENT) or "[]"))
            except (TypeError, ValueError):
                sent = set()
            # 규칙 일정상 ±2일 안에 대상 지표가 있을 때만 FF 를 본다(평소 회차는 네트워크 0).
            cal = [ev for ev in macro.get_macro_events(conn) if ev.get("type") in types]
            near = [ev for ev in cal if (macro.event_datetime_utc(ev) is not None
                    and abs(macro.event_datetime_utc(ev).timestamp() - now) <= 2 * 86400)]
            if not near:
                return "skipped"
            rows = _cached_rows(conn, now)
            ff = ff_events(rows or [])
            # 일정표 이벤트마다: FF 에 같은 타입이 ±2일 안에 있으면 FF 시각(실제 일정), 없으면 일정표 시각.
            # (10-09 최종 리뷰: 지난주 FF 캐시·제목 변경·행 누락 때 알림이 통째로 빠지던 문제 — 타입별
            # 매칭으로 바꿔 FF 가 확인해 주지 못하면 일정표로 폴백.) 중복 방지 키는 '타입|연-월'
            # (CPI·NFP·FOMC 는 월 1회 이하) — FF·일정표 날짜가 하루 달라도 같은 달이면 한 번만.
            todo, used = [], set()
            for ev in near:
                cal_utc = macro.event_datetime_utc(ev)
                m = [f for f in ff if f["type"] == ev["type"]
                     and abs(f["_utc"].timestamp() - cal_utc.timestamp()) <= 2 * 86400]
                pick, pick_utc = (m[0], m[0]["_utc"]) if m else (ev, cal_utc)
                key = f"{pick['type']}|{pick['date'][:7]}"
                if key in sent or key in used:
                    continue
                if 0 < pick_utc.timestamp() - now <= window * 60:
                    todo.append((pick, pick_utc))
                    used.add(key)
            conn.commit()
            if not todo:
                return "skipped"
            status = "ok"
            rows = rows or []
            for ev, ev_utc in todo:
                ok = telegram.send(build_message(ev, ev_utc, now, rows), urgency="high")
                if ok:
                    sent.add(f"{ev.get('type')}|{str(ev.get('date'))[:7]}")
                    logger.info("[macro] 발표 30분 전 알림 발송: %s %s", ev.get("type"), ev.get("date"))
                else:
                    status = "failed"
                    logger.warning("[macro] 발표 전 알림 발송 실패: %s %s", ev.get("type"), ev.get("date"))
            # 날짜순으로 최근 30개만(10-09 리뷰: 문자열 정렬이면 'CPI|…' 가 늘 앞이라 먼저 잘려 재발송됐다).
            db.set_meta(conn, META_SENT, json.dumps(sorted(sent, key=lambda k: k.split("|")[-1])[-30:]))
            conn.commit()
            return status
    except BaseException as e:  # noqa: BLE001 - 회차 격리
        if isinstance(e, (KeyboardInterrupt, SystemExit)):
            raise
        logger.warning("[macro] 발표 전 알림 실패(무시): %s: %s", type(e).__name__, e)
        return "failed"
