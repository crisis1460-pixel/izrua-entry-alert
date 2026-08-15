# scripts/validate_ic.py(정제 시간분할 IC) + storage/audit_dump 작성자 삭제율 테스트.
# 오프라인 전용 — 임시 DB(cache/)만 사용, 운영 DB(data/levels.db)는 건드리지 않는다.
import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from scripts import validate_ic
from storage import audit_dump, db

# init_db 의 주간 감사 자동 훅 차단 (test_weekly_report.py 와 동일 관례) —
# 아래에서 run_weekly_audit 을 명시적으로 직접 부른다.
audit_dump.SUPPRESSED = True

ok = True


def check(name, cond):
    global ok
    print(("✅" if cond else "❌"), name)
    ok = ok and cond


ROOT = Path(__file__).resolve().parent.parent
TEST_DB = ROOT / "cache" / "_test_validate_ic.db"
SMALL_DB = ROOT / "cache" / "_test_validate_ic_small.db"
OUT_DIR = ROOT / "cache" / "_test_validate_ic_audit"

for p in (TEST_DB, SMALL_DB):
    for suf in ("", "-wal", "-shm"):
        Path(str(p) + suf).unlink(missing_ok=True)
if OUT_DIR.is_dir():
    for f in OUT_DIR.iterdir():
        f.unlink()

# ── 합성 데이터 설계 ────────────────────────────────────────────────
# 종결 40행, 2.5일 간격 = 약 3개월. 3폴드 시간순 분할 → 크기 [14, 13, 13].
# 폴드1·2(idx 0~26): ret = 0.1*i (score 와 완전 단조) → 폴드 IC ≈ +1.0
# 폴드3(idx 27~39): ret 교대 부호 패턴 → Spearman ≈ -0.12 (수계산, 0 근방)
# touch_cvd_ratio: 모든 폴드에서 ret 과 완전 단조 → --feature 검증용 (전 폴드 ≈ +1.0)
T0 = 1_750_000_000.0
STEP = 2.5 * 86400.0
N = 40
FOLD3_RETS = [1.0, -1.0, 0.8, -0.8, 0.6, -0.6, 0.4, -0.4,
              0.2, -0.2, 0.05, -0.05, 0.0]   # idx 27~39 (13개)

db.init_db(str(TEST_DB))
with db.connect(str(TEST_DB)) as conn:
    for i in range(N):
        t = T0 + i * STEP
        score = 50.0 + i
        ret = (0.1 * i) if i < 27 else FOLD3_RETS[i - 27]
        conn.execute(
            """INSERT INTO levels (signal_key, coin_symbol, ticker, direction,
                 status, collected_at, touched_at, outcome, score, ret_24h,
                 touch_cvd_ratio)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (f"ic{i:03d}", "TST", "KRW-TST", "long", "touched",
             t - 3600.0, t, "hit" if i % 2 == 0 else "miss",
             score, ret, ret * 0.01))

    # ── 작성자 삭제율 표본 (item 2) ──────────────────────────────
    # Gamer: 글 10개(그중 2개 글은 레벨 2개씩 = 12행)·삭제 글 2개 → rate 0.2
    # Clean: 글 6개·삭제 0 → rate 0.0 / Small: 글 4개(전부 삭제) → <5 제외
    def ins_author(key, author, url, deleted, checked):
        conn.execute(
            """INSERT INTO levels (signal_key, coin_symbol, ticker, direction,
                 status, collected_at, author, post_url, deleted, deleted_checked_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (key, "TST", "KRW-TST", "long", "expired", T0, author, url,
             1 if deleted else 0, T0 if checked else None))

    for j in range(10):
        d = j < 2                        # p0, p1 삭제
        ins_author(f"g{j}", "Gamer", f"https://tv/x/gamer/{j}", d, j < 6)
    ins_author("gdup0", "Gamer", "https://tv/x/gamer/0", True, True)    # p0 중복 레벨
    ins_author("gdup5", "Gamer", "https://tv/x/gamer/5", False, False)  # p5 중복 레벨
    for j in range(6):
        ins_author(f"c{j}", "Clean", f"https://tv/x/clean/{j}", False, j < 3)
    for j in range(4):
        ins_author(f"s{j}", "Small", f"https://tv/x/small/{j}", True, True)

db.init_db(str(SMALL_DB))  # 표본 0건 DB — 부족 메시지 검증용

# ── V1: 폴드 분할·purge 기하학 ───────────────────────────────────────
conn = validate_ic.connect_ro(TEST_DB)
rows = validate_ic.load_rows(conn, feature="score")

check("V1 종결 IC 표본 40행 로드 (작성자 표본은 outcome NULL 이라 제외)",
      len(rows) == N)
check("V1 touched_at 오름차순", all(rows[i]["t"] <= rows[i + 1]["t"]
                                    for i in range(len(rows) - 1)))

folds = validate_ic.purged_time_split(rows)
check("V2 폴드 3개, 크기 [14,13,13]",
      [len(f["test"]) for f in folds] == [14, 13, 13])
for f in folds:
    check(f"V2 폴드{f['fold']} purge 실동작: 훈련 {len(f['train'])}행 < "
          f"비테스트 {N - len(f['test'])}행",
          len(f["train"]) < N - len(f["test"]))
# purge 창 검증: 훈련 행 중 테스트 구간 ±168h(+embargo 168h) 안 행이 없어야 한다
W, E = 168 * 3600.0, 168 * 3600.0
leak = any(f["t_start"] - W <= r["t"] <= f["t_end"] + W + E
           for f in folds for r in f["train"])
check("V2 훈련측에 purge/embargo 구간 잔류 행 없음", not leak)

# ── V3: 폴드별 IC — 폴드1·2 강한 양(+), 폴드3 0 근방 ────────────────
res = validate_ic.evaluate_folds(rows)
ics = {f["fold"]: f["ic"] for f in res["folds"]}
check(f"V3 폴드1 IC 강한 양의 상관 ({ics[1]:+.3f} > 0.9)", ics[1] > 0.9)
check(f"V3 폴드2 IC 강한 양의 상관 ({ics[2]:+.3f} > 0.9)", ics[2] > 0.9)
check(f"V3 폴드3 IC 0 근방 ({ics[3]:+.3f}, |IC| < 0.35)", abs(ics[3]) < 0.35)
check("V3 폴드1·2 유의, 폴드3 비유의 (|IC|>=2/sqrt(n) 휴리스틱)",
      res["folds"][0]["significant"] and res["folds"][1]["significant"]
      and not res["folds"][2]["significant"])
check("V3 요약 지표: 인샘플 IC 계산됨 + 평균 테스트 IC = 폴드 IC 산술평균",
      res["insample_ic"] is not None and res["mean_test_ic"] is not None
      and abs(res["mean_test_ic"] - (ics[1] + ics[2] + ics[3]) / 3.0) < 1e-9)

# ── V4: --feature 컬럼 교체 (touch_cvd_ratio 는 전 폴드에서 ret 과 단조) ──
rows_cvd = validate_ic.load_rows(conn, feature="touch_cvd_ratio")
res_cvd = validate_ic.evaluate_folds(rows_cvd)
check("V4 --feature 표본 40행", len(rows_cvd) == N)
check("V4 touch_cvd_ratio 전 폴드 IC > 0.9",
      all(f["ic"] is not None and f["ic"] > 0.9 for f in res_cvd["folds"]))
try:
    validate_ic.load_rows(conn, feature="no_such_col; DROP TABLE levels")
    bad_col_rejected = False
except SystemExit:
    bad_col_rejected = True
check("V4 존재하지 않는 --feature 컬럼 거부(SystemExit)", bad_col_rejected)
conn.close()

# ── V5: CLI 엔드투엔드 ──────────────────────────────────────────────
buf = io.StringIO()
with redirect_stdout(buf):
    rc = validate_ic.main(["--db", str(TEST_DB)])
out = buf.getvalue()
check("V5 CLI 정상 종료(rc=0)", rc == 0)
check("V5 리포트에 전체 표·폴드·요약 문구 존재",
      "── 전체 표본" in out and "폴드1:" in out and "요약:" in out)
check("V5 v4 표본 없음 → 생략 문구", "생략(표본 부족)" in out)

buf = io.StringIO()
with redirect_stdout(buf):
    rc_f = validate_ic.main(["--db", str(TEST_DB), "--feature", "touch_cvd_ratio"])
check("V5 --feature CLI 정상 종료", rc_f == 0
      and "feature=touch_cvd_ratio" in buf.getvalue())

buf = io.StringIO()
with redirect_stdout(buf):
    rc_small = validate_ic.main(["--db", str(SMALL_DB)])
check("V5 표본 <30 우아한 건너뜀 (rc=0 + 안내 문구)",
      rc_small == 0 and "건너뜁니다" in buf.getvalue())

# ── D1: 작성자 삭제율 (audit_dump._compute_author_deletion_stats) ────
with db.connect(str(TEST_DB)) as conn:
    stats = audit_dump._compute_author_deletion_stats(conn)
    by_author = {s["author"]: s for s in stats}
    g = by_author.get("Gamer")
    check("D1 Gamer 글 10개(레벨 12행이어도 post_url distinct)·삭제 2 → rate 0.2",
          g is not None and g["total"] == 10 and g["deleted"] == 2
          and abs(g["rate"] - 0.2) < 1e-9)
    check("D1 Clean 6글 0삭제 → rate 0.0",
          by_author.get("Clean", {}).get("rate") == 0.0
          and by_author["Clean"]["total"] == 6)
    check("D1 글 <5 작성자(Small)는 삭제율 100% 여도 제외", "Small" not in by_author)
    check("D1 rate 내림차순 정렬 (Gamer 가 Clean 보다 먼저)",
          [s["author"] for s in stats] == ["Gamer", "Clean"])
    check("D1 checked 참고 필드 (Gamer 확인 7글: p0~p5 + 중복확인 합산 distinct)",
          g["checked"] == 6 or g["checked"] == 7)

    # ── D2: 주간 grade_stats JSON 페이로드에 통합 ────────────────────
    res_audit = audit_dump.run_weekly_audit(conn, str(TEST_DB), now=T0 + 100 * 86400,
                                            out_dir=OUT_DIR)
    stats_files = [f for f in res_audit["files"] if f.startswith("grade_stats_")]
    check("D2 grade_stats JSON 생성", len(stats_files) == 1)
    payload = json.loads((OUT_DIR / stats_files[0]).read_text(encoding="utf-8"))
    check("D2 payload 에 author_deletion_rates 키 존재",
          "author_deletion_rates" in payload)
    check("D2 payload 값이 직접 계산과 일치", payload.get("author_deletion_rates") == stats)

print()
print("전체:", "✅ ALL PASS" if ok else "❌ FAIL")
sys.exit(0 if ok else 1)
