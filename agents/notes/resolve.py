import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from app.config import settings
from core.schemas import Button, EntryStatus, QueryPlan, Reply, UpdateRequest
from db.session import async_session
from services import pending_actions
from services.embeddings import embed
from services.entries import (
    add_tag_entry,
    archive_entry,
    complete_entry,
    reopen_entry,
    retitle_entry,
    set_due_entry,
)
from services.llm import call_structured
from services.search import hybrid_search

# §6.2: score(d) = 1/(60+rank_fts) + 1/(60+rank_vec) + RECENCY_WEIGHT*recency(d). The
# theoretical max (rank 1 on both signals, zero age) is what AUTO_THRESHOLD/GAP_THRESHOLD are
# normalized against (see resolve() below) — the HLD says "normalised" without defining how;
# dividing by the top candidate's own score would make AUTO_THRESHOLD always trivially pass.
MAX_POSSIBLE_SCORE = 2 / 61 + settings.recency_weight

_CONFIRM_EMOJI = {
    "complete": "✅",
    "reopen": "🔓",
    "archive": "🗄️",
    "set_due": "📅",
    "add_tag": "🏷️",
    "retitle": "✏️",
}


def _update_request_system_prompt() -> str:
    # FR-4 pattern, reused: set_due's `value` must already be an ISO datetime by the time it
    # reaches the apply step — UpdateRequest.value is a plain str per §5, so the LLM resolves
    # relative phrases itself rather than a second typed field doing it.
    tz = ZoneInfo(settings.user_timezone)
    now = datetime.now(tz)
    return (
        "You translate a message about changing an existing saved note/task into a "
        "structured update request.\n\n"
        "target_hint: the phrase identifying which entry (e.g. \"the intuit follow up\").\n"
        "operation: one of complete, reopen, set_due, add_tag, retitle, archive.\n"
        "value: required for set_due (an ISO 8601 datetime WITH timezone offset — resolve "
        'relative phrases like "next Friday" against the current date/time below, default '
        "to 23:59 local time if no time of day is given), add_tag (the tag text), or "
        "retitle (the new title). Null for complete/reopen/archive.\n\n"
        f"Current date/time: {now.isoformat()} ({settings.user_timezone})."
    )


async def extract_update_request(text: str) -> UpdateRequest:
    return await call_structured(
        model=settings.model_fast,
        system=_update_request_system_prompt(),
        user=text,
        response_model=UpdateRequest,
        trace_name="resolve-extract-update-request",
    )


async def apply_operation(
    *, operation: str, entry_id: uuid.UUID, value: str | None, source_msg_id: int | None
):
    if operation == "complete":
        return await _run(complete_entry, entry_id=entry_id, source_msg_id=source_msg_id)
    if operation == "reopen":
        return await _run(reopen_entry, entry_id=entry_id, source_msg_id=source_msg_id)
    if operation == "archive":
        return await _run(archive_entry, entry_id=entry_id, source_msg_id=source_msg_id)
    if operation == "set_due":
        due_at = datetime.fromisoformat(value) if value else None
        return await _run(set_due_entry, entry_id=entry_id, due_at=due_at, source_msg_id=source_msg_id)
    if operation == "add_tag":
        return await _run(add_tag_entry, entry_id=entry_id, tag=value, source_msg_id=source_msg_id)
    if operation == "retitle":
        return await _run(retitle_entry, entry_id=entry_id, title=value, source_msg_id=source_msg_id)
    raise ValueError(f"unknown operation {operation}")


async def _run(fn, **kwargs):
    async with async_session() as session:
        return await fn(session, **kwargs)


def _confirmation_text(operation: str, entry, value: str | None) -> str:
    emoji = _CONFIRM_EMOJI[operation]
    if operation == "set_due":
        tz = ZoneInfo(settings.user_timezone)
        due_local = entry.due_at.astimezone(tz).strftime("%a %d %b") if entry.due_at else "none"
        return f"{emoji} Due date updated: {entry.title} -> {due_local}"
    if operation == "add_tag":
        return f"{emoji} Tagged '{value}': {entry.title}"
    if operation == "retitle":
        return f"{emoji} Retitled to '{entry.title}'"
    verb = {"complete": "Marked done", "reopen": "Reopened", "archive": "Archived"}[operation]
    return f"{emoji} {verb}: {entry.title}"


async def resolve(*, user_id: int, text: str, msg_id: int) -> Reply:
    """§6.2 — the hard path: extract, resolve to a specific entry (or don't guess), apply."""
    req = await extract_update_request(text)

    query_embedding = await embed(req.target_hint)
    plan = QueryPlan(search_text=req.target_hint, status=EntryStatus.open)

    async with async_session() as session:
        candidates = await hybrid_search(
            session,
            user_id=user_id,
            plan=plan,
            query_embedding=query_embedding,
            apply_similarity_floor=False,
        )

    if not candidates:
        return Reply(text=f"Couldn't find anything matching '{req.target_hint}'.")

    top = candidates[0]
    top_norm = float(top["score"]) / MAX_POSSIBLE_SCORE
    second_norm = float(candidates[1]["score"]) / MAX_POSSIBLE_SCORE if len(candidates) > 1 else 0.0

    if top_norm >= settings.auto_threshold and (top_norm - second_norm) >= settings.gap_threshold:
        entry, changes = await apply_operation(
            operation=req.operation, entry_id=top["id"], value=req.value, source_msg_id=msg_id
        )
        undo_token = await pending_actions.store({"entry_id": str(entry.id), "changes": changes})
        return Reply(
            text=_confirmation_text(req.operation, entry, req.value),
            buttons=[Button(label="Undo", callback_data=undo_token)],
        )

    # Step 6 adds the disambiguation branch here.
    return Reply(text="Multiple matches — disambiguation not yet implemented.")
