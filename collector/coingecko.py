"""
유니버스 = CoinGecko 시총 top-N ∩ 업비트 KRW 마켓.

- CoinGecko /coins/markets 로 top-N + 순위 + 심볼 확보 (2026-08-08 top_n=300부터
  다중 페이지 — per_page 상한 250이라 1콜로는 250까지만, 그 이상은 자동 페이징).
- 업비트 KRW 마켓 목록(pyupbit 없이 공개 REST) 과 교집합.
- 시총 순위 → 등급 아이콘(💎🥇🥈🥉) 매핑.
- 일 1회 캐시(cache/universe.json) — 재실행 시 24h 이내면 API 안 부름.

CoinGecko Demo 키는 env(COINGECKO_API_KEY)에서 읽는다. 키가 없으면 keyless public
엔드포인트로 폴백(속도제한 빡세지만 일 1회라 대개 통과).

2026-07-26 수리: fetch_top_coins/fetch_upbit_krw_symbols 가 raise_for_status() 만
믿고 예외처리가 전혀 없어, 네트워크 오류·레이트리밋 한 번이면 build_universe() 가
예외를 던지며 수집 회차 전체를 죽였다. 이제 요청 단계 예외는 로그로 남기고 상위
(build_universe)로 전파해, 신선 캐시가 없어도 만료된 캐시가 있으면 그걸로 폴백한다
(완전 실패보다 낡은 유니버스가 낫다 - 어차피 다음 회차에 다시 시도됨).
"""

import json
import logging
import time
from pathlib import Path
from typing import Optional

import requests

from config import settings

BINANCE_EXCHANGE_INFO_URL = "https://api.binance.com/api/v3/exchangeInfo"

logger = logging.getLogger("alert.coingecko")

CG_MARKETS_URL = "https://api.coingecko.com/api/v3/coins/markets"
UPBIT_MARKET_URL = "https://api.upbit.com/v1/market/all"

# 스테이블코인은 $1 고정이라 엔트리 터치 알림이 무의미 → 유니버스에서 제외.
STABLECOINS = {
    "USDT", "USDC", "USDS", "DAI", "TUSD", "USDD", "FDUSD", "PYUSD", "BUSD",
    "GUSD", "USDP", "FRAX", "USDe", "USDE", "LUSD", "SUSD", "USDL", "RLUSD",
}


def _mcap_tier(rank: int) -> tuple:
    """순위 → (아이콘, 라벨). 경계는 settings.mcap_tiers.
    2026-08-08: mcap_tiers 최상단(현재 200)을 넘는 순위는 유니버스
    universe_top_n(현재 300) 안이어도 아이콘 없이 여기로 온다 — "순위밖"은
    "아이콘표 밖"이라는 뜻이지 "유니버스 밖"이 아니다(현재 아무 코드도 이
    라벨 텍스트를 읽지 않지만, 혼동 방지를 위해 명시)."""
    for upper, icon, label in settings.get("mcap_tiers"):
        if rank <= upper:
            return icon, label
    return "·", "아이콘표밖(유니버스 안)"


def fetch_top_coins(top_n: int, timeout: float) -> list:
    """CoinGecko 시총 상위 코인. 반환: [{symbol, rank, name, price_usd, tier_icon}, ...]

    2026-08-08 페이지네이션: CoinGecko per_page 상한이 250이라 top_n>250 은
    다중 페이지가 필요하다(유니버스 200→300 단계 시험, 사용자 결정). 종전
    코드는 min(250, top_n) 1페이지만 불러 300을 요청해도 조용히 250에서
    잘렸다. 일 1회 캐시 갱신 경로라 페이지 2~3콜 추가는 한도 부담 없음."""
    key = settings.secret("COINGECKO_API_KEY")
    headers = {}
    if key:
        # Demo 키는 헤더로 전달 (공식 권장). Pro 키와 엔드포인트가 다르지만 Demo 는 이 헤더 사용.
        headers["x-cg-demo-api-key"] = key
    coins = []
    page = 1
    while len(coins) < top_n:
        if page > 1:
            time.sleep(1.2)  # keyless 레이트리밋 방어 — 페이지 사이에만, 일 1회 경로라 무부담
        params = {
            "vs_currency": "usd",
            "order": "market_cap_desc",
            # per_page 는 페이지 간 반드시 고정 — 가변이면 오프셋(page×per_page)이
            # 어긋나 2페이지가 51~100위를 중복 반환한다(2026-08-08 라이브 검증서
            # 실측된 버그: min(250, 잔여) 로 줄였더니 마지막 코인 rank=101).
            # 초과 수신분은 아래 len(coins)>=top_n 가드가 자른다.
            "per_page": 250,
            "page": page,
            "sparkline": "false",
        }
        try:
            resp = requests.get(CG_MARKETS_URL, params=params, headers=headers, timeout=timeout)
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as e:
            logger.warning("[cg] top coins 조회 실패(page %d): %s", page, e)
            if coins:
                # 부분 성공 — 이미 받은 앞 페이지는 살린다(빈 유니버스보다 낫다).
                # 캐시가 다음날 갱신을 재시도한다.
                break
            raise
        if not data:
            break  # 빈 응답 = 마지막 페이지 도달
        for c in data:
            if len(coins) >= top_n:
                break
            rank = c.get("market_cap_rank")
            if rank is None:
                continue
            icon, _label = _mcap_tier(rank)
            coins.append({
                "symbol": (c.get("symbol") or "").upper(),
                "rank": rank,
                "name": c.get("name"),
                "price_usd": c.get("current_price"),
                "tier_icon": icon,
                "total_volume": c.get("total_volume"),
                "market_cap": c.get("market_cap"),
            })
        if len(data) < 250:
            break  # per_page 미만 응답 = 데이터 소진(마지막 페이지) — 다음 페이지 무의미
        page += 1
    return coins


def fetch_upbit_krw_symbols(timeout: float) -> set:
    """업비트 KRW 마켓의 코인 심볼 집합. 예: {'BTC','ETH','LINK',...} (인증 불필요)."""
    try:
        resp = requests.get(UPBIT_MARKET_URL, params={"isDetails": "false"}, timeout=timeout)
        resp.raise_for_status()
        payload = resp.json()
    except requests.RequestException as e:
        logger.warning("[cg] 업비트 마켓 조회 실패: %s", e)
        raise
    symbols = set()
    for m in payload:
        market = m.get("market", "")
        if market.startswith("KRW-"):
            symbols.add(market.split("-", 1)[1])
    return symbols


def fetch_upbit_warning_symbols(timeout: float) -> set:
    """업비트 투자경고(market_event.warning=True) 심볼 집합. isDetails=true 필요."""
    try:
        resp = requests.get(UPBIT_MARKET_URL, params={"isDetails": "true"}, timeout=timeout)
        resp.raise_for_status()
        payload = resp.json()
    except Exception as e:  # noqa: BLE001 - 필터 실패는 해당 필터만 건너뜀(JSON 디코드 오류 포함)
        logger.warning("[cg] 업비트 마켓 상세 조회 실패 (유의종목 건너뜀): %s", e)
        return set()
    warned = set()
    for m in payload:
        market = m.get("market", "")
        if not market.startswith("KRW-"):
            continue
        if m.get("market_event", {}).get("warning"):
            warned.add(market.split("-", 1)[1])
    return warned


def fetch_binance_usdt_symbols(timeout: float) -> set:
    """Binance USDT 마켓에 TRADING 상태로 상장된 코인 심볼 집합."""
    try:
        resp = requests.get(BINANCE_EXCHANGE_INFO_URL, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:  # noqa: BLE001 - 필터 실패는 해당 필터만 건너뜀(JSON 디코드 오류 포함)
        logger.warning("[cg] Binance exchangeInfo 조회 실패 (Binance 필터 건너뜀): %s", e)
        return set()
    return {
        s.get("baseAsset", "").upper()
        for s in data.get("symbols", [])
        if s.get("quoteAsset") == "USDT" and s.get("status") == "TRADING"
        and s.get("baseAsset")
    }


def _load_json_cache(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        return {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning("[cg] 캐시 파일 로드 실패(무시): %s: %s", path, e)
        return {}


def _save_json_cache(path: str, data: dict) -> None:
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(p)  # atomic overwrite (pathlib.Path.replace ≡ os.replace)
    except OSError as e:
        # 이력 저장 실패가 유니버스 빌드를 죽이면 안 된다 — 이력은 다음 회차에
        # 다시 쌓이지만 유니버스 실패는 수집 전체 정지다(2026-08-11 검토 BUG3).
        logger.warning("[cg] 필터 캐시 저장 실패(무시): %s: %s", path, e)


def _apply_quality_filters(universe: list, now: float, timeout: float) -> list:
    """유니버스 품질 필터 6종 적용. 각 필터 실패는 해당 필터만 건너뜀(회차 안전).

    1. 신규 상장 N일 미만 제외 (최초 감지 기준)
    2. Binance USDT 미상장 제외
    3. 업비트 투자경고(warning) 제외
    4. 시총 순위 급락 코인 제외
    5. 24h 거래대금 하한 미달 제외
    6. 시총 절대 하한 미달 제외
    """
    exclude_new_days = settings.get("universe_exclude_new_listing_days")
    exclude_binance = settings.get("universe_exclude_non_binance")
    exclude_warning = settings.get("universe_exclude_upbit_warning")
    rank_drop_threshold = settings.get("universe_rank_drop_threshold")
    rank_drop_min_hist = settings.get("universe_rank_drop_min_history")
    min_volume_usd = settings.get("universe_min_volume_usd") or 0
    min_mcap_usd = settings.get("universe_min_mcap_usd") or 0

    # ── 필터 1: 신규 상장 ─────────────────────────────────────────────────
    first_seen: dict = {}
    first_seen_path = settings.get("universe_first_seen_cache_path")
    if exclude_new_days:
        first_seen = _load_json_cache(first_seen_path)
        # 첫 실행(파일 없음/빈 파일) 조부조항: 현재 유니버스 전원을 "이미 오래된
        # 코인"으로 소급 등재한다. 안 하면 전원이 first_seen=now 로 찍혀 90일간
        # 유니버스가 통째로 비는 치명 버그(2026-08-11 검토 BUG1). 이 필터는
        # "가동 시작 이후에 새로 나타난 코인"만 잡는 것이 설계 의도다.
        _grandfather = not first_seen
        for coin in universe:
            sym = coin["symbol"]
            if sym not in first_seen:
                first_seen[sym] = 0.0 if _grandfather else now
        _save_json_cache(first_seen_path, first_seen)

    # ── 필터 2: Binance 상장 여부 ──────────────────────────────────────────
    binance_syms: set = set()
    if exclude_binance:
        binance_syms = fetch_binance_usdt_symbols(timeout)

    # ── 필터 3: 업비트 투자경고 ────────────────────────────────────────────
    warning_syms: set = set()
    if exclude_warning:
        warning_syms = fetch_upbit_warning_symbols(timeout)

    # ── 필터 4: 순위 급락 ─────────────────────────────────────────────────
    rank_hist: dict = {}
    rank_hist_path = settings.get("universe_rank_history_cache_path")
    if rank_drop_threshold:
        rank_hist = _load_json_cache(rank_hist_path)
        for coin in universe:
            sym = coin["symbol"]
            # 2026-08-17 rank=None 가드: build_universe 재작성으로 Upbit-only 소형
            # 알트가 rank=None 으로 편입되는데, None 을 rank_hist 에 append 하면
            # 아래 min()·subtract 에서 TypeError → fail-open 이 다른 5개 필터까지
            # 무력화(리뷰 확인). None 은 rank_drop 판정 대상이 아니므로 스킵.
            if coin.get("rank") is None:
                continue
            entries = rank_hist.setdefault(sym, [])
            # 하루 1엔트리 강제(2026-08-11 검토 BUG2): 캐시 미스·force 재호출로
            # 같은 날 여러 번 실행돼도 len(entries) 가 "일수"로 유지되게 20h
            # 이내 재기록은 마지막 엔트리를 갱신만 한다. min_history=7 이
            # 실제 7일을 뜻하려면 엔트리=일 단위여야 한다.
            if entries and now - entries[-1]["ts"] < 20 * 3600:
                entries[-1] = {"ts": now, "rank": coin["rank"]}
            else:
                entries.append({"ts": now, "rank": coin["rank"]})
            if len(entries) > 30:
                rank_hist[sym] = entries[-30:]
        _save_json_cache(rank_hist_path, rank_hist)

    # ── 적용 ───────────────────────────────────────────────────────────────
    filtered = []
    excluded = {"new_listing": [], "binance": [], "warning": [], "rank_drop": [],
                "low_volume": [], "low_mcap": []}
    new_cutoff = now - exclude_new_days * 86400 if exclude_new_days else 0

    for coin in universe:
        sym = coin["symbol"]

        if exclude_new_days and new_cutoff > 0:
            seen_at = first_seen.get(sym, now)
            if seen_at > new_cutoff:
                excluded["new_listing"].append(sym)
                continue

        if exclude_binance and binance_syms and sym not in binance_syms:
            excluded["binance"].append(sym)
            continue

        if exclude_warning and sym in warning_syms:
            excluded["warning"].append(sym)
            continue

        if rank_drop_threshold and coin.get("rank") is not None:
            # 2026-08-17 rank=None 스킵 (위 append 스킵과 동일 근거)
            entries = rank_hist.get(sym, [])
            # entries[:-1] 이 비지 않으려면 최소 2엔트리 필요 — min_hist 가 1 이하여도 안전
            if len(entries) >= max(rank_drop_min_hist, 2):
                # None 은 최소 산정에서도 제외 (이력에 섞였을 수 있음)
                past_ranks = [e["rank"] for e in entries[:-1] if e.get("rank") is not None]
                if past_ranks:
                    best_rank = min(past_ranks)
                    if coin["rank"] - best_rank >= rank_drop_threshold:
                        excluded["rank_drop"].append(sym)
                        continue

        # 2026-08-17 min_volume/mcap None 안전 처리: build_universe 재작성으로
        # CG top-N 밖 코인은 total_volume=None, market_cap=None. 종전 `or 0` 은
        # None→0 으로 처리해 임계>0 활성 시 소형 알트 전량 배제. None 은 '측정
        # 불가 → 통과' 로 취급(리뷰 확인). 임계는 CG 데이터 있는 코인만 적용.
        if min_volume_usd > 0:
            vol = coin.get("total_volume")
            if vol is not None and vol < min_volume_usd:
                excluded["low_volume"].append(sym)
                continue

        if min_mcap_usd > 0:
            mcap = coin.get("market_cap")
            if mcap is not None and mcap < min_mcap_usd:
                excluded["low_mcap"].append(sym)
                continue

        filtered.append(coin)

    total_excluded = sum(len(v) for v in excluded.values())
    if total_excluded:
        logger.info(
            "[cg] 품질 필터 제외 %d개: 신규=%s 바이낸스미상장=%s 투자경고=%s 순위급락=%s 저거래량=%s 저시총=%s",
            total_excluded,
            excluded["new_listing"] or "없음",
            excluded["binance"] or "없음",
            excluded["warning"] or "없음",
            excluded["rank_drop"] or "없음",
            excluded["low_volume"] or "없음",
            excluded["low_mcap"] or "없음",
        )
    return filtered


def build_universe(force: bool = False) -> list:
    """업비트 KRW 전체 종목 스캔 + CoinGecko 메타 병합 (2026-08-17 사용자 결정).

    이전 방식: CoinGecko top-N ∩ Upbit KRW → LSK/VELODROME 등 시총 300위+ 는
    후보 자체에서 제외돼 신규 채널(BitcoinBullets 등) 파싱 매칭율 저하.
    새 방식: **Upbit KRW 전체 마켓이 곧 유니버스** (250~300종). CoinGecko top-N
    은 rank/tier_icon 메타 부여 용도로만 활용 — 매칭 실패(top-N 밖)는
    rank=None, tier_icon='·'(플레인) 로 편입.

    반환 항목: {symbol, ticker, rank, name, price_usd, tier_icon}
      · rank 는 CoinGecko 시총 순위 (top-N 밖 코인은 None)
      · tier_icon 은 시총 등급 아이콘 ('💎🥇🥈🥉·')
      · CoinGecko 매칭 실패 코인은 price_usd=None (터치 판정에 무영향)"""
    cache_path = settings.get("universe_cache_path")
    max_age = settings.get("universe_refresh_hours") * 3600
    timeout = settings.get("http_timeout_sec")

    if not force:
        cached = _load_cache(cache_path, max_age)
        if cached is not None:
            return cached

    try:
        top = fetch_top_coins(settings.get("universe_top_n"), timeout)
        krw = fetch_upbit_krw_symbols(timeout)
    except (requests.RequestException, KeyError, TypeError, ValueError):
        stale = _load_cache_any_age(cache_path)
        if stale is not None:
            logger.warning("[cg] 유니버스 갱신 실패 - 만료된 캐시로 폴백(%d개)", len(stale))
            return stale
        raise

    # CoinGecko top 결과를 symbol → meta 로 인덱싱 (Upbit 스캔 후 조회)
    cg_meta = {c["symbol"]: c for c in top if c["symbol"] not in STABLECOINS}

    # Upbit KRW 전체 마켓이 유니버스의 기반. 스테이블코인은 제외.
    universe = []
    for sym in sorted(krw):
        if sym in STABLECOINS:
            continue
        meta = cg_meta.get(sym)
        if meta:
            universe.append({
                "symbol": sym,
                "ticker": f"KRW-{sym}",
                "rank": meta["rank"],
                "name": meta["name"],
                "price_usd": meta["price_usd"],
                "tier_icon": meta["tier_icon"],
                "total_volume": meta.get("total_volume"),
                "market_cap": meta.get("market_cap"),
            })
        else:
            # CoinGecko top-N 밖 — LSK/VELODROME 등 소형 알트. rank·price·tier
            # 는 미상이지만 알림 파이프라인(터치 판정·심볼 매칭) 은 정상 동작.
            universe.append({
                "symbol": sym,
                "ticker": f"KRW-{sym}",
                "rank": None,
                "name": sym,
                "price_usd": None,
                "tier_icon": "·",
                "total_volume": None,
                "market_cap": None,
            })

    # 품질 필터는 fail-open(2026-08-11 검토 BUG3): 필터 내부의 예상 밖 예외가
    # 유니버스 빌드 자체를 죽이면 수집 전체가 정지한다 — 필터 없는 유니버스가
    # 빈 유니버스보다 항상 낫다.
    try:
        filtered = _apply_quality_filters(universe, now=time.time(), timeout=timeout)
        # 안전망: 필터가 전량 제외하면 필터 자체가 오작동한 것 — 미필터 원본 사용
        if not filtered and universe:
            logger.error("[cg] 품질 필터가 유니버스 전체(%d개)를 제외 — 필터 무시하고 원본 사용",
                         len(universe))
        else:
            universe = filtered
    except Exception as e:  # noqa: BLE001
        logger.error("[cg] 품질 필터 예외(무시하고 미필터 유니버스 사용): %s", e)

    _save_cache(cache_path, universe)
    return universe


def _load_cache(path: str, max_age_sec: float) -> Optional[list]:
    p = Path(path)
    if not p.exists():
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            payload = json.load(f)
        if time.time() - payload.get("updated_at", 0) > max_age_sec:
            return None
        universe = payload.get("universe")
        # 2026-07-27 수리(개발자B, 교차감사 A-m5 확정 - "major 에 가깝다"): 이전엔
        # `is not None` 게이트라 [] 도 "신선한 캐시"로 통과했다. CoinGecko/업비트
        # 어느 한쪽이 순간적으로 빈 배열을 응답한 회차가 하필 이 함수를 거치면
        # 그 [] 가 그대로 파일에 박히고(과거 _save_cache 는 무조건 저장), 이후
        # universe_refresh_hours(24h) 내내 "신선"으로 읽혀 수집·텔레그램 소스
        # 전체가 조용히 무동작했다 - 로그도 예외도 없이. truthy 검사로 바꿔 빈
        # 캐시는 캐시-미스로 취급해 즉시 재조회를 유도한다(아래 _save_cache 도
        # 같은 이유로 빈 목록을 애초에 저장하지 않게 바꿨다 - 이중 방어).
        return universe if universe else None
    except Exception:
        return None


def _load_cache_any_age(path: str) -> Optional[list]:
    """신선도 무시하고 캐시가 존재하기만 하면 반환 - 실패 시 폴백 전용(2026-07-26).

    2026-07-27 수리(A-m5): 빈 목록은 폴백할 가치가 없다 - 이 함수의 취지는 "완전
    실패보다 낡은 유니버스가 낫다"인데, 빈 유니버스는 완전 실패와 다를 바 없이
    수집·텔레그램 소스 전체를 조용히 멈춘다(오히려 예외가 안 나서 더 나쁘다 -
    호출부가 "이번 회차 스킵"으로 명시 처리할 기회조차 사라진다). 빈 캐시면
    None 을 반환해 build_universe 의 except 블록이 원 예외를 그대로 전파하게
    한다(_load_cache 와 동일한 truthy 게이트)."""
    p = Path(path)
    if not p.exists():
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            payload = json.load(f)
        universe = payload.get("universe")
        return universe if universe else None
    except Exception:
        return None


def _save_cache(path: str, universe: list) -> None:
    # 2026-07-27 수리(A-m5): 빈 목록을 저장하면 다음 호출의 _load_cache/
    # _load_cache_any_age 가 빈 캐시를 읽게 된다 - 위 두 함수를 truthy 게이트로
    # 고쳐도, 애초에 저장을 안 하는 편이 기존 정상 캐시를 보존한다는 점에서 낫다
    # (완전 실패보다 낡은 유니버스가 낫다는 기존 폴백 철학과 같은 방향 - 빈
    # 유니버스보다 '어제자 정상 유니버스'가 훨씬 낫다). 저장을 생략하면 다음
    # 호출은 기존 캐시가 있으면 그걸(신선하면), 없거나 만료됐으면 재조회를 탄다.
    if not universe:
        logger.warning("[cg] 빈 유니버스 - 캐시 저장 생략(기존 캐시가 있다면 보존)")
        return
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"updated_at": time.time(), "universe": universe}, f, ensure_ascii=False, indent=2)
    tmp.replace(p)
