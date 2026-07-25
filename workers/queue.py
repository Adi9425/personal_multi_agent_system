import asyncio
import logging

from app.config import settings
from app.logging_config import configure_logging
from core.schemas import JobPayload, Reply
from workers.pool import REDIS_SETTINGS

configure_logging()
logger = logging.getLogger(__name__)


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

    await send_reply(job.chat_id, Reply(text=f"echo: {job.text}"))
    logger.info("sent echo reply for update_id=%s", job.update_id)


class WorkerSettings:
    functions = [handle_message]
    redis_settings = REDIS_SETTINGS
    # Phase 1 jobs are near-instant (echo only) — arq's 300s default lease would make a
    # crashed-worker's job sit orphaned for ~5 minutes before redelivery. 30s is generous
    # for this workload; revisit once real LLM calls (Phase 3+) can legitimately run longer.
    job_timeout = 30
