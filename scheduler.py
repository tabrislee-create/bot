from apscheduler.schedulers.asyncio import AsyncIOScheduler
from db import get_pending_reminders, mark_reminded, get_pending_custom_reminders, mark_custom_reminded
from datetime import datetime
import logging
logger = logging.getLogger(__name__)
_bot = None
_chat_id = None
_scheduler = None
def init_scheduler(bot, chat_id: int):
    global _bot, _chat_id, _scheduler
    _bot = bot
    _chat_id = chat_id
    _scheduler = AsyncIOScheduler(timezone="Asia/Taipei")
    _scheduler.add_job(check_reminders, "interval", minutes=1, id="reminder_check")
    _scheduler.start()
    logger.info("排程器已啟動")
async def check_reminders():
    if not _bot or not _chat_id:
        return
    # 1小時前提醒
    for event in get_pending_reminders():
        event_time = datetime.strptime(event["event_time"], "%Y-%m-%d %H:%M:%S")
        time_str = event_time.strftime("%m/%d %H:%M")
        msg = f"⏰ 提醒：**{event['title']}**\n時間：{time_str}（1 小時後）"
        try:
            await _bot.send_message(chat_id=_chat_id, text=msg, parse_mode="Markdown")
            mark_reminded(event["id"])
            logger.info(f"已發送1小時提醒：{event['title']}")
        except Exception as e:
            logger.error(f"發送提醒失敗：{e}")
    # 自訂提醒時間
    for event in get_pending_custom_reminders():
        event_time = datetime.strptime(event["event_time"], "%Y-%m-%d %H:%M:%S")
        time_str = event_time.strftime("%m/%d %H:%M")
        msg = f"🔔 提醒：**{event['title']}**\n行程時間：{time_str}"
        try:
            await _bot.send_message(chat_id=_chat_id, text=msg, parse_mode="Markdown")
            mark_custom_reminded(event["id"])
            logger.info(f"已發送自訂提醒：{event['title']}")
        except Exception as e:
            logger.error(f"發送自訂提醒失敗：{e}")
