#!/usr/bin/env python3
"""
비밀정보 유출 스캐너 — 공개 레포 안전장치 (2차 방어선).

git 이 실제로 추적하는(=push 될) 파일만 검사한다. 아래 중 하나라도 걸리면 exit 1:
  1) .env / *.key / *.pem 같은 비밀 파일이 git 추적 대상에 들어감
  2) 추적 파일 본문에 알려진 비밀 패턴(업비트 키, 텔레그램 토큰, GitHub PAT, 구글 키 등)이 존재

사용:
  - 로컬: `python scripts/check_secrets.py` (커밋/푸시 전 수동 실행 또는 pre-commit 훅)
  - 운영: .github/workflows/secret-scan.yml 이 매 push 마다 자동 실행 → 걸리면 빌드 실패

설계상 이 스캐너는 '값'을 출력하지 않는다. 어느 파일 몇 번째 줄에서 어떤 '유형'이
걸렸는지만 알려준다 (스캐너 로그 자체가 유출 경로가 되지 않도록).
"""

import re
import subprocess
import sys

# Windows 콘솔 기본 인코딩(cp949)에서 이모지/한글 출력이 깨지지 않게 UTF-8 강제.
# (GitHub Actions 러너는 Linux/UTF-8 이라 무영향)
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

# 절대 커밋되면 안 되는 파일 (경로 정확일치 또는 확장자)
FORBIDDEN_NAMES = {".env", "secrets.json", "credentials.json"}
FORBIDDEN_SUFFIXES = (".key", ".pem")
# .env.example 은 견본이라 허용 (플레이스홀더만 있어야 함)
ALLOWED_EXCEPTIONS = {".env.example"}

# 알려진 비밀 패턴 (유형명, 정규식). 값은 로그에 안 남긴다.
SECRET_PATTERNS = [
    ("텔레그램 봇 토큰", re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b")),
    ("GitHub PAT (fine-grained)", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{60,}\b")),
    ("GitHub PAT (classic)", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("구글 API 키 (AIza)", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("구글 OAuth 키 (AQ.)", re.compile(r"\bAQ\.[A-Za-z0-9_\-]{30,}\b")),
    ("AWS 액세스 키", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("PEM 개인키 블록", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
]

# 업비트 키는 40자 base64형이라 오탐이 나기 쉬워, 변수명과 붙어있을 때만 잡는다.
UPBIT_ASSIGN = re.compile(
    r"(?i)UPBIT_(?:ACCESS|SECRET)_KEY\s*[=:]\s*['\"]?[A-Za-z0-9]{30,}"
)
# 일반 대입형 시크릿(길이 20+ 무작위값이 KEY/TOKEN/SECRET 변수에 하드코딩)
GENERIC_ASSIGN = re.compile(
    r"(?i)(?:api_?key|secret|token|password|passwd)\s*[=:]\s*['\"][A-Za-z0-9_\-]{20,}['\"]"
)
# 견본 파일이 쓰는 한글 플레이스홀더는 오탐이므로 제외
PLACEHOLDER_HINT = re.compile(r"여기에|placeholder|your_|xxxx|<.*>|example", re.IGNORECASE)


def tracked_files() -> list:
    """git 이 추적 중인(=push 대상) 파일 목록."""
    try:
        out = subprocess.run(
            ["git", "ls-files"], capture_output=True, text=True, check=True
        ).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("⚠️  git 저장소가 아니거나 git 을 찾을 수 없습니다. 스캔을 건너뜁니다.")
        return []
    return [f for f in out.splitlines() if f.strip()]


def is_forbidden_file(path: str) -> bool:
    name = path.split("/")[-1]
    if name in ALLOWED_EXCEPTIONS:
        return False
    if name in FORBIDDEN_NAMES:
        return True
    if name.startswith(".env") and name not in ALLOWED_EXCEPTIONS:
        return True
    return path.endswith(FORBIDDEN_SUFFIXES)


def scan() -> int:
    findings = []
    files = tracked_files()

    for path in files:
        if is_forbidden_file(path):
            findings.append((path, 0, "금지된 비밀 파일이 커밋 대상에 포함됨"))

    for path in files:
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()
        except (OSError, UnicodeDecodeError):
            continue
        is_example = path.split("/")[-1] in ALLOWED_EXCEPTIONS
        for i, line in enumerate(lines, start=1):
            if is_example and PLACEHOLDER_HINT.search(line):
                continue
            for label, pat in SECRET_PATTERNS:
                if pat.search(line):
                    findings.append((path, i, f"비밀 패턴 감지: {label}"))
            if UPBIT_ASSIGN.search(line):
                findings.append((path, i, "비밀 패턴 감지: 업비트 API 키 대입"))
            if GENERIC_ASSIGN.search(line) and not PLACEHOLDER_HINT.search(line):
                findings.append((path, i, "비밀 패턴 감지: 하드코딩된 키/토큰 대입"))

    if findings:
        print("❌ 비밀정보 유출 위험 발견 — 커밋/푸시를 중단하세요:\n")
        for path, line, msg in findings:
            loc = f"{path}:{line}" if line else path
            print(f"  • {loc} — {msg}")
        print(
            "\n조치: 해당 값을 코드에서 제거하고 .env(로컬) 또는 GitHub Secrets(운영)로 옮기세요.\n"
            "이미 커밋했다면 git 히스토리에서도 제거하고 해당 키를 즉시 폐기(rotate)하세요."
        )
        return 1

    print(f"✅ 스캔 통과 — 추적 파일 {len(files)}개에서 비밀정보 미발견.")
    return 0


if __name__ == "__main__":
    sys.exit(scan())
