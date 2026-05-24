"""Baseline — existing schema as of Phase 1 completion.

Covers all raw SQL migrations applied before Alembic was introduced:
  - db_schema.sql          (base schema)
  - 002                    (indexes + worker_status seed)
  - 003_deployment_members (deployment_members table)
  - 004_rename_roles       (student→participant, admin→sys_admin)
  - 005_token_audit_log    (token_audit_log table)

This migration is intentionally a no-op on upgrade and downgrade.
The schema already exists in the live database. Alembic just needs
to record that this revision has been applied.

Revision ID: 0001
Revises:
Create Date: 2026-04-03
"""

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Schema already exists — applied via raw SQL before Alembic was set up.
    pass


def downgrade() -> None:
    # No automated downgrade for the baseline.
    # To roll back to a clean DB, drop and recreate from db_schema.sql.
    pass