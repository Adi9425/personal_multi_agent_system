"""enable row-level security on entries and usage_records

Revision ID: f1f3d4c93e50
Revises: 807c09468d8a
Create Date: 2026-08-09 23:41:36.033588

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f1f3d4c93e50'
down_revision: Union[str, None] = '807c09468d8a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Postgres superusers (and any role with BYPASSRLS) ignore RLS policies entirely,
    # unconditionally -- verified live: this project's own app role ("notes", from
    # docker-compose.yml's POSTGRES_USER) is a superuser, since that's how the official
    # postgres image provisions its initial user. Writing RLS policies without first moving
    # the app's actual runtime connection off that role would ship policies that silently do
    # nothing. This migration creates a separate, unprivileged role for that connection;
    # migrations themselves keep running as the existing superuser role (unchanged).
    #
    # No password is set here on purpose -- secrets don't belong in a committed migration
    # file. After this migration runs, set one manually:
    #   ALTER ROLE notes_app PASSWORD 'choose-a-real-password';
    # then point APP_DATABASE_URL (see app/config.py) at notes_app instead of the superuser
    # role, using that password.
    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'notes_app') THEN
                CREATE ROLE notes_app LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
            END IF;
        END
        $$;
    """)
    op.execute("GRANT CONNECT ON DATABASE notes TO notes_app")
    op.execute("GRANT USAGE ON SCHEMA public TO notes_app")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO notes_app")
    op.execute("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO notes_app")
    # So a future migration's new table/sequence is usable by notes_app without a follow-up
    # GRANT -- migrations still run as the superuser role, which is what ALTER DEFAULT
    # PRIVILEGES here scopes to.
    op.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO notes_app")
    op.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO notes_app")

    # RLS itself. `current_setting(..., true)` returns NULL the very first time a custom GUC
    # is read in a session -- but verified live: once `app.current_user_id` has been SET
    # LOCAL at all (i.e. on every request after the connection's first), Postgres resets it
    # to '' (empty string), not NULL, when that transaction ends. Casting ''::bigint throws,
    # which is still safe (an error returns no rows either -- fails closed, not open) but not
    # the graceful "just see zero rows" behavior intended. nullif(..., '') normalizes both
    # "never set" and "reset to empty" to the same NULL case, so an unscoped session cleanly
    # matches zero rows either way instead of throwing.
    op.execute("ALTER TABLE entries ENABLE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY entries_tenant_isolation ON entries
        USING (
          nullif(current_setting('app.current_user_id', true), '') IS NOT NULL
          AND user_id = nullif(current_setting('app.current_user_id', true), '')::bigint
        )
        WITH CHECK (
          nullif(current_setting('app.current_user_id', true), '') IS NOT NULL
          AND user_id = nullif(current_setting('app.current_user_id', true), '')::bigint
        )
    """)

    op.execute("ALTER TABLE usage_records ENABLE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY usage_records_tenant_isolation ON usage_records
        USING (
          nullif(current_setting('app.current_user_id', true), '') IS NOT NULL
          AND user_id = nullif(current_setting('app.current_user_id', true), '')::bigint
        )
        WITH CHECK (
          nullif(current_setting('app.current_user_id', true), '') IS NOT NULL
          AND user_id = nullif(current_setting('app.current_user_id', true), '')::bigint
        )
    """)


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS usage_records_tenant_isolation ON usage_records")
    op.execute("ALTER TABLE usage_records DISABLE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS entries_tenant_isolation ON entries")
    op.execute("ALTER TABLE entries DISABLE ROW LEVEL SECURITY")
    # Role and grants deliberately left in place on downgrade -- dropping a role a live
    # connection might be using is a bigger footgun than leaving an unused, harmless
    # low-privilege role behind. Drop it by hand if it's genuinely no longer needed.
