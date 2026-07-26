import asyncio
import logging

import agents.notes
from app.config import settings
from app.logging_config import configure_logging
from core.schemas import JobPayload, Reply
from db.models import EntryCategory, EntryKind
from db.session import async_session
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

    await send_reply(job.chat_id, reply)
    logger.info("sent reply for update_id=%s", job.update_id)


class WorkerSettings:
    functions = [handle_message]
    redis_settings = REDIS_SETTINGS
    # Phase 3 jobs now involve real LLM calls (classify + extract + embed, sequentially) —
    # a few seconds typically, but the old 30s Phase-1 echo-tuned ceiling is too tight a
    # margin. 60s gives real headroom without reverting to arq's oversized 300s default.
    job_timeout = 60
