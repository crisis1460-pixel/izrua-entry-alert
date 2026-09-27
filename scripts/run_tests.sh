#!/usr/bin/env bash
# 테스트 전체 실행기 — CI(tests.yml)와 로컬이 **같은 목록·같은 판정**을 쓰게 하는 단일 입구.
# (2026-09-28 개발·운영 방식 최종안 #1: 테스트 14종 전부 CI 편입, 실패 0 기준선)
#
# - 각 파일을 별도 프로세스로 실행한다. 테스트들이 settings.SETTINGS 를 전역으로 덮어써서
#   (특히 db_path) 한 프로세스에 모으면 서로 오염된다(2026-07-26 감사).
# - 판정 = exit code **와** 줄 머리 '❌' 둘 다. 09-27 로컬 루프가 `echo "$(basename $t) exit=$?"`
#   로 basename 의 종료코드를 찍어 실패를 통과로 읽은 사고가 있었다 — 판정은 여기서만 한다.
# - 하나가 실패해도 나머지를 마저 돌려 전체 그림을 보고 끝에 종합 판정한다.
# - 테스트가 data/ 캐시를 갱신하므로 로컬에서는 끝에 `git checkout -- data` 를 돌린다.
#
# 사용: bash scripts/run_tests.sh            (전체)
#       bash scripts/run_tests.sh test_infra  (일부)
set -u
cd "$(dirname "$0")/.."
export PYTHONIOENCODING=utf-8

TESTS=("$@")
if [ ${#TESTS[@]} -eq 0 ]; then
  TESTS=()
  for f in scripts/test_*.py; do
    TESTS+=("$(basename "$f" .py)")
  done
fi

LOG_DIR="${TMPDIR:-/tmp}/izrua_test_logs"
mkdir -p "$LOG_DIR"
FAILED=""
for t in "${TESTS[@]}"; do
  t="$(basename "${t%.py}")"   # 'test_infra.py'·'scripts/test_infra' 로 줘도 동작
  log="$LOG_DIR/$t.log"
  [ -n "${GITHUB_ACTIONS:-}" ] && echo "::group::$t"
  start=$(date +%s)
  python "scripts/$t.py" > "$log" 2>&1
  code=$?
  n_fail=$(grep -c '^❌' "$log")
  dur=$(( $(date +%s) - start ))
  if [ -n "${GITHUB_ACTIONS:-}" ]; then
    cat "$log"
    echo "::endgroup::"
  fi
  if [ $code -ne 0 ] || [ "$n_fail" -ne 0 ]; then
    FAILED="$FAILED $t"
    echo "❌ $t (exit $code, 실패 체크 ${n_fail}건, ${dur}s)"
    grep '^❌' "$log" | head -20
    [ -n "${GITHUB_ACTIONS:-}" ] && echo "::error::$t 실패 (exit $code, ❌ ${n_fail}건)"
  else
    echo "✅ $t (${dur}s)"
  fi
done

if [ -z "${GITHUB_ACTIONS:-}" ] && { [ -d .git ] || [ -f .git ]; }; then
  git checkout -- data 2>/dev/null || true
fi

if [ -n "$FAILED" ]; then
  echo "실패한 테스트:$FAILED"
  exit 1
fi
echo "테스트 ${#TESTS[@]}종 전부 통과"
