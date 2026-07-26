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
    "tv_fetch_sleep_sec": 3.0,           # 심볼당 요청 간격 (Cloudflare 차단 회피)
    "tv_empty_rest_sec": 30.0,           # 연속 0건 시 휴식

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

    # 글 삭제 감지 (2026-07-26 ACCURACY_DB_PLAN 안티게이밍 - 백로그 구현)
    "deletion_check_daily_limit": 5,        # 하루 1회(수집 주기 중 1번)만 순환 확인,
                                             # 이 건수만큼만 - TradingView 부담/차단 위험 억제
    "deletion_recheck_after_days": 30,      # 한 번 "생존" 확인한 글도 이 기간 후 재확인
                                             # (뒤늦게 지워지는 글도 잡기 위함)

    # TradingView 차단 감지 즉시 알림 (2026-07-26 과제2 - 수집 급감 24h 경보보다 빠른 신호)
    "tv_block_alert_daily_limit": 1,        # 하루 최대 1회만 - 사용자는 알림 과다를 싫어함

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
