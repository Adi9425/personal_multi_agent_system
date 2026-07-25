import logging

from arq import ArqRedis, create_pool
from arq.connections import RedisSettings

from app.config import settings

logger = logging.getLogger("worker")
logging.basicConfig(level=logging.INFO)

REDIS_SETTINGS = RedisSettings.from_dsn(settings.redis_url)

_pool: ArqRedis | None = None


async def get_pool() -> ArqRedis:
    """Shared producer-side pool — used by the Telegram adapter to enqueue jobs."""
    global _pool
    if _pool is None:
        _pool = await create_pool(REDIS_SETTINGS)
    return _pool


async def handle_message(ctx, payload: dict) -> None:
    """Step 5: still a placeholder — just proves whitelist/dedupe/enqueue wiring.
    Step 7 replaces this body with the real echo send."""
    logger.info("received job payload: %s", payload)


class WorkerSettings:
    functions = [handle_message]
    redis_settings = REDIS_SETTINGS
