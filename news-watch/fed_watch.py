#!/usr/bin/env python3
"""
fed_watch.py — FOMC 公告解讀（正式版）

跑在 TAB-MINI：
  - 用內建 FOMC_MEETING_DATES 行事曆比對「昨天（ET）是否為 FOMC 公告日」，
    非公告日安靜跳過，不呼叫 LLM、不推播
  - 公告日：抓取 FOMC 聲明 + 記者會逐字稿（requests + BeautifulSoup + pdfplumber）
  - 丟給 TAB-SERVER 的 Ollama 27B 解析（與 market_close_review.py 同一台、同模型）
  - FOMC 聲明歷史回填 + 趨勢整合分析（寫入 screener_meta.db 的 fed_fomc_history 表）
  - 結果組成 Telegram 訊息直接推播（走 ~/.hermes/.env 的 bot token，與 jensen_watch.py 同寫法）

用法：
  python3 fed_watch.py                  # 用台北「今天」判斷
  python3 fed_watch.py --date 2026-06-18 # 覆蓋台北「今天」日期（測試用）

理事/官員演講監控已搬到獨立腳本 fed_speech_watch.py（輪詢頻率不同，邏輯不依賴會議行事曆）。
"""

import os
import re
import sys
import json
import time
import sqlite3
import argparse
import datetime
from io import BytesIO
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

# 已知 FOMC 公告日（聲明發布日 = 記者會日），由舊到新。
# 來源：https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm
# 每年底記得補下一年度的會議日期。
FOMC_MEETING_DATES = [
    "20250730", "20250917", "20251029", "20251210",
    "20260128", "20260318", "20260429", "20260617",
    "20260729", "20260916", "20261028", "20261209",
]

DB_PATH = Path.home() / "screener_meta.db"
RESULTS_PATH = os.path.expanduser("~/.hermes/fed_watch_results.json")

_env_path = os.path.expanduser("~/.hermes/.env")
if os.path.exists(_env_path):
    for _line in open(_env_path, encoding="utf-8"):
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; fed-watch/1.0)"}


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


# ── 日期判斷：今天是不是該跑 ───────────────────────────

def get_active_fomc_dates(check_date=None):
    """回傳 (curr_date, prev_date, history_dates_desc) 或 None（今天不是 FOMC 公告後的播報日）。

    台北時間永遠領先美東 12~13 小時，所以「台北今天」對應的是「美東昨天（含昨晚）」。
    cron 排在台北 10:00 跑，對應美東前一天約 21~22 點，公告與逐字稿都已發布完畢。
    """
    taipei_today = check_date or datetime.date.today()
    et_date = taipei_today - datetime.timedelta(days=1)
    et_str = et_date.strftime("%Y%m%d")
    if et_str not in FOMC_MEETING_DATES:
        return None
    idx = FOMC_MEETING_DATES.index(et_str)
    if idx == 0:
        return None  # 行事曆裡最早一筆，沒有前一次聲明可比較
    curr_date = et_str
    prev_date = FOMC_MEETING_DATES[idx - 1]
    history_dates = list(reversed(FOMC_MEETING_DATES[max(0, idx - 7):idx + 1]))
    return curr_date, prev_date, history_dates


# ── 第一部分：抓取 ─────────────────────────────────────

def fetch_fomc_statement(date_str):
    url = f"https://www.federalreserve.gov/newsevents/pressreleases/monetary{date_str}a.htm"
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    article = soup.find("div", id="article") or soup.find("div", class_="col-xs-12 col-sm-8 col-md-8")
    text = article.get_text("\n", strip=True) if article else soup.get_text("\n", strip=True)
    start = text.find("For release at")
    if start != -1:
        text = text[start:]
    return text, len(text)


def fetch_press_conf_transcript(date_str):
    import pdfplumber

    url = f"https://www.federalreserve.gov/mediacenter/files/FOMCpresconf{date_str}.pdf"
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()

    full_text = ""
    with pdfplumber.open(BytesIO(resp.content)) as pdf:
        for page in pdf.pages:
            full_text += (page.extract_text() or "") + "\n"

    total_chars = len(full_text)

    start_match = re.search(r"CHAIR POWELL\.", full_text)
    if not start_match:
        return full_text, total_chars, total_chars

    rest = full_text[start_match.end():]
    end_match = re.search(r"\n[A-Z][A-Z .]+\.\s", rest)
    opening = rest[:end_match.start()] if end_match else rest
    opening = "CHAIR POWELL. " + opening.strip()
    return opening, total_chars, len(opening)


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


def build_fomc_prompt(prev_date, prev_text, curr_date, curr_text):
    return f"""你是 Fed 政策分析師。請比較以下兩份 FOMC 聲明，只回傳 JSON，不加任何說明或 markdown。

【上次聲明 {prev_date}】
{prev_text}

【本次聲明 {curr_date}】
{curr_text}

輸出 JSON：
{{
  "rate_decision": "維持/升X碼/降X碼",
  "rate_after": "X.XX%",
  "vote_result": "贊成X票:反對X票",
  "guidance_tone": "hawkish/neutral/dovish",
  "hawkish_score": 1到10整數,
  "key_changes": ["措辭異動1", "措辭異動2"],
  "inflation_language": "一句話",
  "employment_language": "一句話",
  "next_meeting_hint": "一句話",
  "holding_impact": {{
    "TQQQ": "利多/利空/中性，一句話",
    "TSM": "利多/利空/中性，一句話",
    "NVDA": "利多/利空/中性，一句話",
    "ALM": "利多/利空/中性，一句話"
  }}
}}"""


def build_presconf_prompt(date, opening_text):
    return f"""你是 Fed 政策分析師。以下是 Powell 記者會開場聲明節錄，只回傳 JSON，不加任何說明或 markdown。

【日期】{date}
【內容】
{opening_text}

輸出 JSON：
{{
  "tone": "hawkish/neutral/dovish",
  "hawkish_score": 1到10整數,
  "economic_assessment": "一句話",
  "inflation_view": "一句話",
  "labor_view": "一句話",
  "policy_bias": "偏升息/偏降息/data-dependent/無明顯偏向",
  "key_quotes": ["重要措辭1", "重要措辭2"],
  "market_implication": "一句話"
}}"""


def build_trend_prompt(history_rows):
    compact = [
        {
            "date": r["curr_date"],
            "rate_decision": r["rate_decision"],
            "rate_after": r["rate_after"],
            "guidance_tone": r["guidance_tone"],
            "hawkish_score": r["hawkish_score"],
            "key_changes": json.loads(r["key_changes_json"] or "[]"),
        }
        for r in history_rows
    ]
    return f"""你是 Fed 政策分析師。以下是近 {len(compact)} 次 FOMC 會議聲明的客觀解析記錄，按時間排序（由舊到新），只回傳 JSON，不加任何說明或 markdown。

{json.dumps(compact, ensure_ascii=False, indent=2)}

輸出 JSON：
{{
  "trend_direction": "持續鷹派/逐步轉鷹/逐步轉鴿/持續鴿派/震盪不定",
  "trend_summary": "一句話總結這幾次會議立場演變",
  "turning_points": ["YYYY-MM-DD: 一句話描述轉折"],
  "current_vs_average": "本次鷹派分數相對於歷史平均的位置，一句話",
  "outlook": "基於趨勢的展望，一句話"
}}"""


# ── 第二.五部分：FOMC 歷史回填（寫 screener_meta.db） ──────

def ensure_fomc_history_table(con):
    con.execute("""
        CREATE TABLE IF NOT EXISTS fed_fomc_history (
            curr_date TEXT PRIMARY KEY,
            prev_date TEXT,
            rate_decision TEXT,
            rate_after TEXT,
            vote_result TEXT,
            guidance_tone TEXT,
            hawkish_score INTEGER,
            key_changes_json TEXT,
            inflation_language TEXT,
            employment_language TEXT,
            next_meeting_hint TEXT,
            updated_at TEXT
        )
    """)
    existing_cols = {row[1] for row in con.execute("PRAGMA table_info(fed_fomc_history)")}
    for col in ("trend_direction", "trend_summary", "trend_outlook", "trend_updated_at"):
        if col not in existing_cols:
            con.execute(f"ALTER TABLE fed_fomc_history ADD COLUMN {col} TEXT")


def upsert_fomc_history(con, curr_date, prev_date, parsed):
    con.execute("""
        INSERT INTO fed_fomc_history
            (curr_date, prev_date, rate_decision, rate_after, vote_result,
             guidance_tone, hawkish_score, key_changes_json,
             inflation_language, employment_language, next_meeting_hint, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(curr_date) DO UPDATE SET
            prev_date=excluded.prev_date,
            rate_decision=excluded.rate_decision,
            rate_after=excluded.rate_after,
            vote_result=excluded.vote_result,
            guidance_tone=excluded.guidance_tone,
            hawkish_score=excluded.hawkish_score,
            key_changes_json=excluded.key_changes_json,
            inflation_language=excluded.inflation_language,
            employment_language=excluded.employment_language,
            next_meeting_hint=excluded.next_meeting_hint,
            updated_at=excluded.updated_at
    """, (
        curr_date, prev_date,
        parsed.get("rate_decision"), parsed.get("rate_after"), parsed.get("vote_result"),
        parsed.get("guidance_tone"), parsed.get("hawkish_score"),
        json.dumps(parsed.get("key_changes", []), ensure_ascii=False),
        parsed.get("inflation_language"), parsed.get("employment_language"),
        parsed.get("next_meeting_hint"), datetime.datetime.now().isoformat(),
    ))
    con.commit()


def backfill_fomc_history(con, history_dates, statement_cache):
    """回填 history_dates（新到舊）裡尚未存在的相鄰會議對，已存在的歷史資料不重算。"""
    pairs = list(zip(history_dates[:-1], history_dates[1:]))  # (curr, prev)
    existing = {row[0] for row in con.execute("SELECT curr_date FROM fed_fomc_history")}
    backfilled = 0
    for curr_date, prev_date in pairs:
        if curr_date in existing:
            continue
        if curr_date not in statement_cache:
            statement_cache[curr_date] = fetch_fomc_statement(curr_date)
        if prev_date not in statement_cache:
            statement_cache[prev_date] = fetch_fomc_statement(prev_date)
        curr_text, _ = statement_cache[curr_date]
        prev_text, _ = statement_cache[prev_date]
        parsed, status, _raw, elapsed = call_ollama(build_fomc_prompt(prev_date, prev_text, curr_date, curr_text))
        print(f"  回填 {curr_date} vs {prev_date}：{status}，耗時 {elapsed:.1f}s", file=sys.stderr)
        if status == "ok":
            upsert_fomc_history(con, curr_date, prev_date, parsed)
            backfilled += 1
    return backfilled


def save_trend_to_history(con, curr_date, trend_parsed):
    """把趨勢整合分析寫回最新一筆 fed_fomc_history（供 portfolio-pwa 讀取顯示）。"""
    con.execute("""
        UPDATE fed_fomc_history
        SET trend_direction=?, trend_summary=?, trend_outlook=?, trend_updated_at=?
        WHERE curr_date=?
    """, (
        trend_parsed.get("trend_direction"), trend_parsed.get("trend_summary"),
        trend_parsed.get("outlook"), datetime.datetime.now().isoformat(), curr_date,
    ))
    con.commit()


# ── 第三部分：組訊息 ─────────────────────────────────

def bullets(items):
    return "\n".join(f"• {x}" for x in items) if items else "（無）"


def build_telegram_message(today_date, curr_date, prev_date, fomc, presconf, trend,
                            presconf_available, timings, char_counts):
    f = fomc or {}
    p = presconf or {}
    t = trend or {}

    presconf_section = f"""━━━ 記者會 opening ({curr_date}) ━━━
立場：{p.get('tone', 'N/A')}（鷹派分數 {p.get('hawkish_score', 'N/A')}/10）
政策偏向：{p.get('policy_bias', 'N/A')}
經濟評估：{p.get('economic_assessment', 'N/A')}
通膨：{p.get('inflation_view', 'N/A')}
就業：{p.get('labor_view', 'N/A')}
市場含義：{p.get('market_implication', 'N/A')}
關鍵措辭：
{bullets(p.get('key_quotes', []))}""" if presconf_available else f"""━━━ 記者會 opening ({curr_date}) ━━━
（逐字稿尚未發布，略過此段）"""

    msg = f"""🏦 FOMC 解讀 {today_date}

━━━ FOMC 聲明 ({curr_date} vs {prev_date}) ━━━
利率決策：{f.get('rate_decision', 'N/A')}（{f.get('rate_after', 'N/A')}）
票數：{f.get('vote_result', 'N/A')}
立場：{f.get('guidance_tone', 'N/A')}（鷹派分數 {f.get('hawkish_score', 'N/A')}/10）

主要措辭異動：
{bullets(f.get('key_changes', []))}

通膨：{f.get('inflation_language', 'N/A')}
就業：{f.get('employment_language', 'N/A')}
下次會議：{f.get('next_meeting_hint', 'N/A')}

{presconf_section}

━━━ FOMC 歷史趨勢整合（近 {char_counts['history_size']} 次） ━━━
方向：{t.get('trend_direction', 'N/A')}
{t.get('trend_summary', 'N/A')}
轉折點：
{bullets(t.get('turning_points', []))}
現況對比：{t.get('current_vs_average', 'N/A')}
展望：{t.get('outlook', 'N/A')}

⏱ 耗時：FOMC {timings['fomc']:.1f}s｜逐字稿 {timings['presconf']:.1f}s｜趨勢 {timings['trend']:.1f}s
📊 文本量：聲明 {char_counts['fomc']}字｜逐字稿截段 {char_counts['presconf_truncated']}/{char_counts['presconf_total']}字"""
    return msg


# ── 主流程 ─────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--date", help="覆蓋台北「今天」日期（YYYY-MM-DD），用於測試非會議日跳過或重跑特定會議日")
    return p.parse_args()


def main():
    args = parse_args()
    check_date = datetime.date.fromisoformat(args.date) if args.date else None

    pair = get_active_fomc_dates(check_date)
    today_date = (check_date or datetime.date.today()).isoformat()
    if pair is None:
        print(f"[skip] {today_date}（台北）不是 FOMC 公告後的播報日，結束。", file=sys.stderr)
        return
    curr_date, prev_date, history_dates = pair
    print(f"[FOMC Watch] 偵測到 {curr_date} 為待播報的 FOMC 公告日（台北今天 {today_date}）", file=sys.stderr)

    results = {}

    print("[1/6] 抓取 FOMC 聲明...", file=sys.stderr)
    try:
        prev_text, prev_chars = fetch_fomc_statement(prev_date)
        curr_text, curr_chars = fetch_fomc_statement(curr_date)
    except Exception as e:
        print(f"[error] FOMC 聲明抓取失敗，可能尚未發布，結束：{e}", file=sys.stderr)
        return
    print(f"  OK：上次 {prev_chars} 字，本次 {curr_chars} 字", file=sys.stderr)
    results["fomc_raw"] = {"prev_date": prev_date, "prev_chars": prev_chars,
                            "curr_date": curr_date, "curr_chars": curr_chars}

    print("[2/6] 抓取記者會逐字稿...", file=sys.stderr)
    try:
        opening_text, presconf_total, presconf_truncated = fetch_press_conf_transcript(curr_date)
        presconf_available = True
    except Exception as e:
        print(f"  [warn] 記者會逐字稿抓取失敗，略過此段：{e}", file=sys.stderr)
        opening_text, presconf_total, presconf_truncated = "", 0, 0
        presconf_available = False
    results["presconf_raw"] = {"available": presconf_available,
                                "total_chars": presconf_total, "truncated_chars": presconf_truncated}

    print("[3/6] 27B 解析 FOMC 聲明...", file=sys.stderr)
    fomc_parsed, fomc_status, fomc_raw, fomc_elapsed = call_ollama(
        build_fomc_prompt(prev_date, prev_text, curr_date, curr_text))
    print(f"  {fomc_status}，耗時 {fomc_elapsed:.1f}s", file=sys.stderr)
    results["fomc_parsed"] = fomc_parsed
    results["fomc_status"] = fomc_status

    print("[4/6] 27B 解析記者會 opening...", file=sys.stderr)
    if presconf_available:
        presconf_parsed, presconf_status, presconf_raw, presconf_elapsed = call_ollama(
            build_presconf_prompt(curr_date, opening_text))
        print(f"  {presconf_status}，耗時 {presconf_elapsed:.1f}s", file=sys.stderr)
    else:
        presconf_parsed, presconf_status, presconf_raw, presconf_elapsed = None, "skipped", "", 0.0
        print("  skipped（逐字稿未取得）", file=sys.stderr)
    results["presconf_parsed"] = presconf_parsed
    results["presconf_status"] = presconf_status

    print("[5/6] 回填 FOMC 歷史趨勢資料...", file=sys.stderr)
    con = sqlite3.connect(DB_PATH)
    ensure_fomc_history_table(con)
    statement_cache = {curr_date: (curr_text, curr_chars), prev_date: (prev_text, prev_chars)}
    if fomc_status == "ok":
        upsert_fomc_history(con, curr_date, prev_date, fomc_parsed)
    n_backfilled = backfill_fomc_history(con, history_dates, statement_cache)
    print(f"  OK：新回填 {n_backfilled} 筆", file=sys.stderr)

    cur = con.execute("SELECT * FROM fed_fomc_history ORDER BY curr_date ASC")
    cols = [d[0] for d in cur.description]
    history_rows = [dict(zip(cols, row)) for row in cur.fetchall()]

    print("[6/6] 27B 趨勢整合分析...", file=sys.stderr)
    if len(history_rows) >= 3:
        trend_parsed, trend_status, trend_raw, trend_elapsed = call_ollama(build_trend_prompt(history_rows))
    else:
        trend_parsed, trend_status, trend_raw, trend_elapsed = None, "skipped", "", 0.0
    print(f"  {trend_status}，耗時 {trend_elapsed:.1f}s（歷史樣本 {len(history_rows)} 筆）", file=sys.stderr)
    if trend_status == "ok":
        save_trend_to_history(con, curr_date, trend_parsed)
    con.close()
    results["trend_parsed"] = trend_parsed
    results["trend_status"] = trend_status
    results["history_sample_size"] = len(history_rows)

    with open(RESULTS_PATH, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"完整結果已存至 {RESULTS_PATH}", file=sys.stderr)

    msg = build_telegram_message(
        today_date, curr_date, prev_date, fomc_parsed, presconf_parsed, trend_parsed,
        presconf_available,
        timings={"fomc": fomc_elapsed, "presconf": presconf_elapsed, "trend": trend_elapsed},
        char_counts={"fomc": curr_chars, "presconf_truncated": presconf_truncated,
                     "presconf_total": presconf_total, "history_size": len(history_rows)},
    )
    print("\n" + msg + "\n", file=sys.stderr)

    if BOT_TOKEN and CHAT_ID:
        send_telegram(msg)
        print("Telegram 推播完成", file=sys.stderr)
    else:
        print("[warn] 未設定 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID，略過推播", file=sys.stderr)


if __name__ == "__main__":
    main()
