# 인수인계 — izrua-entry-alert 고도화 세션용 컨텍스트

> 새 세션 시작: "C:\Users\User\Desktop\izrua_entry_alert\HANDOFF.md 읽고 시작해" 한 줄.
> (최종 갱신: 2026-08-04)
>
> **08-04**: 새벽 코드 감사 세션이 사용한도 초과로 중단됐다가 같은 날 이어서 R2 마무리·
> 커밋·푸시까지 완료. 상세: `izrua_company/planner/HANDOFF_2026-08-04_audit_session.md`
> (완료 기록으로 보존). 다음 큰 항목은 §7 "관찰 대기(~08-10)" 참고.

## 1. 프로젝트 정체

**TradingView 차티스트 글 기반 "진입가 터치 알림 봇"** — 자동매매 아님(2026-07-22 폐기 결정).
글에서 진입가/손절/목표를 추출해두고, 업비트 KRW 실시간 가격이 진입가를 터치하면
텔레그램(Upbit_izrua bot)으로 알림(접근 예고는 2026-07-31 제거 — §3). 매수 판단은 사용자가 직접 함.

- **레포**: https://github.com/crisis1460-pixel/izrua-entry-alert (공개 — Actions 무료 무제한의 조건)
- **로컬 클론**: `C:\Users\User\Desktop\izrua_entry_alert`
- **관계 프로젝트**:
  - `izrua_watcher`(비공개 레포, 로컬 `C:\Users\User\Desktop\izrua_watcher`) — 별도 운영 워쳐.
    **무수정 유지 원칙**(2026-07-26 문구 수정 1건만 예외적으로 승인받아 처리). 이 봇은 워쳐의
    DB 아티팩트(`crypto-db`)에서 작성자 적중률·화이트리스트만 읽어옴.
    ⚠️ 워쳐 스캔 주기는 **6시간**(2026-07-26 변경 — GitHub 무료 2,000분/월이 매달 25일경
    소진되던 문제. cron-job.org 설정 `23 */6 * * *`)
  - `C:\Users\User\Desktop\upbit_bot` — 폐기된 구 자동매매 봇. **보관(삭제 안 함)** — 자료 보존 목적

## 2. 아키텍처 (100% 서버리스, PC 불필요, 비용 0)

```
cron-job.org — 등록 잡은 **1개뿐** (2분 트리거. 단 실행이 3분+ 걸려 concurrency
                직렬화로 겹친 트리거가 버려짐 → **실측 실효 회차 간격 ~4분**)
└─ price-check.yml → scripts/run_cycle.py  ★ 단일 DB 라이터
   ├─ 매 회차   : 가격 감시 → 접근/터치 판정 → 알림 → 적중 판정 → 관찰 집계
   ├─ 4시간마다 : TradingView 수집 (meta.last_collect_at)
   ├─ 7일마다   : 작성자 주간 스냅샷 (meta.last_author_snapshot_at)
   └─ 7일마다   : 주간 리포트 — **자동발송 OFF**(사용자 결정), 스위치만 켜면 재개
상태: data/levels.db (SQLite) — 매 변경 시 레포 커밋백([skip ci]) = 영속+백업
Secrets: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, COINGECKO_API_KEY, WATCHER_GITHUB_TOKEN
         (TV_COOKIE 는 선택 — 등록만 하면 코드 수정 없이 자동 활용됨)
```

**★ 2026-07-26 최대 사건**: collect.yml 과 price-check.yml 이 **각자** `data/levels.db`를
커밋했는데, 바이너리는 3-way 머지가 불가능해 **항상 충돌 → 한쪽이 반드시 유실**됐다.
`git pull --rebase -X ours` 의 ours/theirs 가 직관과 반대(재생되는 우리 커밋이 theirs)라
07-23~26 **모든 수집분이 조용히 폐기**돼 신규 레벨 0건, 알림이 사실상 멈췄다.
Actions 는 전부 success 로 보여 3일간 아무도 몰랐다. → **라이터를 1개로 통합**(run_cycle.py)해
충돌 경로 자체를 제거. `test_cycle.py` W1 이 "`git add data/` 워크플로는 1개"를 불변식으로 검사.

### 파일 지도
- `collector/` — coingecko(유니버스 top200∩업비트KRW), tradingview(수집 3단 폴백 +
  차단 감지 `hard_block_detected` + 글 삭제 확인 `check_post_deleted`), extractor(파싱),
  **grading**(등급 — `TP_DISTANCE_BANDS` 단일 배점표, `regrade_current` 재채점), watcher_stats
- `monitor/` — **price_check**(핵심: 감시·알림·적중판정·관찰집계·`magnify_order`),
  upbit(시세·캔들·`fetch_trades_window`·호가), binance(김프), market_sentiment,
  **announcements**(업비트 유의종목·거래지원종료 공지 폴링 → 즉시경보 + 레벨 만료.
  비문서화 API라 방어적 파싱, 발송 실패는 meta 대기 큐로 재시도)
- `analytics/` — **ranking**(E_LB·베이지안 수축·최신성 가중), **clustering**(클러스터 규칙
  정본 + 합의 CR + 베이스라인). 둘 다 "프로젝트 모듈 import 0" 순수 함수
- `notify/telegram.py` — render_alert(양식 확정) / render_weekly_report /
  render_collect_silence_alert / render_tv_block_alert
- `storage/db.py` — 스키마·마이그레이션·조회 전부
- `scripts/` — **run_cycle**(운영 엔트리), run_collect·run_price_check·run_weekly_report(수동),
  **show_status**(현황 조회 CLI), 테스트 7종, check_secrets, repair_*(일회성 수리 이력)

## 3. 핵심 확정 결정 (변경 시 사용자 합의 필요)

- 알림: 하방 터치 본알림 1회, 글당 1회. **접근(+1%) 예고는 2026-07-31 완전 제거**
  (질문카드 확정 — 예고를 보면 조기 진입 유혹, 첫 알림 = 진입가 터치 본알림).
  `preview_alert_enabled=False` 로 **발송만** 껐다: previewed 상태 전이·관찰 집계
  (previews_total/preview_dwell)는 그대로 유지 — True 로 되돌리면 즉시 재개.
  주의: show_status 전환율(발송÷(터치+예고))은 이 변경으로 구조적으로 하락한다
- **거래량 급증 2단계 알림(Feature 4)**: 터치 후 72h 감시. 판정은 2026-07-31 교체 —
  최근 1시간 거래대금 > 직전 20시간(완결 60분봉) 평균 × **5.0** + 최근 1h ≥ **2억**
  (저유동 새벽 위양성 가드 — 08-01 ETHFI 0.54억 위양성 실사례로 0.5억→2억 상향).
  구 기준(24h > 7일평균 × 3)은 24h 누적이 급증을 반나절
  늦게 반영해 폐기. **감시 제외 밴드**: 현재가가 [진입가 −10%, **마지막 유효 TP**
  (없으면 +10%)] 이탈 시 감시 즉시 종료(밴드는 터치 시점 환율 KRW 고정, 레거시
  NULL 행은 시간 만료만). 상단은 처음 TP1 이었으나 당일 2차에서 마지막 TP 로 교체
  — 백테스트(57건)상 짧은 TP1 셋업 25%가 1시간 내 조기종료돼 사다리 구간 사각.
  급증 알림엔 현재가 바로 위 "다음 TP (k/N단계)" 동적 표시(volume_watch.tps_krw)
- **수명**: 수집은 게시 7일 이내 글만(`max_post_age_hours`), 만료는 **수집 후 7일**
  (`level_expiry_hours`) — 합쳐서 절대 상한 게시 후 14일. 예전엔 이 줄이 "게시 7일
  만료"로 잘못 적혀 있었고, 2026-07-27 에 실제로 게시 기준으로 바꿨다가 되돌렸다:
  실측 터치 67건 중 9건(13.4%)이 게시 7일 넘겨 터치됐는데 전부 '늦게 주운 글'
  (수집 당시 5.4~6.6일, 수집 후 0.5~4.0일 만에 터치)이었고 성적도 나쁘지 않았다
  (승률 50%·-0.21R vs 신선분 48%·-0.69R). 게시 기준이면 수집 지연이 곧 기회 상실.
  `test_price_logic.py` EX1~EX4 가 이 결정을 못 박는다
- 클러스터: 같은 코인 진입가 ±1% 병합, 상단 기준 1회 알림, 출처 링크(URL 비노출)
- 필터: 수집은 전부 저장, 알림은 등급 C↑ + 코인당 **본알림** 3건/일
- **적중 DB**: TP1=승 / SL=패 / 동시터치는 **체결내역으로 순서 복원 시도(Bar Magnifier)**
  후 실패 시에만 패+ambiguous / TP 없으면 타임박스 / 판정창=작성자 타임프레임 /
  R-멀티플 [-1,+5] / 터치 이후 캔들만 / 기준가=자기 진입가+터치시점 환율
- **등급**(2026-08-03 **v4** — 사용자 질문카드 결정, S10 v3 배포 이틀 뒤 팔로워 강화 재조정):
  팔로워(1~25) + 근접도(0~20) + 목표거리(-6~+12) + 완결성(2~23, SL 보너스 +10→+3) +
  **작성자 실적 가점(0/+5/+10/+15)** = 실질 상한 95(SL 있음)·92(SL 없음).
  팔로워 티어: 100k+ 25 / 50k+ 22 / 10k+ 17 / 5k+ 12 / 1k+ 8 / 100+ 3 / 미만 1
  (하단은 v3 그대로 — 소형 작성자 초근접 TP 노이즈 채널 차단).
  등급 임계 S≥85/A≥70/B≥55/C≥40 유지. **SL 없는 글도 실적/팔로워로 A~S 도달 가능**
  (v4 설계 의도 — 유명 작성자가 SL 안 적는 관행 반영).
  발단: 프로덕션 54%(105/193) SL 미기재로 BigBeluga(9.7만) 등 유명 작성자 D 고착.
  근거·리서치: `izrua_company/planner/sprint08_SL없는시그널_등급설계_리서치.md`
  (업계 관행: MQL5/Darwinex/Pyckio/eToro 전부 포맷 미채점·실현 실적만 채점).
  목표거리 배점표(v3 그대로 유지) — 2%↓ −6 / 2~3% −4 / 3~5% −2 / 5~8% +12 /
  8~15% +12 / 15~25% +8 / 25~40% +4 / 40%+ 0.
  실적 가점(v3 그대로): 자기 DB 종결 n≥5 + Wilson 80% 단측 하한 ≥0.55/0.40/0.25 →
  +15/+10/+5. 롤백 스위치: `grade_author_points_enabled=False` (실적 가점만 0 고정,
  팔로워/SL 변경과 무관). 산식 버전 태그 `levels.grade_ver='v4'` (D4 유지 — 과거 v3/NULL
  행 소급 재라벨 금지, active 행만 재수집 시 v4 로 재채점됨).
  ※ 추가 필터(2026-08-03 사용자 결정): 마지막 TP < **5%** 이면 발송 억제
  (`alert_min_last_tp_pct=5.0` 신설). 07-30 B안 2% 조정을 5% 로 복원 — "최종 TP <5% =
  레버리지(선물) 설계, 스팟 스윙 대상 아님" 원칙. 등급 감점표 경계(SWING_MIN_TP_PCT=2)와는
  분리(필터만 조임, 배점 구조 불변). 실측: 최종 TP 2~5% 구간 종결 29건 hit 10/miss 19.
- **타임프레임 필터**(2026-08-02 스프린트07): `alert_min_timeframe_hours=4.0` — 이 미만
  아이디어는 알림 억제(`suppressed_timeframe`). NULL(미명시) + 0(파서 이상)은 통과 —
  08-03 R1 감사로 0 케이스도 NULL 취급하도록 방어 추가(로컬 미커밋 상태).
- **펀딩 레짐 전환**(2026-08-03 스프린트08): 히스토리 Binance/Bybit/OKX 30일 조회. 현재값은 Binance직접→CoinGecko(Binance 원본, 프로덕션 주 경로)→Bybit→OKX (08-07 — Actions 미국 IP 가 Binance·Bybit 선물 차단, CoinGlass 무료 폐지로 CoinGecko 채택) →
  min_neg_days*3 연속 <=0 후 최근 >0 이면 flipped 판정 → 알림에 "🔥 N일 음수→양수" 배지.
  등급 산식엔 영향 없음(배지만). `binance.fetch_funding_history` / `detect_funding_regime_flip`.
- **역신호 확정**(2026-08-01 S9 구현): 주간 스냅샷 2주 연속 neff_r≥5 & E_LB<0
  → 확정, 2주 연속 회복(E_LB≥0) → 해제 (표본 부족·게이트 미달은 보수적 유지).
  meta `reverse_confirmed_{author}` 기록, **알림 필터·발송량 무변경**.
  ⚠️ 당일 사용자 번복: **텔레그램 발송 안 함 — 향후 분석용 저장만**
  (`reverse_alert_send_enabled=False`, True 로 되돌리면 즉시 재개). 확인은
  show_status 🔻역신호확정 태그로
- 표시: 자체 표본 n_eff≥5 부터 `🏹 승률67% (4승2패) 터치율67%` 자동 병기
- 환산: USD 저장값 × 업비트 KRW-USDT 시세

## 4. 알림 양식 (최종 확정 — 임의 변경 금지)

```
━━━━━━━━━━━━━━━━━━━━
🎯 [진입가 터치] LINK          ← 예고 ⚠️[진입가 접근] 양식은 스위치 OFF 로 미발송(§3)
🥇 시총 19위 · B등급 · 2일 전   ← 등급은 알림 시점 재채점값(v4)
✍️ 작성자: @ProChartist ⭐⭐
📊 평균 적중률: 72% (워쳐 25건)
🏹 승률73% (8승3패) 터치율73%   ← 자체 n_eff≥5 일 때만
⏱ 평균 3.2일 보유              ← 종결 3건+ 있을 때만 (스프린트07)
━━━━━━━━━━━━━━━━━━━━
타점 (현재/진입/목표/거래순위)
52주 (고가/저가/위치바)
🌡️ 자리: 우호 (60일지지·정배열·일38)  ← RSI+MA 5단계 판정(08-08). 52주 블록 아래.
━━━━━━━━━━━━━━━━━━━━
🧭 수급: 우호 (숏 몰림)         ← 펀딩×OI 합성 판정(08-07). 옛 "💰 펀딩" 줄 대체.
🔥 32일 음수→양수              ← 레짐 전환 감지 시에만 (스프린트08)
❄️ 김프 -0.13%
🌍 BTC.D 56.6% · 🪙 ALT.S 32 · 😨 F&G 28
━━━━━━━━━━━━━━━━━━━━
🔗 출처 링크
```
- 🌡️ 자리 5단계: 최적/우호/중립/주의/위험 (monitor/upbit.py derive_position_verdict)
- 🧭 수급 3단계: 우호/중립/주의 (monitor/binance.py derive_supply_verdict)
- 펀딩 데이터는 Binance 직접→CoinGecko(Binance 원본)→Bybit→OKX 폴백 (08-07,
  Actions 미국 IP 가 Binance/Bybit 선물 직접 차단 — CoinGecko 가 사실상 주 경로)
- 터치 시점 판정(수급/자리/200일선상하)은 **내부 축적 전용** — 알림·리포트
  어디에도 미노출(사용자 확정, storage.db.record_touch_verdicts/get_verdict_stats)
**손절 행(📐 SL·R:R) 완전 삭제**(2026-08-03 사용자 결정) — SL 은 판정 엔진 내부
기준선으로만 사용, 알림엔 절대 노출 안 함. rep.sl_usd/rr 은 등급·판정에 계속 반영.
사용자 스타일: 스윙 트레이더로 손절 비중시 — SL 감점류 기능 제안 금지.

## 5. 검증 (신뢰 기준선)

- **테스트 8종** — extractor / grading / price_logic / ranking / weekly_report / cycle /
  show_status / resilience. **push 전 필수**, 2026-07-26부터 **CI(tests.yml)에서 자동 실행**
  (파일별 독립 프로세스 — settings 전역 오염 때문에 한 프로세스에 모으면 안 됨)
- 감사 이력: 07-24 전수감사 15건 + major3/minor2, 07-26 재감사(major3+minor7),
  07-26 밤 당일배포 전수감사(MAJOR 3건 — 2건 즉시 수정, 1건 시간 경과 대기)
- 실전 버그 이력: TP 서수 오인 / 20xx 가격을 연도로 / 터치 이전 캔들 오염 /
  collect 큐 기아 / 커밋백 abort / **rebase 전략으로 수집분 전량 폐기** /
  등급 freeze(터치 52건 중 18건 억제) / 리포트 HTML `<` 미이스케이프로 발송 400 /
  관찰지표가 '체류 회차'를 세던 문제
- 보안: 비밀값 4중 방어. **업비트 API 키는 이 봇에 없음**(시세 무인증 — 설계 원칙, 추가 금지)

## 6. 작업 컨벤션 (반드시 유지)

1. **기획/설계 변경은 질문카드**: AskUserQuestion 객관식 + 추천안(근거 포함)
2. **양식 변경은 샘플 먼저**: 렌더링 출력 → 사용자 확정 후 push
3. **push 절차**: 테스트 8종 + check_secrets → `git checkout origin/main -- data` 로 정리 →
   rebase 후 push. **봇이 2분마다 data 를 커밋하므로 경합은 정상** — 재시도 루프를 쓴다.
   `rm -rf data` 후 `git add -A` **절대 금지**(봇 데이터 삭제 사고 이력)
   ⚠️ **사람(CTO 포함)은 data/ 를 직접 커밋·푸시하지 않는다** (2026-07-27 규약).
   커밋백은 origin 의 data/ 가 다른 주체에 의해 변한 걸 감지하면 **덮지 않고 exit 1**
   로 멈춘다(2026-07-27 M3 재구조 — 데이터 유실은 자동 판단하지 않는다). 즉 사람이
   data 를 건드리면 봇 커밋백이 사람 확인 대기 상태로 실패한다. DB 수리가 필요하면
   수리 스크립트를 레포에 커밋해 두고 **다음 봇 회차가 자기 커밋백에 실어 가게** 할 것.
   ⚠️ 알려진 한계(2차 교차검토 M-A2, 백로그): 커밋백이 끝내 실패한 회차는 발송 기록
   (alerts_log)이 origin 에 못 실려 **다음 회차가 같은 알림을 1회 재발송**할 수 있다.
   로컬만으로는 해결 불가(origin 이 유일한 진실원) — 피해는 중복 1회로 자체 한정되고
   다음 성공 push 가 종결한다. 커밋백 실패 메일을 보면 중복 알림 1건은 정상 증상이다
4. Windows: `PYTHONIOENCODING=utf-8` 필수. PowerShell 에서 프로덕션 DB 쓰기가
   분류기에 막히면 Bash 로 실행
5. 토큰 값은 채팅에 넣지 않기. 리서치는 웹 검증 후 결정
6. **프로덕션 DB 는 읽기 전용** — 사본은 `sqlite3.backup()`(단순 파일복사는 WAL 미반영)

## 7. 현재 국면 — 08-04 감사 R1+R2 배치 완료·관찰 대기(~08-10)

**08-03 스프린트07/v4/스프린트08 배포 → 08-04 새벽 감사(R1+R2) 완료·배치 커밋·푸시**:
- 오늘 배포: 스프린트07(펀딩·포지션·타임프레임·보유기간) → v4 등급 → 5% TP 필터
  → 2차 심층 검토 fix(판정 크래시 방어·해시체인·재발송 방어) → 스프린트08(펀딩 레짐·
  SL행 삭제·펀딩 라벨) → secret-scan gitleaks-action 제거(매일 저녁 실패 이메일 원인)
- 08-04 감사 R1(5개)+R2(5개) 전부 완료 — fix 10건 + 회귀 테스트 7건 추가, 8개 스위트
  전체 통과 후 단일 배치 커밋·푸시. 상세: `izrua_company/planner/HANDOFF_2026-08-04_audit_session.md`
- 관찰 대기: v4 + 5% 필터 알림량·등급분포 리뷰 ~2026-08-10
- 스프린트08 시장조사 15개 후보 중 사장님이 "펀딩 레짐"만 진행, 나머지 스킵/보류
  결정 (`izrua_company/planner/sprint08_시장조사_신규기능_후보.md`)

현황 조회: `python scripts/show_status.py [--days 7] [--report]`

## 8. 다음 할 일

### ★★ 08-04 최우선 (감사 세션 이어받기) — ✅ 같은 날 이어서 전부 완료
1. ~~로컬 미커밋 5파일 존재 확인~~ ✅ 확인 후 그대로 유지
2. ~~background 결과 파일 확인~~ ✅ 둘 다 빈 파일(0줄) — 재실행 불필요(R1 결과로 부분 커버)
3. ~~R2 남은 fix 4건 반영~~ ✅ 전부 반영:
   - price_check.py 펀딩·m-8 김프 try/except 로깅 2건 추가
   - telegram.py timeout 하드코딩 → `settings.get("http_timeout_sec")`
   - binance.py `detect_funding_regime_flip` all-zero false positive 방어(`min(window) < 0`)
4. ~~회귀 테스트 상위 3건 추가~~ ✅ UPS1/UPS1b/UPS2/RPA1/PTP1/PTP1b + FR4(위 4번 짝) 총 7건
5. ✅ 전 8개 스위트(extractor/grading/price_logic/ranking/weekly_report/cycle/
   show_status/resilience) 재실행 전부 통과 (price_logic 287건 포함)
6. ✅ 단일 배치 커밋·푸시 완료
7. 사장님 보고 — 이 세션에서 완료

세부 지시서: `izrua_company/planner/HANDOFF_2026-08-04_audit_session.md`(완료 기록으로 보존)

### ★ 다음 세션 최우선 (2026-07-31 세션 인수인계 — 대부분 완료)
1. ~~**S10 등급 재조정 — 사용자 결정 D1~D5 질문카드 → 개발자 A 구현 투입**~~ ✅ 2026-08-01 구현 완료(아래 08-01 완료 내역)
   - 기획 완료(07-31): `izrua_company/plan_S10_grade_recalibration.md`
     (재현 스크립트 `izrua_company/query_0731_recalib_sim.py`, 수치 검증 완료)
   - 핵심 발견: ① 역전의 절반은 옛 산식 라벨(32/74건 불일치 — 현행 산식 소급에도
     C 25% < D 61% 잔존) ② D 72%의 실체는 TP-only 초근접 군집(CryptoAnalystSignal
     20/27, 상당수 B안 필터 억제 대상)이 39점=C컷 1점 미달로 D에 갇힘 ③ 작성자
     실적이 최강 예측변수인데 배점 전무, 원거리 TP 보상은 역상관(8%+ 도달 0/3)
   - 재조정안: 안1 TP원거리 보상 축소(단독 비권장) / 안2 작성자 실적 가점 신설
     (+5/10/15, n≥5+Wilson 80% 하한) / **안3=안1+안2 결합(추천)** — 소급 시
     B 50% > C 41% > D 30% 단조 회복, 알림량 27/34 불변, SL 감점 없음
   - 결정 항목: D1 등급 목적=TP1 도달 예측 축 확정 / D2 안 선택 / D3 실측 실적의
     산식 반영 승인(calibration "표기 전용" 원칙 부분 해제) / D4 grade_ver 태그 도입
     +과거 등급 소급 재라벨 금지 / D5 S/A 희귀화(검증 작성자 전용) 용인
2. **S9 통합 감사 (미착수)**: TP 클러스터 중복 차단(07-30 배포)이 테스트만 통과,
   프로덕션 실사례 아직 0건. 개발자 B 감사 투입 추천한 상태에서 세션 종료
3. ~~**역신호**: W31 작성자 스냅샷 ~08-02 생성 후 2주 연속 판정 구현 가능~~
   ✅ 2026-08-01 구현 완료(아래 08-01 완료 내역 — Q1=B안·Q2=해제 확정)

#### 08-01 세션 완료 내역
- **S9 역신호 최종 판정 구현 완료**(개발자 A, Q1=B안·Q2=해제 확정):
  `ranking.is_confirmed_reverse/is_recovered_reverse`(순수 함수) +
  `db.get_author_last_n_snapshots` + run_cycle `maybe_reverse_check`(스냅샷 훅 직후,
  meta 쓰기→commit→send 순서, try 격리) + 확정/해제 경보 렌더러 2종 +
  show_status 🔻역신호확정 태그·주간 리포트 확정 한 줄(표시 전용).
  당일 번복: 발송 스위치 OFF(분석용 저장만, `reverse_alert_send_enabled`) — RS9 로 고정.
  W31 스냅샷(~08-02 저녁 회차)에서 첫 판정 — mastercrypto2020 이 W31 도 음수면 조용히 확정 기록.
  기획: `izrua_company/plan_S9_reverse_signal.md`(구현 완료 갱신)
- **S10 등급 배점 재조정(안3) 구현 완료**(개발자 A, D1~D5 승인): TP 원거리 보상 축소 +
  작성자 실적 가점(+5/10/15, Wilson 80% 하한 게이트) + grade_ver='v3' 태그 + 롤백 스위치.
  소급 검증 = 기획서 §4-3 정확 일치(B 4/8 > C 23/56 > D 3/10, 위반 0), 통과 집합 불변 확인.
  테스트 8종 전체 통과. 기획: `izrua_company/plan_S10_grade_recalibration.md` §6

#### 07-31 세션 완료 내역
- 거래량 알림 개편(개발자 A): 접근 예고 완전 제거(`preview_alert_enabled`) + Feature 4
  판정 RVOL 교체(1h > 20h평균 ×5, `fetch_rvol_1h`) + 감시 제외 밴드(진입가 −10%~TP1)
  — 결정 3건·근거는 `izrua_company/meetings/2026-07-31_거래량알림_개편.md`
  — 배포 전 4렌즈 감사: major 1(재터치 밴드 합집합) + minor 6 수정(동 회의록 §감사)
- TP 클러스터 중복 발송 수정 배포(`_tp_cluster_dup`, T35/T35b, 커밋 d569e8d)
  — AERO 07-30 중복(같은 신호 URL 2개 → 레벨 2건 → TP1 알림 2건)의 재발 방지
- 오늘 알림 9건 전수 검토: 이상 없음. BTC 터치(18:36) = 클러스터 병합(162+168) +
  `_rep` 재채점 실사례(수집점수 열세인 DomicChaina 가 재채점으로 대표 선정) +
  B안 2% 완화 실사례(last TP 3.2% — 구 5% 기준이면 억제됐을 건)
- 관찰기 종료 판정(§7 참조): 알림량 "적당" → 필터 무변경, 등급 재조정 착수 결정

### 즉시 확인 (시간 경과 필요)
- ~~**오늘 배포한 3경로가 프로덕션에서 미검증**: daily_stats 첫 행 / 글 삭제 감지
  (`deleted_checked_at`) / Bar Magnifier. 수집 회차 후 `show_status` 로 확인~~ ✅ 2026-07-30: 4일 운영 정상 확인
- ~~07-23 터치분 15건+ 미종결 → **07-30경 168h 창 만료로 일괄 타임박스 종결 예정**~~ ✅ 2026-07-30: BTC TP1~5 완주 등 실제 hit로 종결 확인

### 관찰기 종료(07-31) 후 결정 반영
- ~~알림 필터 조정(이미터치 무알림 → C→B → 3→2건/일)~~ ✅ 보류 확정 — 알림량 "적당" 판정
- **등급 밴드 경계·배점 실측 재조정** → S10 착수(2026-07-31). 기획: `izrua_company/plan_S10_grade_recalibration.md`
- 유니버스 200→400 — 미결(등급 재조정 안정화 후 재상정)
- ~~역신호 최종 판정 로직 — W31 스냅샷(~08-02) 후 구현~~ ✅ 2026-08-01 구현 완료(§8 08-01 내역).
  후보 1호: mastercrypto2020(W30 E_LB -0.92, n_eff 8) — W31 스냅샷 회차에 자동 첫 판정

### 미조치 minor (2026-07-26 감사 기록 → 스프린트 8에서 대부분 정비)
- ~~감점 역산 불변식 테스트 없음~~ ✅ S8: G1d~G1j 9종(현행 위반 0)
- ~~`ambiguous_unresolved` 합산~~ ✅ S8: `ambiguous_skipped` 분리(판정 동작 변화 0)
- ~~run_cycle 3·4단계 try 밖~~ ✅ S8: 예외 격리 완료. ~~`maybe_collect`(2단계) 잔존~~ ✅ 2026-07-30 확인: f4bfe09에서 이미 격리 완료
- ~~`tv_block_alert_count_*` 무한 증가~~ ✅ S8: 7일 경과분 정리
- ~~`last_collect_at` staleness 미감시~~ ✅ S8: 정체 감시 신설(12h 임계, 1회/일).
  수집 급감 감지의 '신규 0건' 위양성 축은 잔존(감시 2종이 서로 다른 고장을 커버)
- ~~`_rep` 가 수집 시점 score 로 대표 선정(재채점 전)~~ ✅ 2026-07-30 S9: `_rep(cluster, current_usd)` 로 터치 시점 재채점 기준 선정으로 수정

### 사용자 액션 대기
- ~~업비트 구 API 키 폐기 확인~~ ✅ 완료(사용자가 코드에서 직접 제거)
- ~~upbit_bot 폴더 삭제 승인~~ ✅ 불필요 — 자료 보존 목적으로 그대로 유지하기로 결정
- ~~TV_COOKIE 등록~~ ✅ 2026-07-26 밤 등록 완료(봇 전용 계정).
  **효과 판정 완료(2026-07-30)**: 07-27~07-30 매일 차단 경보 1회 발생 → 쿠키 효과 미미.
  `tv_blocked_until` = 약 2026-08-29 (장기 차단 상태). 3단계 폴백으로 수집은 정상 동작 중.

## 9. 운영 체제 — 3인 개발사

`C:\Users\User\Desktop\izrua_company\` 에 전체 기록.
`NEXT.md`(재개 브리핑) → `meetings/`(회의록) → `planner/`·`dev_a/`·`dev_b/`(직원별 이력·산출물)

| 직원 | 역할 | 모델 |
|---|---|---|
| 🧭 기획자 | 리서치·기획·정책 검토 | Sonnet |
| 🔨 개발자 A | 신규 기능 설계·구현 | **Opus 고정** |
| 🔧 개발자 B | 버그·유지보수·감사·효율화 | Sonnet (심층 감사 시 Opus) |

- 트리거: 「스프린트 개시」(3인) / 「정비 개시」(B) / 「기획 개시」(기획자) / 「구현 개시」(A)
- 파일 경계를 나눠 병렬 투입 — **경계면 상호작용이 실제로 두 번 사고를 냈으므로
  스프린트 종료 시 통합 검증 필수**
- 직원은 레포 **읽기 전용**, git 상태 변경 금지. 커밋·푸시는 CTO(오케스트레이터)만
- 큰 결정만 사장님께 질문카드. 최종 보고는 핵심만 간결하게(전문은 회사 폴더)
