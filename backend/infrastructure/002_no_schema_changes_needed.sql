-- migrations/002_no_schema_changes_needed.sql
--
-- Token revocation: handled entirely in Redis — no DB schema changes.
-- Role authz: 'role' column already exists in the users table.
-- Health monitoring: worker_status table already exists.
--
-- This file is a no-op. It exists to document that the above features
-- were deliberately implemented without schema changes.
--
-- The only recommended addition is an index on headscale_keys if you
-- query by user_id frequently (already has user_id FK, add index if needed):

CREATE INDEX IF NOT EXISTS idx_headscale_keys_user_id
  ON headscale_keys (user_id);

-- And ensure the worker_status rows are pre-seeded so the health endpoint
-- doesn't return 'unknown' on first boot before workers have started:

INSERT INTO worker_status (id, last_seen)
VALUES
  ('lab_worker',         now() - interval '999 seconds'),
  ('lab_cleanup_worker', now() - interval '999 seconds')
ON CONFLICT (id) DO NOTHING;

-- The large negative offset ensures both workers appear 'stale' immediately
-- on a fresh deploy, prompting the health check to return 503 until the
-- workers actually start and post their first real heartbeat.