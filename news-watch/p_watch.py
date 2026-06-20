#!/home/tabris/.hermes/hermes-agent/venv/bin/python3
"""
p_watch.py — Everpure (NYSE: P) 進場價格監控
進場目標區間：$76–$78（理想），$72–$76（撿便宜）
到區間推 Telegram，1小時內同訊號不重複推
監控至 2026-06-30，正規交易時間（美東 9:30–16:00，週一到週五）
"""
import os, json, requests
from datetime import datetime, timezone, timedelta
import yfinance as yf
from dotenv import load_dotenv

load_dotenv(os.path.expanduser('~/.hermes/.env'))

TG_TOKEN   = os.environ.get('TELEGRAM_BOT_TOKEN', '')
TG_CHAT_ID = os.environ.get('TELEGRAM_ALLOWED_USERS', '').split(',')[0]
STATE_FILE = os.path.expanduser('~/.hermes/p_watch_state.json')

WATCH_UNTIL = datetime(2026, 6, 30, tzinfo=timezone.utc)

RULES = [
    {
        'key':  'bargain',
        'type': 'below',
        'price': 76.0,
        'msg':  '🟢 P 進入撿便宜區間 — 可以建倉',
        'note': '現價接近財報後低點 $71.56，風險報酬佳',
    },
    {
        'key':  'ideal',
        'type': 'below',
        'price': 78.0,
        'msg':  '🟡 P 進入理想進場區間 — 可以試單',
        'note': 'MA20 $80.73 以下，距高 -20%+ 甜蜜區',
    },
    {
        'key':  'stop',
        'type': 'below',
        'price': 68.0,
        'msg':  '🔴 P 跌破 $68 — 故事可能有變，暫停進場',
        'note': '重新評估毛利率壓力是否惡化',
    },
]

def send_tg(msg):
    if not TG_TOKEN or not TG_CHAT_ID:
        print(f"[TG] {msg}")
        return
    requests.post(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        json={"chat_id": TG_CHAT_ID, "text": msg},
        timeout=10
    )

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}

def save_state(state):
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f)

def already_sent(state, key, cooldown_hours=1):
    if key not in state:
        return False
    last = datetime.fromisoformat(state[key])
    return (datetime.now(timezone.utc) - last) < timedelta(hours=cooldown_hours)

def is_market_open():
    now_et = datetime.now(timezone(timedelta(hours=-4)))  # EDT
    if now_et.weekday() >= 5:
        return False
    market_open  = now_et.replace(hour=9,  minute=30, second=0, microsecond=0)
    market_close = now_et.replace(hour=16, minute=0,  second=0, microsecond=0)
    return market_open <= now_et < market_close

def main():
    now = datetime.now(timezone.utc)

    if now > WATCH_UNTIL:
        print("[p_watch] 已過監控到期日 2026-06-30，腳本結束")
        return

    if not is_market_open():
        print("[p_watch] 非交易時間，跳過")
        return

    state = load_state()
    now_str = datetime.now(timezone(timedelta(hours=-4))).strftime('%H:%M ET')

    try:
        info  = yf.Ticker('P').info
        price = info.get('currentPrice') or info.get('regularMarketPrice')
        if not price:
            print("[p_watch] 無法取得價格")
            return

        print(f"[p_watch] NYSE: P 現價 ${price:.2f}  ({now_str})")

        for rule in RULES:
            key       = rule['key']
            triggered = rule['type'] == 'below' and price < rule['price']

            if triggered and not already_sent(state, key):
                msg = (
                    f"{rule['msg']}\n"
                    f"現價 ${price:.2f}  ({now_str})\n"
                    f"{rule['note']}"
                )
                send_tg(msg)
                state[key] = now.isoformat()
                print(f"[推播] {msg}")

            elif not triggered and key in state:
                del state[key]

    except Exception as e:
        print(f"[p_watch] 查詢失敗: {e}")

    save_state(state)

if __name__ == '__main__':
    main()
