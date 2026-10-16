import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest

import whatsapp as wa


SG = ZoneInfo("Asia/Singapore")


def test_explicit_new_booking_detector():
    assert wa.is_explicit_new_booking_intent("Hi, pls book appointment for aircon service")
    assert wa.is_explicit_new_booking_intent("I want to book aircon servicing")
    assert not wa.is_explicit_new_booking_intent("yes, book it")


def test_booking_session_expiry_and_durable_reset():
    old = (datetime.now(SG) - timedelta(hours=7)).isoformat()
    state = {
        "status": "alternatives_offered", "updated_at": old,
        "requested_slots": [{"requested_date": "2026-08-10", "requested_time": "16:00"}],
        "requested_date": "2026-08-10", "requested_time": "16:00",
        "service_address": "Stored address", "postal_code": "123456", "property_type": "HDB",
        "contact_number": "+6590000000", "service": "Aircon service",
    }
    assert wa._booking_session_expired(state)
    reset = wa._reset_booking_session_state(state)
    assert reset["service_address"] == "Stored address"
    assert reset["postal_code"] == "123456"
    assert reset["property_type"] == "HDB"
    assert "requested_slots" not in reset
    assert "requested_date" not in reset
    assert "status" not in reset


@pytest.mark.parametrize(
    ("reference", "expected"),
    [
        (datetime(2026, 8, 10, 23, 58, tzinfo=SG), "2026-08-11"),
        (datetime(2026, 8, 11, 0, 2, tzinfo=SG), "2026-08-12"),
    ],
)
def test_tomorrow_resolves_from_current_singapore_message_time(reference, expected):
    parsed = wa._parse_whatsapp_requested_datetime("Tomorrow 4pm", appointment_context=True, reference_time=reference)
    assert parsed["requested_date"] == expected
    assert parsed["requested_time"] == "16:00"
    assert parsed["resolved_timezone"] == "Asia/Singapore"


@pytest.mark.parametrize(
    ("text", "expected_date", "expected_time"),
    [
        ("today 4pm", "2026-08-10", "16:00"),
        ("tomorrow 4pm", "2026-08-11", "16:00"),
        ("tomorrow at 4 PM", "2026-08-11", "16:00"),
        ("book tomorrow 4pm", "2026-08-11", "16:00"),
        ("next Monday 4pm", "2026-08-17", "16:00"),
        ("12 Aug 4pm", "2026-08-12", "16:00"),
    ],
)
def test_supported_singapore_date_phrases(text, expected_date, expected_time):
    reference = datetime(2026, 8, 10, 12, 0, tzinfo=SG)
    parsed = wa._parse_whatsapp_requested_datetime(text, appointment_context=True, reference_time=reference)
    assert parsed["requested_date"] == expected_date
    assert parsed["requested_time"] == expected_time


def test_fresh_intent_resets_old_alternatives_and_preserves_durable_facts():
    conv = {"appointment_state": {
        "status": "alternatives_offered", "requested_slots": [{"requested_date": "2026-08-09"}],
        "service_address": "Stored address", "postal_code": "123456", "property_type": "HDB",
    }}
    patched, sent = AsyncMock(), AsyncMock(return_value={"success": True})
    with patch("db.get_appointment_settings", new=AsyncMock(return_value={"appointment_name": "Service Appointment", "delivery_mode": "onsite"})), patch("db.get_next_active_appointment", new=AsyncMock(return_value=None)), patch.object(wa, "patch_conversation", new=patched), patch.object(wa, "_send_and_save_whatsapp_ai_text", new=sent), patch.object(wa, "_diagnostic_event", new=AsyncMock()):
        assert asyncio.run(wa._handle_whatsapp_appointment_state("+6590000000", "conv", "Hi, pls book appointment for aircon service", conv, {"created_at": datetime(2026, 8, 10, 12, 0, tzinfo=SG).isoformat()}, {"address": "Stored address"}))
    new_state = sent.await_args.args[4]["appointment_state"]
    assert new_state["status"] == "awaiting_datetime"
    assert new_state["service_address"] == "Stored address"
    assert "requested_slots" not in new_state
    assert "preferred date and time" in sent.await_args.args[2].lower()


def test_active_appointment_new_booking_intent_asks_safe_disambiguation():
    active = {"id": "active-1", "date": "2026-08-15", "time": "16:00", "status": "booked"}
    sent = AsyncMock(return_value={"success": True})
    with patch("db.get_appointment_settings", new=AsyncMock(return_value={"appointment_name": "Service Appointment"})), patch("db.get_next_active_appointment", new=AsyncMock(return_value=active)), patch.object(wa, "_send_and_save_whatsapp_ai_text", new=sent), patch.object(wa, "_diagnostic_event", new=AsyncMock()), patch.object(wa, "patch_conversation", new=AsyncMock()):
        assert asyncio.run(wa._handle_whatsapp_appointment_state("+6590000000", "conv", "I want to book an appointment", {"appointment_state": {}}, {}, {}))
    assert "new booking or change" in sent.await_args.args[2].lower()


def test_availability_exception_sends_safe_response_instead_of_silence():
    sent = AsyncMock(return_value={"success": True})
    with patch("db.get_appointment_settings", new=AsyncMock(return_value={"appointment_name": "Service Appointment", "delivery_mode": "online"})), patch("db.get_next_active_appointment", new=AsyncMock(return_value=None)), patch("db.get_appointment_staff", new=AsyncMock(return_value=[{"id": "staff", "active": True}])), patch("db.check_slot", new=AsyncMock(side_effect=RuntimeError("availability offline"))), patch.object(wa, "_send_and_save_whatsapp_ai_text", new=sent), patch.object(wa, "_diagnostic_event", new=AsyncMock()), patch.object(wa, "_log_whatsapp_ai_event", new=AsyncMock()), patch.object(wa, "patch_conversation", new=AsyncMock()):
        assert asyncio.run(wa._handle_whatsapp_appointment_state("+6590000000", "conv", "Tomorrow 4pm", {"appointment_state": {}}, {"created_at": datetime(2026, 8, 10, 12, 0, tzinfo=SG).isoformat()}, {}))
    assert "could not verify appointment availability" in sent.await_args.args[2].lower()
