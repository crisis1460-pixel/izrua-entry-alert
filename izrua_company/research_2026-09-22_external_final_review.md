# 해외 사례 기반 종합검토 — "지금이 최종 버전인가"

작성일: 2026-09-22 / 작성: 기획
조사 방식: 웹 리서치(WebSearch/WebFetch). 아래 각 항목의 출처 URL 명시. **직접 본문을 열어 확인한 것(F)** 과 **검색 결과 요약만 본 것(S)** 을 구분 표기했다. 추측은 `[추측]` 으로 명시.

---

## 3줄 요약

1. 해외의 시그널 추적·검증 서비스(Cornix, 3Commas 마켓플레이스, 트레이드 저널 SaaS)가 **최종형으로 수렴한 기능은 "실행 자동화"가 아니라 "집계 지표 3종(win rate·profit factor·expectancy/EV)과 채널별 비교표"** 다. 우리 봇은 이미 결과 판정 데이터를 갖고 있으므로 이 부분은 추가 비용 0으로 따라갈 수 있다.
2. 업계 공통 결론은 **win rate 단독은 무의미하고 EV(기대값)/profit factor/R-multiple 분포가 실제 판별 지표**이며, **MFE/MAE·edge ratio가 "진입 품질"과 "청산 품질"을 분리**해 준다는 것. 우리는 09-22부터 MFE/MAE가 정확해졌으므로, 이 축이 다음 고도화의 유일한 신규 정보원이다.
3. 반대로 **알림 규칙·점수 모델을 더 깎는 방향은 수확 체감 구간**이다. 등급 예측력 r≈−0.03, 종결 400건대 표본은 "채널 단위 비교"에는 겨우 쓸 수 있고 "세분 규칙 튜닝"에는 부족하다(업계 통용 최소 표본 100건/셀, 고신뢰 200~500건). 즉 **현 버전은 "기능적으로는 거의 최종형", 남은 여지는 리포트/집계 레이어와 표본 축적**이라는 게 사례들이 시사하는 바다.

---

## 1. 해외 시그널 추적/검증 서비스 사례

### 1-1. Cornix (텔레그램 시그널 자동화의 사실상 표준) — F
- **무엇을 하나**: 텔레그램/디스코드 채널의 시그널을 파싱해 거래소에 자동 주문, 이후 포지션 타임라인·체결 추적. 공식 기능 페이지가 광고하는 것은 "Tracking Performance(거래 타임라인/체결 추적)", "Portfolio Tracking(거래소 간 성과)", "Market Overview", "요약된 실시간 알림" 정도다.
- **주목할 점**: 공식 페이지에는 **win rate/ROI/Sharpe 같은 구체 지표명이 없다.** Signals Bot 도움말에도 **채널별 리더보드·평점·성과 이력 기능은 언급되지 않는다**(개인 계정의 결과 추적만). 즉 시장 1위 서비스도 "채널 품질 검증"은 사실상 비워 두고 있다.
- **우리 적용**: △ — 자동매매는 제외 대상. 단 "요약된 알림(summarized notifications)" 관점은 참고 가치.
- 출처: https://cornix.io/features/tracking-performance/ (F), https://help.cornix.io/en/articles/8800202-what-is-the-cornix-signals-bot (F)

### 1-2. cornix-trading-analysis (오픈소스, Cornix CSV → 리포트) — F
- **무엇을 하나**: Cornix가 내보낸 거래 CSV를 읽어 **win rate, profit factor, 평균 손익, 일/월 P&L 차트, 누적 손익, 승패 분포 히스토그램**을 계산하고 PDF 리포트로 출력. **채널을 골라서 채널별로 분석**하는 UI가 있다.
- **시사점**: 커뮤니티가 상용 서비스의 빈칸(채널별 성과 비교)을 오픈소스로 메우고 있다는 증거. 우리가 이미 SQLite에 갖고 있는 데이터로 동일한 산출물이 나온다.
- **우리 적용**: **O** — 주간 리포트에 채널별 win rate / profit factor / 평균 R 표를 넣는 것과 정확히 동형. 추가 비용 없음.
- 출처: https://github.com/delta2win/cornix-trading-analysis (F)

### 1-3. 3Commas Marketplace Signals — S
- **무엇을 하나**: 시그널 제공자 마켓플레이스. 성과 표기 지표는 **WPY/MPY/APY(주간·월간·연간 수익률)**. 핵심은 **제공자가 성과를 제출하지 않고, 해당 시그널을 구독한 봇들의 실제 체결 결과를 3Commas가 자동 집계**한다는 점(자기신고 배제).
- **시사점**: "성과 수치의 신뢰성 = 수집 주체가 제공자가 아닐 것". 우리 봇은 채널 글이 아니라 **Upbit 실가격으로 터치·TP/SL을 판정**하므로 구조적으로 이 조건을 이미 만족한다. 이게 우리 제품의 가장 큰 자산이다.
- **우리 적용**: O(개념), X(마켓플레이스 자체)
- 출처: https://help.3commas.io/en/articles/4728077-marketplace-signals-faq (S), https://help.3commas.io/en/articles/4728025-what-are-marketplace-signals (S), https://3commas.io/crypto-signals (S)

### 1-4. Zignaly — S
- **무엇을 하나**: 과거 카피트레이딩/시그널 마켓플레이스 → 현재는 **독립 카피트레이딩·시그널 마켓플레이스·DIY 봇을 접고 Z-Indexes와 profit-sharing 인프라로 축소**. 수익공유(카피어가 벌 때만 트레이더가 번다) 모델.
- **시사점**: 시그널 마켓플레이스라는 형태 자체가 지속되지 못하고 **"인센티브 정렬"쪽으로 수렴**했다. 시그널 품질 표기만으로는 사업이 안 된다는 방증.
- **우리 적용**: X (우리는 1인 사용자, 수익공유 무관)
- 출처: https://zignaly.com/crypto-copy-trading (S), https://www.daytrading.com/zignaly (S)

### 1-5. 시그널 검증 실무(제3자 검증) — F/S
- **무엇을 하나**: 포렉스권에서 확립된 관행은 **MyFxBook/FX Blue 같은 제3자 트래커에 read-only 계정 연결** → 제공자가 데이터를 통제할 수 없게 만드는 것. 스크린샷 증거는 무가치로 취급.
- 검증 가이드가 권하는 구체 방법: ① 승률 대신 **R:R로 재계산**(80% 승률이 적자, 40% 승률이 흑자일 수 있음), ② **체리피킹/레버리지 가정/모호한 기간** 을 레드플래그로, ③ **4~8주 페이퍼 트래킹으로 자체 검증**, ④ **직전 대폭락 구간 성과** 확인.
- 또한 "대부분 제공자의 광고 승률은 제3자 추적 성과보다 10~20%p 높다"는 주장(S, 업체 블로그 출처라 신뢰도 낮음 — `[참고용]`).
- **우리 적용**: **O** — 우리 봇의 SQLite 판정 데이터가 곧 "제3자 트래커" 역할. **채널이 주장하는 성과 vs 우리 실측 성과 괴리**를 주간 리포트에 넣는 것이 사례상 가장 정당한 기능.
- 출처: https://www.fatpigsignals.com/blog/how-to-verify-crypto-signal-claims-in-2026/ (F), https://www.myfxbook.com/reviews/closed-signal-providers/13,1 (S), https://targethit.com/blog/best-crypto-signal-provider-2026 (S)

### 1-6. 오픈소스 시그널 파서 — S
- joostmbakker/telegram-crypto-signal-parser: 다양한 포맷의 텔레그램 시그널을 **단일 JSON 스키마로 정규화**하는 것까지만 담당(성과 추적 없음). 우리 규칙 기반 파서와 동일 계층.
- **시사점**: 오픈소스 생태계에서도 "파싱"과 "성과 검증"은 분리돼 있고, **둘을 잇는 것(파싱 → 실가격 판정 → 채널 성적표)을 통합 제공하는 공개 사례는 찾지 못했다.** `[추측]` 우리 봇의 구조적 차별점이 여기일 가능성이 높다.
- 출처: https://github.com/joostmbakker/telegram-crypto-signal-parser (S)

---

## 2. 퀀트/실무 알림 설계 관행

### 2-1. Alert fatigue(알림 피로) — F/S
- **트레이딩 쪽 실무 가이드**: "알림 피로는 트레이더가 알림 시스템을 버리는 1번 이유". 50개 종목 × 5종 알림 = 오전 10시까지 30건 → 3~4번 무시하면 **정작 중요한 알림까지 무시하도록 스스로를 훈련**하게 된다. 권고는 **하루 5~10건, 종목 5~8개**로 시작하고, **10건/일 이상이면 임계값이 너무 민감한 것**.
- 추가 권고: **최소 시간 간격(인트라데이 15분 스페이싱)**, **유사 알림의 다이제스트 묶기(시간당 요약)**.
- **모니터링 업계(Datadog/BetterStack)**: 모든 알림은 **actionable** 해야 하고, 관련 알림은 **grouping/correlation으로 1건으로 묶으며**, **Alert-to-Action Ratio(알림 중 실제 행동으로 이어진 비율)** 를 품질 지표로 추적하라.
- **우리 적용**: **O(일부 이미 적용)** — 하루 ≤5건 목표는 사례가 권하는 밴드(5~10건)의 보수적 끝단으로 이미 정합. 신규 여지는 **Alert-to-Action Ratio**: 우리는 이미 **이모지 리액션(🏆👍👌👎)** 을 받고 있으므로, "보낸 알림 중 반응이 달린 비율 / 반응이 좋은 비율"을 주간 리포트 지표로 쓸 수 있다. 인라인 키보드 없이 기존 리액션만으로 가능 → 제외 목록 저촉 없음.
- 출처: https://pro.stockalarm.io/blog/day-trading-alerts-setup-guide (S), https://www.datadoghq.com/blog/best-practices-to-prevent-alert-fatigue/ (S), https://betterstack.com/community/guides/monitoring/best-practices-alert-fatigue/ (S), https://datadog.criticalcloud.ai/how-to-assess-datadog-alert-performance/ (S)

### 2-2. "no-trade" 필터 / 레짐 필터 — S
- **정의**: "regime filter는 전략이 **거래를 해도 되는지 여부를 결정하는 스위치**". 대표 형태가 "지수(또는 BTC)가 200일 이동평균 위일 때만 롱". 크립토에서도 MA200을 macro-regime filter로 써 구조적 강세장에서만 진입, 장기 약세장 노출을 줄이는 구현이 보고됨.
- **반론/주의**: 20종 트렌드 레짐 필터를 비교한 글에 따르면 "레짐 필터는 작동하긴 하나, 일부는 연 4%의 비용을 물리고, 하나는 6일 중 5일을 현금으로 보내며, 여러 개는 **단 한 종류의 재앙**만 막아준다". 즉 필터 선택이 성과의 대부분을 좌우.
- **우리 적용**: **O** — BTC 200일선 레짐이 이미 기록돼 있으므로 **추가 데이터 없이 "레짐별 성과 분해"를 먼저 계산**할 수 있다. 다만 사례의 경고대로 **곧장 게이트로 쓰지 말고 사후 분해(리포트)부터**가 순서. `[추측]` 표본 400건을 레짐 2구간으로 쪼개면 셀당 200건 내외라 "방향성 확인"은 되지만 "임계값 튜닝"은 아직 무리.
- 출처: https://tradingstrategy.ai/glossary/regime-filter (S), https://setup4alpha.substack.com/p/i-tested-20-trend-based-regime-filters (S), https://medium.com/@nayabbhutta/added-a-market-regime-filter-to-my-trading-strategy-heres-the-difference-828f6c4a7326 (S)

### 2-3. 진입 후 관리 알림 (안내만, 자동매매 아님)
- **(a) 타임 스톱 / N-bar exit** — S: "진입 후 정해진 바/기간 안에 충분한 진행이 없으면 예정대로 정리"라는 룰 기반 개념. 일부 주장으로 8~10일 타임 exit가 수익 증가·MDD 감소를 낳았다는 언급, DiNapoli의 "rule of three"(4H 기준 3봉/12시간 내 우호적 이동이 없으면 이탈) 인용. 흔한 실패는 **"거의 다 왔으니까" 하며 N을 늘리는 것**. (원문 페이지는 fetch 404, 검색 스니펫 기준 — `[신뢰도 중]`)
- 학술/구현 쪽에서는 **triple-barrier(TP/SL/시간 배리어)** 가 표준 백테스트 라벨링 방식으로 정착.
- **(b) TP1 도달 시 본전 이동 안내** — S: 3Commas·ProfitFarmers 등에 "Move to Breakeven(TP1 체결 시 SL을 진입가로 이동)"이 **기본 옵션으로 탑재**돼 있을 만큼 관행화. 트레이드오프는 "정상 노이즈에 털림".
- **우리 적용**: (a) **△** — 우리는 이미 타임아웃 판정(`timeboxed_win`)을 쓰고 있어 개념은 내장됨. 사용자에게 "X시간 무반응" 알림을 **추가로 보내는 것은 2-1의 알림 피로와 정면 충돌**하므로 신규 푸시보다 **아침 브리핑 한 줄에 합류**가 맞다. (b) **X — 제외 목록 저촉**(SL 관련 기능 제안 금지, 사용자는 SL 비중시).
- 출처: https://medium.com/@jpolec_72972/stop-loss-take-profit-triple-barrier-time-exit-advanced-strategies-for-backtesting-8b51836ec5a2 (S), https://nasdaqplaybook.substack.com/p/time-stops-n-bar-exits-when-price (S, 스니펫), https://help.3commas.io/en/articles/9464682-dca-bot-stop-loss-breakeven (S), https://www.profitfarmers.com/breakeven-stop-loss/ (S)

### 2-4. 트레이드 저널 자동화: MFE/MAE·edge ratio — F
- **정의(Tradervue, F)**: MFE = 거래 중 최대 평가이익(runup), MAE = 거래 중 최대 평가손실(drawdown). 포지션 크기와 무관한 **가격 기준 MFE/MAE** 도 별도 제공. 상세 리포트에 평균 MFE/MAE, 승·패일 리포트에 MFE/MAE 섹션.
- **분석 프레임(TradesViz, F)**: 8종 산점도(MFE/MAE × PnL·거래량·진입시각·청산시각). 해석 구역이 명확하다.
  - MFE vs PnL: **오른쪽 아래=효율적 포착(good)**, **오른쪽 위=테이블에 돈을 남김(watch)**, **왼쪽 위=크게 유리했다가 손실 마감(가장 비싼 습관, bad)**.
  - MAE vs PnL: 0 근처=good, 깊은 MAE의 승리 거래=운 좋은 승리(프로세스 리스크).
  - 구체 패턴 → 조치: **손절선 바로 바깥에 패자가 몰리면 스톱이 타이트**, **승자의 MFE 피크가 이르면 엣지의 유통기한이 짧다 → 타임 스톱/부분 익절 고려**.
  - 작업 순서: 패턴 발견 → **가설 1개** → 시뮬레이터/필터로 검증 → 적용 후 **월 단위 재측정**. 컷오프는 "규칙이 아니라 출발점"이라고 명시.
- **edge ratio(e-ratio)** — S: MFE/MAE 비율. **청산이 데이터를 왜곡하기 전에 셋업의 구조적 우위를 재는 몇 안 되는 지표**. >1.0이면 평균적으로 유리 방향으로 더 간다는 뜻, 2.0이면 유리 폭이 불리 폭의 2배. 핵심 해석: **e-ratio는 높은데 실적이 평범하면 "엣지는 진짜, 문제는 청산"**.
- **우리 적용**: **O(최우선)** — 09-22부터 MFE/MAE가 정확해졌고, 등급 점수는 예측력이 없음이 실측된 상태다. **e-ratio는 "채널/셋업 품질"을 결과 판정과 독립적으로 재는 새 축**이므로, 기존 등급 체계가 못 한 일을 할 가능성이 있는 유일한 후보. 기존 데이터로 계산 가능, GitHub Actions에서 집계만 하면 됨.
- 출처: https://help.tradervue.com/article/3440-mfe-and-mae-calculations (F), https://www.tradesviz.com/blog/mfe-mae-charts/ (F), https://journalplus.co/metrics/edge-ratio/ (S, 원문 리다이렉트 실패), https://trademetria.com/blog/understanding-mae-and-mfe-metrics-a-guide-for-traders/ (S)

---

## 3. 개인 트레이더용 성과 리포트 모범사례

### 3-1. 저널 SaaS가 공통으로 싣는 지표 — S
- TradeZella 대시보드: **win rate, profit factor, expectancy, max drawdown, equity curve, 캘린더 P&L**, R-multiple/틱/핍 등 7+ 뷰.
- Edgewonk: **모든 거래를 R-multiple로 측정**, 수학적 엣지 계산, **결과와 분리된 실행 품질(execution quality) 점수**, 50+ 사전 통계 + MAE/MFE, **Edge Finder가 주간 단위로 강점/약점/개선점 도출**.
- TradesViz: 600+ 통계(win rate, expectancy, drawdown, MFE/MAE, equity curve).
- 정의: **Profit Factor = 총이익/총손실, 1.5 이상이면 건강, 1.0 미만이면 적자**. **Expectancy = (승률 × 평균 R 이익) − (패율 × 평균 R 손실)**.

### 3-2. 주간 리뷰 프레임 — S
- 30분 주간 리뷰 6단계: **① 주간 개요 → ② 베이스라인 대비 지표 → ③ 승/패 정렬 → ④ 패턴 이탈 식별 → ⑤ 관찰 3개 → ⑥ 전략 조정 1개**. "**주당 조정 1개가 최적 속도. 더 많으면 혼란, 0이면 수집한 데이터 낭비.**"
- 추적할 핵심 5개: **win rate, 평균 승/평균 패(R:R), profit factor, 거래 수, 주간 최대 낙폭**.
- "**패턴은 30건 이상에서 찾아라. 작은 표본은 무의미.**"
- **우리 적용**: **O** — 주간 리포트를 "지표 나열"에서 **"베이스라인 대비 변화 + 관찰 3개 + 조정 1개"** 포맷으로 바꾸는 것은 구현 난이도 소, 비용 0. 우리 데이터로 win rate·평균 R·profit factor·expectancy 모두 산출 가능(TP/SL/타임아웃 판정이 있으므로 R 환산 가능).
- 출처: https://www.tradezella.com/blog/weekly-trade-review-process (S), https://tradeciety.com/all-edgewonks-metrics-and-statistics-explained-for-successful-journaling (S), https://journalplus.co/learn/guides/weekly-monthly-review-guide/ (S), https://journalplus.co/learn/guides/trading-journal-metrics-guide/ (S), https://www.tradesviz.com/brokers/Edgewonk (S)

### 3-3. 채널별 리더보드 예시
- 상용 사례에서 **자기신고 없이 자동 집계된 채널 리더보드**를 공개적으로 제공하는 곳은 3Commas(WPY/MPY/APY)가 사실상 유일하고, Cornix는 제공하지 않는다(1-1, 1-3). 오픈소스는 cornix-trading-analysis가 채널 선택형 분석을 제공(1-2).
- `[추측]` 우리 리더보드 설계에 바로 쓸 수 있는 컬럼: **채널 / 터치 건수 N / 종결 N / win rate / 평균 R / profit factor / e-ratio / 중앙 지연시간**. 표본 미달 채널은 회색 처리(§5 참조).

---

## 4. 오픈소스 크립토 봇의 "성숙 단계" 기능

### 4-1. freqtrade — F
- **텔레그램 리포트 명령**: `/daily`(기본 7일), `/weekly`(기본 8주), `/monthly`(기본 6개월), `/profit`(ROI·거래수·승률 종합), `/performance`(**페어별 그룹 성과**), `/stats`(**exit reason별 승/패 + 평균 보유시간**).
- **알림 소음 제어**: `notification_settings`가 **on / silent(무음 발송) / off(발송 안 함)** 3단계. 이벤트 종류별로 개별 설정. `fill` 알림은 기본 꺼짐.
- **백테스트 리포트 지표**: 페어별·exit reason별 breakdown(거래수, 평균 수익%, 총수익, 평균 보유시간, Win/Draw/Loss), 요약 테이블에 **CAGR%, Sharpe, Sortino, Calmar, SQN, Profit factor, Expectancy ratio, 평균수익 p-value, 최대 낙폭(기간·시작/종료 타임스탬프), 최대 연속 승/패, 승자 대 패자의 최소/최대/평균 보유시간, 거절된 진입 신호 수**, 그리고 **일/주/월/연 breakdown**.
- **우리 적용**: **O** — 우리에게 특히 이식 가치가 큰 3가지:
  1. **`/stats` 형태의 "판정 사유별(hit / tp_partial / timeboxed_win / fail) 집계 + 평균 보유시간"** — 우리 판정 체계와 1:1 대응.
  2. **`/performance` 형태의 "채널별 그룹 성과"** — §3-3 리더보드와 동일.
  3. **연속 승/패, 승자 vs 패자 보유시간 대비** — "빠른 터치=추격 위험"이라는 우리 실측과 직접 맞물리는 지표.
  4. **`silent` 등급** — 텔레그램은 `disable_notification` 을 지원하므로, **C등급은 무음 발송**처럼 "끄거나 켜거나" 사이의 중간 옵션이 가능. 알림 건수를 늘리지 않고 정보량을 늘리는 유일한 방법. 비용 0.
- 출처: https://www.freqtrade.io/en/stable/telegram-usage/ (F), https://www.freqtrade.io/en/stable/backtesting/ (F), https://github.com/freqtrade/freqtrade (S)

### 4-2. jesse / hummingbot — S
- jesse: 텔레그램/슬랙/디스코드 드라이버가 live plugin에 기본 탑재, 실시간 로그·알림 + 전략 검증용 상세 성과지표, 결과를 지표 기준으로 필터·정렬.
- hummingbot: **정기 리포트 스케줄 발송(email/Telegram/Slack/Discord)** + 주문 체결·에러·잔고 등 이벤트 알림 + 텔레그램에서 상태 조회/성과 리포트 명령.
- **시사점**: 성숙한 봇의 알림 레이어는 공통적으로 **(a) 이벤트 알림 / (b) 정기 스케줄 리포트 / (c) 온디맨드 조회 명령** 3층 구조다. 우리는 (a) 진입 알림, (b) 아침 브리핑·주간 리포트까지 갖췄고 **(c) 온디맨드 조회 명령이 비어 있다.**
- **우리 적용**: **△** — (c)는 봇이 명령을 수신해야 해서 4분 cron 구조상 응답 지연이 크다. `[추측]` 대신 "주간 리포트에 딥링크 없이 텍스트 표를 더 싣는" 쪽이 비용 대비 낫다.
- 출처: https://docs.jesse.trade/docs/notifications.html (S), https://hummingbot.org/reporting/ (S), https://github.com/jesse-ai/jesse (S)

### 4-3. TradingView 알림 생태계 — S
- 핵심 관행은 **alert frequency를 `once_per_bar_close`로 두는 것**. 이유: 실시간 봉 중간에는 high/low/close 기반 계산이 변동하므로 **봉 마감에 트리거하지 않으면 "리페인팅"(마감 시엔 성립하지 않았을 조건으로 알림이 나가는 것)** 이 발생. 진입 알림에는 `once per bar close`가 권장 기본값이며 백테스트(봉 마감 평가)와도 정합.
- **우리 적용**: **△/O(점검 가치)** — 우리 "터치" 판정은 4분 cron의 현재가 기준이라, 개념적으로 **실시간 봉 중간 트리거와 동일한 리스크**가 있다(꼬리 한 번 스치고 되돌아온 터치). `[추측]` 이것이 "빠른 터치 = 추격 위험"이라는 실측 신호의 물리적 원인 중 하나일 수 있다. **즉시 규칙을 바꾸기보다, 기존 MFE/MAE로 "터치 직후 되돌림 폭" 분포를 먼저 재 보는 분석이 선행**이어야 한다.
- 출처: https://www.tradingview.com/pine-script-docs/concepts/alerts/ (S), https://www.tradingview.com/pine-script-docs/faq/alerts/ (S), https://blog.pickmytrade.trade/tradingview-repainting-indicator-reliability-signal-accuracy/ (S)

---

## 5. 한계 인식 — "더 고도화해도 의미 없는" 지점

### 5-1. 표본 크기의 벽 — S
- 업계 통용 기준: **최소 100건**이 "엣지가 드러날 최소 표본", **60건은 대략적 방향성**, **200건+ 고신뢰**, **500건 강한 유의성**. 20건에서 13승(65%)조차 p>0.2로 노이즈와 구분 불가.
- **우리 현황 대입**: 종결 400건+ → **전체 수준의 win rate·profit factor는 신뢰 구간 안**. 그러나 **채널 × 등급 × 레짐으로 3중 분할하면 셀당 수십 건**이 되어 **어떤 튜닝도 통계적으로 정당화되지 않는다.** 등급 예측력 r≈−0.03은 "등급이 무용"이라기보다 "이 표본에서는 구분 불가"에 가깝다. `[추측]` 여기서 파라미터를 더 만지면 곧바로 커브피팅이다.
- **행동 함의**: **새 규칙을 추가하는 대신, 축적 속도를 유지하며 "분해 리포트"만 늘리는 것**이 현 단계의 최적 전략.
- 출처: https://www.edgeflo.com/blog/sample-size-trading (S), https://www.edgeflo.com/blog/hundred-trade-sample-size (S), https://medium.com/@trading.dude/how-many-trades-are-enough-a-guide-to-statistical-significance-in-backtesting-093c2eac6f05 (S), https://traderssecondbrain.com/guides/do-you-have-trading-edge (S)

### 5-2. 과최적화 — S
- 공통 진술: "**과거 데이터에 완벽히 맞을 때까지 규칙을 튜닝하면 무작위 잡음까지 학습**하며, 화려한 백테스트가 실거래 수익으로 이어지지 않는다." "충분히만(only 'enough') 최적화하라."
- arXiv 계열 연구들도 **out-of-sample 성과의 심각한 열화**, 하이퍼파라미터·모델 선택 자체가 in-sample에 과적합됨을 보고.
- 출처: https://papertoprofit.substack.com/p/testing-your-strategy-for-robustness-498 (S), https://arxiv.org/pdf/1905.05023 (S), https://arxiv.org/pdf/2003.13360 (S), https://arxiv.org/pdf/1408.1159 (S)

### 5-3. 시그널 카피 자체의 구조적 한계 — S
- **서바이버십 편향**: "크게 성장한 채널은 좋은 구간을 한 번 맞힌 채널이고, 터진 채널은 조용히 사라진다 → **가입 이전에 이미 편향된 표본에서 고르고 있다.**"
- **전이 불가능성**: "콜러가 글을 멈추거나 그룹이 닫히면 엣지가 통째로 사라지고, 배운 것도 남지 않는다."
- **인간 편향**: 악의가 없어도 콜러는 최근성 편향·연승 후 과신·손실 회피에 노출되며, 이는 팔로워에게 보이지 않는다.
- **정보 지연**: 콜이 그룹에 도달할 때쯤엔 스마트머니가 이미 움직인 뒤인 경우가 많다.
- (출처 페이지가 재접속 시 404 — **검색 스니펫 기준, `[신뢰도 중]`**)
- **우리 적용**: 이 4가지 중 **서바이버십 편향과 정보 지연은 우리 데이터로 직접 계측 가능한 유일한 사례**다. 전자는 "채널 수명/활동 중단 추적", 후자는 이미 있는 **"수집→터치 지연"** 지표. **실측으로 유일하게 검증된 신호가 지연이라는 사실은, 해외 실무자들이 지목한 구조적 한계와 정확히 일치한다.** 이건 우연이 아니라 확증으로 봐야 한다.
- 출처: https://chartmath.com/blog/telegram-trading-signals-vs-backtested-screener (S, 스니펫), https://en.wikipedia.org/wiki/Survivorship_bias (S)

### 5-4. 종합 판단
- 사례들을 합치면, **이 유형의 봇이 도달하는 "최종형"은 (1) 자동·비자기신고 성과 집계, (2) 채널별 비교표, (3) 정기 리포트 3층 구조(이벤트/스케줄/조회), (4) 알림 상한** 정도다. 우리는 (1)(2 일부)(3의 2/3)(4)를 이미 갖췄다.
- `[추측]` **남은 진짜 여지는 두 가지뿐**이다: ① **MFE/MAE·e-ratio 기반 "청산 품질" 축**(09-22부터 데이터 확보 — 신규 정보), ② **리포트 포맷의 의사결정 지향화**(베이스라인 대비 + 관찰 3 + 조정 1). 그 외의 점수 모델 재튜닝은 §5-1/5-2에 따라 **표본이 2배가 되기 전까지는 기대 이득이 음수**로 본다.

---

## 적용 후보 목록

| # | 후보 | 근거 사례 | 기존 데이터로 검증 가능 | 구현 난이도 | 제외 목록 저촉 |
|---|------|-----------|------------------------|-------------|----------------|
| 1 | **e-ratio(MFE/MAE) 채널·셋업별 집계** — 진입 품질과 청산 품질 분리 | TradesViz, Tradervue, JournalPlus | △ (09-22 이후 정확분만, 표본 축적 필요) | 소~중 | 없음 |
| 2 | **채널별 리더보드**(N / win rate / 평균 R / profit factor / e-ratio / 중앙 지연) | 3Commas WPY·MPY·APY, cornix-trading-analysis, freqtrade `/performance` | **O** | 소 | 없음 |
| 3 | **주간 리포트 포맷 개편**: 베이스라인 대비 지표 → 관찰 3개 → 조정 1개 | TradeZella 30분 주간 리뷰, Edgewonk Edge Finder | **O** | 소 | 없음 |
| 4 | **판정 사유별 집계 + 평균 보유시간**(hit/tp_partial/timeboxed_win/fail) | freqtrade `/stats` | **O** | 소 | 없음 |
| 5 | **Alert-to-Action Ratio** — 기존 이모지 리액션(🏆👍👌👎)으로 알림 품질 측정 | Datadog/BetterStack 알림 품질 지표 | **O** (리액션 데이터 보유) | 소 | 없음(인라인 키보드 아님) |
| 6 | **표본 부족 표시** — 셀 N<100이면 회색/괄호 처리, 수치 단정 금지 | 100-trade rule, 30건 패턴 규칙 | **O** | 소 | 없음 |
| 7 | **BTC 200일선 레짐별 성과 분해**(게이트 아님, 리포트만) | regime filter 사례, 20종 필터 비교의 경고 | **O** (레짐 기록 보유) | 소 | 없음 |
| 8 | **터치 직후 되돌림 폭 분포 분석** — "빠른 터치=추격" 원인 규명 | TradingView `once_per_bar_close` 리페인팅 관행 | △ (MFE/MAE 정확분 필요) | 중 | 없음 |
| 9 | **채널 수명/활동 중단 추적** — 서바이버십 편향 계측 | 서바이버십 편향 논의 | △ (수집 로그로 근사) | 중 | 없음(신규 채널 추가 아님) |
| 10 | **C등급 무음 발송**(disable_notification) — 건수 상한 유지하며 정보량 확대 | freqtrade `notification_settings: silent` | O (즉시 적용 가능) | 소 | 없음 |
| 11 | 진입 후 "X시간 무반응" 신규 푸시 | N-bar time stop, triple-barrier | △ | 중 | **저촉 위험** — 알림 건수 증가(§2-1). 브리핑 합류 권장 |
| 12 | TP1 도달 시 본전 이동 안내 | 3Commas breakeven, ProfitFarmers | O | 소 | **저촉 — SL 관련, 제안 금지** |
| 13 | 온디맨드 조회 명령(`/stats` 류) | jesse, hummingbot 3층 알림 구조 | O | 중~대 | 없음이나 4분 cron상 응답 지연 문제 |
| 14 | 제3자 성과 검증 서비스 연동 | MyFxBook/FX Blue | — | — | **저촉 — 외부 서비스 가입 금지** |
| 15 | 자동매매/시그널 마켓플레이스 | Cornix, 3Commas, Zignaly | — | — | **저촉 — 자동매매 금지** |

**권고 순서(기획 의견)**: 2 → 3 → 4 → 6 → 5 → 10 (전부 난이도 소, 비용 0, 기존 데이터만 사용) → 이후 표본이 쌓이면 1 → 7 → 8.
1·7·8은 "새 규칙 추가"가 아니라 "기존 데이터의 새 분해"이므로 §5-2의 과최적화 경고에 걸리지 않는다. **점수 모델 파라미터 재튜닝은 이번 라운드에서 제외하는 것을 권고한다.**

---

## 출처 일람

**F = 본문 직접 확인 / S = 검색 결과 요약만**

1. https://cornix.io/features/tracking-performance/ (F)
2. https://help.cornix.io/en/articles/8800202-what-is-the-cornix-signals-bot (F)
3. https://github.com/delta2win/cornix-trading-analysis (F)
4. https://www.freqtrade.io/en/stable/telegram-usage/ (F)
5. https://www.freqtrade.io/en/stable/backtesting/ (F)
6. https://www.tradesviz.com/blog/mfe-mae-charts/ (F)
7. https://help.tradervue.com/article/3440-mfe-and-mae-calculations (F)
8. https://www.fatpigsignals.com/blog/how-to-verify-crypto-signal-claims-in-2026/ (F)
9. https://help.3commas.io/en/articles/4728077-marketplace-signals-faq (S)
10. https://help.3commas.io/en/articles/4728025-what-are-marketplace-signals (S)
11. https://3commas.io/crypto-signals (S)
12. https://zignaly.com/crypto-copy-trading (S)
13. https://www.daytrading.com/zignaly (S)
14. https://targethit.com/blog/best-crypto-signal-provider-2026 (S)
15. https://github.com/joostmbakker/telegram-crypto-signal-parser (S)
16. https://pro.stockalarm.io/blog/day-trading-alerts-setup-guide (S)
17. https://www.datadoghq.com/blog/best-practices-to-prevent-alert-fatigue/ (S)
18. https://betterstack.com/community/guides/monitoring/best-practices-alert-fatigue/ (S)
19. https://datadog.criticalcloud.ai/how-to-assess-datadog-alert-performance/ (S)
20. https://tradingstrategy.ai/glossary/regime-filter (S)
21. https://setup4alpha.substack.com/p/i-tested-20-trend-based-regime-filters (S)
22. https://medium.com/@nayabbhutta/added-a-market-regime-filter-to-my-trading-strategy-heres-the-difference-828f6c4a7326 (S)
23. https://medium.com/@jpolec_72972/stop-loss-take-profit-triple-barrier-time-exit-advanced-strategies-for-backtesting-8b51836ec5a2 (S)
24. https://nasdaqplaybook.substack.com/p/time-stops-n-bar-exits-when-price (S, 스니펫 · 신뢰도 중)
25. https://help.3commas.io/en/articles/9464682-dca-bot-stop-loss-breakeven (S)
26. https://www.profitfarmers.com/breakeven-stop-loss/ (S)
27. https://journalplus.co/metrics/edge-ratio/ (S)
28. https://journalplus.co/learn/guides/weekly-monthly-review-guide/ (S)
29. https://journalplus.co/learn/guides/trading-journal-metrics-guide/ (S)
30. https://trademetria.com/blog/understanding-mae-and-mfe-metrics-a-guide-for-traders/ (S)
31. https://www.tradezella.com/blog/weekly-trade-review-process (S)
32. https://tradeciety.com/all-edgewonks-metrics-and-statistics-explained-for-successful-journaling (S)
33. https://www.tradesviz.com/brokers/Edgewonk (S)
34. https://docs.jesse.trade/docs/notifications.html (S)
35. https://hummingbot.org/reporting/ (S)
36. https://github.com/jesse-ai/jesse (S)
37. https://github.com/freqtrade/freqtrade (S)
38. https://www.tradingview.com/pine-script-docs/concepts/alerts/ (S)
39. https://www.tradingview.com/pine-script-docs/faq/alerts/ (S)
40. https://blog.pickmytrade.trade/tradingview-repainting-indicator-reliability-signal-accuracy/ (S)
41. https://www.edgeflo.com/blog/sample-size-trading (S)
42. https://www.edgeflo.com/blog/hundred-trade-sample-size (S)
43. https://medium.com/@trading.dude/how-many-trades-are-enough-a-guide-to-statistical-significance-in-backtesting-093c2eac6f05 (S)
44. https://traderssecondbrain.com/guides/do-you-have-trading-edge (S)
45. https://papertoprofit.substack.com/p/testing-your-strategy-for-robustness-498 (S)
46. https://arxiv.org/pdf/1905.05023 (S)
47. https://arxiv.org/pdf/2003.13360 (S)
48. https://arxiv.org/pdf/1408.1159 (S)
49. https://chartmath.com/blog/telegram-trading-signals-vs-backtested-screener (S, 스니펫 · 신뢰도 중)
50. https://en.wikipedia.org/wiki/Survivorship_bias (S)
51. https://www.myfxbook.com/reviews/closed-signal-providers/13,1 (S)
