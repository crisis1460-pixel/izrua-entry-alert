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

# 방향 키워드
_LONG_HINTS = re.compile(r"\b(long|buy|롱|매수|매집)\b|롱\s*포지션|buy\s*zone|long\s*setup", re.I)
_SHORT_HINTS = re.compile(r"\b(short|sell|숏|매도)\b|숏\s*포지션|short\s*setup", re.I)

# 라벨 (그룹1 = 라벨종류). 라벨 뒤에 오는 숫자를 그 항목으로 본다.
_ENTRY_LABEL = re.compile(
    # 2026-07-28 수리: entry/enter/buy 에 단어경계 추가 — center/reenter/buyers 내부
    # "enter"/"buy" 가 엔트리 라벨로 잡히는 버그(DB 현재 0건, 소스 확장 시 노출면 증가).
    # (?:\s*\([^)]*\))? — "Buy (zone):" 같은 괄호 주석을 거치고도 is_spec 판정을 통과.
    r"(\bentry\b|\benter\b|\bbuy\b|long\s*entry|진입가?|진입|매수가?|롱\s*진입|buy\s*zone|entry\s*zone)"
    r"(?:\s*\([^)]*\))?\s*(?:price|zone|구간|가격)?\s*[:=]?\s*",
    re.I,
)
_SL_LABEL = re.compile(
    # 2026-07-28 수리: \bsl\b 단어경계 — sloping/previously/slight 내 "sl" 선점 버그
    # (실측 2건: BTC id=120 S→D 등급 폭락 → min_grade 필터에 막혀 알림 미발송).
    # (?:\s*\([^)]*\))? — "Stop Loss (SL): $1,820" 괄호 주석이 라벨·콜론 사이에 있어도
    # m.group(0) 에 콜론이 포함되어 is_spec=True 로 올바르게 분류된다.
    r"(stop\s*loss|stop|\bsl\b|손절가?|손절|스탑|스톱)(?:\s*\([^)]*\))?\s*[:=]?\s*", re.I,
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
    r"(?:\s*\([^)]*\))?\s*[:=]?\s*", re.I,
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
    r"\b(take\s*profit|profit|익절|TP|SL|Target|Entry|타겟|목표|진입|손절)"
    r"\s+[0-9]{1,2}(?=\s*[:=])", re.I
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

# 줄바꿈 나열형 사다리의 단계 수 상한(표시 신뢰 컷). 실측(원문 103건) 분포는
# 1~8 에서 끝나고 8단계 두 건은 육안 확인 결과 진짜 8단 사다리였다. 12 는 그
# 위로 얹은 여유이자, 본문 산문에 'target' 이 흩뿌려져 단계가 부풀 때의 방어선 —
# 넘으면 '틀린 단계 수를 보여주느니 안 보여준다'(이 모듈의 판단 보류 원칙).
_LADDER_MAX_STEPS = 12

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

# 번호 목록 마커 ("1) 1.1129", "2. 0.5340") — 2026-07-27 채널 실사 중 발견.
# 라벨 다음 줄부터 후보를 번호로 나열하는 포맷에서 **마커 숫자가 가격으로** 읽혔다
# (Entry 뒤 '1)' → entry=1.0). 과거 'Target 1:' → tp=1.0 사고와 같은 계열이고,
# _ORDINAL_LABEL 계열은 라벨에 붙은 서수만 처리해 독립 마커는 못 잡았다.
# 결정적 구분자는 **구두점 뒤 공백**이다: 목록 마커는 "1) " / "1. " 처럼 반드시
# 공백이 따르지만, 소수 가격 "5.298" 은 점 뒤가 곧바로 숫자다. 이 조건 덕에
# 진짜 가격을 지울 위험이 없다. 줄 시작에 한정해 문장 중간의 "(1) 참고" 류도 회피.
_LIST_MARKER = re.compile(r"(?m)^[ \t]*\d{1,2}[).][ \t]+(?=[$\d])")

# 타임프레임 파싱 (2026-07-23 적중창 결정 B: 작성자가 밝힌 지평으로 판정 창 결정)
_TIMEFRAME = re.compile(
    r"(?:timeframe|time\s*frame|타임프레임|봉)\s*[:\-]?\s*([0-9]{1,3})\s*(m|min|h|hr|d|w)\b",
    re.I,
)
_TIMEFRAME_WORD = re.compile(r"\b(daily|weekly|일봉|주봉)\b", re.I)
_TF_UNIT_HOURS = {"m": 1 / 60, "min": 1 / 60, "h": 1.0, "hr": 1.0, "d": 24.0, "w": 168.0}


def parse_timeframe_hours(text: str):
    """글에 명시된 차트 타임프레임(시간 단위). 없으면 None.
    예: '🕒 Timeframe: 1H' → 1.0, '4h' → 4.0, '1D' → 24.0"""
    if not text:
        return None
    m = _TIMEFRAME.search(text)
    if m:
        return float(m.group(1)) * _TF_UNIT_HOURS[m.group(2).lower()]
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
    __slots__ = ("ladder_n", "ladder_last")

    def __init__(self, seq=()):
        super().__init__(seq)
        self.ladder_n = 0
        self.ladder_last = None  # 사다리 마지막 값 — 엔트리 범위 복원에 사용


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
    text = _LEVERAGE.sub(" ", text)
    text = _R_MULTIPLE.sub(" ", text)   # "4R return" → 4 가 진입가로 (AVAX 실사고)
    text = _PERCENT.sub(" ", text)
    text = _DATE_FULL.sub(" ", text)
    text = _YEAR_KR.sub(" ", text)
    text = _YEAR_CTX.sub(" ", text)
    text = _LIST_MARKER.sub("", text)   # "1) 1.1129" → 마커만 제거, 가격은 보존
    text = _ORDINAL_LABEL.sub(lambda m: m.group(1), text)  # "TP1:" → "TP:"
    text = _SPACED_ORDINAL_LABEL.sub(lambda m: m.group(1), text)  # "Target 1:" → "Target:"
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
    out, spec_out = [], []
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
        window = text[m.end(): m.end() + 80]
        rng = _RANGE.search(window)
        sng = _SINGLE.search(window)
        lad = None
        if sng:
            # start(1) = 숫자 첫 자리 위치. start(0) 은 _NUM 앞의 `\$?\s*` 때문에
            # 개행 자체를 가리킬 수 있어(값이 다음 줄인 표기) 창이 빈 채로 잘린다.
            _nl = text.find("\n", m.end() + sng.start(1))  # 첫 숫자가 놓인 줄의 끝
            # 줄 끝과 _LADDER_MAX_WINDOW 중 **짧은 쪽**. 상한의 근거는 그 상수 주석
            # 참고(중첩 수량자의 2차 역추적 — 개행 없는 긴 숫자 줄 방어).
            _lad_end = min(len(text) if _nl < 0 else _nl, m.end() + _LADDER_MAX_WINDOW)
            _seg = text[m.end():_lad_end]
            lad = _LADDER.search(_seg)
            # 화살표 나열은 2개짜리도 사다리다(_LADDER_ARROW 주석 참고). 둘 다
            # 잡히면 더 왼쪽에서 시작하는 쪽 — 같은 라벨 뒤의 첫 표기가 주제다.
            _arw = _LADDER_ARROW.search(_seg)
            if _arw and (not lad or _arw.start() < lad.start()):
                lad = _arw
        # lad/sng 의 start() 는 둘 다 m.end() 기준 오프셋이라 창 길이가 달라도 비교 가능.
        if lad and lad.start() <= sng.start():
            first = _to_float(lad.group(1))
            if first:
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
                (spec_out if is_spec else out).append(vals)
                continue
        # 범위와 단일이 둘 다 잡히면, 더 왼쪽에서 시작하는 쪽을 채택(범위 우선 동률).
        if rng and (not sng or rng.start() <= sng.start()):
            lo, hi = _to_float(rng.group(1)), _to_float(rng.group(2))
            if lo and hi:
                (spec_out if is_spec else out).append(sorted([lo, hi]))
                continue
        if sng:
            v = _to_float(sng.group(1))
            if v:
                (spec_out if is_spec else out).append([v])
    # 스펙형이 하나라도 있으면 산문형은 버린다(위 is_spec 주석 참고).
    return _Grabbed(spec_out or out, is_spec=bool(spec_out))


def _sanity(value: float, current_price: Optional[float], max_dev: float) -> bool:
    """현재가 대비 ±max_dev(비율) 안이면 유효. 현재가 모르면 통과(판단보류)."""
    if current_price is None or current_price <= 0 or value is None:
        return True
    return abs(value - current_price) / current_price <= max_dev


def parse_setup(text: str, current_price: Optional[float] = None,
                max_dev: float = 0.60) -> Optional[dict]:
    """
    반환: {direction, entry, entry_low, entry_high, sl, tp, rr} 또는 None(엔트리 없음).
    entry 는 대표값(범위면 중앙), entry_low/high 는 범위 경계(단일이면 동일).
    현재가가 주어지면 엔트리 sanity 실패 시 None.
    """
    if not text:
        return None
    clean = _clean(text)

    # 방향
    is_long = bool(_LONG_HINTS.search(clean))
    is_short = bool(_SHORT_HINTS.search(clean))
    if is_long and not is_short:
        direction = "long"
    elif is_short and not is_long:
        direction = "short"
    else:
        direction = "long"  # 애매하면 long 가정(이 봇은 하향 터치=매수 관점)

    entries = _grab_after(_ENTRY_LABEL, clean)
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

    sls = _grab_after(_SL_LABEL, clean)
    tps = _grab_after(_TP_LABEL, clean)
    sl = sls[0][0] if sls else None
    tp = tps[0][-1] if tps else None  # 첫 타겟(범위면 상단)

    # 방향성 sanity(방어선 2단계, 2026-07-23): 라벨 매칭이 정상이어도 파싱이 미묘하게
    # 틀리면(신규 소스 포맷 등) TP/SL이 방향과 모순될 수 있다 — long인데 tp<=entry
    # 이거나 sl>=entry면 손익비가 마이너스로 나오는 등 명백히 잘못된 값이므로, 값을
    # 버리지 않고 억지로 쓰기보다 "판단 보류"(None)로 되돌린다(이 모듈의 기존 철학과
    # 동일 — 모르는 것과 틀린 것을 구분).
    if direction == "long":
        if tp is not None and entry is not None and tp <= entry:
            tp = None
        if sl is not None and entry is not None and sl >= entry:
            sl = None
    else:
        if tp is not None and entry is not None and tp >= entry:
            tp = None
        if sl is not None and entry is not None and sl <= entry:
            sl = None

    # 크기 sanity(방어선 3단계, 2026-07-23 ALGO/ARB 실전 사고 후 추가): 방향은 맞아도
    # 엔트리 대비 4배(+300%) 초과 목표나 1/4 미만 손절은 스윙 셋업에서 비현실적 —
    # 파싱 오인(서수/무관 숫자)일 확률이 압도적이므로 판단 보류(None)로 되돌린다.
    # (ALGO 사례: entry 0.083에 tp 1.0 = 12배 → 알림에 '+1103%'로 노출됐던 값)
    if entry is not None and entry > 0:
        if tp is not None and not (entry * 0.25 <= tp <= entry * 4):
            tp = None
        if sl is not None and not (entry * 0.25 <= sl <= entry * 4):
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

    # 사다리 단계 수 — 표시 전용(알림의 "1/8단계"). tp 가 sanity 로 폐기됐으면
    # 같이 버린다(값 없는데 단계만 남으면 표시가 거짓말을 한다).
    tp_ladder_count = 0
    if tps and tp is not None:
        _n = getattr(tps[0], "ladder_n", 0)
        if _n > 1:
            tp_ladder_count = _n  # 한 줄 나열형("a - b - c", "a → b")
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
                _lo, _hi = ((entry * 0.25, entry * 4) if (entry and entry > 0)
                            else (float("-inf"), float("inf")))
                _firsts = {v[0] for v in tps if v and _lo <= v[0] <= _hi}
                if 1 < len(_firsts) <= _LADDER_MAX_STEPS:
                    tp_ladder_count = len(_firsts)

    # 전체 TP 목표가 목록 — 가장 가까운 것부터 먼 것 순. B안 필터에서 "마지막 TP가
    # 스윙급(5%+)인가"를 판단하는 데 쓴다. tp 가 sanity 로 폐기됐으면 전체를 비운다
    # (대표값 없는데 목록만 남기면 후처리가 혼선).
    tps_all: list = []
    if tps and tp is not None and entry and entry > 0:
        _lo, _hi = entry * 0.25, entry * 4
        _n = getattr(tps[0], "ladder_n", 0)
        if _n > 1:
            _cands = list(tps[0])          # 한 줄 나열형("a - b - c")
        elif tps.is_spec:
            _cands = [v[0] for v in tps if v]   # 줄바꿈 스펙형: 각 줄 대표 값
        else:
            _cands = [tps[0][-1]] if tps[0] else []  # 단순 TP 단일 값
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
