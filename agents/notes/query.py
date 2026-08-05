from datetime import datetime
from zoneinfo import ZoneInfo

from app.config import settings
from core.schemas import AnswerSynthesis, QueryPlan, Reply
from db.session import async_session
from services.embeddings import embed
from services.llm import call_structured
from services.search import hybrid_search

CATEGORY_EMOJI = {
    "office_work": "📁",
    "self_learning": "📚",
    "personal": "🏠",
    "ideas": "💡",
}


def _query_plan_system_prompt() -> str:
    tz = ZoneInfo(settings.user_timezone)
    now = datetime.now(tz)
    return (
        "You translate a natural-language question about the user's saved notes/tasks into "
        "structured filters.\n\n"
        "category: one of office_work, self_learning, personal, ideas, or null if not implied.\n"
        "status: one of open, done, archived, or null if not implied.\n"
        'due_before: an ISO datetime if the question implies a deadline window (e.g. "this '
        'week"), else null.\n'
        "completed_after: an ISO datetime if the question asks about recently finished items, "
        "else null.\n"
        "search_text: the core topic/keywords to search for, or null if the question is a "
        'pure filter with no topic (e.g. "what\'s open in office work").\n\n'
        f"Current date/time: {now.isoformat()} ({settings.user_timezone}). Resolve relative "
        'time phrases ("this week", "today") against this exact moment.'
    )


async def extract_plan(text: str, *, user_id: int | None = None) -> QueryPlan:
    return await call_structured(
        model=settings.model_fast,
        system=_query_plan_system_prompt(),
        user=text,
        response_model=QueryPlan,
        trace_name="query-extract-plan",
        user_id=user_id,
    )


def _format_source(row: dict) -> str:
    tz = ZoneInfo(settings.user_timezone)
    due = ""
    if row.get("due_at") is not None:
        due_local = row["due_at"].astimezone(tz)
        due = f" · due {due_local.strftime('%a %d %b')}"
    emoji = CATEGORY_EMOJI.get(row["category"], "📝")
    return f"{emoji} {row['title']} ({row['category']} · {row['status']}{due})"


async def query(*, user_id: int, text: str) -> Reply:
    """§6.3: structured filters as SQL, hybrid ranking only if a topic is present,
    answer synthesis with citations (FR-13)."""
    plan = await extract_plan(text, user_id=user_id)

    query_embedding = None
    if plan.search_text:
        query_embedding = await embed(plan.search_text, user_id=user_id, action="embed-query")

    async with async_session() as session:
        rows = await hybrid_search(session, user_id=user_id, plan=plan, query_embedding=query_embedding)

    if not rows:
        return Reply(text="I couldn't find anything matching that.")

    tz = ZoneInfo(settings.user_timezone)

    def _body_preview(body: str) -> str:
        # Bounded, not omitted — this is the only place the entry's actual content (a fact's
        # value, a note's real detail) ever reaches the synthesis call. Without it, a
        # correctly-found row still reads as empty to the model (found live: "no actual
        # password stored" for a fact whose value was sitting right there in `body`).
        body = body.strip().replace("\n", " ")
        return body if len(body) <= 300 else body[:300] + "..."

    rows_summary = "\n".join(
        f"{i}. {r['title']} | category={r['category']} | status={r['status']} | "
        f"due={r['due_at'].astimezone(tz).strftime('%a %d %b %H:%M') if r['due_at'] else 'none'} | "
        f"content={_body_preview(r['body'])}"
        for i, r in enumerate(rows, start=1)
    )
    now = datetime.now(tz)
    synthesis = await call_structured(
        model=settings.model_strong,
        system=(
            "Answer the user's question using ONLY the numbered entries below. One or two "
            "plain sentences, direct and conversational — no markdown, no bullet points, no "
            "numbered lists, no restating the entries one by one (they're shown to the user "
            "separately right after your answer, in a fixed format you don't control). If "
            "the entries don't actually answer the question, say so plainly.\n\n"
            f"Current date/time: {now.isoformat()} ({settings.user_timezone}). Compare each "
            "entry's due date against this exact moment — a due date before this moment is "
            'OVERDUE, not "coming up" or "nearest upcoming"; say so plainly (e.g. "overdue '
            'since Thu 30 Jul") rather than phrasing it like a future deadline.\n\n'
            f"Entries:\n{rows_summary}"
        ),
        user=text,
        response_model=AnswerSynthesis,
        trace_name="query-synthesize-answer",
        user_id=user_id,
    )

    # The entries list is always rendered here, deterministically — never left to the LLM's
    # own (inconsistent) formatting.
    sources = "\n".join(f"{i}. {_format_source(r)}" for i, r in enumerate(rows, start=1))
    return Reply(text=f"{synthesis.answer}\n\n{sources}")
