# scripts/health_report.py (주간 건강표, CTO 내부용) 테스트.
#
# 임시 DB(db.init_db)에 행을 직접 심고 collect/render 결과를 본다. 네트워크 불필요 —
# CI 조회는 GITHUB_TOKEN 이 없으면 호출 자체를 생략하는 경로만 검증한다.
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from scripts import health_report as hr
from storage import audit_dump, db

# init_db 의 주간 감사 덤프 훅은 이 테스트와 무관 — 부수효과 차단(test_weekly_report 관례).
audit_dump.SUPPRESSED = True

ok = True
n_checks = 0


def check(name, cond):
    global ok, n_checks
    n_checks += 1
    print(("✅" if cond else "❌"), name)
    ok = ok and cond


NOW = 1_790_600_000.0          # 2026-09-28 경 (LEGACY_CUTOFF 이후)
DAY = 86400.0
_seq = [0]


def fresh_db():
    p = tempfile.NamedTemporaryFile(delete=False, suffix=".db").name
    db.init_db(p)
    return p


def add_level(conn, **kw):
    """정상 실터치 롱 1건 기본값 + 덮어쓰기."""
    _seq[0] += 1
    t = kw.pop("touched_at", NOW - 2 * DAY)
    row = dict(signal_key=f"k{_seq[0]}", coin_symbol="BTC", ticker="KRW-BTC",
               direction="long", entry_usd=100.0, sl_usd=90.0, tp_usd=120.0,
               status="touched", collected_at=(t - 3600) if t else NOW - 3 * DAY,
               touched_at=t, touch_price_krw=140000.0)
    row.update(kw)
    cols = ",".join(row)
    conn.execute(f"INSERT INTO levels ({cols}) VALUES ({','.join('?' * len(row))})",
                 tuple(row.values()))
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def add_alert(conn, kind, ids, sent_at, sent=1):
    db.record_alert(conn, "BTC", kind, ids, "2026-09-27", sent_at, sent=sent)


def run(path, now=NOW):
    conn = hr.open_ro(path)
    try:
        return hr.collect(conn, now)
    finally:
        conn.close()


def inv(m, prefix):
    return next(c for n, c in m["invariants"] if n.startswith(prefix))


# ── A. 깨끗한 DB: 불변식 0, 경고 없음 ─────────────────────────────────────────
p = fresh_db()
with db.connect(p) as c:
    a = add_level(c)
    add_alert(c, "touch", [a], NOW - 2 * DAY)
m = run(p)
check("A1 깨끗한 DB → 불변식 전부 0", all(v == 0 for _, v in m["invariants"]))
check("A2 불변식은 10개 이하", len(hr.INVARIANTS) <= 10)
md = hr.render(m, ("CI: 로컬 실행 — 확인 생략", False))
check("A3 경고 없음 표시", "✅ 경고 없음" in md and "⚠️ 경고" not in md)
check("A4 방향 기본값은 측정 불가로 정직하게 표기", "측정 불가 — 기록 없음" in md)

# 읽기 전용 연결은 쓰기를 거부해야 한다
_ro = hr.open_ro(p)
try:
    _ro.execute("INSERT INTO meta (key, value) VALUES ('x','y')")
    _wrote = True
except sqlite3.OperationalError:
    _wrote = False
finally:
    _ro.close()
check("A5 open_ro 는 쓰기 거부(mode=ro)", not _wrote)

# ── B. 불변식별 위반 시드 → 각각 검출 ─────────────────────────────────────────
p = fresh_db()
with db.connect(p) as c:
    add_level(c, touched_at=None, touch_price_krw=5.0)                      # 섀도+가격
    add_level(c, touch_price_krw=None)                                       # 실터치 가격 없음
    add_level(c, outcome="hit", resolved_at=None, best_tp_hit=1)             # outcome↔resolved
    add_level(c, tp_usd=95.0)                                                # 롱 TP≤진입
    add_level(c, direction="short", tp_usd=110.0, sl_usd=120.0)              # 숏 TP≥진입
    add_level(c, outcome="hit", resolved_at=NOW, best_tp_hit=None)           # hit best NULL
    add_level(c, touched_at=NOW - 2 * DAY, collected_at=NOW - 1 * DAY)       # 터치<수집
    add_level(c, armed=1, armed_at=None)                                     # 무장 시각 없음
    x = add_level(c, status="watching", touched_at=None, touch_price_krw=None)
    add_alert(c, "touch", [x], NOW - DAY)                                    # 비터치 레벨 발송
    y = add_level(c)
    add_alert(c, "touch", [y], NOW - DAY)
    add_alert(c, "touch", [y], NOW - DAY + 60)                               # 중복 발송
    # 정상 예외: 섀도 형제가 shadow_touch 로 만료 — 위반 아님
    z = add_level(c, status="expired", touched_at=None, touch_price_krw=None,
                  expired_reason="shadow_touch")
    add_alert(c, "touch", [z], NOW - DAY)
    # 정상 예외: miss 인데 TP 미도달(best_tp_hit NULL) — 위반 아님
    add_level(c, outcome="miss", resolved_at=NOW, best_tp_hit=None)
    # 정상 예외: 섀도 터치(가격·판정 없음)
    add_level(c, touched_at=None, touch_price_krw=None)
    # 레거시(07-27 이전 수집) 가격 없음 — 제외
    add_level(c, touched_at=hr.LEGACY_CUTOFF - DAY, touch_price_krw=None)
m = run(p)
check("B1 섀도 터치에 가격 → 검출", inv(m, "섀도") == 1)
check("B2 실터치 가격 없음 → 검출(레거시 제외)", inv(m, "실터치인데") == 1)
check("B3 outcome↔resolved_at 불일치 → 검출", inv(m, "판정 상태") == 1)
check("B4 롱 기하 위반 → 검출", inv(m, "롱 기하") == 1)
check("B5 숏 기하 위반 → 검출", inv(m, "숏 기하") == 1)
check("B6 hit 인데 best_tp_hit NULL → 검출 (miss NULL 은 정상)", inv(m, "best_tp_hit") == 1)
check("B7 touched_at < collected_at → 검출", inv(m, "touched_at <") == 1)
check("B8 armed=1 & armed_at NULL → 검출", inv(m, "무장 불일치") == 1)
check("B9 비터치 레벨 발송 → 검출 (shadow_touch 만료는 정상)", inv(m, "발송 터치알림") == 1)
check("B10 같은 레벨 터치알림 중복 → 검출", inv(m, "같은 레벨") == 1)
md = hr.render(m)
check("B11 불변식 행 ⚠️ + 10/10 항목", "| 6. 불변식 위반 | 10/10 항목" in md and "⚠️" in md)

# 무장 전 터치 (touched_at < armed_at) → 2b 지표 + 불변식
p = fresh_db()
with db.connect(p) as c:
    add_level(c, armed=1, armed_at=NOW - 1 * DAY, touched_at=NOW - 2 * DAY,
              collected_at=NOW - 3 * DAY)
m = run(p)
check("B12 무장 전 터치 → 2b 1건 + 불변식 검출",
      m["before_armed"][0] == 1 and inv(m, "무장 불일치") == 1)
check("B13 무장 전 터치 행 ⚠️", "| 2b. 무장 전 터치 | 1 |" in hr.render(m)
      and "2b. 무장 전 터치" in hr.render(m).split("⚠️ 경고:")[-1])

# ── C. 오염형 터치 수 + 10분 내 터치율 + 임계 ⚠️ ─────────────────────────────
p = fresh_db()
with db.connect(p) as c:
    t = NOW - 2 * DAY
    for _ in range(hr.THRESHOLDS["stale_touch_max"] + 1):              # 즉시(<600s) & 관통>10%
        add_level(c, touched_at=t, collected_at=t - 120, touch_penetration_pct=15.0)
    add_level(c, touched_at=t, collected_at=t - 120, touch_penetration_pct=None,
              touch_stale=1)                                           # 백필 플래그
    add_level(c, touched_at=t, collected_at=t - 7200, touch_penetration_pct=20.0)  # 깊지만 즉시 아님
    add_level(c, touched_at=t - 8 * DAY, collected_at=t - 8 * DAY - 60,
              touch_penetration_pct=12.0)                              # 지난 주 1건
m = run(p)
n_stale = hr.THRESHOLDS["stale_touch_max"] + 2
check("C1 오염형 이번 주 = 즉시·깊은 관통 + touch_stale(깊기만 한 건 제외)",
      m["stale"][0] == n_stale)
check("C2 오염형 지난 주 1건", m["stale"][1] == 1)
check("C3 10분 내 터치율 = 이번 주 (n-1)/n",
      m["instant_n"][0] == n_stale and abs(m["instant_rate"][0] - 100.0 * n_stale / (n_stale + 1)) < 1e-9)
md = hr.render(m)
check("C4 오염형 임계 초과 → ⚠️", f"| 1. 오염형 터치 수 | {n_stale} |" in md
      and "1. 오염형 터치 수" in md.split("⚠️ 경고:")[-1])
check("C5 10분 내 터치율 임계 초과 → ⚠️", "2. 10분 내 터치율" in md.split("⚠️ 경고:")[-1])

# 임계 이하면 ⚠️ 없음
p = fresh_db()
with db.connect(p) as c:
    add_level(c, touched_at=NOW - DAY, collected_at=NOW - DAY - 60, touch_penetration_pct=15.0)
    for _ in range(3):
        add_level(c, touched_at=NOW - DAY, collected_at=NOW - 2 * DAY)
m = run(p)
md = hr.render(m)
check("C6 임계 이하(오염 1·즉시 25%) → 해당 행 ✅",
      "| 1. 오염형 터치 수 | 1 | 0 |" in md and "✅ 경고 없음" in md)

# ── D. 억제 사유 그룹핑 ─────────────────────────────────────────────────────
p = fresh_db()
with db.connect(p) as c:
    for _ in range(12):
        add_alert(c, "touch_no_tp", [1], NOW - DAY, sent=0)
    for _ in range(3):
        add_alert(c, "touch_no_tp", [1], NOW - 8 * DAY, sent=0)
    for _ in range(4):
        add_alert(c, "touch_deep", [2], NOW - DAY, sent=0)
    add_alert(c, "touch_warning", [3], NOW - 9 * DAY, sent=0)
    add_alert(c, "touch", [4], NOW - DAY, sent=1)                    # 발송분은 집계 제외
    add_alert(c, "news", [], NOW - 20 * DAY, sent=0)                 # 창 밖
m = run(p)
s = m["suppress"]
check("D1 사유별 [이번 주, 지난 주]",
      s.get("touch_no_tp") == [12, 3] and s.get("touch_deep") == [4, 0]
      and s.get("touch_warning") == [0, 1])
check("D2 발송(sent=1)·창 밖 행은 제외", "touch" not in s and "news" not in s)
md = hr.render(m)
tail = md.split("⚠️ 경고:")[-1]
check("D3 12 ≥ 2×3 & ≥10 → touch_no_tp ⚠️, touch_deep(4) 은 최소건수 미달 ✅",
      "`touch_no_tp`" in tail and "`touch_deep`" not in tail)

# ── E. 채움률 ────────────────────────────────────────────────────────────────
p = fresh_db()
with db.connect(p) as c:
    for i in range(20):
        add_level(c, touched_at=NOW - DAY,
                  touch_funding_rate=(0.01 if i < 4 else None),           # 20% < 35% 하한
                  touch_fear_greed=50.0,                                   # 100%
                  touch_long_short_ratio=(1.1 if i < 10 else None))        # OKX 시작 이후 50%
    add_level(c, touched_at=NOW - 30 * DAY, touch_funding_rate=0.01)    # 도입(첫 기록) 앵커
m = run(p, now=NOW)
f = {r["col"]: r for r in m["fills"]}
check("E1 펀딩 채움률 20% (n=20)", f["touch_funding_rate"]["den"][0] == 20
      and abs(f["touch_funding_rate"]["rate"][0] - 20.0) < 1e-9)
check("E2 공포탐욕 100%", f["touch_fear_greed"]["rate"][0] == 100.0)
check("E3 기록 전무 컬럼은 since=None(기록 없음)", f["touch_dex_buy_ratio"]["since"] is None)
check("E4 OKX 명시 시작 이후 분모만 — 50%",
      NOW - DAY >= hr._OKX_V3 and abs(f["touch_long_short_ratio"]["rate"][0] - 50.0) < 1e-9)
md = hr.render(m)
tail = md.split("⚠️ 경고:")[-1]
check("E5 하한 미달(펀딩 20%<35%) → ⚠️, 충족(공포탐욕) → 경고 목록에 없음",
      "채움률 펀딩비" in tail and "공포탐욕" not in tail)

# 소표본 게이트: 분모 < fill_min_denominator 이면 경고 안 함
p = fresh_db()
with db.connect(p) as c:
    add_level(c, touched_at=NOW - 30 * DAY, touch_funding_rate=0.01)
    for _ in range(hr.THRESHOLDS["fill_min_denominator"] - 1):
        add_level(c, touched_at=NOW - DAY, touch_fear_greed=50.0, touch_kimchi_pct=1.0)
md = hr.render(run(p))
check("E6 분모 < 최소표본 → 채움률 0% 여도 ✅", "채움률 펀딩비" not in md.split("⚠️ 경고:")[-1]
      or "⚠️ 경고:" not in md)

# ── F. CI 경로: 토큰 없으면 네트워크 없이 생략 ───────────────────────────────
_saved = {k: os.environ.pop(k, None) for k in ("GITHUB_TOKEN", "GITHUB_REPOSITORY")}
try:
    txt, bad = hr.ci_status()
    check("F1 토큰 없음 → '로컬 실행 — 확인 생략', 경고 아님", txt == "CI: 로컬 실행 — 확인 생략" and not bad)
finally:
    for k, v in _saved.items():
        if v is not None:
            os.environ[k] = v

# ── G. main: --out 파일 + GITHUB_STEP_SUMMARY 덧붙이기 ─────────────────────
p = fresh_db()
with db.connect(p) as c:
    add_level(c)
td = tempfile.mkdtemp()
out_md = os.path.join(td, "h.md")
summ = os.path.join(td, "summary.md")
Path(summ).write_text("기존\n", encoding="utf-8")
_prev = os.environ.get("GITHUB_STEP_SUMMARY")
os.environ["GITHUB_STEP_SUMMARY"] = summ
try:
    rc = hr.main(["--db", p, "--out", out_md, "--now", str(NOW), "--no-ci"])
finally:
    if _prev is None:
        os.environ.pop("GITHUB_STEP_SUMMARY", None)
    else:
        os.environ["GITHUB_STEP_SUMMARY"] = _prev
check("G1 main exit 0", rc == 0)
check("G2 --out 파일 작성", Path(out_md).read_text(encoding="utf-8").startswith("## 🩺 주간 건강표"))
_s = Path(summ).read_text(encoding="utf-8")
check("G3 step summary 에 덧붙임(기존 내용 보존)", _s.startswith("기존\n") and "주간 건강표" in _s)
check("G4 없는 DB → exit 2 (새 파일 생성 안 함)",
      hr.main(["--db", os.path.join(td, "none.db"), "--no-ci"]) == 2
      and not Path(td, "none.db").exists())

print(f"\n{'전부 통과' if ok else '실패 있음'} ({n_checks}건)")
sys.exit(0 if ok else 1)
