# 라운드 9 — 테스트 커버리지 갭 전수 검토 (2026-08-16)

전체 테스트 파일 15개 + 대응 소스 코드 교차 검토 결과.

---

## 카테고리 1: 테스트가 전혀 없는 모듈/함수

### GAP-1 `analytics/signal_quality.py` — 미존재

**대상 함수:** `_spearman_rank_corr`, `compute_ic`, `compute_icir`,
`compute_hourly_performance`, `compute_weekday_performance`, `summarize_best_worst`

어떤 test_*.py 도 import 하지 않는다. 표시 전용 원칙이라 알림·필터에 영향 없지만,
수학 오류가 잘못된 리포트 숫자로 노출되는 경로는 열려 있다.

**추가할 케이스:**
- `_spearman_rank_corr(n=4)` → None, `n=5` → 유효 float 반환
- 동점 처리: `xs=[1,1,1,1,1]` → 평균 랭크 3.0, 결과는 0.0
- 완전 상관 (+1.0) / 완전 역상관 (-1.0) 확인
- `compute_ic([])` → `{"ic": None, "n": 0}` (빈 입력)
- `compute_icir(weekly_ics=[...])` n<4 → icir=None, n≥4 → 유효값
- `compute_hourly_performance` — 24개 버킷 전부 있는지, CLOSED_OUTCOMES 외 outcome 무시 확인
- `summarize_best_worst(min_n=5)` — 모든 버킷이 n<5이면 best_hour=None

---

### GAP-2 `monitor/liquidation.py` — 미존재

**대상 함수:** `fetch_btc_liq_context`, `_fetch_pressure`, `_try_liqmap_fallback`

어떤 test_*.py 도 import 하지 않는다.

**추가할 케이스:**
- `fetch_btc_liq_context` TTL 내 재호출 → HTTP 콜 없이 캐시 반환
- `_fetch_pressure` pressure HTTP 200 + `regime="LONG_CROWDED"` → direction=`"long_heavy"`
- `_fetch_pressure` HTTP non-200 → `_try_liqmap_fallback` 폴백
- `_try_liqmap_fallback` ls_ratio=2.0(경계) → `"long_heavy"`, ls_ratio=0.5(경계) → `"short_heavy"`
- `_try_liqmap_fallback` ls_ratio=None → None
- 두 엔드포인트 모두 실패 → None 반환 + 스테일 캐시 사용(있으면)

---

### GAP-3 `monitor/token_events.py` — 실제 로직 미존재

`test_touch_recording.py:44`에서 import하지만 `lambda conn, t: None`으로만 쓴다.
`_fetch_unlocks_fresh`, `fetch_upcoming_unlocks`, `get_unlock_warning` 실체 로직은 전혀 검증되지 않는다.

**추가할 케이스:**
- `_fetch_unlocks_fresh` — `unlockPercentage=4.9%` → 제외, `5.0%` → 포함 (경계값)
- `unlockPercentage=None + noOfTokens>0 + maxSupply>0` → 폴백 계산 확인
- `maxSupply=0` → ZeroDivisionError 없이 건너뜀
- 7일 창 필터: 오늘 + 7일 이내만, 이후 이벤트 제외
- `fetch_upcoming_unlocks` — DB 캐시 TTL 내 재호출 → HTTP 호출 없음
- HTTP 실패 → None 반환, 캐시 갱신 없음
- `get_unlock_warning("btc")` vs `"BTC"` → 대소문자 무관하게 동일 결과

---

### GAP-4 `collector/telegram_source.py` — 미존재

모든 test_*.py 에서 완전히 비어 있다.

**추가할 케이스:**
- 정상 채널(200 + `tgme_channel_history`) HTML 파싱 → posts 반환
- 비공개 채널(200 + `tgme_page_extra` 있음) → 빈 리스트 + 경고 로그
- 삭제 채널(200 + `tgme_page_extra` 없음) → 빈 리스트 + 경고 로그
- `max_age_hours` 필터: 1시간 초과 글 제외
- `_MIN_INTERVAL_SEC` 이내 재호출 → HTTP 콜 없이 이전 결과
- `published_at` ISO8601 → epoch 변환 정확도

---

### GAP-5 `monitor/macro.py` 내부 함수들 — 미존재

`test_morning_brief.py`는 `get_macro_events`를 `lambda: _mock_events`로만 교체한다.
아래 함수들의 실제 로직 검증 없음:

| 함수 | 갭 |
|---|---|
| `_is_us_dst(d)` | DST 전환일(3월 둘째 일요일, 11월 첫째 일요일) 경계값 미검증 |
| `_kst_release_label(ev_type, ev_date)` | 익일 표기 계산, 서머/윈터 +13h/+14h 오프셋 미검증 |
| `_generate_rule_events(start, months)` | NFP(첫째 금요일), ISM(첫 영업일), CPI(둘째 화+1일) 날짜 정확도 미검증 |
| `_fetch_fomc_calendar()` | 2일 연속 회의 → decision_day = raw[i+1] 처리 로직 미검증 |
| `refresh_macro_calendar()` | DB 캐시 저장, 메모리 캐시 갱신 미검증 |
| `get_macro_events()` — DB 캐시 경로 | 메모리 없음 + DB 캐시 있음 → DB에서 반환 경로 미검증 |

**추가할 케이스:**
- `_is_us_dst(date(2026, 3, 8))` → False(DST 시작 전날), `date(2026, 3, 9)` → True(DST 시작일)
- `_kst_release_label("FOMC", DST 날짜)` → `"한국 익일03:00"`, 윈터타임 날짜 → `"한국 익일04:00"`
- `_generate_rule_events(date(2026, 8, 1), months=1)` → 월요일인 경우 `ISM=8/3`, 금요일인 경우 `NFP=8/7` 확인
- `_fetch_fomc_calendar` mock: `meetings=[{"date":"2026-07-29"},{"date":"2026-07-30"}]` → 연속 2일 → decision=7/30

---

### GAP-6 `monitor/options.py` HTTP 로직 — 미존재

`test_infra.py`에 `_calc_pc_ratio/_calc_max_pain` 4건이 있지만, HTTP 조회 경로(`_fetch_raw`, `fetch_btc_options_context`)는 전혀 없다.

**추가할 케이스:**
- `_fetch_raw` HTTP 200 + 빈 result → None 반환
- `_fetch_raw` HTTP non-200 → None 반환 + 경고 로그
- `fetch_btc_options_context` TTL 내 재호출 → 캐시 반환

---

## 카테고리 2: 실제 버그를 잡지 못하는 너무 단순한 테스트

### GAP-7 `test_touch_recording.py` — Tier2 컬럼 저장 미검증

`test_touch_recording.py` Tier2 섹션이 `touch_btc_regime`, `touch_dvol`, `touch_grade_ver`를 검증하지만, 2026-08-16 신규 추가된 `touch_atr_band_pct = atr_pct * 0.5` 계산 결과 저장이 빠져 있다.

**추가할 케이스:**
- `record_touch_snapshot(rows, atr_pct=10.0)` → `touch_atr_band_pct == 5.0` 확인
- `atr_pct=None` → `touch_atr_band_pct IS NULL`(미기록)
- `atr_pct=0.0` → `touch_atr_band_pct == 0.0`

---

### GAP-8 `test_morning_brief.py` — `get_macro_events` 전체 모킹

`test_morning_brief.py:132`: `macro_mod.get_macro_events = lambda conn=None: _mock_events`

캘린더 생성 로직이 어떻게 잘못되어도 이 테스트는 통과한다.
(상세: GAP-5 참고)

---

### GAP-9 `test_resilience.py` — `market_sentiment.get_sentiment()` DB 캐시 경로 미검증

`수리1`은 `_fetch_fresh()` 내부를 직접 호출한다. `get_sentiment(conn)` 의 DB 캐시 TTL 경로(신선 캐시 반환 / TTL 만료 후 재조회 / 캐시 저장 실패 무시)는 검증되지 않는다.

**추가할 케이스:**
- `get_sentiment` 신선 캐시(TTL 내) → `_fetch_fresh` 미호출
- `get_sentiment` TTL 만료 → 재조회 + 결과 캐시 저장

---

## 카테고리 3: 경계값을 테스트하지 않는 곳

### GAP-10 `analytics/signal_quality.py` — n 경계값 (GAP-1 포함)

- `_spearman_rank_corr`: n=4 → None(경계 미달), n=5 → 유효값(정확히 경계)

### GAP-11 `monitor/token_events.py` — 5% 임계값 경계 (GAP-3 포함)

- `unlock_pct=4.99` → 제외, `5.00` → 포함

### GAP-12 `monitor/announcements.py` — `_extract_notices` 스키마 변형 경계

현재 AN9에서 `{"success": False}`, `None`, `"nope"` 세 케이스만 검증한다.
다음 형태들은 미검증:

- 최상위 배열: `[{"id": 1, ...}]` → 1건 반환
- `data.list` / `data.items` / `data.announcements` 키 형태 → 1건 반환
- 각 항목이 dict가 아닌 경우(`["string"]`) → 0건

### GAP-13 `monitor/liquidation.py` — ls_ratio 경계값 (GAP-2 포함)

- `ls_ratio=2.0` 정확히 경계 → `"long_heavy"` 포함 여부(`>=`)
- `ls_ratio=0.5` 정확히 경계 → `"short_heavy"` 포함 여부(`<=`)

---

## 카테고리 4: 최근 추가된 기능 테스트 부재

### GAP-14 `touch_mfe_atr_ratio` — 미존재

**위치:** `storage/db.py:1594-1595`

```python
"UPDATE levels SET touch_mfe_atr_ratio=? WHERE id=? AND touch_mfe_atr_ratio IS NULL"
```

이 UPDATE를 호출하는 경로(price_check MFE 기록)와 IS NULL 가드 동작 검증 없음.

**추가할 케이스:**
- 최초 기록 → 정상 저장
- 동일 ID 재기록 → IS NULL 가드로 무시 (first-write-wins)
- MFE 값 None → UPDATE 자체 스킵

---

### GAP-15 `touch_atr_band_pct` — 미존재

**위치:** `storage/db.py:805-807`

`atr_pct * 0.5`로 자동 파생. (GAP-7과 동일 위치)

**추가할 케이스:**
- GAP-7 참고

---

### GAP-16 `touch_supply_1h` — 미존재

**위치:** `storage/db.py:1607-1616`, `monitor/price_check.py:312-341`

`db.get_supply_1h_pending`, `db.record_supply_1h`, `price_check._backfill_supply_1h` 모두 테스트 없음.

**추가할 케이스:**
- `get_supply_1h_pending(conn, now)` — touched_at이 1h~2h 창 안에 있는 행만 반환
- touched_at 경계: now-3600(정확히 1h) → 포함, now-7201(2h 초과) → 제외
- `record_supply_1h` IS NULL 가드: 두 번 호출 → 첫 값 유지
- `_backfill_supply_1h` — 대기 행 없음 → 조기 반환, 대기 행 있음 → verdict 기록
- `fetch_deriv_snapshot` 실패(None 반환) → 건너뜀, 예외 발생 → 삼킨 후 계속

---

### GAP-17 자동 캘린더 실제 로직 — 미존재

(GAP-5 참고) `test_morning_brief.py`는 표시 로직만 검증, 생성 로직은 전혀 검증 안 됨.

---

## 카테고리 5: 외부 API 실제 호출 테스트 (CI 실패 위험)

### GAP-18 `scripts/test_tradingview_live.py` — 실제 네트워크 호출

**파일:** `scripts/test_tradingview_live.py:57-60`

```python
ideas = tradingview.fetch_ideas(symbol, timeout=args.timeout, ...)
```

`tradingview.fetch_ideas()` 및 `_fetch_detail()` 이 실제 HTTP 요청을 보낸다.
`nargs="+"` 덕분에 심볼 미지정 시 즉시 오류 종료하지만, CI가 우연히 심볼을 넘기거나
test runner가 glob으로 전체 실행하면 실제 호출이 발생한다.

**조치 제안(코드 수정 없이):**
- CI 스크립트에서 이 파일만 명시적으로 제외하거나,
- 파일명을 `probe_tradingview_live.py`로 변경해 `test_*.py` 패턴에서 제외

---

## 카테고리 6: DB 상태 공유로 순서 의존성이 생기는 패턴

### GAP-19 `scripts/test_price_logic.py` T1~T22+ 시리즈

**위치:** `test_price_logic.py` 전체(총 3732줄)

T1(레벨 INSERT) → T5(mark_touched) → T10(resolve_outcome) → 이후 T들이 같은 `TEST_DB` 상태에 의존한다. `fresh_db()` 재초기화 호출이 없다. 임의 T번호만 단독 실행하면 즉시 실패한다.

이는 의도된 설계(순서 보장 통합 시나리오)지만, 테스트 추가 시 주의:
- 새 테스트는 반드시 직전 T의 DB 상태를 명확히 문서화해야 함
- 또는 새 시나리오는 별도 `fresh_db()` 블록으로 격리할 것

**현재 격리 없이 의존하는 주요 구간:**
- AN 시리즈(AN1-AN14): T 시리즈와 같은 DB를 공유하지만, 개별 AN 테스트 시작 시 `conn.execute("DELETE FROM levels")` 수동 청소
- 그 외 T 시리즈: 청소 없이 누적 상태에 의존

---

## 요약

| 카테고리 | GAP 수 | 우선도 |
|---|---|---|
| 모듈 전혀 미테스트 | 6건 (GAP-1~6) | 높음 |
| 너무 단순한 테스트 | 3건 (GAP-7~9) | 중간 |
| 경계값 미검증 | 4건 (GAP-10~13) | 중간 |
| 최근 기능 미테스트 | 4건 (GAP-14~17) | 높음 |
| CI 위험 실API 호출 | 1건 (GAP-18) | 즉시 조치 |
| DB 순서 의존성 | 1건 (GAP-19) | 낮음(설계 인식 필요) |

**즉시 우선순위:**
1. GAP-18: `test_tradingview_live.py` CI 제외 처리
2. GAP-14~16: touch_mfe_atr_ratio / touch_atr_band_pct / touch_supply_1h (2026-08-16 신규)
3. GAP-1: `signal_quality.py` — 내부 기능 강화 리서치 결과물인데 테스트 전무
4. GAP-2: `liquidation.py` — derive_supply_verdict 보정 입력인데 테스트 전무
