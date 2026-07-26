"""클러스터 병합 규칙(순수 함수) + 합의(confluence) 집계 — 2026-07-26 카드 확정.

배경: "같은 코인, 엔트리 ±cluster_band_pct 는 한 타점" 이라는 병합 규칙은 원래
monitor/price_check.py 의 `_build_clusters` 하나뿐이었다. 주간 리포트가 '터치 이력
전체'에 같은 규칙을 다시 적용해야 해서, 규칙 자체를 여기(프로젝트 모듈 import 0 인
순수 함수)로 올려 양쪽이 같은 정의를 쓰게 한다. price_check 는 이번 스프린트에서
동시 작업 중이라 손대지 않았다 — 이 파일이 규칙의 정본(canonical)이고, price_check
쪽 `_build_clusters` 는 다음 정리 때 여기로 위임시키면 된다(동작은 이미 동일).

합의 가중은 **표시 전용**이다(dev_a "축 분리, 혼합 금지" 원칙). CR 은 E_LB 수식에도,
랭킹 정렬 키에도 들어가지 않는다 — 41클러스터 중 다자 6개(14.6%)뿐이라 계수를
추정할 표본이 애초에 없다(Phase 1). 회귀 추정은 표본 20건 이상 쌓인 뒤 재검토.
"""


def build_clusters(levels: list, band_pct: float, window_sec: float = None,
                   time_key: str = "touched_at") -> list:
    """엔트리 내림차순 greedy 병합. 반환: [ [level,...](entry 내림차순), ... ]

    window_sec=None 이면 monitor/price_check.py `_build_clusters` 와 완전히 동일한
    동작(실시간 경로: 어차피 '지금 동시에 살아있는' 레벨만 넘어오므로 시간 제약 불필요).

    window_sec 지정 시엔 대표(클러스터 상단) 레벨과 time_key 시각 차이가 그 안인
    레벨만 병합한다. 리포트가 과거 이력 전체를 재구성할 때 필요한 안전장치 —
    시간 제약이 없으면 몇 달 간격으로 우연히 같은 가격대에 걸린 두 글이 '합의'로
    집계돼 CR 이 부풀려진다. 기본 168h(= level_expiry_hours)는 두 레벨이 실제로
    동시에 살아있었을 수 있는 최대 간격이라, 실시간 경로가 만들 수 있었던
    클러스터의 상위집합을 유지한다(과소집계 없음).
    """
    with_entry = [l for l in levels if l.get("entry_usd")]
    with_entry.sort(key=lambda l: l["entry_usd"], reverse=True)
    clusters, used = [], set()
    for i, lv in enumerate(with_entry):
        if i in used:
            continue
        top = lv["entry_usd"]
        t_top = lv.get(time_key)
        group = []
        for j, l in enumerate(with_entry):
            if j in used or l["entry_usd"] < top * (1 - band_pct / 100.0):
                continue
            if window_sec is not None and t_top is not None:
                t = l.get(time_key)
                if t is None or abs(t - t_top) > window_sec:
                    continue
            group.append((j, l))
        for j, _ in group:
            used.add(j)
        clusters.append([l for _, l in group])
    return clusters


def build_clusters_by_coin(levels: list, band_pct: float, window_sec: float = None,
                           time_key: str = "touched_at") -> list:
    """코인별로 나눈 뒤 병합 — 실시간 경로(price_check)는 이미 코인 단위로 호출하지만,
    이력 전체를 넘기는 리포트 경로에는 이 래퍼가 필요하다. 코인 순회 순서는
    입력 등장 순서(결정적)."""
    by_coin, order = {}, []
    for lv in levels:
        c = lv.get("coin_symbol")
        if c not in by_coin:
            by_coin[c] = []
            order.append(c)
        by_coin[c].append(lv)
    out = []
    for c in order:
        out.extend(build_clusters(by_coin[c], band_pct, window_sec, time_key))
    return out


def confluence_by_author(levels: list, band_pct: float, window_sec: float = None,
                         time_key: str = "touched_at") -> dict:
    """CR(a) = (a 가 속한 다자 클러스터 수) / (a 의 전체 클러스터 수).

    다자 = 서로 다른 작성자 2명 이상이 속한 클러스터. 한 작성자가 같은 클러스터에
    레벨을 여러 개 올려도 그 클러스터는 1회로만 센다(자기 글 반복 게시로 합의
    수치를 못 부풀리게 — 안티게이밍).

    반환: {author: {"multi": int, "total": int, "cr": float}}
    """
    stat = {}
    for cluster in build_clusters_by_coin(levels, band_pct, window_sec, time_key):
        authors = {l.get("author") for l in cluster if l.get("author")}
        is_multi = len(authors) >= 2
        for a in authors:
            s = stat.setdefault(a, {"multi": 0, "total": 0, "cr": 0.0})
            s["total"] += 1
            s["multi"] += 1 if is_multi else 0
    for s in stat.values():
        s["cr"] = s["multi"] / s["total"] if s["total"] else 0.0
    return stat


# ── 초과 적중률 베이스라인 (arXiv:2105.02728 취지) ────────────────────────
#
# "작성자를 따라갔을 때"가 "그냥 아무거나 사서 들고 있었을 때"보다 나은지.
# 기준선은 이미 매 회차 기록 중인 ret_24h 의 양수 비율 — 추가 API 호출·계산 비용 0
# (질문카드 B안 확정). 판정 정의가 서로 달라(작성자=TP 도달, 기준선=단순 수익 여부)
# 차이값은 '엄밀한 초과수익'이 아니라 방향 참고치다 — 렌더러가 caveat 를 병기한다.


def baseline_positive_rate(ret_values: list) -> dict:
    """ret_24h 값 목록 → {"n": 유효표본, "positive": 양수건, "rate": 비율|None}."""
    vals = [v for v in ret_values if v is not None]
    if not vals:
        return {"n": 0, "positive": 0, "rate": None}
    pos = sum(1 for v in vals if v > 0)
    return {"n": len(vals), "positive": pos, "rate": pos / len(vals)}


def excess_hit_rate(wins: int, losses: int, baseline_rate) -> dict:
    """원시 승률 대비 베이스라인 초과분(%p). 표본 0 또는 기준선 없음이면 None."""
    n = wins + losses
    if n <= 0 or baseline_rate is None:
        return {"raw": None, "excess": None, "n": n}
    raw = wins / n
    return {"raw": raw, "excess": raw - baseline_rate, "n": n}
