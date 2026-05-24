"""Drop unique_lab_type_idx to allow multiple courses per lab type.

The original constraint enforced one content_items row per lab_type value
in metadata. This conflicts with the course admin model where multiple
courses (e.g. different batches) can share the same underlying Terraform
lab type (e.g. 'windows').

Revision ID: 0003
Revises: 0002
Create Date: 2026-04-06
"""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP INDEX IF EXISTS unique_lab_type_idx")


def downgrade() -> None:
    op.execute("""
        CREATE UNIQUE INDEX unique_lab_type_idx
            ON content_items (((metadata ->> 'lab_type')))
    """)