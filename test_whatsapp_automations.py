import asyncio
import unittest
from unittest.mock import AsyncMock, patch

import pytest

import whatsapp as wa
import db
import server
from db import normalize_phone


@pytest.fixture(autouse=True)
def _block_configured_repository(monkeypatch):
    def unexpected_repository_access():
        raise AssertionError("mock-only WhatsApp automation test accessed the configured repository")

    monkeypatch.setattr(db, "get_repository", unexpected_repository_access)


class WhatsAppAutomationTests(unittest.TestCase):
    def test_meta_send_failure_creates_sanitized_outbound_failure_log(self):
        logged = AsyncMock()
        diagnostics = AsyncMock()
        with patch.object(wa, "_is_wa_enabled", new=AsyncMock(return_value=True)), \
             patch.object(wa, "_wa_config", new=AsyncMock(return_value={"WHATSAPP_PROVIDER": "meta"})), \
             patch.object(wa, "_log_wa", new=logged), \
             patch.object(wa, "_diagnostic_event", new=diagnostics), \
             patch.object(wa, "_db", return_value=type("LogDb", (), {"log_error": AsyncMock()})()):
            result = asyncio.run(wa.send_whatsapp_text("+6590000000", "secret customer text"))
        self.assertFalse(result["success"])
        self.assertEqual(logged.await_args.args[1], "outbound_text")
        self.assertEqual(logged.await_args.args[5], "failed")
        diagnostic = diagnostics.await_args_list[-1]
        self.assertEqual(diagnostic.args[0], "outbound_send_failed")
        self.assertNotIn("secret customer text", str(diagnostic))

    def test_crm_models_and_exports_include_address(self):
        create = server.CrmLeadRequest(lead_name="Synthetic Customer", phone_number="+6590000000", address="10 Synthetic Street")
        update = server.CrmLeadUpdateRequest(address="20 Example Avenue")
        self.assertEqual(create.address, "10 Synthetic Street")
        self.assertEqual(update.address, "20 Example Avenue")
        self.assertIn("address", db.CRM_LEAD_FIELDS)
        self.assertIn("address", server.CRM_EXPORT_COLUMNS)

    def test_appointment_route_passes_staff_date_and_status_filters(self):
        get_rows = AsyncMock(return_value=[])
        with patch.object(server, "get_all_appointments", new=get_rows):
            rows = asyncio.run(server.api_get_appointments(
                status="closed", staff_id="staff-1",
                date_from="2026-07-13", date_to="2026-07-20",
            ))
        self.assertEqual(rows, [])
        get_rows.assert_awaited_once_with(
            date_filter=None, status_filter="closed", search_query=None,
            staff_id="staff-1", date_from="2026-07-13", date_to="2026-07-20",
        )

    def test_appointment_settings_accept_configurable_intake_prompt(self):
        model = server.AppointmentSettingsRequest(intake_prompt="Collect project size and access notes")
        self.assertEqual(model.intake_prompt, "Collect project size and access notes")

    def test_meta_payload_normalizes_supplied_test_numbers(self):
        customer = wa.build_meta_template_payload("+6590000000", "test_template", "en", ["A", "你好"])
        staff = wa.build_meta_template_payload("+919000000000", "test_template", "en", ["B"])
        self.assertEqual(customer["to"], "6590000000")
        self.assertEqual(staff["to"], "919000000000")
        self.assertEqual(customer["template"]["components"][0]["parameters"][1]["text"], "你好")

    def test_invalid_numbers_are_rejected(self):
        for number in ("", "abc", "+123"):
            with self.subTest(number=number), self.assertRaises(ValueError):
                wa.build_meta_template_payload(number, "test_template")

    def test_staff_outcome_template_has_expected_parameters(self):
        async def setting(key):
            if key == "staff_demo_status_request": return "staff_demo_status_request"
            if key == "WHATSAPP_DEFAULT_LANGUAGE": return "en"
            return ""
        context = {"customer_name": "Test Customer", "customer_phone": "+6590000000", "appointment_datetime": "12 Jul 2026 10:30 AM"}
        with patch.object(wa, "_get_wa_setting", new=setting):
            built = asyncio.run(wa.build_template_params("staff_demo_status_request", context))
        self.assertEqual(len(built["params"]), 3)
        self.assertEqual(built["params"][1], "+6590000000")

    def test_staff_booking_template_uses_four_approved_variables(self):
        appointment = {
            "id": "appt-1", "name": "Test Customer", "phone": "+6590000000",
            "service": "Aircon service", "date": "2026-07-13", "time": "13:30",
            "staff_id": "staff-1", "staff_name": "Test Staff",
        }
        send = AsyncMock(return_value={"success": True, "provider_message_id": "wamid.staff"})
        log_db = type("LogDb", (), {"log_error": AsyncMock()})()
        with patch.object(db, "get_appointment_staff", new=AsyncMock(return_value=[{"id": "staff-1", "whatsapp_number": "+919000000000"}])), \
             patch.object(wa, "resolve_wa_template", new=AsyncMock(return_value="staff_appointment_notification")), \
             patch.object(wa, "_get_wa_setting", new=AsyncMock(return_value="en")), \
             patch.object(wa, "send_whatsapp_template", new=send), \
             patch.object(wa, "_db", return_value=log_db):
            result = asyncio.run(wa.send_staff_appointment_notification(appointment, update_appointment=False))
        self.assertTrue(result["success"])
        params = send.await_args.args[3]
        self.assertEqual(params, ["Test Customer", "Aircon service", "2026-07-13", "13:30"])
        self.assertEqual(len(params), wa.WA_TEMPLATE_PARAM_COUNTS["staff_appointment_notification_template"])

    def test_meta_webhook_message_types(self):
        payload = {"object": "whatsapp_business_account", "entry": [{"changes": [{"value": {"messages": [{"from": "6590000000", "id": "wamid.1", "type": "interactive", "interactive": {"button_reply": {"id": "yes", "title": "Yes"}}}]}}]}]}
        parsed = wa.parse_webhook_messages(payload)
        self.assertEqual(parsed[0]["phone"], "+6590000000")
        self.assertEqual(parsed[0]["text"], "Yes")

    def test_opt_out_words(self):
        self.assertTrue(wa._is_opt_out("STOP"))
        self.assertTrue(wa._is_opt_out("unsubscribe"))
        self.assertFalse(wa._is_opt_out("Please stop by tomorrow"))

    def test_default_workflows_are_safe_and_unique(self):
        rules = wa._default_automation_rules()
        self.assertEqual(len(rules), len({(r["event_type"], r["source"]) for r in rules}))
        self.assertTrue(all(not r["enabled"] and r["action"] == "manual_only" for r in rules))

    def test_booking_survives_post_insert_lookup_failure(self):
        conv = {
            "id": "conv-1", "contact_name": "Test Customer",
            "appointment_state": '{"status":"pending_confirmation","requested_date":"2026-07-13","requested_time":"13:30","service":"Aircon service"}',
        }
        sent = []

        async def capture(_phone, _conv_id, message, *_args, **_kwargs):
            sent.append(message)
            return {"success": True}

        async def settings():
            return {"duration_minutes": 60, "demo_duration_minutes": 60, "appointment_name": "Service Appointment", "default_service_type": "Aircon service", "delivery_mode": "onsite", "staff_label": "staff"}

        with patch.object(wa, "_atomic_outbox_required", return_value=False), \
             patch.object(db, "get_appointment_settings", new=settings), \
             patch.object(db, "get_existing_active_appointment", new=AsyncMock(return_value=None)), \
             patch.object(db, "check_slot", new=AsyncMock(return_value=True)), \
             patch.object(db, "insert_appointment", new=AsyncMock(return_value="ABC12345")), \
             patch.object(db, "get_appointment_by_id", new=AsyncMock(side_effect=RuntimeError("secondary lookup failed"))), \
             patch.object(db, "get_appointments_by_phone", new=AsyncMock(side_effect=RuntimeError("secondary lookup failed"))), \
             patch.object(wa, "patch_conversation", new=AsyncMock()), \
             patch.object(wa, "_notify_appointment_booked", new=AsyncMock(return_value={})), \
             patch.object(wa, "_send_and_save_whatsapp_ai_text", new=capture), \
             patch.object(wa, "_log_whatsapp_ai_event", new=AsyncMock()):
            handled = asyncio.run(wa._handle_whatsapp_appointment_state("+6590000000", "conv-1", "Yes", conv, None, {"lead_name": "Test Customer"}))

        self.assertTrue(handled)
        self.assertTrue(any("booked for" in message for message in sent))
        self.assertFalse(any("could not book" in message for message in sent))

    def test_failed_booking_clears_pending_confirmation(self):
        conv = {
            "id": "conv-1", "contact_name": "Test Customer",
            "appointment_state": '{"status":"pending_confirmation","requested_date":"2026-07-13","requested_time":"13:30"}',
        }
        patch_state = AsyncMock()
        with patch.object(db, "get_appointment_settings", new=AsyncMock(return_value={"duration_minutes": 60, "appointment_name": "Appointment", "delivery_mode": "onsite", "staff_label": "staff"})), \
             patch.object(db, "get_existing_active_appointment", new=AsyncMock(side_effect=RuntimeError("insert unavailable"))), \
             patch.object(wa, "patch_conversation", new=patch_state), \
             patch.object(wa, "_send_and_save_whatsapp_ai_text", new=AsyncMock(return_value={"success": True})), \
             patch.object(wa, "_log_whatsapp_ai_event", new=AsyncMock()):
            asyncio.run(wa._handle_whatsapp_appointment_state("+6590000000", "conv-1", "Yes", conv, None, {}))
        saved = patch_state.await_args_list[-1].args[1]["appointment_state"]
        self.assertIn('"status": "booking_failed"', saved)

    def test_alternative_slots_replace_stale_pending_state(self):
        conv = {"id": "conv-1", "appointment_state": '{"status":"pending_confirmation","requested_date":"2026-07-13","requested_time":"11:00"}'}
        saved_state = AsyncMock()
        parsed = {"requested_date": "2026-07-13", "requested_time": "11:00", "parsed_scheduled_at": "2026-07-13T11:00:00"}
        suggestions = [{"label": "1:30 PM", "requested_date": "2026-07-13", "requested_time": "13:30"}]
        with patch.object(db, "get_appointment_settings", new=AsyncMock(return_value={"duration_minutes": 60, "appointment_name": "Service Appointment", "delivery_mode": "onsite", "staff_label": "staff"})), \
             patch.object(db, "get_appointment_staff", new=AsyncMock(return_value=[{"id": "staff-1", "active": True}])), \
             patch.object(db, "get_next_active_appointment", new=AsyncMock(return_value=None)), \
             patch.object(db, "get_slot_availability", new=AsyncMock(return_value={"available": False, "reason": "appointment_conflict", "conflict": True})), \
             patch.object(wa, "_parse_whatsapp_requested_datetime", return_value=parsed), \
             patch.object(wa, "_suggest_available_demo_slots", new=AsyncMock(return_value=suggestions)), \
             patch.object(wa, "patch_conversation", new=saved_state), \
             patch.object(wa, "_send_and_save_whatsapp_ai_text", new=AsyncMock(return_value={"success": True})), \
             patch.object(wa, "_log_whatsapp_ai_event", new=AsyncMock()):
            asyncio.run(wa._handle_whatsapp_appointment_state("+6590000000", "conv-1", "tomorrow 11am", conv, None, {}))
        persisted = saved_state.await_args.args[1]["appointment_state"]
        self.assertIn('"status": "alternatives_offered"', persisted)
        self.assertNotIn('"status": "pending_confirmation"', persisted)

    def test_existing_booking_is_rescheduled_not_duplicated(self):
        conv = {
            "id": "conv-1", "contact_name": "Test Customer",
            "appointment_state": '{"status":"pending_confirmation","action":"reschedule","existing_appointment_id":"old-id","existing_date":"2026-07-13","existing_time":"11:00","requested_date":"2026-07-13","requested_time":"13:30","service":"Aircon service"}',
        }
        new_row = {"id": "new-id", "date": "2026-07-13", "time": "13:30", "status": "booked", "staff_name": "Test Staff"}
        reschedule = AsyncMock(return_value=new_row)
        insert = AsyncMock()
        sent = AsyncMock(return_value={"success": True})
        with patch.object(wa, "_atomic_outbox_required", return_value=False), \
             patch.object(db, "get_appointment_settings", new=AsyncMock(return_value={"duration_minutes": 60, "appointment_name": "Service Appointment", "default_service_type": "Service", "delivery_mode": "onsite", "staff_label": "staff"})), \
             patch.object(db, "get_existing_active_appointment", new=AsyncMock(return_value=None)), \
             patch.object(db, "check_slot", new=AsyncMock(return_value=True)), \
             patch.object(db, "reschedule_appointment", new=reschedule), \
             patch.object(db, "insert_appointment", new=insert), \
             patch.object(wa, "patch_conversation", new=AsyncMock()), \
             patch.object(wa, "enqueue_required_appointment_notifications", new=AsyncMock(return_value={"success": True, "intents": []})), \
             patch.object(wa, "_remember_whatsapp_insight", new=AsyncMock()), \
             patch.object(wa, "_send_and_save_whatsapp_ai_text", new=sent), \
             patch.object(wa, "_log_whatsapp_ai_event", new=AsyncMock()):
            asyncio.run(wa._handle_whatsapp_appointment_state("+6590000000", "conv-1", "Yes", conv, None, {"lead_name": "Test Customer"}))
        reschedule.assert_awaited_once_with("old-id", "2026-07-13", "13:30")
        insert.assert_not_awaited()
        self.assertIn("rescheduled", sent.await_args.args[2])

    def test_dashboard_reschedule_notifies_assigned_staff(self):
        moved = {"id": "new-id", "date": "2026-07-13", "time": "13:30", "staff_id": "staff-1", "staff_name": "Test Staff", "phone": "+6590000000"}
        notify = AsyncMock(return_value={"success": True, "intents": [{"notification_type": "appointment_staff_reschedule"}]})
        with patch.object(server, "_atomic_outbox_required", return_value=False), \
             patch.object(server, "reschedule_appointment", new=AsyncMock(return_value=moved)), \
             patch.object(server, "enqueue_required_appointment_notifications", new=notify):
            result = asyncio.run(server.api_patch_appointment_reschedule("old-id", server.AppointmentRescheduleRequest(date="2026-07-13", time="13:30")))
        self.assertTrue(notify.await_count == 1)
        self.assertTrue(result["notification_outbox"]["success"])
        self.assertEqual(result["warning"], "")

    def test_dashboard_reschedule_surfaces_staff_notification_failure(self):
        moved = {"id": "new-id", "date": "2026-07-13", "time": "13:30", "staff_id": "staff-1"}
        with patch.object(server, "_atomic_outbox_required", return_value=False), \
             patch.object(server, "reschedule_appointment", new=AsyncMock(return_value=moved)), \
             patch.object(server, "enqueue_required_appointment_notifications", new=AsyncMock(return_value={"success": False, "errors": ["appointment_staff_reschedule:outbox_insert_failed"]})), \
             patch.object(server, "update_appointment_notifications", new=AsyncMock()):
            with self.assertRaises(server.HTTPException) as ctx:
                asyncio.run(server.api_patch_appointment_reschedule("old-id", server.AppointmentRescheduleRequest(date="2026-07-13", time="13:30")))
        self.assertEqual(ctx.exception.status_code, 500)


if __name__ == "__main__":
    unittest.main()
