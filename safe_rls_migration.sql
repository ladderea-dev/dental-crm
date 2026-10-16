-- AirZen safe RLS migration
-- Architecture: FastAPI-only data access using SUPABASE_SERVICE_KEY.
-- Browser roles (anon/authenticated) receive no direct access.
-- REVIEW IN STAGING FIRST. This file is not executed by the application.
--
-- Run as a database owner/postgres-equivalent role in the Supabase SQL editor.
-- The transaction aborts before changes if required roles/tables or service-role
-- capabilities are missing.

BEGIN;

SET TRANSACTION ISOLATION LEVEL REPEATABLE READ;
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

-- ---------------------------------------------------------------------------
-- PREFLIGHT: read and save this output before approving the migration.
-- ---------------------------------------------------------------------------

-- 1. Required role/table guards. Any failure aborts before security changes.
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

    IF NOT EXISTS (
        SELECT 1
          FROM pg_roles
         WHERE rolname = 'service_role'
           AND rolbypassrls
    ) THEN
        RAISE EXCEPTION 'service_role does not have BYPASSRLS; aborting to protect backend availability';
    END IF;
END
$preflight$;

-- 2. Current RLS state.
SELECT
    n.nspname AS schema_name,
    c.relname AS table_name,
    c.relrowsecurity AS rls_enabled,
    c.relforcerowsecurity AS rls_forced,
    pg_get_userbyid(c.relowner) AS table_owner
FROM pg_class AS c
JOIN pg_namespace AS n ON n.oid = c.relnamespace
JOIN _airzen_sensitive_tables AS t ON t.table_name = c.relname
WHERE n.nspname = 'public'
  AND c.relkind IN ('r', 'p')
ORDER BY c.relname;

-- 3. Current effective table grants, including grants inherited through PUBLIC.
SELECT
    t.table_name,
    r.role_name,
    has_table_privilege(r.role_name, format('public.%I', t.table_name), 'SELECT') AS can_select,
    has_table_privilege(r.role_name, format('public.%I', t.table_name), 'INSERT') AS can_insert,
    has_table_privilege(r.role_name, format('public.%I', t.table_name), 'UPDATE') AS can_update,
    has_table_privilege(r.role_name, format('public.%I', t.table_name), 'DELETE') AS can_delete,
    has_table_privilege(r.role_name, format('public.%I', t.table_name), 'TRUNCATE') AS can_truncate,
    has_table_privilege(r.role_name, format('public.%I', t.table_name), 'REFERENCES') AS can_reference,
    has_table_privilege(r.role_name, format('public.%I', t.table_name), 'TRIGGER') AS can_trigger
FROM _airzen_sensitive_tables AS t
CROSS JOIN (VALUES ('anon'), ('authenticated'), ('service_role')) AS r(role_name)
ORDER BY t.table_name, r.role_name;

-- 4. Explicit grants recorded in information_schema.
SELECT
    g.table_schema,
    g.table_name,
    g.grantee,
    g.privilege_type,
    g.is_grantable
FROM information_schema.role_table_grants AS g
JOIN _airzen_sensitive_tables AS t ON t.table_name = g.table_name
WHERE g.table_schema = 'public'
ORDER BY g.table_name, g.grantee, g.privilege_type;

-- 5. Current policies. No policy is required for the selected architecture.
SELECT
    p.schemaname,
    p.tablename,
    p.policyname,
    p.permissive,
    p.roles,
    p.cmd,
    p.qual,
    p.with_check
FROM pg_policies AS p
JOIN _airzen_sensitive_tables AS t ON t.table_name = p.tablename
WHERE p.schemaname = 'public'
ORDER BY p.tablename, p.policyname;

-- 6. Current row counts (exact counts; review runtime on very large tables).
CREATE TEMP TABLE _airzen_preflight_row_counts (
    table_name name PRIMARY KEY,
    exact_row_count bigint NOT NULL
) ON COMMIT DROP;

DO $counts$
DECLARE
    t record;
    row_count bigint;
BEGIN
    FOR t IN SELECT table_name FROM _airzen_sensitive_tables ORDER BY table_name LOOP
        EXECUTE format('SELECT count(*) FROM public.%I', t.table_name) INTO row_count;
        INSERT INTO _airzen_preflight_row_counts VALUES (t.table_name, row_count);
    END LOOP;
END
$counts$;

SELECT table_name, exact_row_count
FROM _airzen_preflight_row_counts
ORDER BY table_name;

-- 7. Service-role capability preflight.
SELECT
    r.rolname,
    r.rolbypassrls,
    r.rolsuper,
    r.rolcanlogin
FROM pg_roles AS r
WHERE r.rolname = 'service_role';

SELECT
    t.table_name,
    has_table_privilege('service_role', format('public.%I', t.table_name), 'SELECT') AS can_select,
    has_table_privilege('service_role', format('public.%I', t.table_name), 'INSERT') AS can_insert,
    has_table_privilege('service_role', format('public.%I', t.table_name), 'UPDATE') AS can_update,
    has_table_privilege('service_role', format('public.%I', t.table_name), 'DELETE') AS can_delete
FROM _airzen_sensitive_tables AS t
ORDER BY t.table_name;

-- ---------------------------------------------------------------------------
-- MIGRATION: service-role-only backend access.
-- ---------------------------------------------------------------------------

DO $migration$
DECLARE
    t record;
    p record;
BEGIN
    FOR t IN SELECT table_name FROM _airzen_sensitive_tables ORDER BY table_name LOOP
        -- Remove every existing policy on in-scope tables. Direct browser access is
        -- not part of this application architecture, so no JWT policy is retained.
        FOR p IN
            SELECT policyname
              FROM pg_policies
             WHERE schemaname = 'public'
               AND tablename = t.table_name
        LOOP
            EXECUTE format(
                'DROP POLICY %I ON public.%I',
                p.policyname,
                t.table_name
            );
        END LOOP;

        -- Explicitly revoke direct access. Revoking PUBLIC prevents inherited
        -- privileges from silently reaching anon/authenticated.
        EXECUTE format('REVOKE ALL PRIVILEGES ON TABLE public.%I FROM PUBLIC', t.table_name);
        EXECUTE format('REVOKE ALL PRIVILEGES ON TABLE public.%I FROM anon', t.table_name);
        EXECUTE format('REVOKE ALL PRIVILEGES ON TABLE public.%I FROM authenticated', t.table_name);

        -- RLS is enabled but not forced. Supabase service_role has BYPASSRLS.
        EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', t.table_name);
        EXECUTE format('ALTER TABLE public.%I NO FORCE ROW LEVEL SECURITY', t.table_name);

        -- RLS bypass does not itself grant SQL privileges, so preserve/establish
        -- the backend role's table privileges explicitly.
        EXECUTE format('GRANT ALL PRIVILEGES ON TABLE public.%I TO service_role', t.table_name);
    END LOOP;
END
$migration$;

-- Revoke/grant only sequences owned by columns of the in-scope tables.
-- This avoids changing unrelated applications in the public schema.
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
        EXECUTE format(
            'REVOKE ALL PRIVILEGES ON SEQUENCE %I.%I FROM PUBLIC',
            s.sequence_schema,
            s.sequence_name
        );
        EXECUTE format(
            'REVOKE ALL PRIVILEGES ON SEQUENCE %I.%I FROM anon',
            s.sequence_schema,
            s.sequence_name
        );
        EXECUTE format(
            'REVOKE ALL PRIVILEGES ON SEQUENCE %I.%I FROM authenticated',
            s.sequence_schema,
            s.sequence_name
        );
        EXECUTE format(
            'GRANT USAGE, SELECT, UPDATE ON SEQUENCE %I.%I TO service_role',
            s.sequence_schema,
            s.sequence_name
        );
    END LOOP;
END
$sequences$;

-- Remove the unsafe helper from the previous migration if it exists. RESTRICT
-- (the default) intentionally aborts if an unexpected dependency exists.
DO $remove_unsafe_helper$
BEGIN
    IF to_regprocedure('public.is_app_admin()') IS NOT NULL THEN
        REVOKE ALL ON FUNCTION public.is_app_admin() FROM PUBLIC;
        REVOKE ALL ON FUNCTION public.is_app_admin() FROM anon;
        REVOKE ALL ON FUNCTION public.is_app_admin() FROM authenticated;
        DROP FUNCTION public.is_app_admin();
    END IF;
END
$remove_unsafe_helper$;

-- ---------------------------------------------------------------------------
-- POST-MIGRATION VERIFICATION.
-- ---------------------------------------------------------------------------

-- 1. RLS must be enabled and not forced on every table.
SELECT
    c.relname AS table_name,
    c.relrowsecurity AS rls_enabled,
    c.relforcerowsecurity AS rls_forced
FROM pg_class AS c
JOIN pg_namespace AS n ON n.oid = c.relnamespace
JOIN _airzen_sensitive_tables AS t ON t.table_name = c.relname
WHERE n.nspname = 'public'
ORDER BY c.relname;

-- 2. No policies should exist for these tables: no policy means default deny for
-- roles subject to RLS, while service_role bypasses RLS.
SELECT
    p.schemaname,
    p.tablename,
    p.policyname,
    p.roles,
    p.cmd
FROM pg_policies AS p
JOIN _airzen_sensitive_tables AS t ON t.table_name = p.tablename
WHERE p.schemaname = 'public'
ORDER BY p.tablename, p.policyname;

-- 3. Effective privilege verification for browser and backend roles.
SELECT
    t.table_name,
    r.role_name,
    has_table_privilege(r.role_name, format('public.%I', t.table_name), 'SELECT') AS can_select,
    has_table_privilege(r.role_name, format('public.%I', t.table_name), 'INSERT') AS can_insert,
    has_table_privilege(r.role_name, format('public.%I', t.table_name), 'UPDATE') AS can_update,
    has_table_privilege(r.role_name, format('public.%I', t.table_name), 'DELETE') AS can_delete
FROM _airzen_sensitive_tables AS t
CROSS JOIN (VALUES ('anon'), ('authenticated'), ('service_role')) AS r(role_name)
ORDER BY t.table_name, r.role_name;

-- 4. Exact row counts must match the preflight snapshot.
CREATE TEMP TABLE _airzen_post_row_counts (
    table_name name PRIMARY KEY,
    exact_row_count bigint NOT NULL
) ON COMMIT DROP;

DO $post_counts$
DECLARE
    t record;
    row_count bigint;
BEGIN
    FOR t IN SELECT table_name FROM _airzen_sensitive_tables ORDER BY table_name LOOP
        EXECUTE format('SELECT count(*) FROM public.%I', t.table_name) INTO row_count;
        INSERT INTO _airzen_post_row_counts VALUES (t.table_name, row_count);
    END LOOP;
END
$post_counts$;

SELECT
    pre.table_name,
    pre.exact_row_count AS before_count,
    post.exact_row_count AS after_count,
    (pre.exact_row_count = post.exact_row_count) AS unchanged
FROM _airzen_preflight_row_counts AS pre
JOIN _airzen_post_row_counts AS post USING (table_name)
ORDER BY pre.table_name;

-- 5. Hard assertions. Any failure rolls back every change.
DO $verify$
DECLARE
    failures text;
BEGIN
    SELECT string_agg(t.table_name::text, ', ' ORDER BY t.table_name)
      INTO failures
      FROM _airzen_sensitive_tables AS t
      JOIN pg_class AS c ON c.oid = to_regclass(format('public.%I', t.table_name))
     WHERE NOT c.relrowsecurity
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
        RAISE EXCEPTION 'RLS/privilege verification failed for tables: %', failures;
    END IF;

    IF EXISTS (
        SELECT 1
          FROM pg_policies AS p
          JOIN _airzen_sensitive_tables AS t ON t.table_name = p.tablename
         WHERE p.schemaname = 'public'
    ) THEN
        RAISE EXCEPTION 'Unexpected RLS policies remain on protected tables';
    END IF;

    IF EXISTS (
        SELECT 1
          FROM _airzen_preflight_row_counts AS pre
          JOIN _airzen_post_row_counts AS post USING (table_name)
         WHERE pre.exact_row_count <> post.exact_row_count
    ) THEN
        RAISE EXCEPTION 'Row counts changed during the migration';
    END IF;
END
$verify$;

COMMIT;
