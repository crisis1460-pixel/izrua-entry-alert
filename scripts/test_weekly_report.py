# notify.telegram.render_weekly_report 단위·통합 테스트 (analytics/ranking.py 는
# 재설계 없이 그대로 사용 — 여기선 텍스트 조립·섹션 분류·2트랙 확정만 검증).
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from notify import telegram
from storage import db

ok = True


def check(name, cond):
    global ok
    print(("✅" if cond else "❌"), name)
    ok = ok and cond


now = 1_800_000_000.0
RK = dict(min_neff=5.0, half_life_days=90.0, z=1.28, prior_m=10)


def rows_of(outcome, r_multiple, n, hit_rate=None, hit_count=None):
    return [dict(outcome=outcome, r_multiple=r_multiple, touched_at=now,
                 author_hit_rate=hit_rate, author_hit_count=hit_count) for _ in range(n)]


# ── W1: 표본 전혀 없음 → 우아한 빈 상태 ──────────────────────────────
msg_empty = telegram.render_weekly_report({}, now=now, **RK)
check("W1 빈 DB 우아한 표시", "아직 표본 부족" in msg_empty)
check("W1 헤더는 유지", "📈" in msg_empty and "주간 성적 리포트" in msg_empty)

# ── W2: 종합 시나리오 ────────────────────────────────────────────────
# GoodAuthor: R=[1]*5 (전부 hit) → mean 1, var 0, n_eff 5 → E_LB +1.00, 게이트 통과, 정신호
# BadAuthor : R=[-1]*5 (전부 miss) → E_LB -1.00, 게이트 통과, 역신호 후보(🔻)
# TpOnlyAuthor: r_multiple 전부 None, 7승0패(tp_only) → R NULL 2트랙, 승률만 확정
#               p_hat = (1+7)/(1+1+7+0) = 8/9 ≈ 0.889 (워쳐 prior 없음 → Beta(1,1))
# NewAuthor : 2건뿐 (게이트 미달) → 표본부족
rows_by_author = {
    "GoodAuthor": rows_of("hit", 1.0, 5),
    "BadAuthor": rows_of("miss", -1.0, 5),
    "TpOnlyAuthor": rows_of("hit", None, 7),
    "NewAuthor": rows_of("hit", 1.0, 2),
}
msg = telegram.render_weekly_report(rows_by_author, now=now, **RK)
print(msg)
print()

check("W2 작성자/표본 카운트 헤더", "작성자 4명" in msg and "종결 표본 19건" in msg)
check("W2 GoodAuthor 랭킹 등재 +1.00", "@GoodAuthor" in msg and "E_LB +1.00" in msg)
check("W2 BadAuthor 랭킹 등재 -1.00 + 역신호 표시", "@BadAuthor 🔻" in msg and "E_LB -1.00" in msg)
check("W2 GoodAuthor가 BadAuthor보다 먼저(내림차순)",
      msg.index("@GoodAuthor") < msg.index("@BadAuthor"))
check("W2 역신호 후보 안내 1명", "역신호 후보 1명" in msg)
check("W2 TpOnlyAuthor 승률만 확정 섹션(7승0패, 89%)",
      "@TpOnlyAuthor" in msg and "7승0패" in msg and "89%" in msg
      and "승률만 확정" in msg)
check("W2 TpOnlyAuthor 는 랭킹(E_LB) 섹션엔 미등재",
      msg.split("승률만 확정")[0].count("@TpOnlyAuthor") == 0)
check("W2 NewAuthor 표본부족 섹션(2건)", "@NewAuthor(2건)" in msg and "표본 부족" in msg)

# ── W3: 게이트 통과자 전무 → 랭킹 "없음" 문구, 안내 섹션만 ────────────
msg3 = telegram.render_weekly_report({"NewAuthor": rows_of("hit", 1.0, 2)}, now=now, **RK)
check("W3 게이트 통과자 없음 문구", "게이트 통과 작성자 없음" in msg3)
check("W3 승률만 확정 섹션은 생략(대상 없음)", "승률만 확정" not in msg3)
check("W3 표본부족은 그대로 안내", "@NewAuthor(2건)" in msg3)

# ── I1: 임시 DB 통합 — list_authors_with_outcomes + get_author_outcome_rows 연동 ──
TEST_DB = "cache/_test_weekly_report.db"
if os.path.exists(TEST_DB):
    os.remove(TEST_DB)
db.init_db(TEST_DB)
with db.connect(TEST_DB) as conn:
    # GoodAuthor: 5건 hit, r=+1 (실제 도달 터치 + 종결)
    for i, r in enumerate([1.0] * 5):
        conn.execute(
            "INSERT INTO levels (signal_key, coin_symbol, ticker, direction, status, "
            "collected_at, author, outcome, r_multiple, touched_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (f"g{i}", "SOL", "KRW-SOL", "long", "touched", now - 86400, "GoodAuthor",
             "hit", r, now))
    # 섀도 터치(touched_at NULL) — 판정·통계 제외 대상이니 리포트에도 안 잡혀야 함
    conn.execute(
        "INSERT INTO levels (signal_key, coin_symbol, ticker, direction, status, "
        "collected_at, author, outcome, r_multiple, touched_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("shadow1", "SOL", "KRW-SOL", "long", "touched", now - 86400, "GhostAuthor",
         "hit", 1.0, None))
    # 진행 중(outcome NULL) — 미종결이라 제외 대상
    conn.execute(
        "INSERT INTO levels (signal_key, coin_symbol, ticker, direction, status, "
        "collected_at, author, touched_at) VALUES (?,?,?,?,?,?,?,?)",
        ("pending1", "SOL", "KRW-SOL", "long", "touched", now - 3600, "PendingAuthor", now))

    authors = db.list_authors_with_outcomes(conn)
    check("I1 섀도터치/미종결 작성자 제외", "GhostAuthor" not in authors
          and "PendingAuthor" not in authors and "GoodAuthor" in authors)
    rows_by_author_db = {a: db.get_author_outcome_rows(conn, a) for a in authors}

msg_db = telegram.render_weekly_report(rows_by_author_db, now=now, **RK)
check("I1 DB 연동 렌더 결과에 GoodAuthor 랭킹 반영", "@GoodAuthor" in msg_db and "E_LB +1.00" in msg_db)
os.remove(TEST_DB)

print()
n_checks = 15
print(f"{'전체 통과' if ok else '실패 있음'} ({n_checks}개 체크)")
sys.exit(0 if ok else 1)
