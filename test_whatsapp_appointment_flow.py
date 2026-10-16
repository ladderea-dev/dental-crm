"""Mock-only appointment flow tests. No network, customer call, or live send."""

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest

import db
import whatsapp as wa


@pytest.fixture(autouse=True)
def _block_configured_repository(monkeypatch):
    def unexpected_repository_access():
        raise AssertionError("mock-only appointment flow test accessed the configured repository")

    monkeypatch.setattr(db, "get_repository", unexpected_repository_access)


@pytest.mark.parametrize("text", [
    "I would like to book an appointment tomorrow at 3 PM Singapore time.",
    "Book service tomorrow 3pm",
])
def test_appointment_intent_and_time_parse(text):
    assert wa._detect_inbound_intent(text) == "appointment"
    parsed = wa._parse_whatsapp_requested_datetime(text, appointment_context=True)
    assert parsed["requested_time"] == "15:00"
    assert parsed["requested_date"]


def test_ambiguous_date_requires_clarification():
    assert wa._parse_whatsapp_requested_datetime("appointment sometime tomorrow", appointment_context=True) is None


def test_malformed_date_is_not_booked():
    assert wa._parse_whatsapp_requested_datetime("appointment on not-a-date", appointment_context=True) is None


def test_active_staff_working_hours_and_role():
    start = datetime(2026, 7, 14, 15, 0)
    staff = {"id": "E2E_TEST_STAFF", "active": True, "role": "technician", "working_days": '["tue"]', "start_time": "09:00", "end_time": "18:00"}
    assert db._staff_available_for(staff, start)
    assert not db._staff_available_for({**staff, "active": False}, start)
    assert not db._staff_available_for({**staff, "start_time": "", "end_time": ""}, start)
    assert not db._staff_available_for(staff, start.replace(hour=20))


def test_role_mismatch_does_not_fall_back_to_wrong_staff():
    start = datetime(2026, 7, 14, 15, 0)
    settings = {"default_staff_role": "technician"}
    wrong_role = {"id": "E2E_TEST_SALES", "active": True, "role": "sales", "working_days": '["tue"]', "start_time": "09:00", "end_time": "18:00"}
    with patch.object(db, "get_appointment_settings", new=AsyncMock(return_value=settings)), \
         patch.object(db, "get_appointment_staff", new=AsyncMock(return_value=[wrong_role])), \
         patch.object(db, "_appointment_conflicts", new=AsyncMock(return_value=False)):
        assert asyncio.run(db._booking_candidate_staff(start)) == []


@pytest.mark.parametrize(("error", "expected"), [
    (ValueError("No staff available for this slot"), "No staff member"),
    (ValueError("Appointment is too soon"), "too soon"),
    (ValueError("Appointment is too far ahead"), "booking window"),
    (ValueError("Slot is unavailable"), "no longer available"),
    (ValueError("Invalid appointment date or time"), "could not understand"),
    (RuntimeError("database insert failed: secret detail"), "could not complete"),
])
def test_customer_safe_booking_failures(error, expected):
    message = wa._appointment_booking_failure_message(error, "service appointment")
    assert expected.lower() in message.lower()
    assert "secret detail" not in message


def test_no_staff_configured_returns_specific_response():
    parsed = {"requested_date": "2026-07-14", "requested_time": "15:00", "parsed_scheduled_at": "2026-07-14T15:00:00"}
    send = AsyncMock(return_value={"success": True})
    state = AsyncMock()
    with patch.object(db, "get_appointment_settings", new=AsyncMock(return_value={"duration_minutes": 60, "appointment_name": "Service Appointment", "delivery_mode": "onsite", "staff_label": "staff"})), \
         patch.object(db, "get_appointment_staff", new=AsyncMock(return_value=[])), \
         patch.object(wa, "_parse_whatsapp_requested_datetime", return_value=parsed), \
         patch.object(wa, "patch_conversation", new=state), \
         patch.object(wa, "_send_and_save_whatsapp_ai_text", new=send), \
         patch.object(wa, "_log_whatsapp_ai_event", new=AsyncMock()):
        handled = asyncio.run(wa._handle_whatsapp_appointment_state("+6500000000", "E2E_TEST_CONV", "tomorrow 3pm", {"appointment_state": "{}"}, None, {}))
    assert handled
    assert "scheduling team" in send.await_args.args[2].lower()
    assert "no_active_staff" in state.await_args.args[1]["appointment_state"]


def test_valid_confirmation_inserts_and_never_uses_failure_reply():
    conv = {"id": "E2E_TEST_CONV", "contact_name": "E2E_TEST_CUSTOMER", "appointment_state": '{"status":"pending_confirmation","requested_date":"2026-12-20","requested_time":"15:00","service":"E2E_TEST_SERVICE"}'}
    sent = []
    async def capture(_phone, _conv, message, *_args, **_kwargs):
        sent.append(message)
        return {"success": True}
    with patch.object(wa, "_atomic_outbox_required", return_value=False), \
         patch.object(db, "get_appointment_settings", new=AsyncMock(return_value={"duration_minutes": 60, "appointment_name": "Service Appointment", "default_service_type": "Service", "delivery_mode": "onsite", "staff_label": "staff"})), \
         patch.object(db, "get_existing_active_appointment", new=AsyncMock(return_value=None)), \
         patch.object(db, "get_slot_availability", new=AsyncMock(return_value={"available": True, "reason": "available"})), \
         patch.object(db, "insert_appointment", new=AsyncMock(return_value="E2E12345")), \
         patch.object(db, "get_appointment_by_id", new=AsyncMock(return_value={"id": "E2E12345", "date": "2026-12-20", "time": "15:00", "status": "booked", "staff_id": "E2E_TEST_STAFF", "staff_name": "E2E_TEST_STAFF"})), \
         patch.object(wa, "patch_conversation", new=AsyncMock()), \
         patch.object(wa, "_notify_appointment_booked", new=AsyncMock(return_value={"staff_notified": True})), \
         patch.object(wa, "_remember_whatsapp_insight", new=AsyncMock()), \
         patch.object(wa, "_send_and_save_whatsapp_ai_text", new=capture), \
         patch.object(wa, "_log_whatsapp_ai_event", new=AsyncMock()):
        assert asyncio.run(wa._handle_whatsapp_appointment_state("+6500000000", "E2E_TEST_CONV", "Yes", conv, None, {"lead_name": "E2E_TEST_CUSTOMER"}))
    assert any("booked for" in message for message in sent)
    assert not any("could not" in message.lower() for message in sent)


def test_onsite_slot_uses_whatsapp_number_and_requests_address():
    parsed = {"requested_date": "2026-12-20", "requested_time": "16:00", "parsed_scheduled_at": "2026-12-20T16:00:00"}
    saved_state = AsyncMock()
    sent = AsyncMock(return_value={"success": True})
    with patch.object(db, "get_appointment_settings", new=AsyncMock(return_value={"duration_minutes": 60, "appointment_name": "Service Appointment", "delivery_mode": "onsite", "staff_label": "staff"})), \
         patch.object(db, "get_appointment_staff", new=AsyncMock(return_value=[{"id": "E2E_TEST_STAFF", "active": True}])), \
         patch.object(db, "get_next_active_appointment", new=AsyncMock(return_value=None)), \
         patch.object(db, "get_slot_availability", new=AsyncMock(return_value={"available": True, "reason": "available"})), \
         patch.object(wa, "_parse_whatsapp_requested_datetime", return_value=parsed), \
         patch.object(wa, "patch_conversation", new=saved_state), \
         patch.object(wa, "_send_and_save_whatsapp_ai_text", new=sent), \
         patch.object(wa, "_log_whatsapp_ai_event", new=AsyncMock()):
        assert asyncio.run(wa._handle_whatsapp_appointment_state("+6590000000", "E2E_TEST_CONV", "book service tomorrow 4pm", {"appointment_state": "{}"}, None, {}))
    persisted = saved_state.await_args.args[1]["appointment_state"]
    assert '"status": "awaiting_address"' in persisted
    assert '"contact_number": "+6590000000"' in persisted
    assert "service address" in sent.await_args.args[2].lower()
    assert "mobile number" not in sent.await_args.args[2].lower()


def test_same_number_reply_uses_sender_and_keeps_address_step():
    conv = {"appointment_state": '{"status":"awaiting_address","requested_date":"2026-12-20","requested_time":"16:00"}'}
    saved_state = AsyncMock()
    sent = AsyncMock(return_value={"success": True})
    with patch.object(db, "get_appointment_settings", new=AsyncMock(return_value={"appointment_name": "Service Appointment", "delivery_mode": "onsite"})), \
         patch.object(wa, "patch_conversation", new=saved_state), \
         patch.object(wa, "_send_and_save_whatsapp_ai_text", new=sent), \
         patch.object(wa, "_log_whatsapp_ai_event", new=AsyncMock()):
        assert asyncio.run(wa._handle_whatsapp_appointment_state("+6590000000", "E2E_TEST_CONV", "Same number", conv, None, {}))
    assert '"contact_number": "+6590000000"' in saved_state.await_args.args[1]["appointment_state"]
    reply = sent.await_args.args[2].lower()
    assert "whatsapp number" in reply
    assert "service address" in reply
    assert "mobile number" not in reply


def test_address_reply_is_not_parsed_as_time_and_advances_to_confirmation():
    address = "02-240, 217 Synthetic Walk"
    assert wa._looks_like_service_address(address)
    conv = {"appointment_state": '{"status":"awaiting_address","requested_date":"2026-12-20","requested_time":"16:00","service":"Aircon service"}'}
    saved_state = AsyncMock()
    sent = AsyncMock(return_value={"success": True})
    persist_address = AsyncMock()
    with patch.object(db, "get_appointment_settings", new=AsyncMock(return_value={"appointment_name": "Service Appointment", "delivery_mode": "onsite"})), \
         patch.object(wa, "patch_conversation", new=saved_state), \
         patch.object(wa, "_persist_whatsapp_service_address", new=persist_address), \
         patch.object(wa, "_send_and_save_whatsapp_ai_text", new=sent), \
         patch.object(wa, "_log_whatsapp_ai_event", new=AsyncMock()):
        assert asyncio.run(wa._handle_whatsapp_appointment_state("+6590000000", "E2E_TEST_CONV", address, conv, None, {}))
    persisted = saved_state.await_args.args[1]["appointment_state"]
    assert '"status": "pending_confirmation"' in persisted
    assert '"service_address": "02-240, 217 Synthetic Walk"' in persisted
    persist_address.assert_awaited_once_with("+6590000000", address)
    assert "shall i confirm" in sent.await_args.args[2].lower()


def test_service_address_is_saved_to_dedicated_crm_field():
    save_contact = AsyncMock()
    remember = AsyncMock()
    with patch.object(db, "update_crm_contact_full", new=save_contact), \
         patch.object(wa, "_remember_whatsapp_insight", new=remember):
        asyncio.run(wa._persist_whatsapp_service_address("+6590000000", "10 Synthetic Street"))
    save_contact.assert_awaited_once_with("+6590000000", {"address": "10 Synthetic Street"})
    remember.assert_awaited_once()


def test_meta_location_pin_is_parsed_for_booking():
    payload = {
        "object": "whatsapp_business_account",
        "entry": [{"changes": [{"value": {
            "contacts": [{"wa_id": "6590000000", "profile": {"name": "Synthetic Customer"}}],
            "messages": [{
                "from": "6590000000", "id": "wamid.location", "type": "location",
                "location": {"latitude": 1.3001, "longitude": 103.8001, "name": "Synthetic Building", "address": "10 Synthetic Street"},
            }],
        }}]}],
    }
    parsed = wa.parse_webhook_messages(payload)[0]
    assert parsed["message_type"] == "location"
    assert parsed["location"]["address"] == "10 Synthetic Street"
    assert parsed["location"]["url"].endswith("1.3001,103.8001")


def test_address_then_configured_intake_questions_before_confirmation():
    conv = {"appointment_state": '{"status":"awaiting_address","requested_date":"2026-12-20","requested_time":"16:00","service":"Aircon service"}'}
    saved_state = AsyncMock()
    sent = AsyncMock(return_value={"success": True})
    with patch.object(db, "get_appointment_settings", new=AsyncMock(return_value={"appointment_name": "Service Appointment", "delivery_mode": "onsite", "intake_prompt": "How many units and what issue?"})), \
         patch.object(wa, "patch_conversation", new=saved_state), \
         patch.object(wa, "_persist_whatsapp_service_address", new=AsyncMock()), \
         patch.object(wa, "_send_and_save_whatsapp_ai_text", new=sent), \
         patch.object(wa, "_log_whatsapp_ai_event", new=AsyncMock()):
        assert asyncio.run(wa._handle_whatsapp_appointment_state("+6590000000", "E2E_TEST_CONV", "10 Synthetic Street", conv, None, {}))
    assert '"status": "awaiting_intake"' in saved_state.await_args.args[1]["appointment_state"]
    assert "how many units" in sent.await_args.args[2].lower()


def test_staff_can_complete_assigned_appointment_from_chat():
    appointment = {
        "id": "abcdef12-0000", "name": "Synthetic Customer", "phone": "+6590000000",
        "date": "2026-12-20", "time": "16:00", "service": "Aircon service",
        "status": "in_progress", "staff_id": "staff-1", "staff_notification_message_id": "wamid.staff",
    }
    send = AsyncMock(return_value={"success": True})
    update_status = AsyncMock(return_value=True)
    update_fields = AsyncMock(return_value=True)
    with patch.object(db, "get_appointment_staff", new=AsyncMock(return_value=[{"id": "staff-1", "name": "Test Staff", "whatsapp_number": "+919000000000"}])), \
         patch.object(db, "get_all_appointments", new=AsyncMock(return_value=[appointment])), \
         patch.object(db, "update_appointment_status", new=update_status), \
         patch.object(db, "update_appointment_service_fields", new=update_fields), \
         patch.object(wa, "send_whatsapp_text", new=send):
        handled = asyncio.run(wa.handle_staff_appointment_chat_reply("+919000000000", "Service complete - cleaned 3 units", {"context": {"id": "wamid.staff"}}))
    assert handled
    update_status.assert_awaited_once_with("abcdef12-0000", "completed")
    assert "cleaned 3 units" in update_fields.await_args.args[1]["notes"]
    assert "moved to appointment history" in send.await_args.args[1].lower()


def test_staff_details_uses_reply_context_and_service_summary():
    appointment = {
        "id": "abcdef12-0000", "name": "Synthetic Customer", "phone": "+6590000000",
        "date": "2026-12-20", "time": "16:00", "service": "Aircon service",
        "service_address": "10 Synthetic Street", "location_url": "https://maps.google.com/?q=1.3,103.8",
        "intake_data": {"service_summary": "3 Daikin units; one unit leaking"},
        "status": "booked", "staff_id": "staff-1", "staff_notification_message_id": "wamid.staff",
    }
    send = AsyncMock(return_value={"success": True})
    with patch.object(db, "get_appointment_staff", new=AsyncMock(return_value=[{"id": "staff-1", "name": "Test Staff", "whatsapp_number": "+919000000000"}])), \
         patch.object(db, "get_all_appointments", new=AsyncMock(return_value=[appointment])), \
         patch.object(wa, "send_whatsapp_text", new=send):
        assert asyncio.run(wa.handle_staff_appointment_chat_reply("+919000000000", "Details", {"context": {"id": "wamid.staff"}}))
    summary = send.await_args.args[1]
    assert "10 Synthetic Street" in summary
    assert "3 Daikin units" in summary
    assert "Reply START" in summary


def test_alternatives_state_does_not_fall_through_to_ai_or_invent_slot():
    conv = {"appointment_state": '{"status":"alternatives_offered","service":"Aircon service"}'}
    sent = AsyncMock(return_value={"success": True})
    with patch.object(db, "get_appointment_settings", new=AsyncMock(return_value={"appointment_name": "Service Appointment", "delivery_mode": "onsite"})), \
         patch.object(wa, "_parse_whatsapp_requested_datetime", return_value=None), \
         patch.object(wa, "_send_and_save_whatsapp_ai_text", new=sent), \
         patch.object(wa, "_log_whatsapp_ai_event", new=AsyncMock()):
        assert asyncio.run(wa._handle_whatsapp_appointment_state("+6590000000", "E2E_TEST_CONV", "Any update?", conv, None, {}))
    reply = sent.await_args.args[2].lower()
    assert "preferred date and time" in reply
    assert "confirmed" not in reply


@pytest.mark.parametrize("text", ["Hello", "Aircon service"])
def test_stale_failed_slot_state_always_returns_safe_clarification(text):
    conv = {"appointment_state": '{"status":"alternatives_offered","service":"Aircon service"}'}
    sent = AsyncMock(return_value={"success": True})
    with patch.object(db, "get_appointment_settings", new=AsyncMock(return_value={"appointment_name": "Service Appointment", "delivery_mode": "onsite"})), \
         patch.object(wa, "_send_and_save_whatsapp_ai_text", new=sent), \
         patch.object(wa, "_diagnostic_event", new=AsyncMock()), \
         patch.object(wa, "_log_whatsapp_ai_event", new=AsyncMock()):
        assert asyncio.run(wa._handle_whatsapp_appointment_state("+6590000000", "E2E_TEST_CONV", text, conv, None, {}))
    assert "preferred date and time" in sent.await_args.args[2].lower()


def test_valid_alternate_time_replaces_failed_slot_state():
    conv = {"appointment_state": '{"status":"alternatives_offered","service":"Aircon service","requested_slots":[{"requested_date":"2026-12-20","requested_time":"14:00"}]}' }
    parsed = {"requested_date": "2026-12-20", "requested_time": "16:00", "parsed_scheduled_at": "2026-12-20T16:00:00"}
    saved = AsyncMock()
    sent = AsyncMock(return_value={"success": True})
    with patch.object(db, "get_appointment_settings", new=AsyncMock(return_value={"duration_minutes": 60, "appointment_name": "Service Appointment", "delivery_mode": "online"})), \
         patch.object(db, "get_appointment_staff", new=AsyncMock(return_value=[{"id": "staff", "active": True}])), \
         patch.object(db, "get_next_active_appointment", new=AsyncMock(return_value=None)), \
         patch.object(db, "get_slot_availability", new=AsyncMock(return_value={"available": True, "reason": "available"})), \
         patch.object(wa, "_parse_whatsapp_requested_datetime", return_value=parsed), \
         patch.object(wa, "patch_conversation", new=saved), \
         patch.object(wa, "_send_and_save_whatsapp_ai_text", new=sent), \
         patch.object(wa, "_diagnostic_event", new=AsyncMock()), \
         patch.object(wa, "_log_whatsapp_ai_event", new=AsyncMock()):
        assert asyncio.run(wa._handle_whatsapp_appointment_state("+6590000000", "E2E_TEST_CONV", "4pm", conv, None, {}))
    assert '"requested_time": "16:00"' in saved.await_args.args[1]["appointment_state"]
    assert "shall i confirm" in sent.await_args.args[2].lower()


def test_new_booking_request_is_parsed_even_with_old_failed_slot_state():
    parsed = wa._parse_whatsapp_requested_datetime("Hello, please book today at 2 PM", appointment_context=True)
    assert parsed is not None
    assert parsed["requested_time"] == "14:00"


def test_slot_reason_is_not_reported_as_booking_conflict_when_outside_hours():
    settings = {
        "minimum_notice_minutes": 30, "max_booking_days_ahead": 30,
        "default_staff_role": "technician", "demo_duration_minutes": 60,
        "buffer_minutes": 15,
    }
    staff = [{
        "id": "staff-1", "active": True, "role": "technician",
        "working_days": '["wed"]', "start_time": "09:00", "end_time": "18:00",
    }]
    with patch.object(db, "get_appointment_settings", new=AsyncMock(return_value=settings)), \
         patch.object(db, "_appointment_now_local", return_value=datetime(2026, 8, 12, 8, 0)), \
         patch.object(db, "get_appointment_staff", new=AsyncMock(return_value=staff)):
        result = asyncio.run(db.get_slot_availability("2026-08-12", "20:00"))
    assert result == {"available": False, "reason": "outside_staff_hours", "conflict": False}


def test_slot_reason_reports_real_conflict_only_when_all_matching_staff_busy():
    settings = {
        "minimum_notice_minutes": 30, "max_booking_days_ahead": 30,
        "default_staff_role": "technician", "demo_duration_minutes": 60,
        "buffer_minutes": 15,
    }
    staff = [{
        "id": "staff-1", "active": True, "role": "technician",
        "working_days": '["wed"]', "start_time": "09:00", "end_time": "18:00",
    }]
    with patch.object(db, "get_appointment_settings", new=AsyncMock(return_value=settings)), \
         patch.object(db, "_appointment_now_local", return_value=datetime(2026, 8, 12, 8, 0)), \
         patch.object(db, "get_appointment_staff", new=AsyncMock(return_value=staff)), \
         patch.object(db, "_appointment_conflicts", new=AsyncMock(return_value=True)):
        result = asyncio.run(db.get_slot_availability("2026-08-12", "14:00"))
    assert result["available"] is False
    assert result["reason"] == "appointment_conflict"
    assert result["conflict"] is True


def test_delayed_same_day_request_gets_truthful_past_time_reply_not_booked_claim():
    conv = {"appointment_state": '{"status":"awaiting_datetime","service":"Aircon service","requested_date":"2026-08-12"}'}
    parsed = {
        "requested_date": "2026-08-12", "requested_time": "14:00",
        "parsed_scheduled_at": "2026-08-12T14:00:00",
        "candidate_slots": [{"requested_date": "2026-08-12", "requested_time": "14:00", "parsed_scheduled_at": "2026-08-12T14:00:00"}],
    }
    sent = AsyncMock(return_value={"success": True})
    with patch.object(db, "get_appointment_settings", new=AsyncMock(return_value={"duration_minutes": 60, "appointment_name": "Service Appointment", "delivery_mode": "online", "staff_label": "technician"})), \
         patch.object(db, "get_appointment_staff", new=AsyncMock(return_value=[{"id": "staff", "active": True}])), \
         patch.object(db, "get_next_active_appointment", new=AsyncMock(return_value=None)), \
         patch.object(db, "get_slot_availability", new=AsyncMock(return_value={"available": False, "reason": "past_or_too_soon", "conflict": False})), \
         patch.object(wa, "_parse_whatsapp_requested_datetime", return_value=parsed), \
         patch.object(wa, "_suggest_available_demo_slots", new=AsyncMock(return_value=[])), \
         patch.object(wa, "patch_conversation", new=AsyncMock()), \
         patch.object(wa, "_send_and_save_whatsapp_ai_text", new=sent), \
         patch.object(wa, "_diagnostic_event", new=AsyncMock()), \
         patch.object(wa, "_log_whatsapp_ai_event", new=AsyncMock()):
        assert asyncio.run(wa._handle_whatsapp_appointment_state("+6590000000", "conv", "2 pm", conv, {"created_at": "2026-08-12T00:56:00+00:00"}, {}))
    reply = sent.await_args.args[2].lower()
    assert "passed or is too close" in reply
    assert "already booked" not in reply


def test_meta_parser_preserves_provider_timestamp_for_latency_and_date_resolution():
    payload = {"object": "whatsapp_business_account", "entry": [{"changes": [{"value": {
        "messages": [{"from": "6590000000", "id": "wamid.timestamp", "timestamp": "1786496160", "type": "text", "text": {"body": "2 pm"}}]
    }}]}]}
    parsed = wa.parse_webhook_messages(payload)[0]
    assert parsed["timestamp"] == "1786496160"


def test_staff_notification_contract_matches_meta_template():
    assert wa.WA_TEMPLATE_PARAM_COUNTS["staff_appointment_notification_template"] == 4
    assert wa.WA_TEMPLATE_PARAM_COUNTS["staff_appointment_reminder_template"] == 3
    assert wa.WA_TEMPLATE_PARAM_COUNTS["staff_handoff_notification_template"] == 2
