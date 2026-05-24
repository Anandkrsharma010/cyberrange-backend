"""operations_feed: acknowledge / assign / escalation (Phase 3).

Revision ID: 0016
Revises: 0015
Create Date: 2026-05-04
"""

from alembic import op


revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE operations_feed
        ADD COLUMN IF NOT EXISTS acknowledged_at TIMESTAMPTZ,
        ADD COLUMN IF NOT EXISTS acknowledged_by UUID REFERENCES users(id) ON DELETE SET NULL,
        ADD COLUMN IF NOT EXISTS assigned_to_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
        ADD COLUMN IF NOT EXISTS escalation TEXT NOT NULL DEFAULT 'none'
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'operations_feed_escalation_chk'
            ) THEN
                ALTER TABLE operations_feed
                ADD CONSTRAINT operations_feed_escalation_chk
                CHECK (escalation = ANY (ARRAY['none','watch','urgent']));
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE operations_feed DROP CONSTRAINT IF EXISTS operations_feed_escalation_chk"
    )
    op.execute(
        """
        ALTER TABLE operations_feed
        DROP COLUMN IF EXISTS escalation,
        DROP COLUMN IF EXISTS assigned_to_user_id,
        DROP COLUMN IF EXISTS acknowledged_by,
        DROP COLUMN IF EXISTS acknowledged_at
        """
    )
