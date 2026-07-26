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
        if risk > 0 and reward > 0:
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

    # 목표 거리 감점 (2026-07-26 사용자 결정 — A안 '약하게').
    # 배경: 목표가를 진입가 +1~2%로 아주 촘촘히 잡는 글이 다수 발견됐다. 이런 글은
    # 승률이 쉽게 100%로 쌓이지만(CryptoAnalystSignal 8승0패 실측) 업비트 왕복
    # 수수료(0.1%)와 슬리피지를 빼면 스윙 관점에서 실익이 거의 없다 — 승률 착시의 원인.
    # 실측 시뮬레이션: 이 감점으로 알림 후보 23건 → 17건, 차단 6건 중 5건이 해당 패턴.
    # 감점만 하고 배제는 하지 않는다(R:R·근접도가 충분히 높으면 여전히 통과 가능).
    if entry and target and entry > 0:
        tp_pct = ((target - entry) if direction == "long" else (entry - target)) / entry * 100
        if tp_pct < 2:
            score -= 6
        elif tp_pct < 3:
            score -= 4
        elif tp_pct < 5:
            score -= 2

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
