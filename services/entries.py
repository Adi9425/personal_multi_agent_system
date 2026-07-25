from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Entry, EntryCategory, EntryKind


async def create_entry(
    session: AsyncSession,
    *,
    user_id: int,
    category: EntryCategory,
    kind: EntryKind,
    title: str,
    body: str,
    source_msg_id: int | None,
) -> Entry:
    """D5: this is the only function that writes to the entries table."""
    entry = Entry(
        user_id=user_id,
        category=category,
        kind=kind,
        title=title,
        body=body,
        source_msg_id=source_msg_id,
    )
    session.add(entry)
    await session.commit()
    await session.refresh(entry)
    return entry


async def list_recent(session: AsyncSession, *, user_id: int, limit: int = 10) -> list[Entry]:
    result = await session.execute(
        select(Entry).where(Entry.user_id == user_id).order_by(Entry.created_at.desc()).limit(limit)
    )
    return list(result.scalars().all())
