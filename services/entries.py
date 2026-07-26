import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.schemas import EntryCategory, EntryKind, EntryStatus, EventType
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
    """D5: this is the only module that writes to the entries table — and, for the
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


async def _fetch_entry(session: AsyncSession, entry_id: uuid.UUID) -> Entry:
    entry = await session.get(Entry, entry_id)
    if entry is None:
        raise ValueError(f"entry {entry_id} not found")
    return entry


async def _apply_and_log(
    session: AsyncSession,
    entry: Entry,
    *,
    event: EventType,
    changes: dict,
    source_msg_id: int | None,
) -> tuple[Entry, dict]:
    entry.updated_at = datetime.now(timezone.utc)
    session.add(EntryEvent(entry_id=entry.id, event=event, changes=changes, source_msg_id=source_msg_id))
    await session.commit()
    await session.refresh(entry)
    return entry, changes


# FR-8: the six supported update operations. Each returns (entry, changes) — the changes
# dict is what gets logged to entry_events AND what an Undo token needs to reverse the
# mutation generically (see revert_entry below), so callers never re-derive it.


async def complete_entry(
    session: AsyncSession, *, entry_id: uuid.UUID, source_msg_id: int | None = None
) -> tuple[Entry, dict]:
    entry = await _fetch_entry(session, entry_id)
    changes = {"status": {"from": entry.status.value, "to": EntryStatus.done.value}}
    entry.status = EntryStatus.done
    return await _apply_and_log(session, entry, event=EventType.completed, changes=changes, source_msg_id=source_msg_id)


async def reopen_entry(
    session: AsyncSession, *, entry_id: uuid.UUID, source_msg_id: int | None = None
) -> tuple[Entry, dict]:
    entry = await _fetch_entry(session, entry_id)
    changes = {"status": {"from": entry.status.value, "to": EntryStatus.open.value}}
    entry.status = EntryStatus.open
    return await _apply_and_log(session, entry, event=EventType.reopened, changes=changes, source_msg_id=source_msg_id)


async def archive_entry(
    session: AsyncSession, *, entry_id: uuid.UUID, source_msg_id: int | None = None
) -> tuple[Entry, dict]:
    entry = await _fetch_entry(session, entry_id)
    changes = {"status": {"from": entry.status.value, "to": EntryStatus.archived.value}}
    entry.status = EntryStatus.archived
    return await _apply_and_log(session, entry, event=EventType.archived, changes=changes, source_msg_id=source_msg_id)


async def set_due_entry(
    session: AsyncSession, *, entry_id: uuid.UUID, due_at: datetime | None, source_msg_id: int | None = None
) -> tuple[Entry, dict]:
    entry = await _fetch_entry(session, entry_id)
    changes = {
        "due_at": {
            "from": entry.due_at.isoformat() if entry.due_at else None,
            "to": due_at.isoformat() if due_at else None,
        }
    }
    entry.due_at = due_at
    return await _apply_and_log(session, entry, event=EventType.updated, changes=changes, source_msg_id=source_msg_id)


async def add_tag_entry(
    session: AsyncSession, *, entry_id: uuid.UUID, tag: str, source_msg_id: int | None = None
) -> tuple[Entry, dict]:
    entry = await _fetch_entry(session, entry_id)
    old_tags = list(entry.tags)
    new_tags = old_tags if tag in old_tags else [*old_tags, tag]
    changes = {"tags": {"from": old_tags, "to": new_tags}}
    entry.tags = new_tags
    return await _apply_and_log(session, entry, event=EventType.updated, changes=changes, source_msg_id=source_msg_id)


async def retitle_entry(
    session: AsyncSession, *, entry_id: uuid.UUID, title: str, source_msg_id: int | None = None
) -> tuple[Entry, dict]:
    entry = await _fetch_entry(session, entry_id)
    changes = {"title": {"from": entry.title, "to": title}}
    entry.title = title
    return await _apply_and_log(session, entry, event=EventType.updated, changes=changes, source_msg_id=source_msg_id)


_FIELD_PARSERS = {
    "status": lambda v: EntryStatus(v) if v is not None else None,
    "due_at": lambda v: datetime.fromisoformat(v) if v is not None else None,
    "title": lambda v: v,
    "tags": lambda v: v if v is not None else [],
}


async def revert_entry(
    session: AsyncSession, *, entry_id: uuid.UUID, changes: dict, source_msg_id: int | None = None
) -> tuple[Entry, dict]:
    """Generic Undo: given the `changes` dict a prior mutation logged, set every field back
    to its "from" value and log a new event for the revert itself (FR-9 — Undo is its own
    mutation, so it gets its own entry_events row, per Phase 5's acceptance criterion)."""
    entry = await _fetch_entry(session, entry_id)
    reverted = {}
    for field, diff in changes.items():
        parser = _FIELD_PARSERS[field]
        old_value = parser(diff["from"])
        reverted[field] = {"from": diff["to"], "to": diff["from"]}
        setattr(entry, field, old_value)
    return await _apply_and_log(session, entry, event=EventType.updated, changes=reverted, source_msg_id=source_msg_id)
