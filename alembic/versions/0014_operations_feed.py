"""Operations feed inbox for sys_admin.

Revision ID: 0014
Revises: 0013
Create Date: 2026-05-04
"""

from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS operations_feed (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            event_key TEXT NOT NULL UNIQUE,
            event_type TEXT NOT NULL,
            severity TEXT NOT NULL DEFAULT 'info',
            title TEXT NOT NULL,
            message TEXT NOT NULL,
            actor_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
            actor_email TEXT,
            subject_type TEXT,
            subject_id TEXT,
            workshop_id UUID REFERENCES workshops(id) ON DELETE SET NULL,
            deployment_id UUID REFERENCES lab_deployments(id) ON DELETE SET NULL,
            target_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
            deep_link TEXT,
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
            is_read BOOLEAN NOT NULL DEFAULT FALSE,
            read_at TIMESTAMPTZ,
            read_by UUID REFERENCES users(id) ON DELETE SET NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT operations_feed_severity_check
              CHECK (severity = ANY (ARRAY['info','warning','critical']))
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_operations_feed_unread_created
          ON operations_feed (is_read, created_at DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_operations_feed_severity_created
          ON operations_feed (severity, created_at DESC)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_operations_feed_severity_created")
    op.execute("DROP INDEX IF EXISTS idx_operations_feed_unread_created")
    op.execute("DROP TABLE IF EXISTS operations_feed")

