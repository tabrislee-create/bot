#!/home/tabris/.hermes/hermes-agent/venv/bin/python3
"""
n1x_watch.py — N1X事件盤中盯盤
QCOM：跌破$220推警報，站回$235推機會訊號
MSFT：回測$460推機會訊號，突破$475推注意訊號
每5分鐘查一次，避免重複推播（1小時內同方向只推一次）
"""
import os, json, requests
from datetime import datetime, timezone, timedelta
from utils import now_et_str
import yfinance as yf
from dotenv import load_dotenv
load_dotenv(os.path.expanduser('~/.hermes/.env'))

TG_TOKEN   = os.environ.get('TELEGRAM_BOT_TOKEN', '')
TG_CHAT_ID = os.environ.get('TELEGRAM_ALLOWED_USERS', '').split(',')[0]
STATE_FILE = os.path.expanduser('~/.hermes/n1x_watch_state.json')

RULES = {
    'QCOM': [
        {'type': 'below',  'price': 220.0, 'msg': '🔴 QCOM 跌破 $220 — 反向機會區，觀察支撐'},
        {'type': 'above',  'price': 235.0, 'msg': '🟢 QCOM 站回 $235 — 跌幅收斂，可考慮進場'},
    ],
    'MSFT': [
        {'type': 'below',  'price': 460.0, 'msg': '🟢 MSFT 回測 $460 — 進場窗口開啟'},
        {'type': 'above',  'price': 475.0, 'msg': '⚠️ MSFT 突破 $475 — 追高風險，觀察即可'},
    ],
}

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

def main():
    state = load_state()
    now   = datetime.now(timezone.utc)
    now_str = now_et_str('%H:%M ET')

    for ticker, rules in RULES.items():
        try:
            info  = yf.Ticker(ticker).info
            price = info.get('currentPrice') or info.get('regularMarketPrice')
            if not price:
                continue

            for rule in rules:
                key       = f"{ticker}_{rule['type']}_{rule['price']}"
                triggered = (rule['type'] == 'below' and price < rule['price']) or \
                            (rule['type'] == 'above' and price > rule['price'])

                if triggered and not already_sent(state, key):
                    msg = f"{rule['msg']}\n現價 ${price:.2f}  ({now_str} ET)"
                    send_tg(msg)
                    state[key] = now.isoformat()
                    print(f"[推播] {msg}")
                elif not triggered and key in state:
                    # 條件解除，重置讓下次能再推
                    del state[key]

        except Exception as e:
            print(f"[{ticker}] 查詢失敗: {e}")

    save_state(state)

if __name__ == '__main__':
    main()
