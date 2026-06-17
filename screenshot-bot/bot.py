import logging
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

load_dotenv(os.path.expanduser("~/.hermes/.env"))

TOKEN = os.environ["SCREENSHOT_BOT_TOKEN"]
ALLOWED_USER_ID = 7180330735
SCREENSHOTS_DIR = Path(os.path.expanduser("~/screenshots"))
TAIPEI_TZ = ZoneInfo("Asia/Taipei")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

HELP_TEXT = (
    "📸 截圖接收 Bot\n\n"
    "傳圖片給我，我會存到 ~/screenshots/\n\n"
    "指令：\n"
    "/list - 列出最近 10 張截圖\n"
    "/help - 顯示這個說明"
)


def is_allowed(update: Update) -> bool:
    user = update.effective_user
    return user is not None and user.id == ALLOWED_USER_ID


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return

    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)

    filename = datetime.now(TAIPEI_TZ).strftime("%Y%m%d_%H%M%S") + ".jpg"
    dest_path = SCREENSHOTS_DIR / filename
    await file.download_to_drive(custom_path=str(dest_path))

    await update.message.reply_text(f"✅ 已存：screenshots/{filename}")


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return

    doc = update.message.document
    if doc.mime_type and doc.mime_type.startswith("image/"):
        file = await context.bot.get_file(doc.file_id)
        filename = datetime.now(TAIPEI_TZ).strftime("%Y%m%d_%H%M%S") + ".jpg"
        dest_path = SCREENSHOTS_DIR / filename
        await file.download_to_drive(custom_path=str(dest_path))
        await update.message.reply_text(f"✅ 已存：screenshots/{filename}")
    else:
        await update.message.reply_text("請傳圖片")


async def handle_other(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    await update.message.reply_text("請傳圖片")


async def list_screenshots(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return

    files = sorted(SCREENSHOTS_DIR.glob("*.jpg"), key=lambda p: p.stat().st_mtime, reverse=True)[:10]
    if not files:
        await update.message.reply_text("目前沒有截圖")
        return

    listing = "\n".join(f.name for f in files)
    await update.message.reply_text(f"最近 {len(files)} 張截圖：\n{listing}")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    await update.message.reply_text(HELP_TEXT)


def main() -> None:
    SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)

    application = Application.builder().token(TOKEN).build()

    application.add_handler(CommandHandler("list", list_screenshots))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(MessageHandler(filters.Document.IMAGE, handle_document))
    application.add_handler(MessageHandler(~filters.COMMAND, handle_other))

    logger.info("Screenshot bot starting...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
