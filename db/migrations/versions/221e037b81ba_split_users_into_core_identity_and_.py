"""split users into core identity and profile, uuid user ids

One-way data migration: take a pg_dump first (see downgrade).

Revision ID: 221e037b81ba
Revises: 71e8b295a17d
Create Date: 2026-09-17 20:32:31.158062

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID


# revision identifiers, used by Alembic.
revision: str = '221e037b81ba'
down_revision: Union[str, None] = '71e8b295a17d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Runs as the table owner (the superuser role migrations always use — see
    # db/migrations/env.py). If entries/usage_records ever use FORCE ROW LEVEL SECURITY this
    # would need a BYPASSRLS role instead, or the backfills below would silently see zero rows
    # — not the case today (f1f3d4c93e50 doesn't force it), but worth remembering if that changes.

    # 1. Internal ID. gen_random_uuid() is volatile, so every existing row gets its own value.
    op.add_column(
        "users",
        sa.Column("id", UUID(as_uuid=True), nullable=False, server_default=sa.text("gen_random_uuid()")),
    )

    # 2. Users that exist only as entries/usage_records rows (pre-multi-tenancy data, or any
    #    row inserted straight against the DB rather than through get_or_create_user).
    #    Derived entirely from live rows, so no Telegram ID is ever written into migration
    #    history. Must run while telegram_user_id is still the PK (it's the conflict target).
    op.execute(
        """
        INSERT INTO users (telegram_user_id)
        SELECT user_id FROM entries
        UNION
        SELECT user_id FROM usage_records
        ON CONFLICT (telegram_user_id) DO NOTHING
        """
    )

    # 3. Swap the primary key. Nothing references users yet, so this is safe.
    op.drop_constraint("users_pkey", "users", type_="primary")
    op.create_primary_key("users_pkey", "users", ["id"])
    op.create_unique_constraint("users_telegram_user_id_key", "users", ["telegram_user_id"])

    # 4. Suspension + bot-blocked columns on the core table.
    op.add_column("users", sa.Column("suspended_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("users", sa.Column("suspension_reason", sa.Text(), nullable=True))
    op.add_column("users", sa.Column("bot_blocked_at", sa.DateTime(timezone=True), nullable=True))
    op.execute("UPDATE users SET suspended_at = updated_at WHERE status = 'suspended'")
    op.create_check_constraint(
        "ck_users_suspension_consistency",
        "users",
        "(status = 'suspended') = (suspended_at IS NOT NULL)",
    )

    # 5. Profile table, seeded from the column moving out of users.
    op.create_table(
        "user_profiles",
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("telegram_username", sa.Text(), nullable=True),
        sa.Column("first_name", sa.Text(), nullable=True),
        sa.Column("last_name", sa.Text(), nullable=True),
        sa.Column("display_name", sa.Text(), nullable=True),
        sa.Column("language", sa.Text(), nullable=False, server_default="en"),
        sa.Column("timezone", sa.Text(), nullable=False, server_default="UTC"),
        sa.Column("settings", JSONB(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.execute(
        "INSERT INTO user_profiles (user_id, telegram_username) SELECT id, telegram_username FROM users"
    )
    op.drop_column("users", "telegram_username")

    # 6. Repoint entries and usage_records from Telegram IDs to users.id.
    #
    #    RLS: f1f3d4c93e50's policies (entries_tenant_isolation, usage_records_tenant_isolation)
    #    depend on user_id's expression — Postgres refuses to drop a column a policy depends on
    #    ("cannot drop column user_id ... policy ... depends on column user_id"), so both are
    #    dropped here and recreated below casting against ::uuid instead of ::bigint.
    #
    #    searchable_items (71e8b295a17d) SELECTs entries.user_id directly, so it blocks the
    #    column drop the same way ("view searchable_items depends on column user_id") --
    #    confirmed live on a first attempt at this migration. Dropped and recreated verbatim
    #    (same definition as 71e8b295a17d) around the swap; the column's type change from
    #    bigint to uuid needs no changes to the view body itself.
    op.execute("DROP POLICY IF EXISTS entries_tenant_isolation ON entries")
    op.execute("DROP POLICY IF EXISTS usage_records_tenant_isolation ON usage_records")
    op.execute("DROP VIEW IF EXISTS searchable_items")

    for table in ("entries", "usage_records"):
        op.add_column(table, sa.Column("user_uuid", UUID(as_uuid=True), nullable=True))
        op.execute(
            f"UPDATE {table} AS t SET user_uuid = u.id "
            f"FROM users AS u WHERE u.telegram_user_id = t.user_id"
        )
        op.alter_column(table, "user_uuid", nullable=False)
        op.drop_column(table, "user_id")  # also drops entries_lookup_idx / usage_records_user_idx
        op.alter_column(table, "user_uuid", new_column_name="user_id")
        op.create_foreign_key(
            f"{table}_user_id_fkey", table, "users", ["user_id"], ["id"], ondelete="CASCADE"
        )

    op.create_index("entries_lookup_idx", "entries", ["user_id", "status", "category", "due_at"])
    op.create_index("usage_records_user_idx", "usage_records", ["user_id", "created_at"])

    op.execute("""
        CREATE VIEW searchable_items AS
        SELECT
          id,
          user_id,
          'notes'::text AS source,
          title,
          body,
          embedding,
          search_tsv,
          status::text AS status,
          category::text AS category,
          kind::text AS kind,
          due_at,
          created_at,
          updated_at
        FROM entries
        WHERE status <> 'archived'
    """)

    op.execute("""
        CREATE POLICY entries_tenant_isolation ON entries
        USING (
          nullif(current_setting('app.current_user_id', true), '') IS NOT NULL
          AND user_id = nullif(current_setting('app.current_user_id', true), '')::uuid
        )
        WITH CHECK (
          nullif(current_setting('app.current_user_id', true), '') IS NOT NULL
          AND user_id = nullif(current_setting('app.current_user_id', true), '')::uuid
        )
    """)
    op.execute("""
        CREATE POLICY usage_records_tenant_isolation ON usage_records
        USING (
          nullif(current_setting('app.current_user_id', true), '') IS NOT NULL
          AND user_id = nullif(current_setting('app.current_user_id', true), '')::uuid
        )
        WITH CHECK (
          nullif(current_setting('app.current_user_id', true), '') IS NOT NULL
          AND user_id = nullif(current_setting('app.current_user_id', true), '')::uuid
        )
    """)


def downgrade() -> None:
    raise NotImplementedError(
        "One-way migration: profile data and the Telegram-ID-to-UUID mapping can't be folded "
        "back cleanly. Restore from the pg_dump taken before upgrading."
    )
