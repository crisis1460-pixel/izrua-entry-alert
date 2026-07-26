"""일회성 데이터 복구 — 2026-07-24 감사 수정 이전에 종결된 14건 재판정.

배경(2026-07-26 발견): 07-23 22:06 판정엔진 v1 배포 ~ 07-24 04:09 2차 감사 배포
사이에 종결된 14건은 두 버그의 영향권에서 판정됐다.
  ① 추출기 TP 서수 오인(tp=1.0 등) → "TP 이미 도달"로 가짜 hit
     (증거: outcome=hit 인데 r_multiple=-1.0 — tp_old=1 로 계산하면 정확히 재현)
  ② 소급 구간 고저 뭉개기 → 터치 이전 캔들이 판정 오염
추출기 수정 후 레벨의 sl/tp 는 재수집으로 교정됐지만 outcome 은
`WHERE outcome IS NULL` 가드 때문에 오염값이 잔존 → 허위 승률 표시로 이어짐.

복구 방식 (사용자 승인 2026-07-26):
  - 업비트 과거 1분봉 + KRW-USDT 1분봉(환율)으로 터치 시점부터 재스캔
  - 터치 자체 재검증: 원 touched_at 소급창(50분)~현재 사이 첫 entry 도달 캔들에
    재앵커링. 검증 결과 조기 오터치 2건(LINK/ICP: 기록 시점엔 +0.89%/+0.07% 위,
    실제 도달은 각 86/77분 뒤)은 실제 도달 시점부터 판정. 현재까지 한 번도
    도달이 없으면 그때만 터치 무효화(previewed 복귀 — 봇이 이어서 감시)
  - 판정 규칙은 현행 price_check._judge_outcomes 와 동일(TP1=hit / SL=miss /
    같은 캔들 동시=miss+ambiguous / 창 만료=타임박스 / R 클리핑)
  - touch_price_krw / touch_usdt_krw 백필(당시 컬럼 부재로 NULL 이던 것)
  - ret_24h / ret_72h 도 과거 캔들로 정밀 백필(환율 드리프트 보정 동일 공식)
  - 창이 안 끝났고 TP/SL 미도달이면 outcome NULL 로 되돌려 봇이 이어서 판정

사용법 (레포 루트에서):
  python scripts/repair_rejudge_20260726.py           # 드라이런 (DB 무변경)
  python scripts/repair_rejudge_20260726.py --apply   # 실제 반영
  --cache <dir> : 캔들 응답 JSON 캐시 (커밋 경합 재시도 시 재조회 생략)

멱등: 대상은 고정 id 목록이라 몇 번을 다시 실행해도 같은 결과로 수렴한다.
"""

import argparse
import bisect
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import settings  # noqa: E402
from storage import db  # noqa: E402

# 감사 수정 이전(2026-07-24 04:09 KST = ec80ca9 배포 전) 종결 전수 — 고정 목록(멱등)
TARGET_IDS = [1, 6, 7, 14, 22, 24, 26, 27, 28, 34, 39, 45, 49, 59]

_BASE = "https://api.upbit.com/v1"
_PACE = 0.12
_RETRO_SEC = 50 * 60      # 터치 재검증 소급창 (라이브 최대 45분 + 여유)
_RET_TOL_SEC = 3600       # ret 백필: 목표시각 이전 1시간 내 캔들만 인정


def _fetch_page(market: str, to_ts: float, timeout: float) -> list:
    """1분봉 200개 1페이지 (to 이전, 최신순). 실패 시 3회 재시도."""
    to_iso = datetime.fromtimestamp(to_ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for attempt in range(3):
        try:
            resp = requests.get(
                f"{_BASE}/candles/minutes/1",
                params={"market": market, "count": 200, "to": to_iso},
                timeout=timeout,
            )
            if resp.status_code == 429:
                time.sleep(1.0 + attempt)
                continue
            resp.raise_for_status()
            time.sleep(_PACE)
            return resp.json()
        except Exception as e:  # noqa: BLE001
            print(f"  [경고] {market} 캔들 조회 실패({attempt + 1}/3): {e}")
            time.sleep(1.0 + attempt)
    raise RuntimeError(f"{market} 캔들 조회 3회 실패 - 중단(부분 판정 방지)")


def fetch_candles(market: str, start_ts: float, end_ts: float, timeout: float,
                  cache_dir: Path = None) -> list:
    """[start_ts, end_ts] 1분봉 오름차순 [(start, high, low, close), ...]. 페이지네이션."""
    cache_file = None
    if cache_dir:
        cache_file = cache_dir / f"{market}_{int(start_ts)}_{int(end_ts)}.json"
        if cache_file.exists():
            return [tuple(c) for c in json.loads(cache_file.read_text())]

    out = {}
    to_ts = end_ts + 120  # 'to'는 미포함 경계 — 여유를 두고 결과에서 거른다
    while True:
        page = _fetch_page(market, to_ts, timeout)
        if not page:
            break
        starts = []
        for c in page:
            s = datetime.fromisoformat(c["candle_date_time_utc"]).replace(
                tzinfo=timezone.utc).timestamp()
            starts.append(s)
            if start_ts <= s <= end_ts:
                out[s] = (s, float(c["high_price"]), float(c["low_price"]),
                          float(c["trade_price"]))
        oldest = min(starts)
        if oldest <= start_ts:
            break
        to_ts = oldest  # 'to' 미포함이라 oldest 자체가 다음 페이지에 중복되지 않음

    candles = sorted(out.values())
    if cache_file:
        cache_file.write_text(json.dumps(candles))
    return candles


class FxSeries:
    """KRW-USDT 1분봉 종가 시계열 — fx(t) = t 이전 마지막 종가 (캐리포워드)."""

    def __init__(self, candles: list):
        self.starts = [c[0] for c in candles]
        self.closes = [c[3] for c in candles]
        if not self.starts:
            raise RuntimeError("KRW-USDT 환율 캔들이 비어 있음 - 중단")

    def at(self, t: float) -> float:
        i = bisect.bisect_right(self.starts, t) - 1
        return self.closes[max(i, 0)]


def rejudge_row(lv: dict, candles: list, fx: FxSeries, now: float, cfg_get) -> dict:
    """한 행 재판정. 반환: levels 컬럼 갱신값 dict (touch 무효면 {'void': True})."""
    entry = lv["entry_usd"]
    tp, sl = lv["tp_usd"], lv["sl_usd"]
    r_lo, r_hi = cfg_get("r_clip_low"), cfg_get("r_clip_high")
    window_sec = (lv["judgment_window_hours"] or 0) * 3600 \
        or cfg_get("outcome_window_hours") * 3600

    # ① 터치 재검증/재앵커링 — 원 touched_at 소급창 이후 첫 entry 도달 캔들.
    #    (버그 시대의 조기 오터치는 실제 도달 시점으로 옮겨 앵커 — LINK/ICP 사례.
    #     현재까지 도달이 아예 없으면 터치 무효)
    t_orig = lv["touched_at"]
    touch_candle = None
    for c in candles:
        if c[0] >= t_orig - _RETRO_SEC and c[2] <= entry * fx.at(c[0]):
            touch_candle = c
            break
    if touch_candle is None:
        return {"void": True}

    t0 = touch_candle[0]
    touched_new = t0 + 60           # 터치 캔들 종료 시각
    touch_fx = fx.at(t0)
    upd = {
        "void": False,
        "status": "touched",
        "touched_at": touched_new,
        "touch_price_krw": entry * touch_fx,
        "touch_usdt_krw": touch_fx,
        "outcome": None, "resolved_at": None, "resolve_price_krw": None,
        "r_multiple": None, "best_tp_hit": None, "ambiguous": 0,
        "judgment_mode": None, "ret_24h": None, "ret_72h": None,
    }

    mode = ("tp_sl" if (tp and sl) else "tp_only" if tp else "timeboxed")

    def _r(resolve_krw, f):
        e_krw, s_krw = entry * f, (sl or 0) * f
        if s_krw <= 0 or e_krw <= s_krw:
            return None
        return max(r_lo, min(r_hi, (resolve_krw - e_krw) / (e_krw - s_krw)))

    # ② 터치 캔들 이후를 시간순 스캔 (라이브와 동일: 터치 캔들 자체는 제외)
    for c in candles:
        if c[0] <= t0:
            continue
        f = fx.at(c[0])
        tp_krw = (tp or 0) * f
        sl_krw = (sl or 0) * f
        tp_hit = tp_krw > 0 and c[1] >= tp_krw
        sl_hit = sl_krw > 0 and c[2] <= sl_krw
        if not (tp_hit or sl_hit):
            continue
        if tp_hit and sl_hit:
            upd.update(outcome="miss", resolve_price_krw=sl_krw, ambiguous=1,
                       r_multiple=_r(sl_krw, f))
        elif tp_hit:
            upd.update(outcome="hit", resolve_price_krw=tp_krw, best_tp_hit=1,
                       r_multiple=_r(tp_krw, f))
        else:
            upd.update(outcome="miss", resolve_price_krw=sl_krw,
                       r_multiple=_r(sl_krw, f))
        upd.update(resolved_at=c[0] + 60, judgment_mode=mode)
        break

    # ③ 미종결이면 창 만료 확인 → 타임박스 (창이 열려 있으면 봇이 이어서 판정)
    if upd["outcome"] is None and now >= touched_new + window_sec:
        expiry = touched_new + window_sec
        past = [c for c in candles if c[0] <= expiry]
        if past:
            c = past[-1]
            f = fx.at(c[0])
            base_eff = entry * f  # touch_price_krw × f/touch_fx 와 동일 (환산 상쇄)
            oc = "timeboxed_win" if c[3] >= base_eff else "timeboxed_loss"
            upd.update(outcome=oc, resolve_price_krw=c[3], r_multiple=_r(c[3], f),
                       resolved_at=expiry, judgment_mode=mode)

    # ④ ret 24h/72h 백필 — 목표시각 직전 종가, 환율 드리프트 보정(= USD 공간 수익률)
    for field, hours in (("ret_24h", 24), ("ret_72h", 72)):
        target = touched_new + hours * 3600
        if target > now:
            continue
        past = [c for c in candles if c[0] <= target]
        if not past or target - past[-1][0] > _RET_TOL_SEC:
            continue
        c = past[-1]
        base_eff = entry * fx.at(c[0])
        upd[field] = (c[3] - base_eff) / base_eff * 100

    return upd


VOID_SQL = """UPDATE levels SET status='previewed', touched_at=NULL,
  touch_price_krw=NULL, touch_usdt_krw=NULL, outcome=NULL, resolved_at=NULL,
  resolve_price_krw=NULL, r_multiple=NULL, best_tp_hit=NULL, ambiguous=0,
  judgment_mode=NULL, ret_24h=NULL, ret_72h=NULL WHERE id=?"""

APPLY_SQL = """UPDATE levels SET status=?, touched_at=?, touch_price_krw=?,
  touch_usdt_krw=?, outcome=?, resolved_at=?, resolve_price_krw=?, r_multiple=?,
  best_tp_hit=?, ambiguous=?, judgment_mode=?, ret_24h=?, ret_72h=? WHERE id=?"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="DB 에 실제 반영 (기본: 드라이런)")
    ap.add_argument("--cache", help="캔들 JSON 캐시 디렉터리")
    args = ap.parse_args()

    cache_dir = None
    if args.cache:
        cache_dir = Path(args.cache)
        cache_dir.mkdir(parents=True, exist_ok=True)

    # 10분 버킷으로 내림 — 캔들 캐시 키 안정화(커밋 경합 재시도 시 재조회 생략).
    # 판정창 만료·ret 목표시각 판단에 10분 오차는 무영향(창 단위가 시간~일).
    now = int(time.time() // 600) * 600
    timeout = settings.get("http_timeout_sec")
    db.init_db(settings.get("db_path"))

    with db.connect(settings.get("db_path")) as conn:
        rows = [dict(r) for r in conn.execute(
            f"SELECT * FROM levels WHERE id IN ({','.join('?' * len(TARGET_IDS))})",
            TARGET_IDS).fetchall()]
        rows.sort(key=lambda r: r["id"])
        missing = set(TARGET_IDS) - {r["id"] for r in rows}
        if missing:
            print(f"[경고] 대상 id 미존재: {sorted(missing)}")

        span_start = min(r["touched_at"] for r in rows if r["touched_at"]) - _RETRO_SEC - 60
        print(f"[1/3] 환율(KRW-USDT) 캔들 조회 {datetime.fromtimestamp(span_start)} ~ 현재")
        fx = FxSeries(fetch_candles("KRW-USDT", span_start, now, timeout, cache_dir))

        print("[2/3] 코인별 캔들 조회 + 재판정")
        by_ticker: dict = {}
        for r in rows:
            by_ticker.setdefault(r["ticker"], []).append(r)

        results = []
        for ticker, trows in sorted(by_ticker.items()):
            t_min = min(r["touched_at"] for r in trows) - _RETRO_SEC - 60
            candles = fetch_candles(ticker, t_min, now, timeout, cache_dir)
            print(f"  {ticker}: 캔들 {len(candles)}개")
            for lv in trows:
                results.append((lv, rejudge_row(lv, candles, fx, now, settings.get)))

        results.sort(key=lambda x: x[0]["id"])
        print("\n[3/3] 재판정 결과 (기존 → 신규)")
        fmt = "{:>4} {:6} {:22} {:>28} -> {:28}"
        print(fmt.format("id", "코인", "작성자", "기존 outcome/R", "신규 outcome/R"))
        changed = 0
        for lv, upd in results:
            old = f"{lv['outcome']}/R={lv['r_multiple'] if lv['r_multiple'] is None else round(lv['r_multiple'], 2)}"
            if upd["void"]:
                new = "TOUCH VOID(previewed 복귀)"
            else:
                r_new = upd["r_multiple"] if upd["r_multiple"] is None else round(upd["r_multiple"], 2)
                new = f"{upd['outcome']}/R={r_new}"
                if upd["ambiguous"]:
                    new += " (ambiguous)"
                if upd["ret_24h"] is not None:
                    new += f" 24h={upd['ret_24h']:+.1f}%"
                if upd["ret_72h"] is not None:
                    new += f" 72h={upd['ret_72h']:+.1f}%"
                shift_min = (upd["touched_at"] - lv["touched_at"]) / 60
                if abs(shift_min) > 5:
                    new += f" 터치재앵커{shift_min:+.0f}분"
            mark = "" if (not upd["void"] and upd["outcome"] == lv["outcome"]) else "  *변경"
            if mark:
                changed += 1
            print(fmt.format(lv["id"], lv["coin_symbol"], (lv["author"] or "?")[:22], old, new) + mark)

        print(f"\noutcome 변경 {changed}건 / 대상 {len(results)}건")

        if not args.apply:
            print("드라이런 — DB 무변경. 반영하려면 --apply")
            return

        for lv, upd in results:
            if upd["void"]:
                conn.execute(VOID_SQL, (lv["id"],))
            else:
                conn.execute(APPLY_SQL, (
                    upd["status"], upd["touched_at"], upd["touch_price_krw"],
                    upd["touch_usdt_krw"], upd["outcome"], upd["resolved_at"],
                    upd["resolve_price_krw"], upd["r_multiple"], upd["best_tp_hit"],
                    upd["ambiguous"], upd["judgment_mode"], upd["ret_24h"],
                    upd["ret_72h"], lv["id"],
                ))
        conn.commit()
        print("반영 완료.")


if __name__ == "__main__":
    main()
