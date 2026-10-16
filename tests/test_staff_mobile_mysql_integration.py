import asyncio
import gc
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from airzen_repository import MySQLRepository


def _mysql_env_ready() -> bool:
    return bool(
        os.getenv("AIRZEN_ALLOW_MYSQL_STAGING_TESTS", "").lower() == "true"
        and os.getenv("AIRZEN_DB_BACKEND", "").lower() == "mysql"
        and os.getenv("MYSQL_HOST")
        and os.getenv("MYSQL_DATABASE")
        and os.getenv("MYSQL_USER")
    )


def _driver_available() -> bool:
    try:
        import aiomysql  # noqa: F401
        return True
    except Exception:
        return False


pytestmark = [
    pytest.mark.skipif(not _mysql_env_ready(), reason="real MySQL staging configuration is required"),
    pytest.mark.skipif(not _driver_available(), reason="aiomysql is not installed"),
    pytest.mark.asyncio(loop_scope="module"),
]


@pytest_asyncio.fixture(scope="module", autouse=True, loop_scope="module")
async def _finalize_mysql_connections_before_loop_close():
    yield
    gc.collect()
    await asyncio.sleep(0)


@pytest_asyncio.fixture(loop_scope="module")
async def mobile_context():
    repo = MySQLRepository()
    suffix = uuid.uuid4().hex[:12]
    ids = {
        "staff": str(uuid.uuid4()),
        "appointment": str(uuid.uuid4()),
        "contact": str(uuid.uuid4()),
        "session": str(uuid.uuid4()),
        "phone": f"mysql_mobile_{suffix}",
    }
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    try:
        yield repo, ids, now
    finally:
        if repo._pool is not None:
            pool = await repo._ensure_pool()
            async with pool.acquire() as conn:
                try:
                    async with conn.cursor() as cur:
                        await cur.execute("DELETE FROM notification_outbox WHERE appointment_id=%s", (ids["appointment"],))
                        await cur.execute("DELETE FROM staff_leave_requests WHERE staff_id=%s", (ids["staff"],))
                        await cur.execute("DELETE FROM staff_attendance WHERE staff_id=%s", (ids["staff"],))
                        await cur.execute("DELETE FROM appointment_start_otps WHERE appointment_id=%s", (ids["appointment"],))
                        await cur.execute("DELETE FROM appointment_evidence WHERE appointment_id=%s", (ids["appointment"],))
                        await cur.execute("DELETE FROM appointment_tracking_events WHERE appointment_id=%s", (ids["appointment"],))
                        await cur.execute("DELETE FROM appointment_job_events WHERE appointment_id=%s", (ids["appointment"],))
                        await cur.execute("DELETE FROM staff_mobile_sessions WHERE staff_id=%s", (ids["staff"],))
                        await cur.execute("DELETE FROM staff_mobile_credentials WHERE staff_id=%s", (ids["staff"],))
                        await cur.execute("DELETE FROM appointments WHERE id=%s", (ids["appointment"],))
                        await cur.execute("DELETE FROM appointment_staff WHERE id=%s", (ids["staff"],))
                        await cur.execute("DELETE FROM crm_contacts WHERE phone_number=%s", (ids["phone"],))
                    await conn.commit()
                except BaseException:
                    await conn.rollback()
                    raise
        if repo._pool is not None:
            repo._pool.terminate()
        await repo.close()


def _event(ids: dict, session_id: str, event_type: str, key: str, now: datetime) -> dict:
    return {
        "id": str(uuid.uuid4()),
        "appointment_id": ids["appointment"],
        "staff_id": ids["staff"],
        "session_id": session_id,
        "event_type": event_type,
        "event_data": {"source": "mysql_mobile_integration"},
        "idempotency_key": key,
        "created_at": now,
    }


def _evidence(ids: dict, session_id: str, evidence_type: str, key: str, now: datetime) -> dict:
    return {
        "id": str(uuid.uuid4()),
        "appointment_id": ids["appointment"],
        "staff_id": ids["staff"],
        "session_id": session_id,
        "evidence_type": evidence_type,
        "storage_key": f"integration/{ids['appointment']}/{key}.jpg",
        "sha256": "a" * 64,
        "content_type": "image/jpeg",
        "file_size": 128,
        "captured_at": now,
        "metadata_json": {"synthetic": True},
        "idempotency_key": key,
        "created_at": now,
    }


async def test_staff_mobile_mysql_schema_health(mobile_context):
    repo, _ids, _now = mobile_context
    health = await repo.staff_mobile_schema_health()

    assert health["backend"] == "mysql"
    assert health["ok"] is True
    assert len(health["required_tables_present"]) == 8
    assert health["required_tables_missing"] == []
    assert health["required_columns_missing"] == {}
    assert health["critical_constraints_missing"] == {}
    assert health["critical_indexes_missing"] == {}


async def test_staff_mobile_mysql_atomic_job_lifecycle_and_rollback(mobile_context):
    repo, ids, now = mobile_context
    await repo._execute(
        """
        INSERT INTO appointment_staff
          (id,name,email,whatsapp_number,role,working_days,start_time,end_time,timezone,active,round_robin_order,created_at,updated_at)
        VALUES (%s,'Mobile Test Staff','mobile@example.test',%s,'service_staff',%s,'09:00','18:00','Asia/Singapore',1,0,%s,%s)
        """,
        (ids["staff"], ids["phone"], '["mon","tue","wed","thu","fri","sat","sun"]', now, now),
    )
    await repo.upsert_crm_contact({
        "id": ids["contact"], "phone_number": ids["phone"], "lead_name": "Synthetic Mobile Customer",
        "crm_status": "Appointment Booked", "journey_stage": "demo_booked",
    })
    await repo.create_appointment({
        "id": ids["appointment"], "name": "Synthetic Mobile Customer", "phone": ids["phone"],
        "date": "2030-08-27", "time": "10:00", "service": "Synthetic aircon service",
        "status": "booked", "staff_id": ids["staff"], "staff_name": "Mobile Test Staff",
        "timezone": "Asia/Singapore",
    })
    await repo.upsert_staff_mobile_credential(
        ids["staff"], f"mobile.{ids['staff'][:8]}", "pbkdf2_sha256$310000$synthetic$synthetic",
    )
    await repo.create_staff_mobile_session({
        "id": ids["session"], "staff_id": ids["staff"], "refresh_token_hash": "b" * 64,
        "device_id": "mysql-integration-device", "device_name": "CI Android", "platform": "android",
        "app_version": "1.0.0", "last_seen_at": now, "expires_at": now + timedelta(days=1),
        "created_at": now,
    })

    attendance_row = {
        "id": str(uuid.uuid4()), "staff_id": ids["staff"], "work_date": "2030-08-27",
        "check_in_at": now, "check_in_latitude": 1.3521, "check_in_longitude": 103.8198,
        "check_in_accuracy_meters": 5.0, "check_in_session_id": ids["session"],
        "notes": "synthetic integration shift", "created_at": now, "updated_at": now,
    }
    checked_in = await repo.check_in_staff_attendance(attendance_row)
    duplicate_check_in = await repo.check_in_staff_attendance({**attendance_row, "id": str(uuid.uuid4())})
    assert checked_in["id"] == attendance_row["id"]
    assert duplicate_check_in["already_checked_in"] is True
    checked_out = await repo.check_out_staff_attendance(ids["staff"], "2030-08-27", {
        "check_out_at": now + timedelta(hours=8), "check_out_latitude": 1.3522,
        "check_out_longitude": 103.8199, "check_out_accuracy_meters": 6.0,
        "check_out_session_id": ids["session"],
    })
    duplicate_check_out = await repo.check_out_staff_attendance(ids["staff"], "2030-08-27", {
        "check_out_at": now + timedelta(hours=9), "check_out_session_id": ids["session"],
    })
    assert checked_out["check_out_at"] is not None
    assert duplicate_check_out["already_checked_out"] is True
    assert len(await repo.list_staff_attendance("2030-08-27", "2030-08-27", ids["staff"])) == 1

    leave = await repo.create_staff_leave_request({
        "id": str(uuid.uuid4()), "staff_id": ids["staff"], "leave_type": "annual",
        "start_date": "2030-09-01", "end_date": "2030-09-02", "reason": "Synthetic leave",
        "status": "pending", "admin_note": "", "created_at": now, "updated_at": now,
    })
    with pytest.raises(ValueError, match="overlaps"):
        await repo.create_staff_leave_request({
            "id": str(uuid.uuid4()), "staff_id": ids["staff"], "leave_type": "medical",
            "start_date": "2030-09-02", "end_date": "2030-09-03", "reason": "Overlap",
            "status": "pending", "admin_note": "", "created_at": now, "updated_at": now,
        })
    decided = await repo.decide_staff_leave_request(leave["id"], "approved", "Coverage confirmed", "integration-admin")
    assert decided["status"] == "approved"
    with pytest.raises(ValueError, match="Only a pending"):
        await repo.decide_staff_leave_request(leave["id"], "declined", "Changed mind", "integration-admin")

    listed = await repo.list_staff_mobile_appointments(ids["staff"], statuses=["booked"])
    assert [row["id"] for row in listed] == [ids["appointment"]]
    assert await repo.get_staff_mobile_appointment(str(uuid.uuid4()), ids["appointment"]) is None

    en_route = _event(ids, ids["session"], "en_route", "c" * 64, now)
    assert (await repo.begin_staff_mobile_job(ids["appointment"], ids["staff"], en_route))["job_state"] == "en_route"
    retry = await repo.begin_staff_mobile_job(ids["appointment"], ids["staff"], en_route)
    assert retry["already_applied"] is True

    await repo.record_staff_mobile_location({
        "id": str(uuid.uuid4()), "appointment_id": ids["appointment"], "staff_id": ids["staff"],
        "session_id": ids["session"], "latitude": 1.3521, "longitude": 103.8198,
        "accuracy_meters": 5.0, "is_mocked": False, "captured_at": now,
        "idempotency_key": "d" * 64, "created_at": now,
    })

    rollback_evidence = _evidence(ids, ids["session"], "arrival_selfie", "e" * 64, now)
    rollback_event = _event(ids, ids["session"], "arrived", "f" * 64, now)
    with pytest.raises(ValueError, match="not issued"):
        await repo.mark_staff_mobile_arrived(
            ids["appointment"], ids["staff"], rollback_evidence,
            rollback_event,
        )
    assert await repo._fetchone("SELECT id FROM appointment_evidence WHERE id=%s", (rollback_evidence["id"],)) is None
    assert await repo._fetchone("SELECT appointment_id FROM appointment_start_otps WHERE appointment_id=%s", (ids["appointment"],)) is None
    assert await repo._fetchone("SELECT id FROM appointment_job_events WHERE id=%s", (rollback_event["id"],)) is None

    await repo._execute(
        """
        INSERT INTO appointment_start_otps
          (appointment_id,staff_id,otp_hash,status,attempt_count,max_attempts,issued_at,expires_at,updated_at)
        VALUES (%s,%s,%s,'pending',0,5,%s,%s,%s)
        """,
        (ids["appointment"], ids["staff"], "3" * 64, now, datetime(2030, 8, 28), now),
    )

    arrival_evidence = _evidence(ids, ids["session"], "arrival_selfie", "1" * 64, now)
    arrival_event = _event(ids, ids["session"], "arrived", "2" * 64, now)
    arrival = await repo.mark_staff_mobile_arrived(
        ids["appointment"], ids["staff"], arrival_evidence, arrival_event,
    )
    assert arrival["job_state"] == "arrived"

    started = await repo.verify_staff_mobile_start_code(
        ids["appointment"], ids["staff"], ids["session"], "3" * 64,
        _event(ids, ids["session"], "job_started", "4" * 64, now),
    )
    assert started["verified"] is True
    assert (await repo.get_appointment(ids["appointment"]))["status"] == "in_progress"

    completion_evidence = _evidence(ids, ids["session"], "payment_proof", "5" * 64, now)
    completion_event = _event(ids, ids["session"], "completed", "6" * 64, now)
    completed = await repo.complete_staff_mobile_job(
        ids["appointment"], ids["staff"], completion_evidence,
        {
            "recipient_phone": ids["phone"], "notification_type": "appointment_customer_completion",
            "template_purpose": "service_completion_template", "payload": {"payment_method": "synthetic"},
        },
        completion_event,
    )
    assert completed == {"appointment_id": ids["appointment"], "job_state": "completed", "crm_closed": True}
    completed_retry = await repo.complete_staff_mobile_job(
        ids["appointment"], ids["staff"], completion_evidence, {}, completion_event,
    )
    assert completed_retry["already_applied"] is True

    appointment = await repo.get_appointment(ids["appointment"])
    contact = await repo.get_crm_contact_by_phone(ids["phone"])
    detail = await repo.get_staff_mobile_job_detail(ids["appointment"])
    outbox = await repo._fetchall(
        "SELECT notification_type FROM notification_outbox WHERE appointment_id=%s ORDER BY created_at",
        (ids["appointment"],),
    )
    assert appointment["status"] == "completed"
    assert contact["crm_status"] == "Converted"
    assert contact["journey_stage"] == "service_completed"
    assert detail["latest_location"]["latitude"] == pytest.approx(1.3521)
    assert [row["event_type"] for row in detail["events"]] == ["en_route", "arrived", "job_started", "completed"]
    assert {row["evidence_type"] for row in detail["evidence"]} == {"arrival_selfie", "payment_proof"}
    assert {row["notification_type"] for row in outbox} == {"appointment_customer_completion"}
    credential = await repo.get_staff_mobile_credential(f"mobile.{ids['staff'][:8]}")
    assert credential["staff_id"] == ids["staff"]
    assert await repo.disable_staff_mobile_credential(ids["staff"])
    disabled = await repo.get_staff_mobile_credential(f"mobile.{ids['staff'][:8]}")
    session = await repo.get_staff_mobile_session(ids["session"])
    assert not disabled["active"]
    assert session["revoked_at"] is not None
