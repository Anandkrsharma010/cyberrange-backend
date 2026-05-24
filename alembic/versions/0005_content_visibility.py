"""Add content_items.visibility for lab discovery (public / unlisted / private).

Revision ID: 0005
Revises: 0004
Create Date: 2026-04-19
"""

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

_VISIBILITY_CHECK = """
    visibility IN ('public', 'unlisted', 'private')
"""


def upgrade() -> None:
    op.execute("""
        ALTER TABLE content_items
        ADD COLUMN IF NOT EXISTS visibility TEXT NOT NULL DEFAULT 'public'
    """)
    op.execute(f"""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'content_items_visibility_check'
            ) THEN
                ALTER TABLE content_items
                ADD CONSTRAINT content_items_visibility_check
                CHECK ({_VISIBILITY_CHECK.strip()});
            END IF;
        END $$;
    """)


def downgrade() -> None:
    op.execute(
        "ALTER TABLE content_items DROP CONSTRAINT IF EXISTS content_items_visibility_check"
    )
    op.execute("ALTER TABLE content_items DROP COLUMN IF EXISTS visibility")
