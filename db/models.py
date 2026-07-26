import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import BigInteger, Computed, DateTime, ForeignKey, Index, Text, func, text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.config import settings
from core.schemas import EntryCategory, EntryKind, EntryStatus, EventType


class Base(DeclarativeBase):
    pass


class ProcessedUpdate(Base):
    """Backs FR-17 (dedupe of Telegram update_id)."""

    __tablename__ = "processed_updates"

    update_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


def _enum_values(python_enum):
    return [member.value for member in python_enum]


class Entry(Base):
    """D5: only services/entries.py writes to this table."""

    __tablename__ = "entries"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    category: Mapped[EntryCategory] = mapped_column(
        SAEnum(EntryCategory, name="entry_category", values_callable=_enum_values), nullable=False
    )
    kind: Mapped[EntryKind] = mapped_column(
        SAEnum(EntryKind, name="entry_kind", values_callable=_enum_values), nullable=False
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)  # D3: immutable, set once at creation
    status: Mapped[EntryStatus] = mapped_column(
        SAEnum(EntryStatus, name="entry_status", values_callable=_enum_values),
        nullable=False,
        server_default=EntryStatus.open.value,
    )
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    tags: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default="{}")
    # Unpopulated until Phase 3 (embedding generation) — mapped now so Base.metadata matches
    # the real schema created by this migration.
    embedding: Mapped[list[float] | None] = mapped_column(Vector(settings.embedding_dim), nullable=True)
    source_msg_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # Postgres-generated column — application code never sets this.
    search_tsv: Mapped[str] = mapped_column(
        TSVECTOR,
        Computed(
            "setweight(to_tsvector('english', coalesce(title, '')), 'A') || "
            "setweight(to_tsvector('english', coalesce(body, '')), 'B')",
            persisted=True,
        ),
        nullable=False,
    )

    __table_args__ = (
        Index("entries_tsv_idx", "search_tsv", postgresql_using="gin"),
        Index(
            "entries_embed_idx",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        Index("entries_lookup_idx", "user_id", "status", "category", "due_at"),
        Index("entries_tags_idx", "tags", postgresql_using="gin"),
    )


class EntryEvent(Base):
    """Schema exists from Phase 2 onward (HLD section 4), but nothing writes here until
    Phase 5's update/undo path. Modeled now so Alembic's autogenerate (which diffs the live
    DB against Base.metadata) doesn't mistake this table for one that should be dropped."""

    __tablename__ = "entry_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    entry_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("entries.id", ondelete="CASCADE"), nullable=False
    )
    event: Mapped[EventType] = mapped_column(
        SAEnum(EventType, name="event_type", values_callable=_enum_values), nullable=False
    )
    changes: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    source_msg_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (Index("entry_events_entry_idx", "entry_id", "created_at"),)
