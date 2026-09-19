"""add kind to searchable_items view

Revision ID: 71e8b295a17d
Revises: f1f3d4c93e50
Create Date: 2026-08-29 23:30:13.742773

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '71e8b295a17d'
down_revision: Union[str, None] = 'f1f3d4c93e50'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # searchable_items has never exposed `kind` (note/task/fact) since the view was first
    # created — QueryPlan/hybrid_search() have no way to filter by it even once the app-level
    # fields are added, because the column simply isn't there to select. Confirmed live: a
    # "pending task" query returns facts and notes too, since nothing downstream of this view
    # can tell them apart from a real task. Views run with the invoker's own privileges, so
    # the existing entries_tenant_isolation RLS policy on the base table already applies here
    # unchanged — nothing to redo on that front.
    op.execute("DROP VIEW searchable_items")
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


def downgrade() -> None:
    op.execute("DROP VIEW searchable_items")
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
          due_at,
          created_at,
          updated_at
        FROM entries
        WHERE status <> 'archived'
    """)
