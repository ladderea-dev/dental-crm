import asyncio
import gc
import os
import uuid
from unittest.mock import patch

import pytest
import pytest_asyncio

from airzen_repository import MySQLRepository


def _mysql_env_ready() -> bool:
    return (
        os.getenv("AIRZEN_ALLOW_MYSQL_STAGING_TESTS", "").lower() == "true"
        and os.getenv("AIRZEN_DB_BACKEND", "").lower() == "mysql"
        and os.getenv("MYSQL_HOST")
        and os.getenv("MYSQL_DATABASE")
        and os.getenv("MYSQL_USER")
    )


pytestmark = pytest.mark.skipif(
    not _mysql_env_ready(),
    reason="real MySQL staging tests require AIRZEN_ALLOW_MYSQL_STAGING_TESTS=true and MYSQL_* env vars",
)


def _driver_available():
    try:
        import aiomysql  # noqa: F401
        return True
    except Exception:
        return False


pytestmark = [
    pytestmark,
    pytest.mark.skipif(not _driver_available(), reason="aiomysql is not installed"),
    pytest.mark.asyncio(loop_scope="module"),
]


@pytest_asyncio.fixture(scope="module", autouse=True, loop_scope="module")
async def _finalize_mysql_connections_before_loop_close():
    yield
    gc.collect()
    await asyncio.sleep(0)


@pytest.fixture
def marker():
    return uuid.uuid4().hex[:12]


@pytest_asyncio.fixture(loop_scope="module")
async def repo(marker):
    repo = MySQLRepository()
    try:
        health = await repo.backend_health_check()
        if not health["ok"]:
            pytest.fail(f"MySQL integration schema is not ready: {health}")
        yield repo
    finally:
        try:
            if repo._pool is not None:
                await _cleanup(repo, marker)
        finally:
            await _close_repo(repo)


async def _close_repo(repo: MySQLRepository):
    pool = repo._pool
    if pool is not None:
        pool.terminate()
    await repo.close()


async def _cleanup(repo: MySQLRepository, marker: str):
    phone = _phone(marker)
    appointment_ids = (
        "mysql_it_appt_" + marker,
        "mysql_it_rollback_" + marker,
        "mysql_it_reschedule_old_" + marker,
        "mysql_it_reschedule_new_" + marker,
    )
    placeholders = ",".join(["%s"] * len(appointment_ids))
    pool = await repo._ensure_pool()
    async with pool.acquire() as conn:
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"DELETE FROM notification_outbox WHERE appointment_id IN ({placeholders})",
                    appointment_ids,
                )
                await cur.execute("DELETE FROM followup_actions WHERE source_id=%s", ("mysql_it_" + marker,))
                await cur.execute(f"DELETE FROM appointments WHERE id IN ({placeholders})", appointment_ids)
                await cur.execute("DELETE FROM appointments WHERE phone=%s", (phone,))
                await cur.execute("DELETE FROM appointment_staff WHERE id=%s", ("mysql_it_staff_" + marker,))
                await cur.execute("DELETE FROM whatsapp_health_test_results WHERE test_run_id=%s", ("mysql_hu_" + marker,))
                await cur.execute("DELETE FROM whatsapp_health_test_runs WHERE id=%s", ("mysql_hu_" + marker,))
                await cur.execute("DELETE FROM whatsapp_test_recipients WHERE id=%s", ("mysql_hr_" + marker,))
                await cur.execute("DELETE FROM broadcast_recipients WHERE campaign_id=%s", ("mysql_it_broadcast_" + marker,))
                await cur.execute("DELETE FROM broadcast_campaigns WHERE id=%s", ("mysql_it_broadcast_" + marker,))
                await cur.execute("DELETE FROM automation_actions WHERE phone_number=%s", (phone,))
                await cur.execute("DELETE FROM campaigns WHERE id=%s", ("mysql_it_campaign_" + marker,))
                await cur.execute("DELETE FROM agent_profiles WHERE id IN (%s,%s)", ("mysql_it_profile_a_" + marker, "mysql_it_profile_b_" + marker))
                await cur.execute("DELETE FROM call_logs WHERE phone_number=%s", (phone,))
                await cur.execute("DELETE FROM error_logs WHERE source=%s", ("mysql_it_" + marker,))
                await cur.execute("DELETE FROM lead_statuses WHERE name=%s", ("mysql_it_" + marker,))
                await cur.execute("DELETE FROM crm_tags WHERE name=%s", ("mysql_it_" + marker,))
                await cur.execute("DELETE FROM whatsapp_logs WHERE phone_number=%s", (phone,))
                await cur.execute("DELETE FROM whatsapp_messages WHERE phone_number=%s", (phone,))
                await cur.execute("DELETE FROM whatsapp_conversations WHERE phone_number=%s", (phone,))
                await cur.execute("DELETE FROM contact_memory WHERE phone_number=%s", (phone,))
                await cur.execute("DELETE FROM contact_memory WHERE phone_number=%s", (phone + "_blocker",))
                await cur.execute("DELETE FROM crm_contacts WHERE phone_number=%s", (phone,))
                await cur.execute(
                    "DELETE FROM settings WHERE `key` IN (%s,%s)",
                    ("MYSQL_IT_SETTING_" + marker, "MYSQL_IT_SETTING_SECOND_" + marker),
                )
            await conn.commit()
        except BaseException:
            try:
                await conn.rollback()
            except Exception:
                pass
            raise


def _phone(marker):
    return f"mysql_it_{marker}"


async def test_mysql_backend_health(repo):
    health = await repo.backend_health_check()
    assert health["backend"] == "mysql"
    assert health["connection_ok"] is True
    assert health["ok"] is True


async def test_crm_upsert_duplicate_phone_merge(repo, marker):
    phone = _phone(marker)
    first_id = str(uuid.uuid4())
    second_id = str(uuid.uuid4())
    first = await repo.upsert_crm_contact({
        "id": first_id,
        "phone_number": phone,
        "lead_name": "First",
        "source": "WhatsApp",
        "address": "217B Sumang Walk",
        "location": "Singapore 822217",
        "service_type": "Aircon servicing",
        "crm_status": "New Lead",
        "crm_notes": "Durable canonical note",
        "last_call_outcome": "mysql_it_" + marker,
    })
    second = await repo.upsert_crm_contact({
        "id": second_id,
        "phone_number": phone,
        "lead_name": "Second",
        "source": "WhatsApp",
        "notes": "runtime-only alias",
    })
    assert first_id != second_id
    assert first["phone_number"] == phone
    assert second["phone_number"] == phone
    assert second["id"] == first["id"] == first_id
    stored = await repo.get_crm_contact_by_phone(phone)
    assert stored["lead_name"] == "Second"
    assert stored["address"] == "217B Sumang Walk"
    assert stored["location"] == "Singapore 822217"
    assert stored["service_type"] == "Aircon servicing"
    assert stored["crm_notes"] == "Durable canonical note"
    assert "notes" not in stored
    assert [row["id"] for row in await repo.list_crm_contacts(
        status="New Lead", outcome="mysql_it_" + marker,
    )] == [first_id]


async def test_contact_memory_replacement_rolls_back_on_insert_failure(repo, marker):
    phone = _phone(marker)
    blocker_phone = phone + "_blocker"
    await repo.add_contact_memory(phone, "Original durable memory")
    blocker_id = await repo.add_contact_memory(blocker_phone, "Primary-key blocker")

    with patch("airzen_repository.new_uuid", return_value=blocker_id):
        with pytest.raises(Exception):
            await repo.replace_contact_memory(phone, "Replacement that must roll back")

    after_failure = await repo.get_contact_memory(phone)
    assert [row["insight"] for row in after_failure] == ["Original durable memory"]

    await repo.replace_contact_memory(phone, "Compressed durable summary")
    after_success = await repo.get_contact_memory(phone)
    assert [row["insight"] for row in after_success] == ["Compressed durable summary"]


async def test_followup_claim_concurrency_returns_action_to_one_worker(repo, marker):
    phone = _phone(marker)
    action = await repo.create_followup_action({
        "phone_number": phone,
        "event_type": "no_response",
        "action_type": "whatsapp_message",
        "channel": "whatsapp",
        "scheduled_at": "2026-08-01 00:00:00.000000",
        "status": "scheduled",
        "priority": 1,
        "source": "mysql_integration",
        "source_id": "mysql_it_" + marker,
        "payload": {"message": "Hello"},
        "result": {},
        "attempt_number": 0,
        "max_attempts": 3,
    })
    second_repo = MySQLRepository()
    try:
        worker_a, worker_b = await asyncio.gather(
            repo.claim_followup_actions("2026-08-02 00:00:00.000000", limit=1),
            second_repo.claim_followup_actions("2026-08-02 00:00:00.000000", limit=1),
        )
    finally:
        await _close_repo(second_repo)

    assert not ({row["id"] for row in worker_a} & {row["id"] for row in worker_b})
    claimed = worker_a + worker_b
    assert [row["id"] for row in claimed] == [action["id"]]
    assert claimed[0]["status"] == "processing"
    assert claimed[0]["attempt_number"] == 1
    assert await repo.update_followup_action_status(action["id"], "completed", {"sent": True})
    assert not await repo.update_followup_action_status(action["id"], "failed", {"sent": False})


async def test_conversation_message_and_appointment_state(repo, marker):
    phone = _phone(marker)
    conv = await repo.get_or_create_whatsapp_conversation(phone, "Karuna")
    same = await repo.get_or_create_whatsapp_conversation(phone, "Karuna")
    assert conv["id"] == same["id"]

    msg = await repo.save_whatsapp_message({
        "conversation_id": conv["id"],
        "phone_number": phone,
        "direction": "inbound",
        "message_type": "text",
        "message_text": "hello",
        "provider_message_id": "mysql_it_msg_" + marker,
        "provider_status": "processing",
        "raw_payload": {"test": True},
    })
    assert msg["provider_message_id"].startswith("mysql_it_msg_")
    found = await repo.get_inbound_whatsapp_message_by_provider_id(msg["provider_message_id"])
    assert found["id"] == msg["id"]
    assert await repo.update_inbound_whatsapp_message_status(msg["provider_message_id"], "processed")

    outbound = await repo.save_whatsapp_message({
        "conversation_id": conv["id"],
        "phone_number": phone,
        "direction": "outbound",
        "message_type": "template",
        "message_text": "booking confirmed",
        "provider_message_id": "mysql_it_outbound_" + marker,
        "provider_status": "sent",
        "raw_payload": {"test": True},
        "ai_generated": True,
    })
    await repo.create_whatsapp_log({
        "phone_number": phone,
        "event_type": "integration_delivery",
        "template_name": "test_template",
        "language": "en",
        "parameters": ["Karuna"],
        "status": "sent",
        "provider_message_id": msg["provider_message_id"],
        "error_message": "",
        "source_type": "integration_test",
        "source_id": marker,
        "idempotency_key": "mysql_it:" + marker,
        "cooldown_until": "",
    })
    delivery = await repo.record_whatsapp_delivery_status(
        msg["provider_message_id"],
        "delivered",
        "2026-08-23T01:02:03+00:00",
    )
    assert delivery == {"matched": True, "message_matched": True, "log_matched": True}
    assert (await repo.get_inbound_whatsapp_message_by_provider_id(msg["provider_message_id"]))["provider_status"] == "delivered"
    logs = await repo.list_whatsapp_logs(phone=phone, provider_message_id=msg["provider_message_id"])
    assert logs[0]["status"] == "delivered"

    conversations = await repo.list_whatsapp_conversations(phone=phone)
    assert [row["id"] for row in conversations] == [conv["id"]]
    assert (await repo.get_whatsapp_conversation_by_id(conv["id"]))["phone_number"] == phone
    messages = await repo.list_whatsapp_messages(conv["id"])
    assert [row["id"] for row in messages] == [msg["id"], outbound["id"]]
    activity = await repo.get_whatsapp_message_activity()
    assert activity["last_inbound_whatsapp_message_at"] is not None
    assert activity["last_outbound_ai_message_at"] is not None

    assert await repo.soft_delete_whatsapp_message(outbound["id"], "integration_test")
    assert [row["id"] for row in await repo.list_whatsapp_messages(conv["id"])] == [msg["id"]]
    assert await repo.soft_delete_whatsapp_conversation(conv["id"], "integration_test")
    assert await repo.get_whatsapp_conversation_by_id(conv["id"]) is None
    restored = await repo.get_or_create_whatsapp_conversation(phone, "Karuna")
    assert restored["id"] == conv["id"]
    assert not restored["is_deleted"]

    await repo.persist_appointment_state(conv["id"], {"status": "awaiting_intake", "requested_time": "10:00"})
    assert (await repo.load_appointment_state(conv["id"]))["requested_time"] == "10:00"

    await repo.close()
    reopened = await repo.get_or_create_whatsapp_conversation(phone, "Karuna")
    assert reopened["id"] == conv["id"]
    assert (await repo.load_appointment_state(reopened["id"]))["requested_time"] == "10:00"


async def test_staff_availability_appointment_followup_and_outbox(repo, marker):
    phone = _phone(marker)
    staff_id = "mysql_it_staff_" + marker
    appt_id = "mysql_it_appt_" + marker
    now = "2026-08-14 02:00:00.000000"
    await repo._execute(
        """
        INSERT INTO appointment_staff
          (id,name,email,whatsapp_number,role,working_days,start_time,end_time,timezone,active,round_robin_order,created_at,updated_at)
        VALUES (%s,'Test Staff','staff@example.com',%s,'service_staff',%s,'09:00','18:00','Asia/Singapore',1,0,%s,%s)
        ON DUPLICATE KEY UPDATE active=1
        """,
        (staff_id, phone, '["mon","tue","wed","thu","fri","sat","sun"]', now, now),
    )
    staff = await repo.get_active_staff()
    assert any(row["id"] == staff_id for row in staff)
    updated_staff = await repo.upsert_appointment_staff({
        "id": staff_id,
        "name": "Updated Test Staff",
        "working_days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
        "updated_at": now,
    }, staff_id=staff_id)
    assert updated_staff["id"] == staff_id
    assert updated_staff["name"] == "Updated Test Staff"
    assert (await repo.get_appointment_staff(staff_id))["id"] == staff_id
    assert any(row["id"] == staff_id for row in await repo.list_appointment_staff(include_inactive=True))
    assert await repo.update_appointment_staff_calendar(staff_id, {
        "calendar_sync_error": "integration-test",
        "last_calendar_sync_at": now,
    })
    assert (await repo.get_appointment_staff(staff_id))["calendar_sync_error"] == "integration-test"
    assert await repo.set_appointment_staff_active(staff_id, False)
    assert not any(row["id"] == staff_id for row in await repo.get_active_staff())
    assert await repo.set_appointment_staff_active(staff_id, True)

    appointment = {
        "id": appt_id,
        "name": "Karuna",
        "phone": phone,
        "date": "2026-08-20",
        "time": "10:00",
        "service": "General Servicing",
        "status": "booked",
        "staff_id": staff_id,
        "staff_name": "Test Staff",
        "duration_minutes": 60,
        "buffer_minutes": 15,
        "timezone": "Asia/Singapore",
        "conversation_id": "mysql_it_conversation_" + marker,
        "intake_data": {"units": "2"},
    }
    intent = {
        "recipient_phone": phone,
        "notification_type": "appointment_customer_confirmation",
        "template_purpose": "appointment_confirmation_template",
        "payload": {"name": "Karuna"},
    }
    staff_intent = {
        "recipient_phone": phone,
        "notification_type": "appointment_staff_confirmation",
        "template_purpose": "staff_appointment_notification_template",
        "payload": {"name": "Karuna"},
    }
    start_code = {
        "staff_id": staff_id,
        "otp_hash": "7" * 64,
        "max_attempts": 5,
        "issued_at": now,
        "expires_at": "2026-08-21 10:00:00.000000",
    }
    result = await repo.create_appointment_with_outbox(appointment, intent, staff_intent, start_code=start_code)
    assert result["success"] is True
    stored_start_code = await repo._fetchone(
        "SELECT staff_id,otp_hash,status FROM appointment_start_otps WHERE appointment_id=%s",
        (appt_id,),
    )
    assert stored_start_code == {"staff_id": staff_id, "otp_hash": "7" * 64, "status": "pending"}
    assert await repo.get_appointments_for_slot("2026-08-20", "10:00")
    assert (await repo.get_appointment(appt_id))["phone"] == phone
    assert [row["id"] for row in await repo.list_appointments_by_phone(phone, date="2026-08-20")] == [appt_id]
    assert [row["id"] for row in await repo.list_appointments(
        statuses=["booked"], staff_id=staff_id, date_from="2026-08-20", date_to="2026-08-20",
    )] == [appt_id]
    assert await repo.update_appointment(appt_id, {
        "telegram_notified": True,
        "telegram_reminder_sent": True,
        "telegram_reminder_sent_at": now,
        "calendar_sync_error": "integration-test",
        "last_calendar_sync_at": now,
    })
    appointment_after_update = await repo.get_appointment(appt_id)
    assert appointment_after_update["telegram_notified"] == 1
    assert appointment_after_update["calendar_sync_error"] == "integration-test"

    # Duplicate booking confirmation is idempotent: no second appointment row.
    await repo.create_appointment_with_outbox(appointment, intent, staff_intent, start_code=start_code)
    rows = await repo._fetchall("SELECT id FROM appointments WHERE id=%s", (appt_id,))
    assert len(rows) == 1

    action = await repo.create_followup_action({
        "phone_number": phone,
        "event_type": "booking_created",
        "action_type": "whatsapp_followup",
        "channel": "whatsapp",
        "scheduled_at": "2026-08-20 09:00:00.000000",
        "status": "scheduled",
        "source_id": "mysql_it_" + marker,
        "payload": {"appointment_id": appt_id},
        "result": {},
    })
    assert action["id"]

    # Leave exactly one due row, then race two independent pools for it.
    await repo._execute(
        "UPDATE notification_outbox SET next_attempt_at='2099-01-01 00:00:00.000000' WHERE appointment_id=%s AND recipient_type='staff'",
        (appt_id,),
    )
    second_repo = MySQLRepository()
    try:
        worker_a, worker_b = await asyncio.gather(
            repo.claim_notification_outbox(limit=1, lease_seconds=60),
            second_repo.claim_notification_outbox(limit=1, lease_seconds=60),
        )
    finally:
        await _close_repo(second_repo)
    assert not ({row["id"] for row in worker_a} & {row["id"] for row in worker_b})
    claimed = worker_a + worker_b
    assert len(claimed) == 1

    # A stale worker cannot complete or fail another worker's active claim.
    assert not await repo.complete_notification_outbox(claimed[0]["id"], str(uuid.uuid4()), "stale-provider-id")
    assert not await repo.fail_notification_outbox(claimed[0]["id"], str(uuid.uuid4()), "failed", "stale worker")
    assert await repo.fail_notification_outbox(claimed[0]["id"], claimed[0]["claim_token"], "failed", "mock failure", "2026-08-20 09:05:00.000000")
    reclaimed = await repo.claim_notification_outbox(limit=1, lease_seconds=60)
    assert len(reclaimed) == 1
    assert reclaimed[0]["id"] == claimed[0]["id"]
    assert await repo.complete_notification_outbox(reclaimed[0]["id"], reclaimed[0]["claim_token"], "mock-provider-id")

    # Standalone SHA-256 dedupe keys fit the schema and remain idempotent.
    dedupe_key = ("d" * 52) + marker
    standalone = {
        "id": str(uuid.uuid4()),
        "dedupe_key": dedupe_key,
        "recipient_type": "customer",
        "recipient_phone": phone,
        "notification_type": "appointment_customer_confirmation",
        "template_purpose": "appointment_confirmation_template",
        "appointment_id": appt_id,
        "payload": {"integration": True},
        "next_attempt_at": "2099-01-01 00:00:00.000000",
    }
    first = await repo.enqueue_notification(standalone)
    second = await repo.enqueue_notification({**standalone, "id": str(uuid.uuid4())})
    assert first["id"] == second["id"]
    assert len(await repo._fetchall("SELECT id FROM notification_outbox WHERE dedupe_key=%s", (dedupe_key,))) == 1

    # Recovery excludes a valid lease and atomically releases an expired claim.
    await repo._execute(
        """
        UPDATE notification_outbox
        SET status='processing',attempt_count=2,claim_token=%s,
            lease_expires_at='2026-08-20 08:00:00.000000',updated_at='2026-08-20 07:00:00.000000'
        WHERE id=%s
        """,
        (str(uuid.uuid4()), first["id"]),
    )
    assert await repo.recover_stuck_notification_outbox(15, 50) == 1
    recovered = (await repo._fetchall("SELECT status,claim_token,lease_expires_at,attempt_count FROM notification_outbox WHERE id=%s", (first["id"],)))[0]
    assert recovered["status"] == "failed"
    assert recovered["claim_token"] is None
    assert recovered["lease_expires_at"] is None
    assert recovered["attempt_count"] == 2

    cancel_customer = {
        "recipient_phone": phone,
        "notification_type": "appointment_customer_cancellation",
        "template_purpose": "appointment_cancellation_template",
        "payload": {"name": "Karuna"},
    }
    cancel_staff = {
        "recipient_phone": phone,
        "notification_type": "appointment_staff_cancellation",
        "template_purpose": "staff_appointment_cancellation_template",
        "payload": {"name": "Karuna"},
    }
    await repo.cancel_appointment_with_outbox(appt_id, cancel_customer, cancel_staff)
    await repo.cancel_appointment_with_outbox(appt_id, cancel_customer, cancel_staff)
    cancelled = await repo._fetchall("SELECT status FROM appointments WHERE id=%s", (appt_id,))
    assert cancelled[0]["status"] == "cancelled"
    cancel_rows = await repo._fetchall(
        "SELECT id FROM notification_outbox WHERE appointment_id=%s AND notification_type LIKE %s",
        (appt_id, "%cancellation"),
    )
    assert len(cancel_rows) == 2

    replacement_id = str(uuid.uuid4())
    replacement = await repo.reschedule_appointment_record(appt_id, {
        **appointment,
        "id": replacement_id,
        "date": "2026-08-22",
        "time": "12:00",
        "rescheduled_from": appt_id,
        "telegram_reminder_sent": False,
        "created_at": now,
    })
    assert replacement["id"] == replacement_id
    assert (await repo.get_appointment(appt_id))["status"] == "rescheduled"
    assert (await repo.get_appointment(replacement_id))["rescheduled_from"] == appt_id


async def test_appointment_outbox_transaction_rolls_back(repo, marker):
    appt_id = "mysql_it_rollback_" + marker
    appointment = {
        "id": appt_id,
        "name": "Rollback",
        "phone": _phone(marker),
        "date": "2026-08-21",
        "time": "11:00",
        "service": "General Servicing",
        "status": "booked",
        "staff_id": "mysql_it_staff_" + marker,
        "staff_name": "Test Staff",
        "duration_minutes": 60,
        "buffer_minutes": 15,
        "timezone": "Asia/Singapore",
    }
    good_intent = {
        "recipient_phone": _phone(marker),
        "notification_type": "appointment_customer_confirmation",
        "template_purpose": "appointment_confirmation_template",
        "payload": {},
    }
    bad_intent = {
        "recipient_phone": "",
        "notification_type": "appointment_staff_confirmation",
        "template_purpose": "staff_appointment_notification_template",
        "payload": {},
    }
    with pytest.raises(ValueError):
        await repo.create_appointment_with_outbox(appointment, good_intent, bad_intent)
    rows = await repo._fetchall("SELECT id FROM appointments WHERE id=%s", (appt_id,))
    assert rows == []

    old_id = "mysql_it_reschedule_old_" + marker
    new_id = "mysql_it_reschedule_new_" + marker
    old_appointment = {**appointment, "id": old_id, "status": "booked"}
    new_appointment = {
        **appointment,
        "id": new_id,
        "date": "2026-08-22",
        "time": "12:00",
        "status": "booked",
    }
    await repo.create_appointment(old_appointment)
    reschedule_customer = {
        **good_intent,
        "notification_type": "appointment_customer_reschedule",
        "template_purpose": "appointment_reschedule_template",
    }
    reschedule_staff = {
        **good_intent,
        "notification_type": "appointment_staff_reschedule",
        "template_purpose": "staff_appointment_reschedule_template",
    }

    # The customer intent is inserted before the invalid staff intent raises;
    # appointment/status/outbox changes must all roll back together.
    with pytest.raises(ValueError):
        await repo.reschedule_appointment_with_outbox(old_id, new_appointment, reschedule_customer, bad_intent)
    assert (await repo._fetchall("SELECT status FROM appointments WHERE id=%s", (old_id,)))[0]["status"] == "booked"
    assert await repo._fetchall("SELECT id FROM appointments WHERE id=%s", (new_id,)) == []
    assert await repo._fetchall("SELECT id FROM notification_outbox WHERE appointment_id=%s", (new_id,)) == []

    # A valid retry succeeds, and repeating the same event is idempotent.
    first = await repo.reschedule_appointment_with_outbox(old_id, new_appointment, reschedule_customer, reschedule_staff)
    second = await repo.reschedule_appointment_with_outbox(old_id, new_appointment, reschedule_customer, reschedule_staff)
    assert first["new_appointment_id"] == second["new_appointment_id"] == new_id
    assert (await repo._fetchall("SELECT status FROM appointments WHERE id=%s", (old_id,)))[0]["status"] == "rescheduled"
    assert len(await repo._fetchall("SELECT id FROM appointments WHERE id=%s", (new_id,))) == 1
    assert len(await repo._fetchall("SELECT id FROM notification_outbox WHERE appointment_id=%s", (new_id,))) == 2


async def test_full_repository_customer_journey(repo, marker):
    phone = _phone(marker)
    setting_key = "MYSQL_IT_SETTING_" + marker
    second_setting_key = "MYSQL_IT_SETTING_SECOND_" + marker
    await repo.set_settings({setting_key: "first", second_setting_key: "second"})
    settings = await repo.list_settings()
    assert settings[setting_key] == "first"
    assert settings[second_setting_key] == "second"
    assert await repo.get_setting(setting_key) == "first"
    contact = await repo.upsert_crm_contact({
        "id": str(uuid.uuid4()),
        "phone_number": phone,
        "lead_name": "Karuna",
        "source": "WhatsApp",
        "crm_status": "New Lead",
    })
    conv = await repo.get_or_create_whatsapp_conversation(phone, "Karuna")
    await repo.save_whatsapp_message({
        "conversation_id": conv["id"],
        "phone_number": phone,
        "direction": "inbound",
        "message_type": "text",
        "message_text": "tomorrow 10am",
        "provider_message_id": "mysql_it_e2e_" + marker,
        "provider_status": "processed",
        "raw_payload": {},
    })
    await repo.add_contact_memory(phone, "Customer asked for aircon servicing")
    await repo.persist_appointment_state(conv["id"], {
        "status": "pending_confirmation",
        "requested_date": "2026-08-22",
        "requested_time": "10:00",
        "service": "General Servicing",
    })
    assert await repo.get_contact_memory(phone)
    assert (await repo.load_appointment_state(conv["id"]))["requested_time"] == "10:00"
    assert contact["phone_number"] == phone


async def test_remaining_reference_calls_and_profiles(repo, marker):
    phone = _phone(marker)
    name = "mysql_it_" + marker
    await repo.create_error_log({"source": name, "level": "warning", "message": "integration", "detail": "safe"})
    assert (await repo.list_error_logs(source=name))[0]["message"] == "integration"
    assert (await repo.upsert_lead_status({"name": name, "color": "#123456"}))["name"] == name
    assert (await repo.upsert_crm_tag({"name": name, "color": "#654321"}))["name"] == name

    call = await repo.create_call_log({
        "phone_number": phone,
        "lead_name": "Integration",
        "outcome": "booked",
        "duration_seconds": 45,
        "recording_url": "https://example.invalid/recording",
        "recording_size_bytes": 2048,
    })
    assert [row["id"] for row in await repo.list_call_logs(phone=phone)] == [call["id"]]
    assert (await repo.get_recording_storage_stats())["active_recordings"] >= 1
    assert await repo.update_call_log(call["id"], {"notes": "verified"})

    first_id = "mysql_it_profile_a_" + marker
    second_id = "mysql_it_profile_b_" + marker
    await repo.create_agent_profile({"id": first_id, "name": "First", "enabled_tools": "[]"}, set_default=True)
    await repo.create_agent_profile({"id": second_id, "name": "Second", "enabled_tools": "[]"}, set_default=True)
    profiles = {row["id"]: row for row in await repo.list_agent_profiles() if row["id"] in {first_id, second_id}}
    assert not profiles[first_id]["is_default"]
    assert profiles[second_id]["is_default"]


async def test_campaign_claim_concurrency_and_stale_owner_rejection(repo, marker):
    campaign_id = "mysql_it_campaign_" + marker
    await repo.create_campaign({
        "id": campaign_id,
        "name": "Integration campaign",
        "status": "active",
        "contacts_json": [{"phone": _phone(marker), "status": "pending"}],
    })
    second_repo = MySQLRepository()
    try:
        worker_a, worker_b = await asyncio.gather(
            repo.claim_campaign(campaign_id, lease_seconds=60),
            second_repo.claim_campaign(campaign_id, lease_seconds=60),
        )
    finally:
        await _close_repo(second_repo)
    claims = [row for row in (worker_a, worker_b) if row]
    assert len(claims) == 1
    claim = claims[0]
    assert not await repo.update_campaign(campaign_id, {"total_dispatched": 99}, claim_token=str(uuid.uuid4()))
    assert await repo.update_campaign(campaign_id, {"status": "active"})
    assert await repo.claim_campaign(campaign_id, lease_seconds=60) is None
    assert await repo.update_campaign(
        campaign_id,
        {"status": "completed", "total_dispatched": 1, "claim_token": None, "lease_expires_at": None},
        claim_token=claim["claim_token"],
    )


async def test_automation_claim_concurrency_and_stale_owner_rejection(repo, marker):
    action = await repo.create_automation_action({
        "phone_number": _phone(marker),
        "event_type": "integration",
        "action_type": "health_test",
        "scheduled_at": "2026-08-01 00:00:00.000000",
        "payload": {"is_test": True},
        "result": {},
    })
    second_repo = MySQLRepository()
    try:
        worker_a, worker_b = await asyncio.gather(
            repo.claim_automation_actions("2026-08-02 00:00:00.000000", limit=1, lease_seconds=60),
            second_repo.claim_automation_actions("2026-08-02 00:00:00.000000", limit=1, lease_seconds=60),
        )
    finally:
        await _close_repo(second_repo)
    claimed = worker_a + worker_b
    assert [row["id"] for row in claimed] == [action["id"]]
    assert not await repo.update_automation_action(action["id"], {"status": "completed"}, claim_token=str(uuid.uuid4()))
    assert await repo.update_automation_action(
        action["id"],
        {"status": "completed", "action_status": "completed", "claim_token": None, "lease_expires_at": None},
        claim_token=claimed[0]["claim_token"],
    )
    await repo._execute(
        "UPDATE automation_actions SET status='running',action_status='running',claim_token=%s,"
        "lease_expires_at='2026-08-01 00:00:00.000000' WHERE id=%s",
        (str(uuid.uuid4()), action["id"]),
    )
    reclaimed = await repo.claim_automation_actions("2026-08-02 00:00:00.000000", limit=1, lease_seconds=60)
    assert [row["id"] for row in reclaimed] == [action["id"]]
    assert reclaimed[0]["attempt_count"] == 2


async def test_broadcast_and_health_repository_round_trip(repo, marker):
    campaign_id = "mysql_it_broadcast_" + marker
    campaign = await repo.create_broadcast_campaign({
        "id": campaign_id,
        "name": "Integration broadcast",
        "template_name": "integration_template",
        "segment": {"phone_number": _phone(marker)},
        "status": "draft",
    })
    second_repo = MySQLRepository()
    try:
        worker_a, worker_b = await asyncio.gather(
            repo.claim_broadcast_campaign(campaign_id, lease_seconds=60),
            second_repo.claim_broadcast_campaign(campaign_id, lease_seconds=60),
        )
    finally:
        await _close_repo(second_repo)
    claims = [row for row in (worker_a, worker_b) if row]
    assert len(claims) == 1
    claimed = claims[0]
    assert claimed and claimed["id"] == campaign["id"]
    recipient = {
        "id": str(uuid.uuid4()),
        "campaign_id": campaign_id,
        "phone_number": _phone(marker),
    }
    owned = await repo.claim_broadcast_recipient(recipient, claimed["claim_token"])
    assert owned
    assert await repo.claim_broadcast_recipient({**recipient, "id": str(uuid.uuid4())}, claimed["claim_token"]) is None
    assert await repo.update_broadcast_recipient(owned["id"], {"status": "sent", "claim_token": None}, claimed["claim_token"])
    assert await repo.update_broadcast_campaign(
        campaign_id,
        {"status": "completed", "sent_count": 1, "failed_count": 0, "claim_token": None, "lease_expires_at": None},
        claimed["claim_token"],
    )
    assert (await repo.get_broadcast_campaign(campaign_id))["sent_count"] == 1
    assert await repo.update_broadcast_campaign(campaign_id, {"status": "paused"})
    assert await repo.claim_broadcast_campaign(campaign_id, lease_seconds=60) is None

    recipient_id = "mysql_hr_" + marker
    run_id = "mysql_hu_" + marker
    await repo.create_health_recipient({
        "id": recipient_id,
        "recipient_name": "Integration",
        "phone_number_encrypted": "encrypted",
        "phone_number_fingerprint": (marker * 6)[:64],
        "phone_number_masked": "+65****0000",
        "country_code": "+65",
        "verified": True,
    })
    await repo.create_health_run({
        "id": run_id,
        "status": "RUNNING",
        "recipient_id": recipient_id,
        "initiated_by": "integration",
        "selected_tests": ["database"],
    })
    result = await repo.upsert_health_result({
        "test_run_id": run_id,
        "test_key": "database",
        "test_name": "Database",
        "status": "PASS",
        "started_at": "2026-08-23 00:00:00.000000",
        "details_json": {"backend": "mysql"},
    })
    assert (await repo.get_health_run(run_id))["selected_tests"] == ["database"]
    assert (await repo.get_health_result(run_id, "database"))["details_json"] == {"backend": "mysql"}
    assert [row["id"] for row in await repo.list_health_results(run_id)] == [result["id"]]
