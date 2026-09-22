# 거래대금 순위 감점 + TP 없는 글 알림 배제 — 구현 노트

- 작성: 2026-09-22 · 개발자B
- 근거: `plan_2026-09-22_최종버전_종합검토.md` §2-1 D1·D2, §3 R1·R3
- 사용자 결정: 거래대금 순위는 **등급 감점** 방식 · **v6 지연 감점 관찰이 끝나는
  2026-10-06 이후 활성화**(그때까지 플래그 OFF)
- 수정 파일: `collector/grading.py` · `monitor/price_check.py` · `config/settings.py`
  · `scripts/test_grading.py` · `scripts/test_price_logic.py`
  (telegram.py · weekly report · db.py 스키마 무수정. db.py 는 조회 추가도 없음)

---

## 1. 설정 키 · 기본값 · 활성화 예정일

| 키 | 기본값 | 활성화 | 의미 |
|---|---|---|---|
| `grade_volume_rank_enabled` | **False** | **2026-10-06 이후 True** | 거래대금 순위 감점 롤백/활성 스위치 |
| `grade_volume_rank_top_n` | `20` | — | 이 순위 **이하**(1~N)면 감점. 경계 20 = 감점 포함 |
| `grade_volume_rank_penalty` | **-6** | — | 감점 폭 (추천값, 아래 §2) |
| `alert_exclude_no_tp` | **True** | **즉시** | TP 없는 글(timeboxed) 터치 알림 억제 |

`alert_exclude_no_tp` 만 즉시 켠다 — 09-13 C 컷이 이미 사실상 차단하고 있어
알림량 영향이 ≈0 이라 v6 지연 감점 2주 관찰(~10-06)과 겹치지 않는다
(plan §3 순서 원칙 "알림량을 바꾸는 변경은 겹치지 않는다").

**산식 버전 태그는 v6 그대로 둔다.** 순위 감점 스위치가 OFF 인 동안 점수가
전혀 변하지 않으므로 `grade_formula_ver` 를 올리면 같은 산식에 두 태그가 생겨
캘리브레이션 축만 갈린다. 켜는 날 태그 승격 여부를 함께 판단한다.

---

## 2. 감점 폭 N 시뮬레이션

**표본**: 운영 DB(`C:\Users\User\Desktop\izrua_entry_alert\data\levels.db`, `mode=ro`)
의 **터치 종결** 행 중 `touch_volume_rank` 가 있는 전건 — **n = 214**
(1-20위 88 / 21-50위 34 / 51-100위 35 / 100위+ 57).

**기준선 재구성**: 저장된 `touch_score` 는 전부 v5 산식(212건 v5 · 2건 NULL)이라,
v6 기준선으로 맞추기 위해 `(touched_at−collected_at)/60 < 30` 인 행에 지연
감점 −6 을 덧입혔다. 등급 경계는 v6(A55/B47/C40).

### 2-1. 버킷별 기준선 (감점 0)

| 버킷 | n | 승률 | C 이상 통과율 |
|---|---|---|---|
| 1-20위 | 88 | **44.3%** | 54.5% (48건) |
| 21-50위 | 34 | 50.0% | 61.8% |
| 51-100위 | 35 | 51.4% | 68.6% |
| 100위+ | 57 | **68.4%** | 71.9% |

기획서 D2 의 방향(상위권 열위)이 종결 표본에서 그대로 재현된다.

### 2-2. 후보 비교표

| N | 1-20 C이상 | 감소율 | B→C | C→D | A→B | 그중 지연(−6) 중첩 | **잘린 건의 승률** | 100위+ 영향 | 전체 통과군 승률 |
|---|---|---|---|---|---|---|---|---|---|
| −3 | 48→45 | 6.2% | 8 | 3 | 9 | B→C 5 · C→D 0 | 0.0% (n=3) | **0건** | 64.1% |
| −4 | 48→44 | 8.3% | 15 | 4 | 9 | B→C 9 · C→D 1 | 0.0% (n=4) | **0건** | 64.6% |
| **−6** | 48→41 | **14.6%** | 23 | 7 | 10 | B→C 16 · C→D 3 | **14.3%** (n=7) | **0건** | **65.4%** |
| (−9) | 48→36 | 25.0% | 23 | 8 | 6 | B→C 16 · C→D 4 | 41.7% (n=12) | 0건 | 64.8% |
| (−10) | 48→32 | 33.3% | 19 | 8 | 5 | B→C 14 · C→D 4 | 37.5% (n=16) | 0건 | 66.1% |

- 100위+ 군 영향은 **모든 후보에서 정확히 0건** (설계상 rank ≤ 20 에만 적용).
- "그중 지연 중첩"은 잘려나간 등급 이동 건 가운데 v6 지연 감점(−6)도 함께 맞은 건.
  1-20위 군의 B→C 이동 대부분이 지연 감점과 겹친다 — 두 감점이 같은 모집단
  (대형코인·즉시 터치)을 때린다는 뜻이라, 켜는 순간 체감 감소가 표의 수치보다
  크게 느껴질 수 있다. 이것이 10-06 이후로 미루는 실무적 이유이기도 하다.

### 2-3. 추천: **−6**

**근거 한 줄**: 후보 중 전체 통과군 승률 개선폭이 가장 크면서(62.7% → 65.4%)
잘려나간 7건의 승률이 14.3% 에 그쳐 **노이즈만 잘라내는 마지막 지점**이다.

보충:
- 기획서 목표(1-20위 C 이상 통과율 25~35% 감소)는 −9~−10 이라야 충족되는데,
  그 구간부터 **잘리는 건의 승률이 37~42% 로 뛴다** — 정상 신호를 자르기
  시작한다는 신호이고, 전체 통과군 승률도 −6 의 65.4% 를 넘지 못한다(−9 는
  오히려 64.8% 로 후퇴). 목표치는 "감소량"이지 "품질"이 아니므로 품질 축을 따랐다.
- 점수 분포상 1-20위 통과건이 49~55점에 두껍게 뭉쳐 있어 −9/−10 은 그 덩어리를
  통째로 넘기는 **절벽**이다(−8 22.9% → −9 25.0% → −10 33.3% → −11 47.9%).
  ±1점 노이즈에 결과가 크게 흔들리는 자리에 기본값을 두지 않는다.
- −6 은 지연 감점(−6)과 같은 크기라 배점 체계 안에서 설명이 단순하다
  (사다리 −3 의 2배 = "실측 격차가 큰 축").

**표본 한계**: n=214, 그중 1-20위 88건. 기획서 D2 주의사항대로 100위+ 군에
09-13 이전 sanity 우회 오알림이 섞였을 수 있다. 10-06 활성화 시점에 표본을
다시 돌려 N 을 재확인할 것(표 재생성은 §2 쿼리 그대로).

---

## 3. 배선 위치 (파일:함수)

### 3-1. 거래대금 순위 감점

| 위치 | 내용 |
|---|---|
| `collector/grading.py` : 모듈 상수 | `VOLUME_RANK_TOP_N_DEFAULT=20` · `VOLUME_RANK_PENALTY_DEFAULT=-6` (설정 부재 시 폴백) |
| `collector/grading.py` : `_volume_rank_points(rank)` | rank ≤ top_n → penalty · None/그 외 → 0 · **플래그 OFF → 0** · 설정 조회 실패 → 0(무감점 쪽 fail-safe) |
| `collector/grading.py` : `score_breakdown(..., touch_volume_rank=None)` | `bd["vol_rank"]` 키 신설. `sum(values()) == 총점` 불변식 유지 |
| `collector/grading.py` : `calculate_grade` / `calculate_grade_with_breakdown` | 동명 kwarg 그대로 전달 |
| `collector/grading.py` : `regrade_current` | **명시 인자 우선 → 없으면 레벨 dict 의 `touch_volume_rank` 키** (지연 감점과 동일 2경로) |
| `monitor/price_check.py` : `run_once` 터치 루프 | 지연 감점 주입 **바로 아래**, `_rep`(대표 선정)·전 멤버 재채점 **앞**. `_lv["touch_volume_rank"] = _volume_ranks().get(ticker) if touched else None` (예고는 None) |

**추가 API 콜 0**: `_volume_ranks()` 는 이미 회차 캐시이고, 터치 건은 어차피
아래 억제-터치 기록 경로(m-8)에서 같은 캐시를 부른다. 조회 실패(`{}`)는
rank None → 감점 0 으로 자연 폴백하며 try/except 로 격리했다.

**알림 본문 점수 내역 표시**: 현재 `notify/telegram.py` 에 score_breakdown 을
출력하는 경로가 없다(사다리·지연도 미표시). 따라서 추가 항목 없음 — telegram.py
무수정 원칙과도 일치.

### 3-2. TP 없는 글 배제

| 위치 | 내용 |
|---|---|
| `monitor/price_check.py` : `_has_effective_tp(lv)` (신설) | 판정부와 **같은 유효 TP 정의** — `_volume_band_tps` (오염 방어선 `entry < tp <= entry*4`, `tps_usd ∪ tp_usd`). 진입가가 없어 sanity 를 못 거는 행은 raw `tp_usd>0` 로 보수적 판단(억제하지 않는 쪽) |
| `monitor/price_check.py` : `run_once` 알림 게이트 | 게이트 3(TP 스윙) **뒤**, 게이트 4(일일 상한) **앞**. `kind=="touch"` 한정 · `cfg_get("alert_exclude_no_tp")` · 클러스터 **전 멤버**가 무TP 일 때만 억제 |

예고(preview)에는 적용하지 않는다. 터치 기록·판정·MFE 추적·터치 스냅샷은
게이트 밖이라 **그대로 수행**된다 — 데이터는 계속 쌓이고 알림만 꺼진다.

---

## 4. 억제 경로 (no_tp)

1. `send_ok = False` → 기존 `else: summary["suppressed"] += 1` 경로를 그대로 탄다.
2. `summary["suppressed_no_tp"]` 카운터 +1 (`preview_disabled` 와 같은
   `summary.get(...)` 관례 — daily_stats 컬럼 신설 없음 = db.py 스키마 무수정).
3. 로그 1줄: `[체크] {코인} TP 없는 글(timeboxed) - 알림 억제(no_tp)`.
4. 무음 기록(A안 패턴): `db.record_alert(conn, coin, "touch_no_tp", ids, day, now, sent=0)`.
   기록 실패는 try/except 로 격리(터치 경로 생존 우선).

**왜 `kind='touch'` 가 아니라 `'touch_no_tp'` 인가** — `'touch'` 로 쓰면
`count_alerts_today`(코인당 5건)·`count_all_alerts_today`(전체 15건)·
`recent_alert_exists`(재발송 차단)·`touch_alert_sent`(TP 단계 알림 게이트)가
전부 이 행을 "발송된 본알림"으로 오인한다. 억제된 신호가 같은 코인의 정상
알림 슬롯을 잡아먹고, TP 단계 알림 게이트까지 잘못 열린다. 별도 kind 라
기존 조회(`kind='touch'` 정확일치, `kind LIKE 'tp%'`)는 전부 무손상이다.

---

## 5. 테스트

| ID | 파일 | 내용 |
|---|---|---|
| VR1 | test_grading | 설정 정본 — 스위치 OFF · top_n 20 · 감점 −6 · 모듈 상수 일치 |
| VR2 | test_grading | **플래그 OFF — 어떤 순위도 0점**(기존 동작 완전 불변) |
| VR3 | test_grading | `vol_rank` 키 신설되나 OFF 면 0 · `sum==총점` |
| VR4 | test_grading | ON: top_n 이내 −6 / 밖 0 · **경계 rank==20 은 감점 포함** |
| VR5 | test_grading | ON 이어도 `None`(수집·예고)은 0 |
| VR6 | test_grading | breakdown 키 격리 — 다른 키 불변 |
| VR7 | test_grading | **지연 감점(−6)과 중첩 시 단순 합산 −12** |
| VR8 | test_grading | regrade 2경로(명시 인자 / dict 키) 동일 |
| VR9 | test_grading | 순수 감점 — 어떤 순위도 None 보다 점수를 올리지 못함 |
| VR10 | test_grading | 롤백 스위치 OFF 복귀 시 즉시 감점 0 |
| VR-P1 | test_price_logic | 터치 회차 — 재채점에 회차 캐시 순위(KRW-LINK=5) 주입 |
| VR-P2 | test_price_logic | **예고 회차 — 주입값 None** |
| VR-P3 | test_price_logic | 저장 `touch_score` 가 정확히 −6 (같은 픽스처 ON/OFF 비교) |
| VR-P4 | test_price_logic | 플래그 OFF — 순위가 실려도 점수 불변 |
| NOTP1 | test_price_logic | TP 없는 글 터치 → 알림 0건 |
| NOTP2 | test_price_logic | 억제해도 터치 기록·재채점 스냅샷 유지 |
| NOTP3 | test_price_logic | `alerts_log` 에 `kind='touch_no_tp'` · `sent=0` |
| NOTP4 | test_price_logic | 무음 기록이 `'touch'` 가 아님(상한·재발송·TP 게이트 무손상) |
| NOTP5 | test_price_logic | TP 있는 글은 종전대로 발송 |
| NOTP6 | test_price_logic | 플래그 False → 종전 동작 |

VR-P 는 `collector.grading.regrade_current` 를 스파이로 감싼다 — price_check 가
순환 import 방지를 위해 **호출 시점에** 지연 import 하므로 패치 지점이 grading
모듈 쪽이다(price_check 속성 패치는 먹지 않는다).

### 실행 결과 (워크트리 루트, `PYTHONIOENCODING=utf-8`, 순차 포그라운드)

| 스크립트 | exit | 비고 |
|---|---|---|
| `scripts/test_grading.py` | 0 | 97건 통과 (VR1~VR10 신규 10건 포함) |
| `scripts/test_price_logic.py` | 0 | VR-P1~4 · NOTP1~6 신규 10건 포함 |
| `scripts/test_touch_recording.py` | 0 | 53건 |
| `scripts/test_cycle.py` | 0 | |
| `scripts/test_infra.py` | 0 | 224건 |

`git status` 는 의도한 5개 파일 + 이 노트만. 테스트가 갱신한 `data/` 는
`git checkout -- data` 로 되돌렸다.

---

## 6. 롤백

| 상황 | 조치 (각 1줄) |
|---|---|
| 거래대금 감점을 끄고 싶다 | `grade_volume_rank_enabled` → `False` (현재 기본값) |
| 감점 폭만 줄이고 싶다 | `grade_volume_rank_penalty` → `-3` (사다리 감점과 동급) |
| 적용 범위를 넓히고 싶다 | `grade_volume_rank_top_n` → `50` (§2 표 재산출 후에만) |
| TP 없는 글 배제를 끄고 싶다 | `alert_exclude_no_tp` → `False` |

세 키 모두 코드 경로를 우회하는 게 아니라 **감점 0 / 게이트 통과**로 떨어지므로,
되돌린 뒤의 동작은 이 변경 이전과 비트 단위로 같다(VR2·VR10·NOTP6 이 증명).

---

## 7. 남은 이슈 / CTO 판단 요청

1. **N 추천값이 기획서 목표치와 충돌한다.** 기획서는 "1-20위 C 이상 통과율
   25~35% 감소"를 기준으로 제시했으나, 그 구간(−9~−10)은 승률 40%대 신호를
   자르기 시작한다. 품질 축을 따라 −6 을 기본값으로 넣었다 — 목표치 우선이면
   `grade_volume_rank_penalty` 를 `-9`(25.0%) 또는 `-10`(33.3%)로 한 줄 수정.
2. **10-06 활성화 시 재시뮬 필요.** 그때 v6 태그가 찍힌 터치 표본이 쌓여 있어
   기준선 재구성(지연 감점 덧입히기) 없이 실측으로 돌릴 수 있다.
3. **지연 감점과의 모집단 중첩**(§2-2 주석). 두 감점이 같은 건을 때리므로 동시
   가동 첫 주 알림량을 별도로 관찰할 것.
4. `alert_pipeline_manual.md` 는 직접 수정하지 않았다 — §3 배선표·§1 설정표·
   게이트 절(no_tp 게이트를 게이트 3-2 로) 반영은 CTO 병합 시.
