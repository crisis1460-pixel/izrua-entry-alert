# 스프린트08 리서치 — SL 없는 시그널의 등급 설계 (업계 관행 조사)

- 작성: 2026-08-03, 기획자(리서치 에이전트)
- 발단: 프로덕션 DB 193건 중 54%(105건)가 SL 미기재. BigBeluga(9.7만 팔로워) 등
  유명 작성자가 SL을 안 적어 완결성 보너스 +10을 못 받고 D등급 고착.
  사장님 문제 제기: "유명 작성자일수록 리스크 때문에 SL 상세히 안 적는다.
  50% 넘는 글이 SL 없는데 이걸 배점 기준으로 랭크 판별은 잘못됐다."

## 전제 정정 (사내 데이터 재검증)

- S/A 0% 적중(0/12) 데이터는 **전부 v3 이전 구식 산식**의 유산 (R:R 가점 시절).
  v3(08-01~)는 이미 R:R 제거·TP 원거리 가점 축소·작성자 실적 가점(+15) 적용.
- 현행 v3에서 SL이 미치는 영향은 **완결성 보너스 +10점 하나뿐**.
  단, 이 +10 때문에 SL 없는 작성자는 S(85+) 구조적 불가능, A는 실적 만점일 때만.

## 조사 결과 요약 (6문항)

### Q1. 기성 플랫폼은 SL 필수인가?
실행(자동매매) 플랫폼은 형식상 요구하나 전부 폴백 내장. 분석 플랫폼은 요구 안 함.
- Cornix: Default Stop-Loss 기능 — 시그널에 SL 없으면 수신자 설정 기본 SL 자동 적용.
  SL 누락은 생태계 정상 케이스. (help.cornix.io)
- 3Commas: 시그널은 진입 트리거일 뿐, SL/TP는 봇 레벨 설정 담당.
- Zignaly: 포맷 평가 폐기, 실현 성과 기반 Z-Score(0~100)로 전환.
- TradingView 규정: 아이디어 발행 요건은 제목+근거뿐. SL/TP 필수 아님.
  **TV 아이디어는 실행 시그널이 아니라 분석 콘텐츠** — 실행 기준을 들이대는 건 매체 불일치.

### Q2. 완결성 채점에서 SL의 위상
소비자 가이드에서 SL 존재는 이진 신뢰 마커(red flag 체크)이지 큰 가중치 점수가 아님.
- red flag 논거 = "포지션 공식 종료 없으면 패배 미집계 → 승률 조작 가능".
  **우리는 timeboxed 판정으로 전 시그널 강제 판정 → 이 조작 경로 이미 차단.**
  SL 부재를 등급 감점으로 옮기면 이미 해결한 리스크를 이중 벌점하는 셈.
- MQL5 마켓플레이스: 랭킹은 실현 지표(성장률·드로다운·Reliability·MAE)만.
  시그널 포맷 점수 개념 자체가 없음.

### Q3. 합성/임퓨티드 SL 관행
확립된 관행이나 용도는 '실행'과 '공정 비교(정규화)'이지 저자 스킬 점수가 아님.
- ATR 기반 스탑(2~3×ATR)이 정석. 9,433건 백테스트에서 고정 pip 대비 우수.
- 임퓨티드 SL로 계산한 R:R은 평가자 규칙의 함수 — 저자 실력 예측력은 제한적.

### Q4. R:R은 예측 변수인가 — "R:R 인플레이션"
알려진 현상. stated R:R 단독은 예측 변수가 아님.
- CFA Institute(2026): 스탑을 좁힐수록 "시그널이 틀려서가 아니라 단기 변동이
  임계를 넘어서" 청산. 우리 'SL 0.4~0.9% → S등급 → 0/12' 관측과 동일 메커니즘.
- 크립토 스탑 헌팅/유동성 스윕 광범위 문서화. BTC 일중 ATR 통상 2%+ 대비
  0.4~0.9% 스탑은 노이즈 영역.
- FXCM 연구(R:R≥1 사용자 53% 수익 vs 미사용 17%)는 문턱 효과이지
  "높을수록 좋다"는 단조 증가 근거 아님.

### Q5. 저자 등급 vs 시그널 등급
조사한 모든 공급자 랭킹 시스템이 실현 결과 기반. 포맷 점수를 주요 가중치로
쓰는 시스템 발견 못 함.
- Darwinex: 12개 속성 전부 실현 트레이딩에서 계산. 리스크 엔진이 모든 전략을
  목표 VaR로 정규화 — 트레이더의 자체 SL/사이징을 평가에서 걷어내고
  의사결정 품질만 봄. "리스크는 정규화하고 결과로 판정"의 직접 선례.
- eToro Popular Investor: 게이트 전부 실현/트랙 기반. 포맷 요건 없음.
- Wilson 하한 정렬은 소표본 비율 랭킹의 정석(Evan Miller) — 현 시스템 선택 재확인.
- PSR/DSR(Bailey & López de Prado): 트랙 레코드 유의성 보정이 스킬/운 구분 표준.

### Q6. 팁스터 랭킹 (SL 개념이 없는 세계)
flat-stake 정규화 + 유의성 검정 + 시장 대비 지표(CLV)로 해결. 픽 형식은 채점 안 함.
- Pyckio: yield + level-stakes yield(균일 베팅 정규화) + t-test 유의성.
- CLV(Closing Line Value): 스킬 측정 업계 제1 지표.
  우리 timeboxed 판정은 구조적으로 CLV형 실현-엣지 지표에 가까움.

## 설계 옵션 (현행 v3 기준 매핑)

공통 전제: "SL 없음 = red flag"의 실제 근거(판정 회피)는 timeboxed로 이미 무력화.
SL 부재를 벌점화할 업계 근거가 우리 맥락에선 소멸. 오너 제약(SL 감점 금지)과 일치.

### 옵션 1 — SL 보너스 완전 제거 + 실적 상한 +25 (★기획 권장)
완결성 = entry+TP 20점으로 단순화. 실적 티어 (0.55,+25)/(0.40,+15)/(0.25,+8).
- 만점 87(SL 무관): 팔로워10+근접20+TP12+완결20+실적25. S(85+)는
  "1만+ 팔로워 & 최적 근접 & 최적 TP & 검증 실적"에서만 — 희귀성 유지.
- 근거: Q2(포맷 채점 없음)·Q5(실현 지배) 정합. SL은 판정 엔진 전용으로 강등.
- 예시: CryptoAnalystSignal(무SL, Wilson 하한 ~0.83) → 터치 시점 재채점에서 A 도달 가능.

### 옵션 2 — SL 보너스 +3 축소 + 실적 +22 (온건 재배분)
- SL 있는 작성자 최대 87, 없는 작성자 84. S는 여전히 SL 게이트(임계 85 유지 시).
- 근거는 옵션 1과 동일하나 완충 유지. S 게이트 문제 잔존.

### 옵션 3 — 현행 유지 + v3 실측 대기
- v3는 이틀째(27건, B1/C17/D9). v3 자체의 예측력 실측이 아직 없음.
- 근거: 우리 역상관 데이터는 구식 산식 소표본 — "문헌과 방향 일치" 수준.
  1~2주 더 관찰 후 v4 결정이 통계적으로 안전.

### 장기 후보 (v4 이후)
- CLV형 실현 엣지 지표: 판정 윈도우 내 방향 초과수익을 심볼 베이스라인 대비
  집계(flat-stake + 유의성). SL 유무 완전 무관, 게이밍 저항성 최고.
- 임퓨티드 SL(2~3×ATR) 정규화 R:R: ATR 데이터 수집 비용 필요. 표시용 참고 지표로 유효.

## 출처
Cornix Help(Signal Posting/Default Stop-Loss), 3Commas Help(Signal Bot FAQ),
Zignaly(cryptoadventure 리뷰), TradingView House Rules, Mudrex, CoinCodeCap,
Safetrading, MQL5 Forum(Reliability), QuantVPS/LuxAlgo/Quant Signals(ATR),
For Traders/Stats Edge(백테스트 정규화), CFA Institute Enterprising Investor(2026,
tight stop), TradingWithRayner/WeMasterTrade/TradersPost(R:R), FXCM(Traits of
Successful Traders), KuCoin/MEXC(스탑 헌팅), Darwinex Help(Investable Attributes),
eToro Help(Popular Investor), Evan Miller(Wilson), arXiv 1809.07694,
SSRN 2460551(PSR/DSR), Pyckio Help/Blog, Pikkit/VSiN(CLV), Tipstrr, Blogabet.
