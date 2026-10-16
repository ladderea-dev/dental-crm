-- AirZen security-preserving RLS rollback
-- REVIEW IN STAGING FIRST. This file is not executed by the application.
--
-- This rollback intentionally does NOT restore anon/authenticated/PUBLIC grants
-- or the unsafe metadata-based admin policies. It disables RLS only on the 19
-- in-scope tables while preserving direct-browser denial and service-role access.
--
-- Why this is not a byte-for-byte rollback:
--   The pre-migration database may contain unknown grants/policies. Recreating
--   them without a captured, reviewed snapshot could reopen CRM data. If exact
--   restoration is required, use a separately reviewed database backup or the
--   saved preflight output from safe_rls_migration.sql.

BEGIN;

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '120s';

CREATE TEMP TABLE _airzen_sensitive_tables (
    table_name name PRIMARY KEY
) ON COMMIT DROP;

INSERT INTO _airzen_sensitive_tables (table_name) VALUES
    ('settings'),
    ('error_logs'),
    ('call_logs'),
    ('appointment_staff'),
    ('contact_memory'),
    ('campaigns'),
    ('agent_profiles'),
    ('lead_statuses'),
    ('crm_contacts'),
    ('crm_tags'),
    ('crm_custom_fields'),
    ('broadcast_campaigns'),
    ('broadcast_recipients'),
    ('followup_actions'),
    ('whatsapp_conversations'),
    ('whatsapp_messages'),
    ('automation_actions'),
    ('whatsapp_logs'),
    ('appointments');

-- Preflight: abort if a table/role is missing.
DO $preflight$
DECLARE
    missing_tables text;
    role_name text;
BEGIN
    SELECT string_agg(t.table_name::text, ', ' ORDER BY t.table_name)
      INTO missing_tables
      FROM _airzen_sensitive_tables AS t
     WHERE to_regclass(format('public.%I', t.table_name)) IS NULL;

    IF missing_tables IS NOT NULL THEN
        RAISE EXCEPTION 'Missing required public tables: %', missing_tables;
    END IF;

    FOREACH role_name IN ARRAY ARRAY['anon', 'authenticated', 'service_role'] LOOP
        IF to_regrole(role_name) IS NULL THEN
            RAISE EXCEPTION 'Required database role is missing: %', role_name;
        END IF;
    END LOOP;
END
$preflight$;

-- Report current state before rollback.
SELECT
    c.relname AS table_name,
    c.relrowsecurity AS rls_enabled,
    c.relforcerowsecurity AS rls_forced,
    has_table_privilege('anon', format('public.%I', c.relname), 'SELECT') AS anon_can_select,
    has_table_privilege('authenticated', format('public.%I', c.relname), 'SELECT') AS authenticated_can_select,
    has_table_privilege('service_role', format('public.%I', c.relname), 'SELECT') AS service_role_can_select
FROM pg_class AS c
JOIN pg_namespace AS n ON n.oid = c.relnamespace
JOIN _airzen_sensitive_tables AS t ON t.table_name = c.relname
WHERE n.nspname = 'public'
ORDER BY c.relname;

-- Security-preserving rollback:
-- 1. Keep browser roles and PUBLIC fully revoked.
-- 2. Keep service_role privileges.
-- 3. Disable RLS/force-RLS to match the old backend execution characteristic.
DO $rollback$
DECLARE
    t record;
BEGIN
    FOR t IN SELECT table_name FROM _airzen_sensitive_tables ORDER BY table_name LOOP
        EXECUTE format('REVOKE ALL PRIVILEGES ON TABLE public.%I FROM PUBLIC', t.table_name);
        EXECUTE format('REVOKE ALL PRIVILEGES ON TABLE public.%I FROM anon', t.table_name);
        EXECUTE format('REVOKE ALL PRIVILEGES ON TABLE public.%I FROM authenticated', t.table_name);
        EXECUTE format('GRANT ALL PRIVILEGES ON TABLE public.%I TO service_role', t.table_name);
        EXECUTE format('ALTER TABLE public.%I NO FORCE ROW LEVEL SECURITY', t.table_name);
        EXECUTE format('ALTER TABLE public.%I DISABLE ROW LEVEL SECURITY', t.table_name);
    END LOOP;
END
$rollback$;

-- Preserve the same security posture for owned sequences.
DO $sequences$
DECLARE
    s record;
BEGIN
    FOR s IN
        SELECT DISTINCT
            seq_ns.nspname AS sequence_schema,
            seq.relname AS sequence_name
        FROM pg_class AS tbl
        JOIN pg_namespace AS tbl_ns
          ON tbl_ns.oid = tbl.relnamespace
        JOIN _airzen_sensitive_tables AS t
          ON t.table_name = tbl.relname
        JOIN pg_depend AS dep
          ON dep.refobjid = tbl.oid
         AND dep.deptype IN ('a', 'i')
        JOIN pg_class AS seq
          ON seq.oid = dep.objid
         AND seq.relkind = 'S'
        JOIN pg_namespace AS seq_ns
          ON seq_ns.oid = seq.relnamespace
        WHERE tbl_ns.nspname = 'public'
    LOOP
        EXECUTE format('REVOKE ALL PRIVILEGES ON SEQUENCE %I.%I FROM PUBLIC', s.sequence_schema, s.sequence_name);
        EXECUTE format('REVOKE ALL PRIVILEGES ON SEQUENCE %I.%I FROM anon', s.sequence_schema, s.sequence_name);
        EXECUTE format('REVOKE ALL PRIVILEGES ON SEQUENCE %I.%I FROM authenticated', s.sequence_schema, s.sequence_name);
        EXECUTE format('GRANT USAGE, SELECT, UPDATE ON SEQUENCE %I.%I TO service_role', s.sequence_schema, s.sequence_name);
    END LOOP;
END
$sequences$;

-- Post-rollback verification and hard assertion.
SELECT
    c.relname AS table_name,
    c.relrowsecurity AS rls_enabled,
    c.relforcerowsecurity AS rls_forced,
    has_table_privilege('anon', format('public.%I', c.relname), 'SELECT') AS anon_can_select,
    has_table_privilege('authenticated', format('public.%I', c.relname), 'SELECT') AS authenticated_can_select,
    has_table_privilege('service_role', format('public.%I', c.relname), 'SELECT') AS service_role_can_select
FROM pg_class AS c
JOIN pg_namespace AS n ON n.oid = c.relnamespace
JOIN _airzen_sensitive_tables AS t ON t.table_name = c.relname
WHERE n.nspname = 'public'
ORDER BY c.relname;

DO $verify$
DECLARE
    failures text;
BEGIN
    SELECT string_agg(t.table_name::text, ', ' ORDER BY t.table_name)
      INTO failures
      FROM _airzen_sensitive_tables AS t
      JOIN pg_class AS c ON c.oid = to_regclass(format('public.%I', t.table_name))
     WHERE c.relrowsecurity
        OR c.relforcerowsecurity
        OR has_table_privilege('anon', format('public.%I', t.table_name), 'SELECT')
        OR has_table_privilege('anon', format('public.%I', t.table_name), 'INSERT')
        OR has_table_privilege('anon', format('public.%I', t.table_name), 'UPDATE')
        OR has_table_privilege('anon', format('public.%I', t.table_name), 'DELETE')
        OR has_table_privilege('authenticated', format('public.%I', t.table_name), 'SELECT')
        OR has_table_privilege('authenticated', format('public.%I', t.table_name), 'INSERT')
        OR has_table_privilege('authenticated', format('public.%I', t.table_name), 'UPDATE')
        OR has_table_privilege('authenticated', format('public.%I', t.table_name), 'DELETE')
        OR NOT has_table_privilege('service_role', format('public.%I', t.table_name), 'SELECT')
        OR NOT has_table_privilege('service_role', format('public.%I', t.table_name), 'INSERT')
        OR NOT has_table_privilege('service_role', format('public.%I', t.table_name), 'UPDATE')
        OR NOT has_table_privilege('service_role', format('public.%I', t.table_name), 'DELETE');

    IF failures IS NOT NULL THEN
        RAISE EXCEPTION 'Security-preserving rollback verification failed for: %', failures;
    END IF;
END
$verify$;

COMMIT;
