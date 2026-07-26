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

# ── W4: 신규 2건 반영 렌더 (초과 적중률 베이스라인 + 합의 표시) ──────────
# 기준선 10%(n=29). GoodAuthor 5승0패 → 원시 100% → +90%p,
# BadAuthor 0승5패 → 0% → -10%p, TpOnlyAuthor 7승0패 → +90%p
BASE = {"n": 29, "positive": 3, "rate": 3 / 29}
CONF = {
    "GoodAuthor": {"multi": 1, "total": 7, "cr": 1 / 7},
    "TpOnlyAuthor": {"multi": 3, "total": 4, "cr": 0.75},
    "BadAuthor": {"multi": 0, "total": 1, "cr": 0.0},   # 클러스터 1개 → 표시 생략
}
msg4 = telegram.render_weekly_report(rows_by_author, now=now, baseline=BASE,
                                     confluence=CONF, baseline_min_n=20,
                                     confluence_min_clusters=2, **RK)
print(msg4)
print()

check("W4 베이스라인 헤더(10%, n=29)", "🎲 초과 적중률" in msg4
      and "24h 보유 시 수익권 10%" in msg4 and "n=29" in msg4)
check("W4 GoodAuthor 초과분 +90%p", "원시승률 100% → 베이스라인 대비 +90%p" in msg4)
check("W4 BadAuthor 초과분 -10%p", "원시승률 0% → 베이스라인 대비 -10%p" in msg4)
check("W4 caveat 병기(판정 기준 상이 + 하락장)",
      "판정 기준이 서로 다릅니다" in msg4 and "하락장" in msg4)
check("W4 합의 표시(랭킹 행)", "🤝 합의 참여 1/7회(14%)" in msg4)
check("W4 합의 표시(승률축 행도)", "🤝 합의 참여 3/4회(75%)" in msg4)
check("W4 클러스터 1개짜리는 합의 표시 생략", "0/1회" not in msg4)
check("W4 정렬 키 불변 — 합의율 높은 쪽이 순위를 못 밀어냄(E_LB 내림차순 유지)",
      msg4.index("@GoodAuthor") < msg4.index("@BadAuthor")
      and "E_LB +1.00" in msg4 and "E_LB -1.00" in msg4)
check("W4 표본부족 작성자는 초과 적중률 섹션에 미등장(표본부족 안내에만 1회)",
      msg4.count("@NewAuthor") == 1)

# W5: pooled 표본 미달(n=19 < 20) → 섹션 통째로 생략
msg5 = telegram.render_weekly_report(rows_by_author, now=now,
                                     baseline={"n": 19, "positive": 2, "rate": 2 / 19},
                                     confluence=CONF, baseline_min_n=20, **RK)
check("W5 표본 미달 시 베이스라인 섹션 생략", "초과 적중률" not in msg5)
check("W5 그래도 합의 표시는 유지", "🤝 합의 참여" in msg5)

# W6: 미주입(기존 호출부 호환) → 두 기능 모두 조용히 빠지고 나머지는 동일
msg6 = telegram.render_weekly_report(rows_by_author, now=now, **RK)
check("W6 미주입 시 두 섹션 없음", "초과 적중률" not in msg6 and "🤝" not in msg6)
check("W6 기존 렌더 결과 불변", msg6 == msg)

# W7: raw_records 주입 경로(운영 경로) — rows 카운트가 아니라 DB 집계값을 쓴다
msg7 = telegram.render_weekly_report(
    {"GoodAuthor": rows_of("hit", 1.0, 5)}, now=now, baseline=BASE,
    raw_records={"GoodAuthor": {"wins": 3, "losses": 7}}, baseline_min_n=20, **RK)
check("W7 raw_records 우선 사용(3승7패=30%)", "원시승률 30%" in msg7)

print()
n_checks = 29
print(f"{'전체 통과' if ok else '실패 있음'} ({n_checks}개 체크)")
sys.exit(0 if ok else 1)
