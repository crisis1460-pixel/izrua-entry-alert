"""
글(레벨) 등급 산정 — upbit_bot signals/watcher_feed.py 의 검증된 배점을 이식.

원 배점(2026-07-17 리서치 반영본): 팔로워 10 + R:R 55 + 가격근접도 20 + 데이터완결성 30
= 115점 만점, 등급 임계 S85/A70/B55/C40. 팔로워 배점을 낮게 두는 근거는
Kakhbod et al. "Finfluencers" (팔로워수는 실력의 양(+)신호가 아님).

이 봇은 글 단위(작성자 1명)라 원본의 chartist_count 분기가 필요 없어 단순화했다.
"""

from typing import Optional, Tuple

GRADE_ORDER = ["S", "A", "B", "C", "D"]


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
        if risk > 0:
            rr = reward / risk
            if rr >= 5:
                score += 55
            elif rr >= 3:
                score += 44
            elif rr >= 2:
                score += 33
            elif rr >= 1.5:
                score += 22
            elif rr >= 1:
                score += 11

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
