"""Optional workshop description (public-facing blurb in create form).

Revision ID: 0010
Revises: 0009
Create Date: 2026-05-01
"""

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE workshops
            ADD COLUMN IF NOT EXISTS description TEXT
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE workshops DROP COLUMN IF EXISTS description")
