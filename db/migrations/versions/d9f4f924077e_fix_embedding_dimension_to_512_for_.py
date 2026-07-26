"""fix embedding dimension to 512 for voyage-3-lite

Revision ID: d9f4f924077e
Revises: ff4f4e539287
Create Date: 2026-07-26 16:04:00.839752

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd9f4f924077e'
down_revision: Union[str, None] = 'ff4f4e539287'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # voyage-3-lite's actual output is a fixed 512 dims, not the 1024 the HLD assumed
    # (confirmed against the live Voyage API). No data loss: entries.embedding is all NULL
    # so far (embeddings only start getting generated later in this phase).
    op.execute("DROP VIEW searchable_items")
    op.execute("DROP INDEX entries_embed_idx")
    op.execute("ALTER TABLE entries ALTER COLUMN embedding TYPE vector(512)")
    op.execute(
        "CREATE INDEX entries_embed_idx ON entries USING hnsw (embedding vector_cosine_ops)"
    )
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


def downgrade() -> None:
    op.execute("DROP VIEW searchable_items")
    op.execute("DROP INDEX entries_embed_idx")
    op.execute("ALTER TABLE entries ALTER COLUMN embedding TYPE vector(1024)")
    op.execute(
        "CREATE INDEX entries_embed_idx ON entries USING hnsw (embedding vector_cosine_ops)"
    )
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
