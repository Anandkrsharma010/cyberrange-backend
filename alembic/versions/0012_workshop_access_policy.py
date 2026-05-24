"""Workshop access_policy for grant rules (requires_payment vs demo).

Revision ID: 0012
Revises: 0011
Create Date: 2026-05-02
"""

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE workshops
            ADD COLUMN IF NOT EXISTS access_policy TEXT NOT NULL DEFAULT 'requires_payment'
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'workshops_access_policy_check'
            ) THEN
                ALTER TABLE workshops
                ADD CONSTRAINT workshops_access_policy_check
                CHECK (access_policy IN ('requires_payment', 'demo'));
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE workshops DROP CONSTRAINT IF EXISTS workshops_access_policy_check")
    op.execute("ALTER TABLE workshops DROP COLUMN IF EXISTS access_policy")
