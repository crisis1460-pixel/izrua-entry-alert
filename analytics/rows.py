"""분석 단일 입구 `analysis_rows()` + 데이터 사전 (2026-09-28, 개발운영방식 최종안 #3).

근거: izrua_company/plan_2026-09-27_개발운영방식_최종안.md §3 — 과거 분석이 틀린
원인은 즉석 조회마다 필터를 빠뜨린 것이었다:
  - 즉시터치 오염 표본 혼입(승률 4.3% 표본이 섞여 등급·작성자 통계를 끌어내림)
  - judgment_mode 혼합(tp_only 는 SL 이 없어 miss 가 구조적으로 없고 r_multiple 이
    NULL — tp_sl 과 한 표에 섞으면 승률·평균 R 이 착시가 된다, r +0.226→−0.028)
  - MFE/MAE 무효 구간(2026-08-14 도입 ~ 2026-09-22 수리, 39일) 값 사용
  - best_tp_hit 의미 변화(09-27 이전엔 'hit' 종결 행에만 = 사다리 완주 단계)

그래서 **새 분석은 이 함수로 행을 받고**, 표 아래에 `describe_filters()` 한 줄을
찍는다. 기본값이 곧 "안전한 표본"이다 — 무언가를 포함하려면 명시적으로 켜야 한다.

설계 메모
  - 기존 analytics/*(calibration·weekly·ranking·distribution)는 **건드리지 않는다**.
    이 모듈은 새 분석 전용 입구다.
  - analytics 의 다른 모듈은 "프로젝트 import 0" 원칙이지만, 이 모듈은 **조회 계층**
    이라 오염 조건 문자열을 storage.db 에서 그대로 import 한다(SQL 사본을 두면
    두 곳이 어긋나는 순간 같은 버그가 재발한다).
  - 원천 보존: 행을 수정·삭제하지 않는다. 읽기만 한다(conn 의 row_factory 도 안 바꾼다).
  - ⚠️ 기본값 차이: 기존 운영 조회(weekly·calibration)는 deleted(글 삭제) 행을
    **빼지 않는다**. 여기서는 기본 제외다(include_deleted=True 로 기존과 맞출 수 있다).
    운영 DB 삭제 확정은 4건(2026-09-28)이라 수치 영향은 미미하다.
"""

import sqlite3
from datetime import date, datetime, timedelta, timezone

from storage.db import NOT_STALE, STALE_TOUCH_COND

_KST = timezone(timedelta(hours=9))

# outcome 정의 — analytics/calibration.py·weekly.py 와 같은 값(사본이 아니라 대조용:
# 테스트가 세 모듈의 값 일치를 확인한다).
CLOSED_OUTCOMES = ("hit", "miss", "timeboxed_win", "timeboxed_loss")
WIN_OUTCOMES = ("hit", "timeboxed_win")
JUDGMENT_MODES = ("tp_sl", "tp_only", "timeboxed")

# 기간 필터로 쓸 수 있는 시각 컬럼 (SQL 에 문자열로 들어가므로 화이트리스트)
TIME_COLS = ("touched_at", "collected_at", "resolved_at")

# columns 를 좁혀도 파생 필드 계산에 필요해서 항상 함께 조회하는 컬럼
_REQUIRED_COLS = ("id", "status", "outcome", "judgment_mode", "best_tp_hit",
                  "collected_at", "touched_at", "resolved_at",
                  "mfe_pct", "mae_pct", "touch_mfe_atr_ratio")
# MFE 무효 구간에서 None 으로 비우는 컬럼
_MFE_COLS = ("mfe_pct", "mae_pct", "touch_mfe_atr_ratio")

MFE_META_KEY = "mfe_mae_fixed_since"


# ── 데이터 사전 ──────────────────────────────────────────────────────────────
# 생존표 날짜는 **2026-09-28 실측**(운영 DB 사본 data/levels.db 를 읽기전용으로 열어
# 컬럼별 첫 non-NULL 행의 collected_at(수집 시점 컬럼) / touched_at(터치·판정 컬럼)
# 을 KST 로 읽은 값). DB 전체: 레벨 901행, 첫 수집 2026-07-23, 터치 561행.
# 코드에 박제된 날짜라 이후 행이 늘어도 바뀌지 않는다 — "언제부터 믿을 수 있나"용.
COLUMNS = {
    "entry_usd / sl_usd / tp_usd": {
        "뜻": "글에서 추출한 진입가·손절가·1차 목표가(USD). 다단 TP 는 tps_usd(JSON)"
              "·tp_ladder_count 에 사다리 전체",
        "단위": "USD",
        "생존표": "07-23 수집분부터(DB 시작). entry 901/901, sl 456/901, tp 693/901 "
                  "— SL·TP 미기재 글이 많다(NULL = 글에 없음)",
    },
    "status": {
        "뜻": "레벨 생애주기: watching → previewed → touched | expired",
        "단위": "TEXT",
        "생존표": "전 행(NOT NULL). 실측 분포 touched 566 · expired 288 · watching 42 · "
                  "previewed 5. touched 인데 touched_at NULL = 섀도 터치(5행)",
    },
    "outcome": {
        "뜻": "종결 판정: hit(최종 TP 도달) | miss(SL) | timeboxed_win | timeboxed_loss"
              "(판정창 만료 시점 손익 부호). NULL = 미종결",
        "단위": "TEXT",
        "생존표": "07-23 터치분부터. 불변 스냅샷(해시 체인) — 소급 수정 없음",
    },
    "best_tp_hit": {
        "뜻": "실제 도달한 최고 TP 단계(1-based). hit 이면 최종 단계, "
              "miss/timeboxed 면 중간에 닿은 단계(없으면 NULL)",
        "단위": "INTEGER(단계)",
        "생존표": "⚠️ 의미 변화: 09-27 이전엔 outcome='hit' 행에만 기록 = '사다리 "
                  "완주 단계'. 09-27 S1 에 miss/timeboxed 중간 도달 62행을 "
                  "tp_alert_idx 로 1회 백필(meta.backfill_best_tp_hit_v1) + 이후 신규 "
                  "종결은 직접 기록 → 지금은 전 구간 '도달 최고 단계'로 읽어도 된다",
    },
    "r_multiple": {
        "뜻": "(청산가-진입가)/(진입가-SL). [-1,+5] 클리핑",
        "단위": "R",
        "생존표": "07-23부터. SL 이 있어야 계산 → judgment_mode tp_sl 205/205, "
                  "timeboxed 26/103, tp_only 0/191. tp_only 는 **항상 NULL**(평균 R 에 "
                  "섞으면 tp_sl 만의 값이 된다)",
    },
    "judgment_mode": {
        "뜻": "판정 방식: tp_sl(TP·SL 둘 다) | tp_only(SL 없음 → miss 구조적 불가, "
              "만료로만 짐) | timeboxed(TP 없음 → hit 불가). **층화 필수 키**",
        "단위": "TEXT",
        "생존표": "07-23 종결분부터 outcome 과 함께 전 행(499/499)",
    },
    "ret_4h / ret_12h / ret_24h / ret_72h": {
        "뜻": "터치가(touch_price_krw) 대비 N시간 뒤 수익률, 방향 반영. 도과 시 1회 기록",
        "단위": "%",
        "생존표": "ret_24h·ret_72h 07-23 터치분부터, ret_12h 08-13부터, ret_4h 08-14부터",
    },
    "mfe_pct / mae_pct": {
        "뜻": "터치 후 최대유리·최대불리 이동폭(누적 단조 MAX/MIN)",
        "단위": "%",
        "생존표": "⚠️ 값은 07-23 터치분부터 있으나 **touched_at < meta.mfe_mae_fixed_since"
                  "(2026-09-22 08:04:16 KST) 인 행은 무효**(회차마다 0 초기화 버그 — "
                  "08-14 도입~09-22 수리 39일, 승리 건 98.3% mae=0). analysis_rows 가 "
                  "mfe_valid=False 로 표시하고 기본으로 None 마스킹. "
                  "touch_mfe_atr_ratio(08-16~)도 같은 구간 무효",
    },
    "touch_stale": {
        "뜻": "즉시터치 오염 플래그(1=오염). 관통 NULL 이라 관통 규칙으로 못 잡는 "
              "즉시터치를 1분봉 복원으로 확인한 행",
        "단위": "0/1",
        "생존표": "09-27 1회 백필 12행만 1(meta.backfill_touch_stale_v1), 나머지 NULL. "
                  "배제 조건은 storage.db.STALE_TOUCH_COND(= touch_stale=1 OR "
                  "(관통>10% AND 터치-수집<600초)) — 직접 쓰지 말고 이 모듈로",
    },
    "touch_penetration_pct": {
        "뜻": "터치 캔들이 진입가를 관통한 깊이",
        "단위": "%",
        "생존표": "08-16 터치분부터(295/561). NULL(구세대·억제·백필 대기)은 오염 아님으로 "
                  "읽는다(COALESCE fail-open)",
    },
    "armed / armed_at": {
        "뜻": "무장 게이트: 0=대기(수집 시 이미 진입가 아래), 1=진입가 위 확인 후 무장. "
              "armed_at = 무장 시각(epoch), 터치 판정 캔들 하한",
        "단위": "0/1, epoch",
        "생존표": "09-27 10:48 KST(meta.arming_since) 배포 후 그때 살아 있던 레벨"
                  "(수집 09-20~)부터. 그 이전 행은 NULL(소급 없음). 실측 armed=1 54, 0 4",
    },
    "grade / score / grade_ver": {
        "뜻": "수집 시점 등급(S~D)·점수와 그 산식 버전",
        "단위": "TEXT / 점 / TEXT",
        "생존표": "grade·score 07-23부터 전 행. grade_ver 는 07-26 수집분부터(이전 166행 "
                  "NULL). 버전 시작 meta: v3 08-01 07:04, v5 08-16 22:40, v6 09-22 08:04 "
                  "(KST). **버전 간 등급은 비교 불가** — grade_ver 로 자를 것",
    },
    "touch_grade / touch_score / touch_grade_ver": {
        "뜻": "터치 시점 재채점 등급·점수·산식 버전(v5+). grade 와 다른 축",
        "단위": "TEXT / 점 / TEXT",
        "생존표": "08-16 터치분부터(약 307/561)",
    },
    "tp_alert_idx": {
        "뜻": "TP 단계 알림 진행 인덱스(= 도달한 TP 단계 수, 0=미도달). "
              "outcome 과 무관하게 실제 도달 단계를 든다",
        "단위": "INTEGER",
        "생존표": "07-27 컬럼 추가(DEFAULT 0), 첫 비0 은 07-29 터치분",
    },
    "touch_* 스냅샷(시장·수급 컨텍스트)": {
        "뜻": "터치 순간 1회 기록한 외부 지표. 최초 기록 우선(덮어쓰기 없음). "
              "억제 터치·API 실패면 NULL",
        "단위": "컬럼별",
        "생존표": "첫 터치일(KST): 07-23 touch_price_krw·touch_usdt_krw / 07-27 "
                  "bid_ask_ratio / 07-30 fear_greed·btc_dominance·volume_rank / 08-01 "
                  "kimchi_pct / 08-08 supply_verdict·position_verdict·ma200_above / 08-11 "
                  "cvd_ratio / 08-13 funding_rate·stablecoin_mcap_b / 08-14 oi_pct / 08-15 "
                  "mtf_score / 08-16 closed_below·tp_usd·atr_pct·btc_regime·dvol·"
                  "post_age_hours / 08-17 atr_band_pct·supply_1h·adx14·bb_width_pctile·"
                  "dex_*·active_addr_pctile·stwits_bullish_ratio / 09-22 message_id·"
                  "reaction. **0행(미가동)**: token_unlock_pct·rvol_d20·low30_pct·"
                  "upbit_warning·post_move_pct·alt_breadth·stwits_n. **1행뿐**: "
                  "long_short_ratio·top_trader_ratio·taker_buy_sell_ratio",
    },
    "deleted": {
        "뜻": "원문 글 삭제 확정(404) = 1",
        "단위": "0/1",
        "생존표": "07-26부터 확인. 실측 4행",
    },
}

# 파생 필드 설명 (analysis_rows 가 각 행 dict 에 붙인다)
DERIVED = {
    "is_stale": "STALE_TOUCH_COND 해당 여부(bool). include_stale=True 일 때만 True 행이 나온다",
    "mfe_valid": "touched_at >= meta.mfe_mae_fixed_since. meta 없거나 미터치면 False",
    "hit_tp1": "outcome == 'hit' — analytics/calibration.py HIT_OUTCOMES 와 **같은 정의**"
               "(timeboxed_win 은 목표가를 찍은 게 아니라 제외). ⚠️ 다단 사다리에선 "
               "'최종 TP 완주'를 뜻한다 — TP1 만 찍고 진 건은 reached_tp1 을 볼 것",
    "reached_tp1": "outcome == 'hit' 또는 best_tp_hit >= 1 — 사다리 중간 도달 포함",
    "win": "outcome in (hit, timeboxed_win) — analytics/weekly.py WIN_OUTCOMES 와 같은 정의",
    "stratum": "judgment_mode (NULL 이면 '(미종결)') — stratify() 기본 키",
}


# ── 내부 도우미 ──────────────────────────────────────────────────────────────
def _to_epoch(v):
    """epoch(int/float) · datetime · date · 'YYYY-MM-DD[ HH:MM]'(KST) → epoch float."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, datetime):
        return (v if v.tzinfo else v.replace(tzinfo=_KST)).timestamp()
    if isinstance(v, date):
        return datetime(v.year, v.month, v.day, tzinfo=_KST).timestamp()
    s = str(v).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=_KST).timestamp()
        except ValueError:
            continue
    raise ValueError(f"since/until 형식 오류: {v!r}")


def _fmt_kst(ts, with_time=False):
    d = datetime.fromtimestamp(float(ts), _KST)
    if with_time or (d.hour, d.minute, d.second) != (0, 0, 0):
        return d.strftime("%Y-%m-%d %H:%M")
    return d.strftime("%Y-%m-%d")


def _as_tuple(v):
    if v is None:
        return None
    if isinstance(v, str):
        return (v,)
    return tuple(v)


def _fetch_mfe_fixed_since(conn):
    """meta.mfe_mae_fixed_since(epoch). 없거나 깨졌으면 None(= 전 구간 무효로 본다).
    conn.row_factory 를 건드리지 않게 튜플 인덱스로 읽는다."""
    try:
        row = conn.execute("SELECT value FROM meta WHERE key=?", (MFE_META_KEY,)).fetchone()
    except sqlite3.OperationalError:
        return None
    try:
        return float(row[0]) if row and row[0] else None
    except (TypeError, ValueError):
        return None


def _table_columns(conn):
    return [r[1] for r in conn.execute("PRAGMA table_info(levels)").fetchall()]


# ── 공개 API ────────────────────────────────────────────────────────────────
def analysis_rows(conn, *, since=None, until=None, time_col="touched_at",
                  grade_ver=None, judgment_mode=None, closed_only=True,
                  statuses=None, include_stale=False, include_deleted=False,
                  require_valid_mfe=False, mask_invalid_mfe=True, columns=None):
    """levels 한 행 = dict 하나. 기본값이 곧 '안전한 분석 표본'이다.

    기본 표본(인자 생략 시):
      - 종결 + 실터치: outcome ∈ CLOSED_OUTCOMES AND touched_at NOT NULL
        (weekly·calibration 과 같은 모집단 축). closed_only=False 면 전 행.
      - 즉시터치 오염 제외: storage.db.NOT_STALE (include_stale=True 로 포함)
      - 삭제글 제외: COALESCE(deleted,0)=0 (include_deleted=True 로 포함)
      - MFE 무효 구간은 행을 남기되 mfe_pct/mae_pct/touch_mfe_atr_ratio 를 None 으로
        비운다(mask_invalid_mfe). require_valid_mfe=True 면 그 행 자체를 뺀다.

    인자:
      since/until  — 반열린 구간 [since, until). epoch·datetime·'YYYY-MM-DD'(KST 자정).
      time_col     — 기간 기준 컬럼: touched_at(기본) | collected_at | resolved_at.
      grade_ver    — 'v6' 또는 ('v5','v6'). 수집 시점 산식 버전(grade_ver 컬럼).
      judgment_mode— 'tp_sl' 등 또는 튜플. 섞어서 쓸 거면 반드시 stratify() 할 것.
      statuses     — status 추가 필터(예: ('touched',)). 기본 None = 제한 없음.
      columns      — 조회할 컬럼 목록(None = 전 컬럼). 파생 계산용 필수 컬럼은
                     항상 함께 조회된다. 존재하지 않는 컬럼은 ValueError.

    반환 행에는 DERIVED 의 파생 필드(is_stale·mfe_valid·hit_tp1·reached_tp1·win·
    stratum)가 붙는다. 정렬: time_col 오름차순, 그다음 id."""
    if time_col not in TIME_COLS:
        raise ValueError(f"time_col 은 {TIME_COLS} 중 하나: {time_col!r}")

    existing = _table_columns(conn)
    if columns is None:
        sel_cols = existing
    else:
        unknown = [c for c in columns if c not in existing]
        if unknown:
            raise ValueError(f"levels 에 없는 컬럼: {unknown}")
        sel_cols = list(dict.fromkeys(list(_REQUIRED_COLS) + list(columns)))
    sel_cols = [c for c in sel_cols if c in existing]

    where, params = [], []
    if closed_only:
        where.append("outcome IN (%s)" % ",".join("?" * len(CLOSED_OUTCOMES)))
        params.extend(CLOSED_OUTCOMES)
        where.append("touched_at IS NOT NULL")
    if not include_stale:
        where.append(NOT_STALE)
    if not include_deleted:
        where.append("COALESCE(deleted, 0) = 0")
    s_ep, u_ep = _to_epoch(since), _to_epoch(until)
    if s_ep is not None:
        where.append(f"{time_col} >= ?")
        params.append(s_ep)
    if u_ep is not None:
        where.append(f"{time_col} < ?")
        params.append(u_ep)
    for col, val in (("grade_ver", grade_ver), ("judgment_mode", judgment_mode),
                     ("status", statuses)):
        vals = _as_tuple(val)
        if vals is not None:
            where.append(f"{col} IN (%s)" % ",".join("?" * len(vals)))
            params.extend(vals)

    fixed_since = _fetch_mfe_fixed_since(conn)
    if require_valid_mfe:
        if fixed_since is None:
            return []     # 수리 시점을 모르면 유효 표본도 없다
        where.append("touched_at >= ?")
        params.append(fixed_since)

    sql = ("SELECT " + ", ".join(sel_cols)
           + f", CASE WHEN {STALE_TOUCH_COND} THEN 1 ELSE 0 END AS _is_stale"
           + " FROM levels"
           + ((" WHERE " + " AND ".join(where)) if where else "")
           + f" ORDER BY {time_col} IS NULL, {time_col}, id")
    cur = conn.cursor()
    cur.row_factory = sqlite3.Row     # 호출부 conn 의 row_factory 는 그대로 둔다
    out = []
    for r in cur.execute(sql, params).fetchall():
        d = {k: r[k] for k in sel_cols}
        d["is_stale"] = bool(r["_is_stale"])
        t = d.get("touched_at")
        d["mfe_valid"] = bool(fixed_since is not None and t is not None and t >= fixed_since)
        if mask_invalid_mfe and not d["mfe_valid"]:
            for c in _MFE_COLS:
                if c in d:
                    d[c] = None
        oc = d.get("outcome")
        d["hit_tp1"] = oc == "hit"
        d["reached_tp1"] = oc == "hit" or (d.get("best_tp_hit") or 0) >= 1
        d["win"] = oc in WIN_OUTCOMES
        d["stratum"] = d.get("judgment_mode") or "(미종결)"
        out.append(d)
    return out


def stratify(rows, key="judgment_mode"):
    """rows → {층 값: [행...]}. key 는 컬럼명 또는 callable(row)->값.
    judgment_mode 층은 JUDGMENT_MODES 순서로 먼저, 나머지(NULL 은 '(미종결)')는 뒤에."""
    groups = {}
    for r in rows:
        k = key(r) if callable(key) else r.get(key)
        if k is None:
            k = "(미종결)"
        groups.setdefault(k, []).append(r)
    if key == "judgment_mode":
        order = [m for m in JUDGMENT_MODES if m in groups]
        order += [k for k in groups if k not in JUDGMENT_MODES]
        return {k: groups[k] for k in order}
    return groups


def describe_filters(n=None, *, since=None, until=None, time_col="touched_at",
                     grade_ver=None, judgment_mode=None, closed_only=True,
                     statuses=None, include_stale=False, include_deleted=False,
                     require_valid_mfe=False, mask_invalid_mfe=True, columns=None,
                     mfe_fixed_since=None):
    """분석 표 아래에 찍을 한 줄 요약 — 표본·기간·대상·층·제외 조건.
    analysis_rows 와 같은 키워드를 받으므로 `describe_filters(len(rows), **kw)` 로 쓴다.
    mfe_fixed_since 를 주면 MFE 절단 시각을 함께 적는다(columns 는 무시)."""
    parts = [f"표본 n={n}" if n is not None else "표본"]
    s_ep, u_ep = _to_epoch(since), _to_epoch(until)
    if s_ep is None and u_ep is None:
        parts.append(f"기간 전체({time_col})")
    else:
        a = _fmt_kst(s_ep) if s_ep is not None else "처음"
        b = (_fmt_kst(u_ep) + " 미만") if u_ep is not None else "현재"
        parts.append(f"기간 {a} ~ {b}({time_col}, KST)")
    parts.append("대상 종결·실터치" if closed_only else "대상 전 행(미종결 포함)")
    for label, val in (("judgment_mode", judgment_mode), ("grade_ver", grade_ver),
                       ("status", statuses)):
        vals = _as_tuple(val)
        if vals is not None:
            parts.append(f"{label}={'/'.join(vals)}")
    excl = []
    if not include_stale:
        excl.append("오염터치(NOT_STALE)")
    if not include_deleted:
        excl.append("삭제글")
    parts.append("제외 " + "·".join(excl) if excl else "제외 없음(오염·삭제 포함)")
    cut = f"<{_fmt_kst(mfe_fixed_since, True)}" if mfe_fixed_since else "수리 전"
    if require_valid_mfe:
        parts.append(f"MFE 유효 터치만({cut} 제외)")
    elif mask_invalid_mfe:
        parts.append(f"MFE 무효구간({cut}) 값 비움")
    else:
        parts.append("⚠️ MFE 무효구간 값 포함")
    return " · ".join(parts)
