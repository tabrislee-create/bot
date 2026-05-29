import os
import logging
from datetime import datetime
from dotenv import dotenv_values
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)
from db import (init_db, add_event, get_today_events, get_upcoming_events,
                delete_event, search_events,
                add_coffee, search_coffee, get_recent_coffee)
from nlp import detect_intent, parse_event, parse_coffee, get_brew_params
from scheduler import init_scheduler

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)
ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
env = dotenv_values(ENV_PATH)
BOT_TOKEN = env.get("TELEGRAM_BOT_TOKEN", "")
ALLOWED_USER = int(env.get("TELEGRAM_ALLOWED_USERS", "0"))
if not BOT_TOKEN:
    raise ValueError("找不到 TELEGRAM_BOT_TOKEN，請確認 ~/.hermes/.env")

def is_allowed(update: Update) -> bool:
    return update.effective_user.id == ALLOWED_USER

def format_events(events: list, title: str) -> str:
    if not events:
        return f"{title}\n\n（目前沒有行程）"
    lines = [f"{title}\n"]
    for e in events:
        dt = datetime.strptime(e["event_time"], "%Y-%m-%d %H:%M:%S")
        lines.append(f"📅 {dt.strftime('%m/%d %H:%M')}  {e['title']}  `[id:{e['id']}]`")
    return "\n".join(lines)

# ── /start ─────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    await update.message.reply_text(
        "👋 個人助理啟動\n\n"
        "【行程】\n"
        "• 明天下午三點看牙醫\n"
        "• 今天有什麼行程\n"
        "• 本週行程\n"
        "• 取消看牙醫\n\n"
        "【咖啡豆】\n"
        "• 湛盧 衣索比亞耶加雪菲 水洗 淺焙 450/磅\n"
        "• 查咖啡 湛盧\n\n"
        "/list — 近90天行程（含刪除按鈕）\n"
        "/today — 今天行程\n"
        "/coffees — 最近5筆咖啡記錄"
    )

# ── /list ──────────────────────────────────────────
async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    events = get_upcoming_events(days=90)
    if not events:
        await update.message.reply_text("📭 近90天沒有行程")
        return
    for e in events:
        dt = datetime.strptime(e["event_time"], "%Y-%m-%d %H:%M:%S")
        remind_parts = []
        if e.get("custom_remind_time"):
            cr = datetime.strptime(e["custom_remind_time"], "%Y-%m-%d %H:%M:%S")
            remind_parts.append(f"🔔 {cr.strftime('%m/%d %H:%M')}")
        remind_parts.append("⏰ 1小時前")
        text = f"📅 {dt.strftime('%m/%d（%a） %H:%M')}  {e['title']}\n   提醒：{'、'.join(remind_parts)}"
        keyboard = [[InlineKeyboardButton("🗑 刪除", callback_data=f"del:{e['id']}")]]
        await update.message.reply_text(
            text,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

# ── /today ─────────────────────────────────────────
async def cmd_today(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    events = get_today_events()
    await update.message.reply_text(
        format_events(events, f"📋 今天的行程（{datetime.now().strftime('%m/%d')}）"),
        parse_mode="Markdown"
    )

# ── /coffees ───────────────────────────────────────
async def cmd_coffees(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    beans = get_recent_coffee(5)
    if not beans:
        await update.message.reply_text("☕ 還沒有咖啡豆記錄")
        return
    lines = ["☕ 最近5筆咖啡記錄\n"]
    for b in beans:
        dt = datetime.strptime(b["created_at"], "%Y-%m-%d %H:%M:%S")
        lines.append(
            f"• {b['roaster']} {b['product']}\n"
            f"  {b['process']} {b['roast_level']}　{b['price']}\n"
            f"  記錄於 {dt.strftime('%m/%d')}"
        )
    await update.message.reply_text("\n".join(lines))

# ── 刪除按鈕回呼 ───────────────────────────────────
async def callback_delete(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id != ALLOWED_USER:
        await query.answer("無權限")
        return
    event_id = int(query.data.split(":")[1])
    success = delete_event(event_id)
    await query.answer()
    if success:
        await query.edit_message_text(f"✅ 已刪除  {query.message.text}")
    else:
        await query.edit_message_text("❌ 找不到該行程（可能已刪除）")

# ── 自然語言訊息處理 ───────────────────────────────
async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    text = update.message.text.strip()
    await update.message.reply_text("⏳ 處理中…")
    intent_data = detect_intent(text)
    intent = intent_data.get("intent", "unknown")
    keyword = intent_data.get("keyword", "")

    if intent == "add":
        parsed = parse_event(text)
        if not parsed:
            await update.message.reply_text("😕 無法解析行程，請試試：「明天下午三點看牙醫」")
            return
        add_event(parsed["title"], parsed["event_time"], parsed.get("custom_remind_time"))
        dt_str = parsed["event_time"].strftime("%m/%d %H:%M")
        if parsed.get("custom_remind_time"):
            remind_str = parsed["custom_remind_time"].strftime("%m/%d %H:%M")
            await update.message.reply_text(
                f"✅ 已新增行程\n\n📅 {dt_str}  {parsed['title']}\n\n"
                f"🔔 自訂提醒：{remind_str}\n"
                f"⏰ 另有 1 小時前提醒"
            )
        else:
            await update.message.reply_text(
                f"✅ 已新增行程\n\n📅 {dt_str}  {parsed['title']}\n\n（1小時前會提醒你）"
            )

    elif intent == "list_today":
        events = get_today_events()
        await update.message.reply_text(
            format_events(events, f"📋 今天的行程（{datetime.now().strftime('%m/%d')}）"),
            parse_mode="Markdown"
        )

    elif intent == "list_week":
        events = get_upcoming_events(days=7)
        await update.message.reply_text(
            format_events(events, "📋 近7天行程"),
            parse_mode="Markdown"
        )

    elif intent == "delete":
        matches = search_events(keyword)
        if not matches:
            await update.message.reply_text(f"😕 找不到包含「{keyword}」的行程")
            return
        if len(matches) == 1:
            e = matches[0]
            delete_event(e["id"])
            dt = datetime.strptime(e["event_time"], "%Y-%m-%d %H:%M:%S")
            await update.message.reply_text(
                f"✅ 已刪除：{dt.strftime('%m/%d %H:%M')}  {e['title']}"
            )
        else:
            lines = ["找到多筆符合的行程，請選擇要刪除哪一筆："]
            keyboard = []
            for e in matches:
                dt = datetime.strptime(e["event_time"], "%Y-%m-%d %H:%M:%S")
                label = f"{dt.strftime('%m/%d %H:%M')} {e['title']}"
                lines.append(f"• {label}")
                keyboard.append([InlineKeyboardButton(f"🗑 {label}", callback_data=f"del:{e['id']}")])
            await update.message.reply_text(
                "\n".join(lines),
                reply_markup=InlineKeyboardMarkup(keyboard)
            )

    elif intent == "coffee_add":
        parsed = parse_coffee(text)
        if not parsed:
            await update.message.reply_text(
                "😕 無法解析咖啡豆資訊\n"
                "格式：烘豆商 產品名 處理法 烘焙度 售價\n"
                "例：湛盧 衣索比亞耶加雪菲 水洗 淺焙 450/磅"
            )
            return
        # 取得沖煮建議（同時進行）
        brew = get_brew_params(
            parsed["roaster"], parsed["product"],
            parsed["process"], parsed["roast_level"]
        )
        add_coffee(
            parsed["roaster"], parsed["product"],
            parsed["process"], parsed["roast_level"],
            parsed["price"]
        )
        await update.message.reply_text(
            f"☕ 已記錄咖啡豆\n\n"
            f"烘豆商：{parsed['roaster']}\n"
            f"產品：{parsed['product']}\n"
            f"處理法：{parsed['process']}\n"
            f"烘焙度：{parsed['roast_level']}\n"
            f"售價：{parsed['price']}\n\n"
            f"─────────────\n"
            f"{brew}"
        )

    elif intent == "coffee_query":
        beans = search_coffee(keyword)
        if not beans:
            await update.message.reply_text(f"☕ 找不到「{keyword}」的記錄")
            return
        lines = [f"☕ 找到 {len(beans)} 筆記錄\n"]
        for b in beans:
            dt = datetime.strptime(b["created_at"], "%Y-%m-%d %H:%M:%S")
            lines.append(
                f"• {b['roaster']} {b['product']}\n"
                f"  {b['process']} {b['roast_level']}　{b['price']}\n"
                f"  記錄於 {dt.strftime('%m/%d')}"
            )
        await update.message.reply_text("\n".join(lines))

    else:
        await update.message.reply_text(
            "😕 我只處理行程和咖啡豆，試試：\n"
            "• 明天下午三點看牙醫\n"
            "• 湛盧 衣索比亞耶加雪菲 水洗 淺焙 450/磅\n"
            "• 查咖啡 湛盧"
        )

# ── 主程式 ─────────────────────────────────────────
def main():
    init_db()
    logger.info("資料庫初始化完成")
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("coffees", cmd_coffees))
    app.add_handler(CallbackQueryHandler(callback_delete, pattern=r"^del:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    async def post_init(application):
        init_scheduler(application.bot, ALLOWED_USER)
        logger.info(f"Bot 啟動，監聽 user {ALLOWED_USER}")
    app.post_init = post_init
    app.run_polling(drop_pending_updates=True)
if __name__ == "__main__":
    main()
