"""Billing: product_prices, billing_webhook_events, subscription_records, payments extensions.

Revision ID: 0004
Revises: 0003
Create Date: 2026-04-13
"""

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE product_prices (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            content_id      UUID NOT NULL REFERENCES content_items(id) ON DELETE CASCADE,
            amount_minor    INTEGER NOT NULL,
            currency        TEXT NOT NULL DEFAULT 'INR',
            is_active       BOOLEAN NOT NULL DEFAULT TRUE,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (content_id)
        )
    """)
    op.execute("""
        CREATE INDEX idx_product_prices_content_id ON product_prices (content_id)
    """)

    op.execute("""
        CREATE TABLE billing_webhook_events (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            gateway         TEXT NOT NULL,
            event_id        TEXT NOT NULL,
            payload         JSONB,
            processed_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (gateway, event_id)
        )
    """)
    op.execute("""
        CREATE INDEX idx_billing_webhook_events_gateway_event
            ON billing_webhook_events (gateway, event_id)
    """)

    op.execute("""
        CREATE TABLE subscription_records (
            id                          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id                     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            gateway                     TEXT NOT NULL DEFAULT 'razorpay',
            gateway_subscription_id     TEXT NOT NULL,
            status                      TEXT NOT NULL,
            current_period_end          TIMESTAMPTZ,
            content_id                  UUID REFERENCES content_items(id),
            raw_response                JSONB,
            created_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (gateway, gateway_subscription_id)
        )
    """)
    op.execute("""
        CREATE INDEX idx_subscription_records_user_id ON subscription_records (user_id)
    """)

    op.execute("""
        ALTER TABLE payments
            ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'one_time'
    """)
    op.execute("""
        ALTER TABLE payments
            ADD COLUMN IF NOT EXISTS gateway_subscription_id TEXT
    """)
    op.execute("""
        ALTER TABLE payments
            ADD CONSTRAINT payments_kind_check
            CHECK (kind IN ('one_time', 'subscription'))
    """)

    # Seed INR 5000 paise (₹50) for windows lab if present
    op.execute("""
        INSERT INTO product_prices (content_id, amount_minor, currency, is_active)
        SELECT id, 5000, 'INR', true
        FROM content_items
        WHERE type = 'lab' AND metadata->>'lab_type' = 'windows'
        LIMIT 1
        ON CONFLICT (content_id) DO NOTHING
    """)


def downgrade() -> None:
    op.execute("ALTER TABLE payments DROP CONSTRAINT IF EXISTS payments_kind_check")
    op.execute("ALTER TABLE payments DROP COLUMN IF EXISTS gateway_subscription_id")
    op.execute("ALTER TABLE payments DROP COLUMN IF EXISTS kind")
    op.execute("DROP TABLE IF EXISTS subscription_records")
    op.execute("DROP TABLE IF EXISTS billing_webhook_events")
    op.execute("DROP TABLE IF EXISTS product_prices")
