"""Content hardening: activity logs and page revisions.

Revision ID: 0008
Revises: 0007
Create Date: 2026-04-26
"""

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS content_activity_logs (
            id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            actor_user_id UUID REFERENCES users(id),
            entity_type  TEXT NOT NULL,
            entity_id    TEXT NOT NULL,
            action       TEXT NOT NULL,
            metadata     JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_content_activity_logs_created
            ON content_activity_logs (created_at DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_content_activity_logs_entity
            ON content_activity_logs (entity_type, entity_id, created_at DESC)
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS content_page_revisions (
            id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            page_id        UUID NOT NULL REFERENCES website_pages(id) ON DELETE CASCADE,
            snapshot       JSONB NOT NULL,
            reason         TEXT,
            created_by     UUID REFERENCES users(id),
            created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_content_page_revisions_page_created
            ON content_page_revisions (page_id, created_at DESC)
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS content_page_revisions")
    op.execute("DROP TABLE IF EXISTS content_activity_logs")

