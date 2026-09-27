# S1: 무장 관용 범위 수리(P0-3) + best_tp_hit 기록·백필(P2-13)

2026-09-27 · 개발자B · origin/main 워크트리(wt_s1b), 커밋 없음(CTO 병합 대기)

변경 파일: `monitor/price_check.py`, `storage/db.py`, `config/settings.py`(키 1개 추가), `scripts/test_price_logic.py`

---

## 1. 무장 관용 범위 (감사 P0-3 / A-T2)

### 결함
신규 레벨도 INSERT 시 `armed=NULL` 이라 `_first = armed is None` 경로를 항상 탔다. 그래서 "레거시 관용 2%"가 **모든 신규 레벨**에 적용됐다. 진입가 0~2% 아래에서 수집된 글이 `armed_at=collected_at` 으로 무장한 뒤 즉시 터치됐다(147건, 발송 30건).

### arming_since 값과 출처
- **확정값: `1790473697.0` = 2026-09-27 10:48:17 KST**
- 출처: 운영 DB에 armed 가 처음 기록된 회차. data 커밋 이력을 차례로 확인했다.
  - `bf84af80b`(10:46:13 KST, 무장 배포 커밋) 시점 DB: `armed` 컬럼 없음
  - 바로 다음 data 커밋 `dbfe55dde`(01:49:14Z): armed 기록 42건, `meta.last_cycle_at = 1790473697.16`
- `MIN(armed_at)`은 기준으로 쓸 수 없다. 첫 판정 무장은 armed_at=collected_at 이라 MIN 값이 09-20 수집 시각으로 나온다.
- 런타임 동작(`price_check._arming_since`): `meta.arming_since` 가 있으면 그 값을 쓴다. 없으면 settings `watch_arming_since_ts` 를, 그것도 없으면 now 를 meta 에 1회 기록한다. 운영 DB에는 이 meta 가 아직 없으므로 배포 첫 회차에 `1790473697.0` 이 기록된다.
- 운영 DB 사본 기준으로 활성(watching/previewed) 레벨 중 `armed IS NULL` 은 0건이다. armed NULL 행 843건은 모두 종결·만료된 행이고, 이 중 가장 늦은 collected_at 은 1790469800(09:43 KST)으로 기준선보다 앞이다. 따라서 **운영에서 레거시 관용 경로는 사실상 닫혀 있다.** 앞으로 들어오는 신규 레벨은 모두 관용 0 규칙을 받는다.

### 관용 규칙표 (롱 기준, 숏은 부등호 반대)

| armed | 조건 | 결과 armed | armed_at |
|---|---|---|---|
| NULL, collected_at < since (레거시) | current ≥ entry | 1 | collected_at (소급 터치 보존) |
| NULL, 레거시 | entry×(1−tol) ≤ current < entry (**관용 무장**) | 1 | **now** (신규, A-T2 ②) |
| NULL, 레거시 | current < entry×(1−tol) | 0 | NULL |
| NULL, collected_at ≥ since (신규) | current ≥ entry | 1 | collected_at (09-27 설계 유지) |
| NULL, 신규 | current < entry | **0** (신규, 관용 없음) | NULL |
| 0 | current ≥ entry | 1 | now (종전 그대로) |
| 1 | — | 유지 | 유지 |

- 참고: 관용 무장 레벨은 현재가가 이미 진입가 아래에 있다. `_eff_low` 가 항상 current 를 포함하므로 **같은 회차에 터치가 난다.** 이는 레거시 관용의 원래 설계("이미 감시 중이던 정상 대기 레벨 보호")와 같다. armed_at=now 는 무장 전 캔들 저가가 터치 앵커나 관통 측정에 소급되는 것만 막는다(ARM9).
- 로그 꼬리표(CTO 추가 요청): `" (레거시 관용 2.0%, armed_at=now)"` 는 실제로 관용 구간에서 무장한 경우(`_by_tol`)에만 붙는다. 종전에는 첫 판정이면 무조건 붙어서, SOL 164,500 vs 88,335 같은 건에도 찍혔다(ARM11).

## 2. best_tp_hit NULL (감사 P2-13 / B-E1)

### 수리
`_judge_outcomes` 의 miss 경로와 timeboxed_* 경로에서 `resolve_outcome(..., best_tp_hit=(_tp_alert_idx if _tp_alert_idx > 0 else None))` 을 넘긴다. `tp_alert_idx` 는 이미 도달한 TP 개수(다음에 감시할 TP 인덱스)다.

### 해시 영향: 없음
`_compute_outcome_hash` 의 입력은 `(prev, id, outcome, resolved_at, r_multiple, ambiguous)` 뿐이고 best_tp_hit 는 들어가지 않는다. 따라서 신규 기록과 백필 모두 체인에 영향이 없다. 백필은 best_tp_hit 컬럼만 UPDATE 하고 해시를 다시 계산하지 않는다.

### 백필
- 구현: `db._backfill_best_tp_hit`. `_migrate` 끝에서 호출되고 `meta.backfill_best_tp_hit_v1` 가드가 있어 DB당 1회만 돈다. meta 값은 `{"at", "rows"}` 이다.
  ```sql
  UPDATE levels SET best_tp_hit=tp_alert_idx
  WHERE outcome IN ('miss','timeboxed_win','timeboxed_loss')
    AND best_tp_hit IS NULL AND tp_alert_idx > 0
  ```
- 불변 스냅샷 원칙의 **문서화된 예외**다. outcome·r_multiple·resolved_at·해시는 건드리지 않는다.
- **사본 검증**: 메인 클론 `data/levels.db`(2026-09-27 11:21) 를 스크래치패드로 복사한 뒤 `init_db` 를 실행했다. 원본에는 쓰지 않았다.
  - 변경 행: **62건** (감사 E1 수치와 일치)
  - 분포: miss 7 (TP1 5·TP2 2) / timeboxed_loss 20 (TP1 15·TP2 5) / timeboxed_win 35 (TP1 15·TP2 11·TP3 3·TP4~8 각 1~2)
  - outcome_hash 변경 행 0, `verify_outcome_chain` 은 전후 모두 None(정상)
  - 두 번째 `init_db` 실행: 가드에 막혀 no-op (meta rows=62 유지)

### best_tp_hit 소비처 (grep 결과)
- 운영 코드(`*.py`, archive 제외): **소비처 없음.** 기록하는 곳은 `resolve_outcome` 뿐이다. 아침 브리핑·주간 리포트·show_status 는 best_tp_hit 를 읽지 않는다. 브리핑의 TP 도달 집계(`get_tp_hits_since`)는 alerts_log 의 tpN 을 기준으로 한다.
- `storage/db.py:279` 컬럼 주석 "도달한 최고 TP 차수 (v1은 1만 사용)"는 이미 낡은 설명이다. 수정은 선택 사항이다.
- 영향이 있는 곳:
  - `data/audit/levels_YYYY-Www.ndjson` 주간 감사 덤프: 다음 주 덤프에서 62행이 best_tp_hit 컬럼 차이로 diff 에 잡힌다. 백필에 따른 예상된 차이다.
  - 분석 문서(research_2026-09-13/17/22, audit_2026-09-13_best_tp_hit.md)의 "best_tp_hit = hit 전용" 전제와 TP2 급감 해석은 백필 이후 다시 읽어야 한다. 이제 `best_tp_hit IS NOT NULL` 은 "TP1 이상 도달"을 뜻하고 "적중"을 뜻하지 않는다. 적중은 `outcome='hit'` 으로 판단한다.
  - `scripts/archive/repair_*` 는 과거 1회성 스크립트라 영향이 없다.

## 3. 테스트

- 메인 TEST_DB 에 `meta.arming_since = 9e12` 를 심었다. T*/RS*/LG* 구세대 픽스처는 "진입가 바로 아래에서 이미 감시 중이던 레벨"을 전제로 하므로 레거시로 취급한다(EI8 의 armed=1 심기와 같은 취지). 이렇게 하지 않으면 T7·T26~28·T33b·T34·RS1/3/4·LG1 이 실패한다. 이 실패는 픽스처가 결함 경로에 기대고 있었기 때문이다.
- ARM5: 기대값을 바꿨다(관용 무장 armed_at=collected_at → **now**). 지시된 규칙을 반영한 것이다.
- 신규 테스트:
  - ARM7: 신규 −1% 수집 → armed=0, 터치·알림 없음
  - ARM8: 신규 current≥entry → armed_at=collected_at, 소급 터치·발송 유지
  - ARM9: 레거시 관용 무장 → armed_at=now, touched_at 소급 없음
  - ARM10: arming_since meta 기록·유지·now 폴백
  - ARM11: 로그 꼬리표
  - BTH1: miss 시 best=1
  - BTH2: timeboxed 시 best=2, idx=0 이면 NULL
  - BTH3: idx=0 miss → NULL, 체인 정상
  - BTH4: 백필 2행만 갱신, 가드로 1회성, 판정값·해시 불변
- 변이 검증: 수리를 되돌린 코드로 돌리면 ARM7·BTH1·BTH2 가 실패한다. 테스트가 결함을 실제로 잡는다.
- 실행 결과(순차, 포그라운드, PYTHONIOENCODING=utf-8): test_price_logic 0 · test_touch_recording 0 · test_cycle 0 · test_infra 0 · test_weekly_report 0 · test_morning_brief 0 · test_show_status 0. 실행 후 `git checkout -- data` 로 되돌렸다.

## 4. 매뉴얼 반영 필요 (CTO)
`alert_pipeline_manual.md` 게이트 0(무장)에 다음 내용을 반영해야 한다. 이번 작업에서는 매뉴얼을 직접 수정하지 않았다.
1. 관용은 `collected_at < meta.arming_since(=1790473697)` 인 행에만 적용한다.
2. 관용 무장은 armed_at=now 로 기록한다.
3. 신규 행의 첫 판정은 current≥entry 를 요구한다.
4. best_tp_hit 의 의미가 "도달한 최고 TP 단계(종결 유형 무관)"로 바뀌었고, 62행을 백필했다.
