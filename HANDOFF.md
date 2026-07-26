# 인수인계 — izrua-entry-alert 고도화 세션용 컨텍스트

> 사용 방법: 새 세션 시작 시 이 파일 내용을 통째로 붙여넣거나,
> "C:\Users\User\Desktop\izrua_entry_alert\HANDOFF.md 읽고 시작해" 한 줄로 시작.
> (작성: 2026-07-24, 직전 세션에서 개발·검증 완료 후 작성)

## 1. 프로젝트 정체

**TradingView 차티스트 글 기반 "진입가 터치 알림 봇"** — 자동매매 아님(2026-07-22 폐기 결정).
글에서 진입가/손절/목표를 추출해두고, 업비트 KRW 실시간 가격이 진입가에 접근(+1%)/터치하면
텔레그램(Upbit_izrua bot)으로 알림. 매수 판단은 사용자가 직접 함.

- **레포**: https://github.com/crisis1460-pixel/izrua-entry-alert (공개 — Actions 무료 무제한의 조건)
- **로컬 클론**: `C:\Users\User\Desktop\izrua_entry_alert`
- **관계 프로젝트**:
  - `izrua_watcher`(비공개 레포) — 별도 운영 중인 워쳐. **무수정 유지 원칙**. 이 봇은 워쳐의
    DB 아티팩트(이름 `crypto-db`)에서 작성자 적중률·화이트리스트만 읽어옴 (스키마:
    chartist_stats(username, outcome∈hit/miss) 집계 = hit/(hit+miss))
  - `C:\Users\User\Desktop\upbit_bot` — 폐기된 구 자동매매 봇. **삭제 대기**(사용자 승인 필요,
    결정 #10). 로컬 `.env`에 텔레그램 토큰 등 있음(테스트 발송 시 load_dotenv로 사용)

## 2. 아키텍처 (100% 서버리스, PC 불필요, 비용 0)

```
cron-job.org (사용자 계정, 워쳐와 같은 패턴) — 등록 잡은 이제 **1개뿐**
└─ price-check.yml 트리거: 2분마다 → scripts/run_cycle.py (단일 DB 라이터)
   ├─ 매 회차: 업비트 시세로 접근/터치 판정→알림→적중판정
   ├─ 4시간마다(meta.last_collect_at): TradingView 글 수집→추출→등급→DB 저장
   └─ 7일마다(meta.last_weekly_report_at, KST 09~22시): 주간 성적 리포트 발송
   ※ 2026-07-26: collect.yml 폐지. 두 잡이 각자 levels.db(바이너리)를 커밋해
     항상 충돌→한쪽 유실되던 구조를 라이터 1개로 정리(장애 26ac522 근본 해결).
상태: data/levels.db (SQLite) — 매 변경 시 레포에 커밋백([skip ci]) = 영속+백업
Secrets: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, COINGECKO_API_KEY, WATCHER_GITHUB_TOKEN
```

파일 지도: `collector/`(coingecko 유니버스=top200∩업비트KRW·스테이블 제외, tradingview 수집
3단 폴백, extractor 파싱, grading 등급, watcher_stats), `monitor/`(price_check 핵심 로직,
upbit REST, binance 김프용, market_sentiment BTC.D/ALT.S/F&G 1h캐시), `notify/telegram.py`
(렌더러+발송), `storage/db.py`, `scripts/`(run_cycle=운영 엔트리포인트, run_collect·
run_price_check·run_weekly_report=수동/하위 단계, 테스트, check_secrets), `ALERT_BOT_PLAN.md`·`ACCURACY_DB_PLAN.md`(확정 기획서 — 상세 결정 전부 여기).

## 3. 핵심 확정 결정 (변경 시 사용자 합의 필요)

- 알림: 접근(+1%) 예고 1회 → 하방 터치 본알림 1회, 글당 1회, 게시 7일 만료
- 클러스터: 같은 코인 진입가 ±1% 글들 병합, 상단 기준 1회 알림, 출처1·2 링크(URL 비노출)
- 필터: 수집은 전부 저장, 알림은 등급 C↑ + 코인당 **본알림** 3건/일 (예고는 상한 제외)
- **적중 DB** (ACCURACY_DB_PLAN 확정): TP1 도달=승 / SL=패 / 같은 1분봉 동시=패+ambiguous /
  TP 없으면 타임박스(수익률 부호) / 판정창 = 작성자 타임프레임(1H=7일, 4H=14일, 1D=30일,
  없으면 목표거리 10%당 7일, 상한 30일) / R-멀티플 [-1,+5] / 터치 이후 캔들만 시간순 스캔 /
  기준가=자기 진입가(지정가 체결 모델)+터치시점 환율 저장(FX 드리프트 보정) /
  클러스터 하단 미도달 레벨=섀도터치(통계 제외) / 24h·72h 수익률(도과 6h 허용오차)
- 표시: 자체 표본 5건↑부터 `🏹 승률67% (4승2패) 터치율67%` 자동 병기, 워쳐 적중률과 별도 줄
  (측정 기준이 달라 섞지 않음 — 자체는 "터치 시점부터", 워쳐는 "글 시점부터")
- 환산: USD 저장값 × 업비트 KRW-USDT 시세(테더 김프 포함). 코인별 김프 잔차는 알림의 김프 행

## 4. 현재 알림 양식 (최종 확정 — 임의 변경 금지)

```
━━━━━━━━━━━━━━━━━━━━
🎯 [진입가 터치] LINK          ← 예고는 ⚠️ [진입가 접근]
🥇 시총 19위 · B등급 · 2일 전   ← 글 나이는 실시간 재계산
✍️ 작성자: @ProChartist ⭐⭐    ← ⭐⭐=워쳐 화이트리스트
📊 평균 적중률: 72% (워쳐 25건)
🏹 승률73% (8승3패) 터치율73%   ← 자체 표본 5건↑일 때만
━━━━━━━━━━━━━━━━━━━━
타점
    현재:  12,078원             ← 원화 단독, 반올림 정수(1원 미만만 소수)
    진입:  12,006~12,035원      ← 범위면 ~ 표기
    목표:  13,848원  (+15.1%)   ← 손절 행은 비표시(사용자 결정, 데이터는 보존)
    거래:  38위                 ← 업비트 KRW 24h 거래대금 실시간 순위
52주(고가/저가/위치바) · 김프 · BTC.D/ALT.S/F&G · 출처1·2 하이퍼링크
```

## 5. 검증 이력 (신뢰 기준선)

- 테스트 3종: `scripts/test_extractor.py` 25/25, `scripts/test_price_logic.py` 30/30,
  `scripts/test_ranking.py` 15개 — **push 전 필수 실행** (2026-07-26 T18~20·랭킹 추가)
- 전수감사 3회(멀티에이전트): 1차 15건 + 2차 major3/minor2 전부 수정 완료, critical 0 수렴
- 실전 버그 이력(회귀 테스트 있음): TP 서수 오인("TP1:"의 1을 가격으로), 20xx 가격을 연도로
  삭제, 터치 이전 캔들이 판정 오염, collect 큐 기아, 커밋백 abort로 재시도 사망
- 보안: 비밀값 4중 방어(gitignore/자체스캐너/gitleaks CI/히스토리 전수검사 0건).
  **업비트 API 키는 이 봇에 없음**(시세 무인증 — 설계 원칙, 추가 금지)

## 6. 작업 컨벤션 (사용자와 합의된 방식 — 반드시 유지)

1. **기획/설계 변경은 질문카드**: AskUserQuestion 객관식 + 추천안(근거 포함), 하나씩
2. **양식 변경은 샘플 먼저**: 채팅에 렌더링 출력 → 사용자 확정 후 push (텔레그램 테스트
   발송은 upbit_bot/.env 로드해 가능, "🧪 테스트" 라벨 필수)
3. **push 절차**: 테스트 2종 통과 → `python scripts/check_secrets.py` → data/ 제외하고
   커밋(`git checkout origin/main -- data` 로 정리) → `git pull --rebase -X ours origin main`
   → push. **주의: 봇이 2분마다 data 커밋을 push하므로 경합 정상, `rm -rf data` 후
   `git add -A` 절대 금지**(봇 데이터 삭제 커밋 사고 이력 있음)
4. Windows: 파이썬 실행 시 `PYTHONIOENCODING=utf-8` 필수(cp949 이모지 크래시)
5. 토큰 값은 채팅에 넣지 않기(GitHub/cron-job 화면에만). 리서치는 웹 검증 후 결정
6. wait-and-see 원칙: 필터값(등급/상한/밴드)은 관찰 데이터 없이 변경 제안하지 말 것

## 7. 미결·고도화 후보 (이번 방에서 할 일 후보)

**사용자 액션 대기**: ① 업비트 구 API 키 폐기 확인(업비트 웹) ② upbit_bot 폴더 완전삭제 승인
**관찰 후 진행**: ③ 알림량 필터 조정 — 합의된 순서: 이미-터치 무알림 → C→B → 3→2건/일
**고도화 백로그** (ACCURACY_DB_PLAN 2단계, 리서치 완료 상태):
- ~~E_LB·베이지안 수축·최신성 가중~~ → **구현됨(2026-07-26)**: `analytics/ranking.py`
  (카드 확정: R NULL 은 2트랙, prior 강도 m_eff=min(10,워쳐표본), 노출은 주간 리포트만,
  알림은 게이트 교체(raw n≥5→n_eff≥5)만. 상세: izrua_company/dev_a/sprint01_ELB설계.md)
- 주간 텔레그램 리포트(터치 N건 → 승/패/진행, 작성자 순위)
- E_LB 지속 음수 작성자 역신호 태깅(Finfluencers: 56%가 anti-skilled)
- 동시터치 ambiguous 건 하위 타임프레임 재검사(Bar Magnifier 방식)
- 글 삭제 감지(deleted 플래그 — 삭제 건수 자체가 신뢰도 신호)
- TV_COOKIE Secret 미설정(선택 — 차단 시 대비용)
```
