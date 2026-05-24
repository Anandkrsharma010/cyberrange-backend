"""Keep workshops.used_seats in sync with active cohort entitlements.

Revision ID: 0011
Revises: 0010
Create Date: 2026-05-01
"""

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE workshops w
        SET used_seats = (
            SELECT COUNT(*)::int
            FROM entitlements e
            WHERE e.workshop_id = w.id AND e.status = 'active'
        )
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION entitlements_refresh_workshop_used_seats()
        RETURNS trigger AS $$
        DECLARE
            wids uuid[];
            w uuid;
        BEGIN
            IF TG_OP = 'DELETE' THEN
                wids := ARRAY[OLD.workshop_id];
            ELSIF TG_OP = 'UPDATE' THEN
                wids := ARRAY[OLD.workshop_id, NEW.workshop_id];
            ELSE
                wids := ARRAY[NEW.workshop_id];
            END IF;

            FOREACH w IN ARRAY wids
            LOOP
                IF w IS NOT NULL THEN
                    UPDATE workshops SET
                        used_seats = (
                            SELECT COUNT(*)::int
                            FROM entitlements e
                            WHERE e.workshop_id = w AND e.status = 'active'
                        ),
                        updated_at = now()
                    WHERE id = w;
                END IF;
            END LOOP;

            RETURN COALESCE(NEW, OLD);
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute("DROP TRIGGER IF EXISTS trg_entitlements_refresh_workshop_used_seats ON entitlements")
    op.execute(
        """
        CREATE TRIGGER trg_entitlements_refresh_workshop_used_seats
        AFTER INSERT OR UPDATE OR DELETE ON entitlements
        FOR EACH ROW
        EXECUTE PROCEDURE entitlements_refresh_workshop_used_seats()
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_entitlements_refresh_workshop_used_seats ON entitlements"
    )
    op.execute("DROP FUNCTION IF EXISTS entitlements_refresh_workshop_used_seats()")
