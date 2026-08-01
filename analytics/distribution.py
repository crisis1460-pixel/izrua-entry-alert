"""R-멀티플 분포 · 보유기간(홀딩) 분석 — 사후 관찰용 순수 통계
(2026-08-01 내부기능강화 리서치 영역3·4, izrua_company/research_2026-08-01_internal_features.md).

배경: r_multiple(윈저라이즈 -1~+5)·touched_at/resolved_at 은 판정마다 이미 저장되지만
분포 형태로 집계·표시된 적이 없다. 이 모듈은 "손실은 짧게, 수익은 길게 가져가고
있는가"(R분포), "판정창 막바지까지 끄는 신호가 성적이 나쁜가"(보유기간)를 숫자로
보여준다. analytics.calibration 과 같은 표기 전용 원칙 — 여기 결과는 리포트 문구로만
나가고 등급 산식·알림 필터·엔트리 알림 양식은 절대 바뀌지 않는다.

analytics/calibration.py·ranking.py 와 같은 "프로젝트 모듈 import 0" 원칙 — 표준
라이브러리도 안 쓴다(외부 API 호출 0, 추가 의존성 0). 행 데이터는 호출부
(scripts/run_weekly_report.py·show_status.py 의 SELECT)가 공급한다.
"""

# R-멀티플 구간 — r_clip_low/high 기본값(-1.0~5.0, config/settings.py)을 그대로 덮는다.
# 상한 구간은 +inf 로 열어 클립값이 바뀌어도 항상 전체를 담는다.
R_BUCKETS = ((-1.0, 0.0, "-1~0"), (0.0, 1.0, "0~1"), (1.0, 2.0, "1~2"),
            (2.0, 3.0, "2~3"), (3.0, float("inf"), "3+"))

# 보유기간 구간(시간) — ret_24h/ret_72h 와 동일한 24h/72h 경계를 재사용한다(카드 관례 일치).
HOLD_BUCKETS = ((0.0, 24.0, "24h 이내"), (24.0, 72.0, "24~72h"),
               (72.0, float("inf"), "72h+"))

HIT_OUTCOMES = ("hit",)
CLOSED_OUTCOMES = ("hit", "miss", "timeboxed_win", "timeboxed_loss")


def _field(row, key: str, idx: int, default=None):
    """dict / sqlite3.Row / 튜플 아무거나 받는다 — calibration.py 와 동일 헬퍼."""
    try:
        return row[key]
    except (TypeError, KeyError, IndexError):
        pass
    try:
        return row[idx]
    except (TypeError, KeyError, IndexError):
        return default


def _bucket_of(value: float, buckets) -> str:
    for lo, hi, label in buckets:
        if lo <= value < hi:
            return label
    return buckets[-1][2]  # 상한 초과(이론상 미발생 — 마지막 구간이 +inf) 방어적 폴백


def r_multiple_distribution(rows, buckets=R_BUCKETS) -> dict:
    """종결 표본 → R-멀티플 구간별 건수 + 평균.

    rows: [(r_multiple, grade), ...] 또는 같은 키를 가진 dict/sqlite3.Row 목록.
    r_multiple 이 None(SL 미기재 tp_only 표본)인 행은 조용히 제외한다 — 이 지표는
    R 트랙 표본만 다룬다(analytics.ranking 의 E_LB 와 동일 축, 2트랙 원칙 일치).

    반환: {"n", "mean", "buckets": [{"label","n"}, ...]}  (buckets 는 구간 정의 순서 그대로)
    n=0 이면 mean=None, 모든 버킷 n=0.
    """
    counts = {label: 0 for _, _, label in buckets}
    total = 0
    s = 0.0
    for row in rows or []:
        r = _field(row, "r_multiple", 0)
        if r is None:
            continue
        counts[_bucket_of(r, buckets)] += 1
        total += 1
        s += r
    return {
        "n": total,
        "mean": (s / total) if total else None,
        "buckets": [{"label": label, "n": counts[label]} for _, _, label in buckets],
    }


def r_distribution_by_grade(rows, grade_order=("S", "A", "B", "C", "D"), buckets=R_BUCKETS) -> dict:
    """등급별 R분포 — "S등급이 실제로 고R 쪽에 몰려있는가"(S10 재조정 근거로도 재사용 가능).

    rows: r_multiple_distribution 과 동일 형식(grade 필드 필요).
    grade 가 grade_order 밖이거나 None 이면 조용히 제외.
    반환: {등급: r_multiple_distribution 결과, ...} — 표본 0인 등급도 키는 유지(n=0).
    """
    by_grade = {g: [] for g in grade_order}
    for row in rows or []:
        g = _field(row, "grade", 1)
        if g in by_grade:
            by_grade[g].append(row)
    return {g: r_multiple_distribution(rs, buckets) for g, rs in by_grade.items()}


def holding_period_distribution(rows, buckets=HOLD_BUCKETS) -> dict:
    """종결 표본 → 보유기간(터치~종결 경과시간) 구간별 건수·hit율.

    rows: [(touched_at, resolved_at, outcome), ...] 또는 같은 키를 가진 dict/sqlite3.Row 목록.
    elapsed_hours = (resolved_at - touched_at) / 3600. 음수(데이터 이상)·outcome 이
    CLOSED_OUTCOMES 밖인 행은 조용히 제외.

    반환: {"n", "buckets": [{"label","n","hits","rate"}, ...]}
    rate 는 그 구간 표본이 0 이면 None.
    """
    counts = {label: [0, 0] for _, _, label in buckets}  # [n, hits]
    total = 0
    for row in rows or []:
        t0 = _field(row, "touched_at", 0)
        t1 = _field(row, "resolved_at", 1)
        outcome = _field(row, "outcome", 2)
        if t0 is None or t1 is None or outcome not in CLOSED_OUTCOMES:
            continue
        elapsed_h = (t1 - t0) / 3600.0
        if elapsed_h < 0:
            continue
        label = _bucket_of(elapsed_h, buckets)
        counts[label][0] += 1
        counts[label][1] += 1 if outcome in HIT_OUTCOMES else 0
        total += 1
    return {
        "n": total,
        "buckets": [
            {"label": label, "n": counts[label][0], "hits": counts[label][1],
             "rate": (counts[label][1] / counts[label][0]) if counts[label][0] else None}
            for _, _, label in buckets
        ],
    }
