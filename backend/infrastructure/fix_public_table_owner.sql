-- Run as PostgreSQL superuser (e.g. postgres) if Alembic fails with:
--   must be owner of table <name>
--
-- Your app user (from DATABASE_URL / MIGRATION_DATABASE_URL) must own tables
-- it needs to ALTER. After load_schema / manual DDL, objects may still be owned
-- by postgres — reassign so migrations work.
--
-- Replace cyberrange with your actual DB username if different.

DO $$
DECLARE
  app_user TEXT := 'cyberrange';
  r RECORD;
BEGIN
  FOR r IN
    SELECT tablename
    FROM pg_tables
    WHERE schemaname = 'public'
  LOOP
    EXECUTE format('ALTER TABLE public.%I OWNER TO %I', r.tablename, app_user);
  END LOOP;
END $$;
