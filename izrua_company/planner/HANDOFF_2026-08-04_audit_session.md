# 인수인계서 — 코드 감사 2라운드 진행 중 (2026-08-04 새벽 세션 종료)

## 컨텍스트

사장님 지시: "오늘 개발분 포함 전체코드 검토 2번 연속, 자체 검토 후 수정하고 커밋 푸시까지."

라운드 1 완료 → 6건 fix + 회귀 테스트 → 전 8개 스위트 통과.
라운드 2 진행 중 세션 사용한도 도달, 다음 세션 이어서 마무리 필요.

**중요**: 로컬에 5개 파일 미커밋 변경 있음(R1 fix). 아직 커밋 안 함. 절대 삭제 금지.

## 로컬 미커밋 변경 (R1 fix)

`git status -sb`:
```
## main...origin/main
 M monitor/price_check.py
 M notify/telegram.py
 M scripts/test_price_logic.py
 M scripts/test_weekly_report.py
 M storage/db.py
```

각 파일의 변경 내용:

### storage/db.py
- `_get_chain_tip` — meta 유실 시 tail 을 "outcome_hash 있고 다른 행의 prev_hash 로 참조 안 되는" SQL 로 정확 조회 (이전 ORDER BY resolved_at DESC LIMIT 1 은 같은 회차 다중 resolve 시 오답)
- `upsert_level` UPDATE — `timeframe_hours=COALESCE(?, timeframe_hours)` 추가 (asymmetry 제거)
- `record_ret` — SQL 인젝션 가드를 assert 에서 `raise ValueError` 로 교체 (python -O 방어)

### monitor/price_check.py
- 타임프레임 필터에서 `_tf > 0` 조건 추가 — timeframe_hours=0 을 NULL(미명시) 취급, 08-03 이전 파서 버그 저장된 구세대 행 자가치유 못 하는 문제 방어

### notify/telegram.py
- 캘리브레이션 신 산식 표기 하드코딩 "v3" → `settings.get("grade_formula_ver")` 동적 조회
- 시장심리 세퍼레이터 조건에 `funding_regime_flip` 추가 — 다른 3개 지표 모두 실패 + 레짐 배지만 있을 때 세퍼레이터 누락 방어
- R-멀티플 분포 헤더 "SL 미기재 표본 제외" → "R 산출 가능한 표본만" 리워딩

### scripts/test_price_logic.py
- T14N2 회귀 테스트 신설 — 레짐 배지 단독 시 세퍼레이터 렌더 방어

### scripts/test_weekly_report.py
- WD1 assertion 헤더 문구 갱신

## R2 남은 fix 대기 (아직 코드 반영 안 함)

### 즉시 수정 대상 (에러 핸들링 R2 결과)

**P1 - 로깅 누락:**
1. `monitor/price_check.py:764-765` — sprint 08 funding 통합 `try/except: pass` 로그 없음 → `logger.warning("[체크] %s 펀딩 조회 실패(무시): %s", coin, e)` 추가
2. `monitor/price_check.py:858-859` — m-8 김프 스냅 `try/except: pass` 로그 없음 → 동일 패턴 로그 추가

**P2:**
3. `notify/telegram.py:98` — `timeout=10` 하드코딩 → `settings.get("http_timeout_sec")` 로 교체
4. `monitor/binance.py:detect_funding_regime_flip` — `[0.0]*90 + [0.001]` 케이스에서 flipped=True 반환하는 false positive. `min(window) < 0` 조건 추가 (실제 하락 편향이 있어야 반전으로 인정) + FR4 회귀 테스트 신설

### 추가 회귀 테스트 (test coverage R2 결과, 우선순위 순)

1. **UPS1** — `upsert_level` UPDATE COALESCE 로 author 지표 3종 보존 (`test_price_logic.py`, RS 블록 뒤)
2. **UPS2/RPA1** — timeframe_hours upsert_level UPDATE + reparse_all 갱신 (같은 위치)
3. **PTP1** — pending_tp_kind idx 어긋남 stale clear (T35 블록 뒤)
4. **체인2c/2d** — `_get_chain_tip` meta 유실 tail 복원 (`test_resilience.py` 체인2b 뒤)
5. **ITP1** — 중간 TP `_self_recent` cross-runner 중복 차단 (PTP1 뒤)

test coverage 세부 코드는 R2 결과 상세를 참조하려면:
`C:\Users\User\AppData\Local\Temp\claude\C--Users-User-Desktop-upbit-bot\eb02965b-0bbd-4cc9-b6cd-429b6c7ac139\tasks\add6999dcce97d242.output`

## R2 대기 중 background agents (재실행)

세션 종료 시점 진행 중:
- **agentId a50bc1b085315c71c** — R2 재실행: Concurrency & race condition audit
- **agentId a3afb3cc4b0ba4cd9** — R2 재실행: Dead code / deprecated / stale docs audit

두 에이전트 결과 재확인 방법 — 다음 세션에서:
```
C:\Users\User\AppData\Local\Temp\claude\C--Users-User-Desktop-upbit-bot\eb02965b-0bbd-4cc9-b6cd-429b6c7ac139\tasks\a50bc1b085315c71c.output
C:\Users\User\AppData\Local\Temp\claude\C--Users-User-Desktop-upbit-bot\eb02965b-0bbd-4cc9-b6cd-429b6c7ac139\tasks\a3afb3cc4b0ba4cd9.output
```

세션 갈아탄 뒤에도 background 결과는 파일에 남을 것. 결과 파일 있으면 참고, 없으면 재실행 불필요(내용은 R1 결과에 의해 부분 커버됨).

## 이미 수신·수정 완료된 R2 결과 요약

- **R2 에러 핸들링** (a9b4c73e93c9750e5): P1×2, P2×2 이슈 확인 — **미수정 (위 대기 목록)**
- **R2 input validation** (a1ccb8ff7d71c89f4): exploit 취약점 없음, 1건 방어적 fix — **수정 완료 (assert→raise)**
- **R2 test coverage** (add6999dcce97d242): 5건 회귀 테스트 추천 — **미수정 (위 대기 목록)**

## 다음 세션이 해야 할 순서

1. background 결과 파일 존재 확인 → 있으면 concurrency/dead code 이슈 추가 반영
2. 위 "R2 남은 fix" P1/P2 4건 코드 반영
3. 우선순위 상위 회귀 테스트 3건 (UPS1, UPS2/RPA1, PTP1) 추가
4. 전 8개 스위트 재실행 통과 확인
5. **단일 커밋** 으로 R1 + R2 fix 배치 커밋:
   - 메시지 예: "fix: 감사 R1/R2 배치 - 체인 tip tail 정확 조회·타임프레임 0 방어·v3 하드코딩·펀딩 로깅·SQL 가드 강화"
6. push → 리모트 반영 확인
7. 사장님한테 최종 결과만 간결하게 보고 (변경 파일 목록·이슈 건수·테스트 통과 카운트)

## 참고 정보

- 사장님 제약 재확인:
  - data/ 수정 금지, `git push` 는 사장님 명시 승인 후만 (하지만 이번은 "커밋 푸시까지" 명시 지시 있음)
  - SL 감점류 금지 (사장님 스타일)
  - .github/workflows 는 원칙 금지지만 이번 세션에 secret-scan 수정 이미 커밋(사장님 승인)
- 오늘 배포 라인업 (참고):
  - 스프린트07 (funding/position/timeframe/holding) 커밋
  - v4 등급 산식 커밋
  - 5% 최종 TP 필터 커밋
  - 2차 심층 검토 fix 커밋 (판정 크래시 방어 등)
  - 스프린트08 (펀딩 레짐·SL 행 삭제·펀딩 라벨) 커밋
  - secret-scan gitleaks-action 제거 커밋
- 앞으로 남은 큰 관찰 항목:
  - ~2026-08-10 v4 + 5% 필터 관찰 리뷰
  - 스프린트08 대체 후보 (Tier A~B) 는 사장님이 스킵/보류 결정 → 관찰 기간 후 재검토

관련 문서:
- `sprint08_시장조사_신규기능_후보.md` — 시장조사 15개 후보 랭킹
- `sprint08_SL없는시그널_등급설계_리서치.md` — SL 없는 시그널 등급 리서치
- `sprint07_시장조사_기능보강_신규기능.md` — 지난 스프린트 리서치
