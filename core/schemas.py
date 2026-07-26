import enum
from datetime import datetime, timedelta, timezone
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class EntryCategory(str, enum.Enum):
    office_work = "office_work"
    self_learning = "self_learning"
    personal = "personal"
    ideas = "ideas"


class EntryKind(str, enum.Enum):
    note = "note"
    task = "task"


class EntryStatus(str, enum.Enum):
    open = "open"
    done = "done"
    archived = "archived"


class EventType(str, enum.Enum):
    created = "created"
    updated = "updated"
    completed = "completed"
    reopened = "reopened"
    archived = "archived"


class JobPayload(BaseModel):
    update_id: int
    user_id: int
    chat_id: int
    msg_id: int
    text: str
    conversation_id: str  # D10 — derived from chat_id for now; unused downstream
    enqueued_at: datetime


class Button(BaseModel):
    label: str
    callback_data: str


class Reply(BaseModel):
    text: str
    buttons: list[Button] | None = None
    attachments: list[str] | None = None


class IntentResult(BaseModel):
    action: Literal["capture", "update", "query", "unknown"]
    confidence: float = Field(ge=0, le=1)
    reasoning: str = Field(max_length=200)


class CapturedEntry(BaseModel):
    category: EntryCategory
    kind: EntryKind
    title: str = Field(max_length=80)  # short, human-scannable, used for matching
    tags: list[str] = Field(default_factory=list, max_length=5)
    due_at: datetime | None = None
    due_source_text: str | None = None  # the raw phrase, e.g. "next Friday" — for audit

    @field_validator("due_at")
    @classmethod
    def _validate_due_at(cls, value: datetime | None) -> datetime | None:
        # §5 prompting rule: these are almost always parse errors, not real intent.
        if value is None:
            return value
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        if value > now + timedelta(days=365 * 2):
            raise ValueError("due_at is more than 2 years in the future")
        if value < now - timedelta(days=1):
            raise ValueError("due_at is more than 1 day in the past")
        return value


class QueryPlan(BaseModel):
    search_text: str | None = None
    category: EntryCategory | None = None
    status: EntryStatus | None = None
    due_before: datetime | None = None
    completed_after: datetime | None = None
    limit: int = 10


class AnswerSynthesis(BaseModel):
    answer: str = Field(max_length=500)


class UpdateRequest(BaseModel):
    target_hint: str  # the phrase identifying which entry
    operation: Literal["complete", "reopen", "set_due", "add_tag", "retitle", "archive"]
    value: str | None = None  # new due date (ISO) / tag / title, if applicable


class CallbackPayload(BaseModel):
    callback_query_id: str
    user_id: int
    chat_id: int
    message_id: int
    token: str  # lookup key into services/pending_actions.py's Redis store
    conversation_id: str  # D10 — unused downstream, same as JobPayload
