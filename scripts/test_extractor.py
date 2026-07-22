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
        "실전버그 재현 - INJ SHORT (동일 패턴)",
        "INJ USDT SHORT SIGNAL\n#81.  INJ/USDT – Trade Setup (SHORT)\n\n"
        "📈 Position Type: SHORT\n🕒 Timeframe: 1H\n📊 Market: Futures\n\n"
        "💰 Entry Zone:\n\n5.090\n\n\n5.185\n\n🛑 Stop-Loss:\n\n5.290\n\n"
        "🎯 Take-Profit Targets:\n\n• TP1: 4.970\n\n• TP2: 4.828\n\n"
        "• TP3: 4.663\n\n• TP4: 4.434\n\n⚙️ Leverage:\n\n5 *10",
        5.10,
        {"entry": 5.09, "sl": 5.29, "tp": 4.970, "direction": "short"},
    ),
]

for desc, text, price, expected in REAL_BUG_CASES:
    r = parse_setup(text, current_price=price)
    passed = (
        r is not None
        and r["direction"] == expected["direction"]
        and abs(r["entry"] - expected["entry"]) < 0.01
        and r["sl"] is not None and abs(r["sl"] - expected["sl"]) < 0.01
        and r["tp"] is not None and abs(r["tp"] - expected["tp"]) < 0.01
        and r["rr"] is not None and r["rr"] > 0  # 마이너스/None RR 재발 방지 확인
    )
    got = "None" if r is None else f"entry={r['entry']} sl={r['sl']} tp={r['tp']} rr={r['rr']}"
    mark = "✅" if passed else "❌"
    if passed:
        ok += 1
    print(f"{mark} {desc}\n    → {got}")
    if not passed:
        print(f"    (기대: {expected})")

TOTAL = len(CASES) + len(REAL_BUG_CASES)
print(f"\n{ok}/{TOTAL} 통과")
sys.exit(0 if ok == TOTAL else 1)
