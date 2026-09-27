# 감사 B — 결과·판정·상태 전이 모순 (2026-09-27, 읽기 전용)

감사자: 개발자B · DB `levels.db`(mode=ro) · 기준시각 meta.last_cycle_at=1790473938 (09-27 약 10:12 KST)
스크립트: `scratchpad/audit_b/s0~s18.py` · 표본: levels 885행, alerts_log 467행(08-28~09-27, 30일 보존)

## 요약 (5줄)
1. **실제 결함 1순위**: 중간 TP까지 간 뒤 손절·기간만료로 끝나면 `best_tp_hit` 이 NULL로 남음. 62건(발송 19). 알려진 "TP2 급감"은 이 결함 때문에 생긴 착시임. 매주 TP2 이상 간 레벨은 8~30건인데 best_tp_hit=2 는 0~7건.
2. **판정 라벨 결함**: SL만 있는 글이 `judgment_mode='timeboxed'`(TP·SL 없음)로 찍힘. 26건이고 그중 miss 18건은 전부 r=−1. 09-22 D1 근거인 "timeboxed PF 0.60"이 이 행들로 오염됐을 수 있음.
3. **가짜 진입가 종결**: Roddy01 템플릿에서 진입가 12.5/SL 5가 뽑힌 글이 miss로 종결됨. 종결가가 시장가의 수십~수백 배임(4건, 발송 3). 알려진 ret_24h ≤ −80% 6건도 전부 이 계열(엔트리 파싱 오류 + 즉시터치)임. 09-27 무장 게이트로 신규 발생은 막힘.
4. **알림 순서·중복은 무결**: TP가 터치보다 먼저 나간 건, 터치 없는 TP, 터치 2회, TP 순번 건너뜀, 종결 뒤 TP, 사다리 초과 모두 0건. tp7/tp8은 ARB id=502의 9단 사다리 안이라 정상임. 원장(ndjson)과 DB도 7일 구간에서 완전히 일치함.
5. **내부 리포트 과대계상**: show_status 의 "발송" 칼럼이 sent=0 행까지 셈. 09-13 이후 15일간 159건이 부풀려짐. 환율 축 괴리(hit인데 KRW로는 손실 등, 22건)는 판정 오류는 아니지만 저장값만 보면 해석을 헷갈리게 함.

---

## E1. 중간 TP 도달 후 비적중 종결 시 best_tp_hit 미기록 ★
- **정의**: `tp_alert_idx>0`(TP1 이상 도달)인데 outcome ∈ {miss, timeboxed_*} 이고 `best_tp_hit IS NULL`.
- **건수**: 62건. 이 중 miss 7, timeboxed_loss 20, timeboxed_win 35. 터치 알림이 발송된 건(sent=1)은 19건.
- **대표 사례**
  - id=502 ARB: 9단 사다리 중 TP8까지 도달했고 tp1~tp8 알림 8건이 모두 실제 발송됨(sent=1). 그런데 DB 결과는 timeboxed_win, best_tp_hit=NULL, r=+5.0임.
  - id=707 FIL: 8단 중 TP6 도달(tp1 발송, tp2~6은 sent=0 기록) → timeboxed_win, best NULL
  - id=277 BTC: 5단 중 TP2 도달 → miss, best NULL
- **"TP2 급감" 원인 규명**: best_tp_hit 은 최종 TP 적중(hit)일 때만 기록됨. 그래서 best_tp_hit=2 는 실제로는 "2단 사다리를 끝까지 간 건"이라는 뜻이 됨. 주별 비교는 다음과 같음.
  - TP2 이상 도달: W35 17, W36 9, W37 20, W38 17
  - best_tp_hit=2: 같은 주 0, 0, 2, 0
  - 즉 TP2 도달 자체가 줄어든 게 아님. 3단 사다리가 주류가 되면서 TP2에서 멈춘 건이 전부 NULL로 빠진 것임.
- **사용자 체감**: 낮음. 알림·반응은 정상임. 👍 는 우선순위상 👎 로 덮이지 않음. 다만 분석·캘리브레이션 영향은 높음.
- **근본 원인**: `monitor/price_check.py:_judge_outcomes`의 L2362(miss)와 L2368~2371(timeboxed) 두 곳에서 `db.resolve_outcome(...)`를 호출할 때 `best_tp_hit` 인자를 넘기지 않음. 최종 hit 경로(L2357 `best_tp_hit=_best`)만 넘김.
- **추천**
  1. 두 호출에 `best_tp_hit=(_tp_alert_idx or None)`을 전달. 장점은 한 줄 수리라는 점. 해시 체인(`_compute_outcome_hash`)이 best_tp_hit 을 포함하지 않으므로 체인을 깨지 않음. 단점은 `best_tp_hit IS NOT NULL`을 "적중"으로 읽는 조회가 있는지 확인해야 한다는 점임.
  2. 과거 62행을 `best_tp_hit = tp_alert_idx`로 1회 백필. 장점은 과거 통계까지 복원된다는 점. 단점은 종결 행을 수정하는 것이어서 불변 스냅샷 원칙의 예외로 기록을 남겨야 한다는 점임.
- **09-27 수리로 막혔나**: 아니오.

## E2. SL-only 글의 judgment_mode 가 'timeboxed'
- **정의**: 유효 SL이 있는데 TP가 없음. 이 경우 mode가 `tp_sl`도 `tp_only`도 아니어서 'timeboxed'로 떨어짐.
- **건수**: 26건(miss 18, timeboxed_loss 3, timeboxed_win 5). 발송 8건(544, 615, 628, 663, 671, 683, 700, 703).
- **대표 사례**
  - id=615 BTC: 엔트리 78900, SL 77500 → miss, r=−1, mode=timeboxed
  - id=808 ADA: 엔트리 0.2449, SL 0.2417 → miss, r=−1
  - id=865 TRX: 엔트리 0.3389, SL 0.3384 → miss, r=−1
- **체감**: 없음(내부). 분석 영향은 중간임. 매뉴얼 게이트 3-2의 근거 "TP·SL 둘 다 파싱 안 된 글(`judgment_mode='timeboxed'`) PF 0.60"에서 이 모집단에 SL-only miss 18건(r=−1)이 섞여 있음. [추측] 그래서 PF가 실제보다 낮게 나왔을 수 있음.
- **원인**: `monitor/price_check.py:_judge_outcomes` L2217~2218. `mode = "tp_sl" if tp&sl else "tp_only" if tp else "timeboxed"` 식이어서 sl_only 분기가 없음.
- **추천**
  1. `sl_only` 값을 신설. 장점은 층화가 정확해진다는 점. 단점은 mode 를 소비하는 쪽(grading 연구 스크립트)을 확인해야 한다는 점임.
  2. 코드는 그대로 두고 분석 쿼리에서 `timeboxed AND sl_usd 유효` 조건으로 분리. 무수정이지만 매번 잊을 위험이 있음.
- **09-27 수리로 막혔나**: 아니오.

## E3. 가짜 진입가 레벨의 종결가·수익률 모순 (알려진 ret ≤ −80% 포함)
- **정의**: miss인데 resolve_price_krw 가 터치 직후 시장가(touch×(1+ret))의 2배를 넘음. 동시에 ret_* ≤ −80%임.
- **건수**
  - resolve 모순: 4건(438, 671, 700, 703), 발송 3건(671, 700, 703)
  - ret ≤ −80%: 6건(438 MOODENG, 575 ZORA, 656 PROVE, 671 MASK, 700 GMT, 703 MOODENG). 전원 엔트리 파싱 오류임.
- **대표 사례**
  - id=703 MOODENG: 엔트리 12.5 USD, SL 5. touch_price 17000원, 시장가 약 54원. 수집 후 6.7분 만에 "터치". 종결가 6800원(miss, r=−1), ret_24h −99.68%.
  - id=700 GMT: 종결가 6795원 vs 시장가 약 9.8원
  - id=671 MASK: 종결가 6785원 vs 시장가 약 609원
- **원인**
  - (a) 수집 축: Roddy01SIGNALSPROVIDER 템플릿 문구("Leverage x 5-10-20")에서 엔트리 12.5와 SL 5가 뽑힘. 같은 패턴이 626, 651, 653, 655, 719, 752, 769, 796, 810에도 있음.
  - (b) `monitor/price_check.py` L1808 `touches.append((id, e_krw ...))`: 터치가가 시장가가 아니라 엔트리 KRW(지정가 체결 모델)로 저장됨. 그래서 수익률 기준가와 종결가가 모두 가짜 가격 축 위에 놓임.
- **체감**: 높았음. 사용자 그룹에 3건이 발송됨.
- **09-27 수리**: 신규 발생은 막힘. 무장 게이트가 현재가 ≥ 진입가를 한 번 확인해야 하므로 12.5 USD 엔트리는 무장되지 않음. 09-13 sanity(×4)는 MOODENG/GMT는 걸렀지만 ETC(769, 796)·AVAX(810)의 12.5는 4배 이내라 통과했음.
- **잔존**: 미종결 796 ETC, 810 AVAX(미발송)는 앞으로 쓰레기 종결이 될 예정임. 다만 `STALE_TOUCH_COND`(관통 > 10% AND 지연 < 600초)에 걸려 분석에서는 배제됨(각각 관통 29.8%/2.7분, 10.7%/1.4분).
- **추천**
  1. 추출기에서 "Leverage x N-N-N" 구문을 가격 후보에서 빼기. 장점은 근본 수리라는 점. 단점은 수집 축 수정이 필요하다는 점임.
  2. 판정부에서 `touch_penetration_pct > 50`인 종결 행을 `expired_no_data`로 격리. 장점은 통계 위생. 단점은 이미 종결된 행에는 적용되지 않는다는 점임.

## E4. 즉시·깊은 관통 터치 — 알려진 것(09-27 SUI id=884)
- 09-13 이후 관통 10% 이상 터치 14건, 발송 3건(736 LSK 관통 36.1%, 835 PENGU 11.5%, 884 SUI 17.5%).
- 전체 기간으로 보면 터치 − 수집 ≤ 10분인 즉시터치가 290건(09-13 이전 224, 이후 66)임. 이 중 관통이 얕은 건은 정상적인 늦은 수집일 수 있음.
- **09-27 수리로 막힘**: 무장 게이트(게이트 0)와 관통 게이트(3-3)가 적용됨. 배포 후 터치 표본은 아직 0건이라 실효 검증은 대기 중임.

## E5. 환율 축 괴리 — 판정은 USD 축 일관, 저장값은 KRW
- **정의**: outcome과 KRW 부호가 반대인 건.
- **건수**
  - hit인데 resolve < touch: 11건, 발송 0
  - timeboxed_win인데 resolve < 기준가: 9건, 발송 2(552 VET 등)
  - timeboxed_loss인데 resolve ≥ 터치가: 2건, 발송 2(680 TRX, 688 VET)
- **대표 사례**
  - id=120 BTC: TP 65240, 터치 환율 1460 → 판정 환율 1422. hit 이지만 종결가 9277만 < 터치가 9475만.
  - id=680 TRX: 종결가 465 ≥ 터치가 461.0, 그런데 timeboxed_loss(r=−0.0024)
- **원인**: `_judge_outcomes`에서 L2064 `tp_krw = tp_usd * usdt_krw`(현재 환율), L2049 `base_eff`(환율 보정), L2369 비교가 USD 축으로 이뤄짐. 반면 `resolve_price_krw`는 보정하지 않은 KRW로 저장됨. 판정 로직 자체는 일관적이므로 오류 아님.
- **체감**: 잠재적으로 중간. Q4 결과 반응(👌/👎)이 KRW 손익과 반대로 달릴 수 있음. 현재 반응이 달린 종결 건 중 해당 사례는 0건임.
- **추천**
  1. 종결 시 `resolve_usdt_krw`를 함께 저장. 장점은 해석 모호성이 사라진다는 점. 단점은 칼럼 추가가 필요하다는 점임.
  2. 변경 없이 매뉴얼에 "판정은 USD 축"이라고 명시. 비용 0이지만 반응 이모지 괴리는 그대로 남음.
- **09-27 수리**: 무관.

## E6. show_status "발송" 과대계상 (daily_stats × alerts_log 대사)
- **정의**: `get_alerts_sent_by_day`가 `alerts_log`의 모든 행을 셈. 여기에는 sent=0인 tp/news, touch_no_tp, touch_deep 기록 행이 포함됨.
- **건수**: 09-13~09-27 15일 모두 불일치. 합계 159건이 부풀려짐. 예를 들어 09-18은 23건으로 집계되지만 실제 발송은 1건, 09-21은 17건 vs 실제 1건.
- **체감**: 없음(내부 CLI). 다만 알림량 판단을 왜곡할 수 있음.
- **원인**: `storage/db.py:get_alerts_sent_by_day` L2757~2764에 `WHERE sent=1`이 없음.
- **추천**
  1. `WHERE sent=1` 추가. 1줄 수리임.
  2. 발송분과 기록분 두 칼럼으로 분리. 관찰 가치가 더 크지만 show_status 헤더도 바꿔야 함.
- **보조 판정**: `touches_total`과 (억제 + 터치 발송)의 대사는 **불가능**함. 억제 카운터(suppressed_grade 등)를 예고·터치가 공유하기 때문임. 08-29은 억제 7 + 발송 6 = 13 > touches_total 10으로 나옴. 오류로 단정하지 않음.
- **09-27 수리**: 무관.

## E7. 결과 반응 — 섀도 형제에 결과 이모지
- id=807 XRP: 섀도 터치(touched_at NULL, outcome NULL)인데 touch_reaction='hit'임. 형제 806이 hit이라 같은 message_id(913)를 공유함.
- tp_partial이 idx=0인 형제에 달린 건이 3건(820·855·856 계열)임.
- **판정**: 설계대로임. 반응은 메시지 속성이고 `db.set_touch_reaction`이 message_id 단위로 기록함(L945). 오류 아님. 다만 레벨 단위 분석에서 touch_reaction을 쓰면 오염되므로, 쓸 때는 반드시 outcome과 함께 봐야 함.
- 반응 불일치(message 있음 + 종결 + 반응 누락 또는 반대)는 0건. 운영 개시(첫 반응 09-25) 이후 종결된 건이 hit 5건뿐이라 fail/👌 경로는 실운영 표본이 0임.

---

## 오류 아님 / 레거시 판정 (짧게)
| 의심 패턴 | 결과 | 판정 |
|---|---|---|
| tp7/tp8 실발송 | ARB 502 9단 사다리의 TP7·8 | 정상. 사다리 초과 0건 |
| OP 750 hit인데 tp1 알림만 기록 | 형제 728(엔트리 차 0.93%)이 tp2/tp3 기록 → `_tp_cluster_dup` 차단 | 설계 |
| 터치 알림 level_ids 중 expired 4건 | 섀도 멤버(556, 558, 567, 595)가 shadow_touch로 만료된 것 | 설계 |
| touched_at < collected_at 6건 | 전부 07-23~24(id 1, 7, 27, 45, 49, 59), 07-26 major3 수리 이전 | 레거시·수리됨 |
| touched_at 있음 & touch_price NULL 20건 | 전부 id ≤ 57(07-23대) | 레거시 |
| 판정창 ≠ 타임프레임 파생값 26건(터치) | 전부 07-23~07-31 수집, raw_text 없음 | 레거시 |
| 미종결 장기 적체 | 미종결 61건 중 판정창 초과 **0건**. 34건이 720h 창(1D+ TF, 최장 경과 706h) | 설계(30일 창) |
| resolved_at < touched_at, 종결 후 status ≠ touched | 0건 | 무결 |
| TP → 터치 역순, 터치 없는 TP, 터치 중복, TP 번호 건너뜀, 종결 뒤 TP, TP > best/idx | 전부 0건. 터치 없는 TP 518/519는 터치 알림이 30일 보존으로 삭제된 것 | 무결 |
| 클러스터 형제 hit/miss 동시(±30분, 엔트리 1% 이내) | 0건 | 무결 |
| MFE/MAE 수리 이후(09-22~) 종결·부분 TP 20행 모순(hit인데 MFE < TP 거리 등) | 0건 | 수리 유효 |
| 브리핑 get_tp_hits_since의 tp_total ≠ 판정 사다리 | 0/64 | 무결 |
| ndjson 원장 ↔ alerts_log(7일) | 양방향 누락 0, 중복 0 | 무결 |

## 잠재 위험 [추측]
- `get_tp_hits_since`는 `sent_at > last_morning_brief_at` 조건을 씀. price_check 회차의 now(sent_at)가 브리핑 now보다 앞서는데 커밋이 브리핑 조회보다 늦으면 해당 TP가 영구히 빠짐. 두 잡이 동시에 돌 때만 생기는 문제라 빈도는 미확인임.
- 브리핑은 코인당 1행으로 접음. 별개의 터치 알림 2건(OP 728·750, 09-16/09-17)도 한 줄로 합쳐짐. 표시 정책 문제이며 오류는 아님.
- `alerts_log` 보존 30일 + 원장 보존 7일 vs 720h 판정창. 창 끝 무렵 TP는 `touch_alert_sent`가 거짓 음성을 낼 수 있어 브리핑 기록이 누락될 수 있음. 현재 실측 영향은 0건임.
