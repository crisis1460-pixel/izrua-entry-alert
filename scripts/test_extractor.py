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

TOTAL = (len(CASES) + len(REAL_BUG_CASES) + TOTAL_EXTRA + len(TF_CASES)
         + len(WINDOW_CASES) + len(LADDER_CASES))
print(f"\n{ok}/{TOTAL} 통과")
sys.exit(0 if ok == TOTAL else 1)
