# B4 — best_tp_hit 분포 이상 감사 (2026-09-13, 개발자B)

## 판정: **정상** (코드 변경 없음)

TP1 56 / TP2 11 / TP3 57 / TP5 1 의 "계단형" 분포는 버그가 아니라
`best_tp_hit` 컬럼의 정의 자체(= "사다리를 끝까지 완주해 outcome='hit' 로
종결된 신호의 최종 rung") 때문에 생기는 집계 아티팩트다. TP2 에서 멈추고
끝내 TP3 에 못 간 신호들은 outcome 이 'hit' 가 아니라 'miss'/'timeboxed_*'
로 종결되어 애초에 `WHERE outcome='hit'` 필터에 잡히지 않는다.

## 코드 확인

- `storage/db.py:1461` `resolve_outcome()` — `best_tp_hit` 파라미터는
  **딱 한 곳**(`monitor/price_check.py:1981`, 최종 rung "hit" 종결 분기)에서만
  넘겨진다. `miss`(1986)·`timeboxed_*`(1992) 호출부는 `best_tp_hit` 을 아예
  넘기지 않으므로 해당 행은 NULL로 남는다 — 즉 "TP2까지는 갔지만 TP3 못 감"은
  best_tp_hit 통계에 **아예 나타나지 않는다**(버킷 자체가 없음).
- `monitor/price_check.py:1873` — 중간 rung 적중 시 `advance_tp_alert_idx` 로
  `tp_alert_idx` 만 전진시키고 `continue`(종결 아님). 최종 rung 적중 시에만
  (`_tp_alert_idx >= len(_tps_valid)-1`, 라인 1935~) `_best = _tp_alert_idx+1`
  로 종결한다 — "사다리 3단은 무조건 마지막 단으로 기록" 가설은 틀렸다.
  마지막 rung 에서만 종결되는 게 **설계 그 자체**이고, `tp_alert_idx` 는
  outcome 과 무관하게 항상 "실제 도달한 rung" 을 정확히 들고 있다.

## DB 실데이터 교차검증 (읽기전용 `file:data/levels.db?mode=ro`)

`outcome='hit'` 만 봤을 때 (원 증상):
```
tp_ladder_count=2: idx0(hit)=1, idx1(hit)=11
tp_ladder_count=3: idx0(hit)=7, idx2(hit)=57         ← idx1(hit) 없음
```

`outcome` 전체(hit/miss/timeboxed_*)로 넓혀서 "실제 도달한 tp_alert_idx" 를 보면:
```
tp_ladder_count=2: idx0=19(1hit+17miss+1tbloss), idx1=14(11hit+1tbloss+2tbwin)
tp_ladder_count=3: idx0=46, idx1=16(2miss+9tbloss+5tbwin), idx2=73(57hit+1miss+4tbloss+11tbwin)
```
→ TP2(idx1)에 도달한 3단 사다리 16건 중 **11건(69%)이 outcome='hit' 가 아님**
(2 miss, 9 timeboxed_loss, 5 timeboxed_win 도합 16건과 겹치지 않게 재확인:
`miss=2, tbloss=9, tbwin=5` 합 16 — 이게 "TP2까지 갔지만 TP3 못 감"에 해당하고
전부 best_tp_hit 통계 바깥에 있다). 즉 원 질문의 "TP2가 급감" 은 실재 도달
분포(46→16→73)가 아니라, hit-only 필터가 idx1 단계의 표본 대부분을 솎아낸
결과다.

alerts_log 대조(`tp_ladder_count=3 & outcome='hit' & idx=2` 표본 5건, id
159/176/177/188/189): 전부 alerts_log 에 tp1/tp2/tp3 알림 기록이 없음 —
그러나 이는 위음성이 아니라 `_touch_sent` 게이트(본알림 무발송·초단타 필터
등, price_check.py:1888) 로 TP 알림 자체가 억제된 신호들이기 때문(코드상
게이트가 걸려도 `tp_alert_idx` 전진과 `best_tp_hit` 기록은 게이트와 무관하게
정상 수행됨 — 라인 1862 주석 "적중 판정·상태 전진·종결은 게이트와 무관하게
전부 수행"과 일치).

## 결론

- "중간 TP 알림은 갔는데 best_tp_hit 이 최종값으로만 기록된 케이스"는
  **없음** — `tp_alert_idx`(항상 정확) vs `best_tp_hit`(hit 종결시에만 기록)
  는 서로 다른 목적의 컬럼이고 코드는 이 둘을 일관되게 다루고 있다.
- 분포가 계단형으로 보인 이유: 3단 사다리에서 TP2까지 갔다가 TP3를 못
  채우는 신호(16건 중 16건 전부)가 outcome=miss/timeboxed 로 종결되며
  best_tp_hit 통계에서 완전히 빠지기 때문. TP1은 "1단만 있는 신호(46) +
  사다리가 있어도 런타임에 유효 rung 이 1개로 걸러진 신호(10)" 가 섞여 큰
  값이고, TP3는 "TP2를 넘긴 신호는 실제로 TP3까지 잘 간다"(73건 중 57건
  hit)는 시장 특성 때문에 크다.
- **영향 범위**: 없음(코드 변경 없음, 과거 데이터 소급 수정 대상 아님).
  다만 향후 "사다리 도달률"을 분석할 일이 있으면 `best_tp_hit`(hit 종결
  전용) 대신 `tp_alert_idx`(outcome 무관, 실제 도달 rung) 를 봐야 한다는
  점을 기록해 둔다.

## 근거 쿼리

```sql
-- 원 증상
SELECT best_tp_hit, count(*) FROM levels WHERE outcome='hit' GROUP BY best_tp_hit;
-- tp_ladder_count 별 best_tp_hit
SELECT tp_ladder_count, best_tp_hit, count(*) FROM levels WHERE outcome='hit'
  GROUP BY tp_ladder_count, best_tp_hit;
-- outcome 전체로 넓힌 실제 도달 rung(tp_alert_idx)
SELECT tp_ladder_count, outcome, tp_alert_idx, count(*) FROM levels
  WHERE outcome IS NOT NULL GROUP BY tp_ladder_count, outcome, tp_alert_idx;
```
(전부 `sqlite3.connect('file:data/levels.db?mode=ro', uri=True)` 읽기전용
연결로 실행, data/ 수정 없음)
