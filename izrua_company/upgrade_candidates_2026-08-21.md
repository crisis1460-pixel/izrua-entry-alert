# 고도화 후보 조사 (2026-08-21, 3인 개발사 병렬 탐색)

> 기획 1 + 개발 2 에이전트 병렬 조사. 기존 거절 항목(SL 감점류, 판정데이터 알림·리포트 노출,
> 워쳐 푸시 재활성화, 유니버스 확대·필터 완화, 등급 산식 변경, 알림 본문 행 추가류) 제외 완료.

## A. 인프라·신뢰성 (개발A)

| # | 후보 | 근거 | Effort | 비고 |
|---|------|------|--------|------|
| A1 | 펀딩비 폴백 순서 재배치 | binance.py:297 — 1순위 Binance 직접호출이 GH Actions(미국 IP)에서 상시 451 차단인데 매번 먼저 시도, 터치 발송마다 최대 10s 낭비. CoinGecko를 1순위로 | S | 즉효 |
| A2 | touched 복합 인덱스 | db.py — `WHERE status='touched' AND x IS NULL` 핫쿼리 4곳이 status 단일 인덱스만 사용. levels 영구보존 정책이라 시간 지날수록 비용 증가 | S | 조기 처리 유리 |
| A3 | 회차 소요시간 기록 | 07-27 큐잉 사고(수집 회차 5-8분 → 2분 트리거 몰림) 재발 감지 수단 없음. meta.last_cycle_duration_sec 기록 → show_status 노출 | S | 순수 기록 |
| A4 | 데드맨 스위치 설정 | run_cycle.py:593 `_deadman_ping()` 구현돼 있으나 URL 미설정 no-op. 텔레그램 자체 장애 시 백업 채널 부재 | S | 사용자 액션 필요 (healthchecks.io 무료 URL 발급) |
| A5 | 발송 경로 총 데드라인 | price_check.py:1074-1219 — 터치 1건당 외부 HTTP 15+ 순차, 개별 timeout 만 있고 총예산 없음. 네트워크 저하 시 발송 1건이 분 단위 지연 가능. 전체 30s 데드라인 가드 | M | 설계 필요 |
| A6 | 저장소 히스토리 성장 대응 | 12,022 커밋/.git 33MB, secret-scan fetch-depth:0 전체 클론 비용 단조 증가. diff 스캔 분리 or 이력 압축 | M | Risk Med, 신중 |

## B. 분석·품질 (개발B)

발견: **write-only 컬럼 다수** — touch_funding_rate/oi_pct/long_short_ratio/top_trader_ratio/
taker_buy_sell_ratio/stablecoin_mcap_b/ma200_above/mtf_score/token_unlock_pct/atr_pct/btc_regime/
dvol/ret_4h/ret_12h 등이 기록만 되고 아무 데도 안 읽힘.

| # | 후보 | 답하는 질문 | Effort | 가치 |
|---|------|------------|--------|------|
| B1 | Tier2 3종 승률 교차분석 | BTC 레짐·고DVOL 구간에서 적중률 달라지나? (analyze_touch_quality 패턴 재사용) | S | 4 |
| B2 | 초기반응 IC (ret_4h/12h/24h) | 등급 점수가 몇 시간 만에 실현되나? (compute_ic 컬럼만 교체) | S | 3 |
| B3 | MTF·토큰언락 유효성 검증 | MTF 점수·언락 경고가 실제 승률과 상관 있나? → 수급보정 승격 판단 근거 | S | 3 |
| B4 | 캘리브레이션 드리프트 추적기 | v5 관찰기 동안 등급별 적중률이 흔들리나? 주차 JSON 8주치를 시계열로 연결 | M | 5 |
| B5 | 미사용 파생지표 상관 감사 | 6개 write-only API 지표가 승률과 상관 있나, 비용만 쓰나? | M | 4 |
| B6 | 작성자×시간대 교차 분석 | 이 작성자는 몇 시에 믿을 만한가? (표본 희소성 게이트 설계 필요) | M | 3 |

## C. 기능·제품 (기획)

발견: **문서-코드 불일치** — 매뉴얼 267/310행 "알림 반응 피드백(feedback_poll.py)" 구현 완료로
기재돼 있으나 실제 파일 없음. 08-16 Fix4에서 telegram.send()의 reply_markup 파라미터 삭제됨.
alert_feedback 테이블·Wilson 집계는 존재하나 투표 유입 경로가 없어 영구 0건 상태.

| # | 후보 | 내용 | Effort | 가치 |
|---|------|------|--------|------|
| C1 | 작성자 고급성과 주간리포트 편입 | get_author_advanced_stats()(PF·R기대값·연속손실·일관성) 이미 계산됨 — render_weekly_report 섹션 추가만 | S | 5 |
| C2 | 해시리본 크로스 단독 알림 | SMA30/60 골든/데드크로스(수주~수개월 1회) 시장 전체 1회성 알림. 알림피로 없음 | S | 4 |
| C3 | 텔레그램 봇 명령 인프라 | 피드백 실배선 + /status 원격 조회. 사전 확인 필요: Fix4 삭제가 의도적 폐기인지 | M-L | 5 |
| C4 | 작성자 삭제율 배지 (리포트 전용) | get_author_deletion_stats() 이미 계산 — 주간 리포트만 노출 | S | 3 |
| C5 | 김프 스파이크 단독 알림 | push_kimchi_history 12h 이력 활용, 급변 시 1회성 알림 | S-M | 3 |
| C6 | 섹터 집중도 활성화 | CoinGecko /coins/{id} 응답의 categories 필드(추가 콜 0) 파싱 → placeholder 활성화 | M | 3 |

## 종합 우선순위 (value/effort)

**즉시 (S급, 이번 주):**
1. A1 펀딩비 순서 (터치 발송 지연 즉효)
2. A2+A3 인덱스+소요시간 기록 (예방)
3. B1+B2+B3 분석 3종 세트 (기존 패턴 재사용, 축적 데이터 활용 개시)
4. C1 작성자 고급성과 리포트 편입

**다음 스프린트 (M급):**
5. B4 캘리브레이션 드리프트 (v5 관찰과 직결 — 가치 최고)
6. C2 해시리본 크로스 알림
7. A5 발송 데드라인 가드
8. C3 텔레그램 명령 인프라 (사전 확인 후)

**보류/신중:** A6 (히스토리 압축 — 파괴적), B5·B6·C4·C5·C6 (표본/설계 대기)
