from arq import ArqRedis, create_pool
from arq.connections import RedisSettings

from app.config import settings

REDIS_SETTINGS = RedisSettings.from_dsn(settings.redis_url)

_pool: ArqRedis | None = None


async def get_pool() -> ArqRedis:
    """Shared producer-side pool — used by the Telegram adapter to enqueue jobs."""
    global _pool
    if _pool is None:
        _pool = await create_pool(REDIS_SETTINGS)
    return _pool
