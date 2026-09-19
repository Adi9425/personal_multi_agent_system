import logging
from datetime import datetime, timezone

from aiogram import Bot, Dispatcher
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.logging_config import configure_logging
from core.schemas import CallbackPayload, JobPayload, Reply
from db.models import ProcessedUpdate, User
from db.session import async_session
from services.rate_limit import check_and_increment
from services.users import (
    check_access,
    get_or_create_user,
    is_awaiting_name,
    mark_onboarding_prompted,
    needs_onboarding_prompt,
    set_display_name,
)
from workers.pool import get_pool

configure_logging()
logger = logging.getLogger(__name__)

bot = Bot(token=settings.telegram_bot_token)
dispatcher = Dispatcher()

_REJECTION_MESSAGES = {
    "trial_expired": "Your free trial has ended. Payment support is coming soon — thanks for trying this out!",
    "suspended": "Your account has been suspended.",
}

_ONBOARDING_WELCOME = (
    "Hey, I'm Jacob 👋 — your personal notes, tasks and quick-facts assistant.\n\n"
    "Tell me things naturally: \"submit the report by Friday\", \"save my wifi password: ...\", "
    "\"what do I have open this week\" — no rigid syntax needed.\n\n"
    "First — what should I call you?"
)

_ONBOARDING_MAX_NAME_LEN = 80


async def _check_access(
    *, user_id: int, chat_id: int, username: str | None, first_name: str | None = None,
    last_name: str | None = None, language_code: str | None = None,
) -> User | None:
    """Replaces the old single-chat_id whitelist (deliberately reverses FR-16) — any
    Telegram user is now accepted, auto-registered on a free trial. Sends a direct
    rejection reply and returns None for anyone not allowed to proceed; nothing gets
    enqueued for a rejected user. Rate limiting doesn't apply to the admin — the plan's own
    verification goal is that normal admin use has zero rate-limiting friction.

    Returns the User (not just bool) so callers can pull user.id — the internal UUID
    everything past this point (JobPayload.user_uuid) is keyed by."""
    user = await get_or_create_user(user_id, username, first_name, last_name, language_code)
    decision = await check_access(user)
    if decision != "allowed":
        await bot.send_message(chat_id, _REJECTION_MESSAGES[decision])
        return None

    if not user.is_admin and not await check_and_increment(user_id, limit=settings.rate_limit_max_per_minute):
        await bot.send_message(chat_id, "You're sending messages too fast — please slow down.")
        return None

    return user


async def handle_update(
    *,
    user_id: int,
    chat_id: int,
    msg_id: int,
    text: str,
    update_id: int,
    username: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
    language_code: str | None = None,
) -> None:
    """§3.3 steps 1-3: access control -> dedupe -> enqueue. Nothing here talks to Telegram
    beyond what aiogram already handed us — this function is the one seam FastAPI's
    webhook route and the polling loop both funnel through."""
    user = await _check_access(
        user_id=user_id, chat_id=chat_id, username=username,
        first_name=first_name, last_name=last_name, language_code=language_code,
    )
    if user is None:
        logger.info("access denied for user_id=%s", user_id)
        return

    async with async_session() as session:
        session.add(ProcessedUpdate(update_id=update_id))
        try:
            await session.commit()
        except IntegrityError:
            logger.info("duplicate update_id=%s, skipping", update_id)
            return

    # Onboarding: a brand-new user's first message gets a welcome + name prompt instead of
    # being run through capture/query/resolve; their very next message is consumed as the
    # answer instead of being processed normally. Both branches return before enqueueing —
    # same "handled here, nothing goes to the worker" shape as a rejected user above.
    if is_awaiting_name(user):
        display_name = text.strip()[:_ONBOARDING_MAX_NAME_LEN]
        if not display_name:
            await bot.send_message(chat_id, "Didn't catch a name there — what should I call you?")
            return
        await set_display_name(user.id, display_name)
        await bot.send_message(
            chat_id,
            f"Good to meet you, {display_name}! Go ahead — tell me something to remember, "
            "ask a question, or say what you need done.",
        )
        return

    if needs_onboarding_prompt(user):
        await mark_onboarding_prompted(user.id)
        await bot.send_message(chat_id, _ONBOARDING_WELCOME)
        return

    payload = JobPayload(
        update_id=update_id,
        user_id=user_id,
        user_uuid=user.id,
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
        username=message.from_user.username,
        first_name=message.from_user.first_name,
        last_name=message.from_user.last_name,
        language_code=message.from_user.language_code,
    )


async def handle_callback_query(
    *,
    user_id: int,
    chat_id: int,
    message_id: int,
    callback_id: str,
    token: str,
    update_id: int,
    username: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
    language_code: str | None = None,
) -> None:
    """Same access-control -> dedupe -> enqueue shape as handle_update, for button presses.
    Answered immediately (Telegram shows a loading spinner on the button until this
    happens) — the actual apply logic runs later in the worker, same as messages."""
    user = await _check_access(
        user_id=user_id, chat_id=chat_id, username=username,
        first_name=first_name, last_name=last_name, language_code=language_code,
    )
    if user is None:
        logger.info("access denied (callback) for user_id=%s", user_id)
        await bot.answer_callback_query(callback_id)
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
        username=callback.from_user.username,
        first_name=callback.from_user.first_name,
        last_name=callback.from_user.last_name,
        language_code=callback.from_user.language_code,
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
