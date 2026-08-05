from datetime import datetime
from zoneinfo import ZoneInfo

from app.config import settings
from core.schemas import CapturedEntry, EntryKind, Reply
from db.session import async_session
from services.embeddings import embed
from services.entries import create_entry
from services.llm import call_structured

CATEGORY_EMOJI = {
    "office_work": "📁",
    "self_learning": "📚",
    "personal": "🏠",
    "ideas": "💡",
}


def _extraction_system_prompt() -> str:
    # FR-4: always inject current datetime + timezone — never let the model assume "now".
    tz = ZoneInfo(settings.user_timezone)
    now = datetime.now(tz)
    return (
        "You extract a structured note/task entry from a short message typed into a "
        "personal assistant.\n\n"
        "category: one of office_work, self_learning, personal, ideas.\n"
        'kind: "task" if there is an action to complete or a deadline, otherwise "note".\n'
        "title: a short, human-scannable summary, max 80 characters.\n"
        "tags: up to 5 short lowercase tags, or an empty list if none obviously apply.\n\n"
        f"Current date/time: {now.isoformat()} ({settings.user_timezone}). Resolve any "
        'relative date phrase ("tomorrow", "next Friday", "in 3 days") against this exact '
        'moment. If a due date is implied but no specific time is given, use 23:59 in the '
        "user's timezone, converted to UTC for due_at. Set due_source_text to the exact "
        'phrase that implied the date (e.g. "next Friday"). If no due date is implied, leave '
        "due_at and due_source_text null."
    )


async def extract(text: str, *, user_id: int | None = None) -> CapturedEntry:
    return await call_structured(
        model=settings.model_strong,
        system=_extraction_system_prompt(),
        user=text,
        response_model=CapturedEntry,
        trace_name="capture-extract",
        user_id=user_id,
    )


async def capture(*, user_id: int, text: str, msg_id: int) -> Reply:
    """§6.1, minus the Phase-6 Doc re-render step."""
    captured = await extract(text, user_id=user_id)

    vector = await embed(f"{captured.title}\n{text}", user_id=user_id, action="embed-capture")

    async with async_session() as session:
        await create_entry(
            session,
            user_id=user_id,
            category=captured.category,
            kind=EntryKind(captured.kind),
            title=captured.title,
            body=text,
            source_msg_id=msg_id,
            due_at=captured.due_at,
            tags=captured.tags,
            embedding=vector,
        )

    tz = ZoneInfo(settings.user_timezone)
    due_line = ""
    if captured.due_at is not None:
        due_local = captured.due_at.astimezone(tz)
        due_line = f" · due {due_local.strftime('%a %d %b')}"

    emoji = CATEGORY_EMOJI.get(captured.category.value, "📝")
    header = f"{emoji} {captured.category.value} · {captured.kind}{due_line}"
    return Reply(text=f"{header}\n{captured.title}")
