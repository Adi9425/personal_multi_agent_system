import asyncio
import re

from app.config import settings
from core.schemas import EntryCategory, EntryKind, FactExtraction, Reply
from db.session import tenant_session
from services import dispatch_patterns
from services.embeddings import embed
from services.entries import archive_entry, create_entry, find_fact
from services.llm import call_structured

# "is this even a fact-save attempt at all" — a stable, rarely-changing fact about which verbs
# mean "save a fact," not a per-shape parsing rule, so it stays in code rather than
# dispatch_patterns (which holds the SHAPE-parsing rules, tried below). "set"/"get" were
# considered and dropped: "get milk tomorrow" and "set the meeting time to 3pm" are both
# common, legitimate task-capture phrasings that would otherwise get silently hijacked into a
# flat KV lookup/save instead of going through the real LLM capture flow. "save"/"remember"
# are unambiguous by comparison — a real capture essentially never starts with those exact
# words. Because of that, ANY message starting with save/remember is committed to the fact
# path (a DB pattern match, or the LLM fallback below) — never the general capture pipeline.
_SAVE_TRIGGER = re.compile(r"^(?:save|remember)\b", re.IGNORECASE)

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
    async with tenant_session(user_id) as session:
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


def _try_extract(text: str, flow: str) -> tuple[str, str | None] | None:
    """Tries every enabled DB-backed pattern for `flow`, in priority order — first match
    that survives the reject-rule check wins; a vetoed match tries the next pattern rather
    than giving up on the message. Returns (key, value) — value is None for "get", which has
    no value group. Returns None if nothing matches, or every match gets vetoed. Match/veto
    telemetry is fire-and-forget: it must never add latency to, or fail, the fast path."""
    for pattern in dispatch_patterns.get_patterns(flow):
        match = pattern.regex.match(text)
        if not match:
            continue

        key = _normalize_key(match.group("key"))

        vetoed_by = next(
            (rule for rule in dispatch_patterns.get_reject_rules(flow) if rule.regex.fullmatch(key)),
            None,
        )
        if vetoed_by is not None:
            asyncio.create_task(dispatch_patterns.record_veto(vetoed_by.id))
            continue  # this pattern's match is vetoed — try the next pattern, not give up

        asyncio.create_task(dispatch_patterns.record_match(pattern.id))
        value = match.group("value").strip() if flow == "save" else None
        return key, value

    return None


async def try_handle_save(user_id: int, text: str, msg_id: int) -> Reply | None:
    """DB-pattern-first fast path for "save X: Y" style messages, with a cheap LLM fallback
    for messages that clearly want to save a fact (trigger word present) but don't parse
    cleanly. Returns None only if there's no save/remember trigger at all — once the trigger
    is present, this always handles the message (never falls through to the general capture
    pipeline)."""
    text = text.strip()

    extracted = _try_extract(text, "save")
    if extracted is not None:
        key, value = extracted
        await _save_fact(user_id, key, value, msg_id=msg_id, embedding=None)
        return Reply(text=f"Saved: {key} = {value}")

    if not _SAVE_TRIGGER.match(text):
        return None

    fact = await _extract_fact(text, user_id=user_id)
    key = _normalize_key(fact.key)
    vector = await embed(f"{fact.key}: {fact.value}", user_id=user_id, action="embed-fact")
    await _save_fact(user_id, key, fact.value, msg_id=msg_id, embedding=vector)
    return Reply(text=f"Saved: {fact.key} = {fact.value}")


async def try_handle_get(user_id: int, text: str) -> Reply | None:
    """DB-pattern-first fast path for "what's X" style messages: exact-title lookup among
    the user's fact entries. Falls through to the full query() pipeline (skipping
    classify_intent, since a matched pattern already establishes query intent) if no fact
    title matches — a miss here might still be a real note, so it isn't a dead end. Returns
    None only if no enabled get-pattern matches at all."""
    text = text.strip()

    extracted = _try_extract(text, "get")
    if extracted is None:
        return None
    key, _ = extracted

    async with tenant_session(user_id) as session:
        fact = await find_fact(session, user_id=user_id, key=key)
    if fact is not None:
        return Reply(text=f"{fact.title}: {fact.body}")

    from agents.notes.query import query  # deferred: avoids a circular import at module load

    return await query(user_id=user_id, text=text)
