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

    # 원문 정리는 "이번 덤프가 실제로 원문을 담아 성공했을 때"만. 덤프가 0건이거나
    # 원문 제외 모드면 아카이브가 없는 것이므로 지우지 않는다(복구 불가 손실 방지).
    raw_pruned = 0
    if files and include_raw:
        raw_pruned = db.prune_raw_text(conn, now=now,
                                       keep_days=_cfg("audit_raw_text_keep_days"))

    removed = prune_old_dumps(out_dir, _cfg("audit_dump_keep_weeks"))
    return {"week": week, "dir": str(out_dir), "files": files,
            "raw_text_pruned": raw_pruned, "removed": removed}


def _meta_float(conn, key: str) -> float:
    from storage import db
    try:
        return float(db.get_meta(conn, key) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def maybe_weekly_audit(conn, db_path, now: float = None, force: bool = False,
                       out_dir=None) -> str:
    """주기가 도래했으면 감사 덤프 1회 수행. 반환 "skipped" | "ok" | "failed".

    주기 판정은 run_cycle 의 수집/리포트와 같은 meta 패턴(성공 키 + 실패 백오프 키,
    미래 시각 방어)을 그대로 따른다. run_cycle.py 는 다른 세션이 잡고 있어 임포트하지
    않고 최소 형태로 재현한다(의존 방향도 storage → scripts 로 뒤집히면 안 된다)."""
    from storage import db

    if SUPPRESSED or not _cfg("audit_dump_enabled"):
        return "skipped"
    now = time.time() if now is None else now

    last_ok = _meta_float(conn, META_LAST_DUMP)
    last_fail = _meta_float(conn, META_LAST_DUMP_FAIL)
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
    except BaseException as e:  # noqa: BLE001 - 감사 실패가 회차를 죽이면 안 된다
        if isinstance(e, KeyboardInterrupt):
            raise
        logger.error("감사 덤프 실패: %s: %s", type(e).__name__, e)
        try:
            db.set_meta(conn, META_LAST_DUMP_FAIL, str(now))
        except BaseException:  # noqa: BLE001 - 백오프 기록 실패까지 전파시키지 않는다
            pass
        return "failed"

    db.set_meta(conn, META_LAST_DUMP, str(now))
    logger.info("감사 덤프 완료: %s (%s) / 원문정리 %d행 / 만료덤프 %d개 삭제",
                res["week"], ", ".join(res["files"]) or "없음",
                res["raw_text_pruned"], len(res["removed"]))
    return "ok"
