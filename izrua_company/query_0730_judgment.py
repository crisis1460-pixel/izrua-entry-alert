"""
2026-07-30 판정 회의용 데이터 조회 스크립트
  안건 1: 등급 R-가중치 조정 검토
  안건 2: 필터 조정 검토 (min_grade, min_rr_ratio)
  안건 3: CryptoAnalystSignal TP 초근접 예외 처리

실행: python izrua_company/query_0730_judgment.py
"""

import sys
import sqlite3

sys.stdout.reconfigure(encoding="utf-8")

DB_PATH = r"C:\Users\User\Desktop\izrua_entry_alert\data\levels.db"


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ──────────────────────────────────────────────
# 안건 1: 등급별 R-multiple 집계
# ──────────────────────────────────────────────
def run_agenda_1(conn):
    print()
    print("=" * 62)
    print("=== 안건 1: 등급 R-가중치 조정 검토 ===")
    print("=" * 62)

    rows = conn.execute("""
        SELECT
            grade,
            COUNT(*)                                           AS n,
            ROUND(AVG(r_multiple), 3)                          AS avg_r,
            ROUND(MIN(r_multiple), 3)                          AS min_r,
            ROUND(MAX(r_multiple), 3)                          AS max_r,
            SUM(CASE WHEN outcome='hit'  THEN 1 ELSE 0 END)    AS hits,
            SUM(CASE WHEN outcome='miss' THEN 1 ELSE 0 END)    AS misses,
            ROUND(
                100.0 * SUM(CASE WHEN outcome='hit' THEN 1 ELSE 0 END) / COUNT(*),
                1
            )                                                  AS hit_pct
        FROM levels
        WHERE outcome IN ('hit','miss')
          AND r_multiple IS NOT NULL
        GROUP BY grade
        ORDER BY grade
    """).fetchall()

    if not rows:
        print("표본 없음")
        return

    print(f"{'등급':^6} {'N':>5} {'avg_R':>8} {'min_R':>8} {'max_R':>8} "
          f"{'hit':>5} {'miss':>5} {'hit%':>7}")
    print("-" * 62)
    for r in rows:
        print(f"{r['grade']:^6} {r['n']:>5} {r['avg_r']:>8} {r['min_r']:>8} "
              f"{r['max_r']:>8} {r['hits']:>5} {r['misses']:>5} {r['hit_pct']:>6}%")

    total_n = sum(r['n'] for r in rows)
    total_hits = sum(r['hits'] for r in rows)
    hit_pct = round(100.0 * total_hits / total_n, 1) if total_n else 0
    print("-" * 62)
    print(f"{'전체':^6} {total_n:>5} {'':>8} {'':>8} {'':>8} "
          f"{total_hits:>5} {total_n - total_hits:>5} {hit_pct:>6}%")
    print(f"\n  총 표본: {total_n}건  (hit {total_hits}건 / miss {total_n - total_hits}건)")


# ──────────────────────────────────────────────
# 안건 2: 필터 조정 검토
# ──────────────────────────────────────────────
def run_agenda_2(conn):
    print()
    print("=" * 62)
    print("=== 안건 2: 필터 조정 검토 (min_grade, min_rr_ratio) ===")
    print("=" * 62)

    # 2-A. grade별 적중률
    print("\n[2-A] 등급별 적중률 (outcome 있는 전체 건)")
    rows_g = conn.execute("""
        SELECT
            grade,
            COUNT(*)                                           AS n,
            SUM(CASE WHEN outcome='hit' THEN 1 ELSE 0 END)     AS hits,
            ROUND(
                100.0 * SUM(CASE WHEN outcome='hit' THEN 1 ELSE 0 END) / COUNT(*),
                1
            )                                                  AS hit_pct
        FROM levels
        WHERE outcome IN ('hit','miss')
        GROUP BY grade
        ORDER BY grade
    """).fetchall()

    if not rows_g:
        print("  표본 없음")
    else:
        print(f"  {'등급':^6} {'N':>5} {'hit':>5} {'hit%':>7}")
        print("  " + "-" * 28)
        for r in rows_g:
            print(f"  {r['grade']:^6} {r['n']:>5} {r['hits']:>5} {r['hit_pct']:>6}%")

    # 2-B. RR 구간별 적중률
    print("\n[2-B] RR 구간별 적중률 (outcome 있는 전체 건)")
    rows_rr = conn.execute("""
        SELECT
            CASE
                WHEN rr IS NULL THEN 'rr=NULL'
                WHEN rr < 1     THEN '0~1'
                WHEN rr < 2     THEN '1~2'
                WHEN rr < 3     THEN '2~3'
                ELSE                 '3+'
            END                                                AS rr_band,
            COUNT(*)                                           AS n,
            SUM(CASE WHEN outcome='hit' THEN 1 ELSE 0 END)     AS hits,
            ROUND(
                100.0 * SUM(CASE WHEN outcome='hit' THEN 1 ELSE 0 END) / COUNT(*),
                1
            )                                                  AS hit_pct
        FROM levels
        WHERE outcome IN ('hit','miss')
        GROUP BY rr_band
        ORDER BY
            CASE rr_band
                WHEN 'rr=NULL' THEN 0
                WHEN '0~1'     THEN 1
                WHEN '1~2'     THEN 2
                WHEN '2~3'     THEN 3
                ELSE                4
            END
    """).fetchall()

    if not rows_rr:
        print("  표본 없음")
    else:
        print(f"  {'RR구간':^10} {'N':>5} {'hit':>5} {'hit%':>7}")
        print("  " + "-" * 32)
        for r in rows_rr:
            print(f"  {r['rr_band']:^10} {r['n']:>5} {r['hits']:>5} {r['hit_pct']:>6}%")

    # 2-C. grade × RR 교차표
    print("\n[2-C] 등급 × RR구간 교차 (N건/hit%)")
    rows_cross = conn.execute("""
        SELECT
            grade,
            CASE
                WHEN rr IS NULL THEN 'rr=NULL'
                WHEN rr < 1     THEN '0~1'
                WHEN rr < 2     THEN '1~2'
                WHEN rr < 3     THEN '2~3'
                ELSE                 '3+'
            END AS rr_band,
            COUNT(*)                                           AS n,
            SUM(CASE WHEN outcome='hit' THEN 1 ELSE 0 END)     AS hits
        FROM levels
        WHERE outcome IN ('hit','miss')
        GROUP BY grade, rr_band
        ORDER BY grade, rr_band
    """).fetchall()

    if not rows_cross:
        print("  표본 없음")
    else:
        bands = ['rr=NULL', '0~1', '1~2', '2~3', '3+']
        data, grades_seen = {}, []
        for r in rows_cross:
            g = r['grade']
            if g not in data:
                data[g] = {}
                grades_seen.append(g)
            data[g][r['rr_band']] = (r['n'], r['hits'])

        col_w = 11
        header = f"  {'등급':^6}" + "".join(f"{b:^{col_w}}" for b in bands)
        print(header)
        print("  " + "-" * (6 + col_w * len(bands)))
        for g in sorted(grades_seen):
            row_str = f"  {g:^6}"
            for b in bands:
                if b in data[g]:
                    n, h = data[g][b]
                    pct = round(100.0 * h / n, 0) if n else 0
                    cell = f"{n}건/{pct:.0f}%"
                else:
                    cell = "—"
                row_str += cell.center(col_w)
            print(row_str)

    # 2-D. suppressed_grade (meta 테이블)
    print("\n[2-D] suppressed_grade 집계 (meta 테이블)")
    try:
        rows_meta = conn.execute(
            "SELECT key, value FROM meta WHERE key LIKE '%suppressed%' OR key LIKE '%grade%'"
        ).fetchall()
        if rows_meta:
            for r in rows_meta:
                print(f"  {r['key']}: {r['value']}")
        else:
            print("  suppressed_grade 관련 메타 없음")
    except Exception as e:
        print(f"  meta 조회 오류({e}) — 생략")


# ──────────────────────────────────────────────
# 안건 3: CryptoAnalystSignal TP 초근접
# ──────────────────────────────────────────────
def run_agenda_3(conn):
    print()
    print("=" * 62)
    print("=== 안건 3: CryptoAnalystSignal TP 초근접 예외 처리 ===")
    print("=" * 62)

    rows = conn.execute("""
        SELECT
            id, coin_symbol, direction,
            entry_usd, tp_usd, rr, outcome, grade,
            CASE
                WHEN entry_usd IS NOT NULL AND tp_usd IS NOT NULL AND entry_usd != 0
                THEN ROUND((tp_usd - entry_usd) / entry_usd * 100, 3)
                ELSE NULL
            END AS tp_pct,
            datetime(collected_at, 'unixepoch', 'localtime') AS collected_dt
        FROM levels
        WHERE author = 'CryptoAnalystSignal'
          AND deleted = 0
        ORDER BY collected_at DESC
    """).fetchall()

    if not rows:
        print("표본 없음")
        return

    print(f"\n총 {len(rows)}건 (CryptoAnalystSignal, deleted=0)\n")
    hdr = (f"{'ID':>5} {'심볼':^8} {'방향':^5} {'entry':>12} {'tp':>12} "
           f"{'tp_pct%':>9} {'rr':>6} {'등급':^5} {'outcome':^8}  수집일시")
    print(hdr)
    print("-" * 90)
    for r in rows:
        tp_pct_s  = f"{r['tp_pct']:.3f}"   if r['tp_pct']   is not None else "N/A"
        entry_s   = f"{r['entry_usd']:.4f}" if r['entry_usd'] is not None else "N/A"
        tp_s      = f"{r['tp_usd']:.4f}"    if r['tp_usd']   is not None else "N/A"
        rr_s      = f"{r['rr']:.2f}"        if r['rr']       is not None else "N/A"
        outcome_s = r['outcome'] or "—"
        grade_s   = r['grade']  or "—"
        print(f"{r['id']:>5} {r['coin_symbol']:^8} {r['direction']:^5} "
              f"{entry_s:>12} {tp_s:>12} {tp_pct_s:>9} {rr_s:>6} "
              f"{grade_s:^5} {outcome_s:^8}  {r['collected_dt']}")

    # tp_pct 분포 요약
    tp_pcts = sorted(v for v in (r['tp_pct'] for r in rows) if v is not None)
    print()
    print("[TP 거리(tp_pct) 분포 요약]")
    if not tp_pcts:
        print("  tp_pct 계산 가능 건 없음")
    else:
        n = len(tp_pcts)
        avg = sum(tp_pcts) / n
        bands = [
            ("tp_pct < 1%",  sum(1 for v in tp_pcts if v < 1)),
            ("1% ~ 3%",      sum(1 for v in tp_pcts if 1 <= v < 3)),
            ("3% ~ 5%",      sum(1 for v in tp_pcts if 3 <= v < 5)),
            ("5%+",          sum(1 for v in tp_pcts if v >= 5)),
            ("음수(역방향)", sum(1 for v in tp_pcts if v < 0)),
        ]
        print(f"  표본: {n}건 | 평균: {avg:.3f}% | "
              f"최소: {min(tp_pcts):.3f}% | 중앙: {tp_pcts[n//2]:.3f}% | "
              f"최대: {max(tp_pcts):.3f}%")
        print()
        for band_name, cnt in bands:
            pct_of_n = round(100.0 * cnt / n, 1) if n else 0
            print(f"    {band_name:<16}: {cnt:>3}건 ({pct_of_n}%)")

    # outcome별 평균 tp_pct
    print()
    print("[outcome별 평균 tp_pct]")
    rows_oc = conn.execute("""
        SELECT
            COALESCE(outcome, '미확정') AS outcome,
            COUNT(*) AS n,
            ROUND(AVG(
                CASE WHEN entry_usd != 0
                THEN (tp_usd - entry_usd) / entry_usd * 100
                ELSE NULL END
            ), 3) AS avg_tp_pct
        FROM levels
        WHERE author = 'CryptoAnalystSignal'
          AND deleted = 0
          AND tp_usd IS NOT NULL AND entry_usd IS NOT NULL
        GROUP BY outcome
        ORDER BY outcome
    """).fetchall()
    if rows_oc:
        print(f"  {'outcome':^10} {'N':>5} {'avg_tp_pct':>12}")
        print("  " + "-" * 32)
        for r in rows_oc:
            avg_s = f"{r['avg_tp_pct']:.3f}%" if r['avg_tp_pct'] is not None else "N/A"
            print(f"  {r['outcome']:^10} {r['n']:>5} {avg_s:>12}")
    else:
        print("  데이터 없음")


# ──────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 62)
    print("  2026-07-30 판정 회의 데이터 조회")
    print(f"  DB: {DB_PATH}")
    print("=" * 62)

    conn = get_conn()
    try:
        run_agenda_1(conn)
        run_agenda_2(conn)
        run_agenda_3(conn)
    finally:
        conn.close()

    print()
    print("=" * 62)
    print("  조회 완료. 위 결과를 기반으로 3개 안건을 판정하세요.")
    print("=" * 62)
