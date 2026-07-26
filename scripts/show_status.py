#!/usr/bin/env python3
"""
관찰 현황 조회 도구 — "필요할 때 꺼내보는" 모델 (2026-07-26 사용자 결정).

배경: 주간 리포트 자동 발송은 사용자가 껐고(weekly_report_auto_send=False),
daily_stats/author_snapshots 는 계속 쌓이는데 볼 방법이 없어 CTO가 매번 SQL을
직접 짜던 상황이었다. 이 스크립트는 그 자리를 메운다 — 한 번 실행하면 지금
상태를 사람이 읽기 좋게 콘솔에 출력한다. 텔레그램 발송 없음, 콘솔 출력만.

읽기 전용 원칙: DB에 절대 쓰지 않는다. sqlite3 연결도 URI mode=ro 로 열어
OS 레벨에서 쓰기를 차단한다(db.connect()/db.init_db() 는 스키마 마이그레이션
ALTER 를 수행할 수 있어 프로덕션 DB에는 쓰지 않는다 — 여기선 사용하지 않음).

계산 로직은 재구현하지 않는다 — storage/db.py 와 analytics/ 의 기존 함수만 쓴다.

사용:
  python scripts/show_status.py                 # 최근 7일
  python scripts/show_status.py --days 14        # 관찰 기간 조정
  python scripts/show_status.py --report         # 주간 리포트 텍스트도 함께 (발송 없음)
  python scripts/show_status.py --db path/to.db  # 다른 DB(예: 사본)로 조회
"""

import argparse
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

from analytics import clustering, ranking
from collector.grading import GRADE_ORDER
from config import settings
from storage import db

KST = timezone(timedelta(hours=9))
_SEP = "─" * 60


def _ro_connect(db_path: str):
    """읽기 전용 연결. URI mode=ro 로 OS 레벨에서 쓰기를 차단한다.
    (db.connect() 는 쓰기 가능 모드로 열고 커밋까지 하므로 여기선 쓰지 않는다.)
    DB 파일이 없으면 None."""
    p = Path(db_path).resolve()
    if not p.exists():
        return None
    uri = f"{p.as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _fmt_ago(ts, now) -> str:
    """'N시간 전' 형태. ts 없으면 '기록 없음'."""
    if not ts:
        return "기록 없음"
    try:
        ts = float(ts)
    except (TypeError, ValueError):
        return "기록 없음"
    elapsed = now - ts
    when = datetime.fromtimestamp(ts, tz=KST).strftime("%m-%d %H:%M")
    if elapsed < 0:
        return f"{when} (미래?!)"
    if elapsed < 3600:
        return f"{when} ({elapsed / 60:.0f}분 전)"
    if elapsed < 86400:
        return f"{when} ({elapsed / 3600:.1f}시간 전)"
    return f"{when} ({elapsed / 86400:.1f}일 전)"


def _fmt_eta(last_ts, now, interval_sec) -> str:
    if not last_ts:
        return "이력 없음(다음 회차에 시도)"
    try:
        last_ts = float(last_ts)
    except (TypeError, ValueError):
        return "이력 없음(다음 회차에 시도)"
    remain = last_ts + interval_sec - now
    if remain <= 0:
        return "다음 회차에 즉시 (주기 도래)"
    if remain < 3600:
        return f"약 {remain / 60:.0f}분 후"
    return f"약 {remain / 3600:.1f}시간 후"


def _n(v) -> str:
    return f"{int(v):,}"


# ── ① 알림량 관찰 ──────────────────────────────────────────────────

def print_observation(conn, days: int) -> None:
    print(_SEP)
    print(f"📊 알림량 관찰 (최근 {days}일)")
    print(_SEP)
    try:
        rows = db.get_observation_report(conn, days)
    except sqlite3.OperationalError:
        rows = []
    if not rows:
        print("  (관찰 집계 없음 — daily_stats 가 아직 쌓이지 않았습니다)")
        return

    header = (f"{'날짜':<12}{'수집':>6}{'터치':>6}{'예고':>6}{'발송':>6}"
              f"{'전환율':>8}  {'억제(등급/상한/실패)':<20} {'체류':>5}")
    print(header)
    print("-" * len(header))

    tot = {"collected": 0, "touches_total": 0, "previews_total": 0, "alerts_sent": 0,
           "suppressed_grade": 0, "suppressed_cap": 0, "preview_dwell": 0,
           "suppressed_send_fail": 0}
    for r in rows:
        for k in tot:
            tot[k] += r.get(k, 0) or 0
        raw_events = (r["touches_total"] or 0) + (r["previews_total"] or 0)
        conv = f"{r['alerts_sent'] / raw_events * 100:.0f}%" if raw_events else "-"
        supp = (f"{r['suppressed_grade']}/{r['suppressed_cap']}/"
                f"{r['suppressed_send_fail']}"
                f"  {r.get('preview_dwell', 0):>5}")
        flag = " ⚠️" if r["suppressed_send_fail"] else ""
        print(f"{r['day_kst']:<12}{_n(r['collected']):>6}{_n(r['touches_total']):>6}"
              f"{_n(r['previews_total']):>6}{_n(r['alerts_sent']):>6}"
              f"{conv:>8}  {supp:<24}{flag}")

    print("-" * len(header))
    raw_total = tot["touches_total"] + tot["previews_total"]
    conv_total = f"{tot['alerts_sent'] / raw_total * 100:.0f}%" if raw_total else "-"
    supp_total = (f"{tot['suppressed_grade']}/{tot['suppressed_cap']}/"
                  f"{tot['suppressed_send_fail']}"
                  f"  {tot.get('preview_dwell', 0):>5}")
    print(f"{'합계':<12}{_n(tot['collected']):>6}{_n(tot['touches_total']):>6}"
          f"{_n(tot['previews_total']):>6}{_n(tot['alerts_sent']):>6}"
          f"{conv_total:>8}  {supp_total:<24}")
    print("  (전환율 = 발송 ÷ (터치+예고). 억제 = 등급미달/일일상한/텔레그램실패)")
    print("   체류 = 예고 밴드에 머문 회차 — 억제가 아니라 관측 지표")


# ── ② 파이프라인 현재 상태 ─────────────────────────────────────────

def _grade_distribution(conn) -> dict:
    try:
        rows = conn.execute(
            "SELECT grade, COUNT(*) AS n FROM levels "
            "WHERE status IN ('watching','previewed') GROUP BY grade"
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    return {r["grade"] or "(미채점)": r["n"] for r in rows}


def print_pipeline_status(conn, now: float) -> None:
    print()
    print(_SEP)
    print("🔧 현재 파이프라인 상태")
    print(_SEP)
    try:
        s = db.stats(conn)
    except sqlite3.OperationalError:
        s = {"watching": 0, "previewed": 0, "touched": 0, "expired": 0, "total": 0}
    print(f"  감시중(watching) {_n(s['watching'])}  ·  예고중(previewed) {_n(s['previewed'])}  ·  "
          f"터치(touched) {_n(s['touched'])}  ·  만료(expired) {_n(s['expired'])}  ·  "
          f"전체 {_n(s['total'])}")

    dist = _grade_distribution(conn)
    if dist:
        order = GRADE_ORDER + ["(미채점)"]
        parts = [f"{g}:{dist[g]}" for g in order if g in dist]
        parts += [f"{g}:{n}" for g, n in dist.items() if g not in order]
        print(f"  등급 분포(감시중+예고중): {' · '.join(parts)}")
    else:
        print("  등급 분포(감시중+예고중): (대상 없음)")

    print()
    try:
        last_check = db.get_meta(conn, "last_check_at")
        last_collect = db.get_meta(conn, "last_collect_at")
        last_report = db.get_meta(conn, "last_weekly_report_at")
        last_snapshot = db.get_meta(conn, "last_author_snapshot_at")
    except sqlite3.OperationalError:
        last_check = last_collect = last_report = last_snapshot = None

    print(f"  마지막 가격체크    : {_fmt_ago(last_check, now)}")
    print(f"  마지막 수집        : {_fmt_ago(last_collect, now)}"
          f"  (다음 수집 {_fmt_eta(last_collect, now, settings.get('collect_interval_hours') * 3600)})")
    print(f"  마지막 작성자 스냅샷: {_fmt_ago(last_snapshot, now)}")
    auto_send = settings.get("weekly_report_auto_send")
    report_note = "자동발송 OFF — 사용자 결정, --report 로 수동 확인" if not auto_send else ""
    print(f"  마지막 주간리포트  : {_fmt_ago(last_report, now)}  {report_note}")


# ── ③ 작성자 성적 ──────────────────────────────────────────────────

def _rows_by_author(conn) -> dict:
    try:
        authors = db.list_authors_with_outcomes(conn)
    except sqlite3.OperationalError:
        return {}
    return {a: db.get_author_outcome_rows(conn, a) for a in authors}


def print_author_performance(conn, now: float) -> None:
    print()
    print(_SEP)
    print("✍️  작성자 성적")
    print(_SEP)
    rows_by_author = _rows_by_author(conn)
    if not rows_by_author:
        print("  (종결 표본 없음 — 터치 후 승패가 확정된 레벨이 아직 없습니다)")
        return

    min_neff = settings.get("rank_min_neff")
    ranked = ranking.rank_authors(
        rows_by_author, now, min_neff=min_neff,
        half_life_days=settings.get("rank_half_life_days"),
        z=settings.get("rank_z"), m=settings.get("rank_prior_m"))

    total_rows = sum(len(r) for r in rows_by_author.values())
    print(f"  작성자 {len(rows_by_author)}명 · 종결 표본 {total_rows}건 "
          f"· E_LB 게이트 n_eff≥{min_neff:g}")
    print()
    if ranked:
        print(f"  {'순위':<4}{'작성자':<20}{'E_LB':>8}{'n_eff':>8}{'승률':>8}")
        for i, (author, met) in enumerate(ranked, 1):
            anti = " 🔻역신호후보" if met["e_lb"] is not None and met["e_lb"] <= 0 else ""
            pct = f"{met['p_hat'] * 100:.0f}%" if met.get("p_hat") is not None else "-"
            print(f"  {i:<4}{author:<20}{met['e_lb_display']:>+8.2f}"
                  f"{met['neff_r']:>8.1f}{pct:>8}{anti}")
    else:
        print("  게이트 통과 작성자 없음 (계속 관찰 중)")

    ranked_authors = {a for a, _ in ranked}
    under_sample = sorted(
        ((a, len(r)) for a, r in rows_by_author.items() if a not in ranked_authors),
        key=lambda x: x[1], reverse=True)
    if under_sample:
        names = " · ".join(f"{a}({n}건)" for a, n in under_sample[:10])
        more = f" · 외 {len(under_sample) - 10}명" if len(under_sample) > 10 else ""
        print(f"\n  표본 부족(n_eff<{min_neff:g}): {names}{more}")

    # 주간 스냅샷 추이 — 최근 2주 비교(있으면). author_snapshots 는 저장 전용이라
    # 알림/필터에 영향 없음 — 순수 표시.
    try:
        snaps = db.get_author_snapshots(conn, limit_weeks=2)
    except sqlite3.OperationalError:
        snaps = []
    if snaps:
        by_week: dict = {}
        for r in snaps:
            by_week.setdefault(r["week_kst"], {})[r["author"]] = r
        weeks = sorted(by_week, reverse=True)
        print(f"\n  주간 스냅샷 추이 (최근 {len(weeks)}주: {', '.join(weeks)})")
        if len(weeks) >= 2:
            cur, prev = by_week[weeks[0]], by_week[weeks[1]]
            for author in sorted(set(cur) & set(prev)):
                c, p = cur[author], prev[author]
                if c.get("e_lb") is None or p.get("e_lb") is None:
                    continue
                delta = c["e_lb"] - p["e_lb"]
                arrow = "▲" if delta > 0.01 else ("▼" if delta < -0.01 else "─")
                print(f"    {author:<20} E_LB {p['e_lb']:+.2f} → {c['e_lb']:+.2f} {arrow}")
        else:
            print("    (비교할 이전 주 없음 — 다음 주부터 추세 표시)")
    else:
        print("\n  주간 스냅샷: (아직 없음)")


# ── ④ 건강 상태 ────────────────────────────────────────────────────

def print_health(conn, now: float) -> None:
    print()
    print(_SEP)
    print("🩺 건강 상태")
    print(_SEP)
    warnings = []

    last_check = db.get_meta(conn, "last_check_at") if _has_table(conn, "meta") else None
    if last_check:
        elapsed = now - float(last_check)
        if elapsed > 30 * 60:
            warnings.append(f"⚠️ 가격체크가 {elapsed / 60:.0f}분째 멈춰 있습니다"
                            f"(2분 주기 회차 기준 이상 지연) — cron/Actions 확인 필요")
    else:
        warnings.append("⚠️ 가격체크 이력 없음(last_check_at 미기록)")

    last_collect = db.get_meta(conn, "last_collect_at") if _has_table(conn, "meta") else None
    interval = settings.get("collect_interval_hours") * 3600
    if last_collect:
        elapsed = now - float(last_collect)
        if elapsed > interval * 3:
            warnings.append(f"⚠️ 마지막 수집이 {elapsed / 3600:.1f}시간 전 "
                            f"(주기 {settings.get('collect_interval_hours')}시간의 3배 초과) "
                            f"— 수집이 조용히 멈췄을 수 있습니다")
    else:
        warnings.append("⚠️ 수집 이력 없음(last_collect_at 미기록)")

    try:
        unresolved = db.get_unresolved_touched(conn)
    except sqlite3.OperationalError:
        unresolved = []
    if len(unresolved) > 50:
        warnings.append(f"⚠️ 판정 대기(터치됐지만 미종결) {len(unresolved)}건 — "
                        f"평소보다 많이 쌓였다면 가격체크가 밀리고 있을 수 있습니다")

    try:
        recent_stats = db.get_daily_stats(conn, 7)
    except sqlite3.OperationalError:
        recent_stats = []
    amb_unresolved = sum(r.get("ambiguous_unresolved", 0) or 0 for r in recent_stats)
    amb_magnified = sum(r.get("ambiguous_magnified", 0) or 0 for r in recent_stats)
    if amb_unresolved:
        total_amb = amb_unresolved + amb_magnified
        warnings.append(f"⚠️ 최근 7일 동시터치(TP/SL 동시도달) 판별불가 {amb_unresolved}건"
                        f"/{total_amb}건 — 보수적 miss 로 처리됨(판정 신뢰도 참고)")

    send_fail = sum(r.get("suppressed_send_fail", 0) or 0 for r in recent_stats)
    if send_fail:
        warnings.append(f"⚠️ 최근 7일 텔레그램 발송 실패 {send_fail}건 — 토큰/네트워크 확인 필요")

    if warnings:
        for w in warnings:
            print(f"  {w}")
    else:
        print("  ✅ 이상 징후 없음")


def _has_table(conn, name: str) -> bool:
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        return row is not None
    except sqlite3.OperationalError:
        return False


# ── ⑤ (옵션) 기존 주간 리포트 텍스트 ────────────────────────────────

def print_weekly_report_text(conn, now: float) -> None:
    print()
    print(_SEP)
    print("📈 주간 리포트 미리보기 (발송 없음 — telegram.render_weekly_report 그대로)")
    print(_SEP)
    rows_by_author = _rows_by_author(conn)
    try:
        baseline = clustering.baseline_positive_rate(db.get_ret24_values(conn))
        raw_records = db.get_author_raw_record(conn)
        confluence = clustering.confluence_by_author(
            db.get_touched_levels_for_clusters(conn),
            settings.get("cluster_band_pct"),
            window_sec=settings.get("confluence_window_hours") * 3600,
        )
    except sqlite3.OperationalError:
        baseline, raw_records, confluence = None, None, None

    from notify import telegram
    text = telegram.render_weekly_report(
        rows_by_author, now=now, baseline=baseline,
        raw_records=raw_records, confluence=confluence)
    print(_strip_html(text))


def _strip_html(text: str) -> str:
    """텔레그램용 HTML 마크업을 걷어내 터미널에서 읽히게 한다.

    render_weekly_report 는 parse_mode=HTML 전송용이라 <b> 같은 태그와 &lt; 이스케이프가
    섞여 있다. 여기선 발송이 아니라 눈으로 보는 게 목적이므로 태그를 벗기고 엔티티를
    되돌린다(원문 자체는 건드리지 않는다 — 출력 시점 변환)."""
    import html as _html
    import re as _re
    return _html.unescape(_re.sub(r"</?[a-zA-Z][^<>]*>", "", text))


def run(db_path: str, days: int, show_report: bool, now: float = None) -> int:
    now = now if now is not None else time.time()
    conn = _ro_connect(db_path)
    if conn is None:
        print(f"❌ DB 없음: {db_path}")
        return 1
    try:
        print(f"izrua-entry-alert 관찰 현황 — {datetime.fromtimestamp(now, tz=KST).strftime('%Y-%m-%d %H:%M KST')}")
        print(f"DB: {db_path} (읽기 전용)")
        print_observation(conn, days)
        print_pipeline_status(conn, now)
        print_author_performance(conn, now)
        print_health(conn, now)
        if show_report:
            print_weekly_report_text(conn, now)
    finally:
        conn.close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="관찰 현황 조회 (읽기 전용, 발송 없음)")
    ap.add_argument("--days", type=int, default=7, help="관찰 기간(일), 기본 7")
    ap.add_argument("--report", action="store_true",
                    help="기존 주간 리포트 텍스트도 함께 출력(발송은 하지 않음)")
    ap.add_argument("--db", default=None, help="조회할 DB 경로(기본: settings.db_path)")
    args = ap.parse_args()

    db_path = args.db or settings.get("db_path")
    return run(db_path, args.days, args.report)


if __name__ == "__main__":
    sys.exit(main())
