import asyncio
import logging
import uuid

import agents.notes
from agents.notes.resolve import apply_operation, confirmation_text, undo
from app.config import settings
from app.logging_config import configure_logging
from core.schemas import Button, CallbackPayload, JobPayload, Reply
from db.models import EntryCategory, EntryKind
from db.session import async_session
from services import pending_actions
from services.entries import create_entry, list_recent
from workers.pool import REDIS_SETTINGS

configure_logging()
logger = logging.getLogger(__name__)


async def _build_reply(job: JobPayload) -> Reply:
    text = job.text.strip()

    if text == "/list":
        async with async_session() as session:
            entries = await list_recent(session, user_id=job.user_id)
        if not entries:
            return Reply(text="No notes yet.")
        lines = [f"{i}. {e.title} ({e.created_at:%Y-%m-%d %H:%M})" for i, e in enumerate(entries, start=1)]
        return Reply(text="\n".join(lines))

    if text == "/note" or text.startswith("/note "):
        body = text[len("/note") :].strip()
        if not body:
            return Reply(text="Usage: /note <text>")
        async with async_session() as session:
            entry = await create_entry(
                session,
                user_id=job.user_id,
                category=EntryCategory.personal,
                kind=EntryKind.note,
                title=body[:80],
                body=body,
                source_msg_id=job.msg_id,
            )
        return Reply(text=f"saved: {entry.title}")

    # Phase 3: plain messages (no slash command) route to LLM-driven capture.
    return await agents.notes.handle(user_id=job.user_id, text=job.text, msg_id=job.msg_id)


async def handle_message(ctx, payload: dict) -> None:
    # Deferred import: transport.telegram.adapter imports workers.pool (producer side),
    # so importing adapter at module load time here would be circular.
    from transport.telegram.adapter import send_reply

    job = JobPayload.model_validate(payload)

    if settings.worker_test_delay_seconds:
        logger.info(
            "test delay: sleeping %ss before send (update_id=%s)",
            settings.worker_test_delay_seconds,
            job.update_id,
        )
        await asyncio.sleep(settings.worker_test_delay_seconds)

    reply = await _build_reply(job)

    message_id = await send_reply(job.chat_id, reply)
    logger.info("sent reply for update_id=%s", job.update_id)

    if reply.buttons:
        # The pending-action token(s) were created before this message existed (resolve.py
        # needs the token to build the button before anything is sent) — attach the
        # now-known (chat_id, message_id) so a later callback can edit this exact message.
        for button in reply.buttons:
            await pending_actions.attach(button.callback_data, {"chat_id": job.chat_id, "message_id": message_id})


async def handle_callback(ctx, payload: dict) -> None:
    from transport.telegram.adapter import edit_reply

    cb = CallbackPayload.model_validate(payload)
    data = await pending_actions.fetch(cb.token)

    if data is None:
        await edit_reply(cb.chat_id, cb.message_id, Reply(text="This action has expired."))
        return

    await pending_actions.delete(cb.token)  # one-shot use

    if data["kind"] == "cancel":
        await edit_reply(cb.chat_id, cb.message_id, Reply(text="OK, no changes made."))
        return

    if data["kind"] == "disambiguation_choice":
        entry, changes = await apply_operation(
            operation=data["operation"],
            entry_id=uuid.UUID(data["entry_id"]),
            value=data["value"],
            source_msg_id=data["source_msg_id"],
        )
        undo_token = await pending_actions.store(
            {"kind": "undo", "entry_id": str(entry.id), "changes": changes}
        )
        await edit_reply(
            cb.chat_id,
            cb.message_id,
            Reply(text=confirmation_text(data["operation"], entry, data["value"]),
                  buttons=[Button(label="Undo", callback_data=undo_token)]),
        )
        # This new Undo button also needs its (chat_id, message_id) attached — it's the
        # same message we just edited, so we already know both.
        await pending_actions.attach(undo_token, {"chat_id": cb.chat_id, "message_id": cb.message_id})
        return

    if data["kind"] == "undo":
        entry, _ = await undo(entry_id=uuid.UUID(data["entry_id"]), changes=data["changes"])
        await edit_reply(cb.chat_id, cb.message_id, Reply(text=f"Undone: {entry.title}"))
        return

    logger.warning("unknown pending action kind=%s for token=%s", data.get("kind"), cb.token)


class WorkerSettings:
    functions = [handle_message, handle_callback]
    redis_settings = REDIS_SETTINGS
    # Phase 3 jobs now involve real LLM calls (classify + extract + embed, sequentially) —
    # a few seconds typically, but the old 30s Phase-1 echo-tuned ceiling is too tight a
    # margin. 60s gives real headroom without reverting to arq's oversized 300s default.
    job_timeout = 60
