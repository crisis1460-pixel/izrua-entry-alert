# 파이프라인 정밀도 감사 — 신호 정밀도를 무디게 하는 지점 8곳

> 작성: 2026-08-15 (시니어 엔지니어 감사)
> 대상: monitor/price_check.py · collector/grading.py · collector/extractor.py · config/settings.py · monitor/upbit.py · monitor/binance.py
> 원칙: **조이기·데이터 축적·내부 전용 변경만 제안.** 필터 완화, SL 감점, 유니버스 확대, 알림 양식 변경, 내부 판정 노출은 전부 제안하지 않는다.

---

## 요약 순위표 (기대 정밀도 이득 ÷ 구현 비용)

| 순위 | 항목 | 성격 | 비용 | 기대 이득 |
|---|---|---|---|---|
| 1 | F1. 터치 시점 등급 미영속 — 필터가 쓴 점수가 DB에 없다 | 데이터 기록 | 극소 (컬럼 2개 + 1줄) | **높음** — 향후 모든 캘리브레이션의 라벨-결정 불일치 제거 |
| 2 | F3. TP1 단일 라벨 병행 트리플배리어 라벨 | 병행 라벨 (기존 라벨 불변) | 낮음 (기존 캔들 스캔 안에서 계산, API 0콜) | **높음** — 작성자 간 공정 비교 축 확보 |
| 3 | F2. 터치 품질(꼬리 vs 종가) 미기록 — 종가 자체가 유실됨 | 데이터 기록 | 중간 (캔들 튜플 확장) | **높음** — 스탑헌트성 터치 식별의 유일한 경로 |
| 4 | F6. 수급/자리 판정 단일 스냅샷 | 내부 재검증 기록 | 낮음 (시간당 사이클 재사용) | 중간 — 판정 안정성 실측 가능화 |
| 5 | F8. 게이트 순서 — dup_preview 뒤늦은 판정으로 매 회차 낭비 | 순서 재배치 | 극소 | 소 (정밀도 직접 이득은 없고 비용 절감) |
| 6 | F4. 고정 168h 만료 vs 타임프레임 | 데이터 기록 → 후속 조이기 | 낮음 | 중간 (단, tf 명시 글 비중이 낮아 커버리지 제한) |
| 7 | F7. 글 신선도(터치 시점 글 나이) 분석 | 분석 (데이터는 이미 존재) | 극소 | 소~중간 |
| 8 | F5. 고정 1% 클러스터 밴드 | 섀도 계산 + 기록 → 후속 결정 | 중간 | 불확실 — 데이터 먼저 |

---

## F1. 터치 시점 등급이 영속되지 않는다 (감사각 6)

**현상.** `price_check.py`는 터치/예고 순간 `regrade_current()`로 클러스터 전 멤버를 재채점해 그 점수로 대표 선정과 `alert_min_grade` 필터를 판정한다(2026-07-26 freeze 결함 수정, 671행). 그러나 재채점 결과는 메모리 dict에만 반영되고 DB의 `grade`/`score`는 수집 시점 값이 그대로 남는다("DB 원본 grade/score 는 보존한다" 주석). `mark_touched()`가 저장하는 것은 touch_price·환율·심리 스냅샷뿐, **터치 시점 등급은 없다.**

**정밀도 손실 메커니즘.** 알림 발송을 실제로 가른 변수(터치 시점 점수)와, 사후 캘리브레이션이 보는 변수(수집 시점 점수)가 다르다. 근접도 항목(최대 20점)은 두 시점 사이에 완전히 달라진다 — 수집 당시 D였다가 터치 순간 C가 되어 발송된 건이 터치 52건 중 18건(35%)이었다는 자체 실측이 이미 코드 주석에 있다. 이 상태로 "등급 vs 성과" 상관을 재면 v4 산식의 예측력을 체계적으로 오측정한다. 등급 관찰(~08-10 v4 관찰 항목)의 데이터 품질 문제이기도 하다.

**업계 관례.** ML 라벨링에서 결정 시점(point-in-time) 피처 스냅샷은 기본 원칙 — 룩어헤드/라벨 누수 방지의 표준이다(López de Prado, *Advances in Financial ML*; [트리플배리어 해설](https://quantstrategy.io/blog/the-triple-barrier-method-revolutionizing-how-we-label/)). "모델이 결정한 순간의 입력"을 저장하지 않으면 그 모델을 평가할 수 없다.

**최소 변경안.** `levels`에 `touch_grade TEXT, touch_score REAL` 추가(기존 마이그레이션 패턴 그대로). `mark_touched()` 호출부에서 재채점이 끝난 `_lv["grade"]/_lv["score"]`를 함께 전달, `IS NULL` 조건으로 최초 기록만. `grade_ver`는 이미 기록되므로 병기 불필요. 알림·필터·판정 로직 변화 0.

**리스크.** 사실상 없음. 소급 재라벨 금지(D4)와도 무충돌 — 기존 행은 NULL 유지.

---

## F2. 터치 품질(꼬리 vs 종가, 관통 깊이)이 기록 불가능한 구조 (감사각 3)

**현상.** 터치 = "1분봉 저가가 엔트리 이하"(소급 판정, `_eff_low`). 그런데 `fetch_range_since()`가 반환하는 튜플은 `(시작, 종료, 고가, 저가)` — **종가를 버린다**(업비트 응답에는 `trade_price`가 있는데 파싱에서 탈락). 1분 꼬리로 0.1% 스치고 즉시 복귀한 터치와, 종가로 뚫고 내려앉은 터치가 DB에서 완전히 동일하다.

**정밀도 손실 메커니즘.** 지정가 체결 모델(꼬리 터치 = 체결)은 그 자체로 방어 가능한 설계다 — 엔트리 지정가 매수는 꼬리에도 체결되고, TP 지정가 매도도 꼬리에 체결된다. 문제는 체결 모델이 아니라 **관측 손실**이다: 업계에서 "꼬리로 뚫고 종가로 복귀"는 유동성 스윕/스탑헌트의 고전적 시그니처로, 진성 이탈(종가 확정)과 행동이 다르다는 것이 관례적 구분이다([VT Markets](https://www.vtmarkets.com/en-ca/discover/liquidity-sweeps-stop-hunts-explained-for-cfd-trading/), [Alchemy Markets](https://alchemymarkets.com/education/strategies/liquidity-sweep/), [Quadcode 용어집](https://quadcode.com/glossary/what-is-liquidity-sweep-everything-you-need-to-know)). 지금 구조로는 "꼬리 터치의 hit률이 종가 터치와 다른가"라는 질문 자체를 영원히 못 던진다 — 어떤 작성자의 레벨이 상습적으로 스윕만 당하는지도 알 수 없다.

**업계 관례.** 유효 이탈 = 캔들 몸통이 레벨 너머에서 종가 확정, 스윕 = 꼬리 관통 후 1~3봉 내 재이탈 복귀 — 위 출처들의 공통 정의. 많은 시스템이 이를 진입 필터가 아니라 **터치 분류 데이터**로 먼저 쌓는다.

**최소 변경안 (필터 아님, 순수 기록).**
1. `fetch_range_since()` 튜플을 `(시작, 종료, 고가, 저가, 종가)`로 확장 — 기존 호출부는 전부 `c[0]~c[3]` 인덱스 접근이라 **끝에 붙이면** 무수정 호환. 추가 API 콜 0.
2. 터치 앵커 캔들 확정 시(1123~1128행 루프에서 이미 캔들을 찾는다) 함께 기록:
   - `touch_penetration_pct` = (entry − 캔들저가)/entry ×100 (관통 깊이)
   - `touch_candle_closed_below` = 앵커 캔들 종가 ≤ entry (0/1)
3. (선택) 다음 회차에 계산 가능한 `touch_reclaim_5m` — 앵커 이후 5분 내 종가가 entry 위 복귀 여부. 판정 루프가 어차피 터치 이후 캔들을 스캔하므로 그 안에서 1회 기록.

**리스크.** 튜플 확장이 15분봉 폴백 경로·테스트(`test_price_logic.py`)에 닿는다 — 끝-추가 방식이면 회귀 범위는 파서 한 곳. 저장만 하므로 알림 동작 변화 0.

---

## F3. TP1 단일 성공 라벨 — 병행 변동성 라벨 부재 (감사각 4)

**현상.** 종결 라벨은 `hit`(TP1 도달)/`miss`(SL)/`timeboxed_*`. `best_tp_hit`·`r_multiple`(윈저라이즈 −1~5)·MFE/MAE는 기록되지만, **작성자 채점(author_closed_stats → 실적 가점 +15)은 TP1 도달률 하나로 돈다.**

**정밀도 손실 메커니즘.** TP1은 작성자가 정한 자의적 거리다. 2% TP를 쓰는 작성자와 12% TP를 쓰는 작성자의 "TP1 도달률"은 실력이 아니라 목표 거리의 함수로 갈린다. 목표거리 배점(-6~+12)이 부분 보정하지만, 등급의 최강 예측변수(실적 가점, 작성자별 74% vs 0~29% 격차)가 이 편향된 라벨 위에 서 있다. 같은 품질의 콜을 낸 두 작성자가 TP 스타일 차이만으로 가점 티어가 갈리면, 등급 필터의 정밀도가 그만큼 무뎌진다.

**업계 관례.** López de Prado의 **트리플배리어**: 상단·하단 배리어를 최근 변동성의 배수로, 수직 배리어를 시간으로 두고 "먼저 닿은 쪽"으로 라벨링 — 작성자 자의성이 제거된 표준 라벨이다([QuantStrategy.io](https://quantstrategy.io/blog/the-triple-barrier-method-revolutionizing-how-we-label/), [Quant Memo](https://www.quantmemo.com/concepts/triple-barrier-labeling), [PapersWithBacktest](https://paperswithbacktest.com/course/triple-barrier-method)). 변동성 스케일링(레짐 적응)이 핵심 권고.

**최소 변경안 (병행 라벨 — 기존 outcome 절대 불변, 소급 재작성 없음).**
`_judge_outcomes`의 기존 캔들 스캔 루프(이미 MFE/MAE를 누적 중, 1354~1366행)에 대칭 배리어 최초 교차 추적을 추가:
- `tb_label` ∈ {+1, −1, 0}: 터치가 기준 ±X%에 먼저 닿은 쪽 (기간 내 미도달 = 0)
- `tb_barrier_pct`: 그 회차에 쓴 X 값 (1단계는 고정 3%로 시작 — 추후 ATR 스케일로 승격 가능하도록 값 자체를 저장)
- `tb_hours`: 교차까지 시간
MFE/MAE 최종값만으로는 "어느 쪽이 먼저였나"를 복원할 수 없으므로 스캔 루프 안에서 잡아야 한다 — 지금이 유일하게 공짜인 지점이다(추가 API 0콜, 순수 파이썬 몇 줄). 작성자 랭킹·가점은 당분간 TP1 그대로 두고, 두 라벨의 괴리(TP1 순위 vs tb 순위)를 내부 리포트로만 관찰한 뒤 교체 여부를 별도 결정한다.

**리스크.** 판정 핫패스에 연산 추가 — 산술 몇 줄이라 무시 가능. 라벨 축 이원화로 혼동 여지 → 컬럼 주석·매뉴얼에 "병행 관찰용, 공식 라벨 아님" 명기. SL 미기재 글에도 라벨이 생기므로(대칭 배리어는 SL 불필요) SL 감점류 기능이 아니다 — 금지 원칙과 무충돌.

---

## F4. 고정 168h 레벨 만료 — 타임프레임 무시 (감사각 1)

**현상.** `level_expiry_hours=168` 고정. 반면 **판정 창은 이미 타임프레임 비례**다(`judgment_window_hours`: ≤30분봉 72h / 1~2H 168h / 4H급 336h / 1D+ 720h). 즉 "터치를 기다리는 기간"만 계단 없이 평평하다. 4h 아이디어의 168h = 42봉 대기, 1D 아이디어의 168h = 7봉 대기 — 같은 7일이 봉 수로는 6배 차이다.

**정밀도 손실 메커니즘.** 4h 차트 셋업이 6일 뒤 터치되면 작성자의 분석 맥락(42봉 경과)은 소멸한 뒤다 — 신선도가 다른 터치들이 같은 라벨 풀에 섞여 등급-성과 상관을 희석한다. 다만 자체 실측(expire_old 주석: 게시 7일+ 터치 9건, 승률 50% vs 0~3일 48%)은 **절대 나이** 기준으로는 늦은 터치가 나쁘지 않음을 보여줬다. 미검증 축은 **tf 상대 나이**(경과 봉 수)다.

**업계 관례.** 셋업 유효기간을 시간·봉 수로 자르는 time stop / GTD 주문이 표준 관례 — 모멘텀 셋업은 시간 단위, 스윙은 일 단위로 유효기간을 달리 두고, DiNapoli의 "rule of three"처럼 봉 수 기준 무효화 규칙도 통용된다([Forex Factory time stops](https://www.forexfactory.com/thread/56413-using-time-stops), [TradingView time stop 해설](https://www.tradingview.com/chart/EURUSD/JsH2RCAg-Trade-Management-Using-Time-Stops/)). 유효기간을 진술된 타임프레임에 비례시키는 것이 관례의 골자다.

**최소 변경안 (2단계, 1단계는 기록만).**
1. 터치 시 `touch_bars_elapsed` = (touched_at − collected_at) / (timeframe_hours×3600) 기록 (tf 명시 글만, NULL이면 NULL). 컬럼 1개.
2. 표본이 쌓이면(수 주) tf 상대 나이 구간별 hit률을 내부 집계로 확인. 열화가 실측되면 **tf 명시 글에 한해** `expiry = min(168h, N봉 × tf)` 적용 — 항상 168h 이하로만 움직이므로 순수 조이기(완화 없음). 예: N=40이면 1h 아이디어 40h, 4h 아이디어 160h, 1D+는 그대로 168h.

**리스크.** tf 명시 글 비중이 낮아(파서가 "Timeframe:" 류 명시 키워드만 인식, "대부분 미명시" — extractor 주석) 커버리지가 제한적. 2단계는 알림량 감소 방향이므로 사용자 확인 후 적용. 1단계는 리스크 0.

---

## F5. 고정 1% 클러스터 밴드 — 변동성 무시 (감사각 2)

**현상.** `cluster_band_pct=1.0` 전 코인 공통. BTC(일변동 ~1.5~3%)와 고변동 알트(일변동 8~15%)에 같은 1%.

**정밀도 손실 메커니즘.** 양방향 손실이 있다. ① 저변동 대형주: BTC에서 0.9% 떨어진 두 엔트리는 실질적으로 별개 셋업인데 병합된다 — 트리거는 상단 엔트리, 하단 레벨은 섀도 처리되어 "어느 분석가의 어느 레벨이 맞았나"의 해상도가 낮아진다. ② 고변동 알트: 1.5% 간격의 두 레벨은 사실상 같은 존인데 별개 클러스터로 남아, 급락 관통 시 몇 분 간격으로 본알림이 연발된다(재발송 차단은 같은 ids만 막고, 코인당 상한 3이 유일한 방어).

**업계 관례.** S/R을 선이 아니라 **존**으로 취급하고 존 폭을 ATR(14) 배수로 스케일링, 근접 피벗을 ATR 범위 안에서 클러스터로 병합하는 것이 통용 관례다([TradingView: Dynamic ATR Cluster S/R Zones](https://www.tradingview.com/script/OMUh7f5y-Dynamic-ATR-Cluster-Support-Resistance-Zones/), [MQL5: ATR Ranked S/R Zones](https://www.mql5.com/en/code/74421), [LuxAlgo S/R Zone](https://www.luxalgo.com/library/concept/s-r-zone/)).

**최소 변경안 (섀도 우선 — 실밴드 변경은 데이터 확인 후).**
1. 터치 시 기록: `touch_cluster_span_pct`(클러스터 내 엔트리 최대-최소 스프레드), `touch_cluster_n`(멤버 수) — API 0콜. 발송 경로에서는 이미 일봉 200개를 받으므로(`fetch_position_data`) 그 종가·고저로 `touch_atr14_pct`를 계산해 함께 기록 — 추가 콜 0.
2. 표본으로 "코인 변동성 대비 1% 밴드의 과병합/과분리" 분포를 확인한 뒤, 필요 시 `band = clamp(0.5×ATR14%, 0.3%, 1.0%)` — **상한을 현행 1%로 못박아** 지금보다 넓게 병합되는 일이 없게 한다(병합 확대·알림 증가 방향 배제).

**리스크.** 밴드 축소는 대형주에서 클러스터 분리 → 알림이 소폭 늘 수 있다(코인당 3·전체 15 상한이 방어). 이 방향성 때문에 반드시 데이터 확인 + 사용자 결정 후 적용. 1단계 기록은 리스크 0. 유니버스 300 관찰 기간(~08-22) 중에는 1단계만.

---

## F6. 수급/자리 판정이 발송 순간 1스냅샷 (감사각 7)

**현상.** `derive_supply_verdict`(펀딩+OI+CVD+호가+옵션+매크로 합성)와 `derive_position_verdict`는 발송 시 1회 계산·기록으로 끝. 입력 중 펀딩(8h 주기)·OI 24h 변화는 느리지만, **호가 잔량비(OBI)는 단일 오더북 스냅샷, CVD는 4h 창** — 순간 노이즈가 라벨(우호/중립/주의)을 한 칸 밀 수 있는 구조다(우호+warn 1개 → 중립).

**정밀도 손실 메커니즘.** 축적 중인 `touch_supply_verdict` 통계의 라벨 자체에 스냅샷 노이즈가 섞인다. "우호 판정 터치의 hit률" 같은 후속 분석의 분별력이 그만큼 무뎌진다.

**업계 관례.** 모니터링/알림 시스템의 표준은 단일 샘플을 신뢰하지 않는 것 — Prometheus의 `for` 지속시간(조건이 N초간 유지돼야 발화)·`keep_firing_for`, 연속 N회 확인, 이중 임계 히스테리시스가 관례다([Prometheus alerting rules](https://prometheus.io/docs/prometheus/latest/configuration/alerting_rules/), [Prometheus debouncing](https://alvo.me/en/posts/2023/prometheus-debouncing/), [알림 플래핑 완화](https://web-alert.io/blog/alert-flapping-detection-taming-unstable-alerts)).

**최소 변경안.** 터치 알림은 지연 불가(2사이클 합의 대기 = 알림 지연)이므로 발송 라벨은 그대로 두고, **+1h 재판정 기록**을 붙인다: 이미 시간당 도는 `_snapshot_oi` 사이클에서, 최근 1h 내 터치된 코인에 한해 `derive_supply_verdict`를 재계산해 `touch_supply_verdict_1h`로 저장. 파생 스냅샷은 그 사이클이 이미 받는 CoinGecko 맵 재사용(추가 콜 0), OBI만 코인당 1콜(터치는 하루 ≤15건 상한이라 상한 15콜/일). 두 판정의 일치율이 나오면 "이 판정을 언젠가 필터로 승격해도 되는가"를 처음으로 수치로 답할 수 있다. 전부 내부 전용 — 알림 무노출 원칙 유지.

**리스크.** 사실상 없음(기록 전용, 콜 상한 유계). 재판정 시점(+1h)은 터치 직후 변동성 구간이라 "안정성 측정"이라는 목적에 부합.

---

## F7. 글 신선도 — 데이터는 이미 있고 분석 축만 비어 있다 (감사각 5)

**현상.** `post_age_minutes`(수집 시점 글 나이)와 `touched_at − collected_at`이 모두 저장되므로 **터치 시점 글 나이는 지금도 계산 가능**하다. expire_old 주석의 1회성 분석(터치 67건: 게시 7일+ 승률 50% vs 0~3일 48%)이 있었지만 정기 관찰 축은 아니다.

**정밀도 손실 메커니즘.** 신호는 시간이 지나며 정보 가치가 감쇠한다는 것이 알파 디케이 문헌의 일관된 결론이고([Di Mascio & Lines, *Alpha Decay* (SSRN)](https://www.top1000funds.com/wp-content/uploads/2021/05/SSRN-id2580551.pdf), [신호 감쇠 해설](https://microalphas.com/signal-decay-patterns/)), 시그널 자동화 도구도 "게시 후 가격이 엔트리 범위를 이탈했으면 미체결"을 기본 동작으로 둔다([Cornix 시그널 관리](https://help.cornix.io/en/articles/5814964-managing-signals)). 자체 1회 실측은 "절대 나이 무해"였지만 표본 67건 시점의 결론 — 지금은 표본이 몇 배로 늘었고, 등급·작성자별 교차 축은 본 적이 없다.

**최소 변경안.** 코드 변경 없이도 가능하나, 조회 편의를 위해 터치 시 `touch_post_age_h`(= post_age_minutes/60 + (touched_at−collected_at)/3600) 비정규화 저장 1줄. 주간 내부 집계(자동 발송 없음 — weekly_report_auto_send=False 유지)에 "터치 시점 글 나이 구간(0-1d/1-3d/3-7d/7d+)별 hit률" 섹션 추가. F4의 tf 상대 나이와 같은 표에서 보면 좋다.

**리스크.** 없음. 결과가 "감쇠 실재"로 나와도 조치는 조이기(오래된 글 억제)이므로 금지 원칙과 무충돌.

---

## F8. 억제 게이트 순서 — 대체로 건전, 미세 낭비 1곳 (감사각 8)

**현상 및 평가.** 순서는 `재채점(순수연산) → 등급 → 타임프레임 → 스윙TP(순수) → 코인상한(DB) → 글로벌상한(DB) → 재발송차단(DB+원장) → 발송확정 후에만 고비용 API(52주·RSI/MA 3콜·펀딩·CVD·오더북 등)`. **싼 게이트 → 비싼 호출 순서는 올바르게 잡혀 있다.** 상태 전이가 필터와 무관하게 수행되는 것(억제돼도 touched 전이)도 재알림 방지 원칙상 의도된 설계고, 상한 억제 건은 `suppressed_cap`(등급 통과 후에만 증가하므로 "아까운 억제"의 정확한 부분집합) + m-8 심리 스냅샷으로 이미 사후 분석 가능하다. 구조적 결함 없음.

**미세 낭비 1곳.** 클러스터 루프에서 `작성자 실적 DB 조회 → _rep(전 멤버 재채점)`이 **dup_preview 판정보다 앞**에 있다(621~632행 → 643행). 예고 밴드에 머무는(dwell) 클러스터는 매 회차(2분) 이 경로에 도달하므로, 하루 수백 회차 × (author_closed_stats 조회 + 전 멤버 재채점)이 결과가 버려지는 채로 반복된다. `dup_preview`는 rep와 무관하게 `cluster` 상태만으로 계산되므로, **dup_preview 판정·continue 를 작성자 주입 앞으로 올리는 순수 재배치**로 낭비가 사라진다. 동작 변화 0(터치 경로·신규 예고 경로는 순서 무영향).

**리스크.** 재배치 회귀 범위는 해당 블록뿐. `test_price_logic.py` 기존 회귀로 커버 확인.

---

## 구현 묶음 제안 (전부 내부 전용·양식 불변)

- **1차(즉시, 위험 0):** F1 터치 등급 영속 + F7 touch_post_age_h + F4-1단계 touch_bars_elapsed + F5-1단계 클러스터 스팬/ATR 기록 — 전부 `mark_touched` 주변 컬럼 추가라 한 스프린트 묶음이 자연스럽다.
- **2차:** F3 트리플배리어 병행 라벨(판정 루프 내 계산) + F2 캔들 종가 확장·터치 품질 기록.
- **3차:** F6 +1h 수급 재판정, F8 dup_preview 재배치.
- **후속 결정(데이터 확인 후, 사용자 카드):** F4-2단계 tf 비례 만료(조이기), F5-2단계 ATR 밴드(상한 1% 고정).

## 출처
- López de Prado 트리플배리어: [QuantStrategy.io](https://quantstrategy.io/blog/the-triple-barrier-method-revolutionizing-how-we-label/) · [Quant Memo](https://www.quantmemo.com/concepts/triple-barrier-labeling) · [PapersWithBacktest](https://paperswithbacktest.com/course/triple-barrier-method)
- ATR 기반 S/R 존 폭: [TradingView Dynamic ATR Cluster S/R](https://www.tradingview.com/script/OMUh7f5y-Dynamic-ATR-Cluster-Support-Resistance-Zones/) · [MQL5 ATR Ranked S/R Zones](https://www.mql5.com/en/code/74421) · [LuxAlgo S/R Zone](https://www.luxalgo.com/library/concept/s-r-zone/)
- 유동성 스윕(꼬리 vs 종가): [VT Markets](https://www.vtmarkets.com/en-ca/discover/liquidity-sweeps-stop-hunts-explained-for-cfd-trading/) · [Alchemy Markets](https://alchemymarkets.com/education/strategies/liquidity-sweep/) · [Quadcode](https://quadcode.com/glossary/what-is-liquidity-sweep-everything-you-need-to-know)
- Time stop / 셋업 유효기간: [Forex Factory](https://www.forexfactory.com/thread/56413-using-time-stops) · [TradingView](https://www.tradingview.com/chart/EURUSD/JsH2RCAg-Trade-Management-Using-Time-Stops/)
- 알림 디바운스/히스테리시스: [Prometheus alerting rules](https://prometheus.io/docs/prometheus/latest/configuration/alerting_rules/) · [alvo.me](https://alvo.me/en/posts/2023/prometheus-debouncing/) · [web-alert.io](https://web-alert.io/blog/alert-flapping-detection-taming-unstable-alerts)
- 알파 디케이/신호 신선도: [Di Mascio & Lines (SSRN)](https://www.top1000funds.com/wp-content/uploads/2021/05/SSRN-id2580551.pdf) · [microalphas](https://microalphas.com/signal-decay-patterns/) · [Cornix](https://help.cornix.io/en/articles/5814964-managing-signals)
