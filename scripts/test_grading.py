# collector/grading 단위 테스트 — 2026-08-01 v3(S10 안3: TP 원거리 보상 축소 +
# 작성자 실적 가점) 회귀 방어. 기대값은 전부 손계산.
#
# 배점 요약(v3): 팔로워(1~10) + 가격근접도(0~20)
#            + 목표거리(-6 ~ +12, SL 유무 무관 전 신호 적용, 40%+ 무배점)
#            + 데이터완결성(2/8/20/30) + 작성자 실적 가점(0/+5/+10/+15),
#            컷 S85/A70/B55/C40
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from analytics.calibration import wilson_interval
from collector.grading import (AUTHOR_TRACK_MAX, AUTHOR_TRACK_MIN_N, AUTHOR_TRACK_TIERS,
                               AUTHOR_TRACK_Z, TP_DISTANCE_BANDS, TP_REWARD_MAX,
                               author_track_points, calculate_grade, grade_from_score,
                               meets_min_grade, regrade_current, tp_distance_points)
from config import settings

ok = True
n_checks = 0


def check(name, cond):
    global ok, n_checks
    n_checks += 1
    print(("✅" if cond else "❌"), name)
    ok = ok and cond


def eq(a, b, tol=1e-9):
    return a is not None and abs(a - b) < tol


# 공통 픽스처: 팔로워 500(+3), 현재가=진입가(근접도 +20), entry+target(완결성 +20)
# → SL 없는 글의 기본 43점 + 목표거리 배점
def s(tp_pct, followers=500, direction="long", sl=None, cur=100.0):
    entry = 100.0
    target = entry * (1 + tp_pct / 100) if direction == "long" else entry * (1 - tp_pct / 100)
    return calculate_grade(followers, direction, entry, sl, target, cur)


# ── G1: 배점표 자체의 정합성 ─────────────────────────────────────
check("G1 배점표 상한 단조 증가", [b[0] for b in TP_DISTANCE_BANDS]
      == sorted(b[0] for b in TP_DISTANCE_BANDS))
check("G1b 감점 구간(<5%)은 전부 음수, 이후는 전부 0 이상(v3: 40%+ 무배점 밴드 허용)",
      all(p < 0 for hi, p in TP_DISTANCE_BANDS if hi <= 5)
      and all(p >= 0 for hi, p in TP_DISTANCE_BANDS if hi > 5))
check("G1c 대체배점 상한 일치", max(p for _, p in TP_DISTANCE_BANDS) == TP_REWARD_MAX)
check("G1c-2 v3 보상 축소 정본 — 5~8/8~15 +12, 15~25 +8, 25~40 +4, 40%+ 0",
      TP_DISTANCE_BANDS[3:] == [(8, 12), (15, 12), (25, 8), (40, 4), (60, 0),
                                (float("inf"), 0)] and TP_REWARD_MAX == 12)

# ── G1d~G1j: 배점표 부호-경계 불변식 (2026-07-26 감사 미조치 minor 조치) ────
# TP_DISTANCE_BANDS 는 감점과 대체배점을 한 표에 담은 '단일 배점표'라, 두 영역을
# 가르는 표식이 따로 없고 **점수의 부호**뿐이다(tp_distance_points 의
# `0.0 if (pts > 0 and has_rr) else pts` 게이팅). 그래서 아래 감점 역산이
# 배점표의 구조에 암묵적으로 의존한다:
#
#   monitor/price_check.py `_tp_distance_penalty` = -tp_distance_points(has_rr=True)
#   → run_once 가 `grade_from_score(score + tp_penalty)` 로 "감점만 없었다면
#     min_grade 를 통과했을 건"(suppressed_grade_tp_penalty_only)을 역산한다.
#
# 의존하는 성질: (1) 첫 매칭 루프라 상한이 강한 오름차순 (2) 마지막이 inf 라
# 전 구간을 덮음 (3) 감점(음수) 밴드에 0 이 없고 전부 앞쪽에 몰려 있어 음수→비음수
# 전환이 정확히 1회 = 감점/보상 영역이 배타적 (v3 부터 보상 영역에 0점 밴드
# (40%+ 무배점)가 존재한다 — 역산 게이팅은 `pts > 0` 기준이라 0 밴드는 자동으로
# '되돌릴 것 없음' 쪽) (4) 역산이 감점을 정확히 상쇄하고 비감점 구간엔 아무것도
# 되돌리지 않음. 나중에 누가 배점표만 고쳐도 역산이 조용히 깨지지 않도록
# 값이 아니라 '구조'를 검사한다. 실제 price_check 함수와의 값 교차검증은
# scripts/test_price_logic.py T32 담당(여기선 grading.py 만 import — 순수 유지).
_HI = [hi for hi, _ in TP_DISTANCE_BANDS]
_PTS = [p for _, p in TP_DISTANCE_BANDS]


def _pts_at(tp_pct, has_rr):
    return tp_distance_points("long", 100.0, 100.0 * (1 + tp_pct / 100.0), has_rr)


def _band_probe(i):
    """i번째 밴드에 확실히 걸리는 tp_pct — (직전 상한, 이 밴드 상한) 사이의 값."""
    lo = _HI[i - 1] if i else min(_HI[0] - 1.0, 0.0)
    return lo + 1.0 if _HI[i] == float("inf") else (lo + _HI[i]) / 2.0


def _penalty_at(tp_pct):
    """price_check._tp_distance_penalty 의 역산 규칙 그대로(부호만 뒤집기)."""
    return -_pts_at(tp_pct, True)


check("G1d 상한이 강한 오름차순(중복 상한이 있으면 뒤 밴드가 죽는다)",
      all(a < b for a, b in zip(_HI, _HI[1:])))
check("G1e 마지막 밴드만 inf — 위쪽 구멍 없이 전 구간을 덮는다",
      _HI[-1] == float("inf") and all(h != float("inf") for h in _HI[:-1]))
check("G1f 아래쪽 구멍 없음 — 역방향 목표(tp_pct<0)도 첫 밴드가 받아낸다",
      eq(_pts_at(-50.0, True), _PTS[0]) and eq(_pts_at(-50.0, False), _PTS[0]))
check("G1g 모든 밴드가 도달 가능(빈틈/겹침으로 죽은 밴드 없음)",
      all(eq(_pts_at(_band_probe(i), False), _PTS[i]) for i in range(len(_PTS))))
check("G1h 감점 밴드에 0 없음(음수만) — 부호가 감점 영역을 가르는 표식 유지",
      all(p < 0 for hi, p in TP_DISTANCE_BANDS if hi <= 5))
check("G1i 음수→비음수 전환 정확히 1회(감점 전부 앞 = 두 영역 배타적)",
      sum(1 for a, b in zip(_PTS, _PTS[1:]) if (a < 0) != (b < 0)) == 1
      and _PTS[0] < 0 and all(p >= 0 for p in _PTS[3:]))

# 역산 정확성 — 밴드마다 has_rr 양쪽으로 확인한다.
_inv_sign, _inv_exact, _inv_ghost = True, True, True
for _i in range(len(_PTS)):
    _probe = _band_probe(_i)
    _pen = _penalty_at(_probe)
    if _pen < 0:
        _inv_sign = False              # 되돌림이 점수를 깎으면 등급이 되레 내려간다
    for _has_rr in (True, False):
        _applied = _pts_at(_probe, _has_rr)
        if _applied < 0 and not eq(_applied + _pen, 0):
            _inv_exact = False         # 감점이 정확히 상쇄되지 않음
        if _applied >= 0 and not eq(_pen, 0):
            _inv_ghost = False         # 감점이 아닌데 되돌릴 게 있다고 나옴
check("G1j-1 역산 결과는 항상 0 이상(되돌림이 감점이 되면 안 됨)", _inv_sign)
check("G1j-2 감점 밴드는 역산으로 정확히 상쇄(score+penalty = 감점 이전 점수)", _inv_exact)
check("G1j-3 가점/무감점 밴드는 되돌릴 것이 없다(유령 되돌림 0)", _inv_ghost)

# ── G2: 목표거리 감점 (모든 글 공통, 기존 규칙 불변) ─────────────
# has_rr=True 경로 = 감점 구간만 반환, 보상 구간 0 (관찰 역산 전용, 등급 채점과 무관)
check("G2 초근접 TP 감점 -6", eq(tp_distance_points("long", 100.0, 101.5, True), -6))
check("G2b 2~3% -4", eq(tp_distance_points("long", 100.0, 102.5, True), -4))
check("G2c 3~5% -2", eq(tp_distance_points("long", 100.0, 104.0, True), -2))
check("G2d 5% 이상은 감점 0 (R:R 있는 글은 대체배점도 없음)",
      eq(tp_distance_points("long", 100.0, 105.0, True), 0)
      and eq(tp_distance_points("long", 100.0, 300.0, True), 0))
check("G2e 숏 대칭", eq(tp_distance_points("short", 100.0, 98.5, True), -6)
      and eq(tp_distance_points("short", 100.0, 90.0, True), 0))
check("G2f entry/target 없으면 0", eq(tp_distance_points("long", None, 105.0, True), 0)
      and eq(tp_distance_points("long", 100.0, None, True), 0)
      and eq(tp_distance_points("long", 0.0, 105.0, True), 0))

# ── G3: 대체배점(SL 없는 글) — 감점 구간과 배타적 ────────────────
check("G3 5% 미만은 대체배점 없이 감점만 (이중계산/상쇄 없음)",
      eq(tp_distance_points("long", 100.0, 101.5, False), -6)
      and eq(tp_distance_points("long", 100.0, 102.5, False), -4)
      and eq(tp_distance_points("long", 100.0, 104.0, False), -2))
check("G3b 5~8% +12 / 8~15% +12 (v3: 20→12)",
      eq(tp_distance_points("long", 100.0, 106.0, False), 12)
      and eq(tp_distance_points("long", 100.0, 110.0, False), 12))
check("G3c 15~25% +8 (v3: 25→8 — 원거리 보상 축소)",
      eq(tp_distance_points("long", 100.0, 120.0, False), 8))
check("G3d 숏 대칭", eq(tp_distance_points("short", 100.0, 80.0, False), 8))

# ── G4: 먼 목표 함정 회피 — v3 부터 40%+ 는 아예 무배점 ────────────
_far = [(30.0, 4), (50.0, 0), (56.8, 0), (185.7, 0)]
check("G4 25% 초과부터 배점이 계속 깎이고 40%+ 는 0 (v3)",
      all(eq(tp_distance_points("long", 100.0, 100.0 * (1 + p / 100), False), want)
          for p, want in _far))
check("G4b 실측 환상목표(SOL +56.8%, BCH +185.7%)는 무배점",
      tp_distance_points("long", 100.0, 156.8, False) == 0
      and tp_distance_points("long", 100.0, 285.7, False) == 0)
check("G4c 극단 목표(+1000%)도 무배점",
      tp_distance_points("long", 100.0, 1100.0, False) == 0)

# ── G5: SL 없는 글의 등급 상한 — v3 부터 실적 가점 없이는 B 가 상한 ────
# (2026-07-26 대체배점 도입으로 A 까지 열렸었으나, v3 보상 축소로 다시 닫힘 —
#  D5 승인: S/A 는 "검증된 작성자 + 완전 데이터 + 적정 TP" 전용으로 희귀화)
_g_max, _s_max, _rr_max = s(20.0, followers=200_000)   # 10 + 20 + 8 + 20 = 58
check("G5 SL 없는 최상급 글도 실적 가점 없이는 B 상한 (v3)",
      eq(_s_max, 58) and _g_max == "B" and _rr_max is None)
_g_mid, _s_mid, _ = s(10.0, followers=2_800)           # 5 + 20 + 12 + 20 = 57
check("G5b SL 없는 평범한 팔로워 + 스윗스팟 목표 → B", eq(_s_mid, 57) and _g_mid == "B")
_g_lo, _s_lo, _ = s(6.0, followers=500)                # 3 + 20 + 12 + 20 = 55
check("G5c SL 없는 소형 작성자 + 6% 목표 → B 경계", eq(_s_lo, 55) and _g_lo == "B")
check("G5d 대체배점만으로 S(85)에는 못 간다 (리스크 명시 글보다 항상 낮게)",
      10 + 20 + 20 + TP_REWARD_MAX < 85)
check("G5e 실적 가점 만점을 더해도 SL 없는 글은 S 불가 (실질 상한 검증: 87)",
      10 + 20 + 20 + TP_REWARD_MAX + AUTHOR_TRACK_MAX < 85
      and 10 + 20 + 30 + TP_REWARD_MAX + AUTHOR_TRACK_MAX == 87)

# ── G6: 초근접 TP 글은 여전히 걸러진다 (감점 취지 유지) ──────────
_g_close, _s_close, _ = s(1.5)     # 3 + 20 + 20 - 6 = 37
check("G6 SL 없고 TP +1.5% → 여전히 D (알림 필터 min_grade=C 미달)",
      eq(_s_close, 37) and _g_close == "D")
check("G6b 감점 구간이 대체배점으로 상쇄되지 않는다 — 초근접이 항상 최하 "
      "(v3: 5~15% 동점 +12, 15~25% +8 이라 단조가 아니라 '감점 < 보상' 만 본다)",
      s(1.5)[1] < s(20.0)[1] and s(1.5)[1] < s(6.0)[1]
      and s(6.0)[1] == s(10.0)[1] and s(10.0)[1] > s(20.0)[1])
check("G6c 2.5%/4.0% 도 C 이하 유지", s(2.5)[1] == 39 and s(4.0)[1] == 41
      and s(2.5)[0] == "D" and s(4.0)[0] == "C")

# ── G7: SL 있는 글도 목표거리 배점 전면 적용(2026-07-29 R:R 제거) ──
# entry100/sl90/tp150 → rr=5(표시용, 점수無). tp_pct=50% → (60,0)밴드 → +0 (v3)
# score = 3(팔로워) + 0(R:R 제거) + 20(근접) + 0(TP) + 30(완결) = 53 → C
_g_rr, _s_rr, _rr = calculate_grade(500, "long", 100.0, 90.0, 150.0, 100.0)
check("G7 SL 있어도 R:R 점수無, rr 표시용 유지(53/C)", eq(_s_rr, 53) and _g_rr == "C" and eq(_rr, 5.0))
# 과대목표 +185.7% → (inf,0)밴드 → +0. score = 3 + 20 + 0 + 30 = 53 → C
_g_rr_far, _s_rr_far, _rr_far = calculate_grade(500, "long", 100.0, 90.0, 285.7, 100.0)
check("G7b 과대목표 SL 있음: tp_pct 185.7% → +0, score=53/C",
      eq(_s_rr_far, 53) and _g_rr_far == "C" and _rr_far > 5)
# 초근접 TP +1.5% → (2,-6)밴드 → -6. rr=1.5 표시용. score = 3 + 20 - 6 + 30 = 47 → C
_g_close, _s_rr_close, _rr_close = calculate_grade(500, "long", 100.0, 99.0, 101.5, 100.0)
check("G7c SL 있는 초근접 TP: 감점(-6) 유지, R:R 점수無, score=47/C",
      eq(_s_rr_close, 3 + 20 - 6 + 30) and _g_close == "C" and eq(_rr_close, 1.5))
# 역전된 SL(risk<=0)이면 rr 계산 불가 → R:R 제거 후에도 동일(tp_distance 정상 적용)
_, _s_bad_sl, _rr_bad = calculate_grade(500, "long", 100.0, 110.0, 120.0, 100.0)
check("G7d 잘못된 SL(리스크<=0)로 R:R 불가 시 TP 배점 정상 적용(61/B)",
      _rr_bad is None and eq(_s_bad_sl, 3 + 20 + 8 + 30))

# ── G8: 데이터완결성/근접도 배점 불변 ────────────────────────────
check("G8 목표만 있고 진입가 없음 → 완결성 8, 목표거리 배점 0",
      eq(calculate_grade(500, "long", None, None, 110.0, 100.0)[1], 3 + 8))
check("G8b 아무 수치도 없음 → 완결성 2", eq(calculate_grade(500, "long", None, None, None, 100.0)[1],
                                     3 + 2))
check("G8c 가격근접도 구간 불변(-3% → +17)",
      eq(calculate_grade(500, "long", 100.0, None, 110.0, 97.0)[1], 3 + 17 + 12 + 20))

# ── G9: regrade_current 가 새 배점을 그대로 태운다 ───────────────
_lv = dict(author_followers=2_800, direction="long", entry_usd=100.0, sl_usd=None,
           tp_usd=110.0)
check("G9 재채점 - 가격이 멀면 근접도 0점(37=D), 근접 시 +20(57=B)",
      eq(regrade_current(_lv, 200.0)[1], 37) and regrade_current(_lv, 200.0)[0] == "D"
      and eq(regrade_current(_lv, 100.0)[1], 57) and regrade_current(_lv, 100.0)[0] == "B")
check("G9b 재채점 결과가 calculate_grade 와 동일",
      regrade_current(_lv, 100.0) == calculate_grade(2_800, "long", 100.0, None, 110.0, 100.0))
# 실적 가점 포함 재채점 동등성 (v3): level dict 의 author_closed_n/hits 키가
# calculate_grade 의 동명 인자로 그대로 전달된다
_lv_at = dict(_lv, author_closed_n=27, author_closed_hits=20)
check("G9c 재채점 동등성 — 실적 키 주입 시에도 calculate_grade(author 인자)와 동일",
      regrade_current(_lv_at, 100.0) == calculate_grade(
          2_800, "long", 100.0, None, 110.0, 100.0,
          author_closed_n=27, author_closed_hits=20)
      and eq(regrade_current(_lv_at, 100.0)[1], 57 + 15))

# ── G10: 등급 컷/필터 헬퍼 불변 ──────────────────────────────────
check("G10 컷 경계", grade_from_score(85) == "S" and grade_from_score(84.9) == "A"
      and grade_from_score(70) == "A" and grade_from_score(55) == "B"
      and grade_from_score(40) == "C" and grade_from_score(39.9) == "D")
check("G10b meets_min_grade", meets_min_grade("B", "C") and meets_min_grade("C", "C")
      and not meets_min_grade("D", "C"))

# ── G11: 작성자 실적 가점 author_track_points (2026-08-01 v3 안2) ──────────
# 게이트: 종결 n>=5 + Wilson 80% 단측 하한(z=1.28) >= 0.55/0.40/0.25 → +15/+10/+5.
# n=5 손계산 하한: 5/5→0.753 / 4/5→0.514 / 3/5→0.331 / 2/5→0.180
check("G11 콜드스타트 — n<5 는 도달률 100% 여도 0점 (소표본 과대평가 차단)",
      author_track_points(4, 4) == 0 and author_track_points(1, 1) == 0
      and author_track_points(0, 0) == 0 and author_track_points(None, None) == 0)
check("G11b n=5 경계 — 5/5→+15(하한 0.75>=0.55), 4/5→+10(0.51>=0.40), "
      "3/5→+5(0.33>=0.25), 2/5→0(0.18<0.25)",
      author_track_points(5, 5) == 15 and author_track_points(5, 4) == 10
      and author_track_points(5, 3) == 5 and author_track_points(5, 2) == 0)
check("G11c 실측 사례 — CryptoAnalystSignal 20/27→+15, mastercrypto2020 4/14→0",
      author_track_points(27, 20) == 15 and author_track_points(14, 4) == 0)
# 배점표(tier)와 wilson_interval 재사용의 교차검증 — 넓은 (n,h) 그리드에서
# '하한 → 첫 매칭 tier' 규칙과 결과가 전부 일치해야 한다
_g11_ok = True
for _n in (5, 6, 8, 10, 14, 20, 27, 40):
    for _h in range(_n + 1):
        _lo, _ = wilson_interval(_h, _n, z=AUTHOR_TRACK_Z)
        _want = next((p for t, p in AUTHOR_TRACK_TIERS if _lo >= t), 0)
        if author_track_points(_n, _h) != _want:
            _g11_ok = False
check("G11d 게이트 규칙 = wilson_interval(z=1.28) 하한 + tier 표 (전 그리드 일치)",
      _g11_ok and AUTHOR_TRACK_MIN_N == 5 and abs(AUTHOR_TRACK_Z - 1.28) < 1e-9)
check("G11e 1건짜리 100% 가 27건짜리 74% 를 못 이긴다 (Wilson 하한의 존재 이유)",
      author_track_points(1, 1) < author_track_points(27, 20))

# ── G12: calculate_grade 하위호환 — author 인자 생략 = 기존 점수 그대로 ─────
_base = calculate_grade(500, "long", 100.0, 90.0, 110.0, 100.0)
check("G12 인자 생략(구 호출부) == n=0 전달 == 가점 0",
      _base == calculate_grade(500, "long", 100.0, 90.0, 110.0, 100.0,
                               author_closed_n=0, author_closed_hits=0)
      and eq(_base[1], 3 + 20 + 12 + 30))
check("G12b 실적 인자 전달 시 가점 합산 (+15 → 65+15=80/A)",
      eq(calculate_grade(500, "long", 100.0, 90.0, 110.0, 100.0,
                         author_closed_n=27, author_closed_hits=20)[1], 65 + 15))
check("G12c 콜드스타트 작성자(n=3)는 전달해도 기존 점수 그대로 (중립)",
      calculate_grade(500, "long", 100.0, 90.0, 110.0, 100.0,
                      author_closed_n=3, author_closed_hits=3) == _base)

# ── G13: 롤백 스위치 grade_author_points_enabled=False → 가점 0 고정 ───────
settings.SETTINGS["grade_author_points_enabled"] = False
try:
    _off = calculate_grade(500, "long", 100.0, 90.0, 110.0, 100.0,
                           author_closed_n=27, author_closed_hits=20)
    _off_re = regrade_current(dict(author_followers=500, direction="long",
                                   entry_usd=100.0, sl_usd=90.0, tp_usd=110.0,
                                   author_closed_n=27, author_closed_hits=20), 100.0)
finally:
    settings.SETTINGS["grade_author_points_enabled"] = True
check("G13 스위치 OFF — calculate_grade 경로 가점 0 (배점표 축소분은 유지)",
      _off == _base)
check("G13b 스위치 OFF — regrade_current 경로도 동일하게 가점 0",
      _off_re == _base)
check("G13c 스위치 ON 복원 후 가점 재적용 (런타임 토글 즉시 반영)",
      eq(calculate_grade(500, "long", 100.0, 90.0, 110.0, 100.0,
                         author_closed_n=27, author_closed_hits=20)[1], 80))

print()
print(f"{'전체 통과' if ok else '실패 있음'} ({n_checks}개 체크)")
sys.exit(0 if ok else 1)
