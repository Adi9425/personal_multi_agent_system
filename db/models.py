import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import BigInteger, Computed, DateTime, ForeignKey, Index, Text, func, text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from sqlalchemy import Boolean, Integer

from app.config import settings
from core.schemas import EntryCategory, EntryKind, EntryStatus, EventType, UserStatus


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


class User(Base):
    """No FK from Entry.user_id to here — deliberate, see the multi-tenancy plan's privacy
    note: enforcing it at the DB level would need a data migration seeding the admin's row,
    putting a real person's Telegram ID in committed (public) migration history. Enforced at
    the application level instead (services/users.get_or_create_user runs before any
    entry-creating action reaches the data layer)."""

    __tablename__ = "users"

    telegram_user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    telegram_username: Mapped[str | None] = mapped_column(Text, nullable=True)  # display only, never an identity key
    is_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    status: Mapped[UserStatus] = mapped_column(
        SAEnum(UserStatus, name="user_status", values_callable=_enum_values),
        nullable=False,
        server_default=UserStatus.trial.value,
    )
    trial_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    trial_ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class UsageRecord(Base):
    """The trial-limit source of truth (services/users.check_access queries this directly —
    no separate counter to drift out of sync) and the raw per-call data for real cost
    analysis. Written once per real Anthropic/Voyage call, success or failure, by
    services/usage.py — the only writer, same single-choke-point pattern as D5."""

    __tablename__ = "usage_records"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)  # e.g. "classify-intent", "embed-capture"
    model: Mapped[str] = mapped_column(Text, nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (Index("usage_records_user_idx", "user_id", "created_at"),)


class UserKVNote(Base):
    """Deliberately separate from Entry — a flat fact ("save my linkedin profile: ...")
    has no category/kind/status/due_at, and forcing it through create_entry() would mean
    either faking those values or calling the LLM anyway, defeating the point of this
    zero-LLM fast path. services/kv_notes.py is the only writer."""

    __tablename__ = "user_kv_notes"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    key: Mapped[str] = mapped_column(Text, primary_key=True)  # normalized (lowercased, trimmed)
    value: Mapped[str] = mapped_column(Text, nullable=False)  # stored verbatim — casing matters
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
