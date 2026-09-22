# notify.telegram.render_weekly_report 단위·통합 테스트.
#
# 2026-09-22 R4: 리포트를 "지표 나열형 → 의사결정형"으로 전면 개편하면서 이 파일도
# 함께 갈았다. 랭킹 수학(analytics/ranking.py)과 캘리브레이션 수학
# (analytics/calibration.py)은 재설계 없이 그대로라 여기선 **텍스트 조립·섹션 선택·
# 표본 게이트·길이 예산**만 본다. 새 순수 계산부(analytics/weekly.py)의 손계산은
# 이 파일의 V 섹션이 렌더와 함께 검증한다.
#
# 제거된 섹션(🎲 초과 적중률 / 📊 R-멀티플 분포 / ⏱️ 보유기간 분포 / 🌡️ 히트맵 /
# 🤝 합의 / 구 산식 병기)은 "출력에 없다"를 역으로 검증한다(X 섹션).
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from analytics import weekly
from notify import telegram
from storage import audit_dump, db

# 이 파일의 기존 블록들은 임시 DB 로 db.init_db 를 부르는데, 2026-07-27 카드 #4 이후
# init_db 에는 주간 감사 덤프 훅이 달려 있다. 기본으로 꺼두고(부수효과 없는 기존 검증
# 유지) 아래 A 섹션에서만 명시적으로 켜서 훅 자체를 검증한다.
audit_dump.SUPPRESSED = True

ok = True
n_checks = 0


def check(name, cond):
    global ok, n_checks
    n_checks += 1
    print(("✅" if cond else "❌"), name)
    ok = ok and cond


now = 1_800_000_000.0
DAY = 86400.0
RK = dict(min_neff=5.0, half_life_days=90.0, z=1.28, prior_m=10)


def rows_of(outcome, r_multiple, n, hit_rate=None, hit_count=None):
    return [dict(outcome=outcome, r_multiple=r_multiple, touched_at=now,
                 author_hit_rate=hit_rate, author_hit_count=hit_count) for _ in range(n)]


def lv(outcome, *, r=None, touched=None, resolved=None, collected=None,
       author=None, direction="long", touch=1000.0, resolve=None, vrank=None):
    """levels 한 행(주간 리포트 v2 조회 결과 형식)."""
    t = now - 2 * DAY if touched is None else touched
    return dict(outcome=outcome, r_multiple=r, touched_at=t,
                resolved_at=(t + 24 * 3600) if resolved is None else resolved,
                collected_at=(t - 3600) if collected is None else collected,
                author=author, direction=direction,
                touch_price_krw=touch,
                resolve_price_krw=(touch * 1.02 if resolve is None else resolve),
                touch_volume_rank=vrank, judgment_mode="tp_sl")


# ── W: 랭킹 섹션 (기존 로직 불변 — 표시만 상위 N) ─────────────────────
# W1: 표본 전혀 없음 → 우아한 빈 상태
msg_empty = telegram.render_weekly_report({}, now=now, **RK)
check("W1 빈 DB 우아한 표시", "아직 표본 부족" in msg_empty)
check("W1 헤더 유지 + 주차(KST) 행 + 각주",
      "📈" in msg_empty and "주간 성적 리포트" in msg_empty
      and "(KST, 7일)" in msg_empty and "터치 후 7일" in msg_empty)

# W2: 종합 시나리오
# GoodAuthor: R=[1]*5 (전부 hit) → E_LB +1.00, 게이트 통과, 정신호
# BadAuthor : R=[-1]*5 (전부 miss) → E_LB -1.00, 역신호 후보(🔻)
# TpOnlyAuthor: r_multiple 전부 None, 7승0패 → R NULL 2트랙(랭킹 미등재)
# NewAuthor : 2건뿐 → 표본부족
rows_by_author = {
    "GoodAuthor": rows_of("hit", 1.0, 5),
    "BadAuthor": rows_of("miss", -1.0, 5),
    "TpOnlyAuthor": rows_of("hit", None, 7),
    "NewAuthor": rows_of("hit", 1.0, 2),
}
msg = telegram.render_weekly_report(rows_by_author, now=now, **RK)
print(msg)
print()

check("W2 표본 헤더(작성자/종결)", "작성자 4명" in msg and "종결 19건" in msg)
check("W2 GoodAuthor 랭킹 등재 +1.00", "@GoodAuthor" in msg and "E_LB +1.00" in msg)
check("W2 BadAuthor 랭킹 등재 -1.00 + 역신호 표시",
      "@BadAuthor 🔻" in msg and "E_LB -1.00" in msg)
check("W2 GoodAuthor가 BadAuthor보다 먼저(내림차순)",
      msg.index("@GoodAuthor") < msg.index("@BadAuthor"))
check("W2 역신호 후보 1명 안내", "역신호 후보 1명" in msg)
check("W2 TpOnlyAuthor 승률만 확정(7승0패, 89%)",
      "@TpOnlyAuthor" in msg and "7승0패" in msg and "89%" in msg
      and "승률만 확정" in msg)
check("W2 TpOnlyAuthor 는 E_LB 랭킹엔 미등재",
      msg.split("승률만 확정")[0].count("@TpOnlyAuthor") == 0)
check("W2 NewAuthor 표본부족 섹션(2건)", "@NewAuthor(2건)" in msg and "표본 부족" in msg)

# W3: 게이트 통과자 전무
msg3 = telegram.render_weekly_report({"NewAuthor": rows_of("hit", 1.0, 2)}, now=now, **RK)
check("W3 게이트 통과자 없음 문구", "게이트 통과 작성자 없음" in msg3)
check("W3 승률만 확정 섹션 생략(대상 없음)", "승률만 확정" not in msg3)

# W4: 상위 N 만 표시 (2026-09-22 — 수학·정렬은 불변, 표시만 자른다)
many = {f"A{i:02d}": rows_of("hit", 1.0 + i * 0.1, 5) for i in range(8)}
msg_top = telegram.render_weekly_report(many, now=now, top_authors=5, **RK)
check("W4 상위 5명만 표시 + 나머지 건수 안내",
      msg_top.count("E_LB +") == 5 and "외 3명 (상위 5만 표시)" in msg_top)
check("W4 1위는 E_LB 최대(A07)", "1. @A07" in msg_top)

# W5: 작성자 0명이어도 다른 섹션은 독립적으로 나온다
msg_noauthor = telegram.render_weekly_report(
    {}, now=now, current=weekly.summary([lv("hit")] * 12, alerts=4),
    previous=weekly.summary([lv("miss")] * 12, alerts=9), **RK)
check("W5 작성자 0명 + v2 데이터 → 랭킹은 빈 상태, 요약은 정상 출력",
      "아직 표본 부족" in msg_noauthor and "📌" in msg_noauthor
      and "알림 수  4건 (9건 → ▼)" in msg_noauthor)

# ── I1: 임시 DB 통합 (list_authors_with_outcomes + get_author_outcome_rows) ──
TEST_DB = "cache/_test_weekly_report.db"
if os.path.exists(TEST_DB):
    os.remove(TEST_DB)
db.init_db(TEST_DB)
with db.connect(TEST_DB) as conn:
    for i, r in enumerate([1.0] * 5):
        conn.execute(
            "INSERT INTO levels (signal_key, coin_symbol, ticker, direction, status, "
            "collected_at, author, outcome, r_multiple, touched_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (f"g{i}", "SOL", "KRW-SOL", "long", "touched", now - 86400, "GoodAuthor",
             "hit", r, now))
    # 섀도 터치(touched_at NULL) — 판정·통계 제외 대상
    conn.execute(
        "INSERT INTO levels (signal_key, coin_symbol, ticker, direction, status, "
        "collected_at, author, outcome, r_multiple, touched_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("shadow1", "SOL", "KRW-SOL", "long", "touched", now - 86400, "GhostAuthor",
         "hit", 1.0, None))
    conn.execute(
        "INSERT INTO levels (signal_key, coin_symbol, ticker, direction, status, "
        "collected_at, author, touched_at) VALUES (?,?,?,?,?,?,?,?)",
        ("pending1", "SOL", "KRW-SOL", "long", "touched", now - 3600, "PendingAuthor", now))

    authors = db.list_authors_with_outcomes(conn)
    check("I1 섀도터치/미종결 작성자 제외", "GhostAuthor" not in authors
          and "PendingAuthor" not in authors and "GoodAuthor" in authors)
    rows_by_author_db = {a: db.get_author_outcome_rows(conn, a) for a in authors}

msg_db = telegram.render_weekly_report(rows_by_author_db, now=now, **RK)
check("I1 DB 연동 렌더 결과에 GoodAuthor 랭킹 반영",
      "@GoodAuthor" in msg_db and "E_LB +1.00" in msg_db)

# ── I2: 신규 조회 함수 (읽기 전용, 2026-09-22 R4) ─────────────────────
with db.connect(TEST_DB) as conn:
    conn.execute("UPDATE levels SET resolved_at=? WHERE signal_key='g0'", (now - 3 * DAY,))
    conn.execute("UPDATE levels SET resolved_at=? WHERE signal_key='g1'", (now - 10 * DAY,))
    conn.execute("INSERT INTO alerts_log (coin_symbol, kind, level_ids, sent_at, "
                 "day_kst, sent) VALUES (?,?,?,?,?,?)",
                 ("SOL", "touch", "1", now - 2 * DAY, "2027-01-14", 1))
    conn.execute("INSERT INTO alerts_log (coin_symbol, kind, level_ids, sent_at, "
                 "day_kst, sent) VALUES (?,?,?,?,?,?)",
                 ("SOL", "touch", "2", now - 2 * DAY, "2027-01-14", 0))
    conn.execute("INSERT INTO alerts_log (coin_symbol, kind, level_ids, sent_at, "
                 "day_kst, sent) VALUES (?,?,?,?,?,?)",
                 ("SOL", "preview", "3", now - 2 * DAY, "2027-01-14", 1))
    win = db.get_resolved_rows_between(conn, now - 7 * DAY, now)
    check("I2a get_resolved_rows_between 창 필터(7일 내 1건, 10일 전 건 제외)",
          len(win) == 1 and win[0]["author"] == "GoodAuthor")
    check("I2b 섀도 터치·미종결은 창 조회에서도 제외",
          all(r["outcome"] and r["touched_at"] for r in
              db.get_resolved_rows_between(conn, 0, now + DAY)))
    check("I2c count_touch_alerts_between = kind='touch' AND sent=1 만",
          db.count_touch_alerts_between(conn, now - 7 * DAY, now) == 1)
    check("I2d count_resolved_touches_since (touched_at>=since AND outcome NOT NULL)",
          db.count_resolved_touches_since(conn, now - DAY) == 5
          and db.count_resolved_touches_since(conn, now + DAY) == 0)
os.remove(TEST_DB)

# ── V: v2 본문 — ② 이번 주 한눈에 (지난주 대비 화살표) ─────────────────
CUR = weekly.summary([lv("hit", r=1.0)] * 6 + [lv("miss", r=-1.0)] * 6, alerts=10)
PRV = weekly.summary([lv("hit", r=1.0)] * 3 + [lv("miss", r=-1.0)] * 9, alerts=14)
msg_v = telegram.render_weekly_report(rows_by_author, now=now, current=CUR,
                                      previous=PRV, **RK)
print(msg_v)
print()

check("V1 헤더 표본 = 알림/종결", "📦 표본: 알림 10건 · 종결 12건" in msg_v)
check("V2 알림 수 감소 ▼ / 종결 수 동일 →",
      "알림 수  10건 (14건 → ▼)" in msg_v and "종결 수  12건 (12건 → →)" in msg_v)
check("V3 승률 상승 ▲ (50% vs 25%)", "승률    50% (25% → ▲)" in msg_v)
check("V4 PF·평균 R 도 지난주 대비", "PF      1.00 (0.33 → ▲)" in msg_v
      and "평균 R  +0.00 (-0.50 → ▲)" in msg_v)
check("V5 R 표본 수 명시", "R 산출 가능 표본 12건 기준" in msg_v)

# V6: 표본 n<10 이면 화살표 생략 + (n=…, 참고)
CUR_THIN = weekly.summary([lv("hit", r=1.0)] * 3 + [lv("miss", r=-1.0)] * 2, alerts=2)
msg_thin = telegram.render_weekly_report(rows_by_author, now=now, current=CUR_THIN,
                                         previous=PRV, min_n=10, **RK)
check("V6 소표본 지표는 화살표 생략 + '참고' 표기",
      "승률    60% (n=5, 참고)" in msg_thin and "PF      1.50 (n=5, 참고)" in msg_thin)
check("V6b 건수(알림/종결)는 표본 규칙과 무관하게 화살표 유지",
      "알림 수  2건 (14건 → ▼)" in msg_thin and "종결 수  5건 (12건 → ▼)" in msg_thin)

# V7: 지난주 데이터가 없으면 '지난주 없음'
msg_nop = telegram.render_weekly_report(rows_by_author, now=now, current=CUR,
                                        previous=None, **RK)
check("V7 지난주 미주입 → '지난주 없음'", "알림 수  10건 (지난주 없음)" in msg_nop)

# ── O: ③ 관찰 3줄 (선택 규칙) ─────────────────────────────────────────
# (c) 거래대금 순위: 1-20위 20건 중 4승(20%) vs 100위 밖 20건 중 16승(80%) → 격차 60%p
POOL_RANK = ([lv("hit", vrank=5)] * 4 + [lv("miss", vrank=5)] * 16
             + [lv("hit", vrank=150)] * 16 + [lv("miss", vrank=150)] * 4)
# (b) 지연: <30분 12건 중 6승(50%) vs 30분+ 12건 중 6승(50%) → 격차 0%p(후보는 되나 꼴찌)
POOL_DELAY = ([lv("hit", collected=now - 2 * DAY - 60)] * 6
              + [lv("miss", collected=now - 2 * DAY - 60)] * 6
              + [lv("hit", collected=now - 2 * DAY - 7200)] * 6
              + [lv("miss", collected=now - 2 * DAY - 7200)] * 6)
obs = weekly.observations([], [], POOL_RANK + POOL_DELAY)
check("O1 격차 큰 순으로 선택 — 거래대금(60%p)이 지연(0%p)보다 앞",
      obs and obs[0]["kind"] == "volume_rank" and round(obs[0]["gap"]) == 60)
check("O2 최대 3개", len(weekly.observations([], [], POOL_RANK + POOL_DELAY)) <= 3)

msg_o = telegram.render_weekly_report(rows_by_author, now=now, current=CUR,
                                      observations=obs, pool_n=len(POOL_RANK), **RK)
check("O3 관찰 줄에 n 병기 + 격차 표기",
      "거래대금 1-20위 승률 20%(n=20)" in msg_o and "100위 밖 80%(n=20)" in msg_o
      and "격차 60%p" in msg_o)

# O4: 한쪽 그룹 n<10 → 후보 자체가 안 만들어진다(침묵)
POOL_SMALL = [lv("hit", vrank=5)] * 4 + [lv("miss", vrank=150)] * 20
check("O4 한쪽 n<10 이면 후보 제외",
      not any(c["kind"] == "volume_rank"
              for c in weekly.observations([], [], POOL_SMALL)))
msg_o4 = telegram.render_weekly_report(rows_by_author, now=now, current=CUR,
                                       observations=[], pool_n=24, **RK)
check("O4b 후보 0 → '특이 관찰 없음(표본 n)'", "특이 관찰 없음 (표본 n=24)" in msg_o4)

# O5: (a) 판정 사유 구성 변화 — ±10%p 미만이면 후보 아님
MIX_CUR = [lv("hit")] * 5 + [lv("miss")] * 5
MIX_PRV_SAME = [lv("hit")] * 5 + [lv("miss")] * 5
MIX_PRV_DIFF = [lv("hit")] * 9 + [lv("miss")] * 1
check("O5 구성 변화 10%p 미만이면 침묵",
      not any(c["kind"] == "outcome_mix"
              for c in weekly.observations(MIX_CUR, MIX_PRV_SAME, [])))
check("O5b 40%p 변화면 후보 채택(양쪽 n≥10)",
      any(c["kind"] == "outcome_mix"
          for c in weekly.observations(MIX_CUR, MIX_PRV_DIFF, [])))
check("O5c 지난주 n<10 이면 비교 불가 → 침묵",
      not any(c["kind"] == "outcome_mix"
              for c in weekly.observations(MIX_CUR, MIX_PRV_DIFF[:5], [])))

# O6: (d) 작성자 극단 — 종결 n≥5 자격자 2명 이상일 때만
AUTH_POOL = ([lv("hit", author="alice")] * 5 + [lv("miss", author="bob")] * 5
             + [lv("hit", author="solo")] * 2)
a_obs = [c for c in weekly.observations(AUTH_POOL, [], []) if c["kind"] == "authors"]
check("O6 작성자 최고/최저(n≥5 자격자만, 격차 100%p)",
      a_obs and a_obs[0]["top"] == "alice" and a_obs[0]["low"] == "bob"
      and round(a_obs[0]["gap"]) == 100)
check("O6b 자격자 1명이면 후보 아님",
      not any(c["kind"] == "authors"
              for c in weekly.observations([lv("hit", author="alice")] * 5, [], [])))

# O7: (e) 보유시간 이상치 — 상대편차 50% 미만이면 침묵
HOLD_FLAT = ([lv("hit", resolved=now - 2 * DAY + 24 * 3600)] * 10
             + [lv("miss", resolved=now - 2 * DAY + 26 * 3600)] * 10)
HOLD_OUT = ([lv("hit", resolved=now - 2 * DAY + 10 * 3600)] * 10
            + [lv("miss", resolved=now - 2 * DAY + 160 * 3600)] * 10)
check("O7 편차 작으면 후보 아님",
      not any(c["kind"] == "hold_outlier"
              for c in weekly.observations([], [], HOLD_FLAT)))
check("O7b 큰 이상치는 후보 채택",
      any(c["kind"] == "hold_outlier"
          for c in weekly.observations([], [], HOLD_OUT)))

# ── M: ④ 다음 판단 (마일스톤) ─────────────────────────────────────────
MS = [dict(weekly.milestone(42, 150, per_day=3.0), label="v6 지연감점 평가"),
      dict(weekly.milestone(50, 50, per_day=2.0), label="MFE/MAE e-ratio")]
msg_m = telegram.render_weekly_report(rows_by_author, now=now, current=CUR,
                                      milestones=MS, **RK)
check("M1 진행률 + 최근 30일 속도 기반 예상일",
      "v6 지연감점 평가: 42/150건 (예상 36일)" in msg_m)
check("M2 도달 완료는 ✅", "MFE/MAE e-ratio: 50/50건 (도달 ✅)" in msg_m)
check("M3 카운트 정의를 본문에 명시(단순 정의)",
      "종결 = 기준시각 이후 터치 + outcome 기록" in msg_m)
check("M4 속도 0 이면 예상일 생략",
      weekly.milestone(10, 150, per_day=0)["eta_days"] is None)

# ── S: ⑤ 판정 사유별 집계 ─────────────────────────────────────────────
STAT_ROWS = ([lv("hit", resolve=1100.0)] * 10                      # +10%
             + [lv("miss", resolve=950.0)] * 6                     # -5%
             + [lv("timeboxed_loss", resolve=2000.0)] * 2          # +100% → 이상치 제외
             + [lv("hit", direction="short", resolve=900.0)] * 2)  # 숏 -10% → +10%
ST = weekly.outcome_stats(STAT_ROWS)
msg_s = telegram.render_weekly_report(rows_by_author, now=now, current=CUR,
                                      outcome_stats=ST, pool_days=28, **RK)
check("S1 헤더(최근 28일, 종결 n)", "📋 <b>판정 사유별</b> (최근 28일, 종결 20건)" in msg_s)
check("S2 사유별 n·비중·평균 보유·평균 실현%",
      "적중(TP) 12건(60%) · 보유 24h · 실현 +10.0%" in msg_s)
check("S3 |50%| 초과 이상치는 실현% 표본에서 제외(행은 남음)",
      "만료·손실 2건(10%) · 보유 24h · 실현 -" in msg_s)
check("S4 숏은 부호 반전(−10% 가격 → +10% 실현)",
      round(weekly.realized_pct(lv("hit", direction="short", resolve=900.0)), 6) == 10.0)
check("S5 표본 0 사유는 행 자체 생략", "만료·수익" not in msg_s)
check("S6 미주입이면 섹션 생략",
      "판정 사유별" not in telegram.render_weekly_report(rows_by_author, now=now, **RK))

# ── C: ⑦ 등급 캘리브레이션 (1줄/등급 압축, 최신 grade_ver 만) ──────────
from analytics import calibration  # noqa: E402

CAL_ROWS = ([("S", "miss", 0)] * 4 + [("A", "miss", 0)] * 4
            + [("C", "hit", 0)] * 5 + [("C", "miss", 0)] * 7 + [("D", "hit", 0)] * 6)
CAL = calibration.calibrate_grades(CAL_ROWS)
msg_c = telegram.render_weekly_report(rows_by_author, now=now,
                                      calibration_result=CAL, calibration_ver="v6", **RK)
check("C1 헤더에 산식 버전 + 종결 표본",
      "🎚️ <b>등급 캘리브레이션</b> (v6 표본, TP1 도달률, 종결 26건)" in msg_c)
check("C2 1줄/등급 (등급·도달률·CI)",
      "S  0% (0/4)  CI 0%~49%" in msg_c and "C  42% (5/12)  CI 19%~68%" in msg_c)
check("C3 표본 없는 등급(B)은 행 생략", "\n  B  " not in msg_c)
check("C4 소표본 등급 ⚠️ 표기 (S·A 2건)", msg_c.count("⚠️n&lt;5") == 2)
check("C5 단조성 위반은 1줄 요약", "단조성 위반 1건" in msg_c
      and "D 100% &gt; C 42%, CI 겹침" in msg_c and "표기 전용" in msg_c)
check("C6 HTML 안전 — 날 '<'/'>' 없음", "n<5" not in msg_c and "% > " not in msg_c)
check("C7 구 산식 병기 제거(legacy 주입해도 출력 없음)",
      "구 산식" not in telegram.render_weekly_report(
          rows_by_author, now=now, calibration_result=CAL,
          calibration_legacy=calibration.calibrate_grades([("S", "hit", 0)] * 9), **RK))

CAL_OK = calibration.calibrate_grades(
    [("S", "hit", 0)] * 9 + [("S", "miss", 0)]
    + [("C", "hit", 0)] * 5 + [("C", "miss", 0)] * 5
    + [("D", "hit", 0)] + [("D", "miss", 0)] * 9)
check("C8 단조 유지 시 ✅ 한 줄",
      "✅ 단조성 유지" in telegram.render_weekly_report(
          rows_by_author, now=now, calibration_result=CAL_OK, **RK))
CAL_THIN = calibration.calibrate_grades([("C", "hit", 0)] * 3 + [("D", "miss", 0)] * 2)
check("C9 판정 보류 문구",
      "단조성 판정 보류" in telegram.render_weekly_report(
          rows_by_author, now=now, calibration_result=CAL_THIN, **RK))
check("C10 표본 0 이면 섹션 통째 생략",
      "등급 캘리브레이션" not in telegram.render_weekly_report(
          rows_by_author, now=now,
          calibration_result=calibration.calibrate_grades([]), **RK))
check("C11 작성자 표본 0 에서도 등급 축은 독립적으로 표시",
      "🎚️" in telegram.render_weekly_report({}, now=now, calibration_result=CAL, **RK))

# ── RC: 역신호 확정 구분 (S9, 표시 전용) ──────────────────────────────
msg_rc = telegram.render_weekly_report(rows_by_author, now=now,
                                       reverse_confirmed={"BadAuthor"}, **RK)
check("RC1 역신호 확정 한 줄(필터 무변경 명시 + HTML 안전 &lt;)",
      "🔻 역신호 확정 1명" in msg_rc and "@BadAuthor" in msg_rc
      and "알림 필터 무변경" in msg_rc and "E_LB&lt;0" in msg_rc)
check("RC2 미주입 시 확정 줄 없음", "역신호 확정" not in msg)
check("RC3 작성자 표본 0 에서도 확정 줄은 유지(유일 노출 지점)",
      "역신호 확정 1명" in telegram.render_weekly_report(
          {}, now=now, reverse_confirmed={"BadAuthor"}, **RK))

# ── X: 제거된 섹션은 출력에 없다 (하위호환 인자는 받되 무시) ────────────
from analytics import distribution  # noqa: E402

R_ROWS = [(-0.5, "C"), (0.3, "C"), (1.5, "B"), (2.7, "A"), (4.0, "S")]
msg_x = telegram.render_weekly_report(
    rows_by_author, now=now,
    baseline={"n": 29, "positive": 3, "rate": 3 / 29},
    raw_records={"GoodAuthor": {"wins": 3, "losses": 7}}, baseline_min_n=20,
    confluence={"GoodAuthor": {"multi": 1, "total": 7, "cr": 1 / 7}},
    confluence_min_clusters=2,
    r_distribution=distribution.r_multiple_distribution(R_ROWS),
    r_distribution_by_grade=distribution.r_distribution_by_grade(R_ROWS),
    holding_period=distribution.holding_period_distribution(
        [dict(touched_at=0, resolved_at=10 * 3600, outcome="hit")]),
    regime_heatmap={"cells": {("S", "trend"): {"n": 8, "hit": 0.75, "mfe": 12.0}}},
    **RK)
check("X1 🎲 초과 적중률 제거", "초과 적중률" not in msg_x)
check("X2 🤝 합의 줄 제거", "🤝" not in msg_x)
check("X3 📊 R-멀티플 분포 제거", "R-멀티플 분포" not in msg_x)
check("X4 ⏱️ 보유기간 분포 제거", "보유기간 분포" not in msg_x)
check("X5 🌡️ 등급×장세 히트맵 제거", "히트맵" not in msg_x and "🌡️" not in msg_x)
check("X6 구 인자를 넘겨도 예외 없이 기존 렌더와 동일", msg_x == msg)

# ── L: 길이 예산 ──────────────────────────────────────────────────────
BIG = {f"Author{i:03d}": rows_of("hit", 1.0 + i * 0.01, 6) for i in range(120)}
msg_big = telegram.render_weekly_report(BIG, now=now, current=CUR, previous=PRV,
                                        observations=obs, pool_n=40,
                                        outcome_stats=ST, calibration_result=CAL,
                                        calibration_ver="v6", milestones=MS,
                                        top_authors=200, max_chars=4000, **RK)
check("L1 4,000자 초과분은 절단", len(msg_big) <= 4000)
check("L2 절단 사실을 한 줄로 알림", "길이 제한으로 이하 생략" in msg_big)
check("L3 절단은 줄 경계에서만(태그 중간 절단 금지)",
      msg_big.count("<b>") == msg_big.count("</b>"))
msg_fit = telegram.render_weekly_report(rows_by_author, now=now, max_chars=4000, **RK)
check("L4 예산 이내면 그대로", "길이 제한" not in msg_fit)
check("L5 운영 설정 기본값(3,500자) 이내 — 표준 구성",
      len(telegram.render_weekly_report(
          rows_by_author, now=now, current=CUR, previous=PRV, observations=obs,
          pool_n=40, outcome_stats=ST, calibration_result=CAL,
          calibration_ver="v6", milestones=MS, **RK)) <= 3500)

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
      and {lv_path.name, ds_path.name} <= set(res["files"]))

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
print(f"{'전체 통과' if ok else '실패 있음'} ({n_checks}개 체크)")
sys.exit(0 if ok else 1)
