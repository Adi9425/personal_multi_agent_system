import logging
from datetime import datetime, timezone

from aiogram import Bot, Dispatcher
from aiogram.types import Message, Update
from sqlalchemy.exc import IntegrityError

from app.config import settings
from core.schemas import JobPayload
from db.models import ProcessedUpdate
from db.session import async_session
from workers.queue import get_pool

logger = logging.getLogger("telegram_adapter")
logging.basicConfig(level=logging.INFO)

bot = Bot(token=settings.telegram_bot_token)
dispatcher = Dispatcher()


async def handle_update(*, user_id: int, chat_id: int, msg_id: int, text: str, update_id: int) -> None:
    """§3.3 steps 1-3: whitelist -> dedupe -> enqueue. Nothing here talks to Telegram
    beyond what aiogram already handed us — this function is the one seam FastAPI's
    webhook route and the polling loop both funnel through."""
    if chat_id not in settings.allowed_chat_ids:
        logger.info("dropping message from non-whitelisted chat_id=%s", chat_id)
        return

    async with async_session() as session:
        session.add(ProcessedUpdate(update_id=update_id))
        try:
            await session.commit()
        except IntegrityError:
            logger.info("duplicate update_id=%s, skipping", update_id)
            return

    payload = JobPayload(
        update_id=update_id,
        user_id=user_id,
        chat_id=chat_id,
        msg_id=msg_id,
        text=text,
        conversation_id=f"tg:{chat_id}",
        enqueued_at=datetime.now(timezone.utc),
    )
    pool = await get_pool()
    await pool.enqueue_job("handle_message", payload.model_dump(mode="json"))
    logger.info("enqueued job for update_id=%s", update_id)


@dispatcher.message()
async def on_message(message: Message, event_update: Update) -> None:
    if message.text is None or message.from_user is None:
        return
    await handle_update(
        user_id=message.from_user.id,
        chat_id=message.chat.id,
        msg_id=message.message_id,
        text=message.text,
        update_id=event_update.update_id,
    )


async def run_polling() -> None:
    await dispatcher.start_polling(bot)


async def process_webhook_update(update_data: dict) -> None:
    update = Update.model_validate(update_data)
    await dispatcher.feed_update(bot, update)
