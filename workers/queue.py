import asyncio
import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.logging_config import configure_logging
from core.schemas import JobPayload, Reply
from db.models import EntryCategory, EntryKind
from db.session import async_session
from services.entries import create_entry, list_recent
from workers.pool import REDIS_SETTINGS

configure_logging()
logger = logging.getLogger(__name__)


async def _build_reply(session: AsyncSession, job: JobPayload) -> Reply:
    text = job.text.strip()

    if text == "/list":
        entries = await list_recent(session, user_id=job.user_id)
        if not entries:
            return Reply(text="No notes yet.")
        lines = [f"{i}. {e.title} ({e.created_at:%Y-%m-%d %H:%M})" for i, e in enumerate(entries, start=1)]
        return Reply(text="\n".join(lines))

    if text == "/note" or text.startswith("/note "):
        body = text[len("/note") :].strip()
        if not body:
            return Reply(text="Usage: /note <text>")
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

    # Phase 1 fallback — anything that isn't a recognized command still echoes.
    return Reply(text=f"echo: {job.text}")


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

    async with async_session() as session:
        reply = await _build_reply(session, job)

    await send_reply(job.chat_id, reply)
    logger.info("sent reply for update_id=%s", job.update_id)


class WorkerSettings:
    functions = [handle_message]
    redis_settings = REDIS_SETTINGS
    # Phase 1 jobs are near-instant (echo only) — arq's 300s default lease would make a
    # crashed-worker's job sit orphaned for ~5 minutes before redelivery. 30s is generous
    # for this workload; revisit once real LLM calls (Phase 3+) can legitimately run longer.
    job_timeout = 30
