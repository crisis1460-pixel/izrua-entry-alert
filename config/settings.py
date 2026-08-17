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
    # 2026-08-08 200→300 단계 시험 (사용자 결정): 알림 빈도 개선은 필터 완화가
    # 아니라 모수 확대로 — 등급 백테스트(research_2026-08-08) 결론. 1~2주
    # 관찰(300위권 차티스트 글 품질·알림 증가량 실측) 후 400 확대 여부 결정.
    # 되돌리기: 이 값만 200 으로. 201~300위는 mcap_tiers 밖이라 알림에
    # 등급 아이콘 대신 '·' 로 표기됨(의도 — 새 구간임을 눈에 띄게).
    "universe_top_n": 300,               # CoinGecko 시총 상위 N
    "universe_refresh_hours": 24,        # 시총 목록 갱신 주기

    # ── 유니버스 품질 필터 (2026-08-11) ─────────────────────────────────────
    "universe_exclude_new_listing_days": 90,      # 업비트 최초 감지 후 N일 미만 제외 (0=비활성화)
    "universe_exclude_non_binance": True,         # Binance USDT 미상장 제외
    "universe_exclude_upbit_warning": True,       # 업비트 투자경고(warning) 코인 제외
    "universe_rank_drop_threshold": 80,           # N일 최고순위 대비 현재 하락폭 초과 시 제외 (0=비활성화)
    "universe_rank_drop_min_history": 7,          # 순위 급락 판정에 필요한 최소 기록 일수
    "universe_min_volume_usd": 0,                 # 24h 거래대금(USD) 하한 (0=비활성화). CoinGecko total_volume 기준
    "universe_min_mcap_usd": 0,                   # 시총(USD) 절대 하한 (0=비활성화). 침체장 저시총 방어

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
    # 수집 자체 시간 예산 (2026-08-15 수리). 24시간 슬로우 페이싱(12~18s) 전환 후
    # 유니버스 완주에 ~20분이 걸려 **매 회차** run_cycle 의 12분 하드킬에 걸렸다 —
    # 하드킬은 subprocess 실패라 last_collect_at(성공 마킹)이 영원히 안 찍혀
    # "수집 단계 정지" 경고가 오탐으로 울렸고(08-14 20:20/08-15 00:05 실사고),
    # 더 심각하게는 루프 '뒤'의 뒷정리(레벨 만료·재파싱·삭제감지 이월분)가 아예
    # 실행되지 않았다. 이 예산 안에서 루프가 스스로 멈추고 순환 이월(offset)로
    # 다음 회차에 이어받는다 — 하드킬(720s)보다 넉넉히 짧게 잡아 TG 소스·뒷정리
    # (~1.5분 실측)까지 12분 안에 완주시킨다.
    "collect_tv_deadline_sec": 570,      # TV 수집 루프 자체 마감 (9.5분)
    "max_post_age_hours": 168,           # 7일 이내 글만 수집
    "tv_fetch_sleep_sec": 12.0,          # 심볼당 요청 최소 간격. 이력: 3.0(2026-07-26,
                                          # 61번째 403 실측) → 5.0 → 6~9s 지터(08-02).
                                          # 2026-08-14 24시간 슬로우 전환(사용자 확정):
                                          # git DB 스냅샷 47건 복원 분석 결과 주간
                                          # 페이싱(6~9s, 8건/분)은 **매 사이클** 시작
                                          # 7~8분(~60번째 요청)에 차단됐고(하루 5~6회,
                                          # 경보 상한 1회가 아침 차단만 보여줘 착시),
                                          # 심야 슬로우(12~18s, 4건/분)는 도입 후 단
                                          # 한 번도 차단되지 않았다 — 검증된 안전값을
                                          # 하루 종일 적용한다. 예산: 12분÷15s≈48심볼/
                                          # 회차, 미도달분은 순환(collect_universe_offset)
                                          # +중간 커밋(10심볼)이 이어받아 커버리지 무손실
                                          # (심볼당 방문 4.5→3.6회/일, 7일 창 대비 무해).
    "tv_fetch_sleep_max_sec": 18.0,      # 심볼당 요청 최대 간격 — uniform(min,max) 랜덤
                                          # 지터(고정 간격은 그 자체가 봇 패턴 — 관례:
                                          # Scrapy RANDOMIZE_DOWNLOAD_DELAY).
    "tv_night_sleep_sec": 12.0,           # KST 0~8시 새벽 최소 간격 — 24시간 슬로우
    "tv_night_sleep_max_sec": 18.0,       # 전환으로 주간과 동일값(분기 자체는 유지 —
                                          # 향후 주/야 차등이 다시 필요하면 값만 조정).
    "tv_empty_rest_sec": 30.0,           # 연속 0건 시 휴식
    # 수집 중간 커밋 주기 (2026-07-27 교차감사 M1) — 심볼 이 개수마다 conn.commit().
    # 왜 필요한가: 수집은 run_cycle.py 가 subprocess timeout(12분)으로 **하드킬**할 수
    # 있는데, 하드킬은 열린 트랜잭션을 통째로 롤백시킨다. 예전엔 회차 전체가 한
    # 트랜잭션이라 12분에 걸린 순간 그 회차 수집분 전량 + 순환 offset 까지 증발하고
    # 다음 회차가 같은 지점을 다시 돌았다(무한 제자리걸음).
    # 왜 10 인가: 페이싱 5.0초 기준 10심볼 ≈ 50~60초라 하드킬 시 최대 손실이 1분치
    # 작업(≈ 12분 예산의 8%)으로 유계된다. 커밋 자체는 fsync 1회(ms 단위)라 81심볼
    # 회차에 8~9회 늘어도 sleep 400초 옆에서는 측정 불가 수준. 1 로 낮춰도 안전하며
    # (손실 최소화) 값을 키울수록 하드킬 손실 구간이 그대로 커진다.
    "collect_commit_every_symbols": 10,

    # 텔레그램 공개채널 소스 (2026-07-27 기획 카드 #14 — 두 번째 입력원)
    # TradingView 단일 의존이 구조적 위험이라(403 한 번에 회차 수집 0건) t.me/s/{채널}
    # 공개 미리보기 HTML 을 두 번째 경로로 붙였다. 상세는 collector/telegram_source.py.
    # ⚠️ 기본 OFF + 빈 화이트리스트가 이 기능의 안전장치다 — 아래 두 값을 건드리기
    # 전까지 코드가 배포돼도 요청 0건·동작 변화 0 이다. 채널 목록은 사장님 승인 사항.
    # 켜는 법: enabled 를 True 로, channels 에 채널명(@ 없이)을 넣는다.
    # 2026-07-27 첫 채널 활성화 (126개 실사 후 사장님 승인). BitmexSignalsFee 선정 근거:
    #   · ENTRY/TARGETS/STOP LOSS 전문을 무료 공개(유료 VIP 가림 없음) — 실물 확인
    #   · 업비트 KRW 상장 비중 71%(17건 중 12), 관측 표본 8건 중 롱 7건
    #   · 손실도 그대로 게시("KITE 🚫18.7% Loss") — 체리피킹 안 함
    #   · 어댑터 실측: 진입가 추출 8/8, 미상장(GRAM·CHILLGUY)은 정상 폐기
    #   · 발행 0.9건/일 → 신규 레벨 하루 0~1건 수준. 관찰기 baseline 간섭 최소
    # 늘릴 때는 반드시 '사후 게시(retro-post)' 검사를 통과시킬 것 — 신호 게시 후
    # 수 초~수십 초 만에 "TP1 hit"을 붙이는 사기 채널이 실사에서 3곳 나왔다.
    "telegram_source_enabled": True,
    # 2026-08-17 사용자 결정: 채널 2개 추가 (기존 1 → 3).
    # · cryptosignals0rg — 양방향(long/short), R/R 명시, 손실 정직, Upbit ~50%
    # · wolfoftrading — Upbit 겹침 ~80%(INJ/ETH/MANA/BTC), 밀도 낮음(주 1~2건)
    # 둘 다 retro-post 검증 PASS. 예상 증가: cryptosignals0rg 0.5~1/일 +
    # wolfoftrading 주 0~1건. 관찰 후 실측 채택 여부 재판단.
    "telegram_source_channels": ["BitmexSignalsFee", "cryptosignals0rg", "wolfoftrading"],
    "telegram_source_sleep_sec": 5.0,    # 채널당 요청 간격. 비공식 경로라 TradingView
                                          # 페이싱(5.0)보다 느슨하게 가지 않는다.
    "telegram_source_max_posts": 20,     # 채널당 채택 상한(1페이지가 20건 — 실측)

    # 알림 트리거
    "preview_band_pct": 1.0,             # entry 대비 이 % 이내 접근 시 예고
    # 접근 예고 알림 발송 스위치 (2026-07-31 사용자 결정 — 질문카드).
    # 예고를 보면 조기 진입 유혹이 생겨 **완전 제거**한다: 첫 알림 = 진입가 터치
    # 본알림. 발송(과 record_alert/원장 기록)만 끄고 상태 전이(mark_previewed)와
    # 관찰 집계(previews_total/preview_dwell)는 그대로 유지한다 — True 로 되돌리면
    # 즉시 재개되고 관찰 시계열도 끊기지 않는다.
    "preview_alert_enabled": False,
    "cluster_band_pct": 1.0,             # 같은 코인 내 이 % 이내 entry 는 한 클러스터로 병합
    "level_expiry_hours": 168,           # 미터치 레벨 만료 (7일)

    # ── 시장환경 필터 (2026-08-14 고도화) ─────────────────────────────
    # 데이터 수집만 — 알림 메시지 양식 불변. 실패 시 무시(fail-safe).
    "macro_dxy_enabled": True,           # DXY 달러인덱스 (Yahoo Finance, 무료)
    "macro_fomc_cpi_enabled": True,      # FOMC/CPI 정적 캘린더 (API 0콜)
    "macro_dvol_enabled": True,          # Deribit DVOL 변동성지수 (무료)
    "token_unlock_enabled": True,        # DeFiLlama 토큰 언락 경고 (무료)
    # Hash Ribbons (2026-08-15) — mempool.space 무료 해시레이트, 채굴자 항복/회복
    # 감지. 수급 판정 내부 보정 전용(알림 무노출).
    "hash_ribbons_enabled": True,

    # ── 모닝 브리핑 (2026-08-15 사용자 확정: "모닝브리핑은 좋아") ──────────
    # 하루 1회, KST 아침 시간창에 시장환경 요약 1통. 엔트리 알림 양식과 무관한
    # 별도 메시지 종류 — 주간리포트와 같은 meta 주기 판정 패턴(외부 크론 불필요).
    "morning_brief_enabled": True,
    "morning_brief_kst_hour_from": 8,    # 이 시각(포함)부터 발송 창
    "morning_brief_kst_hour_to": 10,     # 이 시각(미만)까지 — 놓치면 그날 생략

    # 알림 필터
    # 2026-08-17 사용자 결정: C → D 완화. 근거: 최근 30일 실측 승률 D 45% >
    # C 36% (A 12% · S 9% 로 상위 등급 예측력이 오히려 낮음 — v5 산식 재점검
    # 대상). 억제된 D 18건/주 발송 → 하루 +2.5건 예상. Universe300 관찰 원칙과
    # 상충하지만 preview 완전 중단(07-31) 후 알림량 급감 대응.
    "alert_min_grade": "D",              # 이 등급 이상만 알림 (수집은 전부 저장)
    # 유음(소리) 최소 등급 (2026-08-15 Tier1): 이 등급 이상 터치만 소리, 미만은
    # 무음 발송(disable_notification — 메시지 내용·양식 완전 동일, 소리만 제거).
    # 근거(research_2026-08-15_alert_quality.md): 푸시 2~5건/주에 사용자 46% 이탈,
    # PagerDuty 경보 예산 15건/주. 즉시 가역 — "C" 로 낮추면 종전과 동일.
    "alert_sound_min_grade": "B",
    # 2026-08-17 사용자 결정: 3 → 5 완화. 인기 알트 반복 터치 억제 45건/주
    # 완화 목적. 글로벌 상한(15)은 유지 — 코인당만 여유.
    "alert_max_per_coin_per_day": 5,     # 코인당 하루 알림 상한
    "alert_max_global_per_day": 15,      # 전체 코인 합산 하루 알림 상한 (알림 피로 방지)
    "alert_min_timeframe_hours": 4.0,    # 이 타임프레임 미만 아이디어는 알림 제외
                                          # (스캘핑/단타 필터). NULL(미명시)은 통과.
    # 최종(마지막) TP 가 진입가 대비 이 % 미만이면 알림 제외 (2026-08-03 사용자 결정:
    # "최종 TP <5% = 레버리지(선물)용 설계, 스팟 스윙 대상 아님". 실측 근거: 2~5%
    # 구간 종결 29건 hit 10/miss 19(34%), 3~5%는 18%). 07-29 신설 당시 5%였다가
    # 07-30 에 2%로 완화했던 것을 설정키로 분리하며 5%로 복원. 등급 감점표 경계
    # (grading.SWING_MIN_TP_PCT=2, 배점표 구조 공유)와는 별개 — 감점은 그대로.
    # 경계는 프로젝트 관례대로 '이상 통과'(정확히 5.0% = 발송). 형제 승계(m-4) 유지.
    "alert_min_last_tp_pct": 5.0,

    # 등급 산식 버전 (2026-08-01 S10 안3 — plan_S10_grade_recalibration.md)
    # grade_formula_ver: 신규 채점분에 levels.grade_ver 로 기록되는 태그. 과거 행은
    # NULL 유지(구 산식) — 소급 재라벨 금지(사용자 결정 D4). 캘리브레이션은 버전
    # 분리 집계(v3 기본, 구버전은 "구 산식(참고)" 병기).
    # v4 (2026-08-03, 사용자 질문카드): 팔로워 상단 강화(최대 25) + SL 보너스 +10→+3.
    # 리서치: izrua_company/planner/sprint08_SL없는시그널_등급설계_리서치.md
    # v5 (2026-08-15, 사용자 승인 Tier1 스프린트): TP 사다리 0~1단 감점 -3.
    # 실측 근거(research_2026-08-15_db_signal_analysis.md): 2단+ 48.2% vs
    # 0~1단 31.7% (+16.5%p, n=189, v4 표본에서도 재현) — 종결 189건 기준
    # 현재 채점 밖 신호 중 가장 두꺼운 판별력. 감점 폭 -3은 v4 SL 보너스(+3)와
    # 대칭 — 조이기 전용(경계 C=40 근처만 영향, 완화 없음).
    "grade_formula_ver": "v5",
    # 작성자 실적 가점(안2) 롤백 스위치 — False 면 calculate_grade/regrade_current
    # 양쪽에서 실적 가점 0 고정(배점표 축소분(안1)은 유지 — 위험 없는 부분).
    # 배포 후 1주 알림량 ±10% 이탈 시 이 값만 False 로 되돌린다(§6-4).
    "grade_author_points_enabled": True,

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
    # 역신호 확정/해제 경보 발송 스위치 (2026-08-01 사용자 결정 — 향후 분석용
    # 저장만, 텔레그램 발송 안 함. 판정·meta 기록·show_status 표시는 계속 쌓이며
    # True 로 되돌리면 다음 스냅샷 회차부터 즉시 재개 — weekly_report_auto_send
    # 와 같은 패턴).
    "reverse_alert_send_enabled": False,
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

    # 거래량 급증 2단계 알림 (Feature 4, 2026-07-29 / 2026-07-31 판정 지표 교체).
    # 진입가 터치 알림 발송 후 해당 티커를 감시 목록에 올리고, 이후 회차마다
    # **최근 1시간 거래대금 > 직전 20시간(완결 60분봉) 평균 × multiplier** 이면 별도 알림.
    # 2026-07-31 교체 근거: 기존 "24h 누적 > 7일 일평균 × 3" 은 업비트 시장경보
    # (거래량 급등 종목 안내) 준거였으나, 24h **누적**은 1시간짜리 급증을 반나절
    # 늦게(희석해서) 반영한다 — 터치 후 72h 안에 잡아야 하는 이 기능과 맞지 않음.
    # 새 지표는 RVOL 업계 관례(TradingView RVOL/altFINS 등: 현재봉 ÷ 최근 20봉 평균,
    # 2x=스파이크·4x+=극단)를 따르되 임계는 5.0 으로 보수적으로 상회 — 알림 과다를
    # 싫어하는 사용자 성향 + 저유동 코인의 시간당 변동성이 일봉보다 큰 점 반영.
    "volume_spike_enabled": True,
    # 2026-08-17 사용자 결정: 5.0 → 3.0 완화. 근거: 30일 실측 발동 0건(alerts_log
    # 아닌 텔레그램 직접 발송이지만 oi_spike_state 등 우회 검증 결과), 임계가 너무
    # 극단적. RVOL 관례(TradingView) 2x 스파이크·3x 강한 스파이크 → 3x는 강한 급증
    # 만 잡음. 최근 30일 알림량 급감(하루 3~5건) 회복 목적.
    "volume_spike_multiplier": 3.0,       # 최근 1h > 20h 평균 × 이 배수 → 급증 판정
    # 최근 1시간 거래대금 절대 하한 (KRW). 저유동 코인은 새벽에 20h 평균이 수백만원
    # 수준까지 내려가 몇 건의 체결만으로 5x 를 넘는 위양성이 난다 — 절대금액 바닥을
    # 함께 요구해 "평균이 워낙 작아서 난 배수"를 걸러낸다 (2026-07-31, 지표 교체와 동시 도입).
    # 2026-08-01 5천만→2억 상향(사용자 확정): 첫날 실사례 ETHFI 02:04 가 최근 1h
    # 5,437만원(하한 9% 차 통과)·평균 790만원/h 로 발송됨 — 정확히 막으려던 새벽
    # 저유동 위양성. 진짜 펌핑은 중소형 알트도 시간당 수억은 터진다.
    # 2026-08-17 사용자 결정: 2억 → 1억 완화. 근거: 임계 5x→3x 완화와 함께
    # 저유동 새벽 위양성 가드 부담도 함께 낮춤. 여전히 시간당 1억원은 저유동
    # 알트에서 흔치 않은 규모라 위양성 방어 유효.
    "volume_spike_min_krw_60m": 100_000_000,
    "volume_spike_watch_hours": 72,        # 터치 후 이 시간(3일) 동안만 감시, 초과 시 만료

    # OI(미결제약정) 급증 알림 (2026-08-08 사용자 결정) — 진입가 터치와 무관한
    # 별도 알림. oi_history 시간당 스냅샷(수급 판정용, 08-07 도입)을 그대로
    # 재사용 — 추가 API 콜 0. 1시간 전 대비 변화율이 임계 이상이면 발동, 코인당
    # 쿨다운 내 재발동 금지(같은 급증 구간에서 매시 반복 스팸 방지).
    "oi_spike_enabled": True,
    # 2026-08-17 사용자 결정: 15.0 → 10.0 완화. 근거: oi_spike_state 30일간
    # 1건만 발동. 10%도 파생 관례상 유의미한 급증(관례 5~10%). 쿨다운 6h는 유지.
    "oi_spike_pct": 10.0,           # 1h 전 대비 |변화율| 이 이상이면 급증 판정
    # 코인당 재발동 최소 간격. 감지 자체가 시간당 1회(_OI_SNAP_INTERVAL_SEC=
    # 3600초, price_check.py)라 이 값이 1.0 미만이면 사실상 "쿨다운 없음"과
    # 같다(다음 감지 시점 자체가 이미 1h 뒤라 쿨다운이 걸릴 일이 없음) —
    # 1.0 미만으로 낮춰도 위험하진 않지만 의미가 없다는 점만 알아둘 것.
    "oi_spike_cooldown_hours": 6.0,

    # 데드맨 스위치 — 회차 성공 시 외부 서비스(healthchecks.io 등)에 핑.
    # 텔레그램 단일 채널 장애 시에도 파이프라인 정지를 독립 감지할 수 있다.
    # 빈 문자열이면 비활성(핑 안 함).
    "deadman_ping_url": "",

    # 파일 경로 — data/ 는 레포에 커밋 백되는 영속 상태 (아티팩트 3일 만료 대체)
    "db_path": "data/levels.db",
    "universe_cache_path": "data/universe.json",
    "universe_first_seen_cache_path": "data/universe_first_seen.json",
    "universe_rank_history_cache_path": "data/universe_rank_history.json",
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

