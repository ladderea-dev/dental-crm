import asyncio
import unittest
from unittest.mock import AsyncMock, patch

import pytest

import db
import whatsapp as wa


@pytest.fixture(autouse=True)
def _block_configured_repository(monkeypatch):
    def unexpected_repository_access():
        raise AssertionError("mock-only notification outbox test accessed the configured repository")

    monkeypatch.setattr(db, "get_repository", unexpected_repository_access)


class _Result:
    def __init__(self, data=None):
        self.data = data or []


class _Query:
    def __init__(self, table):
        self.table = table
        self._row = None

    async def execute(self):
        if self._row is not None:
            self.table.rows.append(self._row)
            return _Result([self._row])
        return _Result(list(self.table.rows))

    def upsert(self, row, **kwargs):
        self._row = row
        return self

    def insert(self, row):
        self._row = row
        return self

    def update(self, updates):
        self.table.updates.append(updates)
        return self

    def select(self, *_args):
        return self

    def eq(self, *_args):
        return self

    def lte(self, *_args):
        return self

    def in_(self, *_args):
        return self

    def limit(self, *_args):
        return self


class _Table:
    def __init__(self):
        self.rows = []
        self.updates = []

    def upsert(self, row, **kwargs):
        return _Query(self).upsert(row, **kwargs)

    def insert(self, row):
        return _Query(self).insert(row)

    def update(self, updates):
        return _Query(self).update(updates)

    def select(self, *args):
        return _Query(self).select(*args)


class _Db:
    def __init__(self):
        self.outbox = _Table()
        self.log_error = AsyncMock()
        self.repository = _Repository(self.outbox)

    def get_repository(self):
        return self.repository


class _Repository:
    def __init__(self, outbox):
        self.outbox = outbox
        self.calls = []

    async def enqueue_notification(self, row):
        existing = next((item for item in self.outbox.rows if item.get("dedupe_key") == row.get("dedupe_key")), None)
        if existing:
            return existing
        self.outbox.rows.append(row)
        return row

    async def complete_notification_outbox(self, row_id, _claim_token, provider_message_id):
        self.outbox.updates.append({"id": row_id, "status": "sent", "provider_message_id": provider_message_id, "last_error": ""})
        return True

    async def fail_notification_outbox(self, row_id, _claim_token, status, safe_error, next_attempt_at=""):
        update = {"id": row_id, "status": status, "last_error": safe_error}
        if status == "failed" and next_attempt_at:
            update["next_attempt_at"] = next_attempt_at
        self.outbox.updates.append(update)
        return True

    async def recover_stuck_notification_outbox(self, max_processing_age_minutes=15, limit=50):
        self.calls.append(("recover", max_processing_age_minutes, limit))
        return 2

    async def reschedule_appointment_with_outbox(
        self,
        old_appointment_id,
        new_appointment,
        customer_intent,
        staff_intent,
        event_identity="v1",
        start_code=None,
    ):
        self.calls.append(("reschedule", old_appointment_id, new_appointment, customer_intent, staff_intent, event_identity, start_code))
        return {"success": True, "new_appointment_id": new_appointment["id"]}

    async def cancel_appointment_with_outbox(self, appointment_id, customer_intent, staff_intent, event_identity="v1"):
        self.calls.append(("cancel", appointment_id, customer_intent, staff_intent, event_identity))
        return {"success": True, "appointment_id": appointment_id}


class NotificationOutboxTests(unittest.TestCase):
    def test_enqueue_appointment_notification_persists_deduped_intent(self):
        fake = _Db()
        appointment = {"id": "appt-1", "phone": "+6590000000", "name": "Test", "date": "2026-08-01", "time": "10:00", "staff_id": "s1"}
        with patch.object(wa, "_db", return_value=fake):
            result = asyncio.run(wa.enqueue_appointment_notification(appointment, "appointment_customer_confirmation", "customer"))
        self.assertTrue(result["success"])
        self.assertEqual(len(fake.outbox.rows), 1)
        row = fake.outbox.rows[0]
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["recipient_type"], "customer")
        self.assertEqual(row["notification_type"], "appointment_customer_confirmation")
        self.assertIn("dedupe_key", row)

    def test_enqueue_existing_sent_intent_is_idempotent_and_not_reset(self):
        fake = _Db()
        dedupe = wa.appointment_notification_dedupe_key("appt-1", "appointment_customer_confirmation", "customer")
        fake.outbox.rows.append({"id": "existing", "dedupe_key": dedupe, "status": "sent"})
        appointment = {"id": "appt-1", "phone": "+6590000000", "name": "Test", "date": "2026-08-01", "time": "10:00"}
        with patch.object(wa, "_db", return_value=fake):
            result = asyncio.run(wa.enqueue_appointment_notification(appointment, "appointment_customer_confirmation", "customer"))
        self.assertTrue(result["success"])
        self.assertEqual(result["status"], "sent")
        self.assertEqual(len(fake.outbox.rows), 1)

    def test_required_appointment_notifications_fail_when_staff_intent_cannot_persist(self):
        async def fake_enqueue(_appointment, notification_type, recipient_type, **_kwargs):
            if recipient_type == "staff":
                return {"success": False, "reason": "outbox_insert_failed"}
            return {"success": True, "dedupe_key": notification_type}

        with patch.object(wa, "enqueue_appointment_notification", new=fake_enqueue):
            result = asyncio.run(wa.enqueue_required_appointment_notifications({"id": "appt-1", "phone": "+6590000000"}, event="created"))
        self.assertFalse(result["success"])
        self.assertIn("appointment_staff_confirmation:outbox_insert_failed", result["errors"])

    def test_worker_marks_successful_send_sent(self):
        fake = _Db()
        row = {
            "id": "row-1",
            "claim_token": "claim-1",
            "appointment_id": "appt-1",
            "notification_type": "appointment_customer_confirmation",
            "template_purpose": "appointment_confirmation_template",
            "recipient_phone": "+6590000000",
            "payload": "{}",
            "attempt_count": 1,
            "max_attempts": 5,
        }
        with patch.object(wa, "_db", return_value=fake), \
             patch.object(wa, "resolve_wa_template", new=AsyncMock(return_value="appointment_confirmation")), \
             patch.object(wa, "build_template_params", new=AsyncMock(return_value={"template_name": "appointment_confirmation", "language": "en", "params": []})), \
             patch.object(wa, "send_whatsapp_template", new=AsyncMock(return_value={"success": True, "provider_message_id": "wamid.test"})):
            result = asyncio.run(wa.process_notification_outbox_row(row))
        self.assertTrue(result["success"])
        self.assertTrue(any(update.get("status") == "sent" for update in fake.outbox.updates))

    def test_worker_permanent_failure_stops_retrying(self):
        fake = _Db()
        row = {
            "id": "row-1",
            "claim_token": "claim-1",
            "appointment_id": "appt-1",
            "notification_type": "appointment_customer_confirmation",
            "template_purpose": "appointment_confirmation_template",
            "recipient_phone": "bad",
            "payload": "{}",
            "attempt_count": 1,
            "max_attempts": 5,
        }
        with patch.object(wa, "_db", return_value=fake), \
             patch.object(wa, "resolve_wa_template", new=AsyncMock(return_value="appointment_confirmation")), \
             patch.object(wa, "build_template_params", new=AsyncMock(return_value={"template_name": "appointment_confirmation", "language": "en", "params": []})), \
             patch.object(wa, "send_whatsapp_template", new=AsyncMock(return_value={"success": False, "reason": "invalid_phone", "error": "Invalid phone"})):
            result = asyncio.run(wa.process_notification_outbox_row(row))
        self.assertFalse(result["success"])
        self.assertEqual(result["status"], "permanently_failed")
        self.assertTrue(any(update.get("status") == "permanently_failed" for update in fake.outbox.updates))

    def test_recovery_uses_repository_boundary(self):
        fake = _Db()
        with patch.object(wa, "_db", return_value=fake):
            recovered = asyncio.run(wa.recover_stuck_notification_outbox(20))
        self.assertEqual(recovered, 2)
        self.assertEqual(fake.repository.calls, [("recover", 20, 50)])

    def test_reschedule_and_cancel_use_repository_boundary(self):
        fake = _Db()
        appointment = {
            "id": "new-1",
            "phone": "+6590000000",
            "name": "Karuna",
            "date": "2026-08-22",
            "time": "12:00",
        }
        with patch.object(wa, "_db", return_value=fake), \
             patch.object(wa, "_appointment_staff_phone", new=AsyncMock(return_value="+6590000001")):
            rescheduled = asyncio.run(wa.reschedule_appointment_with_notification_outbox_rpc("old-1", appointment))
            cancelled = asyncio.run(wa.cancel_appointment_with_notification_outbox_rpc(appointment))

        self.assertTrue(rescheduled["success"])
        self.assertTrue(cancelled["success"])
        self.assertEqual([call[0] for call in fake.repository.calls], ["reschedule", "cancel"])
        self.assertEqual(fake.repository.calls[0][1], "old-1")
        self.assertEqual(fake.repository.calls[1][1], "new-1")


if __name__ == "__main__":
    unittest.main()
