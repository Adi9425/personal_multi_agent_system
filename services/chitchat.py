import re
from datetime import datetime
from zoneinfo import ZoneInfo

from app.config import settings
from core.schemas import Reply
from services import chitchat_constants as const

# Zero-LLM fast path for greetings/small talk. classify_intent's own prompt already buckets
# these as "unknown" (§ agents/notes/__init__.py:INTENT_SYSTEM_PROMPT) — this just answers
# them deterministically instead of spending an LLM call to land on the same flat "I'm not
# sure what you'd like me to do with that" reply for a message that was never meant to do
# anything in the first place.
_GREETING_PATTERN = re.compile(const.GREETING_PATTERN, re.IGNORECASE)
_ALIVE_PATTERN = re.compile(const.ALIVE_PATTERN, re.IGNORECASE)
_WHOAMI_PATTERN = re.compile(const.WHOAMI_PATTERN, re.IGNORECASE)
_HELP_PATTERN = re.compile(const.HELP_PATTERN, re.IGNORECASE)
_THANKS_PATTERN = re.compile(const.THANKS_PATTERN, re.IGNORECASE)
_BYE_PATTERN = re.compile(const.BYE_PATTERN, re.IGNORECASE)
_TIME_PATTERN = re.compile(const.TIME_PATTERN, re.IGNORECASE)


def try_handle(text: str) -> Reply | None:
    text = text.strip()

    if _GREETING_PATTERN.match(text):
        return Reply(text=const.GREETING_REPLY)
    if _ALIVE_PATTERN.match(text):
        return Reply(text=const.ALIVE_REPLY)
    if _WHOAMI_PATTERN.match(text) or _HELP_PATTERN.match(text):
        return Reply(text=const.ABOUT_REPLY)
    if _THANKS_PATTERN.match(text):
        return Reply(text=const.THANKS_REPLY)
    if _BYE_PATTERN.match(text):
        return Reply(text=const.BYE_REPLY)
    if _TIME_PATTERN.match(text):
        now = datetime.now(ZoneInfo(settings.user_timezone))
        return Reply(text=f"It's {now.strftime('%I:%M %p')} on {now.strftime('%a %d %b')} ({settings.user_timezone}).")
    return None
