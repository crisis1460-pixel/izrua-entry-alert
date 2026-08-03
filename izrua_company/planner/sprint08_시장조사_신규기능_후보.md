# 스프린트08 시장조사 — 신규 기능 후보 (2026-08-03)

- 조사 범위: 지난번 스프린트07(배경정보 5종) 외 새로운 각도 8개
- 리서치 관점: 스윙 트레이더 · 업비트 스팟 전용 · SL 비중시 · 무료 지향 · 알림 피로 우려

## 8개 섹션 요약

### 1. 온체인/거래소 흐름 시그널
- ★★★ **DefiLlama 스테이블코인 공급** — 완전 무료 API, "매수 대기 자금 확장/축소" 배지
- ★★★ **CryptoQuant 거래소 순유입** (BTC/ETH/XRP) — 매도 준비 리드 지표, 스크래핑 필요
- ★★ Arkham 링크·Whale Alert
- ★ Glassnode (해상도 24h 스윙 부적합)

### 2. 청산·파생 데이터 (스팟 편향)
- ★★★ **펀딩 레짐 전환** — 기존 펀딩 인프라 재활용, 30일+ 음수 → 양수 플립 시 강한 반전
- ★★★ **Coinglass 집계 청산량** — 스팟 저점/고점 예측력 실증(2026-06 저점 표본), 스크래핑
- ★★ OI 델타·롱/숏 극단·청산 히트맵 링크

### 3. 대체 시그널 소스 (TV 외)
- ★★★ **YouTube Data API 채널 감시** — 무료(1일 10K units), 한국 크립토 유튜버 5~15명 uploads 폴링
- ★★ Reddit r/CryptoCurrency Daily Discussion(velocity), Bluesky(6개월 후 재평가)
- ★ X API(유료화·200만건 상한), Nitter(붕괴), Kakao 오픈챗(약관 위반)

### 4. 알림 UX 혁신 (2025-2026)
- ★★★ **Urgency 티어링 (Critical/Ambient)** — 상위 20%만 즉시, 나머지 아침 다이제스트
- ★★★ **하이브리드 딜리버리** — 즉시 + 08시 요약, 알림 총량 60~70% 체감 감소
- ★★★ **Telegram 인라인 버튼** — `[💤 저자 하루 뮤트]` `[📌 관심]` `[✅ 진입함]`
- ★★★ **멀티TF 중복제거** — 같은 심볼 4h+1d 동시 시 첫 알림 in-place 편집
- ★★ Piper TTS 오프라인 음성 요약 (다이제스트 첨부)

### 5. 사후 라이프사이클 (Post-alert)
- ★★★ **Upbit read-only API "실제 매수 자동 감지"** — 자산조회 권한만, 무료, 초당 30회, 옵트인
- ★★★ **TP1 도달 자동 팔로우업** — **무손절 스타일과 정확 일치**, "TP1 후 진입가 방어" 개념
- ★★★ **주간 회고 리포트** — "취한 5 / 스킵 12, 등급별 사후 PnL"
- ★★★ **아웃컴 태깅 학습 루프** — 4주 skip 100% 저자 자동 뮤트 제안
- ★★ 저널링 프리셋 (진입 이유 원-태그)

### 6. AI/LLM 시그널 품질
- ★★★ **regex 파싱 fallback** (Gemini 2.5 Flash-Lite 무료, 1000 RPD)
- ★★★ **뉴스 → 심볼 영향도 태깅** ("SEC XRP 승소 → +0.9")
- ★★★ **논지 클러스터링** (3명+ 저자 합의 → "합의 알림" 격상)
- ★★ 등급 근거 한 줄 자동 설명, 댓글 감정 부스트, sanity-check 계층
- ★★ CryptoTrade 방식 저자별 장세 적합도 (주 1회 배치)
- 예상 월 비용 $0~5 (Groq/Gemini/Cerebras 무료 티어 + Claude Haiku 4.5 소량)
- **원칙**: LLM은 부가 태그·설명·보조 배지로만, 결정 권한 안 넘김

### 7. 한국 특화 데이터
- ★★★ **업비트 공지 API** — 무인증 JSON, 신규 상장 5분 내 감지, 알림 주 1~3건 (알림 피로 없음)
- ★★★ **김치프리미엄 티커별 급변** — 현재는 전역 김프만 표시, ±5%+ 급변 티커별 감지
- ★★★ **Coinness Live Feed 무료 API** — business@coinness.live 문의 (파트너 40+ 통합)
- ★★ TokenPost/BlockMedia RSS, Xangle 공시 스크래핑, Bithumb 공지(교차검증)
- ★ Kakao 오픈챗·DCInside 코인갤·네이버 카페 (약관/노이즈)

### 8. 안정성·관측성
- **핵심 인사이트**: Telegram보다 GitHub Actions가 더 취약 (2025-2026 Actions 16건 장애)
- ★★★ **GitHub Actions 실패 이메일 알림** (5분 세팅)
- ★★★ **healthchecks.io 하트비트** — 무료 20개, cron 스킵 감지 (30분)
- ★★★ **Discord 웹훅 fallback** — 완전 무료, 텔레그램 실패 시 이중 발송 (30분)
- ★★★ **cron-job.org 워치독** — healthchecks 이중 안전망 (15분)
- ★ Kakao 알림톡(사업자 필수), Pushover($5, iOS 지연 시), GitLab CI(400분 부족)

## 스프린트08 최종 후보 랭킹 (두 리포트 크로스 검증 후)

두 개의 독립 리서치를 병렬 실행했고, 서로 다른 관점에서 후보를 도출.
아래 랭킹은 **양쪽 리포트에서 공통 추천된 항목을 상위**로 두고 통합.

### Tier S (양쪽 모두 즉시 도입 권장)
| # | 기능 | 이유 |
|---|------|------|
| 1 | **봇 헬스체크 3종** (8) | Healthchecks.io + Discord fallback + GHA 실패 알림. 봇 죽으면 신뢰 붕괴. 90분·무료 |
| 2 | **업비트 공지 신규 상장 감지** (7-1) | 무인증 JSON, 알파 최대, 주 1~3건 (알림 피로 zero) |
| 3 | **Upbit orderbook 유동성 태그** (7 신규) | 알림 심볼의 KRW 마켓 상위 20호가 총액 → "🟢/🟡/🔴 유동성" 태그. 슬리피지 방어. 무료 인프라 재활용 |
| 4 | **Telegram 인라인 버튼 + callback 로깅** (4-3 + 5) | `[봤음][찜][트레이드함][무시]` — 개인화 승률 트래킹 데이터 축적. zero-cost |

### Tier A (한 리포트 강력 추천, 명확한 가치)
| # | 기능 | 이유 |
|---|------|------|
| 5 | **펀딩 레짐 전환** (2-2) | 기존 인프라 재활용, 30일+ 음수 → 양수 플립 시 반전 시그널 |
| 6 | **DefiLlama 스테이블 배지** (1-2) | 무료·무한대 API, 매수 대기 자금 컨텍스트 |
| 7 | **TP1 팔로우업** (5-2) | 무손절 스타일과 정확 일치, "TP1 후 진입가 방어" 개념 |
| 8 | **주간 회고 리포트 텔레그램 발송** (5-3) | 이미 인프라 있음, 알림 총량 안 늘고 신뢰만 상승 |

### Tier B (관점 상충·조건부, 관찰 후 판단)
| # | 기능 | 두 리포트 관점 |
|---|------|--------------|
| 9 | **LLM regex fallback + 뉴스 태깅** (6-1,5) | ① 강력 추천(Gemini Flash-Lite 무료) / ② 무과금 원칙 위배로 보류 |
| 10 | **김치프리미엄 티커별 급변** (7-5) | 한국 스윙 필수라는 관점 vs 이미 전역 김프 있음 |
| 11 | **YouTube 채널 감시** (3-5) | 안정 소스 vs SNR 낮음 |
| 12 | **Coinglass 청산량** (2-1) | 예측력 실증 vs 스윙 현물엔 무관 |
| 13 | **Arkham 웨일 웹훅** (1 신규) | 별도 `#whale` 서브채널만, 심볼 알림 병합 금지 |
| 14 | **하이브리드 딜리버리** (4-2) | 알림 감소 크지만 UX 대개편 |
| 15 | **Upbit read-only 매수 자동 감지** (5-1) | 옵트인, 키 등록 UX 부담 |

### 양쪽 리포트 명시적 배제
- Coinness API 문의(파트너 승인 소요·응답 불확실)
- LLM debate·논지 클러스터링(복잡도 대비 가치 불확실)
- X API 유료화, Nitter 붕괴, Kakao 오픈챗 스크래핑
- Coinglass 유료 API, Nansen/CryptoQuant 유료 티어
- 청산 히트맵 자동 파싱 (canvas 렌더 난이도·현물 무관)
- 본격 트레이딩 저널 SaaS 통합(TradeZella·TraderVue)

## 권장 다음 액션

1. **~08-10 v4+5% 필터 관찰기 종료** 대기
2. 그 이후 Tier S부터 순차 도입:
   - 8번(헬스체크 3종, 90분·무료·즉시)
   - 1번(업비트 공지, 알파 크고 즉시)
   - 3-4번(DefiLlama·펀딩 레짐)
3. Tier A는 사용자 실사용 피드백 반영해 순서 조정
4. 전 항목 사용자 알림 피로 원칙 준수 — 신규 알림 소스 도입은 항상 배지·컨텍스트 우선, 새 트리거는 최후

## 출처 (전체 URL은 원본 리포트 참조)

Coinness Live Feed, Upbit Open API Docs, Kimpga, YouTube Data API, DefiLlama Docs,
CryptoQuant User Guide, Coinglass Liquidations, Yellow Funding Regime, arxiv 2504.14633
(LLM Financial Entity Extraction), arxiv 2410.12464 (Fact-Subjectivity Reasoning),
arxiv 2501.00826 (Multi-Agent Crypto Portfolio), Groq/Gemini/Cerebras pricing,
healthchecks.io, Better Stack, Discord Webhook Docs, GitHub Actions Notifications,
PagerDuty Dynamic Notifications, Rootly Alert Deduplication, Alertatron Trailing Stop,
Piper TTS, Nansen Smart Alerts.

## 관련 파일
- 스프린트07 리서치: sprint07_시장조사_기능보강_신규기능.md
- SL 없는 시그널 등급 리서치: sprint08_SL없는시그널_등급설계_리서치.md
