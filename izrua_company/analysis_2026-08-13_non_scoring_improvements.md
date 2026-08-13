# 비채점 영역 개선점 종합 보고서

> 분석일: 2026-08-13  
> 범위: 채점 산식·게이트 필터 제외 전 영역  
> 조사 영역: 수집 파이프라인, 모니터링, 알림 UX, CI/CD·인프라, 코드 품질

---

## P1 — 실제 장애/데이터 유실 위험

### 1. 주간 리포트 Telegram 4096자 초과 시 조용히 유실
- **파일:** `notify/telegram.py:186`, `render_weekly_report()` (L792-939)
- **문제:** `send()`에 길이 체크/분할 로직 없음. 작성자·섹션 증가 시 4096자 초과 → 400 Bad Request → 리포트 전체 유실 (로그만 남음)
- **수정:** send()에 길이 체크 + 섹션 단위 분할 전송

### 2. run_collect.py 뒷정리 구간 예외 미보호
- **파일:** `scripts/run_collect.py:585-587`
- **문제:** `reparse_all`, `expire_old`, `stats` 3줄이 try/except 없이 노출. 예외 시 차단 쿨다운 영속화·meta 정리·최종 커밋 전부 스킵 → 다음 회차가 밴 벽에 재요청 위험
- **수정:** 동일 파일의 다른 구간처럼 try/except 감싸기

### 3. Git 저장소 무한 증가 (levels.db 바이너리 커밋)
- **파일:** `.github/workflows/price-check.yml`
- **문제:** 2분마다 levels.db(1.1MB 바이너리) 커밋 → 3주간 커밋 9,014개, .git 26MB. 현 속도면 1~2년 내 1GB 도달
- **수정:** 장기적으로 DB 히스토리 축약 전략 필요 (주기적 squash, 또는 아카이브 분리)

---

## P2 — 운영 안정성·감지 능력

### 4. 텔레그램 단일 장애점 — 데드맨 스위치 부재
- **파일:** `notify/telegram.py:186`
- **문제:** 시스템 경보(수집정체·가격체크공백·체인무결성)가 전부 텔레그램 1채널 → 텔레그램 장애 시 "장애를 알릴 방법"도 같이 죽음
- **수정:** healthchecks.io 등 외부 핑 서비스로 데드맨 스위치 추가

### 5. requirements.txt 버전 상한 없음
- **파일:** `requirements.txt`
- **문제:** `curl_cffi>=0.7.0` 등 하한만 지정. TLS 지문 위장의 핵심 의존성인데 메이저 업그레이드가 조용히 들어올 수 있음
- **수정:** `pip freeze`로 lock 파일 생성 또는 최소 curl_cffi 상한 지정

### 6. 출처(🔗) 줄 32칼럼 잘림
- **파일:** `notify/telegram.py:137-150, 493`
- **문제:** `_MAX_LINE_COLS=32`로 다중 출처(합의 클러스터) 시 링크 2개 이후 잘림. HTML 태그 중간 절단 가능
- **수정:** 출처 줄만 폭 예외 처리 또는 링크 텍스트 축약

### 7. 감사 덤프 정체 감시 부재
- **파일:** `storage/audit_dump.py:55-56`
- **문제:** `META_LAST_DUMP` 메타키 기록은 하지만 정체 경보 로직 없음. 수집 정체(collect_stale)에는 이미 있는 패턴
- **수정:** `run_cycle.py`에 감사 덤프 정체 체크 + 경보 추가

### 8. 스택트레이스 손실
- **파일:** `scripts/run_cycle.py:191,213,285,293,375,509,540`
- **문제:** `logger.error(... type(e).__name__, e)` — 예외 메시지만 남기고 트레이스백 버림. `logger.exception` 사용처 1곳뿐
- **수정:** `logger.error` → `logger.exception` 전환 또는 `exc_info=True` 추가

### 9. 연속 발송 레이트리밋 사전 대응 부재
- **파일:** `monitor/price_check.py:1104-1110`
- **문제:** 다건 동시 TP 도달 시 연속 `send()` 호출 → Telegram 초당 1건 제한 초과 → 429 반복. 사후 재시도만 존재
- **수정:** 발송 간 최소 간격(1초) sleep 추가

---

## P3 — 코드 품질·유지보수

### 10. price_check.py `run_once()` 700줄 god function
- **파일:** `monitor/price_check.py:351-1050`
- **문제:** 감시·클러스터링·필터링·알림·후속조회가 단일 함수. 내부 클로저 4개. 테스트·추적 어려움
- **수정:** 단계별 분리 (클러스터링, 알림 발송, 후속 데이터 조회)

### 11. `_day_kst()` / `_KST` 5개 파일 중복
- **파일:** db.py, price_check.py, run_collect.py, run_cycle.py, announcements.py
- **수정:** `utils/time_kst.py` 모듈로 통합

### 12. 하드코딩 운영 상수 (settings.py 밖)
- **파일:** tradingview.py (`_BLOCK_COOLDOWN_SEC=1800`), price_check.py (`_RESEND_BLOCK_SEC=600`), run_cycle.py (`COLLECT_TIMEOUT_SEC=720`) 등
- **수정:** 최소 쿨다운 시간류만이라도 settings.py 이관

### 13. 일회성 repair 스크립트 5개 방치
- **파일:** `scripts/repair_*_20260726/27.py` 5개
- **수정:** `scripts/archive/`로 분리

### 14. secrets_status()에 TV_COOKIE 누락
- **파일:** `config/settings.py`
- **수정:** TV_COOKIE 진단 추가

### 15. 팔로워 캐시 TTL 문서 불일치
- **파일:** `collector/tradingview.py:114`
- **문제:** "7일 TTL" 문서화되었으나 subprocess 프로세스 종료 시 소멸 (실제 4h 수명)
- **수정:** 주석 정정 또는 디스크 캐시 전환

---

## 요약 매트릭스

| 우선순위 | 건수 | 핵심 |
|---------|------|------|
| P1 (장애 위험) | 3건 | 리포트 유실, 예외 미보호, Git 비대화 |
| P2 (운영 안정성) | 6건 | 데드맨 스위치, 버전 고정, 출처 잘림, 레이트리밋 등 |
| P3 (코드 품질) | 6건 | god function 분리, 중복 제거, 상수 이관 등 |
