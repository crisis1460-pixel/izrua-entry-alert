"""
TradingView 아이디어 본문에서 트레이드 셋업(방향/엔트리/손절/목표)을 추출.

설계 근거(2026-07 리서치):
- entry 는 단일값이 아니라 존/범위(예: 0.45–0.48)로 오는 경우가 흔하다 → 범위 대응.
- 함정: 날짜(2026), 레버리지(10x), 퍼센트(+20%), 다중 TP 를 가격 숫자로 오인.
  → 추출한 숫자는 현재가 대비 sanity check(기본 ±60%)로 거른다.
- 라벨 표기가 소스마다 달라 한 규칙으로 전부는 못 잡는다 → 한/영 키워드를 넓게 커버하고,
  못 뽑은 항목은 None(판단보류)으로 남긴다(억지 추론 금지).

이 모듈은 순수 함수라 네트워크 없이 단위 테스트된다.
"""

import re
from typing import Optional

from config import settings

# 방향 키워드
# 2026-08-01 검토 수정: \bbuy\b/\bsell\b 가 ICT/SMC 계열 트레이딩 용어 "buy-side
# liquidity"/"sell-side liquidity"(매수측/매도측 유동성 — 방향과 무관한 시장구조
# 서술어) 내부의 buy/sell 까지 잡아 방향을 반대로 오분류했다. "buy-side liquidity"
# 만 언급하고 "long"/"buy zone" 등 실제 방향 단어를 안 쓴 글이 direction="short"
# 로 저장되면, 이 봇은 long 전용(get_active_levels)이라 그 신호가 조용히
# 감시 대상에서 빠진다. buy/sell 뒤에 "(-)side" 가 바로 붙는 경우만 제외 —
# "buy at 61500"/"buyers" 등 정상 용법은 그대로 매칭(단어경계는 기존 수리 유지).
_LONG_HINTS = re.compile(
    r"\b(long|롱|매수|매집)\b|\bbuy\b(?!-?\s*side)|롱\s*포지션|buy\s*zone|long\s*setup", re.I)
_SHORT_HINTS = re.compile(
    r"\b(short|숏|매도)\b|\bsell\b(?!-?\s*side)|숏\s*포지션|short\s*setup", re.I)

# 비방향 관용구 (2026-09-27 감사 C-D / A-T3 — 숏 글이 롱으로 저장된 1순위 원인).
# \blong\b 가 "as long as"·"long-term"·"long upper wick" 에, \bshort\b 가
# "short-term"·"short squeeze" 에 걸려 두 힌트가 동시에 켜지면(텍스트 301건 중 44건)
# 기본값 long 이 됐다. 실측: 636 ETH("short setup … as long as the rectangle …"),
# 699 ETH("Entry (Short)" + "long upper wick"). 방향 판정 **전에만** 지운다 — 가격
# 추출용 텍스트(clean)에는 손대지 않는다.
_DIRECTION_NOISE = re.compile(
    r"\bas\s+long\s+as\b|\bso\s+long\s+as\b|\blong[\s-]*(?:term|standing|awaited|lasting|lived)\b"
    r"|\blonger\b|\bno\s+longer\b|\bbefore\s+long\b|\b(?:in|over)\s+the\s+long\s+(?:run|haul)\b"
    r"|\blong\s+(?:upper\s+|lower\s+)?(?:wick|shadow|tail|time|way|consolidation|period)s?\b"
    r"|\bshort[\s-]*(?:term|lived)\b|\bshortly\b|\b(?:in|over)\s+the\s+short\s+run\b"
    r"|\bshort[\s-]*squeez\w*|\blong[\s-]*squeez\w*|\blong\s*/\s*short\b|\bshort\s*/\s*long\b"
    r"|\bin\s+short\b|\bfalls?\s+short\b|\bshort\s+of\b"
    r"|\b(?:buy|sell)(?:ing)?[\s-]*(?:volume|pressure|orders?|walls?|flow|signal)s?\b"
    r"|\bsell[\s-]*off\b",
    re.I,
)

# 라벨 (그룹1 = 라벨종류). 라벨 뒤에 오는 숫자를 그 항목으로 본다.
_ENTRY_LABEL = re.compile(
    # 2026-07-28 수리: entry/enter/buy 에 단어경계 추가 — center/reenter/buyers 내부
    # "enter"/"buy" 가 엔트리 라벨로 잡히는 버그(DB 현재 0건, 소스 확장 시 노출면 증가).
    # (?:\s*\([^)]*\))? — "Buy (zone):" 같은 괄호 주석을 거치고도 is_spec 판정을 통과.
    # 2026-09-27 감사 C-L: "Re-Entry"(884 — 재진입 '기회' 서술 뒤 트리거가 $1.42 를
    # 진입가로 흡수)와 "buy-side liquidity"(628·694·695 — 유동성 목표가를 진입가로
    # 흡수)를 라벨에서 뺀다. _LONG_HINTS 의 buy 예외와 같은 규칙.
    # "Buy Limit Zone:" 도 스펙형으로 인정(656 — limit 이 허용 접미어가 아니라
    # 산문형으로 떨어졌다).
    r"((?<!re-)(?<!re\s)\bentry\b|\benter\b|\bbuy\b(?!-?\s*side)|long\s*entry|진입가?|진입|매수가?"
    r"|롱\s*진입|buy\s*zone|entry\s*zone)"
    r"(?:\s*\((?![ \t]*\$?[0-9][0-9,]*(?:\.[0-9]+)?[ \t]*\))[^)]*\))?\s*(?:limit\s*)?(?:price|zone|level|구간|가격)?\s*[:=]?\s*",
    re.I,
)
_SL_LABEL = re.compile(
    # 2026-07-28 수리: \bsl\b 단어경계 — sloping/previously/slight 내 "sl" 선점 버그
    # (실측 2건: BTC id=120 S→D 등급 폭락 → min_grade 필터에 막혀 알림 미발송).
    # (?:\s*\([^)]*\))? — "Stop Loss (SL): $1,820" 괄호 주석이 라벨·콜론 사이에 있어도
    # m.group(0) 에 콜론이 포함되어 is_spec=True 로 올바르게 분류된다.
    # 2026-09-27 사용자 제보(ARB 891): "Invalidation: $0.202" 는 작성자가 명시한 손절선인데
    # SL 라벨로 인식되지 않아 sl=None → judgment_mode tp_only·등급 R:R 미반영. 라벨형
    # (뒤에 콜론/=)일 때만 인정 — 산문 "invalidation of the setup" 은 제외.
    r"(stop\s*loss|stop|\bsl\b|손절가?|손절|스탑|스톱"
    r"|invalidation(?:\s*(?:level|point|price|zone))?(?=\s*(?:\([^)]*\))?\s*[:=]))"
    r"(?:\s*\((?![ \t]*\$?[0-9][0-9,]*(?:\.[0-9]+)?[ \t]*\))[^)]*\))?\s*[:=]?\s*", re.I,
)
# 복수형 "TARGETS:" 도 받는다 (2026-07-27). 예전엔 `target(?!s)` 로 복수형을 배제했는데,
# "Take-Profit Targets:" 헤더가 매칭돼 그 뒤 "TP1: 5.298" 대신 라벨 번호를 가리키던
# 2026-07-23 사고 방어였다. 그 근본 원인은 이후 _ORDINAL_LABEL(서수 제거)이 없앴고,
# 배제를 유지하면 "TARGETS: 7.15 - 7.45 - …" 형태를 쓰는 소스에서 목표가가 통째로
# None 이 된다. INJ 재현 케이스가 test_extractor.py 에 남아 회귀를 막는다.
_TP_LABEL = re.compile(
    # 2026-07-28 수리: \btp\d?\b 단어경계 — http/https URL 내 "tp" 가 목표가 라벨로
    # 잡히는 버그(URL 숫자가 tp 자리 선점 → 정상 목표가 유실, DB 현재 0건).
    # (?:\s*\([^)]*\))? — entry/sl 과 일관성 유지.
    # 2026-07-29 수리: "Profit level 0.3981" 형식에서 `targets?`가 서술부 동사
    # "the thesis targets 0.75" 를 라벨로 오인해 0.75 를 TP 로 잡는 버그.
    # \bprofit\s*level\b 추가 → "Profit level" 이 텍스트에 먼저 나오면 우선 채택.
    # (Take-Profit 케이스: "Profit" 뒤가 "Targets/zone/…" 이지 "level" 이 아니라 불일치 ✓)
    r"(take\s*profit|\bprofit\s*level\b|targets?|\btp\d?\b|목표가?|목표|타겟\s*\d?|익절가?)"
    r"(?:\s*\((?![ \t]*\$?[0-9][0-9,]*(?:\.[0-9]+)?[ \t]*\))[^)]*\))?\s*[:=]?\s*", re.I,
)

# 실전 버그(2026-07-23): "TP1: 5.298 / TP2: 5.420 / TP3: 5.560" 처럼 다중 목표가를
# 번호 매긴 글에서, "Take-Profit Targets:"(복수형 헤더)가 먼저 매칭되고 그 검색창(30자)
# 안에 있는 "TP1"의 "1"이라는 숫자를 실제 목표가로 오인하는 사고가 실전 알림에서
# 발생했다(INJ 글: 진짜 목표가 5.298 대신 라벨 번호 1이 tp로 잡혀 RR이 마이너스로
# 계산됨). 라벨의 서수(TP1/TP2/SL1 등)를 숫자 탐색 전에 미리 제거해 이 숫자가
# "가격"으로 오인되지 않게 한다 — "TP1:" → "TP:" (라벨 자체는 유지, 서수만 제거).
# 라벨과 숫자 사이에 공백을 절대 허용하지 않는다(\s* 아님) — "목표 68,000"처럼
# 공백을 둔 정상 가격의 앞자리(68)까지 서수로 오인해 지워버리는 회귀가 실제로
# 났었다(2026-07-23 자체 발견). "TP1"/"목표1"처럼 라벨에 숫자가 바로 붙어있을 때만
# 서수로 간주한다.
_ORDINAL_LABEL = re.compile(r"\b(TP|SL|Target|Entry|타겟|목표)[0-9]{1,2}\b", re.I)
# "T1: 84,800 / T2: 86,500"(793 BTC, 849) — 'T' 약칭 목표 라벨은 라벨로 인식되지 않아
# 서수 1 이 창의 첫 숫자로 잡혔다. 콜론이 뒤따를 때만 "TP:" 로 정규화한다.
_T_ORDINAL = re.compile(r"\bT[1-9]\b(?=\s*(?:\(|[:=]))")  # "T1 (Mirror …): 86,024"(851)

# 2차 실전 버그(2026-07-23 저녁, ALGO/ARB 알림): "Target 1: 0.08977" 처럼 서수가
# 공백으로 떨어져 있는 표기는 위 규칙이 못 잡아서, "Target"까지만 라벨로 매칭된 뒤
# 창 안의 "1"이 목표가로 오인됐다(두 코인 모두 tp=1.0 → ₩1,458 표기 사고).
# "목표 68,000" 오탐을 피하면서 이 표기만 잡는 결정적 차이는 숫자 뒤의 콜론:
# 서수는 "Target 1:" 처럼 반드시 :/= 가 따라온다. 그래서 공백 서수는 콜론이
# 뒤따를 때만 제거한다.
# 2026-07-27 프로덕션 실사고 확장: "Take Profit 1: $0.385" 가 안 잡혀 ONDO 목표가에
# 서수 1 이 들어갔다(tp=1.0). 위 목록이 라벨 '단어'만 갖고 있어서 "Take **Profit**" 처럼
# 두 단어짜리 라벨의 마지막 단어를 몰랐던 것 — 같은 사고(ALGO/ARB)의 미완 수리였다.
# 한/영 익절 표기(Take Profit / Profit / 익절)를 추가한다. 콜론 요구 조건은 그대로라
# "목표 68,000" 류 오탐 위험은 늘지 않는다.
_SPACED_ORDINAL_LABEL = re.compile(
    r"\b((?:take\s*profit|profit|익절|TP|SL|Target|Entry|타겟|목표|진입|손절)"
    # 2026-09-27 재파싱 회귀(822 NEAR "Entry Zone 1: 4.103"): 라벨과 서수 사이의
    # zone/level/price/area 한 단어도 허용 — 지우는 건 서수뿐이다.
    r"(?:\s+(?:zone|level|price|area))?)"
    # 2026-09-27 감사 A-T4/C-T: "Take Profit 1 (TP1): 0.7460"(643 SUI)·"Target 1 (TP1):
    # $0.5843"(878 ONDO) 처럼 서수 뒤에 괄호 주석이 오면 콜론 요구 조건에 안 걸려
    # 서수 1 이 tp=1.0 으로 잡혔다. 괄호도 서수의 뒤따름으로 인정한다 —
    # "Target 2 (major) 0.5" 류에서도 지워지는 건 서수뿐이라 가격은 보존된다.
    # 재파싱 회귀 보강: "Target 1 at $2,507.06"(749), "Target Two (R2): $2,714.10"(799) —
    # 'at'/'@' 뒤따름과 영어 서수 단어도 서수로 본다.
    # 2026-09-27 리뷰 RV2-E1: 괄호·at 뒤따름을 무조건 서수로 보면 1~2자리 **가격**이 지워졌다
    # ("Entry 45 (support zone)" → 진입 없음, "Target 15 at resistance" → TP 없음 — SOL·
    # AVAX·LINK 가격대). 괄호는 ①안이 서수 표기(TP1·T2·R2)이거나 ②닫은 뒤 콜론·등호·
    # 가격 숫자가 올 때만, at/@ 은 **뒤에 가격 숫자가 올 때만** 서수로 본다.
    r"\s+(?:[0-9]{1,2}|one|two|three|four|five|six)"
    r"(?=[ \t]*(?:[:=]"
    r"|\((?:TP|TGT|T|R|Target)[ \t]?[0-9]{1,2}\)"
    # 09-27 디버깅: "Target 1 (2.40)" — 괄호 안이 가격뿐이면 뒤따름과 무관하게 서수.
    r"|\([ \t]*\$?[0-9][0-9,]*(?:\.[0-9]+)?[ \t]*\)"
    r"|\([^)\n]{0,40}\)[ \t]*(?:[:=]|[:=]?[ \t]*\$?[0-9])"
    r"|(?:@|at\s)[ \t]*\$?[0-9]))", re.I
)

# 가격 숫자 하나: 1,234.56 / 0.00123 / 12100 / $8.30 (콤마·$ 허용)
_NUM = r"\$?\s*([0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]+)?|[0-9]*\.[0-9]+|[0-9]+)"
# 범위: 0.45 - 0.48 / 12,000~12,500
_RANGE = re.compile(_NUM + r"\s*[-–~〜]\s*" + _NUM)
_SINGLE = re.compile(_NUM)

# 사다리형 나열: "7.15 - 7.45 - 7.85 - 8.25" 처럼 같은 구분자로 3개 이상 이어지는 목록
# (2026-07-27 신규 소스 실사 중 발견). 같은 하이픈이 ENTRY 줄에서는 '범위'인데 TARGETS
# 줄에서는 '나열'이라, _RANGE 로만 보면 두 번째 값을 범위 상단으로 오인한다.
# 실측 왜곡: ETC 첫 목표 +4.7% → +9.1%, AERO +5.1% → +10.0% (약 2배).
# 적중 판정("TP1 도달=승")과 TP 거리 배점이 모두 첫 목표를 전제하므로 나열이면 맨 앞
# 값을 취해야 한다. 숫자 사이에 구분자만 허용한다 — 사이에 글자가 끼면
# ("6.81 - 6.85\nSTOP LOSS: 6.25") 매칭되지 않아 진짜 범위를 나열로 오인하지 않는다.
_LADDER = re.compile(_NUM + r"(?:[ \t]*[-–~〜/][ \t]*" + _NUM + r"){2,}")

# 화살표 구분자 사다리: "TP: $13.62 → $14.05" (2026-07-27 실전 발견, VVV 글).
# 하이픈과 달리 **2개짜리도 사다리로 본다.** 근거는 기호의 뜻이다 —
#   "110 - 120" 은 '110에서 120 사이'(범위)로도 읽히지만,
#   "13.62 → 14.05" 는 '13.62 다음 14.05'(순서)일 수밖에 없다.
# 그래서 하이픈은 3개 이상일 때만 나열로 보고(2개는 범위 유지), 화살표는 2개부터
# 나열로 센다. 값은 어느 쪽이든 맨 앞(TP1)만 쓴다.
_LADDER_ARROW = re.compile(
    _NUM + r"(?:[ \t]*(?:→|->|➡|=>|~>)[ \t]*" + _NUM + r"){1,}")

# _LADDER 검색 창의 절대 상한 (2026-07-27 2차 교차검토 — 가용성 수리).
# 왜 필요한가: _LADDER 는 _NUM(내부 교대)을 `{2,}` 로 감싼 **중첩 수량자**라, 긴
# 숫자 런에서 역추적이 2차식으로 터진다. `_LADDER.search` 실측(같은 머신):
#     120자 1.4ms · 200자 4.1ms · 400자 13.5ms · 3,200자 **1,186ms**
#     16,000자 → 수십 초(측정 중단)
# _RANGE·_SINGLE 은 30자 창이라 안전하고 무경계인 건 _LADDER 하나뿐이다 — B-M1
# 수리로 창이 30자 → '첫 숫자가 놓인 줄의 끝'이 되면서 새로 생긴 노출면이다(수리
# 자체는 옳았다). 수집기는 제3자가 쓴 임의 텍스트를 파싱하고 한 회차는 12분 하드킬
# 사정권이라, 개행 없는 긴 숫자 줄 하나로 그 회차를 통째로 태울 수 있다.
# 왜 200인가: 실전 최장 사다리 추정 = 값 12자(`1,234,567.89`) × 8 rung + 구분자
# ≈ 120자. 여유 1.7배를 얹어 200자 → 최악 소요가 4.1ms 로 고정되고, B-M1 이 고친
# 케이스(필러 15~19자 + 18자 사다리 ≈ 37자)는 여유롭게 들어온다.
# 잘려도 결과값은 안 변한다: 사다리는 어차피 **맨 앞 값 하나만** 반환하고,
# 200자 안에 rung 3개가 들어오면 '나열인가' 판별은 이미 끝나 있다.
_LADDER_MAX_WINDOW = 200

# (2026-08-15 v5 후속) 종전의 단계 수 상한 _LADDER_MAX_STEPS=12(표시 신뢰 컷)는
# 저장 게이트에서 제거하고 notify/telegram.py 의 "1/N" 표시 상한으로 이동했다 —
# tp_ladder_count 가 v5 등급 산식에 들어가면서, 13단+ 진짜 사다리를 0(미상)으로
# 뭉개면 사다리 감점(-3)을 잘못 물리기 때문. 저장은 참 개수, 표시만 12 컷.
_SANITY_LO_MULT = settings.get("sanity_lo_mult")
_SANITY_HI_MULT = settings.get("sanity_hi_mult")
# 진입가 sanity 하단 허용폭 (2026-09-14, 감사 F3) — _sanity 독스트링 참고.
# 0.90 = 현재가의 10% 미만이면 기각(자릿수 오파싱). 상단은 parse_setup(max_dev).
_SANITY_BELOW_MAX = settings.get("sanity_below_max_dev")

# 오인 유발 토큰 제거용: 레버리지 10x, 퍼센트, 날짜(문맥 한정)
_LEVERAGE = re.compile(r"\b\d{1,3}\s*x\b", re.I)
_PERCENT = re.compile(r"[+\-]?\s*\d+(?:\.\d+)?\s*%")
# 2026-07-24 감사 수정: 예전 `\b20[2-9]\d\b`(연도 무조건 삭제)는 "Entry: 2050" 같은
# $2,0xx 가격대 코인(ETH 등)의 진짜 가격을 지워 다음 숫자를 엔트리로 오인시키는
# 치명 버그였다. 날짜 '문맥'일 때만 제거한다: 완전한 날짜형(2026-07-24), '2026년',
# 전치사 동반("in 2026"). "Target: 2050-2100" 같은 가격 범위는 두 번째 구분자가
# 없어 보존된다.
_DATE_FULL = re.compile(r"\b20[2-9]\d[-./]\d{1,2}[-./]\d{1,2}\b")
_YEAR_KR = re.compile(r"\b20[2-9]\d\s*년")
_YEAR_CTX = re.compile(r"\b(?:in|by|since|until|late|early|year)\s+20[2-9]\d\b", re.I)

# R-멀티플 표기 ("4R return", "1.5R", "+2R") — 2026-07-27 프로덕션 실사고.
# AVAX 글의 산문 "the initial entry achieving a solid 4R return" 에서 라벨 'entry' 가
# 잡히고 바로 뒤 '4R' 의 4 가 진입가로 들어가, 실제가 $6.48 인 코인에 **$4.0 짜리
# 가짜 레벨**이 감시 상태로 저장됐다(원문은 숏 분석인데 롱으로도 저장). 레버리지(10x)
# 를 지우는 것과 완전히 같은 이유다 — 이건 가격이 아니라 '배수'다.
# 소수 R(1.5R)도 흔하고, 부호가 붙는 경우(+2R/-1R)도 함께 지운다.
_R_MULTIPLE = re.compile(r"[+\-]?\s*\d+(?:\.\d+)?\s*R\b", re.I)

# ── 2026-09-27 감사 A-T4 / C-L 추가 패턴 (비가격 숫자 4종) ──────────────────
# ① 손익비 비율: "RR 1:1.5"·"R/R:2"(772 FIL tp=2.0)·"R:R 3"·"Risk/Reward Ratio (R:R): ~2.3:1".
#    _R_MULTIPLE 은 "2R" 꼴만 지웠다. 라벨 동반형을 먼저 지우고, 라벨 없이 남는
#    "1:2 risk-to-reward"·"to 3:1" 처럼 한쪽이 1 인 비율도 지운다(가격이 아님).
_RR_RATIO = re.compile(
    r"(?:\bR\s*[/:]\s*R\b|\bRRR?\b|\brisk\s*[/:-]?\s*(?:to\s*[-\s]?)?reward(?:\s*ratio)?)"
    # 2026-09-27 리뷰 RV2-E5: 종전 `\s*[:=]?\s*~?\s*` 는 공백 런에서 3차 백트래킹
    # ("RR"+공백 1600자 → 8초). 구분 문자 한 클래스로 합친다(허용 문자열은 같다).
    r"(?:\s*\([^)]*\))?[\s:=~]*\d+(?:\.\d+)?(?:\s*[:/]\s*\d+(?:\.\d+)?)?",
    re.I,
)
_ONE_RATIO = re.compile(
    r"(?<![\d.,:])(?:1\s*:\s*\d{1,2}(?:\.\d+)?|\d{1,2}(?:\.\d+)?\s*:\s*1)(?![\d.,:])")
# ② 레버리지 템플릿: "👉Leverage x 5-10-20"(Roddy01 — _LADDER 가 5..20 을 사다리로
#    읽어 진입 12.5), "Lev 10x", "x10", "Margin 1-5%". 기존 _LEVERAGE 는 "10x" 만.
_LEVERAGE_LABEL = re.compile(
    r"\b(?:leverage|lev)\b\s*[:=]?\s*(?:(?:cross|isolated|max|up\s+to)\s*)?(?:x\s*)?\d+(?:\.\d+)?(?:\s*[-–/,~]\s*x?\s*\d+(?:\.\d+)?)*\s*x?\b"
    r"|\bmargin\b\s*[:=]?\s*\d+(?:\.\d+)?(?:\s*[-–~]\s*\d+(?:\.\d+)?)?\s*%?"
    r"|(?<![\w.])x\s?\d{1,3}\b",
    re.I,
)
# ③ 타임프레임 토큰: "H1"·"M15"(대문자 문자+숫자), "4H"·"15m"·"1D"·"1W"(숫자+단위),
#    "4-hour"·"15 minute"(831). 656 PROVE "(M15 Entry / H1" → 1.0, 879 "4H" → 4.0.
_TF_TOKEN = re.compile(r"(?<![\w.,$])[HMDW]\d{1,3}\b")
_TF_NUM_UNIT = re.compile(
    # "650 w/ trailing stop" 의 w/ 는 with 약어다 — 한 글자 단위 w 뒤 '/' 는 기간 아님(RV2-E6).
    r"(?<![\w.,$])\d{1,3}\s?(?:h|hr|hrs|m|min|mins|d|w(?!/))\b"
    # `\s*-?\s*` 는 공백 런에서 2차 백트래킹(RV2-E5 계열, 숫자+공백 4000자 0.58s) —
    # `(?:\s*-)?\s*` 로 같은 문자열을 선형으로 받는다.
    r"|(?<![\w.,$])\d{1,3}(?:\s*-)?\s*(?:hours?|minutes?|mins?|days?|weeks?|months?|years?|candles?|bars?)\b",
    re.I,
)
# ④ 기간: "Over The Next 30 Days"(575 ZORA → 진입 30.0), "2 weeks" — ③의 둘째 줄이 담당.

# "N to N" 범위 (감사 C-Z 646 ARB "Entry zone: $0.16 to $0.10" → 0.16). _RANGE 는
# 하이픈·물결만 구분자로 봐서 "to" 를 놓쳤다. 진입 라벨이 있는 **같은 줄**에서만
# 범위로 인정한다(산문 "from 0.5 to 0.8" 류가 TP 등 다른 라벨로 새지 않게).
_RANGE_TO = re.compile(_NUM + r"\s+to\s+" + _NUM, re.I)

# 산문형 라벨 근접 제약 (감사 C-L). 스펙형 라벨(콜론)이 없는 글에서 라벨 뒤
# 80자 창의 첫 숫자를 쓰다 보니 목표가·손절가·유동성·일수가 진입가로 흡수됐다
# (575·623·628·669·768). 산문형은 라벨과 숫자 사이 25자 이하, 그리고 문장 끝이나
# 다른 라벨 키워드에서 창을 끊는다. 스펙형 경로는 종전 그대로(80자 창)다.
_PROSE_MAX_GAP = 60
_SENT_END = re.compile(r"[.!?](?=\s|$)")
_ENTRY_STOP = re.compile(
    r"\btargets?\b|\btp\b|take[\s-]*profit|\bstop|\bsl\b|invalidat|liquidity|resistance"
    r"|current(?:ly)?\s+(?:price|trading)|trading\s+(?:around|at)|leverage|margin", re.I)
# TP/SL 창은 진입 단어에서 끊지 않는다 — "the SL is 2% below entry, which puts it
# around $12.02"(684)처럼 진입가를 기준점으로 말하는 산문이 흔하다(재파싱 회귀).
_TP_STOP = re.compile(r"\bstop|\bsl\b|invalidat", re.I)
_SL_STOP = re.compile(r"\btargets?\b|\btp\b|take[\s-]*profit", re.I)

# 돌파 트리거 (감사 C-B). "Entry: 1.4968 Buy Stop"(807), "Entry: 2,526.97 (Confirmed
# 1H close breaking above …)"(637), "weekly close above $1.42"(884) — 작성자는 '그
# 위로 올라서면 진입'인데 봇은 '위에서 내려와 닿으면 매수'로 감시한다. 진입값
# 주변(같은 문장 또는 ±40자)에 이 키워드가 있으면 진입가로 쓰지 않는다.
# 리테스트 진입("Breakout retest"·"pullback to")은 눌림목이라 예외(804·819·850·855).
_TRIGGER = re.compile(
    r"buy[\s-]*stop|\bclose[sd]?\s+(?:\w+\s+){0,2}?above|\bclosing\s+above"
    r"|\bbreak(?:s|ing)?\s+(?:out\s+)?above|\bbreakout\s+above|bullish\s+flip|\breclaim(?:s|ed|ing)?\b"
    # "Break and confirm above $106.11 (entry trigger …)"(673), "A confirmed break above"(754)
    r"|\bbreak(?:s|ing)?\s+and\s+(?:confirm|close|hold)\w*\s+above|\bconfirmed\s+break\s+above",
    re.I)
_BUY_STOP = re.compile(r"buy[\s-]*stop", re.I)
_RETEST = re.compile(r"re-?test|pull\s*back\s+(?:in)?to|pullback\s+(?:in)?to", re.I)
_TRIGGER_CTX = 40
_BUY_STOP_CTX = 100

# 번호 목록 마커 ("1) 1.1129", "2. 0.5340") — 2026-07-27 채널 실사 중 발견.
# 라벨 다음 줄부터 후보를 번호로 나열하는 포맷에서 **마커 숫자가 가격으로** 읽혔다
# (Entry 뒤 '1)' → entry=1.0). 과거 'Target 1:' → tp=1.0 사고와 같은 계열이고,
# _ORDINAL_LABEL 계열은 라벨에 붙은 서수만 처리해 독립 마커는 못 잡았다.
# 결정적 구분자는 **구두점 뒤 공백**이다: 목록 마커는 "1) " / "1. " 처럼 반드시
# 공백이 따르지만, 소수 가격 "5.298" 은 점 뒤가 곧바로 숫자다. 이 조건 덕에
# 진짜 가격을 지울 위험이 없다.
# 2026-08-01 검토 수정: 예전엔 진짜 줄 시작(^)에만 적용해 "TARGETS: 1) 0.05 -
# 2) 0.06 - 3) 0.07" 처럼 라벨과 같은 줄에 붙은 마커를 못 지웠다 — _LADDER 가
# ")"에 막혀 매칭 실패하고 _RANGE/_SINGLE 로 떨어지면서 마커 숫자 "1"이 그대로
# TP 로 잡혔다(과거 "Target 1:" 서수오인과 같은 계열, ALGO/ARB 실사고 재발 형태).
# 줄 시작 외에 "라벨 뒤 공백/콜론/등호 직후"도 마커로 인정한다 — 문장 중간의
# "(1) 참고" 류는 앞 문자가 저 셋에 없어(보통 알파벳·괄호) 여전히 회피된다.
_LIST_MARKER = re.compile(r"(?:^|(?<=[\s:=]))[ \t]*\d{1,2}[).][ \t]+(?=[$\d])", re.M)

# 타임프레임 파싱 (2026-07-23 적중창 결정 B: 작성자가 밝힌 지평으로 판정 창 결정)
_TIMEFRAME = re.compile(
    r"(?:timeframe|time\s*frame|타임프레임|봉)\s*[:\-]?\s*([0-9]{1,3})\s*(m|min|h|hr|d|w)\b",
    re.I,
)
_TIMEFRAME_WORD = re.compile(r"\b(daily|weekly|일봉|주봉)\b", re.I)
_TF_UNIT_HOURS = {"m": 1 / 60, "min": 1 / 60, "h": 1.0, "hr": 1.0, "d": 24.0, "w": 168.0}


def parse_timeframe_hours(text: str):
    """글에 명시된 차트 타임프레임(시간 단위). 없으면 None.
    _TIMEFRAME 은 앞의 키워드(Timeframe/타임프레임/봉)를 요구하므로 산문 속 '4h' 만
    으로는 매칭되지 않는다. 예: '🕒 Timeframe: 1H' → 1.0, 'Timeframe: 4H' → 4.0,
    'Timeframe: 1D' → 24.0. 0 값은 무의미해 None 반환(스캘프 판정창 오지정 방지)."""
    if not text:
        return None
    m = _TIMEFRAME.search(text)
    if m:
        v = float(m.group(1)) * _TF_UNIT_HOURS[m.group(2).lower()]
        return v if v > 0 else None
    w = _TIMEFRAME_WORD.search(text)
    if w:
        return 168.0 if w.group(1).lower() in ("weekly", "주봉") else 24.0
    return None


def judgment_window_hours(tf_hours, entry, tp) -> float:
    """적중 판정 창 (2026-07-23 확정 B안): 작성자 타임프레임 우선, 없으면 목표 거리
    비례(10% 거리당 7일), 어느 쪽이든 상한 30일.
    근거: '+40% 목표를 7일 만에 채점'하는 부당함 제거 — 작성자가 밝힌 지평이
    가장 공정한 귀속 기준(사용자 결정)."""
    cap = 720.0  # 30일
    if tf_hours is not None:
        if tf_hours <= 0.5:      # ≤30분봉: 초단타
            return 72.0          # 3일
        if tf_hours <= 2:        # 1H~2H
            return 168.0         # 7일
        if tf_hours <= 12:       # 4H 등
            return 336.0         # 14일
        return cap               # 1D 이상: 30일
    if entry and tp and entry > 0:
        dist_pct = abs(tp - entry) / entry * 100
        return max(168.0, min(cap, dist_pct / 10.0 * 168.0))
    return 168.0


class _Vals(list):
    """_grab_after 가 돌려주는 값 목록 + 부가정보. list 를 그대로 상속해 기존
    호출부([0]/[-1]/len)는 전혀 바뀌지 않고, 사다리 단계 수만 얹어 나른다."""
    __slots__ = ("ladder_n", "ladder_last", "ladder_values")

    def __init__(self, seq=()):
        super().__init__(seq)
        self.ladder_n = 0
        self.ladder_last = None   # 사다리 마지막 값 — 엔트리 범위 복원에 사용
        self.ladder_values = []   # 인라인 사다리 전체 값 — tps_all 계산용


class _Grabbed(list):
    """_grab_after 의 반환 그릇. 담긴 값들이 스펙형(라벨 뒤 :/=)에서 나왔는지를
    호출부에 알린다 — 줄바꿈 나열 사다리를 셀 때 산문형을 배제하는 데 쓴다."""
    __slots__ = ("is_spec",)

    def __init__(self, seq=(), is_spec=False):
        super().__init__(seq)
        self.is_spec = is_spec


def _to_float(s: str) -> Optional[float]:
    try:
        return float(s.replace(",", "").replace("$", "").strip())
    except (ValueError, AttributeError):
        return None


def _clean(text: str) -> str:
    """숫자 오인 유발 토큰을 먼저 지운다(레버리지/R배수/퍼센트/연도/목록마커/라벨 서수).

    공통 원칙: **가격이 아닌 숫자**를 가격 탐색 전에 없앤다. 남기면 라벨 뒤 창에서
    가장 왼쪽 숫자로 잡혀 그대로 진입가·목표가가 된다(전부 실사고 이력이 있다)."""
    text = _LEVERAGE_LABEL.sub(" ", text)  # "Leverage x 5-10-20" (2026-09-27, 레버리지 템플릿)
    text = _LEVERAGE.sub(" ", text)
    text = _RR_RATIO.sub(" ", text)     # "RR 1:1.5"·"R/R:2" (2026-09-27, 772·656)
    text = _R_MULTIPLE.sub(" ", text)   # "4R return" → 4 가 진입가로 (AVAX 실사고)
    text = _PERCENT.sub(" ", text)
    text = _DATE_FULL.sub(" ", text)
    text = _YEAR_KR.sub(" ", text)
    text = _YEAR_CTX.sub(" ", text)
    text = _TF_TOKEN.sub(" ", text)     # "H1"·"M15" (2026-09-27, 656)
    text = _TF_NUM_UNIT.sub(" ", text)  # "4H"·"15m"·"30 Days" (2026-09-27, 575·879·831)
    text = _LIST_MARKER.sub("", text)   # "1) 1.1129" → 마커만 제거, 가격은 보존
    text = _T_ORDINAL.sub("TP", text)   # "T1:" → "TP:" (2026-09-27)
    text = _ORDINAL_LABEL.sub(lambda m: m.group(1), text)  # "TP1:" → "TP:"
    text = _SPACED_ORDINAL_LABEL.sub(lambda m: m.group(1), text)  # "Target 1:" → "Target:"
    # 서수 제거 **뒤에** 돈다 — "Target 1:1.5" 가 먼저 "Target:1.5" 가 되어야
    # 가격 1.5 를 비율로 오인해 지우지 않는다.
    text = _ONE_RATIO.sub(" ", text)    # "1:2 risk-to-reward", "~2.3:1 to 3:1"
    return text


def _grab_after(label_pat, text: str) -> list:
    """라벨 뒤 80자 창 안에서 첫 숫자(또는 범위)를 검색해 수집. 범위면 [lo, hi].
    '맨 앞 고정'이 아니라 검색으로 하는 이유: 라벨과 숫자 사이에 '가', 'around', '@',
    통화기호 등 잡토큰이 끼는 경우가 흔하기 때문. 단, 창을 짧게 잡아 무관한 숫자를
    끌어오지 않는다(가장 왼쪽 숫자만 채택).

    사다리형 나열("7.15 - 7.45 - 7.85")은 범위보다 먼저 판정해 **맨 앞 값 하나만**
    돌려준다(_LADDER 주석 참고). 호출부가 tp 를 `[-1]`(범위면 상단)로 꺼내므로,
    나열을 범위로 넘기면 두 번째 목표가 TP1 자리에 앉는다.

    ⚠️ 창 크기가 비대칭이다 — _LADDER 만 '첫 숫자가 있는 줄의 끝'(단 _LADDER_MAX_WINDOW
    상한)까지 보고, _RANGE·_SINGLE 은 80자 고정이다 (2026-07-27 교차감사 B-M1 수리,
    2026-07-28 콤마 자릿수 버그 수리로 30→80 확장).
    비대칭은 의도된 것이고 그 상한이 대가다(상수 주석의 ReDoS 실측 참고). 이유:
      · 세 값 나열은 최소 15자를 먹는다("7.15 - 7.45 - 7.85"는 18자). 라벨과 첫 숫자
        사이 필러가 15자를 넘으면 30자 창 안에 세 번째 rung 이 안 들어와 _LADDER 가
        매칭 실패 → _RANGE 로 떨어져 두 번째 값이 TP1 자리에 앉는다. 합성 재현으로
        필러 15~19자에서 tp 가 7.15 대신 7.45/7.4 로 나옴을 확인했다(수리 전).
      · 대칭 확대(_RANGE·_SINGLE 도 줄 끝까지)는 해법이 아니다 — 실패 띠가 29~32자로
        옮겨갈 뿐이고, 같은 줄 뒤쪽의 무관한 숫자까지 끌어와 오탐만 는다.
    _LADDER 만 넓혀도 안전한 근거(둘 다 성립해야 채택):
      · 사다리는 나열 전체를 봐도 **맨 앞 값 하나만** 반환한다 — 창이 길어져 뒤쪽
        rung 이 더 보여도 결과값은 안 변하고 '온전한 나열인가' 판별만 정확해진다.
      · `lad.start() <= sng.start()` 가드로 '라벨에서 30자 안에 숫자가 있고 그 첫
        숫자에서 사다리가 시작할 때'만 채택한다 → 라벨 근접성 규칙은 종전 그대로고,
        넓힌 창은 '나열이 어디까지 이어지는가'만 결정한다(sng 없으면 아예 후보 없음).
      · 줄 경계에서 잘리므로 다음 줄에 있는 다른 라벨의 숫자는 유입되지 않는다.
    기준선을 '라벨 뒤 개행'이 아니라 '첫 숫자가 있는 줄의 개행'으로 잡은 이유:
    "TARGETS (all levels):\\n7.15 - 7.45 - 7.85" 처럼 값이 다음 줄로 내려가는 표기가
    실전에 있는데(라벨 뒤 잡토큰이 \\s* 를 끊어 m.end() 가 라벨 줄에 남는다), 라벨 줄
    끝에서 자르면 창이 비어 사다리를 통째로 놓친다 — 종전 30자 창은 개행을 넘겨
    보던 동작이라 그게 회귀가 된다."""
    return _grab_after_ex(label_pat, text)


def _window_end(text: str, end: int) -> int:
    """창 끝이 숫자 토큰 한가운데면 그 토큰 끝까지 늘린다 (감사 C-Z 612 TRUMP:
    "from 2.16" 이 80자 경계에서 "2.1" 로 잘렸다)."""
    n = len(text)
    while end < n and end > 0 and (
            text[end].isdigit()
            or (text[end] in ".," and end + 1 < n and text[end + 1].isdigit()
                and text[end - 1].isdigit())):
        end += 1
    return end


def _is_trigger(text: str, nstart: int, nend: int, same_line: bool = False) -> bool:
    """진입값(text[nstart:nend]) 주변이 돌파 트리거 서술인가 (감사 C-B).
    문맥 = 그 숫자가 놓인 문장(문장부호·개행 경계) ∪ 숫자 앞뒤 ±40자.
    'buy stop' 은 브래킷 표(663: 머리행 "(Buy Stop)" → 다음 행 "Buy Entry:")처럼
    떨어져 쓰이는 일이 있어 ±100자까지 본다. 리테스트 동반이면 예외.

    same_line=True(스펙형 "Entry: 2500" — 2026-09-27 리뷰 RV2-E2): ±40자 창을 **그 숫자가
    놓인 줄**로 자른다. 스펙형은 한 줄이 한 항목이라, 옆 줄(TP 줄의 "reclaim of the
    range high", 무효화 줄의 "breakout above")의 트리거 단어가 진입을 기각해 정상 셋업
    전체가 None 이 됐다. buy stop ±100자 예외는 그대로 둔다(663 브래킷 표)."""
    s = nstart
    while s > 0 and text[s - 1] != "\n" and not (
            text[s - 1] in ".!?" and (s >= len(text) or text[s].isspace())):
        s -= 1
    e = nend
    n = len(text)
    while e < n and text[e] != "\n" and not (
            text[e] in ".!?" and (e + 1 >= n or text[e + 1].isspace())):
        e += 1
    cs = min(s, max(0, nstart - _TRIGGER_CTX))
    ce = max(e, min(n, nend + _TRIGGER_CTX))
    if same_line:
        ls = text.rfind("\n", 0, nstart) + 1
        le = text.find("\n", nend)
        cs, ce = max(cs, ls), min(ce, n if le < 0 else le)
    ctx = text[cs:ce]
    wide = text[max(0, nstart - _BUY_STOP_CTX): min(n, nend + _BUY_STOP_CTX)]
    hit = bool(_TRIGGER.search(ctx)) or bool(_BUY_STOP.search(wide))
    if not hit:
        return False
    return not _RETEST.search(ctx)


_POST_RESIST = re.compile(r"[ \t]*(?:\w+[ \t]+){0,2}?resistance\b", re.I)


def _grab_after_ex(label_pat, text: str, stop_pat=None, allow_to_range: bool = False,
                   reject_trigger: bool = False, prose_resistance_reject: bool = False) -> list:
    """_grab_after 의 확장판 (2026-09-27 감사 C-L/B/Z).

    stop_pat: 산문형 창을 끊을 '다른 라벨' 키워드. None 이면 문장 끝만 본다.
    allow_to_range: 라벨과 같은 줄의 "N to N" 을 범위로 인정(진입 전용).
    reject_trigger: 돌파 트리거 문맥의 값을 버린다(진입 전용). 스펙형 후보가
      있었는데 전부 트리거로 버려졌으면 산문형으로 내려가지 않는다 — 작성자가
      명시한 진입이 트리거인 글에서 산문 숫자를 줍는 것은 더 나쁜 오류다."""
    out, spec_out = [], []
    spec_seen = False
    for m in label_pat.finditer(text):
        # 스펙형(라벨 뒤에 :/= 가 붙은 표기)인지 — 2026-07-27 ONDO 실사고.
        # 한 글에 라벨이 여러 번 나오면 종전엔 **맨 앞 것**이 이겼는데, 제목이
        # "ONDO Breakout: Pullback Entry Toward \$0.415" 라 제목의 산문 수치가
        # 본문 스펙 "Entry: \$0.346–\$0.350" 을 눌렀다(알림이 엉뚱한 가격에 나간다).
        # 작성자가 콜론으로 명시한 값이 산문 언급보다 항상 더 신뢰할 만하므로,
        # 스펙형이 하나라도 있으면 그것만 쓰고 산문형은 버린다. 스펙형이 없을 때만
        # 종전처럼 전부 쓴다(콜론 없이 "Entry 10.5" 로 쓰는 소스가 실제로 있다).
        is_spec = m.group(0).rstrip().endswith((":", "="))
        # 2026-07-28 수리: 30자 고정 창이 라벨-값 사이 필러가 길면 숫자를 콤마 경계에서
        # 잘라 자릿수를 버리는 버그(필러 1글자 차이로 1,234,567→1,234, 1000배 오차).
        # 줄 끝 대신 단순 확장(30→80)을 쓴다 — 다음 줄에 값이 오는 포맷("Stop-Loss:\n\n5")
        # 이 실전에 존재하므로 줄 경계로 자르면 회귀가 발생한다.
        # 2026-09-27 감사 C-Z: 창 끝이 숫자 중간이면 그 숫자 끝까지 늘린다(2.16→2.1 방지).
        _end = _window_end(text, m.end() + 80)
        if not is_spec:
            # 산문형 근접 제약(감사 C-L): 문장 끝·다른 라벨 키워드에서 창을 끊는다.
            _se = _SENT_END.search(text, m.end(), _end)
            if _se:
                _end = _se.start()
            if stop_pat is not None:
                _sp = stop_pat.search(text, m.end(), _end)
                if _sp:
                    _end = _sp.start()
        window = text[m.end(): _end]
        rng = _RANGE.search(window)
        sng = _SINGLE.search(window)
        if sng and not is_spec and sng.start(1) > _PROSE_MAX_GAP:
            # 산문형은 라벨과 숫자 사이 25자 이하만 인정 — 먼 숫자는 다른 뜻이다
            # (575 "Enter … Over The Next 30 Days", 669 "open buy positions. Target $13.7").
            continue
        lad = None
        if sng:
            # start(1) = 숫자 첫 자리 위치. start(0) 은 _NUM 앞의 `\$?\s*` 때문에
            # 개행 자체를 가리킬 수 있어(값이 다음 줄인 표기) 창이 빈 채로 잘린다.
            _nl = text.find("\n", m.end() + sng.start(1))  # 첫 숫자가 놓인 줄의 끝
            # 줄 끝과 _LADDER_MAX_WINDOW 중 **짧은 쪽**. 상한의 근거는 그 상수 주석
            # 참고(중첩 수량자의 2차 역추적 — 개행 없는 긴 숫자 줄 방어).
            _lad_end = min(len(text) if _nl < 0 else _nl, m.end() + _LADDER_MAX_WINDOW)
            if not is_spec:
                _lad_end = min(_lad_end, _end)  # 산문형은 문장/라벨 경계도 넘지 않는다
            _seg = text[m.end():_lad_end]
            lad = _LADDER.search(_seg)
            # 화살표 나열은 2개짜리도 사다리다(_LADDER_ARROW 주석 참고). 둘 다
            # 잡히면 더 왼쪽에서 시작하는 쪽 — 같은 라벨 뒤의 첫 표기가 주제다.
            _arw = _LADDER_ARROW.search(_seg)
            if _arw and (not lad or _arw.start() < lad.start()):
                lad = _arw
        _base = m.end()

        def _rejected(nstart, nend):
            """돌파 트리거 문맥이면 True(진입 전용). 스펙형이었다면 표시해 둔다."""
            nonlocal spec_seen
            if prose_resistance_reject and not is_spec and _POST_RESIST.match(
                    text, _base + nend):
                # 산문 진입 숫자 바로 뒤가 "key resistance" — 저항은 롱 진입가가
                # 아니다(754 ARB "entry opportunity before the next push toward
                # 0.2241 key resistance").
                return True
            if reject_trigger and _is_trigger(text, _base + nstart, _base + nend,
                                              same_line=is_spec):
                if is_spec:
                    spec_seen = True
                return True
            return False

        # lad/sng 의 start() 는 둘 다 m.end() 기준 오프셋이라 창 길이가 달라도 비교 가능.
        if lad and lad.start() <= sng.start():
            first = _to_float(lad.group(1))
            if first:
                if _rejected(lad.start(1), lad.end()):
                    continue
                # 값은 첫 rung 하나만 쓰되(판정·배점 축이 TP1), 사다리가 몇 단계인지는
                # 표시용으로 함께 실어 보낸다(2026-07-27 사용자 승인 A안 — 알림에
                # "1/8단계"). 반환 타입을 리스트 그대로 유지해야 호출부의 [0]/[-1]
                # 접근이 안 깨지므로, 개수만 속성으로 얹는 얇은 하위 클래스를 쓴다.
                _nums = _SINGLE.findall(lad.group(0))
                vals = _Vals([first])
                vals.ladder_n = len(_nums)
                # 마지막 rung — 엔트리 경로에서 "Entry - $90/95/$100" 같은 슬래시
                # 구분 존 표기의 범위(90~100)를 복원하는 데 쓴다. TP 경로는 이 값을
                # 읽지 않으므로 TP 동작에는 영향 없다.
                vals.ladder_last = _to_float(_nums[-1]) if _nums else None
                # 전체 rung 값 — tps_all(B안 필터) 계산용. None 제거 후 보존.
                vals.ladder_values = [v for v in (_to_float(n) for n in _nums) if v is not None]
                (spec_out if is_spec else out).append(vals)
                continue
        # "N to N" 범위 — 진입 라벨과 같은 줄에서만(감사 C-Z 646).
        if allow_to_range and sng:
            _le = text.find("\n", m.end())
            _line = text[m.end(): min(_end, len(text) if _le < 0 else _le)]
            rt = _RANGE_TO.search(_line)
            if rt and rt.start() <= sng.start():
                lo, hi = _to_float(rt.group(1)), _to_float(rt.group(2))
                if lo and hi:
                    if _rejected(rt.start(1), rt.end()):
                        continue
                    (spec_out if is_spec else out).append(sorted([lo, hi]))
                    continue
        # 범위와 단일이 둘 다 잡히면, 더 왼쪽에서 시작하는 쪽을 채택(범위 우선 동률).
        if rng and (not sng or rng.start() <= sng.start()):
            lo, hi = _to_float(rng.group(1)), _to_float(rng.group(2))
            if lo and hi:
                if _rejected(rng.start(1), rng.end()):
                    continue
                (spec_out if is_spec else out).append(sorted([lo, hi]))
                continue
        if sng:
            v = _to_float(sng.group(1))
            if v:
                if _rejected(sng.start(1), sng.end()):
                    continue
                (spec_out if is_spec else out).append([v])
    # 스펙형이 하나라도 있으면 산문형은 버린다(위 is_spec 주석 참고).
    # 스펙형 값이 전부 돌파 트리거로 버려졌으면(spec_seen) 산문형으로 내려가지 않는다.
    if not spec_out and spec_seen:
        return _Grabbed([], is_spec=False)
    return _Grabbed(spec_out or out, is_spec=bool(spec_out))


def _sanity(value: float, current_price: Optional[float], max_dev: float) -> bool:
    """현재가 대비 유효 범위 검사. 현재가 모르면 통과(판단보류).

    **비대칭이다** (2026-09-14). 위아래가 뜻하는 바가 다르기 때문:
      · 위(value > 현재가): 롱 진입가가 현재가보다 높다 = 이미 지나간 자리이거나
        오파싱. 여기가 오파싱 탐지축이라 `max_dev`(기본 60%)로 좁게 잡는다.
      · 아래(value < 현재가): 가격이 오른 뒤의 **깊은 눌림목 대기**로 정상이다.
        같은 60%로 자르면 멀쩡한 셋업이 죽는다 — 실측 NEAR(진입 1,360원 vs 현재
        3,138원 = −56.7%)는 감시 중인 정상 레벨인데 커트에서 3.3%p 차이였다.
        같은 날 감시 단계 방어선(price_check)도 "하단 이탈은 정상"이라 판단해
        비대칭을 택했는데, 수집 단계만 대칭이라 두 관문의 기준이 어긋나 있었다.
    하단은 `_SANITY_BELOW_MAX`(기본 0.90 = 현재가의 10% 미만이면 기각)로 둔다 —
    그 아래는 자릿수 오파싱(0.83 → 0.083)이지 눌림목이 아니다.

    이 비대칭이 수집을 **느슨하게** 만드는 방향이라는 점은 의도된 것이다: 종전엔
    CoinGecko 달러가가 있는 코인만 이 관문을 탔고, 2026-09-13 업비트 폴백가를
    붙이면서 157개 코인이 새로 이 관문에 들어왔다. 새 관문이 정상 셋업을 자르는
    부작용을 막는 게 이 수정의 목적이다(감사 F3)."""
    if current_price is None or current_price <= 0 or value is None:
        return True
    dev = (value - current_price) / current_price
    if dev >= 0:
        return dev <= max_dev
    return -dev <= _SANITY_BELOW_MAX


def _setting(key: str, default=True):
    """롤백 스위치 조회 — 키가 없거나(구 설정) 조회 실패면 기본값."""
    try:
        return settings.get(key)
    except Exception:  # noqa: BLE001
        return default


def parse_setup(text: str, current_price: Optional[float] = None,
                max_dev: float = 0.60, direction_hint: Optional[str] = None) -> Optional[dict]:
    """
    반환: {direction, entry, entry_low, entry_high, sl, tp, rr} 또는 None(엔트리 없음).
    entry 는 대표값(범위면 중앙), entry_low/high 는 범위 경계(단일이면 동일).
    현재가가 주어지면 엔트리 sanity 실패 시 None.

    direction_hint: 작성자가 단 방향 태그('long'/'short', TradingView 메타). 있으면
      텍스트 판정보다 우선한다(2026-09-27 감사 C-D ①, 스위치 extract_use_tv_direction).
    """
    if not text:
        return None
    clean = _clean(text)

    # 방향 (2026-09-27 감사 C-D / A-T3 재설계)
    #  ① 작성자 태그 우선 ② 비방향 관용구 제거 후 힌트 판정 ③ 애매/없음이면 TP 기하
    #  (유효 TP 후보 2개 이상이 전부 진입가 아래 → short) ④ 그래도 모르면 long.
    direction = None
    if direction_hint in ("long", "short") and _setting("extract_use_tv_direction"):
        direction = direction_hint
    _ambiguous = False
    if direction is None:
        _dir_text = _DIRECTION_NOISE.sub(" ", clean)
        is_long = bool(_LONG_HINTS.search(_dir_text))
        is_short = bool(_SHORT_HINTS.search(_dir_text))
        if is_long and not is_short:
            direction = "long"
        elif is_short and not is_long:
            direction = "short"
        else:
            direction = "long"  # 애매하면 long 가정(이 봇은 하향 터치=매수 관점)
            _ambiguous = True

    entries = _grab_after_ex(_ENTRY_LABEL, clean, stop_pat=_ENTRY_STOP, allow_to_range=True,
                             reject_trigger=bool(_setting("extract_breakout_trigger_skip")),
                             prose_resistance_reject=True)
    if not entries:
        return None

    # 첫 엔트리 채택 (여러 개면 첫 라벨)
    e = entries[0]
    _elast = getattr(e, "ladder_last", None)
    if _elast is not None:
        # 사다리형 엔트리 존("Entry - $90/95/$100" 등) — 첫값·끝값으로 범위를 복원한다.
        # TP 사다리는 ladder_last 를 무시하므로 TP 동작에는 영향 없다.
        entry_low, entry_high = min(e[0], _elast), max(e[0], _elast)
    else:
        entry_low, entry_high = (e[0], e[-1])
    entry = (entry_low + entry_high) / 2

    if not _sanity(entry, current_price, max_dev):
        return None

    sls = _grab_after_ex(_SL_LABEL, clean, stop_pat=_SL_STOP)
    tps = _grab_after_ex(_TP_LABEL, clean, stop_pat=_TP_STOP)
    sl = sls[0][0] if sls else None

    # ③ TP 기하 방향 판정 — 방향 단어가 애매/없을 때만. 숏 템플릿(682 FIL 등:
    # "This setup supports a decline" + Target 1~3 전부 진입가 아래)은 방향 단어가
    # 아예 없어 long 이 됐고, _tp_valid 가 아래쪽 TP 를 조용히 버려 '무TP 롱'이 됐다.
    # 크기 sanity(0.25~4배)를 통과한 서로 다른 TP 후보가 **2개 이상** 전부 진입가
    # 아래일 때만 short — 1개만으로는 서수 오인(TP=1.0) 한 건에 뒤집힐 수 있다.
    if _ambiguous and entry and entry > 0:
        _geo = set()
        for _g in (tps or []):
            if not _g:
                continue
            for _v in (getattr(_g, "ladder_values", None) or [_g[-1]]):
                if _v is not None and entry * _SANITY_LO_MULT <= _v <= entry * _SANITY_HI_MULT \
                        and _v != entry:
                    _geo.add(_v)
        if len(_geo) >= 2 and all(_v < entry for _v in _geo):
            direction = "short"
        # 보조 신호(감사 A-T3 추천 1): TP 후보가 1개뿐이어도 그게 진입가 아래이고
        # 손절 후보가 진입가 **위**면 숏이다(792 BTC "Ultimate Swing Short": Targets 가
        # 라벨 없는 번호 목록이라 첫 값만 잡히고, Stop Loss 93,300 > Entry 84,900).
        elif _geo and all(_v < entry for _v in _geo) and sl is not None and sl > entry:
            direction = "short"

    def _tp_valid(v) -> bool:
        """방향·크기 sanity 를 모두 통과하는 tp 후보인가 (아래 sl 사후검증과 같은 기준)."""
        if v is None:
            return False
        if direction == "long" and entry is not None and v <= entry:
            return False
        if direction == "short" and entry is not None and v >= entry:
            return False
        if entry is not None and entry > 0 and not (entry * _SANITY_LO_MULT <= v <= entry * _SANITY_HI_MULT):
            return False
        return True

    # tp 후보 선정 (2026-08-01 CFX 실전 발견 — 원문 "Target 1: 0.04087(entry
    # 0.04088과 거의 동일해 sanity 탈락) / Target 2: 0.04259 / Target 3: 0.04392").
    # 예전엔 첫 후보(tps[0][-1])만 보고, 그게 sanity 를 통과 못 하면 tp 를 통째로
    # None 처리했다 — 뒤따르는 멀쩡한 Target 2/3 까지 함께 버려져 tps_all 도 빈
    # 목록이 됐다(그 목록이 감시밴드·다단계 TP 알림의 유일한 입력이라 영향이 큼).
    # 이제 후보를 순서대로 검증해 처음 통과하는 값을 쓴다. 한 줄 사다리(tps[0]
    # 하나뿐인 경우)는 동작 변화 없음 — 줄바꿈 나열형(여러 줄 Target N:)에서만
    # 실질적으로 달라진다.
    tp = None
    _tp_grp = None
    for _g in (tps or []):
        if _g and _tp_valid(_g[-1]):
            tp = _g[-1]
            _tp_grp = _g
            break

    # SL 방향·크기 sanity(2026-07-23, ALGO/ARB 실전 사고 후 추가) — 라벨 매칭이
    # 정상이어도 파싱이 미묘하게 틀리면(신규 소스 포맷 등) SL이 방향과 모순되거나
    # (long인데 sl>=entry) 비현실적 크기(엔트리 대비 4배 초과/0.25배 미만)일 수 있다.
    # 값을 버리지 않고 억지로 쓰기보다 "판단 보류"(None)로 되돌린다(이 모듈의 기존
    # 철학 — 모르는 것과 틀린 것을 구분). TP는 위 _tp_valid 후보선정에서 이미 같은
    # 기준으로 검증됐다.
    if direction == "long":
        if sl is not None and entry is not None and sl >= entry:
            sl = None
    else:
        if sl is not None and entry is not None and sl <= entry:
            sl = None
    if entry is not None and entry > 0:
        if sl is not None and not (entry * _SANITY_LO_MULT <= sl <= entry * _SANITY_HI_MULT):
            sl = None

    # 손익비
    rr = None
    if entry and sl and tp:
        if direction == "long":
            risk, reward = entry - sl, tp - entry
        else:
            risk, reward = sl - entry, entry - tp
        if risk > 0 and reward > 0:
            rr = round(reward / risk, 2)

    # 사다리 단계 수 — 원래 표시 전용(알림의 "1/8단계")이었지만 2026-08-15 v5 부터
    # 등급 산식에 들어간다(count<=1 이면 -3 감점). 0 의 의미는 '단일 목표 또는
    # 사다리 미상' — 감점 실증치 -3 은 미상 행이 섞인 이 저장 필드 그대로에서
    # 측정됐으므로(research_2026-08-15_db_signal_analysis.md) 산문형 다중 목표를
    # 0 에 두는 현행 의미와 일관된다. sanity 로 걸러진 rung(아래 각 분기)은 애초에
    # 유효하지 않은 값이라 걸러낸 뒤의 개수가 곧 참값이다. tp 가 sanity 로
    # 폐기됐으면 같이 버린다(값 없는데 단계만 남으면 표시가 거짓말을 한다).
    tp_ladder_count = 0
    if _tp_grp and tp is not None:
        _n = getattr(_tp_grp, "ladder_n", 0)
        if _n > 1:
            # 한 줄 나열형("a - b - c", "a → b") — 2026-08-08 재검토 수정: 원래는
            # rung 개수(_n)를 그대로 썼는데, tps_all(아래)은 같은 rung 목록에
            # entry 0.25~4배 크기+방향 sanity 를 걸러 세므로 크기 이상값이 섞인
            # 원문("TP: $12 - $15 - $50", entry=$10 이면 $50 은 4배 초과로
            # tps_all 에서 탈락)에서 "3/3단계"라 표시되는데 실제 감시 목록은
            # 2개뿐인 불일치가 있었다. 줄바꿈 스펙형(아래 elif 분기, 2026-08-01
            # 수정)과 동일 기준(entry 방향+크기)으로 통일한다.
            _ivals = getattr(_tp_grp, "ladder_values", None) or list(_tp_grp)
            _ilo, _ihi = ((entry * _SANITY_LO_MULT, entry * _SANITY_HI_MULT) if (entry and entry > 0)
                         else (float("-inf"), float("inf")))
            _ivalid = {v for v in _ivals if v is not None and _ilo <= v <= _ihi
                      and (entry is None
                           or (v > entry if direction == "long" else v < entry))}
            # 스펙형 자매 분기와 동일 게이트: 유효 rung 이 2개 미만이면
            # tp_ladder_count 는 초기값(0)에 남는다 — 필터 안 거친 원시값으로
            # 되돌아가지 않는다. 2026-08-15 v5: 상한(12) 게이트는 제거 — 13단+
            # 진짜 사다리를 0(미상)으로 뭉개면 v5 가 -3 을 잘못 물린다. 참 개수를
            # 저장하고 "1/N" 표시 상한은 렌더러(notify/telegram.py)가 담당.
            if 1 < len(_ivalid):
                tp_ladder_count = len(_ivalid)
        else:
            # 줄바꿈 나열형("Target 1: …" 세 줄, "• TP1: …" 네 줄). 실측상 원문의
            # 절반가량이 이 형태다. 값이 서로 다른 것만 세는 이유는 "Take-Profit
            # Targets:" 같은 머리말이 첫 목표를 한 번 더 잡아 단계를 부풀리기 때문.
            #
            # 스펙형(콜론 라벨)일 때만 세는 이유 — 2026-07-27 SOL 오탐. 산문에서
            # 목표가 여러 번 나오는 건 사다리가 아니라 **같은 자리의 다른 안**인
            # 경우가 많다("originally … take-profit at 73.6 … optimized … 74.06"
            # = 원래안과 수정안). 그걸 '1/2단계'로 보여주면 있지도 않은 분할 익절을
            # 지어내는 셈이라, 작성자가 라벨로 명시한 표기만 사다리로 인정한다.
            #
            # 각 rung 에도 tp 와 **같은 크기 sanity**(엔트리의 0.25~4배)를 건다 —
            # 2026-07-27 JUP/JTO 실사고. 원문이 "TP4:\n5 *10" 처럼 깨져 들어와
            # 0.185 짜리 셋업에 5.0 이 한 단계로 잡혔다(실제 목표는 3개, 본문의
            # 배분도 TP1~TP3 뿐). 대표값 tp 만 걸러선 단계 수가 부푼 채로 남는다.
            if tps.is_spec:
                _lo, _hi = ((entry * _SANITY_LO_MULT, entry * _SANITY_HI_MULT) if (entry and entry > 0)
                            else (float("-inf"), float("inf")))
                # 2026-08-01 수정: 크기뿐 아니라 방향도 걸러 tps_all/_tp_valid 와
                # 같은 기준으로 맞춘다 — 안 그러면 CFX 케이스처럼 entry 아래인
                # Target 1(sanity 탈락, 실제 목표에서 제외됨)까지 단계 수에 세어
                # "1/3단계"로 표시되는데 실제 유효 목표는 2개뿐인 불일치가 생겼다.
                # v[-1] 기준 (2026-08-07 재검토): tp 선정(grp[-1])·tps_all 과 동일
                # 끝값으로 통일 — 범위형 스펙(TP1: 5.0~5.5)에서 하한(v[0])은 entry
                # 아래라 방향 필터에 걸리는데 상한(v[-1])은 tps_all 에 살아남아
                # "1/N단계" 표시와 감시 목록 개수가 어긋나던 불일치 제거.
                _lasts = {v[-1] for v in tps if v and _lo <= v[-1] <= _hi
                          and (entry is None
                               or (v[-1] > entry if direction == "long" else v[-1] < entry))}
                # 2026-08-15 v5: 상한(12) 게이트 제거 — 위 인라인 분기와 동일 사유.
                if 1 < len(_lasts):
                    tp_ladder_count = len(_lasts)

    # 전체 TP 목표가 목록 — 가장 가까운 것부터 먼 것 순. B안 필터에서 "마지막 TP가
    # 스윙급(5%+)인가"를 판단하는 데 쓴다. tp 가 sanity 로 폐기됐으면 전체를 비운다
    # (대표값 없는데 목록만 남기면 후처리가 혼선).
    tps_all: list = []
    if tps and tp is not None and entry and entry > 0:
        _lo, _hi = entry * _SANITY_LO_MULT, entry * _SANITY_HI_MULT
        _n = getattr(_tp_grp, "ladder_n", 0)
        if _n > 1:
            # ladder_values: _grab_after 에서 저장한 인라인 사다리 전체 값 목록.
            # list(_tp_grp)=[first] 은 tp 계산용 하나뿐이라 tps_all 에는 불충분.
            _cands = getattr(_tp_grp, "ladder_values", None) or list(_tp_grp)
        elif tps.is_spec:
            _cands = [v[-1] for v in tps if v]  # 줄바꿈 스펙형: tp 선정(grp[-1])과 동일 기준
        else:
            _cands = [_tp_grp[-1]] if _tp_grp else []  # 단순 TP 단일 값
        # 방향·크기 sanity 적용 (tp 에 적용한 것과 동일 기준)
        _valid = []
        for _c in _cands:
            if not (_lo <= _c <= _hi):
                continue
            if direction == "long" and _c > entry:
                _valid.append(_c)
            elif direction == "short" and _c < entry:
                _valid.append(_c)
        # 중복 제거 + 정렬(closest first: long=오름차순, short=내림차순)
        _seen: set = set()
        for _c in (sorted(_valid) if direction == "long" else sorted(_valid, reverse=True)):
            if _c not in _seen:
                _seen.add(_c)
                tps_all.append(_c)

    # 대표 TP = 유효 TP 사다리의 첫 값 (2026-09-27 감사 A-T5 / 종합 P1-10).
    # 종전 tp 는 '본문 순서상 처음 유효한 그룹'이라, "Target 1: 1.20 | Target 2: 1.16"
    # (630 ZRO)처럼 작성자 순서가 가격 순서와 다르면 알림 "목표"가 TP1 이 아니었다.
    # 판정부 price_check._volume_band_tps 는 유효 TP(entry < t <= entry*4)를 오름차순
    # 정렬해 [0] 을 TP1 로 쓴다 — tps_all 은 같은 방향·크기 기준을 거쳐 롱이면
    # 오름차순이므로 tps_all[0] 이 곧 판정부의 TP1 이다. rr 도 그 값으로 다시 잰다.
    if tps_all and tp is not None and tps_all[0] != tp:
        tp = tps_all[0]
        rr = None
        if entry and sl and tp:
            if direction == "long":
                risk, reward = entry - sl, tp - entry
            else:
                risk, reward = sl - entry, entry - tp
            if risk > 0 and reward > 0:
                rr = round(reward / risk, 2)

    return {
        "direction": direction,
        "entry": entry,
        "entry_low": entry_low,
        "entry_high": entry_high,
        "sl": sl,
        "tp": tp,
        "rr": rr,
        "tp_ladder_count": tp_ladder_count,
        "tps_all": tps_all,
    }


# ── 비셋업 글 판정 (2026-09-27 감사 C-R / C-C, 종합 P0-5) ─────────────────────
# 결과보고·관찰글("+91% From Our Entry"(835), "SCENARIOS (to watch — NOT signals)"
# (678·697), "+208% PROFIT: Our Entry Filled"(881))이 새 셋업으로 수집됐다. 산문
# entry/buy 창이 지지선·현재가를 주워 알림까지 나갔다.
# 면책문 예외: "(Not a Trading signal or Financial Advice)"(774·775·777·811 정상
# 셋업), "not a signal, DYOR" 류는 스킵하지 않는다 — 문맥(±60자)에 면책 어휘가
# 있으면 면책문으로 본다.
_NOT_SIGNAL = re.compile(r"\bnot\s+(?:a\s+)?signals?\b", re.I)
_DISCLAIMER_CTX = re.compile(
    r"financial\s+advice|investment\s+advice|educational|own\s+research|\bDYOR\b|\bNFA\b", re.I)
_RESULT_REPORT = re.compile(
    r"\bfrom\s+our\s+entry\b|\bentry\s+filled\b"
    r"|\btp\s*\d{0,2}\s*(?:hit|reached|achieved|done)\b"
    r"|\btargets?\s*\d{0,2}\s*(?:reached|hit|achieved|smashed)\b"
    r"|\+\s*\d+(?:\.\d+)?\s*%\s*(?:profits?|gains?)\b",
    re.I)
# "once TP1 hit, move SL to entry" 처럼 조건절 안의 'TP hit' 은 결과보고가 아니다.
_CONDITIONAL_BEFORE = re.compile(r"\b(?:if|once|when|after|until|before|as\s+soon\s+as)\b[^.\n]{0,20}$", re.I)
# 2026-09-27 리뷰 RV2-E3: 운영 플랜 문구 "TP1 hit -> move SL to entry" 는 조건절이다
# (hit 뒤에 화살표·then·move). 콤마는 "move/then" 이 이어질 때만 인정한다 — "TP1 hit,
# +10% profit" 같은 진짜 결과보고를 놓치지 않게.
_CONDITIONAL_AFTER = re.compile(r"[ \t]*,?[ \t]*(?:->|→|=>|➡|\bthen\b|\bmove\w*\b)", re.I)  # .match(text, pos)
# "+10% profit/gain" 이 TP·Target 라벨 줄에 있으면 목표 수익률 주석이다
# ("TP1: 0.22 (+10% profit)", "Target: 2.40 for a +20% gain") — 결과보고 아님.
_PROFIT_PCT = re.compile(r"^\+\s*\d", re.I)
_TP_LINE_LABEL = re.compile(r"\b(?:tp\s*\d{0,2}|targets?\s*\d{0,2}|take[\s-]*profit|tgt)\b\s*[:=]|목표|익절", re.I)
_RESULT_WORD = re.compile(r"\b(?:hit|reached|achieved|smashed|done|filled|booked|secured)\b|✅", re.I)

# 본문 티커 — "#COREUSDT", "$ZORA", "BINANCE:ETHUSDT", "CRYPTOCAP:IOST".
_TICKER_TAG = re.compile(
    r"(?:(?<![\w#$])[#$]([A-Z][A-Z0-9]{1,14})\b(\.[A-Z]{1,2})?"
    r"|\b[A-Z]{2,12}:([A-Z0-9]{2,20})\b(\.[A-Z]{1,2})?)")
_TICKER_SUFFIX = re.compile(r"(?:USDT|USDC|BUSD|USD|PERP|KRW|BTC)$")
# 시장 지표·지수 등 코인이 아닌 태그는 교차검증에서 빼 준다.
_TICKER_NEUTRAL = {"TOTAL", "TOTAL2", "TOTAL3", "OTHERS", "USDT", "USDC", "DXY", "SPX",
                   "NDX", "GOLD", "XAU", "XAUUSD", "DYOR", "NFA", "TA", "DCA", "ATH",
                   "HTF", "LTF", "FOMC", "CPI", "ETF", "USD", "KRW",
                   # 흔한 해시태그(RV2-E4 "#CRYPTO #TRADING") — 코인 티커가 아니다.
                   "CRYPTO", "CRYPTOCURRENCY", "CRYPTOS", "TRADING", "TRADE", "ALTCOIN",
                   "ALTCOINS", "ALTSEASON", "BLOCKCHAIN", "DEFI", "NFT", "NFTS", "WEB3",
                   "SIGNAL", "SIGNALS", "TECHNICALANALYSIS", "PRICEACTION", "CHARTS",
                   "BULLISH", "BEARISH", "LONG", "SHORT", "SPOT", "FUTURES", "SCALP", "SWING"}


def _body_tickers(text: str) -> set:
    """작성자가 명시한 티커 집합. '#XXX'·'$XXX' 는 본문 전체에서, 'EXCHANGE:XXX' 는
    **첫 두 줄(제목·첫 줄)** 에서만 센다 — TradingView 가 본문의 코인 언급을
    'CRYPTOCAP:BTC' 류 자동 링크로 바꿔 넣기 때문에, 본문 전체를 보면 BTC·ETH 를
    참고로 언급한 XRP 글(867)까지 '불일치'가 된다(재파싱 회귀에서 확인)."""
    out = set()
    text = text or ""
    _head_end = -1
    for _ in range(2):
        _head_end = text.find("\n", _head_end + 1)
        if _head_end < 0:
            _head_end = len(text)
            break
    for m in _TICKER_TAG.finditer(text):
        if m.group(3) and m.start() > _head_end:
            continue
        raw = m.group(1) or m.group(3)
        dot = m.group(2) or m.group(4)
        if not raw or dot:          # "BTC.D"·"USDT.D" 는 지표
            continue
        t = raw.upper()
        base = _TICKER_SUFFIX.sub("", t) or t
        if base.startswith("1000") and len(base) > 4:
            base = base[4:]
        if base in _TICKER_NEUTRAL or t in _TICKER_NEUTRAL or len(base) < 2:
            continue
        out.add(base)
    return out


def _ticker_matches(tag: str, coin: str) -> bool:
    """별칭 관대 매칭(PUMPFUN↔PUMP, 1000PEPE↔PEPE) — 거짓 스킵보다 누락이 싸다."""
    return tag == coin or tag.startswith(coin) or coin.startswith(tag) or tag.endswith(coin)


def nonsetup_reason(text: str, coin_symbol: Optional[str] = None) -> Optional[str]:
    """셋업이 아닌 글이면 사유 문자열, 셋업이면 None (스위치 extract_nonsetup_skip).

    사유: 'not_signal'(작성자가 시그널 아님을 명시), 'result_report'(결과보고),
    'coin_mismatch'(본문 티커가 수집 코인과 하나도 안 맞음 — 665 CRO vs #COREUSDT)."""
    if not text or not _setting("extract_nonsetup_skip"):
        return None
    for m in _NOT_SIGNAL.finditer(text):
        ctx = text[max(0, m.start() - 60): m.end() + 60]
        if not _DISCLAIMER_CTX.search(ctx):
            return "not_signal"
    for m in _RESULT_REPORT.finditer(text):
        if _CONDITIONAL_BEFORE.search(text[max(0, m.start() - 40): m.start()]):
            continue
        if _CONDITIONAL_AFTER.match(text, m.end()):
            continue                                  # "TP1 hit -> move SL" (RV2-E3)
        if _PROFIT_PCT.match(m.group(0)):
            ls = text.rfind("\n", 0, m.start()) + 1
            le = text.find("\n", m.end())
            line = text[ls: len(text) if le < 0 else le]
            if _TP_LINE_LABEL.search(line) and not _RESULT_WORD.search(line):
                continue                              # "TP1: 0.22 (+10% profit)" (RV2-E3)
        return "result_report"
    if coin_symbol:
        coin = coin_symbol.upper()
        tags = _body_tickers(text)
        if tags and not any(_ticker_matches(t, coin) for t in tags):
            # 2026-09-27 리뷰 RV2-E4: 불일치는 '수집 코인 언급이 없고 **다른 코인 태그가
            # 머리(첫 두 줄)에 있을 때**'만. 본문 뒤쪽 참조 티커("Watch $BTC for
            # confirmation")나, 머리에서 수집 코인을 맨 이름으로 적은 글("BINANCE:BTCUSDT
            # correlation\nETH long")은 불일치가 아니다(665 CRO vs #COREUSDT 는 유지).
            head = "\n".join(text.split("\n")[:2])
            head_tags = _body_tickers(head)
            coin_named = bool(re.search(r"(?<![A-Za-z0-9])" + re.escape(coin) + r"(?![A-Za-z0-9])",
                                        head))
            if head_tags and not coin_named:
                return "coin_mismatch"
    return None
