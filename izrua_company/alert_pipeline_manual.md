# 엔트리 알림 파이프라인 매뉴얼

> 마지막 업데이트: 2026-08-18 (워쳐 SL률 연동 + VS 테스트 임계값 갱신)  
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

### 1-1. 기본 선정 (2026-08-17 재설계)
| 조건 | 내용 |
|------|------|
| 모집단 | **업비트 KRW 전체 마켓** (283종) — 종전 CG top-N ∩ Upbit 은 시총 300위+ 소형 알트 미포함 |
| CG 메타 | CoinGecko 시총 상위 300위 매칭 시 rank/tier_icon/price_usd 부여, 밖은 rank=None·tier='·' |
| 기본 제외 | 스테이블코인 (USDT, USDC, DAI 등 20종) |
| 최종 크기 | ~191종 (품질 필터 후, LSK/MTL/KAITO 등 소형 알트 포함) |

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
등급 ≥ D    (alert_min_grade = "D", 2026-08-17 사용자 결정: C→D 완화. 실측 승률 D 45% > C 36%)
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
코인당 하루 최대 5회    (alert_max_per_coin_per_day = 5, 2026-08-17: 3→5 완화)
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
| 워쳐 SL률 — 코인별 (2026-08-18) | `watcher_stats.load_watcher_data()` → `coin_sl_rates` — 워쳐 DB 7일 signal_tracking×chartist_stats JOIN, signal_hash 중복 제거. 터치 알림 뱃지 표시: ≥2건 시 info(기본)/risk(60%+). 4h 인프로세스 캐시, price_check 회차별 lazy-load | `collector/watcher_stats.py` → `monitor/price_check.py` → `notify/telegram.py` |
| 워쳐 SL률 — 시장 전체 (2026-08-18) | `watcher_stats.load_watcher_data()` → `market_sl` — 전 코인 합산 SL률. ≥5건 시 모닝 브리핑에 📉 행 추가 | `collector/watcher_stats.py` → `notify/morning_brief.py` |
| 작성자 적중률 로컬 연동 (2026-08-18) | `watcher_stats.load_author_stats()` (하위 호환 래퍼) — chartist_stats 적중률·팔로워·화이트리스트. `load_watcher_data()` 통합 호출로 전환, 단독 캐시 공유 | `collector/watcher_stats.py` (기존 수집 경로 유지) |
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

---

## DB 무결성 수정 이력 (라운드7, 2026-08-16)

| 버그 | 위치 | 수정 내용 |
|------|------|-----------|
| SCHEMA ↔ `_DAILY_STATS_COLS` 불일치 | `storage/db.py` SCHEMA | `daily_stats` 정의에 `suppressed_global_cap`, `token_unlock_warned` 두 컬럼 추가 — 기존 DB는 `_migrate()` ALTER TABLE로 이미 보완되고 있었으나 SCHEMA가 허위 문서화 상태였음 |
| `get_observation_report()` 데이터 누락 | `storage/db.py` | `suppressed_global_cap`·`token_unlock_warned`가 DB에 쌓이지만 리포트 반환값에 없었음 → 두 키 추가 |
| `grade_v5_since` meta 마커 부재 | `storage/db.py` `_migrate()` | `grade_v3_since`만 기록하고 v5 배포(08-16) 시점 마커가 없었음 → `grade_v5_since` 조건부 기록 추가 |

## 10라운드 전수검토 즉시수정 이력 (2026-08-16)

| 심각도 | 버그 | 위치 | 수정 |
|--------|------|------|------|
| HIGH | `upsert_level` SELECT→INSERT TOCTOU | `storage/db.py:527` | `IntegrityError` catch → 중복 삽입 시 레벨 손실 방지 |
| HIGH | `_backfill_supply_1h` funding 키 오타 | `monitor/price_check.py:327` | `.get("funding")` → `.get("fr")` — 1h 수급 재판정 펀딩 입력이 항상 None이었음 |
| MEDIUM | `coingecko.py` baseAsset KeyError | `collector/coingecko.py:165` | `s["baseAsset"]` → `s.get("baseAsset", "")` — Binance exchangeInfo 응답 방어 |
| MEDIUM | Yahoo Finance `result[]` IndexError | `monitor/macro.py:68,119` | `(result or [{}])[0]` — 빈 리스트 응답 시 IndexError 방어 |
| 데드코드 | `secrets_status()` 미사용 함수 | `config/settings.py` | 삭제 |
| CI위험 | `test_tradingview_live.py` CI glob 노출 | `scripts/` | `probe_tradingview_live.py` 리네임 (실네트워크 테스트 CI 제외) |

## Tier2 개발 이력 (2026-08-17)

| 항목 | 데이터·기능 | 반영 위치 |
|------|------------|-----------|
| **#6 preview→touch 스레딩** | telegram.send 반환 bool→Optional[int], reply_to_message_id kwarg | notify/telegram.py, storage/db.py (preview_message_id 컬럼 + set/get 함수), monitor/price_check.py (touch 발송 시 조회·전달, preview 성공 시 저장) |
| **#5 히트맵 파이프라인** | touch_adx14/touch_bb_width_pctile 스냅샷 축적, get_regime_heatmap 집계, 주간 리포트 섹션 | storage/db.py (2개 컬럼 + 집계 함수), notify/telegram.py (_regime_heatmap_section, 각 셀 n<5 자동 스킵), scripts/run_weekly_report.py (호출부) |

**#6 스레딩 동작**: preview 알림 발송 성공 → 각 레벨 preview_message_id 저장 → 같은 클러스터 touch 시 조회해 reply_to_message_id 로 전달. Telegram UI 에서 예고→터치가 스레드로 연결. 예고 미발송·저장 실패는 종전대로 최상위 메시지로 발송(폴백).

**#5 자동 침묵**: 각 셀 n<5 이면 셀 생략, 전체 셀 부족이면 섹션 통째 스킵 → 표본 도달(각 셀 최소 5건) 전까지 리포트에 아무것도 안 나옴. Universe300 관찰 완료(~8말) + 2~3주 축적 후 자연스럽게 활성화.

**필터 안정성**: 두 기능 모두 알림 필터·등급 산식 무영향 (표시·기록 전용).

## Tier1 3건 개발 이력 (2026-08-17)

| 항목 | 데이터/계산 | 반영 위치 |
|------|------------|-----------|
| **F1: ETH.D + 알트시즌** | CoinGecko `/global`의 `market_cap_percentage.eth`, ALT.S 이미 있음 | 모닝 브리핑에 렌더 (💠 이더 점유율, 🌊 알트시즌 N) |
| **F2: 스테이블 시총+7d 변화** | DeFiLlama `/stablecoincharts/all`, 최신 vs 7일 전 % | 모닝 브리핑에 렌더 (💵 스테이블 XB (+Y%/7d)) |
| **F3: BB Squeeze + ADX** | `upbit.py` adx14/bb_width_pct/bb_width_percentile (일봉 1콜 공유, 추가 API 콜 0) | 알림엔 ADX≥25 배지만(📈 추세장), 등급엔 ADX+BB 둘 다 (regime 키, ±2~3점) |

**F3 등급 임계** (`_regime_points`):
- ADX ≥25 +3 (추세장), <20 -2 (횡보), 20~25 0
- BB Width 백분위 ≤20 +2 (압축·폭발 전조), ≥80 -2 (팽창 후반), 나머지 0
- None 폴백 = 0 (감점 아님)

**필터 안정성**: F3 재채점은 발송 확정 후 rep 표시 등급에만 적용, 필터(min_grade) 통과 여부는 종전 등급 유지 → 관찰 기간(v5 08-16 배포) 알림 발송량 안정.

## 08-17 확장 배포 통합 이력 (되돌리기 카드)

**한 사이클 안에 원자적으로 적용된 8건**. 부작용 발생 시 아래 값을 이전값으로 되돌려 롤백.

| 항목 | 이전 | 현재 | 위치 |
|------|------|------|------|
| `alert_min_grade` | "C" | **"D"** | config/settings.py |
| `alert_max_per_coin_per_day` | 3 | **5** | config/settings.py |
| `volume_spike_multiplier` | 5.0 | **3.0** | config/settings.py |
| `volume_spike_min_krw_60m` | 200_000_000 | **100_000_000** | config/settings.py |
| `oi_spike_pct` | 15.0 | **10.0** | config/settings.py |
| `telegram_source_channels` | 1개 | **4개** (+cryptosignals0rg, wolfoftrading, BitcoinBullets) | config/settings.py |
| `build_universe` 방식 | CG top-N ∩ Upbit KRW (86종) | **Upbit KRW 전체 스캔** (191종) | collector/coingecko.py |
| 뉴스·시황 알림 (`kind='news'`) | 없음 | **활성** (매매 셋업 파싱 실패 + 심볼 매칭 성공 → 원문 요약) | notify/news_brief.py, notify/telegram.render_news_brief |

**예상 발송 증가**: 매매 +3~4/일 + 뉴스 +3~5/일 + spike +1~4/일 = **하루 +7~14건**.

**관찰 기간**: 최소 3~5일. 위양성·피로 증가 시 우선순위대로 되돌리기: (1) alert_min_grade D→C (2) news_alert_enabled=False (3) spike 임계 복원.

**리뷰 라운드 수정 이력 (2026-08-17 저녁)**:
- coingecko._apply_quality_filters: rank=None TypeError 방어 (fail-open 방지, 6개 필터 무력화 위험 제거)
- coingecko 필터: min_volume/min_mcap None 처리 명시 (`None` = 측정 불가 → 통과)
- run_collect.py: `if had_setup is False:` 엄격 비교 (None=ingest 예외 시 뉴스 오탐 방지)
- news_brief._summary: title+description 결합 (헤드라인만 있는 채널 스킵 방지)
- news_brief._rate_limit_ok: 순서 재조정 (코인→채널→글로벌) + 세션 캐시 short-circuit
- notify/news_brief.py: level_ids 필드 재사용 계약 주석 명시
- config/settings.py universe_top_n 주석 갱신 (의미 변경 반영)
- test_price_logic VS5/VS6: SETTINGS override 로 임계 재조정에 견고

**전수 리뷰 버그 수정 (2026-08-17 심야)**:
- telegram.send(): mid 파싱 실패 폴백 1→-1 (Telegram mid=1 은 실제 메시지 — 스레딩 오염 방지)
- telegram: _SEND_MIN_INTERVAL_SEC 1.0→1.5 (그룹 429 여유)
- price_check: `from monitor import binance` 모듈 상단 이동 (_backfill_supply_1h NameError 수정)
- price_check: fetch_week52·fetch_usdt_price 미래핑 HTTP → try/except 격리 (회차 전체 롤백 방지)
- price_check: alert_ledger.append 를 db.record_alert 앞으로 이동 + DB 기록 try/except 격리
- price_check: need_low 밴드 5%→15% (플래시크래시 소급 스캔 범위 확장)
- price_check: preview msgid 저장 시 `_sent_mid > 0` 가드 (폴백 -1 저장 방지)
- market_sentiment: all-None 딕셔너리 캐시 오염 방지 (전 API 실패 시 stale 반환)
- options.py/liquidation.py: 스테일 캐시 최대 수명 1시간 상한 추가 (무기한 반환 방지)
- run_cycle._check_audit_dump_stale: 무음 except → logger.debug 추가
- test_price_logic T27: alert_min_grade C→D 설정 변경 정합 (settings override/restore)

## 뉴스 한글 번역 (2026-08-17)

| 항목 | 설명 |
|------|------|
| **번역 모듈** | `notify/translator.py` — Google Translate (비공식) 메인 + MyMemory 폴백 + 원문 유지 (3중 안전) |
| **Google Translate** | 비공식 gtx 엔드포인트, 키 불필요, Gemini NMT 품질, 저볼륨 차단 리스크 없음 |
| **MyMemory 폴백** | 키 불필요 5,000자/일, Google 실패 시 자동 전환 |
| **설정** | `news_translate_enabled: True` (config/settings.py). API 키 불필요 |
| **통합 위치** | `news_brief.maybe_send_news_brief()` → `_summary()` 후 `translate_en_ko()` 호출 → `render_news_brief()` |
| **캐시** | 인메모리 24h TTL (동일 원문 재번역 방지) |
| **장애 안전** | 번역 전 경로 실패 시 원문 영어 그대로 발송 (알림 누락 없음) |

## 뉴스 오탐 필터 (2026-08-18)

| 항목 | 설명 |
|------|------|
| **모호 심볼 차단** | OPEN, SIGN, GAS, ID, T, W 등 20개 일반 영단어 심볼을 뉴스 경로에서 제외 (매매 시그널 무영향) |
| **프로모션 필터** | bonus, airdrop, mt5, vip channel 등 광고 키워드 포함 시 스킵 (2026-08-21 대소문자 버그 수정 — MT5/MT4 가 소문자 비교에 안 걸리던 문제) |
| **매매 결과 리캡 필터 (2026-08-21)** | "manually closed +929.8 pips, profits secured, well played" 류 청산 결과 자랑 글 스킵. 점수제: 고정밀 패턴(N pips·manually closed·profits secured·well played·TP/SL hit·closed at N·clean win·익절 완료 등) 2점, 보조 패턴(trade update·breakeven·target reached) 1점 — 합산 2점 이상만 스킵. 일반 시황의 profit-taking/closed above 단어 단독으론 안 걸림 (회귀 NB8·NB8b·NB9) |
| **위치** | `notify/news_brief.py` — `_AMBIGUOUS_SYMBOLS` + `_PROMO_KEYWORDS` + `_RESULT_PATTERNS` |

## 코드 다듬기 (2026-08-18)

### A. 에러 방어 강화 (5건)
| 대상 | 내용 |
|------|------|
| `storage/db.py` connect() | 예외 시 명시적 `conn.rollback()` 추가 |
| `price_check.py` base_eff | `t_rate > 100` 가드 — USDT/KRW 극소값 방어 |
| `price_check.py` _r() | `entry ≤ SL` 이상 데이터 경고 로그 추가 |
| `storage/db.py` record_mfe_mae | `touch_atr_pct > 0.01` 극소값 ratio 폭등 방어 |
| `notify/telegram.py` TP알림 | `entry_krw=0` 시 "0.0%" 대신 % 생략 |

### B. 설정 일원화 (5건)
| 하드코딩 | 이관 위치 |
|---------|-----------|
| sanity 배수 0.25/4.0 (extractor+price_check 중복) | `settings.sanity_lo_mult` / `sanity_hi_mult` |
| 재발송 차단 600초 | `settings.resend_block_sec` |
| TG 발송 간격 1.5초 | `settings.telegram_send_min_interval_sec` |
| TV 상세/프로필 예산 20/10 | `settings.tv_cycle_detail_budget` / `tv_cycle_profile_budget` |
| TG 소스 차단 쿨다운 1800초 | `settings.telegram_source_block_cooldown_sec` |

### C. 코드 위생 (5건)
| 대상 | 내용 |
|------|------|
| `telegram.py` SEP/FNG_KR | private → public export (morning_brief 크로스 모듈 접근 정리) |
| `db.py` json_str_list | private → public (price_check/test 외부 호출 정리) |
| `run_cycle.py`/`morning_brief.py` | BaseException 핸들러에 SystemExit 재발생 추가 |
| `morning_brief.py` maybe_send_brief | DB 연결 3회 → 판정+조립 1회로 통합 (TOCTOU 해소) |
| `translator.py` 캐시 | dict → OrderedDict, O(n) eviction → O(1) popitem |

## FRED 매크로 통합 (2026-08-17)

| 항목 | 데이터 | 반영 위치 |
|------|--------|-----------|
| **FRED 공용 헬퍼** | `_fetch_fred_latest(series_id)` / `_fetch_fred_series(series_id, limit)` — 무료·이메일 가입만 (120 req/min·무제한 일), 키는 `.env` FRED_API_KEY | `monitor/macro.py` |
| **US 지수 폴백** | Yahoo 우선 → 결측분만 FRED `SP500`/`NASDAQCOM` 최근 2개 종가 비율로 재계산 | `_fetch_us_indices_fresh()` |
| **VIX 신규** | FRED `VIXCLS` (S&P 500 변동성) — 시장 공포 벤치마크. 1h DB 캐시 | `fetch_vix()`, 모닝 브리핑 (😱 VIX N) |
| **10년 국채 신규** | FRED `DGS10` — 위험자산 밸류에이션 벤치마크. 1h DB 캐시 | `fetch_ust_10y()`, 모닝 브리핑 (🏦 미10년물 N%) |

**결측 안전**: 키 미설정·API 실패 시 조용히 None → 해당 브리핑 행만 생략(종전 규칙 유지). 알림/등급 산식 무영향.

**첫 실호출 검증(2026-08-17)**: VIX 14.63, 10Y 4.63% 정상 조회.

## StockTwits 소셜 심리 (2026-08-17)

| 항목 | 데이터 | 반영 위치 |
|------|--------|-----------|
| **StockTwits 헬퍼** | `fetch_sentiment_stats(coin, timeout)` — 무료·무등록·200 req/hr, UA 헤더만 필요. 심볼 뒤에 `.X` 접미 (BTC.X, PEPE.X). 최신 30건 스트림에서 Bullish/Bearish 태그 집계 | `monitor/stocktwits.py` (신설) |
| **커버 특성** | SOL/SUI/APT/TAO/WLD/TIA/PEPE/SHIB 등 최근 유행 알트 커버 강함 (Coin Metrics 미커버 자산 상당수 보완). WEMIX/KAIA 등 국내 알트는 심볼 미존재 or 태그 없음 → 자연 스킵 |
| **노이즈 컷** | 태그된 표본 <5건 시 `bullish_ratio=None` (판정 유보) | `_MIN_TAGGED=5` |
| **터치 시점 조회** | 발송 확정 터치에만 1콜. 15/일 × 1콜 = 15콜/일 (200 req/hr 여유) | `monitor/price_check.py` |
| **알림 배지** | ≥0.75 `💬 소셜 매수세 N% (매수 유리)` / ≤0.30 `💬 소셜 매도세 N% (매수 부담)`. 중립 무표기 | `notify/telegram.py` E1 리스크/긍정 그룹 자동 배치 |
| **등급 반영** | `_social_sentiment_points(ratio)` — ≥0.75 +1 / ≤0.30 -1 / None 0. 소셜은 노이즈 축이라 가중치 낮음(±1). breakdown 'social' 키 | `collector/grading.py` |
| **DB 섀도** | `touch_stwits_bullish_ratio` (기록 전용, IS NULL 가드) | `storage/db.py` |

**Reddit OAuth 대체 배경**: Reddit "Responsible Builder Policy" 로 신규 앱 생성 반복 실패 → StockTwits 로 전환 (사용자 결정, 2026-08-17). 사용자 준비 작업 0 + Bullish/Bearish 태그 이미 존재 → 노이즈 필터링 부담 완전 회피.

**커버 상보성 (Coin Metrics + StockTwits)**:
- Coin Metrics: BTC/ETH/XRP/ADA/DOGE/LTC 등 레거시 L1 138종 (활성 주소)
- StockTwits: SOL/SUI/APT/TAO/WLD/TIA/PEPE/SHIB 등 유행 알트 (소셜 심리)
- 두 축은 데이터 종류·커버 자산이 상보 → 유니버스 300 상당수 커버

**필터 안정성**: rep 재채점만, min_grade 필터는 종전 유지. 관찰 기간 알림 발송량 안정.

**회귀 테스트**: infra 8건(정상 파싱·표본 부족·404·빈 응답·등급·배지 3종) + grading 2건. infra 107·grading 74 통과.

## Coin Metrics 온체인 활성주소 (2026-08-17)

| 항목 | 데이터 | 반영 위치 |
|------|--------|-----------|
| **Coin Metrics 헬퍼** | `fetch_active_addr_percentile(coin, conn)` — 무료·무키·1.6 rps·CC 비상업. AdrActCnt 30일 창의 현재값 백분위(0~100). 24h DB 캐시 / 미커버 7일 캐시 | `monitor/coinmetrics.py` (신설) |
| **커버 자산 (18종)** | BTC/ETH/XRP/ADA/DOGE/LTC/BCH/ETC/BNB/TRX/DASH/ZEC/XTZ/EOS/XLM/LINK/UNI/AAVE (실측). SOL/AVAX/MATIC/SHIB/PEPE 등은 유료 티어 → API 콜 없이 즉시 None | `COVERED` 화이트리스트 |
| **터치 시점 조회** | 발송 확정 터치에만 조회. 미커버 자산은 API 콜 0, 커버 자산은 24h DB 캐시로 하루 첫 알림만 실호출 | `monitor/price_check.py` |
| **알림 배지** | 극단만 노출 (백분위 ≥80 `⛓ 온체인 활발 N위 (매수 유리)` / ≤20 `⛓ 온체인 저조 N위 (매수 부담)`). 중립(20~80) 무표기 | `notify/telegram.py` render_alert `active_addr_pctile` kwarg |
| **등급 반영** | `_onchain_activity_points(pctile)` — ≥80 +1 / ≤20 -1 / None 0. 가중치 낮은(±1) 이유는 커버율 5% (유니버스 형평성 유지) | `collector/grading.py` breakdown 'onchain_addr' 키 |
| **DB 섀도** | `touch_active_addr_pctile` (기록 전용, IS NULL 가드) | `storage/db.py` |

**필터 안정성**: rep 재채점에만 반영, 필터(min_grade) 통과 여부는 종전 유지. 관찰 기간 알림 발송량 안정.

**커버 한계**: 유니버스 300 중 실질 ~16종(BNB/DASH는 Upbit 미상장) → 알트 대부분 배지·등급 반영 없음(자연 스킵). BTC/ETH/XRP 등 메이저 터치 시에만 신호 노출.

**콜 예산**: 미커버 자산은 함수 진입에서 즉시 None → API 콜 0. 커버 자산도 24h 캐시로 하루 자산당 1콜 → 극단적으로 커버 16종이 하루 각 1회 → 16콜/일 (1.6 rps = 9600콜/일 대비 여유).

**회귀 테스트**: infra 8건(미커버 스킵·백분위 계산·표본 부족·HTTP 오류·등급·배지 3종) + grading 2건. infra 98·grading 72 전 통과.

## DEX Screener 통합 (2026-08-17)

| 항목 | 데이터 | 반영 위치 |
|------|--------|-----------|
| **DEX Screener 헬퍼** | `fetch_token_stats(token_addr)` — 무료·무키·300 req/min. 페어 전체 집계(유동성/24h볼륨/buys·sells/buy_ratio/top_chain) | `monitor/dexscreener.py` (신설) |
| **Upbit→DEX 매핑** | CoinGecko `/coins/{id}` platforms → 첫 non-null 컨트랙트 주소. 24h 파일 캐시(성공)/7일(실패) | `monitor/upbit_dex_mapping.py` (신설), `data/upbit_dex_addr_cache.json` |
| **터치 시점 조회** | 발송 확정 터치에만 1콜(예고·억제는 미조회 — 콜 예산). 매핑 없는 네이티브(XRP/ADA/BTC)·매핑 실패 → `_snap_dex=None` 자연 스킵 | `monitor/price_check.py` |
| **알림 배지 3종** | 저유동성 <100k$ (`💧 DEX 유동성 낮음 Nk$`), 매수 우위 ≥65% (`🟢 DEX 매수 우위 N%`), 매도 우위 ≤35% (`🔴 DEX 매도 우위 N%`) | `notify/telegram.py` (render_alert `dex_stats` kwarg) |
| **등급 반영** | `_dex_points(buy_ratio, liquidity)` — 매수 우위 +2 / 매도 우위 -2 / 저유동 -3 (독립 가감). breakdown 키 `dex`, sum(values)에 포함 | `collector/grading.py` (score_breakdown/calculate_grade/calculate_grade_with_breakdown/regrade_current) |
| **DB 섀도 컬럼** | `touch_dex_liquidity_usd` / `touch_dex_volume_24h_usd` / `touch_dex_buy_ratio` 3개 (기록 전용, IS NULL 가드) | `storage/db.py` (`_OUTCOME_COLUMNS` + `record_touch_snapshot` kwargs) |

**필터 안정성**: 발송 확정 후 rep 재채점(F3/DEX 통합)에만 반영 → 필터(min_grade) 통과 여부는 종전 등급 유지. 관찰 기간 알림 발송량 안정.

**콜 예산**: 알림당 최대 2콜 (CoinGecko 매핑 첫회 + DexScreener). 매핑은 24h 캐시라 사실상 하루 1회. 발송당 실질 1콜 → 15/일 (DexScreener 300 req/min 대비 여유).

**함정·현실**: (1) 순수 KR/JP 알트(WEMIX/XPLA 등 platforms 빈값)는 매핑 실패로 배지·등급 반영 없음(형평성 이슈 없음, 그냥 데이터 없음). (2) 소형 알트 노이즈 방지 위해 유동성 임계 100k$ 하한. (3) DEX 페어 유동성은 CEX·업비트 KRW 시장과 무관 — buy_ratio는 온체인 트레이더 판단만 반영.

**회귀 테스트**: infra 11건(dex/mapping/render_alert 배지) + grading 3건(_dex_points/breakdown/regrade). infra 90건·grading 70건 통과.

## Coinalyze 파생 폴백 (2026-08-17)

| 항목 | 데이터 | 반영 위치 |
|------|--------|-----------|
| **Coinalyze 헬퍼** | `fetch_funding_rate()` / `fetch_open_interest()` / `fetch_oi_change_24h()` — 무료·40 req/min·이메일만, 키는 `.env` COINALYZE_API_KEY | `monitor/coinalyze.py` (신설) |
| **펀딩 최종 폴백** | Binance→CoinGecko→Bybit→OKX 전 경로 실패 시 Coinalyze로 마지막 시도. 30+ 파생 거래소 통합 → 소형 알트 커버리지 확장 | `binance.fetch_funding_rate` 체인 확장 |
| **OI/파생 스냅샷 폴백** | CoinGecko 미상장 알트 → Coinalyze `fetch_open_interest`/`fetch_funding_rate`로 `{fr, oi, pchg=None}` 반환 | `binance.fetch_deriv_snapshot` |

**활성 조건**: 기존 4개 소스가 모두 결측인 경우에만 호출 → 주 경로가 정상 동작하는 대부분 케이스에서 API 콜 0. `touch_funding_rate`/`touch_oi_pct` 결측률 감소 = 등급 산식·수급 판정 축의 데이터 완성도 상승.

**단위 검증(2026-08-17 실측)**: Coinalyze value 는 이미 %단위(BTC ~0.008, XRP ~-0.012 등). 기존 소스들은 raw decimal → `*100` 로 %단위 변환하는데, Coinalyze는 원시값 그대로 반환해 소스 간 단위 일치.

**필터 안정성**: 기존 함수 시그니처·반환 형식 불변. 알림 필터·등급 산식 무영향.

**회귀 테스트**: infra 6건(키 미설정·정상 파싱 3개·HTTP 오류·최종 폴백 체인).

## 10라운드 전수검토 2차 수정 이력 (2026-08-17)

| 심각도 | 버그 | 위치 | 수정 |
|--------|------|------|------|
| MEDIUM | `alert_ledger.append()` OSError만 catch | `storage/alert_ledger.py` | Exception으로 확장 (docstring 보장) |
| MEDIUM | `alert_ledger.merge_files()` 예외 없음 | `storage/alert_ledger.py` | OSError try/except 추가 |
| MEDIUM | 백필 루프 per-row 예외 격리 없음 | `storage/db.py` | `_backfill_volume_watch_urls`, `_backfill_outcome_chain` 행별 예외 격리 |
| MEDIUM | upbit 429 → 회차 전체 스킵 | `monitor/upbit.py` | 1초 sleep 후 1회 재시도 추가 |
| LOW | `options._fetch_dvol` data[-1][4] IndexError | `monitor/options.py` | `len(data[-1]) > 4` 길이 검사 |
| LOW | `binance.fetch_cvd_ratio` dict 응답 IndexError | `monitor/binance.py` | `isinstance(list)` 체크 추가 |
| LOW | `market_sentiment` TTL 만료 후 fallback 없음 | `monitor/market_sentiment.py` | 실패 시 stale 캐시 반환 |
| 성능 | `run_collect` score_breakdown 이중 호출 | `collector/grading.py`, `scripts/run_collect.py` | `calculate_grade_with_breakdown()` 추가, 단일 호출로 교체 |
