-- migrations/003_deployment_members.sql
--
-- Adds deployment_members table to support multiple users sharing a single lab deployment.
--
-- Design:
-- - Admin deploys a lab (no entitlement check for admin)
-- - Admin adds students to the deployment via POST /admin/deployments/{id}/members/{user_id}
-- - Students can see deployments they've been added to in GET /labs/status
-- - Students join the tailnet via GET /labs/join/{deployment_id}
-- - A student can be a member of multiple active deployments simultaneously
-- - Student entitlements are still tracked for audit/billing purposes

CREATE TABLE IF NOT EXISTS deployment_members (
    deployment_id UUID NOT NULL REFERENCES lab_deployments(id) ON DELETE CASCADE,
    user_id       UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    added_by      UUID NOT NULL REFERENCES users(id),
    added_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (deployment_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_deployment_members_user_id
    ON deployment_members (user_id);

CREATE INDEX IF NOT EXISTS idx_deployment_members_deployment_id
    ON deployment_members (deployment_id);