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
    r"(entry|enter|buy|long\s*entry|진입가?|진입|매수가?|롱\s*진입|buy\s*zone|entry\s*zone)"
    r"\s*(?:price|zone|구간|가격)?\s*[:=]?\s*",
    re.I,
)
_SL_LABEL = re.compile(
    r"(stop\s*loss|stop|sl|손절가?|손절|스탑|스톱)\s*[:=]?\s*", re.I,
)
# "target(?!s)": "Take-Profit Targets:" 같은 복수형 섹션 헤더에서 "target"만 매칭돼
# 그 뒤 실제 "TP1: 5.298" 라벨을 건너뛰고 엉뚱한 위치를 가리키는 것을 막는다
# (2026-07-23 실전 발견 — 아래 _ORDINAL_LABEL 설명 참고).
_TP_LABEL = re.compile(
    r"(take\s*profit|target(?!s)|tp\d?|목표가?|목표|타겟\s*\d?|익절가?)\s*[:=]?\s*", re.I,
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
_SPACED_ORDINAL_LABEL = re.compile(
    r"\b(TP|SL|Target|Entry|타겟|목표|진입|손절)\s+[0-9]{1,2}(?=\s*[:=])", re.I
)

# 가격 숫자 하나: 1,234.56 / 0.00123 / 12100 / $8.30 (콤마·$ 허용)
_NUM = r"\$?\s*([0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]+)?|[0-9]*\.[0-9]+|[0-9]+)"
# 범위: 0.45 - 0.48 / 12,000~12,500
_RANGE = re.compile(_NUM + r"\s*[-–~〜]\s*" + _NUM)
_SINGLE = re.compile(_NUM)

# 오인 유발 토큰 제거용: 레버리지 10x, 퍼센트, 날짜연도
_LEVERAGE = re.compile(r"\b\d{1,3}\s*x\b", re.I)
_PERCENT = re.compile(r"[+\-]?\s*\d+(?:\.\d+)?\s*%")
_YEAR = re.compile(r"\b20[2-9]\d\b")


def _to_float(s: str) -> Optional[float]:
    try:
        return float(s.replace(",", "").replace("$", "").strip())
    except (ValueError, AttributeError):
        return None


def _clean(text: str) -> str:
    """숫자 오인 유발 토큰을 먼저 지운다(레버리지/퍼센트/연도/라벨 서수)."""
    text = _LEVERAGE.sub(" ", text)
    text = _PERCENT.sub(" ", text)
    text = _YEAR.sub(" ", text)
    text = _ORDINAL_LABEL.sub(lambda m: m.group(1), text)  # "TP1:" → "TP:"
    text = _SPACED_ORDINAL_LABEL.sub(lambda m: m.group(1), text)  # "Target 1:" → "Target:"
    return text


def _grab_after(label_pat, text: str) -> list:
    """라벨 뒤 짧은 창(30자) 안에서 첫 숫자(또는 범위)를 검색해 수집. 범위면 [lo, hi].
    '맨 앞 고정'이 아니라 검색으로 하는 이유: 라벨과 숫자 사이에 '가', 'around', '@',
    통화기호 등 잡토큰이 끼는 경우가 흔하기 때문. 단, 창을 짧게 잡아 무관한 숫자를
    끌어오지 않는다(가장 왼쪽 숫자만 채택)."""
    out = []
    for m in label_pat.finditer(text):
        window = text[m.end(): m.end() + 30]
        rng = _RANGE.search(window)
        sng = _SINGLE.search(window)
        # 범위와 단일이 둘 다 잡히면, 더 왼쪽에서 시작하는 쪽을 채택(범위 우선 동률).
        if rng and (not sng or rng.start() <= sng.start()):
            lo, hi = _to_float(rng.group(1)), _to_float(rng.group(2))
            if lo and hi:
                out.append(sorted([lo, hi]))
                continue
        if sng:
            v = _to_float(sng.group(1))
            if v:
                out.append([v])
    return out


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

    return {
        "direction": direction,
        "entry": entry,
        "entry_low": entry_low,
        "entry_high": entry_high,
        "sl": sl,
        "tp": tp,
        "rr": rr,
    }
