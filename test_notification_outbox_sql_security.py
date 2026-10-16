from pathlib import Path


ROOT = Path(__file__).resolve().parent
MIGRATION = (ROOT / "notification_outbox_migration.sql").read_text(encoding="utf-8").lower()
ROLLBACK = (ROOT / "notification_outbox_rollback.sql").read_text(encoding="utf-8").lower()
DESIGN = (ROOT / "notification_outbox_design.md").read_text(encoding="utf-8").lower()


BACKEND_FUNCTIONS = (
    "claim_notification_outbox(integer, integer)",
    "mark_notification_outbox_sent(uuid, uuid, text)",
    "mark_notification_outbox_failed(uuid, uuid, text, text, timestamptz)",
    "insert_notification_outbox_intent(text, text, text, text, text, text, jsonb, text, text, text, timestamptz)",
    "create_appointment_with_notification_outbox(jsonb, jsonb, jsonb, text)",
    "reschedule_appointment_with_notification_outbox(text, jsonb, jsonb, jsonb, text)",
    "cancel_appointment_with_notification_outbox(text, jsonb, jsonb, text)",
)


def test_public_anon_authenticated_privileges_are_revoked_and_service_role_is_granted():
    assert "revoke all on public.notification_outbox from public, anon, authenticated" in MIGRATION
    assert "grant usage on schema public to service_role" in MIGRATION
    assert "grant select, insert, update, delete on public.notification_outbox to service_role" in MIGRATION
    assert "grant execute on function public.airzen_notification_dedupe_key(text, text, text, text) to service_role" in MIGRATION
    for signature in BACKEND_FUNCTIONS:
        assert f"revoke all on function public.{signature} from public, anon, authenticated" in MIGRATION
        assert f"grant execute on function public.{signature} to service_role" in MIGRATION
    assert "revoke all on function public.touch_notification_outbox_updated_at() from public, anon, authenticated" in MIGRATION


def test_security_invoker_rls_and_no_policies_are_asserted():
    assert "security definer" not in MIGRATION
    assert "security invoker" in MIGRATION
    assert "alter table public.notification_outbox enable row level security" in MIGRATION
    assert "alter table public.notification_outbox no force row level security" in MIGRATION
    assert "pg_catalog.pg_policy" in MIGRATION
    assert "notification_outbox_dedupe_key_key" in MIGRATION


def test_appointment_preflight_checks_every_referenced_column_and_service_role_access():
    for phrase in (
        "required table public.appointments does not exist",
        "public.appointments.id must be text",
        "public.appointments.name must be text",
        "public.appointments.phone must be text",
        "public.appointments.date must be text",
        "public.appointments.time must be text",
        "public.appointments.service must be text",
        "public.appointments.status must be text",
        "public.appointments.staff_id must be text",
        "public.appointments.staff_name must be text",
        "public.appointments.duration_minutes must be integer-compatible",
        "public.appointments.buffer_minutes must be integer-compatible",
        "public.appointments.timezone must be text",
        "public.appointments.created_at must be text",
        "public.appointments.rescheduled_at must be timestamptz",
        "public.appointments.rescheduled_from must be text",
        "public.appointments.cancelled_at must be timestamptz",
        "service_role must have select and insert and update on public.appointments",
    ):
        assert phrase in MIGRATION
    assert "has_table_privilege('service_role', 'public.appointments', 'select')" in MIGRATION
    assert "has_table_privilege('service_role', 'public.appointments', 'insert')" in MIGRATION
    assert "has_table_privilege('service_role', 'public.appointments', 'update')" in MIGRATION
    assert "has_table_privilege('service_role', 'public.appointments', 'select,insert" not in MIGRATION
    assert "has_table_privilege('service_role', 'public.appointments', 'select, insert" not in MIGRATION


def test_atomic_rpcs_lock_verify_and_reject_conflicts():
    assert "for update" in MIGRATION
    assert "conflicting appointment already exists" in MIGRATION
    assert "conflicting rescheduled appointment already exists" in MIGRATION
    assert "original appointment already has different replacement" in MIGRATION
    assert "idx_appointments_one_reschedule_child" in MIGRATION
    assert "original appointment does not exist" in MIGRATION
    assert "cannot reschedule appointment from status" in MIGRATION
    assert "appointment does not exist" in MIGRATION
    assert "cannot cancel appointment from status" in MIGRATION
    assert "notification intent was not persisted for deterministic dedupe key" in MIGRATION


def test_dedupe_keys_are_deterministic_and_do_not_update_existing_sent_rows():
    assert "airzen_notification_dedupe_key" in MIGRATION
    assert "digest(" not in MIGRATION
    assert "md5(" in MIGRATION
    assert "p_operation" in MIGRATION
    assert "p_appointment_id" in MIGRATION
    assert "p_recipient_type" in MIGRATION
    assert "p_event_identity" in MIGRATION
    assert "on conflict (dedupe_key) do nothing" in MIGRATION
    assert "do update" not in MIGRATION
    assert "caller-provided arbitrary dedupe keys are not trusted" in DESIGN
    assert "service_role must execute %" in MIGRATION


def test_worker_lease_and_uncertain_delivery_are_supported():
    assert "claim_token uuid" in MIGRATION
    assert "lease_expires_at timestamptz" in MIGRATION
    assert "v_claim_token uuid := pg_catalog.gen_random_uuid()" in MIGRATION
    assert "claim_token = p_claim_token" in MIGRATION
    assert "delivery_unknown" in MIGRATION
    assert "exactly-once whatsapp delivery cannot be guaranteed" in DESIGN


def test_postflight_checks_cover_all_functions_and_roles():
    for signature in BACKEND_FUNCTIONS:
        assert f"public.{signature.replace(', ', ',')}" in MIGRATION
    for role_phrase in (
        "public must not execute %",
        "anon must not execute %",
        "authenticated must not execute %",
        "service_role must execute %",
    ):
        assert role_phrase in MIGRATION
    assert "service_role must have select and insert and update and delete on notification_outbox" in MIGRATION
    assert "has_table_privilege('service_role', 'public.notification_outbox', 'select')" in MIGRATION
    assert "has_table_privilege('service_role', 'public.notification_outbox', 'insert')" in MIGRATION
    assert "has_table_privilege('service_role', 'public.notification_outbox', 'update')" in MIGRATION
    assert "has_table_privilege('service_role', 'public.notification_outbox', 'delete')" in MIGRATION
    assert "has_table_privilege('service_role', 'public.notification_outbox', 'select,insert" not in MIGRATION
    assert "has_table_privilege('service_role', 'public.notification_outbox', 'select, insert" not in MIGRATION


def test_duplicate_create_compares_all_material_fields():
    for field in ("name", "phone", "date", "time", "service", "status", "staff_id", "staff_name", "duration_minutes", "buffer_minutes", "timezone"):
        assert f"v_existing.{field}" in MIGRATION


def test_rollback_is_idempotent_and_handles_partial_migration():
    assert "to_regclass('public.notification_outbox')" in ROLLBACK
    for signature in BACKEND_FUNCTIONS:
        assert f"public.{signature.replace(', ', ',')}" in ROLLBACK
    assert "drop trigger if exists trg_notification_outbox_updated_at" in ROLLBACK
    assert "drop table public.notification_outbox" in ROLLBACK


def test_staging_integration_plan_is_not_static_only():
    assert (ROOT / "test_notification_outbox_postgres_integration.py").exists()
    gate = (ROOT / "scripts" / "run_airzen_production_gate.py").read_text(encoding="utf-8").lower()
    assert 'assert "test_notification_outbox_postgres_integration.py" or true' not in gate
    assert "postgres_atomic_outbox_integration" in gate
    assert "test_notification_outbox_postgres_integration.py" in gate
    assert "validate_staging_database_target()[0]" in gate
    assert "airzen_allowed_staging_project_ref" in gate
    assert "airzen_production_project_ref" in gate
    assert "staging integration tests" in DESIGN
    assert "old worker token rejected" in DESIGN
