#!/usr/bin/env python3
"""investment-bot — 投資專用 Telegram Bot"""
import os
import sqlite3
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from dotenv import dotenv_values
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)
ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
env = dotenv_values(ENV_PATH)
BOT_TOKEN    = env.get("TELEGRAM_BOT_TOKEN", "")
ALLOWED_USER = int(env.get("TELEGRAM_ALLOWED_USERS", "0"))
DB_PATH      = os.path.expanduser("~/ft_trades.db")
ET = ZoneInfo("America/New_York")

if not BOT_TOKEN:
    raise ValueError("找不到 TELEGRAM_BOT_TOKEN")

def is_allowed(update: Update) -> bool:
    return update.effective_user.id == ALLOWED_USER

def get_conn():
    return sqlite3.connect(DB_PATH)

def now_et() -> datetime:
    """回傳美東當下時間"""
    return datetime.now(ET)

def now_et_str() -> str:
    """回傳格式化美東時間字串，例：2026-06-05 09:32 ET"""
    return now_et().strftime("%Y-%m-%d %H:%M ET")

def get_market_snapshot():
    """抓 QQQ / SOXX / VIX 即時狀態"""
    import yfinance as yf
    result = {}
    for sym in ["QQQ", "SOXX", "^VIX"]:
        try:
            p = yf.Ticker(sym).fast_info
            last = float(p["last_price"])
            prev = float(p["previous_close"])
            chg = (last / prev - 1) * 100
            result[sym] = (last, chg)
        except Exception:
            result[sym] = (0, 0)
    qqq  = f"QQQ ${result['QQQ'][0]:.1f}({result['QQQ'][1]:+.1f}%)"
    soxx = f"SOXX ${result['SOXX'][0]:.1f}({result['SOXX'][1]:+.1f}%)"
    vix_val = result['^VIX'][0]
    vix_chg = result['^VIX'][1]
    vix  = f"VIX {vix_val:.1f}({vix_chg:+.1f}%)"
    return f"【大盤】{qqq} | {soxx} | {vix}"

def format_entry(row) -> str:
    id_, sym, direction, reason, cur_qty, tgt_qty, delta, price, dv, executed, ts = row
    done_tag = " ✅" if executed else ""
    if delta > 0:
        action = f"買入 +{delta} 股（約 ${dv:,.0f}）→ 目標 {tgt_qty} 股"
    elif delta < 0:
        action = f"賣出 {delta} 股（約 ${abs(dv):,.0f}）→ 剩 {tgt_qty} 股"
    else:
        action = "已達目標，無需調整"
    return f"[{id_}] {sym}{done_tag} ${price:.2f}\n  {reason}\n  💡 {action}"

# /help
async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    await update.message.reply_text(
        "📊 投資助理指令\n\n"
        "/signals — 最近一次加減倉建議（每股只顯示最新一筆）\n"
        "/log NVDA — 某股最近30天訊號歷史\n"
        "/done 123 — 標記決策 id=123 已執行\n"
        "/screen — 今日順風車 Top 10（可貼給 Claude）\n"
        "/live AAOI GLW — 個股技術面+持倉分析（含大盤）\n"
        "/pending — 未執行的建議清單\n"
        "/sync_ft — 同步 Firstrade 最新交易\n"
        "/analyze AAOI GLW — 技術面+持倉損益分析\n"
        "📎 傳 CSV — 嘉信 Transactions/Positions 或 Firstrade 交易紀錄 CSV 直接傳此對話自動匯入\n"
        "📋 貼 JSON — 貼 GEM 總經事件 JSON 自動更新 macro_events.json\n"
        "/help — 顯示此說明"
    )

# /signals
async def cmd_signals(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    conn = get_conn()
    try:
        last = conn.execute(
            "SELECT date(ts) FROM decision_log ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        if not last:
            await update.message.reply_text("📭 decision_log 尚無資料")
            return
        last_date = last[0]
        rows = conn.execute("""
            SELECT id, symbol, direction, reason, current_qty, target_qty,
                   delta_qty, price, delta_value, executed, ts
            FROM decision_log
            WHERE id IN (
                SELECT MAX(id) FROM decision_log
                WHERE date(ts) = ?
                GROUP BY symbol
            )
            ORDER BY direction, symbol
        """, (last_date,)).fetchall()
    finally:
        conn.close()
    reduce_lines, add_lines = [], []
    for row in rows:
        entry = format_entry(row)
        if row[2] == "REDUCE":
            reduce_lines.append(entry)
        else:
            add_lines.append(entry)
    parts = [f"🔍 持倉訊號 {last_date}（查詢：{now_et_str()}）"]
    if reduce_lines:
        parts.append(f"\n🔴 減倉/停損（{len(reduce_lines)} 筆）")
        parts.extend(reduce_lines)
    if add_lines:
        parts.append(f"\n🟢 可考慮加倉（{len(add_lines)} 筆）")
        parts.extend(add_lines)
    if not reduce_lines and not add_lines:
        parts.append("今日無訊號")
    parts.append("\n用 /done <id> 標記已執行")
    msg = "\n\n".join(parts)
    for i in range(0, len(msg), 4000):
        await update.message.reply_text(msg[i:i+4000])

# /log
async def cmd_log(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    if not ctx.args:
        await update.message.reply_text("用法：/log NVDA")
        return
    symbol = ctx.args[0].upper()
    since  = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    conn = get_conn()
    try:
        rows = conn.execute("""
            SELECT id, direction, reason, delta_qty, price, delta_value, executed, ts
            FROM decision_log
            WHERE symbol = ? AND date(ts) >= ?
            ORDER BY ts DESC
        """, (symbol, since)).fetchall()
    finally:
        conn.close()
    if not rows:
        await update.message.reply_text(f"📭 {symbol} 近30天無訊號記錄")
        return
    lines = [f"📋 {symbol} 近30天訊號（{len(rows)} 筆）"]
    for id_, direction, reason, delta, price, dv, executed, ts in rows:
        dt   = ts[:10]
        icon = "🔴" if direction == "REDUCE" else "🟢"
        done = " ✅" if executed else ""
        if delta > 0:
            act = f"+{delta}股 ${dv:,.0f}"
        elif delta < 0:
            act = f"{delta}股 ${abs(dv):,.0f}"
        else:
            act = "無需調整"
        lines.append(f"{icon} [{id_}] {dt}{done}  {act}\n  {reason}")
    msg = "\n\n".join(lines)
    for i in range(0, len(msg), 4000):
        await update.message.reply_text(msg[i:i+4000])

# /done
async def cmd_done(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    if not ctx.args or not ctx.args[0].isdigit():
        await update.message.reply_text("用法：/done 123")
        return
    log_id = int(ctx.args[0])
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT symbol, direction, delta_qty, price FROM decision_log WHERE id=?",
            (log_id,)
        ).fetchone()
        if not row:
            await update.message.reply_text(f"找不到 id={log_id}")
            return
        conn.execute("UPDATE decision_log SET executed=1 WHERE id=?", (log_id,))
        conn.commit()
        sym, direction, delta, price = row
        icon = "🔴" if direction == "REDUCE" else "🟢"
        await update.message.reply_text(
            f"✅ 已標記執行\n{icon} [{log_id}] {sym}  delta={delta:+d}  ${price:.2f}"
        )
    finally:
        conn.close()

# /screen
async def cmd_screen(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    import json
    try:
        d = json.load(open("/home/tabris/.hermes/scripts/screen_result.json"))
    except Exception as e:
        await update.message.reply_text(f"⚠️ 無法讀取 screen_result.json: {e}")
        return
    top10 = [x for x in d.get("top10", []) if not x.get("is_etf")]
    if not top10:
        await update.message.reply_text("📭 目前無個股 Top 10 資料")
        return
    market = get_market_snapshot()
    await update.message.reply_text("⏳ 抓取現價中...")
    import yfinance as yf
    tickers = [x["ticker"] for x in top10]
    live = {}
    for t in tickers:
        try:
            p = yf.Ticker(t).fast_info["last_price"]
            if p and p > 0:
                live[t] = float(p)
        except Exception:
            pass
    et_now = now_et()
    now_str = et_now.strftime("%Y-%m-%d %H:%M ET")
    lines = [f"【順風車 Top 10】{now_str}（含現價）\n（直接貼給 Claude 分析）\n"]
    for i, x in enumerate(top10, 1):
        ticker = x["ticker"]
        close  = x.get("close", 0)
        now_p  = live.get(ticker)
        ma20   = x.get("ma20", 0)
        ma60   = x.get("ma60", 0)
        high52 = close / (1 + x.get("pct_from_high", -1) / 100) if x.get("pct_from_high") else 0
        if now_p and close > 0:
            chg = (now_p / close - 1) * 100
            chg_str = f"+{chg:.1f}%" if chg >= 0 else f"{chg:.1f}%"
            pct_ma20 = (now_p / ma20 - 1) * 100 if ma20 > 0 else 0
            pct_high_now = (now_p / high52 - 1) * 100 if high52 > 0 else x.get("pct_from_high", 0)
            price_line = (f"昨收:${close:.2f} 現價:${now_p:.2f}({chg_str}) | "
                         f"距MA20:{pct_ma20:+.1f}% 距高:{pct_high_now:.1f}%")
        else:
            price_line = f"昨收:${close:.2f} 現價:取得失敗 | 距高:{x.get('pct_from_high',0):.1f}%"
        warn = ", ".join(x.get("fund_warnings", [])) or "✅"
        lines.append(
            f"{i} {ticker}\n"
            f"  {price_line}\n"
            f"  MA20:${ma20:.2f} MA60:${ma60:.2f} | RSI:{x.get('rsi',0):.1f} 量比:{x.get('vol_ratio',0):.2f}\n"
            f"  {x.get('sector','')} | 基本面:{warn}"
        )
    msg = "\n\n".join(lines)
    for i in range(0, len(msg), 4000):
        await update.message.reply_text(msg[i:i+4000])

# /live
async def cmd_live(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    if not ctx.args:
        await update.message.reply_text("用法：/live AAOI GLW TQQQ")
        return
    tickers = [t.upper() for t in ctx.args]
    await update.message.reply_text(f"⏳ 查詢 {' '.join(tickers)}...")
    import subprocess
    market = get_market_snapshot()
    result = subprocess.run(
        ["/home/tabris/.hermes/hermes-agent/venv/bin/python3",
         "/home/tabris/.hermes/scripts/analyze_positions.py"] + tickers,
        capture_output=True, text=True, timeout=60
    )
    body = result.stdout if result.stdout else result.stderr or "⚠️ 無輸出"
    msg = f"{market}\n\n{body}"
    for i in range(0, len(msg), 4000):
        await update.message.reply_text(msg[i:i+4000])


async def cmd_analyze(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    if not ctx.args:
        await update.message.reply_text("用法：/analyze AAOI GLW TQQQ")
        return
    tickers = [t.upper() for t in ctx.args]
    await update.message.reply_text(f"⏳ 長期分析 {' '.join(tickers)}...")
    import subprocess
    result = subprocess.run(
        ["/home/tabris/.hermes/hermes-agent/venv/bin/python3",
         "/home/tabris/.hermes/scripts/analyze_positions_long.py"] + tickers,
        capture_output=True, text=True, timeout=60
    )
    body = result.stdout if result.stdout else result.stderr or "⚠️ 無輸出"
    for i in range(0, len(body), 4000):
        await update.message.reply_text(body[i:i+4000])

# /sync_ft
async def cmd_sync_ft(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    await update.message.reply_text("⏳ 同步 Firstrade 交易中...")
    import subprocess
    result = subprocess.run(
        ["/home/tabris/.hermes/hermes-agent/venv/bin/python3",
         "/home/tabris/.hermes/scripts/US_trades.py", "--sync"],
        capture_output=True, text=True, timeout=120
    )
    body = result.stdout if result.stdout else result.stderr or "⚠️ 無輸出"
    msg = f"{body.strip()}\n\n同步完成：{now_et_str()}"
    await update.message.reply_text(msg[:4000])

# /pending
async def cmd_pending(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    conn = get_conn()
    try:
        rows = conn.execute("""
            SELECT id, symbol, direction, delta_qty, price, delta_value, ts
            FROM decision_log
            WHERE executed = 0
            ORDER BY ts DESC
            LIMIT 20
        """).fetchall()
    finally:
        conn.close()
    if not rows:
        await update.message.reply_text("✅ 沒有未執行的建議")
        return
    lines = [f"⏳ 未執行建議（{len(rows)} 筆）（查詢：{now_et_str()}）"]
    for id_, sym, direction, delta, price, dv, ts in rows:
        dt   = ts[:10]
        icon = "🔴" if direction == "REDUCE" else "🟢"
        if delta > 0:
            act = f"+{delta}股 ${dv:,.0f}"
        elif delta < 0:
            act = f"{delta}股 ${abs(dv):,.0f}"
        else:
            act = "無需調整"
        lines.append(f"{icon} [{id_}] {dt} {sym}  {act}")
    lines.append("\n用 /done <id> 標記已執行")
    await update.message.reply_text("\n".join(lines))

# /holdings
async def cmd_holdings(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    market = get_market_snapshot()
    conn = get_conn()
    try:
        rows = conn.execute("""
            SELECT symbol, SUM(qty) as qty,
                   SUM(qty * avg_cost) / SUM(qty) as avg_cost,
                   broker
            FROM holdings_snapshot
            WHERE qty > 0
            GROUP BY symbol, broker
            ORDER BY broker, symbol
        """).fetchall()
    finally:
        conn.close()
    if not rows:
        await update.message.reply_text("📭 目前無持倉資料")
        return
    # 從 price_cache.db 取最新收盤日期+收盤價（比 fast_info 更穩定且帶日期）
    price_db = os.path.expanduser("~/price_cache.db")
    symbols = [r[0] for r in rows if r[0] != "CASH.USD"]
    closes = {}  # sym -> (close_price, "MM/DD")
    if symbols:
        try:
            pc = sqlite3.connect(price_db)
            ph = ",".join("?" * len(symbols))
            for ticker, close, date_str in pc.execute(
                f"""SELECT p.ticker, p.close, p.date
                    FROM prices p
                    INNER JOIN (
                        SELECT ticker, MAX(date) AS md FROM prices
                        WHERE ticker IN ({ph}) GROUP BY ticker
                    ) m ON p.ticker = m.ticker AND p.date = m.md
                    WHERE p.ticker IN ({ph})""",
                symbols + symbols,
            ).fetchall():
                if close and close > 0:
                    d = datetime.strptime(date_str, "%Y-%m-%d")
                    closes[ticker] = (float(close), d.strftime("%-m/%-d"))
            pc.close()
        except Exception:
            pass
    lines = [market, f"查詢時間：{now_et_str()}", "", "【目前持倉】（可貼給 Claude）", ""]
    cur_broker = None
    total_mv = 0.0
    for sym, qty, avg_cost, broker in rows:
        if broker != cur_broker:
            cur_broker = broker
            lines.append(f"── {broker.upper()} ──")
        if sym == "CASH.USD":
            lines.append(f"{sym}  ${qty:,.0f}")
            continue
        if sym in closes:
            close_p, date_lbl = closes[sym]
            mv = close_p * qty
            total_mv += mv
            if avg_cost > 0:
                pnl = (close_p / avg_cost - 1) * 100
                pnl_str = f"+{pnl:.1f}%" if pnl >= 0 else f"{pnl:.1f}%"
                lines.append(
                    f"{sym}  {qty:.0f}股  均成本${avg_cost:.2f}"
                    f"  收盤({date_lbl})${close_p:.2f}  損益{pnl_str}  市值${mv:,.0f}"
                )
            else:
                lines.append(
                    f"{sym}  {qty:.0f}股  收盤({date_lbl})${close_p:.2f}  市值${mv:,.0f}"
                )
        else:
            lines.append(f"{sym}  {qty:.0f}股  均成本${avg_cost:.2f}  收盤價取得失敗")
    if total_mv > 0:
        lines.append(f"\n持倉總市值 ${total_mv:,.0f}")
    await update.message.reply_text("\n".join(lines))

# /trades
async def cmd_trades(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    if not ctx.args:
        await update.message.reply_text(
            "用法：\n/trades 2026-06-02\n/trades 2026-06-01~2026-06-02"
        )
        return
    try:
        arg = ctx.args[0]
        if "~" in arg:
            date_from, date_to = arg.split("~", 1)
        else:
            date_from = arg
            date_to   = ctx.args[1] if len(ctx.args) >= 2 else date_from
        date_from = date_from.strip()
        date_to   = date_to.strip()
        datetime.strptime(date_from, "%Y-%m-%d")
        datetime.strptime(date_to,   "%Y-%m-%d")
    except ValueError:
        await update.message.reply_text("⚠️ 日期格式錯誤，請用 YYYY-MM-DD 或 YYYY-MM-DD~YYYY-MM-DD")
        return
    conn = get_conn()
    try:
        rows = conn.execute("""
            SELECT report_date, symbol, action, quantity, price, amount, account
            FROM trades
            WHERE report_date >= ? AND report_date <= ?
            ORDER BY report_date DESC, account, symbol
        """, (date_from, date_to)).fetchall()
    finally:
        conn.close()
    if not rows:
        period = date_from if date_from == date_to else f"{date_from} ~ {date_to}"
        await update.message.reply_text(f"📭 {period} 無交易記錄")
        return
    period = date_from if date_from == date_to else f"{date_from} ~ {date_to}"
    if len(rows) > 50:
        from collections import defaultdict, Counter
        daily = defaultdict(lambda: {"buy": Counter(), "sell": Counter()})
        for date, sym, action, qty, price, amount, account in rows:
            if action.upper() in ("BUY", "BUY TO OPEN"):
                daily[date]["buy"][sym] += int(abs(qty))
            else:
                daily[date]["sell"][sym] += int(abs(qty))
        lines = [f"📋 交易摘要 {period}（{len(rows)} 筆）\n"]
        for date in sorted(daily.keys(), reverse=True):
            buys  = " ".join(f"{s}({n})" for s, n in sorted(daily[date]["buy"].items()))  or "—"
            sells = " ".join(f"{s}({n})" for s, n in sorted(daily[date]["sell"].items())) or "—"
            lines.append(f"📅 {date}\n  🟢 {buys}\n  🔴 {sells}")
        lines.append("\n查單日明細：/trades YYYY-MM-DD")
        await update.message.reply_text("\n\n".join(lines)[:4000])
        return
    lines  = [f"📋 交易記錄 {period}（{len(rows)} 筆）\n"]
    cur_date, cur_acc = None, None
    for date, sym, action, qty, price, amount, account in rows:
        if date != cur_date:
            cur_date = date
            cur_acc  = None
            lines.append(f"\n📅 {date}")
        if account != cur_acc:
            cur_acc = account
            acc_label = "Firstrade" if "91554" in str(account) else "Schwab" if account else "未知"
            lines.append(f"  ── {acc_label} ──")
        icon = "🟢" if action.upper() in ("BUY", "BUY TO OPEN") else "🔴"
        lines.append(f"  {icon} {sym}  {action}  {qty:+.0f}股 @ ${price:.2f}  (${abs(amount):,.0f})")
    msg = "\n".join(lines)
    for i in range(0, len(msg), 4000):
        await update.message.reply_text(msg[i:i+4000])

# Firstrade 中文匯出檔表頭欄位（用內容判斷，因為檔名通常就是 export.csv，不像
# 嘉信檔名固定帶 Transactions/Positions 可以直接從檔名認）
FIRSTRADE_HEADER_COLS = {"日期", "交易類別", "數量", "說明", "代號", "價格", "金額"}

def _sniff_firstrade(path):
    import csv as _csv
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            header = next(_csv.reader(f), None)
    except Exception:
        return False
    if not header:
        return False
    return FIRSTRADE_HEADER_COLS.issubset({c.strip() for c in header})

# 收 CSV 檔案
async def handle_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    doc = update.message.document
    if not doc or not doc.file_name:
        return
    fname = doc.file_name
    if not fname.endswith(".csv"):
        return
    is_transactions = "Transactions" in fname
    is_positions    = "Positions" in fname

    await update.message.reply_text(f"⏳ 收到 {fname}，下載中...")
    import subprocess
    tmp_path = f"/tmp/{fname.replace(' ', '_')}"
    try:
        tg_file = await ctx.bot.get_file(doc.file_id)
        await tg_file.download_to_drive(tmp_path)
    except Exception as e:
        await update.message.reply_text(f"⚠️ 下載失敗：{e}")
        return

    is_firstrade = False
    if not (is_transactions or is_positions):
        is_firstrade = _sniff_firstrade(tmp_path)
        if not is_firstrade:
            await update.message.reply_text(f"⚠️ 不認識的檔案：{fname}\n請傳嘉信 Transactions/Positions CSV，或 Firstrade 交易紀錄 CSV")
            try:
                os.remove(tmp_path)
            except Exception:
                pass
            return

    if is_firstrade:
        script = "/home/tabris/.hermes/scripts/import_firstrade_csv.py"
    else:
        script = (
            "/home/tabris/.hermes/scripts/import_schwab_trades.py"
            if is_transactions else
            "/home/tabris/.hermes/scripts/import_schwab_positions.py"
        )
    await update.message.reply_text(f"⏳ 判斷為 {'Firstrade' if is_firstrade else 'Schwab'} 格式，匯入中...")
    result = subprocess.run(
        ["/home/tabris/.hermes/hermes-agent/venv/bin/python3", script, tmp_path],
        capture_output=True, text=True, timeout=60
    )
    body = result.stdout if result.stdout else result.stderr or "⚠️ 無輸出"
    await update.message.reply_text(body[:4000])
    try:
        os.remove(tmp_path)
    except Exception:
        pass

# 收純文字 macro_events JSON
async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    text = (update.message.text or "").strip()
    if not text.startswith("["):
        return
    import json
    try:
        events = json.loads(text)
    except Exception:
        return
    if not isinstance(events, list) or not events:
        return
    if not all(isinstance(e, dict) and "date" in e and "event" in e for e in events):
        return
    macro_path = os.path.expanduser("~/.hermes/data/macro_events.json")
    try:
        os.makedirs(os.path.dirname(macro_path), exist_ok=True)
        with open(macro_path, "w", encoding="utf-8") as f:
            json.dump(events, f, ensure_ascii=False, indent=2)
    except Exception as e:
        await update.message.reply_text("寫入失敗: " + str(e))
        return
    from collections import Counter
    counter = Counter(e.get("importance", "?") for e in events)
    date_range = events[0]["date"] + " ~ " + events[-1]["date"]
    summary = "  ".join(k + ":" + str(v) for k, v in sorted(counter.items()))
    reply = "macro_events.json updated\n" + date_range + "\n" + str(len(events)) + " events  " + summary
    await update.message.reply_text(reply)

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("help",    cmd_help))
    app.add_handler(CommandHandler("start",   cmd_help))
    app.add_handler(CommandHandler("signals", cmd_signals))
    app.add_handler(CommandHandler("log",     cmd_log))
    app.add_handler(CommandHandler("done",    cmd_done))
    app.add_handler(CommandHandler("pending", cmd_pending))
    app.add_handler(CommandHandler("live",    cmd_live))
    app.add_handler(CommandHandler("analyze", cmd_analyze))
    app.add_handler(CommandHandler("screen",  cmd_screen))
    app.add_handler(CommandHandler("sync_ft", cmd_sync_ft))
    app.add_handler(CommandHandler("holdings", cmd_holdings))
    app.add_handler(CommandHandler("trades",   cmd_trades))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    logger.info(f"investment-bot 啟動，監聽 user {ALLOWED_USER}")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
