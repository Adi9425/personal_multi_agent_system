import logging

from app.config import settings
from core.schemas import IntentResult, Reply
from services.llm import LLMValidationError, call_structured

from agents.notes.capture import capture

logger = logging.getLogger(__name__)

INTENT_SYSTEM_PROMPT = (
    "Classify the user's message into exactly one intent:\n"
    "- capture: they want to save a new note or task.\n"
    "- update: they want to modify or complete an existing entry.\n"
    "- query: they're asking a question about existing entries.\n"
    "- unknown: none of the above clearly apply.\n"
    "Respond with your confidence (0-1) and a short reason."
)


async def classify_intent(text: str) -> IntentResult:
    return await call_structured(
        model=settings.model_fast,
        system=INTENT_SYSTEM_PROMPT,
        user=text,
        response_model=IntentResult,
        trace_name="classify-intent",
    )


async def handle(user_id: int, text: str, msg_id: int) -> Reply:
    try:
        intent = await classify_intent(text)
    except LLMValidationError:
        logger.warning("intent classification failed validation twice for msg_id=%s", msg_id)
        return Reply(text="Sorry, I couldn't understand that — could you rephrase?")

    if intent.action == "capture":
        try:
            return await capture(user_id=user_id, text=text, msg_id=msg_id)
        except LLMValidationError:
            logger.warning("capture extraction failed validation twice for msg_id=%s", msg_id)
            return Reply(text="I had trouble understanding the details — could you rephrase?")

    if intent.action == "unknown":
        return Reply(text="I'm not sure what you'd like me to do with that.")

    # update / query — not built until Phases 4/5.
    return Reply(text=f"I can't handle {intent.action}s yet — that's coming in a later phase.")
