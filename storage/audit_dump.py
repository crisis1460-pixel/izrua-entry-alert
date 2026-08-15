"""
주간 감사 덤프 (2026-07-27 기획 카드 #4).

## 왜 필요한가
운영 상태는 data/levels.db (SQLite 바이너리) 하나에 있고 2분마다 레포에 커밋백된다.
바이너리는 git diff 가 불가능해서, 커밋 이력이 수천 개 쌓여 있어도 "이 레벨의
등급/상태가 언제·왜 바뀌었나"를 사후에 되짚을 방법이 없다 — 백업은 되는데
읽을 수가 없는 상태였다.

## 무엇을 하나
주 1회, levels/daily_stats 전체를 "한 줄 = 한 행"(ndjson) 텍스트로 data/audit/ 에
떨군다. 같은 잡(price-check.yml)이 `git add data/` 로 커밋하므로 워크플로를 손대지
않아도 DB 와 **같은 커밋**에 실린다. 이후엔 주차 파일 두 개를 diff 하면 사람이(그리고
git 이) 변화를 그대로 읽을 수 있다.

## 형식 — simonw/sqlite-diffable 참고, 라이브러리는 추가하지 않는다(json 표준 모듈만)
sqlite-diffable 의 핵심 아이디어("행을 텍스트 한 줄로 떨궈 git 이 diff 할 수 있게
만든다")만 가져오되, 행 표현은 배열이 아니라 **객체**로 한다:
  · 이 스키마는 ALTER TABLE ADD COLUMN 으로 계속 자란다(db._OUTCOME_COLUMNS). 위치
    기반 배열이면 컬럼이 추가된 주차와 그 이전 주차의 같은 위치가 다른 의미가 돼
    비교가 조용히 어긋난다. 객체는 자기기술적이라 그 사고가 구조적으로 불가능하다.
  · 주차별로 파일이 갈리므로(levels_2026-W30 vs _2026-W31) 애초에 "같은 파일의 라인
    diff"가 아니라 "두 파일 비교"가 기본 사용법이다 — 배열의 크기 이점보다
    `"grade": "C"` 가 눈에 그대로 보이는 쪽이 목적(사후 리뷰)에 맞는다.
첫 줄은 메타 한 줄(`_table`/`_columns`/`_week_kst`/`_rows`, 밑줄 접두사로 데이터 행과
구분), 이후 한 줄에 한 행. 정렬은 고정(levels=id, daily_stats=day_kst)이라 같은 상태면
항상 같은 바이트가 나온다.

## 원문(raw_text) 정책 — 과제 2 와의 결합
levels.raw_text 는 DB 용량의 큰 몫을 차지하는데 소비처는 재파싱 자가치유
(db.reparse_all) 하나뿐이고 그건 활성(watching/previewed) 행만 본다 → 종결 행의
원문은 런타임에 영영 안 쓰인다. 그래서 **덤프에는 포함하고(원문 보존), 덤프가
성공한 뒤에 런타임 DB 에서만 비운다(용량 회수)**. 순서가 곧 안전장치다:
  덤프 → (덤프 성공 시에만) raw_text 정리 → 오래된 덤프 파일 정리
덤프에서 원문을 빼는 설정(audit_dump_include_raw_text=False)을 쓰면 정리도 함께
멈춘다 — 아카이브되지 않은 원문을 지우는 경로는 만들지 않는다.

## 실패 격리 / 보존
- 어떤 실패도 회차를 죽이지 않는다(호출부 db.init_db 가 통째로 삼킨다).
- 최근 audit_dump_keep_weeks 주차분만 남기고 오래된 덤프 파일은 지운다 — 레포
  무한 증가 방지. 지워도 그 파일이 실렸던 과거 커밋에는 영구히 남는다(= 아카이브).
"""

import json
import logging
import math
import os
import re
import time
from pathlib import Path

logger = logging.getLogger("alert.audit_dump")

# 주기 판정 meta 키 — run_cycle 의 last_collect_at / last_weekly_report_at 과 동일 패턴
META_LAST_DUMP = "last_audit_dump_at"
META_LAST_DUMP_FAIL = "last_audit_dump_fail_at"

# 덤프 대상 테이블과 결정적 정렬 키.
# alerts_log/author_snapshots 를 뺀 이유: 카드 #4 의 목적은 "등급/상태 변화의 사후
# 리뷰"이고 그 원천은 levels 다. daily_stats 는 그 변화의 집계 맥락(억제 사유)이라
# 같이 봐야 의미가 있어 포함한다. 나머지는 파생·로그라 레포 용량만 늘린다.
TABLES = ("levels", "daily_stats")
_ORDER_BY = {"levels": "id", "daily_stats": "day_kst"}

# levels_2026-W30.ndjson / daily_stats_2026-W30.ndjson
_FILE_RE = re.compile(r"^(?:%s)_(\d{4}-W\d{2})\.ndjson$" % "|".join(TABLES))

# 읽기 전용 잡(scripts/run_weekly_report.py)이 자기 실행 동안 자동 훅을 끄는 스위치.
# 그 잡은 DB 를 커밋백하지 않으므로, 거기서 덤프가 돌면 주기 meta 만 앞당겨져
# 정작 라이터 회차가 그 주 덤프를 건너뛴다(= 그 주가 통째로 비는 사고).
SUPPRESSED = False

# settings 에 키가 없어도(구버전 설정·외부 테스트) 동작하도록 기본값을 여기 둔다.
_DEFAULTS = {
    "audit_dump_enabled": True,
    "audit_dump_interval_hours": 168,    # 주 1회
    "audit_dump_retry_minutes": 60,      # 실패 시 백오프 (2분 회차마다 재시도 방지)
    "audit_dump_keep_weeks": 8,          # 작업본에 남길 주차 수 (과거분은 git 이력에 영구 보존)
    "audit_dump_include_raw_text": True, # 원문 아카이브 (아래 raw_text 정리의 전제)
    "audit_raw_text_keep_days": 14,      # 종결 후 이 기간이 지난 행의 원문을 DB 에서 비움
}


def _cfg(key):
    try:
        from config import settings
        # 2026-07-28 수리: settings.get() 이 예외 없이 None 을 반환하는 경우도 _DEFAULTS 로 폴백.
        # None 이 그대로 내려가면 prune_raw_text(keep_days=None) → TypeError 로 raw_text 정리 비활성화.
        v = settings.get(key)
        return v if v is not None else _DEFAULTS[key]
    except Exception:  # noqa: BLE001 - 설정 부재는 기본값으로 흡수 (덤프가 회차를 죽이면 안 됨)
        return _DEFAULTS[key]


def audit_dir_for(db_path) -> Path:
    """덤프 위치는 DB 옆(data/audit/). 런타임 DB·백업 경로는 건드리지 않는다는
    제약을 지키면서도 커밋백 대상(data/) 안에 들어가야 같은 커밋에 실린다."""
    return Path(db_path).resolve().parent / "audit"


def _json_safe(v):
    """JSON 으로 안전하게 직렬화 가능한 값으로 정규화.

    NaN/Infinity 는 json 모듈이 뱉긴 하지만 표준 JSON 이 아니라 다른 도구(jq 등)가
    읽지 못한다 — 감사 산출물은 남이 읽을 수 있어야 하므로 None 으로 눕힌다.
    bytes 는 이 스키마엔 없지만 방어적으로 처리(덤프가 예외로 죽는 걸 막는다)."""
    if isinstance(v, float) and not math.isfinite(v):
        return None
    if isinstance(v, (bytes, bytearray)):
        return v.decode("utf-8", "replace")
    return v


def dump_table(conn, out_dir, table: str, week: str, now: float = None,
               include_raw_text: bool = True):
    """테이블 하나를 out_dir/{table}_{week}.ndjson 로 덤프. 반환: Path (테이블 없으면 None).

    같은 주차 재실행은 같은 파일을 덮어쓴다(멱등) — 실패 백오프로 한 주에 두 번
    돌아도 파일이 늘어나지 않는다. 임시파일에 다 쓴 뒤 os.replace 로 원자 교체해,
    쓰다 죽어도 반쯤 잘린 덤프가 커밋되는 일은 없다."""
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    if not cols:
        return None
    if table == "levels" and not include_raw_text and "raw_text" in cols:
        cols.remove("raw_text")

    order = _ORDER_BY.get(table)
    q = f"SELECT {', '.join(cols)} FROM {table}"
    if order in cols:
        q += f" ORDER BY {order}"
    rows = conn.execute(q).fetchall()

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{table}_{week}.ndjson"
    tmp = out_dir / f".{table}_{week}.ndjson.tmp"
    meta = {"_table": table, "_columns": list(cols), "_week_kst": week,
            "_rows": len(rows), "_dumped_at": now if now is not None else time.time()}
    # newline="\n" 고정 — Windows 로컬에서 만든 덤프와 Actions(리눅스) 덤프가
    # 줄바꿈만 달라 통째로 diff 되는 걸 막는다.
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(meta, ensure_ascii=False) + "\n")
        for r in rows:
            obj = {c: _json_safe(v) for c, v in zip(cols, tuple(r))}
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")
    os.replace(tmp, path)
    return path


def prune_old_dumps(out_dir, keep_weeks: int) -> list:
    """최근 keep_weeks 주차분만 남기고 오래된 덤프 파일 삭제. 반환: 지운 파일명 정렬 목록.

    주차 문자열(YYYY-Www)은 사전순 = 시간순이라 별도 파싱이 필요 없다.
    삭제해도 그 파일이 실렸던 과거 커밋에는 영구히 남는다 — 작업본만 가볍게 유지."""
    d = Path(out_dir)
    if not d.is_dir():
        return []
    files = [(m.group(1), p) for p in d.iterdir()
             for m in [_FILE_RE.match(p.name)] if m]
    weeks = sorted({w for w, _ in files}, reverse=True)
    keep = set(weeks[:max(0, int(keep_weeks))])
    removed = []
    for w, p in files:
        if w not in keep:
            p.unlink(missing_ok=True)  # 동시 삭제 경합 시 FileNotFoundError 방지
            removed.append(p.name)
    return sorted(removed)


def _compute_touch_time_stats(conn) -> dict:
    """수집→터치 소요시간 구간별 적중률 + 등급 교차분석.

    반환: {"overall": {bucket: {n, wins, hit_rate}},
           "by_grade": {grade: {bucket: {n, wins, hit_rate}}}}"""
    rows = conn.execute(
        """SELECT (touched_at - collected_at) / 3600.0 AS hours_to_touch,
                  outcome, grade
           FROM levels
           WHERE touched_at IS NOT NULL AND collected_at IS NOT NULL
             AND outcome IN ('hit','timeboxed_win','miss','timeboxed_loss')"""
    ).fetchall()
    if not rows:
        return {}

    def _bucket_key(h):
        if h < 1:
            return "<1h"
        if h < 6:
            return "1-6h"
        if h < 24:
            return "6-24h"
        if h < 72:
            return "24-72h"
        return ">72h"

    _LABELS = ("<1h", "1-6h", "6-24h", "24-72h", ">72h")
    overall = {k: [0, 0] for k in _LABELS}
    by_grade: dict = {}
    for r in rows:
        b = _bucket_key(r["hours_to_touch"])
        is_win = r["outcome"] in ("hit", "timeboxed_win")
        overall[b][0] += 1
        if is_win:
            overall[b][1] += 1
        g = r["grade"]
        if g:
            if g not in by_grade:
                by_grade[g] = {k: [0, 0] for k in _LABELS}
            by_grade[g][b][0] += 1
            if is_win:
                by_grade[g][b][1] += 1

    def _fmt(d):
        return {k: {"n": v[0], "wins": v[1],
                     "hit_rate": round(v[1] / v[0], 3) if v[0] else None}
                for k, v in d.items() if v[0]}

    return {"overall": _fmt(overall),
            "by_grade": {g: _fmt(d) for g, d in by_grade.items() if any(v[0] for v in d.values())}}


def _compute_signal_quality_stats(conn) -> dict:
    """IC(점수↔ret24h)·ICIR 주간 기록 (2026-08-15, 내부 통계 전용 — 알림 미출력).

    scripts/show_status.py 의 '신호 품질' 섹션과 같은 계산을 JSON 으로도 남긴다 —
    주차 파일이 쌓이면 IC 추이를 git 이력으로 되짚을 수 있다. analytics 모듈은
    "프로젝트 모듈 import 0" 원칙이라 행 공급은 여기(호출부)가 한다."""
    from datetime import datetime

    from analytics import signal_quality as sq
    from utils.time_kst import KST

    ph = ",".join("?" * len(sq.CLOSED_OUTCOMES))
    rows = conn.execute(
        f"""SELECT score, ret_24h, touched_at FROM levels
            WHERE touched_at IS NOT NULL AND score IS NOT NULL
              AND ret_24h IS NOT NULL AND outcome IN ({ph})""",
        sq.CLOSED_OUTCOMES).fetchall()

    ic = sq.compute_ic([{"score": r["score"], "ret_24h": r["ret_24h"]} for r in rows])

    weekly: dict = {}
    for r in rows:
        iso = datetime.fromtimestamp(float(r["touched_at"]), KST).isocalendar()
        weekly.setdefault(f"{iso[0]}-W{iso[1]:02d}", []).append(
            {"score": r["score"], "ret_24h": r["ret_24h"]})
    weekly_ics = {}
    for wk in sorted(weekly):
        w_ic = sq.compute_ic(weekly[wk])["ic"]
        if w_ic is not None:   # 주당 n<5 는 제외 (compute_ic 가 None 반환)
            weekly_ics[wk] = w_ic
    icir = sq.compute_icir(list(weekly_ics.values()))

    return {"ic": ic, "icir": icir, "weekly_ics": weekly_ics}


def _compute_author_deletion_stats(conn, min_posts: int = 5, top_n: int = 20) -> list:
    """작성자별 글 삭제율 (안티게이밍, 2026-08-15 — 내부 통계 전용, 알림 미출력).

    ACCURACY_DB_PLAN 의 "삭제 건수 자체가 신뢰도 신호" 원칙의 집계면 — 틀린 예측
    글을 지워 실적을 세탁하는 작성자를 주간 JSON 에서 사후 추적할 수 있게 한다.
    삭제 판정 원천은 run_collect._check_deletions 가 찍는 levels.deleted(0/1) 플래그.

    집계 단위는 **글(post_url distinct)** — 한 글에서 레벨(코인+엔트리)이 여러 개
    나오므로 레벨 단위로 세면 다단계 셋업 작성자의 분모·분자가 함께 부풀어
    삭제율이 왜곡된다. checked 는 생존 확인이 한 번이라도 수행된 글 수(참고용) —
    total 대비 checked 가 작으면 rate 는 하한 추정치라는 맥락을 준다.

    반환: [{"author", "total", "checked", "deleted", "rate"}, ...]
          수집 글 min_posts 건 이상 작성자만, rate 내림차순 top_n."""
    rows = conn.execute(
        """SELECT author,
                  COUNT(DISTINCT post_url) AS total,
                  COUNT(DISTINCT CASE WHEN deleted_checked_at IS NOT NULL
                                      THEN post_url END) AS checked,
                  COUNT(DISTINCT CASE WHEN deleted = 1 THEN post_url END) AS deleted
           FROM levels
           WHERE author IS NOT NULL AND post_url IS NOT NULL
           GROUP BY author
           HAVING COUNT(DISTINCT post_url) >= ?""",
        (min_posts,)).fetchall()
    stats = [{"author": r["author"], "total": r["total"], "checked": r["checked"],
              "deleted": r["deleted"],
              "rate": round(r["deleted"] / r["total"], 4) if r["total"] else 0.0}
             for r in rows]
    stats.sort(key=lambda s: (-s["rate"], -s["total"], s["author"]))
    return stats[:top_n]


def _compute_misses_stats(conn, since_epoch: float, cap: int = 30) -> dict:
    """감사 창(최근 7일) 내 종결된 패배 신호의 실패 분류 (2026-08-15 Tier2 #9).

    프롭 데스크의 3버킷 포스트모템 관행(연구: research_2026-08-15_alert_quality.md
    §6 권고 6-A)을 자동 태깅으로 옮긴 것 — MFE 로 "애초에 안 갔나 / 갔다가
    반납했나"를 가르면 고칠 대상(진입 논리 vs 지속성·타이밍)이 달라진다:
      · 즉시반전: mfe_pct < 1.0  — 터치 후 한 번도 못 감 (진입 논리 약함)
      · 이익반납: mfe_pct >= 2.0 — 올랐다가 다 돌려줌 (지속성·타이밍 문제)
      · 중간    : 1.0 <= mfe_pct < 2.0
      · 판정불가: mfe_pct 없음 (MFE 기록 도입 이전 행 등)

    내부 통계 전용 — 알림/텔레그램 미출력. class_counts 는 창 내 **전체** 패배건
    기준, signals 목록만 최근 cap 건으로 자른다(요약은 온전히, 상세는 상한).

    옵션 컬럼(touch_btc_regime 등)은 스키마가 ALTER 로 자라는 중이라 PRAGMA 로
    존재 확인 후 없으면 NULL 로 눕힌다 — 구버전 DB 에서도 덤프가 죽지 않는다."""
    have = {r[1] for r in conn.execute("PRAGMA table_info(levels)").fetchall()}

    def _col(name):
        return name if name in have else f"NULL AS {name}"

    def _prefer(primary, fallback, alias):
        # 터치 시점 스냅샷(touch_*)이 있으면 그걸, 없으면 수집 시점 값으로 폴백
        if primary in have and fallback in have:
            return f"COALESCE({primary}, {fallback}) AS {alias}"
        if primary in have:
            return f"{primary} AS {alias}"
        if fallback in have:
            return f"{fallback} AS {alias}"
        return f"NULL AS {alias}"

    sel = ", ".join([
        "coin_symbol", "author", "outcome", "touched_at", "resolved_at",
        _prefer("touch_grade", "grade", "grade"),
        _prefer("touch_score", "score", "score"),
        _col("mfe_pct"), _col("mae_pct"),
        _col("touch_supply_verdict"), _col("touch_position_verdict"),
        _col("touch_btc_regime"),
    ])
    rows = conn.execute(
        f"""SELECT {sel} FROM levels
            WHERE outcome IN ('miss','timeboxed_loss')
              AND resolved_at IS NOT NULL AND resolved_at >= ?
            ORDER BY resolved_at DESC, id DESC""",
        (since_epoch,)).fetchall()

    def _fail_class(mfe):
        if mfe is None:
            return "판정불가"
        if mfe < 1.0:
            return "즉시반전"
        if mfe >= 2.0:
            return "이익반납"
        return "중간"

    class_counts: dict = {}
    signals = []
    for r in rows:
        cls = _fail_class(r["mfe_pct"])
        class_counts[cls] = class_counts.get(cls, 0) + 1
        if len(signals) >= cap:
            continue    # 목록만 상한 — 요약(class_counts)은 전건 집계
        ttf = None
        if r["resolved_at"] is not None and r["touched_at"] is not None:
            ttf = round((r["resolved_at"] - r["touched_at"]) / 3600.0, 1)
        signals.append({
            "coin": r["coin_symbol"], "author": r["author"],
            "outcome": r["outcome"],
            "grade": r["grade"], "score": r["score"],
            "mfe_pct": r["mfe_pct"], "mae_pct": r["mae_pct"],
            "time_to_fail_h": ttf,
            "supply_verdict": r["touch_supply_verdict"],
            "position_verdict": r["touch_position_verdict"],
            "btc_regime": r["touch_btc_regime"],
            "failure_class": cls,
        })
    return {"total": len(rows), "class_counts": class_counts, "signals": signals}


# 글 나이 버킷 경계(시간). 168h 만료 상한 대비 "글이 이미 얼마나 묵은 채 터치됐나"
_POST_AGE_LABELS = ("<24h", "24-72h", "72-120h", "120h+")


def _compute_post_age_stats(conn) -> dict:
    """터치 시점 글 나이(수집→터치 경과 + 수집 당시 이미 묵은 나이) 버킷별 적중률
    (2026-08-15 Tier2 #10 분석 절반).

    목적: 168h 만료를 96-120h 로 조일지 판단할 **근거 데이터 축적** — 계산만 하고
    동작(만료·필터)은 아무것도 바꾸지 않는다. 글이 묵을수록 적중률이 꺾이는
    구간이 실측으로 보이면 그 지점이 새 만료 상한의 후보가 된다.

    age_at_touch_h = (touched_at - collected_at)/3600 + post_age_minutes/60
    — collected_at 은 봇이 글을 수집한 시각이지 글이 쓰인 시각이 아니므로,
    수집 시점에 이미 지나 있던 나이(post_age_minutes)를 더해야 진짜 글 나이다."""
    have = {r[1] for r in conn.execute("PRAGMA table_info(levels)").fetchall()}
    age_expr = "(touched_at - collected_at) / 3600.0"
    if "post_age_minutes" in have:
        age_expr += " + COALESCE(post_age_minutes, 0) / 60.0"
    rows = conn.execute(
        f"""SELECT {age_expr} AS age_h, outcome FROM levels
            WHERE touched_at IS NOT NULL AND collected_at IS NOT NULL
              AND outcome IN ('hit','timeboxed_win','miss','timeboxed_loss')"""
    ).fetchall()

    def _bucket(h):
        if h < 24:
            return "<24h"
        if h < 72:
            return "24-72h"
        if h < 120:
            return "72-120h"
        return "120h+"

    agg = {k: [0, 0] for k in _POST_AGE_LABELS}
    for r in rows:
        n_w = agg[_bucket(r["age_h"])]
        n_w[0] += 1
        if r["outcome"] in ("hit", "timeboxed_win"):
            n_w[1] += 1
    # 빈 버킷도 n=0 으로 남긴다 — 주차 파일 diff 시 "표본이 아예 없는 구간"이
    # 자기기술적으로 보여야 만료 조이기 논의에서 오독이 없다.
    return {k: {"n": v[0], "wins": v[1],
                "win_rate": round(v[1] / v[0], 2) if v[0] else None}
            for k, v in agg.items()}


def run_weekly_audit(conn, db_path, now: float = None, out_dir=None) -> dict:
    """덤프 → (성공 시) raw_text 정리 → 오래된 덤프 정리. 반환: 요약 dict.

    순서가 곧 안전장치다 — 원문은 ndjson 에 아카이브된 **뒤에만** DB 에서 비운다."""
    from storage import db  # 지연 임포트: db.py 가 이 모듈을 부르므로 순환 방지

    now = time.time() if now is None else now
    week = db.week_kst(now)
    out_dir = Path(out_dir) if out_dir else audit_dir_for(db_path)
    include_raw = bool(_cfg("audit_dump_include_raw_text"))

    files = []
    for t in TABLES:
        p = dump_table(conn, out_dir, t, week, now=now, include_raw_text=include_raw)
        if p is not None:
            files.append(p.name)

    # 등급별 적중률 + 작성자별 고급 통계 JSON 덤프 (내부 통계용, 알림 미출력)
    try:
        grade_stats = db.get_grade_stats(conn)
        score_bucket_stats = db.get_score_bucket_stats(conn)
        grade_ret_stats = db.get_grade_ret_stats(conn)
        authors = [r["author"] for r in conn.execute(
            """SELECT DISTINCT author FROM levels
               WHERE r_multiple IS NOT NULL AND touched_at IS NOT NULL
                 AND outcome IN ('hit','timeboxed_win','miss','timeboxed_loss')"""
        ).fetchall()]
        author_advanced = {a: db.get_author_advanced_stats(conn, a) for a in authors}
        touch_time_stats = _compute_touch_time_stats(conn)
        signal_quality_stats = _compute_signal_quality_stats(conn)
        author_deletion_rates = _compute_author_deletion_stats(conn)
        misses_stats = _compute_misses_stats(conn, since_epoch=now - 7 * 86400)
        post_age_stats = _compute_post_age_stats(conn)
        stats_path = out_dir / f"grade_stats_{week}.json"
        stats_tmp = out_dir / f".grade_stats_{week}.json.tmp"
        with open(stats_tmp, "w", encoding="utf-8", newline="\n") as fp:
            json.dump({
                "_week": week, "_generated_at": now,
                "grade_hit_rates": grade_stats,
                "score_bucket_hit_rates": score_bucket_stats,
                "grade_ret_distribution": grade_ret_stats,
                "author_advanced": author_advanced,
                "touch_time_analysis": touch_time_stats,
                "signal_quality": signal_quality_stats,
                "author_deletion_rates": author_deletion_rates,
                "misses": misses_stats,
                "post_age_stats": post_age_stats,
            }, fp, ensure_ascii=False, indent=2)
        os.replace(stats_tmp, stats_path)
        files.append(stats_path.name)
    except Exception as _e:  # noqa: BLE001 - 통계 덤프 실패가 전체를 중단시키면 안 됨
        logger.warning("등급 통계 덤프 실패: %s", _e)

    # 원문 정리는 "이번 덤프가 실제로 원문을 담아 성공했을 때"만. 덤프가 0건이거나
    # 원문 제외 모드면 아카이브가 없는 것이므로 지우지 않는다(복구 불가 손실 방지).
    raw_pruned = 0
    if any("levels_" in f for f in files) and include_raw:
        raw_pruned = db.prune_raw_text(conn, now=now,
                                       keep_days=_cfg("audit_raw_text_keep_days"))

    removed = prune_old_dumps(out_dir, _cfg("audit_dump_keep_weeks"))
    return {"week": week, "dir": str(out_dir), "files": files,
            "raw_text_pruned": raw_pruned, "removed": removed}


def maybe_weekly_audit(conn, db_path, now: float = None, force: bool = False,
                       out_dir=None) -> str:
    """주기가 도래했으면 감사 덤프 1회 수행. 반환 "skipped" | "ok" | "failed".

    주기 판정은 run_cycle 의 수집/리포트와 같은 meta 패턴(성공 키 + 실패 백오프 키,
    미래 시각 방어)을 그대로 따른다. run_cycle.py 는 다른 세션이 잡고 있어 임포트하지
    않고 최소 형태로 재현한다(의존 방향도 storage → scripts 로 뒤집히면 안 된다).
    meta_float 만은 db.py 의 공용 함수를 쓴다(run_cycle.py 와 중복 정의였던 것을
    2026-08-13 storage/db.py 로 통합)."""
    from storage import db

    if SUPPRESSED or not _cfg("audit_dump_enabled"):
        return "skipped"
    now = time.time() if now is None else now

    last_ok = db.meta_float(conn, META_LAST_DUMP)
    last_fail = db.meta_float(conn, META_LAST_DUMP_FAIL)
    if last_ok > now:      # 시계 역행/수동 편집 — 영구 굶주림 방지
        last_ok = 0.0
    if last_fail > now:
        last_fail = 0.0
    if not force:
        if last_ok and (now - last_ok) < _cfg("audit_dump_interval_hours") * 3600:
            return "skipped"
        if last_fail and (now - last_fail) < _cfg("audit_dump_retry_minutes") * 60:
            return "skipped"

    try:
        res = run_weekly_audit(conn, db_path, now=now, out_dir=out_dir)
    except Exception as e:  # noqa: BLE001 - 감사 실패가 회차를 죽이면 안 된다
        logger.error("감사 덤프 실패: %s: %s", type(e).__name__, e)
        try:
            db.set_meta(conn, META_LAST_DUMP_FAIL, str(now))
        except Exception:  # noqa: BLE001 - 백오프 기록 실패까지 전파시키지 않는다
            pass
        return "failed"

    try:
        db.set_meta(conn, META_LAST_DUMP, str(now))
    except Exception:  # noqa: BLE001
        pass
    logger.info("감사 덤프 완료: %s (%s) / 원문정리 %d행 / 만료덤프 %d개 삭제",
                res["week"], ", ".join(res["files"]) or "없음",
                res["raw_text_pruned"], len(res["removed"]))
    return "ok"
