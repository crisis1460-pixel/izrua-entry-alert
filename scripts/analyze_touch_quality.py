"""터치 품질 분석 — 꼬리 스침 vs 종가 이탈 (2026-08-15 Tier2 #12, 내부 분석 전용).

## 왜 필요한가
"첫 터치에 바로 잡느냐, 캔들 종가 이탈(재확인)까지 기다리느냐"는 진입 타이밍의
고전 논쟁인데 공개 백테스트가 없다(research_2026-08-15_alert_quality Q1). Tier1
스프린트(2026-08-15)가 터치 캔들의 종가 이탈 여부(touch_closed_below)와 관통
깊이(touch_penetration_pct)를 기록하기 시작했으므로, 우리 데이터로 직접 판정한다:
  · 그룹 비교 — 꼬리 스침(closed_below=0) vs 종가 이탈(=1)의 승률·수익률·MFE/MAE
  · 관통 깊이 3구간(<0.5% / 0.5~1.5% / >1.5%) 별 같은 통계
승률 비교는 소표본 방어를 위해 Wilson 80% 단측 하한(z=1.28, 프로젝트 관례)을
병기하고, 판정 문구는 양 그룹 n>=20 부터만 낸다.

## 원칙 (scripts/validate_ic.py 와 동일)
- **읽기 전용**: DB 는 mode=ro URI 로만 연다. 쓰기 경로 없음.
- **표기 전용**: 결과는 콘솔 출력뿐 — 등급 산식·알림·필터 어디에도 연결되지 않는다.
- 순수 표준 라이브러리 + analytics.calibration.wilson_interval(프로젝트 정본) 재사용.

## 사용법
  python scripts/analyze_touch_quality.py                 # 기본 DB (settings)
  python scripts/analyze_touch_quality.py --db path/to.db # 테스트용 DB 지정
"""

import argparse
import sqlite3
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001 - 리다이렉트 환경 등에서는 그대로 진행
    pass

from analytics.calibration import wilson_interval  # noqa: E402

# 판정축 — analytics/ranking.py 의 승/패 정의와 동일 (timeboxed_win 은 승).
WIN_OUTCOMES = ("hit", "timeboxed_win")
CLOSED_OUTCOMES = ("hit", "miss", "timeboxed_win", "timeboxed_loss")

Z = 1.28               # 80% 단측 하한 — rank_z·작성자 실적 게이트와 동일 관례
MIN_GROUP_N = 20       # 이 미만이면 그룹 비교 판정 유보 (표본 부족 문구)

# 관통 깊이 버킷 경계(%) — 경계값 0.5/1.5 는 가운데 구간에 포함
BUCKET_LABELS = ("<0.5%", "0.5~1.5%", ">1.5%")


def connect_ro(db_path) -> sqlite3.Connection:
    """운영 DB 보호 — 읽기 전용 URI 로만 연다 (쓰기 시도는 sqlite 가 즉시 거부)."""
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def default_db_path() -> Path:
    from config import settings
    p = Path(settings.get("db_path"))
    return p if p.is_absolute() else ROOT / p


def load_rows(conn) -> list:
    """터치 품질이 기록된 종결 표본만 —
    [{"closed_below", "pen", "outcome", "ret_24h", "mfe_pct", "mae_pct"}, ...].

    touch_closed_below 는 2026-08-15 이후 기록 시작이라 NULL(구세대·현재가 단독
    감지 터치)은 표본이 아니다. 컬럼 자체가 없는 구세대 DB 는 OperationalError
    → 호출부에서 표본 0건으로 접는다."""
    ph = ",".join("?" * len(CLOSED_OUTCOMES))
    try:
        rows = conn.execute(
            f"""SELECT touch_closed_below AS closed_below,
                       touch_penetration_pct AS pen,
                       outcome, ret_24h, mfe_pct, mae_pct
                FROM levels
                WHERE outcome IN ({ph}) AND touched_at IS NOT NULL
                  AND touch_closed_below IS NOT NULL""",
            CLOSED_OUTCOMES).fetchall()
    except sqlite3.OperationalError:
        return []
    return [dict(r) for r in rows]


def penetration_bucket(pen):
    """관통 깊이(%) → 버킷 라벨. NULL(구 4-튜플 캔들 등)은 버킷 없음(None)."""
    if pen is None:
        return None
    if pen < 0.5:
        return BUCKET_LABELS[0]
    if pen <= 1.5:
        return BUCKET_LABELS[1]
    return BUCKET_LABELS[2]


def _median(rows: list, key: str):
    """key 값이 있는 행만의 중앙값. 전무하면 None."""
    vals = [r[key] for r in rows if r.get(key) is not None]
    return statistics.median(vals) if vals else None


def group_stats(rows: list) -> dict:
    """행 묶음 → {n, wins, rate, wilson_lo, med_ret_24h, med_mfe, med_mae}."""
    n = len(rows)
    wins = sum(1 for r in rows if r["outcome"] in WIN_OUTCOMES)
    lo, _ = wilson_interval(wins, n, z=Z)
    return {
        "n": n,
        "wins": wins,
        "rate": (wins / n) if n else None,
        "wilson_lo": lo,
        "med_ret_24h": _median(rows, "ret_24h"),
        "med_mfe": _median(rows, "mfe_pct"),
        "med_mae": _median(rows, "mae_pct"),
    }


def split_groups(rows: list) -> dict:
    """{"wick": 꼬리 스침(closed_below=0) 행, "closed": 종가 이탈(=1) 행}."""
    return {
        "wick": [r for r in rows if not r["closed_below"]],
        "closed": [r for r in rows if r["closed_below"]],
    }


def _fmt(v, suffix: str = "") -> str:
    return f"{v:+.2f}{suffix}" if v is not None else "-"


def _stat_line(label: str, s: dict) -> str:
    if not s["n"]:
        return f"  {label:<18} n=0"
    return (f"  {label:<18} n={s['n']:<4} 승률 {s['rate'] * 100:.0f}%"
            f"({s['wins']}/{s['n']}) · Wilson LB {s['wilson_lo']:.2f}"
            f" · 중앙값 ret24h {_fmt(s['med_ret_24h'], '%')}"
            f" / MFE {_fmt(s['med_mfe'], '%')} / MAE {_fmt(s['med_mae'], '%')}")


def render_report(rows: list) -> str:
    """전체 리포트 텍스트. rows = load_rows 결과."""
    lines = [f"종결 + 터치 품질 기록 표본: {len(rows)}건 "
             f"(touch_closed_below 기록 시작 2026-08-15 이후 터치분)"]

    groups = split_groups(rows)
    wick = group_stats(groups["wick"])
    closed = group_stats(groups["closed"])
    lines.append("")
    lines.append("── 그룹 비교: 꼬리 스침 vs 종가 이탈 ──")
    lines.append(_stat_line("꼬리 스침(0)", wick))
    lines.append(_stat_line("종가 이탈(1)", closed))

    lines.append("")
    lines.append("── 관통 깊이 3구간 (touch_penetration_pct) ──")
    by_bucket = {b: [] for b in BUCKET_LABELS}
    skipped = 0
    for r in rows:
        b = penetration_bucket(r["pen"])
        if b is None:
            skipped += 1
            continue
        by_bucket[b].append(r)
    for b in BUCKET_LABELS:
        lines.append(_stat_line(b, group_stats(by_bucket[b])))
    if skipped:
        lines.append(f"  (깊이 미기록 {skipped}건 — 구 4-튜플 캔들/현재가 단독 감지분, 구간 제외)")

    lines.append("")
    if wick["n"] >= MIN_GROUP_N and closed["n"] >= MIN_GROUP_N:
        # 양 그룹 표본 충족 — 승률 축으로만 판정(수익률 중앙값은 참고 병기).
        # LB 끼리 비교: 소표본 운빨 그룹이 이기는 걸 Wilson 이 자동 벌점한다.
        diff = (closed["rate"] - wick["rate"]) * 100
        if closed["wilson_lo"] > wick["rate"]:
            verdict = "종가 이탈(재확인 대기) 우위 — 확인 후 진입이 유리했다"
        elif wick["wilson_lo"] > closed["rate"]:
            verdict = "꼬리 스침(첫 터치) 우위 — 재확인 대기는 손해였다"
        else:
            verdict = "차이 불명확 (신뢰구간 겹침) — 표본 추가 축적 후 재판정"
        lines.append(f"판정: {verdict} (승률차 {diff:+.1f}%p, "
                     f"LB {wick['wilson_lo']:.2f} vs {closed['wilson_lo']:.2f})")
        lines.append("(내부 분석 전용 — 알림·필터·등급 미반영. 실행 결정은 별도 기획 카드로)")
    else:
        lines.append(f"표본 부족 — 수집 시작 2026-08-16, 판정 대기 "
                     f"(필요 n={MIN_GROUP_N}/그룹, 현재 꼬리 {wick['n']}·종가 {closed['n']})")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="터치 품질 분석(꼬리 스침 vs 종가 이탈) — 읽기 전용 내부 분석")
    ap.add_argument("--db", default=None,
                    help="DB 경로 (기본: config.settings 의 db_path)")
    args = ap.parse_args(argv)

    db_path = Path(args.db) if args.db else default_db_path()
    if not db_path.exists():
        print(f"[오류] DB 없음: {db_path}")
        return 1

    conn = connect_ro(db_path)
    try:
        rows = load_rows(conn)
    finally:
        conn.close()

    print(f"터치 품질 분석 — 첫 터치 vs 종가 이탈 재확인 "
          f"(Wilson 80% 단측 하한, z={Z})")
    print(f"DB: {db_path} (읽기 전용)")
    print()
    print(render_report(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
