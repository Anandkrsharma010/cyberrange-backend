"""Workshop cohort email invites (single-use token, pending cap vs seats).

Revision ID: 0013
Revises: 0012
Create Date: 2026-05-04
"""

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS workshop_invites (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            workshop_id UUID NOT NULL REFERENCES workshops(id) ON DELETE CASCADE,
            email TEXT NOT NULL,
            token_hash TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL DEFAULT 'pending',
            invited_by UUID NOT NULL REFERENCES users(id),
            accepted_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
            accepted_at TIMESTAMPTZ,
            expires_at TIMESTAMPTZ NOT NULL,
            email_sent_at TIMESTAMPTZ,
            last_email_error TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT workshop_invites_status_check CHECK (
                status = ANY (ARRAY['pending','accepted','revoked','expired']::text[])
            )
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_workshop_invites_pending_email
            ON workshop_invites (workshop_id, lower(trim(email)))
            WHERE status = 'pending'
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_workshop_invites_workshop_id
            ON workshop_invites (workshop_id)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_workshop_invites_workshop_id")
    op.execute("DROP INDEX IF EXISTS uq_workshop_invites_pending_email")
    op.execute("DROP TABLE IF EXISTS workshop_invites")
