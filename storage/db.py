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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

_KST = timezone(timedelta(hours=9))

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

-- 관찰 집계 (스프린트5 "알림량 관찰기" — 조용히 누적만, 발송 없음).
-- 신규 수집 건수(levels.collected_at)와 발송 건수(alerts_log)는 이미 원본이 있어
-- 여기 중복 저장하지 않고 조회 시점에 집계한다(get_observation_report). 여기엔
-- 다른 곳엔 없는 값 — 필터 억제 전 '원(raw) 이벤트'와 억제 사유별 건수만 쌓는다.
-- 보존기간 60일(prune_daily_stats) — 하루 1행이라 자연히 가벼움.
-- 작성자 주간 지표 스냅샷 (2026-07-26 사용자 결정 — 역신호 태깅의 선행 인프라).
-- 역신호 판정 규칙은 "n_eff>=5 이면서 E_LB<0 이 2주 연속"인데, 지금은 매 시점의
-- 값만 계산할 수 있고 '지난주에 어땠는지'를 알 방법이 없어 연속 판정이 불가능했다.
-- 여기 주 1회 찍어두면 나중에 판정 로직만 얹으면 된다.
-- **저장 전용** — 이 테이블은 알림·필터·등급 어디에도 영향을 주지 않는다(관찰기 안전).
CREATE TABLE IF NOT EXISTS author_snapshots (
    week_kst   TEXT NOT NULL,        -- ISO 주차 (YYYY-Www, KST 기준)
    author     TEXT NOT NULL,
    e_lb       REAL,                 -- R 트랙 보수적 기대값 (표본 없으면 NULL)
    neff_r     REAL,                 -- R 트랙 유효표본 (게이트 판정용)
    p_hat      REAL,                 -- 베이지안 수축 승률
    neff_win   REAL,                 -- 승률축 유효표본
    wins       INTEGER,              -- 원시 승 (해석 보조)
    losses     INTEGER,
    taken_at   REAL NOT NULL,
    PRIMARY KEY (week_kst, author)
);
CREATE INDEX IF NOT EXISTS idx_snap_author ON author_snapshots(author, week_kst);

CREATE TABLE IF NOT EXISTS daily_stats (
    day_kst              TEXT PRIMARY KEY,          -- YYYY-MM-DD (KST)
    touches_total        INTEGER NOT NULL DEFAULT 0, -- 필터 무관 전체 터치 발생(클러스터 단위)
    previews_total       INTEGER NOT NULL DEFAULT 0, -- 필터 무관 전체 예고 발생
    suppressed_grade     INTEGER NOT NULL DEFAULT 0, -- 등급 미달로 억제
    suppressed_cap       INTEGER NOT NULL DEFAULT 0, -- 코인당 일일 상한으로 억제
    suppressed_dup       INTEGER NOT NULL DEFAULT 0, -- (사용 안 함) 2026-07-26 감사
        -- MAJOR-1 이전에 '밴드 체류 회차'를 억제로 잘못 세던 자리. 과거 데이터
        -- 해석용으로만 남기고 더는 증가하지 않는다 → preview_dwell 로 이관.
    preview_dwell        INTEGER NOT NULL DEFAULT 0, -- 예고 밴드 체류 회차(억제 아님)
    suppressed_send_fail INTEGER NOT NULL DEFAULT 0, -- 필터는 통과했으나 텔레그램 발송 실패
    -- suppressed_grade 의 부분집합(중복 카운트 아님!) — 2026-07-26 목표거리 감점 도입
    -- 효과를 분리 측정하려는 사용자 결정. "등급 미달로 억제된 건" 중에서 "그 감점만
    -- 되돌리면 alert_min_grade 를 통과했을 건"만 별도 표시한다. suppressed_grade 는
    -- 이 값을 포함한 채로 그대로 유지(둘 다 봐야 "감점 때문에 억제 vs 원래도 미달"이
    -- 갈린다).
    suppressed_grade_tp_penalty_only INTEGER NOT NULL DEFAULT 0,
    -- 동시터치(같은 1분봉 TP·SL 동시 도달) 재검사 결과 — 판정 신뢰도 지표.
    -- magnified   : 체결내역으로 실제 순서를 복원해 hit/miss 확정
    -- unresolved  : 체결내역을 봤는데도 순서를 못 가려 보수적 miss+ambiguous 유지
    -- skipped     : 예산 소진·기능 OFF·구간 길이 초과로 체결내역을 아예 못 봄(2026-07-26
    --               감사 minor 분리). 결과는 unresolved 와 똑같은 보수적 miss+ambiguous
    --               지만 성격이 다르다 — unresolved 는 '데이터로도 못 가르는 한계',
    --               skipped 은 '설정만 바꾸면 줄어드는 운영 이슈'라 한 칸에 합쳐 세면
    --               신뢰도 지표로 쓸 수 없다.
    -- 셋의 합 = 그 날 발생한 동시터치 전체 건수. 모두 드물게만 증가한다.
    ambiguous_magnified   INTEGER NOT NULL DEFAULT 0,
    ambiguous_unresolved  INTEGER NOT NULL DEFAULT 0,
    ambiguous_skipped     INTEGER NOT NULL DEFAULT 0,
    updated_at           REAL
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
    # 만료 사유 (2026-07-26 카드 #5). 기존 시간경과 만료는 NULL 로 남고, 거래소
    # 리스크 공지 등 외부 사유로 앞당겨 만료된 건만 값이 찬다 - 사후에 "왜 사라졌나"
    # 를 구분할 수 있어야 만료 통계가 오염되지 않는다.
    "expired_reason": "TEXT",
    # 터치 시점 호가 매수/매도 압력 = total_bid_size / total_ask_size (2026-07-26 카드 #19).
    # >1 이면 매수 잔량 우위. **순수 로깅 컬럼** - 알림·필터·등급 어디에도 쓰이지
    # 않는다. 나중에 outcome 과의 상관을 사후 분석하기 위한 원천 데이터.
    "touch_bid_ask_ratio": "REAL",
    # 글 삭제 감지 (2026-07-26 ACCURACY_DB_PLAN 안티게이밍 항목 구현).
    # 판정/통계는 그대로 유지하고 플래그만 추가 - "삭제 건수 자체가 신뢰도 신호".
    "deleted": "INTEGER DEFAULT 0",       # 1 = post_url 이 확인 시점에 404(삭제 확정)
    "deleted_checked_at": "REAL",         # 마지막 생존 확인 시각 (없으면 미확인)
    # 적중 판정 해시체인 (2026-07-27 기획 카드 #3 — 사후 행 변조·유실 자가 감지).
    # outcome_hash = SHA-256(outcome_prev_hash + 판정 정체성 필드 직렬화), 최초 행은
    # outcome_prev_hash = 고정 제네시스. 나중에 이 행의 outcome/resolved_at/r_multiple/
    # ambiguous 가 조금이라도 바뀌면 재계산 해시가 달라져 체인이 끊긴다(verify_outcome_chain).
    "outcome_prev_hash": "TEXT",
    "outcome_hash": "TEXT",
}


def _migrate(conn) -> None:
    """기존 DB에 없는 컬럼만 ALTER 로 추가 (레포 커밋백 DB는 스키마가 과거일 수 있음).

    daily_stats 도 함께 본다 — CREATE TABLE IF NOT EXISTS 는 '테이블이 이미 있으면'
    새 컬럼을 붙여주지 않아서, 운영 DB에 테이블이 생긴 뒤 컬럼을 추가하면 조용히
    누락된다(2026-07-26 ambiguous_* 추가 때 발견). 카운터라 기본값 0 으로 채운다."""
    existing = {r["name"] for r in conn.execute("PRAGMA table_info(levels)").fetchall()}
    for col, decl in _OUTCOME_COLUMNS.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE levels ADD COLUMN {col} {decl}")

    ds_cols = {r["name"] for r in conn.execute("PRAGMA table_info(daily_stats)").fetchall()}
    if ds_cols:  # 테이블이 아직 없으면 SCHEMA 가 최신 정의로 만들어준다
        for col in _DAILY_STATS_COLS:
            if col not in ds_cols:
                conn.execute(
                    f"ALTER TABLE daily_stats ADD COLUMN {col} INTEGER NOT NULL DEFAULT 0")

    # 적중 판정 해시체인 소급 구축 (2026-07-27 카드 #3) — outcome_hash 컬럼이 방금
    # 생겼거나 과거 판정 행이 있으면(레포 커밋백 DB) 1회성으로 체인을 이어붙인다.
    # 이미 체인이 있는 행은 WHERE 절이 걸러내 매 init_db 호출마다 사실상 공짜(no-op).
    _backfill_outcome_chain(conn)


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
                 usdt_krw: Optional[float] = None,
                 bid_ask_ratio: Optional[float] = None) -> None:
    """touches: [(level_id, touch_price_krw|None, touched_at|None), ...].

    2026-07-24 감사 수정: 클러스터 상단 터치 시 하단 레벨(자기 엔트리 미도달)까지
    같은 기준가로 판정되던 편향 제거 — price=None 인 레벨은 '섀도 터치'(재알림
    방지용 상태 전이만, touched_at 없음 → 판정·통계에서 제외)로 처리하고,
    도달 레벨은 자기 entry_krw(지정가 체결 모델)를 기준가로 저장한다.

    2026-07-26 감사 major2: touched_at 은 감지 시각이 아니라 '실제 도달한 첫 캔들의
    종료 시각'(호출부 계산, 진행 중 캔들이면 미래일 수 있음)을 앵커로 받는다 —
    감지 시각 앵커면 터치 캔들 전체가 다음 회차 판정 필터(c_end<=touched_at)를
    통과해 터치 이전 가격이 판정에 섞였다.

    bid_ask_ratio 는 터치 시점 호가 매수/매도 잔량비 스냅샷(2026-07-26 카드 #19).
    **기록 전용** — 알림·필터·판정 어디에도 쓰이지 않는다. 실제 도달 터치에만
    남긴다(섀도 터치는 그 레벨의 엔트리에 닿은 게 아니라 시점이 무의미)."""
    now = now or time.time()
    for lid, price, t_anchor in touches:
        if price is None:
            conn.execute(
                "UPDATE levels SET status='touched' "
                "WHERE id=? AND status IN ('watching','previewed')", (lid,))
        else:
            conn.execute(
                "UPDATE levels SET status='touched', touched_at=?, touch_price_krw=?, "
                "touch_usdt_krw=?, touch_bid_ask_ratio=? "
                "WHERE id=? AND status IN ('watching','previewed')",
                (t_anchor or now, price, usdt_krw, bid_ask_ratio, lid))


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


def week_kst(now: Optional[float] = None) -> str:
    """ISO 주차 문자열 (YYYY-Www, KST 기준) — 작성자 스냅샷의 주 단위 키."""
    d = datetime.fromtimestamp(now if now is not None else time.time(), tz=_KST)
    iso = d.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def save_author_snapshot(conn, week: str, author: str, met: dict,
                         now: Optional[float] = None) -> None:
    """작성자 주간 지표 1행 저장(같은 주 재실행이면 덮어씀 — 그 주 마지막 값이 남는다).

    met 는 analytics.ranking.author_metrics() 반환 dict. 저장 전용이며 어떤 판정에도
    쓰이지 않는다 — 역신호 '2주 연속' 판정 로직을 나중에 얹기 위한 원천 데이터."""
    conn.execute(
        """INSERT INTO author_snapshots
             (week_kst, author, e_lb, neff_r, p_hat, neff_win, wins, losses, taken_at)
           VALUES (?,?,?,?,?,?,?,?,?)
           ON CONFLICT(week_kst, author) DO UPDATE SET
             e_lb=excluded.e_lb, neff_r=excluded.neff_r, p_hat=excluded.p_hat,
             neff_win=excluded.neff_win, wins=excluded.wins, losses=excluded.losses,
             taken_at=excluded.taken_at""",
        (week, author, met.get("e_lb"), met.get("neff_r"), met.get("p_hat"),
         met.get("neff_win"), met.get("wins"), met.get("losses"),
         now if now is not None else time.time()),
    )


def get_author_snapshots(conn, author: Optional[str] = None, limit_weeks: int = 8) -> list:
    """최근 주차 스냅샷 조회 (author 지정 시 그 작성자만). 최신 주차부터 내림차순."""
    if author:
        rows = conn.execute(
            "SELECT * FROM author_snapshots WHERE author=? "
            "ORDER BY week_kst DESC LIMIT ?", (author, limit_weeks)).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM author_snapshots ORDER BY week_kst DESC, author "
            "LIMIT ?", (limit_weeks * 50,)).fetchall()
    return [dict(r) for r in rows]


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


## ── 적중 판정 해시체인 (2026-07-27 기획 카드 #3) ─────────────────────────
# 목적: levels 에 쌓이는 판정(hit/miss 등)은 작성자 랭킹(E_LB)의 근간이라 "단 한 번만
# 쓰인다"는 불변 스냅샷 원칙이 이미 있지만, DB 파일을 직접 열어 행을 고치거나 지우면
# 그 원칙을 우회할 수 있고 지금까지는 스스로 감지하는 장치가 없었다. 여기서는 판정이
# 확정되는 순간 그 값들을 체인으로 엮어 - 나중에 몰래 바뀌면 재계산 해시가 어긋나게
# 만든다(git 커밋 이력은 "언제 바뀌었나"는 보여주지만 "바뀐 그 자체"를 DB 스스로
# 감지하진 못했다 - 이 체인은 DB 파일 자체의 자기 검증 능력을 더한다).
#
# 체인 규칙: outcome_hash = SHA256(outcome_prev_hash + "|" + 직렬화(id, outcome,
# resolved_at, r_multiple, ambiguous)). 직렬화 필드는 딱 "판정의 정체성"만 —
# resolve_price_krw/judgment_mode 등 부가 정보는 제외한다(안 그러면 사소한 필드
# 변경까지 전부 체인 검증 실패로 잡혀 알림이 소음이 된다. 반대로 저 5개 필드가
# 하나라도 바뀌면 반드시 체인이 깨져야 한다 - 그게 곧 hit/miss 판정 자체다).
#
# tip 은 meta 테이블에 저장한다(연결 재시작·회차 사이에도 이어지도록) - 매 resolve_outcome
# 호출이 현재 tip 을 prev_hash 로 쓰고, 새 해시로 tip 을 갱신한다. 이 프로세스는
# 단일 스레드로 순차 실행되므로(run_once 는 동기 루프) tip 읽기→쓰기 사이 경합이 없다.
_OUTCOME_CHAIN_GENESIS = "izrua-outcome-chain-genesis-v1"  # 고정 제네시스 (최초 판정의 prev_hash)
_OUTCOME_CHAIN_TIP_META_KEY = "outcome_chain_tip"


def _outcome_chain_payload(level_id, outcome, resolved_at, r_multiple, ambiguous) -> str:
    """판정 정체성 필드만 직렬화 - 이 문자열이 바뀌면 반드시 해시도 바뀐다.
    float(resolved_at/r_multiple)는 repr() 로 고정한다 - str()은 일부 파이썬
    버전/로케일에서 표현이 달라질 수 있지만 repr() 은 재현 가능한 정규 표현이다."""
    parts = [
        str(int(level_id)),
        str(outcome),
        repr(float(resolved_at)),
        repr(float(r_multiple)) if r_multiple is not None else "None",
        "1" if ambiguous else "0",
    ]
    return "|".join(parts)


def _compute_outcome_hash(prev_hash: str, level_id, outcome, resolved_at,
                          r_multiple, ambiguous) -> str:
    payload = prev_hash + "|" + _outcome_chain_payload(
        level_id, outcome, resolved_at, r_multiple, ambiguous)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _get_chain_tip(conn) -> str:
    return get_meta(conn, _OUTCOME_CHAIN_TIP_META_KEY) or _OUTCOME_CHAIN_GENESIS


def _set_chain_tip(conn, chain_hash: str) -> None:
    set_meta(conn, _OUTCOME_CHAIN_TIP_META_KEY, chain_hash)


def _backfill_outcome_chain(conn) -> int:
    """구세대 판정 행(outcome 은 있는데 outcome_hash 가 없는 - 이 컬럼이 생기기 전에
    이미 종결된 행) 에 1회성으로 소급 체인을 구축한다. 반환: 새로 엮은 행 수.

    순서는 (resolved_at, id) 오름차순으로 결정적으로 고정 - 실제 판정이 확정된
    시간순을 최대한 보존하면서, 동시각(같은 run_once 회차에서 여러 건 종결) 타이는
    id 로 확정한다 - 재실행해도 항상 같은 체인이 나와야 검증이 의미가 있다.
    이미 체인이 있는 행(outcome_hash NOT NULL)은 절대 건드리지 않는다(불변 원칙 —
    한 번 확정된 판정의 해시가 사후에 바뀌면 그 자체가 체인 목적에 반한다)."""
    rows = conn.execute(
        "SELECT id, outcome, resolved_at, r_multiple, ambiguous FROM levels "
        "WHERE outcome IS NOT NULL AND outcome_hash IS NULL "
        "ORDER BY resolved_at ASC, id ASC"
    ).fetchall()
    if not rows:
        return 0
    prev = _get_chain_tip(conn)
    for r in rows:
        h = _compute_outcome_hash(prev, r["id"], r["outcome"], r["resolved_at"] or 0.0,
                                  r["r_multiple"], bool(r["ambiguous"]))
        conn.execute(
            "UPDATE levels SET outcome_prev_hash=?, outcome_hash=? WHERE id=?",
            (prev, h, r["id"]))
        prev = h
    _set_chain_tip(conn, prev)
    return len(rows)


def verify_outcome_chain(conn) -> Optional[dict]:
    """전체 판정 해시체인을 재계산해 첫 불일치 지점을 찾는다. 정상이면 None.

    실제 저장된 prev_hash→hash 포인터를 그대로 링크드리스트처럼 따라가며 재검증한다
    (임의의 정렬 기준을 가정하지 않는다 - 같은 회차에 여러 건이 동시에 체인에
    엮여도 실제 호출 순서가 곧 체인 순서이므로 항상 올바르게 재구성된다).

    반환: None(정상) | {"level_id": int|None, "reason": str}
      - "hash_mismatch": 어떤 행의 판정 필드가 사후에 바뀜(변조)
      - "orphan_not_chained": 체인에 연결되지 않은 행이 있음(중간 행 유실/삭제로
        다음 행이 가리키는 prev_hash 를 가진 행이 사라짐)
      - "missing_hash": outcome 은 있는데 outcome_hash 가 없음(마이그레이션 누락/버그)
      - "broken_genesis": 제네시스에서 시작하는 행이 0개 또는 2개 이상(갈래/유실)
      - "chain_fork": 한 해시에서 다음 행이 2개 이상으로 갈라짐(불가능해야 정상)
    """
    rows = conn.execute(
        "SELECT id, outcome, resolved_at, r_multiple, ambiguous, "
        "outcome_prev_hash, outcome_hash FROM levels WHERE outcome_hash IS NOT NULL"
    ).fetchall()

    def _orphan_missing_hash():
        row = conn.execute(
            "SELECT id FROM levels WHERE outcome IS NOT NULL AND outcome_hash IS NULL "
            "ORDER BY id LIMIT 1").fetchone()
        return {"level_id": row["id"], "reason": "missing_hash"} if row else None

    if not rows:
        return _orphan_missing_hash()

    by_prev: dict = {}
    for r in rows:
        by_prev.setdefault(r["outcome_prev_hash"], []).append(r)

    heads = by_prev.get(_OUTCOME_CHAIN_GENESIS, [])
    if len(heads) != 1:
        return {"level_id": (heads[0]["id"] if heads else rows[0]["id"]),
                "reason": "broken_genesis"}

    visited = []
    cur = heads[0]
    while True:
        expected = _compute_outcome_hash(
            cur["outcome_prev_hash"], cur["id"], cur["outcome"],
            cur["resolved_at"] or 0.0, cur["r_multiple"], bool(cur["ambiguous"]))
        if expected != cur["outcome_hash"]:
            return {"level_id": cur["id"], "reason": "hash_mismatch"}
        visited.append(cur["id"])
        nxts = by_prev.get(cur["outcome_hash"], [])
        if len(nxts) > 1:
            return {"level_id": nxts[1]["id"], "reason": "chain_fork"}
        if not nxts:
            break
        cur = nxts[0]

    if len(visited) != len(rows):
        visited_set = set(visited)
        missing = next(r for r in rows if r["id"] not in visited_set)
        return {"level_id": missing["id"], "reason": "orphan_not_chained"}

    return _orphan_missing_hash()


def resolve_outcome(conn, level_id: int, outcome: str, resolve_price_krw: float,
                    judgment_mode: str, r_multiple: Optional[float] = None,
                    ambiguous: bool = False, best_tp_hit: Optional[int] = None,
                    now: Optional[float] = None) -> None:
    now_val = now or time.time()
    # 체인 계산은 판정 로직/값 자체와 무관 - 이 함수가 실제로 새 판정을 쓸 때만
    # (WHERE outcome IS NULL 에 걸려 rowcount>0 일 때만) tip 을 전진시킨다. 이미
    # 종결된 행에 재호출되면(no-op) tip 도 건드리지 않는다.
    prev_hash = _get_chain_tip(conn)
    chain_hash = _compute_outcome_hash(prev_hash, level_id, outcome, now_val,
                                       r_multiple, ambiguous)
    cur = conn.execute(
        """UPDATE levels SET outcome=?, resolved_at=?, resolve_price_krw=?,
             judgment_mode=?, r_multiple=?, ambiguous=?, best_tp_hit=?,
             outcome_prev_hash=?, outcome_hash=?
           WHERE id=? AND outcome IS NULL""",
        (outcome, now_val, resolve_price_krw, judgment_mode,
         r_multiple, 1 if ambiguous else 0, best_tp_hit,
         prev_hash, chain_hash, level_id),
    )
    if cur.rowcount:
        _set_chain_tip(conn, chain_hash)


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


# ── 글 삭제 감지 (2026-07-26, ACCURACY_DB_PLAN 안티게이밍) ──────────────
# 비용 방어 원칙: 레벨 수십~수백 개를 매번 전수 확인하면 TradingView 부담·차단
# 위험이 커진다 - 호출부(scripts/run_collect.py)가 하루 상한(deletion_check_daily_limit)
# 을 두고 순환 확인한다. 대상은 '종결된' 레벨만(watching/previewed 는 아직 결론이
# 나지 않은 글이라 삭제 여부가 신뢰도 신호로서 의미가 약하고, 확인 비용도 아깝다).


def get_deletion_check_candidates(conn, limit: int, recheck_after_sec: float) -> list:
    """삭제 확인 대상 - 종결(touched/expired) + post_url 보유 + 아직 삭제 미확정
    + (미확인이거나 확인한 지 오래됨) 레벨. 미확인 우선, 그다음 오래전에 수집된
    순(오래된 글일수록 삭제 위험이 누적돼 있을 가능성이 높음)."""
    return [dict(r) for r in conn.execute(
        """SELECT id, post_url, author FROM levels
           WHERE status IN ('touched','expired') AND post_url IS NOT NULL
             AND (deleted IS NULL OR deleted = 0)
             AND (deleted_checked_at IS NULL OR deleted_checked_at < ?)
           ORDER BY (deleted_checked_at IS NOT NULL), collected_at ASC
           LIMIT ?""",
        (time.time() - recheck_after_sec, limit),
    ).fetchall()]


def mark_deletion_checked(conn, level_id: int, deleted: bool, now: Optional[float] = None) -> None:
    """확인 결과 반영. deleted=False 여도 deleted_checked_at 은 갱신해(생존 확인도
    '확인함'으로 기록) 다음 순번이 같은 글을 바로 다시 뽑지 않게 한다."""
    conn.execute(
        "UPDATE levels SET deleted=?, deleted_checked_at=? WHERE id=?",
        (1 if deleted else 0, now or time.time(), level_id),
    )


def get_author_deletion_stats(conn, author: Optional[str]) -> dict:
    """작성자별 삭제 건수 - 나중에 신뢰도 지표로 쓰기 위한 조회 전용 함수
    (ACCURACY_DB_PLAN: '삭제 건수 자체를 신뢰도 신호로'). 분모는 '확인 시도한'
    글 수(checked) - 아직 한 번도 확인 안 된 글은 분모에서 제외해 표본을 왜곡하지
    않는다."""
    if not author:
        return {"checked": 0, "deleted": 0}
    row = conn.execute(
        """SELECT
             SUM(CASE WHEN deleted_checked_at IS NOT NULL THEN 1 ELSE 0 END) AS checked,
             SUM(CASE WHEN deleted = 1 THEN 1 ELSE 0 END) AS deleted
           FROM levels WHERE author=?""",
        (author,),
    ).fetchone()
    return {"checked": row["checked"] or 0, "deleted": row["deleted"] or 0}


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


def expire_levels_for_coin(conn, coin_symbol: str, reason: str,
                           now: Optional[float] = None) -> int:
    """한 코인의 '알림 대기' 레벨을 사유와 함께 즉시 만료. 반환: 만료 건수.
    (2026-07-26 카드 #5 — 업비트 유의종목/거래지원 종료 공지 대응)

    대상은 watching/previewed 뿐이다. 미종결 touched 를 건드리지 않는 이유:
    ① 이미 알림이 나간 레벨이라 만료해도 '앞으로의 알림'을 막는 효과가 없고
    ② 판정 진행 중인 표본을 status 변경으로 판정 대상(get_unresolved_touched)에서
       빼버리면 승패가 조용히 유실돼 적중 DB 가 오염된다. 상장폐지로 시세 조회가
       끊긴 건은 _judge_outcomes 의 기존 '판정불능 제외' 경로가 이미 처리한다.
    outcome 이 이미 확정된 건도 당연히 손대지 않는다(불변 스냅샷 원칙)."""
    now = now or time.time()
    cur = conn.execute(
        "UPDATE levels SET status='expired', expired_at=?, expired_reason=? "
        "WHERE coin_symbol=? AND status IN ('watching','previewed')",
        (now, reason, coin_symbol),
    )
    return cur.rowcount


def get_recent_bid_ask_ratios(conn, limit: int = 10) -> list:
    """최근 터치의 호가 매수/매도 잔량비 기록 (2026-07-26 카드 #19, 관찰 표시용).
    조회 전용 — 어떤 판정에도 쓰이지 않는다."""
    return [dict(r) for r in conn.execute(
        "SELECT coin_symbol, touched_at, touch_bid_ask_ratio, outcome FROM levels "
        "WHERE touch_bid_ask_ratio IS NOT NULL "
        "ORDER BY touched_at DESC LIMIT ?", (limit,)
    ).fetchall()]


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


# ── 관찰 집계 (스프린트5 알림량 관찰기, 2026-07-26) ──────────────────────
# "알림이 많다/적다"를 감이 아니라 숫자로 보기 위한 조용한 누적. 알림 발송과
# 무관하게 매 가격체크 회차(monitor.price_check.run_once)가 하루 1행을 갱신한다.

_DAILY_STATS_COLS = ("touches_total", "previews_total", "suppressed_grade",
                     "suppressed_cap", "suppressed_dup", "suppressed_send_fail",
                     "suppressed_grade_tp_penalty_only",
                     "preview_dwell",
                     # 동시터치(같은 1분봉에 TP·SL 동시 도달) 재검사 결과.
                     # magnified = 체결내역으로 실제 순서를 복원해 확정한 건,
                     # unresolved = 체결내역을 보고도 판별 못 해 보수적 miss 로 남은 건,
                     # skipped = 예산/스위치/구간길이 때문에 체결내역을 못 본 건.
                     # magnified 비율이 곧 판정 신뢰도 지표이고, skipped 은 지표가
                     # 아니라 운영 신호다(예산·스위치를 손보면 줄어든다) — 그래서
                     # 분리한다(2026-07-26 감사 minor, Bar Magnifier 후속).
                     "ambiguous_magnified", "ambiguous_unresolved",
                     "ambiguous_skipped")


def bump_daily_stats(conn, day_kst: str, **deltas) -> None:
    """관찰 집계 증분 반영(회차당 1회 호출 권장 — 여러 클러스터분을 합산해서 넘긴다).
    deltas 키는 _DAILY_STATS_COLS 중 일부만 넘겨도 된다(나머지는 0 취급).
    전부 0이면 쓰기 자체를 생략한다(불필요한 커밋 소음 방지)."""
    vals = {c: int(deltas.get(c, 0)) for c in _DAILY_STATS_COLS}
    if not any(vals.values()):
        return
    conn.execute(
        f"""INSERT INTO daily_stats (day_kst, {', '.join(_DAILY_STATS_COLS)}, updated_at)
            VALUES (?, {', '.join('?' for _ in _DAILY_STATS_COLS)}, ?)
            ON CONFLICT(day_kst) DO UPDATE SET
              {', '.join(f'{c} = {c} + excluded.{c}' for c in _DAILY_STATS_COLS)},
              updated_at = excluded.updated_at""",
        (day_kst, *[vals[c] for c in _DAILY_STATS_COLS], time.time()),
    )


def prune_daily_stats(conn, now: Optional[float] = None, keep_days: int = 60) -> int:
    """보존기간(기본 60일) 넘은 관찰집계 삭제 — DB 무한 증가 방지. 반환: 삭제 행 수."""
    now = now or time.time()
    cutoff_day = datetime.fromtimestamp(now - keep_days * 86400, tz=_KST).strftime("%Y-%m-%d")
    cur = conn.execute("DELETE FROM daily_stats WHERE day_kst < ?", (cutoff_day,))
    return cur.rowcount


def get_daily_stats(conn, days: int = 30) -> list:
    """최근 N일 관찰집계 원본 행(날짜 내림차순). 화면 표시는 get_observation_report 권장."""
    return [dict(r) for r in conn.execute(
        "SELECT * FROM daily_stats ORDER BY day_kst DESC LIMIT ?", (days,)
    ).fetchall()]


def get_collected_counts_by_day(conn, days: int = 30) -> dict:
    """일자별(KST) 신규 수집 건수 — levels.collected_at 원본을 그대로 집계
    (별도 저장 없음, ACCURACY_DB_PLAN 원천 보존 원칙과 동일 취지)."""
    rows = conn.execute(
        "SELECT strftime('%Y-%m-%d', collected_at, 'unixepoch', '+9 hours') AS d, "
        "COUNT(*) AS n FROM levels GROUP BY d ORDER BY d DESC LIMIT ?", (days,)
    ).fetchall()
    return {r["d"]: r["n"] for r in rows}


def get_alerts_sent_by_day(conn, days: int = 30) -> dict:
    """일자별 실제 발송 알림 건수(예고+본알림 합계) — 기존 alerts_log 재활용,
    중복 집계 없음."""
    rows = conn.execute(
        "SELECT day_kst, COUNT(*) AS n FROM alerts_log GROUP BY day_kst "
        "ORDER BY day_kst DESC LIMIT ?", (days,)
    ).fetchall()
    return {r["day_kst"]: r["n"] for r in rows}


def get_observation_report(conn, days: int = 30) -> list:
    """관찰기 판단용 통합 뷰 — 일자별 [수집/터치/예고/발송/억제사유] 한 줄 요약.
    이번 스프린트는 '쌓기'까지가 범위라 아직 어디서도 호출하지 않지만, 다음
    스프린트의 주간 리포트 노출을 위해 조회 함수만 미리 준비해 둔다."""
    collected = get_collected_counts_by_day(conn, days)
    sent = get_alerts_sent_by_day(conn, days)
    stats_rows = get_daily_stats(conn, days)
    by_day = {r["day_kst"]: r for r in stats_rows}
    all_days = sorted(set(collected) | set(sent) | set(by_day), reverse=True)[:days]
    out = []
    for d in all_days:
        s = by_day.get(d, {})
        out.append({
            "day_kst": d,
            "collected": collected.get(d, 0),
            "touches_total": s.get("touches_total", 0),
            "previews_total": s.get("previews_total", 0),
            "alerts_sent": sent.get(d, 0),
            "suppressed_grade": s.get("suppressed_grade", 0),
            "suppressed_cap": s.get("suppressed_cap", 0),
            "suppressed_dup": s.get("suppressed_dup", 0),
            "suppressed_send_fail": s.get("suppressed_send_fail", 0),
            # suppressed_grade 의 부분집합(합산 대상 아님) - TP 거리 감점 효과 분리용
            "suppressed_grade_tp_penalty_only": s.get("suppressed_grade_tp_penalty_only", 0),
        })
    return out
