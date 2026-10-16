# Notification Outbox Design

The appointment notification outbox makes critical customer and staff notification intents durable in the same database transaction as appointment creation, reschedule, or cancellation.

## Final Privilege Model

Security model:

- `public.notification_outbox` has RLS enabled.
- `public.notification_outbox` uses `NO FORCE ROW LEVEL SECURITY`.
- No RLS policies exist on `public.notification_outbox`.
- `PUBLIC`, `anon`, and `authenticated` have no table privileges.
- `PUBLIC`, `anon`, and `authenticated` have no execute privileges on any outbox function.
- `service_role` has:
  - `USAGE` on schema `public`
  - `SELECT`, `INSERT`, `UPDATE`, `DELETE` on `public.notification_outbox`
  - `EXECUTE` on backend RPCs
- `service_role` is granted `EXECUTE` on `public.airzen_notification_dedupe_key(text,text,text,text)` because backend-required RPCs call it and post-migration assertions verify the grant.
- Functions use `SECURITY INVOKER`. Supabase `service_role` must have `BYPASSRLS`, and migration preflight aborts if it does not.

The trigger function `public.touch_notification_outbox_updated_at()` is also `SECURITY INVOKER`; it only changes `updated_at` for an already-authorized row update. It is not directly called by the application, so no direct `service_role` execute grant is required.

## RPC Names And Parameters

### `public.airzen_notification_dedupe_key(p_operation text, p_appointment_id text, p_recipient_type text, p_event_identity text default 'v1')`

Derives deterministic dedupe keys using operation, appointment id, recipient type, and event identity/version. It uses PostgreSQL built-in `md5(text)` and does not depend on `pgcrypto.digest()` or an extension schema. Caller-provided arbitrary dedupe keys are not trusted by the atomic RPCs.

### `public.create_appointment_with_notification_outbox(p_appointment jsonb, p_customer_intent jsonb, p_staff_intent jsonb, p_event_identity text default 'v1')`

Creates or verifies the appointment and creates customer/staff notification intents in one transaction. Duplicate execution is accepted only when the existing appointment row represents the same appointment. Conflicting existing IDs abort.

### `public.reschedule_appointment_with_notification_outbox(p_old_appointment_id text, p_new_appointment jsonb, p_customer_intent jsonb, p_staff_intent jsonb, p_event_identity text default 'v1')`

Locks the original appointment, rejects missing/cancelled/completed/no-show appointments, creates or verifies the new appointment, marks the original rescheduled, and creates customer/staff reschedule intents in one transaction.

### `public.cancel_appointment_with_notification_outbox(p_appointment_id text, p_customer_intent jsonb, p_staff_intent jsonb, p_event_identity text default 'v1')`

Locks the appointment, rejects missing/completed/no-show appointments, makes repeated cancellation idempotent, and creates customer/staff cancellation intents exactly once.

### `public.claim_notification_outbox(batch_limit integer default 20, lease_seconds integer default 300)`

Claims due `pending`/`failed` rows and stale `processing` rows whose lease expired. It assigns a UUID `claim_token`, sets `lease_expires_at`, increments `attempt_count`, and returns the claimed rows.

### `public.mark_notification_outbox_sent(p_id uuid, p_claim_token uuid, p_provider_message_id text)`

Marks a row sent only when the row is still `processing` and the claim token matches.

### `public.mark_notification_outbox_failed(p_id uuid, p_claim_token uuid, p_status text, p_last_error text, p_next_attempt_at timestamptz default null)`

Marks a row `failed`, `permanently_failed`, or `delivery_unknown` only when the claim token matches.

## Transaction Boundaries

The production transaction boundaries are the three appointment RPCs:

- appointment creation plus customer/staff notification intents
- appointment reschedule plus customer/staff notification intents
- appointment cancellation plus customer/staff notification intents

PostgreSQL functions execute atomically in the caller's transaction. A process crash cannot leave a committed appointment mutation without both required notification intents when the application uses these RPCs.

The Python fallback path exists only for compatibility during migration rollout. In production/atomic mode, fallback must fail closed and must not report notification-complete success.

## Retry, Lease, And Recovery Behavior

The scheduler runs `run_due_notification_outbox()` every 60 seconds.

Worker behavior:

- claim a bounded batch with `claim_notification_outbox`
- process only rows returned with a claim token
- mark sent/failed through token-protected RPCs
- prevent an old worker from updating a row reclaimed by another worker
- retry temporary failures with controlled backoff
- stop retrying permanent failures
- recover stale `processing` rows only after `lease_expires_at`

## Uncertain Meta Delivery

Exactly-once WhatsApp delivery cannot be guaranteed when Meta may have accepted a message but AirZen did not receive a reliable response. Those cases use `delivery_unknown`.

`delivery_unknown` rows are not retried automatically as ordinary temporary failures. They remain visible for reconciliation or controlled manual review.

## Data Retention

`recipient_phone` and `payload` are operationally necessary while work is retryable, but sensitive.

Recommended retention:

- keep retryable rows until completion or manual resolution
- after the audit window, redact `recipient_phone` and sensitive `payload` fields for `sent`, `permanently_failed`, and `delivery_unknown`
- retain hashed references, appointment id, notification type, status, timestamps, and provider id
- never expose outbox rows to browser clients
- never log full phone numbers, message bodies, tokens, template payloads, or customer personal data

## Staging Integration Tests

The staging test suite must run against an isolated Supabase project after migration:

Required guarded environment:

- `AIRZEN_TEST_ENVIRONMENT=staging`
- `ALLOW_STAGING_DB_WRITES=true`
- `AIRZEN_REQUIRE_ATOMIC_OUTBOX=true`
- `STAGING_SUPABASE_URL=https://<isolated-staging-project-ref>.supabase.co`
- `STAGING_SUPABASE_SERVICE_KEY=<staging service-role key>`
- `AIRZEN_ALLOWED_STAGING_PROJECT_REF=<isolated-staging-project-ref>`
- `AIRZEN_PRODUCTION_PROJECT_REF=<production-project-ref>` when known

The gate extracts the project reference from `STAGING_SUPABASE_URL` and requires an exact match with `AIRZEN_ALLOWED_STAGING_PROJECT_REF`. It explicitly rejects a staging reference that equals `AIRZEN_PRODUCTION_PROJECT_REF` or the project reference derived from `SUPABASE_URL`. This avoids relying on human-readable project names such as `staging` or `test`, because Supabase project references are normally random.

- missing appointment
- conflicting existing appointment ID
- duplicate create
- duplicate reschedule
- invalid reschedule state
- duplicate cancellation
- missing cancellation target
- overlapping claims
- stale lease recovery
- old worker token rejected
- `delivery_unknown` behavior
- full transaction rollback when either notification intent fails

## Rollback

`notification_outbox_rollback.sql` is idempotent and uses `to_regclass` and `to_regprocedure`. It succeeds when all objects exist, only some exist, none exist, or migration failed halfway. It revokes privileges before dropping and does not reopen anonymous access.
