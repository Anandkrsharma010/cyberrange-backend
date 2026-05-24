"""operations_feed: read-state consistency CHECK.

Revision ID: 0015
Revises: 0014
Create Date: 2026-05-04
"""

from alembic import op


revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE operations_feed
        SET is_read = false, read_at = NULL, read_by = NULL
        WHERE is_read = true
          AND (read_at IS NULL OR read_by IS NULL)
        """
    )
    op.execute(
        """
        UPDATE operations_feed
        SET read_at = NULL, read_by = NULL
        WHERE is_read = false
          AND (read_at IS NOT NULL OR read_by IS NOT NULL)
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'operations_feed_read_state_chk'
            ) THEN
                ALTER TABLE operations_feed
                ADD CONSTRAINT operations_feed_read_state_chk
                CHECK (
                  (is_read = false AND read_at IS NULL AND read_by IS NULL)
                  OR
                  (is_read = true AND read_at IS NOT NULL AND read_by IS NOT NULL)
                );
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE operations_feed DROP CONSTRAINT IF EXISTS operations_feed_read_state_chk"
    )
