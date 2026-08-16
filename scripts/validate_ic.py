"""정제(purged) 시간분할 IC 검증 하네스 (2026-08-15, 내부 분석 전용 · 읽기 전용).

## 왜 필요한가
주간 감사의 인샘플 IC(전체 종결 표본에서 score↔ret_24h Spearman, 예: 0.207)는
판정창이 서로 겹치는 표본(168h 창이 시간축에서 중첩)을 한 덩어리로 재기 때문에
부풀려질 수 있다. 표준 처방은 López de Prado 의 purged K-fold + embargo:
  · 시간순으로 K등분해 각 폴드를 테스트로 쓰고,
  · 테스트 구간과 판정창이 겹칠 수 있는 훈련 행(테스트 범위 ±168h)을 제거(purge),
  · 테스트 구간 '이후' 168h 를 추가로 비워(embargo) 정보 누출을 차단한다.
IC 자체는 적합(fit)이 없어 폴드별 테스트 IC = "그 시기 표본만의 순수 IC"이고,
폴드 간 편차가 곧 시간 안정성이다. purge/embargo 는 훈련측 표본 수를 함께
보고해 "겹침 제거 후에도 남는 표본"이 얼마나 되는지 감사 흔적을 남긴다.

## 원칙
- **읽기 전용**: 운영 DB(data/levels.db)는 mode=ro URI 로만 연다. 쓰기 경로 없음.
- **표기 전용**: 결과는 콘솔 출력뿐 — 등급 산식·알림·필터 어디에도 연결되지 않는다.
- 순수 표준 라이브러리 + analytics.signal_quality(프로젝트 모듈 import 0 원칙이라
  순환 위험 없음)의 _spearman_rank_corr 재사용 — 계산 축을 주간 감사와 일치시킨다.

## 사용법
  python scripts/validate_ic.py                     # score 기준 (기본)
  python scripts/validate_ic.py --feature touch_cvd_ratio   # 임의 수치 컬럼 IC 검증
  python scripts/validate_ic.py --db path/to.db     # 테스트용 DB 지정 (기본: settings)
"""

import argparse
import math
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001 - 리다이렉트 환경 등에서는 그대로 진행
    pass

from analytics.signal_quality import CLOSED_OUTCOMES, _spearman_rank_corr  # noqa: E402

_KST = timezone(timedelta(hours=9))

# 판정창 겹침 제거 폭 — 최장 판정창(judgment_window_hours 상한) 168h 와 동일 축
PURGE_HOURS = 168.0
EMBARGO_HOURS = 168.0
K_FOLDS = 3
MIN_ROWS = 30          # 이 미만이면 폴드당 표본이 너무 얇아 검증 자체가 무의미
REF_INSAMPLE_IC = 0.2072  # 비교 문구용 참고값 (2026-08 주간 감사 인샘플 IC)


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


def load_rows(conn, feature: str = "score") -> list:
    """종결 표본 로드 — [{"t", "x", "ret", "grade_ver"}, ...] touched_at 오름차순.

    feature 는 levels 의 실제 컬럼명과 대조해 검증한다(임의 문자열 SQL 주입 차단).
    숫자로 강제 변환 불가한 값은 조용히 제외 — --feature 로 TEXT 컬럼을 잘못
    지정하면 표본 0건 → 상위에서 부족 메시지로 귀결된다."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(levels)").fetchall()}
    if feature not in cols:
        raise SystemExit(f"[오류] levels 에 '{feature}' 컬럼이 없습니다. "
                         f"--feature 는 실제 컬럼명이어야 합니다.")
    ph = ",".join("?" * len(CLOSED_OUTCOMES))
    rows = conn.execute(
        f"""SELECT touched_at AS t, "{feature}" AS x, ret_24h AS ret, grade_ver
            FROM levels
            WHERE outcome IN ({ph}) AND touched_at IS NOT NULL
              AND "{feature}" IS NOT NULL AND ret_24h IS NOT NULL
            ORDER BY touched_at""",
        CLOSED_OUTCOMES).fetchall()
    out = []
    for r in rows:
        try:
            out.append({"t": float(r["t"]), "x": float(r["x"]),
                        "ret": float(r["ret"]), "grade_ver": r["grade_ver"]})
        except (TypeError, ValueError):
            continue  # 숫자 아님 — IC 대상에서 제외
    return out


def purged_time_split(rows, k: int = K_FOLDS,
                      purge_hours: float = PURGE_HOURS,
                      embargo_hours: float = EMBARGO_HOURS) -> list:
    """시간순 K 폴드 + purge/embargo.

    rows 는 touched_at 오름차순 전제. 각 폴드 dict:
      {"fold": 1기준 번호, "test": 행 목록, "train": purge 후 훈련 행 목록,
       "t_start"/"t_end": 테스트 구간 경계(epoch)}

    purge: 훈련측에서 touched_at ∈ [t_start - purge, t_end + purge] 제거 —
           168h 판정창이 테스트 구간과 겹칠 수 있는 모든 행.
    embargo: 테스트 구간 '이후'로 embargo_hours 를 추가 제외 — 테스트 표본의
           판정 결과가 시차를 두고 스며드는 잔류 누출 차단(표준 관행)."""
    n = len(rows)
    sizes = [n // k + (1 if i < n % k else 0) for i in range(k)]
    folds, start = [], 0
    purge_s = purge_hours * 3600.0
    embargo_s = embargo_hours * 3600.0
    for f, size in enumerate(sizes):
        a, b = start, start + size
        start = b
        test = rows[a:b]
        if not test:
            continue
        t_start, t_end = test[0]["t"], test[-1]["t"]
        lo = t_start - purge_s
        hi = t_end + purge_s + embargo_s
        train = [r for i, r in enumerate(rows)
                 if not (a <= i < b) and not (lo <= r["t"] <= hi)]
        folds.append({"fold": f + 1, "test": test, "train": train,
                      "t_start": t_start, "t_end": t_end})
    return folds


def evaluate_folds(rows, k: int = K_FOLDS) -> dict:
    """폴드별 테스트 IC + 인샘플 IC. 반환:
      {"insample_ic", "n", "folds": [{fold, n_test, n_train, ic, significant,
                                      t_start, t_end}], "mean_test_ic"}"""
    insample = _spearman_rank_corr([r["x"] for r in rows], [r["ret"] for r in rows])
    result = {"insample_ic": insample, "n": len(rows), "folds": [],
              "mean_test_ic": None}
    for f in purged_time_split(rows, k=k):
        xs = [r["x"] for r in f["test"]]
        ys = [r["ret"] for r in f["test"]]
        ic = _spearman_rank_corr(xs, ys)   # n<5 → None (signal_quality 관례)
        n_test = len(xs)
        # 유의 판정 휴리스틱: |IC| >= 2/sqrt(n) (순위상관 표준오차 ≈ 1/sqrt(n) 근사)
        sig = (ic is not None and n_test > 0
               and abs(ic) >= 2.0 / math.sqrt(n_test))
        result["folds"].append({
            "fold": f["fold"], "n_test": n_test, "n_train": len(f["train"]),
            "ic": ic, "significant": sig,
            "t_start": f["t_start"], "t_end": f["t_end"],
        })
    valid = [f["ic"] for f in result["folds"] if f["ic"] is not None]
    if valid:
        result["mean_test_ic"] = sum(valid) / len(valid)
    return result


def _d(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=_KST).strftime("%Y-%m-%d")


def render_report(res: dict, feature: str, label: str = "전체",
                  flag_small: bool = False) -> str:
    """폴드 표 + 요약을 한국어 텍스트로. flag_small=True 면 n<30 폴드에 참고용 표시."""
    lines = [f"── {label} 표본 (feature={feature}, n={res['n']}) ──"]
    for f in res["folds"]:
        ic_s = f"{f['ic']:+.4f}" if f["ic"] is not None else "N/A(n<5)"
        thr = 2.0 / math.sqrt(f["n_test"]) if f["n_test"] else float("inf")
        sig_s = ("유의" if f["significant"] else f"비유의(기준 {thr:.3f})") \
            if f["ic"] is not None else "판정불가"
        note = " [참고용: n<30]" if (flag_small and f["n_test"] < 30) else ""
        lines.append(
            f"  폴드{f['fold']}: {_d(f['t_start'])}~{_d(f['t_end'])} | "
            f"테스트 IC {ic_s} (n={f['n_test']}) | "
            f"purge 후 훈련 n={f['n_train']} | {sig_s}{note}")
    ins = res["insample_ic"]
    mean = res["mean_test_ic"]
    ins_s = f"{ins:+.4f}" if ins is not None else "N/A"
    if mean is None:
        lines.append(f"  요약: 유효 폴드 없음 (인샘플 IC {ins_s})")
    else:
        # 유지/하락 휴리스틱: 시간분할 평균 IC 가 실전 유효 관례선(0.05) 이상이고
        # 인샘플 대비 낙폭이 0.10 이내면 '유지', 아니면 '하락'
        held = mean >= 0.05 and (ins is None or (ins - mean) <= 0.10)
        verdict = "유지" if held else "하락"
        lines.append(f"  요약: 인샘플 IC {ins_s} → 시간분할 평균 테스트 IC "
                     f"{mean:+.4f} ({verdict})")
        if feature == "score":
            lines.append(f"  비교: 기존 주간감사 인샘플 IC {REF_INSAMPLE_IC:.4f} 대비 "
                         f"{'유지' if mean >= REF_INSAMPLE_IC - 0.10 and mean >= 0.05 else '하락'} "
                         f"(겹치는 168h 판정창 제거 후 순수 시기별 IC 기준)")
        else:
            # 참고값 {REF_INSAMPLE_IC} 는 score 전용(주간감사 인샘플 IC)이라 다른
            # --feature 와 견주면 축이 다른 수치를 비교하는 셈 — 중립 표기만 남긴다.
            lines.append(f"  비교: {feature} 평균 테스트 IC {mean:+.4f} — "
                         f"기준 IC 비교 생략(참고값 {REF_INSAMPLE_IC:.4f} 는 score 전용)")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="정제(purged) 시간분할 IC 검증 — 읽기 전용 내부 분석")
    ap.add_argument("--feature", default="score",
                    help="IC 를 잴 levels 수치 컬럼 (기본: score)")
    ap.add_argument("--db", default=None,
                    help="DB 경로 (기본: config.settings 의 db_path)")
    args = ap.parse_args(argv)

    db_path = Path(args.db) if args.db else default_db_path()
    if not db_path.exists():
        print(f"[오류] DB 없음: {db_path}")
        return 1

    conn = connect_ro(db_path)
    try:
        rows = load_rows(conn, feature=args.feature)
    finally:
        conn.close()

    print(f"정제 시간분할 IC 검증 — {args.feature} vs ret_24h "
          f"(폴드 {K_FOLDS}개, purge ±{PURGE_HOURS:.0f}h, embargo {EMBARGO_HOURS:.0f}h)")
    print(f"DB: {db_path} (읽기 전용)")

    if len(rows) < MIN_ROWS:
        print(f"\n종결 표본 {len(rows)}건 < 최소 {MIN_ROWS}건 — 폴드당 표본이 너무 "
              f"얇아 검증을 건너뜁니다. 표본이 더 쌓인 뒤 다시 실행하세요.")
        return 0

    res_all = evaluate_folds(rows)
    print()
    print(render_report(res_all, args.feature, label="전체"))

    # v4 산식 표본만 별도 표 (2026-08-03 배포분) — 폴드 n<30 은 참고용 표시
    v4_rows = [r for r in rows if r["grade_ver"] == "v4"]
    print()
    if len(v4_rows) < MIN_ROWS:
        print(f"── v4 전용 표본 ── {len(v4_rows)}건 < {MIN_ROWS}건 — 생략(표본 부족)")
    else:
        res_v4 = evaluate_folds(v4_rows)
        print(render_report(res_v4, args.feature, label="grade_ver=v4",
                            flag_small=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
