import json
import secrets

from workers.pool import get_pool

# §6.2: disambiguation choices get a 1h TTL. Undo tokens reuse the same TTL — the HLD doesn't
# give Undo its own number, and inventing a second one wouldn't be more principled than reuse.
DEFAULT_TTL_SECONDS = 3600
_KEY_PREFIX = "pending_action:"


async def store(data: dict, *, ttl: int = DEFAULT_TTL_SECONDS) -> str:
    """Telegram caps callback_data at 64 bytes, so buttons carry only this short token —
    never the actual entry_id/operation/value, which live here instead."""
    token = secrets.token_urlsafe(8)
    pool = await get_pool()
    await pool.set(_KEY_PREFIX + token, json.dumps(data), ex=ttl)
    return token


async def fetch(token: str) -> dict | None:
    pool = await get_pool()
    raw = await pool.get(_KEY_PREFIX + token)
    if raw is None:
        return None
    return json.loads(raw)


async def delete(token: str) -> None:
    pool = await get_pool()
    await pool.delete(_KEY_PREFIX + token)
