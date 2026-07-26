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

from analytics import ranking
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

# I2: 렌더 게이트 — n_eff 4.9 미표시 / 5.0 표시 (raw 4승2패=6건이어도 n_eff 가 기준)
from notify import telegram  # noqa: E402

rep = dict(coin_symbol="LINK", ticker="KRW-LINK", direction="long", entry_usd=10.0,
           sl_usd=9.0, tp_usd=12.0, grade="B", score=60, author="GateA",
           author_followers=10, author_hit_rate=None, author_hit_count=None,
           author_whitelisted=0, mcap_rank=19, mcap_tier_icon="🥇",
           post_url="https://tv.com/g", post_age_minutes=100,
           author_self_wins=4, author_self_losses=2,
           author_touched_n=6, author_untouched_expired=0)
msg_a = telegram.render_alert("touch", "LINK", [dict(rep, author_self_neff=4.9)],
                              10.05 * 1400, 1400)
msg_b = telegram.render_alert("touch", "LINK", [dict(rep, author_self_neff=5.0)],
                              10.05 * 1400, 1400)
check("I2a n_eff 4.9 → 🏹 미표시", "🏹" not in msg_a)
check("I2b n_eff 5.0 → 🏹 표시", "🏹" in msg_b and "4승2패" in msg_b)

print()
n_checks = 15
print(f"{'전체 통과' if ok else '실패 있음'} ({n_checks}개 체크)")
sys.exit(0 if ok else 1)
