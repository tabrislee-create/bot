#!/usr/bin/env python3
import os
import sys
import re
import requests

BASE_URL = "https://truthsocial.com"
ACCT = "realDonaldTrump"
LAST_ID_FILE = os.path.expanduser("~/.hermes/trump_last_id.txt")
OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
OLLAMA_MODEL = "sorc/qwen3.5-instruct-uncensored:4b"

TUNGSTEN_KEYWORDS = [
    "tungsten ban", "tungsten NDAA", "Section 4872",
    "critical minerals China tungsten", "鎢禁令",
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}

def is_meaningful(text: str) -> bool:
    if not text:
        return False
    t = text.strip()
    if len(t) < 15:
        return False
    # skip pure link / quote posts
    if t.lower().startswith(("http://", "https://")):
        return False
    # skip very short or boilerplate
    if re.match(r"^https?://\S+$", t):
        return False
    return True

def get_account_id():
    url = f"{BASE_URL}/api/v1/accounts/lookup"
    params = {"acct": ACCT}
    resp = requests.get(url, params=params, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    ctype = resp.headers.get("content-type", "")
    body = resp.text or ""
    if "html" in ctype or body.strip().lower().startswith(("<!doctype", "<html")) or "cloudflare" in body.lower():
        raise RuntimeError("Cloudflare or non-JSON response on account lookup")
    data = resp.json()
    if isinstance(data, dict) and "id" in data:
        return data["id"]
    if isinstance(data, list) and len(data) > 0:
        return data[0]["id"]
    raise ValueError("Could not fetch account ID")

def get_latest_status(account_id):
    url = f"{BASE_URL}/api/v1/accounts/{account_id}/statuses"
    params = {"limit": 1, "exclude_replies": True}
    resp = requests.get(url, params=params, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    ctype = resp.headers.get("content-type", "")
    body = resp.text or ""
    if "html" in ctype or body.strip().lower().startswith(("<!doctype", "<html")) or "cloudflare" in body.lower():
        raise RuntimeError("Cloudflare or non-JSON response on statuses")
    data = resp.json()
    if isinstance(data, list) and len(data) > 0:
        return data[0]
    raise ValueError("No statuses found")

def summarize_and_translate(text: str):
    """Ask local Ollama for brief EN summary + full ZH translation in the target format."""
    prompt = (
        "以下是 Donald Trump 在 Truth Social 發表的貼文：\n\n"
        f"{text}\n\n"
        "請完成：\n"
        "1. 用 1-2 句英文寫出關鍵事實摘要 (English Summary)。\n"
        "2. 將整篇貼文完整、忠實地翻譯為繁體中文 (Traditional Chinese Translation)。\n"
        "規則：\n"
        "- 保留人名「Trump」為英文，不要譯成「川普」或「特朗普」。\n"
        "- 其他內容一律繁體中文。\n"
        "- 摘要保持簡潔客觀。\n\n"
        "只輸出以下兩段，不要加任何前言或結尾：\n"
        "原文摘要 (English Summary): \n[英文摘要]\n\n"
        "繁體中文翻譯 (Traditional Chinese Translation): \n[完整繁體中文]"
    )
    payload = {
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {"num_ctx": 4096, "temperature": 0.2}
    }
    resp = requests.post(OLLAMA_URL, json=payload, timeout=90)
    resp.raise_for_status()
    out = resp.json().get("response", "").strip()
    # Fallback parse if model didn't follow format exactly
    summary = ""
    translation = out
    m = re.search(r"原文摘要 \(English Summary\):\s*(.*?)(?:\n\s*\n|繁體中文翻譯)", out, re.S | re.I)
    if m:
        summary = m.group(1).strip()
    m2 = re.search(r"繁體中文翻譯 \(Traditional Chinese Translation\):\s*(.*)", out, re.S | re.I)
    if m2:
        translation = m2.group(1).strip()
    if not summary:
        # derive a simple summary: first sentence-ish
        first = re.split(r"[.!?。！？]\s*", text.strip())[0][:200]
        summary = (first + ("..." if len(first) < len(text) else "")).strip()
    if not translation or translation == out:
        # last resort: use a plain translate
        translation = text  # will be overwritten below if we fall through, but caller will handle
    return summary, translation

def fetch_and_format_report():
    """Return (message, new_status_id) or (None, None) if no new meaningful post."""
    acct_id = get_account_id()
    status = get_latest_status(acct_id)
    status_id = str(status["id"])
    content = status.get("content", "") or status.get("text", "") or ""
    text = re.sub(r"<[^>]+?>", "", content).strip()
    url = f"{BASE_URL}/@{ACCT}/status/{status_id}"

    if not is_meaningful(text):
        return None, None

    last_id = ""
    if os.path.exists(LAST_ID_FILE):
        with open(LAST_ID_FILE, "r") as f:
            last_id = f.read().strip()

    if status_id == last_id:
        return None, None

    # New meaningful post
    try:
        summary, translation = summarize_and_translate(text)
    except Exception:
        # fallback: use full text as summary, and attempt simple translate
        summary = text[:500] + ("..." if len(text) > 500 else "")
        try:
            payload = {
                "model": OLLAMA_MODEL,
                "prompt": f"將以下英文翻譯成繁體中文，只輸出翻譯結果：\n\n{text}",
                "stream": False,
                "options": {"num_ctx": 2048}
            }
            r = requests.post(OLLAMA_URL, json=payload, timeout=60)
            translation = r.json().get("response", text).strip()
        except Exception:
            translation = text

    tungsten_tag = ""
    text_lower = text.lower()
    if any(kw.lower() in text_lower for kw in TUNGSTEN_KEYWORDS):
        tungsten_tag = " 🪨[鎢/關鍵礦產]"

    message = (
        f"🚨 Trump新貼文（Truth Social）{tungsten_tag}  \n"
        "原文摘要 (English Summary):  \n"
        f"{summary}\n\n"
        "繁體中文翻譯 (Traditional Chinese Translation):  \n"
        f"{translation}\n\n"
        f"🔗 {url}"
    )
    return message, status_id

def main():
    try:
        message, new_id = fetch_and_format_report()
        if message and new_id:
            print(message)
            with open(LAST_ID_FILE, "w") as f:
                f.write(new_id)
        else:
            print("[SILENT]")
    except Exception as e:
        # Silent on transient errors (CF, network, rate limit, no text etc). Do not spam.
        print(f"[trump_watch] transient: {e}", file=sys.stderr)
        print("[SILENT]")

if __name__ == "__main__":
    main()
