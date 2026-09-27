# 스프린트1 — 추출기 의미 오해 수리 (개발자A, 2026-09-27)

범위: 감사 종합 P0-1(숏→롱)·P0-2(숫자 오인)·P0-4(돌파 트리거)·P0-5(비셋업/코인 오인) + A-T5(TP1 표시 정합).
변경 파일: `collector/extractor.py`, `scripts/run_collect.py`(`_ingest_idea`), `config/settings.py`(스위치 3), `scripts/test_extractor.py`.
`collector/tradingview.py` 는 **무수정** — 방향 태그는 이미 `idea["direction"]`(목록 JSON·HTML 폴백·상세 보강)에 실려 있어 `_ingest_idea` 가 넘기기만 하면 됐다.
운영 DB 는 `mode=ro` 로만 열었다. `alert_pipeline_manual.md` 는 CTO 몫이라 건드리지 않았다.

## 1. 바뀐 규칙

| # | 규칙 | 위치 | 스위치 |
|---|---|---|---|
| 1① | TradingView 작성자 방향 태그(long/short)가 있으면 텍스트보다 우선. short 는 기존 `collect_short_enabled=False` 경로로 스킵 | `parse_setup(direction_hint=)`, `_ingest_idea`(태그 있을 때만 인자 전달 → 구 시그니처 목과 호환) | `extract_use_tv_direction` |
| 1② | 방향 단어 판정 **전에** 비방향 관용구 제거: as long as, long-term/standing, longer, long upper wick, short-term, shortly, in the short run, short squeeze, long/short ratio, buy/sell volume·pressure, sell-off 등 | `_DIRECTION_NOISE` | — |
| 1③ | 방향이 애매·없음일 때 크기 sanity 를 통과한 TP 후보 **2개 이상이 전부 진입가 아래**면 short. 보조: TP 1개(아래) + 손절 후보가 진입가 **위**면 short(792) | `parse_setup` | — |
| 2 | `_clean` 추가: 괄호 서수 "Target 1 (TP1)"·"Target 1 at"·"Target Two (R2)", "T1:" 약칭, RR 비율(RR 1:1.5, R/R:2, R:R 3, Risk/Reward Ratio (R:R): ~2.3:1, 1:N 류), 레버리지("Leverage x 5-10-20", "Lev 10x", "x10", "Leverage: cross 12.5", "Margin 1-5%"), 타임프레임(H1·M15·4H·15m·1D·"4-hour"), 기간("30 Days", "2 weeks") | `_RR_RATIO`, `_ONE_RATIO`, `_LEVERAGE_LABEL`, `_TF_TOKEN`, `_TF_NUM_UNIT`, `_T_ORDINAL`, `_SPACED_ORDINAL_LABEL` | — |
| 2 | 산문형 라벨(콜론 없음)은 라벨-숫자 거리 제한 + 문장 끝·다른 라벨 키워드(target/tp/stop/sl/invalidation/liquidity/resistance/current price/leverage/margin)에서 창을 끊음. 산문 진입 숫자 바로 뒤가 "… resistance" 면 버림(754). 스펙형(콜론) 경로는 종전 80자 창 그대로 | `_grab_after_ex` | — |
| 2 | `_ENTRY_LABEL`: "Re-Entry" 제외, "buy-side" 제외, "Buy Limit Zone:"·"Entry Level:" 스펙형 인정 | `_ENTRY_LABEL` | — |
| 2 | 80자 창 끝이 숫자 중간이면 숫자 끝까지 읽음(2.16→2.1 방지). "N to N" 범위는 진입 라벨 **같은 줄**에서만 | `_window_end`, `_RANGE_TO` | — |
| 3 | 진입값 주변(같은 문장 ∪ ±40자, buy stop 만 ±100자)에 buy stop / close(s) above / breaking above / breakout above / bullish flip / reclaim / break and confirm above 가 있으면 그 값 기각. retest·pullback to 동반이면 예외. 스펙형 진입이 전부 트리거로 기각되면 산문으로 내려가지 않음(스킵) | `_is_trigger` | `extract_breakout_trigger_skip` |
| 4 | 비셋업 스킵: "NOT (a) signal(s)"(±60자에 financial advice/educational/own research/DYOR/NFA 가 있으면 면책문으로 보고 통과), From Our Entry / Entry Filled / TP hit / Target(s) hit·reached / +N% profit(앞 40자에 if/once/when/after 조건절이면 통과), 본문 티커 불일치 | `nonsetup_reason()` → `_ingest_idea` 가 had_setup=True·저장 안 함·`skip_counts["nonsetup"]` | `extract_nonsetup_skip` |
| 5 | 대표 `tp` = 유효 TP 사다리 첫 값(`tps_all[0]`), rr 재계산 | `parse_setup` 말미 | — |

**T5 순서 규약 확인**: `price_check._volume_band_tps` = `entry < t <= entry*4` 필터 + 오름차순 → `[0]` 이 TP1. extractor `tps_all`(롱) = `entry*0.25 <= t <= entry*4` 이고 `t > entry` → 같은 집합, 오름차순. 따라서 `tp == tps_all[0] == _volume_band_tps(rep)[0]`. 이제 `tp_usd ∈ tps_usd` 가 항상 성립해 `_volume_band_tps` 의 합집합 보정은 신규 행에서 no-op 이 된다.

**코인 불일치 규칙**: 본문의 `#XXX`·`$XXX` 와 제목·첫 줄의 `EXCHANGE:XXX` 만 센다(USDT/USD/PERP·1000 접두 제거, BTC.D·TOTAL·DXY 등 지표 제외, 별칭은 접두/접미 관대 매칭). 본문 전체의 `CRYPTOCAP:BTC` 자동 링크까지 세면 867 XRP(BTC·ETH·SOL 을 참고로만 언급)가 불일치로 오탐돼서 좁혔다.

## 2. 과거 원문 재파싱 회귀 (수정 전 origin/main vs 수정 후, raw_text 보유 301행)

방법: `git show origin/main:collector/extractor.py` 를 임시 모듈로 로드. 양쪽 모두 `current_price=None`(파서 자체 비교). 파이프라인 동치 판정: parse 결과 없음 → 스킵(no_setup), direction=short → 스킵(short), 수정 후만 `nonsetup_reason` → 스킵. 스크립트: `scratchpad/s1a/reparse.py`, 결과 `out4.txt`.

| 구분 | 건수 |
|---|---|
| 변경 없음 (값 동일) | 186 |
| 변경 없음 (양쪽 다 스킵) | 25 |
| **스킵으로 바뀜** | **63** — no_setup 45 · short 15 · coin_mismatch 1 · not_signal 1 · result_report 1 |
| **값 변경** (양쪽 다 셋업) | **27** — tp 22 · entry 6 · sl 3 (한 행에 여러 필드 가능) · direction 0 |
| 스킵→셋업 | 0 |

### 2-1. 감사 C 판정표 '정상' 91건 중 바뀐 것 (오탐 후보 전부)

| id | 코인 | 변경 | 판정 |
|---|---|---|---|
| 630 | ZRO | tp 1.20 → 1.16 | **의도된 변경**(T5: 원문 "Target 1: 1.20 / Target 2: 1.16", 가격순 TP1 = 1.16) |
| 666 | DOGE | sl 없음 → 0.088 | 개선. 원문 "Stop : below the recent low at $0.0880" |
| 849 | BTC | tp 없음 → 86,700 | 개선. "T1:" 약칭 라벨 인식(감사 A-T3 이 지적한 미인식 건) |
| 710 | DOT | entry 0.963 → 0.993 | 중립. 종전값은 "…entry.\n\nBuying Order Block: $0.933 – $0.993" 문장 경계를 넘어 주운 존 중앙값. 수정 후는 작성자 라벨 "Entry Trigger: $0.993"(존 상단) |

→ **해로운 오탐 0건**(값이 틀려진 정상 건 없음). 정상 건이 스킵으로 바뀐 경우도 0건.

### 2-2. '오해' 건 수정률

- **발송분 31건(감사 C 부록): 31/31 수정 (100%)**
  - D 방향 9/9: 636·682·686·688·692·699·736·792 → short 스킵, 643 → 셋업 없음(진입 산문 "buy volume … 0.8060" 이 거리·문장 제약에 걸림)
  - L 라벨 오귀속 9/9: 575·623·628·669·671·700·703·768 → 셋업 없음, 656 → 진입 0.1955 / SL 0.1854 / TP 0.2045(원문 값)
  - B 돌파 5/5: 637·663·683·807·884 → 셋업 없음
  - T 1/1: 772 tp 2.0 → 0.9614
  - C 1/1: 665 → coin_mismatch
  - R 3/3: 678·697·835 → 셋업 없음(산문 창 제약으로 먼저 걸림; 규칙 단독으로도 not_signal/result_report 판정 — 테스트 S1-R1·R3)
  - Z 3/3: 612 → 2.16, 646 → 0.10~0.16(중앙 0.13), 538 → 셋업 없음(틀린 230 제거. 원문 "add around $230 - $220" 은 라벨 단어가 없어 복원 못 함)
- **미발송 확인분 38건(감사 C §2 + A-T4 878): 35/38 수정 또는 이미 무해**
  - T 7/7 수정: 749·783·799·812·851·868·878 모두 원문 TP1 회복(878 1.0 → 0.5843)
  - D 7/8: 760·791·796·814·834·848·875 스킵, **794 미수정**(방향 단어 없음·TP 1개·SL 없음 — 규칙 ③ 조건 미충족)
  - L 17/17 무해: 9건 수정(660·676·719·752·769·810·846·879 스킵, 831 진입 15 → 11.129 원문값), 8건은 종전에도 short 로 스킵되던 행
  - B 2/4: 754 스킵, 673 종전에도 스킵. **704·766 미수정** — 둘 다 "pullback"/"retest" 동반이라 리테스트 예외에 해당(766 은 "retests 102.00–102.50 and closes back above" 로 사실상 리테스트 진입이라 유지가 타당하다고 판단)
  - R 1/1: 881 result_report
- 판정표 '애매' 3건(625·648·853)은 변화 없음.

### 2-3. 대가 — 판정표 밖(무라벨)에서 잃은 정상 셋업

산문 제약 때문에 **라벨 없는 행 중 원문상 타당한 진입가를 잃은 것**: 739 SOL("Risky Limit Entry … limit order around $97.22", 거리 초과), 765 SOL("The entry is not here … RBS zone at $76.12 – $84.23"), 867 XRP("Buy / Entry Region: … ($1.4900 - $1.5300)", 거리 초과), 642 SOL(PRZ 존). 판단 보류 쪽이라 틀린 알림은 안 나간다. 그 밖의 무라벨 스킵(603·672·674·685·693·723·729·734·746·790·809·824·863·882·709)은 잡음·가짜 진입(1.0 등)·결과글이라 제거가 맞다.
무라벨 값 변경은 전부 원문 확인 결과 개선: 822 NEAR 진입 1.0 → 4.103("Entry Zone 1: 4.103"), 852 CKB·869 AAVE·763 PENDLE tp 가 숏 플랜 값/뒤 TP → 롱 플랜 TP1, 764·808·829·865·870·877 TP 회복, 705 SL 회복.

### 2-4. 지시 대비 조정한 값 (재파싱 근거)

- **산문 라벨-숫자 거리 25자 → 60자**: 25/40/60/80 을 비교했다. 25 는 정상 산문 진입 4건(593 BTC $70,853, 644 XRP "Entry confirmation / key support: 1.3733", 751 PUMP, 869 AAVE "Entry after a retest and confirmation between 144.5-146")을 추가로 잃고, 추가로 막는 오해는 754 1건뿐이었다(754 는 "숫자 뒤 resistance" 규칙으로 따로 막음). 80 은 734 XRP 에 틀린 진입(1.445)을 만든다. 60 에서 오해 수정률은 25 와 같다(100%). 문장 끝·다른 라벨 끊기가 L 유형 대부분을 막는 실질 장치였다.
- TP/SL 창은 "entry" 단어에서 끊지 않는다("SL is 2% below entry, around $12.02" 684 회귀 때문).

## 3. 운영 반영 시 알아둘 것

- **`db.reparse_all` 이 매 수집 회차 활성 롱 레벨을 새 파서로 재계산한다.** 현재 활성 42행(raw_text 보유) 시뮬레이션: 13행이 다르게 파싱된다. 이 중 7행은 TP 가 생기거나 바뀐다(799·851·783·868·877 은 TP 없음 → 생김, 852·869 는 숏 플랜 TP → 롱 TP1). 무TP 게이트(09-22)에 막혀 있던 5행이 발송 대상이 될 수 있다. 나머지 6행(790·809 ATOM 진입 1.0, 822·863·867·882)은 새 파서가 None 을 돌려주는데, `reparse_all` 은 None 이면 건너뛰므로 **그대로 남는다**(기존 오염 레벨은 자연 만료).
- `tp` 선정이 바뀌어(T5) 신규 레벨의 등급 tp_dist 배점과 판정창(`judgment_window_hours`)이 이전 표본과 조금 달라진다(감사 A-T5 추천 2 의 "채점 축 이동" — v6 관찰 중이라는 점은 CTO 판단 사항).
- TV 방향 태그는 DB 에 저장되지 않아 **과거 재파싱으로는 검증하지 못했다**. 단위 테스트(S1-D7·D8·SW3)와 `_ingest_idea` 통합 테스트(S1-I1·I3)로만 확인했다. 태그 오기(롱 글에 short 태그) 비율은 미측정이다.
- 수집 로그에 `[수집] … 비셋업 글 스킵(사유)` 가 새로 찍히고, `skip_counts["nonsetup"]` 이 누적된다(요약 출력에 쓰려면 호출부 집계 표시 추가가 필요 — 이번 범위 밖).

## 4. 테스트 (순차·포그라운드, PYTHONIOENCODING=utf-8)

| 스위트 | 결과 |
|---|---|
| test_extractor | exit 0 — 133/133 (신규 S1 39건 + `_ingest_idea` 통합 3건, FB1 기대값을 "사고 재현"에서 "12.5 를 줍지 않음"으로 변경) |
| test_resilience | exit 0 — 235건 전부 통과 |
| test_cycle | exit 0 |
| test_infra | exit 0 — 224건 전부 통과 |
| test_touch_recording | exit 0 — ALL PASS |

테스트가 건드린 `data/universe_rank_history.json` 은 `git checkout -- data` 로 되돌렸다.

## 5. 남은 이슈

1. 794 BTC 류(방향 단어 없음 + TP 1개 아래 + SL 없음) 숏 글은 여전히 롱으로 저장된다. 무TP 가 되어 09-22 게이트가 알림은 막는다.
2. 704(Marked Entry / Trigger Level)·766 은 리테스트 예외로 유지된다.
3. 2단 하이픈 TP("Targets: 0.2513 R/R:2 - 0.2636" → RR 제거 후 "0.2513 - 0.2636")는 종전 규칙대로 **범위**로 읽혀 tp=상단 0.2636(808). 하이픈 2개짜리가 범위인지 나열인지는 기존 설계상 모호한 부분이라 이번에 건드리지 않았다.
4. 산문 제약 때문에 739·765·867·642 같은 먼 거리 산문 진입가를 잃는다(2-3).
