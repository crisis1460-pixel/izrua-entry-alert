"""발송 원장 — 알림을 실제로 보냈다는 사실만 담는, DB 밖의 append-only 파일.

## 왜 DB 밖인가 (2026-07-28)

07-28 03:53·03:54 에 같은 알림이 두 번 나갔다. 응급수리로 `alerts_log` 테이블을
조회해 재발송을 막았는데, **그 방어선 자체가 유실될 수 있다**는 게 곧바로 드러났다.
실측 증거 — 커밋 두 개에서 DB 를 꺼내 비교하면:

    28e14a9 : id 68·69·70 = AAVE·DOT·ENA, 발송 03:53:50
    870e889 : id 68·69·70 = AAVE·DOT·ENA, 발송 03:54:35   ← 같은 번호, 다른 내용

뒤 회차가 66번 다음부터 번호를 다시 매겼다 = 앞 회차의 행이 없는 DB 에서 출발했고,
커밋백이 그 위에 자기 스냅샷을 통째로 얹어 앞 회차 기록을 지웠다는 뜻이다.
levels.db 는 바이너리라 3-way 머지가 불가능해 커밋백은 "전체 파일 교체"밖에 할 수
없다 — 즉 **DB 안에 있는 한 어떤 표도 경합에서 지면 사라진다.**

그래서 원장만 텍스트(NDJSON)로 빼낸다. 줄 단위라 합집합 병합이 가능하고, 커밋백이
두 회차의 원장을 모두 살릴 수 있다(merge_files 참고 — 워크플로가 호출한다).

## 설계 원칙

* **이 파일은 '보냈다'는 사실만 담는다.** 판정·집계·표시는 전부 DB 몫이다. 원장이
  깨져도 봇 기능은 그대로 돌고, 재발송 차단만 약해진다.
* 한 줄 = 한 발송. 키 순서를 고정(sort_keys)해 같은 발송이 항상 같은 문자열이 되게
  한다 — 그래야 합집합 병합에서 중복 제거가 문자열 비교만으로 성립한다.
* 손상된 줄은 조용히 버린다. 원장 한 줄 때문에 회차가 죽는 건 본말전도다.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

LEDGER_FILENAME = "alerts_log.ndjson"


def ledger_path(db_path: str) -> str:
    """DB 와 같은 디렉터리에 둔다 — 커밋백이 data/ 통째로 다루므로 그 안에 있어야 한다."""
    return os.path.join(os.path.dirname(db_path) or ".", LEDGER_FILENAME)


def _key(coin_symbol: str, kind: str, level_ids: Iterable) -> str:
    """발송 하나를 가리키는 키. storage.db.record_alert·recent_alert_exists 와 동일하게
    sorted() 로 순서를 고정한다(2026-07-28 수리) — build_clusters 출력 순서가 바뀌어도
    같은 클러스터는 항상 같은 키를 만들어 재발송 차단이 조용히 무력화되지 않는다."""
    return f"{coin_symbol}|{kind}|{','.join(str(i) for i in sorted(level_ids))}"


def append(db_path: str, coin_symbol: str, kind: str, level_ids: Iterable,
           sent_at: Optional[float] = None) -> None:
    """발송 1건 기록. 실패해도 예외를 올리지 않는다 — 원장 기록 실패가 이미 나간
    알림을 되돌리지도 못하면서 회차를 죽이면 손해만 크다(로그로 남긴다)."""
    row = {"k": _key(coin_symbol, kind, level_ids), "t": round(sent_at or time.time(), 3)}
    try:
        with open(ledger_path(db_path), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n")
    except OSError as e:
        logger.warning("[원장] 발송 기록 실패(무시): %s", e)


def recent_exists(db_path: str, coin_symbol: str, kind: str, level_ids: Iterable,
                  since: float) -> bool:
    """since 이후에 같은 발송이 있었는가. 파일이 없으면 False(첫 회차 등)."""
    want = _key(coin_symbol, kind, level_ids)
    try:
        with open(ledger_path(db_path), encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except (ValueError, AttributeError, TypeError):
                    continue          # 손상 줄은 건너뛴다(위 설계 원칙)
                if row.get("k") == want and (row.get("t") or 0) >= since:
                    return True
    except OSError:
        return False
    return False


def _load(path: str) -> dict:
    """경로 → {줄 문자열: 시각}. 중복 제거와 시간 정렬을 동시에 하기 위한 형태."""
    out = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    t = float(json.loads(line).get("t") or 0)
                except (ValueError, AttributeError, TypeError):
                    continue
                out[line] = t
    except OSError:
        pass
    return out


def merge_files(*paths: str, out_path: str, keep_sec: float = 7 * 86400,
                now: Optional[float] = None) -> int:
    """여러 원장을 **합집합**으로 병합해 out_path 에 쓴다. 반환: 기록한 줄 수.

    커밋백이 호출한다 — 경합에서 진 회차의 발송 기록이 이긴 회차 스냅샷에 덮여
    사라지는 것을 막는 유일한 지점이다. 합집합인 이유: 두 회차 모두 '실제로 보낸'
    것이라 어느 쪽도 버릴 근거가 없다. 같은 발송이면 줄 문자열이 완전히 같으므로
    (append 가 sort_keys 로 쓴다) 문자열 집합만으로 중복이 제거된다.

    keep_sec 이전 줄은 버린다 — 원장은 재발송 차단(_RESEND_BLOCK_SEC=10분)에만
    쓰이므로 7일이면 차고 넘치고, 안 지우면 파일이 무한히 자란다."""
    cutoff = (now or time.time()) - keep_sec
    merged = {}
    for p in paths:
        merged.update(_load(p))
    rows = sorted(((t, line) for line, t in merged.items() if t >= cutoff))
    # 2026-07-28 수리: 임시파일에 쓰고 os.replace 로 원자 교체 — 프로세스 중단 시
    # 반쪽 잘린 원장이 남아 최근 발송 기록이 유실되는 것 방지.
    tmp = out_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        for _t, line in rows:
            fh.write(line + "\n")
    os.replace(tmp, out_path)
    return len(rows)
