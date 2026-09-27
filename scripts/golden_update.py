# 골든셋 스냅샷 갱신 도구 (2026-09-28 승인 계획 #2 실데이터 골든셋 회귀).
#
# 사용법:
#   python scripts/golden_update.py                 # 현재 코드 기준 before/after 표만 출력
#   python scripts/golden_update.py --write         # expected 를 현재 코드 출력으로 덮어씀
#   python scripts/golden_update.py --from-db PATH [--human PATH] [--write]
#       # DB 사본(읽기 전용)에서 케이스 입력을 새로 뽑아 재구성 (운영 data/levels.db 직접
#       # 지정 금지 — 사본을 쓴다). --human 은 감사 C(2026-09-27) md 경로.
#
# 원칙: expected 만 갱신한다. human(사람 판독) 필드는 절대 바꾸지 않는다 —
# 기본 모드에서는 픽스처의 입력·human 을 그대로 두고 expected 만 다시 계산하므로
# 결정적이다. 의도한 변경인지 표로 먼저 확인한 뒤 --write 로 반영할 것.
import argparse
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


def _regen_parse(cases: list, old_by_id: dict) -> list:
    rows = []
    for c in cases:
        act = gl.run_parse(c)
        prev = old_by_id.get(c["id"])
        before = dict(c, expected=prev.get("expected")) if prev else dict(c, expected=None)
        if prev is None:
            rows.append((c["id"], "(신규)", None, "setup" if act else None))
        else:
            rows.extend(gl.diff_parse(before, act))
        c["expected"] = act
    return rows


def _regen_render(cases: list, old_by_id: dict) -> list:
    rows = []
    for c in cases:
        act = gl.run_render(c)
        prev = old_by_id.get(c["id"])
        if prev is None:
            rows.append((c["id"], "(신규)", None, f"{len(act.splitlines())}줄"))
        else:
            rows.extend(gl.diff_render(dict(c, expected=prev.get("expected")), act))
        c["expected"] = act
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description="골든셋 expected 재생성")
    ap.add_argument("--write", action="store_true", help="픽스처 파일을 실제로 덮어쓴다")
    ap.add_argument("--from-db", metavar="PATH", help="DB 사본에서 케이스 입력 재구성(읽기 전용)")
    ap.add_argument("--human", metavar="PATH", help="감사 C md(사람 판독표) 경로 (--from-db 와 함께)")
    ap.add_argument("--render-n", type=int, default=30)
    args = ap.parse_args()

    old_parse = gl.load(gl.PARSE_PATH) if gl.PARSE_PATH.exists() else []
    old_render = gl.load(gl.RENDER_PATH) if gl.RENDER_PATH.exists() else []
    old_p = {c["id"]: c for c in old_parse}
    old_r = {c["id"]: c for c in old_render}

    if args.from_db:
        if Path(args.from_db).resolve() == (gl.ROOT / "data" / "levels.db").resolve():
            print("⚠️ 운영 data/levels.db 대신 사본 경로를 지정하세요 (읽기 전용이라도 잠금 경합 방지)")
            return 2
        parse_cases = gl.build_parse_from_db(args.from_db, args.human)
        if not args.human:   # 사람 판독값은 재구성 시에도 기존 픽스처 것을 보존
            for c in parse_cases:
                if c["id"] in old_p and old_p[c["id"]].get("human"):
                    c["human"] = old_p[c["id"]]["human"]
        render_cases = gl.build_render_from_db(args.from_db, args.render_n)
        gone_p = sorted(set(old_p) - {c["id"] for c in parse_cases})
        gone_r = sorted(set(old_r) - {c["id"] for c in render_cases})
    else:
        parse_cases = [dict(c) for c in old_parse]
        render_cases = [dict(c) for c in old_render]
        gone_p, gone_r = [], []

    p_rows = _regen_parse(parse_cases, old_p)
    r_rows = _regen_render(render_cases, old_r)
    p_rows += [(i, "(삭제)", "case", None) for i in gone_p]
    r_rows += [(i, "(삭제)", "case", None) for i in gone_r]

    print(f"== 파싱 골든 {len(parse_cases)}건 — 변경 {len({r[0] for r in p_rows})}건")
    gl.print_table(p_rows)
    print(f"== 렌더 골든 {len(render_cases)}건 — 변경 {len({r[0] for r in r_rows})}건")
    gl.print_table(r_rows)

    if not args.write:
        print("\n(미기록 — 의도한 변경이면 --write 로 반영)")
        return 0
    gl.save(gl.PARSE_PATH, parse_cases)
    gl.save(gl.RENDER_PATH, render_cases)
    print(f"\n✅ 기록: {gl.PARSE_PATH.name}, {gl.RENDER_PATH.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
