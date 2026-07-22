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

print(f"\n{ok}/{len(CASES)} 통과")
sys.exit(0 if ok == len(CASES) else 1)
