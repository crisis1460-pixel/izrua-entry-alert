#!/usr/bin/env python3
"""
주간 건강표 (CTO 내부용) — 2026-09-28 대표 승인 개발 절차 #4.

무엇: levels.db 를 **읽기 전용**으로 열어 최근 7일 vs 직전 7일(+4주 추이)의 운영
건강 지표를 Markdown 표로 출력한다. 기준(THRESHOLDS)을 넘는 행에 ⚠️ 를 붙인다.

누구에게: 내부 전용. 텔레그램 발송 없음(대표 결정 — 건강/CI 정보는 대표에게 보내지
않는다). 출력처는 stdout, --out 파일, GitHub Actions 의 step summary 뿐이다.

읽기 전용 원칙(show_status.py 와 동일): sqlite URI mode=ro 로 열어 OS 레벨에서
쓰기를 차단한다. db.connect()/init_db() 는 마이그레이션 ALTER 를 할 수 있어 쓰지
않는다. 오염 터치 판별식은 storage.db.STALE_TOUCH_COND 를 그대로 import 한다
(SQL 복제 금지 — 정의가 바뀌면 여기도 자동으로 따라간다).

사용:
  python scripts/health_report.py                    # data/levels.db, 지금 기준
  python scripts/health_report.py --db 사본.db --out health.md
  python scripts/health_report.py --now 1790550000   # 기준 시각(epoch) 고정 — 테스트용
환경변수 GITHUB_STEP_SUMMARY 가 있으면 그 파일에도 덧붙인다.
환경변수 GITHUB_TOKEN + GITHUB_REPOSITORY 가 있으면 tests.yml 최신 결론을 조회한다
(네트워크 실패는 무시 — 건강표 자체는 절대 실패하지 않는다).
"""

import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from storage.db import STALE_TOUCH_COND  # noqa: E402

_KST = timezone(timedelta(hours=9))
WEEK = 7 * 86400.0


def _utc(*a) -> float:
    return datetime(*a, tzinfo=timezone.utc).timestamp()


# ── 경고 기준 ────────────────────────────────────────────────────────────────
# 2026-09-28 초기값: 운영 DB 최근 8주 실측(주별)에서 "평상시 주간은 경고가 안 뜨도록"
# 보수적으로 잡았다. 관찰 후(10-06 즉시터치 수리 관찰 종료 시점) 좁힐 것.
THRESHOLDS = {
    # 오염형 터치(STALE_TOUCH_COND) 주간 건수 — 8주 실측 2·2·4·6·8·3 (+09-21~27 주 11,
    # 무장 게이트 배포 직전의 즉시터치 버그 주간). 버그 주간 이전 최대 8 을 상한으로.
    # 09-27 무장 게이트 이후 기대치는 ~0 — 10-06 관찰 후 3 으로 좁히는 것을 검토.
    "stale_touch_max": 8,
    # 10분 내 터치율(%) — 8주 실측 40~74%(수집 시점에 이미 진입가 근처인 글이 많아
    # 원래 높다). 8주 최대 74% 위로 여유를 둔 80%.
    "instant_touch_rate_max": 80.0,
    # 무장 전 터치(touched_at < armed_at) — 게이트가 있으면 구조적으로 0 이어야 한다.
    "touch_before_armed_max": 0,
    # 억제(sent=0) 사유별: 이번 주가 지난 주의 N배 이상 **그리고** 최소 건수 이상이면 경고.
    # 실측 news 32~35·tp1~3 12~18/주로 안정 — 2배 급증만 잡는다.
    "suppress_jump_ratio": 2.0,
    "suppress_jump_min": 10,
    # 채움률 경고는 분모(해당 소스 도입 이후 터치 수)가 이 값 이상일 때만 — 소표본 오경보 방지.
    "fill_min_denominator": 10,
    # 불변식 위반 허용 건수 — 0 초과면 경고.
    "invariant_violation_max": 0,
}

# ── 외부 API 채움률 대상 ─────────────────────────────────────────────────────
# (컬럼, 표시명, 경고 하한 %, 측정 시작 epoch|None)
# 하한은 5주(08-24~09-27) 주별 실측 최저치의 약 2/3 로 잡았다:
#   funding 51~80 → 35 · oi 40~73 → 25 · stwits 41~59 → 25 · fear_greed 100 → 90 ·
#   kimchi 94~100 → 85 · adx14 51~80 → 35 · active_addr 16~38 → 10 · dex 23~43 → 12.
# 측정 시작 None = 그 컬럼에 값이 처음 찍힌 터치부터(도입 전 터치는 분모에서 제외).
# 명시 시작 = 의미가 바뀐 시점: 롱숏 3종은 Binance 미국 IP 차단으로 08-28 이후 사실상
# 비어 있다가 09-27 OKX 폴백(7b0107e8e, 21:06 KST)으로 재개. rvol/low30/breadth 는
# 같은 커밋에서 신설 — 실측 기준이 아직 없어 하한 20%(보수적 임시값).
_OKX_V3 = _utc(2026, 9, 27, 12, 6, 53)
FILL_COLUMNS = [
    ("touch_long_short_ratio", "롱숏비 (OKX 폴백)", 20.0, _OKX_V3),
    ("touch_top_trader_ratio", "탑트레이더 롱숏 (OKX 폴백)", 20.0, _OKX_V3),
    ("touch_taker_buy_sell_ratio", "테이커 매수/매도 (OKX 폴백)", 20.0, _OKX_V3),
    ("touch_funding_rate", "펀딩비", 35.0, None),
    ("touch_oi_pct", "미결제약정 변화", 25.0, None),
    ("touch_stwits_bullish_ratio", "StockTwits 강세비", 25.0, None),
    ("touch_rvol_d20", "RVOL(20일)", 20.0, _OKX_V3),
    ("touch_low30_pct", "30일 저점 거리", 20.0, _OKX_V3),
    ("touch_alt_breadth", "알트 브레드스", 20.0, _OKX_V3),
    ("touch_fear_greed", "공포탐욕", 90.0, None),
    ("touch_kimchi_pct", "김치프리미엄", 85.0, None),
    ("touch_adx14", "ADX14", 35.0, None),
    ("touch_active_addr_pctile", "활성주소 백분위", 10.0, None),
    ("touch_dex_liquidity_usd", "DEX 유동성", 12.0, None),
    ("touch_dex_volume_24h_usd", "DEX 24h 거래량", 12.0, None),
    ("touch_dex_buy_ratio", "DEX 매수비", 12.0, None),
]

# ── 불변식 ──────────────────────────────────────────────────────────────────
# 07-26 감사 수리 이전 수집분은 알려진 레거시 결함(touch_price_krw 컬럼 도입 전 NULL 20건,
# 터치 앵커 버그로 touched_at < collected_at 6건 — 07-22~23, repair_rejudge_20260726
# 이 원천 보존 원칙으로 행을 남겼다)이라 불변식 검사에서 제외한다.
LEGACY_CUTOFF = _utc(2026, 7, 26, 15, 0, 0)  # 2026-07-27 00:00 KST

# 실터치 = touched_at 이 있는 행. status='touched' 인데 touched_at 이 NULL 인 행은
# **섀도 터치**(db.mark_touched: 클러스터 형제의 재알림 방지용 상태 전이만, 판정·통계
# 제외)라 정상이다 — "touched 인데 touched_at 없음"은 불변식이 아니다.
_L = f"FROM levels WHERE collected_at >= {LEGACY_CUTOFF}"
_A_JOIN = ("FROM alerts_log a JOIN levels l "
           "ON instr(',' || a.level_ids || ',', ',' || l.id || ',') > 0 "
           f"WHERE a.kind = 'touch' AND l.collected_at >= {LEGACY_CUTOFF}")
INVARIANTS = [
    ("섀도 터치에 가격·판정이 있음",
     f"SELECT COUNT(*) {_L} AND touched_at IS NULL "
     "AND (touch_price_krw IS NOT NULL OR outcome IS NOT NULL)"),
    ("실터치인데 touch_price_krw 없음",
     f"SELECT COUNT(*) {_L} AND status = 'touched' AND touched_at IS NOT NULL "
     "AND touch_price_krw IS NULL"),
    ("판정 상태 불일치(outcome↔resolved_at, 비터치 판정)",
     f"SELECT COUNT(*) {_L} AND ((outcome IS NULL) != (resolved_at IS NULL) "
     "OR (outcome IS NOT NULL AND status != 'touched'))"),
    ("롱 기하 위반(TP≤진입 또는 SL≥진입)",
     f"SELECT COUNT(*) {_L} AND direction = 'long' AND entry_usd IS NOT NULL "
     "AND ((tp_usd IS NOT NULL AND tp_usd <= entry_usd) "
     "OR (sl_usd IS NOT NULL AND sl_usd >= entry_usd))"),
    ("숏 기하 위반(TP≥진입 또는 SL≤진입)",
     f"SELECT COUNT(*) {_L} AND direction = 'short' AND entry_usd IS NOT NULL "
     "AND ((tp_usd IS NOT NULL AND tp_usd >= entry_usd) "
     "OR (sl_usd IS NOT NULL AND sl_usd <= entry_usd))"),
    # best_tp_hit 는 09-27 S1 부터 miss/timeboxed 에도 중간 도달 차수를 남긴다.
    # 도달 못 한 miss 의 NULL 은 정상이므로 "hit 인데 NULL"과 "발송한 TP 차수보다 낮음"만 본다.
    ("best_tp_hit 누락(hit 인데 NULL / TP알림 차수 미만)",
     f"SELECT COUNT(*) {_L} AND outcome IS NOT NULL AND ("
     "(outcome = 'hit' AND best_tp_hit IS NULL) "
     "OR (COALESCE(tp_alert_idx, 0) > 0 AND COALESCE(best_tp_hit, 0) < tp_alert_idx))"),
    ("touched_at < collected_at",
     f"SELECT COUNT(*) {_L} AND touched_at IS NOT NULL AND touched_at < collected_at"),
    ("무장 불일치(armed=1 인데 armed_at 없음 / 무장 전 터치)",
     f"SELECT COUNT(*) {_L} AND ((armed = 1 AND armed_at IS NULL) "
     "OR (touched_at IS NOT NULL AND armed_at IS NOT NULL AND touched_at < armed_at))"),
    # 발송된 터치 알림의 레벨은 touched 여야 한다. 단 섀도 터치 형제는 나중에
    # expired_reason='shadow_touch' 로 만료될 수 있다(db.py 만료 경로) — 정상.
    ("발송 터치알림의 레벨이 터치 상태 아님",
     f"SELECT COUNT(*) {_A_JOIN} AND a.sent = 1 AND NOT (l.status = 'touched' "
     "OR (l.status = 'expired' AND l.expired_reason = 'shadow_touch'))"),
    ("같은 레벨 터치알림 중복",
     f"SELECT COUNT(*) FROM (SELECT l.id {_A_JOIN} GROUP BY l.id HAVING COUNT(*) > 1)"),
]


def open_ro(db_path: str) -> sqlite3.Connection:
    """읽기 전용 연결. 파일이 없으면 sqlite 가 새로 만들지 않도록 URI mode=ro."""
    uri = "file:" + Path(db_path).resolve().as_posix() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _one(conn, sql, params=()):
    return conn.execute(sql, params).fetchone()[0] or 0


def _windows(now: float, n: int = 4):
    """[(lo, hi)] — 0 = 최근 7일, 1 = 직전 7일, ... (n 주)."""
    return [(now - (k + 1) * WEEK, now - k * WEEK) for k in range(n)]


_REAL = "touched_at IS NOT NULL AND touched_at >= ? AND touched_at < ?"


def collect(conn, now: float) -> dict:
    wins = _windows(now, 4)
    m = {"now": now, "wins": wins}

    m["touches"] = [_one(conn, f"SELECT COUNT(*) FROM levels WHERE {_REAL}", w) for w in wins]
    m["stale"] = [_one(conn, f"SELECT COUNT(*) FROM levels WHERE {_REAL} AND {STALE_TOUCH_COND}", w)
                  for w in wins]
    inst = [_one(conn, f"SELECT COUNT(*) FROM levels WHERE {_REAL} "
                       "AND (touched_at - collected_at) < 600", w) for w in wins]
    m["instant_n"] = inst
    m["instant_rate"] = [(100.0 * i / n) if n else None for i, n in zip(inst, m["touches"])]
    m["before_armed"] = [_one(conn, f"SELECT COUNT(*) FROM levels WHERE {_REAL} "
                                    "AND armed_at IS NOT NULL AND touched_at < armed_at", w)
                         for w in wins]

    sup = {}
    for k, (lo, hi) in enumerate(wins[:2]):
        for r in conn.execute("SELECT kind, COUNT(*) FROM alerts_log WHERE COALESCE(sent, 1) = 0 "
                              "AND sent_at >= ? AND sent_at < ? GROUP BY kind", (lo, hi)):
            sup.setdefault(r[0], [0, 0])[k] = r[1]
    m["suppress"] = sup

    fills = []
    for col, label, min_pct, since in FILL_COLUMNS:
        if since is None:
            since = conn.execute(f"SELECT MIN(touched_at) FROM levels WHERE {col} IS NOT NULL "
                                 "AND touched_at IS NOT NULL").fetchone()[0]
        row = {"col": col, "label": label, "min": min_pct, "since": since, "rate": [], "den": []}
        for lo, hi in wins[:2]:
            if since is None:
                row["den"].append(0)
                row["rate"].append(None)
                continue
            lo2 = max(lo, since)
            den = _one(conn, f"SELECT COUNT(*) FROM levels WHERE {_REAL}", (lo2, hi)) if lo2 < hi else 0
            num = (_one(conn, f"SELECT COUNT({col}) FROM levels WHERE {_REAL}", (lo2, hi))
                   if lo2 < hi else 0)
            row["den"].append(den)
            row["rate"].append((100.0 * num / den) if den else None)
        fills.append(row)
    m["fills"] = fills

    m["invariants"] = [(name, _one(conn, sql)) for name, sql in INVARIANTS]
    return m


# ── CI 상태 (선택) ──────────────────────────────────────────────────────────
def ci_status(timeout: float = 10.0):
    """(표시문, 경고여부). 토큰 없으면 조회 생략, 네트워크 실패는 경고 없이 표시만."""
    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not token or not repo:
        return "CI: 로컬 실행 — 확인 생략", False
    url = (f"https://api.github.com/repos/{repo}/actions/workflows/tests.yml/runs"
           "?branch=main&status=completed&per_page=10")
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "izrua-health-report",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            runs = json.loads(r.read().decode("utf-8")).get("workflow_runs") or []
    except Exception as e:  # noqa: BLE001 - 건강표는 네트워크 때문에 실패하면 안 된다
        return f"CI: 조회 실패({type(e).__name__}) — 확인 생략", False
    # 연속 push 의 cancel-in-progress 로 취소된 실행은 판정이 아니다 — 실제 결론이 난 첫 실행.
    runs = [r for r in runs if r.get("conclusion") not in ("cancelled", "skipped", None)]
    if not runs:
        return "CI: tests.yml 완료 실행 없음", False
    run = runs[0]
    concl = run.get("conclusion") or "?"
    when = (run.get("updated_at") or "")[:16].replace("T", " ")
    sha = (run.get("head_sha") or "")[:9]
    return f"CI: tests.yml 최신 = {concl} ({when} UTC, {sha})", concl != "success"


# ── 렌더 ────────────────────────────────────────────────────────────────────
def _pct(v):
    return "–" if v is None else f"{v:.0f}%"


def _trend(vals, fmt=str):
    # vals: [w0, w1, w2, w3] → "w3 → w2 → w1 → w0" (과거 → 최근)
    return " → ".join(fmt(v) for v in reversed(vals))


def render(m: dict, ci=None) -> str:
    T = THRESHOLDS
    now = m["now"]
    (lo0, hi0), (lo1, _) = m["wins"][0], m["wins"][1]
    d = lambda t: datetime.fromtimestamp(t, _KST).strftime("%m-%d")  # noqa: E731
    out = [f"## 🩺 주간 건강표 ({d(lo0)}~{d(hi0)} KST, 직전 {d(lo1)}~{d(lo0)})", ""]
    out.append(f"_생성 {datetime.fromtimestamp(now, _KST).strftime('%Y-%m-%d %H:%M')} KST · "
               "내부용 · 읽기 전용_")
    out.append("")
    out += ["| 지표 | 이번 주 | 지난 주 | 4주 추이 | 기준 | 상태 |",
            "|---|---|---|---|---|---|"]
    warns = []

    def row(name, cur, prev, trend, crit, bad):
        out.append(f"| {name} | {cur} | {prev} | {trend} | {crit} | {'⚠️' if bad else '✅'} |")
        if bad:
            warns.append(name)

    row("실터치 수(참고)", m["touches"][0], m["touches"][1], _trend(m["touches"]), "—", False)
    row("1. 오염형 터치 수", m["stale"][0], m["stale"][1], _trend(m["stale"]),
        f"≤ {T['stale_touch_max']}", m["stale"][0] > T["stale_touch_max"])
    ir = m["instant_rate"]
    row("2. 10분 내 터치율", f"{_pct(ir[0])} ({m['instant_n'][0]})", _pct(ir[1]), _trend(ir, _pct),
        f"≤ {T['instant_touch_rate_max']:.0f}%",
        ir[0] is not None and ir[0] > T["instant_touch_rate_max"])
    row("2b. 무장 전 터치", m["before_armed"][0], m["before_armed"][1], _trend(m["before_armed"]),
        f"≤ {T['touch_before_armed_max']}", m["before_armed"][0] > T["touch_before_armed_max"])

    sup = m["suppress"]
    if not sup:
        row("3. 억제(sent=0) — 없음", 0, 0, "—", "—", False)
    for kind in sorted(sup):
        cur, prev = sup[kind]
        bad = cur >= T["suppress_jump_min"] and cur >= T["suppress_jump_ratio"] * max(prev, 1)
        row(f"3. 억제 `{kind}`", cur, prev, "—",
            f"< {T['suppress_jump_ratio']:.0f}×지난주 or < {T['suppress_jump_min']}", bad)

    for f in m["fills"]:
        cur, prev = f["rate"]
        if f["since"] is None:
            row(f"4. 채움률 {f['label']}", "기록 없음", "–", "—", f"≥ {f['min']:.0f}%", False)
            continue
        bad = (cur is not None and f["den"][0] >= T["fill_min_denominator"] and cur < f["min"])
        cur_s = "측정 전(분모 0)" if cur is None else f"{_pct(cur)} (n={f['den'][0]})"
        row(f"4. 채움률 {f['label']}", cur_s, _pct(prev), "—", f"≥ {f['min']:.0f}%", bad)

    # 방향 기본값: parse_setup 이 ④ "애매하면 long" 경로를 탔는지 DB 에 남지 않는다.
    row("5. 방향 기본값 사용 수", "측정 불가 — 기록 없음", "–", "—", "—", False)

    inv = m["invariants"]
    n_bad = sum(1 for _, c in inv if c > T["invariant_violation_max"])
    row("6. 불변식 위반", f"{n_bad}/{len(inv)} 항목", "—", "—",
        f"각 ≤ {T['invariant_violation_max']}", n_bad > 0)

    ci_text, ci_bad = ci if ci is not None else ("CI: 로컬 실행 — 확인 생략", False)
    row("7. CI 상태", ci_text, "—", "—", "success", ci_bad)

    out += ["", "### 불변식 상세 (07-27 KST 이후 수집분, 누적)", "",
            "| 불변식 | 위반 | 상태 |", "|---|---|---|"]
    for name, c in inv:
        out.append(f"| {name} | {c} | {'⚠️' if c > T['invariant_violation_max'] else '✅'} |")

    out += ["", "### 메모",
            "- 5번(방향 기본값): 추출기(collector/extractor.parse_setup)가 방향을 어느 경로"
            "(작성자 태그 / 본문 힌트 / TP 기하 / 기본 long)로 정했는지 반환·저장하지 않는다. "
            "측정하려면 parse_setup 반환에 `direction_source` 를 추가하고 run_collect 가 "
            "levels.direction_source 컬럼에 기록해야 한다.",
            "- 채움률 분모 = 해당 소스 도입(첫 기록 또는 명시 시작) 이후의 실터치. "
            f"분모 < {T['fill_min_denominator']} 이면 경고하지 않는다.",
            ""]
    if warns:
        out.append("**⚠️ 경고: " + ", ".join(warns) + "**")
    else:
        out.append("**✅ 경고 없음**")
    out.append("")
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="주간 건강표 (내부용, 읽기 전용)")
    ap.add_argument("--db", default=None, help="DB 경로 (기본: settings db_path)")
    ap.add_argument("--out", default=None, help="Markdown 파일로도 저장")
    ap.add_argument("--now", type=float, default=None, help="기준 시각 epoch (기본: 현재)")
    ap.add_argument("--no-ci", action="store_true", help="CI 조회 생략")
    a = ap.parse_args(argv)

    db_path = a.db
    if db_path is None:
        from config import settings
        db_path = settings.get("db_path")
    if not Path(db_path).exists():
        print(f"DB 없음: {db_path}", file=sys.stderr)
        return 2
    conn = open_ro(db_path)
    try:
        m = collect(conn, a.now if a.now is not None else time.time())
    finally:
        conn.close()
    ci = ("CI: 조회 생략(--no-ci)", False) if a.no_ci else ci_status()
    md = render(m, ci)
    print(md)
    if a.out:
        Path(a.out).write_text(md, encoding="utf-8")
    summ = os.environ.get("GITHUB_STEP_SUMMARY")
    if summ:
        with open(summ, "a", encoding="utf-8") as fh:
            fh.write(md + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
