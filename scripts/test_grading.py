# collector/grading 단위 테스트 — 2026-07-26 등급 배점 재조정(SL 없는 글의
# 구조적 등급 상한 해소) 회귀 방어. 기대값은 전부 손계산.
#
# 배점 요약: 팔로워(1~10) + R:R(0~55, SL 필요) + 가격근접도(0~20)
#            + 목표거리(-6 ~ +25) + 데이터완결성(2/8/20/30), 컷 S85/A70/B55/C40
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

# ── G7: R:R 경로(SL 있는 글)는 이번 변경으로 바뀌지 않는다 ───────
# entry100/sl90/tp150 → risk10 reward50 → rr=5 → +55. 3 + 55 + 20 + 30 = 108 → S
_g_rr, _s_rr, _rr = calculate_grade(500, "long", 100.0, 90.0, 150.0, 100.0)
check("G7 R:R 5 이상 만점 경로 불변", eq(_s_rr, 108) and _g_rr == "S" and eq(_rr, 5.0))
# 과대목표 감점은 이번 범위 밖 — R:R 경로는 먼 목표에도 그대로 만점(백로그 이월)
_, _s_rr_far, _rr_far = calculate_grade(500, "long", 100.0, 90.0, 285.7, 100.0)
check("G7b R:R 경로의 과대목표는 이번 범위 밖(현행 유지)",
      eq(_s_rr_far, 108) and _rr_far > 5)
# SL 있는 글의 초근접 TP 감점도 그대로 (rr<1 이라 R:R 가점 0, 감점 -6)
_, _s_rr_close, _rr_close = calculate_grade(500, "long", 100.0, 99.0, 101.5, 100.0)
check("G7c SL 있는 초근접 TP: 감점 유지 + 대체배점 없음",
      eq(_s_rr_close, 3 + 22 + 20 - 6 + 30) and eq(_rr_close, 1.5))
# 역전된 SL(risk<=0)이면 rr 계산 불가 → 대체배점 경로로 넘어간다
_, _s_bad_sl, _rr_bad = calculate_grade(500, "long", 100.0, 110.0, 120.0, 100.0)
check("G7d 잘못된 SL(리스크<=0)로 R:R 불가 시 대체배점 적용(+데이터완결성 30 유지)",
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
