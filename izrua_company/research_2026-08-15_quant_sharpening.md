# 리서치 보고서: 소표본 환경에서 시그널 정밀도를 높이는 확립된 기법들
- 작성일: 2026-08-15
- 작성: 퀀트 리서치 (웹 조사 기반, 출처 명기)
- 대상 시스템: izrua 진입 알림봇 (TradingView 애널리스트 진입가 수집 → 터치 알림 → TP1/168h 판정)
- 현재 상태: 수집 ~170건, 판정 완료 수십~170건, 일 ~15건 증가. grade v4 IC 0.207 / ICIR 1.6 (vs ret_24h)
- 제약: 무료 데이터, GitHub Actions 서버리스, 순수 Python(stdlib+requests), 소표본(n≈100~300)
- 금지 사항 준수: SL 기반 감점 제안 없음 / 기존 필터 완화 없음 / 유니버스 확대 없음 / 알림 메시지 추가 없음

---

## 요약 (TL;DR)

| # | 기법 | 판정 | 착수 시점 |
|---|------|------|-----------|
| 1 | 변동성 스케일 내부 라벨 (상단+시간 배리어만) | 즉시 가능, 소급 계산 가능 | 지금 |
| 2 | Purged 시계열 분할 + 터사일 IC 스크린 | 즉시 가능, <100줄 순수 Python | 지금 |
| 3 | 레짐 태깅 (BTC 200d MA + DVOL) — 기록만 | 즉시 가능, 기존 피드 재활용 | 지금 |
| 4 | Platt 보정 + Brier 추적 (섀도 모드) | n≈200부터 의미 | 2~4주 내 |
| 5 | 메타라벨링 2차 모델 (로지스틱 3~5피처) | n≈300~500 필요 | 대기 |
| 6 | Isotonic 보정, 가중치 최적화 | n≥1000 필요 | 장기 대기 |

핵심 원칙 세 가지:
1. **소표본에서는 가중치를 학습하지 말 것** — 균등가중이 회귀를 이긴다 (Dawes 1979, DeMiguel 2009).
2. **겹치는 결과 창(168h)에서는 순진한 교차검증이 반드시 과적합** — purge/embargo 필수 (López de Prado).
3. **보정(calibration)은 표시가 아니라 게이트에 쓴다** — 메시지 포맷 불변, 통과 여부만 좌우.

---

## Q1. 메타라벨링 (Lopez de Prado AFML ch.3)

### 구조가 우리 시스템과 정확히 일치한다
메타라벨링은 "1차 모델(방향 결정)이 낸 시그널이 맞을 확률"을 2차 모델이 예측하여 거짓 양성을 걸러내고 사이즈를 정하는 ML 레이어다 (Joubert, *Meta-Labeling: Theory and Framework*, Journal of Financial Data Science 2022년 여름호 4(3):31-44). 우리 봇의 구조 — **애널리스트 = 1차 모델(방향+진입가), grade/supply/position verdict = 2차 레이어** — 는 교과서적 메타라벨링 프레임과 그대로 대응된다. 즉 우리는 이미 "수동 메타라벨링"을 하고 있고, 문헌의 조언을 직수입할 수 있다.

- 원 논문: https://jfds.pm-research.com/content/4/3/31 (SSRN: https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4032018)
- 후속: *Meta-Labeling Architecture* (JFDS 4(4):10), *Ensemble Meta-Labeling* (JFDS 2022.12), 코드 저장소 https://github.com/hudson-and-thames/meta-labeling

### 최소 표본 수: 공식 수치는 없다, 그러나 하한은 계산할 수 있다
발표된 문헌에서 "메타라벨링 2차 모델의 최소 n"을 못박은 곳은 찾지 못했다 (실무 예시는 수백 건 규모 — 예: MQL5의 RSI 메타라벨링 사례는 훈련 790건 + 시험 576건, https://www.mql5.com/en/articles/22274). 대신 로지스틱 회귀의 **EPV(events per variable) 문헌**이 가장 좋은 대용 기준이다:
- 고전 규칙: 예측변수당 사건(소수 클래스) ≥10건. Vittinghoff & McCulloch (2007, https://academic.oup.com/aje/article-pdf/165/6/710/140367/kwk052.pdf)는 5~9 EPV도 종종 허용, van Smeden et al. (2016, https://link.springer.com/article/10.1186/s12874-016-0267-3)은 경직된 10 컷오프에 근거 없음을 지적하나, 계수 편향 제거에는 EPV ≥20을 권하는 연구도 있다.
- 우리 봇 대입: n≈100~300, TP1 적중률 40~60% 가정 시 "사건" ~50~150건. **피처 3개면 EPV ~17~50 (양호), 피처 5개 + n=100이면 EPV ~8~12 (경계선)**. 결론: **3피처는 n≥150부터 방어 가능, 5피처는 n≥250~500 필요**.
- Dawes(1979) 계열 연구: 소표본·다변수에서 회귀 가중치 학습은 균등가중보다 못하다 (adjusted R²>0.9인 극단적 경우 제외).
- QuantConnect 실무 토론 "Why Meta-Labeling Is Not a Silver Bullet"(https://www.quantconnect.com/forum/discussion/14706/): 2차 모델은 훈련 구간 조건에 과적합하기 쉽고, 2차 모델 확률이 0.4~0.6에 몰리면(분리 실패) 아무것도 더하지 못한다고 경고. purged K-fold/CPCV 없이는 누수로 성과가 부풀려진다.
- 실무 사다리 (종합): (a) n<150 — 구간별 경험 적중률 + Wilson 하한 게이트, (b) n≈150~500 — 3피처 로지스틱, (c) n>500 — 트리/앙상블 검토.

**n<500 함정 정리**: (a) 겹치는 라벨 간 누수 → purged CV 필수, (b) 클래스 불균형(TP1 적중률이 한쪽으로 쏠리면 2차 모델이 다수 클래스만 출력), (c) 적응형 피처(최근 성과 기반 피처)의 과적합, (d) 다중 시험 — 피처 조합을 여러 번 시도한 것 자체가 선택 편향 (Q5의 DSR 참조).

### 제3자 시그널 스트림에 대한 메타라벨링: 학술 공백
텔레그램/카피트레이딩 시그널 스트림에 메타라벨링을 정식 적용한 학술 문헌은 검색되지 않았다. 상업 카피 툴(TelegramSignalCopier, Copygram 등)이 "신뢰도 60% 이상만 실행" 류의 임계 필터를 쓰는 것이 확인되는 정도로, 일화적 수준이다. **우리의 판정 축적 데이터는 이 분야에서 드문 자산**이며, 그만큼 참고할 선례가 없으니 보수적으로 가야 한다.

### 실행 권고
- **지금**: 2차 모델 학습은 하지 않는다. 대신 기존 grade v4를 "수동 메타라벨"로 유지하고, 판정 데이터 축적에 집중.
- **n≈300~500 도달 시** (현재 속도로 1~2개월): 피처 3개 이하(예: grade 점수, 저자 Wilson LB, 레짐 상태)의 로지스틱 회귀를 순수 Python으로 구현(경사하강 ~50줄). 반드시 시간순 분할 + purge로 검증.

---

## Q2. 확률 보정과 베팅 사이징 (소표본)

### Platt vs Isotonic: 소표본에서는 Platt 일택
Niculescu-Mizil & Caruana (ICML 2005, *Predicting Good Probabilities With Supervised Learning*, https://www.cs.cornell.edu/~alexn/papers/calibration.icml05.crc.rev3.pdf)의 확립된 결과:
- **보정 세트 <1000건: Platt(시그모이드)이 isotonic보다 우수** — isotonic은 소표본에서 과적합.
- ≥1000건부터 isotonic이 동등 이상.
- 후속 문헌(예: 신용리스크 보정 연구 arXiv:1710.08901)도 "~2000건 미만이면 Platt" 가이드를 재확인.

scikit-learn 공식 문서도 이를 명문화: isotonic은 "보정 표본이 너무 적으면(≪1000) 과적합 경향이 있어 비권장"이며, 시그모이드(Platt)는 절편을 적합하므로 불균형 데이터에도 유리하다 (https://scikit-learn.org/stable/modules/calibration.html).

→ n≈100~300인 우리는 **Platt만 후보**. 더 단순한 대안으로 grade 점수 구간별 적중률 binning(3구간 + Wilson 하한)도 유효하며 이것이 현재 방식과 연속적이다.

### Wilson vs Jeffreys: 현행 유지가 정답
Brown, Cai & DasGupta (2001, *Interval Estimation for a Binomial Proportion*, Statistical Science 16(2):101-133, https://projecteuclid.org/journals/statistical-science/volume-16/issue-2/Interval-Estimation-for-a-Binomial-Proportion/10.1214/ss/1009213286.full)의 표준 권고:
- 소표본(n≤40): **Wilson 또는 Jeffreys** 권장 (Wald는 절대 금지). 대표본(n>40): Agresti-Coull. 둘의 커버리지는 근사 동등 — Jeffreys는 등꼬리성, Wilson은 커버리지가 강점.
- 저자별 트랙레코드처럼 저자당 판정 수가 한 자릿수~수십인 상황은 정확히 이 권고 범위. **현행 Wilson LB는 문헌상 최선의 선택이며 변경 불요.**
- 소표본 승률로 "행동"하는 업계 표준 패턴도 Wilson 하한 정렬/게이트다 — Evan Miller, *How Not to Sort by Average Rating* (https://www.evanmiller.org/how-not-to-sort-by-average-rating.html; Reddit 등 채택). 운 좋은 소표본을 자동으로 벌점하고 n이 커지면 원 승률로 수렴한다.

### 보정 확률 → 게이트 (메시지 불변)
Joubert & Meyer, *Meta-Labeling: Calibration and Position Sizing* (JFDS 2023 5(2):23, https://www.pm-research.com/content/iijjfds/5/2/23, 코드: https://github.com/hudson-and-thames/meta-labeling/blob/master/calibration_and_position_sizing/position_sizing_with_calibration.py):
- 핵심 결과: **고정 규칙 기반 사이징은 보정을 거치면 성과가 유의하게 개선**된다 (데이터로 함수를 추정하는 사이징은 보정 이득이 없음). → 우리처럼 규칙 기반 게이트를 쓰는 시스템일수록 보정의 이득이 크다.
- 실무 적용 형태: 보정 확률 p̂에 임계값을 걸어 **통과/차단만 결정** — 표시 내용은 그대로. 상업 시그널 카피 서비스들도 동일하게 "임계 필터" 형태로 운용한다.
- Kelly 관점: 추정 오차 때문에 실무는 예외 없이 fractional Kelly(반·1/3 Kelly)를 쓴다 (Wikipedia Kelly criterion; Harry Crane, https://harrycrane.substack.com/p/two-arguments-for-fractional-kelly). 우리는 사이징을 하지 않으므로 이 교훈은 "**추정 확률을 절대 액면대로 믿지 말고 임계값에 여유를 두라**"로 번역된다.

### 확률 임계 게이트의 선례
암호화폐 대상 동료심사 연구(MDPI Applied Sciences 15(20):11145, 2025, https://www.mdpi.com/2076-3417/15/20/11145)는 방향 예측과 실행을 분리해 **p>0.70일 때만 롱 실행**하는 신뢰 임계 구조를 사용 — 확률 밴드가 곧 내부 티어가 되고, 티어별 과거 승률만 관리한다. de Prado AFML ch.10의 베팅 사이징(z=(p−0.5)/√(p(1−p)), m=2·Φ(z)−1)도 "사이즈 이산화(discretization)"로 작은 확률 변화에 휘둘리지 않게 하라고 권고 (https://random-docs.readthedocs.io/en/latest/implementations/bet_sizing.html).

### 보정 품질 추적: Brier score
Brier = mean((p̂ − outcome)²). 순수 Python 한 줄. 월별 롤링 Brier로 grade→확률 매핑의 열화를 감시. 소표본에서 log-loss는 극단 확률에 과민하고, ECE/신뢰도 다이어그램은 binning이 필요해 불안정하므로 Brier가 안전하다. 단 Bradley et al. (2008, Weather and Forecasting, https://journals.ametsoc.org/view/journals/wefo/23/5/2007waf2007049_1.xml)에 따르면 수백 건 미만에서는 Brier 추정치 자체가 흔들리므로, **원값이 아니라 base-rate(항상 기저 적중률 예측) 대비 상대 성능**으로 읽어야 한다.

### 실행 권고
- **지금**: grade 점수 터사일별 TP1 적중률 + Wilson 하한을 내부 리포트에 축적 (이미 유사 기능 있음 → 확장만).
- **n≈200부터**: Platt 스케일링(2파라미터 시그모이드, 뉴턴법 ~30줄)으로 grade→p̂ 매핑, 섀도 모드로 Brier 추적. 게이트 임계값 변경은 관찰 후 별도 결정 (필터 완화 금지 원칙과 충돌하지 않게, 강화 방향만 검토).

---

## Q3. 트리플 배리어 라벨링 vs 고정 TP1/168h 판정

### 왜 고정 TP 판정이 피팅 타깃으로는 시끄러운가
문헌의 논지 (de Prado AFML ch.3; mlfinlab 문서 https://random-docs.readthedocs.io/en/latest/implementations/tb_meta_labeling.html; https://mlfinpy.readthedocs.io/en/latest/Labelling.html):
- 고정 %/애널리스트 임의 TP는 **코인별 변동성 차이를 무시**한다. 일변동 12%인 코인의 +8% TP는 동전던지기, 일변동 2%인 코인의 +8% TP는 대형 사건 — 같은 "TP1 적중"이 전혀 다른 난이도를 갖는다. 이 이질성이 grade 피팅 타깃의 노이즈가 된다.
- 표준 해법: 배리어를 시점별 변동성에 비례시킴. 공식 형태는 `S_t × (1 ± σ_t × Δ)`, σ_t는 일수익률의 EWMA 표준편차 또는 ATR (paperswithbacktest, https://paperswithbacktest.com/course/triple-barrier-method).
- 관례적 파라미터 (수렴하는 실무 값):
  - de Prado 정본(AFML Snippet 3.1 `getDailyVol`): 일수익률 EWMA 표준편차, **기본 span=100일**. mlfinlab 예시의 pt_sl 배수는 [1,1]~[1,2] (https://github.com/hudson-and-thames/mlfinlab/blob/master/mlfinlab/labeling/labeling.py).
  - 실무자 프리셋: σ 배수 **1.5~3.0 범위**가 통용 (보수형 TP 1.5σ, 대칭형 2.0σ, 공격형 3.0σ; 변동성 룩백은 10~20일 선호 — https://alm0stsurely.github.io/2026/03/05/triple-barrier-labeling/).
  - ATR 관례(리스크 관리 쪽에서 동일 사상): 스윙(2~10일 보유)은 **ATR(14~20) × 2~3배**가 표준, 2×ATR14가 범용 기본값 (LuxAlgo https://www.luxalgo.com/blog/5-atr-stop-loss-strategies-for-risk-control/).
  - 암호화폐 적용 연구(MDPI Mathematics 12(5):780, https://www.mdpi.com/2227-7390/12/5/780 등)도 변동성 스케일 배리어 + CUSUM 필터 조합을 표준으로 채택. "변동성 스케일 배리어는 초고변동 구간에서 인위적으로 높은 성공률이 잡히는 것을 막는다" (https://waylandz.com/quant-book-en/Triple-Barrier-Labeling-Method/).
  - 정확한 배수는 과학이 아니라 관례다 — 발표되는 범위가 1.5~3×σ라는 사실 자체가 결론.

### 우리 제약에 맞춘 변형: "상단 + 시간 배리어만" (SL 배리어 의도적 제외)
정식 트리플 배리어는 하단(손절) 배리어를 포함하지만, 본 시스템 정책상 SL 기반 요소는 도입하지 않는다. 다행히 목적(라벨 노이즈 감소)에는 하단 배리어가 필수가 아니다:
- **내부 연구용 이중 라벨**: 기존 TP1/168h 판정(사용자 노출)은 그대로 두고, 내부에만 `label_vol = (168h 내 최고가 ≥ 진입가 + k×ATR20)` 를 병기. k=1.0과 2.0 두 개를 저장해 민감도 확인.
- 이미 저장 중인 **MFE가 사실상 이 라벨의 원료** — MFE ≥ k×ATR20 비교만 하면 되므로 과거 판정분도 소급 계산 가능. Upbit 일봉으로 ATR20은 stdlib로 계산.
- 수직(시간) 배리어는 관심 지평과 일치시키는 것이 원칙이므로 기존 168h 유지.
- "이중 라벨링" 선례: 문헌에서 사용자 노출 라벨과 연구 라벨을 분리한 명시적 사례는 못 찾았으나, 메타라벨링 문헌 자체가 "1차 판정은 그대로 두고 2차 라벨을 추가"하는 구조여서 개념상 동일하다.

### 기대 효과
grade 피팅/IC 계산 시 타깃을 `label_vol`로 바꾸면 코인 간 변동성 이질성이 제거되어 **같은 n에서 신호 대 잡음이 개선**된다. IC 0.207이 진짜인지 검증하는 가장 싼 방법이기도 하다 (타깃을 바꿔도 IC 부호와 규모가 유지되면 강건성 증거).

---

## Q4. 레짐 조건화

### 근거: 단순 추세 필터의 실적은 확립되어 있다
- **Faber (2007/2013), *A Quantitative Approach to Tactical Asset Allocation*** (https://mebfaber.com/wp-content/uploads/2016/05/SSRN-id962461.pdf): 10개월 SMA(≈200일 MA) 상회 시만 보유하는 규칙으로 "주식 수익률 + 채권급 드로다운". 발표 후 18년 out-of-sample에서도 Sharpe ~0.68 유지 (Concretum Group 재검증, https://concretumgroup.substack.com/p/global-tactical-asset-allocation). 단순 MA 필터 중 가장 강한 OOS 실적.
- **암호화폐 자체에서의 추세추종 검증**: *A Decade of Evidence of Trend Following in Cryptocurrencies* (arXiv:2009.12155) — 앙상블 추세 모델이 CAGR 30%, Sharpe 1.58, 최대 드로다운 19% vs 패시브 BTC >80%. Concretum Group의 상위 20개 유동 코인 추세 앙상블도 Sharpe >1.5, 연 알파 ~10.8% vs BTC (비용 차감 후, https://concretumgroup.com/catching-crypto-trends-a-tactical-approach-for-bitcoin-and-altcoins/). 학술적 기반: Liu & Tsyvinski (RFS 2021) — 암호화폐 1~4주 시계열 모멘텀은 강하고 유의 (https://www.nber.org/system/files/working_papers/w24877/w24877.pdf).
- **알트코인의 BTC 베타와 상관 급등**: ETH/SOL의 90일 베타 ~0.83-0.85+ (Canary Capital, https://www.canary.capital/alt-beta-to-btc), 시스템 리스크 베타의 구조적 상승 (Economics Letters 2024, https://www.sciencedirect.com/science/article/pii/S0165176524002726). 결정적 수치: 2017-18 폭락기 암호화폐 간 평균 부분상관 **0.769 vs 평시 ~0.29** (arXiv:2405.05642) — BTC가 무너질 때 알트 간 분산효과가 소멸하며, BTC 하락추세에서의 알트 롱은 사실상 레버리지 BTC 베팅이다. → **BTC가 추세 아래일 때 알트 롱의 사전 승률이 구조적으로 낮다**는 방향성 근거는 충분. 다만 "BTC<200d MA에서 알트 롱 정밀도가 X%p 하락"을 계량한 공개 연구는 없어, 자체 데이터 검증이 필요하다.
- **과장 경계**: Zakamulin의 재검증 연구(https://papers.ssrn.com/abstract=2242795)는 MA 타이밍 문헌의 성과가 데이터마이닝 편향으로 부풀려져 있고, 현실 비용 반영 시 수익 우위는 크게 줄어든다고 보고 — 단 **드로다운 방어 효과는 대체로 살아남는다**. 우리 용도(정밀도 방어)와 부합.

### 실무에서 쓰이는 가장 단순한 레짐 정의
- 2-상태: **BTC 종가 > 200d MA = risk-on** (crypto에서 가장 통용되는 불/베어 필터; Look Into Bitcoin https://www.lookintobitcoin.com/charts/bitcoin-200-day-moving-average/).
- 보조 지표: Altcoin Season Index(상위 100 중 75%가 90일간 BTC 아웃퍼폼 시 알트시즌; CoinMarketCap https://coinmarketcap.com/charts/altcoin-season-index/ — 단 90일 후행이라 게이트용으론 느림), BTC 도미넌스 52-55% 하향 돌파. DVOL 수준 필터("DVOL<60 = 저변동")는 실무 관행이나 공표된 표준 임계값은 없음.
- 함정: (a) MA 근처 휩쏘 — 히스테리시스(예: 3일 연속 종가 확인) 필요, (b) 레짐 분할은 버킷당 n을 더 쪼갠다 — n≈170을 2-상태로 나누면 버킷당 ~85건, 3-상태는 무리.

### 실행 권고
- **지금**: 각 시그널에 레짐 스탬프 2개만 기록 — `btc_above_200dma`(bool, 3일 확인), `dvol_level`(값 그대로). BTC 200d MA는 Upbit BTC/KRW 일봉으로 stdlib 계산, DVOL은 이미 수집 중. **게이트/가중치 변경은 하지 않는다** (관찰 원칙 준수, 버킷당 n 부족).
- **n 충분 시(레짐별 ~100건)**: 레짐별 TP1 적중률 Wilson 구간이 분리되는지 확인 후, 분리가 확인될 때만 risk-off 레짐 소프트 감점(강화 방향)을 검토.

---

## Q5. 초소표본 피처 선별

### IC 기준선: 우리 수치의 해석
- Grinold & Kahn 계열 관례: **IC 0.05 = 양호, 0.10 = 매우 우수** (MSCI Barra 공식 문서가 명시 — https://app2.msci.com/products/analytics/aegis/PI_Converting_Scores_Into_Alphas.pdf; Micro Alphas https://microalphas.com/information-coefficient/). 실무 통설: 평균 IC >0.10이면 이미 강함, **>0.20은 희귀하며 의심부터 해야 하는 수준**. ICIR 0.5 이상 양호, 1.0+ 우수 (ml4trading https://ml4trading.io/primer/reading-the-information-coefficient-stability-icir-and-horizon-decay/).
- **통계적 바닥**: 상관계수 표준오차 ≈ 1/√n → n=100에서 95% 유의 바닥이 |IC|≈0.2, n=300에서 ≈0.11 (Bonett & Wright, Psychometrika, https://link.springer.com/article/10.1007/BF02294183). 즉 **IC 0.207은 n≈100대에서 유의성 경계선 그 자체**다. 게다가 겹치는 168h 창은 MA(h−1) 자기상관을 유발해 Newey-West 보정 후에도 t-통계를 부풀린다 (Hansen & Hodrick 1980; Boudoukh et al., *Long-Horizon Predictability: A Cautionary Tale*, https://www.tandfonline.com/doi/full/10.1080/0015198X.2018.1547056) — 유효 n은 명목 n보다 훨씬 작다.
- 결론: IC 0.207 / ICIR 1.6은 **in-sample 상한선으로만 취급**하고, purged 시간분할 위에서 재측정 전까지 믿지 않는다. (재측정 후에도 살아남으면 진짜 대단한 것.)

### 왜 순진한 CV가 우리 데이터에서 과적합하는가
168h 결과 창은 하루 ~15건 유입과 겹친다 — 같은 시장 국면을 공유하는 시그널 수십 건이 사실상 한 개의 독립 관측이다. 셔플 K-fold는 시험 구간의 "답"이 훈련 구간에 새어 들어간다 (Wikipedia Purged CV https://en.wikipedia.org/wiki/Purged_cross-validation; https://github.com/eslazarev/purged-cross-validation).
- **Purge**: 시험 fold의 라벨 형성 기간(±168h)과 겹치는 훈련 관측 제거.
- **Embargo**: 시험 fold 직후 추가 제외 구간 (지연 반응 누수 방지). 관례는 ~1% 관측이지만, 우리 라벨 창이 168h이므로 **embargo = 168h**로 두는 것이 자연스럽다.
- 순수 Python 구현: 시그널을 시간순 정렬 → 시간순 3~5분할 → 경계에서 구간 교차 검사로 제거. 50줄 이내 (참고 구현: https://github.com/eslazarev/purged-cross-validation).
- CPCV 주의: n≈100~300에서 그룹을 잘게 나누면 purge 후 fold가 텅 빈다. 쓰려면 N=6 그룹, k=2 시험 그룹(15조합) 정도가 한계.

### 다중 시험 보정
피처 10+개를 이리저리 시험한 것 자체가 선택 편향을 만든다 — Bailey & López de Prado, *The Deflated Sharpe Ratio* (2014, https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2460551): 잡음 시행 N개의 기대 최대 Sharpe는 √(2 ln N)로 자란다. Harvey, Liu & Zhu (RFS 2016, https://academic.oup.com/rfs/article/29/1/5/1843824)는 다중 시험 반영 시 신규 팩터의 유의 기준을 **t>3.0** (1.96 아님)으로 제시. DSR 공식 자체를 구현할 필요는 없고, 실무 규칙만 채용: **"시도한 피처 수를 기록하고(15개 피처 심사 = 15회 시험), 시간 분할 전 구간에서 IC 부호가 유지되는 피처만 생존, 점수 동결 후 신규 유입 데이터에서 생존해야 영구 편입"**.

### n≈100~300용 최소 절차 (권고)
1. **용량 예산 먼저**: one-in-ten rule (예측변수당 관측 10~20건, https://en.wikipedia.org/wiki/One_in_ten_rule) — 겹침으로 유효 n이 명목보다 작으므로 **점수 내 피처 3~5개가 상한**.
2. 피처별 단변량 Spearman rank-IC (타깃: Q3의 `label_vol` 및 ret_24h 병행) — stdlib 구현 가능. 이 n에서 IC 스크린은 "명백히 죽었거나 부호가 반대인 피처를 탈락"시키는 용도이지, 생존자 간 순위 매기기가 아니다.
3. 데실 대신 **터사일** 스프레드 + 3버킷 단조성 확인 (n<300에서 데실은 버킷당 10여 건으로 무의미; Alphalens도 기본 5분위).
4. 시간순 3~4분할(purge 적용)에서 **IC 부호 안정성** — ≥3/4 분할 동일 부호만 통과. 이는 Meinshausen & Bühlmann stability selection(선택 빈도 0.6~0.9 임계, https://arxiv.org/abs/0809.2932)의 순수 Python 유사물이다.
5. 생존 피처 간 pairwise Spearman — |ρ|>0.6~0.7 쌍은 부호 안정성 좋은 쪽만 채택하거나, **패밀리로 묶어 랭크 평균 후 그룹에 가중치 1개** 부여. 의심 클러스터: {CVD, 오더북, 청산}(플로우), {DXY, USDT.D}(리스크오프), {RSI/MA, 근접도}(가격위치), {팔로워, Wilson 실적}(저자).
6. **점수 동결 → 워크포워드**: 동결 후 새로 들어온 판정에서 생존해야 영구 편입. 같은 100~300건 위에서 재튜닝을 반복할수록 통과 기준은 올라가야 한다 (DSR 논리).

supply verdict의 8개 신호(CVD, 오더북, P/C, 청산, DXY, USDT.D, DVOL, FOMC, 해시리본)도 같은 절차의 심사 대상이다 — 전부가 점수 자격을 얻을 것으로 기대하면 안 된다.

---

## Q6. 앙상블/컨플루언스 점수 구조

### 소표본에서는 균등가중이 왕이다 (확립된 결과)
- **Dawes (1979), *The Robust Beauty of Improper Linear Models*** (https://philpapers.org/rec/DAWTRB): 변수 선택과 부호만 맞으면 균등(단위)가중이 최적화 가중과 대등하거나 우수. 소표본·다변수에서 회귀가 균등가중을 이기는 건 adjusted R²>0.9인 예외적 상황뿐 (Graefe 2013 재확인, https://statmodeling.stat.columbia.edu/wp-content/uploads/2013/08/Graefe-2013-Improving-forecasts-using-equally-weighted-predictors-JBR.pdf).
- **DeMiguel, Garlappi & Uppal (2009), RFS 22(5)** (https://academic.oup.com/rfs/article-abstract/22/5/1915/1592901): 14개 최적화 모델 중 어느 것도 1/N을 일관되게 못 이김; 25자산 최적화가 1/N을 이기려면 **~3000개월** 추정 창 필요. → **n≈170에서 가중치 최적화는 시도 자체가 손해.**

### 가산 점수 vs 곱셈 게이트 vs 거부권(veto)
문헌 종합으로 도출되는 구조 원칙 (소표본 강건성 순위: **가산 균등가중 > 소수의 구조적 게이트 > 곱셈 구조 > 피팅 가중치**):
- **등급이 있는 품질 신호(저자 실적, 근접도, 포지션)** → 가산 점수 유지 (현행 구조가 문헌과 부합). 가중치는 균등 또는 손으로 정한 작은 정수(1/2/3점), 데이터로 미세조정하지 않는다. Wainer (1976, *It Don't Make No Nevermind*)와 Robert Carver (*Systematic Trading*: 자체 실험에서 in-sample 최적화 가중치 SR 0.3 vs 수제 균등 계열 SR ~0.5)도 동일 결론.
- 연속 정보를 이진화하지 말 것 (Carver): 이진 룰은 신호 강도 기울기를 버린다. 연속 피처를 피팅된 컷오프로 hard-gate하는 것은 정보 손실 + 검증 불가능한 자유 파라미터 추가로 최악의 조합.
- **AND 게이트/곱셈 구조** (예: Minervini 트렌드 템플릿의 7~8개 전조건 충족식): 그 역할은 "유니버스/레짐 정의"이지 랭킹이 아니다 — 게이트는 자격을 정하고, 점수는 자격자 중 순위를 매긴다. 조건 수만큼 통과 표본이 기하급수로 줄어 소표본에서 평가 불능. 신규 도입 비권장.
- **Veto는 "조건이 기대수익을 이동시키는 게 아니라 통계적 레짐 자체를 바꾸는 경우"에만** 정당화된다. 대표 사례가 이벤트 리스크: FOMC 당일 SPY 레인지는 평일 대비 40-60% 넓고 초기 움직임의 ~65%가 당일 반전 (TradingPub https://thetradingpub.com/kane-shieh/the-hard-truth-about-trading-through-todays-fomc/; Benzinga 2026-06 https://www.benzinga.com/Opinion/26/06/53097073/) — 자체 데이터로 증명할 필요 없이 외부 통계로 정당화 가능한 유형. 데이터 무결성(입력 누락/노후)도 veto 대상 — 나쁜 데이터 위 점수는 감점이 아니라 무효화가 맞다. 실용적 판별법: **임계값을 20% 옮겨도 여전히 veto하겠는가?** 구조적 veto("FOMC가 168h 창 안에 있다/없다")는 통과, 피팅된 veto("RSI>72면 컷")는 탈락 → 점수로 전환. (FOMC 시간창 veto는 필터 강화 방향이므로 금지 목록과 무충돌; 도입 여부는 별도 결정.)
- 상관 피처의 중복 계상: 가산 점수에 ρ=0.9인 피처 두 개는 독립 베팅 ~1.05개이지 2개가 아니다 (Grinold 법칙의 breadth 붕괴). → Q5-5의 pairwise 체크로 방지.

### 실행 권고
현행 "가산 grade + verdict 병기" 구조는 소표본 문헌의 권고와 이미 정합적이다. 바꿀 것은 구조가 아니라 **입장 자격 심사(Q5)와 타깃 정제(Q3)** 다.

---

## 구현 로드맵 (우선순위순)

### 1개월 내 (n≈100~300에서 유효)
1. **[1주차] 변동성 스케일 내부 라벨** — `label_vol_k1`, `label_vol_k2` (MFE ≥ k×ATR20, k=1,2; 168h 창). 저장된 MFE로 소급 계산. 순수 Python, Upbit 일봉만 필요.
2. **[1~2주차] Purged 시간분할 검증 하네스** — 시간순 3분할 + ±168h purge + 168h embargo. 이 위에서 grade v4 IC 0.207 재측정 (부풀림 여부 판정).
3. **[2~3주차] 피처 심사** — 10+개 신호 전수 단변량 rank-IC(터사일) + 부호 안정성 + pairwise 상관. 산출물: "점수 자격" 피처 3~5개 목록.
4. **[3~4주차] 레짐 스탬프** — `btc_above_200dma`(3일 히스테리시스), `dvol_level` 기록만. 게이트 변경 없음.
5. **[4주차~, n≥200] Platt 보정 섀도 모드** — grade→p̂, 월별 Brier 추적. 표시·게이트 불변.

### n≈300~500 도달 후 (현재 속도로 +1~2개월)
6. 메타라벨링 2차 모델: 심사 통과 피처 ≤3개 로지스틱 (순수 Python 경사하강). purged 분할 검증 필수.
7. 레짐별 적중률 Wilson 구간 분리 확인 → 분리 시에만 risk-off 소프트 감점 검토.

### n≥1000 (장기)
8. Isotonic 보정 전환 검토, 가중치 미세조정 검토. 그 전에는 균등가중 고수.

---

## 출처 목록
- Joubert, *Meta-Labeling: Theory and Framework*, JFDS 4(3), 2022 — https://jfds.pm-research.com/content/4/3/31
- Joubert & Meyer, *Meta-Labeling: Calibration and Position Sizing*, JFDS 5(2), 2023 — https://www.pm-research.com/content/iijjfds/5/2/23
- Hudson & Thames meta-labeling 코드 — https://github.com/hudson-and-thames/meta-labeling
- QuantConnect, *Why Meta-Labeling Is Not a Silver Bullet* — https://www.quantconnect.com/forum/discussion/14706/
- Niculescu-Mizil & Caruana, *Predicting Good Probabilities With Supervised Learning*, ICML 2005 — https://www.cs.cornell.edu/~alexn/papers/calibration.icml05.crc.rev3.pdf
- Brown, Cai & DasGupta, *Interval Estimation for a Binomial Proportion*, Stat. Sci. 16(2), 2001 — https://projecteuclid.org/journals/statistical-science/volume-16/issue-2/Interval-Estimation-for-a-Binomial-Proportion/10.1214/ss/1009213286.full
- Kelly criterion / fractional Kelly — https://en.wikipedia.org/wiki/Kelly_criterion ; https://harrycrane.substack.com/p/two-arguments-for-fractional-kelly
- Triple-barrier: paperswithbacktest — https://paperswithbacktest.com/course/triple-barrier-method ; mlfinlab 문서 — https://random-docs.readthedocs.io/en/latest/implementations/tb_meta_labeling.html ; mlfinpy — https://mlfinpy.readthedocs.io/en/latest/Labelling.html ; MDPI Mathematics 12(5):780 — https://www.mdpi.com/2227-7390/12/5/780
- Faber, *A Quantitative Approach to Tactical Asset Allocation* — https://mebfaber.com/wp-content/uploads/2016/05/SSRN-id962461.pdf ; OOS 재검증 — https://concretumgroup.substack.com/p/global-tactical-asset-allocation
- 알트 베타/상관: Canary Capital — https://www.canary.capital/alt-beta-to-btc ; Economics Letters 2024 — https://www.sciencedirect.com/science/article/pii/S0165176524002726 ; Coinbase Institutional — https://www.coinbase.com/institutional/research-insights/research/monthly-outlook/monthly-outlook-august-2024
- Altcoin Season Index — https://coinmarketcap.com/charts/altcoin-season-index/
- IC/ICIR 기준: Micro Alphas — https://microalphas.com/information-coefficient/ ; ml4trading — https://ml4trading.io/primer/reading-the-information-coefficient-stability-icir-and-horizon-decay/
- Purged CV — https://en.wikipedia.org/wiki/Purged_cross-validation ; https://github.com/eslazarev/purged-cross-validation
- Bailey & López de Prado, *The Deflated Sharpe Ratio*, 2014 — https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2460551
- Dawes, *The Robust Beauty of Improper Linear Models*, 1979 — https://philpapers.org/rec/DAWTRB ; Graefe 2013 — https://statmodeling.stat.columbia.edu/wp-content/uploads/2013/08/Graefe-2013-Improving-forecasts-using-equally-weighted-predictors-JBR.pdf
- DeMiguel, Garlappi & Uppal, *Optimal Versus Naive Diversification*, RFS 22(5), 2009 — https://academic.oup.com/rfs/article-abstract/22/5/1915/1592901
- FOMC 변동성: TradingPub — https://thetradingpub.com/kane-shieh/the-hard-truth-about-trading-through-todays-fomc/ ; Benzinga — https://www.benzinga.com/Opinion/26/06/53097073/
- EPV 규칙: Vittinghoff & McCulloch 2007 — https://academic.oup.com/aje/article-pdf/165/6/710/140367/kwk052.pdf ; van Smeden et al. 2016 — https://link.springer.com/article/10.1186/s12874-016-0267-3 ; One in ten rule — https://en.wikipedia.org/wiki/One_in_ten_rule
- sklearn 보정 문서 — https://scikit-learn.org/stable/modules/calibration.html
- Evan Miller, *How Not to Sort by Average Rating* — https://www.evanmiller.org/how-not-to-sort-by-average-rating.html
- MDPI Applied Sciences 15(20):11145 (신뢰 임계 게이트, 암호화폐) — https://www.mdpi.com/2076-3417/15/20/11145
- Bradley et al. 2008 (소표본 Brier 불확실성) — https://journals.ametsoc.org/view/journals/wefo/23/5/2007waf2007049_1.xml
- de Prado ch.10 베팅 사이징 (mlfinlab) — https://random-docs.readthedocs.io/en/latest/implementations/bet_sizing.html
- 암호화폐 추세추종: arXiv:2009.12155 — https://arxiv.org/pdf/2009.12155 ; Concretum — https://concretumgroup.com/catching-crypto-trends-a-tactical-approach-for-bitcoin-and-altcoins/ ; Liu & Tsyvinski — https://www.nber.org/system/files/working_papers/w24877/w24877.pdf
- Zakamulin, MA 타이밍 재검증 — https://papers.ssrn.com/abstract=2242795
- 폭락기 상관 급등 (0.769 vs 0.29) — https://arxiv.org/pdf/2405.05642
- Harvey, Liu & Zhu, RFS 2016 (t>3 기준) — https://academic.oup.com/rfs/article/29/1/5/1843824
- Hansen-Hodrick/겹침 편향: Boudoukh et al. — https://www.tandfonline.com/doi/full/10.1080/0015198X.2018.1547056
- 상관계수 표본 크기: Bonett & Wright — https://link.springer.com/article/10.1007/BF02294183
- Stability selection: Meinshausen & Bühlmann — https://arxiv.org/abs/0809.2932
- Wainer 1976, *It Don't Make No Nevermind* — https://www.semanticscholar.org/paper/fd405df188259fc8ef9abb3d254a8d48543dab16 ; Unit-weighted regression — https://en.wikipedia.org/wiki/Unit-weighted_regression
- Carver, *Systematic Trading* 노트 — https://the7circles.uk/systematic-trading-6-practice/
- Purged CV 참고 구현 — https://github.com/eslazarev/purged-cross-validation
- ATR 관례: LuxAlgo — https://www.luxalgo.com/blog/5-atr-stop-loss-strategies-for-risk-control/ ; 실무자 배리어 프리셋 — https://alm0stsurely.github.io/2026/03/05/triple-barrier-labeling/
- MQL5 메타라벨링 실례 (790/576건) — https://www.mql5.com/en/articles/22274
