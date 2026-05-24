"""Workshops: cohort header, course-admin assignment; entitlements.workshop_id; lab_deployments.workshop_id.

Revision ID: 0009
Revises: 0008
Create Date: 2026-05-01
"""

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS workshops (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            internal_code   TEXT UNIQUE,
            title           TEXT NOT NULL,
            content_id      UUID NOT NULL REFERENCES content_items(id) ON DELETE RESTRICT,
            start_at        TIMESTAMPTZ,
            end_at          TIMESTAMPTZ,
            mode            TEXT NOT NULL,
            seat_cap        INTEGER NOT NULL,
            used_seats      INTEGER NOT NULL DEFAULT 0,
            payment_status  TEXT NOT NULL DEFAULT 'pending',
            payment_id      UUID REFERENCES payments(id) ON DELETE SET NULL,
            payer_ref       TEXT,
            status          TEXT NOT NULL DEFAULT 'draft',
            created_by      UUID REFERENCES users(id) ON DELETE SET NULL,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT workshops_mode_check
                CHECK (mode IN ('sponsored', 'open_organizer')),
            CONSTRAINT workshops_seat_cap_check
                CHECK (seat_cap >= 0),
            CONSTRAINT workshops_used_seats_check
                CHECK (used_seats >= 0),
            CONSTRAINT workshops_payment_status_check
                CHECK (payment_status IN ('pending', 'paid', 'waived', 'refunded')),
            CONSTRAINT workshops_status_check
                CHECK (status IN ('draft', 'active', 'archived'))
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_workshops_content_id ON workshops (content_id)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_workshops_status ON workshops (status)
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS workshop_course_admins (
            id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            workshop_id  UUID NOT NULL REFERENCES workshops(id) ON DELETE CASCADE,
            user_id      UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            is_lead      BOOLEAN NOT NULL DEFAULT FALSE,
            created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (workshop_id, user_id)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_workshop_course_admins_user_id ON workshop_course_admins (user_id)
        """
    )

    op.execute(
        """
        ALTER TABLE entitlements
            ADD COLUMN IF NOT EXISTS workshop_id UUID REFERENCES workshops(id) ON DELETE SET NULL
        """
    )
    op.execute(
        """
        ALTER TABLE entitlements DROP CONSTRAINT IF EXISTS entitlements_user_id_content_id_key
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS entitlements_user_content_individual
            ON entitlements (user_id, content_id)
            WHERE workshop_id IS NULL
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS entitlements_user_content_workshop
            ON entitlements (user_id, content_id, workshop_id)
            WHERE workshop_id IS NOT NULL
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_entitlements_workshop_id
            ON entitlements (workshop_id)
            WHERE workshop_id IS NOT NULL
        """
    )

    op.execute(
        """
        ALTER TABLE lab_deployments
            ADD COLUMN IF NOT EXISTS workshop_id UUID REFERENCES workshops(id) ON DELETE SET NULL
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_lab_deployments_workshop_id
            ON lab_deployments (workshop_id)
            WHERE workshop_id IS NOT NULL
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_lab_deployments_workshop_id")
    op.execute(
        "ALTER TABLE lab_deployments DROP COLUMN IF EXISTS workshop_id"
    )

    op.execute("DROP INDEX IF EXISTS idx_entitlements_workshop_id")
    op.execute("DROP INDEX IF EXISTS entitlements_user_content_workshop")
    op.execute("DROP INDEX IF EXISTS entitlements_user_content_individual")

    op.execute(
        "ALTER TABLE entitlements DROP COLUMN IF EXISTS workshop_id"
    )
    op.execute(
        """
        ALTER TABLE entitlements
            ADD CONSTRAINT entitlements_user_id_content_id_key UNIQUE (user_id, content_id)
        """
    )

    op.execute("DROP TABLE IF EXISTS workshop_course_admins")
    op.execute("DROP TABLE IF EXISTS workshops")
