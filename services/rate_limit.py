from workers.pool import get_pool

_KEY_PREFIX = "rate_limit:"
WINDOW_SECONDS = 60


async def check_and_increment(user_id: int, *, limit: int) -> bool:
    """Fixed-window counter, independent of trial status — protects against runaway API
    cost from any single user (including the admin) sending messages too fast. Same
    TTL-counter pattern services/pending_actions.py already established, same Redis
    connection (workers/pool.py)."""
    pool = await get_pool()
    key = f"{_KEY_PREFIX}{user_id}"
    count = await pool.incr(key)
    if count == 1:
        await pool.expire(key, WINDOW_SECONDS)
    return count <= limit
