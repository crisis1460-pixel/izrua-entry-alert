"""
레벨 상태 DB (SQLite).

한 "레벨" = TradingView 아이디어 글 하나에서 뽑은 (코인 + 엔트리가) 조합.
상태 머신: watching → previewed(엔트리 ±밴드 접근) → touched(엔트리 하향 터치) / expired(7일 경과)

가격 비교의 기준 통화: entry/sl/tp 는 TradingView(USDT 페어) 기준이라 USD 스케일로 저장하고,
KRW 환산은 가격체크 시점의 실시간 USDT/KRW 로 그때그때 계산한다(환율 변동 반영).

값(비밀) 없음 — 이 DB 는 공개 아티팩트로 올라가도 되는 시세/공개글 데이터만 담는다.
"""

import hashlib
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS levels (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_key        TEXT UNIQUE NOT NULL,   -- 중복 방지 해시
    coin_symbol       TEXT NOT NULL,          -- LINK
    ticker            TEXT NOT NULL,          -- KRW-LINK
    direction         TEXT NOT NULL,          -- long / short
    entry_usd         REAL,
    sl_usd            REAL,
    tp_usd            REAL,
    rr                REAL,                    -- 보상/위험비 (계산 가능 시)
    grade             TEXT,                    -- S/A/B/C/D
    score             REAL,
    author            TEXT,
    author_followers  INTEGER,
    author_hit_rate   REAL,                    -- 워쳐 DB 적중률 (0~1), 없으면 NULL
    author_hit_count  INTEGER,                 -- 표본 수
    author_whitelisted INTEGER DEFAULT 0,      -- 워쳐 화이트리스트 여부 (0/1)
    mcap_rank         INTEGER,                 -- 시총 순위 (수집 시점)
    mcap_tier_icon    TEXT,                    -- 💎🥇🥈🥉
    post_url          TEXT,
    post_age_minutes  REAL,                    -- 수집 시점의 글 나이
    status            TEXT NOT NULL DEFAULT 'watching',
    collected_at      REAL NOT NULL,
    previewed_at      REAL,
    touched_at        REAL,
    expired_at        REAL
);
CREATE INDEX IF NOT EXISTS idx_levels_status ON levels(status);
CREATE INDEX IF NOT EXISTS idx_levels_coin   ON levels(coin_symbol);

-- 알림 발송 로그 (코인당 하루 상한 계산 + 중복 방지용)
CREATE TABLE IF NOT EXISTS alerts_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    coin_symbol  TEXT NOT NULL,
    kind         TEXT NOT NULL,      -- preview / touch
    level_ids    TEXT,               -- 병합 시 여러 id (콤마구분)
    sent_at      REAL NOT NULL,
    day_kst      TEXT NOT NULL       -- YYYY-MM-DD (KST) — 일일 카운트 키
);
CREATE INDEX IF NOT EXISTS idx_alerts_day ON alerts_log(coin_symbol, day_kst);

-- 잡 간 공유 상태 (예: 가격체크의 last_check_at) — 아티팩트로 DB 와 함께 이동
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def make_signal_key(coin_symbol: str, entry_usd, author: str, post_url: str) -> str:
    """같은 글의 같은 엔트리를 한 레벨로 식별. 엔트리는 소수 6자리로 라운딩해
    부동소수 미세차로 중복 생성되는 걸 막는다."""
    entry_str = f"{float(entry_usd):.6f}" if entry_usd is not None else "none"
    raw = f"{coin_symbol}|{entry_str}|{author or ''}|{post_url or ''}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


@contextmanager
def connect(db_path: str):
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# 적중 DB 확장 컬럼 (2026-07-23 ACCURACY_DB_PLAN 확정) — 기존 DB 무중단 마이그레이션용
_OUTCOME_COLUMNS = {
    "outcome": "TEXT",            # hit | miss | timeboxed_win | timeboxed_loss
    "resolved_at": "REAL",
    "resolve_price_krw": "REAL",
    "best_tp_hit": "INTEGER",     # 도달한 최고 TP 차수 (v1은 1만 사용)
    "r_multiple": "REAL",         # (청산-진입)/(진입-SL), [-1,+5] 클리핑, SL 없으면 NULL
    "ambiguous": "INTEGER DEFAULT 0",   # 같은 구간 TP·SL 동시 터치(보수적 miss 처리됨)
    "judgment_mode": "TEXT",      # tp_sl | tp_only | timeboxed
    "ret_24h": "REAL",            # 터치 후 24h 수익률(%) — 최초 도과 시 1회 기록
    "ret_72h": "REAL",
    "touch_price_krw": "REAL",    # 터치 시점 현재가 (타임박스/수익률 기준가)
    # 판정 창(시간). 작성자 타임프레임 기반 — extractor.judgment_window_hours (2026-07-23 B안)
    "judgment_window_hours": "REAL",
    # 글 원문(제목+본문). 파서 개선 시 재수집 없이 재파싱해 오염값 자동 치유
    # (2026-07-23 SEI/SOL 서수오인 재발 후 추가 — reparse_all 참고)
    "raw_text": "TEXT",
}


def _migrate(conn) -> None:
    """기존 DB에 없는 컬럼만 ALTER 로 추가 (레포 커밋백 DB는 스키마가 과거일 수 있음)."""
    existing = {r["name"] for r in conn.execute("PRAGMA table_info(levels)").fetchall()}
    for col, decl in _OUTCOME_COLUMNS.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE levels ADD COLUMN {col} {decl}")


def init_db(db_path: str) -> None:
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)


def upsert_level(conn, level: dict) -> bool:
    """새 레벨이면 INSERT, 이미 있으면(같은 signal_key) 갱신 대상 필드만 UPDATE.
    반환: 신규 삽입이면 True."""
    key = level["signal_key"]
    row = conn.execute("SELECT id, status FROM levels WHERE signal_key = ?", (key,)).fetchone()
    if row is None:
        conn.execute(
            """INSERT INTO levels
               (signal_key, coin_symbol, ticker, direction, entry_usd, sl_usd, tp_usd,
                rr, grade, score, author, author_followers, author_hit_rate,
                author_hit_count, author_whitelisted, mcap_rank, mcap_tier_icon,
                post_url, post_age_minutes, status, collected_at, judgment_window_hours,
                raw_text)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                key, level["coin_symbol"], level["ticker"], level["direction"],
                level.get("entry_usd"), level.get("sl_usd"), level.get("tp_usd"),
                level.get("rr"), level.get("grade"), level.get("score"),
                level.get("author"), level.get("author_followers"),
                level.get("author_hit_rate"), level.get("author_hit_count"),
                1 if level.get("author_whitelisted") else 0,
                level.get("mcap_rank"), level.get("mcap_tier_icon"),
                level.get("post_url"), level.get("post_age_minutes"),
                "watching", level.get("collected_at", time.time()),
                level.get("judgment_window_hours"), level.get("raw_text"),
            ),
        )
        return True
    # 기존 레벨: 시총순위/등급/작성자 통계 + SL/TP 를 최신값으로 갱신 (상태·시각은 보존).
    # sl/tp 갱신 이유(2026-07-23): 추출기 버그 수정이 배포돼도 이미 저장된 오염값
    # (예: 서수 오인 tp=1.0)이 그대로 알림에 노출되는 것을 막는다 — 매 수집마다
    # 재파싱 결과로 덮어써 파서 개선이 기존 레벨에도 전파되게 한다. entry 는
    # signal_key 정체성의 일부라 갱신하지 않는다.
    conn.execute(
        """UPDATE levels SET
             grade=?, score=?, rr=?, sl_usd=?, tp_usd=?, author_followers=?,
             author_hit_rate=?, author_hit_count=?, author_whitelisted=?,
             mcap_rank=?, mcap_tier_icon=?, judgment_window_hours=?,
             raw_text=COALESCE(?, raw_text)
           WHERE signal_key=?""",
        (
            level.get("grade"), level.get("score"), level.get("rr"),
            level.get("sl_usd"), level.get("tp_usd"),
            level.get("author_followers"), level.get("author_hit_rate"),
            level.get("author_hit_count"), 1 if level.get("author_whitelisted") else 0,
            level.get("mcap_rank"), level.get("mcap_tier_icon"),
            level.get("judgment_window_hours"), level.get("raw_text"), key,
        ),
    )
    return False


def reparse_all(conn) -> int:
    """raw_text 가 있는 활성 레벨(watching/previewed)을 현재 파서로 재파싱해
    sl/tp/rr/판정창을 갱신한다. 파서 개선이 기존 레벨 전체에 자동 전파 —
    재수집 목록에서 밀려난 오래된 오염 레벨(예: 서수오인 tp=1.0)도 치유된다.
    entry 는 signal_key 정체성이라 갱신하지 않는다. 반환: 값이 바뀐 레벨 수."""
    from collector.extractor import parse_setup, parse_timeframe_hours, judgment_window_hours

    changed = 0
    rows = conn.execute(
        "SELECT id, entry_usd, sl_usd, tp_usd, raw_text FROM levels "
        "WHERE status IN ('watching','previewed') AND raw_text IS NOT NULL"
    ).fetchall()
    for r in rows:
        setup = parse_setup(r["raw_text"], current_price=r["entry_usd"])
        if not setup:
            continue
        new_sl, new_tp = setup.get("sl"), setup.get("tp")
        if new_sl == r["sl_usd"] and new_tp == r["tp_usd"]:
            continue
        rr = setup.get("rr")
        win = judgment_window_hours(parse_timeframe_hours(r["raw_text"]),
                                    r["entry_usd"], new_tp)
        conn.execute(
            "UPDATE levels SET sl_usd=?, tp_usd=?, rr=?, judgment_window_hours=? WHERE id=?",
            (new_sl, new_tp, rr, win, r["id"]),
        )
        changed += 1
    return changed


def get_active_levels(conn, direction: Optional[str] = "long") -> list:
    """감시 중(watching/previewed)인 레벨. 기본은 long 만 (하향 터치 알림 대상)."""
    q = "SELECT * FROM levels WHERE status IN ('watching','previewed')"
    params = ()
    if direction:
        q += " AND direction = ?"
        params = (direction,)
    return [dict(r) for r in conn.execute(q, params).fetchall()]


def mark_previewed(conn, level_id: int, now: Optional[float] = None) -> None:
    conn.execute(
        "UPDATE levels SET status='previewed', previewed_at=? WHERE id=? AND status='watching'",
        (now or time.time(), level_id),
    )


def mark_touched(conn, level_ids: list, now: Optional[float] = None,
                 touch_price_krw: Optional[float] = None) -> None:
    now = now or time.time()
    conn.executemany(
        "UPDATE levels SET status='touched', touched_at=?, touch_price_krw=? "
        "WHERE id=? AND status IN ('watching','previewed')",
        [(now, touch_price_krw, lid) for lid in level_ids],
    )


# ── 적중 판정 (ACCURACY_DB_PLAN v1) ──────────────────────────────

def get_unresolved_touched(conn) -> list:
    """터치됐지만 아직 승패 미종결인 레벨 — 가격체크 잡이 매 회차 평가."""
    return [dict(r) for r in conn.execute(
        "SELECT * FROM levels WHERE status='touched' AND outcome IS NULL"
    ).fetchall()]


def resolve_outcome(conn, level_id: int, outcome: str, resolve_price_krw: float,
                    judgment_mode: str, r_multiple: Optional[float] = None,
                    ambiguous: bool = False, best_tp_hit: Optional[int] = None,
                    now: Optional[float] = None) -> None:
    conn.execute(
        """UPDATE levels SET outcome=?, resolved_at=?, resolve_price_krw=?,
             judgment_mode=?, r_multiple=?, ambiguous=?, best_tp_hit=?
           WHERE id=? AND outcome IS NULL""",
        (outcome, now or time.time(), resolve_price_krw, judgment_mode,
         r_multiple, 1 if ambiguous else 0, best_tp_hit, level_id),
    )


def get_author_self_stats(conn, author: str) -> dict:
    """자체 적중 DB 기준 작성자 성적 (터치 후 판정 건만).
    승 = hit + timeboxed_win, 패 = miss + timeboxed_loss."""
    if not author:
        return {"wins": 0, "losses": 0}
    row = conn.execute(
        """SELECT
             SUM(CASE WHEN outcome IN ('hit','timeboxed_win') THEN 1 ELSE 0 END) AS w,
             SUM(CASE WHEN outcome IN ('miss','timeboxed_loss') THEN 1 ELSE 0 END) AS l
           FROM levels WHERE author=? AND outcome IS NOT NULL""",
        (author,),
    ).fetchone()
    return {"wins": row["w"] or 0, "losses": row["l"] or 0}


def record_ret(conn, level_id: int, field: str, value: float) -> None:
    """터치 후 24h/72h 수익률 1회 기록 (이미 있으면 보존 — 최초 도과 시점 값 유지)."""
    assert field in ("ret_24h", "ret_72h")
    conn.execute(
        f"UPDATE levels SET {field}=? WHERE id=? AND {field} IS NULL", (value, level_id)
    )


def expire_old(conn, max_age_sec: float, now: Optional[float] = None) -> int:
    """수집 후 max_age_sec 지난 미터치 레벨을 expired 처리. 반환: 만료 건수."""
    now = now or time.time()
    cutoff = now - max_age_sec
    cur = conn.execute(
        "UPDATE levels SET status='expired', expired_at=? "
        "WHERE status IN ('watching','previewed') AND collected_at < ?",
        (now, cutoff),
    )
    return cur.rowcount


def count_alerts_today(conn, coin_symbol: str, day_kst: str, kind: Optional[str] = None) -> int:
    q = "SELECT COUNT(*) AS n FROM alerts_log WHERE coin_symbol=? AND day_kst=?"
    params = [coin_symbol, day_kst]
    if kind:
        q += " AND kind=?"
        params.append(kind)
    return conn.execute(q, params).fetchone()["n"]


def record_alert(conn, coin_symbol: str, kind: str, level_ids: list, day_kst: str,
                 now: Optional[float] = None) -> None:
    conn.execute(
        "INSERT INTO alerts_log (coin_symbol, kind, level_ids, sent_at, day_kst) VALUES (?,?,?,?,?)",
        (coin_symbol, kind, ",".join(str(i) for i in level_ids), now or time.time(), day_kst),
    )


def get_meta(conn, key: str, default: Optional[str] = None) -> Optional[str]:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(conn, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def stats(conn) -> dict:
    """대시보드/아침요약/헬스체크용 요약."""
    def n(where):
        return conn.execute(f"SELECT COUNT(*) AS n FROM levels WHERE {where}").fetchone()["n"]
    return {
        "watching": n("status='watching'"),
        "previewed": n("status='previewed'"),
        "touched": n("status='touched'"),
        "expired": n("status='expired'"),
        "total": conn.execute("SELECT COUNT(*) AS n FROM levels").fetchone()["n"],
    }
