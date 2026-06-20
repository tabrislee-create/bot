import datetime
import zoneinfo

import requests

try:
    import pytz
    ET = pytz.timezone('America/New_York')
    def now_et() -> datetime.datetime:
        return datetime.datetime.now(ET)
except ImportError:
    from datetime import timezone, timedelta
    # pytz 不可用時 fallback：固定 UTC-4（EDT）
    ET = timezone(timedelta(hours=-4))
    def now_et() -> datetime.datetime:
        return datetime.datetime.now(ET)

def now_et_str(fmt: str = "%m/%d %H:%M ET") -> str:
    return now_et().strftime(fmt)

def today_et_str() -> str:
    return now_et().strftime("%Y-%m-%d")

def today_et_fmt(fmt: str = "%m/%d") -> str:
    return now_et().strftime(fmt)

TAIPEI_TZ = zoneinfo.ZoneInfo("Asia/Taipei")


def now_taipei() -> datetime.datetime:
    return datetime.datetime.now(TAIPEI_TZ)


def today_taipei_str() -> str:
    return now_taipei().strftime("%Y-%m-%d")


def now_taipei_str(fmt: str = "%m/%d %H:%M") -> str:
    return now_taipei().strftime(fmt)


# 美股假日（NYSE 公告，涵蓋 2026-2028，下次更新待 NYSE 公告 2029 後）
US_HOLIDAY_NAMES = {
    # 2026
    datetime.date(2026, 1, 1): "元旦",
    datetime.date(2026, 1, 19): "馬丁路德金紀念日",
    datetime.date(2026, 2, 16): "總統日",
    datetime.date(2026, 4, 3): "耶穌受難日",
    datetime.date(2026, 5, 25): "國殤紀念日",
    datetime.date(2026, 6, 19): "六月節（解放紀念日）",
    datetime.date(2026, 7, 3): "獨立紀念日（觀察日）",
    datetime.date(2026, 9, 7): "勞動節",
    datetime.date(2026, 11, 26): "感恩節",
    datetime.date(2026, 12, 25): "聖誕節",
    # 2027
    datetime.date(2027, 1, 1): "元旦",
    datetime.date(2027, 1, 18): "馬丁路德金紀念日",
    datetime.date(2027, 2, 15): "總統日",
    datetime.date(2027, 3, 26): "耶穌受難日",
    datetime.date(2027, 5, 31): "國殤紀念日",
    datetime.date(2027, 6, 18): "六月節（觀察日）",
    datetime.date(2027, 7, 5): "獨立紀念日（觀察日）",
    datetime.date(2027, 9, 6): "勞動節",
    datetime.date(2027, 11, 25): "感恩節",
    datetime.date(2027, 12, 24): "聖誕節（觀察日）",
    # 2028（2028-01-01 元旦為週六，不補假）
    datetime.date(2028, 1, 17): "馬丁路德金紀念日",
    datetime.date(2028, 2, 21): "總統日",
    datetime.date(2028, 4, 14): "耶穌受難日",
    datetime.date(2028, 5, 29): "國殤紀念日",
    datetime.date(2028, 6, 19): "六月節（解放紀念日）",
    datetime.date(2028, 7, 4): "獨立紀念日",
    datetime.date(2028, 9, 4): "勞動節",
    datetime.date(2028, 11, 23): "感恩節",
    datetime.date(2028, 12, 25): "聖誕節",
}

US_HOLIDAYS = set(US_HOLIDAY_NAMES.keys())


def is_us_trading_day(target_date: datetime.date) -> bool:
    if target_date.weekday() >= 5:
        return False
    return target_date not in US_HOLIDAYS


def last_us_market_close_date() -> datetime.date:
    """回傳最近一個已完成的美股收盤交易日。"""
    now = now_et()
    today = now.date()
    if is_us_trading_day(today) and now.hour >= 16:
        return today

    d = today - datetime.timedelta(days=1)
    while not is_us_trading_day(d):
        d -= datetime.timedelta(days=1)
    return d


OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
OLLAMA_MODEL = "frob/qwen3.5-instruct:9b"


def ollama_generate(prompt, *, num_ctx=2048, temperature=0.3, timeout=300):
    resp = requests.post(OLLAMA_URL, json={
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {"num_ctx": num_ctx, "temperature": temperature},
    }, timeout=timeout)
    resp.raise_for_status()
    return resp.json().get("response", "").strip()


def translate_zh(text):
    """將英文文字翻譯成繁體中文，只回傳翻譯結果。"""
    prompt = f"將以下英文翻譯成繁體中文，只輸出翻譯結果，不要加任何說明：\n\n{text}"
    return ollama_generate(prompt, num_ctx=1024, temperature=0)


def summarize_trump_news(titles, date_str):
    """兩段式摘要：先英文整理，再翻譯成繁體中文。"""
    if not titles:
        return "今日無川普相關新聞。"

    joined = "\n".join(titles)
    en_prompt = (
        f"Below are news headlines about Trump from {date_str}:\n\n{joined}\n\n"
        f"Write a structured summary in ENGLISH. Merge duplicate stories across sources. "
        f"Use this category order:\n"
        f"1. International Politics (Iran, China, Russia, Israel, diplomacy, military, wars)\n"
        f"2. Domestic Politics (Republicans, Democrats, Congress, judiciary, elections)\n"
        f"3. Finance & Investment (markets, tariffs, economic policy)\n"
        f"4. Other\n\n"
        f"Format (strict):\n"
        f"[Category Name]\n"
        f"• One distinct event per bullet. 2-3 complete sentences per bullet.\n"
        f"• Blank line between categories.\n\n"
        f"Rules:\n"
        f"- Only use information from the headlines/descriptions above. Do not invent details.\n"
        f"- Preserve full meaning: who did what, to whom, and the stated outcome or reaction.\n"
        f"- Skip any category with no relevant headlines; do not output the category heading.\n"
        f"- Do not write meta commentary like 'no items in this category'.\n"
        f"- Do not merge unrelated events into one long paragraph.\n"
        f"- In war/military contexts, write 'military strikes' or 'airstrikes', not bare 'strikes'."
    )
    en_summary = ollama_generate(en_prompt, temperature=0.3)

    zh_prompt = (
        f"Translate the following English news summary into Traditional Chinese (繁體中文).\n\n"
        f"Keep the same structure: one bullet per event, full semantic fidelity. Rules:\n"
        f"- Faithful translation only — do not add, remove, shorten, or change facts.\n"
        f"- Keep complete meaning in every bullet; do not compress multiple facts into vague phrases.\n"
        f"- Skip empty categories exactly as in the source.\n"
        f"- 2-3 complete sentences per bullet.\n"
        f"- Format (strict):\n"
        f"  【國際政治】\n"
        f"  • …\n"
        f"  • …\n"
        f"  （分類之間空一行）\n"
        f"- Disambiguation:\n"
        f"  - military strikes / airstrikes / attacks → 「軍事打擊」「空襲」或「襲擊」，不可譯為「罷工」\n"
        f"  - labor strike / union strike → 「罷工」\n"
        f"- Category headings:\n"
        f"  International Politics → 【國際政治】\n"
        f"  Domestic Politics → 【國內政治】\n"
        f"  Finance & Investment → 【財金投資】\n"
        f"  Other → 【其他】\n\n"
        f"English summary:\n{en_summary}"
    )
    return ollama_generate(zh_prompt, temperature=0.2)
