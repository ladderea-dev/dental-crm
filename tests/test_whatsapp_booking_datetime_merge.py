import asyncio
import json
from datetime import datetime
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest

import whatsapp as wa


SG = ZoneInfo("Asia/Singapore")


@pytest.mark.parametrize("text", ["2pm", "2 pm", "2:00pm", "2:00 pm", "14:00", "at 2pm", "around 2pm"])
def test_time_only_merges_with_persisted_date(text):
    merged = wa.merge_booking_datetime(
        {"requested_date": "2026-08-11", "requested_time": ""},
        text,
        datetime(2026, 8, 10, 23, 59, tzinfo=SG),
    )
    assert merged["requested_date"] == "2026-08-11"
    assert merged["requested_time"] == "14:00"
    assert merged["complete"]


def test_empty_extraction_never_erases_persisted_datetime():
    merged = wa.merge_booking_datetime(
        {"requested_date": "2026-08-11", "requested_time": "14:00"},
        "thanks",
        datetime(2026, 8, 10, 12, 0, tzinfo=SG),
    )
    assert merged["requested_date"] == "2026-08-11"
    assert merged["requested_time"] == "14:00"


def test_ambiguous_bare_hour_requests_confirmation():
    merged = wa.merge_booking_datetime({"requested_date": "2026-08-11"}, "2")
    assert merged["ambiguous_time"] == 2
    assert not merged["time_extracted"]


@pytest.mark.parametrize("reference", [datetime(2026, 8, 10, 23, 59, tzinfo=SG), datetime(2026, 8, 11, 0, 1, tzinfo=SG)])
def test_absolute_persisted_date_does_not_shift_at_midnight(reference):
    merged = wa.merge_booking_datetime({"requested_date": "2026-08-12"}, "2 pm", reference)
    assert merged["requested_date"] == "2026-08-12"
    assert merged["requested_time"] == "14:00"


def test_date_only_preserves_date_and_requests_exact_time():
    merged = wa.merge_booking_datetime({}, "tomorrow any time", datetime(2026, 8, 10, 12, 0, tzinfo=SG))
    assert merged["requested_date"] == "2026-08-11"
    assert not merged["requested_time"]
    assert not merged["complete"]


def _run_time_reply(check_slot_result=True, check_slot_error=None, inbound_text="2 pm"):
    conv = {"appointment_state": {
        "status": "awaiting_datetime", "requested_date": "2026-08-11",
        "requested_time": "", "service": "Aircon service",
    }}
    saved, sent = AsyncMock(), AsyncMock(return_value={"success": True})
    check = AsyncMock(side_effect=check_slot_error) if check_slot_error else AsyncMock(return_value={"available": check_slot_result, "reason": "available" if check_slot_result else "appointment_conflict", "conflict": not check_slot_result})
    with patch("db.get_appointment_settings", new=AsyncMock(return_value={"appointment_name": "Service Appointment", "delivery_mode": "online"})), patch("db.get_next_active_appointment", new=AsyncMock(return_value=None)), patch("db.get_appointment_staff", new=AsyncMock(return_value=[{"id": "staff", "active": True}])), patch("db.get_slot_availability", new=check), patch.object(wa, "patch_conversation", new=saved), patch.object(wa, "_send_and_save_whatsapp_ai_text", new=sent), patch.object(wa, "_suggest_available_demo_slots", new=AsyncMock(return_value=[])), patch.object(wa, "_diagnostic_event", new=AsyncMock()), patch.object(wa, "_log_whatsapp_ai_event", new=AsyncMock()):
        handled = asyncio.run(wa._handle_whatsapp_appointment_state(
            "+6590000000", "conv", inbound_text, conv,
            {"created_at": datetime(2026, 8, 10, 23, 59, tzinfo=SG).isoformat()}, {},
        ))
    return handled, saved, sent, check


def test_standalone_time_is_persisted_and_availability_checked():
    handled, saved, sent, check = _run_time_reply(check_slot_result=True)
    assert handled
    check.assert_awaited_once_with("2026-08-11", "14:00")
    persisted = [call.args[1]["appointment_state"] for call in saved.await_args_list if "appointment_state" in call.args[1]]
    assert any(json.loads(value).get("requested_date") == "2026-08-11" and json.loads(value).get("requested_time") == "14:00" for value in persisted)
    assert sent.await_count == 1


def test_nine_am_reply_preserves_date_and_gets_a_booking_response():
    handled, saved, sent, check = _run_time_reply(check_slot_result=True, inbound_text="9 am")
    assert handled
    check.assert_awaited_once_with("2026-08-11", "09:00")
    persisted = [call.args[1]["appointment_state"] for call in saved.await_args_list if "appointment_state" in call.args[1]]
    assert any(json.loads(value).get("requested_date") == "2026-08-11" and json.loads(value).get("requested_time") == "09:00" for value in persisted)
    assert sent.await_count == 1


def test_unavailable_standalone_time_gets_alternative_response():
    handled, _saved, sent, check = _run_time_reply(check_slot_result=False)
    assert handled
    check.assert_awaited_once()
    assert "cannot be offered" in sent.await_args.args[2].lower()


def test_availability_exception_gets_safe_response():
    handled, _saved, sent, check = _run_time_reply(check_slot_error=RuntimeError("availability failed"))
    assert handled
    check.assert_awaited_once()
    assert "could not verify appointment availability" in sent.await_args.args[2].lower()


def test_persisted_date_survives_state_reconstruction_before_time_reply():
    restored = wa._restore_booking_state(
        {"status": "awaiting_datetime", "requested_date": "2026-08-11"},
        {"phone_number": "+6590000000"},
    )
    merged = wa.merge_booking_datetime(restored, "2 pm", datetime(2026, 8, 11, 0, 1, tzinfo=SG))
    assert merged["requested_date"] == "2026-08-11"
    assert merged["requested_time"] == "14:00"
