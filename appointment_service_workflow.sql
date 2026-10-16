-- AirZen appointment service workflow migration.
-- Review and run separately in Supabase before deploying the matching code.
-- This migration does not modify RLS, grants, policies, or existing rows.

BEGIN;

ALTER TABLE public.appointments ADD COLUMN IF NOT EXISTS service_address text NOT NULL DEFAULT '';
ALTER TABLE public.appointments ADD COLUMN IF NOT EXISTS location_latitude double precision;
ALTER TABLE public.appointments ADD COLUMN IF NOT EXISTS location_longitude double precision;
ALTER TABLE public.appointments ADD COLUMN IF NOT EXISTS location_name text NOT NULL DEFAULT '';
ALTER TABLE public.appointments ADD COLUMN IF NOT EXISTS location_url text NOT NULL DEFAULT '';
ALTER TABLE public.appointments ADD COLUMN IF NOT EXISTS intake_data jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE public.appointments ADD COLUMN IF NOT EXISTS staff_notification_message_id text NOT NULL DEFAULT '';
ALTER TABLE public.appointments ADD COLUMN IF NOT EXISTS staff_notification_sent_at timestamptz;
ALTER TABLE public.appointments ADD COLUMN IF NOT EXISTS service_started_at timestamptz;
ALTER TABLE public.appointments ADD COLUMN IF NOT EXISTS staff_status_updated_at timestamptz;
ALTER TABLE public.appointments ADD COLUMN IF NOT EXISTS staff_status_updated_by text NOT NULL DEFAULT '';

CREATE INDEX IF NOT EXISTS idx_appointments_staff_status_date
    ON public.appointments (staff_id, status, date, time);
CREATE INDEX IF NOT EXISTS idx_appointments_staff_notification_message
    ON public.appointments (staff_notification_message_id)
    WHERE staff_notification_message_id <> '';

COMMIT;

-- Verification only:
SELECT column_name, data_type, is_nullable
FROM information_schema.columns
WHERE table_schema = 'public'
  AND table_name = 'appointments'
  AND column_name IN (
      'service_address', 'location_latitude', 'location_longitude',
      'location_name', 'location_url', 'intake_data',
      'staff_notification_message_id', 'staff_notification_sent_at',
      'service_started_at', 'staff_status_updated_at', 'staff_status_updated_by'
  )
ORDER BY column_name;
