# 2차 비채점 개선 세부분석 보고서

> 일시: 2026-08-13  
> 방법: 5개 병렬 에이전트 (안정성·관측성·설정/배포·데이터파이프라인·코드품질)  
> 범위: 채점·게이트 제외, 인프라/운영/코드품질 전영역  
> 중복 제거 후 총 **38건** (P1: 3, P2: 18, P3: 17)

---

## P1 — 즉시 조치 권장 (3건)

### P1-1. 시작 시 필수 시크릿 미검증 → 무음 알림 블랙아웃
- **파일:** `config/settings.py`, `scripts/run_cycle.py`
- `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` 누락·오타 시 `send()`가 False 반환만 하고 예외 없음 → 워크플로우 green 유지하며 알림 0건 발송
- **수정:** `run_cycle.main()` 최상단에 `secret("TELEGRAM_BOT_TOKEN", required=True)` 호출, fail-fast

### P1-2. universe.json 비원자적 쓰기 → 크래시 시 부패
- **파일:** `collector/coingecko.py:426-439` (`_save_cache`)
- 같은 파일 내 `_save_json_cache`는 tmp+replace 패턴 사용하나, `_save_cache`는 직접 `open("w")` → 프로세스 kill 시 truncated JSON
- **수정:** `_save_json_cache`의 tmp+replace 패턴 재사용

### P1-3. watcher_stats.py 무음 except → 팔로워/화이트리스트 소실 불가시
- **파일:** `collector/watcher_stats.py:115-116, 123-124`
- DB 읽기 3곳 중 2곳이 `except: pass` (로깅 없음), 스키마 변경 시 데이터 무음 소실
- **수정:** `logger.warning(...)` 추가

---

## P2 — 다음 배치 권장 (18건)

### 관측성 (5건)
| # | 파일 | 내용 |
|---|------|------|
| P2-1 | `run_collect.py` | 수집 사이클 소요시간 메트릭 미기록 (12분 timeout 접근 불가시) |
| P2-2 | `run_collect.py` | 개별 ingest 실패 카운트 미집계 (파서 회귀 불가시) |
| P2-3 | `run_collect.py` | TV/TG 차단 빈도·지속시간 트렌드 미기록 |
| P2-4 | `telegram.py` | `_split_send` 성공 경로 로그 없음 |
| P2-5 | `db.py:341-376` | `_migrate` 컬럼 추가 시 로깅 없음 |

### DB/스토리지 (2건)
| # | 파일 | 내용 |
|---|------|------|
| P2-6 | `db.py:210-219` | WAL 모드·busy_timeout 미설정 (동시 접근 시 locked 위험) |
| P2-7 | `db.py` 스키마 | `levels.author` 인덱스 누락 (주간리포트 풀스캔) |

### 코드 중복 (7건)
| # | 파일 | 내용 |
|---|------|------|
| P2-8 | `tradingview.py` ↔ `telegram_source.py` | HTTP 서킷브레이커 인프라 중복 (~200줄) |
| P2-9 | `tradingview.py` ↔ `telegram_source.py` | `_iso_to_epoch` 동일 함수 중복 |
| P2-10 | `binance.py:402-453` | 3개 ratio 함수 복붙 (endpoint·필드명만 다름) |
| P2-11 | `binance.py:171-326` | 3거래소 fallback 패턴 2회 수작업 반복 |
| P2-12 | `upbit.py` | 캔들 fetch 보일러플레이트 3회 반복 (~40줄) |
| P2-13 | `audit_dump.py` ↔ `run_cycle.py` | `_meta_float` 동일 헬퍼 중복 |
| P2-14 | `run_cycle.py` | `maybe_*` 5개 함수 동일 try/except 스켈레톤 반복 |

### 타입·품질 (2건)
| # | 파일 | 내용 |
|---|------|------|
| P2-15 | `price_check.py:350` | `float = None` → `Optional[float]` (타입체커 오류) |
| P2-16 | `extractor.py` | TP sanity 배수(0.25/4) 4곳 매직넘버, 상수 미추출 |

### 테스트·배포 (2건)
| # | 파일 | 내용 |
|---|------|------|
| P2-17 | `check_secrets.py` | 시크릿 스캐너 전용 테스트 없음 (회귀 시 유출 위험) |
| P2-18 | 프로젝트 루트 | dependabot.yml 없음 (보안 패치 자동 감지 불가) |

---

## P3 — 기회 시 개선 (17건)

| # | 파일 | 내용 |
|---|------|------|
| P3-1 | `alerts_log` 테이블 | 보존 정책 없음 (무한 증가) |
| P3-2 | DB | VACUUM/auto_vacuum 없음 (prune 후 공간 미회수) |
| P3-3 | DB 스키마 | direction/status CHECK 제약조건 없음 |
| P3-4 | DB | 스키마 버전 추적 없음 (user_version 미사용) |
| P3-5 | `cache/`, 프로젝트 루트 | 테스트 잔여 파일 미정리 (tempfile 미사용) |
| P3-6 | `tradingview.py` ↔ `telegram_source.py` | `_strip_html` 중복 (구현 분기) |
| P3-7 | `coingecko.py:171-179` | 캐시 로드 무음 예외 삼킴 |
| P3-8 | `binance.py:82-83` | 펀딩맵 로드 무음 `pass` |
| P3-9 | `show_status.py:555` | `_fetch_live_prices` 무음 예외 |
| P3-10 | `watcher_stats.py:15` | 미사용 import (`Optional`) |
| P3-11 | `telegram.py:35` | 미사용 import (`distribution`) |
| P3-12 | 전역 | `conn` 파라미터 `sqlite3.Connection` 타입힌트 누락 |
| P3-13 | `coingecko.py`, `upbit.py` | 반환타입 bare `dict`/`list` (TypedDict 미사용) |
| P3-14 | `telegram.py:200` | `urgency` 파라미터 `Literal["high","low"]` 미적용 |
| P3-15 | `db.py:1495-1500` | `record_touch_verdicts` 타입힌트 전무 |
| P3-16 | `db.py` | `[dict(r) for r in ...]` 패턴 25회+ 반복 |
| P3-17 | `price-check.yml` | 구버전 마이그레이션 스텝+`actions:read` 권한 잔존 |

---

## 1차 배치(완료)와의 관계

1차 15건 중 12건 코드 적용 완료. 2차 38건은 1차와 **중복 없음** (1차 미완 #3 Git 성장·#10 god function·#12 하드코딩 상수는 P2-14/P2-16으로 재분류).

## 권장 실행 순서

1. **즉시:** P1 3건 (시크릿 검증, 원자적 쓰기, 무음 except) — 각 5줄 이내 수정
2. **다음 세션:** P2 관측성 5건 + DB 2건 — 운영 가시성 대폭 향상
3. **리팩터링 세션:** P2 중복 7건 — 코드량 200줄+ 절감, 유지보수성 개선
4. **기회 시:** P3 — 타입힌트·정리 사안
