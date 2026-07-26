"""등급 캘리브레이션 — "우리 등급 산식이 실제로 맞는가"의 사후 검증 (기획 카드 #26).

배경: collector/grading.py 의 배점은 설계 당시의 가설이다. 적중 DB(levels)가 쌓인
지금은 그 가설을 실측으로 되짚어볼 수 있다 — 등급별 TP1 도달률이 정말 S>A>B>C>D
순서인가. 이 모듈은 그 계산만 한다.

**표기 전용 원칙**: 여기서 나온 위반 신호는 리포트 문구로만 나간다. 배점·알림 필터·
엔트리 알림 양식은 이 모듈의 결과로 절대 바뀌지 않는다(관찰기 동결 준수). 산식을
자동으로 되먹임하면 표본 5~10건대 노이즈에 등급이 끌려다니고, 과거 알림과 지금
알림의 등급 의미가 달라져 적중 DB 자체가 비교 불가능해진다.

**축 분리**: analytics/ranking.py 의 E_LB 는 '작성자가 잘하는가'(사람 축)를 재고,
이 모듈은 '등급 라벨이 맞는가'(산식 축)를 잰다. 두 숫자는 섞지 않는다.

analytics/ranking.py·clustering.py 와 같은 "프로젝트 모듈 import 0" 원칙 —
순환 import 차단 + DB·수집기 없이 손계산으로 단위 테스트(scripts/test_ranking.py).
표준 라이브러리는 math 만 쓴다(외부 API 호출 0, 추가 의존성 0).
행 데이터는 호출부(scripts/show_status.py·run_weekly_report.py 의 SELECT)가 공급한다.
"""

import math

# 등급 순서(상위→하위). collector/grading.GRADE_ORDER 의 사본이다 — import 0 원칙
# 때문에 참조하지 않는다. 등급 체계가 바뀌면 grade_order 인자로 넘기면 된다.
GRADE_ORDER = ("S", "A", "B", "C", "D")

# TP1 도달로 인정하는 outcome. timeboxed_win 은 '판정창이 끝난 시점에 수익권'일 뿐
# 목표가를 실제로 찍은 건이 아니라 분자에서 뺀다 — 이 지표가 재는 건 오직
# "그 등급이 예고한 목표가에 정말 닿았는가" 하나다. 분모(종결 표본)에는 포함한다.
HIT_OUTCOMES = ("hit",)
CLOSED_OUTCOMES = ("hit", "miss", "timeboxed_win", "timeboxed_loss")

DEFAULT_Z = 1.96      # 95% 양측 정규분위수(관례적 반올림값)
DEFAULT_MIN_N = 5     # 이 미만이면 '표본 부족, 참고용' — 단조성 판정에서 제외


def _field(row, key: str, idx: int, default=None):
    """dict / sqlite3.Row / 튜플 아무거나 받는다 — 호출부(SELECT 결과)와 손계산
    단위 테스트(튜플 리터럴)가 같은 함수를 쓰게 하려는 목적."""
    try:
        return row[key]
    except (TypeError, KeyError, IndexError):
        pass
    try:
        return row[idx]
    except (TypeError, KeyError, IndexError):
        return default


def wilson_interval(hits: int, n: int, z: float = DEFAULT_Z):
    """이항비율 hits/n 의 Wilson score 신뢰구간. n=0 이면 (None, None).

        center = (p + z²/2n) / (1 + z²/n)
        half   = z/(1 + z²/n) · √( p(1−p)/n + z²/4n² )

    Wald(정규근사)를 쓰지 않는 이유: 지금 등급별 표본은 4~12건대이고 0/4·6/6 같은
    극단 비율이 실제로 나온다. Wald 는 그 지점에서 폭이 0 으로 붕괴하거나 구간이
    [0,1] 밖으로 새어나가 '표본이 적다'는 사실 자체를 감춘다. Wilson 은 소표본·극단
    비율에서도 항상 유한 폭을 주고 구간이 [0,1] 안에 머문다.
    (클램프는 부동소수 오차로 −1e−17 같은 값이 나오는 것까지 막는 용도.)
    """
    if not n or n <= 0:
        return None, None
    p = hits / n
    zz = z * z
    denom = 1.0 + zz / n
    center = (p + zz / (2 * n)) / denom
    half = (z / denom) * math.sqrt(p * (1 - p) / n + zz / (4 * n * n))
    return max(0.0, center - half), min(1.0, center + half)


def confidence_pct(z: float = DEFAULT_Z) -> float:
    """z(양측 정규분위수) → 신뢰수준 %. z=1.96 → 95.0. 리포트 문구용 —
    'z=1.96' 보다 '95%' 가 읽는 사람에게 훨씬 정확하게 전달된다."""
    return math.erf(z / math.sqrt(2.0)) * 100.0


def _bucket(hits: int, n: int, ambiguous: int, z: float, min_n: int) -> dict:
    lo, hi = wilson_interval(hits, n, z)
    return {
        "n": n,
        "hits": hits,
        # 동시터치(TP·SL 같은 구간 도달)로 보수적 miss 처리된 건수. 분모/분자 취급은
        # 판정 결과 그대로 두되(다른 통계와 축을 맞춘다), 이 등급의 도달률이 얼마나
        # '판별 한계' 때문에 눌려 있는지 읽는 사람이 알 수 있게 건수만 따로 센다.
        "ambiguous": ambiguous,
        "rate": (hits / n) if n else None,
        "ci_low": lo,
        "ci_high": hi,
        "enough": n >= min_n,
    }


def find_monotonicity_violations(buckets: dict, grade_order=GRADE_ORDER,
                                 min_n: int = DEFAULT_MIN_N) -> list:
    """'상위 등급이 하위 등급보다 실측이 좋아야 한다'는 전제의 위반 목록.

    판정 기준(2026-07-27 확정):
    - 표본 min_n 이상인 등급끼리만 비교한다. 소표본은 1건 차이로 순위가 뒤집혀
      위반을 만들어내기 너무 쉽다 — 판정에서 빼고 '참고용' 표기만 남긴다.
    - 인접 쌍뿐 아니라 **모든 상위/하위 쌍**을 본다. S 와 D 처럼 건너뛴 역전도
      배점 문제 신호이며, 인접만 보면 S>C>D 인데 S<D 인 상황을 놓친다.
    - lower.rate > higher.rate 이면 '역전'. 그중 두 신뢰구간이 전혀 겹치지 않으면
      (lower.ci_low > higher.ci_high) significant=True — 표본 노이즈만으로는 설명하기
      어려운 역전이라는 뜻이다. 겹치면 약한 신호로 남긴다(둘 다 보고는 한다).

    반환: [{"higher","lower","higher_rate","lower_rate","significant"}, ...]
          (상위 등급 순 → 하위 등급 순으로 결정적 정렬)
    """
    order = list(grade_order)
    usable = [g for g in order
              if buckets.get(g) and buckets[g]["n"] >= min_n and buckets[g]["rate"] is not None]
    out = []
    for i, higher in enumerate(usable):
        for lower in usable[i + 1:]:
            hb, lb = buckets[higher], buckets[lower]
            if lb["rate"] <= hb["rate"]:
                continue
            out.append({
                "higher": higher,
                "lower": lower,
                "higher_rate": hb["rate"],
                "lower_rate": lb["rate"],
                "significant": lb["ci_low"] > hb["ci_high"],
            })
    return out


def calibrate_grades(rows, grade_order=GRADE_ORDER, z: float = DEFAULT_Z,
                     min_n: int = DEFAULT_MIN_N) -> dict:
    """종결 표본 원시 행 → 등급별 TP1 도달률 + Wilson CI + 단조성 판정.

    rows: [(grade, outcome, ambiguous), ...] 또는 같은 키를 가진 dict/sqlite3.Row 목록.
          grade 는 **수집 시점에 DB 에 기록된 등급**을 그대로 쓴다(사후분석용 원본).
          지금 배점으로 다시 채점하면 과거 알림이 왜 나갔는지와 무관한 숫자가 되고,
          배점을 손댈 때마다 과거 실적이 통째로 바뀌어 비교 자체가 불가능해진다.
    미종결(outcome NULL)·미채점(grade NULL)·정의 밖 등급 행은 조용히 건너뛴다.

    반환: {order, buckets:{등급: {n,hits,rate,ci_low,ci_high,ambiguous,enough}},
           pooled, violations, significant, monotonic, eligible, min_n, z}
      - monotonic: 판정 가능한 등급쌍에서 역전이 하나도 없음
      - eligible : 표본 min_n 이상이라 판정에 쓰인 등급 수 (2 미만이면 판정 보류)
    """
    counts = {g: [0, 0, 0] for g in grade_order}  # [n, hits, ambiguous]
    for row in rows or []:
        grade = _field(row, "grade", 0)
        outcome = _field(row, "outcome", 1)
        ambiguous = _field(row, "ambiguous", 2, 0)
        if grade not in counts or outcome not in CLOSED_OUTCOMES:
            continue
        c = counts[grade]
        c[0] += 1
        c[1] += 1 if outcome in HIT_OUTCOMES else 0
        c[2] += 1 if ambiguous else 0

    buckets = {g: _bucket(c[1], c[0], c[2], z, min_n) for g, c in counts.items()}
    pooled = _bucket(sum(c[1] for c in counts.values()),
                     sum(c[0] for c in counts.values()),
                     sum(c[2] for c in counts.values()), z, min_n)
    violations = find_monotonicity_violations(buckets, grade_order, min_n)
    eligible = sum(1 for g in grade_order if buckets[g]["n"] >= min_n)
    return {
        "order": list(grade_order),
        "buckets": buckets,
        "pooled": pooled,
        "violations": violations,
        "significant": sum(1 for v in violations if v["significant"]),
        "monotonic": not violations,
        "eligible": eligible,
        "min_n": min_n,
        "z": z,
    }
