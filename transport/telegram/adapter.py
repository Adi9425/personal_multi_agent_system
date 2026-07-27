import logging
from datetime import datetime, timezone

from aiogram import Bot, Dispatcher
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.logging_config import configure_logging
from core.schemas import CallbackPayload, JobPayload, Reply
from db.models import ProcessedUpdate
from db.session import async_session
from workers.pool import get_pool

configure_logging()
logger = logging.getLogger(__name__)

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


async def handle_callback_query(*, user_id: int, chat_id: int, message_id: int, callback_id: str, token: str, update_id: int) -> None:
    """Same whitelist -> dedupe -> enqueue shape as handle_update, for button presses.
    Answered immediately (Telegram shows a loading spinner on the button until this
    happens) — the actual apply logic runs later in the worker, same as messages."""
    if chat_id not in settings.allowed_chat_ids:
        logger.info("dropping callback from non-whitelisted chat_id=%s", chat_id)
        return

    async with async_session() as session:
        session.add(ProcessedUpdate(update_id=update_id))
        try:
            await session.commit()
        except IntegrityError:
            logger.info("duplicate callback update_id=%s, skipping", update_id)
            return

    await bot.answer_callback_query(callback_id)

    payload = CallbackPayload(
        callback_query_id=callback_id,
        user_id=user_id,
        chat_id=chat_id,
        message_id=message_id,
        token=token,
        conversation_id=f"tg:{chat_id}",
    )
    pool = await get_pool()
    await pool.enqueue_job("handle_callback", payload.model_dump(mode="json"))
    logger.info("enqueued callback job for update_id=%s", update_id)


@dispatcher.callback_query()
async def on_callback(callback: CallbackQuery, event_update: Update) -> None:
    if callback.message is None or callback.from_user is None or callback.data is None:
        return
    await handle_callback_query(
        user_id=callback.from_user.id,
        chat_id=callback.message.chat.id,
        message_id=callback.message.message_id,
        callback_id=callback.id,
        token=callback.data,
        update_id=event_update.update_id,
    )


def _build_markup(reply: Reply) -> InlineKeyboardMarkup | None:
    if not reply.buttons:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=b.label, callback_data=b.callback_data)] for b in reply.buttons]
    )


async def send_reply(chat_id: int, reply: Reply) -> int:
    """The other half of the D6 boundary: agent/worker code produces a transport-neutral
    Reply, this is the one place that turns it into an actual Telegram call. Returns the
    sent message's id — needed when a Reply carries buttons, so the pending action can
    later be edited in place (§6.2) rather than replied to with a new message."""
    message = await bot.send_message(chat_id, reply.text, reply_markup=_build_markup(reply))
    return message.message_id


async def edit_reply(chat_id: int, message_id: int, reply: Reply) -> None:
    """§6.2: "on callback: apply, edit message to confirm" — used after a button press
    resolves, instead of sending a new message."""
    await bot.edit_message_text(reply.text, chat_id=chat_id, message_id=message_id, reply_markup=_build_markup(reply))


async def run_polling() -> None:
    await dispatcher.start_polling(bot)


async def process_webhook_update(update_data: dict) -> None:
    update = Update.model_validate(update_data)
    await dispatcher.feed_update(bot, update)
