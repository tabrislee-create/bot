#!/home/tabris/.hermes/hermes-agent/venv/bin/python3
"""
price_watch.py — 通用盤中價格警報
可監控任意股票的上下突破價位
每5分鐘查一次，1小時內同方向不重複推播
"""
import os, json, requests
from datetime import datetime, timezone, timedelta
from utils import now_et_str
import yfinance as yf
from dotenv import load_dotenv
load_dotenv(os.path.expanduser('~/.hermes/.env'))
TG_TOKEN   = os.environ.get('TELEGRAM_BOT_TOKEN', '')
TG_CHAT_ID = os.environ.get('TELEGRAM_ALLOWED_USERS', '').split(',')[0]
STATE_FILE = os.path.expanduser('~/.hermes/price_watch_state.json')

RULES = {
    'AEHR': [
        {'type': 'below', 'price': 85.0,  'msg': '🔴 AEHR 跌破 $85 — 停損警戒，考慮減半倉（賣8股）'},
        {'type': 'below', 'price': 70.0,  'msg': '🚨 AEHR 跌破 $70（MA60）— 故事型停損線，考慮全出'},
        {'type': 'above', 'price': 100.0, 'msg': '🟢 AEHR 站回 $100 — 回到成本區上方，觀察動能'},
    ],
    'QCOM': [
        {'type': 'below', 'price': 220.0, 'msg': '🔴 QCOM 跌破 $220 — 接近MA20支撐，觀察'},
        {'type': 'above', 'price': 240.0, 'msg': '🟢 QCOM 站上 $240 — 動能回升'},
    ],
    'MSFT': [
        {'type': 'below', 'price': 450.0, 'msg': '🔴 MSFT 跌破 $450 — 留意支撐'},
        {'type': 'above', 'price': 475.0, 'msg': '⚠️ MSFT 突破 $475 — 追高風險'},
    ],
    'GLW': [
        {'type': 'below', 'price': 161.0, 'msg': '🔴 GLW 跌破 $161（MA60≈成本）— 停損考慮執行'},
    ],
    'VST': [
        {'type': 'below', 'price': 145.0, 'msg': '🔴 VST 跌破 $145 — 停損警戒'},
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
    state   = load_state()
    now     = datetime.now(timezone.utc)
    now_str = now_et_str('%H:%M ET')
    for ticker, rules in RULES.items():
        try:
            price = yf.Ticker(ticker).fast_info['last_price']
            if not price:
                continue
            for rule in rules:
                key = f"{ticker}_{rule['type']}_{rule['price']}"
                triggered = (rule['type'] == 'below' and price < rule['price']) or \
                            (rule['type'] == 'above' and price > rule['price'])
                if triggered and not already_sent(state, key):
                    msg = f"{rule['msg']}\n現價 ${price:.2f}  ({now_str} ET)"
                    send_tg(msg)
                    state[key] = now.isoformat()
                    print(f"[推播] {msg}")
                elif not triggered and key in state:
                    del state[key]
        except Exception as e:
            print(f"[{ticker}] 查詢失敗: {e}")
    save_state(state)

if __name__ == '__main__':
    main()
