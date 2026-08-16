"""
해외 공개 시세 — 김프 계산용 USD 가격 (인증 불필요, izrua_watcher 방식).
김프% = (업비트KRW가 ÷ 해외USDT가 − USDT/KRW환율) ÷ USDT/KRW환율 × 100

2026-08-01 (m-7 수리): Binance 두 엔드포인트(api.binance.com + data-api.binance.vision)
모두 HTTP 451(지역 차단)로 실패 → touch_kimchi_pct 전 행 NULL.
Bybit 공개 스팟 시세를 3차 폴백으로 추가 — 인증 불필요, 한국/미국 접근 가능.
"""

import logging
from typing import Optional

import requests

logger = logging.getLogger("alert.binance")

_BINANCE_ENDPOINTS = (
    "https://api.binance.com",
    "https://data-api.binance.vision",
)


def _try_binance(pair: str, timeout: float) -> Optional[float]:
    """Binance 엔드포인트 순회. 200 → 가격, 400(미상장) → None, 기타 → None(폴백)."""
    for base in _BINANCE_ENDPOINTS:
        try:
            r = requests.get(
                f"{base}/api/v3/ticker/price",
                params={"symbol": pair}, timeout=timeout,
            )
            if r.status_code == 200:
                return float(r.json()["price"])
            if r.status_code == 400:
                return None
            logger.warning("[binance] %s %s HTTP %s", base, pair, r.status_code)
        except Exception as e:  # noqa: BLE001
            logger.warning("[binance] %s %s 실패: %s", base, pair, e)
    return None


def _try_bybit(pair: str, timeout: float) -> Optional[float]:
    """Bybit 공개 스팟 시세. 한국/미국 접근 가능."""
    try:
        r = requests.get(
            "https://api.bybit.com/v5/market/tickers",
            params={"category": "spot", "symbol": pair}, timeout=timeout,
        )
        if r.status_code != 200:
            logger.warning("[bybit] %s HTTP %s", pair, r.status_code)
            return None
        items = r.json().get("result", {}).get("list", [])
        if items:
            return float(items[0]["lastPrice"])
    except Exception as e:  # noqa: BLE001
        logger.warning("[bybit] %s 실패: %s", pair, e)
    return None


# CoinGecko 경유 Binance 펀딩비 캐시 — 엔드포인트가 심볼 단위가 아니라 거래소
# 전체(754 티커, ~630KB)를 주므로 한 회차의 다중 터치가 각각 재호출하지 않게
# TTL 캐시로 묶는다. 무료 keyless 레이트리밋(분당 한 자릿수)도 이걸로 방어.
_CG_FUNDING_TTL_SEC = 180.0
_cg_funding_cache: dict = {"ts": 0.0, "map": None}


def _coingecko_binance_funding_map(timeout: float) -> Optional[dict]:
    """CoinGecko /derivatives/exchanges/binance_futures → {심볼페어: 펀딩%}.
    Binance 원본 수치(소수 3자리 반올림)를 미국 IP 에서도 받을 수 있는 유일한
    무료 경로 (2026-08-07 — CoinGlass 무료 티어 폐지 확인 후 채택).
    실패 시 None — 호출부가 다음 폴백으로 넘어간다."""
    import time as _time
    now = _time.time()
    if (_cg_funding_cache["map"] is not None
            and now - _cg_funding_cache["ts"] < _CG_FUNDING_TTL_SEC):
        return _cg_funding_cache["map"]
    headers = {}
    try:
        from config import settings as _settings  # 지연 로드 — 순환 의존 방지
        key = _settings.secret("COINGECKO_API_KEY")
        if key:
            headers["x-cg-demo-api-key"] = key
    except Exception:  # noqa: BLE001 - 키는 선택사항, 설정 로드 실패는 keyless 진행
        logger.debug("[binance] CoinGecko API 키 로드 실패 - keyless 진행")
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/derivatives/exchanges/binance_futures",
            params={"include_tickers": "unexpired"}, headers=headers,
            timeout=timeout,
        )
        if r.status_code != 200:
            logger.warning("[funding] CoinGecko HTTP %s", r.status_code)
            return None
        out = {}
        for t in r.json().get("tickers") or []:
            sym = t.get("symbol")
            if not sym:
                continue
            row = {}
            for src, dst in (("funding_rate", "fr"),          # 이미 % 단위
                             ("open_interest_usd", "oi"),      # USD 명목
                             ("h24_percentage_change", "pchg")):  # 가격 24h %
                v = t.get(src)
                if v is not None:
                    try:
                        row[dst] = float(v)
                    except (ValueError, TypeError):
                        pass
            if row:
                out[sym] = row
        if out:
            _cg_funding_cache.update(ts=now, map=out)
            return out
    except Exception as e:  # noqa: BLE001
        logger.warning("[funding] CoinGecko 실패: %s", e)
    return None


def fetch_deriv_snapshot(symbol: str, timeout: float) -> Optional[dict]:
    """CoinGecko(Binance) 파생 스냅샷 {fr, oi, pchg} — 없는 필드는 키 부재.
    수급 판정(derive_supply_verdict)과 OI 스냅샷 적재의 단일 출처.
    실패/미상장 시 None."""
    m = _coingecko_binance_funding_map(timeout)
    if not m:
        return None
    return m.get(f"{symbol.upper()}USDT")


# 수급 판정 임계 (2026-08-07 사용자 결정: 펀딩+OI 를 결론 한 줄로 합성)
SUPPLY_FUNDING_HOT = 0.01    # % — 기존 펀딩 라벨과 동일 경계
SUPPLY_OI_MIN_PCT = 3.0      # OI 24h 변화 유의미 경계 (미만은 보합)
SUPPLY_PRICE_MIN_PCT = 1.0   # 가격 24h 방향 판별 경계
# CVD·호가비율 보정 경계 (2026-08-14 기획 확정 — 알림에 수치 비노출,
# 판정 라벨 이동에만 사용. 임계값 근거: taker 불균형 ±0.15 는 4h 누적으로
# 유의미한 쏠림, 호가 0.67/1.5 는 매도벽/매수벽 관례 경계(1:1.5 비율)).
SUPPLY_CVD_NEG = -0.15       # CVD 비율 이하 = 공격 매도 우위 (경고 신호)
SUPPLY_CVD_POS = 0.15        # CVD 비율 이상 = 공격 매수 우위 (확인 신호)
SUPPLY_OBI_SELL_WALL = 0.67  # 호가 매수/매도 잔량비 이하 = 매도벽
SUPPLY_OBI_BUY_WALL = 1.5    # 호가 매수/매도 잔량비 이상 = 매수벽
# 옵션·청산 보정 경계 (2026-08-14 기획 확정 — BTC 전용 시장 컨텍스트,
# 전 코인 알림에 적용. 수치 비노출, 판정 라벨 이동에만 사용).
SUPPLY_PC_EXTREME_HIGH = 1.0   # P/C ≥1.0 = 극단 풋 우위 (Deribit 3년 최고 0.84, >1.0은 공포 구간)
SUPPLY_PC_EXTREME_LOW = 0.30   # P/C ≤0.3 = 극단 콜 과열 (정상 0.5~0.6, 역대 최저 0.38)
SUPPLY_MAX_PAIN_DIST = 0.03    # Max Pain 거리(3%) 이상이면 보정 적용
SUPPLY_LIQ_WARN = "long_heavy"   # 하방 롱 청산 집중 = 경고
SUPPLY_LIQ_CONFIRM = "short_heavy"  # 상방 숏 청산 집중 = 확인


def derive_supply_verdict(funding_pct, oi_change_pct, price_change_pct,
                          cvd_ratio=None, bid_ask_ratio=None,
                          options_ctx=None, liq_ctx=None,
                          # 새 시장환경 입력 (2026-08-14)
                          dxy=None, usdt_dominance=None, dvol=None,
                          macro_event=None, hash_ribbons=None):
    """펀딩 쏠림 × (OI 증감 + 가격 방향) → 매수 관점 판정.

    반환 (label, reason|None) — label ∈ '우호'/'주의'/'중립', reason 은 괄호
    근거(최대 5자, 모바일 한 줄 유지). 입력 전부 None 이면 (None, None) —
    호출부가 행을 생략한다. OI/가격이 없으면 펀딩 단독 판정으로 폴백.

    CVD·호가·옵션·청산 보정: cvd_ratio, bid_ask_ratio, options_ctx(BTC 옵션
    P/C Ratio·Max Pain), liq_ctx(BTC 청산 클러스터)는 **라벨 이동에만** 반영하고
    근거 텍스트·수치는 알림에 노출하지 않는다(내부 기준선 원칙). 좋은 판정을 더
    올리는 것보다 나쁜 신호로 낮추는 쪽을 우선(허수 터치 경고가 목적):
      · 우호 + 경고신호 1개 이상 → 중립
      · 중립 + 경고신호 2개 → 주의
      · 중립 + 확인신호 2개 → 우호 (둘 다 갖춰야만 상향 — 보수 원칙)
    '주의'는 보정으로 좋아지지 않는다.

    options_ctx (2026-08-14): {"pc_ratio": float, "max_pain": float} 또는 None.
    liq_ctx (2026-08-14): {"pressure": float, "direction": str} 또는 None."""
    f_hot_long = funding_pct is not None and funding_pct > SUPPLY_FUNDING_HOT
    f_hot_short = funding_pct is not None and funding_pct < -SUPPLY_FUNDING_HOT

    label, reason = None, None
    oi_valid = oi_change_pct is not None and price_change_pct is not None
    if oi_valid and abs(oi_change_pct) >= SUPPLY_OI_MIN_PCT:
        oi_up = oi_change_pct > 0
        px_up = price_change_pct >= SUPPLY_PRICE_MIN_PCT
        px_down = price_change_pct <= -SUPPLY_PRICE_MIN_PCT
        # 근거 어휘 개편 (2026-08-14 사용자 확정 — 초보자 직관 어휘):
        # 숏 몰림→반등 연료, 숏 유입→하락 베팅, 롱 과열→추격 위험,
        # 청산 반등→속임 반등, 숏 과열→반등 여지. 라벨(우호/중립/주의)은
        # 불변 — 축적 통계(touch_supply_verdict) 라벨 연속성 유지.
        if oi_up and px_down:
            # 신규 숏 유입 — 숏 과열까지 겹치면 스퀴즈 연료
            label, reason = ("우호", "반등 연료") if f_hot_short else ("중립", "하락 베팅")
        elif oi_up and px_up:
            # 신규 매수 유입 — 롱 과열이면 추격 위험
            label, reason = ("주의", "추격 위험") if f_hot_long else ("우호", "자금 유입")
        elif (not oi_up) and px_up:
            label, reason = ("주의", "속임 반등")   # 새 돈 없는 반등
        elif (not oi_up) and px_down:
            label, reason = ("중립", "투매 진행")   # 롱 청산
        else:
            # 가격 보합(±1% 미만) + OI 유의미 변화 — 방향 판단 보류
            label, reason = ("중립", None)
    # OI 미확보/보합 → 펀딩 단독 폴백 (기존 라벨 의미 유지)
    elif f_hot_short:
        label, reason = "우호", "반등 여지"
    elif f_hot_long:
        label, reason = "주의", "추격 위험"
    elif funding_pct is not None:
        label, reason = "중립", None
    else:
        return None, None

    # ── CVD·호가 라벨 보정 (표기 없음 — 라벨만 이동, reason 유지) ──
    warn = 0
    confirm = 0
    if cvd_ratio is not None:
        if cvd_ratio <= SUPPLY_CVD_NEG:
            warn += 1
        elif cvd_ratio >= SUPPLY_CVD_POS:
            confirm += 1
    if bid_ask_ratio is not None:
        if bid_ask_ratio <= SUPPLY_OBI_SELL_WALL:
            warn += 1
        elif bid_ask_ratio >= SUPPLY_OBI_BUY_WALL:
            confirm += 1
    # 옵션 컨텍스트 보정 (BTC 전용 시장 컨텍스트 — 전 코인 적용)
    if options_ctx is not None:
        pc = options_ctx.get("pc_ratio")
        mp = options_ctx.get("max_pain")
        if pc is not None:
            if pc >= SUPPLY_PC_EXTREME_HIGH or pc <= SUPPLY_PC_EXTREME_LOW:
                warn += 1
        if mp is not None and funding_pct is not None:
            # Max Pain 대비 현재 BTC 가격 거리는 호출부가 제공할 수 없으므로
            # P/C Ratio 극단만 보정에 사용 (Max Pain은 향후 가격 입력 추가 시 활용)
            pass
    # 청산 클러스터 보정 (BTC 전용 시장 컨텍스트 — 전 코인 적용)
    if liq_ctx is not None:
        liq_dir = liq_ctx.get("direction")
        if liq_dir == SUPPLY_LIQ_WARN:
            warn += 1
        elif liq_dir == SUPPLY_LIQ_CONFIRM:
            confirm += 1
    # ── 매크로 환경 보정 (2026-08-14) ──────────────────────────
    # DXY > 105 = 달러 강세 → 코인 약세 압력 (warn)
    # DXY < 100 = 달러 약세 → 코인 우호 (confirm)
    if dxy is not None:
        if dxy > 105:
            warn += 1
        elif dxy < 100:
            confirm += 1

    # USDT.D > 8% = 위험회피 심화 (warn)
    # USDT.D < 5% = 위험선호 (confirm)
    if usdt_dominance is not None:
        if usdt_dominance > 8:
            warn += 1
        elif usdt_dominance < 5:
            confirm += 1

    # DVOL > 80 = 변동성 위기 (warn, 강하게)
    # DVOL > 60 = 변동성 경계 (warn)
    # DVOL < 40 = 평상시 — 신호 없음
    if dvol is not None:
        if dvol > 80:
            warn += 2
        elif dvol > 60:
            warn += 1

    # FOMC/CPI 24h 이내: 경고(warn) — 고영향 이벤트 전후 방향성 불확실
    if macro_event is not None:
        warn += 1

    # Hash Ribbons (2026-08-15) — 채굴자 항복=스트레스 구간(warn),
    # 회복 크로스 후 14일=역사적 매집 구간(confirm)
    if hash_ribbons is not None:
        state = hash_ribbons.get("state")
        if state == "capitulation":
            warn += 1
        elif state == "recovery":
            confirm += 1

    if label == "우호" and warn >= 1:
        label = "중립"
    elif label == "중립" and warn >= 2:
        label = "주의"
    elif label == "중립" and confirm >= 2:
        label = "우호"
    return label, reason


def fetch_funding_rate(symbol: str, timeout: float) -> Optional[float]:
    """선물 펀딩비율(%). Binance 직접 → CoinGecko(Binance 원본) → Bybit → OKX.
    양수=롱 과열, 음수=숏 과열. 전 경로 실패 시 None(알림에서 행 생략).

    2026-08-07 폴백 체인 확장: fapi.binance.com 과 Bybit linear 는 미국 IP
    지역 차단(451/403) — GitHub Actions 러너(미국 Azure)에서 막혀 프로덕션
    알림에 펀딩 행이 항상 생략되던 문제. 2차 CoinGecko 는 Binance 원본 수치를
    미국에서도 주므로(거래량 대표성 — 사용자 결정) 프로덕션 주 경로가 되고,
    Bybit/OKX 는 최후 폴백. 비-200 응답에도 경고를 남긴다."""
    pair = f"{symbol.upper()}USDT"
    # Binance Futures
    try:
        r = requests.get(
            "https://fapi.binance.com/fapi/v1/premiumIndex",
            params={"symbol": pair}, timeout=timeout,
        )
        if r.status_code == 200:
            rate = r.json().get("lastFundingRate")
            if rate is not None:
                return float(rate) * 100  # 0.0001 → 0.01%
        else:
            logger.warning("[funding] Binance %s HTTP %s", pair, r.status_code)
    except Exception as e:  # noqa: BLE001
        logger.warning("[funding] Binance %s 실패: %s", pair, e)
    # CoinGecko 경유 Binance 원본 (프로덕션 주 경로 — 위 직접 호출은 미국 차단)
    _cg_map = _coingecko_binance_funding_map(timeout)
    if _cg_map is not None:
        rate = (_cg_map.get(pair) or {}).get("fr")
        if rate is not None:
            return rate
        # Binance 미상장 심볼 — Bybit/OKX 로 계속 폴백
    # Bybit Linear
    try:
        r = requests.get(
            "https://api.bybit.com/v5/market/tickers",
            params={"category": "linear", "symbol": pair}, timeout=timeout,
        )
        if r.status_code == 200:
            items = r.json().get("result", {}).get("list", [])
            if items:
                rate = items[0].get("fundingRate")
                if rate is not None:
                    return float(rate) * 100
        else:
            logger.warning("[funding] Bybit %s HTTP %s", pair, r.status_code)
    except Exception as e:  # noqa: BLE001
        logger.warning("[funding] Bybit %s 실패: %s", pair, e)
    # OKX Swap (미상장 심볼은 51001 코드 200 응답 — data 비어 폴백 없이 None)
    try:
        r = requests.get(
            "https://www.okx.com/api/v5/public/funding-rate",
            params={"instId": f"{symbol.upper()}-USDT-SWAP"}, timeout=timeout,
        )
        if r.status_code == 200:
            data = r.json().get("data") or []
            if data:
                rate = data[0].get("fundingRate")
                if rate is not None:
                    return float(rate) * 100
        else:
            logger.warning("[funding] OKX %s HTTP %s", pair, r.status_code)
    except Exception as e:  # noqa: BLE001
        logger.warning("[funding] OKX %s 실패: %s", pair, e)
    return None


def fetch_funding_history(symbol: str, timeout: float,
                          days: int = 32) -> Optional[list]:
    """선물 펀딩비율 히스토리(오래된 순 float 리스트, 단위 % — 원시 decimal *100).
    Binance Futures → Bybit Linear → OKX 폴백. 인증 불필요.
    days: 조회할 일수 (기본 32일 — 30일 지속 음수 감지에 여유 2일).
    반환: None(전 경로 실패) 또는 시간순 정렬된 %값 리스트.
    Binance/Bybit 모두 펀딩 간격 8h → days*3 개 요청, 최대 1000/200 상한.
    OKX 는 페이지당 100 상한 — 기본 32일(99개)은 1페이지로 충분
    (2026-08-07 미국 IP 차단 대응, fetch_funding_rate 주석 참고).
    주의: days>32 를 넘기면 Bybit(66일)/OKX(33일) 폴백 경로는 요청보다 짧은
    리스트를 조용히 반환할 수 있다 — detect_funding_regime_flip 은 표본 부족 시
    None(배지 생략)이라 안전하지만, 새 호출부는 이 상한을 알고 써야 한다."""
    pair = f"{symbol.upper()}USDT"
    n_needed = min(days * 3 + 3, 1000)  # 8h 간격 + 여유
    # Binance Futures /fapi/v1/fundingRate 는 limit 최대 1000
    try:
        r = requests.get(
            "https://fapi.binance.com/fapi/v1/fundingRate",
            params={"symbol": pair, "limit": n_needed}, timeout=timeout,
        )
        if r.status_code == 200:
            items = r.json()
            if isinstance(items, list) and items:
                # 응답은 시간순(과거→최근). fundingRate 문자열 원시 decimal.
                out = []
                for it in items:
                    v = it.get("fundingRate")
                    if v is None:
                        continue
                    try:
                        out.append(float(v) * 100)
                    except (ValueError, TypeError):
                        continue
                if out:
                    return out
    except Exception as e:  # noqa: BLE001
        logger.warning("[funding-hist] Binance %s 실패: %s", pair, e)
    # Bybit V5 /v5/market/funding/history — limit 최대 200 (약 66일치)
    try:
        r = requests.get(
            "https://api.bybit.com/v5/market/funding/history",
            params={"category": "linear", "symbol": pair,
                    "limit": min(n_needed, 200)},
            timeout=timeout,
        )
        if r.status_code == 200:
            items = r.json().get("result", {}).get("list", [])
            if items:
                # Bybit 응답은 최신→과거 순 → 뒤집어서 시간순으로.
                out = []
                for it in reversed(items):
                    v = it.get("fundingRate")
                    if v is None:
                        continue
                    try:
                        out.append(float(v) * 100)
                    except (ValueError, TypeError):
                        continue
                if out:
                    return out
    except Exception as e:  # noqa: BLE001
        logger.warning("[funding-hist] Bybit %s 실패: %s", pair, e)
    # OKX /api/v5/public/funding-rate-history — limit 최대 100 (약 33일치)
    try:
        r = requests.get(
            "https://www.okx.com/api/v5/public/funding-rate-history",
            params={"instId": f"{symbol.upper()}-USDT-SWAP",
                    "limit": min(n_needed, 100)},
            timeout=timeout,
        )
        if r.status_code == 200:
            items = r.json().get("data") or []
            if items:
                # OKX 응답은 최신→과거 순 → 뒤집어서 시간순으로.
                out = []
                for it in reversed(items):
                    v = it.get("fundingRate")
                    if v is None:
                        continue
                    try:
                        out.append(float(v) * 100)
                    except (ValueError, TypeError):
                        continue
                if out:
                    return out
        else:
            logger.warning("[funding-hist] OKX %s HTTP %s", pair, r.status_code)
    except Exception as e:  # noqa: BLE001
        logger.warning("[funding-hist] OKX %s 실패: %s", pair, e)
    return None


def detect_funding_regime_flip(history: Optional[list],
                               min_neg_days: int = 30) -> Optional[dict]:
    """펀딩 레짐 전환 감지 — min_neg_days 일 이상 지속 음수 후 최근 양수 플립이면
    {"flipped": True, "neg_days": N, "latest": float} 반환, 아니면 None.

    "지속 음수"는 8h 간격 기준으로 min_neg_days*3 개 연속 <=0 상태를 의미하고
    (경계 0 포함 — 미미한 음수/제로도 롱 편향 없음), 최근 값은 양수(>0)여야 한다.
    사용자 결정(2026-08-03 질문카드): 임계 30일(리서치 정설).

    실패 시(히스토리 부재, 표본 부족) None — 알림 렌더에서 배지 생략."""
    if not history or len(history) < min_neg_days * 3 + 1:
        return None
    latest = history[-1]
    if latest <= 0:
        return None  # 최근이 양수여야 플립
    # 최근값 직전까지 min_neg_days*3 개가 모두 <=0
    window = history[-(min_neg_days * 3 + 1):-1]
    if not all(v <= 0 for v in window):
        return None
    # 전부 0(또는 사실상 0)인 구간은 "하락 편향"이 아니라 무편향 — 실제 음수가
    # 있어야 플립으로 인정 (2026-08-04 R2 감사: [0.0]*90+[0.001] false positive)
    if min(window) >= 0:
        return None
    # 플립 확정 — 실제 연속 음수 구간 길이(일수)를 정확히 세어 표기 정확도 향상.
    neg_count = 0
    for v in reversed(history[:-1]):
        if v <= 0:
            neg_count += 1
        else:
            break
    return {"flipped": True, "neg_days": neg_count / 3.0, "latest": latest}


def fetch_cvd_ratio(symbol: str, timeout: float, hours: int = 4) -> Optional[float]:
    """CVD(누적 거래량 델타) 비율 — 최근 N시간 taker 매수-매도 불균형 (2026-08-11).

    축적(touch_cvd_ratio 컬럼) + 수급 판정 보정 입력(2026-08-14 승격).
    수치 자체는 알림·필터·등급에 노출하지 않고 derive_supply_verdict 의
    라벨 이동에만 관여한다. outcome 상관 사후 분석용 원천 데이터 역할은
    유지(touch_bid_ask_ratio·touch_ma200_above 와 동일 성격).

    계산: aggTrades 페이징 대신 klines 1콜 — 캔들의 takerBuyBaseVolume(인덱스 9)과
    총 volume(인덱스 5)으로 캔들별 delta = 2×takerBuy − total 을 합산, 총량으로
    정규화해 [-1, +1] 비율로 돌려준다. +1 = 전량 taker 매수(공격 매수 우위),
    -1 = 전량 taker 매도. 미상장(400)·전 경로 실패·거래량 0 → None(행만 미기록).
    """
    pair = f"{symbol.upper()}USDT"
    limit = hours * 4  # 15분봉 × 4/h
    for base in _BINANCE_ENDPOINTS:
        try:
            r = requests.get(
                f"{base}/api/v3/klines",
                params={"symbol": pair, "interval": "15m", "limit": limit},
                timeout=timeout,
            )
            if r.status_code == 400:
                return None  # 미상장
            if r.status_code != 200:
                logger.warning("[binance] cvd %s %s HTTP %s", base, pair, r.status_code)
                continue
            candles = r.json()
            if not isinstance(candles, list):
                continue
            total = sum(float(c[5]) for c in candles)
            if total <= 0:
                return None
            taker_buy = sum(float(c[9]) for c in candles)
            return (2 * taker_buy - total) / total
        except Exception as e:  # noqa: BLE001
            logger.warning("[binance] cvd %s %s 실패: %s", base, pair, e)
    return None


_FAPI_BASE = "https://fapi.binance.com"


def _fetch_fapi_ratio(endpoint: str, symbol: str, field: str,
                      timeout: float, label: str) -> Optional[float]:
    """Binance FAPI ratio 공통 헬퍼. 미상장/실패 → None."""
    pair = f"{symbol.upper()}USDT"
    try:
        r = requests.get(
            f"{_FAPI_BASE}/futures/data/{endpoint}",
            params={"symbol": pair, "period": "4h", "limit": 1},
            timeout=timeout,
        )
        if r.status_code != 200:
            return None
        data = r.json()
        return float(data[0][field]) if data else None
    except Exception as e:  # noqa: BLE001
        logger.warning("[binance] %s %s 실패: %s", label, pair, e)
        return None


def fetch_long_short_ratio(symbol: str, timeout: float) -> Optional[float]:
    """전체 계정 롱/숏 비율 — 최근 4h 평균. 미상장/실패 → None."""
    return _fetch_fapi_ratio("globalLongShortAccountRatio", symbol,
                             "longAccount", timeout, "longShort")


def fetch_top_trader_position_ratio(symbol: str, timeout: float) -> Optional[float]:
    """상위 트레이더 롱 비율 — 최근 4h. 미상장/실패 → None."""
    return _fetch_fapi_ratio("topLongShortPositionRatio", symbol,
                             "longAccount", timeout, "topTrader")


def fetch_taker_buy_sell_ratio(symbol: str, timeout: float) -> Optional[float]:
    """선물 테이커 매수/매도 비율 — 최근 4h. >1 매수 우위, <1 매도 우위. 미상장/실패 → None."""
    return _fetch_fapi_ratio("takerlongshortRatio", symbol,
                             "buySellRatio", timeout, "takerRatio")


def fetch_usdt_price(symbol: str, timeout: float) -> Optional[float]:
    """코인의 USDT 페어 현재가. 전 경로 실패 시 None
    (김프 줄만 생략됨 — 알림 발송은 계속된다)."""
    pair = f"{symbol.upper()}USDT"
    price = _try_binance(pair, timeout)
    if price is not None:
        return price
    price = _try_bybit(pair, timeout)
    if price is not None:
        logger.info("[bybit] %s 폴백 성공: %s", pair, price)
    return price
