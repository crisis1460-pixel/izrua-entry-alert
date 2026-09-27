# analytics.rows (분석 단일 입구 analysis_rows + 데이터 사전) 단위 테스트.
#
# 2026-09-28 개발운영방식 최종안 #3: 즉석 조회가 필터를 빠뜨려 분석이 틀리던 문제
# (오염 터치 혼입·judgment_mode 혼합·MFE 무효 구간·best_tp_hit 해석)를 기본값으로
# 막는지 본다. 임시 DB(storage.db.init_db)에 손으로 행을 넣고 경계값을 확인한다.
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from analytics import calibration, rows as ar, weekly
from storage import audit_dump, db

audit_dump.SUPPRESSED = True   # init_db 의 주간 감사 덤프 훅 끔(부수효과 없음)

ok = True
n_checks = 0


def check(name, cond):
    global ok, n_checks
    n_checks += 1
    print(("✅" if cond else "❌"), name)
    ok = ok and cond


T0 = 1_790_000_000.0          # 기준 시각
FIX = T0 + 10_000.0           # MFE 수리 시각(meta 로 덮어씀)
_seq = [0]


def ins(conn, **kw):
    _seq[0] += 1
    base = dict(signal_key=f"k{_seq[0]}", coin_symbol="AAA", ticker="KRW-AAA",
                direction="long", entry_usd=1.0, status="touched",
                collected_at=T0 - 3600, touched_at=T0, resolved_at=T0 + 86400,
                outcome="hit", judgment_mode="tp_sl", grade="A", grade_ver="v6",
                best_tp_hit=1, mfe_pct=5.0, mae_pct=-1.0)
    base.update(kw)
    cols = list(base)
    cur = conn.execute(
        f"INSERT INTO levels ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
        [base[c] for c in cols])
    return cur.lastrowid


tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
db.init_db(tmp)
with db.connect(tmp) as conn:
    db.set_meta(conn, ar.MFE_META_KEY, str(FIX))
    ids = {}
    ids["normal"] = ins(conn)
    ids["stale_flag"] = ins(conn, touch_stale=1)
    # 관통>10% & 즉시(<600s) = 오염, 관통>10% 이지만 느린 터치 = 정상, 관통 NULL = 정상
    ids["stale_rule"] = ins(conn, touch_penetration_pct=15.0, collected_at=T0 - 300)
    ids["deep_slow"] = ins(conn, touch_penetration_pct=15.0, collected_at=T0 - 3600)
    ids["pen_599"] = ins(conn, touch_penetration_pct=10.5, collected_at=T0 - 599)
    ids["pen_600"] = ins(conn, touch_penetration_pct=10.5, collected_at=T0 - 600)
    ids["pen_10"] = ins(conn, touch_penetration_pct=10.0, collected_at=T0 - 10)
    ids["deleted"] = ins(conn, deleted=1)
    ids["tp_only"] = ins(conn, judgment_mode="tp_only", outcome="timeboxed_win",
                         best_tp_hit=None)
    ids["tbx"] = ins(conn, judgment_mode="timeboxed", outcome="timeboxed_loss",
                     best_tp_hit=None, grade_ver="v5")
    ids["miss_mid"] = ins(conn, outcome="miss", best_tp_hit=2)   # 중간 TP 도달 후 손절
    ids["open"] = ins(conn, outcome=None, judgment_mode=None, best_tp_hit=None,
                      resolved_at=None)
    ids["shadow"] = ins(conn, touched_at=None)                   # 섀도 터치
    # MFE 경계: 수리 1초 전 / 정각 / 1초 후
    ids["mfe_before"] = ins(conn, touched_at=FIX - 1, collected_at=FIX - 7200)
    ids["mfe_on"] = ins(conn, touched_at=FIX, collected_at=FIX - 7200)
    ids["mfe_after"] = ins(conn, touched_at=FIX + 1, collected_at=FIX - 7200)

with db.connect(tmp) as conn:
    conn.row_factory = None     # 호출부 row_factory 와 무관하게 동작해야 한다
    rows = ar.analysis_rows(conn)
    got = {r["id"] for r in rows}
    inv = {v: k for k, v in ids.items()}
    names = {inv[i] for i in got}

    # ── S: 오염·삭제·미종결 기본 제외 ─────────────────────────────
    check("S1 touch_stale=1 기본 제외", "stale_flag" not in names)
    check("S2 관통>10% & 즉시(<600s) 기본 제외", "stale_rule" not in names)
    check("S3 관통>10% 느린 터치(정상 표본)는 남는다", "deep_slow" in names)
    check("S4 경계: 599s 제외 · 600s 유지", "pen_599" not in names and "pen_600" in names)
    check("S5 경계: 관통 정확히 10% 는 오염 아님", "pen_10" in names)
    check("S6 deleted=1 기본 제외", "deleted" not in names)
    check("S7 미종결·섀도 터치 기본 제외", "open" not in names and "shadow" not in names)
    check("S8 conn.row_factory 불변", conn.row_factory is None)
    check("S9 모든 반환 행 is_stale=False", all(not r["is_stale"] for r in rows))

    rs = ar.analysis_rows(conn, include_stale=True)
    sn = {inv[r["id"]] for r in rs}
    check("S10 include_stale=True → 오염 행 포함 + is_stale 표시",
          {"stale_flag", "stale_rule", "pen_599"} <= sn
          and all(r["is_stale"] == (inv[r["id"]] in ("stale_flag", "stale_rule", "pen_599"))
                  for r in rs))
    check("S11 NOT_STALE 는 storage.db 것 그대로(사본 아님)", ar.NOT_STALE is db.NOT_STALE)
    rd = ar.analysis_rows(conn, include_deleted=True)
    check("S12 include_deleted=True → 삭제글 포함", ids["deleted"] in {r["id"] for r in rd})
    ra = ar.analysis_rows(conn, closed_only=False)
    check("S13 closed_only=False → 미종결·섀도 포함",
          {ids["open"], ids["shadow"]} <= {r["id"] for r in ra})
    rst = ar.analysis_rows(conn, closed_only=False, statuses="watching")
    check("S14 statuses 필터(해당 없음 → 0)", rst == [])

    # ── J: judgment_mode 필터·층화 ───────────────────────────────
    rj = ar.analysis_rows(conn, judgment_mode="tp_only")
    check("J1 judgment_mode='tp_only' 단일", [inv[r["id"]] for r in rj] == ["tp_only"])
    rj2 = ar.analysis_rows(conn, judgment_mode=("tp_only", "timeboxed"))
    check("J2 judgment_mode 튜플", {inv[r["id"]] for r in rj2} == {"tp_only", "tbx"})
    st = ar.stratify(rows)
    check("J3 stratify 키 순서 tp_sl→tp_only→timeboxed",
          list(st) == ["tp_sl", "tp_only", "timeboxed"])
    check("J4 stratify 합계 = 전체", sum(len(v) for v in st.values()) == len(rows))
    st2 = ar.stratify(ra)
    check("J5 NULL judgment_mode → '(미종결)' 층", "(미종결)" in st2)
    st3 = ar.stratify(rows, key=lambda r: r["grade_ver"])
    check("J6 stratify callable 키", set(st3) == {"v6", "v5"})
    check("J7 stratum 파생 = judgment_mode",
          all(r["stratum"] == r["judgment_mode"] for r in rows))
    rg = ar.analysis_rows(conn, grade_ver="v5")
    check("J8 grade_ver 필터", [inv[r["id"]] for r in rg] == ["tbx"])

    # ── M: MFE 유효 구간 경계 ───────────────────────────────────
    by = {inv[r["id"]]: r for r in rows}
    check("M1 수리 1초 전 → mfe_valid False + mfe/mae None 마스킹",
          by["mfe_before"]["mfe_valid"] is False and by["mfe_before"]["mfe_pct"] is None
          and by["mfe_before"]["mae_pct"] is None)
    check("M2 수리 정각 → 유효(>=)", by["mfe_on"]["mfe_valid"] and by["mfe_on"]["mfe_pct"] == 5.0)
    check("M3 수리 1초 후 → 유효", by["mfe_after"]["mfe_valid"])
    rv = ar.analysis_rows(conn, require_valid_mfe=True)
    check("M4 require_valid_mfe → 유효 터치만",
          {inv[r["id"]] for r in rv} == {"mfe_on", "mfe_after"})
    rraw = ar.analysis_rows(conn, mask_invalid_mfe=False)
    check("M5 mask_invalid_mfe=False → 원값 유지(플래그는 False)",
          any(inv[r["id"]] == "mfe_before" and r["mfe_pct"] == 5.0 and not r["mfe_valid"]
              for r in rraw))

    # ── T: since/until 경계 (반열린 [since, until)) ─────────────────
    rt = ar.analysis_rows(conn, since=FIX, until=FIX + 1)
    check("T1 since 포함 · until 미포함", [inv[r["id"]] for r in rt] == ["mfe_on"])
    rt2 = ar.analysis_rows(conn, since=FIX + 1)
    check("T2 since 이후만", [inv[r["id"]] for r in rt2] == ["mfe_after"])
    rt3 = ar.analysis_rows(conn, until=FIX)
    check("T3 until 은 미포함", ids["mfe_on"] not in {r["id"] for r in rt3}
          and ids["mfe_before"] in {r["id"] for r in rt3})
    rt4 = ar.analysis_rows(conn, since=FIX - 7200, until=FIX - 7199, time_col="collected_at")
    check("T4 time_col=collected_at", {inv[r["id"]] for r in rt4}
          == {"mfe_before", "mfe_on", "mfe_after"})
    check("T5 날짜 문자열 = KST 자정",
          ar._to_epoch("2026-09-28") == datetime(2026, 9, 27, 15, tzinfo=timezone.utc).timestamp()
          and ar._fmt_kst(ar._to_epoch("2026-09-28")) == "2026-09-28")
    try:
        ar.analysis_rows(conn, time_col="expired_at; DROP")
        check("T6 time_col 화이트리스트", False)
    except ValueError:
        check("T6 time_col 화이트리스트", True)
    try:
        ar.analysis_rows(conn, columns=["nope_col"])
        check("T7 없는 컬럼 ValueError", False)
    except ValueError:
        check("T7 없는 컬럼 ValueError", True)
    rc = ar.analysis_rows(conn, columns=["grade"])
    check("T8 columns 좁혀도 필수·파생 필드 존재",
          set(rc[0]) >= {"grade", "outcome", "judgment_mode", "touched_at",
                          "mfe_valid", "hit_tp1", "stratum"}
          and "coin_symbol" not in rc[0])

    # ── H: TP1 도달 의미 ───────────────────────────────────────
    check("H1 hit_tp1 = outcome=='hit' (calibration 정의)",
          by["normal"]["hit_tp1"] and not by["miss_mid"]["hit_tp1"]
          and not by["tp_only"]["hit_tp1"])
    check("H2 reached_tp1 = 중간 TP 도달 포함", by["miss_mid"]["reached_tp1"]
          and not by["tbx"]["reached_tp1"])
    check("H3 win = weekly WIN_OUTCOMES", by["tp_only"]["win"] and not by["tbx"]["win"])
    check("H4 outcome 상수가 calibration·weekly 와 일치",
          ar.CLOSED_OUTCOMES == calibration.CLOSED_OUTCOMES
          and set(ar.CLOSED_OUTCOMES) == set(weekly.CLOSED_OUTCOMES)
          and ar.WIN_OUTCOMES == weekly.WIN_OUTCOMES
          and ("hit",) == calibration.HIT_OUTCOMES)

    # ── M 추가: meta 없음 → 전 구간 무효 ───────────────────────────
    conn.execute("DELETE FROM meta WHERE key=?", (ar.MFE_META_KEY,))
    rn = ar.analysis_rows(conn)
    check("M6 meta 없음 → 전부 mfe_valid False", rn and all(not r["mfe_valid"] for r in rn))
    check("M7 meta 없음 + require_valid_mfe → 0행",
          ar.analysis_rows(conn, require_valid_mfe=True) == [])
    conn.rollback()

# ── D: describe_filters 문구 ─────────────────────────────────────
d0 = ar.describe_filters(12)
check("D1 기본 문구", d0 == "표본 n=12 · 기간 전체(touched_at) · 대상 종결·실터치 · "
      "제외 오염터치(NOT_STALE)·삭제글 · MFE 무효구간(수리 전) 값 비움")
d1 = ar.describe_filters(5, since="2026-09-01", until="2026-09-28",
                         judgment_mode="tp_sl", grade_ver=("v5", "v6"),
                         require_valid_mfe=True, mfe_fixed_since=1790031856.234269)
check("D2 기간·층·MFE 절단 표기",
      "기간 2026-09-01 ~ 2026-09-28 미만(touched_at, KST)" in d1
      and "judgment_mode=tp_sl" in d1 and "grade_ver=v5/v6" in d1
      and "MFE 유효 터치만(<2026-09-22 08:04 제외)" in d1)
d2 = ar.describe_filters(include_stale=True, include_deleted=True, closed_only=False,
                         mask_invalid_mfe=False)
check("D3 포함 모드 경고 표기", "제외 없음" in d2 and "미종결 포함" in d2
      and "⚠️ MFE" in d2 and d2.startswith("표본 · "))
check("D4 한 줄", "\n" not in d0 + d1 + d2)

# ── C: 데이터 사전 ───────────────────────────────────────────────
need = ["entry_usd", "status", "outcome", "best_tp_hit", "r_multiple", "judgment_mode",
        "ret_24h", "mfe_pct", "touch_stale", "touch_penetration_pct", "armed",
        "grade_ver", "touch_grade", "tp_alert_idx", "touch_*"]
keys = " ".join(ar.COLUMNS)
check("C1 사전에 핵심 컬럼 전부", all(k in keys for k in need))
check("C2 사전 항목마다 뜻·단위·생존표",
      all(set(v) == {"뜻", "단위", "생존표"} for v in ar.COLUMNS.values()))

try:
    os.remove(tmp)
except OSError:
    pass

print(f"\n{'OK' if ok else 'FAIL'} — {n_checks} checks")
sys.exit(0 if ok else 1)
