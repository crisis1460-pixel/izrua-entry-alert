"""작성자 랭킹 순수 수학 (ACCURACY_DB_PLAN 2단계, 2026-07-26 질문카드 확정).

축 분리 원칙: E_LB(R 트랙)=랭킹 주지표, 수축 승률 p̂=승률 해석 지표 — 혼합 금지.
알림 필터에는 미사용(wait-and-see). 알림에서 바뀌는 것은 자체 승률 줄의
발동 게이트(raw n≥5 → n_eff≥5) 하나뿐이고 본격 노출은 주간 리포트(후속).

카드 확정: R NULL 종결건은 2트랙(랭킹 제외, 승률축 포함) /
prior 강도 m_eff = min(m, 워쳐 표본수) — 소표본 워쳐 prior 과신 왜곡 차단.

프로젝트 모듈 import 0 (순환 import 원천 차단 + DB 없이 손계산 단위 테스트,
scripts/test_ranking.py). 행 데이터는 호출부(storage.db.get_author_outcome_rows)가 공급.
"""

import math

WIN_OUTCOMES = ("hit", "timeboxed_win")
LOSS_OUTCOMES = ("miss", "timeboxed_loss")


def recency_weight(now: float, touched_at: float, half_life_days: float = 90.0) -> float:
    """w = 0.5^(경과일/반감기). 기준점은 touched_at (판정창 길이 편향 제거·불변성)."""
    d = max((now - touched_at) / 86400.0, 0.0)  # 시계 왜곡 방어 클램프
    return 0.5 ** (d / half_life_days)


def effective_n(weights: list) -> float:
    """Kish 유효표본수 (Σw)²/Σw². 빈 목록이면 0."""
    sq = sum(w * w for w in weights)
    if sq <= 0:
        return 0.0
    sw = sum(weights)
    return sw * sw / sq


def weighted_mean_var(values: list, weights: list):
    """신뢰도 가중 평균·모집단형 분산. (소표본 페널티는 E_LB 의 √n_eff 항이 담당 —
    베셀 보정을 겹치면 n_eff≤1 에서 정의 불능이라 모집단형 채택.)"""
    sw = sum(weights)
    if sw <= 0:
        return None, None
    mean = sum(w * v for v, w in zip(values, weights)) / sw
    var = sum(w * (v - mean) ** 2 for v, w in zip(values, weights)) / sw
    return mean, var


def e_lb(values: list, weights: list, z: float = 1.28):
    """보수적 기대값 = mean_w − z·√(var_w/n_eff) (z=1.28 ≈ 80% 단측 신뢰하한).
    표본 없으면 None. 표시용 클립은 author_metrics 의 e_lb_display."""
    mean, var = weighted_mean_var(values, weights)
    if mean is None:
        return None
    neff = effective_n(weights)
    if neff <= 0:
        return None
    return mean - z * math.sqrt(var / neff)


def shrunk_win_rate(wins_w: float, losses_w: float, prior_p=None, prior_n=None,
                    m: int = 10) -> float:
    """베이지안 수축 승률. prior Beta(m_eff·p+1, m_eff(1−p)+1),
    m_eff = min(m, 워쳐 표본수) — 워쳐 4건짜리 prior 1.0 이 10건 무게로 얹히던
    왜곡 차단(카드2 확정). 워쳐 없으면 Beta(1,1) = 균등."""
    m_eff = min(m, prior_n) if (prior_p is not None and prior_n) else 0
    p = prior_p if prior_p is not None else 0.5
    a0 = m_eff * p + 1.0
    b0 = m_eff * (1.0 - p) + 1.0
    return (a0 + wins_w) / (a0 + b0 + wins_w + losses_w)


def author_metrics(rows: list, now: float, half_life_days: float = 90.0,
                   z: float = 1.28, m: int = 10) -> dict:
    """작성자 종결 표본 → 두 축 지표.

    rows: [{outcome, r_multiple, touched_at, author_hit_rate, author_hit_count}, ...]
    반환: neff_win(승률축 게이트용) / p_hat(수축 승률) / neff_r·e_lb(R 트랙 랭킹)
          / e_lb_display(표시 클립 max(E_LB,−1), 내부값은 e_lb 보존) / sum_w
    """
    outc = [r for r in rows if r.get("outcome") and r.get("touched_at")]
    ws = [recency_weight(now, r["touched_at"], half_life_days) for r in outc]
    wins_w = sum(w for r, w in zip(outc, ws) if r["outcome"] in WIN_OUTCOMES)
    losses_w = sum(w for r, w in zip(outc, ws) if r["outcome"] in LOSS_OUTCOMES)

    prior_p = prior_n = None
    for r in outc:  # 워쳐 prior — 같은 작성자 행이라 아무 행의 값이나 동일
        if r.get("author_hit_rate") is not None and r.get("author_hit_count"):
            prior_p, prior_n = r["author_hit_rate"], r["author_hit_count"]
            break

    # R 트랙(2트랙 확정): r_multiple 실측 보유 표본만 — tp_only hit 등 NULL 은 제외
    r_pairs = [(r["r_multiple"], w) for r, w in zip(outc, ws)
               if r.get("r_multiple") is not None]
    rs = [v for v, _ in r_pairs]
    rws = [w for _, w in r_pairs]
    lb = e_lb(rs, rws, z) if rs else None

    return {
        "neff_win": effective_n(ws),
        "p_hat": shrunk_win_rate(wins_w, losses_w, prior_p, prior_n, m) if ws else None,
        "neff_r": effective_n(rws) if rws else 0.0,
        "e_lb": lb,
        "e_lb_display": max(lb, -1.0) if lb is not None else None,
        "sum_w": sum(ws),
    }


def is_confirmed_reverse(snapshots: list, min_neff: float = 5.0) -> bool:
    """최근 N개 스냅샷(최신순)이 모두 neff_r>=min_neff 이고 e_lb<0 이면 True.

    순수 함수 — DB 접근 없음. 스냅샷이 2개 미만이면 False(증거 부족).
    n 주 연속 판정으로 확장하려면 snapshots 길이만 늘리면 된다."""
    if len(snapshots) < 2:
        return False
    return all(
        (s.get("neff_r") or 0) >= min_neff and (s.get("e_lb") is not None) and s["e_lb"] < 0
        for s in snapshots
    )


def is_recovered_reverse(snapshots: list, min_neff: float = 5.0) -> bool:
    """역신호 해제 판정 — 최근 N개 스냅샷이 모두 neff_r>=min_neff 이고 e_lb>=0 이면
    True (2026-08-01 사용자 결정 Q2: 2주 연속 회복 시 해제).

    is_confirmed_reverse 의 대칭이되 경계는 비대칭이다: 확정은 e_lb<0, 해제는 e_lb>=0
    (0 은 '음수 기대'가 아니므로 회복 쪽). 표본 부족(2개 미만)·neff 게이트 미달·
    e_lb 결측이면 False = **확정 상태를 보수적으로 유지**한다 — 해제는 확정만큼
    강한 증거(2주 연속 게이트 통과 + 비음수)를 요구한다."""
    if len(snapshots) < 2:
        return False
    return all(
        (s.get("neff_r") or 0) >= min_neff and (s.get("e_lb") is not None) and s["e_lb"] >= 0
        for s in snapshots
    )


def rank_authors(rows_by_author: dict, now: float, min_neff: float = 5.0, **kw) -> list:
    """주간 리포트용 랭킹: {author: rows} → R트랙 게이트(n_eff≥min) 통과 작성자를
    E_LB 내림차순 [(author, metrics)] 로. 미달 작성자는 리포트에서 '표본 부족' 그룹."""
    out = []
    for author, rows in rows_by_author.items():
        met = author_metrics(rows, now, **kw)
        if met["neff_r"] >= min_neff and met["e_lb"] is not None:
            out.append((author, met))
    out.sort(key=lambda x: x[1]["e_lb"], reverse=True)
    return out
