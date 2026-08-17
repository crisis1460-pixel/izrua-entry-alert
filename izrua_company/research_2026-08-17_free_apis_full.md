# 무료 크립토 API 종합 리서치 — 2026-08-17

10개 병렬 리서치 에이전트 결과 통합. 알트 스윙 알림봇(Upbit·한국) 관점.

---

## Tier S — 지금 바로 붙일 가치 있음 (Top 10)

| # | API | 무료 한도 | 즉시 활용 아이디어 |
|---|-----|-----------|-------------------|
| 1 | **FRED** (St. Louis Fed) | 무제한, 이메일만 | yfinance 스크래핑 → 안정 대체 (DXY/VIX/DGS10/DGS2/SP500/Gold) |
| 2 | **업비트 비공식 notices JSON** | 무키·무제한 (예의상 30s) | 신규 상장 즉시 감지 알림 |
| 3 | **한국은행 ECOS** | 무료, 회원+키 | USD/KRW 공식값 → 김프 정확도 상승 |
| 4 | **DART OpenAPI** | 개인 20,000/일 | 위메이드·카카오·네이버 크립토 공시 조기 포착 |
| 5 | **Coinalyze** | 40 req/min, 이메일 키 | 펀딩·OI·롱숏·**청산 히트맵** 통합 (Coinglass 무료 대체) |
| 6 | **Coin Metrics Community** | 무키, 1.6 rps | active_addresses·tx_count·hash_rate (Glassnode 일부 대체) |
| 7 | **Reddit OAuth API** | 100 QPM 비상업 | 티커별 언급 급증 감지 |
| 8 | **GitHub GraphQL** | 5000 pt/hr | Electric Capital taxonomy로 심볼→레포 매핑, 개발자 활동 |
| 9 | **네이버 검색어 트렌드** | 1000 req/일 | 리테일 관심도 프록시 (SerpAPI 대체) |
| 10 | **DEX Screener** | 300 req/min 무키 | DEX 트렌딩·유동성 급변 감지 |
| 11 | **Alchemy** | 30M~300M CU/월 (플랜 표기 상충) | ERC-20 whale·Webhook 알림 |
| 12 | **Helius (Solana)** | 100k credits/월, Webhooks 무료 | Solana 밈코인·SOL 생태 추적 |
| 13 | **Moralis** `/erc20/{addr}/owners` | 40k CU/일 | Etherscan Pro인 홀더 리스트를 무료로 |

---

## Tier A — 보조/특수 용도

- **CoinGecko Demo** `/search/trending` + `community_data` — 무료키, 30/min
- **CMC keyless** `/v3/fear-and-greed/latest` + Altcoin Season Index — 등록 없음
- **Alchemy** — 월 30M CU, 이메일만, ERC-20 whale tracking
- **Ankr Freemium** — 200M credits/월, 이메일, `ankr_getTokenHoldersCount` 등
- **Etherscan v2** — 60체인 단일키, 3 rps, 100k/day (2026-07 축소 주의)
- **Bybit/OKX/Bitget/BitMEX public** — 무키, 파생 원본 검증
- **RSS 피드** (CoinTelegraph/CoinDesk/코인니스/토큰포스트/블록미디어)
- **Mempool.space** — 무키, BTC 실시간 WS
- **ClankApp** — 완전 무료 whale 피드 (SLA 없음)
- **Coindar API 2.0** — 이벤트 캘린더 무료
- **BlockCypher** — BTC/LTC/DOGE, 3 rps
- **Bitquery** — GraphQL, 1000 pt/월 (10행 캡)
- **The Graph** — 100k queries/월 (지갑 인증 필요)
- **StockTwits `.X` 스트림** — 인증 없이 200/hr, Bullish/Bearish 태그
- **Messari** — 20 req/min, asset profile
- **Santiment Free** — 1000/월, 30일 지연 (국면용만)

---

## Tier B — 백업/제한적

- Twelve Data (800/day, 지수·FX), Stooq CSV(무키·EOD), yfinance(변경 리스크)
- Flipside(무료지만 큐 대기), Chainbase(200k credits/월), Dune(2500 credits/월)
- CoinCap v3(200/min, 11년 히스토리), LiveCoinWatch(10000/day)
- Solscan public(429 빈발), Tokenview(5000/day, 120체인)
- Blockchair(30/min 키필요), Nansen(10 credits/day 사실상 데모)
- Airdrops RSS(광고성 노이즈 다수)

---

## 함정 — 무료지만 유용한 부분 유료 (사용 금지)

| 서비스 | 상태 |
|--------|------|
| CryptoPanic | 2026-04-01 무료 폐지 |
| CryptoCompare/CCData | 2026-05-21 무료 API 완전 폐기 |
| X/Twitter API | 2026-02 free 폐지, pay-per-use |
| Nitter | 2026 사실상 사망 |
| pytrends | 2025-04 아카이빙, 첫 콜부터 429 |
| LunarCrush Hobby | 시장 데이터만, 소셜 지표 유료 ($90/월+) |
| Glassnode Standard | UI만, API 접근 실질 없음 |
| CryptoQuant | Basic 웹만, API는 Pro $109/월부터 |
| Coinglass | Hobbyist $29/월부터 |
| CoinAPI | free 아님, $25 트라이얼 크레딧만 |
| Amberdata/Kaiko/Laevitas | 트라이얼·엔터프라이즈만 |
| Whale Alert | $29.95/월, 무료는 웹/트위터만 |
| Token Terminal | API 자체 $49/월+ |
| CoinDesk Data | 250k lifetime 이후 세일즈 컨택 |
| Nomics | 2022 종료 |
| IntoTheBlock | Sentora로 병합, 무료 API 없음 |
| Arkham | API 수동 신청, 유료 |
| DappRadar Pro | RapidAPI 유료 |
| Covalent/GoldRush | 14일 트라이얼만 |
| Alpha Vantage | 25 req/day (2024 하향) |
| Marketstack | 100 req/월, EOD only |
| Polygon.io Free | 5 req/min, 15분 지연 |
| Tiingo | 지수·commodity 없음 |
| EODHD | 20 req/day |
| Tokenomist unlocks | 웹만, API는 트라이얼 |
| DefiLlama unlocks | Pro $300/월 |
| CryptoRank fundraising | Sandbox 무료엔 락 |
| Birdeye Free | 1 rps 사실상 사용 불가 |

---

## 통합 스택 제안 (모두 무료)

### 매크로 (yfinance 대체)
`FRED` (일봉 정본) + `Stooq CSV` (백업) + `yfinance` (실시간 근사)

### 뉴스/이벤트
`CoinTelegraph/CoinDesk RSS` + `코인니스 텔레그램` + `Coindar` + `Reddit OAuth` (티커 언급)

### 센티먼트
`alternative.me F&G` (현행) + `CMC keyless` (Altcoin Season) + `Reddit RSS` (알트 언급 스파이크)

### 파생/청산
`Coinalyze` (통합) + `Bybit/OKX/Bitget public` (원본 검증)

### 온체인
`Coin Metrics Community` (무키 base) + `Etherscan v2` (60체인 EVM tx) + `Alchemy 또는 Ankr` (whale/holder) + `Mempool.space` (BTC 실시간)

### 한국 특화
`업비트 notices JSON` (상장 감지) + `ECOS` (USD/KRW) + `DART` (공시) + `네이버 트렌드`

### 개발자 활동
`GitHub GraphQL` + `Electric Capital taxonomy`

### DEX/알트 트렌드
`DEX Screener` + `GeckoTerminal` + `CoinGecko trending`

---

## 우선 도입 후보 (봇 현행에 즉시 유익)

1. **업비트 notices JSON** — 상장 감지 알림 = 알트 최대 알파
2. **한국은행 ECOS** — 김프 계산 정확도 (현재 없음)
3. **FRED** — 매크로 소스 안정화 (현재 yfinance 리스크)
4. **Coinalyze** — 청산 히트맵 통합 (현재 수동 fetcher만)
5. **DART OpenAPI** — 국내 크립토 공시 조기 포착 (현재 없음)
6. **CMC Altcoin Season Index** — 알트 시즌 판정 강화
7. **Reddit RSS** — 티커 언급 스파이크 (소셜 시그널)
8. **DEX Screener** — 신규 DEX 유동성 급변 감지
9. **Coin Metrics Community** — 온체인 활성 지표
10. **GitHub GraphQL + Electric Capital** — 개발자 활동 프록시

---

## 각 리포트 원본
- Cat 1 (매크로/TradFi): FRED·Stooq·yfinance 상세
- Cat 2 (뉴스/센티먼트): RSS·Reddit·StockTwits·CoinGecko 상세
- Cat 3 (RPC/DeFi): Alchemy·Ankr·DefiLlama·Dune 상세
- Cat 4 (DeFi/온체인/웨일): DEX Screener·Etherscan·ClankApp 상세
- Cat 5 (블록 익스플로러): Etherscan v2·BlockCypher·Mempool·Tokenview 상세
- Cat 6 (상장/언락/이벤트): 업비트 notices·Coindar·Tokenomist 상세
- Cat 7 (트렌드/개발자/센티먼트): GitHub GraphQL·SerpAPI·CMC 상세
- Cat 8 (온체인 애널리틱스): Coin Metrics·Santiment·Messari·Nansen 상세
- Cat 9 (한국 특화): 업비트·ECOS·DART·네이버·코인니스 상세
- Cat 10 (파생/청산): Coinalyze·Bybit·OKX·Bitget 상세

세부 curl/엔드포인트/함정 노트는 각 리포트 원본 참조 (이 세션 대화 로그 내).
