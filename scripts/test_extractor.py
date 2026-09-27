# 추출기 단위 테스트 — 다양한 실전 표기 샘플로 검증. 네트워크 불필요.
import sys
sys.path.insert(0, ".")
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from collector.extractor import parse_setup

CASES = [
    # (설명, 텍스트, 현재가, 기대 entry 근사, 기대 direction)
    ("영문 라벨 단일", "LINK long setup. Entry: 8.30, Stop loss: 7.80, Target: 9.50", 8.4, 8.30, "long"),
    ("엔트리 범위", "Buy zone 0.45 - 0.48, SL 0.42, TP1 0.55 TP2 0.60", 0.46, 0.465, "long"),
    ("한글 라벨", "비트코인 롱 진입가 62,000 손절 59,000 목표 68,000", 61000, 62000, "long"),
    ("숏 셋업", "ETH short. Entry 3500 SL 3600 Target 3200", 3450, 3500, "short"),
    ("레버리지/퍼센트 오인방지", "Long entry 12100 with 10x leverage, expect +20% to target 14000, SL 11300", 12050, 12100, "long"),
    ("엔트리 없음→None", "BTC looking bullish, might pump soon. No clear levels.", 60000, None, None),
    ("연도 오인방지", "In 2026 this coin moons. Entry 0.85 SL 0.78 TP 1.10", 0.86, 0.85, "long"),
    ("sanity 실패(현재가와 동떨어짐)", "Entry 5.00 SL 4.5 TP 6", 100.0, None, None),
    # 2026-07-24 감사: 예전엔 20xx 를 연도로 무조건 삭제해 ETH 급($2,0xx) 가격이
    # 지워지고 SL(1980)이 엔트리로 오인되는 치명 버그가 있었다
    ("가격 2050 연도 오인방지", "Long setup. Entry: 2050, SL: 1980, Target: 2200", 2040, 2050, "long"),
]

ok = 0
for desc, text, price, exp_entry, exp_dir in CASES:
    r = parse_setup(text, current_price=price)
    if exp_entry is None:
        passed = r is None
        got = "None" if r is None else f"entry={r['entry']}"
    else:
        passed = r is not None and abs(r["entry"] - exp_entry) / exp_entry < 0.02 and r["direction"] == exp_dir
        got = "None" if r is None else f"entry={r['entry']:.4g} dir={r['direction']} sl={r['sl']} tp={r['tp']} rr={r['rr']}"
    mark = "✅" if passed else "❌"
    if passed:
        ok += 1
    print(f"{mark} {desc}\n    → {got}")

# 2026-07-23 실전 INJ 알림에서 발견된 실제 버그 재현: "Take-Profit Targets:" 복수형
# 헤더 뒤 "TP1"의 "1"이 목표가로 오인되어 tp=1.0(정답 5.298) → RR 마이너스로 노출됐다.
# 아래는 그 실제 원문(izrua_entry_alert 라이브 수집, 2026-07-23 07:xx KST)으로 만든
# 회귀 테스트 — 재발하면 반드시 잡혀야 한다.
REAL_BUG_CASES = [
    (
        "실전버그 재현 - INJ LONG (TP1 라벨숫자 오인)",
        "INJ USDT LONG SIGNAL\n#105  INJ/USDT – Trade Setup (LONG)\n\n"
        "📈 Position Type: LONG\n🕒 Timeframe: 1H\n📊 Market: Futures\n\n"
        "💰 Entry Zone:\n\n5.207\n\n\n\n🛑 Stop-Loss:\n\n5\n\n"
        "🎯 Take-Profit Targets:\n\n• TP1:  5.298\n\n• TP2: 5.420\n\n"
        "• TP3: 5.560\n\n• TP4: 5.700\n\n⚙️ Leverage:\n\n5 *10",
        5.20,
        {"entry": 5.207, "sl": 5.0, "tp": 5.298, "direction": "long"},
    ),
    (
        "실전버그2 재현 - ARB (공백 서수 'Target 1:')",
        "#ARBUSDT | Testing Wedge Breakout Amid Key Support\n\n#ARB\n\n"
        "The price is moving within a descending channel on the 1-hour timeframe.\n"
        "There is a key support zone in green at 0.08325.\n\n"
        "Entry Price: 0.08880\nTarget 1: 0.08977\nTarget 2: 0.09145\nTarget 3: 0.09330\n\n"
        "Stop Loss: At the resistance zone in green\n\nRemember this simple rule: Money management.",
        0.0885,
        # sl 은 원문에 숫자가 없음(정상적으로 None). tp 는 첫 타겟 0.08977 이어야 하며
        # 절대 1.0(서수 오인)이 아니어야 한다. sl 없으므로 rr 은 None.
        {"entry": 0.0888, "sl": None, "tp": 0.08977, "direction": "long", "rr_none_ok": True},
    ),
    (
        "실전버그 재현 - INJ SHORT (동일 패턴)",
        "INJ USDT SHORT SIGNAL\n#81.  INJ/USDT – Trade Setup (SHORT)\n\n"
        "📈 Position Type: SHORT\n🕒 Timeframe: 1H\n📊 Market: Futures\n\n"
        "💰 Entry Zone:\n\n5.090\n\n\n5.185\n\n🛑 Stop-Loss:\n\n5.290\n\n"
        "🎯 Take-Profit Targets:\n\n• TP1: 4.970\n\n• TP2: 4.828\n\n"
        "• TP3: 4.663\n\n• TP4: 4.434\n\n⚙️ Leverage:\n\n5 *10",
        5.10,
        {"entry": 5.09, "sl": 5.29, "tp": 4.970, "direction": "short"},
    ),
    (
        "실전버그 재현 - AAVE 슬래시 엔트리 존 범위 보존 (Entry - $90/95/$100 → 90~100)",
        "Long setup\nEntry - $90/95/$100\nTP's - $125/145/155",
        95.0,
        {"entry": 95.0, "entry_low": 90.0, "entry_high": 100.0, "sl": None, "tp": 125.0,
         "direction": "long", "rr_none_ok": True},
    ),
    # 2026-07-29 실사고 재현 - ONDO id=30: "Profit level 0.3981" 형식에서
    # `targets?`가 서술부 동사 "the thesis targets the 0.75 premium zone"을 라벨로
    # 오인해 tp=0.75(정답 0.3981)로 저장됐다. \bprofit\s*level\b 추가로 수리.
    (
        "실전버그 재현 - ONDO (Profit level 서술부 targets 동사 오인)",
        "Long trade\nEntry 0.3099\nProfit level 0.3981 (28.46%)\n"
        "Stop level 0.3046 (1.71%)\nRR 16.64\n\n"
        "The trade thesis targets the 0.75 premium zone as the exit level.",
        0.32,
        {"entry": 0.3099, "sl": 0.3046, "tp": 0.3981, "direction": "long"},
    ),
    # 2026-08-01 전체코드 검토 발견 - direction 오분류: ICT/SMC 용어 "sell-side
    # liquidity"(매도측 유동성 — 방향과 무관한 시장구조 서술어) 내부의 "sell" 이
    # \bsell\b 에 걸려 "short"로 오분류되고, 이 봇은 long 전용이라 조용히 감시
    # 대상에서 빠졌었다. "long"/"buy" 등 실제 방향 단어를 안 쓴 글이 회귀 대상.
    (
        "검토버그 재현 - sell-side liquidity 오분류(방향 단어 없이도 long 유지)",
        "Sweeping sell-side liquidity near 61200 before the bounce into premium.\n"
        "Entry: 61500\nStop Loss: 60800\nTarget: 63000",
        61600,
        {"entry": 61500.0, "sl": 60800.0, "tp": 63000.0, "direction": "long"},
    ),
    # 2026-08-01 전체코드 검토 발견 - 같은 줄 서수 마커: "TARGETS: 1) a - 2) b"
    # 형태는 _LIST_MARKER 가 줄 시작(^)에만 적용돼 못 지웠다. _LADDER 가 ")"에
    # 막혀 매칭 실패 → _RANGE/_SINGLE 로 떨어지며 마커 숫자 "1"이 tp 로 오인됐다
    # (과거 "Target 1:" 서수오인과 같은 계열의 재발 형태).
    (
        "검토버그 재현 - 같은줄 서수마커 TARGETS: 1) a - 2) b (TP=1.0 오인 방지)",
        "Long setup\nEntry: 0.30\nTARGETS: 1) 0.35 - 2) 0.38 - 3) 0.42\nSL: 0.28",
        0.30,
        {"entry": 0.30, "sl": 0.28, "tp": 0.35, "direction": "long"},
    ),
    # 2026-08-01 실사용자 스크린샷 검토 발견 - CFX id=181 라이브 알림 원문(2026-08-01
    # KST). Target 1(0.04087)이 entry(0.04088)보다 살짝 낮아 sanity 탈락하는데,
    # 예전엔 이 하나 때문에 tp/tps_all 전체가 버려져 정상 Target 2/3 까지
    # "데이터 없음"으로 알림에 노출됐다(감시밴드·다단계TP 도 영향받음).
    (
        "실전버그 재현 - CFX (Target1 sanity탈락 시 Target2 폴백, tp/tps_all/단계수 일치)",
        "Entry Price: 0.04088\nTarget 1: 0.04087\nTarget 2: 0.04259\nTarget 3: 0.04392\n"
        "Stop Loss: At the resistance zone in green.",
        0.0409,
        {"entry": 0.04088, "sl": None, "tp": 0.04259, "direction": "long", "rr_none_ok": True},
    ),
]

def _close(a, b):
    if b is None:
        return a is None
    return a is not None and abs(a - b) < max(0.01, abs(b) * 0.001)


for desc, text, price, expected in REAL_BUG_CASES:
    r = parse_setup(text, current_price=price)
    rr_ok = (r is not None) and (
        (r["rr"] is None) if expected.get("rr_none_ok") else (r["rr"] is not None and r["rr"] > 0)
    )
    passed = (
        r is not None
        and r["direction"] == expected["direction"]
        and _close(r["entry"], expected["entry"])
        and _close(r["sl"], expected["sl"])
        and _close(r["tp"], expected["tp"])
        and rr_ok
        and ("entry_low" not in expected or _close(r.get("entry_low"), expected["entry_low"]))
        and ("entry_high" not in expected or _close(r.get("entry_high"), expected["entry_high"]))
    )
    got = "None" if r is None else f"entry={r['entry']} sl={r['sl']} tp={r['tp']} rr={r['rr']}"
    mark = "✅" if passed else "❌"
    if passed:
        ok += 1
    print(f"{mark} {desc}\n    → {got}")
    if not passed:
        print(f"    (기대: {expected})")

# ── tps_all 회귀 검증 (2026-07-29 B안) ─────────────────────────────────
# parse_setup 이 tps_all(전체 TP 가까운 순 정렬 목록)을 올바르게 반환하는지.
# 이 키가 없으면 tps_usd 컬럼이 항상 "[]" 로 저장돼 B안 필터가 무용지물이 된다.
TPSALL_CASES = [
    (
        "tps_all - INJ LONG 4단계 TP 가까운 순 정렬",
        "INJ USDT LONG SIGNAL\n#105  INJ/USDT – Trade Setup (LONG)\n\n"
        "📈 Position Type: LONG\n🕒 Timeframe: 1H\n📊 Market: Futures\n\n"
        "💰 Entry Zone:\n\n5.207\n\n\n\n🛑 Stop-Loss:\n\n5\n\n"
        "🎯 Take-Profit Targets:\n\n• TP1:  5.298\n\n• TP2: 5.420\n\n"
        "• TP3: 5.560\n\n• TP4: 5.700\n\n⚙️ Leverage:\n\n5 *10",
        5.20,
        [5.298, 5.420, 5.560, 5.700],
    ),
    (
        "tps_all - INJ SHORT 4단계 TP 가까운 순(엔트리→아래) 정렬",
        "INJ USDT SHORT SIGNAL\n#81.  INJ/USDT – Trade Setup (SHORT)\n\n"
        "📈 Position Type: SHORT\n🕒 Timeframe: 1H\n📊 Market: Futures\n\n"
        "💰 Entry Zone:\n\n5.090\n\n\n5.185\n\n🛑 Stop-Loss:\n\n5.290\n\n"
        "🎯 Take-Profit Targets:\n\n• TP1: 4.970\n\n• TP2: 4.828\n\n"
        "• TP3: 4.663\n\n• TP4: 4.434\n\n⚙️ Leverage:\n\n5 *10",
        5.10,
        [4.970, 4.828, 4.663, 4.434],
    ),
    (
        "tps_all - 사다리 하이픈 LONG 4단계",
        "$ETC/USDT LONG\nENTRY: 6.81 - 6.85\nTARGETS: 7.15 - 7.45 - 7.85 - 8.25\nSTOP LOSS: 6.25",
        7.0,
        [7.15, 7.45, 7.85, 8.25],
    ),
    (
        "tps_all - 레벨 없으면 빈 리스트",
        "BTC looking bullish, might pump soon. No clear levels.",
        60000,
        [],
    ),
    (
        "tps_all - CFX Target1 sanity탈락해도 Target2/3 는 살아남음",
        "Entry Price: 0.04088\nTarget 1: 0.04087\nTarget 2: 0.04259\nTarget 3: 0.04392\n"
        "Stop Loss: At the resistance zone in green.",
        0.0409,
        [0.04259, 0.04392],
    ),
    (
        # 2026-08-08 실전 재현(ICP id=254, 2026-08-06 수집분 — W4 수정 전 데이터라
        # tps_usd=[2.185](저점) 로 저장돼 tp_usd=2.21(고점) 과 어긋나 있었다).
        # "Target (TP):" 콜론 라벨 뒤 범위값 하나 — is_spec=True 단일그룹 케이스.
        # W4(오늘 아침 bef75a50, v[0]→v[-1]) 이후엔 tp 선정(grp[-1]=고점)과 동일
        # 기준으로 통일돼 tps_all 도 고점(2.21)을 담아야 한다 — 회귀 고정.
        "tps_all - ICP 범위형 단일 Target(콜론) - tp(고점)와 tps_all[0] 일치(W4 회귀)",
        "ICP/USDT (Long)\nEntry Zone: 2.075 – 2.085\n\n"
        "Target (TP): 2.185 – 2.210 (POI Zone)\n\nStop Loss (SL): Below 2.000",
        2.08,
        [2.210],
    ),
]
for desc, text, price, exp_tpsall in TPSALL_CASES:
    r = parse_setup(text, current_price=price)
    got_tpsall = (r or {}).get("tps_all", [])
    if not exp_tpsall:
        ta_ok = got_tpsall == []
    else:
        ta_ok = (len(got_tpsall) == len(exp_tpsall)
                 and all(_close(a, b) for a, b in zip(got_tpsall, exp_tpsall)))
    mark = "✅" if ta_ok else "❌"
    print(f"{mark} tps_all {desc}\n    → {got_tpsall} (기대 {exp_tpsall})")
    if ta_ok:
        ok += 1

# 타임프레임 파싱 + 판정 창 정책 (2026-07-23 B안)
from collector.extractor import judgment_window_hours, parse_timeframe_hours

TF_CASES = [
    ("🕒 Timeframe: 1H", 1.0), ("Time frame: 4h", 4.0), ("Timeframe:1D", 24.0),
    ("타임프레임: 15m", 0.25), ("daily chart looking good", 24.0), ("no tf here", None),
    # 2026-07-26 커버리지 추가 (수리 8b): 'weekly chart'/'주봉' 문구는 명시적
    # "Timeframe: Xw" 라벨이 아니라 _TIMEFRAME_WORD 로 168.0h 로 분기돼야 한다
    # (daily/일봉은 24.0h 분기와 대비).
    ("weekly chart looking bullish", 168.0), ("주봉 기준 상승추세", 168.0),
]
for text, exp in TF_CASES:
    got = parse_timeframe_hours(text)
    passed = got == exp
    print(("✅" if passed else "❌"), f"TF파싱 '{text[:20]}' → {got} (기대 {exp})")
    if passed:
        ok += 1

WINDOW_CASES = [
    # (tf_hours, entry, tp, 기대 시간, 설명)
    (1.0, 10, 11, 168.0, "1H봉 → 7일"),
    (4.0, 10, 11, 336.0, "4H봉 → 14일"),
    (24.0, 10, 11, 720.0, "1D봉 → 30일"),
    (None, 10, 12, 336.0, "TF없음+20%거리 → 14일"),
    (None, 10, 25, 720.0, "TF없음+150%거리 → 30일 상한"),
    (None, 10, None, 168.0, "TF·TP 둘다없음 → 기본 7일"),
]
for tf, e, tp, exp, desc in WINDOW_CASES:
    got = judgment_window_hours(tf, e, tp)
    passed = abs(got - exp) < 0.1
    print(("✅" if passed else "❌"), f"판정창 {desc} → {got:.0f}h")
    if passed:
        ok += 1

# 크기 sanity 방어선: 서수 오인이 어떤 신규 경로로 재발해도 4배/0.25배 밖 값은 차단
r = parse_setup("Long entry 0.083, target 1.0, SL 0.079", current_price=0.083)
guard_ok = r is not None and r["tp"] is None and _close(r["sl"], 0.079)
print(("✅" if guard_ok else "❌"), "크기 sanity - entry 대비 12배 목표는 판단보류(None)")
print(f"    → {r}")
if guard_ok:
    ok += 1
TOTAL_EXTRA = 1

# 2026-07-26 커버리지 추가 (수리 8a): current_price=None 이면 엔트리 sanity 는
# 항상 통과(판단보류)해야 한다는 게 _sanity()의 명시된 의도(현재가를 모르면 판정
# 불가 → 거르지 않고 그대로 채택) - 회귀로 이 값이 조용히 거부로 바뀌는 걸 막는다.
r_no_price = parse_setup("Long entry 999999, SL 950000, Target 1200000", current_price=None)
no_price_ok = r_no_price is not None and r_no_price["entry"] == 999999
print(("✅" if no_price_ok else "❌"),
      "current_price=None - sanity 판단보류로 통과(현재가와 무관하게 채택)")
print(f"    → {r_no_price}")
if no_price_ok:
    ok += 1
TOTAL_EXTRA += 1

# 2026-07-27 사다리형 목표(_LADDER) — 신규 소스 실사 중 발견.
# "TARGETS: 7.15 - 7.45 - 7.85 - 8.25" 같은 나열을 범위로 오인하면 tps[0][-1] 이
# 두 번째 목표를 집어 TP1 이 약 2배로 부풀려진다(ETC +4.7% → +9.1% 실측).
# 같은 하이픈이 ENTRY 줄에서는 진짜 범위라, 둘을 구분하는지가 핵심이다.
LADDER_CASES = [
    ("사다리 목표 - 첫 값이 TP1",
     "$ETC/USDT LONG\nENTRY: 6.81 - 6.85\nTARGETS: 7.15 - 7.45 - 7.85 - 8.25\nSTOP LOSS: 6.25",
     7.0, {"entry": 6.83, "sl": 6.25, "tp": 7.15}),
    ("사다리 목표 - 소수 자릿수가 섞인 표기",
     "$AERO LONG\nENTRY: 0.4080 - 0.4100\nTARGETS: 0.43 - 0.45 - 0.4750 - 0.50\nSTOP LOSS: 0.37",
     0.41, {"entry": 0.409, "sl": 0.37, "tp": 0.43}),
    # 값이 2개뿐인 진짜 범위는 종전대로 상단 — 사다리 판정이 과잉 적용되면 안 된다
    ("목표가 진짜 범위면 종전대로 상단",
     "Entry: 100\nTarget: 110 - 120\nSL: 95", 100.0,
     {"entry": 100.0, "sl": 95.0, "tp": 120.0}),
    # 화살표 구분자는 2개짜리도 사다리다 (2026-07-27 실전 VVV 글).
    # "110 - 120" 은 범위로도 읽히지만 "13.62 → 14.05" 는 순서일 수밖에 없다.
    ("화살표 2단계 - 첫 값이 TP1, 단계 2",
     "Direction: LONG | Entry: $13.27 | SL: $12.93 | TP: $13.62 → $14.05", 13.25,
     {"entry": 13.27, "sl": 12.93, "tp": 13.62}),
    ("화살표 3단계", "Entry: 10\nTP: 11 → 12 → 13", 10.0, {"entry": 10.0, "tp": 11.0}),
    # 엔트리 범위 뒤에 다른 라벨의 숫자가 이어져도 나열로 오인하지 않는다
    # (숫자 사이에 글자가 끼면 _LADDER 는 매칭되지 않는다)
    ("엔트리 범위는 나열로 오인하지 않음",
     "Entry: 6.81 - 6.85\nStop Loss: 6.25\nTarget: 7.50", 6.8,
     {"entry": 6.83, "sl": 6.25, "tp": 7.5}),
    # ── 2026-07-27 교차감사 B-M1: 사다리 파싱 비대칭 창 ──────────────────
    # 종전엔 _LADDER 도 30자 창을 써서, 라벨과 첫 숫자 사이 필러가 15자를 넘으면
    # 세 번째 rung 이 창 밖으로 밀려 _LADDER 매칭이 실패하고 _RANGE 로 떨어졌다
    # → tps[0][-1] 이 두 번째 값을 집어 "TP1 을 두 번째 목표로 오인"이 조건부로
    # 재발한다. 합성 재현으로 필러 16~19자 구간에서 tp 7.15 → 7.45 확인(수리 전).
    # 필러는 공백이 아니어야 재현된다 — 공백은 라벨 정규식의 \s* 가 먹어치워 숫자를
    # 창 밖으로 밀지 못한다. 수리: _LADDER 만 '첫 숫자가 놓인 줄의 끝'까지 본다.
    ("B-M1 필러 16자 - 사다리가 창 밖으로 밀려도 첫 목표",
     "$ETC LONG\nENTRY: 6.81 - 6.85\nTARGETS for this swing: 7.15 - 7.45 - 7.85 - 8.25\n"
     "STOP LOSS: 6.25", 7.0, {"entry": 6.83, "sl": 6.25, "tp": 7.15}),
    ("B-M1 필러 17자 - 동일",
     "$ETC LONG\nENTRY: 6.81 - 6.85\nTARGETS (partial exits): 7.15 - 7.45 - 7.85 - 8.25\n"
     "STOP LOSS: 6.25", 7.0, {"entry": 6.83, "sl": 6.25, "tp": 7.15}),
    ("B-M1 필러 19자 - 동일",
     "$ETC LONG\nENTRY: 6.81 - 6.85\nTARGETS (TP ladder below): 7.15 - 7.45 - 7.85 - 8.25\n"
     "STOP LOSS: 6.25", 7.0, {"entry": 6.83, "sl": 6.25, "tp": 7.15}),
    # 필러가 있어도 값이 2개뿐이면 진짜 범위 — 창을 넓힌 탓에 나열로 오탐하면 안 된다
    ("B-M1 필러 있는 진짜 범위는 종전대로 상단",
     "Entry: 100\nTarget for this swing: 110 - 120\nSL: 95", 100.0,
     {"entry": 100.0, "sl": 95.0, "tp": 120.0}),
    # 값이 다음 줄로 내려가는 표기(라벨 뒤 잡토큰으로 \s* 가 끊긴 경우)도 종전처럼
    # 사다리를 잡아야 한다 — 기준선이 '라벨 줄의 끝'이 아니라 '첫 숫자 줄의 끝'인 이유
    ("B-M1 값이 다음 줄인 사다리도 첫 목표",
     "$ETC LONG\nENTRY: 6.81 - 6.85\nTARGETS (all levels):\n7.15 - 7.45 - 7.85\n"
     "STOP LOSS: 6.25", 7.0, {"entry": 6.83, "sl": 6.25, "tp": 7.15}),
    # 라벨 뒤 개행 너머의 다른 라벨 숫자는 이 라벨 값으로 유입되지 않는다
    # (엔트리 줄은 단일값, 다음 줄의 목표 나열이 엔트리 사다리로 새면 entry 가 7.15 가 됨)
    ("B-M1 개행 너머 다른 라벨 나열은 유입 안 됨",
     "ENTRY: 6.83\nTARGETS: 7.15 - 7.45 - 7.85\nSTOP LOSS: 6.25", 6.83,
     {"entry": 6.83, "sl": 6.25, "tp": 7.15}),
]
for desc, text, price, exp in LADDER_CASES:
    r = parse_setup(text, current_price=price)
    passed = r is not None and all(
        r.get(k) is not None and _close(r[k], v) for k, v in exp.items())
    print(("✅" if passed else "❌"), desc)
    print(f"    → {r}")
    if passed:
        ok += 1

# ── 2026-07-27 프로덕션 실사고 3종: 가격이 아닌 숫자가 가격으로 읽힌 사례 ──────
# 전부 실제 저장된 레벨에서 발견됐다(채널 2호 실사 중 파생). 공통 원인은 하나 —
# 라벨 뒤 창에서 '가장 왼쪽 숫자'를 집는데, 그 자리에 가격이 아닌 숫자가 있었다.
# exp 의 값이 None 이면 "그 필드가 None 이어야 한다"는 뜻(가짜 값을 만들지 않음).
FAKE_NUMBER_CASES = [
    # ① R-멀티플: AVAX 산문에서 entry 라벨 뒤 "4R" 의 4 가 진입가로 → 실제가 $6.48 인
    #    코인에 $4.0 짜리 가짜 레벨이 감시 상태로 저장됐다(원문은 숏 분석이었다).
    ("실사고 AVAX - 산문의 4R 을 진입가로 오인하지 않는다",
     "AVAX bearish. the initial entry achieving a solid 4R return and more", 6.48,
     {"entry": None}),
    ("R-멀티플 소수·부호형도 제거", "Closed at +1.5R. Entry: 6.80", 6.8, {"entry": 6.80}),
    # ② 번호 목록 마커: "Entry :\n1) 1.1129" 에서 마커 1 이 진입가로.
    ("번호 목록 1) - 마커가 아니라 가격을 집는다",
     "Entry :\n1) 1.1129\n2) 1.1462\nTarget: 1.25", 1.11, {"entry": 1.1129, "tp": 1.25}),
    ("번호 목록 1. - 동일", "Entry:\n1. 0.5120\n2. 0.5340", 0.52, {"entry": 0.5120}),
    # ③ 두 단어 라벨의 서수: "Take Profit 1: $0.385" 가 기존 서수 제거에 안 걸려
    #    ONDO 목표가에 1.0 이 들어갔다(ALGO/ARB 사고의 미완 수리).
    ("실사고 ONDO - 'Take Profit 1:' 서수를 목표가로 오인하지 않는다",
     "Trading Levels\nEntry: $0.346-$0.350\nTake Profit 1: $0.385\nStop Loss: $0.321",
     0.35, {"entry": 0.348, "tp": 0.385, "sl": 0.321}),
    # ④ 스펙형 우선: 제목의 산문 라벨이 본문 스펙을 이기던 문제.
    ("실사고 ONDO - 제목 산문보다 본문 스펙(콜론)이 이긴다",
     "ONDO Breakout: Pullback Entry Toward $0.415\n\nTrading Levels\n"
     "Entry: $0.346-$0.350\nTake Profit 1: $0.385\nStop Loss: $0.321",
     0.35, {"entry": 0.348, "tp": 0.385}),
    # 스펙형이 하나도 없으면 종전대로 산문형이라도 쓴다(콜론 없이 쓰는 소스가 실재)
    ("스펙형이 없으면 산문형이라도 채택(회귀 방지)",
     "Long setup. Entry 10.5 with stop 9.8 and target 12.0", 10.5, {"entry": 10.5}),
    # 소수 가격이 목록 마커로 오인되지 않는지(구두점 뒤 공백 유무가 결정적)
    ("소수 가격 5.298 은 마커로 지워지지 않는다",
     "Entry: 5.298\nTP: 5.42", 5.3, {"entry": 5.298, "tp": 5.42}),
]
for desc, text, price, exp in FAKE_NUMBER_CASES:
    r = parse_setup(text, current_price=price)
    if all(v is None for v in exp.values()):
        passed = r is None or all(r.get(k) is None for k in exp)
    else:
        passed = r is not None and all(
            (r.get(k) is None if v is None else
             (r.get(k) is not None and _close(r[k], v))) for k, v in exp.items())
    print(("✅" if passed else "❌"), desc)
    print(f"    → {r}")
    if passed:
        ok += 1

# ── 2026-07-27 2차 교차검토: _LADDER 검색 창 200자 상한 (가용성 수리) ──────
# _LADDER 는 _NUM(내부 교대)을 {2,} 로 감싼 중첩 수량자라 긴 숫자 런에서 역추적이
# 2차식으로 폭주한다(실측: 200자 4.1ms / 3,200자 1,186ms / 16,000자 수십 초).
# B-M1 이 창을 30자 → '줄 끝'으로 넓히며 생긴 노출면이고, 수집기는 제3자가 쓴 임의
# 텍스트를 파싱한다 — 개행 없는 긴 숫자 줄 하나가 12분 하드킬 사정권의 회차를
# 통째로 태울 수 있었다. 상한 200자의 산정 근거는 extractor._LADDER_MAX_WINDOW 주석.
import time as _time  # noqa: E402

from collector import extractor as _ex  # noqa: E402

# ① 최악 입력에서 시간이 갇히는가.
# 폭주 입력은 '정상 사다리'가 아니라 **구분자 없는 긴 숫자 런**이다 — _NUM 의
# `[0-9]*\.[0-9]+|[0-9]+` 가 매 시작점마다 끝까지 먹었다가 되돌아오고, 구분자가
# 끝내 안 나오므로 O(n²) 가 그대로 실현된다(정상 사다리는 곧바로 매칭돼 싸다).
# _grab_after 를 직접 부르는 이유: parse_setup 전체를 재면 _clean/_PERCENT 등
# 다른 정규식 비용이 섞여 이 수리의 효과가 묻힌다(측정 확인: 683ms vs 1,835ms).
# 실측(같은 머신): 상한 있음 4.2ms / 없음 987ms — 230배.
_redos_text = "TARGETS: " + ("1" * 3200) + "\nSTOP LOSS: 990"
_t0 = _time.perf_counter()
_ex._grab_after(_ex._TP_LABEL, _redos_text)
_elapsed_ms = (_time.perf_counter() - _t0) * 1000
# 임계 200ms: 상한 실측(4.2ms)의 47배 여유 — 느린 CI 도 통과하고, 상한이 사라지면
# (987ms) 반드시 걸린다.
_redos_ok = _elapsed_ms < 200 and _ex._LADDER_MAX_WINDOW == 200
print(("✅" if _redos_ok else "❌"),
      f"ReDoS 방어 - 200자 초과 긴 숫자 런에서 시간 폭주 없음 ({_elapsed_ms:.1f}ms)")
if _redos_ok:
    ok += 1

# ② 상한을 넣어도 정상 사다리는 불변 — 첫 목표를 그대로 집는다
_r_cap = parse_setup(
    "$ETC LONG\nENTRY: 6.81 - 6.85\nTARGETS: 7.15 - 7.45 - 7.85 - 8.25\nSTOP LOSS: 6.25",
    current_price=7.0)
_cap_ok = (_r_cap is not None and _close(_r_cap["tp"], 7.15)
           and _close(_r_cap["entry"], 6.83) and _close(_r_cap["sl"], 6.25))
print(("✅" if _cap_ok else "❌"), "200자 상한을 넣어도 정상 사다리 판정 불변(TP1=7.15)")
print(f"    → {_r_cap}")
if _cap_ok:
    ok += 1

# ③ 200자를 훌쩍 넘는 '진짜' 긴 나열이어도 맨 앞 rung 은 상한 안쪽이라 결과 불변
# (잘림이 값을 바꾸지 않는다는 것이 이 상한을 정당화하는 근거의 절반이다)
_long_line = "TARGETS: " + " - ".join(f"{1005 + i * 0.01:.2f}" for i in range(400))
_r_long = parse_setup("$TEST LONG\nENTRY: 1000\n" + _long_line + "\nSTOP LOSS: 990",
                      current_price=1000.0)
_long_ok = _r_long is not None and _close(_r_long["tp"], 1005.0)
print(("✅" if _long_ok else "❌"), "창이 잘려도 맨 앞 rung 이 TP1 로 그대로 나온다")
print(f"    → tp={None if _r_long is None else _r_long['tp']}")
if _long_ok:
    ok += 1
TOTAL_EXTRA += 3

# 사다리 단계 수 (표시 전용 tp_ladder_count) — 값이 아니라 '몇 단계인가'를 본다.
LADDER_N_CASES = [
    ("화살표 2개 → 2단계", "Entry: 13.0\nTP: 13.62 → 14.05", 13.0, 2),
    ("하이픈 2개는 범위 → 0단계", "Entry: 100\nTarget: 110 - 120", 100.0, 0),
    ("하이픈 3개 → 3단계", "Entry: 10\nTargets: 11 - 12 - 13", 10.0, 3),
    ("단일 목표 → 0단계", "Entry: 10\nTarget: 12", 10.0, 0),
    # 줄바꿈 나열형 — 실측상 원문의 절반가량이 이 형태인데 종전엔 통째로 안 세졌다.
    ("줄바꿈 Target 1/2/3 → 3단계",
     "Entry Price: 100\nTarget 1: 110\nTarget 2: 120\nTarget 3: 130", 100.0, 3),
    ("줄바꿈 불릿 TP1~TP4 → 4단계",
     "Entry: 10\n• TP1: 11\n• TP2: 12\n• TP3: 13\n• TP4: 14", 10.0, 4),
    # 머리말이 첫 목표를 한 번 더 잡아도 단계가 부풀지 않는다(고유 값 기준).
    ("머리말 + TP1~TP3 → 4단계 아닌 3단계",
     "Entry: 10\nTake Profit Targets:\nTP1: 11\nTP2: 12\nTP3: 13", 10.0, 3),
    # 2026-07-27 JUP/JTO 실사고: 원문이 깨져 들어온 이상값은 단계로 세지 않는다.
    ("깨진 rung(엔트리 27배)은 단계에서 제외 → 4 아닌 3단계",
     "JUP Short setup\nEntry: 0.19\n• TP1: 0.185\n• TP2: 0.1811\n• TP3: 0.1768\n"
     "• TP4:\n5 *10", 0.19, 3),
    # 2026-07-27 SOL 오탐: 산문의 반복 언급은 사다리가 아니라 '원래안 vs 수정안'.
    ("산문 반복 목표는 사다리 아님 → 0단계",
     "SOLUSDT short, originally set with an entry at 100, take-profit at 92, "
     "and stop-loss at 104. The optimized take-profit is 94 instead.", 100.0, 0),
    # 2026-08-08 재검토: 한 줄 나열형(하이픈) 사다리도 tps_all 과 같은 크기
    # sanity(엔트리 0.25~4배)를 걸러야 한다 — 안 그러면 마지막 rung 이 4배
    # 초과로 tps_all 에서 탈락하는데 tp_ladder_count 는 원시 개수를 그대로
    # 써 "3/3단계"인데 실제 감시 목록은 2개뿐인 불일치가 생긴다.
    ("하이픈 사다리 중 마지막 rung 이 4배 초과(sanity 탈락) → 3 아닌 2단계",
     "$ETC/USDT LONG\nENTRY: 10\nTARGETS: 12 - 15 - 50\nSTOP LOSS: 8", 10.0, 2),
    # 2026-08-15 기대값 변경(왜): tp_ladder_count 가 v5 등급 산식에 들어가면서
    # (count<=1 이면 -3) 상한(12) 게이트를 제거 — 13단+ 진짜 사다리를 0(미상)으로
    # 저장하면 감점이 잘못 물린다. 이제 참 개수를 저장하고, "1/N" 표시 상한(12)은
    # notify/telegram.py 렌더러가 담당한다(아래 렌더 꼬리표 검증).
    ("줄바꿈 스펙 13단 사다리 → 0 아닌 13단 (v5: 참 개수 저장)",
     "Entry: 10\n" + "".join(f"TP{i}: {10 + i}\n" for i in range(1, 14)), 10.0, 13),
    ("하이픈 13단 사다리 → 0 아닌 13단 (인라인 분기도 상한 게이트 제거)",
     "Entry: 10\nTargets: " + " - ".join(str(10 + i) for i in range(1, 14)),
     10.0, 13),
]
for desc, text, price, exp_n in LADDER_N_CASES:
    r = parse_setup(text, current_price=price)
    got = (r or {}).get("tp_ladder_count", 0)
    passed = got == exp_n
    print(("✅" if passed else "❌"), f"단계수 {desc} → {got}")
    if passed:
        ok += 1

# ── 렌더러 "1/N" 꼬리표 상한 (2026-08-15) ─────────────────────────────
# 저장은 참 개수(위 13단 케이스)지만 표시는 2..12 에서만 붙는다 — 13단+ 알림
# 양식이 종전(0 저장 시절)과 바이트 단위로 동일해야 한다. 기존 렌더 스위트
# (test_price_logic T14l/T14n)는 8단·1단만 다뤄 12 초과 분기가 무검증이라
# 여기서 최소 단위로만 확인한다(스냅샷 스위트에 신규 렌더 테스트는 안 얹는다).
import time as _time
from notify import telegram as _tg
_rep = dict(coin_symbol="LINK", entry_usd=8.3, sl_usd=7.8, tp_usd=9.5, rr=2.4,
            grade="C", score=45, author="SomeChannel", author_followers=None,
            author_hit_rate=None, author_hit_count=None, author_whitelisted=False,
            mcap_rank=19, mcap_tier_icon="🥇", post_url="https://t.me/x/1",
            post_age_minutes=60, collected_at=_time.time(), source="telegram")
_USDT = 1400.0
# 2026-09-27 S2 D4: 단계 수 N 은 판정용 유효 TP 목록(tps_usd∪tp_usd) 길이라
# 사다리를 tps_usd 로 준다. 목표 행이 32칼럼을 넘으면 꼬리표는 값 칼럼 정렬 다음 줄.
_tps13 = "[" + ", ".join(f"{8.4 + 0.1 * i:.1f}" for i in range(12)) + ", 9.6]"
_tps8 = "[8.4, 8.5, 8.6, 8.7, 8.8, 8.9, 9.0, 9.5]"
_m13 = _tg.render_alert("touch", "LINK", [dict(_rep, tp_ladder_count=13, tps_usd=_tps13)],
                        8.35 * _USDT, _USDT)
_m8 = _tg.render_alert("touch", "LINK", [dict(_rep, tp_ladder_count=8, tps_usd=_tps8)],
                       8.35 * _USDT, _USDT)
_m8_strip = _m8.replace("\n" + _tg._VALUE_INDENT + "1/8", "").replace(" 1/8", "")
for _desc, _passed in [
        ("렌더 13단(>12)은 꼬리표 생략 — 종전 양식과 동일", "1/13" not in _m13
         and _m13 == _m8_strip),
        ("렌더 8단(≤12)은 종전대로 '1/8' 병기", "1/8" in _m8)]:
    print(("✅" if _passed else "❌"), _desc)
    if _passed:
        ok += 1
TOTAL_EXTRA += 2

# ── FB: 진입가 sanity 현재가 폴백 (2026-09-13 B1 회귀) ─────────────────
# 실전 사고 재현: CoinGecko 상위 N 밖 코인은 유니버스에 price_usd=None 으로 들어오고,
# _sanity 는 "현재가를 모르면 통과"라 Roddy01SIGNALSPROVIDER 채널 글의 레버리지
# 배수(12.5)가 GMT/MOODENG/MASK/KNC 진입가로 저장됐다(오알림 5건, 감시 중 4건).
# 아래는 그 원문 형태 그대로 — 현재가 없이는 entry=12.5 가 통과하고,
# 업비트 KRW 현재가 ÷ USDT-KRW 환율로 만든 폴백가를 주면 sanity 에서 탈락해야 한다.
from scripts.run_collect import _sanity_price, _usd_price_fallback  # noqa: E402

_FB_TEXT = (
    "#GMT/USDT\n"
    "Entry point: yellow zone\n\n"
    "👉Leverage: cross 12.5\n\n"
    "🎯Targets: 1-2-3-4-5%\n\n"
    "Stop-loss: 5%"
)
_FB_KRW = 73.2        # 업비트 KRW-GMT 현재가(원)
_FB_USDT_KRW = 1400.0  # KRW-USDT 환율 → USD 현재가 ≈ 0.0523

_fb_none = parse_setup(_FB_TEXT, current_price=None)
_fb_coin = {"symbol": "GMT", "price_usd": None,
            "price_usd_fallback": _usd_price_fallback(_FB_KRW, _FB_USDT_KRW)}
_fb_with = parse_setup(_FB_TEXT, current_price=_sanity_price(_fb_coin))
# 폴백이 CoinGecko 달러가를 덮어쓰지 않는지(우선순위) + 시세 결측 시 종전 동작 유지
_fb_cg = _sanity_price({"symbol": "GMT", "price_usd": 0.05,
                        "price_usd_fallback": 999.0})
for _desc, _passed in [
        # 2026-09-27 스프린트1: 원래 "현재가 없으면 12.5 통과(사고 재현)"였다. 이제는
        # 산문 라벨 근접 제약(라벨-숫자 25자·다른 라벨에서 끊기)과 레버리지 라벨 제거로
        # 추출기 자체가 12.5 를 줍지 않는다 — 폴백 sanity(FB2)는 2중 방어선으로 남는다.
        ("FB1 현재가 없어도 레버리지 12.5 를 진입가로 줍지 않음(2026-09-27 근본 수리)",
         _fb_none is None or abs(_fb_none["entry"] - 12.5) > 1e-9),
        ("FB2 업비트 폴백가를 주면 같은 글이 sanity 탈락(None)", _fb_with is None),
        ("FB3 폴백가 계산 = KRW 현재가 ÷ USDT-KRW 환율",
         abs(_usd_price_fallback(_FB_KRW, _FB_USDT_KRW) - _FB_KRW / _FB_USDT_KRW) < 1e-12),
        ("FB4 환율·시세 결측이면 폴백 None(종전 동작=판단보류)",
         _usd_price_fallback(None, _FB_USDT_KRW) is None
         and _usd_price_fallback(_FB_KRW, None) is None
         and _usd_price_fallback(0, 0) is None
         and _sanity_price({"symbol": "GMT", "price_usd": None}) is None),
        ("FB5 CoinGecko 달러가가 있으면 폴백보다 우선", _fb_cg == 0.05)]:
    print(("✅" if _passed else "❌"), _desc)
    if _passed:
        ok += 1
TOTAL_EXTRA += 5

# ── FB6~FB12: 진입가 sanity **비대칭** (2026-09-14, 감사 F3) ──────────────
# 위아래가 뜻하는 바가 다르다. 위(진입가 > 현재가)는 '이미 지나간 자리/오파싱'
# 이라 60% 로 좁게, 아래는 '가격이 오른 뒤의 깊은 눌림목 대기'라 정상이므로
# 현재가의 10% 미만(= 자릿수 오파싱)일 때만 기각한다.
# 이 비대칭이 없으면 실측 NEAR(진입 1,360원 vs 현재 3,138원 = −56.7%, 감시 중인
# 정상 레벨)가 커트에서 3.3%p 차이로 아슬아슬했다. FB6 이 그 경계를 박아둔다.
from collector.extractor import _sanity as _ext_sanity  # noqa: E402
for _desc, _passed in [
        ("FB6 하단 −56.7%(실측 NEAR)는 통과 — 대칭 60%면 여기서 죽는다",
         _ext_sanity(1.0, 2.31, 0.60) is True),
        ("FB7 하단 −80%(깊은 눌림목)도 통과", _ext_sanity(0.2, 1.0, 0.60) is True),
        ("FB8 하단 −95%(자릿수 오파싱 0.83→0.083)는 기각",
         _ext_sanity(0.05, 1.0, 0.60) is False),
        ("FB9 상단 +50% 통과 / +61% 기각(상단은 종전 60% 유지)",
         _ext_sanity(1.5, 1.0, 0.60) is True
         and _ext_sanity(1.61, 1.0, 0.60) is False),
        ("FB10 상단 +2552%(실측 오염 MASK)는 기각",
         _ext_sanity(26.52, 1.0, 0.60) is False),
        ("FB11 현재가 모르면 통과(판단보류 — 종전 계약 불변)",
         _ext_sanity(12.5, None, 0.60) is True
         and _ext_sanity(12.5, 0, 0.60) is True),
        ("FB12 value 가 None 이면 통과(판단보류)",
         _ext_sanity(None, 1.0, 0.60) is True)]:
    print(("✅" if _passed else "❌"), _desc)
    if _passed:
        ok += 1
TOTAL_EXTRA += 7

# ── SX: short 시그널 수집 배제 (2026-09-13 Q3, 사용자 결정) ────────────────
# 근거: v5 이후 direction='short' 119건(16.8%)이 터치 0건·알림 0건·outcome 전량
# NULL — monitor/price_check.py 가 long 레벨만 감시해 애초에 구조적으로 무의미
# (사용자는 업비트 KRW 현물 스윙 트레이더라 공매도 불가). config/settings.py 의
# "collect_short_enabled" 스위치(기본 False)로 신규 short 저장을 막는다.
# 여기선 _ingest_idea 를 직접 호출해 스위치 3가지 조합(False+short/False+long/
# True+short)을 검증한다 - test_resilience.py 의 몽키패치 스타일을 그대로 따름.
import sqlite3 as _sqlite3  # noqa: E402
from scripts.run_collect import _ingest_idea  # noqa: E402
from config import settings as _sx_settings  # noqa: E402
from storage import db as _sx_db  # noqa: E402

_SX_DB = "cache/_test_extractor_short_exclusion.db"
import os as _os  # noqa: E402
if _os.path.exists(_SX_DB):
    _os.remove(_SX_DB)
_sx_db.init_db(_SX_DB)

_SX_COIN = {"symbol": "SXT", "ticker": "KRW-SXT", "rank": 50, "name": "SX Test",
            "price_usd": 10.0, "tier_icon": "🥈"}


def _sx_idea(url, author="SXAuthor"):
    return {"title": "SXT setup", "description": "setup text", "author": author,
            "url": url, "age_minutes": 5, "author_followers": 100}


def _sx_make_parse_setup(direction):
    def _p(text, current_price=None):
        return {"direction": direction, "entry": 10.0, "entry_low": 10.0,
                "entry_high": 10.0, "sl": 9.0 if direction == "long" else 11.0,
                "tp": 12.0 if direction == "long" else 8.0, "rr": 2.0}
    return _p


from scripts import run_collect as _sx_rc  # noqa: E402
_sx_old_parse_setup = _sx_rc.parse_setup
_sx_old_grade = _sx_rc.calculate_grade_with_breakdown
_sx_rc.calculate_grade_with_breakdown = lambda *a, **k: ("B", 60, 2.0, {})
_sx_rc.judgment_window_hours = lambda *a, **k: 168.0
_sx_rc.parse_timeframe_hours = lambda text: None

_sx_old_switch = _sx_settings.SETTINGS.get("collect_short_enabled")
try:
    # SX1: 스위치 False + short → 저장되지 않는다
    _sx_settings.SETTINGS["collect_short_enabled"] = False
    _sx_rc.parse_setup = _sx_make_parse_setup("short")
    with _sx_db.connect(_SX_DB) as conn:
        _sx_had, _sx_new = _ingest_idea(conn, _SX_COIN, _sx_idea("u-sx1"), {}, 5.0)
        conn.commit()
        _sx_rows1 = conn.execute(
            "SELECT COUNT(*) c FROM levels WHERE post_url='u-sx1'").fetchone()["c"]

    # SX2: 같은 조건에서 long 셋업은 정상 저장된다
    _sx_rc.parse_setup = _sx_make_parse_setup("long")
    with _sx_db.connect(_SX_DB) as conn:
        _sx_had2, _sx_new2 = _ingest_idea(conn, _SX_COIN, _sx_idea("u-sx2"), {}, 5.0)
        conn.commit()
        _sx_rows2 = conn.execute(
            "SELECT COUNT(*) c FROM levels WHERE post_url='u-sx2'").fetchone()["c"]

    # SX3: 스위치를 True 로 되돌리면 short 도 다시 저장된다(원복 가능성 보장)
    _sx_settings.SETTINGS["collect_short_enabled"] = True
    _sx_rc.parse_setup = _sx_make_parse_setup("short")
    with _sx_db.connect(_SX_DB) as conn:
        _sx_had3, _sx_new3 = _ingest_idea(conn, _SX_COIN, _sx_idea("u-sx3"), {}, 5.0)
        conn.commit()
        _sx_rows3 = conn.execute(
            "SELECT COUNT(*) c FROM levels WHERE post_url='u-sx3'").fetchone()["c"]
finally:
    _sx_rc.parse_setup = _sx_old_parse_setup
    _sx_rc.calculate_grade_with_breakdown = _sx_old_grade
    if _sx_old_switch is None:
        _sx_settings.SETTINGS.pop("collect_short_enabled", None)
    else:
        _sx_settings.SETTINGS["collect_short_enabled"] = _sx_old_switch

for _desc, _passed in [
        ("SX1 스위치 False + short → 저장 안 됨(had_setup=True, is_new=False, 행 0)",
         _sx_had is True and _sx_new is False and _sx_rows1 == 0),
        ("SX2 같은 조건 long → 정상 저장(신규, 행 1)",
         _sx_had2 is True and _sx_new2 is True and _sx_rows2 == 1),
        ("SX3 스위치 True 로 되돌리면 short 도 저장(원복 가능성)",
         _sx_had3 is True and _sx_new3 is True and _sx_rows3 == 1)]:
    print(("✅" if _passed else "❌"), _desc)
    if _passed:
        ok += 1
TOTAL_EXTRA += 3

# ── S1: 2026-09-27 감사(종합 P0-1·2·4·5, A-T5) 회귀 — 원문 발췌 픽스처 ─────────
# 감사 문서: izrua_company/audit_2026-09-27_C_parsing_semantics.md(부록 판정표),
# audit_2026-09-27_A_price_geometry.md(T3·T4·T5). 괄호 안 숫자 = levels.id.
from collector.extractor import nonsetup_reason as _s1_ns  # noqa: E402
from config import settings as _s1_settings  # noqa: E402

# D — 숏 글이 롱으로 저장되던 유형
_S1_699 = (  # 699 ETH: "Entry (Short)" + "long upper wick" 공존 → 종전 long
    "ETHUSDT: Bearish Drop to 2240?\nBINANCE:ETHUSDT  is eyeing a  bearish  continuation on  "
    "the 1-hour chart  after sweeping liquidity above the equal highs with a long upper wick "
    "in the greed zone.\n\n🎯 Entry (Short):\n2,590 – 2,630\n\n🎯 Target:\n2,240\n\n"
    "❌ Stop Loss:\n4-hour close above 2,670")
_S1_682 = (  # 682 FIL: 방향 단어 없음 + Target 1~3 전부 진입가 아래 → 종전 long
    "#FILUSDT  may continue its trend after correction\n#FIL\n\nThe price is moving within a "
    "bearish channel on the 1-hour timeframe. This setup supports a decline toward that "
    "level.\n\nEntry Price: 0.8000\nTarget 1: 0.7785\nTarget 2: 0.7611\nTarget 3: 0.7413\n\n"
    "Stop Loss: At the green resistance zone.")
_S1_636 = (  # 636 ETH: "short setup" + "as long as" → 종전 long
    "My main scenario is therefore a short setup targeting the lower boundary of the range. "
    "There are several support areas on the way down, but as long as the rectangle remains "
    "valid, I believe price has the potential to reach the lower boundary around $2,419.\n\n"
    "The potential entry for this setup is below the current candle, around $2,506.\n\nBias: Short")
_S1_792 = (  # 792 BTC: Targets 가 번호 목록(첫 값만 잡힘) + SL 이 진입가 위
    "BTC - Ultimate Swing Short - Rev 2\nEntry - 84,800-85,000 (Channel Top) \n\n"
    "Stop Loss - 93,300 (as close to) \n\nTargets (Revised): \n\n1) 72,300\n\n2) 64,650\n\n"
    "Suggest the following buy-back zone (IE long entry or spot buy of BTC)")
# "as long as" 가 들어간 **롱** 글은 롱 유지(힌트 잡음 제거가 롱을 뒤집지 않는다)
_S1_ASLONG = ("SOL bullish continuation as long as 90 holds.\nBuy zone: 101 - 105\n"
              "SL: 95\nTP: 115 / 125")
_S1_ASLONG2 = ("The trend stays intact as long as the weekly support holds; long-term I stay "
               "bullish.\nEntry: 0.450\nStop: 0.410\nTarget 1: 0.520\nTarget 2: 0.600")

# L/T — 서수·비율·레버리지·타임프레임·기간 숫자
_S1_878 = (  # 878 ONDO: "Target 1 (TP1):" → 종전 tp=1.0
    "• Entry Zone: Around $0.5599 (Trading the live structural support hold) \n"
    "• Invalidation (Stop Loss): $0.5401 (Placed mechanically below core support)  .\n"
    "• Target 1 (TP1): $0.5843 (Immediate horizontal resistance shelf) \n"
    "• Target 2 (TP2): $0.6111 (Primary swing target)")
_S1_643TP = ("SUI long plan\nEntry: 0.7000\nStop Loss: 0.6800\n"
             " * Take Profit 1 (TP1): 0.7460 (approx. move)\n"
             " * Take Profit 2 (TP2 / Final Target): 0.7950")  # 643 SUI 괄호 서수 표기
_S1_772 = (  # 772 FIL: "R/R:2" → 종전 tp=2.0
    " 🎯 Entry & Exit Signal: \nBuy Entry: 0.9138\nStop Loss: 0.8900\n"
    "Targets: 0.9614 R/R:2 - 0.9640 - 1.0144\n")
_S1_656 = (  # 656 PROVE: "(M15 Entry / H1" + "RR  1:1" → 종전 진입 1.0
    "PROVEUSDT - Trade Analysis (M15 Entry / H1 + H4 Bias)\nThis is the **buy limit zone**.\n\n"
    "Trade Setup:\n\nBuy Limit Zone:  0.1948 - 0.1962 \nSL\"  Below 0.1854 (structural support) \n"
    "TP: 0.2045+ \nRR  1:1 \n")
_S1_671 = (  # 671 MASK (Roddy01 레버리지 템플릿): "Leverage x 5-10-20" → 종전 진입 12.5
    "maskusdt long\nInstructions:\n\nEntry point: yellow\nStop loss: red\nTake profit: green\n\n"
    "👉Leverage x 5-10-20 for crypto\n👉Leverage x 20-50-100 for commodities, stocks, indices, "
    "and forex\n👉Margin 1-5% max.\nAlways practice risk and money management.")
_S1_LEV = "ETC long\nEntry: 8.20\nLev 10x | Leverage x 5-10-20\nSL: 7.90\nTP: 8.90"
_S1_575 = (  # 575 ZORA: "Enter … Over The Next 30 Days" → 종전 진입 30.0
    "If The Historical Structure Repeats, $ZORA Could Enter Another Major Expansion Phase "
    "Over The Next 30 Days, With A Potential 1000%+ Move From The Breakout Base.")
_S1_TF = "Entry 4H 0.19 (retest)\nSL: 0.17\nTP: 0.23"  # 타임프레임 토큰만 지우고 가격 보존

# B — 돌파 트리거
_S1_884 = (  # 884 SUI 원문 발췌: "weekly close above $1.42" + "Re-Entry" → 종전 진입 1.42
    "$SUI $1.42 BREAKOUT COULD OPEN THE ROAD TO $10–$20\nBut the Real HTF Trend-Change level is "
    "$1.42\nA weekly close above $1.42 would invalidate the bearish structure and activate my "
    "upside targets.\n\nTargets: $2.65 → $5.36 → $10 → $20\n\nIf  CRYPTOCAP:SUI  gets rejected "
    "below $1.42, another retest of the $0.84–$0.70 Accumulation zone remains possible, "
    "potentially creating another high-quality Re-Entry opportunity.\n\n$1.42 = THE KEY LEVEL "
    "FOR SUI’S MACRO TREND FLIP. \n\nNFA & DYOR")
_S1_807 = ("XRP/USDT Trade Analaysis\nH4:Bullish\nEntry: 1.4968 Buy Stop\nSl: 1.3901\n"
           "Take profit:1.7231\nRisk:0.50%\nRR: 1:2")  # 807 XRP
_S1_637 = ("3. Trade Setup\n\nEntry: 2,526.97 (Confirmed 1H close breaking above horizontal "
           "range resistance)\n\nStop Loss (SL): 2,439.75 (Placed safely below)")  # 637 ETH
_S1_804 = (  # 804 SUI: 리테스트 진입은 예외(정상 눌림목)
    "Trade Setup & Key Levels:\n\nEntry Zone: $0.98 – $1.03 (Breakout retest / Current market "
    "range)   \n\nStop Loss (SL): $0.7600 (Dynamic structural invalidation)\n\nTP1: $1.30")
_S1_855 = (  # 855 ETH: "breaks above"가 근처에 있어도 retest 동반이면 진입 유지
    "Entry Zone: $2,665 – $2,685 (Breakout retest above the reclaimed range)\n\n"
    "Stop Loss (SL): $2,447.19 (Below structural support)\n\nTP1: $2,800")

# R/C — 비셋업 글
_S1_835 = ("$PENGU +91% From Our Entry | Is The Next Move A 10X Rally?\n CSECY:PENGU  Is Currently "
           "Trading Around $0.01117, Up ~90% From Our Entry Zone.")  # 835 PENGU
_S1_881 = ("NYSE:PUMP  +208% PROFIT: Our Entry Filled,  NYSE:PUMP  Just Hit $0.0055.\n\n"
           "That’s +208% From Our Entry.")  # 881 PUMP
_S1_678 = ("SCENARIOS (to watch — NOT signals)\n📈 Bullish: hold 77,700 and reclaim 77,884 → "
           "room toward 77,990")  # 678 BTC
_S1_774 = ("$IO Quick  +16% long opportunity\nBuy zone: 0.1300 - 0.1420\nSL: 0.1250\nTP: 0.1550\n\n"
           "(Not a Trading signal or Financial Advice)\n(Always Do Your Own Research as well, DYOR)\n"
           "(Not to FOMO)")  # 774 IO — 면책문 있는 정상 셋업
_S1_DISC = "Entry: 2.10\nSL: 1.95\nTP: 2.40\nThis is not a signal, DYOR."
_S1_TPCOND = "Entry: 2.10\nSL: 1.95\nTP1: 2.40\nOnce TP1 hit, move SL to entry."
_S1_665 = ("#COREUSDT / Ready to go up\n#CORE\n\nThe price is moving within a bearish channel.\n\n"
           "Entry Price: 0.06100\nTarget 1: 0.06200\nTarget 2: 0.06344\n")  # 665 (URL=CROUSDT)
_S1_867 = ("XRP holds support\nMacro backdrop first.\n\nWatching CRYPTOCAP:BTC and CRYPTOCAP:ETH "
           "for direction; CRYPTOCAP:SOL lagging.\nEntry: 1.49\nSL: 1.40\nTP: 1.70")  # 867 XRP — 본문 자동링크

# Z — 창 절단·범위
_S1_612 = ("Traders can enter from here with minor amount and enter more when its below entry.\n\n"
           "Trumpsdt is at the retest zone so it can fly anytime.\n\nBuy Trumpusdt from 2.16\n\n"
           "Stoploss 1.852 (-14.2%)\n\nTarget 2.864(+32.7%)")  # 612 TRUMP — 종전 2.1
_S1_EDGE = "Entry: (" + "w" * 77 + ") 2.16\nSL: 1.9\nTP: 2.6"  # 80자 경계에 숫자
_S1_646 = ("Entry zone: $0.16 to $0.10 — I scale in, no need to go all at once.\n"
           "Invalidation: a 3D close below $0.07.\nTarget: $1.16")  # 646 ARB — 종전 0.16

# T5 — 대표 TP = 유효 사다리 첫 값
_S1_630 = ("Entry Price: 1.067\nTarget 1: 1.20\nTarget 2: 1.16\nTarget 3: 1.22\n\n"
           "Stop Loss: At the resistance zone in green")  # 630 ZRO — 종전 tp=1.20
_S1_852 = ("Possible long plan\n Entry 0.001258\n Stop Loss  below 0.001190\n TP  0.001335\n"
           " TP  0.001394\n")


def _s1p(text, price=None, **kw):
    return parse_setup(text, current_price=price, **kw)


def _s1_close(a, b, tol=1e-6):
    return a is not None and b is not None and abs(a - b) <= tol * max(1.0, abs(b))


_s1_old_tv = _s1_settings.SETTINGS.get("extract_use_tv_direction")
_s1_old_bt = _s1_settings.SETTINGS.get("extract_breakout_trigger_skip")
_s1_old_ns = _s1_settings.SETTINGS.get("extract_nonsetup_skip")
_S1_CASES = []
try:
    _S1_CASES += [
        # D 방향
        ("S1-D1 699 'Entry (Short)'+'long upper wick' → short",
         (_s1p(_S1_699) or {}).get("direction") == "short"),
        ("S1-D2 682 방향 단어 없음 + TP 3개 전부 진입가 아래 → short",
         (_s1p(_S1_682) or {}).get("direction") == "short"),
        ("S1-D3 636 'short setup'+'as long as' → short",
         (_s1p(_S1_636) or {}).get("direction") == "short"),
        ("S1-D4 792 TP 1개(아래)+SL 진입가 위 → short",
         (_s1p(_S1_792) or {}).get("direction") == "short"),
        ("S1-D5 'as long as' 롱 글은 long 유지",
         (_s1p(_S1_ASLONG) or {}).get("direction") == "long"
         and _s1_close((_s1p(_S1_ASLONG) or {}).get("entry"), 103.0)),
        ("S1-D6 'as long as'+'long-term' 롱 글 long 유지(TP 0.52)",
         (_s1p(_S1_ASLONG2) or {}).get("direction") == "long"
         and _s1_close((_s1p(_S1_ASLONG2) or {}).get("tp"), 0.52)),
        ("S1-D7 작성자 태그 short 가 롱처럼 보이는 텍스트보다 우선",
         (_s1p(_S1_ASLONG, direction_hint="short") or {}).get("direction") == "short"),
        ("S1-D8 작성자 태그 long 은 TP 기하(숏 판정)보다 우선",
         (_s1p(_S1_682, direction_hint="long") or {}).get("direction") == "long"),
        # L/T 숫자 오인
        ("S1-T1 878 'Target 1 (TP1): $0.5843' → tp 0.5843(종전 1.0)",
         _s1_close((_s1p(_S1_878) or {}).get("tp"), 0.5843)),
        ("S1-T2 'Take Profit 1 (TP1): 0.7460' → tp 0.7460",
         _s1_close((_s1p(_S1_643TP) or {}).get("tp"), 0.7460)),
        ("S1-T3 772 'R/R:2' 제거 → tp 0.9614(종전 2.0), 3단 사다리",
         _s1_close((_s1p(_S1_772) or {}).get("tp"), 0.9614)
         and (_s1p(_S1_772) or {}).get("tp_ladder_count") == 3),
        ("S1-L1 656 'M15/H1'·'RR 1:1' 제거 → 진입 0.1955(종전 1.0), tp 0.2045",
         _s1_close((_s1p(_S1_656) or {}).get("entry"), 0.1955)
         and _s1_close((_s1p(_S1_656) or {}).get("tp"), 0.2045)),
        ("S1-L2 671 Roddy01 레버리지 템플릿 → 셋업 없음(종전 12.5)", _s1p(_S1_671) is None),
        ("S1-L3 'Lev 10x | Leverage x 5-10-20' 가 있어도 진입 8.20 보존",
         _s1_close((_s1p(_S1_LEV) or {}).get("entry"), 8.20)
         and _s1_close((_s1p(_S1_LEV) or {}).get("sl"), 7.90)),
        ("S1-L4 575 'Next 30 Days' → 셋업 없음(종전 30.0)", _s1p(_S1_575) is None),
        ("S1-L5 'Entry 4H 0.19' → 4 가 아니라 0.19", _s1_close((_s1p(_S1_TF) or {}).get("entry"), 0.19)),
        # B 돌파 트리거
        ("S1-B1 884 SUI 원문 'weekly close above $1.42' → 진입 없음(종전 1.42)", _s1p(_S1_884) is None),
        ("S1-B2 807 'Entry: 1.4968 Buy Stop' → 진입 없음", _s1p(_S1_807) is None),
        ("S1-B3 637 'Confirmed 1H close breaking above' → 진입 없음", _s1p(_S1_637) is None),
        ("S1-B4 804 'Breakout retest' 존은 리테스트 예외 → 진입 1.005",
         _s1_close((_s1p(_S1_804) or {}).get("entry"), 1.005)),
        ("S1-B5 855 'retest above the reclaimed range' 도 예외 → 진입 2675",
         _s1_close((_s1p(_S1_855) or {}).get("entry"), 2675.0)),
        # R/C 비셋업
        ("S1-R1 835 '+91% From Our Entry' → result_report", _s1_ns(_S1_835, "PENGU") == "result_report"),
        ("S1-R2 881 '+208% PROFIT: Our Entry Filled' → result_report",
         _s1_ns(_S1_881, "PUMP") == "result_report"),
        ("S1-R3 678 'NOT signals' → not_signal", _s1_ns(_S1_678, "BTC") == "not_signal"),
        ("S1-R4 774 면책문 '(Not a Trading signal or Financial Advice)' 정상 셋업은 통과",
         _s1_ns(_S1_774, "IO") is None and _s1_close((_s1p(_S1_774) or {}).get("entry"), 0.136)),
        ("S1-R5 'not a signal, DYOR' 면책 문장은 통과", _s1_ns(_S1_DISC, "SXT") is None),
        ("S1-R6 조건절 'Once TP1 hit, move SL' 은 결과보고 아님", _s1_ns(_S1_TPCOND, "SXT") is None),
        ("S1-C1 665 URL=CRO, 본문 #COREUSDT/#CORE → coin_mismatch",
         _s1_ns(_S1_665, "CRO") == "coin_mismatch"),
        ("S1-C2 같은 글을 CORE 로 수집하면 통과", _s1_ns(_S1_665, "CORE") is None),
        ("S1-C3 867 본문 자동링크(CRYPTOCAP:BTC 등)만 있는 XRP 글은 통과",
         _s1_ns(_S1_867, "XRP") is None),
        ("S1-C4 별칭 PUMPFUN 태그 ↔ PUMP 코인은 일치로 본다",
         _s1_ns("#PUMPFUNUSDT long\nEntry: 0.004\nTP: 0.005", "PUMP") is None),
        # Z 창 절단·범위
        ("S1-Z1 612 'Buy Trumpusdt from 2.16' → 2.16(종전 2.1)",
         _s1_close((_s1p(_S1_612) or {}).get("entry"), 2.16)),
        ("S1-Z2 80자 창 경계의 숫자를 끝까지 읽음(2.16)",
         _s1_close((_s1p(_S1_EDGE) or {}).get("entry"), 2.16)),
        ("S1-Z3 646 'Entry zone: $0.16 to $0.10' → 범위 0.10~0.16(중앙 0.13)",
         _s1_close((_s1p(_S1_646) or {}).get("entry"), 0.13)
         and _s1_close((_s1p(_S1_646) or {}).get("entry_low"), 0.10)),
        # T5 TP 순서
        ("S1-O1 630 'Target 1: 1.20 / Target 2: 1.16' → tp=1.16=tps_all[0](종전 1.20)",
         _s1_close((_s1p(_S1_630) or {}).get("tp"), 1.16)
         and (_s1p(_S1_630) or {}).get("tps_all", [None])[0] == (_s1p(_S1_630) or {}).get("tp")),
        ("S1-O2 tp 는 판정부 _volume_band_tps 와 같은 첫 값(오름차순 유효 TP)",
         (lambda s: s is not None and s["tp"] == min(s["tps_all"]))(_s1p(_S1_852))),
    ]
    # 롤백 스위치: 끄면 종전 동작(트리거 스킵·비셋업 스킵·태그 무시)으로 돌아간다
    _s1_settings.SETTINGS["extract_breakout_trigger_skip"] = False
    _s1_settings.SETTINGS["extract_nonsetup_skip"] = False
    _s1_settings.SETTINGS["extract_use_tv_direction"] = False
    _S1_CASES += [
        ("S1-SW1 extract_breakout_trigger_skip=False → 807 진입 1.4968 복귀",
         _s1_close((_s1p(_S1_807) or {}).get("entry"), 1.4968)),
        ("S1-SW2 extract_nonsetup_skip=False → 결과보고도 통과", _s1_ns(_S1_835, "PENGU") is None),
        ("S1-SW3 extract_use_tv_direction=False → 태그 무시, 텍스트(long)",
         (_s1p(_S1_ASLONG, direction_hint="short") or {}).get("direction") == "long"),
    ]
finally:
    for _k, _v in (("extract_use_tv_direction", _s1_old_tv),
                   ("extract_breakout_trigger_skip", _s1_old_bt),
                   ("extract_nonsetup_skip", _s1_old_ns)):
        if _v is None:
            _s1_settings.SETTINGS.pop(_k, None)
        else:
            _s1_settings.SETTINGS[_k] = _v

for _desc, _passed in _S1_CASES:
    print(("✅" if _passed else "❌"), _desc)
    if _passed:
        ok += 1
TOTAL_EXTRA += len(_S1_CASES)

# S1-I: _ingest_idea 통합 — 작성자 태그 short 는 저장 안 됨, 비셋업 글은 스킵 집계
_s1_rc_parse = _sx_rc.parse_setup
_s1_rc_grade = _sx_rc.calculate_grade_with_breakdown
_sx_rc.calculate_grade_with_breakdown = lambda *a, **k: ("B", 60, 2.0, {})
_s1_skip = {}
try:
    with _sx_db.connect(_SX_DB) as conn:
        _i1 = dict(_sx_idea("u-s1-tvshort"), title="SXT plan",
                   description="SXT bullish as long as 9 holds.\nEntry: 10.0\nSL: 9.5\nTP: 11.0",
                   direction="short")
        _s1_h1, _s1_n1 = _ingest_idea(conn, _SX_COIN, _i1, {}, 5.0, skip_counts=_s1_skip)
        _i2 = dict(_sx_idea("u-s1-result"), title="SXT +50% From Our Entry",
                   description="Entry: 10.0\nSL: 9.5\nTP: 11.0", direction=None)
        _s1_h2, _s1_n2 = _ingest_idea(conn, _SX_COIN, _i2, {}, 5.0, skip_counts=_s1_skip)
        _i3 = dict(_sx_idea("u-s1-long"), title="SXT plan",
                   description="Entry: 10.0\nSL: 9.5\nTP: 11.0", direction="long")
        _s1_h3, _s1_n3 = _ingest_idea(conn, _SX_COIN, _i3, {}, 5.0, skip_counts=_s1_skip)
        conn.commit()
        _s1_rows = {r["post_url"] for r in conn.execute(
            "SELECT post_url FROM levels WHERE post_url LIKE 'u-s1-%'")}
finally:
    _sx_rc.parse_setup = _s1_rc_parse
    _sx_rc.calculate_grade_with_breakdown = _s1_rc_grade
for _desc, _passed in [
        ("S1-I1 작성자 태그 short → 저장 안 됨(had_setup=True, short 집계)",
         _s1_h1 is True and _s1_n1 is False and "u-s1-tvshort" not in _s1_rows
         and _s1_skip.get("short") == 1),
        ("S1-I2 결과보고 글 → 저장 안 됨(nonsetup 집계)",
         _s1_h2 is True and _s1_n2 is False and "u-s1-result" not in _s1_rows
         and _s1_skip.get("nonsetup") == 1),
        ("S1-I3 태그 long 정상 셋업은 저장", _s1_n3 is True and "u-s1-long" in _s1_rows)]:
    print(("✅" if _passed else "❌"), _desc)
    if _passed:
        ok += 1
TOTAL_EXTRA += 3

# ── RV2-E1~E6: 2026-09-27 코드 리뷰 확정 결함 회귀 (추출기 P0 수리 후속) ──────────
import time as _rv2_time
from collector import extractor as _rv2_ex


def _rv2_k(t):
    r = _rv2_ex.parse_setup(t)
    return None if not r else (r["direction"], r["entry"], r["sl"], r["tp"])


_rv2_rr_t0 = _rv2_time.perf_counter()
_rv2_ex.parse_setup("RR" + " " * 1600 + ":")
_rv2_ex.parse_setup("risk reward" + " " * 1600 + "x")
_rv2_rr_dt = _rv2_time.perf_counter() - _rv2_rr_t0
_rv2_tf_t0 = _rv2_time.perf_counter()
_rv2_ex.parse_setup("Target 1" + " " * 4000 + "(")
_rv2_tf_dt = _rv2_time.perf_counter() - _rv2_tf_t0
_RV2_EX = [
    # E1: 1~2자리 가격 뒤 괄호·at 은 서수가 아니다
    ("RV2-E1a 'Entry 45 (support zone)' 진입가 45 보존",
     _rv2_k("SOL long setup\nEntry 45 (support zone)\nSL: 40\nTP: 55") == ("long", 45.0, 40.0, 55.0)),
    ("RV2-E1b 'Target 5 (range high)' TP 5 보존",
     _rv2_k("ATOM long\nEntry: 4.10\nSL: 3.90\nTarget 5 (range high)\nTarget 6 (ATH)")
     == ("long", 4.1, 3.9, 5.0)),
    ("RV2-E1c 'Target 15 at resistance' TP 15 보존",
     _rv2_k("LINK long\nEntry: 12.5\nStop: 11.9\nTarget 15 at resistance") == ("long", 12.5, 11.9, 15.0)),
    ("RV2-E1d 'TP 25 (first target)' TP 25 보존",
     _rv2_k("AVAX long\nEntry: 20.5\nSL: 19\nTP 25 (first target)\nTP 28 (second)")
     == ("long", 20.5, 19.0, 25.0)),
    ("RV2-E1e 진짜 서수는 여전히 제거('Take Profit 1 (TP1): 0.7460'·'Target 1 at $2,507.06')",
     _rv2_k("SUI long\nEntry: 0.70\nSL: 0.66\nTake Profit 1 (TP1): 0.7460")[3] == 0.746
     and _rv2_k("ETH long\nEntry: 2400\nSL: 2300\nTarget 1 at $2,507.06")[3] == 2507.06
     and _rv2_k("ETH long\nEntry: 2400\nSL: 2300\nTarget Two (R2): $2,714.10")[3] == 2714.1),
    # E2: 스펙형 진입가의 트리거 문맥은 같은 줄만
    ("RV2-E2a 옆 줄(TP 줄)의 'reclaim' 이 스펙형 진입을 기각하지 않는다",
     _rv2_k("ETH long\nEntry: 2500\nSL: 2400\nTP1: 2700 - reclaim of the range high")
     == ("long", 2500.0, 2400.0, 2700.0)),
    ("RV2-E2b 같은 줄 트리거('Entry: 1.4968 Buy Stop'·'Entry: 5.20 after reclaim')는 여전히 기각",
     _rv2_k("XYZ long\nEntry: 1.4968 Buy Stop\nSL: 1.40\nTP: 1.60") is None
     and _rv2_k("APT long\nEntry: 5.20 after reclaim\nSL: 4.90\nTP: 6.00") is None),
    # E3: 목표 수익률 주석·조건절은 결과보고 아님
    ("RV2-E3a 'TP1: 0.22 (+10% profit)' 은 result_report 아님",
     _rv2_ex.nonsetup_reason("DOGE long setup\nEntry: 0.20\nSL: 0.19\nTP1: 0.22 (+10% profit)\n"
                             "TP2: 0.24 (+20% profit)", "DOGE") is None),
    ("RV2-E3b 'Target: 2.40 for a +20% gain' 은 result_report 아님",
     _rv2_ex.nonsetup_reason("Long XRP\nEntry: 2.00\nSL: 1.90\nTarget: 2.40 for a +20% gain", "XRP") is None),
    ("RV2-E3c 'TP1 hit -> move SL to entry' 은 조건절",
     _rv2_ex.nonsetup_reason("SUI long\nEntry: 3.00\nSL: 2.80\nTP1: 3.30\nTP2: 3.60\n"
                             "TP1 hit -> move SL to entry", "SUI") is None),
    ("RV2-E3d 진짜 결과보고('TP1 hit, +10% profit ✅')는 유지",
     _rv2_ex.nonsetup_reason("BTC long\nTP1 hit, +10% profit ✅", "BTC") == "result_report"),
    # E4: 흔한 해시태그·본문 참조 티커는 불일치 아님
    ("RV2-E4a '#CRYPTO #TRADING' 은 coin_mismatch 아님",
     _rv2_ex.nonsetup_reason("#CRYPTO #TRADING\nETH long\nEntry: 2500\nSL: 2400\nTP: 2700", "ETH") is None),
    ("RV2-E4b 머리에서 수집 코인을 이름으로 적은 글('Watch $BTC'·'BINANCE:BTCUSDT')은 불일치 아님",
     _rv2_ex.nonsetup_reason("ETH long idea. Watch $BTC for confirmation.\nEntry: 2500\nSL: 2400\nTP: 2700",
                             "ETH") is None
     and _rv2_ex.nonsetup_reason("BINANCE:BTCUSDT correlation\nETH long\nEntry: 2500\nSL: 2400\nTP: 2700",
                                 "ETH") is None),
    ("RV2-E4c 665 형태(#COREUSDT 글이 CRO 로 수집)는 여전히 coin_mismatch",
     _rv2_ex.nonsetup_reason("#COREUSDT / Ready to go up\n#CORE\n\nThe price is moving within a "
                             "bearish channel.", "CRO") == "coin_mismatch"),
    # E5: 손익비 라벨 뒤 공백 런 백트래킹
    (f"RV2-E5 'RR'/'risk reward'+공백 1600자 파싱 < 1s (측정 {_rv2_rr_dt:.3f}s; 수정 전 ≈10s)",
     _rv2_rr_dt < 1.0),
    (f"RV2-E5b 숫자+공백 4000자(기간 토큰) 파싱 < 1.5s (측정 {_rv2_tf_dt:.3f}s)", _rv2_tf_dt < 1.5),
    # E6: "w/" 는 with
    ("RV2-E6 'TP: 650 w/ trailing stop' TP 650 보존",
     _rv2_k("BNB long\nEntry: 600\nSL: 580\nTP: 650 w/ trailing stop") == ("long", 600.0, 580.0, 650.0)),
]
for _desc, _passed in _RV2_EX:
    print(("✅" if _passed else "❌"), _desc)
    if _passed:
        ok += 1
TOTAL_EXTRA += len(_RV2_EX)

TOTAL = (len(CASES) + len(REAL_BUG_CASES) + TOTAL_EXTRA + len(TF_CASES)
         + len(WINDOW_CASES) + len(LADDER_CASES) + len(FAKE_NUMBER_CASES)
         + len(LADDER_N_CASES) + len(TPSALL_CASES))
print(f"\n{ok}/{TOTAL} 통과")
sys.exit(0 if ok == TOTAL else 1)
