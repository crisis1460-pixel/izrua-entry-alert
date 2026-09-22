# 주간 리포트 v2 — 지표 나열형 → 의사결정형 (2026-09-22, R4)

- 근거: `plan_2026-09-22_최종버전_종합검토.md` §3 R4·R5 ·
  `research_2026-09-22_external_final_review.md` §3(TradeZella 30분 주간 리뷰 6단계,
  freqtrade `/stats`) · `research_2026-09-22_db_final_review.md` §2-1(결과 확인 7일)
- 알림량·등급 산식·필터에는 **아무 영향 없음**. 리포트 텍스트 조립 레이어만 바뀐다.
- 샘플(운영 DB 실측, `mode=ro` 읽기 전용 · 발송 없음): `izrua_company/sample_weekly_2026-09-22.txt`
  — HTML 1,996자 / 평문 1,941자 (목표 3,500자 이내).

## 1. 새 구조 (9부)

| # | 섹션 | 내용 |
|---|---|---|
| ① | 헤더 | `📈 주간 성적 리포트` + `🗓 MM-DD~MM-DD (KST, 7일)` + `📦 표본: 알림 N건 · 종결 N건` |
| ② | `📌 이번 주 한눈에 — 지난주 대비` | 알림 수 / 종결 수 / 승률 / PF / 평균 R 각각 `값 (지난주 값 → ▲▼→)`. 지난주 = 직전 7일 창 |
| ③ | `🔎 관찰 3줄` | 규칙 기반 자동 선택(격차 큰 순 최대 3). 후보 없으면 `특이 관찰 없음 (표본 n=…)` |
| ④ | `⏳ 다음 판단` | v6 지연감점 평가 X/150 · MFE/MAE e-ratio X/50. 진행률 + 예상일만 — **사람이 결정** |
| ⑤ | `📋 판정 사유별` | freqtrade `/stats` 형: outcome 별 n · 비중 · 평균 보유시간 · 평균 실현% (최근 4주) |
| ⑥ | `🏆 작성자 랭킹` | E_LB 수학·정렬 **불변**, 표시만 상위 5 + `· 외 N명`. 역신호 후보(🔻)/확정 유지 |
| ⑦ | `🎚️ 등급 캘리브레이션` | `grade_ver` 최신 표본만(v6 → 표본 0 이면 v5), 1줄/등급 + 단조성 1줄 |
| ⑧ | (제거) | 아래 §4 |
| ⑨ | 각주 | `ℹ️ 결과 확인 기준: 터치 후 7일 (168h 내 종결 57%)` |

### 표본(n) 규칙
- 비율·평균 지표(승률/PF/평균 R)는 표본 `weekly_report_min_n`(기본 10) 미만이면
  **화살표 생략 + `(n=…, 참고)`** 로만 표기. 건수(알림/종결)는 항상 화살표.
- 지난주 값이 없으면 `(지난주 없음)`.
- 관찰 후보는 **양쪽 그룹 n≥10**. 작성자 극단만 n≥5(주 1회 창에서 작성자당 10건은
  사실상 도달 불가) — `analytics/weekly.AUTHOR_MIN_N`.

### 관찰 3줄 후보 풀 (격차 %p 로 통일해 정렬)
| 키 | 내용 | 창 | 게이트 |
|---|---|---|---|
| `outcome_mix` | 판정 사유 비중 이번주 vs 지난주 ±10%p 이상 | 1주 vs 1주 | 양쪽 n≥10 |
| `delay` | 수집→터치 지연 `<30분` vs 이상 승률 격차 | 4주 누적 | 양쪽 n≥10 |
| `volume_rank` | `touch_volume_rank` 1-20위 vs 100위 밖 승률 격차 | 4주 누적 | 양쪽 n≥10 |
| `authors` | 이번 주 최고/최저 작성자 | 1주 | 자격자(종결 n≥5) 2명 이상 |
| `hold_outlier` | hit vs miss 평균 보유시간의 상대편차(**만료 판정 제외** — CTO 디버깅: 만료는 정의상 판정창 끝까지 가서 매주 동어반복이 1순위로 뽑혔음. gap=편차/4 로 %p 후보와 자릿수 맞춤) | 4주 누적 | n≥10, 편차 ≥50% |

## 2. 새 함수 목록

### `analytics/weekly.py` (신규 — 순수 계산, 프로젝트 모듈 import 0)
- `realized_pct(row)` — 실현 %, `resolve/touch`, short 부호 반전, `|r|>50%` 제외
- `holding_hours(row)` / `touch_delay_min(row)`
- `summary(rows, alerts=None)` — closed/wins/losses/win_rate/pf/avg_r/r_n
- `arrow(cur, prev)` — `▲ ▼ →` / None
- `outcome_stats(rows)` — ⑤ 집계
- `observations(cur, prev, pool, limit, min_n)` + 후보 헬퍼 `_cand_*`
- `milestone(count, target, per_day)` — 진행률·잔여·예상일
- 상수: `WIN_OUTCOMES` `LOSS_OUTCOMES` `OUTCOME_ORDER` `OUTCOME_LABELS`
  `MAX_ABS_RET_PCT=50` `SMALL_N=10` `AUTHOR_MIN_N=5`

### `storage/db.py` (읽기 전용 조회만 추가 — 기존 시그니처·스키마 불변)
- `get_resolved_rows_between(conn, start_ts, end_ts)` — `resolved_at ∈ [start, end)`,
  미종결·섀도 터치 제외. 컬럼 묶음 상수 `_WEEKLY_ROW_COLS`
- `count_touch_alerts_between(conn, start_ts, end_ts)` — `alerts_log kind='touch' AND sent=1`
  (구세대 DB 에 `sent` 컬럼이 없으면 조건 없이 재시도)
- `count_resolved_touches_since(conn, since_ts)` — 마일스톤 카운트.
  **정의: `touched_at >= since AND outcome IS NOT NULL`** (의도적 단순 정의 —
  "공정 종결"은 tp_only 재판정이 필요해 리포트에 싣기 어렵고, 여기선 진행률만 필요)
- `get_weekly_calibration_rows(conn, ver)` — `grade_ver = ver` 표본만

### `notify/telegram.py`
- `_kst_md(ts)` / `_metric_row(...)` / `_overview_section` / `_observation_line` /
  `_observations_section` / `_milestones_section` / `_outcome_stats_section` /
  `_calibration_compact` / `_author_section` / `_fit_weekly`
- `_WEEKLY_FOOTNOTE`, `_WEEKLY_KST`
- `render_weekly_report(...)` — v2 인자 추가: `current` `previous` `observations`
  `pool_n` `pool_days` `milestones` `outcome_stats` `calibration_ver` `period`
  `min_n` `top_authors` `max_chars`. 구 인자(`baseline` `raw_records` `confluence`
  `calibration_legacy` `r_distribution` `r_distribution_by_grade` `holding_period`
  `regime_heatmap`)는 **받되 무시** — `scripts/show_status.py` 호출부 무수정 호환.
- 삭제: `_confluence_line` `_baseline_section` `_calibration_section`
  `_calibration_main_lines` `_r_distribution_section` `_holding_period_section`
- 존치: `_regime_heatmap_section` (출력엔 안 쓰지만 `scripts/test_infra.py` 가 참조)
- import 변경: `clustering` 제거, `weekly` 추가, `datetime` 추가

### `scripts/run_weekly_report.py`
- `_calibration_latest(conn)` — `grade_formula_ver` → v6 → v5 순으로 표본 있는 첫 버전
- `_milestones(conn, now)` — `meta.grade_v6_since` / `meta.mfe_mae_fixed_since` 기준
  카운트 + 최근 30일 속도
- `_collect(conn, now, pool_days)` — SELECT 일괄
- `build_report(db_path=None, now=None, conn=None)` — **발송 없이** 텍스트만 조립.
  `conn` 을 주면 그 연결을 그대로 쓴다(운영 DB `mode=ro` 샘플 렌더링 경로)
- `send_report` 는 `build_report` + `telegram.send` 로 축소
- import 변경: `clustering` `distribution` `scripts.show_status._calibration_pair` 제거

## 3. 설정 키 (`config/settings.py`, `weekly_report_*` 접두만 추가)

| 키 | 기본값 | 뜻 |
|---|---|---|
| `weekly_report_max_chars` | 3500 | 길이 예산. 초과 시 줄 경계에서 절단 + 안내 1줄 |
| `weekly_report_min_n` | 10 | 이 미만이면 화살표 생략 + "(n=…, 참고)" |
| `weekly_report_observations` | 3 | 관찰 줄 최대 개수 |
| `weekly_report_top_authors` | 5 | 랭킹 표시 상한(수학·정렬 불변) |
| `weekly_report_pool_days` | 28 | 관찰·판정사유 집계 누적 창(일) |
| `weekly_report_milestone_v6` | 150 | v6 지연감점 평가 트리거 |
| `weekly_report_milestone_mfe` | 50 | MFE/MAE e-ratio 트리거 |

## 4. 제거된 섹션과 이유

| 섹션 | 제거 이유 |
|---|---|
| `🎲 초과 적중률` | 판정 기준이 다른 두 축(작성자=TP 도달 / 기준선=단순 수익)의 뺄셈이라 매주 caveat 2줄이 따라붙었다. "기준선 대비"는 ② 지난주 대비가 대체 |
| `📊 R-멀티플 분포` | 막대가 길이 예산의 ~15%를 먹는데 의사결정에 쓰인 정보는 평균 R 한 줄뿐 → ② 로 승격 |
| `⏱️ 보유기간 분포` | ⑤ 판정 사유별 집계의 '평균 보유시간' 칼럼이 상위 호환 |
| `🌡️ 등급×장세 히트맵` | BTC 레짐 `below` 표본이 8월 이후 0건 증가(db_final_review §요약3) — 표본 도달이 무기한인 상시 침묵 섹션 |
| `🤝 합의 참여` | 데이터가 가설을 기각(다작성자 2인+ 공정 33.3% vs 단독 38.2%) |
| 구 산식 병기 | 신·구 등급은 의미가 달라 한 리포트에 두 표를 놓으면 어느 쪽을 보는지 모른다. `grade_ver` 최신 표본만 |

## 5. 테스트 (`scripts/test_weekly_report.py`, 97 체크)

| ID | 검증 |
|---|---|
| W1~W5 | 빈 DB 우아한 표시 · 종합 시나리오 · 게이트 미통과 · **상위 5 표시 + 외 N명** · **작성자 0명일 때 다른 섹션 독립 출력** |
| I1 | DB 통합(섀도 터치/미종결 제외) |
| I2a~d | 신규 조회 4종 — 창 필터 · 표본 기준 · `sent=1` 필터 · 마일스톤 카운트 정의 |
| V1~V7 | **지난주 대비 화살표** ▲▼→ · 알림/종결/승률/PF/평균R · **소표본 화살표 생략 + "(n=…, 참고)"** · 지난주 없음 |
| O1~O7b | **관찰 선택 규칙** — 격차 순 정렬 · 최대 3 · n 병기 · **한쪽 n<10 침묵** · 후보 0 시 "특이 관찰 없음(표본 n)" · 구성변화 10%p 게이트 · 작성자 자격자 2명 · 보유시간 편차 50% 게이트 |
| M1~M4 | 마일스톤 진행률 · 도달 ✅ · 카운트 정의 명시 · 속도 0 시 예상일 생략 |
| S1~S6 | 판정 사유별 n·비중·보유·실현% · 이상치 제외 · 숏 부호 반전 · 표본 0 행 생략 · 미주입 시 섹션 생략 |
| C1~C11 | 1줄/등급 압축 · 산식 버전 표기 · 소표본 ⚠️ · 단조성 1줄 · **구 산식 병기 제거** · HTML 안전 |
| RC1~RC3 | 역신호 확정 줄(작성자 0명 경로 포함) |
| X1~X6 | **제거된 섹션이 출력에 없음** + 구 인자를 넘겨도 예외 없이 동일 출력 |
| L1~L5 | **4,000자 초과 절단** · 안내 1줄 · 태그 짝 유지(줄 경계 절단) · 예산 이내 무변경 · 표준 구성 3,500자 이내 |
| A1~A22 | (기존) 주간 감사 덤프 — 무변경 |

실행: `PYTHONIOENCODING=utf-8 python scripts/test_weekly_report.py` → exit 0 (97/97)
동반 확인: `test_infra.py` 224/224 · `test_ranking.py` 63/63 · `test_show_status.py` PASS.

## 6. 남은 이슈 / 후속

- `⏳ 다음 판단` 두 마일스톤은 현재 0/150·0/50 이다 — `meta.grade_v6_since`/
  `mfe_mae_fixed_since` 가 09-22 에 찍혔기 때문. 속도가 0 인 동안은 "속도 산출 불가"로
  뜨고, 터치가 쌓이면 자동으로 예상일이 붙는다.
- `🎚️` 는 현재 **v5 표본으로 폴백**(v6 종결 표본 0건). v6 표본이 차면 자동 전환된다.
- 역신호 확정 9명 줄·표본부족 줄은 단일 장문 행이다. 지금 전체 길이가 1,996자라
  여유가 크지만, 확정 인원이 더 늘면 이 두 줄부터 줄이는 게 맞다.
- `alert_pipeline_manual.md` 는 CTO 병합 대상 — 이 문서를 근거로 §주간 리포트 절을
  교체해야 한다(직접 수정하지 않았다).
