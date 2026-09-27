# 즉시터치 버그 수리 — 무장(arming) 상태 + 관통 깊이 게이트

2026-09-27 · 개발자A · 대상 브랜치: origin/main 워크트리 (커밋 없음, CTO 검토 대기)

---

## 1. 버그 (확정)

터치 판정 조건이 **하나뿐**이었다.

```
터치 = 캔들 저가(수집 이후 구간) ≤ 진입가 상단
```

이 조건은 "가격이 **위에서 내려와** 진입가에 닿았다"를 표현하려는 것인데, 실제로는
**수집 시점에 이미 현재가가 진입가 아래인 롱 레벨**도 통과시킨다. 그런 레벨은 수집
2분 뒤 첫 회차에서 곧바로 '터치'로 판정되고 알림이 나갔다.

전형은 **돌파 트리거 글**이다. `$1.42 BREAKOUT` 은 1.42 를 **위로 뚫으면** 사라는
뜻인데, 현재가가 $1.17 이면 판정부는 "저가 1.17 ≤ 1.42" 로 읽는다.

- 실사례: SUI id=884 — 진입 1,933원 vs 현재 1,590원, 관통 **17.5%** 로 발송
- 운영 DB 실측: 즉시터치(<10분) & 관통>10% **33건**, 발송 **14건**,
  종결 승률 **4.3%(1/23)** ↔ 정상 터치(관통≤2%) 승률 **60.9%**

사용자 결정: **①무장 상태 + ②관통 게이트 동시 적용** (두 겹).

---

## 2. ① 무장(arming) 상태

### 규칙

롱 레벨은 **현재가가 진입가 위에 있는 것을 한 번 확인(=무장)한 뒤에만** 터치·예고
판정 대상이 된다. 그러면 터치의 원래 의미("위에서 내려와 닿았다")가 회복되고, 돌파
트리거 글은 실제로 돌파할 때까지 기다린다.

| 현재 `armed` | 조건 | 결과 |
|---|---|---|
| `NULL`(미판정) | `current >= entry * (1 - tol)` | `armed=1`, `armed_at = collected_at` |
| `NULL` | 그 외 | `armed=0`, `armed_at = NULL` |
| `0`(대기) | `current >= entry` (**관용 없음**) | `armed=1`, `armed_at = now` |
| `1` | — | 손대지 않는다 (되돌림 없음) |

short 는 대칭(`current <= entry`, 관용은 `entry * (1 + tol)`). 현재
`collect_short_enabled=False` 라 표본이 없지만, 켜질 때 같은 버그가 반대 방향으로
재발하지 않도록 로직은 지금 넣어 뒀다.

### `armed_at` 을 두 갈래로 나눈 이유 (설계 핵심)

`armed_at` 은 터치 판정 캔들의 **하한**이 된다(`_arm_floor`). 그래서 값 선택이
동작을 바꾼다.

- **첫 판정(NULL → 1)** = `collected_at`.
  이 레벨은 현재가가 진입가 위에 있는 **정상 대기** 레벨이다. 무장 조건을 수집
  시점부터 만족했다고 보고 `collected_at` 을 쓴다 → 터치 판정 축이 **종전과 완전히
  동일**해진다. `now` 를 쓰면 신규 레벨마다 수집 후 첫 회차의 소급 구간을 통째로
  잃는다(정상 레벨의 검출력 하락).
- **승격(0 → 1)** = `now`.
  이 레벨은 **진입가 아래에 있었음이 기록으로 확정**돼 있다. 그 시절 저가로 터치되면
  고치려던 버그가 한 회차 늦게 재현되므로 반드시 `now` 다. (회귀 ARM4)

### 레거시 관용 (`watch_arm_tolerance_pct = 2.0`)

`armed IS NULL` 인 **첫 판정에만** 적용한다. 배포 첫 회차에 진입가 바로 아래에서
정상 대기(예고 밴드 체류 등)하던 레벨까지 `armed=0` 으로 잠그면 정상 대기를 벌주는
셈이다. `0 → 1` 승격에는 관용이 없다 — 있으면 진입가 2% 아래에서 '무장'해 버그가
축소 재발한다.

### 백필

**별도 스크립트 없음.** 배포 첫 회차가 곧 백필이다 — `armed IS NULL` 인 기존 행이
위 표 그대로 자동 판정된다.

배포 시점 실측(2026-09-27, 운영 DB `mode=ro` + `upbit.fetch_prices` 1회 조회):

```
활성 롱 레벨 42건 / USDT-KRW 1,360
  무장(current >= entry)        42
  레거시 관용으로 무장            0
  armed=0 예상                   0   ← 배포는 기존 행에 무해(no-op)
  시세·진입가 없음                0
```

즉 **배포 즉시 잠기는 레벨은 0건**이다. 게이트는 앞으로 새로 수집되는 돌파 트리거
글에서부터 작동한다.

---

## 3. ② 관통 깊이 게이트 (안전망)

①이 새더라도(스위치 OFF, 수집 경로 변경, 미래의 다른 경로) **알림 직전에** 한 번 더
끊는다.

```
pen = (진입가상단KRW - 현재가) / 진입가상단KRW × 100
pen > alert_touch_max_penetration_pct(기본 10.0)  →  알림 억제
```

- 기준이 **현재가**인 이유: `touch_penetration_pct`(`_touch_quality`)는 완성 캔들만
  인정해 실시간 터치에서 대부분 `NULL` 이다 — 게이트 입력으로 쓰면 항상 통과한다.
- 커트 10.0 근거: 정상 터치 관통은 ≤2% 에 몰려 있고 오염 집단은 >10%(실측 33건).
  사이 구간은 표본이 얇아 커트 위치가 탐지력을 거의 바꾸지 않으므로, 정상 급락
  터치를 죽이지 않는 느슨한 쪽을 택했다.
- **터치 기록·판정·MFE 는 그대로.** 알림만 끈다.

억제 경로는 2026-09-22 `no_tp` 게이트와 **완전히 동일한 관례**다.

```
send_ok = False
summary["suppressed_deep_touch"] += 1
db.record_alert(conn, coin, "touch_deep", ids, day, now, sent=0)
logger.info("[체크] … 관통 %.1f%% > %.1f%% - 알림 억제(touch_deep) …")
```

`kind` 를 `'touch'` 가 아니라 `'touch_deep'` 으로 두는 이유: `'touch'` 로 쓰면 일일
상한(`count_alerts_today`)·재발송 차단·TP 단계 게이트(`touch_alert_sent`)가 이 행을
'발송된 본알림'으로 오인해, 억제된 신호가 같은 코인의 정상 알림 슬롯을 잡아먹는다.

---

## 4. 배선 위치 (파일:함수)

| 무엇 | 위치 |
|---|---|
| 컬럼 `armed` / `armed_at` | `storage/db.py` `_EXTRA_COLUMNS` (기존 `touch_message_id` 와 동일한 ALTER 마이그레이션 패턴 — `_migrate` 가 자동 추가) |
| 상태 기록 | `storage/db.py:set_armed(conn, rows, armed)` — `rows=[(id, armed_at|None)]`, 활성(`watching`/`previewed`) 행만 갱신 |
| 무장 판정 루프 | `monitor/price_check.py:run_once` — **진입가 sanity 만료 뒤, 클러스터 구성 앞** |
| 무장 필터 | 같은 자리에서 `by_ticker` 를 `armed == 1` 만으로 재구성 |
| 캔들 하한 | `monitor/price_check.py:_arm_floor(lv)` = `max(collected_at, armed_at)` |
| ↳ 사용처 3곳 | `_eff_low`(터치 검출) / 터치 앵커 `t_anchor` 탐색 루프 / `_touch_quality` 호출 |
| 관통 게이트 | `monitor/price_check.py:run_once` — **`no_tp` 게이트 바로 다음, 일일 상한 앞** |
| 설정 | `config/settings.py`: `watch_arming_enabled`, `watch_arm_tolerance_pct`, `alert_touch_max_penetration_pct` |

### 위치가 중요한 이유

- **sanity 뒤**: 이미 만료된 오염 레벨에 무장 상태를 찍을 이유가 없다.
- **클러스터 앞**: 무장 안 된 레벨이 클러스터에 섞이면 그 레벨의 엔트리가 상단
  (`top_krw`)이 되어 **클러스터 전체의 터치 판정 기준**을 오염시킨다.
- `armed=0` 레벨은 감시 상태(`watching`/`previewed`)를 그대로 유지하고, 타임프레임
  만료(`level_expiry_hours`)도 종전대로 동작한다 — 이번 회차 판정에서만 빠진다.

---

## 5. ③ 분석 오염 제거 (조회 조건만 — 소급 수정 없음)

`storage/db.py` 상수:

```python
STALE_TOUCH_COND = ("(COALESCE(touch_penetration_pct, 0) > 10 "
                    "AND (COALESCE(touched_at, 0) - COALESCE(collected_at, 0)) < 600)")
NOT_STALE = "NOT " + STALE_TOUCH_COND
```

**`COALESCE` 는 설계안의 평문 비교에서 의도적으로 보강한 것이다.** SQL 3값 논리에서
`touch_penetration_pct` 가 `NULL` 이면 `NULL > 10` = `NULL` → `NOT (NULL AND TRUE)`
= `NULL` → **그 행이 조회에서 조용히 사라진다**. 관통 깊이는 억제 터치·백필 대기·
구세대 행에서 흔히 NULL 이라, 보강 없이 붙이면 오염 33건을 빼려다 정상 표본 수백
건을 날린다. `NULL` 은 "오염 아님"(fail-open)으로 읽는다. 회귀 STALE2 가 이걸 못박는다.

조건은 **AND** 다 — 관통이 깊어도 느린 터치(뉴스 급락 등)는 정상 표본이다(STALE3).

### `AND NOT (…)` 를 붙인 조회 (총 7곳)

| 함수 | 파일 | 사유 |
|---|---|---|
| `get_author_outcome_rows` | `storage/db.py` | 작성자 랭킹(E_LB) 원천 |
| `author_closed_stats` | `storage/db.py` | **등급 가점의 입력** — 오염 승률이 알림 판정에 실린다 |
| `get_closed_r_rows` | `storage/db.py` | R-멀티플 분포 |
| `get_closed_holding_rows` | `storage/db.py` | 보유기간 분포 |
| `get_resolved_rows_between` | `storage/db.py` | 주간 리포트 승률·PF·등급표 |
| `get_weekly_calibration_rows` | `storage/db.py` | 등급 캘리브레이션(주간) |
| `fetch_calibration_rows` | `scripts/show_status.py` | 등급 캘리브레이션(상태 조회) — `db.NOT_STALE` 재사용 |

그 밖의 SQL 은 건드리지 않았다. 특히 **행 자체는 소급 수정하지 않는다**(원천 보존
원칙 — 과거 알림이 왜 나갔는지의 근거를 지우지 않는다).

---

## 6. 설정

| 키 | 기본값 | 의미 |
|---|---|---|
| `watch_arming_enabled` | `True` | 롤백 스위치. `False` 면 전 레벨을 무장 취급 = 종전 동작 |
| `watch_arm_tolerance_pct` | `2.0` | 레거시 관용(%) — `armed IS NULL` 첫 판정에만 |
| `alert_touch_max_penetration_pct` | `10.0` | 관통 게이트 커트(%). `0` 이하면 게이트 OFF |

---

## 7. 테스트

`scripts/test_price_logic.py` 신규 블록 (13건, 전부 통과):

| ID | 계약 |
|---|---|
| ARM1 | 수집 시 `current < entry` → `armed=0` 대기, 터치 안 됨, 알림 0건 |
| ARM2 | 이후 `current >= entry` → `armed=1`, 승격 `armed_at = 그 회차 시각` |
| ARM3 | 무장 후 내려와 닿으면 터치 + 본알림 (정상 경로 복귀) |
| ARM4 | **무장 전 캔들 저가는 무시** (하한 = `max(collected_at, armed_at)`) |
| ARM5 | 레거시 관용 — `NULL` & 진입가 -1%(밴드 내) → `armed=1`, `armed_at=collected_at` |
| ARM6 | 스위치 OFF → 완전한 종전 동작(즉시 터치 + 발송), `armed` 미기록 |
| DEEP1 | 관통 12% → 알림 억제 + `alerts_log kind='touch_deep' · sent=0` |
| DEEP2 | 관통 3% → 종전대로 발송 (정상 터치를 죽이지 않는다) |
| DEEP3 | 억제돼도 터치 기록·재채점 판정은 그대로 진행 |
| DEEP4 | 무음 기록은 `'touch'` 가 아니다 — 일일 상한·재발송·TP 게이트 무손상 |
| STALE1 | 즉시터치 1건이 6개 분석 조회에서 전부 빠진다 |
| STALE2 | 관통 `NULL` 행은 배제되지 않는다 (3값 논리 함정 — `COALESCE` 방어) |
| STALE3 | 조건은 `AND` — 깊지만 느린 터치는 정상 표본 |

### 기존 회귀 1건 픽스처 보강 — EI8

`EI8`(2026-09-14 감사 F2, 커트 60→300 의 존재 이유)의 `crash` 픽스처는 "회차가 길게
끊긴 사이 가격이 진입가 아래로 반토막 난 **정상 대기 레벨**" 을 뜻한다. 정상 대기였다는
말은 곧 급락 **전에** 현재가가 진입가 위에 있었다는 뜻이고, 그 시점에 무장이 이미 끝나
있다. 그래서 픽스처에 `armed=1, armed_at=collected_at` 을 심었다.

심지 않으면 그 픽스처는 EI8 이 말하려는 상황이 아니라 **무장 게이트가 잡아야 하는
바로 그 버그**(수집 시점에 이미 진입가 아래)를 재현하게 된다. 그 반대 케이스는 ARM1 이
따로 못박는다. **EI8 의 계약(정상 급락은 만료가 아니라 터치) 자체는 불변이다.**

### 회귀 실행 결과 (순차·포그라운드, `PYTHONIOENCODING=utf-8`)

| 스크립트 | exit |
|---|---|
| `scripts/test_price_logic.py` | 0 |
| `scripts/test_touch_recording.py` | 0 |
| `scripts/test_cycle.py` | 0 |
| `scripts/test_infra.py` | 0 |
| `scripts/test_weekly_report.py` | 0 |
| `scripts/test_ranking.py` | 0 |

`git status` 는 의도한 5개 파일만 수정(`data/` 는 `git checkout -- data` 로 원복).

---

## 8. 롤백 카드

| 되돌릴 것 | 방법 |
|---|---|
| ① 무장 게이트 | `watch_arming_enabled: False` — 전 레벨 무장 취급, 종전 동작. 컬럼·기록은 남되 판정에 쓰이지 않는다 |
| ① 관용 폭만 | `watch_arm_tolerance_pct` 조정 (`0` = 관용 없음) |
| ② 관통 게이트 | `alert_touch_max_penetration_pct: 0` — 게이트 OFF |
| ③ 오염 배제 | `storage/db.py` 의 `NOT_STALE` 정의를 `"1=1"` 로 바꾸면 7개 조회가 한 번에 종전 표본으로 돌아간다 |

컬럼 `armed`/`armed_at` 은 ALTER 추가라 되돌릴 필요가 없다(스위치 OFF 면 무해한 기록).
`alerts_log` 의 `touch_deep` 행도 `sent=0` 무음 기록이라 어떤 상한·차단에도 잡히지 않는다.

---

## 9. 남은 이슈 / 후속 관찰

- **배포 첫 회차 소급 구간(승격 경로)**: `0 → 1` 승격 회차는 `armed_at = now` 라 그
  회차의 소급 캔들 구간(최대 ~2분, 러너 지연 시 더 길 수 있음)에서 일어난 터치를 놓친다.
  의도된 보수적 선택이다 — 그 구간은 레벨이 무장 상태가 아니었다.
- **`touch_deep` 발생량 관찰**: ①이 정상 작동하면 ②는 거의 발동하지 않아야 한다.
  `summary["suppressed_deep_touch"]` 가 꾸준히 올라가면 ①을 우회하는 경로가 남아 있다는
  신호다. (`daily_stats` 카운터로는 올리지 않았다 — `bump_daily_stats` 컬럼 추가가
  필요하고, 우선 로그·summary 로 관찰한다.)
- **`armed=0` 체류 관찰**: 돌파 트리거 글이 실제로 얼마나 무장에 도달하는지(=대기만
  하다 7일 만료되는 비율) 표본이 쌓이면 볼 것.
- `alert_pipeline_manual.md` 는 손대지 않았다 — 단계 5(터치 판정)와 단계 6(게이트)에
  이 두 게이트를 넣는 반영은 CTO 몫.
