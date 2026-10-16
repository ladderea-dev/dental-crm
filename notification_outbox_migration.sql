-- AirZen notification outbox migration
-- Supabase PostgreSQL only. Review and run in isolated staging before production.
-- Do not run from anon/authenticated clients.

begin;

do $$
begin
  if not exists (select 1 from pg_catalog.pg_roles where rolname = 'anon') then
    raise exception 'required role anon does not exist';
  end if;
  if not exists (select 1 from pg_catalog.pg_roles where rolname = 'authenticated') then
    raise exception 'required role authenticated does not exist';
  end if;
  if not exists (select 1 from pg_catalog.pg_roles where rolname = 'service_role') then
    raise exception 'required role service_role does not exist';
  end if;
  if not exists (select 1 from pg_catalog.pg_roles where rolname = 'service_role' and rolbypassrls) then
    raise exception 'service_role must have BYPASSRLS';
  end if;
  if to_regprocedure('pg_catalog.gen_random_uuid()') is null then
    raise exception 'required function pg_catalog.gen_random_uuid() is not available';
  end if;
end $$;

-- Strict preflight for the appointments table used by atomic appointment RPCs.
do $$
declare
  v_table oid := to_regclass('public.appointments');
begin
  if v_table is null then
    raise exception 'required table public.appointments does not exist';
  end if;

  if not exists (select 1 from pg_catalog.pg_attribute where attrelid = v_table and attname = 'id' and atttypid = 'text'::regtype and not attisdropped) then raise exception 'public.appointments.id must be text'; end if;
  if not exists (select 1 from pg_catalog.pg_attribute where attrelid = v_table and attname = 'name' and atttypid = 'text'::regtype and not attisdropped) then raise exception 'public.appointments.name must be text'; end if;
  if not exists (select 1 from pg_catalog.pg_attribute where attrelid = v_table and attname = 'phone' and atttypid = 'text'::regtype and not attisdropped) then raise exception 'public.appointments.phone must be text'; end if;
  if not exists (select 1 from pg_catalog.pg_attribute where attrelid = v_table and attname = 'date' and atttypid = 'text'::regtype and not attisdropped) then raise exception 'public.appointments.date must be text'; end if;
  if not exists (select 1 from pg_catalog.pg_attribute where attrelid = v_table and attname = 'time' and atttypid = 'text'::regtype and not attisdropped) then raise exception 'public.appointments.time must be text'; end if;
  if not exists (select 1 from pg_catalog.pg_attribute where attrelid = v_table and attname = 'service' and atttypid = 'text'::regtype and not attisdropped) then raise exception 'public.appointments.service must be text'; end if;
  if not exists (select 1 from pg_catalog.pg_attribute where attrelid = v_table and attname = 'status' and atttypid = 'text'::regtype and not attisdropped) then raise exception 'public.appointments.status must be text'; end if;
  if not exists (select 1 from pg_catalog.pg_attribute where attrelid = v_table and attname = 'staff_id' and atttypid = 'text'::regtype and not attisdropped) then raise exception 'public.appointments.staff_id must be text'; end if;
  if not exists (select 1 from pg_catalog.pg_attribute where attrelid = v_table and attname = 'staff_name' and atttypid = 'text'::regtype and not attisdropped) then raise exception 'public.appointments.staff_name must be text'; end if;
  if not exists (select 1 from pg_catalog.pg_attribute where attrelid = v_table and attname = 'duration_minutes' and atttypid in ('integer'::regtype, 'bigint'::regtype, 'smallint'::regtype) and not attisdropped) then raise exception 'public.appointments.duration_minutes must be integer-compatible'; end if;
  if not exists (select 1 from pg_catalog.pg_attribute where attrelid = v_table and attname = 'buffer_minutes' and atttypid in ('integer'::regtype, 'bigint'::regtype, 'smallint'::regtype) and not attisdropped) then raise exception 'public.appointments.buffer_minutes must be integer-compatible'; end if;
  if not exists (select 1 from pg_catalog.pg_attribute where attrelid = v_table and attname = 'timezone' and atttypid = 'text'::regtype and not attisdropped) then raise exception 'public.appointments.timezone must be text'; end if;
  if not exists (select 1 from pg_catalog.pg_attribute where attrelid = v_table and attname = 'created_at' and atttypid = 'text'::regtype and not attisdropped) then raise exception 'public.appointments.created_at must be text'; end if;
  if not exists (select 1 from pg_catalog.pg_attribute where attrelid = v_table and attname = 'rescheduled_at' and atttypid = 'timestamp with time zone'::regtype and not attisdropped) then raise exception 'public.appointments.rescheduled_at must be timestamptz'; end if;
  if not exists (select 1 from pg_catalog.pg_attribute where attrelid = v_table and attname = 'rescheduled_from' and atttypid = 'text'::regtype and not attisdropped) then raise exception 'public.appointments.rescheduled_from must be text'; end if;
  if not exists (select 1 from pg_catalog.pg_attribute where attrelid = v_table and attname = 'cancelled_at' and atttypid = 'timestamp with time zone'::regtype and not attisdropped) then raise exception 'public.appointments.cancelled_at must be timestamptz'; end if;

  if not (
    has_table_privilege('service_role', 'public.appointments', 'SELECT')
    and has_table_privilege('service_role', 'public.appointments', 'INSERT')
    and has_table_privilege('service_role', 'public.appointments', 'UPDATE')
  ) then
    raise exception 'service_role must have SELECT and INSERT and UPDATE on public.appointments';
  end if;
end $$;

do $$
begin
  if to_regclass('public.notification_outbox') is not null then
    if not exists (
      select 1
      from pg_catalog.pg_attribute
      where attrelid = 'public.notification_outbox'::regclass
        and attname in ('dedupe_key', 'status', 'recipient_type', 'recipient_phone',
                        'notification_type', 'template_purpose', 'appointment_id',
                        'payload', 'attempt_count', 'max_attempts', 'next_attempt_at')
        and not attisdropped
      group by attrelid
      having count(*) = 11
    ) then
      raise exception 'existing public.notification_outbox table has incompatible schema';
    end if;
  end if;
end $$;

create table if not exists public.notification_outbox (
  id uuid primary key default pg_catalog.gen_random_uuid(),
  dedupe_key text not null,
  status text not null default 'pending'
    check (status in ('pending', 'processing', 'sent', 'failed', 'permanently_failed', 'delivery_unknown')),
  recipient_type text not null check (recipient_type in ('customer', 'staff')),
  recipient_phone text not null,
  notification_type text not null,
  template_purpose text not null,
  appointment_id text not null,
  conversation_id text not null default '',
  contact_id text not null default '',
  payload jsonb not null default '{}'::jsonb,
  attempt_count integer not null default 0 check (attempt_count >= 0),
  max_attempts integer not null default 5 check (max_attempts between 1 and 20),
  next_attempt_at timestamptz not null default pg_catalog.now(),
  claim_token uuid,
  lease_expires_at timestamptz,
  provider_message_id text not null default '',
  last_error text not null default '',
  processing_started_at timestamptz,
  sent_at timestamptz,
  created_at timestamptz not null default pg_catalog.now(),
  updated_at timestamptz not null default pg_catalog.now()
);

alter table public.notification_outbox add column if not exists claim_token uuid;
alter table public.notification_outbox add column if not exists lease_expires_at timestamptz;

do $$
begin
  if not exists (
    select 1 from pg_catalog.pg_constraint
    where conrelid = 'public.notification_outbox'::regclass
      and conname = 'notification_outbox_dedupe_key_key'
  ) then
    alter table public.notification_outbox
      add constraint notification_outbox_dedupe_key_key unique (dedupe_key);
  end if;
end $$;

create index if not exists idx_notification_outbox_due
  on public.notification_outbox (status, next_attempt_at, lease_expires_at)
  where status in ('pending', 'failed', 'processing');

create index if not exists idx_notification_outbox_appointment
  on public.notification_outbox (appointment_id, notification_type, recipient_type);

create index if not exists idx_notification_outbox_claim
  on public.notification_outbox (claim_token)
  where claim_token is not null;

create unique index if not exists idx_appointments_one_reschedule_child
  on public.appointments (rescheduled_from)
  where coalesce(rescheduled_from, '') <> '';

alter table public.notification_outbox enable row level security;
alter table public.notification_outbox no force row level security;

drop policy if exists notification_outbox_no_anon on public.notification_outbox;
drop policy if exists notification_outbox_no_authenticated on public.notification_outbox;

create or replace function public.touch_notification_outbox_updated_at()
returns trigger
language plpgsql
security invoker
set search_path = pg_catalog, public, pg_temp
as $$
begin
  new.updated_at = pg_catalog.now();
  return new;
end;
$$;

drop trigger if exists trg_notification_outbox_updated_at on public.notification_outbox;
create trigger trg_notification_outbox_updated_at
before update on public.notification_outbox
for each row
execute function public.touch_notification_outbox_updated_at();

create or replace function public.airzen_notification_dedupe_key(
  p_operation text,
  p_appointment_id text,
  p_recipient_type text,
  p_event_identity text default 'v1'
)
returns text
language sql
security invoker
set search_path = pg_catalog, public, pg_temp
as $$
  select md5(lower(coalesce(p_operation, '') || ':' || coalesce(p_appointment_id, '') || ':' || coalesce(p_recipient_type, '') || ':' || coalesce(p_event_identity, 'v1')));
$$;

create or replace function public.claim_notification_outbox(batch_limit integer default 20, lease_seconds integer default 300)
returns setof public.notification_outbox
language plpgsql
security invoker
set search_path = pg_catalog, public, pg_temp
as $$
declare
  v_claim_token uuid := pg_catalog.gen_random_uuid();
begin
  return query
  with due as (
    select public.notification_outbox.id
    from public.notification_outbox
    where (
        public.notification_outbox.status in ('pending', 'failed')
        or (
          public.notification_outbox.status = 'processing'
          and public.notification_outbox.lease_expires_at <= pg_catalog.now()
        )
      )
      and public.notification_outbox.next_attempt_at <= pg_catalog.now()
      and public.notification_outbox.attempt_count < public.notification_outbox.max_attempts
    order by public.notification_outbox.next_attempt_at asc, public.notification_outbox.created_at asc
    limit greatest(1, least(coalesce(batch_limit, 20), 100))
    for update skip locked
  )
  update public.notification_outbox n
  set status = 'processing',
      attempt_count = n.attempt_count + 1,
      claim_token = v_claim_token,
      lease_expires_at = pg_catalog.now() + (greatest(30, least(coalesce(lease_seconds, 300), 3600)) || ' seconds')::interval,
      processing_started_at = pg_catalog.now(),
      updated_at = pg_catalog.now()
  from due
  where n.id = due.id
  returning n.*;
end;
$$;

create or replace function public.mark_notification_outbox_sent(
  p_id uuid,
  p_claim_token uuid,
  p_provider_message_id text
)
returns public.notification_outbox
language plpgsql
security invoker
set search_path = pg_catalog, public, pg_temp
as $$
declare
  v_row public.notification_outbox;
begin
  update public.notification_outbox
  set status = 'sent',
      provider_message_id = coalesce(p_provider_message_id, ''),
      sent_at = pg_catalog.now(),
      last_error = '',
      claim_token = null,
      lease_expires_at = null,
      updated_at = pg_catalog.now()
  where id = p_id
    and claim_token = p_claim_token
    and status = 'processing'
  returning * into v_row;

  if v_row.id is null then
    raise exception 'notification outbox claim token mismatch or row not processing';
  end if;
  return v_row;
end;
$$;

create or replace function public.mark_notification_outbox_failed(
  p_id uuid,
  p_claim_token uuid,
  p_status text,
  p_last_error text,
  p_next_attempt_at timestamptz default null
)
returns public.notification_outbox
language plpgsql
security invoker
set search_path = pg_catalog, public, pg_temp
as $$
declare
  v_row public.notification_outbox;
begin
  if p_status not in ('failed', 'permanently_failed', 'delivery_unknown') then
    raise exception 'invalid failure status %', p_status;
  end if;

  update public.notification_outbox
  set status = p_status,
      last_error = left(coalesce(p_last_error, ''), 500),
      next_attempt_at = case when p_status = 'failed' then coalesce(p_next_attempt_at, next_attempt_at) else next_attempt_at end,
      claim_token = null,
      lease_expires_at = null,
      updated_at = pg_catalog.now()
  where id = p_id
    and claim_token = p_claim_token
    and status = 'processing'
  returning * into v_row;

  if v_row.id is null then
    raise exception 'notification outbox claim token mismatch or row not processing';
  end if;
  return v_row;
end;
$$;

create or replace function public.insert_notification_outbox_intent(
  p_operation text,
  p_recipient_type text,
  p_recipient_phone text,
  p_notification_type text,
  p_template_purpose text,
  p_appointment_id text,
  p_payload jsonb,
  p_event_identity text default 'v1',
  p_conversation_id text default '',
  p_contact_id text default '',
  p_next_attempt_at timestamptz default pg_catalog.now()
)
returns uuid
language plpgsql
security invoker
set search_path = pg_catalog, public, pg_temp
as $$
declare
  v_id uuid;
  v_dedupe_key text;
begin
  if coalesce(p_appointment_id, '') = '' then
    raise exception 'appointment id is required for notification intent';
  end if;
  if p_recipient_type not in ('customer', 'staff') then
    raise exception 'recipient type must be customer or staff';
  end if;

  v_dedupe_key := public.airzen_notification_dedupe_key(p_operation, p_appointment_id, p_recipient_type, coalesce(p_event_identity, 'v1'));

  insert into public.notification_outbox (
    dedupe_key, status, recipient_type, recipient_phone, notification_type,
    template_purpose, appointment_id, conversation_id, contact_id, payload,
    next_attempt_at
  )
  values (
    v_dedupe_key, 'pending', p_recipient_type, p_recipient_phone, p_notification_type,
    p_template_purpose, p_appointment_id, coalesce(p_conversation_id, ''),
    coalesce(p_contact_id, ''), coalesce(p_payload, '{}'::jsonb),
    coalesce(p_next_attempt_at, pg_catalog.now())
  )
  on conflict (dedupe_key) do nothing
  returning id into v_id;

  if v_id is null then
    select public.notification_outbox.id into v_id
    from public.notification_outbox
    where public.notification_outbox.dedupe_key = v_dedupe_key
      and public.notification_outbox.appointment_id = p_appointment_id
      and public.notification_outbox.recipient_type = p_recipient_type
      and public.notification_outbox.notification_type = p_notification_type;
  end if;

  if v_id is null then
    raise exception 'notification intent was not persisted for deterministic dedupe key';
  end if;

  return v_id;
end;
$$;

create or replace function public.create_appointment_with_notification_outbox(
  p_appointment jsonb,
  p_customer_intent jsonb,
  p_staff_intent jsonb,
  p_event_identity text default 'v1'
)
returns jsonb
language plpgsql
security invoker
set search_path = pg_catalog, public, pg_temp
as $$
declare
  v_id text := p_appointment->>'id';
  v_existing public.appointments%rowtype;
  v_customer_id uuid;
  v_staff_id uuid;
begin
  if coalesce(v_id, '') = '' then
    raise exception 'appointment id is required';
  end if;

  select * into v_existing
  from public.appointments
  where id = v_id
  for update;

  if found then
    if coalesce(v_existing.name, '') <> coalesce(p_appointment->>'name', '')
       or coalesce(v_existing.phone, '') <> coalesce(p_appointment->>'phone', '')
       or coalesce(v_existing.date, '') <> coalesce(p_appointment->>'date', '')
       or left(coalesce(v_existing.time, ''), 5) <> left(coalesce(p_appointment->>'time', ''), 5)
       or coalesce(v_existing.service, '') <> coalesce(nullif(p_appointment->>'service', ''), 'Appointment')
       or coalesce(v_existing.status, '') <> coalesce(nullif(p_appointment->>'status', ''), 'booked')
       or coalesce(v_existing.staff_id, '') <> coalesce(p_appointment->>'staff_id', '')
       or coalesce(v_existing.staff_name, '') <> coalesce(p_appointment->>'staff_name', '')
       or coalesce(v_existing.duration_minutes, 30) <> coalesce(nullif(p_appointment->>'duration_minutes', '')::integer, 30)
       or coalesce(v_existing.buffer_minutes, 15) <> coalesce(nullif(p_appointment->>'buffer_minutes', '')::integer, 15)
       or coalesce(v_existing.timezone, '') <> coalesce(nullif(p_appointment->>'timezone', ''), 'Asia/Singapore') then
      raise exception 'conflicting appointment already exists for id %', v_id;
    end if;
  else
    insert into public.appointments (
      id, name, phone, date, time, service, status, staff_id, staff_name,
      duration_minutes, buffer_minutes, timezone, created_at
    )
    values (
      v_id,
      p_appointment->>'name',
      p_appointment->>'phone',
      p_appointment->>'date',
      left(p_appointment->>'time', 5),
      coalesce(nullif(p_appointment->>'service', ''), 'Appointment'),
      coalesce(nullif(p_appointment->>'status', ''), 'booked'),
      coalesce(p_appointment->>'staff_id', ''),
      coalesce(p_appointment->>'staff_name', ''),
      coalesce(nullif(p_appointment->>'duration_minutes', '')::integer, 30),
      coalesce(nullif(p_appointment->>'buffer_minutes', '')::integer, 15),
      coalesce(nullif(p_appointment->>'timezone', ''), 'Asia/Singapore'),
      coalesce(p_appointment->>'created_at', pg_catalog.now()::text)
    );
  end if;

  v_customer_id := public.insert_notification_outbox_intent(
    'appointment_create', 'customer', p_customer_intent->>'recipient_phone',
    p_customer_intent->>'notification_type', p_customer_intent->>'template_purpose',
    v_id, coalesce(p_customer_intent->'payload', '{}'::jsonb), p_event_identity,
    coalesce(p_customer_intent->>'conversation_id', ''), coalesce(p_customer_intent->>'contact_id', '')
  );
  v_staff_id := public.insert_notification_outbox_intent(
    'appointment_create', 'staff', p_staff_intent->>'recipient_phone',
    p_staff_intent->>'notification_type', p_staff_intent->>'template_purpose',
    v_id, coalesce(p_staff_intent->'payload', '{}'::jsonb), p_event_identity,
    coalesce(p_staff_intent->>'conversation_id', ''), coalesce(p_staff_intent->>'contact_id', '')
  );

  return jsonb_build_object('appointment_id', v_id, 'customer_outbox_id', v_customer_id, 'staff_outbox_id', v_staff_id);
end;
$$;

create or replace function public.reschedule_appointment_with_notification_outbox(
  p_old_appointment_id text,
  p_new_appointment jsonb,
  p_customer_intent jsonb,
  p_staff_intent jsonb,
  p_event_identity text default 'v1'
)
returns jsonb
language plpgsql
security invoker
set search_path = pg_catalog, public, pg_temp
as $$
declare
  v_old public.appointments%rowtype;
  v_new public.appointments%rowtype;
  v_existing_child public.appointments%rowtype;
  v_new_id text := p_new_appointment->>'id';
  v_customer_id uuid;
  v_staff_id uuid;
begin
  if coalesce(p_old_appointment_id, '') = '' or coalesce(v_new_id, '') = '' then
    raise exception 'old and new appointment ids are required';
  end if;

  select * into v_old
  from public.appointments
  where id = p_old_appointment_id
  for update;

  if not found then
    raise exception 'original appointment does not exist';
  end if;
  if v_old.status = 'rescheduled' then
    select * into v_existing_child
    from public.appointments
    where rescheduled_from = p_old_appointment_id
    for update;
    if not found then
      raise exception 'original appointment is rescheduled but replacement is missing';
    end if;
    if v_existing_child.id <> v_new_id then
      raise exception 'original appointment already has different replacement %', v_existing_child.id;
    end if;
  end if;
  if v_old.status in ('cancelled', 'completed', 'no_show') then
    raise exception 'cannot reschedule appointment from status %', v_old.status;
  end if;

  select * into v_new
  from public.appointments
  where id = v_new_id
  for update;

  if found then
    if coalesce(v_new.rescheduled_from, '') <> p_old_appointment_id
       or coalesce(v_new.phone, '') <> coalesce(p_new_appointment->>'phone', '')
       or coalesce(v_new.date, '') <> coalesce(p_new_appointment->>'date', '')
       or left(coalesce(v_new.time, ''), 5) <> left(coalesce(p_new_appointment->>'time', ''), 5)
       or coalesce(v_new.service, '') <> coalesce(nullif(p_new_appointment->>'service', ''), 'Appointment')
       or coalesce(v_new.staff_id, '') <> coalesce(p_new_appointment->>'staff_id', '')
       or coalesce(v_new.staff_name, '') <> coalesce(p_new_appointment->>'staff_name', '')
       or coalesce(v_new.duration_minutes, 30) <> coalesce(nullif(p_new_appointment->>'duration_minutes', '')::integer, 30)
       or coalesce(v_new.buffer_minutes, 15) <> coalesce(nullif(p_new_appointment->>'buffer_minutes', '')::integer, 15)
       or coalesce(v_new.timezone, '') <> coalesce(nullif(p_new_appointment->>'timezone', ''), 'Asia/Singapore') then
      raise exception 'conflicting rescheduled appointment already exists for id %', v_new_id;
    end if;
  else
    if exists (
      select 1 from public.appointments
      where rescheduled_from = p_old_appointment_id
        and id <> v_new_id
    ) then
      raise exception 'original appointment already has different replacement';
    end if;
    insert into public.appointments (
      id, name, phone, date, time, service, status, staff_id, staff_name,
      duration_minutes, buffer_minutes, timezone, created_at, rescheduled_from
    )
    values (
      v_new_id,
      p_new_appointment->>'name',
      p_new_appointment->>'phone',
      p_new_appointment->>'date',
      left(p_new_appointment->>'time', 5),
      coalesce(nullif(p_new_appointment->>'service', ''), 'Appointment'),
      'booked',
      coalesce(p_new_appointment->>'staff_id', ''),
      coalesce(p_new_appointment->>'staff_name', ''),
      coalesce(nullif(p_new_appointment->>'duration_minutes', '')::integer, 30),
      coalesce(nullif(p_new_appointment->>'buffer_minutes', '')::integer, 15),
      coalesce(nullif(p_new_appointment->>'timezone', ''), 'Asia/Singapore'),
      coalesce(p_new_appointment->>'created_at', pg_catalog.now()::text),
      p_old_appointment_id
    );
  end if;

  update public.appointments
  set status = 'rescheduled',
      rescheduled_at = coalesce(rescheduled_at, pg_catalog.now())
  where id = p_old_appointment_id
    and status <> 'rescheduled';

  v_customer_id := public.insert_notification_outbox_intent('appointment_reschedule', 'customer', p_customer_intent->>'recipient_phone', p_customer_intent->>'notification_type', p_customer_intent->>'template_purpose', v_new_id, coalesce(p_customer_intent->'payload', '{}'::jsonb), p_event_identity, coalesce(p_customer_intent->>'conversation_id', ''), coalesce(p_customer_intent->>'contact_id', ''));
  v_staff_id := public.insert_notification_outbox_intent('appointment_reschedule', 'staff', p_staff_intent->>'recipient_phone', p_staff_intent->>'notification_type', p_staff_intent->>'template_purpose', v_new_id, coalesce(p_staff_intent->'payload', '{}'::jsonb), p_event_identity, coalesce(p_staff_intent->>'conversation_id', ''), coalesce(p_staff_intent->>'contact_id', ''));

  return jsonb_build_object('old_appointment_id', p_old_appointment_id, 'new_appointment_id', v_new_id, 'customer_outbox_id', v_customer_id, 'staff_outbox_id', v_staff_id);
end;
$$;

create or replace function public.cancel_appointment_with_notification_outbox(
  p_appointment_id text,
  p_customer_intent jsonb,
  p_staff_intent jsonb,
  p_event_identity text default 'v1'
)
returns jsonb
language plpgsql
security invoker
set search_path = pg_catalog, public, pg_temp
as $$
declare
  v_row public.appointments%rowtype;
  v_customer_id uuid;
  v_staff_id uuid;
begin
  if coalesce(p_appointment_id, '') = '' then
    raise exception 'appointment id is required';
  end if;

  select * into v_row
  from public.appointments
  where id = p_appointment_id
  for update;

  if not found then
    raise exception 'appointment does not exist';
  end if;
  if v_row.status in ('completed', 'no_show') then
    raise exception 'cannot cancel appointment from status %', v_row.status;
  end if;

  update public.appointments
  set status = 'cancelled',
      cancelled_at = coalesce(cancelled_at, pg_catalog.now())
  where id = p_appointment_id
    and status <> 'cancelled';

  v_customer_id := public.insert_notification_outbox_intent('appointment_cancel', 'customer', p_customer_intent->>'recipient_phone', p_customer_intent->>'notification_type', p_customer_intent->>'template_purpose', p_appointment_id, coalesce(p_customer_intent->'payload', '{}'::jsonb), p_event_identity, coalesce(p_customer_intent->>'conversation_id', ''), coalesce(p_customer_intent->>'contact_id', ''));
  v_staff_id := public.insert_notification_outbox_intent('appointment_cancel', 'staff', p_staff_intent->>'recipient_phone', p_staff_intent->>'notification_type', p_staff_intent->>'template_purpose', p_appointment_id, coalesce(p_staff_intent->'payload', '{}'::jsonb), p_event_identity, coalesce(p_staff_intent->>'conversation_id', ''), coalesce(p_staff_intent->>'contact_id', ''));

  return jsonb_build_object('appointment_id', p_appointment_id, 'customer_outbox_id', v_customer_id, 'staff_outbox_id', v_staff_id);
end;
$$;

revoke all on public.notification_outbox from public, anon, authenticated;
revoke all on function public.touch_notification_outbox_updated_at() from public, anon, authenticated;
revoke all on function public.airzen_notification_dedupe_key(text, text, text, text) from public, anon, authenticated;
revoke all on function public.claim_notification_outbox(integer, integer) from public, anon, authenticated;
revoke all on function public.mark_notification_outbox_sent(uuid, uuid, text) from public, anon, authenticated;
revoke all on function public.mark_notification_outbox_failed(uuid, uuid, text, text, timestamptz) from public, anon, authenticated;
revoke all on function public.insert_notification_outbox_intent(text, text, text, text, text, text, jsonb, text, text, text, timestamptz) from public, anon, authenticated;
revoke all on function public.create_appointment_with_notification_outbox(jsonb, jsonb, jsonb, text) from public, anon, authenticated;
revoke all on function public.reschedule_appointment_with_notification_outbox(text, jsonb, jsonb, jsonb, text) from public, anon, authenticated;
revoke all on function public.cancel_appointment_with_notification_outbox(text, jsonb, jsonb, text) from public, anon, authenticated;

grant usage on schema public to service_role;
grant select, insert, update, delete on public.notification_outbox to service_role;
grant execute on function public.airzen_notification_dedupe_key(text, text, text, text) to service_role;
grant execute on function public.claim_notification_outbox(integer, integer) to service_role;
grant execute on function public.mark_notification_outbox_sent(uuid, uuid, text) to service_role;
grant execute on function public.mark_notification_outbox_failed(uuid, uuid, text, text, timestamptz) to service_role;
grant execute on function public.insert_notification_outbox_intent(text, text, text, text, text, text, jsonb, text, text, text, timestamptz) to service_role;
grant execute on function public.create_appointment_with_notification_outbox(jsonb, jsonb, jsonb, text) to service_role;
grant execute on function public.reschedule_appointment_with_notification_outbox(text, jsonb, jsonb, jsonb, text) to service_role;
grant execute on function public.cancel_appointment_with_notification_outbox(text, jsonb, jsonb, text) to service_role;

do $$
declare
  v_rel_oid oid := 'public.notification_outbox'::regclass;
  v_functions text[] := array[
    'public.touch_notification_outbox_updated_at()',
    'public.airzen_notification_dedupe_key(text,text,text,text)',
    'public.claim_notification_outbox(integer,integer)',
    'public.mark_notification_outbox_sent(uuid,uuid,text)',
    'public.mark_notification_outbox_failed(uuid,uuid,text,text,timestamptz)',
    'public.insert_notification_outbox_intent(text,text,text,text,text,text,jsonb,text,text,text,timestamptz)',
    'public.create_appointment_with_notification_outbox(jsonb,jsonb,jsonb,text)',
    'public.reschedule_appointment_with_notification_outbox(text,jsonb,jsonb,jsonb,text)',
    'public.cancel_appointment_with_notification_outbox(text,jsonb,jsonb,text)'
  ];
  v_fn text;
begin
  if not exists (select 1 from pg_catalog.pg_class where oid = v_rel_oid and relrowsecurity and not relforcerowsecurity) then
    raise exception 'RLS must be enabled and not forced for notification_outbox';
  end if;
  if exists (select 1 from aclexplode(coalesce((select relacl from pg_catalog.pg_class where oid = v_rel_oid), acldefault('r', (select relowner from pg_catalog.pg_class where oid = v_rel_oid)))) acl where acl.grantee = 0 and acl.privilege_type in ('SELECT', 'INSERT', 'UPDATE', 'DELETE')) then
    raise exception 'PUBLIC must not have notification_outbox table access';
  end if;
  if has_table_privilege('anon', 'public.notification_outbox', 'SELECT') or has_table_privilege('anon', 'public.notification_outbox', 'INSERT') or has_table_privilege('anon', 'public.notification_outbox', 'UPDATE') or has_table_privilege('anon', 'public.notification_outbox', 'DELETE') then raise exception 'anon must not have notification_outbox table access'; end if;
  if has_table_privilege('authenticated', 'public.notification_outbox', 'SELECT') or has_table_privilege('authenticated', 'public.notification_outbox', 'INSERT') or has_table_privilege('authenticated', 'public.notification_outbox', 'UPDATE') or has_table_privilege('authenticated', 'public.notification_outbox', 'DELETE') then raise exception 'authenticated must not have notification_outbox table access'; end if;
  if not (has_table_privilege('service_role', 'public.notification_outbox', 'SELECT') and has_table_privilege('service_role', 'public.notification_outbox', 'INSERT') and has_table_privilege('service_role', 'public.notification_outbox', 'UPDATE') and has_table_privilege('service_role', 'public.notification_outbox', 'DELETE')) then raise exception 'service_role must have SELECT and INSERT and UPDATE and DELETE on notification_outbox'; end if;
  if exists (select 1 from pg_catalog.pg_policy where polrelid = v_rel_oid) then raise exception 'notification_outbox must not have RLS policies'; end if;
  if not exists (select 1 from pg_catalog.pg_constraint where conrelid = v_rel_oid and contype = 'u' and conname = 'notification_outbox_dedupe_key_key') then raise exception 'dedupe_key unique constraint is missing'; end if;

  foreach v_fn in array v_functions loop
    if exists (
      select 1 from pg_catalog.pg_proc p, aclexplode(coalesce(p.proacl, acldefault('f', p.proowner))) acl
      where p.oid = v_fn::regprocedure and acl.grantee = 0 and acl.privilege_type = 'EXECUTE'
    ) then
      raise exception 'PUBLIC must not execute %', v_fn;
    end if;
    if has_function_privilege('anon', v_fn, 'EXECUTE') then raise exception 'anon must not execute %', v_fn; end if;
    if has_function_privilege('authenticated', v_fn, 'EXECUTE') then raise exception 'authenticated must not execute %', v_fn; end if;
    if v_fn <> 'public.touch_notification_outbox_updated_at()' then
      if not has_function_privilege('service_role', v_fn, 'EXECUTE') then
        raise exception 'service_role must execute %', v_fn;
      end if;
    end if;
  end loop;
end $$;

commit;
