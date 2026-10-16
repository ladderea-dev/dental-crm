-- Idempotent CRM address migration.
-- Safe to review and run separately in the Supabase SQL editor.
-- This does not change RLS, grants, policies, or existing data.

BEGIN;

ALTER TABLE public.crm_contacts
    ADD COLUMN IF NOT EXISTS address text;

COMMENT ON COLUMN public.crm_contacts.address IS
    'Full customer or on-site service address. The existing location field remains available for area or branch labels.';

COMMIT;

-- Verification only:
SELECT column_name, data_type, is_nullable
FROM information_schema.columns
WHERE table_schema = 'public'
  AND table_name = 'crm_contacts'
  AND column_name = 'address';
