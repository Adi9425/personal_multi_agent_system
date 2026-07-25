from datetime import datetime

from pydantic import BaseModel


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
