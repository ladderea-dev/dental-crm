-- Enable Supabase Row Level Security for the OutboundAI application tables.
--
-- Backend safety:
-- The FastAPI backend uses SUPABASE_SERVICE_KEY / SUPABASE_SERVICE_ROLE_KEY.
-- Supabase service-role keys bypass RLS by default, so these policies protect
-- browser/client access without blocking the existing backend. Do not enable
-- FORCE ROW LEVEL SECURITY on these tables unless the backend is changed first.

CREATE OR REPLACE FUNCTION public.is_app_admin()
RETURNS boolean
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public
AS $$
    SELECT auth.role() = 'authenticated'
        AND (
            lower(coalesce(auth.jwt() -> 'app_metadata' ->> 'role', '')) IN ('admin', 'owner', 'super_admin')
            OR lower(coalesce(auth.jwt() -> 'user_metadata' ->> 'role', '')) IN ('admin', 'owner', 'super_admin')
            OR lower(coalesce(auth.jwt() -> 'app_metadata' ->> 'is_admin', '')) IN ('true', '1', 'yes')
            OR lower(coalesce(auth.jwt() -> 'user_metadata' ->> 'is_admin', '')) IN ('true', '1', 'yes')
            OR coalesce(auth.jwt() -> 'app_metadata' -> 'roles', '[]'::jsonb) ? 'admin'
            OR coalesce(auth.jwt() -> 'user_metadata' -> 'roles', '[]'::jsonb) ? 'admin'
        );
$$;

COMMENT ON FUNCTION public.is_app_admin()
IS 'Returns true only for authenticated Supabase users marked as admins in app_metadata or user_metadata. Service-role backend requests bypass RLS separately.';

REVOKE ALL ON FUNCTION public.is_app_admin() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.is_app_admin() TO authenticated;

DO $$
DECLARE
    table_name text;
    app_tables text[] := ARRAY[
        'appointments',
        'call_logs',
        'settings',
        'error_logs',
        'campaigns',
        'appointment_staff',
        'contact_memory',
        'agent_profiles',
        'lead_statuses',
        'crm_contacts',
        'crm_tags',
        'crm_custom_fields',
        'broadcast_campaigns',
        'broadcast_recipients',
        'followup_actions',
        'whatsapp_conversations',
        'whatsapp_messages',
        'automation_actions',
        'whatsapp_logs'
    ];
BEGIN
    FOREACH table_name IN ARRAY app_tables LOOP
        -- Enable RLS without FORCE so the service-role backend continues to work.
        EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', table_name);

        -- Remove anonymous API access even if older schema defaults granted it.
        EXECUTE format('REVOKE ALL ON TABLE public.%I FROM anon', table_name);

        -- Authenticated users still need SQL privileges; RLS below limits them to admins.
        EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.%I TO authenticated', table_name);

        EXECUTE format('DROP POLICY IF EXISTS admin_select ON public.%I', table_name);
        EXECUTE format('DROP POLICY IF EXISTS admin_insert ON public.%I', table_name);
        EXECUTE format('DROP POLICY IF EXISTS admin_update ON public.%I', table_name);
        EXECUTE format('DROP POLICY IF EXISTS admin_delete ON public.%I', table_name);

        EXECUTE format(
            'CREATE POLICY admin_select ON public.%I FOR SELECT TO authenticated USING (public.is_app_admin())',
            table_name
        );
        EXECUTE format(
            'CREATE POLICY admin_insert ON public.%I FOR INSERT TO authenticated WITH CHECK (public.is_app_admin())',
            table_name
        );
        EXECUTE format(
            'CREATE POLICY admin_update ON public.%I FOR UPDATE TO authenticated USING (public.is_app_admin()) WITH CHECK (public.is_app_admin())',
            table_name
        );
        EXECUTE format(
            'CREATE POLICY admin_delete ON public.%I FOR DELETE TO authenticated USING (public.is_app_admin())',
            table_name
        );

        EXECUTE format(
            'COMMENT ON POLICY admin_select ON public.%I IS %L',
            table_name,
            'Allows authenticated admin users to read this table. Anonymous and non-admin users receive no rows.'
        );
        EXECUTE format(
            'COMMENT ON POLICY admin_insert ON public.%I IS %L',
            table_name,
            'Allows authenticated admin users to create rows. Anonymous and non-admin users cannot insert.'
        );
        EXECUTE format(
            'COMMENT ON POLICY admin_update ON public.%I IS %L',
            table_name,
            'Allows authenticated admin users to update rows and prevents updates that would fail the admin check.'
        );
        EXECUTE format(
            'COMMENT ON POLICY admin_delete ON public.%I IS %L',
            table_name,
            'Allows authenticated admin users to delete rows. Anonymous and non-admin users cannot delete.'
        );
    END LOOP;
END $$;

-- Deny anonymous access to existing sequences too. The current schema mostly uses
-- text/uuid primary keys, but this keeps future sequence-backed defaults private.
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM anon;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO authenticated;

-- Avoid new tables/sequences becoming anonymously accessible through defaults.
ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM anon;
ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON SEQUENCES FROM anon;
