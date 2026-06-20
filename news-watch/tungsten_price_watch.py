#!/usr/bin/env python3
"""鎢 APT 週報 — 每週五 17:00 ET 抓報價並推 Telegram。

資料來源優先順序：
  1. SMM 上海有色網 hq.smm.cn/tungsten — APT CIF 鹿特丹 USD/MTU
  2. SMM 同頁面 — 國內 APT 人民幣/噸換算為 USD/MTU
  3. Google News RSS 標題估算（備援）
  4. 全部失敗 → 推播「抓取失敗」通知
"""
import os
import re
import json
import time
import requests
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

CACHE_FILE = Path.home() / ".hermes/scripts/tungsten_price_cache.json"
ENV_FILE   = Path.home() / ".hermes/.env"

# 換算係數（每季人工檢查）
USD_CNY = 7.25  # 1 USD = 7.25 CNY

SMM_URL = "https://hq.smm.cn/tungsten"
SMM_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": "https://www.smm.cn/",
}

GOOGLE_NEWS_SOURCES = [
    (
        "Google-APT",
        "https://news.google.com/rss/search?q=%22ammonium+paratungstate%22+price&hl=en-US&gl=US&ceid=US:en",
    ),
    (
        "Google-APT-ZH",
        "https://news.google.com/rss/search?q=%22tungsten+APT%22+price&hl=en-US&gl=US&ceid=US:en",
    ),
]

# 僅供 Google News 備援
PRICE_RE  = re.compile(r"\$?\s*(\d[\d,]*(?:\.\d+)?)\s*(?:/\s*MTU|per\s*MTU|MTU|mtu)", re.IGNORECASE)
NUMBER_RE = re.compile(r"\$?\s*(\d{3,5}(?:[.,]\d{1,3})?)")


def load_env():
    env = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


def load_cache():
    if CACHE_FILE.exists():
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    return {"last_updated": None, "latest_price_usd_mtu": None, "source": None, "history": []}


def save_cache(data):
    CACHE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def fetch_apt_smm():
    """從 SMM 抓 APT 報價。回傳 dict 或 None。"""
    time.sleep(2)
    try:
        r = requests.get(SMM_URL, headers=SMM_HEADERS, timeout=20)
        r.raise_for_status()
        text = r.content.decode("utf-8", errors="replace")

        # 優先：APT CIF 鹿特丹 USD/MTU（區間格式）
        m = re.search(
            r"APT\s*CIF鹿特丹报价.*?(\d{3,5})[–\-—]\s*(\d{3,5})\s*美元/吨度",
            text,
        )
        if m:
            lo, hi = float(m.group(1)), float(m.group(2))
            price = (lo + hi) / 2
            print(f"[SMM] CIF Rotterdam range: {lo}-{hi} → midpoint {price}")
            return {"price": price, "source": f"SMM（APT CIF 鹿特丹 {int(lo)}-{int(hi)}）", "unit": "USD/MTU"}

        # 備援：SMM 統計 APT 收報（人民幣/噸 → 換算）
        m2 = re.search(
            r"SMM\s*APT\s*收报\s*(\d+(?:\.\d+)?)\s*万元/吨",
            text,
        )
        if m2:
            cny_wan = float(m2.group(1))
            price = cny_wan * 10000 / USD_CNY / 100
            print(f"[SMM] Domestic: {cny_wan}萬元/噸 → {price:.0f} USD/MTU")
            return {"price": price, "source": f"SMM 國內（{cny_wan}萬元/噸，換算）", "unit": "USD/MTU"}

        # 最後備援：APT 散貨市場報價（人民幣/噸）
        m3 = re.search(r"APT[^。\n]{0,30}?(\d+(?:\.\d+)?)\s*万元/吨", text)
        if m3:
            cny_wan = float(m3.group(1))
            # 合理性過濾：國內 APT 約 40-120 萬元/噸
            if 30 <= cny_wan <= 200:
                price = cny_wan * 10000 / USD_CNY / 100
                print(f"[SMM] Market: {cny_wan}萬元/噸 → {price:.0f} USD/MTU")
                return {"price": price, "source": f"SMM 散貨（{cny_wan}萬元/噸，換算）", "unit": "USD/MTU"}

        print("[SMM] 找不到 APT 報價數字")
    except Exception as e:
        print(f"[SMM] error: {e}")
    return None


def fetch_apt_google_news():
    """備援：從 Google News RSS 標題估算。回傳 dict 或 None。"""
    time.sleep(2)
    candidates = []
    headers = {"User-Agent": "Mozilla/5.0 (compatible; HermesAgent/1.0)"}
    for src_name, url in GOOGLE_NEWS_SOURCES:
        try:
            resp = requests.get(url, headers=headers, timeout=15)
            resp.raise_for_status()
            root = ET.fromstring(resp.content)
            for item in root.iter("item"):
                title_el = item.find("title")
                if title_el is None or not title_el.text:
                    continue
                t = title_el.text.strip()
                m = PRICE_RE.search(t)
                if m:
                    price = float(m.group(1).replace(",", ""))
                    if 100 <= price <= 20000:
                        candidates.append((price, src_name))
                        continue
                for m2 in NUMBER_RE.finditer(t):
                    price = float(m2.group(1).replace(",", "").replace(".", ""))
                    if 500 <= price <= 9999:
                        candidates.append((price, f"{src_name}(估)"))
                        break
        except Exception as e:
            print(f"[{src_name}] fetch error: {e}")

    if not candidates:
        return None

    prices = [p for p, _ in candidates]
    median_p = sorted(prices)[len(prices) // 2]
    print(f"[GoogleNews] median from {len(candidates)} candidates: {median_p}")
    return {"price": median_p, "source": "Google News 標題估算", "unit": "USD/MTU(估)"}


def fetch_apt_price():
    """依優先順序嘗試各資料源，回傳 dict 或 None。"""
    result = fetch_apt_smm()
    if result:
        return result

    print("[fallback] SMM 失敗，嘗試 Google News")
    result = fetch_apt_google_news()
    if result:
        return result

    return None


def price_status(price):
    if price is None:
        return "⚠️ 無法取得報價"
    if price >= 2000:
        return "🟢 高位｜故事支撐強"
    if price >= 1500:
        return "🟡 警戒｜B2 警戒線接近"
    if price >= 500:
        return "🔴 B2 警戒觸發｜建議主動評估 ALM 持倉"
    return "🔴🔴 B2 出場訊號"


def build_message(today, price, source, prev_price):
    status = price_status(price)

    if price is None:
        return (
            f"🪨 鎢 APT 週報｜{today}\n\n"
            f"{status}\n"
            "本週報價抓取失敗，請手動確認。\n"
            "參考：https://hq.smm.cn/tungsten"
        )

    change_str = ""
    if prev_price:
        delta = price - prev_price
        pct = delta / prev_price * 100
        sign = "+" if delta >= 0 else ""
        change_str = f"\n上週：${prev_price:,.0f} /MTU\n週變化：{sign}${delta:,.0f} ({sign}{pct:.1f}%)\n"

    b2_line = ""
    if price >= 1500:
        b2_dist_pct = (price - 1500) / 1500 * 100
        b2_line = f"B2 警戒線 $1,500 距離：+{b2_dist_pct:.0f}%\n"

    source_label = f"來源：{source}"
    if "估算" in source:
        source_label += "（數字為標題估算，僅供參考）"

    return (
        f"🪨 鎢 APT 週報｜{today}\n\n"
        f"現價：${price:,.0f} /MTU"
        f"{change_str}"
        f"\n{status}\n"
        f"{b2_line}"
        f"{source_label}"
    )


def send_telegram(bot_token, chat_id, text):
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    resp = requests.post(url, json={
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }, timeout=15)
    resp.raise_for_status()


def main():
    env = load_env()
    bot_token = env.get("TELEGRAM_BOT_TOKEN", "")
    chat_id   = env.get("TELEGRAM_CHAT_ID", "")

    today = datetime.now().strftime("%Y-%m-%d")
    cache = load_cache()
    prev_price = cache.get("latest_price_usd_mtu")

    result = fetch_apt_price()
    if result:
        price  = result["price"]
        source = result["source"]
        print(f"[result] 來源={source}  原始單位={result['unit']}  USD/MTU={price:.0f}")
    else:
        price  = None
        source = None

    msg = build_message(today, price, source, prev_price)
    print()
    print(msg)

    if price is not None:
        cache["last_updated"] = today
        cache["latest_price_usd_mtu"] = price
        cache["source"] = source
        history = cache.get("history", [])
        if not history or history[-1]["date"] != today:
            history.append({"date": today, "price": price})
        cache["history"] = history[-52:]  # 保留約一年
        save_cache(cache)

    if bot_token and chat_id:
        try:
            send_telegram(bot_token, chat_id, msg)
        except Exception as e:
            print(f"[telegram] send error: {e}")


if __name__ == "__main__":
    main()
