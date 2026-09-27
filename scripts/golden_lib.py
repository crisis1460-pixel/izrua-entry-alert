# 실데이터 골든셋 회귀 공용 로직 (2026-09-28 승인 계획 #2).
#
# scripts/test_golden.py(검사)와 scripts/golden_update.py(스냅샷 갱신)가 같이 쓴다.
# 픽스처(scripts/golden/*.json)는 입력을 전부 담고 있어 **DB·네트워크 없이** 결정적으로
# 재실행된다. DB 는 build_*_from_db(--from-db) 에서만, 그것도 읽기 전용(mode=ro)으로 연다.
#
#  · parse_cases.json  — 운영 DB levels.raw_text 중복 제거 원문 → parse_setup 스냅샷
#                        (+ nonsetup_reason). 기준가·방향 태그 입력까지 케이스에 저장.
#  · render_cases.json — 실제 발송 터치 알림 30건의 touch_* 스냅샷 → render_alert 스냅샷.
#                        '글 N시간 전' 은 now(발송 시각)를 케이스에 저장하고 렌더 중에만
#                        telegram 모듈의 time 을 고정 시계로 바꿔 결정적으로 만든다.
#  · human            — 2026-09-27 감사 C 부록 A(사람 판독)에서 기계 판독 가능한 값만.
#                        expected(현재 코드 스냅샷)와 별개 — golden_update 는 건드리지 않는다.
import json
import math
import os
import re
import sqlite3
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import settings  # noqa: E402

# 임포트 체인이 db 경로를 읽더라도 운영 data/ 를 건드리지 않게 임시 파일로 돌린다
# (다른 test_*.py 와 같은 관례 — settings.SETTINGS["db_path"] 교체).
settings.SETTINGS["db_path"] = os.path.join(tempfile.gettempdir(), "_golden_unused.db")

GOLDEN_DIR = ROOT / "scripts" / "golden"
PARSE_PATH = GOLDEN_DIR / "parse_cases.json"
RENDER_PATH = GOLDEN_DIR / "render_cases.json"

PARSE_FIELDS = ("direction", "entry", "entry_low", "entry_high", "sl", "tp", "rr",
                "tps_all", "tp_ladder_count", "nonsetup")
HUMAN_FIELDS = ("direction", "entry", "sl", "tp")


# ── 공통 ─────────────────────────────────────────────────────────────
def load(path: Path) -> list:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save(path: Path, cases: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(cases, f, ensure_ascii=False, indent=1)
        f.write("\n")


def _norm(v):
    """JSON 왕복과 같은 모양으로 맞춘다(튜플→리스트, 정수형 float 유지)."""
    return json.loads(json.dumps(v))


def same(a, b, rel: float = 0.0) -> bool:
    """값 비교. rel>0 이면 숫자는 상대오차 허용(사람 판독표는 표기 자릿수가 다르다)."""
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) \
            and not isinstance(a, bool) and not isinstance(b, bool):
        if rel:
            return math.isclose(a, b, rel_tol=rel, abs_tol=0.0)
        return a == b
    return a == b


def short(v, width: int = 60) -> str:
    s = json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v
    s = s.replace("\n", "⏎")
    return s if len(s) <= width else s[: width - 1] + "…"


def print_table(rows: list) -> None:
    """rows: [(id, field, expected, actual)] → 간결한 before/after 표."""
    if not rows:
        return
    w_id = max(4, max(len(str(r[0])) for r in rows))
    w_f = max(5, max(len(str(r[1])) for r in rows))
    print(f"  {'id':<{w_id}}  {'field':<{w_f}}  expected → actual")
    for cid, field, exp, act in rows:
        print(f"  {cid:<{w_id}}  {field:<{w_f}}  {short(exp)} → {short(act)}")


# ── 파싱 골든 ─────────────────────────────────────────────────────────
def run_parse(case: dict):
    """케이스 입력으로 현재 extractor 를 돌려 expected 모양의 dict(또는 None)를 만든다.
    호출 형태는 수집기(run_collect._ingest_idea)와 같다 — 방향 태그가 없으면 인자 자체를
    넘기지 않는다."""
    from collector.extractor import nonsetup_reason, parse_setup
    kw = {"current_price": case.get("current_price")}
    if case.get("direction_hint") in ("long", "short"):
        kw["direction_hint"] = case["direction_hint"]
    r = parse_setup(case["text"], **kw)
    if not r:
        return None
    out = {k: r.get(k) for k in PARSE_FIELDS if k != "nonsetup"}
    out["tps_all"] = list(r.get("tps_all") or [])
    out["nonsetup"] = nonsetup_reason(case["text"], case.get("coin_symbol"))
    return _norm(out)


def diff_parse(case: dict, actual) -> list:
    exp = case.get("expected")
    cid = case["id"]
    if exp is None or actual is None:
        return [] if exp == actual else [(cid, "(setup)", exp, actual)]
    return [(cid, k, exp.get(k), actual.get(k)) for k in PARSE_FIELDS
            if not same(exp.get(k), actual.get(k))]


def human_diff(case: dict, actual) -> list:
    """사람 판독값과 다른 필드 [(field, human, actual)]. human 값이 None 인 필드는 미판독."""
    hum = case.get("human") or {}
    out = []
    for k in HUMAN_FIELDS:
        hv = hum.get(k)
        if hv is None:
            continue
        av = (actual or {}).get(k)
        if not same(hv, av, rel=1e-6):
            out.append((k, hv, av))
    return out


# ── 렌더 골든 ─────────────────────────────────────────────────────────
def run_render(case: dict) -> str:
    """저장된 입력만으로 render_alert 를 호출한다. 시간 의존부(_fresh_age_min 의
    time.time())는 케이스 now 로 고정."""
    from notify import telegram
    inp = case["inputs"]
    now = float(inp["now"])
    real_time = telegram.time
    telegram.time = types.SimpleNamespace(time=lambda: now, sleep=real_time.sleep,
                                          monotonic=real_time.monotonic)
    try:
        cluster = [dict(lv) for lv in inp["cluster"]]
        rep_idx = inp.get("rep_index")
        rep = cluster[rep_idx] if rep_idx is not None else None
        w52 = tuple(inp["week52"]) if inp.get("week52") else None
        sup = tuple(inp["supply"]) if inp.get("supply") else None
        pos = tuple(inp["position"]) if inp.get("position") else None
        return telegram.render_alert(
            inp["kind"], inp["coin_symbol"], cluster, inp["current_krw"], inp["usdt_krw"],
            sentiment=inp.get("sentiment"), week52=w52,
            kimchi_pct=inp.get("kimchi_pct"), volume_rank=inp.get("volume_rank"),
            rep=rep, funding_rate=inp.get("funding_rate"), supply=sup, position=pos,
            adx14=inp.get("adx14"), dex_stats=inp.get("dex_stats"),
            active_addr_pctile=inp.get("active_addr_pctile"),
            stwits_bullish_ratio=inp.get("stwits_bullish_ratio"),
            stwits_n=inp.get("stwits_n"), items=inp.get("items"))
    finally:
        telegram.time = real_time


def diff_render(case: dict, actual: str) -> list:
    """줄 단위 before/after [(id, 'L{n}', expected, actual)]."""
    exp = case.get("expected")
    if exp == actual:
        return []
    el = (exp or "").split("\n")
    al = (actual or "").split("\n")
    rows = []
    for i in range(max(len(el), len(al))):
        e = el[i] if i < len(el) else None
        a = al[i] if i < len(al) else None
        if e != a:
            rows.append((case["id"], f"L{i + 1}", e, a))
    return rows


# ── DB → 케이스 (golden_update --from-db 전용, 읽기 전용) ──────────────
def _ro(db_path: str):
    p = Path(db_path).resolve().as_posix()
    conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


# 감사 C 부록 A 표 한 줄: | id | 코인 | entry | tp | 판정 | 보조 | 결과 | 메모 |
_AUDIT_ROW = re.compile(r"^\|\s*(\d+)\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|"
                        r"\s*([^|]+?)\s*\|")


def load_human_audit(path) -> dict:
    """감사 C(2026-09-27) 부록 A 판정표 → {level_id: human dict}.

    기계 판독 가능한 것만 옮긴다:
      · '정상'        → entry·tp(표기 '-' 면 미판독=None)·direction=long (sl 은 표에 없음)
      · '오해-방향'    → direction=short (나머지 값은 사람이 적지 않았다)
      · 그 밖의 오해·애매·판독불가 → 올바른 값이 표에 없어 verdict 만 남긴다(비교 안 함)."""
    out = {}
    if not path or not Path(path).exists():
        return out
    in_appx = False
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.startswith("## 부록 A"):
            in_appx = True
            continue
        if in_appx and line.startswith("## "):
            break
        if not in_appx:
            continue
        m = _AUDIT_ROW.match(line)
        if not m:
            continue
        lid, _coin, e_s, tp_s, verdict = m.groups()
        if verdict.startswith("판독불가"):
            continue
        h = {"verdict": verdict, "direction": None, "entry": None, "sl": None, "tp": None,
             "src": "audit_2026-09-27_C 부록A"}
        if verdict == "정상":
            h["direction"] = "long"
            try:
                h["entry"] = float(e_s)
            except ValueError:
                pass
            if tp_s not in ("-", ""):
                try:
                    h["tp"] = float(tp_s)
                except ValueError:
                    pass
        elif verdict.startswith("오해-방향"):
            h["direction"] = "short"
        out[int(lid)] = h
    return out


def build_parse_from_db(db_path: str, human_path=None) -> list:
    """levels.raw_text 중복 제거(첫 id 기준) → 파싱 케이스 입력.

    기준가: 저장 entry_usd — 운영 재파싱(storage.db.reparse_all)과 같은 방식.
    수집 당시 CoinGecko 현재가는 DB 에 없고, entry 기준이면 sanity(±60%/−90%)가 항상
    통과해 '파서 자체'의 추출값을 본다.
    방향 태그: TradingView 방향 메타는 DB 에 저장되지 않는다(reparse_all 과 같은 한계) →
    None(텍스트 판정). 필드는 남겨 두어 태그가 저장되기 시작하면 채울 수 있게 한다."""
    human = load_human_audit(human_path) if human_path else {}
    conn = _ro(db_path)
    rows = conn.execute(
        "SELECT id, raw_text, entry_usd, coin_symbol, source FROM levels "
        "WHERE raw_text IS NOT NULL AND raw_text != '' ORDER BY id").fetchall()
    by_text = {}
    for r in rows:
        by_text.setdefault(r["raw_text"], []).append(r)
    cases = []
    for text, grp in by_text.items():
        first = grp[0]
        ids = [g["id"] for g in grp]
        case = {
            "id": f"p{first['id']}",
            "level_ids": ids,
            "source": first["source"] or "tradingview",
            "coin_symbol": first["coin_symbol"],
            "text": text,
            "current_price": first["entry_usd"],
            "direction_hint": None,
            "expected": None,
        }
        hum = next((human[i] for i in ids if i in human), None)
        if hum:
            case["human"] = hum
        cases.append(case)
    conn.close()
    cases.sort(key=lambda c: int(c["id"][1:]))
    return cases


_LEVEL_KEYS = ("id", "coin_symbol", "entry_usd", "sl_usd", "tp_usd", "tps_usd",
               "tp_ladder_count", "score", "grade", "mcap_tier_icon", "mcap_rank",
               "post_age_minutes", "collected_at", "author", "author_followers",
               "author_hit_rate", "author_hit_count", "source", "post_url")


def _split_verdict(v):
    if not v:
        return None
    lab, _, reason = v.partition("|")
    return [lab, reason or None]


def _render_inputs(conn, alert) -> dict:
    ids = [int(x) for x in str(alert["level_ids"]).split(",") if x.strip()]
    lvs = [conn.execute("SELECT * FROM levels WHERE id=?", (i,)).fetchone() for i in ids]
    lvs = [lv for lv in lvs if lv is not None]
    if not lvs:
        return None
    lvs.sort(key=lambda lv: -(lv["entry_usd"] or 0))   # render_alert 계약: entry 내림차순
    snap = max(lvs, key=lambda lv: lv["touch_price_krw"] is not None)
    if snap["touch_price_krw"] is None or not snap["touch_usdt_krw"]:
        return None
    cluster = [{k: lv[k] for k in _LEVEL_KEYS} for lv in lvs]
    rep_i = max(range(len(cluster)), key=lambda i: cluster[i].get("score") or 0)
    sentiment = None
    if snap["touch_btc_dominance"] is not None or snap["touch_fear_greed"] is not None:
        sentiment = {"btc_dominance": snap["touch_btc_dominance"],
                     "fear_greed": (int(snap["touch_fear_greed"])
                                    if snap["touch_fear_greed"] is not None else None)}
    dex = None
    if snap["touch_dex_liquidity_usd"] is not None or snap["touch_dex_buy_ratio"] is not None:
        dex = {"liquidity_usd": snap["touch_dex_liquidity_usd"],
               "buy_ratio_24h": snap["touch_dex_buy_ratio"]}
    warn = snap["touch_upbit_warning"]
    social = None
    if snap["touch_stwits_bullish_ratio"] is not None:
        social = {"bull_ratio": snap["touch_stwits_bullish_ratio"],
                  "n": snap["touch_stwits_n"], "source": "stocktwits"}
    return {
        "alert_id": alert["id"],
        "kind": "touch",
        "coin_symbol": alert["coin_symbol"],
        "now": alert["sent_at"],
        "current_krw": snap["touch_price_krw"],
        "usdt_krw": snap["touch_usdt_krw"],
        "cluster": cluster,
        "rep_index": rep_i,
        "sentiment": sentiment,
        "week52": None,
        "week52_synthetic": False,
        "kimchi_pct": snap["touch_kimchi_pct"],
        "volume_rank": snap["touch_volume_rank"],
        "funding_rate": snap["touch_funding_rate"],
        "supply": _split_verdict(snap["touch_supply_verdict"]),
        "position": _split_verdict(snap["touch_position_verdict"]),
        "adx14": snap["touch_adx14"],
        "dex_stats": dex,
        "active_addr_pctile": snap["touch_active_addr_pctile"],
        "stwits_bullish_ratio": snap["touch_stwits_bullish_ratio"],
        "stwits_n": snap["touch_stwits_n"],
        "items": {
            "alt_breadth": snap["touch_alt_breadth"],
            "rvol_d20": snap["touch_rvol_d20"],
            "low30_pct": snap["touch_low30_pct"],
            "upbit_warning": [w for w in warn.split(",") if w] if warn else None,
            "post_move_pct": snap["touch_post_move_pct"],
            "social": social,
        },
    }


def build_render_from_db(db_path: str, n: int = 30) -> list:
    """발송된 터치 알림(alerts_log kind='touch' sent=1)에서 n 건을 다양성 기준으로 고른다.

    우선순위: ① 다중 레벨 클러스터(범위 표기) 전부 ② 업비트 주의 꼬리표 ③ 가격대
    버킷(<0.01$, <1$, <100$, ≥100$)별 서로 다른 코인 순환. 52주 고저는 DB 에 스냅샷이
    없어(렌더 시점 조회값) 3건 중 1건에 **합성값**(현재가 기준 고정 배수, 1년 미만 상장
    표기 포함)을 넣어 그 분기만 덮는다 — week52_synthetic=True 로 표시."""
    conn = _ro(db_path)
    alerts = conn.execute("SELECT * FROM alerts_log WHERE kind='touch' AND sent=1 "
                          "ORDER BY id").fetchall()
    built = []
    for a in alerts:
        inp = _render_inputs(conn, a)
        if inp:
            built.append(inp)
    conn.close()

    def bucket(inp):
        usd = inp["current_krw"] / inp["usdt_krw"]
        return 0 if usd < 0.01 else 1 if usd < 1 else 2 if usd < 100 else 3

    picked, seen_ids = [], set()

    def take(inp):
        if inp["alert_id"] not in seen_ids and len(picked) < n:
            picked.append(inp)
            seen_ids.add(inp["alert_id"])

    for inp in built:
        if len(inp["cluster"]) > 1:
            take(inp)
    for inp in built:
        if inp["items"]["upbit_warning"]:
            take(inp)
    seen_coins = {p["coin_symbol"] for p in picked}
    pools = {b: [i for i in built if bucket(i) == b] for b in range(4)}
    while len(picked) < n and any(pools.values()):
        for b in range(4):
            pool = pools[b]
            while pool:
                inp = pool.pop(0)
                if inp["coin_symbol"] not in seen_coins and inp["alert_id"] not in seen_ids:
                    take(inp)
                    seen_coins.add(inp["coin_symbol"])
                    break
        if all(not p for p in pools.values()):
            break
    if len(picked) < n:   # 코인 중복 허용으로 채움
        for inp in reversed(built):
            take(inp)
    picked.sort(key=lambda i: i["alert_id"])

    cases = []
    for k, inp in enumerate(picked):
        if k % 3 == 0:
            cur = inp["current_krw"]
            nweeks = 30 if k % 2 else 52   # 상장후 / 52주 두 표기 모두
            inp["week52"] = [round(cur * 2.35, 6), round(cur * 0.62, 6), nweeks]
            inp["week52_synthetic"] = True
        # 알림 항목 v3(09-27 배포) 스냅샷 컬럼(touch_rvol_d20·low30·upbit_warning·
        # alt_breadth·stwits_n)은 빌드 시점 운영 DB 에 값이 0건이라, 3건 중 1건에
        # 고정 합성값을 넣어 v3 줄 렌더 분기를 덮는다 — items_synthetic=True 로 표시.
        # (실측 값이 쌓이면 --from-db 재구성 때 실제 값이 우선한다.)
        inp["items_synthetic"] = False
        if k % 3 == 1 and not any(v is not None for v in inp["items"].values()
                                  if v is not inp["items"].get("social")):
            j = k // 3
            inp["items"].update({
                "rvol_d20": (0.7, 1.24, 1.9, 3.35)[j % 4],
                "low30_pct": (2.4, 9.6, 22.0, -3.1)[j % 4],
                "alt_breadth": (35.0, 55.0, 72.0, 100.0)[j % 4],
                "upbit_warning": ([None, ["TRADING_VOLUME_SOARING"],
                                   ["DEPOSIT_AMOUNT_SOARING", "PRICE_FLUCTUATIONS"],
                                   None])[j % 4],
                "post_move_pct": (1.0, 12.0, -7.0, 4.0)[j % 4],
                "social": {"bull_ratio": 0.42, "n": 25, "source": "stocktwits"}
                if j % 2 == 0 else inp["items"].get("social"),
            })
            inp["items_synthetic"] = True
        cases.append({"id": f"r{inp['alert_id']}", "inputs": inp, "expected": None})
    return cases
