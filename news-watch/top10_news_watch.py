#!/usr/bin/env python3
"""
top10_news_watch.py — Top 10 選股新聞摘要 + 技術面一句話
排程：週一–五 08:40（stock-screen-save 08:35 跑完後）
輸入：~/.hermes/scripts/screen_result.json
輸出：Telegram 推播
"""
import os, re, sys, json, time, hashlib, sqlite3, requests, xml.etree.ElementTree as ET, html as htmllib
from datetime import datetime, timezone, timedelta
from utils import now_et, now_et_str, today_et_str, today_et_fmt

SCREEN_PATH  = os.path.expanduser("~/.hermes/scripts/screen_result.json")
SEEN_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "top10_news_watch_seen.db")
TITLE_OVERLAP_THRESHOLD = 0.7
SEEN_DB_TTL_HOURS = 72
OLLAMA_URL   = "http://127.0.0.1:11434/api/generate"
MODEL_4B     = "sorc/qwen3.5-instruct-uncensored:4b"
MODEL_9B     = "frob/qwen3.5-instruct:9b"
HEADERS      = {"User-Agent": "Mozilla/5.0 (compatible; HermesAgent/1.0)"}
NEWS_MAX_AGE_HOURS = 168  # 7天，涵蓋連假情境
SCORE_THRESHOLD    = 2
MAX_NEWS_PER_STOCK = 5

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

def ollama(model, prompt, num_ctx=1024, timeout=180):
    payload = {
        "model": model, "prompt": prompt,
        "stream": False,
        "options": {"num_ctx": num_ctx, "temperature": 0}
    }
    resp = requests.post(OLLAMA_URL, json=payload, timeout=timeout)
    resp.raise_for_status()
    return resp.json().get("response", "").strip()

def unload(model):
    """用完立即卸載模型，釋放 VRAM"""
    try:
        requests.post(OLLAMA_URL, json={"model": model, "prompt": "", "keep_alive": 0}, timeout=15)
    except Exception:
        pass

def fetch_google_news(ticker):
    url = f"https://news.google.com/rss/search?q={ticker}+stock+company&hl=en-US&gl=US&ceid=US:en"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
        channel = root.find("channel")
        if channel is None:
            return []
        items = channel.findall("item")[:MAX_NEWS_PER_STOCK]
        now = datetime.now(timezone.utc)
        results = []
        for item in items:
            title    = item.findtext("title") or ""
            pub_date = item.findtext("pubDate") or ""
            link     = item.findtext("link") or ""
            if pub_date:
                try:
                    from email.utils import parsedate_to_datetime
                    pub_dt = parsedate_to_datetime(pub_date)
                    if pub_dt.tzinfo is None:
                        pub_dt = pub_dt.replace(tzinfo=timezone.utc)
                    if (now - pub_dt).total_seconds() / 3600 > NEWS_MAX_AGE_HOURS:
                        continue
                except Exception:
                    pass
            if title:
                results.append({"title": title, "link": link, "pub_date": pub_date})
        return results
    except Exception as e:
        print(f"[{ticker}] 抓新聞失敗: {e}", file=sys.stderr)
        return []

def score_news(ticker, title):
    prompt = f"""你是財經新聞重要性評分助手，只輸出一個整數，不輸出任何其他內容。
評分標準（1–5）：
5 = 直接影響股價：財報、重大合約、併購、重大產品發布
4 = 間接影響：合作夥伴消息、產業政策、法人調評
3 = 背景資訊：一般產業評論、市場預測
2 = 人物觀點：CEO 演講、個人看法
1 = 無關：薪資、傳記、非財經內容
股票代號：{ticker}
標題：{title}"""
    try:
        raw = ollama(MODEL_4B, prompt)
        for ch in raw:
            if ch.isdigit():
                return int(ch)
    except Exception:
        pass
    return 3

def analyze_stock(stock):
    """9b 生成技術面一句話 + 整體情緒"""
    ticker       = stock["ticker"]
    rsi          = stock.get("rsi", 0)
    pct_from_high= stock.get("pct_from_high", 0)
    vol_ratio    = stock.get("vol_ratio", 1)
    ma_status    = "多頭排列" if stock.get("ma20", 0) > stock.get("ma60", 0) else "均線偏弱"
    prompt = f"""你是股票技術分析師，請用繁體中文一句話（15字以內）描述 {ticker} 目前技術面狀態。
數據：RSI {rsi:.0f}，距52週高點 {pct_from_high:.1f}%，量比 {vol_ratio:.1f}，{ma_status}
只輸出一句話，不要任何說明："""
    try:
        summary = ollama(MODEL_9B, prompt, num_ctx=512, timeout=120)
        # 清理雜訊
        summary = summary.strip().strip('"').strip("'")
        if len(summary) > 30:
            summary = summary[:30]
    except Exception as e:
        summary = f"RSI {rsi:.0f}，{ma_status}"
    return summary

def main():
    init_seen_db()
    cleanup_seen_db()

    # 1. 讀 Top 10
    if not os.path.exists(SCREEN_PATH):
        print("找不到 screen_result.json，請先執行 stock_screen.py", file=sys.stderr)
        sys.exit(1)

    with open(SCREEN_PATH, encoding="utf-8") as f:
        screen_data = json.load(f)

    generated_at = screen_data.get("generated_at", "")

    # 時效檢查：generated_at 超過 24 小時視為舊資料，跳過（週末/休市日保護）
    if generated_at:
        try:
            from datetime import datetime
            gen_dt = datetime.strptime(generated_at, "%Y-%m-%d %H:%M:%S")
            age_hours = (datetime.now() - gen_dt).total_seconds() / 3600
            if age_hours > 24:
                print(f"screen_result 已超過 24 小時（{age_hours:.1f}h），今日非交易日或 screen 未跑，跳過推播", file=sys.stderr)
                sys.exit(0)
        except Exception as e:
            print(f"時效檢查失敗: {e}", file=sys.stderr)

    top10 = screen_data.get("top10", [])
    if not top10:
        print("今日無 Top 10，結束", file=sys.stderr)
        sys.exit(0)

    print(f"開始處理 {len(top10)} 支 Top 10...", file=sys.stderr)

    # 2. 逐支處理
    messages = []
    for i, stock in enumerate(top10, 1):
        ticker = stock["ticker"]
        score  = stock.get("score", 0)
        rsi    = stock.get("rsi", 0)
        pct_high = stock.get("pct_from_high", 0)
        vol_ratio= stock.get("vol_ratio", 1)
        sector = stock.get("sector", "")

        print(f"[{i}/{ len(top10)}] {ticker}", file=sys.stderr)

        # 技術面一句話（9b），用完立即卸載
        tech_summary = analyze_stock(stock)
        unload(MODEL_9B)
        time.sleep(0.5)

        # 抓新聞
        news_items = fetch_google_news(ticker)

        # 評分過濾（4b）
        good_news = []
        for item in news_items:
            title    = item["title"]
            link     = item["link"]
            pub_date = item.get("pub_date", "")

            # 關鍵字硬過濾
            if is_irrelevant(title):
                print(f"[{ticker}] 關鍵字過濾：{title}", file=sys.stderr)
                record_seen(item_url_hash(link, title), title, pub_date, 0)
                continue

            db_url_hash = item_url_hash(link, title)

            # DB 去重第一層：URL hash
            if is_url_seen(db_url_hash):
                print(f"[{ticker}] DB 重複 URL 跳過：{title}", file=sys.stderr)
                continue

            # DB 去重第二層：標題 token overlap
            if is_title_seen(title):
                print(f"[{ticker}] DB 重複標題跳過：{title}", file=sys.stderr)
                record_seen(db_url_hash, title, pub_date, 0)
                continue

            s = score_news(ticker, title)
            if s >= SCORE_THRESHOLD:
                record_seen(db_url_hash, title, pub_date, 1)
                good_news.append((s, item))
            else:
                record_seen(db_url_hash, title, pub_date, 0)
            time.sleep(0.2)

        # 4b 評分完後卸載
        if news_items:
            unload(MODEL_4B)

        # 組推播段落（所有純文字 escape，避免 HTML parse 失敗）
        stars = "★" * min(score, 5)
        sector_tag = f" [{htmllib.escape(sector)}]" if sector else ""
        tech_esc = htmllib.escape(tech_summary)
        header = f"{stars}{score} {htmllib.escape(ticker)}{sector_tag}  RSI {rsi:.0f}  距高{pct_high:.0f}%  量比{vol_ratio:.1f}"
        lines = [header, f"  📊 {tech_esc}"]

        if good_news:
            # 最多顯示2則
            for ns, item in sorted(good_news, key=lambda x: -x[0])[:2]:
                title = htmllib.escape(item["title"])
                link  = item["link"]
                lines.append(f'  📰 <a href="{link}">{title}</a>')
        else:
            lines.append("  📰 近期無重要新聞")

        messages.append("\n".join(lines))
        time.sleep(1)

    # 3. 組完整推播
    today_fmt = today_et_fmt()
    header_line = f"【{today_fmt} 選股Top10 + 新聞】"
    output = header_line + "\n\n" + "\n\n".join(messages)
    print(output)

    # 直接打 Telegram API，確保 parse_mode=HTML 生效
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id   = os.getenv("TELEGRAM_CHAT_ID")
    if bot_token and chat_id:
        requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": output,
                "parse_mode": "HTML",
                "disable_web_page_preview": True
            },
            timeout=15
        )

if __name__ == "__main__":
    main()
