"""
레벨 상태 DB (SQLite).

한 "레벨" = TradingView 아이디어 글 하나에서 뽑은 (코인 + 엔트리가) 조합.
상태 머신: watching → previewed(엔트리 ±밴드 접근) → touched(엔트리 하향 터치) / expired(7일 경과)

가격 비교의 기준 통화: entry/sl/tp 는 TradingView(USDT 페어) 기준이라 USD 스케일로 저장하고,
KRW 환산은 가격체크 시점의 실시간 USDT/KRW 로 그때그때 계산한다(환율 변동 반영).

값(비밀) 없음 — 이 DB 는 공개 아티팩트로 올라가도 되는 시세/공개글 데이터만 담는다.
"""

import hashlib
import json
import logging
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("alert.db")

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

-- 거래량 급증 알림 감시 목록 (Feature 4, 2026-07-29).
-- 진입가 터치 알림 발송 후 자동 등록, 급증 판정(2026-07-31~: 최근 1h 거래대금 >
-- 직전 20h 완결 60분봉 평균 × multiplier) 시 별도 알림 발송.
-- band_*_krw (2026-07-31): 감시 제외 밴드 [진입가 -10%, 마지막 유효 TP(없으면
-- +10%)] — 터치 시점 환율로 KRW 고정. 현재가가 밴드를 이탈하면 감시를 즉시
-- 종료한다(급증이 나도 이미 셋업을 떠난 가격대라 무의미). 상단은 처음엔 TP1
-- 이었으나 2026-07-31 2차에서 마지막 TP 로 교체 — 백테스트(에피소드 57건)에서
-- 짧은 TP1 셋업의 25%가 1시간 내 조기종료돼 사다리 구간이 사각지대였다.
-- 구세대 행(NULL)은 밴드 체크를 적용하지 않고 시간 만료(watch_hours)만 따른다.
-- tp1_krw/tp_count (2026-07-31 사용자 요청): 급증 알림 TP 행 표시용 스냅샷.
-- tps_krw (2026-07-31 2차): 전체 유효 TP 목록(KRW, JSON 오름차순) — 알림 시점
-- 현재가 바로 위의 "다음 TP (k/N단계)"를 동적으로 고른다. tps_krw 가 있으면
-- tp1_krw/tp_count 는 과도기 행 폴백 표시용. 유효 TP 없는 셋업은 전부 NULL
-- = 알림에서 행 생략.
CREATE TABLE IF NOT EXISTS volume_watch (
    ticker       TEXT PRIMARY KEY,
    coin_symbol  TEXT NOT NULL,
    added_at     REAL NOT NULL,
    alerted      INTEGER DEFAULT 0,
    alerted_at   REAL,
    band_low_krw  REAL,
    band_high_krw REAL,
    tp1_krw      REAL,
    tp_count     INTEGER,
    tps_krw      TEXT
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
    suppressed_dup       INTEGER NOT NULL DEFAULT 0, -- 재발송 차단 건수(2026-07-28~)
        -- ⚠️ 의미가 두 번 바뀐 컬럼이라 날짜로 갈라 읽어야 한다:
        --   ~2026-07-26: '밴드 체류 회차'를 억제로 잘못 세던 자리(MAJOR-1). 값 무의미.
        --   2026-07-26~27: 미사용(0 고정).
        --   2026-07-28~: 회차 경합에 의한 재발송을 alerts_log 로 막은 건수.
        -- 새 컬럼을 파지 않고 이 자리를 쓰는 이유 - 의미가 '중복 억제'로 정확히
        -- 일치하고 이미 휴면이라, 스키마 이관 없이 관측을 되살릴 수 있다.
    preview_dwell        INTEGER NOT NULL DEFAULT 0, -- 예고 밴드 체류 회차(억제 아님)
    suppressed_send_fail INTEGER NOT NULL DEFAULT 0, -- 필터는 통과했으나 텔레그램 발송 실패
    -- suppressed_grade 의 부분집합(중복 카운트 아님!) — 2026-07-26 목표거리 감점 도입
    -- 효과를 분리 측정하려는 사용자 결정. "등급 미달로 억제된 건" 중에서 "그 감점만
    -- 되돌리면 alert_min_grade 를 통과했을 건"만 별도 표시한다. suppressed_grade 는
    -- 이 값을 포함한 채로 그대로 유지(둘 다 봐야 "감점 때문에 억제 vs 원래도 미달"이
    -- 갈린다).
    suppressed_grade_tp_penalty_only INTEGER NOT NULL DEFAULT 0,
    -- 스윙 미달 TP 억제 (2026-07-29 B안) — last TP 기준 2% 미만 신호
    -- (07-30 5%→2% 조정, i-9 주석 정정). 목록은 _volume_band_tps 단일 출처(m-3),
    -- 형제 승계로 클러스터 전체가 미달일 때만 집계(m-4).
    suppressed_tp_too_close  INTEGER NOT NULL DEFAULT 0,
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
    -- TP 단계 알림이 본알림 무발송 게이트(M-2, 2026-08-01)로 차단된 건수.
    -- suppressed_* 와 달리 _judge_outcomes 에서 누적 (가격체크 루프 외부).
    suppressed_tp_gate   INTEGER NOT NULL DEFAULT 0,
    updated_at           REAL
);
"""


def make_signal_key(coin_symbol: str, entry_usd, author: str, post_url: str,
                    source: str = "tradingview") -> str:
    """같은 글의 같은 엔트리를 한 레벨로 식별. 엔트리는 소수 6자리로 라운딩해
    부동소수 미세차로 중복 생성되는 걸 막는다.

    source (2026-07-27 카드 #14): 입력원이 둘 이상이 되면서 서로 다른 소스의 글이
    같은 키로 뭉개지지 않도록 접두사를 넣는다. 기본값 'tradingview' 는 **접두사를
    붙이지 않는다** — 이미 DB 에 쌓인 수천 건의 키가 그대로 유지돼야 하기 때문이다
    (접두사를 무조건 붙이면 기존 레벨 전부가 '신규'로 재삽입돼 중복 알림이 터진다)."""
    entry_str = f"{float(entry_usd):.6f}" if entry_usd is not None else "none"
    raw = f"{coin_symbol}|{entry_str}|{author or ''}|{post_url or ''}"
    if source and source != "tradingview":
        raw = f"{source}|{raw}"
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

# 판정과 무관한 일반 확장 컬럼 — 같은 ALTER 마이그레이션 경로를 쓰되, _OUTCOME_COLUMNS
# (적중 판정 필드)와 성격이 달라 분리해 둔다.
_EXTRA_COLUMNS = {
    # 입력원 (2026-07-27 카드 #14). 'tradingview' | 'telegram'.
    # 왜 남기나: 소스가 둘이 되면 "어느 소스가 더 잘 맞나"가 곧 운영 판단(채널 유지/
    # 퇴출, 비중)의 근거가 된다. 사후에 소스별로 outcome/E_LB 를 가르려면 유입 시점에
    # 찍어두는 수밖에 없다(post_url 로 역추정하는 건 URL 형식이 바뀌면 깨진다).
    # **표시 전용 아님 — 분석 전용**: 알림 양식·필터·등급 어디에도 넣지 않는다.
    # 기존 행은 DEFAULT 로 자동 'tradingview' 가 되어 소급 분류가 정확하다.
    "source": "TEXT DEFAULT 'tradingview'",
    # 목표 사다리 단계 수 (2026-07-27 사용자 승인 A안). 예: "TARGETS: 0.059 - 0.0615
    # - … - 0.085" 이면 8. **표시 전용** — 알림에 "1/8단계"로 붙어 "위로 더 있다"를
    # 알린다. 판정(적중=TP1 도달)과 등급의 TP 거리 배점은 종전대로 첫 목표만 쓴다
    # (바꾸면 기존 34건 표본과 축이 어긋난다). 0/1 이면 꼬리표가 안 붙어 기존 알림과
    # 완전히 동일하다. 기존 행은 DEFAULT 0 이고 raw_text 가 있으면 재파싱이 채운다.
    "tp_ladder_count": "INTEGER DEFAULT 0",
    # 전체 TP 목표가 목록 JSON (2026-07-29 B안). 엔트리 기준 가까운 순으로 정렬된
    # Python list 를 json.dumps 한 TEXT. 빈 목록이면 "[]". 기존 행은 NULL 이다가
    # reparse_all 이 raw_text 로 소급 채운다.
    "tps_usd": "TEXT",
    # 다단계 TP 알림: 현재 감시 중인 TP 인덱스 (0-based, DEFAULT 0 = TP1).
    # TP1 적중 시 1로 전진, TP2 적중 시 2로 전진 … 마지막 TP 적중 시 종결.
    # 단일 TP 레벨은 항상 0 으로 유지되며 기존 판정 경로와 완전히 동일하게 동작한다.
    "tp_alert_idx": "INTEGER DEFAULT 0",
    # 터치 시점 시장 심리 스냅샷 — 표시에만 쓰고 버리던 값을 장기 사후분석용으로 보존.
    # 순수 로깅 컬럼: 알림·필터·등급·판정 어디에도 쓰이지 않는다.
    # 나중에 "F&G 낮을 때 신호 적중률이 더 높은가" 같은 조건부 분석의 원천 데이터.
    "touch_fear_greed": "REAL",       # alternative.me 공포탐욕지수 (0~100)
    "touch_kimchi_pct": "REAL",       # 김치프리미엄 (%)
    "touch_btc_dominance": "REAL",    # BTC 시총 점유율 (%)
    "touch_volume_rank": "INTEGER",   # 업비트 KRW 거래대금 순위
    # 등급 산식 버전 태그 (2026-08-01 S10 v3 — 사용자 결정 D4). 신규 채점 저장분만
    # settings.grade_formula_ver('v3')가 찍히고 **기존 행은 NULL 유지 = 구 산식**.
    # 소급 UPDATE 금지 — 과거 알림이 왜 나갔는지(당시 등급)의 원본 보존 원칙.
    # 캘리브레이션(show_status/run_weekly_report)이 이 값으로 신·구 표본을 분리 집계.
    "grade_ver": "TEXT",
    # TP 발송 실패 시 재시도 마커. 캔들 창 이동으로 다음 회차에서 재탐지 불가능할 때
    # 직전 판정을 복원한다. 발송 성공 또는 resolve 시 NULL 로 복원.
    "pending_tp_kind": "TEXT",
}


def _migrate(conn) -> None:
    """기존 DB에 없는 컬럼만 ALTER 로 추가 (레포 커밋백 DB는 스키마가 과거일 수 있음).

    daily_stats 도 함께 본다 — CREATE TABLE IF NOT EXISTS 는 '테이블이 이미 있으면'
    새 컬럼을 붙여주지 않아서, 운영 DB에 테이블이 생긴 뒤 컬럼을 추가하면 조용히
    누락된다(2026-07-26 ambiguous_* 추가 때 발견). 카운터라 기본값 0 으로 채운다."""
    existing = {r["name"] for r in conn.execute("PRAGMA table_info(levels)").fetchall()}
    for col, decl in list(_OUTCOME_COLUMNS.items()) + list(_EXTRA_COLUMNS.items()):
        if col not in existing:
            conn.execute(f"ALTER TABLE levels ADD COLUMN {col} {decl}")

    ds_cols = {r["name"] for r in conn.execute("PRAGMA table_info(daily_stats)").fetchall()}
    if ds_cols:  # 테이블이 아직 없으면 SCHEMA 가 최신 정의로 만들어준다
        for col in _DAILY_STATS_COLS:
            if col not in ds_cols:
                conn.execute(
                    f"ALTER TABLE daily_stats ADD COLUMN {col} INTEGER NOT NULL DEFAULT 0")
        # 2026-07-28 수리: updated_at 은 INTEGER 카운터가 아니라 REAL(epoch) 이라 별도 처리.
        # _DAILY_STATS_COLS 에 포함하면 "DEFAULT 0 INTEGER" 이 붙어 타입이 틀린다.
        if "updated_at" not in ds_cols:
            conn.execute("ALTER TABLE daily_stats ADD COLUMN updated_at REAL")

    # volume_watch 감시 제외 밴드 (2026-07-31) — daily_stats 와 같은 이유로 기존
    # 테이블에는 ALTER 로만 붙는다. NULL 허용·DEFAULT 없음(SQLite ADD COLUMN 제약
    # + 구세대 행은 NULL = "밴드 없음, 시간 만료만"으로 하위호환).
    vw_cols = {r["name"] for r in conn.execute("PRAGMA table_info(volume_watch)").fetchall()}
    if vw_cols:  # 테이블이 아직 없으면 SCHEMA 가 최신 정의로 만들어준다
        for col in ("band_low_krw", "band_high_krw", "tp1_krw"):
            if col not in vw_cols:
                conn.execute(f"ALTER TABLE volume_watch ADD COLUMN {col} REAL")
        if "tp_count" not in vw_cols:
            conn.execute("ALTER TABLE volume_watch ADD COLUMN tp_count INTEGER")
        if "tps_krw" not in vw_cols:
            conn.execute("ALTER TABLE volume_watch ADD COLUMN tps_krw TEXT")

    # 적중 판정 해시체인 소급 구축 (2026-07-27 카드 #3) — outcome_hash 컬럼이 방금
    # 생겼거나 과거 판정 행이 있으면(레포 커밋백 DB) 1회성으로 체인을 이어붙인다.
    # 이미 체인이 있는 행은 WHERE 절이 걸러내 매 init_db 호출마다 사실상 공짜(no-op).
    _backfill_outcome_chain(conn)

    # grade_v3_since (2026-08-01 S10 §6-3 감사 추적): v3 산식 배포 이후 첫 회차가
    # 여기(모든 엔트리포인트가 지나는 init_db 마이그레이션)서 1회 기록한다 —
    # run_cycle 배선 추가 없이 '이 DB 에 v3 채점이 언제부터 섞였나'를 남기는 목적.
    # 이미 있으면 no-op (meta 조회 1건).
    if get_meta(conn, "grade_v3_since") is None:
        set_meta(conn, "grade_v3_since", str(time.time()))


def _maybe_audit_dump(conn, db_path: str) -> None:
    """주간 감사 덤프 훅 (2026-07-27 기획 카드 #4 — storage/audit_dump.py 참고).

    왜 여기인가: 덤프는 "DB 를 커밋백하는 그 잡"에서 돌아야 같은 커밋에 실리는데,
    그 회차 엔트리포인트(scripts/run_cycle.py, monitor/price_check.py)는 지금 다른
    세션이 잡고 있어 손댈 수 없다. 두 파일 모두 회차 시작 시 init_db 를 정확히
    한 번 부르므로(그리고 이 함수는 이미 _backfill_outcome_chain 같은 1회성
    유지보수를 품고 있으므로) 여기에 붙이면 배선 추가 없이 라이터 잡 안에서 돈다.
    실제 수행 여부는 audit_dump 쪽 주간 meta 게이트가 정한다 — 매 회차 비용은
    meta 조회 1건.

    감사 기능의 어떤 실패도 회차를 죽이면 안 된다 — 통째로 삼키고 경고만 남긴다."""
    try:
        from storage import audit_dump
        audit_dump.maybe_weekly_audit(conn, db_path)
    except BaseException as e:  # noqa: BLE001 - 감사 실패가 회차를 죽이면 안 된다
        if isinstance(e, KeyboardInterrupt):
            raise
        logger.warning("감사 덤프 훅 실패(무시하고 진행): %s: %s", type(e).__name__, e)


def init_db(db_path: str) -> None:
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)
        _maybe_audit_dump(conn, db_path)


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
                raw_text, source, tp_ladder_count, tps_usd, grade_ver)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
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
                # 소스 미지정이면 tradingview (기존 호출부 무수정 호환)
                level.get("source") or "tradingview",
                level.get("tp_ladder_count") or 0,   # 표시 전용(알림 "1/N단계")
                level.get("tps_usd") or "[]",         # 전체 TP 목표가 목록 JSON
                # 산식 버전 태그 (2026-08-01 v3). 미지정(None)=구 호출부/테스트 호환
                level.get("grade_ver"),
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
             grade=?, score=?, rr=?, grade_ver=COALESCE(?, grade_ver),
             sl_usd=?, tp_usd=?,
             tp_ladder_count=?,
             tps_usd=COALESCE(?, tps_usd),
             author_followers=?, author_hit_rate=?, author_hit_count=?,
             author_whitelisted=?, mcap_rank=?, mcap_tier_icon=?,
             judgment_window_hours=?, raw_text=COALESCE(?, raw_text)
           WHERE signal_key=? AND status IN ('watching','previewed')""",
        (
            level.get("grade"), level.get("score"), level.get("rr"),
            # 재수집 재채점은 현재 산식으로 이뤄지므로 버전도 함께 갱신.
            # None(구 호출부)은 COALESCE 로 기존 값 보존 — 소급 재라벨 아님.
            level.get("grade_ver"),
            level.get("sl_usd"), level.get("tp_usd"),
            level.get("tp_ladder_count") or 0,
            # None → COALESCE 가 기존 값을 보존 (backfill 값 보호).
            # "[]" → 명시적 클리어(유효 TP 없는 재수집 결과)라 그대로 덮어씀.
            level.get("tps_usd"),
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
        "SELECT id, entry_usd, sl_usd, tp_usd, rr, judgment_window_hours, raw_text, "
        "tp_ladder_count, tps_usd "
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
        # 사다리 단계 수 치유 (tp 폐기 시 0으로)
        new_lad = (setup.get("tp_ladder_count") or 0) if new_tp is not None else 0
        # 전체 TP 목록 치유 (tp 폐기 시 [])
        new_tps_usd = json.dumps(setup.get("tps_all") or []) if new_tp is not None else "[]"
        old_tps_usd = r["tps_usd"] or "[]"
        if (new_sl == r["sl_usd"] and new_tp == r["tp_usd"]
                and rr == r["rr"] and win == r["judgment_window_hours"]
                and new_lad == (r["tp_ladder_count"] or 0)
                and new_tps_usd == old_tps_usd):
            continue
        conn.execute(
            "UPDATE levels SET sl_usd=?, tp_usd=?, rr=?, judgment_window_hours=?, "
            "tp_ladder_count=?, tps_usd=? WHERE id=?",
            (new_sl, new_tp, rr, win, new_lad, new_tps_usd, r["id"]),
        )
        changed += 1
    return changed


def get_active_levels(conn, direction: Optional[str] = "long") -> list:
    """감시 중(watching/previewed)인 레벨. 기본은 long 만 (하향 터치 알림 대상).

    나이 게이트를 여기 따로 두지 않는 이유: price_check.run_once 가 매 회차
    이 조회 직전에 expire_old 를 부르므로(2026-07-27 확인) 만료 대상은 이미
    status='expired' 로 빠져 있다. 같은 조건을 두 곳에 두면 규칙 출처가 갈린다."""
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
                 bid_ask_ratio: Optional[float] = None,
                 fear_greed: Optional[float] = None,
                 kimchi_pct: Optional[float] = None,
                 btc_dominance: Optional[float] = None,
                 volume_rank: Optional[int] = None) -> None:
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
                "touch_usdt_krw=?, touch_bid_ask_ratio=?, "
                "touch_fear_greed=?, touch_kimchi_pct=?, touch_btc_dominance=?, touch_volume_rank=? "
                "WHERE id=? AND status IN ('watching','previewed')",
                (t_anchor or now, price, usdt_krw, bid_ask_ratio,
                 fear_greed, kimchi_pct, btc_dominance, volume_rank, lid))


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


def author_closed_stats(conn, author: Optional[str]) -> tuple:
    """작성자의 (종결 표본 수 n, TP1 도달 hit 수) — 등급 v3 실적 가점의 원천
    (2026-08-01 S10 안2, collector.grading.author_track_points 입력).

    표본 정의는 캘리브레이션(analytics/calibration.py)과 같은 축 —
    분모 = outcome 확정 전체(타임박스 포함), 분자 = outcome='hit' 만
    (timeboxed_win 은 목표가를 실제로 찍은 게 아니라 분자에서 제외).
    작성자 없음/미상은 (0, 0) = 가점 0(중립). E_LB(ranking, R 트랙·최신성 가중)와는
    다른 축이다 — 등급 가점은 게이트 있는 고정 배점표라 단순 원시 비율을 쓴다."""
    if not author:
        return 0, 0
    row = conn.execute(
        "SELECT COUNT(*) AS n, "
        "SUM(CASE WHEN outcome='hit' THEN 1 ELSE 0 END) AS h "
        "FROM levels WHERE author=? AND outcome IS NOT NULL",
        (author,)).fetchone()
    return row["n"] or 0, row["h"] or 0


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


def get_author_last_n_snapshots(conn, author: str, n: int = 2) -> list:
    """최근 N 주차 스냅샷을 최신순으로 반환. 역신호 연속 판정용.

    기존 get_author_snapshots()(전체/다수 주차 조회용)와 구분되는 전용 함수 —
    "최근 2주만 빠르게"가 목적이다 (S9 기획 §4-1 A)."""
    rows = conn.execute(
        "SELECT * FROM author_snapshots WHERE author=? "
        "ORDER BY week_kst DESC LIMIT ?",
        (author, n)
    ).fetchall()
    return [dict(r) for r in rows]


# ── 역신호 확정 상태 (S9, 2026-08-01 사용자 결정 Q1=B안/Q2=해제 있음) ──────
# 확정 상태는 meta 에 저장한다: key = reverse_confirmed_{author}, value = 확정 주차
# (예: "2026-W31"). 해제 시 value 를 "" 로 비운다(빈 값 = 미확정) — meta 에 삭제
# API 가 없고, 키 잔존 자체가 "한 번 확정됐던 이력"의 흔적이라 감사에도 낫다.
# 판정·경보는 scripts/run_cycle.py 의 스냅샷 훅이 담당하고, 여기는 키 규약과
# 조회만 둔다(show_status 읽기 전용 표시와 라이터가 같은 규약을 쓰도록 단일 출처).

REVERSE_CONFIRM_META_PREFIX = "reverse_confirmed_"


def reverse_confirm_key(author: str) -> str:
    return f"{REVERSE_CONFIRM_META_PREFIX}{author}"


def get_reverse_confirmed_authors(conn) -> set:
    """현재 역신호 '확정' 상태인 작성자 집합 (해제된 빈 값은 제외)."""
    rows = conn.execute(
        "SELECT key, value FROM meta WHERE key LIKE ?",
        (REVERSE_CONFIRM_META_PREFIX + "%",)).fetchall()
    return {r["key"][len(REVERSE_CONFIRM_META_PREFIX):] for r in rows if r["value"]}


def list_snapshot_authors(conn) -> list:
    """스냅샷이 1주라도 있는 작성자 목록 — 역신호 판정 순회 대상."""
    return [r["author"] for r in conn.execute(
        "SELECT DISTINCT author FROM author_snapshots").fetchall()]


def get_ret24_values(conn) -> list:
    """터치 후 24h 수익률(%) 실측값 목록 — 주간 리포트의 초과 적중률 베이스라인용
    (analytics.clustering.baseline_positive_rate 가 양수 비율로 환산).

    표본 정의를 랭킹 쪽과 맞춘다: 실제 도달 터치(touched_at NOT NULL)만.
    종결 여부는 묻지 않는다 — ret_24h 는 조기 종결건에도 계속 기록되므로
    (get_ret_pending 주석 참고) 종결로 거르면 도리어 생존편향이 들어간다."""
    return [r["ret_24h"] for r in conn.execute(
        "SELECT ret_24h FROM levels WHERE touched_at IS NOT NULL AND ret_24h IS NOT NULL"
    ).fetchall()]


def get_closed_r_rows(conn) -> list:
    """R-멀티플 분포 분석용 원천 행 (2026-08-01 내부기능강화 리서치, analytics.distribution
    이 소비). r_multiple 이 NULL(SL 미기재 tp_only 표본)인 행은 그 지표 자체가 R 트랙
    표본만 다루므로 여기서부터 제외 — E_LB(ranking.py)와 동일한 축 원칙."""
    return [dict(r) for r in conn.execute(
        "SELECT r_multiple, grade FROM levels WHERE r_multiple IS NOT NULL "
        "AND outcome IS NOT NULL"
    ).fetchall()]


def get_closed_holding_rows(conn) -> list:
    """보유기간(터치~종결 경과시간) 분석용 원천 행 (2026-08-01 내부기능강화 리서치,
    analytics.distribution 이 소비). 섀도 터치(touched_at NULL)는 애초에 종결
    판정 대상이 아니라 자동 제외된다(get_unresolved_touched 와 동일 표본 기준)."""
    return [dict(r) for r in conn.execute(
        "SELECT touched_at, resolved_at, outcome FROM levels "
        "WHERE touched_at IS NOT NULL AND resolved_at IS NOT NULL "
        "AND outcome IS NOT NULL"
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


def advance_tp_alert_idx(conn, level_id: int, old_idx: int, new_idx: int) -> bool:
    """tp_alert_idx 를 old_idx → new_idx 로 원자적으로 전진시킨다.

    WHERE tp_alert_idx = old_idx 조건으로 경합 회차가 동시에 같은 TP 를 두 번
    알림 보내는 것을 방지한다. 반환 True = 전진 성공(이 회차가 알림 전송 권한을
    가짐), False = 이미 다른 회차가 전진시켰거나 레벨 상태 불일치."""
    cur = conn.execute(
        "UPDATE levels SET tp_alert_idx=? WHERE id=? AND tp_alert_idx=? AND outcome IS NULL",
        (new_idx, level_id, old_idx),
    )
    return cur.rowcount > 0


def set_pending_tp(conn, level_id: int, kind: str) -> None:
    conn.execute("UPDATE levels SET pending_tp_kind=? WHERE id=?", (kind, level_id))


def clear_pending_tp(conn, level_id: int) -> None:
    conn.execute("UPDATE levels SET pending_tp_kind=NULL WHERE id=?", (level_id,))


def get_author_self_stats(conn, author: str) -> dict:
    """자체 적중 DB 기준 작성자 성적.
    승 = hit + timeboxed_win, 패 = miss + timeboxed_loss. **단 R 트랙 표본만** —
    아래 주석 참고.
    터치율(선택편향 처방, ACCURACY_DB_PLAN): 도달터치 ÷ (도달터치 + 미터치만료).

    ⚠️ 승/패의 분자·분모는 `r_multiple IS NOT NULL` 인 종결 행으로 한정한다
    (2026-07-27 교차감사 B-m1 수리). 이유:
      · 같은 날 오전에 알림의 자체 승률 '표시 게이트'를 neff_win(판정 방식 무관 전체
        종결) → neff_r(R 트랙) 로 옮겼는데, 여기 wins/losses 는 여전히 전체 합산이라
        "게이트는 R 트랙인데 화면에 뜨는 숫자는 전체 표본"인 어긋남이 남아 있었다.
      · 지금은 혼합형(tp_only + tp_sl 을 섞어 쓰는) 작성자가 0명이라 게이트를 통과하는
        작성자의 전체 합산 = R 트랙 합산이어서 표시값이 같다. 혼합형이 한 명이라도
        생기는 순간 승률 줄이 '경고 판정과 다른 표본'을 보여주기 시작한다 — 화면 변화
        0으로 고칠 수 있는 창이 지금뿐이라 선반영한다(실측: 프로덕션 39명 전원
        표시값 불변, 변경은 tp_only 전용 2명의 미표시 내부값뿐).
      · 이제 승률 줄과 역신호(E_LB) 판정이 같은 표본을 본다 = 승률이 보이면 그 숫자는
        SL 까지 명시돼 R 로 채점된 건들만 센 것이다.
    touched/untouched(터치율 축)는 그대로 전체 표본이다 — 선택편향 처방이라 '알림이
    나간 뒤 실제로 닿았는가'를 재는 것이고, 판정 방식과 무관한 별개 축이다.
    touched_at IS NOT NULL 을 함께 거는 것은 get_author_outcome_rows(=E_LB 원천)의
    섀도 터치 제외와 표본을 글자 그대로 일치시키기 위함이다(현 데이터엔 해당 행 0건).
    """
    if not author:
        return {"wins": 0, "losses": 0, "touched": 0, "untouched_expired": 0}
    row = conn.execute(
        """SELECT
             SUM(CASE WHEN outcome IN ('hit','timeboxed_win')
                       AND r_multiple IS NOT NULL AND touched_at IS NOT NULL
                      THEN 1 ELSE 0 END) AS w,
             SUM(CASE WHEN outcome IN ('miss','timeboxed_loss')
                       AND r_multiple IS NOT NULL AND touched_at IS NOT NULL
                      THEN 1 ELSE 0 END) AS l,
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
    """수집 후 max_age_sec 지난 미터치 레벨을 expired 처리. 반환: 만료 건수.

    ⚠️ 기준은 '수집 시각'이다 — 게시 시각으로 바꾸지 말 것 (2026-07-27 실측 검증).
    HANDOFF 가 오래 "게시 7일 만료"로 적어와 한 번 게시 기준으로 바꿨다가 되돌렸다.
    근거:
      · 실제 터치 67건 중 9건(13.4%)이 게시 후 7일을 넘겨 터치됐고, 그 9건 전부
        수집 당시 이미 5.4~6.6일 된 글이었다. 즉 '오래 묵은 셋업'이 아니라
        우리가 늦게 주운 글이고, 수집 시점부터는 0.5~4.0일 만에 터치했다.
      · 늦은 터치가 성적이 나쁘지도 않다(게시 7일+ 승률 50%·평균 -0.21R vs
        0~3일 48%·-0.69R). 잘라낼 근거가 없다.
      · 게시 기준이면 수집이 늦어질수록 감시창이 깎여, 5~7일에 수집된 17.5% 는
        감시 시간이 하루도 안 남는다. 수집 지연(차단·순환)이 곧 기회 상실이 된다.
    수집 게이트(max_post_age_hours=168)가 있어 이 기준의 절대 상한은 자동으로
    게시 후 14일이다 — 실측 최장 터치가 10.5일이라 충분히 여유롭다."""
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


def recent_alert_exists(conn, coin_symbol: str, kind: str, level_ids: list,
                        since: float) -> bool:
    """같은 (코인, 종류, 레벨묶음) 알림이 since 이후에 이미 나갔는가 — 재발송 차단용.

    2026-07-28 실사고: 03:53·03:54 에 DOT·ENA 터치와 AAVE 예고가 각각 두 번 나갔다.
    회차 경합이 원인이다 — 뒤 회차가 앞 회차의 커밋 '이전' 상태로 체크아웃돼 상태
    전이(status='touched')를 못 본 채 같은 알림을 다시 보냈다. 같은 시각 TV 차단
    경보도 하루 1회 상한을 뚫고 두 번 나갔는데(카운터는 1로 남음), 그게 '앞 회차
    기록이 없는 DB를 읽었다'는 확정 증거다.

    levels.status 전이와 달리 alerts_log 는 append-only 라 경합 회차가 서로의 행을
    덮어쓰지 않는다 → 상태 전이보다 한 겹 더 단단한 방어선이다. level_ids 는
    record_alert 와 같은 방식(정렬 없이 그대로 콤마 결합)으로 맞춘다 — 같은
    클러스터면 호출부가 같은 순서로 넘긴다."""
    # 2026-07-28 수리: sorted() 로 순서 고정 — build_clusters 출력 순서 변화 시
    # 같은 클러스터가 다른 키를 만들어 재발송 차단이 조용히 무력화되는 것 방지.
    # alert_ledger._key 와 record_alert 세 곳 모두 동시 적용해야 계약이 유지된다.
    key = ",".join(str(i) for i in sorted(level_ids))
    row = conn.execute(
        "SELECT 1 FROM alerts_log WHERE coin_symbol=? AND kind=? AND level_ids=? "
        "AND sent_at >= ? LIMIT 1",
        (coin_symbol, kind, key, since),
    ).fetchone()
    return row is not None


def touch_alert_sent(conn, level_id: int) -> bool:
    """이 레벨이 포함된 터치 본알림이 실제 발송된 적 있는지 (S9 통합감사 M-2,
    2026-07-31 카드 확정). TP 단계 알림 게이트 전용 — 본알림이 억제(등급·B안)된
    신호는 목표 달성 알림도 내지 않는다. level_ids 가 콤마 CSV 라 경계를 포함해
    검색한다. 커밋백 실패로 alerts_log 가 유실된 회차는 이론상 위음성(알림 생략)
    가능 — 게이트 방향의 보수적 실패라 허용."""
    row = conn.execute(
        "SELECT 1 FROM alerts_log WHERE kind='touch' AND "
        "(','||level_ids||',') LIKE ? LIMIT 1",
        (f"%,{level_id},%",)).fetchone()
    return row is not None


def record_alert(conn, coin_symbol: str, kind: str, level_ids: list, day_kst: str,
                 now: Optional[float] = None) -> None:
    conn.execute(
        "INSERT INTO alerts_log (coin_symbol, kind, level_ids, sent_at, day_kst) VALUES (?,?,?,?,?)",
        (coin_symbol, kind, ",".join(str(i) for i in sorted(level_ids)), now or time.time(), day_kst),
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
                     "suppressed_grade_tp_penalty_only", "suppressed_tp_too_close",
                     "preview_dwell",
                     # 동시터치(같은 1분봉에 TP·SL 동시 도달) 재검사 결과.
                     # magnified = 체결내역으로 실제 순서를 복원해 확정한 건,
                     # unresolved = 체결내역을 보고도 판별 못 해 보수적 miss 로 남은 건,
                     # skipped = 예산/스위치/구간길이 때문에 체결내역을 못 본 건.
                     # magnified 비율이 곧 판정 신뢰도 지표이고, skipped 은 지표가
                     # 아니라 운영 신호다(예산·스위치를 손보면 줄어든다) — 그래서
                     # 분리한다(2026-07-26 감사 minor, Bar Magnifier 후속).
                     "ambiguous_magnified", "ambiguous_unresolved",
                     "ambiguous_skipped",
                     # TP 단계 알림이 본알림 무발송 게이트(M-2, 2026-08-01)로 차단된 건수.
                     "suppressed_tp_gate")


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


def prune_raw_text(conn, now: Optional[float] = None, keep_days: int = 14) -> int:
    """종결(touched/expired) 레벨의 글 원문(raw_text)을 보존기간 후 비운다.
    반환: 비운 행 수. (prune_daily_stats 와 같은 '보존정책' 계열 함수)

    왜 비워도 되나: raw_text 의 유일한 소비처는 재파싱 자가치유(reparse_all)이고,
    그건 `status IN ('watching','previewed')` 인 활성 행만 본다. 업서트 경로의
    raw_text 갱신도 같은 조건이라(upsert_level) 종결 행의 원문은 런타임에서 다시
    읽힐 경로가 없다. 그런데 이 컬럼이 DB 용량의 절반 가까이를 차지하고, DB 는
    2분마다 레포에 바이너리로 통째 커밋되므로 그 무게가 매 커밋마다 반복된다.

    왜 종결 '전이 시점'이 아니라 주기 정리인가: 원문은 감사 덤프(ndjson)에 아카이브된
    뒤에만 지워야 한다(storage/audit_dump.py 참고 — 덤프 → 정리 순서가 안전장치다).
    전이 시점에 즉시 비우면 주 1회 덤프가 그 원문을 한 번도 못 본 채 영구 소실된다.
    보존기간(기본 14일)은 주간 덤프 주기(7일)의 2배 — 한 주 덤프가 실패해 백오프로
    밀려도 다음 주 덤프가 반드시 원문을 담고 지나간다.

    복원 불가 지점을 최소화하려고 조건을 좁게 잡는다:
      · status 가 종결(touched/expired)이고 (활성 행은 절대 건드리지 않는다)
      · 종결 시각(resolved_at > expired_at > touched_at > collected_at 순 폴백)이
        보존기간을 넘긴 행만. 섀도 터치(touched_at NULL)는 collected_at 로 떨어진다.
    """
    now = now or time.time()
    cutoff = now - keep_days * 86400
    cur = conn.execute(
        "UPDATE levels SET raw_text=NULL "
        "WHERE raw_text IS NOT NULL AND status IN ('touched','expired') "
        "AND COALESCE(resolved_at, expired_at, touched_at, collected_at) < ?",
        (cutoff,),
    )
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


def _json_list(raw) -> list:
    """JSON 문자열 → 숫자 리스트. 파싱 실패·None → 빈 리스트."""
    if not raw:
        return []
    try:
        return [x for x in json.loads(raw) if isinstance(x, (int, float))]
    except (ValueError, TypeError):
        return []


def add_volume_watch(conn, ticker: str, coin_symbol: str, now: float,
                     band_low_krw: Optional[float] = None,
                     band_high_krw: Optional[float] = None,
                     tp1_krw: Optional[float] = None,
                     tp_count: Optional[int] = None,
                     tps_krw: Optional[str] = None,
                     max_age_sec: Optional[float] = None) -> None:
    """터치 알림 발송 후 거래량 급증 감시 목록에 추가.

    이미 감시 중(alerted=0)이면 건드리지 않는다 — 기존 감시 타이머를 재설정하지 않아야
    "72h 내 한 번"이라는 창이 유지된다. 이미 발송된(alerted=1) 행이면 리셋한다 —
    같은 ticker 라도 새 터치는 새 감시 대상이다. 없으면 INSERT.

    band_*_krw (2026-07-31): 감시 제외 밴드 [진입가 -10%, TP1(없으면 +10%)],
    터치 시점 환율 KRW 고정. 활성 감시(alerted=0) 중 재터치는 타이머(added_at)는
    유지하되 **밴드는 합집합으로 확장**한다(low=min, high=max, NULL=무경계 우선) —
    2026-07-31 감사 major: 밴드를 첫 터치 것으로 고정하면 아래쪽 클러스터 재터치
    직후 현재가가 옛 밴드 밖이라 같은 회차의 밴드 이탈 판정이 방금 유효해진 감시를
    통째로 삭제했다(무성 커버리지 소실). 합집합이면 두 셋업 가격대를 모두 커버한다.

    tp1_krw/tp_count/tps_krw(2026-07-31→08-01 i-12): 급증 알림 표시용 스냅샷.
    활성 재터치는 밴드 합집합과 일관되게 TP 목록도 합집합 병합한다 — 확장된
    밴드에서 "다음 TP" 표시가 옛 셋업만 참조하는 불일치를 제거.

    max_age_sec(2026-07-31 2인검토 B-1): alerted=0 이지만 이미 감시창(72h)을 넘긴
    (아직 prune 전인) 행이 "활성" 분기로 빠지면 타이머가 안 갱신돼, 같은 회차
    말미의 prune 이 방금 재터치로 유효해진 감시를 삭제한다(무성 소실 잔존 변종,
    경합창 ~1회차). 창을 넘긴 행은 alerted=1 과 동일하게 전면 리셋한다."""
    row = conn.execute(
        "SELECT alerted, added_at, band_low_krw, band_high_krw, tp1_krw, tp_count, tps_krw "
        "FROM volume_watch WHERE ticker=?", (ticker,)).fetchone()
    stale = (row is not None and max_age_sec is not None
             and now - row["added_at"] > max_age_sec)
    if row is None:
        conn.execute(
            """INSERT INTO volume_watch (ticker, coin_symbol, added_at,
                                         band_low_krw, band_high_krw, tp1_krw,
                                         tp_count, tps_krw)
               VALUES (?,?,?,?,?,?,?,?)""",
            (ticker, coin_symbol, now, band_low_krw, band_high_krw, tp1_krw,
             tp_count, tps_krw))
    elif row["alerted"] or stale:
        # 발송 완료 후(또는 감시창을 넘긴 행에) 새 터치 — 새 감시로 전면 리셋
        # (타이머·밴드·TP 모두)
        conn.execute(
            """UPDATE volume_watch SET coin_symbol=?, added_at=?, alerted=0,
                   alerted_at=NULL, band_low_krw=?, band_high_krw=?,
                   tp1_krw=?, tp_count=?, tps_krw=?
               WHERE ticker=?""",
            (coin_symbol, now, band_low_krw, band_high_krw, tp1_krw, tp_count,
             tps_krw, ticker))
    else:
        # 활성 감시 중 재터치 — 타이머 유지("72h 내 한 번" 창 보존), 밴드+TP 합집합
        lo = (None if row["band_low_krw"] is None or band_low_krw is None
              else min(row["band_low_krw"], band_low_krw))
        hi = (None if row["band_high_krw"] is None or band_high_krw is None
              else max(row["band_high_krw"], band_high_krw))
        old_tps = _json_list(row["tps_krw"])
        new_tps = _json_list(tps_krw)
        merged = sorted(set(old_tps + new_tps)) if (old_tps or new_tps) else []
        if merged:
            m_tps_krw = json.dumps(merged)
            m_tp1 = merged[0]
            m_count = len(merged)
        else:
            m_tps_krw = tps_krw if row["tps_krw"] is None else row["tps_krw"]
            m_tp1 = tp1_krw if row["tp1_krw"] is None else row["tp1_krw"]
            m_count = tp_count if row["tp_count"] is None else row["tp_count"]
        conn.execute(
            """UPDATE volume_watch SET coin_symbol=?, band_low_krw=?, band_high_krw=?,
                   tp1_krw=?, tp_count=?, tps_krw=?
               WHERE ticker=?""",
            (coin_symbol, lo, hi, m_tp1, m_count, m_tps_krw, ticker))


def get_volume_watch_active(conn, now: float, max_age_sec: float) -> list:
    """미발송(alerted=0) 항목 중 max_age_sec 이내에 등록된 것만 반환."""
    cutoff = now - max_age_sec
    return [dict(r) for r in conn.execute(
        "SELECT ticker, coin_symbol, added_at, band_low_krw, band_high_krw, "
        "tp1_krw, tp_count, tps_krw "
        "FROM volume_watch WHERE alerted=0 AND added_at >= ?", (cutoff,)
    ).fetchall()]


def remove_volume_watch(conn, ticker: str) -> None:
    """감시 즉시 종료 (2026-07-31 — 밴드 이탈 시 호출). 시간 만료(prune)와 별개."""
    conn.execute("DELETE FROM volume_watch WHERE ticker=?", (ticker,))


def mark_volume_alerted(conn, ticker: str, now: float) -> None:
    """거래량 급증 알림 발송 완료 표시."""
    conn.execute(
        "UPDATE volume_watch SET alerted=1, alerted_at=? WHERE ticker=?", (now, ticker))


def prune_volume_watch(conn, now: float, max_age_sec: float) -> None:
    """만료(추가 후 max_age_sec 초과)된 항목 제거."""
    cutoff = now - max_age_sec
    conn.execute("DELETE FROM volume_watch WHERE added_at < ?", (cutoff,))


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
            # B안(2026-07-29) 마지막 TP 2% 미달 억제 카운터 (07-30 5%→2%)
            "suppressed_tp_too_close":          s.get("suppressed_tp_too_close", 0),
            # MAJOR-1(2026-07-26): 예고 체류 회차·Bar Magnifier 집계
            "preview_dwell":                    s.get("preview_dwell", 0),
            "ambiguous_magnified":              s.get("ambiguous_magnified", 0),
            "ambiguous_unresolved":             s.get("ambiguous_unresolved", 0),
            "ambiguous_skipped":                s.get("ambiguous_skipped", 0),
            # M-2(2026-08-01): TP 단계 알림이 본알림 게이트로 차단된 건수 — 리뷰 발견
            # (2026-08-01): 컬럼은 저장되는데 이 리포트에 빠져 있어 관찰 불가였다.
            "suppressed_tp_gate":                s.get("suppressed_tp_gate", 0),
        })
    return out
