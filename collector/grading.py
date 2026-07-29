"""
글(레벨) 등급 산정.

배점(2026-07-29 R:R 제거 확정): 팔로워(1~10) + 가격근접도(0~20)
  + 목표거리(-6~+25, 만능) + 데이터완결성(2~30) = 최대 85점
  등급 임계 S85/A70/B55/C40.

팔로워 배점을 낮게 두는 근거: Kakhbod et al. "Finfluencers"
(팔로워수는 실력의 양(+)신호가 아님).

2026-07-26 개정: SL 없는 글에 목표거리 대체배점(최대 25점)을 도입해 B·A 도달 가능.
2026-07-29 개정: 실전 51건 분석 결과 R:R 상위(S/A) 신호의 실적중률 0%로 확인.
  R:R 은 알림 표시용으로만 쓰고 등급 점수에서 제거. 목표거리 배점은 SL 유무와
  무관하게 전 신호에 적용(has_rr 구분 폐지). 등급 임계는 새 최대점(85)에 맞춰 유지.
"""

from typing import Optional, Tuple

GRADE_ORDER = ["S", "A", "B", "C", "D"]

# 목표거리(TP 거리) 단일 배점표 — (상한 %, 점수). 아래→위 순서로 첫 매칭 구간 적용.
# 음수 구간(=감점)은 R:R 계산 가능 여부와 무관하게 모든 글에 적용되고,
# 양수 구간(=기대보상 대체배점)은 R:R 을 못 재는 글(SL 미기재)에만 적용된다.
# 두 영역은 5% 를 경계로 서로 겹치지 않는다 — 같은 글이 감점과 대체배점을
# 동시에 받는 이중계산/상쇄가 구조적으로 불가능하다.
TP_DISTANCE_BANDS = [
    (2, -6),     # 0~2%   초근접 목표: 왕복 수수료 0.1%+슬리피지 빼면 스윙 실익 없음
    (3, -4),     # 2~3%
    (5, -2),     # 3~5%
    (8, 12),     # 5~8%    현실적이지만 보상 작음
    (15, 20),    # 8~15%   스윙 스윗스팟
    (25, 25),    # 15~25%  최고점
    (40, 18),    # 25~40%  달성 난도 상승 — 감액
    (60, 9),     # 40~60%
    (float("inf"), 3),   # 60%+   'SOL +56.8%, BCH +185.7%' 류 환상적 목표 — 사실상 무배점
]
TP_REWARD_MAX = 25   # 목표거리 최고 배점 (SL 유무 무관. 이론 최고점 = 10+20+25+30 = 85 → S 도달 가능)


def grade_from_score(score: float) -> str:
    if score >= 85:
        return "S"
    if score >= 70:
        return "A"
    if score >= 55:
        return "B"
    if score >= 40:
        return "C"
    return "D"


def tp_distance_points(direction: str, entry: Optional[float], target: Optional[float],
                       has_rr: bool = False) -> float:
    """목표거리 배점(부호 있는 단일 값).

    감점(0~5%)은 모든 신호에 적용. 보상(5%+)도 SL 유무와 무관하게 전 신호에 적용.
    봉우리형 배점(15~25% 최고점 +25)으로 '달성 가능한 큰 목표' vs '환상적 목표' 구분.

    has_rr=True 를 넘기면 감점 구간만 적용하고 보상 구간은 0으로 반환한다.
    이 경로는 price_check._tp_distance_penalty (관찰 역산용)에서만 사용하며,
    등급 채점에서는 항상 has_rr=False(기본값)로 호출된다(2026-07-29 R:R 제거).
    """
    if not (entry and target and entry > 0):
        return 0.0
    tp_pct = ((target - entry) if direction == "long" else (entry - target)) / entry * 100
    for hi, pts in TP_DISTANCE_BANDS:
        if tp_pct < hi:
            return 0.0 if (pts > 0 and has_rr) else float(pts)
    return 0.0


def calculate_grade(
    followers: Optional[float],
    direction: str,
    entry: Optional[float],
    stop_loss: Optional[float],
    target: Optional[float],
    current_usd_price: Optional[float],
) -> Tuple[str, float, Optional[float]]:
    """반환 (grade, score, rr). rr 은 계산 불가 시 None (판단 보류 — 필터에서 제외 금지)."""
    score = 0.0
    rr = None

    f = followers or 0
    if f >= 100_000:
        score += 10
    elif f >= 50_000:
        score += 9
    elif f >= 10_000:
        score += 8
    elif f >= 1_000:
        score += 5
    elif f >= 100:
        score += 3
    else:
        score += 1

    if entry and stop_loss and target:
        if direction == "long":
            risk, reward = entry - stop_loss, target - entry
        else:
            risk, reward = stop_loss - entry, entry - target
        if risk > 0 and reward > 0:
            rr = reward / risk

    if entry and current_usd_price and current_usd_price > 0:
        diff_pct = (current_usd_price - entry) / entry * 100
        if abs(diff_pct) < 2:
            score += 20
        elif -10 <= diff_pct < -2:
            score += 17
        elif 2 <= diff_pct < 5:
            score += 12
        elif 5 <= diff_pct < 10:
            score += 8
        elif diff_pct <= -10:
            score += 15

    # 목표 거리 배점 (2026-07-29 R:R 제거로 전 신호 공통 적용).
    # 감점(0~5%): 초근접 목표 — 왕복 수수료 0.1%+슬리피지 빼면 스윙 실익 없음.
    # 보상(5%+): SL 유무 무관하게 모든 신호에 적용. 배제가 아닌 감점만.
    score += tp_distance_points(direction, entry, target)

    has_entry = entry is not None and entry > 0
    has_stop = stop_loss is not None and stop_loss > 0
    has_target = target is not None and target > 0
    if has_entry and has_target:
        score += 20
        if has_stop:
            score += 10
    elif has_entry or has_target:
        score += 8
    else:
        score += 2

    return grade_from_score(score), score, rr


def meets_min_grade(grade: str, min_grade: str) -> bool:
    return GRADE_ORDER.index(grade) <= GRADE_ORDER.index(min_grade)


def regrade_current(level: dict, current_usd_price: Optional[float]) -> Tuple[str, float, Optional[float]]:
    """수집 시 저장된 레벨 dict에 '현재가'만 갈아끼워 재채점 (알림 필터 재평가용).

    배경(2026-07-26 감사): calculate_grade 의 가격근접도(최대 20점)는 채점 시점
    가격 기준이라, 수집 당시엔 멀어서 근접도 0점 → D등급이던 레벨이 며칠 뒤
    entry 근접(=알림상 가장 중요해진 순간)해도 재채점 없이는 계속 D로 남아
    필터에서 영구 배제됐다(터치 52건 중 18건/35%가 이 사유로 억제됨).

    followers/entry/sl/tp/direction 은 DB 원본 그대로 쓰고 가격만 최신화한다 —
    기존 calculate_grade 를 그대로 재사용(중복 구현 금지)."""
    return calculate_grade(
        level.get("author_followers"),
        level.get("direction"),
        level.get("entry_usd"),
        level.get("sl_usd"),
        level.get("tp_usd"),
        current_usd_price,
    )
