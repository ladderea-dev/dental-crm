-- OutboundAI WhatsApp health-test storage.
-- Apply only through the normal reviewed migration process. This file is not auto-executed.

begin;

create table if not exists public.whatsapp_test_recipients (
    id uuid primary key,
    recipient_name text not null,
    phone_number_encrypted text not null,
    phone_number_fingerprint text not null unique,
    phone_number_masked text not null,
    country_code text not null,
    verified boolean not null default false,
    verification_code_hash text,
    verification_expires_at timestamptz,
    verified_at timestamptz,
    active boolean not null default true,
    last_real_test_at timestamptz,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table if not exists public.whatsapp_health_test_runs (
    id uuid primary key,
    started_at timestamptz not null default now(),
    completed_at timestamptz,
    status text not null check (status in ('RUNNING','PASS','FAIL','WARNING','SKIPPED','STOPPED')),
    recipient_id uuid references public.whatsapp_test_recipients(id) on delete restrict,
    initiated_by text not null,
    selected_tests jsonb not null default '[]'::jsonb,
    language text not null default 'en',
    test_ai_reply boolean not null default false,
    allow_marketing boolean not null default false,
    max_real_messages integer not null default 10 check (max_real_messages between 1 and 50),
    real_messages_submitted integer not null default 0 check (real_messages_submitted between 0 and 50),
    stop_requested boolean not null default false,
    total_tests integer not null default 0,
    passed_tests integer not null default 0,
    failed_tests integer not null default 0,
    warning_tests integer not null default 0,
    skipped_tests integer not null default 0,
    report_json jsonb not null default '{}'::jsonb
);

create table if not exists public.whatsapp_health_test_results (
    id uuid primary key,
    test_run_id uuid not null references public.whatsapp_health_test_runs(id) on delete cascade,
    test_key text not null,
    test_name text not null,
    status text not null check (status in ('PASS','SUBMITTED','FAIL','WARNING','SKIPPED','RUNNING','STOPPED')),
    started_at timestamptz not null default now(),
    completed_at timestamptz,
    duration_ms integer,
    provider_message_id text,
    http_status integer,
    provider_error_code text,
    provider_error_type text,
    provider_error_message text,
    webhook_status text,
    details_json jsonb not null default '{}'::jsonb,
    unique (test_run_id, test_key)
);

alter table public.whatsapp_health_test_results
    add column if not exists template_name text,
    add column if not exists template_category text,
    add column if not exists template_language text,
    add column if not exists payload_json jsonb,
    add column if not exists provider_response_json jsonb,
    add column if not exists retry_count integer not null default 0,
    add column if not exists skip_reason text,
    add column if not exists is_test boolean not null default true,
    add column if not exists submitted_at timestamptz,
    add column if not exists sent_at timestamptz,
    add column if not exists delivered_at timestamptz;

create index if not exists idx_whatsapp_health_runs_recipient_started
    on public.whatsapp_health_test_runs (recipient_id, started_at desc);
create index if not exists idx_whatsapp_health_results_run
    on public.whatsapp_health_test_results (test_run_id, started_at);

alter table public.whatsapp_test_recipients enable row level security;
alter table public.whatsapp_test_recipients no force row level security;
alter table public.whatsapp_health_test_runs enable row level security;
alter table public.whatsapp_health_test_runs no force row level security;
alter table public.whatsapp_health_test_results enable row level security;
alter table public.whatsapp_health_test_results no force row level security;

revoke all on table public.whatsapp_test_recipients from public, anon, authenticated;
revoke all on table public.whatsapp_health_test_runs from public, anon, authenticated;
revoke all on table public.whatsapp_health_test_results from public, anon, authenticated;
grant usage on schema public to service_role;
grant select, insert, update, delete on table public.whatsapp_test_recipients to service_role;
grant select, insert, update, delete on table public.whatsapp_health_test_runs to service_role;
grant select, insert, update, delete on table public.whatsapp_health_test_results to service_role;

commit;
