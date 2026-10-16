import asyncio
import json
from datetime import date, datetime, timedelta
from unittest.mock import AsyncMock, patch

import db
import whatsapp as wa
from airzen_repository import _mysql_result_value


def _settings():
    return {
        "appointment_name": "Service appointment",
        "default_service_type": "Air-conditioning service",
        "delivery_mode": "onsite",
        "staff_label": "Technician",
        "intake_prompt": "Collect service intake details.",
    }


def test_mysql_time_values_are_portable_clock_strings():
    assert _mysql_result_value(timedelta(hours=9)) == "09:00:00"
    assert _mysql_result_value(timedelta(hours=18)) == "18:00:00"
    assert db._staff_available_for({
        "active": True,
        "working_days": ["fri"],
        "start_time": _mysql_result_value(timedelta(hours=9)),
        "end_time": _mysql_result_value(timedelta(hours=18)),
        "timezone": "Asia/Singapore",
    }, datetime(2026, 8, 28, 16, 0), "Asia/Singapore")


def test_mysql_date_values_are_portable_iso_strings():
    assert _mysql_result_value(date(2026, 8, 27)) == "2026-08-27"
    assert _mysql_result_value(datetime(2026, 8, 27, 9, 30, 15)) == "2026-08-27T09:30:15"


def test_bare_unit_reply_uses_previous_ai_question_and_saves_durable_context():
    patched_states = []

    async def fake_patch(_conv_id, payload):
        if "appointment_state" in payload:
            patched_states.append(payload["appointment_state"])
        return True

    sent = AsyncMock(return_value={"success": True})
    remember = AsyncMock()
    save_crm = AsyncMock(return_value=True)
    recent = [
        {"direction": "inbound", "message_text": "Cooling very less and timer is not working"},
        {"direction": "outbound", "message_text": "How many aircon units are affected by these issues?"},
        {"direction": "inbound", "message_text": "2"},
    ]
    with patch("db.get_appointment_settings", new=AsyncMock(return_value=_settings())), patch(
        "db.get_next_active_appointment", new=AsyncMock(return_value=None)
    ), patch("db.update_crm_contact_full", new=save_crm), patch.object(
        wa, "patch_conversation", new=fake_patch
    ), patch.object(wa, "_send_and_save_whatsapp_ai_text", new=sent), patch.object(
        wa, "_remember_whatsapp_insight", new=remember
    ), patch.object(wa, "_diagnostic_event", new=AsyncMock()), patch.object(
        wa, "_log_whatsapp_ai_event", new=AsyncMock()
    ):
        handled = asyncio.run(wa._handle_whatsapp_appointment_state(
            "+919150151775", "conv", "2", {"appointment_state": {}}, {},
            {"phone_number": "+919150151775", "lead_name": "Karuna"}, recent_messages=recent,
        ))

    assert handled
    state = json.loads(patched_states[-1])
    assert state["units"] == "2"
    assert state["problem"] == "Cooling very less and timer is not working"
    assert "do you mean 2 pm" not in sent.await_args.args[2].lower()
    assert "property type" in sent.await_args.args[2].lower()
    save_crm.assert_awaited_once()
    remember.assert_awaited_once()


def test_explicit_unit_correction_recovers_after_a_wrong_time_question():
    recent = [
        {"direction": "inbound", "message_text": "Cooling very less and timer is not working"},
        {"direction": "outbound", "message_text": "How many aircon units are affected by these issues?"},
        {"direction": "inbound", "message_text": "2"},
        {"direction": "outbound", "message_text": "Do you mean 2 PM?"},
        {"direction": "inbound", "message_text": "No 2 unit"},
    ]

    context = wa._booking_intake_context_from_history(recent, "No 2 unit")

    assert context == {
        "units": "2",
        "problem": "Cooling very less and timer is not working",
    }


def test_flexible_time_reply_offers_real_available_slots():
    state = {"status": "awaiting_datetime", "requested_date": "2026-08-28", "service": "Air-conditioning service"}
    patched = AsyncMock(return_value=True)
    sent = AsyncMock(return_value={"success": True})
    slots = [
        {"date": "2026-08-28", "time": "10:15", "label": "10:15 AM"},
        {"date": "2026-08-28", "time": "11:30", "label": "11:30 AM"},
    ]
    with patch("db.get_appointment_settings", new=AsyncMock(return_value=_settings())), patch(
        "db.get_next_active_appointment", new=AsyncMock(return_value=None)
    ), patch.object(wa, "patch_conversation", new=patched), patch.object(
        wa, "_send_and_save_whatsapp_ai_text", new=sent
    ), patch.object(wa, "_suggest_available_demo_slots", new=AsyncMock(return_value=slots)), patch.object(
        wa, "_diagnostic_event", new=AsyncMock()
    ), patch.object(wa, "_log_whatsapp_ai_event", new=AsyncMock()):
        handled = asyncio.run(wa._handle_whatsapp_appointment_state(
            "+919150151775", "conv", "Any time is ok", {"appointment_state": state}, {}, {},
        ))

    assert handled
    assert "available times are" in sent.await_args.args[2].lower()
    assert "10:15 am" in sent.await_args.args[2].lower()
    assert "11:30 am" in sent.await_args.args[2].lower()


def test_leave_it_clears_an_unavailable_booking_attempt():
    state = {"status": "alternatives_offered", "requested_date": "2026-08-28"}
    patched = AsyncMock(return_value=True)
    sent = AsyncMock(return_value={"success": True})
    with patch("db.get_appointment_settings", new=AsyncMock(return_value=_settings())), patch(
        "db.get_next_active_appointment", new=AsyncMock(return_value=None)
    ), patch.object(wa, "patch_conversation", new=patched), patch.object(
        wa, "_send_and_save_whatsapp_ai_text", new=sent
    ), patch.object(wa, "_diagnostic_event", new=AsyncMock()), patch.object(
        wa, "_log_whatsapp_ai_event", new=AsyncMock()):
        handled = asyncio.run(wa._handle_whatsapp_appointment_state(
            "+919150151775", "conv", "Then leave it", {"appointment_state": state}, {}, {},
        ))

    assert handled
    patched.assert_awaited_once_with("conv", {"appointment_state": "{}"})
    assert "not booked" in sent.await_args.args[2].lower()
