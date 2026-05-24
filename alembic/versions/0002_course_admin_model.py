"""Course admin model — assignments, guardrails, course participants.

Revision ID: 0002
Revises: 0001
Create Date: 2026-04-06
"""
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE course_admin_assignments (
            user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            content_id  UUID NOT NULL REFERENCES content_items(id) ON DELETE CASCADE,
            assigned_by UUID NOT NULL REFERENCES users(id),
            assigned_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (user_id, content_id)
        )
    """)

    op.execute("""
        CREATE INDEX idx_course_admin_assignments_content_id
            ON course_admin_assignments (content_id)
    """)

    op.execute("""
        CREATE TABLE course_guardrails (
            id                         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            course_admin_id            UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            content_id                 UUID NOT NULL REFERENCES content_items(id) ON DELETE CASCADE,
            max_concurrent_deployments INT NOT NULL DEFAULT 10,
            max_duration_hours         INT NOT NULL DEFAULT 4,
            set_by                     UUID NOT NULL REFERENCES users(id),
            updated_at                 TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (course_admin_id, content_id)
        )
    """)

    op.execute("""
        CREATE TABLE course_participants (
            user_id       UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            content_id    UUID NOT NULL REFERENCES content_items(id) ON DELETE CASCADE,
            enrolled_by   UUID NOT NULL REFERENCES users(id),
            enrolled_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (user_id, content_id)
        )
    """)

    op.execute("""
        CREATE INDEX idx_course_participants_content_id
            ON course_participants (content_id)
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS course_participants")
    op.execute("DROP TABLE IF EXISTS course_guardrails")
    op.execute("DROP TABLE IF EXISTS course_admin_assignments")