import re

from core.schemas import Reply
from db.models import UserKVNote
from db.session import async_session

# Deliberately narrow trigger words. "set"/"get" were considered and dropped: "get milk
# tomorrow" and "set the meeting time to 3pm" are both common, legitimate task-capture
# phrasings that would otherwise get silently hijacked into a flat KV lookup/save instead of
# going through the real LLM capture flow (with due dates, categories, etc.). "save"/
# "remember" (writing) and "what's"/"what is" (reading) are unambiguous by comparison — a
# real capture/query essentially never starts with those exact words.
SAVE_PATTERN = re.compile(
    r"^(?:save|remember)\s+(?P<key>.+?)\s*(?:\bas\b|\bis\b|\bto\b|:|=)\s*(?P<value>.+)$",
    re.IGNORECASE,
)
GET_PATTERN = re.compile(r"^what(?:'s|\s+is)\s+(?P<key>.+?)\??$", re.IGNORECASE)


def _normalize_key(key: str) -> str:
    return key.strip().lower()


async def save(user_id: int, key: str, value: str) -> None:
    """The only writer of user_kv_notes. Upsert by (user_id, key) — saving the same key
    twice updates it rather than creating a duplicate."""
    normalized_key = _normalize_key(key)
    async with async_session() as session:
        existing = await session.get(UserKVNote, {"user_id": user_id, "key": normalized_key})
        if existing is not None:
            existing.value = value
        else:
            session.add(UserKVNote(user_id=user_id, key=normalized_key, value=value))
        await session.commit()


async def get(user_id: int, key: str) -> str | None:
    normalized_key = _normalize_key(key)
    async with async_session() as session:
        note = await session.get(UserKVNote, {"user_id": user_id, "key": normalized_key})
        return note.value if note is not None else None


async def try_handle(user_id: int, text: str) -> Reply | None:
    """Zero-LLM fast path for "save X: Y" / "what's X" style messages. Returns None if the
    message doesn't match either shape (or a `get` finds nothing), so the caller falls
    through to the normal LLM pipeline — a save-shaped match is always handled here (no
    reasonable fallback interpretation), but a get-shaped miss might still be answerable by
    a real note, so it isn't a dead end."""
    text = text.strip()

    save_match = SAVE_PATTERN.match(text)
    if save_match:
        key = save_match.group("key").strip()
        value = save_match.group("value").strip()
        await save(user_id, key, value)
        return Reply(text=f"Saved: {key} = {value}")

    get_match = GET_PATTERN.match(text)
    if get_match:
        key = get_match.group("key").strip()
        value = await get(user_id, key)
        if value is not None:
            return Reply(text=f"{key}: {value}")
        return None

    return None
