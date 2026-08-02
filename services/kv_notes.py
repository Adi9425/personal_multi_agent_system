import re

from app.config import settings
from core.schemas import EntryCategory, EntryKind, FactExtraction, Reply
from db.session import async_session
from services.embeddings import embed
from services.entries import archive_entry, create_entry, find_fact
from services.llm import call_structured

# Deliberately narrow trigger words. "set"/"get" were considered and dropped: "get milk
# tomorrow" and "set the meeting time to 3pm" are both common, legitimate task-capture
# phrasings that would otherwise get silently hijacked into a flat KV lookup/save instead of
# going through the real LLM capture flow (with due dates, categories, etc.). "save"/
# "remember" (writing) and "what's"/"what is" (reading) are unambiguous by comparison — a
# real capture/query essentially never starts with those exact words. Because of that, ANY
# message starting with save/remember is now committed to the fact path (regex or LLM
# fallback below) — never the general classify_intent+capture pipeline.
_SAVE_TRIGGER = re.compile(r"^(?:save|remember)\b", re.IGNORECASE)

# Key always comes first ("save my linkedin profile: URL" / "... as URL" / "... is URL") —
# tried treating "as" as reversed (file-save convention: "save X as Y" = content X, label
# Y), but real testing showed that's wrong for how this actually gets phrased ("save my
# linkedin profile as https://...") — key-first for every separator matches actual usage.
#
# ":"/"=" must be followed by whitespace — otherwise a URL's own "https:" or a query
# string's "?x=1" gets misread as the separator (a real bug found in testing, since URLs are
# the single most common thing this feature is used to save).
SAVE_PATTERN = re.compile(
    r"^(?:save|remember)\s+(?P<key>.+?)\s*(?:\bas\b|\bis\b|\bto\b|:\s+|=\s+)(?P<value>.+)$",
    re.IGNORECASE,
)
GET_PATTERN = re.compile(r"^what(?:'s|\s+is)\s+(?P<key>.+?)\??$", re.IGNORECASE)

# A key that's JUST a bare pronoun ("save this as ...", "remember that is ...") is almost
# always a parsing artifact of a run-on sentence with no real separator between a filler
# word and the intended label ("save this as my linked profile <url>" — "this" is not the
# label), not a genuine intended key. Rejecting these routes to the LLM-fallback extractor
# below instead of committing a wrong save.
_BARE_PRONOUN_KEYS = {"this", "that", "it"}

_FACT_EXTRACT_SYSTEM_PROMPT = (
    "The user sent a message asking to save a personal fact (their own, or someone else's — "
    'e.g. "save John\'s linkedin profile adi9425" or "remember the recruiter\'s email is '
    'x@y.com"). Extract the label being saved under (key) and the content being saved '
    "(value), verbatim from the message. Do not add or remove information."
)


def _normalize_key(key: str) -> str:
    return key.strip().lower()


async def _extract_fact(text: str, *, user_id: int) -> FactExtraction:
    return await call_structured(
        model=settings.model_fast,
        system=_FACT_EXTRACT_SYSTEM_PROMPT,
        user=text,
        response_model=FactExtraction,
        trace_name="fact-extract",
        user_id=user_id,
    )


async def _save_fact(user_id: int, key: str, value: str, *, msg_id: int, embedding: list[float] | None) -> None:
    """Supersedes any existing fact under the same normalized key: archiving instead of
    mutating in place keeps Entry.body's "immutable, set once at creation" invariant intact
    and reuses the existing audit-trailed archive path rather than inventing new semantics."""
    async with async_session() as session:
        existing = await find_fact(session, user_id=user_id, key=key)
        if existing is not None:
            await archive_entry(session, entry_id=existing.id, source_msg_id=msg_id)
        await create_entry(
            session,
            user_id=user_id,
            category=EntryCategory.personal,
            kind=EntryKind.fact,
            title=key,
            body=value,
            source_msg_id=msg_id,
            embedding=embedding,
        )


async def try_handle_save(user_id: int, text: str, msg_id: int) -> Reply | None:
    """Zero-LLM fast path for "save X: Y" style messages, with a cheap LLM fallback for
    messages that clearly want to save a fact (trigger word present) but don't parse
    cleanly. Returns None only if there's no save/remember trigger at all — once the
    trigger is present, this always handles the message (never falls through to the
    general capture pipeline)."""
    text = text.strip()

    save_match = SAVE_PATTERN.match(text)
    if save_match:
        key = save_match.group("key").strip()
        value = save_match.group("value").strip()
        if _normalize_key(key) not in _BARE_PRONOUN_KEYS:
            await _save_fact(user_id, _normalize_key(key), value, msg_id=msg_id, embedding=None)
            return Reply(text=f"Saved: {key} = {value}")

    if not _SAVE_TRIGGER.match(text):
        return None

    fact = await _extract_fact(text, user_id=user_id)
    key = _normalize_key(fact.key)
    vector = await embed(f"{fact.key}: {fact.value}", user_id=user_id, action="embed-fact")
    await _save_fact(user_id, key, fact.value, msg_id=msg_id, embedding=vector)
    return Reply(text=f"Saved: {fact.key} = {fact.value}")


async def try_handle_get(user_id: int, text: str) -> Reply | None:
    """Zero-LLM fast path for "what's X" style messages: exact-title lookup among the
    user's fact entries. Falls through to the full query() pipeline (skipping
    classify_intent, since the trigger word already establishes query intent) if no fact
    title matches — a miss here might still be a real note, so it isn't a dead end.
    Returns None only if there's no what's/what-is trigger at all."""
    text = text.strip()

    get_match = GET_PATTERN.match(text)
    if not get_match:
        return None

    key = _normalize_key(get_match.group("key"))
    async with async_session() as session:
        fact = await find_fact(session, user_id=user_id, key=key)
    if fact is not None:
        return Reply(text=f"{fact.title}: {fact.body}")

    from agents.notes.query import query  # deferred: avoids a circular import at module load

    return await query(user_id=user_id, text=text)
