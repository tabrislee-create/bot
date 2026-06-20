#!/usr/bin/env python3
import re
import requests
import xml.etree.ElementTree as ET
from datetime import datetime
import zoneinfo
from utils import now_et_str

OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
OLLAMA_MODEL = "frob/qwen3.5-instruct:9b"

RSS_SOURCES = {
    "tungsten": [
        ("Mining.com",     "https://mining.com/feed/"),
        ("Kitco",          "https://www.kitco.com/rss/news.rss"),
        ("Google Tungsten","https://news.google.com/rss/search?q=tungsten+metal+supply&hl=en-US&gl=US&ceid=US:en"),
        ("Google APT",     "https://news.google.com/rss/search?q=ammonium+paratungstate&hl=en-US&gl=US&ceid=US:en"),
        ("Google Almonty", "https://news.google.com/rss/search?q=Almonty+tungsten&hl=en-US&gl=US&ceid=US:en"),
    ],
    "gold": [
        ("GoldSeek",   "https://news.goldseek.com/newsRSS.xml"),
        ("Mining.com", "https://mining.com/feed/"),
        ("Yahoo",      "https://finance.yahoo.com/news/rss"),
    ],
    "silver": [
        ("SilverSeek", "https://silverseek.com/rss.xml"),
        ("Mining.com", "https://mining.com/feed/"),
        ("Yahoo",      "https://finance.yahoo.com/news/rss"),
    ],
}

KEYWORDS = {
    "tungsten": re.compile(
        r"tungsten|APT|ammonium paratungstate|ferrotungsten"
        r"|tungsten export ban|tungsten sanctions|NDAA tungsten|tungsten quota|APT price"
        r"|鎢出口管制|鎢礦開採|中美礦產談判",
        re.IGNORECASE,
    ),
    "gold":     re.compile(r"\bgold\b|XAU", re.IGNORECASE),
    "silver":   re.compile(r"\bsilver\b|XAG", re.IGNORECASE),
}

MAX_TITLES = 5


def fetch_spot_price(symbol):
    try:
        resp = requests.get(f"https://api.gold-api.com/price/{symbol}", timeout=5)
        resp.raise_for_status()
        data = resp.json()
        price = data.get("price")
        if price:
            return f"${price:,.2f} USD/oz"
    except Exception:
        pass
    return None


def fetch_titles(metal, sources):
    seen = set()
    titles = []
    keyword = KEYWORDS[metal]
    headers = {"User-Agent": "Mozilla/5.0 (compatible; HermesAgent/1.0)"}
    for name, url in sources:
        try:
            resp = requests.get(url, headers=headers, timeout=10)
            resp.raise_for_status()
            content = resp.content
            try:
                root = ET.fromstring(content)
            except ET.ParseError:
                root = ET.fromstring(content.decode("windows-1252", errors="replace").encode("utf-8"))
            for item in root.iter("item"):
                title_el = item.find("title")
                if title_el is None or not title_el.text:
                    continue
                t = title_el.text.strip()
                if not keyword.search(t):
                    continue
                key = t.lower()[:60]
                if key in seen:
                    continue
                seen.add(key)
                titles.append(f"[{name}] {t}")
                if len(titles) >= MAX_TITLES:
                    break
        except Exception:
            pass
        if len(titles) >= MAX_TITLES:
            break
    return titles


def summarize(metal_label, titles, date_str):
    if not titles:
        return "本日無相關新聞。"
    joined = "\n".join(titles)
    prompt = (
        f"以下是 {date_str} 關於{metal_label}的英文新聞標題：\n\n{joined}\n\n"
        f"請用繁體中文條列摘要，相同事件只寫一次。\n"
        f"重要規則：\n"
        f"1. 只根據上方標題內容摘要，不要自行推斷或補充標題以外的資訊。\n"
        f"2. 如果標題中有提到價格數字，請保留。\n"
        f"3. 每條 2 至 3 句說明，總字數 300 字以內。"
    )
    payload = {
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {"num_ctx": 2048, "temperature": 0.5},
    }
    resp = requests.post(OLLAMA_URL, json=payload, timeout=300)
    resp.raise_for_status()
    return resp.json().get("response", "").strip()


def main():
    tz = zoneinfo.ZoneInfo("Asia/Taipei")
    now = datetime.now(tz)
    date_str = now.strftime("%Y-%m-%d")

    sections = [
        ("tungsten", "🪨 鎢礦（Tungsten）", None),
        ("gold",     "🥇 黃金（Gold）",      "XAU"),
        ("silver",   "🥈 白銀（Silver）",    "XAG"),
    ]

    output_parts = [f"⛏️ 金屬市場日報（{date_str}）\n🕐 {now_et_str()}\n"]

    for metal, label, symbol in sections:
        price_str = ""
        if symbol:
            price = fetch_spot_price(symbol)
            if price:
                price_str = f"現貨價：{price}\n"
        titles = fetch_titles(metal, RSS_SOURCES[metal])
        summary = summarize(label, titles, date_str)
        output_parts.append(f"{label}\n{price_str}{summary}")

    print("\n\n".join(output_parts))


if __name__ == "__main__":
    main()
