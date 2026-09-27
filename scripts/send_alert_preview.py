"""배포 전 테스트 발송 — XRP 888 알림을 최종 양식으로 렌더해 텔레그램에 보낸다(1회성 확인용).

- **네트워크 조회 없음**: 렌더 입력은 운영 DB 스냅샷(2026-09-27 09:41Z)·업비트 공개 캔들로
  재구성한 값을 아래 상수로 고정했다(izrua_company/sample_alert_items_v3c_2026-09-27.txt 와 동일).
- **DB·alerts_log·meta 쓰기 없음**: storage 를 import 하지 않는다 — 테스트 발송이 운영 통계에
  섞이지 않는다. 발송은 기존 경로 notify.telegram.send(HTML 파싱·재시도 그대로)만 쓴다.
- 기본은 dry-run(출력만). 실제 발송은 `--send` 플래그가 있어야 한다.
  ① 안내 1통(무음) ② 렌더 결과 1통(유음 — 실제 터치 본알림과 같은 조건).

사용: python scripts/send_alert_preview.py            # dry-run
      python scripts/send_alert_preview.py --send     # TELEGRAM_BOT_TOKEN/CHAT_ID 필요
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from notify import telegram  # noqa: E402

NOTICE = "🧪 배포 전 테스트 — 실제 알림 아님 (XRP 888 최종 양식)"

# ── XRP 888 (알림 822, 발송 2026-09-27 15:20 KST) 터치 시점 스냅샷 — 상수 고정 ──
SENT_AT = 1790490019.7297072          # 원 발송 시각(글 나이 재현 기준)
CURRENT_KRW = 2070.6                  # touch_price_krw
USDT_KRW = 1360.0                     # touch_usdt_krw
REP = {
    "coin_symbol": "XRP", "ticker": "KRW-XRP", "direction": "long",
    "entry_usd": 1.5225, "tp_usd": 1.6149, "tps_usd": "[1.6149, 1.7704]",
    "tp_ladder_count": 2, "grade": "A", "display_grade": "A", "score": 56.0,
    "author": "aAbraham1x", "author_followers": 2, "author_hit_rate": 1.0,
    "author_hit_count": 1, "mcap_rank": 5, "mcap_tier_icon": "💎",
    "post_url": "https://www.tradingview.com/chart/XRPUSDT/aYiE2zKC-XRP-Long-Setup-"
                "First-Target-1-6149-Breakout-Could-Send-It-Hi/",
    "post_age_minutes": 897.4138207038244, "collected_at": 1790484441.8317494,
    "source": "tradingview",
    "author_self_tp_hits": 0, "author_self_neff": 0.0, "author_self_neff_r": 0.0,
    "author_self_e_lb": None, "author_rank_min_neff": 5, "author_self_wins": 0,
    "author_self_losses": 0, "author_touched_n": 1, "author_untouched_expired": 0,
}
KWARGS = dict(
    sentiment={"btc_dominance": 58.3, "fear_greed": 70, "fear_greed_label": "Greed",
               "altcoin_season_index": 63},
    week52=(4379.0, 1392.0, 52),          # 업비트 주봉 52개(발송 시각 기준) 고가/저가
    kimchi_pct=-0.11844958369022485,      # |x|<3% → 표시 안 됨
    kimchi_delta=None,
    volume_rank=1,
    supply=("중립", None),
    position=("중립", "상승세·RSI61"),
    adx14=41.73081799883817,
    dex_stats=None,
    active_addr_pctile=6.7,               # 표시 제외 항목(스위치 OFF) — 값은 그대로 전달
    stwits_bullish_ratio=None, stwits_n=None,
    watcher_coin_sl=None,
    items={
        "rvol_d20": 0.6495149324620725,   # 전일 완성봉 거래대금 ÷ 20일 평균
        "low30_pct": 20.80513418903149,   # 30일 저점 대비 %
        "upbit_warning": [],              # 업비트 주의·유의 지정 없음
        "post_move_pct": -1.6342042755344455,  # 표시 OFF(스냅샷 전용)
        "alt_breadth": 50.0,              # 업비트 알트 상승 비율(샘플 v3c 생성 시점 값)
    },
)


def render() -> str:
    # 글 나이('글 16시간 전')를 원 발송 시각 기준으로 재현 — 수집 시각을 지금 기준으로 평행이동.
    rep = dict(REP)
    rep["collected_at"] = time.time() - (SENT_AT - REP["collected_at"])
    return telegram.render_alert("touch", "XRP", [rep], CURRENT_KRW, USDT_KRW,
                                 rep=rep, **KWARGS)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true", help="발송 없이 출력만(기본)")
    g.add_argument("--send", action="store_true", help="텔레그램으로 실제 발송")
    a = ap.parse_args(argv)
    text = render()
    print("[1/2] " + NOTICE)
    print("[2/2]")
    print(text)
    if not a.send:
        print("\n(dry-run — 발송하지 않음. 실제 발송은 --send)")
        return 0
    ok1 = telegram.send(NOTICE, urgency="low")      # 안내는 무음
    ok2 = telegram.send(text, urgency="high")       # 본문은 실제 터치 알림과 같은 유음
    print(f"\n발송 결과: 안내={'성공' if ok1 else '실패'} · 본문={'성공' if ok2 else '실패'}")
    return 0 if (ok1 and ok2) else 1


if __name__ == "__main__":
    sys.exit(main())
