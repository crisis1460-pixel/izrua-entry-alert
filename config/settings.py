"""
설정/비밀정보 로더 — 모든 비밀값은 오직 환경변수에서만 읽는다.

보안 원칙 (공개 레포 전제):
- 이 파일에도, 다른 어떤 파일에도 실제 키를 하드코딩하지 않는다.
- 로컬 개발: python-dotenv 가 있으면 .env 를 읽는다(.env 는 .gitignore).
- 운영: GitHub Actions 가 Secrets 를 환경변수로 주입한다.
- 비밀값은 로그로 절대 출력하지 않는다. mask() 로 존재 여부만 확인한다.
"""

import os

# 운영 파라미터 (비밀 아님 — 공개돼도 무방, 자유롭게 조정)
SETTINGS = {
    # 유니버스
    "universe_top_n": 200,               # CoinGecko 시총 상위 N
    "universe_refresh_hours": 24,        # 시총 목록 갱신 주기

    # 시총 등급 경계 (심볼 옆 아이콘)
    "mcap_tiers": [                      # (상한 순위, 아이콘, 라벨)
        (10, "💎", "초대형"),
        (50, "🥇", "대형"),
        (100, "🥈", "중형"),
        (200, "🥉", "소형"),
    ],

    # 수집
    # 2026-07-26 구조 개선: 수집은 별도 잡이 아니라 가격체크 회차(run_cycle.py)가
    # 흡수한다. 아래 값이 실제 주기 판정 기준이다(meta.last_collect_at 과 비교).
    "collect_interval_hours": 4,         # TradingView 수집 주기 (실효값)
    "collect_retry_minutes": 30,         # 수집 실패 후 재시도까지 백오프 (2분마다 재시도 방지)
    "max_post_age_hours": 168,           # 7일 이내 글만 수집
    "tv_fetch_sleep_sec": 5.0,           # 심볼당 요청 간격. 3.0 이던 2026-07-26 실측:
                                          # 쿠키(TV_COOKIE) 등록 후에도 61번째에서 403 —
                                          # 신원이 아닌 속도 제한으로 추정돼 완충을 확대.
                                          # 예산 81심볼×~5.5s≈7.4분 < 수집 타임아웃 12분.
                                          # 커버리지 보장은 순환(collect_universe_offset)
                                          # 몫이고 페이싱은 차단 빈도 완화용.
    "tv_empty_rest_sec": 30.0,           # 연속 0건 시 휴식

    # 텔레그램 공개채널 소스 (2026-07-27 기획 카드 #14 — 두 번째 입력원)
    # TradingView 단일 의존이 구조적 위험이라(403 한 번에 회차 수집 0건) t.me/s/{채널}
    # 공개 미리보기 HTML 을 두 번째 경로로 붙였다. 상세는 collector/telegram_source.py.
    # ⚠️ 기본 OFF + 빈 화이트리스트가 이 기능의 안전장치다 — 아래 두 값을 건드리기
    # 전까지 코드가 배포돼도 요청 0건·동작 변화 0 이다. 채널 목록은 사장님 승인 사항.
    # 켜는 법: enabled 를 True 로, channels 에 채널명(@ 없이)을 넣는다.
    "telegram_source_enabled": False,
    "telegram_source_channels": [],      # 예: ["some_public_channel"]
    "telegram_source_sleep_sec": 5.0,    # 채널당 요청 간격. 비공식 경로라 TradingView
                                          # 페이싱(5.0)보다 느슨하게 가지 않는다.
    "telegram_source_max_posts": 20,     # 채널당 채택 상한(1페이지가 20건 — 실측)

    # 알림 트리거
    "preview_band_pct": 1.0,             # entry 대비 이 % 이내 접근 시 예고
    "cluster_band_pct": 1.0,             # 같은 코인 내 이 % 이내 entry 는 한 클러스터로 병합
    "level_expiry_hours": 168,           # 미터치 레벨 만료 (7일)

    # 알림 필터
    "alert_min_grade": "C",              # 이 등급 이상만 알림 (수집은 전부 저장)
    "alert_max_per_coin_per_day": 3,     # 코인당 하루 알림 상한

    # 작성자 랭킹 (ACCURACY_DB_PLAN 2단계, 2026-07-26 카드 확정 — 표시·리포트용, 필터 미사용)
    "rank_half_life_days": 90,           # 최신성 가중 반감기 (w=0.5^(경과일/90))
    "rank_z": 1.28,                      # E_LB 80% 단측 신뢰하한 계수
    "rank_prior_m": 10,                  # 워쳐 prior 최대 강도 (m_eff=min(m, 워쳐표본))
    "rank_min_neff": 5,                  # 자체 승률 표시·랭킹 등재 게이트 (raw n≥5 대체)
    "rank_grade_neff": 30,               # 등급 부여 게이트 (3단계, 향후)

    # 주간 성적 리포트 (2026-07-26: 외부 크론 등록 없이 가격체크 회차가 흡수)
    # 2026-07-26 사용자 결정: **자동 발송은 끈다**(샘플을 보고 알림으로 받을 가치가
    # 없다고 판단). 단 데이터 축적·계산 절차는 전부 유지한다 — 적중 판정, ret_24h,
    # daily_stats, E_LB/수축승률/합의/베이스라인 계산 모두 그대로 쌓이고, 보고 싶을 때는
    # `python scripts/run_weekly_report.py` 로 즉시 볼 수 있다. 나중에 다시 받고 싶으면
    # 이 값만 True 로 되돌리면 된다(주기 판정 meta 는 그대로라 바로 재개됨).
    "weekly_report_auto_send": False,
    # 작성자 주간 스냅샷(역신호 '2주 연속' 판정용 원천 데이터) — 리포트 발송과 무관하게
    # 계속 쌓인다. 저장 전용이라 알림·필터에 영향 없음(2026-07-26 사용자 결정).
    "author_snapshot_interval_hours": 168,
    "weekly_report_interval_hours": 168,   # 발송 주기 (7일)
    "weekly_report_retry_minutes": 60,     # 발송 실패 시 재시도 백오프
    "weekly_report_kst_hour_from": 9,      # 이 시각 이후에만 발송 (새벽 발송 방지)
    "weekly_report_kst_hour_to": 22,       # 이 시각 전까지만 발송

    # 초과 적중률 베이스라인 (2026-07-26 카드 B안: ret_24h 양수 비율 재사용, 비용 0)
    "baseline_min_n": 20,                  # pooled 표본이 이 미만이면 그 주 섹션 생략

    # 합의(confluence) — 표시 전용, E_LB/정렬 키 미반영 (Phase 1)
    "confluence_window_hours": 168,        # 클러스터 재구성 시 두 터치의 최대 시간 간격
                                           # (= level_expiry_hours, 동시 생존 가능 상한)
    "confluence_min_clusters": 2,          # 작성자 클러스터 수가 이 미만이면 표시 생략

    # 네트워크
    "http_timeout_sec": 10.0,

    # 적중 DB (2026-07-23 확정: ACCURACY_DB_PLAN.md)
    "outcome_window_hours": 168,     # 터치 후 이 시간 내 미종결 시 타임박스 강제 종결
    "r_clip_low": -1.0,              # R-멀티플 윈저라이즈 하한
    "r_clip_high": 5.0,              # 상한

    # 동시터치 재검사 (Bar Magnifier, 2026-07-26) — 한 캔들 안에서 TP·SL 이 둘 다
    # 닿아 순서를 모를 때, 그 구간의 체결내역(trades)으로 실제 도달 순서를 복원한다.
    # 종결 '이전'에만 돌아 판정 자체를 정확하게 만든다(이미 쓴 판정을 뒤집지 않음 —
    # 불변 스냅샷/안티게이밍 원칙 유지). 판별 실패 시 기존대로 miss+ambiguous.
    "bar_magnifier_enabled": True,
    "bar_magnifier_max_span_sec": 900,   # 재검사할 캔들 길이 상한(15분봉 폴백까지 허용)
    "bar_magnifier_max_pages": 4,        # 구간당 체결 조회 페이지 상한(500건×4=2000건)
    "bar_magnifier_max_per_cycle": 5,    # 회차당 재검사 상한 — 2분 핫패스 보호

    # 수집 급감(조용한 고장) 감지 (2026-07-26: cron/Actions 초록불인데 신규수집
    # 0건이 3일 지속된 사고 재발 방지 - price_check 잡에서 collected_at 집계로 감시)
    "collect_silence_window_hours": 24,     # 이 시간 동안 신규 수집 0건이면 경고 후보
    "collect_silence_baseline_days": 7,     # 비교 기준(직전 N일) 평균 수집량
    "collect_silence_min_baseline_avg": 0.5,  # 직전 평균 일일 수집이 이 미만이면 원래 조용한
                                               # 기간으로 보고 오탐 처리(경고 생략)

    # 수집 단계 정지(meta.last_collect_at staleness) 감시 (2026-07-26 과제3 - 감사 minor)
    # collect_silence 와 신호가 다르다: 저건 '신규 행이 있는가'(결과물) 를 보므로 수집기가
    # 살아있어도 새 글이 없으면 위양성이 날 수 있고, 반대로 '수집 단계 자체가 안 도는'
    # 고장(meta 갱신 정지)은 못 잡는다. 07-23~26 rebase 오류로 수집이 조용히 전량 폐기된
    # 3일 장애가 계기 - 이 값이 있었으면 last_collect_at 정체로 더 빨리 잡혔을 것.
    # 수집 주기(4h)의 3배 - 실패 백오프(30분) 몇 번 겹쳐도 오탐 안 나게 여유를 둔다.
    "last_collect_stale_hours": 12,

    # 가격체크 회차 자체 정지(공백) 감시 (2026-07-27 기획 카드 #2 과제2)
    # collect_stale/collect_silence 는 '수집'을 보지만, 이건 2분 주기 회차(가격체크)
    # 자체가 멈춘 것을 잡는다 — 회차가 도는 동안엔 자기 정지를 스스로 감지할 수
    # 없으므로, 다음에 살아난 회차가 직전 last_check_at 과의 공백을 사후 재구성한다
    # (monitor/price_check.py `_check_price_check_gap`). 카드 #2 과제1(GH schedule
    # 백업, 30분 주기)이 있어 정상 상황에서도 공백이 최대 ~40~60분까지 벌어질 수 있다
    # (백업 자체 지연 포함) — 임계값을 그 위에 넉넉히 잡아 "백업만으로 정상 동작 중"을
    # 오탐하지 않고 "백업까지 죽은" 완전 정지만 잡는다.
    "price_check_gap_alert_minutes": 120,

    # 글 삭제 감지 (2026-07-26 ACCURACY_DB_PLAN 안티게이밍 - 백로그 구현)
    "deletion_check_daily_limit": 5,        # 하루 1회(수집 주기 중 1번)만 순환 확인,
                                             # 이 건수만큼만 - TradingView 부담/차단 위험 억제
    "deletion_recheck_after_days": 30,      # 한 번 "생존" 확인한 글도 이 기간 후 재확인
                                             # (뒤늦게 지워지는 글도 잡기 위함)

    # TradingView 차단 감지 즉시 알림 (2026-07-26 과제2 - 수집 급감 24h 경보보다 빠른 신호)
    "tv_block_alert_daily_limit": 1,        # 하루 최대 1회만 - 사용자는 알림 과다를 싫어함
    "tv_block_alert_meta_keep_days": 7,     # 날짜별 카운터(tv_block_alert_count_YYYY-MM-DD)
                                             # meta 키 보존기간 - 안 지우면 meta 테이블 무한증가
                                             # (2026-07-26 감사 minor, run_collect.py 가 정리)

    # 업비트 거래소 리스크 공지 즉시경보 (2026-07-26 기획 카드 #5)
    # 유의 종목 지정·거래지원 종료 공지를 감지해 추적 중인 코인이면 즉시 경보 +
    # 대기 레벨 만료. 상세는 monitor/announcements.py 참고.
    "announcement_alert_enabled": True,
    # 폴링 주기 — 2분 주기 회차마다 부르지 않는다(meta TTL). 비문서화 API 라
    # 예의상으로도, 무료 운영 원칙상으로도 하루 ~72콜 수준이 적당하다.
    "announcement_poll_interval_minutes": 20,
    "announcement_page_size": 30,        # 1페이지면 20분치 신규 공지를 충분히 덮는다
    # 리스크 키워드 — 제목에 이 중 하나라도 있으면 후보
    "announcement_risk_keywords": ["유의 종목", "유의종목", "거래지원 종료", "상장폐지"],
    # 제외어 — 후보 중 이 단어가 있으면 리스크가 아니다. "거래 유의 종목 지정 해제
    # 안내"(실측 제목)처럼 위험이 해소됐다는 공지가 키워드에 걸리기 때문. 이걸 빼면
    # 안전해진 코인의 레벨을 도리어 만료시킨다.
    "announcement_exclude_keywords": ["해제"],
    # 이보다 오래된 공지는 무시 — 기능을 처음 켜는 회차에 과거 공지 수천 건이
    # 한꺼번에 매칭돼 경보가 쏟아지는 것을 막는다. 다운타임(며칠)도 덮을 만큼 넉넉히.
    "announcement_max_age_hours": 72,
    # 발송 실패한 경보의 재시도 기한. 만료를 발송보다 먼저 하는 설계라, 텔레그램
    # 장애로 못 보낸 경보는 대기 큐(meta)에 남겨 회차마다 재시도한다 — 레벨이 이미
    # 만료돼 공지 재매칭으로는 되살릴 수 없기 때문(2026-07-26 리뷰 지적).
    # 기한이 지나면 폐기: 며칠 늦은 리스크 공지는 소음이라 무한 재시도가 더 해롭다.
    "announcement_pending_ttl_hours": 6,

    # 호가 매수/매도 압력 기록 (2026-07-26 기획 카드 #19 — 폴백안 채택)
    # 카드 #18(REST ticker 의 acc_bid_volume/acc_ask_volume 재활용)은 2026-07-26
    # 무인증 실측에서 **해당 필드가 REST /v1/ticker 응답에 존재하지 않음**을 확인해
    # 폐기했다(웹소켓 전용 필드). 대신 터치 확정 시에만 /v1/orderbook 1콜로 스냅샷.
    # **순수 로깅** — 알림 본문·필터·등급 어디에도 반영하지 않는다(관찰기 동결 준수).
    "orderbook_pressure_enabled": True,

    # 주간 감사 덤프 (2026-07-27 기획 카드 #4 — storage/audit_dump.py)
    # data/levels.db 는 바이너리라 git diff 가 안 된다. 주 1회 levels/daily_stats 를
    # data/audit/*.ndjson 텍스트로 떨궈 "등급/상태가 언제·왜 바뀌었나"를 사후에 읽을 수
    # 있게 한다. 알림·필터·등급 산식과 무관한 **기록 전용** 기능.
    "audit_dump_enabled": True,
    "audit_dump_interval_hours": 168,       # 주 1회 (주간 리포트/스냅샷과 같은 주기)
    "audit_dump_retry_minutes": 60,         # 실패 시 백오프 (2분 회차마다 재시도 방지)
    "audit_dump_keep_weeks": 8,             # 작업본에 남길 주차 수 — 레포 무한 증가 방지.
                                             # 지워도 그 파일이 실렸던 과거 커밋에는 영구히
                                             # 남으므로 아카이브 자체는 잃지 않는다.
    # 원문(levels.raw_text) 아카이브 여부. True 여야만 아래 DB 원문 정리가 돈다 —
    # 아카이브 안 된 원문을 지우는 경로는 만들지 않는다(복구 불가 손실 방지).
    "audit_dump_include_raw_text": True,
    # 종결(touched/expired) 후 이 기간이 지난 레벨의 raw_text 를 DB 에서 비운다.
    # 소비처(reparse_all)가 활성 행만 보므로 런타임엔 무해하고, DB 용량의 큰 몫을
    # 회수한다. 덤프 주기(7일)의 2배 — 한 주 덤프가 실패해도 다음 주가 담고 지나간다.
    "audit_raw_text_keep_days": 14,

    # 파일 경로 — data/ 는 레포에 커밋 백되는 영속 상태 (아티팩트 3일 만료 대체)
    "db_path": "data/levels.db",
    "universe_cache_path": "data/universe.json",
}


def get(key: str):
    return SETTINGS[key]


# ── 비밀정보 (환경변수 전용) ────────────────────────────────────

def _load_dotenv_if_present() -> None:
    """로컬에서 .env 가 있으면 읽는다. python-dotenv 미설치/파일 없음은 조용히 무시
    (운영 환경에서는 Actions 가 환경변수를 직접 주입하므로 .env 가 없는 게 정상)."""
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except Exception:
        pass


_load_dotenv_if_present()


def secret(name: str, required: bool = False) -> str:
    """환경변수에서 비밀값을 읽는다. required=True 인데 없으면 즉시 실패한다
    (값 자체는 예외 메시지에도 절대 넣지 않는다)."""
    val = os.getenv(name, "").strip()
    if required and not val:
        raise RuntimeError(
            f"필수 비밀값 '{name}' 가 설정되지 않았습니다. "
            f"로컬은 .env, 운영은 GitHub Actions Secrets 에 등록하세요. "
            f"(.env.example 참고)"
        )
    return val


def mask(val: str) -> str:
    """로그용 마스킹 — 존재 여부와 길이만 노출, 실제 값은 숨긴다."""
    if not val:
        return "(없음)"
    if len(val) <= 6:
        return "*" * len(val)
    return f"{val[:3]}…{val[-2:]} (len={len(val)})"


def secrets_status() -> dict:
    """어떤 비밀값이 채워졌는지 진단용 요약 (값은 노출하지 않음)."""
    return {
        "TELEGRAM_BOT_TOKEN": mask(secret("TELEGRAM_BOT_TOKEN")),
        "TELEGRAM_CHAT_ID": mask(secret("TELEGRAM_CHAT_ID")),
        "COINGECKO_API_KEY": mask(secret("COINGECKO_API_KEY")),
        "WATCHER_GITHUB_TOKEN": mask(secret("WATCHER_GITHUB_TOKEN")),
    }
