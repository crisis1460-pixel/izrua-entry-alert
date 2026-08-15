# 알림 품질 리서치 — 2026-08-15

> 기획 리서치. 주제: **양식 동결 상태에서 알림 1건당 유용성·정밀도를 끌어올리는 방법** — 실전 서비스(Cornix/3Commas/Cryptohopper, Glassnode/Santiment, TradingView, 프랍데스크 관행)와 리딩방 반면교사 조사.
>
> **전제(불변)**: 알림 양식 동결(32칼럼·~25줄), 줄 추가 금지. SL 감점류 금지. 필터 완화 금지, 조이기만 허용. 유니버스 확대 금지(300 시험 관찰 중 ~08-22). 내부 축적 데이터 알림 노출 금지. 메시지 변경은 "같은 길이 이하의 더 날카로운 내용으로 교체" 또는 "별도 옵트인 메시지"만 허용.
>
> 증거 강도 표기: **[데이터]** 백테스트·연구·규제기관, **[관행]** 실무 컨센서스, **[문서]** 공식 제품 문서.

---

## Q1. 알림 타이밍 — 첫 터치 vs 확인 후 발송

### 조사 결과

- **[문서/관행]** TradingView 알림 설계에서 "Once Per Bar Close"(봉 마감 확인)가 비리페인팅 워크플로의 표준 기본값. 라이브 봉 중간 신호는 마감 전에 사라질 수 있고(가짜 신호), 웹훅 자동매매에선 봉 마감 트리거가 사실상 필수 취급. 단, 원 저자 해설도 명시하듯 "마감 대기 = 약한 신호 회피 + 수익 일부 포기"의 트레이드오프. ([TradingView 공식](https://www.tradingview.com/support/solutions/43000474415-differences-between-alert-frequencies/), [cyatophilum 해설](https://www.tradingview.com/chart/ICXBTC/0rX8ElSp-Clarifying-Once-per-bar-and-Once-per-bar-close-alert-option/), [Supa](https://supa.is/article/tradingview-alert-once-per-bar-vs-once-per-bar-close-explained-2026))
- **[데이터·블로그급]** 가짜 돌파 실패율은 타임프레임과 반비례: 1분봉 돌파의 ~68–72%가 실패 vs 일봉 ~40–45%(5봉 내 범위 복귀 기준). 낮은 TF일수록 첫 터치 알림의 노이즈 비중이 큼. ([For Traders](https://fortraders.com/blog/false-breakouts-why-they-happen-how-to-trade))
- **[데이터·블로그급]** ES/NQ 2026 상반기 백테스트: 리테스트 확인 진입은 **승률은 낮지만 기대값이 더 높음** — 확인은 "빈도·진입가를 내주고 기대값을 사는" 거래. ([동일 출처](https://fortraders.com/blog/false-breakouts-why-they-happen-how-to-trade)) 캔들 확인 일반론으로는 "가짜 신호 ~30% 감소, 즉발 큰 움직임 ~20% 놓침" 수준의 소프트 추정치. ([ChartMini](https://chartmini.com/blog/candlestick-patterns-that-actually-work-high-probability-trading-setups-2026))
- **[관행]** 확인의 표준 문법: **꼬리(wick)는 확인도 무효화도 아니고, 몸통 마감(body close)만 인정**. 지지 터치도 동일 — 몸통이 레벨 위를 지키면 셋업 생존. ([DailyPriceAction](https://dailypriceaction.com/blog/confirm-break-support-resistance/), [SMC 오더블록 규칙](https://grandalgo.com/blog/mitigation-vs-invalidation-order-blocks)) 볼륨 필터는 20봉 평균의 1.5~2배 미만이면 유동성 스윕 의심이 통용 규칙. ([FXNX](https://fxnx.com/en/blog/forex-breakout-trading-anti-fakeout-guide))
- **[중요 공백]** 동일 셋업에서 첫 터치 vs 리테스트 진입을 직접 A/B한 **공개 정면 비교 백테스트는 존재하지 않음**. 위 수치는 전부 블로그급. 업계 절충안은 "즉시 터치 예고 + 15m/1h 몸통 마감 유지 시 확인 후속" 2단 메시지 패턴.

### 우리 봇 적용

현재 터치 판정은 **캔들 저가 ≤ entry_high** 즉시 발동(꼬리 포함 첫 터치, 2분 폴링) — 업계 기준 "Once Per Bar"형. 공개 정면 비교가 없으므로 **우리 데이터로 직접 검증하는 것이 곧 업계 최전선**.

- **[권고 1-A, 내부 분석 먼저]** 코드 변경 없이 기존 축적분으로 검증: 터치 확정건의 `ret_4h`/`ret_12h`/`mfe_pct`/`mae_pct`를 "터치 직후 15m 봉이 진입가 위로 **몸통 마감**했는지"로 이분해 성과 차이 산출(터치 시각 + 업비트 15m 캔들 사후 조회, 일회성 `query_*.py`). 유의하면 그때 발송 옵션 논의.
- **[권고 1-B, 조건부]** 유의미할 때만 `alert_confirm_mode`(off/reclaim15m) 도입 — 발송만 늦추고 양식 불변, 발동률 감소라 "조이기". 전면 전환 전 2주 내부 A/B 필수. 문헌상 확인 대기는 승률↑·진입가 악화 트레이드오프가 명확하므로 데이터 없이 도입 금지.

---

## Q2. 진입 '점' vs '존', 유효기간 표현

### 조사 결과

- **[관행]** 수요/공급·SMC 계열 서비스는 존(구간) 제시가 표준. ATR 기반 존 폭은 **셋업 타임프레임 ATR의 ±0.25~0.5×**가 통용 시작점(스탑 버퍼는 0.5~1.0×). 상위 TF가 존을 정의하고 하위 TF ATR로 폭을 다듬는 멀티TF 방식도 통용. ([TradingView 지표 공식](https://www.tradingview.com/script/fTVJsjx5-Volatility-Adjusted-Supply-Demand-Zones-Footprint/), [Trading AI Blog](https://apptrading.ai/en/blog/atr-indicator-stop-loss-settings-that-actually-work/), [AlphaEx](https://www.alphaexcapital.com/stocks/technical-analysis-for-stock-trading/price-action-and-chart-patterns/support-and-resistance-zones))
- **[관행]** **무효화는 손절 지시와 별개 어휘로 표현 가능**: "X 아래 4H 종가 마감 시 셋업 무효"처럼 몸통 마감 기준 구조 서술이지 주문 지시가 아님. 꼬리 터치는 무효화가 아님. ([Elite CurrenSea](https://elitecurrensea.com/education/determine-confirmations-invalidation-levels-on-price-charts), [Grandalgo](https://grandalgo.com/blog/mitigation-vs-invalidation-order-blocks))
- **[문서]** **시간 TTL 필드를 공개한 메이저 플랫폼은 없음.** 실무 유효성 관리 4유형: ① **가격 이탈 허용치**(Cryptohopper "Percentage higher bid/lower ask" — 신호가 대비 X% 이상 튀면 스킵; 3Commas 가격 편차 필터), ② **미체결 주문 만료**(Zignaly Entry Order Expiration), ③ **알림 정의 자체 만료**(TradingView 기본 2개월), ④ **캔들 수 신선도**(가짜 돌파 문헌의 3~5봉 창). ([Cryptohopper 문서](https://docs.cryptohopper.com/docs/trading-bot/what-are-the-settings-for-a-signal), [3Commas](https://help.3commas.io/en/articles/8529406-signal-bot-custom-signal-type), [Zignaly](https://help.zignaly.com/hc/en-us/articles/360015780840-Position-Statuses), [TradingView](https://www.tradingview.com/support/solutions/43000688759-i-upgraded-to-the-premium-plan-but-my-alerts-still-expiring-in-2-months/))
- **[관행]** **존 신선도**: 첫(미소진) 터치가 최고 확률이고 재활용 존은 실패율이 유의하게 높음 → 존 알림은 1회성으로 운영해도 정당하며 메시지량 억제 효과 겸용. ([Grandalgo](https://grandalgo.com/blog/mitigation-vs-invalidation-order-blocks), [FalconAI](https://thefalconai.com/blog/what-are-order-blocks-smc-explained))
- **[관행]** 시그널 서비스 시간 만료 통념: 인트라데이 ~12h, 스윙은 더 김. 만료는 **진입 유효성**에만 적용. ([Signal Skyline](https://www.signalskyline.com/faq-page), [Forex.com](https://www.forex.com/en-us/news-and-analysis/forex-signals-explained-how-to-use-signals-in-your-strategy/))

### 우리 봇 적용

존(`entry_low`/`entry_high`, 클러스터 1% 밴드)·시간 만료(168h)·1회성 터치 처리 모두 이미 관행 정합. 남은 여지는 **신선도의 질**.

- **[권고 2-A, 내부 분석]** 주간 감사의 터치 소요시간 통계에 **아이디어 나이별 적중률**(수집→터치 0~24h / 24~72h / 72~168h)을 추가. 늦은 터치의 적중률이 유의하게 낮으면 `level_expiry_hours` 168→96~120 **조이기** 검토. "재활용 존 실패율↑" 문헌과 같은 방향.
- **[비권장]** 단일가 포스트를 ATR로 인위 확장하는 존 변환 — 원저자 의도 왜곡 + 터치 빈도 증가(사실상 완화). 양식 내 무효화 표기 추가도 SL 비표시 운영 방침과 충돌하므로 제안하지 않음(내부적으로는 이미 시간 만료가 무효화 역할).

---

## Q3. 적게, 그러나 좋게 — 알림 피로와 등급 티어링

### 조사 결과

- **[데이터]** 의료 알람 연구: 모니터 알람의 85~99%가 조치 불요, 일 ~100건 노출 시 습관적 무시(둔감화) 발생 — 알람 피로는 사망 사고 기여 요인으로 공식 경보(Joint Commission 2013). ([AHRQ](https://psnet.ahrq.gov/perspective/reducing-safety-hazards-monitor-alert-and-alarm-fatigue), [NCBI](https://www.ncbi.nlm.nih.gov/books/NBK555522/))
- **[데이터·사내]** **PagerDuty 운영 임계: 주 15건(≈일 2건) 초과 시 알림 정리 회의 소집** — 공개된 수치형 "알림 예산" 중 가장 구체적. 우리 글로벌 캡 15/일과 자릿수 차이. ([PagerDuty](https://www.pagerduty.com/blog/lets-talk-about-alert-fatigue/))
- **[데이터]** 푸시 알림 이탈 곡선: **주 2~5회 푸시에 사용자 46%가 알림 꺼버림**, 주 6~10회에 추가 32%. 리테일 허용치는 주 단위 한 자릿수. ([MobiLoud 통계](https://www.mobiloud.com/blog/push-notification-statistics), [GrabOn](https://grabon.com/blog/push-notification-statistics/))
- **[데이터·RCT]** 무작위 현장실험(n=237, 14일): 알림을 하루 3회 다이제스트로 묶으면 주의력·통제감 개선, 스트레스 감소 — 완전 차단은 오히려 불안·FoMO 증가. **"저가치는 묶고 고가치는 즉시"의 직접 근거.** ([Computers in Human Behavior](https://www.sciencedirect.com/science/article/abs/pii/S0747563219302596))
- **[관행]** SRE 고전(Rob Ewaschuk→Google SRE): 모든 긴급 알림은 긴급·중요·실행가능·실재해야 하며 "노이즈 알림은 지우는 쪽으로 오류하라". Santiment 아카데미: "매주 발동하는 조건은 알림 가치가 없는 일상 사건". Glassnode: 알림은 주의 비용을 줄이기 위한 고가치 임계값 전용. ([Ewaschuk](https://docs.google.com/document/d/199PqyG3UsyXlwieHaqbGiWVa8eMWi8zzAn0YfcApr8Q/mobilebasic), [Santiment Academy](https://academy.santiment.net/education-and-use-cases/alerts-on-sanbase/), [CryptoAdventure](https://cryptoadventure.com/glassnode-review-2026-on-chain-metrics-derivatives-data-alerts-and-pricing/))
- **[관행]** 유료 시그널 시장은 저빈도를 프리미엄으로 판매: 일 1~2건(Bitcoin Bullets, Binance Signals VIP), 주 2~3건(Wolf of Trading "low frequency is intentional"). 스윙 계열 상업 컨센서스는 **일 1~3건 이하**. ([altFINS](https://altfins.com/knowledge-base/10-best-crypto-telegram-signals-groups-in-2026-ranked-and-compared/), [NFTevening](https://nftevening.com/best-crypto-signals/))
- **[문서]** 텔레그램 `disable_notification=true` = 무음 발송(소리·진동 없음, 메시지는 남음) — 양식 변경 없이 티어링을 구현하는 표준 수단. ([python-telegram-bot 논의](https://github.com/python-telegram-bot/python-telegram-bot/issues/1729))

### 우리 봇 적용

글로벌 캡 15/일은 상업 서비스 기준 상한, 온콜 문헌 기준으로는 이미 과함. C등급 억제는 근거가 강함.

- **[권고 3-A, 저비용·즉시]** C등급 터치 알림 `disable_notification=true` 무음 발송. 내용·양식 완전 불변, 파라미터 1개, 즉시 롤백 가능 — "S/A는 크게, C는 조용히" 표준 티어링.
- **[권고 3-B, 다음 단계]** C등급 하루 1회 저녁 다이제스트(별도 메시지)는 배칭 RCT가 직접 지지하는 방향. 단 유니버스 300 관찰(~08-22) 종료 후, 3-A 운영 데이터 + 👍/👎로 C등급 개별 알림의 체감 가치 확인 뒤 결정.
- 참고: min_grade C→B 상향은 규칙상 가능하나 08-08 등급 타이트니스 분석과 중복 논의라 본 건에서는 무음화 우선.

---

## Q4. TP 후속 메시지의 품질

### 조사 결과

- **[관행·사실상 표준]** Cornix 자동 후속이 업계 템플릿(실채널 검증 원문): 원 신호에 **reply 스레딩**으로 `Take-Profit target 2 ✅ / Profit: 17.5% 📈 / Period: 3 Days 13 Hours ⏰` — **필드 3개뿐(타깃 번호·실현 %·경과 시간)**. 원본 신호는 절대 수정하지 않음. ([AltSignals 실채널](https://t.me/s/altsignals), [Cornix Help](https://help.cornix.io/en/articles/5814956-signal-posting), [Cornix 채널 연동](https://help.cornix.io/en/articles/5814966-how-to-add-the-cornix-trading-bot-to-your-telegram-channel-and-integrate-with-cornix))
- **[관행]** "TP1 도달 → 본절 이동"이 시그널 그룹 통용 후속 문구이고 3Commas는 이를 "Stop Loss Breakeven" 기능으로 제품화. ([3Commas](https://help.3commas.io/en/articles/9464682-dca-bot-stop-loss-breakeven)) — *우리 봇에는 운영자 스타일상 부적용(아래).*
- **[관행]** 트레일링(Chandelier: 22봉 최고가 − 3×ATR22, Le Beau) 갱신 고지는 **봉 마감 시 레벨 숫자만**, 인트라바 연속 갱신 금지, 원본 수정 금지. ([StratBase](https://stratbase.ai/en/blog/average-true-range-trailing-stop), [QuantifiedStrategies](https://www.quantifiedstrategies.com/chandelier-exit-strategy/))
- **[공백]** "모멘텀 정체" 후속의 자동화 관행은 발견되지 않음 — 프리미엄 방의 재량 코멘트 영역. 자동화 관행은 진입 체결·TP별 도달·스탑 이동·전 타깃 완료까지만.

### 우리 봇 적용

TP 다단계 인프라(`tp_alert_idx` 등)는 완성 — 로드맵 B-1 "프로덕션 발송 여부 확인"이 선행 과제.

- **[권고 4-A]** TP 후속은 Cornix 3필드 패턴 그대로: **원 알림에 `reply_to_message_id` 스레딩** + 1줄 고정("🎯 TP1 도달 +6.2% · 34h 경과"). 별도 메시지라 규칙 충족, `disable_notification=true` 권장(보유자만 관심). 사실만 통지 — 행동 지시("본절 이동" 등)는 배제(리딩 행위화 + 운영자 SL 비중시 스타일).
- **[보류 4-B]** Chandelier/ATR 기준선 고지 — 손절·이탈 안내 계열이라 운영자 스타일과 충돌, 제안 보류.
- **[비권장]** 모멘텀 정체 알림 — 업계 자동화 관행 부재 + 노이즈 위험.

---

## Q5. 👍/👎 피드백 데이터 활용

### 조사 결과

- **[데이터]** Netflix 별점→엄지 전환(2017): 이진 피드백이 평점 수량을 **200% 증가** — 명시 피드백의 최대 약점(희소성)을 수량으로 보완. 👍는 유사 항목 부스트, 👎는 노출 억제 입력이며 **한 표가 즉시 뭔가를 켜고 끄지 않음**(누적 신호). ([Variety](https://variety.com/2017/digital/news/netflix-thumbs-vs-stars-1202010492/), [Netflix 공식](http://about.netflix.com/en/news/goodbye-stars-hello-thumbs))
- **[데이터·정석]** 소표본 랭킹의 표준은 **Wilson 신뢰구간 하한**(Evan Miller; Reddit 'best' 정렬이 실전 채택) — 하한 방식 자체가 소표본을 보수 처리하므로 **경직된 최소표본 컷이 불필요**. 대안으로 IMDb식 베이지안 수축(전체 평균으로 끌어당김). ([Evan Miller](https://www.evanmiller.org/how-not-to-sort-by-average-rating.html), [Reddit 알고리즘 해설](https://medium.com/hacking-and-gonzo/how-reddit-ranking-algorithms-work-ef111e33d0d9), [IMDb FAQ](https://help.imdb.com/article/imdb/track-movies-tv/ratings-faq/G67Y87TFYYP6TWAV))
- **[관행]** 실행(조이기·뮤트) 결정은 버킷당 **n≥30**이 통계 관례; A/B 실무는 그 이상 요구. 그 전까지는 Wilson 점수로 "관찰"만. ([Science Insights](https://scienceinsights.org/why-is-30-the-minimum-sample-size-in-statistics/), [Invesp](https://www.invespcro.com/blog/calculating-sample-size-for-an-ab-test/))
- **[관행·주의]** 이진 엄지는 "신호가 나빴다"와 "내 취향/코인이 아니다"를 구분 못 함(Forbes의 Netflix 비판) → **전역 집계가 아니라 작성자·등급·판정조합 버킷별 집계**가 해독 가능. ([Forbes](https://www.forbes.com/sites/insertcoin/2017/06/26/netflixs-thumb-based-ratings-system-is-the-epitome-of-uselessness/))
- **[공백=기회]** 유저 엄지를 신호 큐레이션에 공개 반영하는 시그널 서비스는 발견되지 않음(Cryptohopper 마켓플레이스는 전략 단위 평점, Collective2는 성과 랭킹). **이 기능은 업계를 따라가는 게 아니라 앞서는 것.**

### 우리 봇 적용

작성자 실적 채점에 이미 Wilson 80% 하한(최소 5건)을 사용 중 — 같은 수학을 재사용.

- **[권고 5-A]** 집계 축: ① 작성자별, ② 등급별, ③ 수급/자리 판정 조합별 👍율(Wilson 하한 랭킹). `alert_feedback`×levels 조인으로 스키마 변경 없이 가능. 주간 감사 JSON에 1개 섹션 추가(내부 전용).
- **[권고 5-B]** 행동 임계: 버킷 10표 미만 판단 유보, **30표 전 자동 조치(뮤트·감점) 금지**. 피드백은 "알림이 유용했나"이지 "신호가 맞았나"가 아니므로 적중률과 별개 축 유지 — 교차(👍율 낮음 + 적중률 낮음)일 때만 조이기 근거로 사용.
- **[권고 5-C]** 내부 전용 유지(축적 데이터 노출 금지 규칙과 동일 취급).

---

## Q6. 실패 소통 — 내부 리뷰 파일 (텔레그램 발송 아님)

### 조사 결과

- **[관행·프랍 표준]** 손실건 기록 필드: 셋업 태그, (진입 **전** 기록된) 논지, 진입·종결 시각, R, **MAE/MFE**, 시장 국면, 오류 분류. MAE 분포가 핵심 진단: 손실건 MAE가 무효화 거리 근처에 몰리면 "깨끗한 손실"(논지 무효화, 진입은 정상), MFE가 크게 갔다 반전한 "반납형"이 많으면 관리·타이밍 문제. **셋업 태그당 ~40건은 쌓여야 통계 의미.** ([JournalPlus MAE/MFE](https://journalplus.co/learn/guides/mae-mfe-guide/), [Tradewink](https://www.tradewink.com/learn/trade-journal-mfe-mae-analysis-guide), [Funding Rock](https://www.fundingrock.com/blog/trade-journaling-for-prop-traders-metrics-to-track-templates-to-copy/))
- **[관행·SMB/프랍]** 리뷰는 세 축을 분리 채점: ① 아이디어 질(엣지가 실재했나) ② 실행 ③ 준비 상태. **결과와 의사결정 품질을 분리** — 좋은 판단의 손실은 '우수'로 채점 가능. Van Tharp: '실수' = 규칙 위반이지 손실이 아님. ([SMB Training](https://www.smbtraining.com/blog/how-to-conduct-a-professional-review-of-your-trading))
- **[관행]** 손실 3분류 정착: (a) **논지 오류**(분석이 틀림) (b) **실행/타이밍** (c) **국면 전환**(셋업 검증 시점과 시장이 달라짐). 한 실증 사례에서는 "논지는 맞았는데 진 손실"이 분석 오류 손실보다 많아짐 — 고칠 대상이 달라짐. ([WhyTradersLose](https://whytraderslose.com/), [ForexMechanics](https://forexmechanics.com/traders-workshop/trade-anatomy/))
- **[관행]** 유용한 주간 리뷰는 ~30분·고정 목록(R 분포, 규칙 위반, 최고/최악 실행, 다음 주 목표 1개)이면 충분. 구조적 질문은 월간으로. ([JournalPlus 주간 가이드](https://journalplus.co/learn/guides/weekly-monthly-review-guide/))
- **[관행]** 시그널 업계에서 손실 전건 공개(타임스탬프 포함)가 신뢰 1순위 기준; 삭제·비공개는 즉시 레드 플래그. 검증 대시보드 공개 업체가 리텐션 우위라는 업계 주장도 존재. ([One-Signal 레드플래그](https://www.one-signal.com/news-insights/how-to-spot-trading-signal-scams-7-red-flags-to-avoid), [FX Signals Desk](https://fxsignalsdesk.com/2025/09/14/honest-review-what-makes-a-reliable-forex-signal-provider/))

### 우리 봇 적용

주간 감사 덤프 + MAE/MFE·다구간 수익률·터치 소요시간이 이미 DB에 있어 **새 수집 없이 실패 섹션만 부착 가능**. 내부 파일 전용이라 기존 "주간 리포트 텔레그램 발송 거절" 결정과 충돌 없음.

- **[권고 6-A]** 주간 감사 JSON에 `misses` 섹션: TP1 미달 종결건마다 {심볼, 등급, 작성자, 터치→종결 시간, MAE/MFE, ret_4h/12h/24h, 터치 시점 수급/자리 판정, 실패 분류}. 분류는 3범주 자동 태깅: ① **즉시 역행형**(MFE < +1% — 진입 논리 약함) ② **반납형**(MFE ≥ TP1 거리의 70% 후 실패 — 지속성·타이밍) ③ **국면형**(동주 BTC −5% 이상 등 시장 동반 하락 — 신호 잘못 아님).
- **[권고 6-B]** 사람이 읽는 요약은 월 1회 `izrua_company/`에 5줄 내외(최다 실패 조합 1개 + 국면형 비율). 매주 산문 리포트는 과잉. 셋업당 40건 문턱 전에는 "경향 관찰"로만 소비.

---

## Q7. 리딩방·펌프방 안티패턴 점검

### 조사 결과

- **[데이터·규제]** 금감원 2025 하반기 소비자경보: SNS 광고→비공개 텔레그램방 유인, 주식리딩방·카피트레이딩·AI자동매매가 대표 불법 패턴. 무면허 종목·타이밍 지정은 무인가 투자자문업(형사처벌), 피해자는 금융분쟁조정 보호 밖. ([아시아경제 보도](https://www.asiae.co.kr/article/2025102910294506596), [법무법인 도모](https://domolaw.co.kr/legal-guide/bbb9f4c2-c391-41be-9d6d-257b6de568be), [CleanScanGuard](https://cleanscanguard.com/reading-room/))
- **[관행·수법 목록]** 무료방→VIP방 퍼널(초기 소액 승리 연출 후 고액 유료방), 바람잡이 가짜 수익 인증, 조작 HTS/앱, **이중 계좌 운영**(회원 절반 매수·절반 매도 지시로 항상 '승자' 제조), 사후 인증(후출), 패자 삭제 체리피킹, 모호한 존("눌림목 매수"), 무효화 부재, 목표가 사후 이동, 물타기 지시, "지금 당장" 압박. ([KB의 생각](https://kbthink.com/fraud/stock-tipping-scam.html), [법률사무소 번화](https://bh-law.kr/ko/news/column/coin-reading-room-scam-legal-guide), [나무위키 리딩방](https://namu.wiki/w/리딩방))
- **[데이터·학술]** USENIX Security 2019: 텔레그램 채널 ~100곳에서 412건 펌프 추적(일 평균 2건, 월 ~600만$ 인공 거래량) — 기제는 카운트다운 공지, 유료 등급별 선행 접근, 내부자 선탈출. Kamps & Kleinberg 2018(Crime Science)은 정의·탐지 기준 확립, WSJ 추산 6개월 8.25억$ 규모. ([USENIX](https://arxiv.org/pdf/1811.10109), [Crime Science](https://link.springer.com/article/10.1186/s40163-018-0093-5), [SEC 소셜미디어 사기 경보](https://www.sec.gov/resources-for-investors/investor-alerts-bulletins/social-media-investment-fraud-investor-alert))

### 우리 봇 가드 점검표

| 안티패턴 | 우리 봇 | 판정 |
|---|---|---|
| 사후 인증(후출) | 레벨이 터치 **이전에** DB 저장·타임스탬프, 발송은 터치 순간 | ✅ 구조적 차단 |
| 체리피킹 | 등급 무관 전건 저장, 주간 감사 전 결과 포함 | ✅ (향후 어떤 통계든 **발송건 전수** 기준 유지 필수) |
| 모호한 존 | entry/entry_low/high 수치 명시, 파싱 실패 시 드롭 | ✅ |
| 무효화 부재 | 시간 만료 168h 존재. 가격 무효화는 SL 비표시 방침상 미표기(내부 추적은 가능) | ⚠️ 의도된 공백 — 현행 유지 |
| 목표가 사후 이동 | upsert가 원 포스트 갱신 반영. **터치 후 TP 변경 이력 미추적** | ⚠️ **잔여 갭 1**: 터치 확정 시점 TP 스냅샷 고정(컬럼 1개) → 작성자의 사후 목표 수정 검증 가능, 실적 통계 무결성 보강 |
| 패자 삭제 | TradingView 원 포스트 삭제 감지 로직 존재 | ⚠️ **잔여 갭 2**: 작성자별 삭제율을 내부 실적 통계에 축적 → "지우는 작성자" 조이기 근거(내부 전용) |
| 긴급 압박 | urgency는 등급 데이터 기준, 문구 압박 없음 | ✅ |
| 등급별 정보 비대칭(무료→VIP) | 단일 채널 | ✅ 해당 없음 |
| 물타기 지시·허위 수익 인증 | 해당 행위 없음 | ✅ |

---

## 권고 우선순위 요약

| # | 권고 | 비용 | 규칙 적합성 |
|---|---|---|---|
| 1 | **3-A** C등급 무음 발송(`disable_notification`) | 파라미터 1개 | 양식 불변·조이기 |
| 2 | **6-A** 주간 감사 misses 섹션(3범주 자동 분류) | 기존 데이터 재활용 | 내부 파일 전용 |
| 3 | **5-A/B** 피드백 Wilson 버킷 집계 + 임계(10표 유보/30표 전 자동화 금지) | 조인 쿼리 | 내부 전용·업계 선행 |
| 4 | **1-A** 터치 후 15m 몸통 리클레임 × 성과 내부 검증 | 일회성 스크립트 | 분석만 |
| 5 | **4-A** TP 후속 = Cornix 3필드(타깃·%·경과) reply 스레딩 + 무음 (TP 인프라 가동 확인 선행) | 소규모 | 별도 메시지 |
| 6 | **2-A** 아이디어 나이별 적중률 → 만료 168h 단축 검토 | 감사 확장 | 조이기 |
| 7 | **갭 1·2** 터치 시점 TP 스냅샷 고정 + 작성자 삭제율 축적 | 컬럼 1~2개 | 내부 전용 |

**보류/비권장**: 4-B 트레일링 기준선 고지(운영자 스타일 충돌), 모멘텀 정체 알림(관행 부재), ATR 존 인위 확장(사실상 완화), 3-B C등급 다이제스트(08-22 이후 3-A 성과 보고 결정 — 배칭 RCT 근거는 강함), min_grade 상향(08-08 분석과 중복 논의).
