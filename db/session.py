from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings

# Every application code path uses this engine -- it's the ONLY thing in this module,
# deliberately. Alembic (db/migrations/env.py) builds its own connection straight from
# settings.database_url (the superuser role) and never imports this module, so repointing
# this at settings.effective_app_database_url (the restricted notes_app role once
# APP_DATABASE_URL is configured -- see f1f3d4c93e50's migration comment) doesn't touch
# migrations at all, and every existing call site (services/, agents/, workers/) picks up
# the restricted connection automatically, with no per-file changes needed.
engine = create_async_engine(settings.effective_app_database_url)
async_session = async_sessionmaker(engine, expire_on_commit=False)


async def get_session() -> AsyncSession:
    async with async_session() as session:
        yield session


@asynccontextmanager
async def tenant_session(user_id: int) -> AsyncIterator[AsyncSession]:
    """The tenant-scoped entry point for anything touching a specific user's data. Issues
    `SET LOCAL app.current_user_id` as the first statement in the transaction -- scoped to
    that transaction only (never bare SET, which would leak across a pooled connection to a
    different request) -- so the Row-Level Security policies on `entries`/`usage_records`
    (f1f3d4c93e50) actually have a tenant to enforce against. This is a backstop underneath
    the existing explicit `WHERE user_id = ...` clauses in services/entries.py etc., not a
    replacement for them -- those stay exactly as they are.

    Only effective once settings.app_database_url points at the restricted notes_app role;
    against the superuser fallback, RLS (and therefore this) is a harmless no-op, since
    superusers bypass RLS unconditionally regardless of the session variable.

    Uses set_config(), not a literal `SET LOCAL app.current_user_id = :user_id` -- verified
    live that Postgres's SET statement doesn't accept bind parameters for its value at all
    (it's a utility statement, not regular DML); set_config() is a real function call and
    does. The third argument (true) makes it transaction-scoped, the same as SET LOCAL."""
    async with async_session() as session:
        await session.execute(text("SELECT set_config('app.current_user_id', :user_id, true)"), {"user_id": str(user_id)})
        yield session
