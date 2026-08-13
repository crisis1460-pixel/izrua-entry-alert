# 엔트리 알림 파이프라인 매뉴얼

> 마지막 업데이트: 2026-08-13 (비채점 인프라 개선: TG 4096 자동분할·속도제한, deadman switch, 감사덤프 진부화 감시, 스택트레이스 보존, KST 타임존 통합, 의존성 버전 고정)  
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
**산식 버전:** `grade_formula_ver = "v4"`

### 3-1. 점수 구성 요소

| 항목 | 만점 | 상세 |
|------|------|------|
| 팔로워 수 | 25점 | 100k+=25, 50k+=22, 10k+=17, 5k+=12, 1k+=8, 100+=3, <100=1 |
| 가격 근접도 | 20점 | ±2% 이내=20, 아래 2~10%=17, 위 2~5%=12, 위 5~10%=8, 아래 10%+=15 |
| 목표가 거리 | +12 | 5~15%=+12, 15~25%=+8, 25~40%=+4, 3~5%=-2, 2~3%=-4, 0~2%=-6 |
| 데이터 완성도 | 23점 | 진입+목표=20, 추가 SL=+3; 하나만=8, 둘다없음=2 |
| 작성자 실적 | 15점 | Wilson 80% 하한 기준 TP1 적중률: ≥55%=+15, ≥40%=+10, ≥25%=+5 (최소 5건 필요) |

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

### 게이트 4: 일일 발송 한도
```
코인당 하루 최대 3회    (alert_max_per_coin_per_day = 3)
```
터치 알림에만 적용. 예고 알림(현재 비활성)은 미적용.

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
| CVD 비율 (2026-08-11) | `binance.fetch_cvd_ratio()` — 터치 시점 4h taker 매수-매도 불균형 [-1,+1] | `levels.touch_cvd_ratio` (터치 확정건만) |
| 펀딩비 (2026-08-13) | `binance.fetch_funding_rate()` — 터치 시점 무기한 선물 펀딩비(%) | `levels.touch_funding_rate` (터치 확정건만) |
| OI 변화율 (2026-08-13) | 터치 시점 OI 24h 변화율(%) | `levels.touch_oi_pct` (터치 확정건만) |
| 롱/숏 비율 (2026-08-13) | `binance.fetch_long_short_ratio()` — 전체 계정 롱 비율 (0~1) | `levels.touch_long_short_ratio` (터치 확정건만) |
| 탑 트레이더 비율 (2026-08-13) | `binance.fetch_top_trader_position_ratio()` — 상위 20% 트레이더 롱 비율 | `levels.touch_top_trader_ratio` (터치 확정건만) |
| 선물 테이커 비율 (2026-08-13) | `binance.fetch_taker_buy_sell_ratio()` — 선물 매수/매도 비율 | `levels.touch_taker_buy_sell_ratio` (터치 확정건만) |
| 스테이블코인 시총 (2026-08-13) | DeFiLlama — 전체 스테이블코인 유통량 (십억$) | `levels.touch_stablecoin_mcap_b` (터치 확정건만) |
| 호가 매수/매도 압력 | 터치 시점 스냅샷 | `levels.touch_bid_ask_ratio` |
| 200일선 상/하 | 터치 시점 스냅샷 | `levels.touch_ma200_above` |
| 수급/자리 판정 | 터치 시점 스냅샷 (알림에도 표시) | `levels.touch_supply_verdict` / `touch_position_verdict` |
| 터치 소요시간 분석 (2026-08-13) | `audit_dump._compute_touch_time_stats()` — 구간별 적중률 + 등급 교차분석 | `data/audit/grade_stats_YYYY-WXX.json` (주간) |

---

## 운영 인프라 (2026-08-13)

| 항목 | 파일 | 내용 |
|------|------|------|
| Deadman switch | `scripts/run_cycle.py` | 사이클 완료 시 `deadman_ping_url` (healthchecks.io 등) GET → 미도착 시 외부 알림 |
| 감사덤프 진부화 감시 | `scripts/run_cycle.py` | `META_LAST_DUMP` 타임스탬프 2× interval 초과 시 텔레그램 경고 |
| 스택트레이스 보존 | `scripts/run_cycle.py`, `run_collect.py` | 모든 `logger.error`에 `exc_info=True` 적용 |
| KST 타임존 통합 | `utils/time_kst.py` | `KST`, `day_kst()` 단일 소스 → 5개 파일에서 import |
| 의존성 버전 고정 | `requirements.txt` | 메이저 버전 상한 추가 (`<3`, `<2`, `<1`) |
| 정비 스크립트 아카이브 | `scripts/archive/` | 일회성 repair 스크립트 5건 이동 |

---

## 비활성 기능 (코드 존재, 알림 미발송)

| 기능 | 설정 키 | 현재값 |
|------|---------|--------|
| 예고 알림 (진입 전 접근) | `preview_alert_enabled` | `False` |

---

## 설정 파일 위치

`config/settings.py` — 위 모든 수치의 원본. 조정 시 이 파일 하나만 수정.
