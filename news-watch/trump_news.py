#!/usr/bin/env python3
import re
import requests
import xml.etree.ElementTree as ET
from datetime import datetime
import zoneinfo

from utils import summarize_trump_news

RSS_SOURCES = [
    ("CNN",  "http://rss.cnn.com/rss/edition.rss"),
    ("Fox",  "https://feeds.foxnews.com/foxnews/latest"),
    ("BBC",  "https://feeds.bbci.co.uk/news/rss.xml"),
    ("NPR",  "https://feeds.npr.org/1001/rss.xml"),
]

def fetch_titles(name, url):
    try:
        headers = {"User-Agent": "Mozilla/5.0 (compatible; HermesAgent/1.0)"}
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
        titles = []
        for item in root.iter("item"):
            title_el = item.find("title")
            if title_el is not None and title_el.text:
                t = title_el.text.strip()
                if re.search(r"trump", t, re.IGNORECASE):
                    desc_el = item.find("description")
                    if desc_el is not None and desc_el.text:
                        d = re.sub(r"<[^>]+>", "", desc_el.text).strip()
                        titles.append(f"[{name}] {t}\n    {d}")
                    else:
                        titles.append(f"[{name}] {t}")
        return titles
    except Exception as e:
        return [f"[{name}] 抓取失敗：{e}"]

def main():
    tz = zoneinfo.ZoneInfo("Asia/Taipei")
    now = datetime.now(tz)
    date_str = now.strftime("%Y-%m-%d")

    all_titles = []
    for name, url in RSS_SOURCES:
        all_titles.extend(fetch_titles(name, url))

    summary = summarize_trump_news(all_titles, date_str)
    sources = "、".join([name for name, _ in RSS_SOURCES])
    print(f"📰 川普新聞摘要（{date_str}）\n\n{summary}\n\n來源：{sources}")

if __name__ == "__main__":
    main()
