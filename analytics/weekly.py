"""주간 리포트 v2 (2026-09-22 R4) — "지표 나열형 → 의사결정형" 개편의 순수 계산부.

근거: izrua_company/plan_2026-09-22_최종버전_종합검토.md §3 R4 ·
research_2026-09-22_external_final_review.md §3(TradeZella 30분 주간 리뷰 6단계,
freqtrade `/stats` exit-reason 집계) · research_2026-09-22_db_final_review.md
§2-1(결과 확인은 터치 후 최소 7일).

설계 원칙은 analytics/ranking.py·calibration.py 와 동일하다:
  - **프로젝트 모듈 import 0** (순환 import 차단 + DB 없이 손계산 단위 테스트).
    표준 라이브러리만 쓴다.
  - **표시 전용** — 여기서 나온 어떤 숫자도 등급 산식·알림 필터·판정에 되먹임되지
    않는다. 사람이 주 1회 읽고 "조정을 할지 말지"를 판단하는 자리다.
  - 행 데이터는 호출부(storage.db.get_resolved_rows_between)가 공급한다.

n 규칙: 비율·평균 지표는 표본이 SMALL_N(10) 미만이면 "참고"로만 표기하고 지난주
대비 화살표를 붙이지 않는다(소표본에서 화살표는 노이즈를 방향으로 착각하게 만든다).
관찰 줄 후보는 양쪽 그룹 n≥10 을 요구한다(작성자 극단만 예외 n≥5 — 주 1회 창에서
작성자당 10건은 사실상 도달 불가라 별도 임계를 둔다).
"""

import math

WIN_OUTCOMES = ("hit", "timeboxed_win")
LOSS_OUTCOMES = ("miss", "timeboxed_loss")
CLOSED_OUTCOMES = WIN_OUTCOMES + LOSS_OUTCOMES

# 판정 사유 표시 순서·라벨 (freqtrade `/stats` 의 exit reason 표와 동형)
OUTCOME_ORDER = ("hit", "timeboxed_win", "miss", "timeboxed_loss")
OUTCOME_LABELS = {
    "hit": "적중(TP)",
    "timeboxed_win": "만료·수익",
    "miss": "손절",
    "timeboxed_loss": "만료·손실",
}

# 실현 수익률 이상치 컷 (%). db_final_review §1-3 과 동일한 방어 —
# 진입가 오염(자릿수 오파싱) 몇 건이 평균을 통째로 뒤집는다.
MAX_ABS_RET_PCT = 50.0

SMALL_N = 10          # 이 미만이면 화살표 생략 + "참고" 표기
AUTHOR_MIN_N = 5      # 작성자 극단 관찰 전용 임계
DAY = 86400.0


# ── 행 단위 파생값 ────────────────────────────────────────────────────

def realized_pct(row) -> float:
    """터치가→종결가 실현 수익률(%). direction='short' 는 부호 반전.
    |수익률| > MAX_ABS_RET_PCT 이거나 가격이 결측/0 이면 None(표본에서 제외)."""
    tp = row.get("touch_price_krw")
    rp = row.get("resolve_price_krw")
    if tp is None or rp is None:
        return None
    try:
        tp = float(tp)
        rp = float(rp)
    except (TypeError, ValueError):
        return None
    if tp <= 0:
        return None
    ret = (rp / tp - 1.0) * 100.0
    if (row.get("direction") or "long") == "short":
        ret = -ret
    if not math.isfinite(ret) or abs(ret) > MAX_ABS_RET_PCT:
        return None
    return ret


def holding_hours(row) -> float:
    """터치→종결 경과(h). 둘 중 하나라도 결측이거나 음수면 None."""
    t = row.get("touched_at")
    r = row.get("resolved_at")
    if t is None or r is None:
        return None
    h = (float(r) - float(t)) / 3600.0
    return h if h >= 0 else None


def touch_delay_min(row) -> float:
    """수집→터치 지연(분). 시간 역전 행(db_final_review §8, 6건)은 None."""
    c = row.get("collected_at")
    t = row.get("touched_at")
    if c is None or t is None:
        return None
    m = (float(t) - float(c)) / 60.0
    return m if m >= 0 else None


def _closed(rows) -> list:
    return [r for r in (rows or []) if r.get("outcome") in CLOSED_OUTCOMES]


def _mean(vals):
    vals = [v for v in vals if v is not None]
    return (sum(vals) / len(vals)) if vals else None


# ── ② 이번 주 한눈에 ──────────────────────────────────────────────────

def summary(rows, alerts: int = None) -> dict:
    """한 창(7일)의 요약 지표.

    반환: {alerts, closed, wins, losses, win_rate, win_n, pf, avg_r, r_n}
    - win_rate 분모는 승/패로 분류된 건만(현재 4 outcome 전부가 승/패라 closed 와 같다).
    - PF·평균 R 은 **r_multiple 이 산출된 표본만**이다(SL 결측 52% — db_final_review
      §1-2). 그래서 r_n 을 항상 같이 돌려주고, 리포트는 n 을 병기한다.
    """
    closed = _closed(rows)
    wins = sum(1 for r in closed if r["outcome"] in WIN_OUTCOMES)
    losses = sum(1 for r in closed if r["outcome"] in LOSS_OUTCOMES)
    rs = []
    for r in closed:
        v = r.get("r_multiple")
        if v is not None:
            try:
                fv = float(v)
            except (TypeError, ValueError):
                continue
            if math.isfinite(fv):
                rs.append(fv)
    gross_win = sum(v for v in rs if v > 0)
    gross_loss = -sum(v for v in rs if v < 0)
    return {
        "alerts": alerts,
        "closed": len(closed),
        "wins": wins,
        "losses": losses,
        "win_n": wins + losses,
        "win_rate": (wins / (wins + losses)) if (wins + losses) else None,
        "pf": (gross_win / gross_loss) if gross_loss > 0 else None,
        "avg_r": (sum(rs) / len(rs)) if rs else None,
        "r_n": len(rs),
    }


def arrow(cur, prev) -> str:
    """지난주 대비 화살표. 비교 불가(한쪽 결측)면 None."""
    if cur is None or prev is None:
        return None
    if cur > prev:
        return "▲"
    if cur < prev:
        return "▼"
    return "→"


# ── ⑤ 판정 사유별 집계 (freqtrade /stats 형) ──────────────────────────

def outcome_stats(rows) -> dict:
    """판정 사유(outcome)별 n · 비중 · 평균 보유시간 · 평균 실현%.

    반환: {"total": n, "rows": [{outcome, label, n, share, hold_h, ret_pct, ret_n}, ...]}
    표본 0 인 사유는 행을 만들지 않는다(calibration.py 관례).
    """
    closed = _closed(rows)
    total = len(closed)
    out = []
    for oc in OUTCOME_ORDER:
        grp = [r for r in closed if r["outcome"] == oc]
        if not grp:
            continue
        rets = [realized_pct(r) for r in grp]
        rets = [v for v in rets if v is not None]
        out.append({
            "outcome": oc,
            "label": OUTCOME_LABELS.get(oc, oc),
            "n": len(grp),
            "share": len(grp) / total,
            "hold_h": _mean([holding_hours(r) for r in grp]),
            "ret_pct": _mean(rets),
            "ret_n": len(rets),
        })
    return {"total": total, "rows": out}


# ── ③ 관찰 3줄 (규칙 기반 자동 선택) ──────────────────────────────────
#
# 후보 풀에서 "격차가 크고 양쪽 n 이 충분한" 순으로 최대 3개를 고른다.
# gap 은 **%p 로 통일**한 비교 가능 스케일이다(보유시간 이상치만 상대편차 %).
# 데이터가 지지하지 않으면 후보를 만들지 않는다 = 그 주는 조용히 줄이 준다.

def _win_rate(rows):
    closed = _closed(rows)
    wins = sum(1 for r in closed if r["outcome"] in WIN_OUTCOMES)
    return (wins / len(closed), len(closed)) if closed else (None, 0)


def _cand_outcome_mix(cur_rows, prev_rows, min_n=SMALL_N):
    """(a) 판정 사유 구성 변화 — 이번 주 vs 지난주 비중 ±10%p 이상."""
    cur, prev = _closed(cur_rows), _closed(prev_rows)
    if len(cur) < min_n or len(prev) < min_n:
        return None
    best = None
    for oc in OUTCOME_ORDER:
        c = sum(1 for r in cur if r["outcome"] == oc) / len(cur)
        p = sum(1 for r in prev if r["outcome"] == oc) / len(prev)
        d = (c - p) * 100.0
        if best is None or abs(d) > abs(best["delta_pp"]):
            best = {"kind": "outcome_mix", "outcome": oc,
                    "label": OUTCOME_LABELS.get(oc, oc),
                    "cur": c, "prev": p, "delta_pp": d,
                    "n_cur": len(cur), "n_prev": len(prev)}
    if best is None or abs(best["delta_pp"]) < 10.0:
        return None
    best["gap"] = abs(best["delta_pp"])
    return best


def _cand_split(pool_rows, key, label_a, label_b, pred_a, pred_b, min_n=SMALL_N):
    """두 그룹 승률 격차 공통 헬퍼 — 양쪽 n≥min_n 일 때만 후보가 된다."""
    a = [r for r in _closed(pool_rows) if pred_a(r)]
    b = [r for r in _closed(pool_rows) if pred_b(r)]
    if len(a) < min_n or len(b) < min_n:
        return None
    ra, na = _win_rate(a)
    rb, nb = _win_rate(b)
    if ra is None or rb is None:
        return None
    return {"kind": key, "label_a": label_a, "label_b": label_b,
            "rate_a": ra, "rate_b": rb, "n_a": na, "n_b": nb,
            "gap": abs(ra - rb) * 100.0}


def _cand_delay(pool_rows, min_n=SMALL_N):
    """(b) 수집→터치 지연 <30분 vs 이상 승률 격차."""
    def fast(r):
        d = touch_delay_min(r)
        return d is not None and d < 30.0

    def slow(r):
        d = touch_delay_min(r)
        return d is not None and d >= 30.0

    return _cand_split(pool_rows, "delay", "지연 &lt;30분", "30분 이상",
                       fast, slow, min_n)


def _cand_volume_rank(pool_rows, min_n=SMALL_N):
    """(c) 거래대금 순위 1-20 vs 100+ 승률 격차 (touch_volume_rank)."""
    def top(r):
        v = r.get("touch_volume_rank")
        return v is not None and 1 <= v <= 20

    def tail(r):
        v = r.get("touch_volume_rank")
        return v is not None and v >= 100

    return _cand_split(pool_rows, "volume_rank", "거래대금 1-20위", "100위 밖",
                       top, tail, min_n)


def _cand_authors(cur_rows, min_n=AUTHOR_MIN_N):
    """(d) 이번 주 최고/최저 작성자 (종결 n≥5, 2명 이상 자격 시에만)."""
    by = {}
    for r in _closed(cur_rows):
        a = r.get("author")
        if a:
            by.setdefault(a, []).append(r)
    elig = []
    for a, rows in by.items():
        rate, n = _win_rate(rows)
        if n >= min_n and rate is not None:
            elig.append((a, rate, n))
    if len(elig) < 2:
        return None
    elig.sort(key=lambda x: x[1], reverse=True)
    top, low = elig[0], elig[-1]
    return {"kind": "authors", "top": top[0], "top_rate": top[1], "top_n": top[2],
            "low": low[0], "low_rate": low[1], "low_n": low[2],
            "gap": (top[1] - low[1]) * 100.0}


def _cand_hold_outlier(pool_rows, min_n=SMALL_N, min_dev_pct=50.0):
    """(e) 판정 사유별 평균 보유시간의 이상치 — 전체 평균 대비 상대편차 최대.

    **만료(timeboxed_*) 판정은 제외**한다(CTO 디버깅 2026-09-22): 만료는 정의상
    판정창 끝까지 간 건이라 언제나 평균의 2배 이상 — 매주 같은 동어반복 줄이 1순위로
    뽑혔다. 비교는 TP 도달 vs SL 도달(hit/miss)끼리만 = freqtrade 의 "승자 vs 패자
    보유시간" 대비와 같은 뜻이다. gap 은 상대편차를 4로 나눠 %p 후보들과 같은
    자릿수로 맞춘다(편차 +100% ≈ 25%p 격차 취급) — 안 그러면 이 후보가 항상 1순위."""
    closed = [r for r in _closed(pool_rows) if r["outcome"] in ("hit", "miss")]
    overall = _mean([holding_hours(r) for r in closed])
    if not overall or overall <= 0 or len(closed) < min_n:
        return None
    best = None
    for oc in ("hit", "miss"):
        grp = [r for r in closed if r["outcome"] == oc]
        if len(grp) < min_n:
            continue
        m = _mean([holding_hours(r) for r in grp])
        if m is None:
            continue
        dev = (m - overall) / overall * 100.0
        if best is None or abs(dev) > abs(best["dev_pct"]):
            best = {"kind": "hold_outlier", "outcome": oc,
                    "label": OUTCOME_LABELS.get(oc, oc),
                    "hold_h": m, "overall_h": overall, "dev_pct": dev, "n": len(grp)}
    if best is None or abs(best["dev_pct"]) < min_dev_pct:
        return None
    best["gap"] = abs(best["dev_pct"]) / 4.0
    return best


def observations(cur_rows, prev_rows, pool_rows, limit: int = 3,
                 min_n: int = SMALL_N) -> list:
    """관찰 후보 풀 → 격차 큰 순 최대 `limit` 개. 후보가 없으면 빈 목록.

    pool_rows 는 **최근 4주 누적**을 받는다(1주 창은 그룹당 n≥10 을 거의 못 넘는다).
    (a) 만 이번 주/지난주 비교라 cur/prev 를 쓴다.
    """
    cands = [
        _cand_outcome_mix(cur_rows, prev_rows, min_n),
        _cand_delay(pool_rows, min_n),
        _cand_volume_rank(pool_rows, min_n),
        _cand_authors(cur_rows),
        _cand_hold_outlier(pool_rows, min_n),
    ]
    cands = [c for c in cands if c]
    cands.sort(key=lambda c: c["gap"], reverse=True)
    return cands[:limit]


# ── ④ 다음 판단 (표본 도달 마일스톤) ──────────────────────────────────

def milestone(count: int, target: int, per_day: float = None) -> dict:
    """진행률 + 예상 소요일. per_day 는 최근 30일 누적 속도(건/일).

    **예상일은 속도가 유지된다는 가정의 산술 외삽**일 뿐이다 — 리포트에도 그렇게
    적는다. 도달 후에 조정을 할지 말지는 사람이 정한다(자동 조정 없음)."""
    count = int(count or 0)
    remain = max(target - count, 0)
    eta = None
    if remain and per_day and per_day > 0:
        eta = int(math.ceil(remain / per_day))
    return {"count": count, "target": int(target), "remain": remain,
            "done": remain == 0, "eta_days": eta,
            "ratio": (count / target) if target else None}
