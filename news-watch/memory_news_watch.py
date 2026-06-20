#!/usr/bin/env python3
"""
memory_news_watch.py — 記憶體／儲存 × AI 新聞日報
監控：MU、SK Hynix、Samsung、SNDK、WDC、Seagate + 產業動態
篩選：僅保留記憶體／儲存與 AI／資料中心相關
排程：每天 08:50（避開 06:00 晨報、07:00 holdings/alm 的 LLM 時段）
推播：Telegram 單則日報
"""

import os
import re
import sys
import hashlib
import sqlite3
import datetime
import email.utils
import xml.etree.ElementTree as ET

import requests

from utils import today_taipei_str

OLLAMA_URL = "http://127.0.0.1:11434/api/generate"

# ── 設定 ──────────────────────────────────────────────
def load_env(path):
    env = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env

ENV = load_env(os.path.expanduser("~/.hermes/.env"))
TELEGRAM_TOKEN = ENV.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = ENV.get("TELEGRAM_CHAT_ID", "")

DB_PATH = os.path.expanduser("~/.hermes/data/memory_news_watch.db")
MODEL_FILTER = "sorc/qwen3.5-instruct-uncensored:4b"
MODEL_SUMMARY = "frob/qwen3.5-instruct:9b"

SEEN_TTL_HOURS = 24 * 30
NEWS_MAX_AGE_HOURS = 48
TITLE_OVERLAP_THRESHOLD = 0.7
SCORE_THRESHOLD = 3
MAX_ARTICLES = 12
MAX_PER_QUERY = 4

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; HermesAgent/1.0)"}

SEARCH_QUERIES = [
    ("MU", "Micron", "Micron HBM AI memory datacenter"),
    ("SK Hynix", "SK Hynix", "SK Hynix HBM AI memory"),
    ("Samsung", "Samsung", "Samsung HBM DRAM AI memory chip"),
    ("SNDK", "Sandisk", "SanDisk NAND AI storage SSD"),
    ("WDC", "Western Digital", "Western Digital AI storage HDD SSD"),
    ("STX", "Seagate", "Seagate AI storage HDD datacenter"),
    ("產業", "產業", "HBM AI memory demand datacenter"),
    ("產業", "產業", "DRAM NAND AI infrastructure supply"),
]

SKIP_TITLE_PATTERNS = [
    "stock price, quote", "stock price quote", "quote & chart",
    "share price", "stock chart", "earnings per share",
    "salary", "compensation", "dividend", "buyback",
]

FILTER_PROMPT = """You filter news for a memory/storage × AI investor.

Company/topic: {company}
Title: {title}
Description: {description}

INCLUDE only if the story connects memory/storage semiconductors or HDD/SSD/NAND/DRAM/HBM
with AI, GPU, LLM, or hyperscaler/datacenter demand/supply/capex.

SKIP if:
- Samsung phones, TVs, appliances, non-semiconductor businesses
- Generic stock price moves without memory/AI substance
- Executive gossip, unrelated legal news
- Purely AI software/cloud with no memory/storage angle

Reply with EXACTLY one line:
SKIP
or
RELEVANT N
(where N is importance 1-5)"""

# ── 資料庫 ────────────────────────────────────────────
def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS seen_items (
                url_hash TEXT PRIMARY KEY,
                title_text TEXT,
                company TEXT,
                published_at TEXT,
                seen_at TEXT,
                pushed INTEGER
            )
        """)

def cleanup_db():
    cutoff = (
        datetime.datetime.now(datetime.timezone.utc)
        - datetime.timedelta(hours=SEEN_TTL_HOURS)
    ).isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM seen_items WHERE seen_at < ?", (cutoff,))

def item_url_hash(link, title):
    return hashlib.md5((link or title).encode("utf-8")).hexdigest()

def _tokenize_title(title):
    return set(re.findall(r"[a-z0-9]+", title.lower()))

def title_token_overlap(t1, t2):
    a, b = _tokenize_title(t1), _tokenize_title(t2)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)

def is_duplicate(url_hash, title):
    with sqlite3.connect(DB_PATH) as conn:
        if conn.execute(
            "SELECT 1 FROM seen_items WHERE url_hash = ?", (url_hash,)
        ).fetchone():
            return True
        rows = conn.execute("SELECT title_text FROM seen_items").fetchall()
    for (stored,) in rows:
        if title_token_overlap(title, stored) > TITLE_OVERLAP_THRESHOLD:
            return True
    return False

def record_seen(url_hash, title, company, published_at, pushed):
    seen_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO seen_items
            (url_hash, title_text, company, published_at, seen_at, pushed)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (url_hash, title, company, published_at or "", seen_at, int(pushed)),
        )

# ── 新聞抓取 ──────────────────────────────────────────
def fetch_google_news(query, max_results=5):
    url = (
        "https://news.google.com/rss/search"
        f"?q={requests.utils.quote(query)}&hl=en-US&gl=US&ceid=US:en"
    )
    try:
        resp = requests.get(url, headers=HEADERS, timeout=12)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
        items = []
        for item in root.findall(".//item")[:max_results]:
            title = (item.findtext("title") or "").strip()
            source = (item.findtext("source") or "Unknown").strip()
            pub_date = (item.findtext("pubDate") or "").strip()
            link = (item.findtext("link") or "").strip()
            desc_el = item.find("description")
            description = ""
            if desc_el is not None and desc_el.text:
                description = re.sub(r"<[^>]+>", "", desc_el.text).strip()

            if not title:
                continue
            title_lower = title.lower()
            if any(p in title_lower for p in SKIP_TITLE_PATTERNS):
                continue

            if pub_date:
                try:
                    dt = email.utils.parsedate_to_datetime(pub_date)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=datetime.timezone.utc)
                    age = datetime.datetime.now(datetime.timezone.utc) - dt
                    if age.total_seconds() > NEWS_MAX_AGE_HOURS * 3600:
                        continue
                except Exception:
                    pass

            items.append({
                "title": title,
                "source": source,
                "link": link,
                "pub_date": pub_date,
                "description": description,
            })
        return items
    except Exception as e:
        print(f"[News] fetch error ({query}): {e}", file=sys.stderr)
        return []

# ── LLM ───────────────────────────────────────────────
def ollama_call(model, prompt, *, num_ctx=2048, temperature=0.3, timeout=120):
    resp = requests.post(OLLAMA_URL, json={
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"num_ctx": num_ctx, "temperature": temperature},
    }, timeout=timeout)
    resp.raise_for_status()
    return resp.json().get("response", "").strip()

def unload_model(model):
    try:
        requests.post(
            "http://127.0.0.1:11434/api/generate",
            json={"model": model, "prompt": "", "stream": False, "keep_alive": 0},
            timeout=10,
        )
    except Exception:
        pass

def filter_relevance(company, title, description):
    prompt = FILTER_PROMPT.format(
        company=company,
        title=title,
        description=description or "(none)",
    )
    raw = ollama_call(MODEL_FILTER, prompt, temperature=0, timeout=60, num_ctx=2048)
    unload_model(MODEL_FILTER)
    raw_upper = raw.upper()
    if "SKIP" in raw_upper and "RELEVANT" not in raw_upper:
        return False, 0
    match = re.search(r"RELEVANT\s*(\d)", raw_upper)
    if match:
        return True, int(match.group(1))
    if "RELEVANT" in raw_upper:
        return True, SCORE_THRESHOLD
    return False, 0

def build_digest(articles, date_str):
    if not articles:
        return ""

    blocks = []
    for art in articles:
        blocks.append(
            f"[{art['company']}] {art['title']}\n"
            f"Source: {art['source']}\n"
            f"{art['description'] or '(no description)'}"
        )
    joined = "\n\n".join(blocks)

    en_prompt = (
        f"Below are memory/storage × AI news items from {date_str}.\n\n"
        f"{joined}\n\n"
        f"Write an ENGLISH digest. Group by company (Micron, SK Hynix, Samsung, "
        f"Sandisk, Western Digital, Seagate, Industry).\n"
        f"Rules:\n"
        f"- One bullet per distinct story, 1-2 sentences each\n"
        f"- Only facts from the text; do not invent\n"
        f"- Skip duplicate stories across sources\n"
        f"- Omit companies with no items"
    )
    en_digest = ollama_call(MODEL_SUMMARY, en_prompt, temperature=0.2, timeout=180, num_ctx=4096)
    unload_model(MODEL_SUMMARY)

    zh_prompt = (
        f"Translate this memory/storage × AI news digest into Traditional Chinese.\n\n"
        f"Format:\n"
        f"【公司或產業名稱】\n"
        f"• …\n\n"
        f"Rules:\n"
        f"- Faithful translation, do not add facts\n"
        f"- Keep technical terms: HBM, DRAM, NAND, SSD, HDD\n"
        f"- Under 600 Chinese characters\n\n"
        f"English digest:\n{en_digest}"
    )
    zh = ollama_call(MODEL_SUMMARY, zh_prompt, temperature=0.2, timeout=180, num_ctx=4096)
    unload_model(MODEL_SUMMARY)
    return zh

# ── Telegram ────────────────────────────────────────────
def send_telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(text)
        return
    requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=15,
    ).raise_for_status()

def format_digest_message(date_str, digest_zh, articles):
    lines = [f"💾 <b>記憶體／儲存 AI 新聞</b> {date_str}", ""]
    lines.append(digest_zh)
    if articles:
        sources = []
        for art in articles:
            src = art.get("source", "").strip()
            if src and src not in sources:
                sources.append(src)
        if sources:
            lines.append("")
            lines.append(f"資料來源：{'、'.join(sources)}")
    return "\n".join(lines)

# ── 主流程 ────────────────────────────────────────────
def main():
    init_db()
    cleanup_db()

    date_str = today_taipei_str()
    print(f"[Memory News] 開始 {date_str}")

    candidates = []
    seen_hashes = set()

    for ticker, label, query in SEARCH_QUERIES:
        articles = fetch_google_news(query, max_results=MAX_PER_QUERY)
        for art in articles:
            url_hash = item_url_hash(art["link"], art["title"])
            if url_hash in seen_hashes:
                continue
            seen_hashes.add(url_hash)

            if is_duplicate(url_hash, art["title"]):
                print(f"[Skip] 重複：{art['title'][:60]}", file=sys.stderr)
                continue

            relevant, score = filter_relevance(label, art["title"], art["description"])
            record_seen(url_hash, art["title"], label, art["pub_date"], 0)

            if not relevant or score < SCORE_THRESHOLD:
                print(f"[Skip] 不相關/低分({score})：{art['title'][:60]}", file=sys.stderr)
                continue

            art["company"] = label
            art["ticker"] = ticker
            art["score"] = score
            candidates.append(art)
            print(f"[Hit] {score}/5 {label} — {art['title'][:60]}")

            if len(candidates) >= MAX_ARTICLES:
                break
        if len(candidates) >= MAX_ARTICLES:
            break

    if not candidates:
        print("[Memory News] 今日無相關新聞")
        return

    candidates.sort(key=lambda x: (-x["score"], x["company"]))
    digest_zh = build_digest(candidates, date_str)
    if not digest_zh:
        print("[Memory News] 摘要產生失敗", file=sys.stderr)
        return

    msg = format_digest_message(date_str, digest_zh, candidates)
    send_telegram(msg)

    for art in candidates:
        url_hash = item_url_hash(art["link"], art["title"])
        record_seen(url_hash, art["title"], art["company"], art["pub_date"], 1)

    print(f"[Memory News] 已推播 {len(candidates)} 則 → 1 份日報")

if __name__ == "__main__":
    main()