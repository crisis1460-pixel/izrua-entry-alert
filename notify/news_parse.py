"""
뉴스 영문 원문 구조 추출 (2026-09-27 뉴스 v2 — 기획안 plan_2026-09-27 추천안 A).

왜 영문에서 뜯나: 종전엔 500자를 통째로 번역한 뒤 한글 정규식으로 분석했다.
번역이 수치를 망가뜨리고("$1.69B" → "16억 9천만 개"), 분기선 문구를 매번 다르게
옮겨("황소 케이스"·"강세 사례"·"약세:") 추출이 3/7 실패했다. 원문은 채널 템플릿이
고정이라 규칙 파서가 훨씬 안정적이다. 번역문은 **완결된 문장 단위 보조**로만 쓴다.

출력은 방향 '예측'이 아니라 판단 재료의 구조다(사용자 결정 Q1, 2026-09-27):
  · 사실형 이벤트(12유형) → 🟢 호재 / 🔴 악재 / ⚪ 중립. 방향은 **이벤트 유형 prior**
    (해외 event study: 해킹·언락·SEC 제소·상폐·자금 유출 = 악재, ETF 순유입·고래
    매수·소각·상장 = 호재)로만 정한다.
  · 채널의 차트 해석·방향 콜 → 💬 의견. 색을 입히지 않는다(실측: 논조 기반 방향
    적중 39%, 강세 논조 72h 초과수익 −0.81%).
  · 유형도 금액도 없는 일반론·지난 예측 자찬 → 노이즈(제외).

이 모듈은 순수 함수만 둔다(네트워크·DB 없음) — 테스트와 렌더 양쪽에서 같은 판정을
재사용하기 위해서다.
"""

import re
from typing import Optional

# ── 정제 ─────────────────────────────────────────────────────────────

# 채널 꼬리말: 같은 기호 3번 이상 연속(➖➖➖, ---) 이후는 서명·홍보다.
_FOOTER_RX = re.compile(r"[-=~_➖—–·•*]{3,}")
_READ_MORE_RX = re.compile(r"you can read more here\s*:.*", re.I | re.S)
_URL_RX = re.compile(r"https?://\S+")


def clean(text: str) -> str:
    """꼬리말·링크·장식 줄 제거. 줄 구조는 보존한다(제목 줄 판정에 필요)."""
    raw = (text or "").replace("\r", "")
    raw = _READ_MORE_RX.sub("", raw)
    m = _FOOTER_RX.search(raw)
    if m:
        raw = raw[:m.start()]
    raw = _URL_RX.sub("", raw)
    out = []
    for ln in raw.split("\n"):
        s = ln.strip()
        if s and re.search(r"[0-9A-Za-z]", s):
            out.append(s)
    return "\n".join(out)


def title_of(text: str) -> str:
    t = clean(text)
    return t.split("\n", 1)[0] if t else ""


# ── 금액 (단위 붙은 것만 — 가격 수준 $74.84·$65,000 은 제외) ────────────

_AMT_RX = re.compile(
    r"(?P<sign>[+\-−])?\$\s?(?P<num>\d[\d,]*(?:\.\d+)?)\s?"
    r"(?P<unit>trillion|billion|million|bn|[TBMK])\b"
    r"|(?P<sign2>[+\-−])?\$(?P<big>\d{1,3}(?:,\d{3}){2,})(?!\.\d)(?![\d,])"
    # 셋째 대안($ 없는 "500M in net inflows") — 2026-09-27 리뷰 RV2-N1·N2:
    #  · 단위는 **대문자 B/M 만**(인라인 (?-i:)) — 소문자 "15m" 은 타임프레임이다
    #    ("broke out on the 15m in the morning" 이 $15M 고래 매수로 읽혔다).
    #  · 뒤따름은 달러 흐름 단어로 한정 — "1.2M ETH"·"1.5M of BTC" 는 **코인 수량**이지
    #    달러가 아니다(대표 금액 $1.2M 오표기). 줄을 넘지 않는다([ \t]*).
    r"|(?<![\w$.])(?P<num3>\d+(?:\.\d+)?)(?P<unit3>(?-i:[BM]))\b"
    r"(?=[ \t]*(?:USD\b|ETFs?\b|(?:BTC|ETH)[ \t]+ETFs?\b|worth\b|net\b|"
    r"in[ \t]+(?:net[ \t]+)?(?:inflows?|outflows?)|inflows?|outflows?))"
    r"|(?<![\w$.])(?P<num4>\d+(?:\.\d+)?)\s(?P<unit4>billion|million)\s(?:dollars|USD)",
    re.I)
_UNIT_MUL = {"t": 1e12, "trillion": 1e12, "b": 1e9, "bn": 1e9, "billion": 1e9,
             "m": 1e6, "million": 1e6, "k": 1e3}


def _human_usd(v: float) -> str:
    """$1.69B · $122M · $351.6M · $9M · $4.3M — 원문 정밀도에 가깝게, 끝의 0 제거."""
    for div, suf in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if v >= div:
            x = v / div
            nd = 2 if x < 10 else 1
            s = f"{x:,.{nd}f}".rstrip("0").rstrip(".")
            return f"${s}{suf}"
    return f"${v:,.0f}"


def amounts(text: str, k: int = 4) -> list:
    """[(표기, 값, 부호)] — 단위 금액만. 부호는 +1/-1/0(명시 없음)."""
    out, seen = [], set()
    for m in _AMT_RX.finditer(text or ""):
        sign = m.group("sign") or m.group("sign2") or ""
        if m.group("num"):
            unit = m.group("unit").lower()
            if unit == "k":
                continue                      # $20K 류는 가격·소액이라 핵심 수치 아님
            v = float(m.group("num").replace(",", "")) * _UNIT_MUL[unit]
        elif m.group("big"):
            v = float(m.group("big").replace(",", ""))
        elif m.group("num3"):
            v = float(m.group("num3")) * _UNIT_MUL[m.group("unit3").lower()]
        else:
            v = float(m.group("num4")) * _UNIT_MUL[m.group("unit4").lower()]
        if v < 1e6:
            continue                          # 100만 달러 미만은 뉴스 금액으로 보지 않는다
        s = _human_usd(v)
        sg = -1 if sign in ("-", "−") else (1 if sign == "+" else 0)
        key = (s, sg)
        if key in seen:
            continue
        seen.add(key)
        out.append(((("+" if sg > 0 else "−") if sg else "") + s, v, sg))
        if len(out) >= k:
            break
    return out


# ── 가격 레벨 ────────────────────────────────────────────────────────

# 단위가 붙은 금액("$1.2B"·"$150K"·"$5 million")은 가격 수준이 아니다 — 가격 후보
# 정규식 끝에 붙이는 부정 전방탐색(2026-09-27 리뷰 RV2-N6: "$1.2B in liquidations" 가
# "지지 $1.2" 로, "$150K" 가 "목표 $150(-99.8%)" 로 잘려 쓰였다).
_NO_UNIT = r"(?![\d.,]*\s?(?:[KMBT]|bn|thousand|million|billion|trillion)\b)"
# 가격다운 수치만: $표기 · 소수 · 천단위 · 3자리 이상. "2% inflation" 의 2 같은 건 제외.
_NUM = (r"(?:\$\d[\d,]*(?:\.\d+)?|\d[\d,]*\.\d+|\d{1,3}(?:,\d{3})+|\d{3,})(?![\d%])"
        + r"(?i:" + _NO_UNIT + r")")
_SUPPORT_RX = re.compile(r"support\b[^.\n$\d]{0,24}(" + _NUM + r")|(" + _NUM + r")\s+support\b", re.I)
_RESIST_RX = re.compile(r"resistance\b[^.\n$\d]{0,24}(" + _NUM + r")|(" + _NUM + r")\s+resistance\b", re.I)


def _lv(m) -> str:
    v = (m.group(1) or m.group(2) or "").rstrip(".,")
    return v if v.startswith("$") else v


def levels(text: str) -> dict:
    out = {}
    s = _SUPPORT_RX.search(text or "")
    r = _RESIST_RX.search(text or "")
    if s:
        out["support"] = _lv(s)
    if r:
        out["resistance"] = _lv(r)
    return out


# ── 타임프레임 ───────────────────────────────────────────────────────

def tf_ko(tf: str) -> str:
    t = (tf or "").strip().lower().replace(" ", "")
    if not t:
        return ""
    if t in ("1d", "d", "daily", "day", "1day"):
        return "일봉"
    if t in ("1w", "w", "weekly", "week"):
        return "주봉"
    m = re.fullmatch(r"(\d+)(m|min|mins|minute|minutes|h|hr|hour|hours|d|w)", t)
    if m:
        n, u = m.group(1), m.group(2)[0]
        return {"m": f"{n}분봉", "h": f"{n}시간봉", "d": f"{n}일봉", "w": f"{n}주봉"}[u]
    return ""


# ── 노이즈(영문) ──────────────────────────────────────────────────────
# 지난 예측 자찬·수사 질문·채널 자기홍보. 정보 0 — 기획안 §4-3 노이즈 규칙.
NOISE_RX = re.compile(
    r"perfectly (?:followed|smashed)|followed our (?:prediction|sideways)|we told you|"
    r"as we (?:said|predicted|told)|do you see this|save it,? and keep your eyes|"
    r"\benjoy\.|going for it|how many successful predictions|one of the greatest|"
    r"wolf of trading,|key events this week|^🚨?\s*reminder\b|livestream|"
    r"click on the pinned post|join our vip",
    re.I | re.M)


# ── 사실형 이벤트 12유형 (기획안 §4-3 표 그대로) ────────────────────────
# (key, 트리거, 라벨, 기본 방향, 시간축, 등급)
_TYPES = [
    ("hack", r"\bhack(?:ed|s|er|ers)?\b|\bexploit(?:ed|s)?\b|\bdrain(?:ed|s)?\b|\bstolen\b|\bheist\b|\bbreach", "해킹", -1, "단기", "H"),
    ("etf", r"\bETFs?\b", "ETF", 0, "단기", "H"),
    ("reg", r"\bSEC\b|\bCFTC\b|\bclarity act\b|\bact\b.{0,20}\bsenate|\bsenate\b|\bbill\b|\bregulat\w*|"
            r"\blawsuit\b|\bsu(?:e|es|ed|ing)\b|\bcourt\b|\bruling\b|\binjunction\b|\bexemption\b", "규제", 0, "중장기", "H"),
    ("listing", r"\bdelist\w*|\blist(?:ing|ed|s)\b(?! of)", "상장", 1, "단기", "H"),
    ("unlock", r"\bunlock(?:s|ed|ing)?\b(?!\s+premium)|\bvesting\b|\bcliff\b", "언락", -1, "단기", "H"),
    ("burn", r"\bburn(?:s|ed|ing)?\b|\bbuy-?backs?\b", "소각", 1, "중장기", "M"),
    ("whale", r"\bwhales?\b|\bsmart money\b|\bdeposit(?:s|ed)?\b[^\n]{0,60}\binto\b|"
              r"\bmoved\b[^.\n]{0,40}\$?[A-Z]{2,6}\b[^.\n]{0,20}\bworth\b", "고래", 0, "단기", "M"),
    ("flow", r"\binflows?\b|\boutflows?\b|\bnet-?flows?\b", "자금흐름", 0, "단기", "M"),
    ("partner", r"\bpartner(?:s|ed|ship)?\b|\bteams? up\b|\bcollaborat\w+|\bintegrat(?:es|ed|ion)\b", "제휴", 1, "중장기", "M"),
    ("inst", r"\binstitution\w*|\btreasury\b|\bblackrock\b|\bjpmorgan\b|\btokeniz\w+|\bopen interest\b", "기관", 0, "중장기", "M"),
    ("product", r"\bupgrade\b|\bmainnet\b|\bhard ?fork\b|\blaunch(?:es|ed)?\b|\bamendments?\b", "업그레이드", 1, "중장기", "M"),
    # 매크로는 '강한 단서'(연준 결정·bp·FOMC·CPI 발표)만 여기 둔다. 인플레·금리·긴장 같은
    # 약한 단서는 교양 기사에도 흔해서(실측 "Sam Altman's $1 Trillion Problem") 제목·첫 줄
    # 에서만 본다 — _MACRO_WEAK_RX.
    ("macro", r"\bfed\b[^\n]{0,50}\b(?:hikes?|cuts?|holds?|raises?|lowers?|pauses?|decision)\b|"
              r"\bFOMC\b|\bCPI (?:data|print|report)\b|\bbasis points?\b|\b\d+\s?bps\b|"
              r"\brate (?:hike|cut)s?\b", "매크로", 0, "단기", "M"),
]
TYPES = [(k, re.compile(rx, re.I), lab, pol, hz, tier) for k, rx, lab, pol, hz, tier in _TYPES]
_MACRO_WEAK_RX = re.compile(r"\binflation\b|\byields?\b|\brisk-off\b|\btariffs?\b|\btensions?\b|"
                            r"\bgeopolitic\w*|\bescalat\w*", re.I)
H_TYPES = frozenset(k for k, *_r, tier in _TYPES if tier == "H")

# 유형별 극성 보정 단어 — 유형 문맥 안에서만 센다(전역 감성 사전 아님).
_POL_WORDS = {
    # ETF 승인·거절·연기는 자금 흐름이 아니라 별도 결정 이벤트다(_ETF_DECISION, RV2-N4) —
    # 종전엔 approv 가 긍정 단어라 "SEC approves spot Solana ETF" 가 "ETF 순유입"이 됐다.
    "etf": (r"inflow|net-?flow|turn(?:ed|ing)? (?:back to )?(?:buying|positive)|buying",
            r"outflow|net sold|selling|redemption"),
    "reg": (r"approv|opens? (?:the )?door|exemption|pass(?:es|ed)\b|green light|clears?\b|dismiss|roadmap|framework",
            r"fail|reject|odds (?:crash|drop|fall)|crash|lawsuit|sues?\b|sued|charges?\b|\bban\b|delay|crackdown"),
    "macro": (r"\bcuts?\b|lowers?\b|eas(?:e|es|ing)\b|cooling|ceasefire|de-?escalat",
              r"hikes?\b|raises?\b|risk-off|tensions?|escalat|war\b|tariff|rising yields|higher yields|inflation (?:concerns|fears)|oil"),
    "whale": (r"\bbought\b|\bbuy(?:s|ing)?\b|accumulat",
              r"deposit(?:s|ed)?\b[^\n]{0,60}\binto\b[^\n]{0,20}(?:coinbase|binance|exchange|kraken|okx|bybit|prime)|\bsold\b|dump"),
    "flow": (r"inflow", r"outflow"),
    "inst": (r"buying|bought|adds?\b|adding|bets? on|betting|demand|adoption|raised|expands?|teams? up|partner",
             r"outflow|net sold|selling|volatility test|liquidation"),
    "listing": (r"\blist(?:ing|ed|s)\b", r"delist"),
}
_POL_RX = {k: (re.compile(p, re.I), re.compile(n, re.I)) for k, (p, n) in _POL_WORDS.items()}
# 오래된 사건을 다루는 회고 기사("eighteen months after ...")는 신선한 이벤트가 아니다.
_STALE_RX = re.compile(r"\b(?:months|years)\s+(?:after|ago|later)\b", re.I)
_EXCHANGE_RX = re.compile(r"\b(?:binance|coinbase|kraken|bybit|bitget|okx|upbit|bithumb|"
                          r"robinhood|gemini|kucoin|htx|gate\.io|mexc|exchange)\b", re.I)

# ── 확인되지 않은 서술(가정·예상·루머·질문·의견) — 2026-09-27 리뷰 RV2-N3·N5·N11 ──
# 사용자 규칙: 🟢/🔴/⚪ 칩은 **확인된 사실 뉴스에만**. "Bitcoin could drop to $50K if ETF
# inflows stall, analyst warns" 가 🟢 ETF 순유입으로, "Fed expected to hike rates?" 가
# "연준이 기준금리를 인상했습니다"로 나갔다. 이벤트 트리거가 걸린 **그 구절**(제목의
# 대시·물음표·콜론으로 나뉜 조각, 본문은 문장)에 아래 표지가 있으면 사실로 올리지 않는다.
# 구절 단위로 보는 이유: "Solana Treasury Giant Teams Up With Kraken — Is Institutional
# Demand Entering A New Phase?" 처럼 사실 뒤에 수사 질문이 붙는 제목이 흔하다.
_SPEC_RX = re.compile(
    r"\?|\b(?:could|might|would|if|whether|predicts?|predicted|predictions?|forecasts?|"
    r"expect(?:s|ed|ing|ations?)?|likely|unlikely|odds|rumou?r(?:s|ed)?|reportedly|"
    r"alleged(?:ly)?|unconfirmed|speculat\w*|possible|possibly|potential(?:ly)?|"
    r"warns?|warning|analysts?|price[sd]?\s+in|pricing\s+in|poised|mulls?|considers?|"
    r"weighs?|(?<!in )may(?!\s+\d))\b", re.I)
_SPEC_EXEMPT_RX = re.compile(r"\b(?:as|than)\s+expected\b", re.I)
_SEG_SEP_RX = re.compile(r"(?<=[?!.;:])\s+|\s+[—–|]\s+|\s+-\s+|\n")
# 해킹 트리거가 걸려도 부인·루머·피싱·"피해 없음"이면 해킹 사실이 아니다(RV2-N11).
_HACK_NEG_RX = re.compile(
    r"\bdenie[sd]\b|\bdeny(?:ing)?\b|\brumou?rs?\b|\bno funds\b|\bfunds? (?:are|were|remain) safe\b|"
    # "compromised" 는 넣지 않는다 — "Private keys were not compromised"(실측 Bitget $351.6M
    # 유출 기사)처럼 실제 해킹 기사의 경위 설명에 흔하다.
    r"\bnot (?:been )?(?:hacked|affected|exploited|stolen)\b|\bphishing\b|\bscam\w*|"
    r"\bfalse\b|\bfake\b|\bimpersonat\w*", re.I)
# ETF 승인·거절·연기(RV2-N4). 자금 흐름 단어가 함께 있으면 흐름 기사로 둔다.
_ETF_FLOW_WORD_RX = re.compile(r"\binflows?\b|\boutflows?\b|\bnet-?flows?\b|\bflows?\b", re.I)
_ETF_DECISION = [
    ("approve", 1, re.compile(r"\bapprov(?:e|es|ed|al|ing)\b|\bgreen[- ]?light", re.I)),
    ("reject", -1, re.compile(r"\breject(?:s|ed|ion)?\b|\bdenie[sd]\b|\bdisapprov\w*", re.I)),
    ("delay", -1, re.compile(r"\bdelay(?:s|ed)?\b|\bpostpone\w*|\bextends? (?:the )?(?:review|deadline)", re.I)),
]


def _segment_at(text: str, pos: int) -> str:
    """text 안 pos 가 속한 구절(대시·물음표·콜론·문장 경계로 나눈 조각)."""
    start = 0
    for m in _SEG_SEP_RX.finditer(text or ""):
        if m.start() >= pos:
            return text[start:m.start()] if m.start() > start else text[start:m.end()]
        start = m.end()
    return (text or "")[start:]


def is_speculative(text: str, pos: int) -> bool:
    """pos(이벤트 트리거 위치)가 걸린 구절이 가정·예상·루머·질문·의견 서술인가."""
    seg = _SPEC_EXEMPT_RX.sub(" ", _segment_at(text, pos))
    return bool(_SPEC_RX.search(seg))


def _etf_decision(lead: str) -> tuple:
    """(결정 키, 극성) — 승인·거절·연기 기사면. 흐름 단어가 있으면 ("", 0)."""
    if _ETF_FLOW_WORD_RX.search(lead or ""):
        return "", 0
    for key, pol, rx in _ETF_DECISION:
        if rx.search(lead or ""):
            return key, pol
    return "", 0

# ── 차트 시나리오(BitcoinBullets 템플릿) ───────────────────────────────
_BULL_LINE_RX = re.compile(r"bull(?:ish)?\s*(?:case|scenario)\s*[:：](?P<t>[^\n]*)", re.I)
_BEAR_LINE_RX = re.compile(r"bear(?:ish)?\s*(?:case|scenario)\s*[:：](?P<t>[^\n]*)", re.I)
_PRICE_TF_RX = re.compile(r"(?P<p>\d[\d,]*(?:\.\d+)?)\s+on\s+the\s+(?P<tf>\d*\s?[a-z]+)\b", re.I)
_LEVEL_NUM_RX = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?(?![\w])")
_HIGHS_RX = re.compile(r"from the (\d[\d,]*(?:\.\d+)?) highs?", re.I)
ACTIONS = [
    (re.compile(r"fresh highs|new highs|all-time high", re.I), "신고점 갱신"),
    (re.compile(r"ripped|explod|tearing through|surg|rocket", re.I), "급등"),
    (re.compile(r"break(?:ing|s)? above|clearing|broke above|breaking out|breakout", re.I), "돌파"),
    (re.compile(r"pressing (?:right )?(?:back )?(?:into|toward)|pushing (?:into|toward)|testing [^.]*resistance|near[^.]*resistance", re.I), "저항 시험"),
    (re.compile(r"reject", re.I), "저항에 막힘"),
    (re.compile(r"landing (?:right )?on|test(?:ing)? [^.]*(?:support|demand|trendline)|holding (?:well )?above|bounc", re.I), "지지 시험"),
    (re.compile(r"pulling back|cooling off|easing back|sliding|fading|slipping|dropp|retrac", re.I), "되돌림"),
    (re.compile(r"compress|range|consolidat|sideways", re.I), "박스권"),
]


def _nums_in(s: str) -> list:
    out = []
    for m in _LEVEL_NUM_RX.finditer(s or ""):
        v = m.group(0).rstrip(".,")
        # 날짜·기간 숫자(mid July 의 July 앞 숫자 등)는 걸러낸다 — 2자리 이하 정수는
        # 가격일 수 있으므로 "day/week/month" 가 바로 뒤에 올 때만 제외.
        tail = (s or "")[m.end():m.end() + 8].lower()
        if re.match(r"\s*(?:day|week|month|year|h\b|hr|%)", tail):
            continue
        out.append(v)
    return out


def _fnum(v: str) -> Optional[float]:
    try:
        return float(str(v).replace(",", "").lstrip("$"))
    except (TypeError, ValueError):
        return None


def _case(line: str) -> dict:
    """'hold above 82,000 and resume the push toward 86,000' → 트리거·목표·동사."""
    nums = _nums_in(line)
    if not nums:
        return {}
    low = line.lower()
    if re.search(r"hold(?:s|ing)? above|stay above|defend|keep", low):
        verb = "hold"
    elif re.search(r"clear|break|reclaim|push(?:es)? (?:above|through)|flip", low):
        verb = "break"
    elif re.search(r"\blose|lost|break(?:s)? below|fade|slip|drop|fall|below", low):
        verb = "lose"
    else:
        verb = ""
    return {"trigger": nums[0], "target": nums[-1] if len(nums) > 1 and nums[-1] != nums[0] else "",
            "verb": verb}


def parse_scenario(text: str) -> Optional[dict]:
    b = _BULL_LINE_RX.search(text or "")
    r = _BEAR_LINE_RX.search(text or "")
    if not b or not r:
        return None
    bull, bear = _case(b.group("t")), _case(r.group("t"))
    if not bull and not bear:
        return None
    lines = clean(text).split("\n")
    body = lines[1:] if lines and lines[0].lstrip().startswith("#") and len(lines) > 1 else lines
    first = body[0] if body else ""
    m = _PRICE_TF_RX.search(first)
    price = m.group("p") if m else ""
    tf = tf_ko(m.group("tf")) if m else ""
    act = next((lab for rx, lab in ACTIONS if rx.search(first)), "")
    hi = _HIGHS_RX.search(first)
    return {"price": price, "tf": tf, "action": act, "high": hi.group(1) if hi else "",
            "bull": bull, "bear": bear}


# ── 방향 콜(wolfoftrading "Update: 1h" 등) ─────────────────────────────
_CALL_HEAD_RX = re.compile(r"update\s*:|\bexpect(?:ing|ed)?\b|\bpattern\b|head and shoulders|"
                           r"cup and handle|trendline|\bpennant\b|\bwedge\b|\bfib\b", re.I)
_CALL_TF_RX = re.compile(r"update\s*:\s*(\d+\s?(?:mins?|m|h|d|w)\b|daily|weekly|1d|1w)", re.I)
_BULL_CUE = re.compile(r"bullish|to the upside|go(?:ing)? up|\brise\b|pump|break-?out from above|"
                       r"break from above|reversal to the upside|higher\b|\blongs?\b|upside|x2\b|"
                       r"great position|rally\b(?! dump)", re.I)
_BEAR_CUE = re.compile(r"bearish|downside|go(?:ing)? down|downwards|\bdump\b|\blower\b|\bshort(?:ed)?\b|"
                       r"\bdrop\b|break(?:ing)? from below|break below|correction|go further downwards", re.I)
_WAIT_CUE = re.compile(r"sideways|balance|no need to rush|ranging|range-bound|between two scenarios", re.I)
PATTERNS = [
    (re.compile(r"inverse head and shoulders", re.I), "역헤드앤숄더"),
    (re.compile(r"head and shoulders", re.I), "헤드앤숄더"),
    (re.compile(r"cup and handle", re.I), "컵앤핸들"),
    (re.compile(r"adam\s*(?:&|and)\s*eve", re.I), "아담&이브"),
    (re.compile(r"falling wedge", re.I), "하락 쐐기"),
    (re.compile(r"rising wedge", re.I), "상승 쐐기"),
    (re.compile(r"pennant", re.I), "페넌트"),
    (re.compile(r"double bottom", re.I), "이중 바닥"),
    (re.compile(r"double top", re.I), "이중 천장"),
    (re.compile(r"\bfib\w*", re.I), "피보나치 되돌림"),
    # 돌파·이탈은 **방향이 명시된 표현**일 때만 — 단어 resistance 하나로 "저항 돌파"를
    # 붙이면 약세 글(BNB "our resistance is $775")에 거꾸로 된 근거가 붙는다.
    (re.compile(r"(?:break(?:ing|s)?|broken|broke)(?:-out)? (?:from )?above|"
                r"above (?:of )?(?:the )?(?:major )?(?:short[- ]term )?resistance", re.I), "저항 돌파"),
    (re.compile(r"(?:break(?:ing|s)?|broken|broke)(?:-down)? (?:from )?below|"
                r"lose (?:this|the) (?:level|support)", re.I), "하단 이탈"),
    (re.compile(r"trend ?line|uptrend|downtrend", re.I), "추세선"),
    (re.compile(r"(?:hold|bounc|retest)\w*[^.\n]{0,30}support|support[^.\n]{0,20}holds?", re.I), "지지 확인"),
    (re.compile(r"balance|range|rectangle|sideways", re.I), "박스권"),
]
_TARGET_RX = [
    (re.compile(r"(\$?[\d.,]+)\s*-\s*(\$?[\d.,]+)\s+is the (\w+) target", re.I), "range"),
    (re.compile(r"first target should be around (\d+%)", re.I), "pct"),
    (re.compile(r"\bx2\b", re.I), "x2"),
    (re.compile(r"(?:reach|retest|toward|to)\s+(" + r"\$\d[\d,]*(?:\.\d+)?(?:\s?K\b)?" + _NO_UNIT + r")",
                re.I), "level"),
]

# 제목·목표의 "$가격" 토큰 — 단위 금액($1.2B)은 제외, "$150K" 는 $150,000 으로 정규화
# (2026-09-27 리뷰 RV2-N6).
_DOLLAR_LEVEL_RX = re.compile(r"\$\d[\d,]*(?:\.\d+)?(?:\s?K\b)?" + _NO_UNIT, re.I)


def _norm_level(tok: str) -> str:
    """"$150K" → "$150,000", "$1.5k" → "$1,500". 그 외는 그대로."""
    t = (tok or "").strip()
    m = re.fullmatch(r"\$(\d[\d,]*(?:\.\d+)?)\s?[Kk]", t)
    if not m:
        return t
    v = float(m.group(1).replace(",", "")) * 1000
    return f"${v:,.0f}" if v == int(v) else f"${v:,.2f}"


def _dollar_level(text: str) -> str:
    m = _DOLLAR_LEVEL_RX.search(text or "")
    return _norm_level(m.group(0)) if m else ""


_ART_BULL_RX = re.compile(r"rebound|rally|bulls?\b|bullish|outperform|heading toward|higher|climb|"
                          r"surge|growth|upside|charging", re.I)
_ART_BEAR_RX = re.compile(r"\bdrops?\b|slide|\bfalls?\b|bears?\b|bearish|declin|downside|sell-?off|"
                          r"plunge|slump", re.I)


def parse_call(text: str) -> dict:
    body = clean(text)
    head = body.split("\n", 1)[0]
    tfm = _CALL_TF_RX.search(body)
    tf = tf_ko(tfm.group(1)) if tfm else ""
    nb, nr = len(_BULL_CUE.findall(body)), len(_BEAR_CUE.findall(body))
    if nb > nr:
        stance = "강세"
    elif nr > nb:
        stance = "약세"
    else:
        stance = "관망"
    if stance != "관망" and _WAIT_CUE.search(body) and abs(nb - nr) <= 1:
        stance = "관망"
    pat = next((lab for rx, lab in PATTERNS if rx.search(body)), "")
    target = ""
    for rx, kind in _TARGET_RX:
        m = rx.search(body)
        if not m:
            continue
        if kind == "range":
            target = f"{m.group(1)}~{m.group(2).lstrip('$')}"
        elif kind == "pct":
            target = f"+{m.group(1)}"
        elif kind == "x2":
            target = "2배"
        else:
            target = _norm_level(m.group(1))
        break
    cond = bool(re.search(r"\bif\b|\bonly if\b|once we|after a break", body, re.I))
    return {"tf": tf, "stance": stance, "pattern": pat, "target": target,
            "levels": levels(body), "conditional": cond, "head": head}


# ── 분류 본체 ─────────────────────────────────────────────────────────


def _polarity(key: str, base: int, text: str, amts: list) -> int:
    if key in ("hack", "unlock", "burn", "partner", "product"):
        return base
    rxs = _POL_RX.get(key)
    if not rxs:
        return base
    pos_rx, neg_rx = rxs
    head = clean(text).split("\n")
    # 머리(제목+첫 두 문장)에 가중 — 기사 뒷부분의 반론·일반론이 방향을 뒤집지 않게.
    lead = " ".join(head[:3])
    p, n = len(pos_rx.findall(lead)), len(neg_rx.findall(lead))
    if key == "listing":
        return -1 if n else (1 if p else 0)
    if p == n:
        signed = [a for a in amts if a[2]]
        if signed:
            return signed[0][2]
        return base
    return 1 if p > n else -1


def classify_event(text: str) -> Optional[dict]:
    """사실형 이벤트 판정. 유형이 없으면 None."""
    body = clean(text)
    if not body:
        return None
    lead = "\n".join(body.split("\n")[:2])
    amts = amounts(body)
    # 제목·첫 문장에 걸린 유형을 우선 — 본문 뒤쪽 한 단어로 유형이 정해지지 않게.
    hit = None
    spec = False
    for scope in (lead, body):
        for k, rx, lab, pol, hz, tier in TYPES:
            m = rx.search(scope)
            if not m:
                continue
            # 부인·루머·피싱 기사는 해킹 사실이 아니다(RV2-N11) — 다른 유형을 계속 찾는다.
            if k == "hack" and _HACK_NEG_RX.search(lead):
                continue
            hit = (k, lab, pol, hz, tier)
            spec = is_speculative(scope, m.start())
            break
        if hit:
            break
    if not hit:
        mw = _MACRO_WEAK_RX.search(lead)
        if mw:
            hit = ("macro", "매크로", 0, "단기", "M")
            spec = is_speculative(lead, mw.start())
    if not hit:
        return None
    k, lab, pol, hz, tier = hit
    pol = _polarity(k, pol, body, amts)
    decision = ""
    if k == "etf":
        decision, dpol = _etf_decision(lead)
        if decision:
            pol = dpol
    stale = bool(_STALE_RX.search(lead))
    if stale:
        tier, pol = "M", 0
    return {"type": k, "label": lab, "pol": pol, "horizon": hz, "tier": tier,
            "amounts": amts, "levels": levels(body), "stale": stale,
            "spec": spec, "decision": decision}


def parse(text: str) -> dict:
    """영문 원문 1건 → 구조.

    kind: 'fact' | 'scenario' | 'call' | 'noise'
    판정 순서: 시나리오 템플릿 → 노이즈 → 사실형 이벤트(제목에 유형) → 방향 콜 →
    기사형 의견(제목에 가격 수준) → 사실형(본문에 유형·금액) → 노이즈.
    사실형을 콜보다 앞에 두는 건 제목 기준일 때만이다 — "$ETHUSDT Update: 1D /
    ... breaking resistance" 같은 콜 글이 본문 한 단어로 사실형이 되지 않게."""
    body = clean(text)
    title = body.split("\n", 1)[0] if body else ""
    if not body:
        return {"kind": "noise", "why": "빈 본문", "title": title}
    sc = parse_scenario(text)
    if sc:
        return {"kind": "scenario", "title": title, **sc}
    if NOISE_RX.search(body):
        return {"kind": "noise", "why": "지난 예측 자찬·홍보", "title": title}
    lead2 = "\n".join(body.split("\n")[:2])
    ev = classify_event(lead2)
    # 가정·예상·루머·질문형 구절에서 걸린 이벤트는 사실형으로 올리지 않는다(RV2-N3·N5).
    # 콜·기사형 의견으로 풀리면 그쪽(💬, 칩 없음), 아니면 맨 끝에서 💬 전망 기사로 싣는다.
    spec_ev = ev if (ev and ev.get("spec")) else None
    if spec_ev:
        ev = None
    # 채널 콜 템플릿("$BTCUSDT Update: 15m")의 본문 한 줄("Whales bought the dip")은 채널의
    # 차트 해설이지 확인된 사건이 아니다 — 금액도 H 유형도 없으면 사실형으로 올리지 않는다
    # (RV2-N1: 이 글이 "🟢 호재 고래 매수 $15M" 으로 나갔다).
    if ev and not ev["amounts"] and ev["type"] not in H_TYPES and re.search(r"update\s*:", title, re.I):
        ev = None
    if ev and (ev["amounts"] or ev["type"] in H_TYPES or ev["type"] in ("macro", "whale", "flow")):
        ev_full = classify_event(body) or ev
        # 제목에서 정한 유형을 유지하되 금액·레벨은 본문 전체에서 모은다.
        ev.update(amounts=ev_full["amounts"] or ev["amounts"],
                  levels=ev_full["levels"] or ev["levels"])
        if ev["pol"] == 0 and ev_full["type"] == ev["type"]:
            ev["pol"] = ev_full["pol"]
        return {"kind": "fact", "title": title, **ev}
    if _CALL_HEAD_RX.search(title) or re.search(r"\bexpect(?:ing)?\b", body, re.I):
        c = parse_call(text)
        if c["stance"] != "관망" or c["pattern"] or c["target"] or c["levels"]:
            return {"kind": "call", "title": title, "source": "채널", **c}
    ev = classify_event(body)
    if ev and ev.get("spec"):
        spec_ev = spec_ev or ev
        ev = None
    if ev and ev["amounts"]:
        return {"kind": "fact", "title": title, **ev}
    if spec_ev:
        # 사실형 유형은 걸렸지만 가정·예상·루머·질문 구절 — 칩 없는 💬 전망 기사로만
        # 싣는다. 원문에 없는 사실을 만들지 않도록 수치·레벨·방향은 붙이지 않고 제목만
        # 인용한다(RV2-N3·N5). 아래 기사형 분기("가격 흐름을 다뤘습니다"·"$X 부근을 향해
        # 움직이고 있다고 봤습니다")보다 앞에 둔다 — 질문형 제목엔 그 서술도 단정이다.
        c = parse_call(text)
        c.update(stance="관망", pattern="", target="", levels={}, title_level="",
                 title_en=title, conditional=False)
        return {"kind": "call", "title": title, "source": "기사", "speculative": True,
                "event_type": spec_ev.get("type"), **c}
    # 기사형 의견 — "Dogecoin (DOGE) Faces Strong Resistance at $0.1000" 처럼 제목에
    # 가격 수준이 있는 분석 기사. 방향 단정 없이 💬 로 싣는다(Q3).
    # 가격 수준은 단위 금액이 아닌 $토큰만(RV2-N6 — "$1.2B in liquidations" 는 금액).
    title_lv = _dollar_level(title)
    if title_lv and re.search(
            r"resistance|support|toward|heading|ceiling|mark|level|holds?|rebound|rally|target",
            title, re.I):
        c = parse_call(text)
        # 기사 본문은 수사(修辭)가 많아 단서 개수로 스탠스를 세면 흔들린다 — 제목만 본다.
        if _ART_BULL_RX.search(title) and not _ART_BEAR_RX.search(title):
            c["stance"] = "강세"
        elif _ART_BEAR_RX.search(title) and not _ART_BULL_RX.search(title):
            c["stance"] = "약세"
        else:
            c["stance"] = "관망"
        if c["pattern"] in ("저항 돌파", "지지 확인"):
            c["pattern"] = ""
        lv = title_lv
        c["title_level"] = lv
        if not c["levels"]:
            if re.search(r"support", title, re.I):
                c["levels"] = {"support": lv}
            elif re.search(r"resistance|ceiling", title, re.I):
                c["levels"] = {"resistance": lv}
            else:
                c["target"] = c["target"] or lv
        return {"kind": "call", "title": title, "source": "기사", **c}
    if ev and ev["type"] in H_TYPES:
        return {"kind": "fact", "title": title, **ev}
    # 제목에 방향 단서가 있는 코인 분석 기사("Sui Moderate Bearish Correction May End
    # Shortly") — 가격 수준은 없지만 채널이 코인 하나를 두고 쓴 분석이라 💬 로 싣는다.
    # 방향 단서도 없는 일반론("The Reason Why the Four Seasons of Crypto…")은 노이즈.
    if _ART_TITLE_CUE_RX.search(title):
        c = parse_call(text)
        # 방향은 매기지 않는다 — "Bearish Correction May End Shortly" 처럼 단서 단어와
        # 실제 논지가 반대인 제목이 흔하다(단어 세기로는 뒤집힌다). 제목을 그대로 보여준다.
        c["stance"] = "관망"
        c["pattern"] = ""
        c["title_level"] = ""
        c["title_en"] = title
        return {"kind": "call", "title": title, "source": "기사", **c}
    # 가격 수준이 적힌 시황 코멘트("AAVE holds near 126.19 after pulling back from the
    # 140.00 high") — 사실 이벤트는 아니지만 레벨 정보가 있어 💬 의견으로 싣는다(Q3).
    # 수치 없는 일반론("Four Seasons of Crypto")은 여기 걸리지 않는다.
    if _PRICE_LIKE_RX.search(body) and _CHART_WORD_RX.search(body):
        c = parse_call(text)
        return {"kind": "call", "title": title, "source": "채널", **c}
    return {"kind": "noise", "why": "유형·금액 없는 일반론", "title": title}


_ART_TITLE_CUE_RX = re.compile(r"bull|bear|rally|rebound|correction|breakout|surge|plunge|recover|"
                               r"momentum|uptrend|downtrend|outperform|sell-?off", re.I)
_PRICE_LIKE_RX = re.compile(r"\$\d|\d+\.\d{2,}|\d{1,3}(?:,\d{3})+")
_CHART_WORD_RX = re.compile(r"support|resistance|\bhighs?\b|\blows?\b|trend|pull(?:ing|s)? back|"
                            r"breakout|range|demand zone|rally|holds? (?:near|above)", re.I)


def has_info_number(text: str) -> bool:
    """영문 원문 기준 수치 판정(S0-②). 번역문의 "16억 9천만 개" 같은 변형과 무관하게
    원문의 금액·가격·퍼센트를 본다. news_brief._NUMBER_RX 와 같은 계열의 기준."""
    t = text or ""
    return bool(amounts(t) or re.search(
        r"\$\s?\d|\d+\.\d+|\d[\d,]{3,}|\d+(?:\.\d+)?\s*(?:%|percent)|\d+(?:\.\d+)?[BMK]\b", t))


# ── 🌐 시장 뉴스 판정 (S3, 사용자 결정 Q2) ───────────────────────────────
MARKET_TYPES = frozenset({"etf", "reg", "macro", "hack", "flow"})


def is_market_news(parsed: dict) -> bool:
    """티커 없는 시장 전체 뉴스인가 — 연준·ETF 자금·규제·거래소 해킹.

    일반 DeFi 프로토콜 해킹(상장 코인 아님)은 시장 뉴스로 올리지 않는다 —
    거래소 해킹만 시장 전체 위험이다. 회고 기사(stale)도 제외."""
    if parsed.get("kind") != "fact" or parsed.get("stale"):
        return False
    t = parsed.get("type")
    if t not in MARKET_TYPES:
        return False
    if t == "hack" and not _EXCHANGE_RX.search(parsed.get("title") or ""):
        return False
    return bool(parsed.get("amounts") or parsed.get("pol"))


# ── S3 코인 이름 매칭 ─────────────────────────────────────────────────
# 업비트 KRW 상장 코인 이름 사전. 유니버스(coingecko name)에 없는 흔한 표기를 보탠다.
_EXTRA_ALIASES = {
    "bitcoin": "BTC", "ethereum": "ETH", "ripple": "XRP", "solana": "SOL", "cardano": "ADA",
    "dogecoin": "DOGE", "chainlink": "LINK", "polkadot": "DOT", "cronos": "CRO",
    "litecoin": "LTC", "tron": "TRX", "hedera": "HBAR", "uniswap": "UNI", "arbitrum": "ARB",
    "polygon": "POL", "aptos": "APT", "sui": "SUI", "toncoin": "TON", "monero": "XMR",
    "zcash": "ZEC", "hyperliquid": "HYPE", "ethereum classic": "ETC", "bitcoin cash": "BCH",
}
# 이름이 흔한 영단어·인명과 겹치는 코인 — 이름만으로는 매칭하지 않는다(티커 경로 전용).
NAME_STOPLIST = frozenset({
    "compound", "derive", "plasma", "seeker", "stacks", "immutable", "mantle", "canton",
    "sentient", "jupiter", "stellar", "the graph", "graph", "golem", "optimism", "just",
    "near", "avalanche", "maple", "sun", "sun token", "official trump", "trump",
    "global dollar", "superverse", "super", "edgex", "monad", "cosmos", "venice",
    "vaulta", "aethir", "conflux", "decentraland", "basic attention", "artificial inu",
    "falcon finance", "falcon", "pump.fun", "doublezero", "kamino", "jito", "internet computer",
    "tether gold", "world liberty financial", "theta network", "theta", "ecash",
})
_SUFFIX_RX = re.compile(r"\s+(?:protocol|network|hub|finance|token|coin|chain|\(ex-[^)]*\))$", re.I)
# "Solana-based", "Ethereum-compatible" — 그 체인 위의 다른 프로젝트 얘기다.
_NAME_TAIL_BLOCK_RX = re.compile(r"^\s?[-‑](?:based|powered|native|built|compatible|focused|linked|style)\b", re.I)


def build_name_index(universe) -> dict:
    """{소문자 별칭: 심볼}. universe: [{"symbol","name"}, ...] 또는 심볼 목록."""
    idx = {}
    syms = set()
    for u in universe or ():
        if isinstance(u, dict):
            sym = str(u.get("symbol") or "").upper()
            name = str(u.get("name") or "").strip()
        else:
            sym, name = str(u).upper(), ""
        if not sym:
            continue
        syms.add(sym)
        # 원래 이름이 불용어면 접미사 뗀 별칭도 등록하지 않는다(RV2-N12: "falcon finance"
        # 는 막혔는데 "falcon" 이 새어 "Falcon Heavy launch" → FF 로 매칭됐다).
        if name and name.strip().lower() in NAME_STOPLIST:
            continue
        if name and name.upper() != sym:
            for alias in {name, _SUFFIX_RX.sub("", name)}:
                a = alias.strip().lower()
                if len(a) >= 4 and a not in NAME_STOPLIST:
                    idx.setdefault(a, sym)
    for a, sym in _EXTRA_ALIASES.items():
        if sym in syms and a not in NAME_STOPLIST:
            idx.setdefault(a, sym)
    return idx


def match_coin_name(text: str, name_index: dict) -> Optional[str]:
    """티커 없이 **이름만** 있는 글의 코인. 없거나 둘 이상이면 None.

    오탐 방지:
      · 대문자로 시작하는 표기만(“avalanche of” 같은 일반명사 제외)
      · "-based/-powered/-native" 가 붙으면 그 체인 위 다른 프로젝트라 제외
      · 제목에 나오거나 본문에 2번 이상 나와야 주제로 본다(지나가는 언급 제외)
      · 긴 이름 우선("Bitcoin Cash" 가 "Bitcoin" 으로 새지 않게), 서로 다른 코인이
        제목에 둘 이상이면 모호로 버린다."""
    if not text or not name_index:
        return None
    body = clean(text)
    title = body.split("\n", 1)[0] if body else ""
    hits = {}          # sym -> (in_title, count)
    taken = []         # 이미 긴 이름이 차지한 구간
    for alias in sorted(name_index, key=len, reverse=True):
        rx = re.compile(r"(?<![\w$])" + re.escape(alias) + r"(?![\w])", re.I)
        for m in rx.finditer(body):
            if not m.group(0)[0].isupper():
                continue
            if _NAME_TAIL_BLOCK_RX.match(body[m.end():m.end() + 14]):
                continue
            if any(a <= m.start() < b for a, b in taken):
                continue
            taken.append((m.start(), m.end()))
            sym = name_index[alias]
            in_title = m.start() < len(title)
            t0, c0 = hits.get(sym, (False, 0))
            hits[sym] = (t0 or in_title, c0 + 1)
    cands = [s for s, (t, c) in hits.items() if t or c >= 2]
    titled = [s for s in cands if hits[s][0]]
    if len(titled) == 1:
        return titled[0]
    if len(titled) > 1:
        return None
    return cands[0] if len(cands) == 1 else None


# ── 한국어 조립 도우미 ────────────────────────────────────────────────

_KO_NAMES = {
    "BTC": "비트코인", "ETH": "이더리움", "XRP": "XRP", "SOL": "솔라나", "ADA": "에이다",
    "DOGE": "도지코인", "LINK": "체인링크", "DOT": "폴카닷", "CRO": "크로노스",
    "AVAX": "아발란체", "TRX": "트론", "XLM": "스텔라루멘", "UNI": "유니스왑",
    "ARB": "아비트럼", "NEAR": "니어", "SUI": "수이", "BCH": "비트코인캐시", "ETC": "이더리움클래식",
}
_LATIN_BATCHIM = set("LMNR")          # 엘·엠·엔·알 — 받침 있음
_DIGIT_BATCHIM = set("013678")        # 영·일·삼·육·칠·팔


def ko_name(sym: str) -> str:
    return _KO_NAMES.get((sym or "").upper(), sym or "")


def _has_batchim(word: str) -> bool:
    w = re.sub(r"[\s)\]}'\"’%.,]+$", "", word or "")
    if not w:
        return False
    ch = w[-1]
    if "가" <= ch <= "힣":
        return (ord(ch) - 0xAC00) % 28 != 0
    if ch.isdigit():
        return ch in _DIGIT_BATCHIM
    if ch.isalpha():
        if w.isupper() or len(w) <= 3:
            return ch.upper() in _LATIN_BATCHIM       # 약어는 알파벳 이름으로 읽는다
        # 일반 영단어(BlackRock·Kraken·Coinbase Prime)는 영어 발음 끝소리로 근사한다.
        low = w.lower()
        if low[-1] in "bcdgklmnpt":
            return True                                 # 록·켄·임… 받침 있음
        if low[-1] == "e" and len(low) >= 2 and low[-2] in "mnkt":
            return True                                 # Prime(임)·Stone(온)
        return False
    return False


def josa(word: str, pair: str) -> str:
    """josa("BTC", "은/는") → "BTC는". pair: 은/는 · 이/가 · 을/를 · 과/와 · 으로/로."""
    a, b = pair.split("/")
    if pair == "으로/로":
        w = re.sub(r"[\s)\]}'\"’%.,]+$", "", word or "")
        last = w[-1] if w else ""
        # ㄹ 받침은 '로'
        if "가" <= last <= "힣" and (ord(last) - 0xAC00) % 28 == 8:
            return word + b
        if last.upper() == "L" or last in "178":
            return word + b
        return word + (a if _has_batchim(word) else b)
    return word + (a if _has_batchim(word) else b)


_KO_SENT_SPLIT = re.compile(r"(?<=[.!?。])\s+|\n+")
_KO_SENT_END = re.compile(r"(?:다|요|죠|니다|습니다|됩니다|있다|없다|했다|였다)[.!?]?$|[.!?]$")


def ko_sentences(summary_ko: str, skip_title: bool = True) -> list:
    """번역문을 **완결된 문장**만 골라 돌려준다(중간 절단 금지 계약).

    제외: 제목 줄(마침표 없는 첫 줄), "…"로 끝나거나 포함한 문장(요약 컷의 잘린
    꼬리), 링크 안내, 너무 짧은 조각, 지난 예측 자찬."""
    if not summary_ko:
        return []
    raw = summary_ko.replace("\r", "")
    cut = _FOOTER_RX.search(raw)
    if cut:
        raw = raw[:cut.start()]
    lines = [ln.strip() for ln in raw.split("\n") if ln.strip()]
    if skip_title and lines and not re.search(r"[.!?]$", lines[0]):
        lines = lines[1:]
    out = []
    for s in _KO_SENT_SPLIT.split("\n".join(lines)):
        s = s.strip()
        if len(s) < 12 or "…" in s or "..." in s:
            continue
        if len(re.findall(r"[가-힣]", s)) < 6:
            continue                  # 번역 실패로 영문이 그대로 남은 문장은 한국어 설명에 섞지 않는다
        if re.search(r"https?://|자세한 내용|여기에서|링크|구독|알림을 켜", s):
            continue
        if not _KO_SENT_END.search(s):
            continue
        if re.search(r"완벽하게.*(예측|따랐)|우리가 말했|즐기세요", s):
            continue
        s = re.sub(r"([#$￦])\s+(?=[A-Za-z0-9])", r"\1", s)
        if not re.search(r"[.!?]$", s):
            s += "."                  # "경고했다" → "경고했다." (설명 문단 안에서 문장 경계 유지)
        out.append(s)
    return out


# ── 한국어 조립 (브리핑 항목 = 요약줄 + 설명 2~3문장 + 가격 맥락줄) ─────────
# 2026-09-27 사용자 요청: "뉴스들은 코인별 길이가 너무 짧아. 브리핑이 길어져도 되니
# 내용별 문장을 좀 길게 해줘." → 설명은 ①무슨 일이 있었나 → ②왜 중요한가/시장이
# 통상 어떻게 받아들이나(이벤트 유형 prior) → ③채널·기사가 제시한 수치·레벨·조건.
# 문장은 전부 템플릿(원문에서 뽑은 사실·수치만 끼움)이고, 번역문은 **완결된 문장**
# 하나를 보조로만 쓴다 — 중간 절단·"…" 없음.

CHIP = {1: "🟢 호재", -1: "🔴 악재", 0: "⚪ 중립"}
MARKET_SYMBOL = "MARKET"

# 정보 유형 등급(랭킹, 기획안 §4-4 + 사용자 결정: 사실 > 🌐시장 > 💬)
TIER_FACT_H, TIER_FACT_M, TIER_MARKET, TIER_SCENARIO, TIER_CALL, TIER_LEGACY = 0, 1, 2, 3, 4, 5

_ACTION_SENT = {
    "신고점 갱신": "신고점을 갱신하고 있습니다",
    "급등": "급등한 상태입니다",
    "돌파": "위쪽 레벨을 돌파하고 있습니다",
    "저항 시험": "바로 위 저항을 시험하고 있습니다",
    "저항에 막힘": "저항에 막혀 밀리고 있습니다",
    "지지 시험": "아래쪽 지지 구간을 시험하고 있습니다",
    "되돌림": "되돌림을 받고 있습니다",
    "박스권": "박스권 안에서 움직이고 있습니다",
}


def _pct(a: float, b: float) -> str:
    return f"{(a / b - 1) * 100:+.1f}%"


def _asset_of(text: str, sym: str) -> str:
    if sym and sym != MARKET_SYMBOL:
        return ko_name(sym)
    t = text or ""
    if re.search(r"\bcrypto (?:ETFs?|funds?)\b|\bcrypto ETF\b", t, re.I):
        return "가상자산"
    if re.search(r"\bbitcoin\b|\bBTC\b", t, re.I):
        return "비트코인"
    if re.search(r"\bethereum\b|\bETH\b", t, re.I):
        return "이더리움"
    return "코인"


def _bp(text: str) -> str:
    m = re.search(r"(\d+)\s?(?:bps|basis points?)", text or "", re.I)
    return f"{m.group(1)}bp" if m else ""


_FED_MOVE_RX = [
    ("인상", re.compile(r"\bfed\b[^\n]{0,50}?\b(?:hikes?|hiked|raises?|raised)\b|\brate hike", re.I)),
    ("인하", re.compile(r"\bfed\b[^\n]{0,50}?\b(?:cuts?|lowers?|lowered)\b|\brate cut", re.I)),
    ("동결", re.compile(r"\bfed\b[^\n]{0,50}?\b(?:holds?|held|pauses?|paused)\b", re.I)),
]


def _fed_move(text: str) -> str:
    """연준 결정의 **확정 서술**만 — "인상"/"인하"/"동결" 또는 "".

    2026-09-27 리뷰 RV2-N3: 종전엔 본문 어디서든 hike 류 단어가 보이면(질문·예상 문장
    포함) "미 연준이 기준금리를 인상했습니다"라고 썼다. 이제 줄(제목 먼저) 순서로 보고,
    결정 동사가 걸린 구절이 가정·예상·질문(is_speculative)이면 건너뛴다. 한 줄 안에서는
    **가장 앞에 걸린 결정**을 쓴다("Fed holds … traders expect a rate cut" → 동결)."""
    for line in (text or "").split("\n"):
        best = None
        for mv, rx in _FED_MOVE_RX:
            for m in rx.finditer(line):
                if is_speculative(line, m.end() - 1):
                    continue
                if best is None or m.start() < best[0]:
                    best = (m.start(), mv)
                break
        if best:
            return best[1]
    return ""


_MACRO_FACTORS = [
    (re.compile(r"iran|israel|russia|ukraine|taiwan|geopolit|tensions?|\bwar\b", re.I), "지정학 긴장"),
    (re.compile(r"yields?", re.I), "금리 상승"),
    (re.compile(r"\boil\b", re.I), "유가 부담"),
    (re.compile(r"inflation", re.I), "인플레이션 우려"),
    (re.compile(r"tariffs?", re.I), "관세"),
    (re.compile(r"liquidation", re.I), "레버리지 청산 위험"),
]


def _first_cap(rx: str, text: str) -> str:
    m = re.search(rx, text or "", re.M)
    return m.group(1).strip() if m else ""


_DEPOSIT_RX = re.compile(r"deposit(?:s|ed)?\b[^\n]{0,60}\binto\b", re.I)


def event_phrase(p: dict, text: str) -> str:
    k, pol = p.get("type"), p.get("pol", 0)
    if k == "etf":
        dec = p.get("decision")
        if dec:
            return {"approve": "ETF 승인", "reject": "ETF 거절", "delay": "ETF 결정 연기"}[dec]
        if not _ETF_FLOW_WORD_RX.search(text or "") and not p.get("amounts"):
            # 흐름 단어·금액 없이 "순유입"이라 쓰면 원문에 없는 사실이 된다(RV2-N4).
            return "ETF 매수세" if pol > 0 else ("ETF 매도세" if pol < 0 else "ETF 소식")
        return "ETF 순유입" if pol > 0 else ("ETF 순유출" if pol < 0 else "ETF 자금 동향")
    if k == "whale":
        if _DEPOSIT_RX.search(text):
            return "거래소 대량 입금"
        return "고래 매수" if pol > 0 else ("고래 매도" if pol < 0 else "고래 대량 이동")
    if k == "flow":
        return "자금 유입" if pol > 0 else ("자금 유출" if pol < 0 else "자금 흐름")
    if k == "hack":
        return "해킹 피해"
    if k == "inst":
        return "기관 수요" if pol > 0 else ("기관 매도" if pol < 0 else "기관 동향")
    if k == "partner":
        return "제휴 발표"
    if k == "product":
        return "업그레이드·출시"
    if k == "macro":
        mv = _fed_move(text)
        if mv:
            return f"연준 금리 {mv}"
        return "매크로 위험회피" if pol < 0 else ("매크로 완화" if pol > 0 else "매크로 변수")
    if k == "reg":
        head = "CLARITY 법안" if re.search(r"clarity", text, re.I) else (
            "SEC" if re.search(r"\bSEC\b", text) else "규제")
        tail = "진전" if pol > 0 else ("난항" if pol < 0 else "이슈")
        return f"{head} {tail}"
    if k == "listing":
        return "상장폐지" if pol < 0 else "거래소 상장"
    if k == "unlock":
        return "토큰 언락"
    if k == "burn":
        return "소각·바이백"
    return p.get("label") or "시황"


def summary_line(p: dict, text: str) -> str:
    """② 요약줄 — 칩 + 유형 + 핵심 수치 + 시간축."""
    kind = p.get("kind")
    if kind == "fact":
        amt = p["amounts"][0][0] if p.get("amounts") else ""
        extra = _bp(text) if p.get("type") == "macro" else ""
        chip = CHIP[0] if p.get("stale") else CHIP[p.get("pol", 0)]
        core = " ".join(x for x in (chip, event_phrase(p, text), amt, extra) if x)
        hz = "회고 기사" if p.get("stale") else p.get("horizon", "단기")
        return f"{core} · {hz}"
    if kind == "scenario":
        tf = f"({p['tf']})" if p.get("tf") else ""
        act = p.get("action") or "분기 제시"
        return f"💬 차트 의견{tf} · {act} · 단기"
    if kind == "call":
        tf = f"({p['tf']})" if p.get("tf") else ""
        if p.get("speculative"):
            return "💬 전망·가정 기사 · 미확인"
        if p.get("source") == "기사":
            st = "" if p.get("stance") == "관망" else f" · {p['stance']} 논조"
            lv = p.get("levels") or {}
            tl = p.get("title_level") or ""
            if tl and lv.get("support") == tl:
                lvs = f" · 지지 {tl}"
            elif tl and lv.get("resistance") == tl:
                lvs = f" · 저항 {tl}"
            elif tl:
                lvs = f" · 목표 {tl}"
            else:
                lvs = ""
            return f"💬 분석 기사{st}{lvs}"
        pat = f" · {p['pattern']}" if p.get("pattern") else ""
        return f"💬 {p.get('stance', '관망')} 의견{tf}{pat} · 단기"
    return ""


def _fact_what(p: dict, text: str, sym: str) -> str:
    k, pol = p.get("type"), p.get("pol", 0)
    amt = p["amounts"][0][0].lstrip("+−") if p.get("amounts") else ""
    subj = "시장" if sym == MARKET_SYMBOL else ko_name(sym)
    asset = _asset_of(text, sym)
    if k == "etf":
        dec = p.get("decision")
        if dec:
            actor = "미 SEC" if re.search(r"\bSEC\b", text) else "규제 당국"
            spot = "현물 " if re.search(r"\bspot\b", text, re.I) else ""
            etf = f"{asset} {spot}ETF"
            if dec == "approve":
                return f"{josa(actor, '이/가')} {josa(etf, '을/를')} 승인했습니다."
            if dec == "reject":
                return f"{josa(actor, '이/가')} {josa(etf, '을/를')} 승인하지 않았습니다."
            return f"{josa(actor, '이/가')} {etf} 승인 여부 결정을 미뤘습니다."
        has_flow = bool(_ETF_FLOW_WORD_RX.search(text))
        if pol > 0:
            if amt:
                return f"{asset} 현물 ETF로 {amt} 규모의 순유입이 집계됐습니다."
            return (f"{asset} 현물 ETF 자금이 순유입으로 돌아섰습니다." if has_flow
                    else f"{asset} 현물 ETF에 매수 수요가 들어오고 있다는 소식입니다.")
        if pol < 0:
            if amt:
                return f"{asset} 현물 ETF에서 {amt} 규모의 순유출이 나왔습니다."
            return (f"{asset} 현물 ETF에서 자금이 빠져나가고 있습니다." if has_flow
                    else f"{asset} 현물 ETF에서 매도·환매가 나오고 있다는 소식입니다.")
        return f"{asset} 현물 ETF 자금 흐름 관련 소식입니다" + (f"(언급 규모 {amt})." if amt else ".")
    if k == "hack":
        where = (_first_cap(r"(?:drained|stolen|taken) from ([A-Z][\w.]*\w)", text)
                 or _first_cap(r"([A-Z][\w.]*\w)\s+(?:[Ee]xploit|[Hh]ack)", text)
                 or _first_cap(r"([A-Z][\w.]*\w)(?:,[^\n]{0,40})? was (?:exploited|hacked|drained)", text))
        where = where or subj
        return (f"{where}에서 {amt} 규모의 해킹(자산 탈취) 피해가 발생했습니다." if amt
                else f"{where}에서 해킹(자산 탈취) 피해가 보고됐습니다.")
    if k == "reg":
        if re.search(r"clarity", text, re.I):
            odds = _first_cap(r"odds (?:crash|drop|fall)\w* to (\d+%)", text)
            if pol < 0:
                return ("미국 가상자산 시장구조법(CLARITY 법안)의 상원 처리가 난항을 겪고 있습니다"
                        + (f". 통과 확률은 {odds}까지 떨어졌습니다." if odds else "."))
            if pol > 0:
                return "미국 가상자산 시장구조법(CLARITY 법안) 처리가 한 단계 진전됐습니다."
            return "미국 가상자산 시장구조법(CLARITY 법안) 관련 일정이 나왔습니다."
        if re.search(r"\bSEC\b", text):
            actor = "미 SEC"
        elif re.search(r"\bCFTC\b", text):
            actor = "미 CFTC"
        elif re.search(r"senate|congress|house committee", text, re.I):
            actor = "미 의회"
        elif re.search(r"court|judge|injunction|ruling", text, re.I):
            actor = "법원"
        else:
            actor = "규제 당국"
        if pol > 0:
            return f"{josa(actor, '이/가')} 가상자산에 우호적인 조치를 내놨습니다."
        if pol < 0:
            return f"{josa(actor, '이/가')} 가상자산에 불리한 결정을 내렸거나 관련 일정이 틀어졌습니다."
        return f"{actor} 관련 규제 소식입니다."
    if k == "macro":
        mv, bp = _fed_move(text), _bp(text)
        if mv == "동결":
            return "미 연준이 기준금리를 동결했습니다."
        if mv:
            return f"미 연준이 기준금리를 {bp + ' ' if bp else ''}{mv}했습니다."
        facs = []
        for rx, lab in _MACRO_FACTORS:
            if rx.search(text) and lab not in facs:
                facs.append(lab)
        if facs:
            tail = "위험자산 회피 움직임이 나타났습니다." if pol < 0 else "시장 변수로 떠올랐습니다."
            return f"{'·'.join(facs[:3])} 등으로 {tail}"
        return "매크로 변수가 시장에 영향을 주고 있습니다."
    if k == "whale":
        coins = re.search(r"([\d,]+(?:\.\d+)?)\s+(?:\$|#)?(BTC|BITCOIN|ETH|ETHEREUM|SOL)\b", text, re.I)
        qty = ""
        if coins:
            tk = coins.group(2).upper().replace("BITCOIN", "BTC").replace("ETHEREUM", "ETH")
            qty = f"{tk} {coins.group(1)}개"
        if qty and amt:
            what = f"{qty}({amt} 규모)"
        else:
            what = qty or (f"{amt} 규모" if amt else "")
        if _DEPOSIT_RX.search(text):
            # 주체는 두세 단어 이름까지("Trend Research") — RV2-N10: 종전엔 "Research"만 잡혔다.
            who = _first_cap(r"(?<![\w:])([A-Z][\w]+(?:[ \t]+[A-Z][\w]+){0,2})[ \t]+deposit(?:s|ed)\b",
                             text) or "대형 지갑"
            dest = _first_cap(r"\binto ([A-Z][\w]+(?: [A-Z][\w]+)?)", text) or "거래소"
            # 수량(qty)에 이미 자산 티커가 있으면 자산명을 또 붙이지 않는다("ETH 50,000개 이더리움" 방지).
            obj = what if qty else (f"{what} {asset}" if what else asset)
            return f"{josa(who, '이/가')} {josa(obj, '을/를')} {josa(dest, '으로/로')} 입금했습니다."
        who = _first_cap(r"^(?:BREAKING:\s*|JUST IN:\s*)?([A-Z][a-z]\w+) moved", text) or "고래 지갑"
        verb = "매수했습니다" if pol > 0 else ("매도했습니다" if pol < 0 else "옮겼습니다")
        obj = what if qty else (f"{what}의 {asset}" if what else asset)
        return f"{josa(who, '이/가')} {josa(obj, '을/를')} {verb}."
    if k == "flow":
        d = "유입" if pol >= 0 else "유출"
        return f"{subj}에 {amt + ' 규모의 ' if amt else ''}자금 {josa(d, '이/가')} 확인됐습니다."
    if k == "partner":
        who = _first_cap(r"\b(?:with|With)\s+([A-Z][\w]+(?:\s+[A-Z][\w]+)?)", text)
        return (f"{subj} 생태계가 {josa(who, '과/와')} 손을 잡았습니다." if who
                else f"{subj} 관련 제휴 소식입니다.")
    if k == "inst":
        who = _first_cap(r"\b(BlackRock|JPMorgan|Fidelity|Grayscale|MicroStrategy|Metaplanet)\b", text)
        return f"{subj} 관련 기관 소식입니다" + (f"({who})." if who else ".")
    if k == "product":
        return f"{subj} 네트워크 업그레이드·신규 기능 출시 소식입니다."
    if k == "listing":
        ex = _first_cap(r"\b(Binance|Coinbase|Upbit|Bithumb|OKX|Robinhood|Kraken|Bybit)\b", text) or "거래소"
        return f"{josa(subj, '이/가')} {ex}에 {'상장폐지' if pol < 0 else '상장'}됩니다."
    if k == "unlock":
        return f"{subj} 토큰 {amt + ' 규모 ' if amt else ''}언락(물량 해제)이 예정돼 있습니다."
    if k == "burn":
        return f"{subj} 소각·바이백{' ' + amt if amt else ''} 소식입니다."
    return f"{subj} 관련 소식입니다."


def _fact_why(p: dict, text: str) -> str:
    k, pol = p.get("type"), p.get("pol", 0)
    if p.get("stale"):
        return "과거 사건을 다시 짚는 회고 기사라 새로운 가격 재료로 보기는 어렵습니다."
    if k == "hack":
        if _EXCHANGE_RX.search(text):
            return "거래소 해킹은 해당 거래소 자산뿐 아니라 시장 전반의 위험회피로 번지기도 하는 단기 악재입니다."
        return "해킹은 보통 해당 생태계 코인에 단기 매도 압력으로 작용하는 악재입니다."
    if k == "etf":
        dec = p.get("decision")
        if dec == "approve":
            return "ETF 승인은 기관 자금이 들어올 통로가 열린다는 뜻이라 통상 호재로 받아들여집니다."
        if dec == "reject":
            return "ETF 불승인은 기관 자금 유입 기대를 늦춰 통상 악재로 받아들여집니다."
        if dec == "delay":
            return "ETF 결정 연기는 불확실성을 늘려 통상 단기 악재로 받아들여집니다."
        if pol > 0:
            return "ETF 순유입은 기관의 현물 매수 수요로 해석돼 통상 단기 호재로 받아들여집니다."
        if pol < 0:
            return "ETF 순유출은 기관 수요 약화로 해석돼 통상 단기 악재로 받아들여집니다."
        return "ETF 자금 흐름은 기관 수요를 가늠하는 지표로 쓰입니다."
    if k == "reg":
        if pol > 0:
            return "규제 불확실성을 줄이는 소식은 통상 중장기 호재로 받아들여집니다."
        if pol < 0:
            return "규제 일정의 지연·불발은 불확실성을 키워 통상 악재로 받아들여집니다."
        return "규제 소식은 세부 조건에 따라 해석이 갈리는 재료입니다."
    if k == "macro":
        mv = _fed_move(text)
        if mv == "인상":
            return "금리 인상은 위험자산 선호를 낮춰 코인 시장에 통상 단기 부담으로 받아들여집니다."
        if mv == "인하":
            return "금리 인하는 유동성 기대를 키워 코인 시장에 통상 호재로 받아들여집니다."
        if pol < 0:
            return "위험회피 국면에서는 코인 같은 위험자산이 먼저 매도되고 레버리지 청산이 겹쳐 변동성이 커지기 쉽습니다."
        return "매크로 변수는 개별 코인 재료보다 시장 전체 방향에 영향을 주는 재료입니다."
    if k == "whale":
        if _DEPOSIT_RX.search(text):
            return "거래소로의 대량 입금은 매도 대기 물량으로 해석돼 통상 단기 악재로 받아들여집니다."
        if pol > 0:
            return "대형 지갑의 매수는 수급상 통상 단기 호재로 받아들여집니다."
        if pol < 0:
            return "대형 지갑의 매도는 수급상 통상 단기 악재로 받아들여집니다."
        return "대규모 이동 자체는 방향이 정해지지 않은 사실이라 이후 거래소 입출금 흐름을 함께 봐야 합니다."
    if k == "flow":
        return ("자금 유입은 수요 증가 신호로 통상 단기 호재로 받아들여집니다." if pol >= 0
                else "자금 유출은 수요 감소 신호로 통상 단기 악재로 받아들여집니다.")
    if k == "partner":
        return "제휴는 즉각적인 가격 재료라기보다 중장기 펀더멘털 재료로 받아들여지는 편입니다."
    if k == "inst":
        return "기관 동향은 단기 가격보다 중장기 수요 기반과 관련된 재료입니다."
    if k == "product":
        return "업그레이드·출시는 중장기 펀더멘털 재료이며, 가격 반응은 일정보다 앞서 나타나는 경우가 많습니다."
    if k == "listing":
        return ("상장폐지는 유동성이 사라지는 강한 단기 악재입니다." if pol < 0
                else "상장 소식은 발표 전후로 단기 급등락이 잦은 재료입니다.")
    if k == "unlock":
        return "언락은 유통 물량을 늘려 통상 언락 전후로 매도 압력이 나타나는 악재입니다."
    if k == "burn":
        return "소각·바이백은 유통량을 줄여 통상 중장기 호재로 받아들여집니다."
    return "방향이 정해지지 않은 소식이라 가격 반응을 함께 봐야 합니다."


def _fact_numbers(p: dict) -> str:
    extra = [a[0] for a in (p.get("amounts") or [])[1:4]]
    lv = p.get("levels") or {}
    parts = []
    if extra:
        parts.append(f"원문에 함께 언급된 수치는 {', '.join(extra)}입니다.")
    if lv.get("support") and lv.get("resistance"):
        parts.append(f"원문은 지지 {lv['support']}, 저항 {josa(lv['resistance'], '을/를')} 제시했습니다.")
    elif lv.get("support"):
        parts.append(f"원문은 지지선으로 {josa(lv['support'], '을/를')} 제시했습니다.")
    elif lv.get("resistance"):
        parts.append(f"원문은 저항선으로 {josa(lv['resistance'], '을/를')} 제시했습니다.")
    return " ".join(parts)


def _pick_ko_detail(summary_ko: str, used: str = "") -> str:
    for s in ko_sentences(summary_ko):
        if s in used:
            continue
        return s
    return ""


def _scenario_sentences(p: dict, sym: str) -> list:
    name = ko_name(sym)
    tf = p.get("tf") or ""
    price = p.get("price") or ""
    act = p.get("action") or ""
    out = []
    where = f"{tf} 차트 기준 " if tf else ""
    if price:
        if act == "되돌림" and p.get("high"):
            out.append(f"{josa(name, '은/는')} {where}{p['high']} 고점에서 밀려 {price} 부근에 있습니다.")
        else:
            out.append(f"{josa(name, '은/는')} {where}{price} 부근에서 "
                       f"{_ACTION_SENT.get(act, '움직이고 있습니다')}.")
    b, r = p.get("bull") or {}, p.get("bear") or {}
    up, dn = b.get("trigger", ""), r.get("trigger", "")
    if up and dn and up == dn:
        tail_up = f"{b['target']}까지 반등하고" if b.get("target") else "반등하고"
        tail_dn = f"{r['target']}까지 밀리는" if r.get("target") else "밀리는"
        out.append(f"채널은 {josa(up, '을/를')} 기준선으로 삼아, 지키면 {tail_up} "
                   f"잃으면 {tail_dn} 두 갈래를 제시했습니다.")
    elif up or dn:
        segs = []
        if up:
            cond = f"{josa(up, '을/를')} 지키면" if b.get("verb") == "hold" else f"{up} 위로 올라서면"
            segs.append(f"{cond} {b['target'] + '까지 ' if b.get('target') else ''}상승을 이어가는 강세 시나리오")
        if dn:
            cond = f"{josa(dn, '을/를')} 잃으면" if r.get("verb") == "lose" else f"{dn} 아래로 밀리면"
            segs.append(f"{cond} {r['target'] + '까지 ' if r.get('target') else ''}내려가는 약세 시나리오")
        if len(segs) == 2:
            out.append(f"채널은 {josa(segs[0], '과/와')} {josa(segs[1], '을/를')} 함께 제시했습니다.")
        else:
            out.append(f"채널은 {josa(segs[0], '을/를')} 제시했습니다.")
    pv = _fnum(price)
    if pv and (up or dn):
        uv, dv = _fnum(up), _fnum(dn)
        if up and dn and up == dn and uv:
            out.append(f"게시 시점 가격 기준으로 기준선까지 거리는 {_pct(uv, pv)}였습니다.")
        else:
            bits = []
            if uv:
                bits.append(f"강세 기준선은 {_pct(uv, pv)}")
            if dv:
                bits.append(f"약세 기준선은 {_pct(dv, pv)}")
            out.append(f"게시 시점 가격 기준으로 {', '.join(bits)} 거리에 있었습니다.")
    return out


_MOVE_RX = re.compile(
    r"(gained|rose|risen|surged|climbed|jumped|rallied|dropped|fell|fallen|declined|lost|slid)\s+"
    r"(?:by\s+)?(?:an?\s+)?(?:impressive\s+|more than\s+|over\s+|nearly\s+|almost\s+)?"
    r"(\d+(?:\.\d+)?%)", re.I)
_SPAN_RX = re.compile(r"(?:over|in) the (?:past|last) (\w+) (sessions?|days?|weeks?|hours?|months?)", re.I)
_NUM_WORDS = {"two": "2", "three": "3", "four": "4", "five": "5", "six": "6", "seven": "7",
              "eight": "8", "nine": "9", "ten": "10", "a": "1", "one": "1"}
_SPAN_KO = {"session": "거래일", "day": "일", "week": "주", "hour": "시간", "month": "개월"}


def _article_move(body: str) -> str:
    """기사 본문의 등락 사실 한 문장 — "Over the past four sessions, DOGE has gained an
    impressive 42.86%" → "기사 기준 최근 4거래일 동안 42.86% 올랐습니다." 없으면 ""."""
    m = _MOVE_RX.search(body or "")
    if not m:
        return ""
    up = m.group(1).lower() in ("gained", "rose", "risen", "surged", "climbed", "jumped", "rallied")
    sp = _SPAN_RX.search(body or "")
    span = ""
    if sp:
        n = _NUM_WORDS.get(sp.group(1).lower(), sp.group(1))
        unit = _SPAN_KO.get(sp.group(2).lower().rstrip("s"), "")
        if n.isdigit() and unit:
            span = f"최근 {n}{unit} 동안 "
    return f"기사 기준 {span}{m.group(2)} {'올랐습니다' if up else '내렸습니다'}."


def _ko_title(summary_ko: str) -> str:
    """번역문 첫 줄(제목). 한글이 충분하고 잘림 표시가 없을 때만."""
    first = (summary_ko or "").replace("\r", "").split("\n", 1)[0].strip()
    first = re.sub(r"([#$￦])\s+(?=[A-Za-z0-9])", r"\1", first)
    if len(re.findall(r"[가-힣]", first)) < 4 or "…" in first or len(first) > 80:
        return ""
    return first.rstrip(".")


def _call_sentences(p: dict, sym: str, summary_ko: str) -> list:
    name = ko_name(sym)
    out = []
    lv = p.get("levels") or {}
    tl = p.get("title_level") or ""
    if p.get("speculative"):
        # 가정·예상·루머·질문형 기사(RV2-N3·N5·N11) — 사건을 서술하지 않고 제목만 인용한다.
        kt = _ko_title(summary_ko) or (p.get("title_en") or "").strip()
        subj = "시장" if sym == MARKET_SYMBOL else name
        out.append(f"{josa(subj, '을/를')} 다룬 전망·가정 기사입니다.")
        if kt:
            out.append(f"기사 제목은 “{kt}”입니다.")
        out.append("확인된 사건이 아니라 예상·가정·의견을 담은 내용이라 사실로 단정할 수 없습니다.")
        return out
    if p.get("source") == "기사":
        if tl and lv.get("support") == tl:
            out.append(f"분석 기사는 {josa(name, '이/가')} {tl} 지지선 위에서 버티고 있다고 봤습니다.")
        elif tl and lv.get("resistance") == tl:
            out.append(f"분석 기사는 {josa(name, '이/가')} {tl} 저항선에 부딪혀 있다고 봤습니다.")
        elif tl:
            out.append(f"분석 기사는 {josa(name, '이/가')} {tl} 부근을 향해 움직이고 있다고 봤습니다.")
        else:
            kt = _ko_title(summary_ko) or (p.get("title_en") or "").strip()
            out.append(f"분석 기사가 {name}의 가격 흐름을 다뤘습니다.")
            if kt:
                out.append(f"기사 제목은 “{kt}”입니다.")
        mv = _article_move(p.get("_body") or "")
        if mv:
            out.append(mv)
        if p.get("stance") in ("강세", "약세"):
            out.append(f"기사의 논조는 {p['stance']} 쪽이며, 확인된 사건이 아니라 분석 의견입니다.")
    else:
        tf = p.get("tf")
        st = p.get("stance", "관망")
        phrase = {"강세": "상승을 예상하는 강세 의견을", "약세": "하락을 예상하는 약세 의견을",
                  "관망": "뚜렷한 방향 없이 관망하는 의견을"}[st]
        out.append(f"채널은 {tf + ' 차트 기준으로 ' if tf else ''}{name}에 대해 {phrase} 냈습니다.")
        pat = p.get("pattern")
        if pat == "저항 돌파":
            out.append("근거로는 주요 저항을 위로 돌파한 흐름을 들었습니다.")
        elif pat == "지지 확인":
            out.append("근거로는 지지 구간이 지켜지고 있다는 점을 들었습니다.")
        elif pat:
            out.append(f"근거로는 차트의 {josa(pat, '을/를')} 들었습니다.")
    tgt = p.get("target")
    lv_bits = []
    if lv.get("resistance") and lv.get("resistance") != tl:
        lv_bits.append(f"저항 {lv['resistance']}")
    if lv.get("support") and lv.get("support") != tl:
        lv_bits.append(f"지지 {lv['support']}")
    if tgt and p.get("source") != "기사":
        out.append(f"채널이 제시한 목표는 {tgt}입니다.")
    if lv_bits:
        pre = "조건부 의견으로, " if p.get("conditional") else ""
        out.append(f"{pre}기준 레벨로 {josa('·'.join(lv_bits), '을/를')} 제시했습니다.")
    if len(out) < 2:
        ko = _pick_ko_detail(summary_ko, " ".join(out))
        if ko:
            out.append(ko)
    if len(out) < 2:
        # 번역 보조문도 없으면(번역 실패로 영문 유지 등) 템플릿 2문장째 — 사용자 요구
        # "설명 2~3문장"(RV2-N9). 사실을 보태지 않는 성격 안내 문장만 쓴다.
        out.append("확인된 사건이 아니라 분석 의견입니다." if p.get("source") == "기사"
                   else "확인된 사건이 아니라 채널의 차트 해석입니다.")
    return out


def context_line(p: dict, sym: str, ctx: dict) -> str:
    """④ 가격 맥락줄 — 24h 등락 · 분기선/레벨 거리. 데이터 없으면 해당 부분 생략."""
    parts = []
    chg = ctx.get("chg24")
    if chg is not None:
        parts.append(("BTC " if sym == MARKET_SYMBOL else "") + f"24h {chg:+.1f}%")
    cur = ctx.get("cur_usd")
    kind = p.get("kind")
    age_h = ctx.get("age_h")

    def _d(v: str) -> str:
        fv = _fnum(v)
        return f"({_pct(fv, cur)})" if (fv and cur) else ""

    if kind == "scenario":
        us = (p.get("bull") or {}).get("trigger", "")
        ds = (p.get("bear") or {}).get("trigger", "")
        if us and ds and us == ds:
            parts.append(f"분기 {us}{_d(us)}")
        else:
            seg = []
            if us:
                seg.append(f"↑{us}{_d(us)}")
            if ds:
                seg.append(f"↓{ds}{_d(ds)}")
            if seg:
                parts.append("분기 " + " ".join(seg))
    elif kind == "call":
        lv = p.get("levels") or {}
        if p.get("source") == "기사":
            for key, lab in (("resistance", "저항"), ("support", "지지")):
                v = lv.get(key) or ""
                if v:
                    parts.append(f"{lab} {v}{_d(v) if v.startswith('$') else ''}")
            if p.get("target"):
                v = p["target"]
                parts.append(f"목표 {v}{_d(v) if v.startswith('$') else ''}")
        elif p.get("target"):
            parts.append(f"목표 {p['target']}")
    elif kind == "fact":
        lv = p.get("levels") or {}
        for key, lab in (("support", "지지"), ("resistance", "저항")):
            v = lv.get(key) or ""
            if v:
                parts.append(f"{lab} {v}{_d(v) if v.startswith('$') else ''}")
    # 게시 후 하루 이상 지난 글은 경과를 사실로 적는다 — "이미 반영됐나"를 사용자가 판단
    # (기획안 §4-1 원칙 4: 해석하지 않고 사실만). 하루 미만은 표시하지 않는다.
    if age_h is not None and age_h >= 24:
        parts.append(f"게시 {int(age_h // 24)}일 전")
    return " · ".join(parts)


def compose(p: dict, sym: str, text_en: str, summary_ko: str = "",
            ctx: Optional[dict] = None, n_sent: int = 3) -> Optional[dict]:
    """브리핑 항목 1건 구조 → {"summary", "detail"(문장 목록), "context", "tier"}.
    노이즈면 None. n_sent 는 설명 문장 수 상한(최소 2 — 사용자 요청 "2~3문장")."""
    kind = p.get("kind")
    if kind not in ("fact", "scenario", "call"):
        return None
    ctx = ctx or {}
    n = max(2, int(n_sent or 3))
    text = clean(text_en)
    if kind == "fact":
        what = _fact_what(p, text, sym)
        why = _fact_why(p, text)
        nums = _fact_numbers(p)
        detail = [what]
        # 템플릿이 사실을 거의 못 담는 유형(제휴·기관·업그레이드·규제·매크로 약한 단서)은
        # 번역문 한 문장이 '무슨 일'을 보충한다. 금액이 핵심인 유형은 숫자 문장이 우선.
        if p.get("type") in ("partner", "inst", "product", "reg", "macro", "listing", "burn") \
                and not _fed_move(text):
            ko = _pick_ko_detail(summary_ko, what)
            if ko:
                detail.append(ko)
        detail.append(why)
        if nums:
            detail.append(nums)
        detail = detail[:n]
        if sym == MARKET_SYMBOL:
            tier = TIER_MARKET
        else:
            tier = TIER_FACT_H if p.get("tier") == "H" and not p.get("stale") else TIER_FACT_M
    elif kind == "scenario":
        detail = _scenario_sentences(p, sym)[:n]
        has_branch = (p.get("bull") or {}).get("trigger") or (p.get("bear") or {}).get("trigger")
        tier = TIER_SCENARIO if has_branch else TIER_CALL
    else:
        detail = _call_sentences(dict(p, _body=text), sym, summary_ko)[:n]
        tier = TIER_CALL
    return {"summary": summary_line(p, text), "detail": [d for d in detail if d],
            "context": context_line(p, sym, ctx), "tier": tier}
