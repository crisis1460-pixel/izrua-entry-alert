# 외부 리서치 2026-09-17 (izrua_entry_alert 개선 후보)

조사 범위: 2026년 4월 이후 신규 사례 위주. 제외 목록(데드맨스위치, SL 감점, 새 채널/토큰, 유료/불확실 소스, 자동매매, 유니버스 확대, 피드백 버튼, 숏)은 후보에서 배제.

## 상위 8개 후보 (요약)

1. **[긴급] GitHub Actions Node20 런타임 2026-09-23 제거 대응** — 워크플로 파손 방지, 난이도 하, 비용0
2. **공개 레포 Actions 무료/무제한 유지 확인** — 1월 가격개편에도 public repo 표준 러너는 그대로 무료, 문서화만
3. **메시지 리액션으로 알림 후속 상태 표시** (Bot API 10.0, 2026-05-08) — 새 메시지 대신 이모지 반응으로 상태 갱신
4. **고변동성 구간 알림 폭주에 소프트캡/다이제스트 확장 적용** — 이미 쓰는 "브리핑 흡수" 패턴을 실시간 터치 알림에도 확장
5. **빗썸 공개 API로 김프/유동성 교차검증** — 업비트 단일 소스 의존 완화
6. **작성자별 적중률에 유틸리티가중 캘리브레이션 검토** — 표본 부족 구간(터치 30여종) 안정화, 실무 사례 기반
7. **Rich Messages로 알림 포맷 구조화** (Bot API 10.1, 2026-06-11) — 표/구조화 텍스트, 단 라이브러리 지원 확인 필요해 우선순위 낮음
8. **cryptocurrency.cv 무료 뉴스 API 보완 소스 검토** [추정, 신뢰성 미검증] — 브리핑 뉴스 소스 다변화

---

## 상세 표

| # | 무엇 | 근거 (출처/날짜) | 난이도 | 비용0 | 기대효과 | 제외 저촉 |
|---|---|---|---|---|---|---|
| 1 | GitHub Actions가 2026-06-16부터 Node24를 기본 런타임으로 전환, 2026-09-23에 Node20 완전 제거 예정 (D-6). 워크플로가 참조하는 서드파티 액션 중 node20 고정 버전이 있으면 실행 실패 가능 | [GitHub Changelog: Deprecation of Node 20 on GitHub Actions runners, 2025-09-19 최초 공지](https://github.blog/changelog/2025-09-19-deprecation-of-node-20-on-github-actions-runners/), [community discussion 2026](https://github.com/orgs/community/discussions/189324) | 하 | 예 | 4분기 스케줄 파손 예방(긴급도 높음) | 없음 |
| 2 | 2026-01-01 GitHub Actions 가격 개편(호스티드 러너 15~39% 인하, 셀프호스티드 신규 과금 시도 후 48시간 만에 철회)에도 **공개 레포 표준 GitHub-hosted 러너는 여전히 무료·무제한** | [GitHub: Pricing changes for GitHub Actions (2026)](https://github.com/resources/insights/2026-pricing-changes-for-github-actions), [GitHub Changelog 2026-01-01 Reduced pricing](https://github.blog/changelog/2026-01-01-reduced-pricing-for-github-hosted-runners-usage/) | 하 | 예 | "비용0 원칙" 유지 근거 재확인, 별도 조치 불요 | 없음 |
| 3 | Bot API 10.0 (2026-05-08)에서 `deleteMessageReaction`/`deleteAllMessageReactions` 등 반응 관리 메서드 추가. 기존 봇 토큰으로 자기 메시지에 이모지 반응을 달거나 지울 수 있어, 상태 변화(예: 등급 변경, 근접 이탈)를 새 메시지 없이 반응으로 표시 가능 | [Telegram Bot API changelog, Bot API 10.0 (2026-05-08)](https://core.telegram.org/bots/api-changelog) | 중 | 예 | 채팅 소음/알림 수 증가 없이 상태 갱신 전달 | 없음(기존 토큰, 추가 인증 불요) |
| 4 | 실무 사례: 크립토 알림봇에서 "15개 심볼 × 2개 이벤트 카테고리 = 배치당 30건" 식 알림 폭주가 뮤트/봇 해제로 이어지는 문제를 다루기 위해 전역 알림량 캡 + 적응형 스로틀링 도입 사례. 이미 TP적중/뉴스를 브리핑으로 흡수한 패턴을 변동성 급등 구간의 실시간 터치 알림에도 소프트캡 형태로 확장 검토 | [francovp/cabros-bot Issue #690 (2026), alert fatigue detection and adaptive throttling](https://github.com/francovp/cabros-bot/issues/690) | 중 | 예 | 급등락 구간 알림 과다로 인한 피로/무시 방지, 이미 검증된 자체 패턴 재사용 | SL 아님, 손절 관련 아님 — 확인 |
| 5 | 빗썸 공개 API는 인증 없이 KRW 마켓 시세 접근 가능(REST/WebSocket). 업비트 단일 소스로 계산 중인 김프/유동성 판단을 교차검증할 무료 추가 소스로 활용 가능 | [api-evangelist Bithumb API profile, 2026](https://github.com/api-evangelist/bithumb) — 2차 출처, 공식 문서(apidocs.bithumb.com) 별도 확인 필요 [추정] | 하 | 예(무료 공개 엔드포인트 확인됨, 세부 rate limit은 재확인 필요) | 업비트-빗썸 가격 괴리 감지, 김프 계산 정밀화 | 없음 |
| 6 | 표본이 적은 신규 심볼/작성자 구간에서 노이즈를 무력화하는 유틸리티가중 캘리브레이션 기법. 유한 표본에서도 신호 없는 노이즈를 식별·중화하는 실증 결과(2025-12 표본 8,025건 기반 논문, 2026년 공개) — 등급 산식 v5 이후 소표본 구간(작성자별 적중률, 신규 심볼) 보정에 적용 검토 | [arXiv:2601.07852, Utility-Weighted Forecasting and Calibration under Trading Frictions](https://arxiv.org/pdf/2601.07852) | 상 | 예 | 표본 부족 구간(터치 30여종 축적 중)의 등급 신뢰도 개선 | SL 아님 |
| 7 | Bot API 10.1 (2026-06-11)에서 `sendRichMessage` 등 구조화 텍스트/표 메시지 지원 추가. 알림을 표 형식으로 구조화 가능하나, python-telegram-bot 등 주요 라이브러리의 Rich Messages 지원 여부는 미확인 | [Telegram Bot API changelog, Bot API 10.1 (2026-06-11)](https://core.telegram.org/bots/api-changelog) | 상 (라이브러리 지원 미확인) | 예 | 알림 가독성 개선(효과 제한적) | 없음 |
| 8 | cryptocurrency.cv: API 키/등록 없이 접근 가능한 크립토 뉴스 집계 API(200+ 소스, RSS/JSON). 브리핑의 뉴스 요약 소스를 다변화할 후보 | [GitHub nirholas/cryptocurrency.cv](https://github.com/nirholas/cryptocurrency.cv) — 2차 출처, 무료 한도·안정성·한국어 지원 여부 미검증 [추정] | 하 | 미확인(자체 표기상 무료·무키이나 검증 필요) | 브리핑 뉴스 소스 다변화 | 없음, 단 소스 안정성 검증 전 도입 보류 권장 |

## 조사했으나 채택 보류/제외한 항목

- **Bot API 10.2/10.3의 Ephemeral Messages, Guest Mode, Community 기능** — 그룹/커뮤니티·게스트 상호작용용으로 1인 알림봇 구조에 해당 없음.
- **Telegram Checklist 기능** — 도입 시점이 2026년 이전(Bot API 9.x 이전 추정)이라 "최근 3~6개월 신규" 기준 미충족, 검증 안 돼 제외.
- **한국 규제(VASP 미신고 사업자 입출금 제한, 2026-06-23 개정, 가상자산 양도소득세 2026년 확대)** — 현물 매수·보유·매도만 하는 스윙 알림봇 로직(입출금 없음)에는 직접 영향 없어 후보에서 제외. 다만 업비트가 특정 코인을 상장폐지/거래정지할 경우의 유니버스 필터링 관련 백서 리스크는 별도 모니터링 가치 있음(신규 기능 제안은 아님).
- **altFINS, CoinGlass 등** — 제외 목록에 이미 명시된 소스라 조사 대상에서 배제.
