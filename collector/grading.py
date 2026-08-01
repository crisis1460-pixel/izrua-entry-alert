"""
글(레벨) 등급 산정.

배점(2026-08-01 v3 = S10 안3): 팔로워(1~10) + 가격근접도(0~20)
  + 목표거리(-6~+12) + 데이터완결성(2~30) + 작성자 실적 가점(0~+15)
  = 이론 만점 100 (실질 상한 87 = 10+20+12+30+15).
  등급 임계 S85/A70/B55/C40 유지 — S/A 는 "검증된 작성자 + 완전 데이터 +
  적정 TP" 에서만 나온다(희귀화 용인, 사용자 결정 D5).

팔로워 배점을 낮게 두는 근거: Kakhbod et al. "Finfluencers"
(팔로워수는 실력의 양(+)신호가 아님).

2026-07-26 개정: SL 없는 글에 목표거리 대체배점(최대 25점)을 도입해 B·A 도달 가능.
2026-07-29 개정: 실전 51건 분석 결과 R:R 상위(S/A) 신호의 실적중률 0%로 확인.
  R:R 은 알림 표시용으로만 쓰고 등급 점수에서 제거. 목표거리 배점은 SL 유무와
  무관하게 전 신호에 적용(has_rr 구분 폐지). 등급 임계는 새 최대점(85)에 맞춰 유지.
2026-08-01 개정(v3, S10 안3 — plan_S10_grade_recalibration.md, 사용자 결정 D1~D5):
  등급의 목적을 "TP1 도달 가능성 예측"으로 확정(D1)하고 두 결함을 제거 —
  ① TP 원거리 보상 축소(안1): 실측상 원거리 TP 도달률 0%(8%+ 0/3, rr≥2 0/12)인데
    최대 +25 가점이 S/A 를 만들던 역상관 채널 차단. 8~15% 20→12 / 15~25% 25→8 /
    25~40% 18→4 / 40%+ →0. 감점 구간(0~5%)은 불변.
  ② 작성자 실적 가점 신설(안2): 자기 DB 종결 실적(작성자별 TP1 도달률 격차
    74% vs 0~29% — 전 요소 중 최강 예측변수)을 Wilson 80% 단측 하한 게이트로
    반영(author_track_points, 최대 +15). 콜드스타트(n<5)는 0점(중립).
  롤백 스위치: settings.grade_author_points_enabled=False 면 실적 가점 0 고정
  (배점표 축소분은 유지 — 안1 부분은 위험 없음). 산식 버전 태그 grade_ver='v3'
  (settings.grade_formula_ver) — 과거 저장 등급은 소급 재라벨하지 않는다(D4).
"""

from typing import Optional, Tuple

# Wilson 하한은 프로젝트 정본(analytics/calibration.py)을 재사용한다 —
# analytics → collector 역방향 의존이 없어 순환 import 없음(calibration 은
# "프로젝트 모듈 import 0" 순수 모듈).
from analytics.calibration import wilson_interval

GRADE_ORDER = ["S", "A", "B", "C", "D"]

# 목표거리(TP 거리) 단일 배점표 — (상한 %, 점수). 아래→위 순서로 첫 매칭 구간 적용.
# 음수 구간(=감점)은 모든 글에 적용, 5%+ 구간은 보상(0 포함 — v3 부터 40%+ 는
# 무배점). 두 영역은 5% 를 경계로 서로 겹치지 않는다 — 같은 글이 감점과 보상을
# 동시에 받는 이중계산/상쇄가 구조적으로 불가능하다.
# 2026-08-01 v3(안1): 보상 구간 실측 하향 — 원거리 TP 도달률 0%(8%+ 0/3) 실측과
# 승률-R:R 역관계(원거리일수록 도달 빈도↓)에 맞춘다. 감점 구간(0~5%)은 불변.
SWING_MIN_TP_PCT = 2  # TP 최소 스윙폭(%). _b_swing_pass 컷오프와 아래 첫 경계 공유.
TP_DISTANCE_BANDS = [
    (SWING_MIN_TP_PCT, -6),  # 0~2% 초근접 목표: 왕복 수수료 0.1%+슬리피지 빼면 스윙 실익 없음
    (3, -4),     # 2~3%
    (5, -2),     # 3~5%
    (8, 12),     # 5~8%    현실적 스윙 목표 (유지)
    (15, 12),    # 8~15%   v3: 20→12 — '스윗스팟' 보상이 소표본에서 등급을 지배하던 채널 축소
    (25, 8),     # 15~25%  v3: 25→8 — 실측 0/2
    (40, 4),     # 25~40%  v3: 18→4
    (60, 0),     # 40~60%  v3: 9→0 — 무배점
    (float("inf"), 0),   # 60%+   v3: 3→0 — 'SOL +56.8%, BCH +185.7%' 류 환상적 목표 무배점
]
TP_REWARD_MAX = 12   # 목표거리 최고 배점 (v3: 25→12. TP 만으로는 B 컷도 못 만든다)

# ── 작성자 실적 가점 (2026-08-01 v3 안2 — 사용자 결정 D3) ────────────────
# 자기 DB 종결 실적(levels.outcome) 기반. 소표본 과대평가를 막기 위해
# Wilson score 단측 하한(z=1.28 ≒ 80%, 프로젝트 관례 rank_z 와 동일)을 쓴다 —
# 1건짜리 100% 가 27건짜리 74% 를 이기지 못하게 하는 표준 방법(Evan Miller).
# 최상위 문턱 0.55 는 검증 카피트레이딩 플랫폼의 현실적 승률 밴드(55~65%) 하단.
# 자동 재적합이 아니라 게이트 있는 고정 배점표 — 소표본 노이즈 추종 없음.
AUTHOR_TRACK_Z = 1.28        # 80% 단측 하한 계수 (settings.rank_z 와 같은 관례값)
AUTHOR_TRACK_MIN_N = 5       # 종결 표본 게이트 (settings.rank_min_neff 와 같은 관례값)
AUTHOR_TRACK_TIERS = [       # (Wilson 하한 문턱, 가점) — 위에서부터 첫 매칭
    (0.55, 15),
    (0.40, 10),
    (0.25, 5),
]
AUTHOR_TRACK_MAX = 15        # 실적 가점 상한 (이론 만점 85→100)


def author_track_points(closed_n: Optional[int], closed_hits: Optional[int]) -> float:
    """작성자 실적 가점 — 종결 n≥5 이고 TP1 도달률의 Wilson 80% 단측 하한이
    0.55/0.40/0.25 이상이면 +15/+10/+5, 그 외(콜드스타트 포함) 0.

    입력은 storage.db.author_closed_stats() 반환값 (n, hits). None/0 은 0점 —
    신규 작성자에게 불이익이 아니라 중립이다."""
    n = int(closed_n or 0)
    hits = int(closed_hits or 0)
    if n < AUTHOR_TRACK_MIN_N:
        return 0.0
    lo, _ = wilson_interval(hits, n, z=AUTHOR_TRACK_Z)
    if lo is None:
        return 0.0
    for threshold, pts in AUTHOR_TRACK_TIERS:
        if lo >= threshold:
            return float(pts)
    return 0.0


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
    v3(2026-08-01)부터 보상은 5~15% +12 를 정점으로 멀수록 축소, 40%+ 는 0 —
    '달성 가능한 목표' 에만 소폭 가점하고 '환상적 목표' 는 무배점.

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
    author_closed_n: Optional[int] = None,
    author_closed_hits: Optional[int] = None,
) -> Tuple[str, float, Optional[float]]:
    """반환 (grade, score, rr). rr 은 계산 불가 시 None (판단 보류 — 필터에서 제외 금지).

    author_closed_n/hits (2026-08-01 v3): 작성자의 자기 DB 종결 실적
    (storage.db.author_closed_stats). 기본 None = 가점 0 — 기존 호출부는 무수정
    으로 종전과 동일한 점수가 나온다. settings.grade_author_points_enabled=False
    면 인자를 넘겨도 가점 0 고정(롤백 스위치 — calculate/regrade 경로 공통)."""
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
        if abs(diff_pct) <= 2:
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

    # 작성자 실적 가점 (2026-08-01 v3 안2). 스위치는 호출 시점에 읽는다 —
    # 롤백(False)이 재시작 없이 다음 채점부터 즉시 반영되고, 테스트가 SETTINGS
    # 를 바꿔 양쪽 경로를 검증할 수 있다.
    if author_closed_n:
        from config import settings  # 지연 로드 — grading 단독 사용처의 의존 최소화
        if settings.get("grade_author_points_enabled"):
            score += author_track_points(author_closed_n, author_closed_hits)

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
    기존 calculate_grade 를 그대로 재사용(중복 구현 금지).

    author_closed_n/hits (2026-08-01 v3): 호출부(monitor/price_check.py)가 터치/예고
    처리 직전에 storage.db.author_closed_stats 로 주입해 두는 키. 없으면(구 호출부,
    주입 실패 격리 경로) 가점 0 — 구 산식과 동일하게 동작한다."""
    return calculate_grade(
        level.get("author_followers"),
        level.get("direction"),
        level.get("entry_usd"),
        level.get("sl_usd"),
        level.get("tp_usd"),
        current_usd_price,
        author_closed_n=level.get("author_closed_n"),
        author_closed_hits=level.get("author_closed_hits"),
    )
