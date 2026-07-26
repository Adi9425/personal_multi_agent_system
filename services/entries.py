from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.schemas import EntryCategory, EntryKind, EventType
from db.models import Entry, EntryEvent


async def create_entry(
    session: AsyncSession,
    *,
    user_id: int,
    category: EntryCategory,
    kind: EntryKind,
    title: str,
    body: str,
    source_msg_id: int | None,
    due_at: datetime | None = None,
    tags: list[str] | None = None,
    embedding: list[float] | None = None,
) -> Entry:
    """D5: this is the only function that writes to the entries table — and, for the
    matching audit trail, to entry_events (FR-9: every mutation, including creation,
    gets a diff row), in the same transaction."""
    entry = Entry(
        user_id=user_id,
        category=category,
        kind=kind,
        title=title,
        body=body,
        source_msg_id=source_msg_id,
        due_at=due_at,
        tags=tags or [],
        embedding=embedding,
    )
    session.add(entry)
    await session.flush()  # assigns entry.id without ending the transaction

    session.add(
        EntryEvent(
            entry_id=entry.id,
            event=EventType.created,
            changes={
                "category": {"from": None, "to": category.value},
                "kind": {"from": None, "to": kind.value},
                "title": {"from": None, "to": title},
                "due_at": {"from": None, "to": due_at.isoformat() if due_at else None},
                "tags": {"from": None, "to": tags or []},
            },
            source_msg_id=source_msg_id,
        )
    )
    await session.commit()
    await session.refresh(entry)
    return entry


async def list_recent(session: AsyncSession, *, user_id: int, limit: int = 10) -> list[Entry]:
    result = await session.execute(
        select(Entry).where(Entry.user_id == user_id).order_by(Entry.created_at.desc()).limit(limit)
    )
    return list(result.scalars().all())
