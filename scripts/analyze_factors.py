"""축적 팩터 유효성 분석 — Tier2·MTF·언락·초기반응 IC (2026-08-21 고도화 B1-B3).

## 왜 필요한가
2026-08-13~16 스프린트들이 터치 시점 스냅샷 컬럼을 대거 기록하기 시작했는데
(atr/btc_regime/dvol/mtf_score/token_unlock_pct/ret_4h/12h ...) 지금까지 전부
write-only — 기록만 되고 아무 분석에도 안 읽혔다. 이 스크립트가 세 질문에 답한다:
  · B1 — BTC 레짐(200일선 상/하)·DVOL·ATR 구간별로 신호 승률이 달라지는가?
  · B2 — 등급 점수는 몇 시간 만에 실현되는가? (score vs ret_4h/12h/24h/72h IC 비교)
  · B3 — MTF 정렬점수·토큰언락 경고가 실제 승률과 상관 있는가?
     → 유의미하면 장래 수급보정 입력 승격 판단 근거, 무의미하면 API 콜 절감 근거.

## 원칙 (scripts/analyze_touch_quality.py 와 동일)
- **읽기 전용**: DB 는 mode=ro URI 로만 연다. 쓰기 경로 없음.
- **표기 전용**: 콘솔 출력뿐 — 등급 산식·알림·필터 어디에도 연결되지 않는다.
- Wilson 80% 단측 하한(z=1.28) 병기, 그룹 n>=20 부터만 판정 문구.

## 사용법
  python scripts/analyze_factors.py                 # 기본 DB (settings)
  python scripts/analyze_factors.py --db path/to.db # 테스트용 DB 지정
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

from analytics.calibration import wilson_interval          # noqa: E402
from analytics.signal_quality import _spearman_rank_corr   # noqa: E402

WIN_OUTCOMES = ("hit", "timeboxed_win")
CLOSED_OUTCOMES = ("hit", "miss", "timeboxed_win", "timeboxed_loss")

Z = 1.28
MIN_GROUP_N = 20   # 판정 문구 게이트 — analyze_touch_quality 관례와 동일
MIN_IC_N = 5       # Spearman 자체 최소 표본 (signal_quality 관례)


def connect_ro(db_path) -> sqlite3.Connection:
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def default_db_path() -> Path:
    from config import settings
    p = Path(settings.get("db_path"))
    return p if p.is_absolute() else ROOT / p


def load_rows(conn) -> list:
    ph = ",".join("?" * len(CLOSED_OUTCOMES))
    try:
        rows = conn.execute(
            f"""SELECT outcome, score, ret_4h, ret_12h, ret_24h, ret_72h,
                       touch_atr_pct, touch_btc_regime, touch_dvol,
                       touch_mtf_score, touch_token_unlock_pct
                FROM levels
                WHERE outcome IN ({ph}) AND touched_at IS NOT NULL""",
            CLOSED_OUTCOMES).fetchall()
    except sqlite3.OperationalError as e:
        print(f"스키마 미지원 DB (컬럼 누락): {e}")
        return []
    return [dict(r) for r in rows]


def group_stats(rows: list) -> dict:
    n = len(rows)
    wins = sum(1 for r in rows if r["outcome"] in WIN_OUTCOMES)
    lo, _ = wilson_interval(wins, n, z=Z)
    med = (statistics.median([r["ret_24h"] for r in rows if r["ret_24h"] is not None])
           if any(r["ret_24h"] is not None for r in rows) else None)
    return {"n": n, "wins": wins, "rate": (wins / n) if n else None,
            "wilson_lo": lo, "med_ret": med}


def _fmt_group(label: str, st: dict) -> str:
    if not st["n"]:
        return f"  {label:<14} 표본 없음"
    rate = f"{st['rate']*100:.0f}%" if st["rate"] is not None else "-"
    lo = f"{st['wilson_lo']*100:.0f}%" if st["wilson_lo"] is not None else "-"
    med = f"{st['med_ret']:+.1f}%" if st["med_ret"] is not None else "-"
    gate = "" if st["n"] >= MIN_GROUP_N else "  (표본 부족)"
    return (f"  {label:<14} n={st['n']:<4} 승률 {rate:<5} (W-LB {lo:<5}) "
            f"중앙 ret24 {med}{gate}")


def _bucket(rows, key, edges, labels):
    """수치 컬럼을 경계값 리스트로 버킷팅. NULL 은 제외."""
    out = {lb: [] for lb in labels}
    for r in rows:
        v = r.get(key)
        if v is None:
            continue
        for i, edge in enumerate(edges):
            if v < edge:
                out[labels[i]].append(r)
                break
        else:
            out[labels[-1]].append(r)
    return out


def section_b1(rows: list) -> None:
    print("\n── B1. 레짐·변동성 교차 승률 ─────────────────────")
    # BTC 레짐 (200일선 상/하)
    above = [r for r in rows if r.get("touch_btc_regime") == "above"]
    below = [r for r in rows if r.get("touch_btc_regime") == "below"]
    print("[BTC 200일선 레짐]")
    print(_fmt_group("위(above)", group_stats(above)))
    print(_fmt_group("아래(below)", group_stats(below)))
    # DVOL 구간 (Deribit 30일 내재변동성 — options.py 경계 관례 40/60/80)
    print("[DVOL 내재변동성]")
    for lb, grp in _bucket(rows, "touch_dvol", (40, 60, 80),
                           ("<40 평상", "40~60", "60~80 경계", "80+ 위기")).items():
        print(_fmt_group(lb, group_stats(grp)))
    # ATR% 구간 (일봉 Wilder ATR20 % — 저/중/고 변동 코인)
    print("[코인 ATR(20)%]")
    for lb, grp in _bucket(rows, "touch_atr_pct", (3.0, 6.0),
                           ("<3% 저변동", "3~6%", ">6% 고변동")).items():
        print(_fmt_group(lb, group_stats(grp)))


def section_b2(rows: list) -> None:
    print("\n── B2. 초기반응 IC (score vs ret_*) ──────────────")
    print("  점수가 실현되는 시간축 비교 — IC 가 최고인 구간이 신뢰 시점")
    for col in ("ret_4h", "ret_12h", "ret_24h", "ret_72h"):
        xs, ys = [], []
        for r in rows:
            if r.get("score") is not None and r.get(col) is not None:
                xs.append(float(r["score"]))
                ys.append(float(r[col]))
        ic = _spearman_rank_corr(xs, ys) if len(xs) >= MIN_IC_N else None
        ic_s = f"{ic:+.4f}" if ic is not None else "  표본 부족"
        print(f"  {col:<8} n={len(xs):<4} IC {ic_s}")


def section_b3(rows: list) -> None:
    print("\n── B3. MTF 정렬·토큰언락 유효성 ──────────────────")
    print("[MTF 정렬점수 (-2~+2)]")
    neg = [r for r in rows if (r.get("touch_mtf_score") or 0) < 0
           and r.get("touch_mtf_score") is not None]
    zero = [r for r in rows if r.get("touch_mtf_score") == 0]
    pos = [r for r in rows if (r.get("touch_mtf_score") or 0) > 0]
    print(_fmt_group("약세(<0)", group_stats(neg)))
    print(_fmt_group("혼조(0)", group_stats(zero)))
    print(_fmt_group("강세(>0)", group_stats(pos)))
    print("[토큰언락 경고 (7일 내 5%+ 유통량)]")
    warned = [r for r in rows if r.get("touch_token_unlock_pct") is not None]
    clean = [r for r in rows if r.get("touch_token_unlock_pct") is None]
    print(_fmt_group("경고 있음", group_stats(warned)))
    print(_fmt_group("경고 없음", group_stats(clean)))
    if len(warned) >= MIN_GROUP_N and len(clean) >= MIN_GROUP_N:
        w, c = group_stats(warned), group_stats(clean)
        if w["wilson_lo"] is not None and c["rate"] is not None:
            verdict = ("언락 경고 코인이 유의하게 약함 → 수급보정 승격 검토 근거"
                       if (w["rate"] or 0) + 0.10 < (c["rate"] or 0)
                       else "유의한 차이 없음 → 현행 기록 전용 유지")
            print(f"  판정: {verdict}")
    else:
        print(f"  판정 유보 — 양 그룹 n>={MIN_GROUP_N} 필요")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None)
    args = ap.parse_args(argv)
    db_path = Path(args.db) if args.db else default_db_path()
    if not db_path.exists():
        print(f"DB 없음: {db_path}")
        return 1
    conn = connect_ro(db_path)
    try:
        rows = load_rows(conn)
    finally:
        conn.close()
    print(f"축적 팩터 유효성 분석 — 종결 표본 {len(rows)}건 (읽기 전용)")
    if not rows:
        return 0
    section_b1(rows)
    section_b2(rows)
    section_b3(rows)
    print("\n※ 표기 전용 — 등급 산식·알림·필터에 미연결. Tier2 컬럼은 08-16 이후")
    print("  표본만 존재하므로 초기에는 '표본 부족'이 정상이다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
