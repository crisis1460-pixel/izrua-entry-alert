"""
업비트 공개(quotation) REST — 인증 불필요. 이 봇이 업비트 개인 API 키를 쓰지 않는 것은
보안 설계다(README) — 여기에 인증을 추가하지 말 것.

한도: 시세 REST 초당 10회(IP). 배치 ticker 는 1콜, 캔들은 마켓당 1콜이라
candle 호출만 페이싱(0.12s)한다.

2026-07-26 실측: 레이트리밋 그룹은 엔드포인트별로 분리돼 있다
(`Remaining-Req` 헤더: ticker=`group=ticker`, 캔들=`group=candles`,
체결내역=`group=crix-trades`, 각각 min=600; sec=10). 즉 아래 fetch_trades_window 는
캔들/시세 예산을 전혀 갉아먹지 않는다.
"""

import logging
import time
from datetime import datetime, timezone
from typing import Optional

import requests

logger = logging.getLogger("alert.upbit")

_BASE = "https://api.upbit.com/v1"
_CANDLE_PACE_SEC = 0.12  # 초당 ~8콜 (한도 10의 80%)


def fetch_prices(markets: list, timeout: float) -> dict:
    """여러 마켓 현재가를 1콜로. 반환 {market: price}. 실패 시 {}."""
    if not markets:
        return {}
    try:
        resp = requests.get(
            f"{_BASE}/ticker", params={"markets": ",".join(markets)}, timeout=timeout
        )
        resp.raise_for_status()
        return {t["market"]: float(t["trade_price"]) for t in resp.json()}
    except Exception as e:  # noqa: BLE001
        logger.warning("[upbit] 현재가 조회 실패: %s", e)
        return {}


def fetch_volume_ranks(timeout: float) -> dict:
    """업비트 KRW 전 마켓의 24h 거래대금 순위. 반환 {market: rank(1부터)}. 실패 시 {}.
    알림 발송 시점에만 호출(2콜: 마켓목록 + 배치 ticker) — 조회 시점 기준 순위."""
    try:
        resp = requests.get(f"{_BASE}/market/all", params={"isDetails": "false"}, timeout=timeout)
        resp.raise_for_status()
        markets = [m["market"] for m in resp.json() if m["market"].startswith("KRW-")]
        resp = requests.get(f"{_BASE}/ticker", params={"markets": ",".join(markets)}, timeout=timeout)
        resp.raise_for_status()
        vols = [(t["market"], float(t.get("acc_trade_price_24h") or 0)) for t in resp.json()]
        vols.sort(key=lambda x: x[1], reverse=True)
        return {market: i + 1 for i, (market, _) in enumerate(vols)}
    except Exception as e:  # noqa: BLE001
        logger.warning("[upbit] 거래량 순위 조회 실패: %s", e)
        return {}


_ORDERBOOK_PACE_SEC = 0.12  # 초당 ~8콜 (한도 10의 80%) — 실측 헤더상 별도 그룹


def fetch_volume_data(market: str, timeout: float) -> Optional[dict]:
    """현재 24h 롤링 거래대금 + 최근 7일 일평균 거래대금 (KRW).
    반환: {'current_24h': float, 'avg_7d': float} | None.
    2콜: /v1/ticker + /v1/candles/days — 거래량 급증 감시(Feature 4) 전용."""
    try:
        resp = requests.get(f"{_BASE}/ticker", params={"markets": market}, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        if not data:
            return None
        current_24h = float(data[0].get("acc_trade_price_24h") or 0)
    except Exception as e:  # noqa: BLE001
        logger.warning("[upbit] %s 24h 거래대금 조회 실패: %s", market, e)
        return None

    try:
        resp = requests.get(
            f"{_BASE}/candles/days", params={"market": market, "count": 8}, timeout=timeout)
        resp.raise_for_status()
        candles = resp.json()
        time.sleep(_CANDLE_PACE_SEC)
        # 오늘(진행 중) 캔들을 제외하고 이전 7일 평균
        past = candles[1:8] if len(candles) > 1 else candles
        if not past:
            return None
        avg_7d = sum(float(c.get("candle_acc_trade_price") or 0) for c in past) / len(past)
    except Exception as e:  # noqa: BLE001
        logger.warning("[upbit] %s 7일 일봉 조회 실패: %s", market, e)
        return None

    return {"current_24h": current_24h, "avg_7d": avg_7d}


def fetch_orderbook_ratio(market: str, timeout: float) -> Optional[float]:
    """호가 매수/매도 잔량비 = total_bid_size / total_ask_size. 실패 시 None.

    왜 ticker 가 아니라 호가인가 (2026-07-26 실증): 기획 카드 #18 은 REST
    /v1/ticker 의 acc_bid_volume/acc_ask_volume 를 재활용해 **API 호출 0회 증가**로
    체결강도를 얻자는 안이었는데, 무인증 실측 결과 그 두 필드는 REST ticker 응답에
    아예 없다(웹소켓 ticker 전용). 확인된 응답 키는 acc_trade_price/volume(_24h) 계열
    뿐이라 매수·매도를 가를 방법이 없다 → 폴백 카드 #19 채택.

    비용: 터치 확정 시에만 1콜. 터치는 레벨당 생애 1회뿐이라 호출량이 극소하고,
    실측 `Remaining-Req` 헤더가 `group=orderbook; min=600; sec=9` 로 ticker/캔들과
    레이트리밋 그룹이 분리돼 있어 판정용 예산을 전혀 갉아먹지 않는다.

    **기록 전용** — 이 값은 알림 본문·필터·등급 어디에도 반영하지 않는다."""
    try:
        resp = requests.get(
            f"{_BASE}/orderbook", params={"markets": market}, timeout=timeout
        )
        resp.raise_for_status()
        data = resp.json()
        if not data:
            return None
        book = data[0]
        bid = float(book.get("total_bid_size") or 0)
        ask = float(book.get("total_ask_size") or 0)
        time.sleep(_ORDERBOOK_PACE_SEC)
        if ask <= 0 or bid <= 0:
            return None     # 한쪽 잔량이 0 이면 비율이 무의미(또는 0 나눗셈)
        return bid / ask
    except Exception as e:  # noqa: BLE001 - 관찰 기록 실패가 터치 처리를 막으면 안 됨
        logger.warning("[upbit] %s 호가 조회 실패: %s", market, e)
        return None


def fetch_week52(market: str, timeout: float) -> Optional[tuple]:
    """52주 고가/저가 (KRW) — 주봉 52개의 최고 high / 최저 low. 실패 시 None.
    알림 발송 시에만 호출(회당 1콜)되므로 한도 부담 없음."""
    try:
        resp = requests.get(
            f"{_BASE}/candles/weeks",
            params={"market": market, "count": 52},
            timeout=timeout,
        )
        resp.raise_for_status()
        candles = resp.json()
        if not candles:
            return None
        high = max(float(c["high_price"]) for c in candles)
        low = min(float(c["low_price"]) for c in candles)
        time.sleep(_CANDLE_PACE_SEC)
        return (high, low)
    except Exception as e:  # noqa: BLE001
        logger.warning("[upbit] %s 52주 조회 실패: %s", market, e)
        return None


_RANGE_MAX_PAGES = 3  # 안전판 — 정상 유동성 마켓은 1페이지(1콜)로 끝난다


def fetch_range_since(market: str, minutes: int, timeout: float) -> Optional[list]:
    """최근 minutes 분간의 분봉 목록 [(시작epoch, 종료epoch, high, low), ...] 시간 오름차순.

    2026-07-24 감사 수정: 예전엔 max(high)/min(low)로 뭉개서 반환했는데, 그러면
    ① 터치 이전 가격이 적중판정에 섞이고(가짜 hit) ② TP→SL 도달 순서를 알 수 있는
    경우까지 전부 '동시터치 miss'로 떨어졌다(승률 하향 편향). 캔들 목록을 그대로
    반환해 호출부가 시간순으로 판정하게 한다.

    2026-07-26 재감사 #9 근본 수정: 예전엔 "개수"(count) 기반 요청이라, 무거래 분에는
    캔들이 생성되지 않는 업비트 API 특성상 저유동성 마켓에서 count개가 의도한 minutes
    보다 훨씬 긴 과거까지 덮었다(실측: 추적 유니버스 81종 중 60종이 45분 요청에 1.5배
    이상 확대, SUN 31시간·SYRUP 24시간 등). 이제는 목표 시각(target_start = 요청 시각
    - minutes분) 이후 캔들만 남기는 시간창 필터를 건다.

    count = ceil(minutes/unit) 는 1분봉 하나가 최대 1분을 담당하므로 '목표 구간 안에
    존재할 수 있는 실제 캔들 개수의 이론적 상한'이다 — 즉 유동성이 얼마나 낮든, 목표
    구간 안의 캔들은 전부 이 count 안에 들어온다(더 오래된 캔들이 그 자리를 대신
    차지할 뿐). 그래서 정상적으로는 추가 페이지 없이 1콜 + 시간 필터만으로 끝난다.
    극단적으로 count 가 부족한 경우(이론상 거의 불가능하지만 방어적으로)에만
    scripts/repair_rejudge_20260726.py 의 fetch_candles() 와 같은 방식으로 'to' 를
    당겨가며 최대 _RANGE_MAX_PAGES 까지만 추가 조회한다.

    소급 창이 200분을 넘으면(봇 다운타임) 15분봉으로 폴백해 최대 50시간까지 커버."""
    unit = 1 if minutes <= 200 else 15
    count = max(1, min(200, (minutes + unit - 1) // unit))
    now_ts = datetime.now(timezone.utc).timestamp()
    target_start = now_ts - minutes * 60

    out: dict = {}
    to_param = None
    for _ in range(_RANGE_MAX_PAGES):
        params = {"market": market, "count": count}
        if to_param:
            params["to"] = to_param
        try:
            resp = requests.get(
                f"{_BASE}/candles/minutes/{unit}", params=params, timeout=timeout,
            )
            resp.raise_for_status()
            raw = resp.json()
        except Exception as e:  # noqa: BLE001
            logger.warning("[upbit] %s 분봉 조회 실패: %s", market, e)
            time.sleep(_CANDLE_PACE_SEC)
            break
        time.sleep(_CANDLE_PACE_SEC)
        if not raw:
            break

        oldest_start = None
        for c in raw:
            start = datetime.fromisoformat(c["candle_date_time_utc"]).replace(
                tzinfo=timezone.utc).timestamp()
            oldest_start = start if oldest_start is None else min(oldest_start, start)
            if start >= target_start:
                out[start] = (start, start + unit * 60,
                              float(c["high_price"]), float(c["low_price"]))

        if oldest_start is None or oldest_start <= target_start:
            break  # 이번 페이지가 이미 목표 시각까지 닿았다 - 추가 페이지 불필요
        to_param = datetime.fromtimestamp(oldest_start, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ")

    if not out:
        return None
    return sorted(out.values())


# ── 체결내역(trades) — 동시터치 재검사(Bar Magnifier) 전용 ─────────────────
# 업비트 공개 API의 최소 캔들 단위가 1분이라 "1분봉을 더 쪼갠 봉"은 존재하지 않는다.
# 대신 개별 체결 틱(/v1/trades/ticks)이 그 1분 안의 실제 시간순서를 담고 있어,
# 이것이 사실상의 최하위 타임프레임 역할을 한다.
#
# 2026-07-26 실측으로 확인한 제약:
#   - count 최대 500 (501 요청해도 500으로 절삭)
#   - 과거 조회는 `to`(HH:MM:SS, UTC) + `daysAgo`(1~7). daysAgo=8 은 400 에러 →
#     **조회 가능 과거는 최대 7일**. 우리 재검사는 터치 직후(수 분 내) 돌므로 무관.
#   - 페이지네이션은 `cursor`(직전 페이지 최고(最古) sequential_id)로 이어붙는다.
#     daysAgo 와 함께 넘기면 과거 구간에서도 정상 동작(실측 확인).
#     `to` 를 당겨가며 페이징하는 방식은 초 단위라 같은 초의 체결이 유실되므로 쓰지 않는다.
_TRADES_URL = _BASE + "/trades/ticks"
_TRADES_PAGE = 500          # API 상한
_TRADES_MAX_DAYS_AGO = 7    # API 상한 (실측: 8 은 400)
_TRADE_PACE_SEC = 0.12      # 초당 ~8콜 (한도 10의 80%) — 캔들과 별도 그룹


def fetch_trades_window(market: str, start_ts: float, end_ts: float,
                        timeout: float, max_pages: int = 4) -> Optional[list]:
    """[start_ts, end_ts) 구간의 개별 체결을 시간 오름차순 [(epoch, price), ...] 로.

    **구간 전체를 덮지 못하면 None** — 부분 데이터로 도달 '순서'를 단정하면
    보수적 판정보다 나쁜(틀린) 결론이 나올 수 있기 때문이다. 호출부는 None 을
    "판별 불가 → 기존 보수적 처리 유지"로 다뤄야 한다.

    같은 밀리초 timestamp 가 흔하므로 정렬 키에 sequential_id 를 병기한다
    (거래소가 부여하는 단조 증가 체결 일련번호 = 진짜 체결 순서)."""
    if end_ts <= start_ts:
        return None
    # `to` 는 해당 '초' 직전까지를 뜻하는 것으로 관측된다(경계초 유실 방지를 위해
    # 1초 더 넉넉히 요청하고, 실제 구간 필터는 아래 timestamp 비교로 건다).
    to_dt = datetime.fromtimestamp(end_ts + 1, tz=timezone.utc)
    days_ago = (datetime.now(timezone.utc).date() - to_dt.date()).days
    if days_ago < 0 or days_ago > _TRADES_MAX_DAYS_AGO:
        logger.info("[upbit] %s 체결내역 조회 범위 밖(%d일 전) - 재검사 불가", market, days_ago)
        return None

    base = {"market": market, "count": _TRADES_PAGE}
    if days_ago > 0:
        base["daysAgo"] = days_ago
    params = dict(base, to=to_dt.strftime("%H:%M:%S"))

    seen: dict = {}     # sequential_id → (ts, sid, price) — 페이지 경계 중복 제거
    covered = False
    for _ in range(max(1, max_pages)):
        try:
            resp = requests.get(_TRADES_URL, params=params, timeout=timeout)
            resp.raise_for_status()
            raw = resp.json()
        except Exception as e:  # noqa: BLE001
            logger.warning("[upbit] %s 체결내역 조회 실패: %s", market, e)
            time.sleep(_TRADE_PACE_SEC)
            return None
        time.sleep(_TRADE_PACE_SEC)
        if not raw:
            break

        oldest_ts = None
        for t in raw:
            ts = float(t["timestamp"]) / 1000.0
            oldest_ts = ts if oldest_ts is None else min(oldest_ts, ts)
            if start_ts <= ts < end_ts:
                sid = t["sequential_id"]
                seen[sid] = (ts, sid, float(t["trade_price"]))
        if oldest_ts is not None and oldest_ts <= start_ts:
            covered = True      # 구간 시작보다 더 과거까지 닿음 - 완전 커버
            break
        if len(raw) < _TRADES_PAGE:
            covered = True      # 더 오래된 체결이 없음(저유동성) - 있는 건 다 받음
            break
        params = dict(base, cursor=raw[-1]["sequential_id"])

    if not covered or not seen:
        return None
    return [(ts, price) for ts, _sid, price in sorted(seen.values())]
