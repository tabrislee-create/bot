#!/usr/bin/env python3
"""持股重大事件監控 — 每天早上掃描所有持股的重大消息"""
import os
import re
import sys
import json
import hashlib
import sqlite3
import requests
import difflib
import yfinance as yf
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from utils import now_et_str

# 從 assistant-bot .env 讀取
def load_env(path):
    env = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env

ENV           = load_env(os.path.expanduser("~/.hermes/.env"))
BOT_TOKEN     = ENV.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID       = ENV.get("TELEGRAM_CHAT_ID", "")

DB_PATH       = "/home/tabris/ft_trades.db"
STATE_FILE    = os.path.expanduser("~/.hermes/holdings_news_state.json")
OLLAMA_URL    = "http://127.0.0.1:11434/api/generate"
MODEL_4B      = "sorc/qwen3.5-instruct-uncensored:4b"
MODEL_9B      = "frob/qwen3.5-instruct:9b"
HEADERS       = {"User-Agent": "Mozilla/5.0 (compatible; HermesAgent/1.0)"}

SCORE_THRESHOLD    = 3
NEWS_MAX_AGE_HOURS = 36
MAX_ITEMS_PER_TICKER = 3
SUMMARY_DEDUPE_RATIO = 0.85  # 摘要正規化後相似度高於此值視為同一事件

# 跨次執行去重（仿 jensen_watch.py / top10_news_watch.py 的 seen_items 模式）
SEEN_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "holdings_news_watch_seen.db")
SEEN_DB_TTL_HOURS = 72
TITLE_OVERLAP_THRESHOLD = 0.7

EVENT_KEYWORDS = "bankruptcy OR lawsuit OR recall OR fraud OR resign OR fired OR investigation OR acquisition OR merger"

TICKER_EXTRA_KEYWORDS = {
    "ALM": [
        "Almonty acquires", "Almonty acquisition", "TUNG acquisition",
        "Guardian Metal Almonty", "DoD tungsten contract", "ALM offtake",
        "Sangdong Phase 2", "鎢 Almonty",
    ],
}

# 股票代碼會跟其他常見名詞/機構縮寫撞名、或台灣公司常用中文名報導時，直接寫死查詢用詞（可多個，OR 查詢）
TICKER_SEARCH_OVERRIDE = {
    "ASX": ["ASE Technology Holding", "日月光", "日月光投控"],  # 避免撞到「澳洲證券交易所」(Australian Securities Exchange) 的縮寫 ASX
    "TSM": ["Taiwan Semiconductor Manufacturing", "台積電", "台灣積體電路"],
}

COMPANY_NAME_CACHE_DAYS = 30   # 自動查到公司全名後的快取天數
COMPANY_NAME_RETRY_DAYS = 1    # 查詢失敗時多快重試一次

def get_company_name(ticker, state):
    cache = state.setdefault("company_names", {})
    entry = cache.get(ticker)
    if entry:
        try:
            age_days = (datetime.now(timezone.utc) - datetime.fromisoformat(entry["updated_at"])).days
        except Exception:
            age_days = COMPANY_NAME_CACHE_DAYS + 1
        ttl = COMPANY_NAME_CACHE_DAYS if entry.get("name") else COMPANY_NAME_RETRY_DAYS
        if age_days < ttl:
            return entry.get("name")

    name = None
    try:
        info = yf.Ticker(ticker).info
        name = info.get("longName") or info.get("shortName")
    except Exception as e:
        print(f"[{ticker}] 公司全名查詢失敗：{e}", file=sys.stderr)

    cache[ticker] = {"name": name, "updated_at": datetime.now(timezone.utc).isoformat()}
    return name

def search_terms(ticker, state):
    if ticker in TICKER_SEARCH_OVERRIDE:
        return TICKER_SEARCH_OVERRIDE[ticker]
    name = get_company_name(ticker, state)
    return [name] if name else [ticker]

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}

def save_state(data):
    with open(STATE_FILE, "w") as f:
        json.dump(data, f, ensure_ascii=False)

EXCLUDE_SYMBOLS = {"CASH.USD"}  # 現金替代的虛擬代號，不是真正的股票

def get_holdings():
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT DISTINCT symbol FROM holdings_snapshot WHERE qty > 0"
    ).fetchall()
    conn.close()
    return [r[0] for r in rows if r[0] not in EXCLUDE_SYMBOLS]

def normalize_summary(text):
    return re.sub(r"[\s\W]+", "", text)

def is_duplicate_summary(summary, seen_summaries):
    norm = normalize_summary(summary)
    for prev in seen_summaries:
        if difflib.SequenceMatcher(None, norm, prev).ratio() >= SUMMARY_DEDUPE_RATIO:
            return True
    return False

def item_id(item):
    guid = item.findtext("guid") or ""
    if guid:
        return guid
    title = item.findtext("title") or ""
    link  = item.findtext("link") or ""
    return hashlib.md5(f"{title}{link}".encode()).hexdigest()

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

def fetch_news(ticker, terms):
    extra = TICKER_EXTRA_KEYWORDS.get(ticker, [])
    extra_str = " OR ".join(f'"{k}"' for k in extra)
    kw = f"{EVENT_KEYWORDS} OR {extra_str}" if extra_str else EVENT_KEYWORDS
    name_clause = " OR ".join(f'"{t}"' for t in terms)
    query = f'({name_clause}) ({kw})'
    url = (
        f"https://news.google.com/rss/search"
        f"?q={requests.utils.quote(query)}&hl=en-US&gl=US&ceid=US:en"
    )
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    root    = ET.fromstring(resp.content)
    channel = root.find("channel")
    return channel.findall("item") if channel else []

def ollama(model, prompt, num_ctx=1024):
    payload = {
        "model":   model,
        "prompt":  prompt,
        "stream":  False,
        "keep_alive": 300,
        "options":    {"num_ctx": num_ctx, "temperature": 0}
    }
    resp = requests.post(OLLAMA_URL, json=payload, timeout=180)
    resp.raise_for_status()
    return resp.json().get("response", "").strip()

def unload(model):
    try:
        requests.post(OLLAMA_URL, json={"model": model, "prompt": "", "keep_alive": 0}, timeout=30)
        import time; time.sleep(2)
    except Exception as e:
        import sys; print(f"[unload] {model} error: {e}", file=sys.stderr)

DEDUP_HOURS = 48  # 每天只跑一次，留兩天緩衝避免隔天被別篇文章重新撈到同一事件

DEDUP_PROMPT = """你是新聞去重助手，只輸出 YES 或 NO，不輸出任何其他內容。請使用繁體中文思考。
YES = 兩則新聞描述同一事件（即使數字、措辭或報導角度不同）
NO = 兩則新聞描述不同事件

範例：
新標題：Vertiv shares rise 4.90% as market reacts to outlook
已推標題：Vertiv stock surges 11.8% on AI capex guidance hike and ThermoKey deal
→ YES（同一事件：Vertiv 股價因財報展望/併購消息上漲，只是不同文章引用不同漲幅數字）

新標題：Nvidia unveils new Blackwell Ultra GPU at Computex
已推標題：Nvidia CEO warns chip export ban will hurt revenue
→ NO（不同事件：一個是產品發布，一個是出口禁令警告）

新標題：{new_title}
已推標題：{pushed_title}
→"""

def is_duplicate_event(new_title, pushed_titles):
    if not pushed_titles:
        return False
    for pushed in pushed_titles:
        try:
            raw = ollama(MODEL_9B, DEDUP_PROMPT.format(new_title=new_title, pushed_title=pushed))
            if raw.strip().upper().startswith("Y"):
                return True
        except Exception as e:
            print(f"[dedup] error: {e}", file=sys.stderr)
    return False

def score_event(ticker, terms, title, summary):
    names = "、".join(terms)
    raw = ollama(
        MODEL_4B,
        f"你是財經風險分析師。\n"
        f"請先判斷這則新聞是否直接與公司「{names}」（股票代碼 {ticker}）相關，"
        f"而不是同名機構、縮寫撞名或其他公司的新聞。\n"
        f"若不相關，輸出 0。\n"
        f"若相關，評估對持股者的重要程度：\n"
        f"5=極重要（高管離職/重大訴訟/財報暴雷/併購）\n"
        f"3-4=值得注意（管理層異動/監管調查/產品問題）\n"
        f"1-2=一般資訊\n"
        f"只輸出一個 0–5 的整數，不要加任何說明。\n\n"
        f"標題：{title}\n摘要：{summary}"
    )
    for ch in raw:
        if ch.isdigit():
            return int(ch)
    return 2

def summarize(ticker, title, summary):
    unload(MODEL_4B)
    result = ollama(
        MODEL_9B,
        f"用繁體中文，一句話說明這則 {ticker} 新聞的重點，不超過30字：\n\n"
        f"標題：{title}\n摘要：{summary}",
        num_ctx=2048
    )
    unload(MODEL_9B)
    return result

def send_telegram(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    resp = requests.post(url, json={
        "chat_id":    CHAT_ID,
        "text":       text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }, timeout=15)
    resp.raise_for_status()

def main():
    init_seen_db()
    cleanup_seen_db()

    state    = load_state()
    now      = datetime.now(timezone.utc)
    tickers  = get_holdings()

    if not tickers:
        return

    seen_global = state.get("seen_ids", {})
    messages    = []

    pushed_entries = state.get("pushed_titles", [])
    pushed_entries = [
        e for e in pushed_entries
        if (now - datetime.fromisoformat(e["ts"])) < timedelta(hours=DEDUP_HOURS)
    ]
    pushed_titles = [e["title"] for e in pushed_entries]

    for ticker in tickers:
        terms = search_terms(ticker, state)
        try:
            items = fetch_news(ticker, terms)
        except Exception as e:
            print(f"[{ticker}] fetch error: {e}", file=sys.stderr)
            continue

        seen_ids = set(seen_global.get(ticker, []))
        is_first = len(seen_ids) == 0
        new_seen = list(seen_ids)
        new_items = []

        for item in items:
            iid = item_id(item)
            if iid not in seen_ids:
                new_items.append((iid, item))
                new_seen.append(iid)

        seen_global[ticker] = new_seen[-30:]

        if is_first:
            print(f"[{ticker}] 首次執行，記錄 {len(new_items)} 筆，下次才推播", file=sys.stderr)
            continue

        sent_summaries = []

        for iid, item in new_items[:MAX_ITEMS_PER_TICKER]:
            title    = item.findtext("title") or ""
            desc     = item.findtext("description") or ""
            pub_date = item.findtext("pubDate") or ""
            link     = item.findtext("link") or ""

            # 時間過濾
            if pub_date:
                try:
                    pub_dt = parsedate_to_datetime(pub_date)
                    if pub_dt.tzinfo is None:
                        pub_dt = pub_dt.replace(tzinfo=timezone.utc)
                    age_hours = (now - pub_dt).total_seconds() / 3600
                    if age_hours > NEWS_MAX_AGE_HOURS:
                        print(f"[{ticker}] 舊聞跳過({pub_date})：{title}", file=sys.stderr)
                        continue
                except Exception as e:
                    print(f"[{ticker}] pubDate 解析失敗，跳過：{pub_date} / {e}", file=sys.stderr)
                    continue
            else:
                print(f"[{ticker}] 無 pubDate，跳過：{title}", file=sys.stderr)
                continue

            db_url_hash = item_url_hash(link, title)

            # DB 去重第一層：URL hash（跨次執行，72h TTL）
            if is_url_seen(db_url_hash):
                print(f"[{ticker}] DB 重複 URL 跳過：{title}", file=sys.stderr)
                continue

            # DB 去重第二層：標題 token overlap（同一事件被不同文章/不同天報導）
            if is_title_seen(title):
                print(f"[{ticker}] DB 重複標題跳過（同事件舊聞）：{title}", file=sys.stderr)
                record_seen(db_url_hash, title, pub_date, 0)
                continue

            try:
                score = score_event(ticker, terms, title, desc)
            except Exception as e:
                print(f"[{ticker}] score error: {e}", file=sys.stderr)
                continue

            if score < SCORE_THRESHOLD:
                print(f"[{ticker}] 低分({score})跳過：{title}", file=sys.stderr)
                record_seen(db_url_hash, title, pub_date, 0)
                continue

            try:
                if is_duplicate_event(title, pushed_titles):
                    print(f"[{ticker}] 跨次執行同事件跳過：{title}", file=sys.stderr)
                    record_seen(db_url_hash, title, pub_date, 0)
                    continue
            except Exception as e:
                print(f"[{ticker}] dedup error: {e}", file=sys.stderr)

            try:
                summary_zh = summarize(ticker, title, desc)
            except Exception:
                summary_zh = title

            if is_duplicate_summary(summary_zh, sent_summaries):
                print(f"[{ticker}] 同事件重複摘要跳過：{summary_zh}", file=sys.stderr)
                record_seen(db_url_hash, title, pub_date, 0)
                continue
            sent_summaries.append(normalize_summary(summary_zh))

            stars = "⭐" * score
            messages.append(
                f"🔔 <b>{ticker}</b> 持股動態 {stars}  {now_et_str()}\n"
                f"{summary_zh}\n"
                f'<a href="{link}">閱讀原文</a>'
            )

            record_seen(db_url_hash, title, pub_date, 1)
            pushed_entries.append({"title": title, "ts": now.isoformat()})
            pushed_titles.append(title)

    state["seen_ids"]      = seen_global
    state["pushed_titles"] = pushed_entries
    save_state(state)

    if not messages:
        print("今日無重大持股異動", file=sys.stderr)
        return

    # 每則分開發，避免單則過長
    for msg in messages:
        try:
            send_telegram(msg)
        except Exception as e:
            print(f"send error: {e}", file=sys.stderr)

if __name__ == "__main__":
    main()
