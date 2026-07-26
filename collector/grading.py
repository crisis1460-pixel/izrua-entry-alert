"""
글(레벨) 등급 산정 — upbit_bot signals/watcher_feed.py 의 검증된 배점을 이식.

원 배점(2026-07-17 리서치 반영본): 팔로워 10 + R:R 55 + 가격근접도 20 + 데이터완결성 30
= 115점 만점, 등급 임계 S85/A70/B55/C40. 팔로워 배점을 낮게 두는 근거는
Kakhbod et al. "Finfluencers" (팔로워수는 실력의 양(+)신호가 아님).

이 봇은 글 단위(작성자 1명)라 원본의 chartist_count 분기가 필요 없어 단순화했다.

2026-07-26 개정: 위 배점은 R:R(=SL 필수) 55점이 전체의 절반이라, 손절을 적지 않는
글의 이론 최고점이 50점(B컷 55 미달)으로 묶여 '아무리 좋아도 C'였다. 사용자는 스윙
트레이더로 손절을 중시하지 않는데(알림에서 손절 행도 삭제) 배점이 사실상 'SL 유무
게이트'로 작동해 정책과 반대 효과를 냈다. → SL 없는 글에는 목표거리(기대보상) 기반
대체배점 최대 25점을 주어 B·A 도달을 가능하게 했다(tp_distance_points 참조).
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
TP_REWARD_MAX = 25   # 대체배점 상한 (SL 없는 글의 이론 최고점 = 10+20+20+25 = 75 → A 도달 가능)


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
                       has_rr: bool) -> float:
    """목표거리 배점(부호 있는 단일 값). has_rr=True 면 감점 구간만 적용한다.

    설계 근거(2026-07-26 사용자 결정 — SL 없는 글의 구조적 등급 상한 해소):
    R:R 배점 55점은 entry·SL·TP 가 모두 있어야만 붙어, 손절을 적지 않는 글은
    이론 최고점이 50점(=B컷 55 미달)이라 아무리 좋아도 C 가 한계였다. 사용자는
    스윙 관점에서 손절을 중시하지 않는데 배점이 사실상 'SL 유무 게이트'로 작동해
    정책과 정반대 효과를 냈다.

    해법: R:R 이 재는 것은 '리스크 대비 보상'인데, SL 이 없어도 '기대 보상'
    (목표까지의 거리)은 관측 가능하다 → 그 거리로 대체 배점을 준다. 단
    - 상한 25점(R:R 55점의 절반 이하): 리스크가 명시된 글보다 항상 낮게 평가한다.
    - 문턱형(멀수록 좋음)이 아니라 봉우리형: 15~25% 가 최고점이고 그 위로는
      깎는다. '달성 가능한 큰 목표'와 '환상적인 큰 목표'를 구분 못 하면
      +185% 짜리 목표가 만점을 받는 함정에 빠진다.
    - 5% 미만은 대체배점 0 — 이 구간은 기존 감점(초근접 TP)이 단독으로 지배한다.
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

    # 목표 거리 배점 (2026-07-26).
    # (1) 감점 — 모든 글 공통(A안 '약하게'). 목표가를 진입가 +1~2%로 아주 촘촘히
    #     잡는 글은 승률이 쉽게 100%로 쌓이지만(CryptoAnalystSignal 8승0패 실측)
    #     업비트 왕복 수수료(0.1%)와 슬리피지를 빼면 스윙 실익이 거의 없다.
    #     실측 시뮬: 이 감점으로 알림 후보 23건 → 17건, 차단 6건 중 5건이 이 패턴.
    # (2) 기대보상 대체배점 — R:R 을 못 잰 글(SL 미기재)에만. 근거는
    #     tp_distance_points() docstring 참조. 두 항은 5% 경계로 배타적이라
    #     한 글에 동시에 걸리지 않는다(이중계산/상쇄 없음).
    # 감점만 하고 배제는 하지 않는다(R:R·근접도가 충분히 높으면 여전히 통과 가능).
    score += tp_distance_points(direction, entry, target, has_rr=rr is not None)

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
