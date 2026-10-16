import asyncio
import unittest
from unittest.mock import AsyncMock, patch

import whatsapp as wa


class NotificationWorkerRecoveryTests(unittest.TestCase):
    def test_temporary_failure_schedules_retry(self):
        updates = []

        async def mark(_row, status, safe_error, next_attempt_at=""):
            updates.append({"status": status, "last_error": safe_error, "next_attempt_at": next_attempt_at})
            return True

        row = {
            "id": "row-2",
            "appointment_id": "appt-2",
            "notification_type": "appointment_staff_confirmation",
            "template_purpose": "staff_appointment_notification_template",
            "recipient_phone": "+6590000001",
            "payload": "{}",
            "attempt_count": 2,
            "max_attempts": 5,
        }
        fake_db = type("FakeDb", (), {"log_error": AsyncMock()})()
        with patch.object(wa, "_db", return_value=fake_db), \
             patch.object(wa, "_mark_outbox_failed", new=mark), \
             patch.object(wa, "resolve_wa_template", new=AsyncMock(return_value="staff_appointment_notification")), \
             patch.object(wa, "build_template_params", new=AsyncMock(return_value={"template_name": "staff_appointment_notification", "language": "en", "params": []})), \
             patch.object(wa, "send_whatsapp_template", new=AsyncMock(return_value={"success": False, "reason": "send_error", "error": "network timeout"})):
            result = asyncio.run(wa.process_notification_outbox_row(row))
        self.assertEqual(result["status"], "failed")
        self.assertTrue(any(update.get("status") == "failed" and update.get("next_attempt_at") for update in updates))


if __name__ == "__main__":
    unittest.main()
