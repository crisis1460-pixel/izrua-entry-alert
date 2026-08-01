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

from analytics import calibration, clustering, distribution, ranking
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
              f"{'전환율':>8}  {'억제(등급/상한/실패/근접TP)':<26} {'체류':>5}")
    print(header)
    print("-" * len(header))

    tot = {"collected": 0, "touches_total": 0, "previews_total": 0, "alerts_sent": 0,
           "suppressed_grade": 0, "suppressed_cap": 0, "preview_dwell": 0,
           "suppressed_send_fail": 0, "suppressed_tp_too_close": 0}
    for r in rows:
        for k in tot:
            tot[k] += r.get(k, 0) or 0
        raw_events = (r["touches_total"] or 0) + (r["previews_total"] or 0)
        conv = f"{r['alerts_sent'] / raw_events * 100:.0f}%" if raw_events else "-"
        supp = (f"{r['suppressed_grade']}/{r['suppressed_cap']}/"
                f"{r['suppressed_send_fail']}/{r.get('suppressed_tp_too_close', 0)}"
                f"  {r.get('preview_dwell', 0):>5}")
        flag = " ⚠️" if r["suppressed_send_fail"] else ""
        print(f"{r['day_kst']:<12}{_n(r['collected']):>6}{_n(r['touches_total']):>6}"
              f"{_n(r['previews_total']):>6}{_n(r['alerts_sent']):>6}"
              f"{conv:>8}  {supp:<26}{flag}")

    print("-" * len(header))
    raw_total = tot["touches_total"] + tot["previews_total"]
    conv_total = f"{tot['alerts_sent'] / raw_total * 100:.0f}%" if raw_total else "-"
    supp_total = (f"{tot['suppressed_grade']}/{tot['suppressed_cap']}/"
                  f"{tot['suppressed_send_fail']}/{tot['suppressed_tp_too_close']}"
                  f"  {tot.get('preview_dwell', 0):>5}")
    print(f"{'합계':<12}{_n(tot['collected']):>6}{_n(tot['touches_total']):>6}"
          f"{_n(tot['previews_total']):>6}{_n(tot['alerts_sent']):>6}"
          f"{conv_total:>8}  {supp_total:<26}")
    print("  (전환율 = 발송 ÷ (터치+예고). 억제 = 등급미달/일일상한/텔레그램실패/근접TP(B안))")
    print("   체류 = 예고 밴드에 머문 회차 — 억제가 아니라 관측 지표")
    _print_bid_ask_pressure(conn)


def _print_bid_ask_pressure(conn) -> None:
    """터치 시점 호가 매수/매도 잔량비 최근 기록 (2026-07-26 카드 #19).
    사후 상관분석용으로 쌓기만 하는 값이라 여기서도 '보여주기' 전용이다 —
    구버전 DB(컬럼 없음)에서는 조용히 생략한다."""
    try:
        rows = db.get_recent_bid_ask_ratios(conn, limit=8)
    except sqlite3.OperationalError:
        return
    if not rows:
        return
    vals = [r["touch_bid_ask_ratio"] for r in rows]
    avg = sum(vals) / len(vals)
    print()
    print(f"  호가 매수/매도 잔량비 (최근 {len(rows)}건 터치, 평균 {avg:.2f} — 기록 전용)")
    for r in rows:
        oc = r["outcome"] or "미종결"
        print(f"    {r['coin_symbol']:<8}{r['touch_bid_ask_ratio']:>6.2f}   {oc}")
    print("   (>1 = 매수 잔량 우위. 알림·필터·등급에는 반영되지 않음)")


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


# ── 등급 캘리브레이션 원천 행 (기획 카드 #26) ─────────────────────────
#
# storage/db.py 는 이번 스프린트에 개발자 B가 작업 중이라 손대지 않는다 — 조회는
# 여기서 conn.execute 로 직접 한다(같은 파일의 _grade_distribution 과 동일 패턴).
# scripts/run_weekly_report.py 도 이 함수를 import 해 쓴다: SQL 정의는 한 곳만 둔다.

def fetch_calibration_rows(conn, ver: str = None) -> list:
    """등급 캘리브레이션용 종결 표본 원시 행 [(grade, outcome, ambiguous), ...].

    제외 기준은 기존 통계(db.get_author_outcome_rows)와 동일하게 맞춘다 —
    미종결(outcome NULL)과 섀도 터치(touched_at NULL)는 표본이 아니다.
    grade 는 **수집 시점에 기록된 DB 원본값**을 그대로 읽는다(재채점하지 않는다).
    ambiguous 컬럼이 없는 구세대 DB 는 OperationalError → 호출부가 빈 목록 처리.

    ver (2026-08-01 S10 D4 — 산식 버전 분리 집계):
      None     = 전체 (기존 호출부 호환)
      'legacy' = grade_ver 미기록(NULL)·현행과 다른 버전 = 구 산식 표본
      그 외     = 해당 grade_ver 표본만 (예: 'v3')
    신·구 산식의 등급은 의미가 달라 한 표에 섞으면 캘리브레이션이 오염된다.
    """
    q = ("SELECT grade, outcome, ambiguous FROM levels "
         "WHERE grade IS NOT NULL AND outcome IS NOT NULL AND touched_at IS NOT NULL")
    cur_ver = settings.get("grade_formula_ver")
    if ver == "legacy":
        return [tuple(r) for r in conn.execute(
            q + " AND (grade_ver IS NULL OR grade_ver <> ?)", (cur_ver,)).fetchall()]
    if ver is not None:
        return [tuple(r) for r in conn.execute(
            q + " AND grade_ver = ?", (ver,)).fetchall()]
    return [tuple(r) for r in conn.execute(q).fetchall()]


def _calibration_pair(conn):
    """(현행 산식 표본 결과, 구 산식 표본 결과) — 조회 실패(구세대 스키마)면 (None, None).

    2026-08-01 S10 D4: v3 표본 집계가 기본, 구버전 표는 "구 산식(참고)" 병기.
    """
    try:
        cur = fetch_calibration_rows(conn, ver=settings.get("grade_formula_ver"))
        legacy = fetch_calibration_rows(conn, ver="legacy")
    except sqlite3.OperationalError:
        return None, None
    return calibration.calibrate_grades(cur), calibration.calibrate_grades(legacy)


def _cal_one_liner(cal) -> str:
    """등급별 도달률 한 줄 요약 (표본 있는 등급만)."""
    parts = []
    for g in cal["order"]:
        b = cal["buckets"][g]
        if not b["n"]:
            continue
        low = "" if b["enough"] else "?"  # 표본 부족 표식
        parts.append(f"{g} {b['rate'] * 100:.0f}%({b['hits']}/{b['n']}){low}")
    return " · ".join(parts)


def _print_calibration_summary(conn) -> None:
    """현황 화면용 요약 — v3 표본 기본 + 구 산식(참고) 병기, 표기 전용."""
    cal, legacy = _calibration_pair(conn)
    if cal is None:
        return
    ver = settings.get("grade_formula_ver")
    if cal["pooled"]["n"]:
        print(f"  등급 캘리브레이션({ver}, TP1 도달률, 종결 {cal['pooled']['n']}건): "
              f"{_cal_one_liner(cal)}   ?=표본 n<{cal['min_n']:g}")
        if cal["violations"]:
            v = cal["violations"][0]
            sig = "CI 비겹침" if v["significant"] else "CI 겹침"
            print(f"  ⚠️ 단조성 위반 {len(cal['violations'])}건 (예: {v['lower']} > "
                  f"{v['higher']}, {sig}) — 배점 재검토 '신호'일 뿐, 산식은 그대로입니다")
    elif legacy and legacy["pooled"]["n"]:
        print(f"  등급 캘리브레이션({ver}): 신 산식 종결 표본 아직 0건 (누적 대기)")
    if legacy and legacy["pooled"]["n"]:
        print(f"  구 산식(참고, 종결 {legacy['pooled']['n']}건): {_cal_one_liner(legacy)}"
              f"   — grade_ver 이전 표본, 신 산식과 등급 의미가 다름")


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
    _print_calibration_summary(conn)

    print()
    try:
        # 2026-07-27 M-A1: 두 키의 의미가 다르다.
        #   last_cycle_at  = 기동 하트비트("회차가 깨어났다")
        #   last_check_at  = 스캔 워터마크("어디까지 소급 검출했다")
        # 예전엔 한 키가 둘을 겸직해, 환율/업비트 조회가 몇 시간 죽어도 값이 계속
        # 갱신되며 화면이 '정상'으로 보였다. 이제 둘을 나란히 찍어 그 사각지대를 없앤다.
        # last_cycle_at 이 없는(구세대/커밋백 롤백) DB 는 워터마크로 폴백한다.
        last_cycle = db.get_meta(conn, "last_cycle_at")
        last_check = db.get_meta(conn, "last_check_at")
        last_collect = db.get_meta(conn, "last_collect_at")
        last_report = db.get_meta(conn, "last_weekly_report_at")
        last_snapshot = db.get_meta(conn, "last_author_snapshot_at")
    except sqlite3.OperationalError:
        last_cycle = last_check = last_collect = last_report = last_snapshot = None

    print(f"  마지막 회차      : {_fmt_ago(last_cycle or last_check, now)}  (하트비트)")
    print(f"  마지막 가격스캔  : {_fmt_ago(last_check, now)}  (소급 판정창의 기준)")
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


def _reverse_confirmed(conn) -> set:
    """역신호 '확정' 작성자 집합 (S9 — run_cycle 스냅샷 훅이 meta 에 기록).
    읽기 전용 조회 — 구세대 DB(meta 없음)는 조용히 빈 집합."""
    try:
        return db.get_reverse_confirmed_authors(conn)
    except sqlite3.OperationalError:
        return set()


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
    confirmed = _reverse_confirmed(conn)
    if ranked:
        print(f"  {'순위':<4}{'작성자':<20}{'E_LB':>8}{'n_eff':>8}{'승률':>8}")
        for i, (author, met) in enumerate(ranked, 1):
            # 확정(2주 연속, 경보 발송됨)은 후보 태그를 대체한다 — 상위 상태이므로
            # 병기하면 같은 사실을 두 번 말하는 소음이 된다(S9, 표시 전용).
            if author in confirmed:
                anti = " 🔻역신호확정"
            elif met["e_lb"] is not None and met["e_lb"] <= 0:
                anti = " 🔻역신호후보"
            else:
                anti = ""
            pct = f"{met['p_hat'] * 100:.0f}%" if met.get("p_hat") is not None else "-"
            print(f"  {i:<4}{author:<20}{met['e_lb_display']:>+8.2f}"
                  f"{met['neff_r']:>8.1f}{pct:>8}{anti}")
    else:
        print("  게이트 통과 작성자 없음 (계속 관찰 중)")
    # 게이트 미달로 랭킹에서 빠져도 확정 상태는 보수적으로 유지되므로 따로 알린다
    hidden_confirmed = confirmed - {a for a, _ in ranked}
    if hidden_confirmed:
        print(f"  🔻역신호확정(랭킹 미등재): {' · '.join(sorted(hidden_confirmed))}")

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

    # 2026-07-27 M-A1: '회차가 도는가'는 하트비트(last_cycle_at)로 판정한다.
    # 구세대/커밋백 롤백 DB 는 워터마크(last_check_at)로 폴백 — 폴백이 없으면 배포
    # 첫 회차와 롤백 직후에 이 경고가 거짓으로 뜬다.
    if _has_table(conn, "meta"):
        last_cycle_raw = db.get_meta(conn, "last_cycle_at")
        last_check_raw = db.get_meta(conn, "last_check_at")
    else:
        last_cycle_raw = last_check_raw = None
    last_check = last_cycle_raw or last_check_raw
    if last_check:
        elapsed = now - float(last_check)
        if elapsed > 30 * 60:
            warnings.append(f"⚠️ 가격체크가 {elapsed / 60:.0f}분째 멈춰 있습니다"
                            f"(2분 주기 회차 기준 이상 지연) — cron/Actions 확인 필요")
    else:
        warnings.append("⚠️ 가격체크 이력 없음(last_check_at 미기록)")

    # 두 키가 갈라지면 그 자체가 신호다: "회차는 도는데 스캔을 못 하고 있다"
    # (환율/업비트 조회 실패가 반복되는 상태). 예전엔 한 키가 둘을 겸직해 이 상황이
    # 통째로 관측 사각지대였다 — 업비트가 몇 시간 죽어도 화면은 '정상'이었다.
    # 임계 30분은 위 정지 경고와 같은 값을 재사용(2분 주기 기준 15회차 연속 실패).
    # ⚠️ 텔레그램 경보로 승격하지 않는다 — 알림 추가는 기획 카드 사안이고, 실제로
    # 뜨는 빈도를 여기서 먼저 관찰한 뒤 올린다.
    if last_cycle_raw and last_check_raw:
        try:
            stall = float(last_cycle_raw) - float(last_check_raw)
        except (TypeError, ValueError):
            stall = 0.0
        if stall > 30 * 60:
            warnings.append(f"⚠️ 회차는 도는데 가격 스캔이 {stall / 60:.0f}분째 정체"
                            f"(환율/업비트 조회 실패 의심) — 소급 판정창이 그만큼 벌어져 있습니다")

    # 가격체크 회차 공백 기록 (2026-07-27 기획 카드 #2 과제2) — 회차가 도는 동안엔
    # 자기 정지를 스스로 감지할 수 없어, 다음 회차가 직전 last_check_at 과의 공백을
    # 사후 기록한 값(monitor.price_check._check_price_check_gap)을 그대로 노출한다.
    # 위 last_check 경과(현재 시각 기준)와는 별개 지표 - 이건 "직전 회차가 얼마나
    # 기다렸다 깨어났는지"의 역대 이력이다.
    if _has_table(conn, "meta"):
        last_gap = db.get_meta(conn, "last_price_check_gap_min")
        max_gap = db.get_meta(conn, "max_price_check_gap_min")
    else:
        last_gap = max_gap = None
    if last_gap or max_gap:
        def _f(v):
            try:
                return float(v) if v else None
            except (TypeError, ValueError):
                return None
        parts = []
        if _f(last_gap) is not None:
            parts.append(f"직전 {_f(last_gap):.0f}분")
        if _f(max_gap) is not None:
            parts.append(f"역대 최장 {_f(max_gap):.0f}분")
        threshold = settings.get("price_check_gap_alert_minutes")
        print(f"  가격체크 회차 공백: {' · '.join(parts)} (경보 임계 {threshold:.0f}분)")

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
    # 동시터치 재검사 — '판별불가'(체결내역을 보고도 못 가름)와 '미시도'(예산 소진·
    # 기능 OFF·구간 길이 초과로 아예 못 봄)를 갈라 표시한다(2026-07-26 감사 minor).
    # 둘 다 결과는 보수적 miss 지만 처방이 다르다: 판별불가는 데이터 한계라 손댈 게
    # 없고, 미시도는 bar_magnifier_* 설정을 손보면 줄어든다. ambiguous_skipped 가
    # 없는 구세대 DB 행은 .get 기본값 0 으로 자연히 접힌다.
    amb_unresolved = sum(r.get("ambiguous_unresolved", 0) or 0 for r in recent_stats)
    amb_skipped = sum(r.get("ambiguous_skipped", 0) or 0 for r in recent_stats)
    amb_magnified = sum(r.get("ambiguous_magnified", 0) or 0 for r in recent_stats)
    if amb_unresolved or amb_skipped:
        total_amb = amb_unresolved + amb_skipped + amb_magnified
        detail = f"판별불가 {amb_unresolved}건"
        if amb_skipped:
            detail += f" · 미시도 {amb_skipped}건(예산/OFF)"
        warnings.append(f"⚠️ 최근 7일 동시터치(TP/SL 동시도달) {detail}"
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
    _cal_cur, _cal_legacy = _calibration_pair(conn)
    try:
        _r_rows = db.get_closed_r_rows(conn)
        _r_dist = distribution.r_multiple_distribution(_r_rows)
        _r_dist_by_grade = distribution.r_distribution_by_grade(_r_rows)
        _holding = distribution.holding_period_distribution(db.get_closed_holding_rows(conn))
    except sqlite3.OperationalError:
        _r_dist, _r_dist_by_grade, _holding = None, None, None
    text = telegram.render_weekly_report(
        rows_by_author, now=now, baseline=baseline,
        raw_records=raw_records, confluence=confluence,
        calibration_result=_cal_cur, calibration_legacy=_cal_legacy,
        reverse_confirmed=_reverse_confirmed(conn),
        r_distribution=_r_dist, r_distribution_by_grade=_r_dist_by_grade,
        holding_period=_holding)
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
