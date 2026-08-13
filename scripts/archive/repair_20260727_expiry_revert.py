"""일회성 수리 — 2026-07-27 잘못된 만료 규칙으로 조기 종료된 레벨 되살리기.

경위: HANDOFF 가 오래 "게시 7일 만료"로 적혀 있어 expire_old 기준을 collected_at
에서 게시 시각으로 바꿔 배포했다. 이후 실측(터치 67건 중 9건이 게시 7일 넘겨 터치,
전부 '늦게 주운 글'이고 성적도 나쁘지 않음)으로 그 변경이 틀렸음이 드러나 되돌렸으나,
배포돼 있던 07:00 회차가 이미 6건을 expired 로 전이시킨 뒤였다. 모두 수집 후 4.0일
= 올바른 규칙(수집 후 7일)으로는 잔여 감시 3.0일이 남은 건들이다.

되살리는 기준(보수적):
  · status='expired' 이고
  · expired_at 이 문제 배포 구간(_BAD_WINDOW) 안이며
  · collected_at 기준으로 아직 만료 대상이 아니고 (수집 후 level_expiry_hours 미만)
  · touched_at 이 없다 (터치·판정 이력이 있으면 손대지 않는다)
직전 상태는 previewed_at 유무로 복원한다(예고를 이미 보냈으면 previewed, 아니면
watching) — 되살린 뒤 예고가 재발송되지 않게 하는 것이 핵심이다.

멱등: 조건을 만족하는 행이 없으면 아무것도 하지 않는다. 여러 번 돌려도 안전.
"""
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings  # noqa: E402

# 잘못된 규칙이 프로덕션에 있던 구간(KST 07-27 06:30~08:00 여유 포함). 이 밖의
# 만료는 정상 만료이므로 절대 건드리지 않는다.
_BAD_WINDOW = (1785099600.0, 1785105000.0)


def repair(db_path: str, dry_run: bool = False) -> int:
    expiry_sec = settings.get("level_expiry_hours") * 3600
    now = time.time()
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            """SELECT id, coin_symbol, grade, previewed_at, collected_at, expired_at
                 FROM levels
                WHERE status='expired' AND touched_at IS NULL
                  AND expired_at BETWEEN ? AND ?
                  AND collected_at >= ?""",
            (_BAD_WINDOW[0], _BAD_WINDOW[1], now - expiry_sec),
        ).fetchall()
        if not rows:
            print("되살릴 대상 없음 (이미 복구됐거나 해당 없음)")
            return 0
        for r in rows:
            back = "previewed" if r["previewed_at"] else "watching"
            left = (r["collected_at"] + expiry_sec - now) / 86400
            print(f"  id={r['id']:4} {r['coin_symbol']:6} {r['grade']} "
                  f"→ {back} (잔여 감시 {left:.1f}일)")
            if not dry_run:
                con.execute(
                    "UPDATE levels SET status=?, expired_at=NULL WHERE id=?",
                    (back, r["id"]))
        if dry_run:
            print(f"[dry-run] {len(rows)}건 (쓰기 안 함)")
        else:
            con.commit()
            print(f"복구 완료: {len(rows)}건")
        return len(rows)
    finally:
        con.close()


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    sys.exit(0 if repair(settings.get("db_path"), dry_run=dry) >= 0 else 1)
