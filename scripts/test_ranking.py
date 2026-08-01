# analytics/ranking 단위·통합 테스트 — 2026-07-26 질문카드 확정(2트랙·m_eff) 반영.
# 수치 기대값은 전부 손계산 (dev_a/sprint01_ELB설계.md ⑥).
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from analytics import distribution, ranking
from storage import db

ok = True


def check(name, cond):
    global ok
    print(("✅" if cond else "❌"), name)
    ok = ok and cond


def close(a, b, tol=1e-3):
    return a is not None and abs(a - b) < tol


now = 1_800_000_000.0
D = 86400.0

# U1: 균등 가중 E_LB — R=[+2,+2,−1,−1,−1] → mean 0.2, var 2.16, n_eff 5, E_LB −0.6413
check("U1 E_LB 균등 가중", close(ranking.e_lb([2, 2, -1, -1, -1], [1] * 5), -0.6413))

# U2: 최신성 가중 — [(+1, 0일), (−1, 90일)] → w=[1, 0.5], E_LB −0.5661, n_eff 1.8
w2 = [ranking.recency_weight(now, now), ranking.recency_weight(now, now - 90 * D)]
check("U2 반감기 가중치", close(w2[0], 1.0) and close(w2[1], 0.5))
check("U2 가중 E_LB", close(ranking.e_lb([1, -1], w2), -0.5661))
check("U2 n_eff", close(ranking.effective_n(w2), 1.8))

# U3: 수축 — p=0.72, m=10(워쳐 100건), 4승2패 → p̂ = 12.2/18 = 0.6778
check("U3 베이지안 수축", close(ranking.shrunk_win_rate(4, 2, 0.72, 100, 10), 12.2 / 18))

# U4: 자연 졸업 — 90승10패면 prior 희석 → 98.2/112 = 0.8768
check("U4 자연 졸업", close(ranking.shrunk_win_rate(90, 10, 0.72, 100, 10), 98.2 / 112))

# U4b: m_eff — 워쳐 4건짜리 prior 1.0 + 자체 0승2패 → m_eff=4, p̂=5/8=0.625
#      (m=10 고정이었으면 11/14=0.786 — kiv1n 왜곡 사례 차단 확인)
check("U4b m_eff 소표본 prior 하향", close(ranking.shrunk_win_rate(0, 2, 1.0, 4, 10), 5 / 8))

# U5: std=0 — R=[−1,−1] → E_LB=−1 정확, 크래시 없음
check("U5 동일값 표본", close(ranking.e_lb([-1, -1], [1, 1]), -1.0))

# U6: 게이트 경계 — w=[1,1,1,0.5,0.5] → n_eff 4.571 (raw 5건이어도 미달, 의도 동작)
check("U6 게이트 n_eff", close(ranking.effective_n([1, 1, 1, .5, .5]), 16 / 3.5)
      and close(ranking.effective_n([1] * 5), 5.0))

# U7: 2트랙 분리 — tp_only 7승(R 전부 NULL) → 승률축 n_eff 7, R트랙 0·E_LB None,
#     p̂ = (10.1+7)/(12+7) = 0.9 (워쳐 0.91/91건 → m_eff=10)
rows7 = [dict(outcome="hit", r_multiple=None, touched_at=now,
              author_hit_rate=0.91, author_hit_count=91) for _ in range(7)]
m7 = ranking.author_metrics(rows7, now)
check("U7 2트랙 분리", close(m7["neff_win"], 7.0) and m7["neff_r"] == 0.0
      and m7["e_lb"] is None and close(m7["p_hat"], 17.1 / 19))

# U8: prior 폴백 — 워쳐 없음 + 7승0패 → Beta(1,1), p̂ = 8/9
rows8 = [dict(outcome="hit", r_multiple=None, touched_at=now,
              author_hit_rate=None, author_hit_count=None) for _ in range(7)]
check("U8 prior 폴백", close(ranking.author_metrics(rows8, now)["p_hat"], 8 / 9))

# U9: 표시 클립 — mean −0.8, std 0.6, n_eff 2 → E_LB −1.343 → 표시 −1.0 (내부값 보존)
rows9 = [dict(outcome="miss", r_multiple=-0.2, touched_at=now,
              author_hit_rate=None, author_hit_count=None),
         dict(outcome="miss", r_multiple=-1.4, touched_at=now,
              author_hit_rate=None, author_hit_count=None)]
m9 = ranking.author_metrics(rows9, now)
check("U9 표시 클립", close(m9["e_lb"], -1.3434) and close(m9["e_lb_display"], -1.0))

# I1: 임시 DB 통합 — mastercrypto2020 시나리오 R=[0.46,0.44,−1×5] → E_LB ≈ −0.9026
TEST_DB = "cache/_test_ranking.db"
if os.path.exists(TEST_DB):
    os.remove(TEST_DB)
db.init_db(TEST_DB)
with db.connect(TEST_DB) as conn:
    for i, r in enumerate([0.46, 0.44, -1, -1, -1, -1, -1]):
        conn.execute(
            "INSERT INTO levels (signal_key, coin_symbol, ticker, direction, status, "
            "collected_at, author, outcome, r_multiple, touched_at, author_hit_rate, "
            "author_hit_count) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"k{i}", "SOL", "KRW-SOL", "long", "touched", now - D, "mc2020",
             "hit" if r > 0 else "miss", r, now, 0.72, 59))
    rows = db.get_author_outcome_rows(conn, "mc2020")
m1 = ranking.author_metrics(rows, now)
check("I1 실데이터 시나리오 E_LB", len(rows) == 7 and close(m1["neff_r"], 7.0)
      and close(m1["e_lb"], -0.9026))
os.remove(TEST_DB)

# I2: 렌더 게이트 — 경계 4.9 미표시 / 5.0 표시 (raw 4승2패=6건이어도 n_eff 가 기준).
# 2026-07-27: 게이트 축이 neff_win → neff_r 로 바뀌었다. 승률 표시와 역신호 경고가
# 같은 R 트랙을 보게 해서, SL 미기재 작성자가 '승률만 표시되고 경고는 면제'되던
# 비대칭을 막는다(근거·사례는 test_price_logic.py T14g~T14i).
from notify import telegram  # noqa: E402

rep = dict(coin_symbol="LINK", ticker="KRW-LINK", direction="long", entry_usd=10.0,
           sl_usd=9.0, tp_usd=12.0, grade="B", score=60, author="GateA",
           author_followers=10, author_hit_rate=None, author_hit_count=None,
           author_whitelisted=0, mcap_rank=19, mcap_tier_icon="🥇",
           post_url="https://tv.com/g", post_age_minutes=100,
           author_self_wins=4, author_self_losses=2,
           author_touched_n=6, author_untouched_expired=0, author_rank_min_neff=5.0)
msg_a = telegram.render_alert("touch", "LINK", [dict(rep, author_self_neff_r=4.9)],
                              10.05 * 1400, 1400)
msg_b = telegram.render_alert("touch", "LINK", [dict(rep, author_self_neff_r=5.0)],
                              10.05 * 1400, 1400)
check("I2a neff_r 4.9 → 🏹 미표시", "🏹" not in msg_a)
check("I2b neff_r 5.0 → 🏹 표시", "🏹" in msg_b and "4승2패" in msg_b)
# I2c: 승률축(neff_win)만 크고 R 표본이 없으면 표시하지 않는다 — 비대칭 차단의 핵심
msg_c = telegram.render_alert(
    "touch", "LINK", [dict(rep, author_self_neff=12.0, author_self_neff_r=0.0)],
    10.05 * 1400, 1400)
check("I2c neff_win 12 이어도 neff_r 0 이면 미표시", "🏹" not in msg_c)

# ── C: 합의(confluence) 클러스터 — analytics/clustering.py (2026-07-26 신규) ──
from analytics import clustering  # noqa: E402

H = 3600.0


def lv(i, coin, entry, author, t=0.0):
    return dict(id=i, coin_symbol=coin, entry_usd=entry, author=author, touched_at=now + t)


# C1: price_check._build_clusters 와 병합 결과 동일 (규칙 정본 일치 — 분기 방지 가드)
from monitor.price_check import _build_clusters  # noqa: E402  (읽기 전용 참조)

_same = [lv(1, "SOL", 100.0, "A"), lv(2, "SOL", 99.5, "B"), lv(3, "SOL", 98.0, "A"),
         lv(4, "SOL", 97.9, "A"), lv(5, "SOL", None, "D")]
check("C1 price_check 와 병합 결과 동일",
      [[l["id"] for l in c] for c in _build_clusters(_same, 1.0)]
      == [[l["id"] for l in c] for c in clustering.build_clusters(_same, 1.0)]
      == [[1, 2], [3, 4]])

# C2: CR — SOL 클러스터1(A,B 다자) / 클러스터2(A,A 단독) → A 1/2, B 1/1
conf = clustering.confluence_by_author(_same, 1.0)
check("C2 CR 계산", conf["A"] == {"multi": 1, "total": 2, "cr": 0.5}
      and conf["B"]["cr"] == 1.0 and "D" not in conf)

# C3: 같은 작성자 중복 게시로 '다자' 부풀리기 불가 (A 2건뿐인 클러스터는 단독)
c3 = clustering.confluence_by_author(
    [lv(1, "ETH", 100.0, "A"), lv(2, "ETH", 99.6, "A"), lv(3, "ETH", 99.7, "A")], 1.0)
check("C3 자기 중복 게시는 다자 아님", c3["A"] == {"multi": 0, "total": 1, "cr": 0.0})

# C4: 코인 분리 — 값이 같아도 다른 코인이면 절대 병합 안 됨
c4 = clustering.confluence_by_author(
    [lv(1, "SOL", 100.0, "A"), lv(2, "LINK", 100.0, "B")], 1.0)
check("C4 코인 경계", c4["A"]["multi"] == 0 and c4["B"]["multi"] == 0)

# C5: 시간창 — 200시간 떨어진 두 터치는 같은 가격대여도 합의 아님(우연 병합 차단)
far = [lv(1, "SOL", 100.0, "A"), lv(2, "SOL", 99.6, "B", t=200 * H)]
check("C5 시간창 밖 미병합",
      clustering.confluence_by_author(far, 1.0, window_sec=168 * H)["A"]["multi"] == 0
      and clustering.confluence_by_author(far, 1.0)["A"]["multi"] == 1)

# ── B: 초과 적중률 베이스라인 (ret_24h 양수 비율) ──────────────────────
# B1: 실측 시나리오 — 29건 중 3건 양수 → 10.3%
b1 = clustering.baseline_positive_rate([1.0, 2.0, 0.5] + [-1.0] * 26)
check("B1 베이스라인 비율", b1["n"] == 29 and b1["positive"] == 3 and close(b1["rate"], 3 / 29))

# B2: NULL 제외 · 0%는 양수 아님(경계)
b2 = clustering.baseline_positive_rate([None, 0.0, 1.0, None])
check("B2 NULL 제외/0 경계", b2["n"] == 2 and b2["positive"] == 1)

# B3: 표본 전무 → rate None (렌더러가 섹션 생략)
check("B3 표본 없음", clustering.baseline_positive_rate([None])["rate"] is None)

# B4: 초과분 — 2승5패(28.6%) vs 기준선 10.3% → +18.2%p
b4 = clustering.excess_hit_rate(2, 5, 3 / 29)
check("B4 초과분", close(b4["raw"], 2 / 7) and close(b4["excess"], 2 / 7 - 3 / 29))
check("B5 표본 0 / 기준선 없음 방어",
      clustering.excess_hit_rate(0, 0, 0.1)["excess"] is None
      and clustering.excess_hit_rate(2, 5, None)["excess"] is None)

# ── D: DB 조회 함수 (임시 DB, 프로덕션 DB 미접근) ──────────────────────
TEST_DB2 = "cache/_test_confluence.db"
if os.path.exists(TEST_DB2):
    os.remove(TEST_DB2)
db.init_db(TEST_DB2)
with db.connect(TEST_DB2) as conn:
    rows_in = [
        # (key, coin, entry, author, outcome, touched_at, ret_24h)
        ("d1", "SOL", 100.0, "A", "hit", now, 1.5),
        ("d2", "SOL", 99.5, "B", "miss", now, -2.0),
        ("d3", "SOL", 98.0, "A", "miss", now, -1.0),
        ("d4", "SOL", 97.0, "A", None, now, None),        # 미종결 — 승패엔 미집계
        ("d5", "SOL", 96.0, "C", "hit", None, 9.0),       # 섀도 터치 — 전부 제외
    ]
    for k, coin, entry, author, outcome, t, ret in rows_in:
        conn.execute(
            "INSERT INTO levels (signal_key, coin_symbol, ticker, direction, status, "
            "collected_at, author, entry_usd, outcome, touched_at, ret_24h) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (k, coin, f"KRW-{coin}", "long", "touched", now - D, author, entry,
             outcome, t, ret))
    rets = db.get_ret24_values(conn)
    check("D1 ret24 조회는 섀도터치 제외·미종결 포함", sorted(rets) == [-2.0, -1.0, 1.5])
    cl_rows = db.get_touched_levels_for_clusters(conn)
    check("D2 클러스터 원천행 4건(섀도 제외)", len(cl_rows) == 4)
    rec = db.get_author_raw_record(conn)
    check("D3 원시 승패 집계", rec["A"] == {"wins": 1, "losses": 1}
          and rec["B"]["losses"] == 1 and "C" not in rec)
os.remove(TEST_DB2)

# ── G: 등급 캘리브레이션 (analytics/calibration.py, 2026-07-27 기획 카드 #26) ──
# Wilson 기대값은 전부 손계산. z=1.96 → z²=3.8416.
#   center = (p + z²/2n)/(1 + z²/n),  half = z/(1+z²/n)·√(p(1−p)/n + z²/4n²)
from analytics import calibration  # noqa: E402


def ci(hits, n):
    return calibration.wilson_interval(hits, n)


# G1: 0/5 — p=0 → center=half=0.38416/1.76832=0.2172457 → [0, 0.4344915]
#     (교과서 예시값: 0/5 의 95% Wilson 상한 ≈ 0.4345. Wald 였다면 [0,0] 로 붕괴)
lo1, hi1 = ci(0, 5)
check("G1 Wilson 0/5 손계산", close(lo1, 0.0) and close(hi1, 0.434491))

# G2: 6/6 — p=1, z²/n=0.640267 → center=1.320133/1.640267=0.804827,
#     half=1.194927·√(3.8416/144)=0.195170 → [0.609657, 1.0(클램프)]
lo2, hi2 = ci(6, 6)
check("G2 Wilson 6/6 손계산", close(lo2, 0.609657) and close(hi2, 1.0))

# G3: 5/12 — p=0.416667, z²/n=0.320133 → center=0.436875,
#     half=1.484699·√(0.0202546+0.0066694)=0.243618 → [0.193257, 0.680493]
lo3, hi3 = ci(5, 12)
check("G3 Wilson 5/12 손계산", close(lo3, 0.193257) and close(hi3, 0.680493))

# G4: 경계 — 표본 0 은 구간 없음 / 극단값도 [0,1] 밖으로 새지 않음
check("G4 표본 0·구간 클램프", ci(0, 0) == (None, None)
      and ci(0, 1)[0] == 0.0 and ci(1, 1)[1] == 1.0)

# G5: 실측 스냅샷 시나리오(2026-07-27 프로덕션 DB 분포) — S 0/4, A 0/4, C 5/12, D 6/6
CAL_ROWS = ([("S", "miss", 0)] * 4 + [("A", "miss", 0)] * 4
            + [("C", "hit", 0)] * 5 + [("C", "miss", 0)] * 7 + [("D", "hit", 0)] * 6)
cal = calibration.calibrate_grades(CAL_ROWS)
check("G5 등급 버킷팅·도달률", cal["pooled"]["n"] == 26
      and cal["buckets"]["S"]["rate"] == 0.0 and cal["buckets"]["S"]["n"] == 4
      and close(cal["buckets"]["C"]["rate"], 5 / 12)
      and cal["buckets"]["D"]["rate"] == 1.0
      and cal["buckets"]["B"]["n"] == 0 and cal["buckets"]["B"]["rate"] is None)
check("G5b 소표본 플래그(n<5 인 S·A·B 만 enough=False)",
      not cal["buckets"]["S"]["enough"] and not cal["buckets"]["A"]["enough"]
      and cal["buckets"]["C"]["enough"] and cal["buckets"]["D"]["enough"])

# G6: timeboxed_win 은 TP1 을 찍은 게 아니므로 분자 제외·분모 포함 (2/4 = 50%)
g6 = calibration.calibrate_grades(
    [("B", "hit", 0), ("B", "hit", 0), ("B", "timeboxed_win", 0), ("B", "miss", 0)])
check("G6 timeboxed_win 분모만", g6["buckets"]["B"]["n"] == 4
      and g6["buckets"]["B"]["hits"] == 2)

# G7: 미종결(outcome None)·미채점(grade None)·정의 밖 등급은 표본 아님
g7 = calibration.calibrate_grades(
    [("C", "hit", 0), ("C", None, 0), (None, "hit", 0), ("F", "hit", 0)])
check("G7 미종결·미채점·미지등급 제외", g7["pooled"]["n"] == 1)

# G8: 단조성 — min_n=5 면 S·A(4건)는 판정 제외, C(42%)<D(100%) 역전 1건.
#     D 하한 0.6097 < C 상한 0.6805 → CI 겹침 = 약한 신호
check("G8 단조성 위반 1건·CI 겹침(약한 신호)",
      len(cal["violations"]) == 1 and cal["monotonic"] is False
      and cal["violations"][0]["higher"] == "C" and cal["violations"][0]["lower"] == "D"
      and cal["violations"][0]["significant"] is False and cal["significant"] == 0
      and cal["eligible"] == 2)

# G9: min_n=1 로 낮추면 소표본 등급도 판정 대상 — S(상한 0.4899) vs D(하한 0.6097)
#     은 CI 비겹침 = 강한 신호. 인접쌍만 봤다면 놓쳤을 '건너뛴 역전'이다.
cal_all = calibration.calibrate_grades(CAL_ROWS, min_n=1)
sig = [(v["higher"], v["lower"]) for v in cal_all["violations"] if v["significant"]]
check("G9 비인접 역전 + CI 비겹침 판정", sig == [("S", "D"), ("A", "D")]
      and len(cal_all["violations"]) == 5)

# G10: 정상(단조) 케이스 — S 90% > C 50% > D 10% → 위반 없음
g10 = calibration.calibrate_grades(
    [("S", "hit", 0)] * 9 + [("S", "miss", 0)]
    + [("C", "hit", 0)] * 5 + [("C", "miss", 0)] * 5
    + [("D", "hit", 0)] + [("D", "miss", 0)] * 9)
check("G10 단조 유지 시 위반 없음", g10["violations"] == [] and g10["monotonic"] is True
      and g10["eligible"] == 3)

# G11: ambiguous(동시터치 보수적 miss)는 건수만 따로 — 도달률 분모/분자는 판정 그대로
g11 = calibration.calibrate_grades(
    [("C", "hit", 0), ("C", "miss", 1), ("C", "miss", 1)])
check("G11 판별불가 건수 병기", g11["buckets"]["C"]["ambiguous"] == 2
      and close(g11["buckets"]["C"]["rate"], 1 / 3))

# G12: 입력 형식 무관 — 튜플/dict/sqlite3.Row 모두 같은 결과 (호출부 SELECT 호환)
g12 = calibration.calibrate_grades(
    [dict(grade=g, outcome=o, ambiguous=a) for g, o, a in CAL_ROWS])
check("G12 dict 입력 동등", g12["buckets"] == cal["buckets"])

TEST_DB3 = "cache/_test_calibration.db"
if os.path.exists(TEST_DB3):
    os.remove(TEST_DB3)
db.init_db(TEST_DB3)
from scripts.show_status import fetch_calibration_rows  # noqa: E402

with db.connect(TEST_DB3) as conn:
    rows_in = [("c1", "C", "hit", now), ("c2", "C", "miss", now),
               ("c3", None, "hit", now),          # 미채점 — 조회 단계에서 제외
               ("c4", "S", "hit", None)]          # 섀도 터치 — 조회 단계에서 제외
    for k, grade, outcome, t in rows_in:
        conn.execute(
            "INSERT INTO levels (signal_key, coin_symbol, ticker, direction, status, "
            "collected_at, author, grade, outcome, touched_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (k, "SOL", "KRW-SOL", "long", "touched", now - D, "A", grade, outcome, t))
    fetched = fetch_calibration_rows(conn)
check("G13 조회 SQL — 미채점/섀도터치 제외",
      sorted(fetched) == [("C", "hit", 0), ("C", "miss", 0)]
      and calibration.calibrate_grades(fetched)["pooled"]["n"] == 2)
os.remove(TEST_DB3)

# ── DI: R-멀티플·보유기간 분포 (2026-08-01 내부기능강화 리서치 영역3·4) ──────
# 순수 함수 — analytics/calibration.py 와 동일한 "프로젝트 모듈 import 0" 원칙.

R_ROWS = [(-0.5, "C"), (0.3, "C"), (1.5, "B"), (2.7, "A"), (4.0, "S")]
dist = distribution.r_multiple_distribution(R_ROWS)
check("DI1 R분포 버킷팅 + 평균", dist["n"] == 5 and close(dist["mean"], 1.6)
      and {b["label"]: b["n"] for b in dist["buckets"]}
      == {"-1~0": 1, "0~1": 1, "1~2": 1, "2~3": 1, "3+": 1})
check("DI2 구간 경계 — [lo,hi) 반개구간",
      distribution.r_multiple_distribution([(0.0, "C")])["buckets"][1]["n"] == 1  # 0~1
      and distribution.r_multiple_distribution([(1.0, "C")])["buckets"][2]["n"] == 1  # 1~2
      and distribution.r_multiple_distribution([(-1.0, "C")])["buckets"][0]["n"] == 1  # -1~0
      and distribution.r_multiple_distribution([(5.0, "C")])["buckets"][4]["n"] == 1)  # 3+(상한 오픈)
empty_dist = distribution.r_multiple_distribution([])
check("DI3 빈 입력 — n=0/mean=None/전 버킷 0",
      empty_dist["n"] == 0 and empty_dist["mean"] is None
      and all(b["n"] == 0 for b in empty_dist["buckets"]))
check("DI4 None(SL 미기재 tp_only) 제외",
      distribution.r_multiple_distribution([(-0.5, "C"), (None, "C")])["n"] == 1)

by_g = distribution.r_distribution_by_grade(R_ROWS + [(1.0, "X")])  # X=미정의 등급
check("DI5 등급별 분해 + 미정의 등급 제외", by_g["C"]["n"] == 2 and close(by_g["C"]["mean"], -0.1)
      and by_g["B"]["n"] == 1 and by_g["A"]["n"] == 1 and by_g["S"]["n"] == 1
      and by_g["D"]["n"] == 0 and "X" not in by_g)

HOLD_ROWS = [dict(touched_at=0, resolved_at=10 * 3600, outcome="hit"),
            dict(touched_at=0, resolved_at=20 * 3600, outcome="miss"),
            dict(touched_at=0, resolved_at=50 * 3600, outcome="miss"),
            dict(touched_at=0, resolved_at=100 * 3600, outcome="hit")]
hold = distribution.holding_period_distribution(HOLD_ROWS)
check("DI6 보유기간 버킷팅 + hit율",
      hold["n"] == 4 and hold["buckets"][0]["n"] == 2 and hold["buckets"][0]["hits"] == 1
      and close(hold["buckets"][0]["rate"], 0.5)
      and hold["buckets"][1]["n"] == 1 and hold["buckets"][1]["hits"] == 0
      and hold["buckets"][2]["n"] == 1 and close(hold["buckets"][2]["rate"], 1.0))
check("DI7 음수경과·비종결 outcome 제외",
      distribution.holding_period_distribution(
          [dict(touched_at=100, resolved_at=50, outcome="hit"),
           dict(touched_at=0, resolved_at=3600, outcome="watching")])["n"] == 0)
check("DI8 timeboxed_win 분모포함·분자제외(hit 아님)",
      distribution.holding_period_distribution(
          [dict(touched_at=0, resolved_at=10 * 3600, outcome="timeboxed_win")]
      )["buckets"][0] == {"label": "24h 이내", "n": 1, "hits": 0, "rate": 0.0})
check("DI9 튜플/dict 입력 동등(R분포)",
      distribution.r_multiple_distribution([(-0.5, "C"), (1.5, "B")])["buckets"]
      == distribution.r_multiple_distribution(
          [{"r_multiple": -0.5, "grade": "C"}, {"r_multiple": 1.5, "grade": "B"}])["buckets"])

TEST_DB4 = "cache/_test_distribution.db"
if os.path.exists(TEST_DB4):
    os.remove(TEST_DB4)
db.init_db(TEST_DB4)
with db.connect(TEST_DB4) as conn:
    rows_in4 = [
        # key, grade, outcome, r_multiple, touched_at, resolved_at
        ("d1", "C", "hit", 0.5, now - 2 * D, now - 2 * D + 10 * 3600),
        ("d2", "B", "miss", None, now - 2 * D, now - 2 * D + 5 * 3600),  # tp_only — r NULL
        ("d3", "S", None, None, None, None),                             # 미터치 — 제외
    ]
    for k, grade, outcome, r_multiple, touched_at, resolved_at in rows_in4:
        conn.execute(
            "INSERT INTO levels (signal_key, coin_symbol, ticker, direction, status, "
            "collected_at, author, grade, outcome, touched_at, resolved_at, r_multiple) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (k, "SOL", "KRW-SOL", "long", "touched", now - 3 * D, "A", grade, outcome,
             touched_at, resolved_at, r_multiple))
    r_rows_db = db.get_closed_r_rows(conn)
    hold_rows_db = db.get_closed_holding_rows(conn)
check("DI10 get_closed_r_rows/get_closed_holding_rows 조회 SQL",
      len(r_rows_db) == 1 and r_rows_db[0]["grade"] == "C"
      and close(r_rows_db[0]["r_multiple"], 0.5)
      and len(hold_rows_db) == 2 and {r["outcome"] for r in hold_rows_db} == {"hit", "miss"})
os.remove(TEST_DB4)

# ── RV: 역신호 확정/해제 판정 (S9, 2026-08-01 사용자 결정 Q1=B안/Q2=해제 있음) ──
# 순수 함수 — 스냅샷 dict(최신순) 만 받는다. 확정은 e_lb<0 엄격, 해제는 e_lb>=0.
# 표본 부족/게이트 미달/결측은 둘 다 False = 현 상태 보수적 유지.


def _snap(e_lb, neff_r, week="2026-W31"):
    return dict(week_kst=week, e_lb=e_lb, neff_r=neff_r)


_neg2 = [_snap(-0.9, 8.0), _snap(-0.5, 6.0, "2026-W30")]
_pos2 = [_snap(0.3, 8.0), _snap(0.1, 6.0, "2026-W30")]

check("RV1 스냅샷 0개 → 확정/해제 모두 False(증거 부족)",
      not ranking.is_confirmed_reverse([]) and not ranking.is_recovered_reverse([]))
check("RV2 스냅샷 1개뿐 → 조건 충족해도 False(2주 연속 아님)",
      not ranking.is_confirmed_reverse([_snap(-0.9, 8.0)])
      and not ranking.is_recovered_reverse([_snap(0.3, 8.0)]))
check("RV3 2주 모두 neff≥5 & e_lb<0 → 확정 True / 해제 False",
      ranking.is_confirmed_reverse(_neg2) and not ranking.is_recovered_reverse(_neg2))
check("RV4 한 주만 음수(직전 주 양수) → 확정 False",
      not ranking.is_confirmed_reverse([_snap(-0.9, 8.0), _snap(0.2, 6.0, "2026-W30")]))
check("RV5 neff 게이트 미달(4.9)·결측(None) → 확정 False",
      not ranking.is_confirmed_reverse([_snap(-0.9, 4.9), _snap(-0.5, 6.0, "2026-W30")])
      and not ranking.is_confirmed_reverse([_snap(-0.9, None), _snap(-0.5, 6.0, "2026-W30")]))
check("RV6 e_lb=0 경계 — 확정은 엄격히 <0 이라 False, 해제는 ≥0 이라 True",
      not ranking.is_confirmed_reverse([_snap(0.0, 8.0), _snap(-0.5, 6.0, "2026-W30")])
      and ranking.is_recovered_reverse([_snap(0.0, 8.0), _snap(0.0, 6.0, "2026-W30")]))
check("RV7 2주 모두 neff≥5 & e_lb≥0 → 해제 True", ranking.is_recovered_reverse(_pos2))
check("RV8 해제도 neff 게이트 필요 — 미달이면 False(확정 보수적 유지, Q2)",
      not ranking.is_recovered_reverse([_snap(0.3, 4.9), _snap(0.1, 6.0, "2026-W30")]))
check("RV9 한 주라도 음수면 해제 False",
      not ranking.is_recovered_reverse([_snap(0.3, 8.0), _snap(-0.1, 6.0, "2026-W30")]))
check("RV10 e_lb 결측(None)은 판정 불가 — 확정/해제 모두 False",
      not ranking.is_confirmed_reverse([_snap(None, 8.0), _snap(-0.5, 6.0, "2026-W30")])
      and not ranking.is_recovered_reverse([_snap(None, 8.0), _snap(0.5, 6.0, "2026-W30")]))
check("RV11 min_neff 파라미터 — 4.9 도 min_neff=3 이면 확정",
      ranking.is_confirmed_reverse([_snap(-0.9, 4.9), _snap(-0.5, 4.9, "2026-W30")],
                                   min_neff=3.0))

print()
n_checks = 62
print(f"{'전체 통과' if ok else '실패 있음'} ({n_checks}개 체크)")
sys.exit(0 if ok else 1)
