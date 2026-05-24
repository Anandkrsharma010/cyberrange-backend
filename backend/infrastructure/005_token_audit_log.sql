-- backend/infrastructure/005_token_audit_log.sql
CREATE TABLE IF NOT EXISTS token_audit_log (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id    UUID NOT NULL REFERENCES users(id),
    jti        TEXT NOT NULL,
    event      TEXT NOT NULL,
    ip_address TEXT,
    user_agent TEXT,
    created_at TIMESTAMPTZ DEFAULT now(),
    CONSTRAINT token_audit_log_event_check
        CHECK (event = ANY (ARRAY['issued', 'revoked', 'join_key_issued']))
);

CREATE INDEX IF NOT EXISTS idx_token_audit_user_id
    ON token_audit_log (user_id);

CREATE INDEX IF NOT EXISTS idx_token_audit_jti
    ON token_audit_log (jti);

CREATE INDEX IF NOT EXISTS idx_token_audit_created_at
    ON token_audit_log (created_at DESC);