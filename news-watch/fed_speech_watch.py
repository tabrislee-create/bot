#!/usr/bin/env python3
"""
fed_speech_watch.py — Fed 官員演講即時監控

跑在 TAB-MINI，輪詢頻率較高（建議每 2 小時）以求即時性：
  - 輪詢 Fed 官方演講 RSS feed
  - 用 sqlite seen-db 防止同一篇演講重複推播（仿 jensen_watch.py 的 seen_items 模式）
  - 只有「新出現」的演講才抓全文丟給 TAB-SERVER 的 Ollama 27B 解析並推播 Telegram
  - 首次執行會把目前 feed 上的既有項目當作 baseline 標記為已見（不推播），避免一次性洗版舊演講

FOMC 公告本身（聲明 + 記者會）由 fed_watch.py 處理，邏輯與排程不同（取決於會議行事曆而非輪詢）。

用法：
  python3 fed_speech_watch.py
"""

import os
import re
import sys
import json
import time
import hashlib
import sqlite3
import datetime
import xml.etree.ElementTree as ET
from pathlib import Path

import requests
from bs4 import BeautifulSoup
import httpx

if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

# ── 設定 ─────────────────────────────────────────────

OLLAMA_BASE_URL = "http://100.101.26.58:11434"
OLLAMA_MODEL = "batiai/qwen3.6-27b:iq3"
OLLAMA_TIMEOUT = 180
OLLAMA_NUM_CTX = 32768
OLLAMA_KEEP_ALIVE = 600
OLLAMA_RETRIES = 1

SPEECH_FEED_URL = "https://www.federalreserve.gov/feeds/speeches.xml"
MAX_ITEMS_PER_POLL = 5

SEEN_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fed_speech_watch_seen.db")
DB_PATH = Path.home() / "screener_meta.db"

_env_path = os.path.expanduser("~/.hermes/.env")
if os.path.exists(_env_path):
    for _line in open(_env_path, encoding="utf-8"):
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; fed-speech-watch/1.0)"}


def send_telegram(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    for i in range(0, len(text), 4096):
        chunk = text[i:i + 4096]
        resp = requests.post(url, json={
            "chat_id": CHAT_ID,
            "text": chunk,
            "disable_web_page_preview": True,
        }, timeout=15)
        if resp.status_code != 200:
            print(f"  [warn] Telegram 推播失敗：{resp.status_code} {resp.text}", file=sys.stderr)


# ── seen-db（仿 jensen_watch.py） ───────────────────────

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


def is_first_run():
    with _seen_db_conn() as conn:
        row = conn.execute("SELECT COUNT(*) FROM seen_items").fetchone()
    return row[0] == 0


def item_url_hash(link, title):
    key = link if link else title
    return hashlib.md5(key.encode("utf-8")).hexdigest()


def is_url_seen(url_hash):
    with _seen_db_conn() as conn:
        row = conn.execute("SELECT 1 FROM seen_items WHERE url_hash = ?", (url_hash,)).fetchone()
    return row is not None


def record_seen(url_hash, title, published_at, pushed):
    seen_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    with _seen_db_conn() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO seen_items (url_hash, title_text, published_at, seen_at, pushed)
            VALUES (?, ?, ?, ?, ?)
        """, (url_hash, title, published_at or "", seen_at, int(pushed)))


# ── 演講解析結果持久化（screener_meta.db，供 portfolio-pwa 讀取） ──────

def ensure_speech_analysis_table(con):
    con.execute("""
        CREATE TABLE IF NOT EXISTS fed_speech_analysis (
            url_hash TEXT PRIMARY KEY,
            speaker TEXT,
            speech_date TEXT,
            link TEXT,
            tone TEXT,
            hawkish_score INTEGER,
            rate_view TEXT,
            deviation_from_consensus TEXT,
            importance TEXT,
            importance_reason TEXT,
            key_signals_json TEXT,
            analyzed_at TEXT
        )
    """)


def save_speech_analysis(url_hash, speaker, speech_date, link, parsed):
    con = sqlite3.connect(DB_PATH)
    ensure_speech_analysis_table(con)
    con.execute("""
        INSERT INTO fed_speech_analysis
            (url_hash, speaker, speech_date, link, tone, hawkish_score, rate_view,
             deviation_from_consensus, importance, importance_reason, key_signals_json, analyzed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(url_hash) DO UPDATE SET
            speaker=excluded.speaker, speech_date=excluded.speech_date, link=excluded.link,
            tone=excluded.tone, hawkish_score=excluded.hawkish_score, rate_view=excluded.rate_view,
            deviation_from_consensus=excluded.deviation_from_consensus, importance=excluded.importance,
            importance_reason=excluded.importance_reason, key_signals_json=excluded.key_signals_json,
            analyzed_at=excluded.analyzed_at
    """, (
        url_hash, speaker, speech_date, link,
        parsed.get("tone"), parsed.get("hawkish_score"), parsed.get("rate_view"),
        parsed.get("deviation_from_consensus"), parsed.get("importance"), parsed.get("importance_reason"),
        json.dumps(parsed.get("key_signals", []), ensure_ascii=False),
        datetime.datetime.now().isoformat(),
    ))
    con.commit()
    con.close()


# ── 第一部分：抓取 ─────────────────────────────────────

def fetch_speech_feed():
    resp = requests.get(SPEECH_FEED_URL, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    root = ET.fromstring(resp.content)
    channel = root.find("channel")
    if channel is None:
        return []
    items = []
    for item in channel.findall("item")[:MAX_ITEMS_PER_POLL]:
        items.append({
            "title": item.findtext("title") or "",
            "link": item.findtext("link") or "",
            "published": item.findtext("pubDate") or "",
        })
    return items


def normalize_date(date_str):
    """把 Fed 網頁／RSS 各種日期格式統一成 YYYY-MM-DD，解析失敗就原樣回傳。"""
    if not date_str:
        return date_str
    for fmt in ("%B %d, %Y", "%a, %d %b %Y %H:%M:%S %Z", "%a, %d %b %Y %H:%M:%S %z"):
        try:
            return datetime.datetime.strptime(date_str, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return date_str


def fetch_fed_speech(url):
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    title_tag = soup.find("title") or soup.find("h1")
    title = title_tag.get_text(strip=True) if title_tag else ""
    speaker_match = re.search(r"\bby\s+(.+?)(?:\s+on\s|\s+at\s|,|\s+-\s)", title, re.IGNORECASE)
    speaker = speaker_match.group(1).strip() if speaker_match else title
    speaker = re.sub(r"^(Vice Chair|Chair|Governor|President|Vice Chairman|Chairman)\s+", "", speaker, flags=re.IGNORECASE).strip()

    date_tag = soup.find("p", class_="article__time") or soup.find("time")
    date_text = date_tag.get_text(strip=True) if date_tag else ""

    article = soup.find("div", class_="col-xs-12 col-sm-8 col-md-8") or soup.find("article")
    text = article.get_text("\n", strip=True) if article else soup.get_text("\n", strip=True)
    return text, speaker, date_text, len(text)


# ── 第二部分：27B 解析 ─────────────────────────────────

def _extract_json_text(text: str) -> str:
    text = (text or "").strip()
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        return m.group(1)
    m = re.search(r"\{.*\}", text, re.DOTALL)
    return m.group(0) if m else text


def call_ollama(prompt: str) -> tuple[dict | None, str, str, float]:
    payload = {
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {"num_ctx": OLLAMA_NUM_CTX},
        "keep_alive": OLLAMA_KEEP_ALIVE,
    }
    t0 = time.time()
    last_status, last_raw = "unavailable", ""
    for attempt in range(OLLAMA_RETRIES + 1):
        try:
            with httpx.Client(timeout=OLLAMA_TIMEOUT) as client:
                resp = client.post(f"{OLLAMA_BASE_URL}/api/generate", json=payload)
                resp.raise_for_status()
                raw = resp.json().get("response", "")
        except Exception as e:
            print(f"  [warn] Ollama 第 {attempt + 1} 次呼叫失敗: {e}", file=sys.stderr)
            last_status, last_raw = "unavailable", str(e)
            continue

        try:
            parsed = json.loads(_extract_json_text(raw))
            return parsed, "ok", raw, time.time() - t0
        except Exception as e:
            print(f"  [warn] 第 {attempt + 1} 次回傳非 JSON（{e}），raw 前 500 字：\n{raw[:500]}", file=sys.stderr)
            last_status, last_raw = "parse_error", raw

    return None, last_status, last_raw, time.time() - t0


def build_speech_prompt(speaker, date, speech_text):
    return f"""你是 Fed 政策分析師。請分析以下 Fed 官員演講，只回傳 JSON，不加任何說明或 markdown。

【演講者】{speaker}
【日期】{date}
【內容】
{speech_text}

輸出 JSON：
{{
  "speaker": "姓名",
  "tone": "hawkish/neutral/dovish",
  "hawkish_score": 1到10整數,
  "key_signals": ["訊號1", "訊號2", "訊號3"],
  "rate_view": "一句話",
  "deviation_from_consensus": "aligned/slightly_hawkish/significantly_hawkish/slightly_dovish/significantly_dovish",
  "importance": "high/medium/low",
  "importance_reason": "一句話"
}}"""


# ── 第三部分：組訊息 + 處理單篇演講 ───────────────────────

def bullets(items):
    return "\n".join(f"• {x}" for x in items) if items else "（無）"


def build_telegram_message(speaker, date, parsed, elapsed, chars):
    p = parsed or {}
    return f"""🎤 Fed 官員演講 ⭐ {speaker}（{date}）

立場：{p.get('tone', 'N/A')}（鷹派分數 {p.get('hawkish_score', 'N/A')}/10）
重要性：{p.get('importance', 'N/A')}（{p.get('importance_reason', 'N/A')}）
偏差：{p.get('deviation_from_consensus', 'N/A')}
利率看法：{p.get('rate_view', 'N/A')}

關鍵訊號：
{bullets(p.get('key_signals', []))}

⏱ 解析耗時 {elapsed:.1f}s｜全文 {chars} 字"""


def process_item(item):
    url_hash = item_url_hash(item["link"], item["title"])
    print(f"  新演講：{item['title']}（{item['link']}）", file=sys.stderr)
    try:
        text, speaker, date_text, chars = fetch_fed_speech(item["link"])
    except Exception as e:
        print(f"  [warn] 抓取失敗，跳過：{e}", file=sys.stderr)
        record_seen(url_hash, item["title"], item["published"], pushed=False)
        return
    display_date = normalize_date(date_text) or normalize_date(item["published"]) or (date_text or item["published"])

    parsed, status, _raw, elapsed = call_ollama(build_speech_prompt(speaker, display_date, text))
    print(f"  27B 解析：{status}，耗時 {elapsed:.1f}s", file=sys.stderr)

    if status == "ok":
        save_speech_analysis(url_hash, speaker, display_date, item["link"], parsed)
        msg = build_telegram_message(speaker, display_date, parsed, elapsed, chars)
        if BOT_TOKEN and CHAT_ID:
            send_telegram(msg)
            print("  Telegram 推播完成", file=sys.stderr)
        else:
            print("  [warn] 未設定 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID，略過推播", file=sys.stderr)
        record_seen(url_hash, item["title"], item["published"], pushed=True)
    else:
        print("  [warn] 解析失敗，不推播，下次輪詢會重試", file=sys.stderr)


# ── 主流程 ─────────────────────────────────────────────

def main():
    init_seen_db()

    try:
        items = fetch_speech_feed()
    except Exception as e:
        print(f"[error] RSS feed 抓取失敗：{e}", file=sys.stderr)
        return
    print(f"[FOMC Speech Watch] feed 取得 {len(items)} 篇最新演講", file=sys.stderr)

    if is_first_run():
        print("[init] 首次執行，建立 baseline（不推播既有清單）", file=sys.stderr)
        for item in items:
            record_seen(item_url_hash(item["link"], item["title"]), item["title"], item["published"], pushed=False)
        return

    new_items = [it for it in items if not is_url_seen(item_url_hash(it["link"], it["title"]))]
    if not new_items:
        print("[skip] 沒有新演講", file=sys.stderr)
        return

    print(f"[發現 {len(new_items)} 篇新演講，由舊到新處理]", file=sys.stderr)
    for item in reversed(new_items):
        process_item(item)


if __name__ == "__main__":
    main()
