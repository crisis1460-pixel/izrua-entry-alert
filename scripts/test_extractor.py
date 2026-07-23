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
    )
    got = "None" if r is None else f"entry={r['entry']} sl={r['sl']} tp={r['tp']} rr={r['rr']}"
    mark = "✅" if passed else "❌"
    if passed:
        ok += 1
    print(f"{mark} {desc}\n    → {got}")
    if not passed:
        print(f"    (기대: {expected})")

# 크기 sanity 방어선: 서수 오인이 어떤 신규 경로로 재발해도 4배/0.25배 밖 값은 차단
r = parse_setup("Long entry 0.083, target 1.0, SL 0.079", current_price=0.083)
guard_ok = r is not None and r["tp"] is None and _close(r["sl"], 0.079)
print(("✅" if guard_ok else "❌"), "크기 sanity - entry 대비 12배 목표는 판단보류(None)")
print(f"    → {r}")
if guard_ok:
    ok += 1
TOTAL_EXTRA = 1

TOTAL = len(CASES) + len(REAL_BUG_CASES) + TOTAL_EXTRA
print(f"\n{ok}/{TOTAL} 통과")
sys.exit(0 if ok == TOTAL else 1)
