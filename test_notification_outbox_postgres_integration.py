import os
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from scripts.run_airzen_production_gate import validate_staging_database_target


pytestmark = pytest.mark.skipif(
    not (
        os.getenv("AIRZEN_TEST_ENVIRONMENT") == "staging"
        and os.getenv("ALLOW_STAGING_DB_WRITES") == "true"
        and os.getenv("STAGING_SUPABASE_URL")
        and os.getenv("AIRZEN_ALLOWED_STAGING_PROJECT_REF")
        and (os.getenv("STAGING_SUPABASE_SERVICE_KEY") or os.getenv("SUPABASE_SERVICE_KEY"))
    ),
    reason="isolated staging Supabase writes are not explicitly enabled",
)


@pytest.fixture()
def supabase_client():
    from supabase import create_client

    url = os.environ["STAGING_SUPABASE_URL"]
    key = os.getenv("STAGING_SUPABASE_SERVICE_KEY") or os.environ["SUPABASE_SERVICE_KEY"]
    ok_target, staging_ref, detail = validate_staging_database_target()
    print(f"Detected staging Supabase project_ref={staging_ref or '<unavailable>'}; {detail}")
    if not ok_target:
        pytest.fail(f"Refusing to run staging integration tests: {detail}")
    return create_client(url, key)


def _appointment(prefix):
    appt_id = f"{prefix}_{uuid.uuid4().hex[:12]}"
    return {
        "id": appt_id,
        "name": "AIRZEN_GATE_CUSTOMER",
        "phone": "+6500000000",
        "date": "2099-01-01",
        "time": "10:00",
        "service": "AIRZEN_GATE_SERVICE",
        "status": "booked",
        "staff_id": "AIRZEN_GATE_STAFF",
        "staff_name": "AIRZEN_GATE_STAFF",
        "duration_minutes": 30,
        "buffer_minutes": 15,
        "timezone": "Asia/Singapore",
    }


def _intent(kind, recipient_phone="+6500000000"):
    return {
        "recipient_phone": recipient_phone,
        "notification_type": kind,
        "template_purpose": "appointment_confirmation_template" if "customer" in kind else "staff_appointment_notification_template",
        "payload": {"gate": True},
    }


def test_atomic_create_duplicate_and_conflict(supabase_client):
    appt = _appointment("create")
    customer = _intent("appointment_customer_confirmation")
    staff = _intent("appointment_staff_confirmation")
    first = supabase_client.rpc("create_appointment_with_notification_outbox", {
        "p_appointment": appt,
        "p_customer_intent": customer,
        "p_staff_intent": staff,
        "p_event_identity": "gate_v1",
    }).execute()
    second = supabase_client.rpc("create_appointment_with_notification_outbox", {
        "p_appointment": appt,
        "p_customer_intent": customer,
        "p_staff_intent": staff,
        "p_event_identity": "gate_v1",
    }).execute()
    assert first.data["appointment_id"] == second.data["appointment_id"]
    rows = supabase_client.table("notification_outbox").select("id").eq("appointment_id", appt["id"]).execute().data
    assert len(rows) == 2
    conflicting = {**appt, "phone": "+6500000001"}
    with pytest.raises(Exception):
        supabase_client.rpc("create_appointment_with_notification_outbox", {
            "p_appointment": conflicting,
            "p_customer_intent": customer,
            "p_staff_intent": staff,
            "p_event_identity": "gate_v1",
        }).execute()


def test_missing_cancel_target_and_duplicate_cancellation(supabase_client):
    missing = f"missing_{uuid.uuid4().hex[:12]}"
    with pytest.raises(Exception):
        supabase_client.rpc("cancel_appointment_with_notification_outbox", {
            "p_appointment_id": missing,
            "p_customer_intent": _intent("appointment_customer_cancellation"),
            "p_staff_intent": _intent("appointment_staff_cancellation"),
            "p_event_identity": "gate_cancel_v1",
        }).execute()

    appt = _appointment("cancel")
    supabase_client.rpc("create_appointment_with_notification_outbox", {
        "p_appointment": appt,
        "p_customer_intent": _intent("appointment_customer_confirmation"),
        "p_staff_intent": _intent("appointment_staff_confirmation"),
        "p_event_identity": "gate_create_v1",
    }).execute()
    first = supabase_client.rpc("cancel_appointment_with_notification_outbox", {
        "p_appointment_id": appt["id"],
        "p_customer_intent": _intent("appointment_customer_cancellation"),
        "p_staff_intent": _intent("appointment_staff_cancellation"),
        "p_event_identity": "gate_cancel_v1",
    }).execute()
    second = supabase_client.rpc("cancel_appointment_with_notification_outbox", {
        "p_appointment_id": appt["id"],
        "p_customer_intent": _intent("appointment_customer_cancellation"),
        "p_staff_intent": _intent("appointment_staff_cancellation"),
        "p_event_identity": "gate_cancel_v1",
    }).execute()
    assert first.data["appointment_id"] == second.data["appointment_id"]
    rows = supabase_client.table("notification_outbox").select("id,notification_type").eq("appointment_id", appt["id"]).execute().data
    assert len([r for r in rows if "cancellation" in r["notification_type"]]) == 2


def test_claim_lease_old_worker_token_rejected_and_delivery_unknown(supabase_client):
    appt = _appointment("lease")
    supabase_client.rpc("create_appointment_with_notification_outbox", {
        "p_appointment": appt,
        "p_customer_intent": _intent("appointment_customer_confirmation"),
        "p_staff_intent": _intent("appointment_staff_confirmation"),
        "p_event_identity": "gate_lease_v1",
    }).execute()
    claimed = supabase_client.rpc("claim_notification_outbox", {"batch_limit": 1, "lease_seconds": 30}).execute().data
    assert claimed and claimed[0]["claim_token"]
    row = claimed[0]
    with pytest.raises(Exception):
        supabase_client.rpc("mark_notification_outbox_sent", {
            "p_id": row["id"],
            "p_claim_token": str(uuid.uuid4()),
            "p_provider_message_id": "wamid.bad",
        }).execute()
    updated = supabase_client.rpc("mark_notification_outbox_failed", {
        "p_id": row["id"],
        "p_claim_token": row["claim_token"],
        "p_status": "delivery_unknown",
        "p_last_error": "timeout_after_submit",
        "p_next_attempt_at": None,
    }).execute()
    assert updated.data["status"] == "delivery_unknown"


def test_duplicate_create_rejects_different_staff_timezone_and_duration(supabase_client):
    appt = _appointment("dup_material")
    customer = _intent("appointment_customer_confirmation")
    staff = _intent("appointment_staff_confirmation")
    supabase_client.rpc("create_appointment_with_notification_outbox", {
        "p_appointment": appt,
        "p_customer_intent": customer,
        "p_staff_intent": staff,
        "p_event_identity": "gate_material_v1",
    }).execute()
    for changed in (
        {**appt, "staff_id": "OTHER_STAFF"},
        {**appt, "timezone": "UTC"},
        {**appt, "duration_minutes": 45},
    ):
        with pytest.raises(Exception):
            supabase_client.rpc("create_appointment_with_notification_outbox", {
                "p_appointment": changed,
                "p_customer_intent": customer,
                "p_staff_intent": staff,
                "p_event_identity": "gate_material_v1",
            }).execute()


def test_second_reschedule_child_is_rejected(supabase_client):
    old = _appointment("resched_old")
    supabase_client.rpc("create_appointment_with_notification_outbox", {
        "p_appointment": old,
        "p_customer_intent": _intent("appointment_customer_confirmation"),
        "p_staff_intent": _intent("appointment_staff_confirmation"),
        "p_event_identity": "gate_resched_base_v1",
    }).execute()
    first = {**old, "id": f"new_{uuid.uuid4().hex[:12]}", "date": "2099-01-02", "rescheduled_from": old["id"]}
    second = {**old, "id": f"new_{uuid.uuid4().hex[:12]}", "date": "2099-01-03", "rescheduled_from": old["id"]}
    supabase_client.rpc("reschedule_appointment_with_notification_outbox", {
        "p_old_appointment_id": old["id"],
        "p_new_appointment": first,
        "p_customer_intent": _intent("appointment_customer_reschedule"),
        "p_staff_intent": _intent("appointment_staff_reschedule"),
        "p_event_identity": "gate_resched_v1",
    }).execute()
    with pytest.raises(Exception):
        supabase_client.rpc("reschedule_appointment_with_notification_outbox", {
            "p_old_appointment_id": old["id"],
            "p_new_appointment": second,
            "p_customer_intent": _intent("appointment_customer_reschedule"),
            "p_staff_intent": _intent("appointment_staff_reschedule"),
            "p_event_identity": "gate_resched_v1",
        }).execute()


def test_two_concurrent_conflicting_reschedules_only_one_wins(supabase_client):
    old = _appointment("resched_race")
    supabase_client.rpc("create_appointment_with_notification_outbox", {
        "p_appointment": old,
        "p_customer_intent": _intent("appointment_customer_confirmation"),
        "p_staff_intent": _intent("appointment_staff_confirmation"),
        "p_event_identity": "gate_race_base_v1",
    }).execute()
    children = [
        {**old, "id": f"race_{uuid.uuid4().hex[:12]}", "date": "2099-02-01", "rescheduled_from": old["id"]},
        {**old, "id": f"race_{uuid.uuid4().hex[:12]}", "date": "2099-02-02", "rescheduled_from": old["id"]},
    ]

    def attempt(child):
        try:
            supabase_client.rpc("reschedule_appointment_with_notification_outbox", {
                "p_old_appointment_id": old["id"],
                "p_new_appointment": child,
                "p_customer_intent": _intent("appointment_customer_reschedule"),
                "p_staff_intent": _intent("appointment_staff_reschedule"),
                "p_event_identity": "gate_race_v1",
            }).execute()
            return "ok"
        except Exception:
            return "failed"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, children))
    assert results.count("ok") == 1
    assert results.count("failed") == 1
