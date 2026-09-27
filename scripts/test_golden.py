# 실데이터 골든셋 회귀 테스트 (2026-09-28 승인 계획 #2) — 네트워크·data/ 불필요.
#
# scripts/golden/parse_cases.json  : 운영 원문 → parse_setup(+nonsetup_reason) 스냅샷
# scripts/golden/render_cases.json : 실제 발송 터치 알림 입력 → render_alert 스냅샷
# 추출기·렌더 변경이 있으면 케이스별 before/after 표를 찍고 exit 1.
# 의도한 변경이면 `python scripts/golden_update.py` 로 표 확인 후 `--write`.
#
# 사람 판독(human, 감사 C 2026-09-27 부록 A): 스냅샷(expected)이 이미 사람과 다른
# 필드는 보고만 하고(알려진 오파싱), 스냅샷이 사람과 **일치하던** 필드가 어긋나면 실패.
# 어긋나 있던 필드가 사람과 맞게 되면 개선(✨)으로 보고한다(expected 갱신은 수동).
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import logging
logging.basicConfig(level=logging.ERROR)

from scripts import golden_lib as gl  # noqa: E402

ok = True


def check(name, cond):
    global ok
    print(("✅" if cond else "❌"), name)
    ok = ok and cond


# ── G1: 파싱 골든 ─────────────────────────────────────────────────────
parse_cases = gl.load(gl.PARSE_PATH)
p_rows, h_regress, h_known, h_improved = [], [], [], []
n_human = 0
for c in parse_cases:
    act = gl.run_parse(c)
    p_rows.extend(gl.diff_parse(c, act))
    if not c.get("human"):
        continue
    n_human += 1
    was = {k for k, _h, _a in gl.human_diff(c, c.get("expected"))}   # 스냅샷 시점 불일치
    now = {k: (h, a) for k, h, a in gl.human_diff(c, act)}
    for k, (h, a) in now.items():
        (h_known if k in was else h_regress).append((c["id"], k, h, a))
    for k in was - set(now):
        h_improved.append((c["id"], k, c["human"].get(k), (act or {}).get(k)))

check(f"G1 파싱 골든 {len(parse_cases)}건 스냅샷 일치 (변경 {len({r[0] for r in p_rows})}건)",
      not p_rows)
gl.print_table(p_rows)

# ── G2: 사람 판독 대조 ────────────────────────────────────────────────
check(f"G2 사람 판독 {n_human}건 — 일치하던 필드 회귀 없음 ({len(h_regress)}건)",
      not h_regress)
gl.print_table(h_regress)
if h_known:
    print(f"ℹ️ 알려진 사람-판독 불일치 {len(h_known)}건 (보고만, 실패 아님):")
    gl.print_table(h_known)
if h_improved:
    print(f"✨ 사람 판독과 새로 일치 {len(h_improved)}건 (golden_update 로 스냅샷 갱신 권장):")
    gl.print_table(h_improved)

# ── G3: 렌더 골든 ─────────────────────────────────────────────────────
render_cases = gl.load(gl.RENDER_PATH)
r_rows = []
for c in render_cases:
    r_rows.extend(gl.diff_render(c, gl.run_render(c)))
check(f"G3 렌더 골든 {len(render_cases)}건 스냅샷 일치 (변경 {len({r[0] for r in r_rows})}건)",
      not r_rows)
gl.print_table(r_rows)

# ── G4: 결정성 — 같은 입력 두 번 렌더가 같다(시계 고정이 새지 않음) ─────────
check("G4 렌더 결정성(동일 입력 2회 동일 출력)",
      all(gl.run_render(c) == gl.run_render(c) for c in render_cases[:5]))

# ── G5: 픽스처 형식 ───────────────────────────────────────────────────
check("G5 픽스처 id 중복 없음",
      len({c["id"] for c in parse_cases}) == len(parse_cases)
      and len({c["id"] for c in render_cases}) == len(render_cases))

if not ok:
    print("\n의도한 변경이면: python scripts/golden_update.py (표 확인) → --write")
print("\n전체:", "✅ 통과" if ok else "❌ 실패")
sys.exit(0 if ok else 1)
