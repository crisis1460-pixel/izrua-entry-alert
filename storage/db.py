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
    "touch_price_krw": "REAL",    # 터치 시점 기준가 (지정가 체결 모델: 그 레벨의 entry_krw)
    "touch_usdt_krw": "REAL",     # 터치 시점 USDT/KRW — 장기 판정창의 환율 드리프트 보정용
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
    # 2026-07-24 감사 수정(불변 스냅샷): 갱신은 활성(watching/previewed) 레벨에만 —
    # 터치돼 판정 진행 중이거나 종결된 레벨의 SL/TP 가 재수집(작성자의 글 수정 포함)
    # 으로 사후 변경되면 '판정 기준 골대 이동'이라 안티게이밍 원칙 위반.
    conn.execute(
        """UPDATE levels SET
             grade=?, score=?, rr=?, sl_usd=?, tp_usd=?, author_followers=?,
             author_hit_rate=?, author_hit_count=?, author_whitelisted=?,
             mcap_rank=?, mcap_tier_icon=?, judgment_window_hours=?,
             raw_text=COALESCE(?, raw_text)
           WHERE signal_key=? AND status IN ('watching','previewed')""",
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
    entry 는 signal_key 정체성이라 갱신하지 않는다. 반환: 값이 바뀐 레벨 수.

    2026-07-24 감사 수정: 방향/크기 sanity 와 rr 을 파서가 새로 뽑은 entry 가 아니라
    '저장된 entry' 기준으로 재검증·재계산한다 (파서 변경으로 entry 해석이 달라져도
    저장 레벨의 판정 기준과 어긋나지 않게). 판정창/rr 변경도 갱신 대상에 포함."""
    from collector.extractor import parse_setup, parse_timeframe_hours, judgment_window_hours

    changed = 0
    # long 전용 sanity 라서 long 레벨만 재검증한다 (2026-07-24 감사 #4: 숏 레벨에
    # long 규칙을 적용하면 유효한 숏 sl/tp 를 NULL 로 파괴). 이 봇은 long 만 알림하나
    # DB 무결성을 위해 방향 한정.
    rows = conn.execute(
        "SELECT id, entry_usd, sl_usd, tp_usd, rr, judgment_window_hours, raw_text "
        "FROM levels WHERE status IN ('watching','previewed') AND raw_text IS NOT NULL "
        "AND direction='long'"
    ).fetchall()
    for r in rows:
        entry = r["entry_usd"]
        setup = parse_setup(r["raw_text"], current_price=entry)
        if not setup or not entry or entry <= 0:
            continue
        new_sl, new_tp = setup.get("sl"), setup.get("tp")
        # 저장 entry 기준 재검증 (long 전용: 방향 + 크기 0.25x~4x)
        if new_tp is not None and not (entry < new_tp <= entry * 4):
            new_tp = None
        if new_sl is not None and not (entry * 0.25 <= new_sl < entry):
            new_sl = None
        rr = None
        if new_sl and new_tp and entry > new_sl:
            rr = round((new_tp - entry) / (entry - new_sl), 2)
        win = judgment_window_hours(parse_timeframe_hours(r["raw_text"]), entry, new_tp)
        if (new_sl == r["sl_usd"] and new_tp == r["tp_usd"]
                and rr == r["rr"] and win == r["judgment_window_hours"]):
            continue
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


def mark_touched(conn, touches: list, now: Optional[float] = None,
                 usdt_krw: Optional[float] = None) -> None:
    """touches: [(level_id, touch_price_krw|None, touched_at|None), ...].

    2026-07-24 감사 수정: 클러스터 상단 터치 시 하단 레벨(자기 엔트리 미도달)까지
    같은 기준가로 판정되던 편향 제거 — price=None 인 레벨은 '섀도 터치'(재알림
    방지용 상태 전이만, touched_at 없음 → 판정·통계에서 제외)로 처리하고,
    도달 레벨은 자기 entry_krw(지정가 체결 모델)를 기준가로 저장한다.

    2026-07-26 감사 major2: touched_at 은 감지 시각이 아니라 '실제 도달한 첫 캔들의
    종료 시각'(호출부 계산, 진행 중 캔들이면 미래일 수 있음)을 앵커로 받는다 —
    감지 시각 앵커면 터치 캔들 전체가 다음 회차 판정 필터(c_end<=touched_at)를
    통과해 터치 이전 가격이 판정에 섞였다."""
    now = now or time.time()
    for lid, price, t_anchor in touches:
        if price is None:
            conn.execute(
                "UPDATE levels SET status='touched' "
                "WHERE id=? AND status IN ('watching','previewed')", (lid,))
        else:
            conn.execute(
                "UPDATE levels SET status='touched', touched_at=?, touch_price_krw=?, "
                "touch_usdt_krw=? WHERE id=? AND status IN ('watching','previewed')",
                (t_anchor or now, price, usdt_krw, lid))


# ── 적중 판정 (ACCURACY_DB_PLAN v1) ──────────────────────────────

def get_unresolved_touched(conn) -> list:
    """실제 도달 터치됐지만 아직 승패 미종결인 레벨 — 가격체크 잡이 매 회차 평가.
    (섀도 터치(touched_at NULL)는 재알림 방지 전용이라 판정 대상 아님)"""
    return [dict(r) for r in conn.execute(
        "SELECT * FROM levels WHERE status='touched' AND outcome IS NULL "
        "AND touched_at IS NOT NULL"
    ).fetchall()]


def get_ret_pending(conn, now: Optional[float] = None) -> list:
    """24h/72h 수익률 기록 대상 — 종결 여부와 무관 (2026-07-24 감사 수정:
    조기 종결 건도 수익률은 계속 기록해야 데이터셋에 생존편향이 안 박힘).

    2026-07-26 감사 minor5: 기록 가능 시한(24h+6h / 72h+6h 허용오차)을 이미 넘긴
    행은 제외 — 구세대 결손 행이 매 회차 영구 선택되며 시세조회 티커 목록을
    좀비로 불리던 문제."""
    now = now or time.time()
    return [dict(r) for r in conn.execute(
        "SELECT id, ticker, touched_at, touch_price_krw, touch_usdt_krw, entry_usd, "
        "ret_24h, ret_72h FROM levels WHERE touched_at IS NOT NULL "
        "AND ((ret_24h IS NULL AND touched_at >= ?) "
        "  OR (ret_72h IS NULL AND touched_at >= ?))",
        (now - 30 * 3600, now - 78 * 3600)
    ).fetchall()]


def get_author_outcome_rows(conn, author: Optional[str]) -> list:
    """작성자의 종결 표본 원천 행 — analytics.ranking 계산용 (작성자 통계는 저장하지
    않고 매번 집계, ACCURACY_DB_PLAN 원천 보존 원칙). 섀도 터치는 자동 제외."""
    if not author:
        return []
    return [dict(r) for r in conn.execute(
        "SELECT outcome, r_multiple, touched_at, author_hit_rate, author_hit_count "
        "FROM levels WHERE author=? AND outcome IS NOT NULL AND touched_at IS NOT NULL",
        (author,)).fetchall()]


def list_authors_with_outcomes(conn) -> list:
    """종결 표본(outcome 확정 + 실제 도달 터치)이 있는 작성자 목록 — 주간 리포트가
    analytics.ranking.rank_authors() 에 넘길 {author: rows} 를 만들 때 순회 대상 확보용.
    섀도 터치(touched_at NULL)는 get_author_outcome_rows 와 동일 기준으로 제외."""
    return [r["author"] for r in conn.execute(
        "SELECT DISTINCT author FROM levels "
        "WHERE author IS NOT NULL AND outcome IS NOT NULL AND touched_at IS NOT NULL"
    ).fetchall()]


def get_ret24_values(conn) -> list:
    """터치 후 24h 수익률(%) 실측값 목록 — 주간 리포트의 초과 적중률 베이스라인용
    (analytics.clustering.baseline_positive_rate 가 양수 비율로 환산).

    표본 정의를 랭킹 쪽과 맞춘다: 실제 도달 터치(touched_at NOT NULL)만.
    종결 여부는 묻지 않는다 — ret_24h 는 조기 종결건에도 계속 기록되므로
    (get_ret_pending 주석 참고) 종결로 거르면 도리어 생존편향이 들어간다."""
    return [r["ret_24h"] for r in conn.execute(
        "SELECT ret_24h FROM levels WHERE touched_at IS NOT NULL AND ret_24h IS NOT NULL"
    ).fetchall()]


def get_touched_levels_for_clusters(conn) -> list:
    """터치 이력 전체(실제 도달분) — 주간 리포트가 합의(confluence) 클러스터를
    재구성할 때 쓰는 원천 행. 섀도 터치(touched_at NULL)는 제외한다: 만료되면
    상태상 미터치 레벨과 구분이 불가능해져 리포트마다 집계가 흔들리기 때문
    (재현 가능한 기준을 우선). 필요한 컬럼만 얇게 조회."""
    return [dict(r) for r in conn.execute(
        "SELECT id, coin_symbol, entry_usd, author, touched_at FROM levels "
        "WHERE touched_at IS NOT NULL AND entry_usd IS NOT NULL AND author IS NOT NULL"
    ).fetchall()]


def get_author_raw_record(conn) -> dict:
    """작성자별 원시 승/패 (종결 표본, 최신성 가중 없음) — 초과 적중률 비교용.
    베이스라인(ret_24h 양수 비율)이 가중 없는 단순 비율이라 비교 대상도 원시값으로
    맞춘다(가중 승률과 섞으면 두 축이 어긋난다)."""
    rows = conn.execute(
        """SELECT author,
             SUM(CASE WHEN outcome IN ('hit','timeboxed_win') THEN 1 ELSE 0 END) AS w,
             SUM(CASE WHEN outcome IN ('miss','timeboxed_loss') THEN 1 ELSE 0 END) AS l
           FROM levels WHERE author IS NOT NULL AND outcome IS NOT NULL
             AND touched_at IS NOT NULL GROUP BY author"""
    ).fetchall()
    return {r["author"]: {"wins": r["w"] or 0, "losses": r["l"] or 0} for r in rows}


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
    """자체 적중 DB 기준 작성자 성적.
    승 = hit + timeboxed_win, 패 = miss + timeboxed_loss.
    터치율(선택편향 처방, ACCURACY_DB_PLAN): 도달터치 ÷ (도달터치 + 미터치만료)."""
    if not author:
        return {"wins": 0, "losses": 0, "touched": 0, "untouched_expired": 0}
    row = conn.execute(
        """SELECT
             SUM(CASE WHEN outcome IN ('hit','timeboxed_win') THEN 1 ELSE 0 END) AS w,
             SUM(CASE WHEN outcome IN ('miss','timeboxed_loss') THEN 1 ELSE 0 END) AS l,
             SUM(CASE WHEN touched_at IS NOT NULL THEN 1 ELSE 0 END) AS t,
             SUM(CASE WHEN status='expired' AND touched_at IS NULL THEN 1 ELSE 0 END) AS e
           FROM levels WHERE author=?""",
        (author,),
    ).fetchone()
    return {"wins": row["w"] or 0, "losses": row["l"] or 0,
            "touched": row["t"] or 0, "untouched_expired": row["e"] or 0}


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
    # 섀도 터치(재알림 방지용 전이만, 판정·통계 제외)도 수명이 다하면 만료 —
    # 안 하면 영구 잔류하며 stats() 의 touched 집계를 오염 (2026-07-26 감사 minor8)
    cur2 = conn.execute(
        "UPDATE levels SET status='expired', expired_at=? "
        "WHERE status='touched' AND touched_at IS NULL AND collected_at < ?",
        (now, cutoff),
    )
    return cur.rowcount + cur2.rowcount


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
