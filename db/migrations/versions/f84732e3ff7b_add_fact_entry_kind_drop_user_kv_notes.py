"""add fact entry_kind, drop user_kv_notes

Revision ID: f84732e3ff7b
Revises: e555560d211a
Create Date: 2026-07-31 02:09:45.848030

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f84732e3ff7b'
down_revision: Union[str, None] = 'e555560d211a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TYPE entry_kind ADD VALUE IF NOT EXISTS 'fact'")
    op.drop_table('user_kv_notes')


def downgrade() -> None:
    # Postgres has no DROP VALUE for enums — leaving 'fact' in the type on downgrade is
    # harmless (unused values are inert), so only the table recreation is reversible.
    op.create_table(
        'user_kv_notes',
        sa.Column('user_id', sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column('key', sa.Text(), nullable=False),
        sa.Column('value', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('user_id', 'key'),
    )
