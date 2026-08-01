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
from storage import audit_dump, db

# 이 파일의 기존 블록들은 임시 DB 로 db.init_db 를 부르는데, 2026-07-27 카드 #4 이후
# init_db 에는 주간 감사 덤프 훅이 달려 있다. 기본으로 꺼두고(부수효과 없는 기존 검증
# 유지) 아래 A 섹션에서만 명시적으로 켜서 훅 자체를 검증한다.
audit_dump.SUPPRESSED = True

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

# ── C: 등급 캘리브레이션 섹션 (2026-07-27 기획 카드 #26) ──────────────────
# 수학 검증은 scripts/test_ranking.py(G 섹션) 담당 — 여기선 렌더 통합만 본다.
from analytics import calibration  # noqa: E402

CAL_ROWS = ([("S", "miss", 0)] * 4 + [("A", "miss", 0)] * 4
            + [("C", "hit", 0)] * 5 + [("C", "miss", 0)] * 7 + [("D", "hit", 0)] * 6)
CAL = calibration.calibrate_grades(CAL_ROWS)
msg8 = telegram.render_weekly_report(rows_by_author, now=now,
                                     calibration_result=CAL, **RK)
print(msg8)
print()

check("C1 섹션 헤더(종결 26건) + 신뢰수준 문구", "🎚️ 등급 캘리브레이션" in msg8
      and "TP1 도달률" in msg8 and "종결 26건" in msg8
      and "CI = 95% Wilson score 구간" in msg8)
check("C2 등급 행 + Wilson CI 표기", "S  0% (0/4)" in msg8 and "C  42% (5/12)" in msg8
      and "D  100% (6/6)" in msg8 and "CI 19%~68%" in msg8 and "CI 61%~100%" in msg8)
check("C3 표본 없는 등급(B)은 행 자체 생략", "  B  " not in msg8)
check("C4 소표본 등급에 '참고용' 병기 (S·A 만)",
      msg8.count("표본 부족(n&lt;5), 참고용") == 2)
check("C5 단조성 위반 표기 + CI 겹침 라벨",
      "단조성 위반 1건" in msg8 and "D 100% &gt; C 42%" in msg8
      and "CI 겹침 — 약한 신호" in msg8)
check("C6 산식 불변 명시(재검토 '신호'일 뿐)",
      "배점 재검토" in msg8 and "등급 산식·알림 필터는 이 리포트로 바뀌지 않습니다" in msg8)
check("C7 E_LB 와 다른 축임을 명시", "E_LB(작성자 실력 축)와는 <b>다른 축</b>" in msg8)
check("C8 HTML 안전 — 날 '<'/'>' 없음(parse_mode=HTML 400 방지)",
      "n<5" not in msg8 and "% > " not in msg8)
check("C9 랭킹 섹션은 그대로 (캘리브레이션은 정렬·수식 무관)",
      "E_LB +1.00" in msg8 and msg8.index("@GoodAuthor") < msg8.index("@BadAuthor"))

# C10: 단조 유지 케이스 → ✅ 문구, 위반 문구 없음
CAL_OK = calibration.calibrate_grades(
    [("S", "hit", 0)] * 9 + [("S", "miss", 0)]
    + [("C", "hit", 0)] * 5 + [("C", "miss", 0)] * 5
    + [("D", "hit", 0)] + [("D", "miss", 0)] * 9)
msg9 = telegram.render_weekly_report(rows_by_author, now=now,
                                     calibration_result=CAL_OK, **RK)
check("C10 단조 유지 시 ✅ 문구", "✅ 단조성 유지" in msg9 and "단조성 위반" not in msg9)

# C11: 판정 가능한 등급이 2개 미만 → 보류 문구
CAL_THIN = calibration.calibrate_grades([("C", "hit", 0)] * 3 + [("D", "miss", 0)] * 2)
msg10 = telegram.render_weekly_report(rows_by_author, now=now,
                                      calibration_result=CAL_THIN, **RK)
check("C11 판정 보류 문구", "단조성 판정 보류" in msg10 and "단조성 위반" not in msg10)

# C12: 종결 표본 0 → 섹션 통째 생략 (빈 표를 띄우지 않는다)
CAL_EMPTY = calibration.calibrate_grades([])
check("C12 표본 0 시 섹션 생략",
      "등급 캘리브레이션" not in telegram.render_weekly_report(
          rows_by_author, now=now, calibration_result=CAL_EMPTY, **RK))

# C13: 미주입(기존 호출부) → 섹션만 빠지고 나머지 출력은 완전히 동일
check("C13 미주입 시 기존 렌더 불변", "등급 캘리브레이션" not in msg6 and msg6 == msg)

# C14: 작성자 표본이 하나도 없어도(빈 DB 경로) 등급 축은 표시된다 — 독립 축이므로
msg11 = telegram.render_weekly_report({}, now=now, calibration_result=CAL, **RK)
check("C14 빈 작성자 표본에서도 등급 축 표시",
      "아직 표본 부족" in msg11 and "🎚️ 등급 캘리브레이션" in msg11)

# ── RC: 역신호 확정 구분 한 줄 (S9, 2026-08-01 — 표시 전용, 정렬·수식 불변) ──
msg_rc = telegram.render_weekly_report(rows_by_author, now=now,
                                       reverse_confirmed={"BadAuthor"}, **RK)
check("RC1 역신호 확정 한 줄 표기(필터 무변경 명시 + HTML 안전 &lt;)",
      "🔻 역신호 확정 1명" in msg_rc and "@BadAuthor" in msg_rc
      and "알림 필터 무변경" in msg_rc and "E_LB&lt;0" in msg_rc)
check("RC2 미주입 시 확정 줄 없음(기존 렌더 완전 불변)",
      "역신호 확정" not in msg and msg6 == msg)

# ── WD: R-멀티플 분포 + 보유기간 분포 (2026-08-01 내부기능강화 리서치 영역3·4) ──
# 수학 검증은 scripts/test_ranking.py(DI 섹션) 담당 — 여기선 렌더 통합만 본다.
from analytics import distribution  # noqa: E402

R_ROWS = [(-0.5, "C"), (0.3, "C"), (1.5, "B"), (2.7, "A"), (4.0, "S")]
DIST = distribution.r_multiple_distribution(R_ROWS)
DIST_BY_G = distribution.r_distribution_by_grade(R_ROWS)
msg_d1 = telegram.render_weekly_report(rows_by_author, now=now,
                                       r_distribution=DIST,
                                       r_distribution_by_grade=DIST_BY_G, **RK)
check("WD1 헤더 + 평균R", "📊 R-멀티플 분포 (종결 5건, R 트랙만 — SL 미기재 표본 제외)" in msg_d1
      and "평균 R = +1.60" in msg_d1)
check("WD2 구간별 바+건수", "-1~0 █ 1건" in msg_d1 and "0~1 █ 1건" in msg_d1
      and "1~2 █ 1건" in msg_d1 and "2~3 █ 1건" in msg_d1 and "3+ █ 1건" in msg_d1)
check("WD3 등급별 평균 병기 + 표본0 등급 생략",
      "S 평균+4.00(n=1)" in msg_d1 and "C 평균-0.10(n=2)" in msg_d1 and "D 평균" not in msg_d1)

HOLD_ROWS = [dict(touched_at=0, resolved_at=10 * 3600, outcome="hit"),
            dict(touched_at=0, resolved_at=20 * 3600, outcome="miss"),
            dict(touched_at=0, resolved_at=50 * 3600, outcome="miss"),
            dict(touched_at=0, resolved_at=100 * 3600, outcome="hit")]
HOLD = distribution.holding_period_distribution(HOLD_ROWS)
msg_d2 = telegram.render_weekly_report(rows_by_author, now=now, holding_period=HOLD, **RK)
check("WD4 보유기간 헤더+구간별 hit율",
      "⏱️ 보유기간 분포 (터치→종결 경과, 종결 4건)" in msg_d2
      and "24h 이내" in msg_d2 and "hit 50%" in msg_d2
      and "24~72h" in msg_d2 and "hit 0%" in msg_d2
      and "72h+" in msg_d2 and "hit 100%" in msg_d2)
check("WD5 표시전용 문구", "표시 전용 — 알림 필터에 영향 없음" in msg_d2)

HOLD_SPARSE = distribution.holding_period_distribution(
    [dict(touched_at=0, resolved_at=5 * 3600, outcome="hit")])
msg_d3 = telegram.render_weekly_report(rows_by_author, now=now, holding_period=HOLD_SPARSE, **RK)
check("WD6 표본 없는 구간 행 생략", "24~72h" not in msg_d3 and "72h+" not in msg_d3
      and "24h 이내" in msg_d3)

check("WD7 미주입 시 두 섹션 없음 + 기존 렌더 완전 불변",
      "R-멀티플 분포" not in msg6 and "보유기간 분포" not in msg6 and msg6 == msg)

DIST_EMPTY = distribution.r_multiple_distribution([])
HOLD_EMPTY = distribution.holding_period_distribution([])
check("WD8 표본 0 시 섹션 통째 생략",
      "R-멀티플 분포" not in telegram.render_weekly_report(
          rows_by_author, now=now, r_distribution=DIST_EMPTY, **RK)
      and "보유기간 분포" not in telegram.render_weekly_report(
          rows_by_author, now=now, holding_period=HOLD_EMPTY, **RK))

check("WD9 빈 작성자 표본에서도 독립 축으로 표시(등급 캘리브레이션과 동일 원칙)",
      "아직 표본 부족" in telegram.render_weekly_report({}, now=now, r_distribution=DIST, **RK)
      and "📊 R-멀티플 분포" in telegram.render_weekly_report({}, now=now, r_distribution=DIST, **RK))

# ── A: 주간 감사 덤프 + raw_text 보존정책 (2026-07-27 기획 카드 #4) ──────────
# storage/audit_dump.py + db.prune_raw_text. 알림·필터·등급과 무관한 기록 전용 기능이라
# 여기서 검증하는 건 "파일이 정확히 나오는가 / 원문이 아카이브된 뒤에만 지워지는가 /
# 실패해도 회차를 안 죽이는가" 세 가지다.
import json  # noqa: E402
import shutil  # noqa: E402
from pathlib import Path  # noqa: E402

from config import settings  # noqa: E402

A_DB = "cache/_test_audit.db"
A_DIR = Path("cache/_test_audit_out")
DAY = 86400.0

for _p in (A_DB, str(A_DIR)):
    if os.path.isdir(_p):
        shutil.rmtree(_p)
    elif os.path.exists(_p):
        os.remove(_p)

db.init_db(A_DB)   # SUPPRESSED=True 라 이 시점엔 덤프가 돌지 않는다


def _ins(conn, key, status, collected_at, raw, **kw):
    cols = ["signal_key", "coin_symbol", "ticker", "direction", "status",
            "collected_at", "raw_text"]
    vals = [key, "SOL", "KRW-SOL", "long", status, collected_at, raw]
    for k, v in kw.items():
        cols.append(k)
        vals.append(v)
    conn.execute(f"INSERT INTO levels ({','.join(cols)}) VALUES "
                 f"({','.join('?' for _ in cols)})", vals)


with db.connect(A_DB) as conn:
    # 1) 활성 — 원문은 재파싱 자가치유가 아직 쓰므로 절대 지워지면 안 된다
    _ins(conn, "a_watch", "watching", now - 40 * DAY, "원문-감시중", grade="B")
    _ins(conn, "a_prev", "previewed", now - 40 * DAY, "원문-예고중", grade="A")
    # 2) 종결 + 보존기간 경과 → 정리 대상
    _ins(conn, "a_old_touch", "touched", now - 60 * DAY, "원문-오래된터치",
         touched_at=now - 50 * DAY, resolved_at=now - 45 * DAY, grade="C")
    _ins(conn, "a_old_exp", "expired", now - 60 * DAY, "원문-오래된만료",
         expired_at=now - 40 * DAY, grade="D")
    # 섀도 터치(touched_at NULL) — 종결 시각 폴백이 collected_at 으로 떨어지는 경로
    _ins(conn, "a_shadow", "touched", now - 60 * DAY, "원문-섀도", grade="C")
    # 3) 종결이지만 보존기간 이내 → 아직 유지 (다음 덤프가 한 번 더 담고 지나간다)
    _ins(conn, "a_new_touch", "touched", now - 3 * DAY, "원문-최근터치",
         touched_at=now - 2 * DAY, grade="S")
    conn.execute("INSERT INTO daily_stats (day_kst, touches_total) VALUES (?,?)",
                 ("2026-07-20", 3))
    conn.execute("INSERT INTO daily_stats (day_kst, touches_total) VALUES (?,?)",
                 ("2026-07-19", 1))

WEEK = db.week_kst(now)
with db.connect(A_DB) as conn:
    res = audit_dump.run_weekly_audit(conn, A_DB, now=now, out_dir=A_DIR)

lv_path = A_DIR / f"levels_{WEEK}.ndjson"
ds_path = A_DIR / f"daily_stats_{WEEK}.ndjson"
check("A1 KST 주차 파일명으로 levels·daily_stats 덤프 생성",
      lv_path.exists() and ds_path.exists()
      and set(res["files"]) == {lv_path.name, ds_path.name})

lv_lines = lv_path.read_text(encoding="utf-8").splitlines()
lv_meta = json.loads(lv_lines[0])
lv_rows = [json.loads(x) for x in lv_lines[1:]]
check("A2 첫 줄 메타(테이블·주차·행수) + 나머지는 한 줄 한 행",
      lv_meta["_table"] == "levels" and lv_meta["_week_kst"] == WEEK
      and lv_meta["_rows"] == 6 and len(lv_rows) == 6)
check("A3 행은 객체(컬럼명 자기기술) + id 오름차순 고정",
      isinstance(lv_rows[0], dict) and lv_rows[0]["signal_key"] == "a_watch"
      and [r["id"] for r in lv_rows] == sorted(r["id"] for r in lv_rows)
      and lv_rows[0]["grade"] == "B")
ds_rows = [json.loads(x) for x in ds_path.read_text(encoding="utf-8").splitlines()[1:]]
check("A4 daily_stats 도 덤프(day_kst 오름차순)",
      [r["day_kst"] for r in ds_rows] == ["2026-07-19", "2026-07-20"])

dumped_raw = {r["signal_key"]: r["raw_text"] for r in lv_rows}
check("A5 덤프에 원문(raw_text) 포함 — 정리 대상 행도 원문이 남는다",
      dumped_raw["a_old_touch"] == "원문-오래된터치"
      and dumped_raw["a_old_exp"] == "원문-오래된만료"
      and dumped_raw["a_shadow"] == "원문-섀도")

with db.connect(A_DB) as conn:
    left = {r["signal_key"]: r["raw_text"] for r in
            conn.execute("SELECT signal_key, raw_text FROM levels").fetchall()}
check("A6 종결+보존기간 경과 행의 원문만 DB 에서 비워짐(3행)",
      res["raw_text_pruned"] == 3 and left["a_old_touch"] is None
      and left["a_old_exp"] is None and left["a_shadow"] is None)
check("A7 활성(watching/previewed) 원문은 보존 — 재파싱 자가치유가 쓴다",
      left["a_watch"] == "원문-감시중" and left["a_prev"] == "원문-예고중")
check("A8 종결이라도 보존기간 이내면 보존", left["a_new_touch"] == "원문-최근터치")

# A9: 같은 주차 재실행은 같은 파일을 덮어쓴다(멱등) + 이미 비운 원문은 재정리 0건
with db.connect(A_DB) as conn:
    res2 = audit_dump.run_weekly_audit(conn, A_DB, now=now, out_dir=A_DIR)
check("A9 같은 주차 재실행 멱등(파일 증식 없음, 정리 0건)",
      len(list(A_DIR.glob("*.ndjson"))) == 2 and res2["raw_text_pruned"] == 0)

# ── A10: 보존정책 — 최근 N주차분만 남긴다 ─────────────────────────────
for wk in ("2026-W20", "2026-W21", "2026-W22"):
    for t in ("levels", "daily_stats"):
        (A_DIR / f"{t}_{wk}.ndjson").write_text("{}\n", encoding="utf-8")
(A_DIR / "무관한파일.txt").write_text("x", encoding="utf-8")
_keep_old = settings.SETTINGS["audit_dump_keep_weeks"]
settings.SETTINGS["audit_dump_keep_weeks"] = 2
removed = audit_dump.prune_old_dumps(A_DIR, 2)
left_weeks = sorted({p.name.split("_")[-1][:-len(".ndjson")]
                     for p in A_DIR.glob("*.ndjson")})
check("A11 최근 2주차분만 남기고 오래된 덤프 삭제",
      left_weeks == sorted({WEEK, "2026-W22"}) and len(removed) == 4)
check("A12 덤프 패턴이 아닌 파일은 건드리지 않음", (A_DIR / "무관한파일.txt").exists())
settings.SETTINGS["audit_dump_keep_weeks"] = _keep_old

# ── A13: 주기 게이트 (meta 기반, run_cycle 수집/리포트와 같은 패턴) ─────
G_DB = "cache/_test_audit_gate.db"
G_DIR = Path("cache/_test_audit_gate_out")
for _p in (G_DB, str(G_DIR)):
    if os.path.isdir(_p):
        shutil.rmtree(_p)
    elif os.path.exists(_p):
        os.remove(_p)
db.init_db(G_DB)
audit_dump.SUPPRESSED = False   # 여기서부터 훅/게이트 자체를 검증
with db.connect(G_DB) as conn:
    s1 = audit_dump.maybe_weekly_audit(conn, G_DB, now=now, out_dir=G_DIR)
    s2 = audit_dump.maybe_weekly_audit(conn, G_DB, now=now + 3600, out_dir=G_DIR)
    s3 = audit_dump.maybe_weekly_audit(conn, G_DB, now=now + 8 * DAY, out_dir=G_DIR)
check("A13 최초 실행 ok → 주기 미도래 skipped → 8일 후 ok", (s1, s2, s3) == ("ok", "skipped", "ok"))

with db.connect(G_DB) as conn:
    # 미래 시각 meta(시계 역행/수동 편집)는 신뢰하지 않는다 — 영구 굶주림 방지
    db.set_meta(conn, audit_dump.META_LAST_DUMP, str(now + 999 * DAY))
    s4 = audit_dump.maybe_weekly_audit(conn, G_DB, now=now + 9 * DAY, out_dir=G_DIR)
check("A14 미래 시각 meta 무시(영구 굶주림 방지)", s4 == "ok")

# ── A15: 실패 격리 — 덤프가 터져도 예외가 밖으로 안 나가고 백오프만 남는다 ──
_real_dump = audit_dump.dump_table


def _boom(*a, **k):
    raise RuntimeError("덤프 강제 실패")


audit_dump.dump_table = _boom
with db.connect(G_DB) as conn:
    db.set_meta(conn, audit_dump.META_LAST_DUMP, "0")
    db.set_meta(conn, audit_dump.META_LAST_DUMP_FAIL, "0")
    s5 = audit_dump.maybe_weekly_audit(conn, G_DB, now=now + 20 * DAY, out_dir=G_DIR)
    fail_at = db.get_meta(conn, audit_dump.META_LAST_DUMP_FAIL)
    # 실패 직후 재시도는 백오프로 막힌다(2분 회차마다 재시도 방지)
    s6 = audit_dump.maybe_weekly_audit(conn, G_DB, now=now + 20 * DAY + 60, out_dir=G_DIR)
check("A15 덤프 실패는 예외 전파 없이 failed + 실패시각 기록", s5 == "failed" and fail_at)
check("A16 실패 후 백오프 동안은 재시도 안 함", s6 == "skipped")

with db.connect(G_DB) as conn:
    before = conn.execute("SELECT COUNT(*) n FROM levels").fetchone()["n"]
check("A17 실패해도 DB 는 멀쩡(회차 생존)", before == 0)

# init_db 훅 자체의 격리 — 감사 기능이 통째로 터져도 스키마 초기화는 성공해야 한다
_real_maybe = audit_dump.maybe_weekly_audit
audit_dump.maybe_weekly_audit = _boom
try:
    db.init_db(G_DB)
    hook_isolated = True
except Exception:
    hook_isolated = False
audit_dump.maybe_weekly_audit = _real_maybe
audit_dump.dump_table = _real_dump
check("A18 init_db 훅 실패 격리 — 감사가 터져도 init_db 는 성공", hook_isolated)

# ── A19: 원문 제외 모드면 정리도 함께 멈춘다(아카이브 없는 삭제 경로 없음) ──
N_DB = "cache/_test_audit_noraw.db"
N_DIR = Path("cache/_test_audit_noraw_out")
for _p in (N_DB, str(N_DIR)):
    if os.path.isdir(_p):
        shutil.rmtree(_p)
    elif os.path.exists(_p):
        os.remove(_p)
db.init_db(N_DB)
with db.connect(N_DB) as conn:
    _ins(conn, "n_old", "touched", now - 60 * DAY, "원문-지우면안됨",
         touched_at=now - 50 * DAY)
_raw_old = settings.SETTINGS["audit_dump_include_raw_text"]
settings.SETTINGS["audit_dump_include_raw_text"] = False
with db.connect(N_DB) as conn:
    res_n = audit_dump.run_weekly_audit(conn, N_DB, now=now, out_dir=N_DIR)
settings.SETTINGS["audit_dump_include_raw_text"] = _raw_old
n_meta = json.loads((N_DIR / f"levels_{WEEK}.ndjson").read_text(
    encoding="utf-8").splitlines()[0])
with db.connect(N_DB) as conn:
    n_left = conn.execute("SELECT raw_text FROM levels").fetchone()["raw_text"]
check("A19 원문 제외 모드: 덤프에 raw_text 컬럼 없음",
      "raw_text" not in n_meta["_columns"])
check("A20 원문 제외 모드: DB 원문 정리도 멈춘다(아카이브 없이 삭제 금지)",
      res_n["raw_text_pruned"] == 0 and n_left == "원문-지우면안됨")

# ── A21: 비정상 실수(NaN/Inf)는 표준 JSON 이 아니므로 null 로 눕힌다 ────
with db.connect(N_DB) as conn:
    conn.execute("UPDATE levels SET score=?, rr=? WHERE signal_key='n_old'",
                 (float("nan"), float("inf")))
with db.connect(N_DB) as conn:
    audit_dump.dump_table(conn, N_DIR, "levels", "2099-W01", now=now)
nan_row = json.loads((N_DIR / "levels_2099-W01.ndjson").read_text(
    encoding="utf-8").splitlines()[1])
check("A21 NaN/Infinity → null (jq 등 외부 도구가 읽을 수 있게)",
      nan_row["score"] is None and nan_row["rr"] is None)

# ── A22: SUPPRESSED 스위치 (읽기 전용 잡이 주기 meta 를 앞당기지 못하게) ──
audit_dump.SUPPRESSED = True
with db.connect(N_DB) as conn:
    db.set_meta(conn, audit_dump.META_LAST_DUMP, "0")
    s7 = audit_dump.maybe_weekly_audit(conn, N_DB, now=now + 99 * DAY, out_dir=N_DIR)
    still_zero = db.get_meta(conn, audit_dump.META_LAST_DUMP)
check("A22 SUPPRESSED 면 덤프도 meta 갱신도 없음", s7 == "skipped" and still_zero == "0")

for _p in (A_DB, G_DB, N_DB):
    if os.path.exists(_p):
        os.remove(_p)
for _d in (A_DIR, G_DIR, N_DIR):
    shutil.rmtree(_d, ignore_errors=True)

print()
n_checks = 75
print(f"{'전체 통과' if ok else '실패 있음'} ({n_checks}개 체크)")
sys.exit(0 if ok else 1)
