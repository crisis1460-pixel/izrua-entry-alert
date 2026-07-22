# izrua_entry_alert

TradingView 차티스트 아이디어에서 **엔트리 가격**을 수집하고, 해당 코인이 그 가격에
접근/터치하면 텔레그램으로 알리는 봇. GitHub Actions 서버리스로 24/7 무료 운영.

- 자동매매 **아님** — 매수 판단에 도움될 정보만 간결히 알림.
- 상세 설계는 `ALERT_BOT_PLAN.md`(upbit_bot 폴더) 참고.

## 🔐 보안 — 공개 레포에서 비밀정보 지키는 3중 방어선

이 레포는 **공개(public)** 로 운영된다(그래야 GitHub Actions 가 무료 무제한). 따라서
비밀값은 코드에 절대 넣지 않고, 아래 3중 안전장치로 유출을 막는다.

1. **`.gitignore`** — `.env`, `*.key`, `*.pem`, `cache/`, `*.db` 등을 커밋 자체에서 차단.
   실제 키는 로컬 `.env`(git 무시됨)에만, 운영은 **GitHub Actions Secrets**에만 존재.
2. **`scripts/check_secrets.py`** — 커밋될 파일을 스캔해 비밀 파일/패턴을 찾으면 실패.
   커밋 전 `python scripts/check_secrets.py` 로 수동 확인 가능(pre-commit 훅 권장).
3. **`.github/workflows/secret-scan.yml`** — 모든 push/PR 에서 위 스캐너 + gitleaks 를
   자동 실행. 실수로 로컬 검사를 건너뛰어도 서버가 막는다.

### 필요한 비밀값 (Secrets)
| 이름 | 용도 | 필수 |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | 알림 발송 (기존 @izrua 봇) | ✅ |
| `TELEGRAM_CHAT_ID` | 알림 수신 대상 | ✅ |
| `COINGECKO_API_KEY` | 시총 top-200 조회 (무료 Demo) | ✅ |
| `WATCHER_GITHUB_TOKEN` | 워쳐 DB 읽기(작성자 적중률) | 선택 |

> **업비트 API 키는 쓰지 않는다.** 시세 API 는 인증 불필요라, 가장 위험한 거래 키를
> 애초에 다루지 않는 것이 이 설계의 핵심이다.

### 로컬 개발 준비
```bash
cp .env.example .env      # 그리고 .env 에 실제 값 채우기 (커밋 안 됨)
pip install -r requirements.txt
python scripts/check_secrets.py   # 통과 확인
```

## 구조
```
config/settings.py     운영 파라미터 + 비밀값 로더(env 전용)
collector/coingecko.py 시총 top-200 ∩ 업비트 KRW 유니버스
collector/tradingview.py + extractor.py  아이디어 수집 + entry/SL/TP 추출
storage/db.py          레벨 상태 DB (watching/previewed/touched/expired)
monitor/price_check.py Upbit 시세로 접근/터치 판정
notify/telegram.py     알림 렌더링/발송
.github/workflows/     collect(4h) · price_check(5~10분) · secret-scan
```
