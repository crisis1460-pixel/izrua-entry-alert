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
_TP_LABEL = re.compile(
    r"(take\s*profit|target|tp\d?|목표가?|목표|타겟\s*\d?|익절가?)\s*[:=]?\s*", re.I,
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
    """숫자 오인 유발 토큰을 먼저 지운다(레버리지/퍼센트/연도)."""
    text = _LEVERAGE.sub(" ", text)
    text = _PERCENT.sub(" ", text)
    text = _YEAR.sub(" ", text)
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
