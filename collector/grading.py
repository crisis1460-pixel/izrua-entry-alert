"""
글(레벨) 등급 산정.

배점(2026-08-15 v5): 팔로워(1~25) + 가격근접도(0~20)
  + 목표거리(-6~+12) + 데이터완결성(2~23) + 작성자 실적 가점(0~+15)
  + TP 사다리 감점(-3~0, v5 신설 — 0~1단이면 -3)
  = 실질 상한 95 (25+20+12+23+15, 사다리 2단+ 기준). SL 없는 글 상한 92 —
  실적 가점이 있으면 SL 없이도 S(85+) 도달 가능(v4 설계 의도).
  등급 임계 S85/A70/B55/C40 유지.

팔로워 배점(v4 상단 강화)의 근거: 사용자 결정(2026-08-03) — 자체 DB 축적 기간이
짧은 콜드스타트 동안 "팔로워 많음 = 장기 활동 + 대중 검증"을 대리지표로 쓴다.
단 Kakhbod et al. "Finfluencers"(팔로워수는 실력의 양(+)신호가 아님)와 자체 실측
(팔로워 구간 효과는 사실상 특정 작성자 1인 효과)을 존중해 **하단 티어(1천 미만)는
v3 그대로** 두고 상단(5천+)만 끌어올렸다 — 소형 작성자의 초근접 TP 글이 등급
필터를 새로 뚫는 채널이 열리지 않는다.

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
2026-08-03 개정(v4 — 사용자 질문카드 결정 "팔로워 지배 + 즉시 적용",
  리서치: izrua_company/planner/sprint08_SL없는시그널_등급설계_리서치.md):
  ① 팔로워 상단 강화 1~10 → 1~25 (100k+ 25 / 50k+ 22 / 10k+ 17 / 5k+ 12 /
    1k+ 8 / 100+ 3 / 미만 1). 발단: 프로덕션 54%(105/193)가 SL 미기재이고
    BigBeluga(9.7만) 등 유명 작성자가 완결성 보너스를 못 받아 D 고착.
  ② SL 완결성 보너스 +10 → +3. 근거: TV 아이디어는 실행 시그널이 아닌 분석
    콘텐츠(SL 비필수)이고, "SL 없음 = red flag"의 실논거(판정 회피로 승률 조작)는
    timeboxed 강제 판정으로 이미 차단됨. 업계(MQL5/Darwinex/Pyckio/eToro)는
    포맷을 채점하지 않고 실현 실적만 채점. 사용자 지시 "가산은 소폭만".
  ③ 실적 가점(+15)·목표거리·근접도·등급 임계 불변. SL 없는 글도
    "상위 팔로워 or 검증 실적"이면 A~S 도달 가능해진다.
  ④ settings.grade_formula_ver 'v3' → 'v4' 로 태그 승격 — 신규 채점분에
    levels.grade_ver 로 기록. 과거 v3 행은 소급 재라벨 없음(D4 유지).
2026-08-15 개정(v5 — TP 사다리 0~1단 감점, 사용자 승인):
  실증 근거: izrua_company/research_2026-08-15_db_signal_analysis.md (종결 n=189) —
  tp_ladder_count 2단 이상 win% 48.2 vs 0~1단 31.7 (+16.5%p), v4 한정 부분표본
  (0-1 → 23.1% n=13 vs 2-3 → 45.2% n=42)에서도 방향 재현. 다단계 목표(구조화된
  셋업)가 단일/무목표 글보다 일관되게 우수 — 현 채점에 없던 신호 중 표본 최다.
  변경은 단 하나: tp_ladder_count <= 1 (NULL/0/1 — DB 분석이 0-1 을 한 버킷으로
  묶었고 '단일 목표'와 '사다리 미상' 모두 31.7% 쪽) 이면 -3 (ladder_penalty).
  2단 이상은 0 — 가점이 아니라 순수 감점(조이기)이며 그 외 v4 배점·임계 전부 불변.
  settings.grade_formula_ver 'v4' → 'v5' 태그 승격, 과거 행 소급 재라벨 없음.
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
AUTHOR_TRACK_MAX = 15        # 실적 가점 상한 (v4 이론 만점 80→95)

# ── TP 사다리 감점 (2026-08-15 v5 — 사용자 승인) ─────────────────────────
# 실증 근거: izrua_company/research_2026-08-15_db_signal_analysis.md (종결 n=189)
# — tp_ladder_count 2단 이상 win% 48.2 vs 0~1단 31.7 (+16.5%p), v4 부분표본에서도
# 방향 재현. 0~1단(단일 목표든 사다리 미상이든 — DB 분석이 0-1 을 한 버킷으로
# 집계했고 둘 다 31.7% 쪽)은 -3, 2단 이상은 0. 가점 없는 순수 감점(조이기)이라
# 이 감점으로 등급이 오르는 경로는 구조적으로 없다.
LADDER_PENALTY = -3          # tp_ladder_count <= 1 (NULL/0/1 포함) 일 때
LADDER_MIN_STEPS = 2         # 감점을 면하는 최소 사다리 단계 수


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


def _social_sentiment_points(stwits_bullish_ratio: Optional[float]) -> float:
    """StockTwits 소셜 심리 가감점 (2026-08-17).

    bullish_ratio (0~1) = Bullish / (Bullish + Bearish) — 태그된 표본 기준.
    ≥0.75 강한 매수 심리 +1 (매수 유리)
    ≤0.30 강한 매도 심리 -1 (매수 부담)
    나머지·None(표본 부족·심볼 미존재) 0. 소셜은 노이즈 축이라 가중치 낮음(±1)."""
    if stwits_bullish_ratio is None:
        return 0.0
    if stwits_bullish_ratio >= 0.75:
        return 1.0
    if stwits_bullish_ratio <= 0.30:
        return -1.0
    return 0.0


def _onchain_activity_points(active_addr_pctile: Optional[float]) -> float:
    """Coin Metrics 활성주소 30d 백분위 가감점 (2026-08-17).

    ≥80 활발 +1 (네트워크 관심 유입 = 매수 유리)
    ≤20 저조 -1 (네트워크 관심 이탈 = 매수 부담)
    나머지·None(미커버·조회 실패) 0. 가중치 낮은(±1) 이유는 무료 티어 커버
    자산이 18종으로 좁아 유니버스 형평성 유지 위함."""
    if active_addr_pctile is None:
        return 0.0
    if active_addr_pctile >= 80:
        return 1.0
    if active_addr_pctile <= 20:
        return -1.0
    return 0.0


def _dex_points(buy_ratio_24h: Optional[float],
                liquidity_usd: Optional[float]) -> float:
    """DEX Screener 온체인 시그널 가감점 (2026-08-17).

    buy_ratio_24h (0~1, buys/(buys+sells)): 24h 매수/매도 tx 비율.
      >=0.65 매수 우위 +2, <=0.35 매도 우위 -2, 나머지 0.
    liquidity_usd: DEX 전 페어 합산 유동성.
      <100k$ 저유동 -3 (러그·이탈 위험), 그 외 0. 100k$ 이상은 감점 없음.
    매핑 없는 네이티브 코인(XRP/ADA/BTC)이나 소형 알트(값 None) → 0 (무해)."""
    pts = 0.0
    if buy_ratio_24h is not None:
        if buy_ratio_24h >= 0.65:
            pts += 2
        elif buy_ratio_24h <= 0.35:
            pts -= 2
    if liquidity_usd is not None and liquidity_usd < 100_000:
        pts -= 3
    return pts


def _regime_points(adx14: Optional[float], bb_width_pctile: Optional[float]) -> float:
    """F3 (2026-08-17) — 시장 국면 가감점.

    ADX(14): 방향성 강도. >=25 추세장(+3), <20 횡보장(-2), 20~25 중립(0).
    BB Width 백분위(0~100, 최근 120개 대비 현재): 낮을수록 압축.
      <=20 압축 국면(+2, 변동성 폭발 전조), >=80 팽창 후반부(-2), 나머지 0.
    각 값 None(표본 부족·조회 실패)은 0 — 등급 감점 아님(무해 폴백).

    알림 표시 대상은 ADX 만(사용자 결정) — 등급 산식은 둘 다 반영."""
    pts = 0.0
    if adx14 is not None:
        if adx14 >= 25:
            pts += 3
        elif adx14 < 20:
            pts -= 2
    if bb_width_pctile is not None:
        if bb_width_pctile <= 20:
            pts += 2
        elif bb_width_pctile >= 80:
            pts -= 2
    return pts


def score_breakdown(
    followers: Optional[float],
    direction: str,
    entry: Optional[float],
    stop_loss: Optional[float],
    target: Optional[float],
    current_usd_price: Optional[float],
    author_closed_n: Optional[int] = None,
    author_closed_hits: Optional[int] = None,
    tp_ladder_count: Optional[int] = None,
    adx14: Optional[float] = None,
    bb_width_pctile: Optional[float] = None,
    dex_buy_ratio: Optional[float] = None,
    dex_liquidity_usd: Optional[float] = None,
    active_addr_pctile: Optional[float] = None,
    stwits_bullish_ratio: Optional[float] = None,
) -> dict:
    """채점 요소 분해 — 각 구성 요소별 점수를 dict로 반환.

    calculate_grade()가 내부적으로 사용하며, 수집 시 분해 저장에도 직접 호출된다.
    키: follower, proximity, tp_dist, data, author, ladder, regime.  sum(values()) == 총점.

    tp_ladder_count (2026-08-15 v5): levels.tp_ladder_count (extractor 가 센
    유효 TP 단계 수, 0 = 단일 목표/사다리 미상). None 도 0~1 과 같은 -3 —
    '미상'과 '단일 목표'가 실측에서 같은 저성과 버킷(31.7%, n=104)이었다.

    adx14/bb_width_pctile (2026-08-17 F3): 시장 국면 가감(_regime_points 참고).
    수집 시엔 None (캔들 조회 안 함) — 발송 확정 후 rep 재채점에서만 값이 실린다."""
    bd: dict = {}

    f = followers or 0
    if f >= 100_000:
        bd["follower"] = 25
    elif f >= 50_000:
        bd["follower"] = 22
    elif f >= 10_000:
        bd["follower"] = 17
    elif f >= 5_000:
        bd["follower"] = 12
    elif f >= 1_000:
        bd["follower"] = 8
    elif f >= 100:
        bd["follower"] = 3
    else:
        bd["follower"] = 1

    bd["proximity"] = 0
    if entry and current_usd_price and current_usd_price > 0:
        diff_pct = (current_usd_price - entry) / entry * 100
        if abs(diff_pct) <= 2:
            bd["proximity"] = 20
        elif -10 < diff_pct < -2:
            bd["proximity"] = 17
        elif 2 <= diff_pct < 5:
            bd["proximity"] = 12
        elif 5 <= diff_pct < 10:
            bd["proximity"] = 8
        elif diff_pct <= -10:
            bd["proximity"] = 15
        # diff_pct >= 10: 0점 유지 (현재가가 entry 대비 10%+ 위 = 이미 지나간 자리)

    bd["tp_dist"] = tp_distance_points(direction, entry, target)

    has_entry = entry is not None and entry > 0
    has_stop = stop_loss is not None and stop_loss > 0
    has_target = target is not None and target > 0
    if has_entry and has_target:
        bd["data"] = 20 + (3 if has_stop else 0)
    elif has_entry or has_target:
        bd["data"] = 8
    else:
        bd["data"] = 2

    bd["author"] = 0.0
    if author_closed_n:
        from config import settings
        if settings.get("grade_author_points_enabled"):
            bd["author"] = author_track_points(author_closed_n, author_closed_hits)

    # TP 사다리 감점 (2026-08-15 v5) — 실증: research_2026-08-15_db_signal_analysis.md,
    # 종결 n=189 에서 2단+ win% 48.2 vs 0~1단 31.7 (+16.5%p). NULL/0/1 모두 -3.
    bd["ladder"] = LADDER_PENALTY if (tp_ladder_count or 0) < LADDER_MIN_STEPS else 0

    # 시장 국면 (2026-08-17 F3) — ADX 추세강도 + BB Width 압축·팽창
    bd["regime"] = _regime_points(adx14, bb_width_pctile)

    # DEX 온체인 시그널 (2026-08-17) — 매수/매도 tx 비율 + 저유동성 경고
    bd["dex"] = _dex_points(dex_buy_ratio, dex_liquidity_usd)

    # Coin Metrics 활성주소 백분위 (2026-08-17) — 무료 티어 커버 자산만 값 있음
    bd["onchain_addr"] = _onchain_activity_points(active_addr_pctile)

    # StockTwits 소셜 심리 (2026-08-17) — bullish_ratio 극단만 ±1
    bd["social"] = _social_sentiment_points(stwits_bullish_ratio)

    return bd


def calculate_grade(
    followers: Optional[float],
    direction: str,
    entry: Optional[float],
    stop_loss: Optional[float],
    target: Optional[float],
    current_usd_price: Optional[float],
    author_closed_n: Optional[int] = None,
    author_closed_hits: Optional[int] = None,
    tp_ladder_count: Optional[int] = None,
    adx14: Optional[float] = None,
    bb_width_pctile: Optional[float] = None,
    dex_buy_ratio: Optional[float] = None,
    dex_liquidity_usd: Optional[float] = None,
    active_addr_pctile: Optional[float] = None,
    stwits_bullish_ratio: Optional[float] = None,
) -> Tuple[str, float, Optional[float]]:
    """반환 (grade, score, rr). rr 은 계산 불가 시 None (판단 보류 — 필터에서 제외 금지).

    author_closed_n/hits (2026-08-01 v3): 작성자의 자기 DB 종결 실적
    (storage.db.author_closed_stats). 기본 None = 가점 0 — 기존 호출부는 무수정
    으로 종전과 동일한 점수가 나온다. settings.grade_author_points_enabled=False
    면 인자를 넘겨도 가점 0 고정(롤백 스위치 — calculate/regrade 경로 공통).

    tp_ladder_count (2026-08-15 v5): TP 사다리 단계 수. <=1 (None 포함) 이면
    -3 (LADDER_PENALTY) — 미전달(None)은 '사다리 미상'으로 0~1단과 같은 저성과
    버킷(31.7%, n=104)이라 동일 감점. 상세는 모듈 헤더 v5 항목."""
    bd = score_breakdown(followers, direction, entry, stop_loss, target,
                         current_usd_price, author_closed_n, author_closed_hits,
                         tp_ladder_count=tp_ladder_count,
                         adx14=adx14, bb_width_pctile=bb_width_pctile,
                         dex_buy_ratio=dex_buy_ratio,
                         dex_liquidity_usd=dex_liquidity_usd,
                         active_addr_pctile=active_addr_pctile,
                         stwits_bullish_ratio=stwits_bullish_ratio)
    score = float(sum(bd.values()))

    rr = None
    if entry and stop_loss and target:
        if direction == "long":
            risk, reward = entry - stop_loss, target - entry
        else:
            risk, reward = stop_loss - entry, entry - target
        if risk > 0 and reward > 0:
            rr = reward / risk

    return grade_from_score(score), score, rr


def calculate_grade_with_breakdown(
    followers, direction, entry, stop_loss, target, current_usd_price,
    author_closed_n=None, author_closed_hits=None, tp_ladder_count=None,
    adx14=None, bb_width_pctile=None,
    dex_buy_ratio=None, dex_liquidity_usd=None,
    active_addr_pctile=None, stwits_bullish_ratio=None,
) -> tuple:
    """calculate_grade + score_breakdown 을 단일 호출로 — 수집 경로 이중계산 방지."""
    bd = score_breakdown(followers, direction, entry, stop_loss, target,
                         current_usd_price, author_closed_n, author_closed_hits,
                         tp_ladder_count=tp_ladder_count,
                         adx14=adx14, bb_width_pctile=bb_width_pctile,
                         dex_buy_ratio=dex_buy_ratio,
                         dex_liquidity_usd=dex_liquidity_usd,
                         active_addr_pctile=active_addr_pctile,
                         stwits_bullish_ratio=stwits_bullish_ratio)
    score = float(sum(bd.values()))
    rr = None
    if entry and stop_loss and target:
        if direction == "long":
            risk, reward = entry - stop_loss, target - entry
        else:
            risk, reward = stop_loss - entry, entry - target
        if risk > 0 and reward > 0:
            rr = reward / risk
    return grade_from_score(score), score, rr, bd


def meets_min_grade(grade: str, min_grade: str) -> bool:
    try:
        return GRADE_ORDER.index(grade) <= GRADE_ORDER.index(min_grade)
    except ValueError:
        return False


def regrade_current(
    level: dict,
    current_usd_price: Optional[float],
    adx14: Optional[float] = None,
    bb_width_pctile: Optional[float] = None,
    dex_buy_ratio: Optional[float] = None,
    dex_liquidity_usd: Optional[float] = None,
    active_addr_pctile: Optional[float] = None,
    stwits_bullish_ratio: Optional[float] = None,
) -> Tuple[str, float, Optional[float]]:
    """수집 시 저장된 레벨 dict에 '현재가'만 갈아끼워 재채점 (알림 필터 재평가용).

    배경(2026-07-26 감사): calculate_grade 의 가격근접도(최대 20점)는 채점 시점
    가격 기준이라, 수집 당시엔 멀어서 근접도 0점 → D등급이던 레벨이 며칠 뒤
    entry 근접(=알림상 가장 중요해진 순간)해도 재채점 없이는 계속 D로 남아
    필터에서 영구 배제됐다(터치 52건 중 18건/35%가 이 사유로 억제됨).

    followers/entry/sl/tp/direction 은 DB 원본 그대로 쓰고 가격만 최신화한다 —
    기존 calculate_grade 를 그대로 재사용(중복 구현 금지).

    author_closed_n/hits (2026-08-01 v3): 호출부(monitor/price_check.py)가 터치/예고
    처리 직전에 storage.db.author_closed_stats 로 주입해 두는 키. 없으면(구 호출부,
    주입 실패 격리 경로) 가점 0 — 구 산식과 동일하게 동작한다.

    tp_ladder_count (2026-08-15 v5): 입력 행은 db.get_active_levels 의 SELECT *
    라 levels.tp_ladder_count (DEFAULT 0) 를 항상 실어 온다. 키 부재/NULL 은
    0~1단과 동일하게 -3 (사다리 미상 = 같은 저성과 버킷, 모듈 헤더 v5 참조)."""
    return calculate_grade(
        level.get("author_followers"),
        level.get("direction"),
        level.get("entry_usd"),
        level.get("sl_usd"),
        level.get("tp_usd"),
        current_usd_price,
        author_closed_n=level.get("author_closed_n"),
        author_closed_hits=level.get("author_closed_hits"),
        tp_ladder_count=level.get("tp_ladder_count"),
        adx14=adx14,
        bb_width_pctile=bb_width_pctile,
        dex_buy_ratio=dex_buy_ratio,
        dex_liquidity_usd=dex_liquidity_usd,
        active_addr_pctile=active_addr_pctile,
        stwits_bullish_ratio=stwits_bullish_ratio,
    )
