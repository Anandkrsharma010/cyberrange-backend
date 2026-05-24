-- CyberRange Postgres Schema (Canonical Reference)
-- This file should reflect the LIVE database schema.

-- ------------------------------------------------------------
-- Extensions
-- ------------------------------------------------------------
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ------------------------------------------------------------
-- Sequences
-- ------------------------------------------------------------
CREATE SEQUENCE IF NOT EXISTS user_id_seq START 10;

-- ------------------------------------------------------------
-- Users
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  sso_provider TEXT NOT NULL,
  sso_subject  TEXT NOT NULL,
  email        TEXT NOT NULL UNIQUE,
  name         TEXT,
  role         TEXT NOT NULL,
  is_active    BOOLEAN DEFAULT TRUE,
  created_at   TIMESTAMPTZ DEFAULT now(),
  updated_at   TIMESTAMPTZ DEFAULT now(),
  CONSTRAINT users_role_check
    CHECK (role = ANY (ARRAY['student','instructor','admin'])),
  CONSTRAINT users_sso_provider_sso_subject_key
    UNIQUE (sso_provider, sso_subject)
);

-- ------------------------------------------------------------
-- Content Items
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS content_items (
  id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  type             TEXT NOT NULL,
  title            TEXT NOT NULL,
  description      TEXT,
  difficulty       TEXT,
  duration_minutes INTEGER,
  is_active        BOOLEAN DEFAULT TRUE,
  metadata         JSONB,
  visibility       TEXT NOT NULL DEFAULT 'public',
  created_at       TIMESTAMPTZ DEFAULT now(),
  CONSTRAINT content_items_type_check
    CHECK (type = ANY (ARRAY['lab','quiz'])),
  CONSTRAINT content_items_visibility_check
    CHECK (visibility = ANY (ARRAY['public','unlisted','private']))
);

-- ------------------------------------------------------------
-- Website Content Studio
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS website_pages (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  slug            TEXT NOT NULL UNIQUE,
  title           TEXT NOT NULL,
  description     TEXT,
  status          TEXT NOT NULL DEFAULT 'draft',
  seo_title       TEXT,
  seo_description TEXT,
  created_by      UUID REFERENCES users(id),
  updated_by      UUID REFERENCES users(id),
  published_at    TIMESTAMPTZ,
  archived_at     TIMESTAMPTZ,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT website_pages_status_check
    CHECK (status = ANY (ARRAY['draft','published','archived']))
);

CREATE INDEX IF NOT EXISTS idx_website_pages_status_updated
  ON website_pages (status, updated_at DESC);

CREATE TABLE IF NOT EXISTS website_page_sections (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  page_id      UUID NOT NULL REFERENCES website_pages(id) ON DELETE CASCADE,
  section_key  TEXT NOT NULL,
  section_type TEXT NOT NULL,
  position     INTEGER NOT NULL DEFAULT 0,
  is_visible   BOOLEAN NOT NULL DEFAULT true,
  payload      JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_by   UUID REFERENCES users(id),
  updated_by   UUID REFERENCES users(id),
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT website_page_sections_unique_position UNIQUE (page_id, position)
);

CREATE INDEX IF NOT EXISTS idx_website_page_sections_page_position
  ON website_page_sections (page_id, position);

CREATE TABLE IF NOT EXISTS course_resources (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  content_id    UUID NOT NULL REFERENCES content_items(id) ON DELETE CASCADE,
  title         TEXT NOT NULL,
  description   TEXT,
  resource_type TEXT NOT NULL,
  url           TEXT,
  file_key      TEXT,
  mime_type     TEXT,
  position      INTEGER NOT NULL DEFAULT 0,
  is_visible    BOOLEAN NOT NULL DEFAULT true,
  metadata      JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_by    UUID REFERENCES users(id),
  updated_by    UUID REFERENCES users(id),
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT course_resources_type_check
    CHECK (resource_type = ANY (ARRAY['text','link','pdf','file','manual'])),
  CONSTRAINT course_resources_unique_position UNIQUE (content_id, position)
);

CREATE INDEX IF NOT EXISTS idx_course_resources_content_position
  ON course_resources (content_id, position);

-- ------------------------------------------------------------
-- Content Governance
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS content_activity_logs (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  actor_user_id UUID REFERENCES users(id),
  entity_type   TEXT NOT NULL,
  entity_id     TEXT NOT NULL,
  action        TEXT NOT NULL,
  metadata      JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_content_activity_logs_created
  ON content_activity_logs (created_at DESC);

CREATE INDEX IF NOT EXISTS idx_content_activity_logs_entity
  ON content_activity_logs (entity_type, entity_id, created_at DESC);

-- -- ------------------------------------------------------------
-- Payments
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS payments (
  id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id            UUID NOT NULL REFERENCES users(id),
  gateway            TEXT NOT NULL,
  gateway_order_id   TEXT NOT NULL UNIQUE,
  gateway_payment_id TEXT,
  amount             INTEGER NOT NULL,
  currency           TEXT NOT NULL,
  status             TEXT NOT NULL,
  raw_response       JSONB,
  created_at         TIMESTAMPTZ DEFAULT now()
);

-- ------------------------------------------------------------
-- Workshops (cohorts — Mode A sponsored seats, Mode B organizer-led)
--
-- Mapping note: `workshop_course_admins` is the membership table for “cohorts this
-- user operates” in the course-admin product surface. Per-course managed lab duties
-- (UUID roster + queued runs) live in `course_admin_assignments` / `course_participants`
-- (see Alembic 0002); those tables are a separate path and must not be implied by
-- workshop rows unless explicitly migrated into a cohort model.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS workshops (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  internal_code   TEXT UNIQUE,
  title           TEXT NOT NULL,
  description     TEXT,
  content_id      UUID NOT NULL REFERENCES content_items(id) ON DELETE RESTRICT,
  start_at        TIMESTAMPTZ,
  end_at          TIMESTAMPTZ,
  mode            TEXT NOT NULL,
  seat_cap        INTEGER NOT NULL,
  -- Cohort seats in use: active entitlements with this workshop_id (see Alembic 0011 trigger).
  used_seats      INTEGER NOT NULL DEFAULT 0,
  payment_status  TEXT NOT NULL DEFAULT 'pending',
  payment_id      UUID REFERENCES payments(id) ON DELETE SET NULL,
  payer_ref       TEXT,
  access_policy   TEXT NOT NULL DEFAULT 'requires_payment',
  status          TEXT NOT NULL DEFAULT 'draft',
  created_by      UUID REFERENCES users(id) ON DELETE SET NULL,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT workshops_mode_check
    CHECK (mode = ANY (ARRAY['sponsored','open_organizer'])),
  CONSTRAINT workshops_seat_cap_check
    CHECK (seat_cap >= 0),
  CONSTRAINT workshops_used_seats_check
    CHECK (used_seats >= 0),
  CONSTRAINT workshops_payment_status_check
    CHECK (payment_status = ANY (ARRAY['pending','paid','waived','refunded'])),
  CONSTRAINT workshops_status_check
    CHECK (status = ANY (ARRAY['draft','active','archived'])),
  CONSTRAINT workshops_access_policy_check
    CHECK (access_policy = ANY (ARRAY['requires_payment','demo']))
);

CREATE INDEX IF NOT EXISTS idx_workshops_content_id ON workshops (content_id);
CREATE INDEX IF NOT EXISTS idx_workshops_status ON workshops (status);

-- ------------------------------------------------------------
-- System administration operations feed (inbox)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS operations_feed (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  event_key     TEXT NOT NULL UNIQUE,
  event_type    TEXT NOT NULL,
  severity      TEXT NOT NULL DEFAULT 'info',
  title         TEXT NOT NULL,
  message       TEXT NOT NULL,
  actor_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
  actor_email   TEXT,
  subject_type  TEXT,
  subject_id    TEXT,
  workshop_id   UUID REFERENCES workshops(id) ON DELETE SET NULL,
  deployment_id UUID,
  target_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
  deep_link     TEXT,
  metadata      JSONB NOT NULL DEFAULT '{}'::jsonb,
  acknowledged_at TIMESTAMPTZ,
  acknowledged_by UUID REFERENCES users(id) ON DELETE SET NULL,
  assigned_to_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
  escalation    TEXT NOT NULL DEFAULT 'none',
  is_read       BOOLEAN NOT NULL DEFAULT FALSE,
  read_at       TIMESTAMPTZ,
  read_by       UUID REFERENCES users(id) ON DELETE SET NULL,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT operations_feed_severity_check
    CHECK (severity = ANY (ARRAY['info','warning','critical'])),
  CONSTRAINT operations_feed_escalation_chk
    CHECK (escalation = ANY (ARRAY['none','watch','urgent'])),
  CONSTRAINT operations_feed_read_state_chk
    CHECK (
      (is_read = false AND read_at IS NULL AND read_by IS NULL)
      OR
      (is_read = true AND read_at IS NOT NULL AND read_by IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_operations_feed_unread_created
  ON operations_feed (is_read, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_operations_feed_severity_created
  ON operations_feed (severity, created_at DESC);

CREATE TABLE IF NOT EXISTS content_page_revisions (
  id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  page_id    UUID NOT NULL REFERENCES website_pages(id) ON DELETE CASCADE,
  snapshot   JSONB NOT NULL,
  reason     TEXT,
  created_by UUID REFERENCES users(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_content_page_revisions_page_created
  ON content_page_revisions (page_id, created_at DESC);

-- ------------------------------------------------------------
-- Purchases
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS purchases (
  id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id    UUID NOT NULL REFERENCES users(id),
  content_id UUID NOT NULL REFERENCES content_items(id),
  payment_id UUID NOT NULL REFERENCES payments(id),
  created_at TIMESTAMPTZ DEFAULT now(),
  CONSTRAINT purchases_user_id_content_id_key
    UNIQUE (user_id, content_id)
);

-- Operators allowed to manage this cohort (invites, seats, payment UX, etc.).
CREATE TABLE IF NOT EXISTS workshop_course_admins (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  workshop_id  UUID NOT NULL REFERENCES workshops(id) ON DELETE CASCADE,
  user_id      UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  is_lead      BOOLEAN NOT NULL DEFAULT FALSE,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (workshop_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_workshop_course_admins_user_id ON workshop_course_admins (user_id);

CREATE TABLE IF NOT EXISTS workshop_invites (
  id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  workshop_id        UUID NOT NULL REFERENCES workshops(id) ON DELETE CASCADE,
  email              TEXT NOT NULL,
  token_hash         TEXT NOT NULL UNIQUE,
  status             TEXT NOT NULL DEFAULT 'pending',
  invited_by         UUID NOT NULL REFERENCES users(id),
  accepted_user_id   UUID REFERENCES users(id) ON DELETE SET NULL,
  accepted_at        TIMESTAMPTZ,
  expires_at         TIMESTAMPTZ NOT NULL,
  email_sent_at      TIMESTAMPTZ,
  last_email_error   TEXT,
  created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT workshop_invites_status_check CHECK (
    status = ANY (ARRAY['pending','accepted','revoked','expired']::text[])
  )
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_workshop_invites_pending_email
  ON workshop_invites (workshop_id, lower(trim(email)))
  WHERE status = 'pending';

CREATE INDEX IF NOT EXISTS idx_workshop_invites_workshop_id
  ON workshop_invites (workshop_id);

-- ------------------------------------------------------------
-- Entitlements
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS entitlements (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id     UUID NOT NULL REFERENCES users(id),
  content_id  UUID NOT NULL REFERENCES content_items(id),
  workshop_id UUID REFERENCES workshops(id) ON DELETE SET NULL,
  valid_from  TIMESTAMPTZ DEFAULT now(),
  valid_until TIMESTAMPTZ,
  status      TEXT NOT NULL,
  created_at  TIMESTAMPTZ DEFAULT now(),
  CONSTRAINT entitlements_status_check
    CHECK (status = ANY (ARRAY['active','expired','revoked']))
);

CREATE UNIQUE INDEX IF NOT EXISTS entitlements_user_content_individual
  ON entitlements (user_id, content_id)
  WHERE workshop_id IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS entitlements_user_content_workshop
  ON entitlements (user_id, content_id, workshop_id)
  WHERE workshop_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_entitlements_workshop_id
  ON entitlements (workshop_id)
  WHERE workshop_id IS NOT NULL;

-- workshops.used_seats: maintained by trigger after changes to entitlements (apply Alembic 0011).

-- ------------------------------------------------------------
-- Sessions
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sessions (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id      UUID NOT NULL REFERENCES users(id),
  session_type TEXT NOT NULL,
  expires_at   TIMESTAMPTZ NOT NULL,
  revoked_at   TIMESTAMPTZ,
  created_at   TIMESTAMPTZ DEFAULT now(),
  CONSTRAINT sessions_session_type_check
    CHECK (session_type = ANY (ARRAY['web','lab']))
);

-- ------------------------------------------------------------
-- Headscale Identities
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS headscale_identities (
  user_id            UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  headscale_username TEXT NOT NULL UNIQUE,
  headscale_user_id  INTEGER NOT NULL UNIQUE,
  created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_headscale_identities_user_id
  ON headscale_identities (user_id);

-- ------------------------------------------------------------
-- Headscale Keys
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS headscale_keys (
  id            SERIAL PRIMARY KEY,
  user_id       UUID NOT NULL REFERENCES users(id),
  type          TEXT NOT NULL,
  key_hash      TEXT NOT NULL,
  hs_id         TEXT,
  hs_user       TEXT,
  reusable      BOOLEAN DEFAULT FALSE,
  ephemeral     BOOLEAN DEFAULT FALSE,
  used          BOOLEAN DEFAULT FALSE,
  expiration    TIMESTAMPTZ,
  hs_created_at TIMESTAMPTZ,
  acl_tags      TEXT[],
  created_at    TIMESTAMPTZ DEFAULT now()
);

-- ------------------------------------------------------------
-- Subnet Allocation
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS subnet_tracker (
  id                  TEXT PRIMARY KEY,
  last_assigned_octet INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS subnet_allocations (
  user_id     UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  subnet_cidr TEXT NOT NULL UNIQUE,
  created_at  TIMESTAMPTZ DEFAULT now()
);

-- ------------------------------------------------------------
-- Lab Deployments
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS lab_deployments (
  id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id             UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  content_id          UUID NOT NULL REFERENCES content_items(id) ON DELETE CASCADE,
  workshop_id         UUID REFERENCES workshops(id) ON DELETE SET NULL,
  lab_type            TEXT NOT NULL,
  status              TEXT NOT NULL DEFAULT 'queued',
  terraform_workspace TEXT NOT NULL,
  instance_public_ip  TEXT,
  instance_private_ip TEXT,
  terraform_outputs   JSONB,
  error_message       TEXT,
  expires_at          TIMESTAMPTZ NOT NULL,
  created_at          TIMESTAMPTZ DEFAULT now(),
  updated_at          TIMESTAMPTZ DEFAULT now(),
  CONSTRAINT lab_deployments_status_check CHECK (
    status = ANY (ARRAY[
      'queued',
      'provisioning',
      'running',
      'failed',
      'terminating',
      'cleanup_failed',
      'expired'
    ])
  )
);

CREATE INDEX IF NOT EXISTS idx_lab_user
  ON lab_deployments (user_id);

CREATE INDEX IF NOT EXISTS idx_lab_deployments_workshop_id
  ON lab_deployments (workshop_id)
  WHERE workshop_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_lab_expires
  ON lab_deployments (expires_at);

CREATE INDEX IF NOT EXISTS idx_lab_status_created
  ON lab_deployments (status, created_at);

CREATE INDEX IF NOT EXISTS idx_lab_status_expires
  ON lab_deployments (status, expires_at);

-- ------------------------------------------------------------
-- Worker Status
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS worker_status (
  id        TEXT PRIMARY KEY,
  last_seen TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ------------------------------------------------------------
-- Termination Logs
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS termination_logs (
  id          SERIAL PRIMARY KEY,
  timestamp   TIMESTAMPTZ DEFAULT now(),
  resource_id TEXT,
  action      TEXT,
  status      TEXT,
  reason      TEXT,
  project     TEXT,
  dry_run     BOOLEAN,
  created_at  TIMESTAMPTZ DEFAULT now()
);