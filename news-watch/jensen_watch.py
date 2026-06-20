#!/usr/bin/env python3
import os
import re
import sys
import json
import hashlib
import sqlite3
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta


# Load .env
_env_path = os.path.expanduser("~/.hermes/.env")
if os.path.exists(_env_path):
    for _line in open(_env_path):
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")

def send_telegram(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    requests.post(url, json={
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }, timeout=15)

LAST_IDS_FILE  = os.path.expanduser("~/.hermes/jensen_last_ids.json")
SEEN_DB_PATH   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "jensen_watch_seen.db")
TITLE_OVERLAP_THRESHOLD = 0.7
SEEN_DB_TTL_HOURS = 72
OLLAMA_URL     = "http://127.0.0.1:11434/api/generate"
MODEL_4B       = "sorc/qwen3.5-instruct-uncensored:4b"
MODEL_9B       = "frob/qwen3.5-instruct:9b"

FEEDS = [
    {
        "name": "Google News",
        "url": "https://news.google.com/rss/search?q=Jensen+Huang+NVIDIA&hl=en-US&gl=US&ceid=US:en",
    },
]
HEADERS            = {"User-Agent": "Mozilla/5.0 (compatible; HermesAgent/1.0)"}
MAX_ITEMS_PER_FEED = 5
DEDUP_HOURS        = 12
NEWS_MAX_AGE_HOURS = 36

import datetime as _dt
from utils import now_et_str
_today = _dt.date.today()
_is_event_week = _dt.date(2026, 6, 1) <= _today <= _dt.date(2026, 6, 6)
SCORE_THRESHOLD = 4 if _is_event_week else 3

SCORE_PROMPT = """你是財經新聞重要性評分助手，只輸出一個整數，不輸出任何其他內容。請使用繁體中文思考。

評分標準（1–5）：
5 = 直接影響 NVIDIA 股價：新產品發布、財報數據、重大合約、競爭對手產品、股價大漲大跌
4 = 間接影響：合作夥伴重要消息、產業政策、重要客戶動態
3 = 背景資訊：一般產業評論、政策討論、市場預測
2 = 人物觀點：CEO 演講內容、個人看法、社交活動
1 = 無關：薪資福利、個人傳記、非財經內容

範例：
標題：Nvidia Q2 earnings beat estimates, stock up 15% → 5
標題：Jensen Huang says AI will transform every industry → 2
標題：Nvidia partners with AWS for new cloud GPU service → 4
標題：Jensen Huang's net worth reaches $100B → 1
標題：Marvell stock soars 32% after Jensen Huang endorsement → 5
標題：Jensen Huang attends dinner with US senators → 2

標題：{title}"""

DEDUP_PROMPT = """你是新聞去重助手，只輸出 YES 或 NO，不輸出任何其他內容。請使用繁體中文思考。
YES = 兩則新聞描述同一事件
NO = 兩則新聞描述不同事件

範例：
新標題：MRVL shares jump 30% after Jensen Huang praise
已推標題：Marvell stock soars 32% as Nvidia's Huang endorses company
→ YES（同一事件：黃仁勳讚揚 Marvell 導致股價上漲）

新標題：Jensen Huang announces RTX Spark chip at Computex
已推標題：Nvidia unveils new Blackwell Ultra GPU at Computex
→ NO（不同產品發布：RTX Spark ≠ Blackwell Ultra）

新標題：Nvidia CEO warns chip export ban will hurt revenue
已推標題：Jensen Huang meets senators to discuss AI regulation
→ NO（不同事件：一個是出口禁令警告，一個是國會會面）

新標題：{new_title}
已推標題：{pushed_title}"""

def load_state():
    if os.path.exists(LAST_IDS_FILE):
        with open(LAST_IDS_FILE, "r") as f:
            return json.load(f)
    return {}

def save_state(data):
    with open(LAST_IDS_FILE, "w") as f:
        json.dump(data, f, ensure_ascii=False)

def item_id(item):
    guid = item.findtext("guid") or ""
    if guid:
        return guid
    title = item.findtext("title") or ""
    link  = item.findtext("link") or ""
    return hashlib.md5(f"{title}{link}".encode()).hexdigest()

def fetch_feed(url):
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    root    = ET.fromstring(resp.content)
    channel = root.find("channel")
    if channel is None:
        return []
    return channel.findall("item")

def ollama(model, prompt, num_ctx=1024, timeout=180):
    payload = {
        "model":   model,
        "prompt":  prompt,
        "stream":  False,
        "keep_alive": 300,
        "options":    {"num_ctx": num_ctx, "temperature": 0}
    }
    resp = requests.post(OLLAMA_URL, json=payload, timeout=timeout)
    resp.raise_for_status()
    return resp.json().get("response", "").strip()

def unload(model):
    try:
        requests.post(OLLAMA_URL, json={"model": model, "prompt": "", "keep_alive": 0}, timeout=30)
        import time; time.sleep(2)
    except Exception as e:
        print(f"[unload] {model} error: {e}", file=__import__('sys').stderr)

SKIP_KEYWORDS = [
    "salary", "pay", "compensation", "wage", "earnings per share",
    "stock split", "buyback", "dividend",
    "hire", "layoff", "fired", "resign", "appoint",
    "interview", "profile", "biography",
]

def is_irrelevant(title):
    t = title.lower()
    return any(kw in t for kw in SKIP_KEYWORDS)

def _seen_db_conn():
    return sqlite3.connect(SEEN_DB_PATH)

def init_seen_db():
    with _seen_db_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS seen_items (
                url_hash TEXT PRIMARY KEY,
                title_text TEXT,
                published_at TEXT,
                seen_at TEXT,
                pushed INTEGER
            )
        """)

def cleanup_seen_db():
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=SEEN_DB_TTL_HOURS)).isoformat()
    with _seen_db_conn() as conn:
        conn.execute("DELETE FROM seen_items WHERE seen_at < ?", (cutoff,))

def item_url_hash(link, title):
    key = link if link else title
    return hashlib.md5(key.encode("utf-8")).hexdigest()

def is_url_seen(url_hash):
    with _seen_db_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM seen_items WHERE url_hash = ?", (url_hash,)
        ).fetchone()
    return row is not None

def _tokenize_title(title):
    return set(re.findall(r"[a-z0-9]+", title.lower()))

def title_token_overlap(t1, t2):
    a, b = _tokenize_title(t1), _tokenize_title(t2)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)

def is_title_seen(title):
    with _seen_db_conn() as conn:
        rows = conn.execute("SELECT title_text FROM seen_items").fetchall()
    for (stored_title,) in rows:
        if title_token_overlap(title, stored_title) > TITLE_OVERLAP_THRESHOLD:
            return True
    return False

def record_seen(url_hash, title, published_at, pushed):
    seen_at = datetime.now(timezone.utc).isoformat()
    with _seen_db_conn() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO seen_items
            (url_hash, title_text, published_at, seen_at, pushed)
            VALUES (?, ?, ?, ?, ?)
            """,
            (url_hash, title, published_at or "", seen_at, int(pushed)),
        )

def score_importance(title):
    raw = ollama(MODEL_4B, SCORE_PROMPT.format(title=title))
    for ch in raw:
        if ch.isdigit():
            return int(ch)
    return 3

def is_duplicate_event(new_title, pushed_titles):
    if not pushed_titles:
        return False
    unload(MODEL_4B)
    # 逐一比對，任一 YES 即為重複
    result = False
    for pushed in pushed_titles:
        try:
            raw = ollama(MODEL_9B, DEDUP_PROMPT.format(
                new_title=new_title,
                pushed_title=pushed
            ))
            if raw.strip().upper().startswith("Y"):
                result = True
                break
        except Exception as e:
            print(f"[dedup] error: {e}", file=sys.stderr)
    unload(MODEL_9B)
    return result


def resolve_url(url, timeout=5):
    try:
        import requests
        r = requests.get(url, allow_redirects=True, timeout=timeout,
                         headers={"User-Agent": "Mozilla/5.0"})
        return r.url
    except:
        return url

def translate(text):
    return ollama(
        MODEL_4B,
        f"將以下英文翻譯成繁體中文，只輸出翻譯結果，不要加任何說明：\n\n{text}",
        num_ctx=2048
    )

def main():
    init_seen_db()
    cleanup_seen_db()

    state = load_state()
    now   = datetime.now(timezone.utc)

    pushed_entries = state.get("pushed_titles", [])
    pushed_entries = [
        e for e in pushed_entries
        if (now - datetime.fromisoformat(e["ts"])) < timedelta(hours=DEDUP_HOURS)
    ]
    pushed_titles = [e["title"] for e in pushed_entries]

    # URL hash 去重狀態（新層，在語意去重前）
    pushed_urls = state.get("pushed_urls", []) or []
    pushed_urls = pushed_urls[-100:]  # 只保留最近 100 筆

    messages = []

    for feed in FEEDS:
        feed_name = feed["name"]
        try:
            items = fetch_feed(feed["url"])
        except Exception as e:
            print(f"[{feed_name}] fetch error: {e}", file=sys.stderr)
            continue

        seen         = set(state.get(feed_name, []))
        is_first_run = len(seen) == 0
        new_seen     = list(seen)
        new_items    = []

        for item in items:
            iid = item_id(item)
            if iid not in seen:
                new_items.append(item)
                new_seen.append(iid)

        state[feed_name] = new_seen[-50:]

        if is_first_run:
            print(f"[{feed_name}] 首次執行，已記錄 {len(new_items)} 筆 ID，下次才開始推播。", file=sys.stderr)
            continue

        new_items = new_items[:MAX_ITEMS_PER_FEED]

        for item in new_items:
            title       = item.findtext("title") or "(no title)"
            pub_date    = item.findtext("pubDate") or ""
            link        = item.findtext("link") or ""

            # 時間過濾
            if pub_date:
                try:
                    from email.utils import parsedate_to_datetime
                    pub_dt = parsedate_to_datetime(pub_date)
                    if pub_dt.tzinfo is None:
                        pub_dt = pub_dt.replace(tzinfo=timezone.utc)
                    age_hours = (now - pub_dt).total_seconds() / 3600
                    if age_hours > NEWS_MAX_AGE_HOURS:
                        print(f"[{feed_name}] 跳過舊聞({age_hours:.0f}h)：{title}", file=sys.stderr)
                        continue
                except Exception as e:
                    print(f"[{feed_name}] date parse error: {e}", file=sys.stderr)

            # 關鍵字硬過濾
            if is_irrelevant(title):
                print(f"[{feed_name}] 關鍵字過濾：{title}", file=sys.stderr)
                record_seen(item_url_hash(link, title), title, pub_date, 0)
                continue

            db_url_hash = item_url_hash(link, title)

            # DB 去重第一層：URL hash
            if is_url_seen(db_url_hash):
                print(f"[{feed_name}] DB 重複 URL 跳過：{title}", file=sys.stderr)
                continue

            # DB 去重第二層：標題 token overlap
            if is_title_seen(title):
                print(f"[{feed_name}] DB 重複標題跳過：{title}", file=sys.stderr)
                record_seen(db_url_hash, title, pub_date, 0)
                continue

            # 評分（4b）
            try:
                score = score_importance(title)
            except Exception as e:
                print(f"[{feed_name}] score error: {e}", file=sys.stderr)
                score = 4  # timeout 時預設通過
            if score < SCORE_THRESHOLD:
                print(f"[{feed_name}] 跳過低分({score})：{title}", file=sys.stderr)
                record_seen(db_url_hash, title, pub_date, 0)
                continue

            # URL hash 去重（在語意去重 is_duplicate_event 之前）
            url_hash = None
            if link:
                url_hash = hashlib.md5(link.encode("utf-8")).hexdigest()
                if url_hash in pushed_urls:
                    print(f"[{feed_name}] 重複 URL 跳過：{title}", file=sys.stderr)
                    record_seen(db_url_hash, title, pub_date, 0)
                    continue

            # 去重（9b）
            try:
                if is_duplicate_event(title, pushed_titles):
                    print(f"[{feed_name}] 重複事件跳過：{title}", file=sys.stderr)
                    record_seen(db_url_hash, title, pub_date, 0)
                    continue
            except Exception as e:
                print(f"[{feed_name}] dedup error: {e}", file=sys.stderr)

            # 翻譯（4b）
            try:
                title_zh = translate(title)
            except Exception as e:
                print(f"[{feed_name}] translate error: {e}", file=sys.stderr)
                title_zh = "(翻譯失敗)"

            stars = "⭐" * score
            messages.append(
                f"🟢 Jensen News {stars}  {now_et_str()}\n"
                f'🇹🇼 {title_zh}  <a href="{link}">原文連結</a>\n'
                f"📅 {pub_date}"
            )

            record_seen(db_url_hash, title, pub_date, 1)

            pushed_entries.append({"title": title, "ts": now.isoformat()})
            pushed_titles.append(title)

            if url_hash:
                pushed_urls.append(url_hash)
                pushed_urls = pushed_urls[-100:]  # 只保留最近 100 筆

    state["pushed_titles"] = pushed_entries
    state["pushed_urls"] = pushed_urls[-100:] if pushed_urls else []
    save_state(state)

    if messages:
        for msg in messages:
            send_telegram(msg)

if __name__ == "__main__":
    main()
