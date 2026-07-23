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
    "collect_interval_hours": 4,         # TradingView 수집 주기 (참고용, 실제 트리거는 cron-job.org)
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

    # 네트워크
    "http_timeout_sec": 10.0,

    # 적중 DB (2026-07-23 확정: ACCURACY_DB_PLAN.md)
    "outcome_window_hours": 168,     # 터치 후 이 시간 내 미종결 시 타임박스 강제 종결
    "r_clip_low": -1.0,              # R-멀티플 윈저라이즈 하한
    "r_clip_high": 5.0,              # 상한

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
