"""신호 품질 측정 — IC(정보계수), ICIR(IC 대비 변동) 및 시간대별·요일별 성과.

배경: "등급이 높을수록 결과가 좋은가"만으로는 산식 진단이 부족하다. IC 는 "점수가
실제 결과와 얼마나 동행하는가"를 연속값으로 재는 표준 척도(Grinold & Kahn,
Active Portfolio Management)이며, ICIR=IC/σ(IC) 는 그 안정성을 잰다. IC≥0.05,
ICIR≥0.5 이면 실전 유효(관례). 시간대·요일 분석은 행동적 에지를 수치화한다.

analytics/calibration.py · ranking.py 와 같은 "프로젝트 모듈 import 0" 원칙 — 순환
import 차단 + DB·수집기 없이 손계산 단위 테스트. 행 데이터는 호출부(SELECT)가 공급.

**표기 전용 원칙**: 여기 결과는 리포트 문구로만 나간다. 등급 산식·알림 필터·엔트리
알림 양식은 이 모듈의 결과로 절대 바뀌지 않는다.
"""

import math

HIT_OUTCOMES = ("hit",)
CLOSED_OUTCOMES = ("hit", "miss", "timeboxed_win", "timeboxed_loss")


def _field(row, key: str, idx: int, default=None):
    """dict / sqlite3.Row / 튜플 아무거나 받는다."""
    try:
        return row[key] if row[key] is not None else default
    except (TypeError, KeyError, IndexError):
        pass
    try:
        return row[idx] if row[idx] is not None else default
    except (TypeError, KeyError, IndexError):
        return default


# ── IC / ICIR ──────────────────────────────────────────────────────────

def _spearman_rank_corr(xs, ys):
    """Spearman 순위 상관. n<5 이면 None."""
    n = len(xs)
    if n < 5:
        return None

    def _rank(vals):
        indexed = sorted(range(n), key=lambda i: vals[i])
        ranks = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j < n - 1 and vals[indexed[j + 1]] == vals[indexed[j]]:
                j += 1
            avg_rank = (i + j) / 2.0 + 1.0
            for k in range(i, j + 1):
                ranks[indexed[k]] = avg_rank
            i = j + 1
        return ranks

    rx, ry = _rank(xs), _rank(ys)
    d2 = sum((a - b) ** 2 for a, b in zip(rx, ry))
    return 1.0 - 6.0 * d2 / (n * (n * n - 1.0))


def compute_ic(rows) -> dict:
    """전체 종결 행에서 IC(score vs ret_24h Spearman 순위 상관) 계산.

    rows: 각 행은 {score: float, ret_24h: float, ...} 또는 동일 인덱스 튜플.
          score = 등급 점수(높을수록 좋은 신호), ret_24h = 24시간 수익률(%).

    반환: {"ic": float|None, "n": int}
    """
    scores, rets = [], []
    for r in rows:
        s = _field(r, "score", 0)
        ret = _field(r, "ret_24h", 1)
        if s is not None and ret is not None:
            scores.append(float(s))
            rets.append(float(ret))

    ic = _spearman_rank_corr(scores, rets)
    return {"ic": round(ic, 4) if ic is not None else None, "n": len(scores)}


def compute_icir(weekly_ics) -> dict:
    """주간 IC 시계열에서 ICIR = mean(IC) / std(IC) 계산.

    weekly_ics: IC 값 리스트 (None 제외한 유효값만).

    반환: {"icir": float|None, "mean_ic": float|None, "std_ic": float|None, "weeks": int}
    """
    valid = [x for x in weekly_ics if x is not None]
    n = len(valid)
    if n < 4:
        return {"icir": None, "mean_ic": None, "std_ic": None, "weeks": n}

    mean = sum(valid) / n
    var = sum((x - mean) ** 2 for x in valid) / (n - 1)
    std = math.sqrt(var) if var > 0 else 0.0

    icir = mean / std if std > 0 else None
    return {
        "icir": round(icir, 3) if icir is not None else None,
        "mean_ic": round(mean, 4),
        "std_ic": round(std, 4),
        "weeks": n,
    }


# ── 시간대별 · 요일별 성과 ────────────────────────────────────────────

def compute_hourly_performance(rows) -> list:
    """시간대별(KST 0~23시) 적중률과 표본수.

    rows: 각 행은 {touched_hour_kst: int, outcome: str, ...} 또는 동일 인덱스 튜플.
          touched_hour_kst = 터치 발생 KST 시각(0~23).
          outcome ∈ CLOSED_OUTCOMES.

    반환: [{"hour": int, "n": int, "hits": int, "rate": float}, ...] (24개, 정렬)
    """
    buckets = {h: {"n": 0, "hits": 0} for h in range(24)}

    for r in rows:
        h = _field(r, "touched_hour_kst", 0)
        outcome = _field(r, "outcome", 1, "")
        if h is None or outcome not in CLOSED_OUTCOMES:
            continue
        h = int(h) % 24
        buckets[h]["n"] += 1
        if outcome in HIT_OUTCOMES:
            buckets[h]["hits"] += 1

    result = []
    for h in range(24):
        b = buckets[h]
        rate = b["hits"] / b["n"] if b["n"] > 0 else 0.0
        result.append({"hour": h, "n": b["n"], "hits": b["hits"],
                        "rate": round(rate, 3)})
    return result


def compute_weekday_performance(rows) -> list:
    """요일별(월=0 ~ 일=6) 적중률과 표본수.

    rows: 각 행은 {touched_weekday: int, outcome: str, ...} 또는 동일 인덱스 튜플.
          touched_weekday = 터치 발생 요일(0=월 ~ 6=일, KST 기준).

    반환: [{"weekday": int, "label": str, "n": int, "hits": int, "rate": float}, ...] (7개)
    """
    labels = ["월", "화", "수", "목", "금", "토", "일"]
    buckets = {d: {"n": 0, "hits": 0} for d in range(7)}

    for r in rows:
        wd = _field(r, "touched_weekday", 0)
        outcome = _field(r, "outcome", 1, "")
        if wd is None or outcome not in CLOSED_OUTCOMES:
            continue
        wd = int(wd) % 7
        buckets[wd]["n"] += 1
        if outcome in HIT_OUTCOMES:
            buckets[wd]["hits"] += 1

    result = []
    for d in range(7):
        b = buckets[d]
        rate = b["hits"] / b["n"] if b["n"] > 0 else 0.0
        result.append({"weekday": d, "label": labels[d], "n": b["n"],
                        "hits": b["hits"], "rate": round(rate, 3)})
    return result


def summarize_best_worst(hourly, weekday, min_n=5) -> dict:
    """시간대·요일 분석 요약 — 적중률 최고/최저 구간.

    min_n: 이 미만 표본 구간은 판정에서 제외.

    반환: {"best_hour": dict|None, "worst_hour": dict|None,
           "best_day": dict|None, "worst_day": dict|None}
    """
    valid_h = [h for h in hourly if h["n"] >= min_n]
    valid_d = [d for d in weekday if d["n"] >= min_n]

    def _best(items, key="rate"):
        return max(items, key=lambda x: x[key]) if items else None

    def _worst(items, key="rate"):
        return min(items, key=lambda x: x[key]) if items else None

    return {
        "best_hour": _best(valid_h),
        "worst_hour": _worst(valid_h),
        "best_day": _best(valid_d),
        "worst_day": _worst(valid_d),
    }
