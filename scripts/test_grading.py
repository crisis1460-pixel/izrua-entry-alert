# collector/grading 단위 테스트 — 2026-07-29 R:R 제거(표시전용 유지) 회귀 방어.
# 기대값은 전부 손계산.
#
# 배점 요약: 팔로워(1~10) + 가격근접도(0~20)
#            + 목표거리(-6 ~ +25, SL 유무 무관 전 신호 적용)
#            + 데이터완결성(2/8/20/30), 컷 S85/A70/B55/C40
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from collector.grading import (TP_DISTANCE_BANDS, TP_REWARD_MAX, calculate_grade,
                               grade_from_score, meets_min_grade, regrade_current,
                               tp_distance_points)

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
check("G1b 감점 구간(<5%)은 전부 음수, 이후는 전부 양수",
      all(p < 0 for hi, p in TP_DISTANCE_BANDS if hi <= 5)
      and all(p > 0 for hi, p in TP_DISTANCE_BANDS if hi > 5))
check("G1c 대체배점 상한 일치", max(p for _, p in TP_DISTANCE_BANDS) == TP_REWARD_MAX)

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
# 전 구간을 덮음 (3) 0점 밴드가 없어 부호가 영역을 유일하게 가름 (4) 부호 교차가
# 정확히 1회라 감점/가점이 배타적 (5) 역산이 감점을 정확히 상쇄하고 가점 구간엔
# 아무것도 되돌리지 않음. 나중에 누가 배점표만 고쳐도 역산이 조용히 깨지지 않도록
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
check("G1h 0점 밴드 없음 — 부호가 감점/대체배점을 가르는 유일한 표식이라",
      all(p != 0 for p in _PTS))
check("G1i 부호 교차 정확히 1회(감점 전부 앞 · 가점 전부 뒤 = 두 영역 배타적)",
      sum(1 for a, b in zip(_PTS, _PTS[1:]) if (a < 0) != (b < 0)) == 1
      and _PTS[0] < 0 and _PTS[-1] > 0)

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
# has_rr=True(=SL 있어 R:R 산출됨)면 감점만 적용되고 대체배점은 0
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
check("G3b 5~8% +12 / 8~15% +20", eq(tp_distance_points("long", 100.0, 106.0, False), 12)
      and eq(tp_distance_points("long", 100.0, 110.0, False), 20))
check("G3c 15~25% 최고점 +25", eq(tp_distance_points("long", 100.0, 120.0, False), 25))
check("G3d 숏 대칭", eq(tp_distance_points("short", 100.0, 80.0, False), 25))

# ── G4: 먼 목표 함정 회피 — 봉우리형(문턱형 아님) ────────────────
_far = [(30.0, 18), (50.0, 9), (56.8, 9), (185.7, 3)]
check("G4 25% 초과부터 배점이 계속 깎인다",
      all(eq(tp_distance_points("long", 100.0, 100.0 * (1 + p / 100), False), want)
          for p, want in _far))
check("G4b 실측 환상목표(SOL +56.8%, BCH +185.7%)가 스윙 스윗스팟(+20%)보다 낮음",
      tp_distance_points("long", 100.0, 156.8, False) < 25
      and tp_distance_points("long", 100.0, 285.7, False) < 25)
check("G4c 극단 목표(+1000%)도 만점 아님",
      tp_distance_points("long", 100.0, 1100.0, False) == 3)

# ── G5: SL 없는 글의 구조적 등급 상한 해소 (이번 변경의 핵심) ────
# 변경 전: SL 없는 글 최고점 = 팔로워10 + 근접도20 + 완결성20 = 50 → B컷(55) 미달
_g_max, _s_max, _rr_max = s(20.0, followers=200_000)   # 10 + 20 + 20 + 25 = 75
check("G5 SL 없는 최상급 글이 A 도달 (변경 전 구조적 상한 50=C)",
      eq(_s_max, 75) and _g_max == "A" and _rr_max is None)
_g_mid, _s_mid, _ = s(10.0, followers=2_800)           # 5 + 20 + 20 + 20 = 65
check("G5b SL 없는 평범한 팔로워 + 스윗스팟 목표 → B", eq(_s_mid, 65) and _g_mid == "B")
_g_lo, _s_lo, _ = s(6.0, followers=500)                # 3 + 20 + 20 + 12 = 55
check("G5c SL 없는 소형 작성자 + 6% 목표 → B 경계", eq(_s_lo, 55) and _g_lo == "B")
check("G5d 대체배점만으로 S(85)에는 못 간다 (리스크 명시 글보다 항상 낮게)",
      10 + 20 + 20 + TP_REWARD_MAX < 85)

# ── G6: 초근접 TP 글은 여전히 걸러진다 (감점 취지 유지) ──────────
_g_close, _s_close, _ = s(1.5)     # 3 + 20 + 20 - 6 = 37
check("G6 SL 없고 TP +1.5% → 여전히 D (알림 필터 min_grade=C 미달)",
      eq(_s_close, 37) and _g_close == "D")
check("G6b 감점 구간이 대체배점으로 상쇄되지 않는다",
      s(1.5)[1] < s(6.0)[1] < s(10.0)[1] < s(20.0)[1])
check("G6c 2.5%/4.0% 도 C 이하 유지", s(2.5)[1] == 39 and s(4.0)[1] == 41
      and s(2.5)[0] == "D" and s(4.0)[0] == "C")

# ── G7: SL 있는 글도 목표거리 배점 전면 적용(2026-07-29 R:R 제거) ──
# entry100/sl90/tp150 → rr=5(표시용, 점수無). tp_pct=50% → (60,9)밴드 → +9
# score = 3(팔로워) + 0(R:R 제거) + 20(근접) + 9(TP) + 30(완결) = 62 → B
_g_rr, _s_rr, _rr = calculate_grade(500, "long", 100.0, 90.0, 150.0, 100.0)
check("G7 SL 있어도 R:R 점수無, rr 표시용 유지(62/B)", eq(_s_rr, 62) and _g_rr == "B" and eq(_rr, 5.0))
# 과대목표 +185.7% → (inf,3)밴드 → +3. score = 3 + 20 + 3 + 30 = 56 → B
_, _s_rr_far, _rr_far = calculate_grade(500, "long", 100.0, 90.0, 285.7, 100.0)
check("G7b 과대목표 SL 있음: tp_pct 185.7% → +3, score=56/B",
      eq(_s_rr_far, 56) and _ == "B" and _rr_far > 5)
# 초근접 TP +1.5% → (2,-6)밴드 → -6. rr=1.5 표시용. score = 3 + 20 - 6 + 30 = 47 → C
_g_close, _s_rr_close, _rr_close = calculate_grade(500, "long", 100.0, 99.0, 101.5, 100.0)
check("G7c SL 있는 초근접 TP: 감점(-6) 유지, R:R 점수無, score=47/C",
      eq(_s_rr_close, 3 + 20 - 6 + 30) and _g_close == "C" and eq(_rr_close, 1.5))
# 역전된 SL(risk<=0)이면 rr 계산 불가 → R:R 제거 후에도 동일(tp_distance 정상 적용)
_, _s_bad_sl, _rr_bad = calculate_grade(500, "long", 100.0, 110.0, 120.0, 100.0)
check("G7d 잘못된 SL(리스크<=0)로 R:R 불가 시 TP 배점 정상 적용(78/A)",
      _rr_bad is None and eq(_s_bad_sl, 3 + 20 + 25 + 30))

# ── G8: 데이터완결성/근접도 배점 불변 ────────────────────────────
check("G8 목표만 있고 진입가 없음 → 완결성 8, 목표거리 배점 0",
      eq(calculate_grade(500, "long", None, None, 110.0, 100.0)[1], 3 + 8))
check("G8b 아무 수치도 없음 → 완결성 2", eq(calculate_grade(500, "long", None, None, None, 100.0)[1],
                                     3 + 2))
check("G8c 가격근접도 구간 불변(-3% → +17)",
      eq(calculate_grade(500, "long", 100.0, None, 110.0, 97.0)[1], 3 + 17 + 20 + 20))

# ── G9: regrade_current 가 새 배점을 그대로 태운다 ───────────────
_lv = dict(author_followers=2_800, direction="long", entry_usd=100.0, sl_usd=None,
           tp_usd=110.0)
check("G9 재채점 - 가격이 멀면 근접도 0점(45=C), 근접 시 +20(65=B)",
      eq(regrade_current(_lv, 200.0)[1], 45) and regrade_current(_lv, 200.0)[0] == "C"
      and eq(regrade_current(_lv, 100.0)[1], 65) and regrade_current(_lv, 100.0)[0] == "B")
check("G9b 재채점 결과가 calculate_grade 와 동일",
      regrade_current(_lv, 100.0) == calculate_grade(2_800, "long", 100.0, None, 110.0, 100.0))

# ── G10: 등급 컷/필터 헬퍼 불변 ──────────────────────────────────
check("G10 컷 경계", grade_from_score(85) == "S" and grade_from_score(84.9) == "A"
      and grade_from_score(70) == "A" and grade_from_score(55) == "B"
      and grade_from_score(40) == "C" and grade_from_score(39.9) == "D")
check("G10b meets_min_grade", meets_min_grade("B", "C") and meets_min_grade("C", "C")
      and not meets_min_grade("D", "C"))

print()
print(f"{'전체 통과' if ok else '실패 있음'} ({n_checks}개 체크)")
sys.exit(0 if ok else 1)
