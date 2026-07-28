import logging

from app.config import settings
from core.schemas import IntentResult, Reply
from services.llm import LLMValidationError, call_structured

from agents.notes.capture import capture
from agents.notes.query import query
from agents.notes.resolve import resolve

logger = logging.getLogger(__name__)

INTENT_SYSTEM_PROMPT = (
    "Classify the user's message into exactly one intent, for a personal notes/tasks bot:\n\n"
    "- capture: the user is telling you something to remember — a new note, fact, task, or "
    "idea. This is the default for any statement of information, even with no explicit "
    '"note" or "remember" framing (e.g. "stateless auth tokens carry an expiry claim and '
    'get validated without a server-side session store" is capture, not a question and not '
    "unknown — it's a fact the user wants saved).\n"
    "- update: the user references an existing saved entry and wants it changed (e.g. \"mark "
    'the timesheet as done", "push the passport renewal to next month").\n'
    "- query: the user is asking a question ABOUT their existing saved entries (e.g. \"what "
    'do I have open this week", "notes about JWT").\n'
    "- unknown: only for greetings, small talk, or messages with no notes/tasks-related "
    'content at all (e.g. "hello", "lol").\n\n'
    "When genuinely unsure between capture and unknown, prefer capture — most messages sent "
    "to this bot are things the user wants saved.\n"
    "Respond with your confidence (0-1) and a short reason."
)


async def classify_intent(text: str, *, user_id: int | None = None) -> IntentResult:
    return await call_structured(
        model=settings.model_fast,
        system=INTENT_SYSTEM_PROMPT,
        user=text,
        response_model=IntentResult,
        trace_name="classify-intent",
        user_id=user_id,
    )


async def handle(user_id: int, text: str, msg_id: int) -> Reply:
    try:
        intent = await classify_intent(text, user_id=user_id)
    except LLMValidationError:
        logger.warning("intent classification failed validation twice for msg_id=%s", msg_id)
        return Reply(text="Sorry, I couldn't understand that — could you rephrase?")

    if intent.action == "capture":
        try:
            return await capture(user_id=user_id, text=text, msg_id=msg_id)
        except LLMValidationError:
            logger.warning("capture extraction failed validation twice for msg_id=%s", msg_id)
            return Reply(text="I had trouble understanding the details — could you rephrase?")

    if intent.action == "query":
        try:
            return await query(user_id=user_id, text=text)
        except LLMValidationError:
            logger.warning("query handling failed validation twice for msg_id=%s", msg_id)
            return Reply(text="I had trouble understanding that question — could you rephrase?")

    if intent.action == "update":
        try:
            return await resolve(user_id=user_id, text=text, msg_id=msg_id)
        except LLMValidationError:
            logger.warning("update resolution failed validation twice for msg_id=%s", msg_id)
            return Reply(text="I had trouble understanding that — could you rephrase?")

    # unknown
    return Reply(text="I'm not sure what you'd like me to do with that.")
