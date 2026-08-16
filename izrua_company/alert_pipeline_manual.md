# 엔트리 알림 파이프라인 매뉴얼

> 마지막 업데이트: 2026-08-16 (모닝 브리핑 미국 시황 추가 + DXY/DVOL 한국어 라벨)  
> 목적: 코인 하나가 텔레그램 알림으로 도달하기까지 거치는 모든 관문 정리  
> 대상 독자: 개발·운영 내부용

---

## 전체 흐름 개요

```
유니버스 선정 → 아이디어 수집 → 채점(등급) → DB 저장 → 가격 감시 → 알림 게이트 → 발송
   (1일 1회)     (2분마다)        (수집 시)      (즉시)     (2분마다)     (터치 시)
```

---

## 단계 1. 유니버스 선정

**파일:** `collector/coingecko.py`  
**주기:** 24시간마다 갱신 (`universe_refresh_hours = 24`)  
**결과:** `data/universe.json`

### 1-1. 기본 선정
| 조건 | 내용 |
|------|------|
| 모집단 | CoinGecko 시총 상위 **300위** 이내 |
| 상장 조건 | 업비트 KRW 마켓 교집합 |
| 기본 제외 | 스테이블코인 (USDT, USDC, DAI 등 20종) |

### 1-2. 품질 필터 6종 (2026-08-13 확장)

| 필터 | 제외 조건 | 설정 키 |
|------|-----------|---------|
| 신규 상장 | 최초 감지 후 **90일 미만** | `universe_exclude_new_listing_days = 90` |
| Binance 미상장 | Binance USDT TRADING 쌍 없음 | `universe_exclude_non_binance = True` |
| 업비트 투자경고 | `market_event.warning = True` (투자경고 수준만, 유의종목 아님) | `universe_exclude_upbit_warning = True` |
| 시총 순위 급락 | 과거 최고 순위 대비 현재 **80위 이상** 하락 + **7일 이상** 이력 필요 | `universe_rank_drop_threshold = 80` |
| 24h 거래대금 하한 | CoinGecko 24h 거래대금(USD) 미달 | `universe_min_volume_usd = 0` (비활성) |
| 시총 절대 하한 | CoinGecko 시총(USD) 미달 | `universe_min_mcap_usd = 0` (비활성) |

> **주의:** 품질 필터는 개별 실패 시 해당 필터만 건너뜀 (API 장애에도 수집 회차 안전).  
> 신규 상장 이력은 `data/universe_first_seen.json`, 순위 이력은 `data/universe_rank_history.json` 에 누적.

---

## 단계 2. 아이디어 수집

**파일:** `scripts/run_collect.py` → `collector/extractor.py`  
**주기:** 2분마다 (GitHub Actions `price-check.yml`)

### 2-1. 포스트 수집 조건
| 조건 | 기준 |
|------|------|
| 소스 | TradingView 차티스트 공개 포스트 |
| 포스트 나이 | **7일(168시간)** 이내 (`max_post_age_hours = 168`) |
| 대상 | 유니버스 코인 + 워처 등록 작성자 포스트 |
| 요청 페이싱 | 심볼당 **12~18초 지터** — 2026-08-14 24시간 슬로우 전환. git DB 스냅샷 47건 복원 분석: 주간 6~9s는 매 사이클 7~8분(~60번째 요청)에 403(하루 5~6회, 경보 상한 1회가 아침만 보여줘 착시), 심야 12~18s는 차단 0회 → 검증값으로 통일. 예산 48심볼/회차, 순환 로테이션이 커버 |
| 자체 시간 예산 | TV 루프 **9.5분**(`collect_tv_deadline_sec = 570`) — 2026-08-15 수리. 슬로우 전환 직후 완주 ~20분 > run_cycle 하드킬 12분이라 **매 회차 TimeoutExpired** → `last_collect_at` 미갱신(수집 정지 경고 오탐, 08-14 실사고) + 루프 뒤 뒷정리(만료·재파싱·삭제감지) 전면 정지. 예산 소진 시 차단 이탈과 같은 순환 이월 경로로 스스로 멈춰 12분 안에 정상 종료(성공 마킹·뒷정리 복구). 회귀: 수리9-R7/R7b |

### 2-2. 파싱 & 추출 (`extractor.py`)
추출 항목:
- `entry` / `entry_low` / `entry_high` — 진입가 (구간이면 중간값)
- `sl` — 손절가 (방향 불일치·범위 이탈 시 None)
- `tps_all` — 유효 목표가 전체 (방향 검증 + 진입가 대비 0.25×~4× 범위)
- `timeframe_hours` — 타임프레임 (명시 키워드 필요: "봉:", "Timeframe:" 등. 본문 내 평문 "4h"는 인식 안 됨)

드롭 조건:
- 진입가 없음 (파싱 실패)
- 진입가가 현재 시세와 **60% 이상** 괴리

### 2-3. 가격 동기화 검증
업비트 현재가와 CoinGecko 가격(USD × USDT-KRW 환율) 비교 → **40% 초과** 괴리 시 해당 코인 수집 건너뜀 (심볼 충돌 방지).

### 2-4. 중복 방지
`UNIQUE(symbol, entry, author, url, source)` — 동일 신호는 upsert (UPDATE) 처리, 중복 저장 없음.

---

## 단계 3. 채점 및 등급 부여

**파일:** `collector/grading.py`  
**시점:** 수집 직후 (저장 전), 가격 터치 시 재채점 (regrade)  
**산식 버전:** `grade_formula_ver = "v5"` (2026-08-15)

### 3-1. 점수 구성 요소

| 항목 | 만점 | 상세 |
|------|------|------|
| 팔로워 수 | 25점 | 100k+=25, 50k+=22, 10k+=17, 5k+=12, 1k+=8, 100+=3, <100=1 |
| 가격 근접도 | 20점 | ±2% 이내=20, 아래 2~10%=17, 위 2~5%=12, 위 5~10%=8, 아래 10%+=15 |
| 목표가 거리 | +12 | 5~15%=+12, 15~25%=+8, 25~40%=+4, 3~5%=-2, 2~3%=-4, 0~2%=-6 |
| 데이터 완성도 | 23점 | 진입+목표=20, 추가 SL=+3; 하나만=8, 둘다없음=2 |
| 작성자 실적 | 15점 | Wilson 80% 하한 기준 TP1 적중률: ≥55%=+15, ≥40%=+10, ≥25%=+5 (최소 5건 필요) |
| TP 사다리 (v5) | -3 | `tp_ladder_count` 0~1단(NULL 포함)=-3, 2단+=0. 실측 근거: 2단+ 48.2% vs 0~1단 31.7% (+16.5%p, n=189 — research_2026-08-15_db_signal_analysis.md). score_breakdown 키 "ladder" |

**총 만점: 약 95점** (항목별 조합에 따라 다름)

### 3-2. 등급 경계

| 등급 | 최소 점수 |
|------|----------|
| S | 85점 이상 |
| A | 70점 이상 |
| B | 55점 이상 |
| C | 40점 이상 |
| D | 40점 미만 |

---

## 단계 4. DB 저장 및 감시 등록

**파일:** `storage/db.py`  
**상태:** 저장 시 `status = 'watching'`으로 설정  
**만료:** `level_expiry_hours = 168` (7일 후 자동 만료)

모든 포스트는 **등급 무관하게** 저장됨. 등급 필터는 알림 발송 시점에만 적용.

---

## 단계 5. 가격 감시 및 터치 판정

**파일:** `monitor/price_check.py`  
**주기:** 2분마다

### 5-1. 감시 범위 (캔들 조회 최적화)
- 현재가가 진입가 상단의 **+5% 이내**인 레벨만 캔들 조회 대상
- 캔들 조회 예산: 회차당 **최대 30콜**

### 5-2. 터치 판정
```
터치 = 캔들 저가(최근 45분, 또는 직전 감시 이후) ≤ 진입가 상단(entry_high)
예고 = 현재가 ≤ 진입가 상단 × (1 + 1.0%)   → preview_band_pct = 1.0%
```
캔들 저가는 직전 감시 시각 이후 구간만 사용 (과거 터치 재발동 방지).

### 5-3. 클러스터 그룹핑
동일 코인의 진입가가 **1.0% 이내** 이면 같은 클러스터로 묶음 (`cluster_band_pct = 1.0`).  
클러스터 내 어느 한 레벨이 터치되면 전체가 함께 알림 처리됨.

---

## 단계 6. 알림 게이트 (터치 확정 후)

아래 게이트를 **순서대로** 통과해야 텔레그램 발송:

### 게이트 1: 최소 등급
```
등급 ≥ C    (alert_min_grade = "C")
```
터치 시점에 현재가로 **재채점** 후 판정 (가격 근접도 점수 변동).
2026-08-15: 재채점된 `touch_grade`/`touch_score` 를 DB 에 저장(첫 기록 우선,
발송·억제 무관 전 터치) — 이전엔 수집 시점 등급만 남아 캘리브레이션 축이
어긋났음(터치까지 등급 변동 35%). 소리 게이트 분리: `alert_sound_min_grade="B"`
— B 미만 터치는 무음 발송(disable_notification, 내용·양식 동일).

### 게이트 2: 타임프레임 필터
```
timeframe_hours ≥ 4.0H    (alert_min_timeframe_hours = 4.0)
```
타임프레임이 명시되지 않은 포스트 (None / 0) 는 **통과** (관대).

### 게이트 3: 스윙 필터 (TP 거리)
```
마지막 유효 TP ≥ 진입가 × 5.0%    (alert_min_last_tp_pct = 5.0)
```
스캘핑·단타 포스트 차단. TP가 없으면 **통과**.  
클러스터 내 다른 멤버가 통과하면 낙오 멤버도 함께 승격.

### 게이트 4: 코인별 일일 발송 한도
```
코인당 하루 최대 3회    (alert_max_per_coin_per_day = 3)
```
터치 알림에만 적용. 예고 알림(현재 비활성)은 미적용.

### 게이트 4-2: 글로벌 일일 발송 한도 (2026-08-14)
```
전체 코인 합산 하루 최대 15회    (alert_max_global_per_day = 15)
```
터치 알림에만 적용. 코인별 한도와 별개로 전체 알림 피로 방지.  
**파일:** `monitor/price_check.py` (obs 키: `suppressed_global_cap`)

### 게이트 5: 중복 발송 차단
```
동일 (코인, 종류, 레벨 ID 조합) → 최근 10분 이내 발송 이력 있으면 차단
```
`alert_ledger` (NDJSON) + DB 이중 체크.

---

## 단계 7. 텔레그램 발송

**파일:** `notify/telegram.py`  
조건 통과 시 `sendMessage` (텍스트) 또는 향후 확장 시 `sendPhoto` (이미지).

### 7-1. 안전장치 (2026-08-13)
| 항목 | 내용 |
|------|------|
| 메시지 자동 분할 | 4096자 초과 시 줄바꿈 기준 청크 분할, 청크 간 1초 간격 전송 |
| 발송 속도 제한 | 연속 전송 간 최소 **1.0초** 간격 (`_SEND_MIN_INTERVAL_SEC`) |
| 출처 링크 보존 | 🔗 출처 행과 구분선은 말줄임(`…`) 대상에서 제외 |

### 7-2. 판정 어휘 개편 (2026-08-14 사용자 확정 — 초보자 직관 어휘)
표시 계층만 교체 — 판정 라벨(우호/중립/주의, 최적~위험)은 불변이라 축적 통계 연속성 유지.

| 행 | 종전 | 현행 |
|----|------|------|
| 수급 헤더 | `🧭 수급:` | `🧭 돈 흐름:` |
| 수급 근거 | 숏 몰림 / 숏 유입 / 롱 과열 / 청산 반등 / 숏 과열 | 반등 연료 / 하락 베팅 / 추격 위험 / 속임 반등 / 반등 여지 |
| 자리 근거 | 정배열 / 역배열 / 눌림목 / 일NN / 주NN | 상승세 / 하락세 / 조정중 / RSINN / 주RSINN |
| 자리 토큰 상한 | 3토큰+4h(최장 44칼럼 — 32칼럼 초과로 **잘림 버그**) | **2토큰 상한** (우선순위: 4h경고 > N일지지 > 추세 > RSI숫자) |
| 펀딩 플립 | `🔥 N일 음수→양수` | `🔥 N일만에 매수세 복귀` |
| 심리 헤더 | `🌍 BTC.D:` / `🪙 ALT.S:` / `😨 F&G:` | `🌍 비트 점유율:` / `🪙 알트장:` / `😨 시장심리:` |
| USDT.D (2026-08-14) | — | CoinGecko `/global` 동일 응답에서 추출, 추가 API 콜 없음 |

모든 개편 행은 봇 자체 폭 계산(`_display_width`) 기준 32칼럼 이내 검증 완료.

---

## 기타 내부 집계 (알림 미출력)

| 항목 | 함수 | 저장 위치 |
|------|------|----------|
| 등급별 적중률 | `db.get_grade_stats()` | `data/audit/grade_stats_YYYY-WXX.json` (주간) |
| 점수 구간별 적중률 (2026-08-13) | `db.get_score_bucket_stats()` — 5점 단위 버킷별 적중률 | `data/audit/grade_stats_YYYY-WXX.json` (주간) |
| 등급별 수익률 분포 (2026-08-13) | `db.get_grade_ret_stats()` — 24h/72h 평균·중앙값·표준편차 | `data/audit/grade_stats_YYYY-WXX.json` (주간) |
| 채점 요소 분해 (2026-08-13) | `grading.score_breakdown()` — 5요소별 점수 JSON | `levels.score_breakdown` (수집·재수집 시) |
| Profit Factor | `db.get_author_advanced_stats()` | 조회형 (DB 직접 쿼리) |
| R-Expectancy | 동일 | 동일 |
| Consistency Score | 동일 | 동일 |
| CVD 비율 (2026-08-11) | `binance.fetch_cvd_ratio()` — 터치 시점 4h taker 매수-매도 불균형 [-1,+1]. 2026-08-14 승격: 수급 판정 라벨 보정 입력 겸용(수치 알림 비노출) | `levels.touch_cvd_ratio` (터치 확정건만) |
| 펀딩비 (2026-08-13) | `binance.fetch_funding_rate()` — 터치 시점 무기한 선물 펀딩비(%) | `levels.touch_funding_rate` (터치 확정건만) |
| OI 변화율 (2026-08-13) | 터치 시점 OI 24h 변화율(%) | `levels.touch_oi_pct` (터치 확정건만) |
| 롱/숏 비율 (2026-08-13) | `binance.fetch_long_short_ratio()` — 전체 계정 롱 비율 (0~1) | `levels.touch_long_short_ratio` (터치 확정건만) |
| 탑 트레이더 비율 (2026-08-13) | `binance.fetch_top_trader_position_ratio()` — 상위 20% 트레이더 롱 비율 | `levels.touch_top_trader_ratio` (터치 확정건만) |
| 선물 테이커 비율 (2026-08-13) | `binance.fetch_taker_buy_sell_ratio()` — 선물 매수/매도 비율 | `levels.touch_taker_buy_sell_ratio` (터치 확정건만) |
| 스테이블코인 시총 (2026-08-13) | DeFiLlama — 전체 스테이블코인 유통량 (십억$) | `levels.touch_stablecoin_mcap_b` (터치 확정건만) |
| 호가 매수/매도 압력 | 터치 시점 스냅샷. 2026-08-14 승격: 수급 판정 라벨 보정 입력 겸용(수치 알림 비노출) | `levels.touch_bid_ask_ratio` |
| 200일선 상/하 | 터치 시점 스냅샷 | `levels.touch_ma200_above` |
| MTF 정렬 점수 (2026-08-14) | `upbit.derive_mtf_alignment()` — 일봉 RSI>50 (+1/−1) + 현재가 vs MA200 (+1/−1). score −2~+2, label 강세정렬/약세정렬/혼조. `fetch_position_data` 공유(추가 콜 0). 터치 확정건만 `record_touch_verdicts`로 DB 기록 | `levels.touch_mtf_score` (터치 확정건만) |
| 토큰 언락 경고 (2026-08-14) | `token_events.fetch_upcoming_unlocks()` — DeFiLlama 7일 내 5%+ 유통량 언락 예정 코인 맵. 6h DB 캐시. 회차 1콜, 전 코인 공유. 터치 확정건만 pct를 DB 기록. obs `token_unlock_warned` 카운터도 집계 | `levels.touch_token_unlock_pct` (터치 확정건만) |
| 섹터 집중도 (2026-08-14) | `risk_checks.check_sector_concentration()` — 같은 섹터 코인 알림 집중도 경고. CoinGecko 카테고리 데이터 축적 후 활성화 예정 | `monitor/risk_checks.py` (placeholder) |
| BTC 옵션 컨텍스트 (2026-08-14) | `options.fetch_btc_options_context()` — Deribit P/C Ratio·Max Pain·DVOL. P/C ≥1.0 또는 ≤0.30 → warn(수급 하향 보정). 정상 범위 0.5~0.6, 역대 최저 0.38/최고 0.84 기준. 5분 TTL 캐시, BTC 전용·전 코인 적용 | `monitor/options.py` (내부 보정 전용, 컬럼 미저장) |
| DVOL 내재변동성 (2026-08-14) | `options._fetch_dvol()` — Deribit 30일 내재변동성 지수. 40이하=평상시, 60~80=경계(warn+1), 80+=위기(warn+2). `fetch_btc_options_context()` 반환값에 포함 | `monitor/options.py` (수급 보정 입력) |
| DXY 달러 인덱스 (2026-08-14) | `macro.fetch_dxy()` — Yahoo Finance 비공식 API, 1시간 DB 캐시. 달러 강세 시 코인 약세 경향 (상관관계 −0.72~−0.90). >105 warn, <100 confirm | `monitor/macro.py` (수급 보정 입력) |
| 경제일정 자동 캘린더 (2026-08-16) | `macro.get_macro_events(conn)` — FOMC: the-calendar.net JSON 자동 수집(무인증). NFP·ISM: 규칙 기반(첫금·첫영업일). CPI·PPI·PCE·GDP·소매: 규칙 근사(±1~2일). 7일 DB 캐시, 정적 폴백. `get_nearby_macro_event(conn=conn)` 24h 전~2h 후 이벤트 감지 시 warn+1. 한국 발표시각 자동 계산(서머/윈터타임 반영, "한국 21:30" 또는 "한국 익일03:00") | `monitor/macro.py` (수급 보정 입력 + 브리핑) |
| BTC 청산 클러스터 (2026-08-14) | `liquidation.fetch_btc_liq_context()` — ByKaranteli pressure score·direction. long_heavy → warn, short_heavy → confirm. 5분 TTL 캐시, BTC 전용·전 코인 적용 | `monitor/liquidation.py` (내부 보정 전용, 컬럼 미저장) |
| 수급/자리 판정 | 터치 시점 스냅샷 (알림에도 표시). 2026-08-14: CVD·호가·옵션·청산·DXY·USDT.D·DVOL·FOMC/CPI로 수급 라벨 보정 — 우호+경고1→중립, 중립+경고2→주의, 중립+확인2→우호, 주의는 상향 불가 (`SUPPLY_CVD_NEG=-0.15/POS=0.15`, `SUPPLY_OBI_SELL_WALL=0.67/BUY_WALL=1.5`, `SUPPLY_PC_EXTREME_HIGH=1.0/LOW=0.30`, `SUPPLY_LIQ_WARN=long_heavy/CONFIRM=short_heavy`, DXY>105=warn/<100=confirm, USDT.D>8%=warn/<5%=confirm, DVOL>80=warn+2/>60=warn+1, FOMC/CPI 24h이내=warn+1) | `levels.touch_supply_verdict` / `touch_position_verdict` |
| MFE/MAE (2026-08-14) | `db.record_mfe_mae()` — 터치 후 판정 종결까지 최대유리이동(MFE%)·최대불리이동(MAE%). Freqtrade max_rate/min_rate 패턴. 1회 기록, 재기록 방지 | `levels.mfe_pct` / `levels.mae_pct` |
| 다구간 수익률 (2026-08-14 확장) | 기존 ret_24h/ret_72h에 ret_4h/ret_12h 추가 — 초기 반응(4h)·중기 추세(12h) 포착. 1h는 2분 폴링 대비 오차 과대로 제외 | `levels.ret_4h` / `levels.ret_12h` |
| 김프 급변 화살표 (2026-08-14) | `db.push_kimchi_history()` — 알림 시점 김프 이력 축적(meta, 12h 보존), ~6h 전 대비 ±0.5%p 이상이면 김프 행 끝 ▲/▼ 1글자 (`telegram._KIMCHI_DELTA_TH`) | `meta.kimchi_hist` (JSON) |
| 터치 소요시간 분석 (2026-08-13) | `audit_dump._compute_touch_time_stats()` — 구간별 적중률 + 등급 교차분석 | `data/audit/grade_stats_YYYY-WXX.json` (주간) |
| 토큰 언락 경고 (2026-08-14) | `token_events.get_unlock_warning(conn, symbol)` — DeFiLlama 무료 API, 7일 내 유통량 5%+ 언락 예정 코인 감지. 6시간 DB 캐시. 터치 시점 스냅샷 저장 (내부 축적 전용, 알림 미노출) | `levels.touch_token_unlock_pct` |
| MTF 정렬 점수 (2026-08-14) | `upbit.derive_mtf_alignment(pos_data, price)` — 일봉RSI>50(+1)·MA200 위(+1) = -2~+2 점수. fetch_position_data() 재사용 (추가 콜 0). 터치 시점 스냅샷 저장 (내부 축적 전용, 알림 미노출) | `levels.touch_mtf_score` |
| 섹터 집중도 (2026-08-14) | `risk_checks.check_sector_concentration()` — placeholder. 향후 CoinGecko 카테고리 데이터 축적 후 활성화 | `monitor/risk_checks.py` |
| IC/ICIR 신호 품질 (2026-08-14, 08-15 배선) | `signal_quality.compute_ic()` / `compute_icir()` — 점수↔ret_24h Spearman 순위 상관. IC≥0.05, ICIR≥0.5이면 실전 유효. show_status "신호 품질" 섹션 + 주간 감사덤프 grade_stats JSON에 연결. 첫 실측(08-15): IC 0.2072(n=167) 유효, ICIR 1.615(4주). 표기 전용 | `analytics/signal_quality.py` → `scripts/show_status.py`, `storage/audit_dump.py` |
| 시간대·요일별 성과 (2026-08-14, 08-15 배선) | `signal_quality.compute_hourly_performance()` / `compute_weekday_performance()` — KST 기준 24시·7요일 적중률, best/worst 요약만 표시(n≥5). 표기 전용 | `analytics/signal_quality.py` → `scripts/show_status.py` |
| Hash Ribbons (2026-08-15) | `hash_ribbons.fetch_hash_ribbons()` — mempool.space 무료 해시레이트 90일, SMA30/SMA60. 항복(30<60)=warn+1, 회복 크로스 14일 내=confirm+1 (수급 보정 입력). 6h DB 캐시. `hash_ribbons_enabled` 스위치 | `monitor/hash_ribbons.py` (내부 보정 전용) |
| 터치 스냅샷 5+2종 (2026-08-15 Tier1, 08-16 리뷰 수정) | 발송·억제 무관 전 터치에 첫 기록 우선 저장. **진행 중 캔들 배제**(08-16 Fix1): 실시간 터치의 잠정 종가 편향 방지 → NULL 행은 `_backfill_touch_quality`가 완성 캔들로 소급(회차당 ≤5행). **멤버별 관통**(Fix2): 클러스터 상단이 아닌 각 레벨 자신의 엔트리 기준. **touch_grade_ver**(Fix5): 산식 버전 도장. 기록 컬럼: `touch_grade`/`touch_score`(재채점), `touch_penetration_pct`/`touch_closed_below`(품질), `touch_tp_usd`(TP 동결), `touch_post_age_hours`(글나이, t_anchor 기준 Fix3), `touch_grade_ver`. `db.record_touch_snapshot()` | `monitor/price_check.py` → `storage/db.py` (기록 전용) |
| 터치 스냅샷 Tier2 3종 (2026-08-16) | `touch_atr_pct` — Wilder ATR(20)% (일봉 1콜 공유), `touch_btc_regime` — BTC vs 200일선 above/below (**관측일** 3일 히스테리시스 Fix6: 달력일→실관측 KST일, 같은 날 중복 1회 제한, 조회실패일 미카운트), `touch_dvol` — DVOL 수치(옵션 컨텍스트 재사용) | `monitor/upbit.py`·`macro.py`·`price_check.py` → `storage/db.py` (기록 전용) |
| 주간 감사 misses 섹션 (2026-08-16) | grade_stats JSON `misses` — 최근 7일 실패 신호별 MFE/MAE·소요시간·터치 판정·자동 분류(즉시반전 MFE<1% / 이익반납 ≥2% / 중간 / 판정불가), 상한 30 + class_counts. `post_age_stats` — 글 나이 버킷별 적중률 (<24h/24-72h/72-120h/120h+) | `storage/audit_dump.py` (내부 전용) |
| 피드백 Wilson 집계 (2026-08-16) | show_status "알림 피드백 (시험)" — 전체·등급별·작성자별(상위 10) up율 + Wilson 80% 하한. 10표 미만 판단 보류, 자동조치는 30표+ 정책 (표기 전용) | `scripts/show_status.py` |
| 터치 품질 분석기 (2026-08-16) | `analyze_touch_quality.py` — 꼬리터치 vs 종가이탈 그룹별 + 침투 깊이 3버킷별 승률·ret_24h·MFE/MAE (그룹당 n≥20 도달 시 판정 — 첫 터치 vs 재확인 알림 전환의 자체 근거). 읽기 전용 CLI | `scripts/analyze_touch_quality.py` |
| 캔들 종가 보존 (2026-08-15, Fix7) | `upbit.fetch_range_since` 반환 튜플 4→5원소 `(start, end, high, low, close)` — 뒤에 붙여 기존 인덱스 소비자 무영향. `_fetch_ohlc` **캔들별 가드**(Fix7): 필드 결손 캔들 개별 스킵, 나머지 보존(종전: 1개 불량이 전량 None) | `monitor/upbit.py` |
| TP 후속 무음 승계 (2026-08-16 Fix8) | 중간/최종 TP 알림의 유/무음이 본알림 등급 정책을 승계 — C등급 무음 터치의 후속 TP만 유음이던 비대칭 제거. `touch_grade` 우선, 없으면 `grade` 폴백 | `monitor/price_check.py` |
| 사다리 진값 저장 (2026-08-16 Fix10) | `collector/extractor.py` — TP 사다리 게이트를 `1 < len ≤ 12` → `1 < len`으로 확장, 13단+ 사다리도 진값 저장(v5 감점 연산 무오염). 표시 12단 상한은 `notify/telegram.py`로 이관 | `collector/extractor.py` → `notify/telegram.py` |
| validate_ic score 전용 비교 (2026-08-16 Fix11) | `--feature` != score 시 인샘플 IC 비교 라인 생략 — 다른 피처의 IC를 score IC 기준선과 비교하던 오류 제거 | `scripts/validate_ic.py` |
| show_status PK 인덱스 (2026-08-16 Fix12) | 피드백 JOIN 방향 수정: `CAST(l.id AS TEXT)=f.ref` → `l.id=CAST(f.ref AS INTEGER)` — rowid PK 인덱스 활용 | `scripts/show_status.py` |
| purged IC 검증 (2026-08-15) | `scripts/validate_ic.py` — 시간순 3-fold, ±168h purge + 168h embargo, `--feature COL` 로 임의 컬럼 IC 감사 가능. 첫 실측: 시간외 평균 +0.193 vs 인샘플 +0.202 → 유지 판정 | `scripts/validate_ic.py` (읽기 전용 CLI) |
| 작성자 삭제율 (2026-08-15) | 주간 감사 JSON `author_deletion_rates` — post_url 기준, 5건+ 작성자, 상위 20. "패배 글 삭제" 작성자 감지 (안티게이밍) | `storage/audit_dump.py` (내부 전용) |
| 코드 리뷰 5건 수정 (2026-08-16) | **Fix1** macro.py FOMC fetch timeout `min(timeout,3.0)` 캡 (핫패스 최대 6s). **Fix2** `_STATIC_EVENTS` Q1 2027 비FOMC 항목 추가 + `_d()`/`_ev()` 헬퍼 재구성(kst_time 선언 시 포함). **Fix3** `refresh_macro_calendar` 죽은 FOMC 타입 필터 제거. **Fix4** `telegram.send()` reply_markup 파라미터·페이로드·독스트링 완전 삭제. **Fix5** 테스트 3종(test_price_logic·resilience·touch_recording) reply_markup=None 스텁 서명 정리 | `monitor/macro.py`, `notify/telegram.py`, `scripts/test_*.py` |
| 내부 지표 3종 추가 (2026-08-16) | **F1** `touch_mfe_atr_ratio` — MFE ÷ ATR20% 배수(변동성 스케일 라벨), `record_mfe_mae()` 종결 시 자동 계산. **F5** `touch_atr_band_pct` — ATR×0.5 섀도 밴드(고정 1% 대비 비교용), `record_touch_snapshot()` 내 atr_pct 기록과 동시 저장. **F6** `touch_supply_1h` — 터치 1h 후 수급 재판정("라벨|근거"), `_backfill_supply_1h()` 이 deriv+cvd 2콜로 회차당 최대 5행 소급. 알림 무변동 — 전부 내부 DB 기록 전용 | `storage/db.py`, `monitor/price_check.py` |

---

## 운영 인프라 (2026-08-13)

| 항목 | 파일 | 내용 |
|------|------|------|
| Deadman switch | `scripts/run_cycle.py` | 사이클 완료 시 `deadman_ping_url` (healthchecks.io 등) GET → 미도착 시 외부 알림 |
| 감사덤프 진부화 감시 | `scripts/run_cycle.py` | `META_LAST_DUMP` 타임스탬프 2× interval 초과 시 텔레그램 경고 |
| 스택트레이스 보존 | `scripts/run_cycle.py`, `run_collect.py` | 모든 `logger.error`에 `exc_info=True` 적용 |
| KST 타임존 통합 | `utils/time_kst.py` | `KST`, `day_kst()`, `iso_to_epoch()` 단일 소스 |
| 의존성 버전 고정 | `requirements.txt` | 메이저 버전 상한 추가 (`<3`, `<2`, `<1`) |
| Dependabot | `.github/dependabot.yml` | pip 주간 보안 패치 자동 PR 생성 |
| 정비 스크립트 아카이브 | `scripts/archive/` | 일회성 repair 스크립트 5건 이동 |
| 시작 시 시크릿 검증 | `scripts/run_cycle.py` | `TELEGRAM_BOT_TOKEN`/`CHAT_ID` 미설정 시 즉시 실패 (무음 블랙아웃 방지) |
| SQLite WAL 모드 | `storage/db.py` | `journal_mode=WAL`, `busy_timeout=5000` — 동시 접근 안정성 |
| levels.author 인덱스 | `storage/db.py` | 주간리포트 작성자별 쿼리 풀스캔 방지 |
| alerts_log 보존정책 | `storage/db.py` → `monitor/price_check.py` | 30일 초과 항목 자동 정리 (`prune_alerts_log`, 2분 주기 `run_once`에서 호출) |
| Binance ratio 통합 | `monitor/binance.py` | 3개 ratio 함수 → `_fetch_fapi_ratio` 공통 헬퍼 |
| TP sanity 상수화 | `collector/extractor.py` | `_SANITY_LO_MULT=0.25`, `_SANITY_HI_MULT=4.0` 추출 |
| meta_float 통합 | `storage/db.py` | `run_cycle.py`·`audit_dump.py` 중복 헬퍼 → DB 모듈 단일 정의 |
| GH Actions 정리 | `price-check.yml` | 구버전 마이그레이션 스텝·`actions:read` 권한 제거 |
| 수집 ingest 오류집계 | `scripts/run_collect.py` | 개별 ingest 실패 건수 로그 출력 |

---

## 부가 메시지·상호작용 (2026-08-15)

엔트리 알림 양식(동결)과 무관한 별도 기능들.

| 항목 | 파일 | 내용 |
|------|------|------|
| 모닝 브리핑 | `notify/morning_brief.py` → `scripts/run_cycle.py` | 하루 1회, KST 8~10시 창(`morning_brief_kst_hour_from/to`)에 시장환경 요약 1통 (~12줄): BTC 가격·김프·F&G·BTC.D/USDT.D·달러지수/BTC변동성(한국어 라벨)·🇺🇸 S&P500·나스닥 전일 등락률(Yahoo Finance 무료 1h 캐시)·매크로 D-day(7일 전망)·어제 터치 수·대기 레벨 수. meta `last_morning_brief_date` 게이트 — 발송 성공 시에만 날짜 마킹(실패 시 창 내 재시도). 데이터 None인 줄은 생략. 끄기: `morning_brief_enabled=False` |
| 알림 반응 피드백 (시험) | `notify/telegram.py`(버튼) + `notify/feedback_poll.py`(수거) + `storage/db.py`(`alert_feedback` 테이블) | 터치 본알림에만 👍도움됨/👎별로 인라인 버튼(`fb:<level_id>:<up\|down>`, 본문 텍스트 불변). 2분 회차마다 getUpdates 폴링(meta `feedback_update_offset`), 웹훅 불필요. 유저당 1표(재투표=갱신), UNIQUE(ref, tg_user_id). 내부 축적 전용. 끄기: `alert_feedback_enabled=False` — 버튼 미부착·폴링 중단 |

---

## 비활성 기능 (코드 존재, 알림 미발송)

| 기능 | 설정 키 | 현재값 |
|------|---------|--------|
| 예고 알림 (진입 전 접근) | `preview_alert_enabled` | `False` |
| 섹터 집중도 경고 | `risk_checks.check_sector_concentration()` | placeholder (카테고리 데이터 미축적) |

---

## 설정 파일 위치

`config/settings.py` — 위 모든 수치의 원본. 조정 시 이 파일 하나만 수정.
