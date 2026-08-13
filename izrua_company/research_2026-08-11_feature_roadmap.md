# 기능 로드맵 리서치 — 2026-08-11

> 기획 담당자 정기 리서치. 현재 구조 기준 추가 가능한 기능·지표 조사.

---

## 현황 요약

**터치 시점 수집 중인 지표 (11개)**

| 컬럼 | 내용 |
|------|------|
| touch_price_krw / touch_usdt_krw | 터치 가격·환율 |
| touch_bid_ask_ratio | 업비트 호가 잔량비 |
| touch_fear_greed | 공포탐욕지수 |
| touch_kimchi_pct | 김치프리미엄 |
| touch_btc_dominance | BTC 점유율 |
| touch_volume_rank | 업비트 거래대금 순위 |
| touch_supply_verdict | 수급 판정 (펀딩+OI+가격) |
| touch_position_verdict | 자리 판정 (RSI+MA) |
| touch_ma200_above | 200일선 상/하 (내부) |
| touch_cvd_ratio | 4h CVD [-1,+1] (내부) |

**유니버스 품질 필터 (4종)**: 신규 90일 / Binance 미상장 / 업비트 경고 / 순위 급락

---

## 제안 목록

### A. 즉시 구현 (새 API 콜 없음)

#### A-1. `touch_funding_rate` 컬럼 추가
- **상태**: `price_check.py:793`에서 `_snap_funding` 이미 조회 중. DB에 저장만 안 함.
- **구현**: `_OUTCOME_COLUMNS` 추가 + `record_touch_verdicts()` 파라미터 1개 확장.
- **가치**: "수급판정=우호일 때 펀딩비 분포" 등 사후 분석 가능. CVD·MA200과 동일 성격.

#### A-2. `touch_oi_pct` 컬럼 추가
- **상태**: `price_check.py:814`에서 `_oi_chg` 이미 계산. supply_verdict에만 사용.
- **구현**: A-1과 동일 패턴. ALTER TABLE 1열 + record_touch_verdicts 확장.
- **가치**: OI 급증 시 터치 성과가 어떻게 다른지 추적.

#### A-3. 유니버스 24h 거래대금 하한 필터
- **상태**: CoinGecko `/coins/markets` 응답에 `total_volume`(USD) 포함. 현재 미활용.
- **구현**: `_apply_quality_filters`에 필터 1종 추가. 설정 키 `universe_min_volume_usd`.
- **가치**: 저유동 코인 신호 차단. 현재 volume_rank는 감시 단계에서만 보임.
- **기본값 제안**: 5천만 달러 미만 제외 (top-300 중 하위 50개 정도 탈락 예상).

#### A-4. `touch_time_to_touch_hours` 파생값 — audit 분석용
- **상태**: `collected_at`·`touched_at` 모두 DB에 있음. 계산만 안 함.
- **구현**: audit_dump `run_weekly_audit`에서 grade_stats 산출 시 "평균 터치 소요 시간" 추가. DB 컬럼 불필요.
- **가치**: 빠른 터치 신호(수집→터치 < 1h)와 느린 터치의 적중률 차이 파악.

---

### B. 중기 기능 (소규모 구현)

#### B-1. TP 다단계 알림 활성화 여부 확인·정비
- **상태**: `tp_alert_idx`, `pending_tp_kind`, `advance_tp_alert_idx` 인프라 완성됨 (`price_check.py:1266`).
- **과제**: 실제로 TP2·TP3 알림이 발송되는지 프로덕션 로그 확인 필요. 설정 키로 on/off 가능한지 검토.
- **가치**: 진입 후 TP 진행 상황을 자동 알림으로 추적.

#### B-2. 시총 절대 하한 필터
- **상태**: 현재 top-300 순위 기준만 있음. 장세에 따라 300위 시총이 수천만 달러 수준일 수 있음.
- **구현**: CoinGecko 응답의 `market_cap`(USD) 활용. 설정 키 `universe_min_mcap_usd`.
- **기본값 제안**: 1억 달러 미만 제외.

#### B-3. 알트코인 시즌 컨텍스트
- **상태**: `touch_btc_dominance` 이미 수집 중.
- **구현**: BTC 점유율 < 50% → 알림 메시지에 "🌊 알트 시즌" 컨텍스트 1줄 추가. 단순 임계값.
- **가치**: 시장 국면에 따른 알림 해석 보조. 설정 불필요.

---

### C. 장기 분석 기반 기능

#### C-1. 내부 축적 데이터 패턴 리포트
- **상태**: `touch_cvd_ratio`, `touch_ma200_above`, `touch_supply_verdict` 수집 시작 ~08-11.
- **과제**: 데이터 최소 4주 축적 후(~09-08) 분석 가능. "CVD 양수 + MA200 위 + 수급 우호" 조합의 hit_rate vs 반대 조합 비교.
- **구현**: `query_*.py` 스크립트로 일회성 분석. 결과에 따라 알림 게이트 추가 여부 결정.

#### C-2. 작성자 실적 기반 등급 임계값 개인화
- **상태**: `get_author_advanced_stats()`로 PF·R-Exp 조회 가능. 현재 min_grade=C 고정.
- **아이디어**: PF > 2 + r_track ≥ 10인 작성자의 D등급 신호를 C급으로 승격.
- **과제**: 데이터 충분히 쌓인 후(작성자당 10건+) 시뮬레이션 필요.

---

## 우선순위 추천

| 순위 | 항목 | 이유 |
|------|------|------|
| 1 | A-1 (`touch_funding_rate`) | 콜 추가 없음, 구현 15분, 분석 가치 높음 |
| 2 | A-2 (`touch_oi_pct`) | A-1과 동시 구현 가능 |
| 3 | A-3 (거래대금 하한 필터) | 유니버스 품질 강화, 저유동 노이즈 차단 |
| 4 | B-1 (TP 다단계 알림 상태 확인) | 인프라 이미 있음 — 현황 파악만 하면 됨 |
| 5 | A-4 (터치 소요 시간 분석) | audit 스크립트 확장, DB 변경 없음 |
