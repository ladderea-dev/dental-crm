import asyncio
import json
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import whatsapp as wa


def test_crm_address_restores_after_restart_and_ai_null_cannot_erase_it():
    restored = wa._restore_booking_state(
        {"status": "awaiting_address", "service_address": None},
        {"phone_number": "+6590000000", "address": "Blk 10 Test Street #02-03 Singapore 123456"},
    )
    assert restored["service_address"] == "Blk 10 Test Street #02-03 Singapore 123456"
    assert restored["postal_code"] == "123456"
    assert "service_address" not in wa.get_missing_booking_fields({}, restored)


def test_canonical_missing_fields_preserve_date_time_and_priority():
    state = {"service": "Aircon", "requested_date": "2026-08-20", "requested_time": "14:00"}
    assert wa.get_missing_booking_fields({}, state) == ["service_address"]
    restored = wa._restore_booking_state({"service_address": "1 Test Road Singapore 123456"}, {}, {"date": "2026-08-20", "time": "14:00"})
    assert restored["requested_date"] == "2026-08-20"
    assert restored["requested_time"] == "14:00"


def test_common_singapore_address_formats_are_recognized():
    samples = [
        "Block 123 Tampines Street 11 #04-56 Singapore 520123",
        "Blk 8 Bedok North Rd #12-34 460008",
        "10 Orchard Road, unit #03-02, Singapore 238840",
        "55 Hougang Avenue 8 530055",
    ]
    assert all(wa._looks_like_service_address(value) for value in samples)
    assert wa._extract_postal_code(samples[0]) == "520123"


def test_address_embedded_in_datetime_booking_message_is_extracted():
    text = "aircon service Tomorrow 3pm 02-240, 217B sumang walk, pincode 822217"

    address = wa._extract_service_address_from_booking_text(text)

    assert address == "02-240, 217B sumang walk, pincode 822217"
    assert wa._extract_postal_code(address) == "822217"


def test_booking_known_fields_audits_all_requested_fields():
    known = wa._booking_known_fields(
        {"lead_name": "Customer", "phone_number": "+6590000000", "address": "1 Test Road 123456", "assigned_to": "Team A"},
        {"service": "Aircon", "units": "2", "problem": "Not cold", "property_type": "HDB", "requested_date": "2026-08-20", "requested_time": "14:00", "booking_id": "APT1"},
    )
    expected = {"customer_name", "phone", "service", "units", "problem", "service_address", "postal_code", "property_type", "preferred_date", "preferred_time", "assigned_staff", "appointment_id"}
    assert expected.issubset(known)
    assert all(known[field] for field in expected)


def test_address_save_updates_crm_and_emits_sanitized_event():
    update = AsyncMock(return_value=True)
    diagnostic = AsyncMock()
    remember = AsyncMock()
    with patch("db.update_crm_contact_full", new=update), patch.object(wa, "_diagnostic_event", new=diagnostic), patch.object(wa, "_remember_whatsapp_insight", new=remember):
        assert asyncio.run(wa._persist_whatsapp_service_address("+6590000000", "  Blk 1   Test Road 123456 ", "123456"))
    update.assert_awaited_once_with("+6590000000", {"address": "Blk 1 Test Road 123456"})
    assert diagnostic.await_args.kwargs["field"] == "service_address"
    assert "Blk 1" not in str(diagnostic.await_args)


def test_property_type_turn_does_not_clear_or_reask_address():
    conv = {"appointment_state": {"status": "awaiting_intake", "service": "Aircon", "service_address": "Blk 1 Test Road 123456", "requested_date": "2026-08-20", "requested_time": "14:00"}}
    sent = AsyncMock()
    with patch("db.get_appointment_settings", new=AsyncMock(return_value={"delivery_mode": "onsite", "intake_prompt": "Share details"})), patch("db.get_next_active_appointment", new=AsyncMock(return_value=None)), patch.object(wa, "patch_conversation", new=AsyncMock()), patch.object(wa, "_diagnostic_event", new=AsyncMock()), patch.object(wa, "_send_and_save_whatsapp_ai_text", new=sent):
        handled = asyncio.run(wa._handle_whatsapp_appointment_state("+6590000000", "conv", "HDB", conv, {}, {}))
    assert handled
    reply = sent.await_args.args[2].lower()
    assert "address" not in reply
    saved_state = sent.await_args.args[4]["appointment_state"]
    assert saved_state["service_address"] == "Blk 1 Test Road 123456"
    assert saved_state["property_type"] == "HDB"


def test_ambiguous_changed_address_requires_confirmation():
    conv = {"appointment_state": {"status": "pending_confirmation", "service_address": "Blk 1 Old Road 123456", "requested_date": "2026-08-20", "requested_time": "14:00"}}
    sent, patched = AsyncMock(), AsyncMock()
    with patch("db.get_appointment_settings", new=AsyncMock(return_value={"delivery_mode": "onsite"})), patch("db.get_next_active_appointment", new=AsyncMock(return_value=None)), patch.object(wa, "patch_conversation", new=patched), patch.object(wa, "_diagnostic_event", new=AsyncMock()), patch.object(wa, "_send_and_save_whatsapp_ai_text", new=sent):
        handled = asyncio.run(wa._handle_whatsapp_appointment_state("+6590000000", "conv", "Blk 9 Other Road 654321", conv, {}, {}))
    assert handled
    assert "replace" in sent.await_args.args[2].lower()
    assert sent.await_args.args[4]["appointment_state"]["service_address"] == "Blk 1 Old Road 123456"


def test_active_chat_polling_refetches_without_overlap():
    source = Path("ui/index.html").read_text(encoding="utf-8")
    assert "async function _refreshWaActiveConversation()" in source
    assert "_waActiveRefreshBusy" in source
    assert "setInterval(_refreshWaActiveConversation,3000)" in source


def test_active_booking_state_never_falls_to_generic_ai_for_missing_fields():
    conv = {"appointment_state": {
        "status": "pending_confirmation",
        "service": "Aircon servicing",
        "requested_date": "2026-08-20",
        "requested_time": "16:00",
        "service_address": "02-240, 217B Sumang Walk, Singapore 822217",
        "property_type": "HDB",
    }}
    sent = AsyncMock(return_value={"success": True})
    patched = AsyncMock()
    with patch("db.get_appointment_settings", new=AsyncMock(return_value={
        "appointment_name": "Service appointment",
        "delivery_mode": "onsite",
        "intake_prompt": "Before I confirm, please share property type and number of units.",
    })), patch("db.get_next_active_appointment", new=AsyncMock(return_value=None)), patch.object(
        wa, "patch_conversation", new=patched
    ), patch.object(wa, "_send_and_save_whatsapp_ai_text", new=sent), patch.object(
        wa, "_diagnostic_event", new=AsyncMock()
    ), patch.object(wa, "_log_whatsapp_ai_event", new=AsyncMock()):
        handled = asyncio.run(wa._handle_whatsapp_appointment_state("+6590000000", "conv", "thanks", conv, {}, {}))
    assert handled
    reply = sent.await_args.args[2].lower()
    assert "how many aircon units" in reply
    assert "appointment is not booked yet" not in reply
    assert "team will check" not in reply
    assert '"status": "awaiting_intake"' in patched.await_args.args[1]["appointment_state"]


def test_general_ai_prompt_does_not_force_not_booked_yet_wording():
    source = Path("whatsapp.py").read_text(encoding="utf-8")
    assert "explicitly say the appointment is not booked yet" not in source
    assert "do not claim the appointment is booked" in source


def test_booking_units_parser_accepts_common_customer_replies():
    samples = ["2", "2 units", "two units", "2 aircon units", "2 FCU", "2 FCUs"]
    assert [wa._parse_booking_units(value, expecting_units=True) for value in samples] == ["2"] * len(samples)
    assert wa._parse_booking_units("2 pm", expecting_units=True) == ""


def test_common_appointment_confirmation_replies_are_supported():
    accepted = ["yes", "yes please", "y", "ok", "okay", "confirm", "CONFIRM", "book it", "please book", "go ahead", "proceed"]
    assert all(wa._is_appointment_confirmation_reply(value) for value in accepted)
    rejected = ["yesterday 2pm", "book price", "okay what is price", "please do not book", "go ahead tomorrow at 4"]
    assert not any(wa._is_appointment_confirmation_reply(value) for value in rejected)


def test_booking_flow_units_turn_reaches_confirmation_and_confirm_creates_once():
    sg = ZoneInfo("Asia/Singapore")
    settings = {
        "appointment_name": "Service appointment",
        "default_service_type": "Aircon servicing",
        "delivery_mode": "onsite",
        "staff_label": "Technician",
        "duration_minutes": 60,
        "buffer_minutes": 15,
        "timezone": "Asia/Singapore",
        "intake_prompt": "Before I confirm, please share property type and number of units.",
    }
    replies = []
    patched_states = []
    inserted = []
    booking_crm = AsyncMock()

    async def fake_send(_phone, _conv_id, reply, _inbound, raw_payload=None):
        replies.append(reply)
        return {"reply": reply, "raw_payload": raw_payload or {}}

    async def fake_patch(_conv_id, payload):
        if "appointment_state" in payload:
            patched_states.append(payload["appointment_state"])
        return True

    async def fake_insert(name, phone, date_s, time_s, service, extra):
        inserted.append({
            "name": name, "phone": phone, "date": date_s, "time": time_s,
            "service": service, "extra": extra,
        })
        return "appt-1400-id"

    existing_active = AsyncMock(side_effect=[
        None,
        {"id": "appt-1400-id", "staff_name": "Test Staff", "status": "booked"},
    ])
    patches = [
        patch("db.get_appointment_settings", new=AsyncMock(return_value=settings)),
        patch("db.get_next_active_appointment", new=AsyncMock(return_value=None)),
        patch("db.get_appointment_staff", new=AsyncMock(return_value=[{"id": "staff-1", "name": "Test Staff", "active": True}])),
        patch("db.get_slot_availability", new=AsyncMock(return_value={"available": True, "staff": {"id": "staff-1", "name": "Test Staff"}})),
        patch("db.check_slot", new=AsyncMock(return_value=True)),
        patch("db.get_existing_active_appointment", new=existing_active),
        patch("db.insert_appointment", new=fake_insert),
        patch("db.get_appointment_by_id", new=AsyncMock(return_value={"id": "appt-1400-id", "staff_name": "Test Staff", "status": "booked"})),
        patch("db.get_appointments_by_phone", new=AsyncMock(return_value=[])),
        patch("db.update_appointment_notes", new=AsyncMock(return_value=True)),
        patch.object(wa, "_persist_whatsapp_service_address", new=AsyncMock(return_value=True)),
        patch.object(wa, "_notify_appointment_booked", new=AsyncMock(return_value={"success": True})),
        patch.object(wa, "_persist_whatsapp_booking_to_crm", new=booking_crm),
        patch.object(wa, "_remember_whatsapp_insight", new=AsyncMock()),
        patch.object(wa, "_diagnostic_event", new=AsyncMock()),
        patch.object(wa, "_log_whatsapp_ai_event", new=AsyncMock()),
        patch.object(wa, "patch_conversation", new=fake_patch),
        patch.object(wa, "_send_and_save_whatsapp_ai_text", new=fake_send),
        patch.object(wa, "_atomic_outbox_required", return_value=False),
    ]
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], patches[8], patches[9], patches[10], patches[11], patches[12], patches[13], patches[14], patches[15], patches[16], patches[17], patches[18]:
        inbound = {"created_at": datetime(2026, 8, 11, 10, 0, tzinfo=sg).isoformat()}
        conv = {"id": "conv", "contact_name": "Karuna", "appointment_state": {}}
        crm = {"lead_name": "Karuna", "phone_number": "+6590000000", "service_type": "Aircon servicing"}

        assert asyncio.run(wa._handle_whatsapp_appointment_state("+6590000000", "conv", "I need aircon servicing tomorrow at 2 PM", conv, inbound, crm))
        state = replies[-1] and patched_states[-1]
        conv["appointment_state"] = state
        assert '"requested_time": "14:00"' in state

        assert asyncio.run(wa._handle_whatsapp_appointment_state("+6590000000", "conv", "02-240, 217B Sumang Walk, Singapore 822217", conv, {}, crm))
        conv["appointment_state"] = patched_states[-1]
        assert "property type" in replies[-1].lower()

        assert asyncio.run(wa._handle_whatsapp_appointment_state("+6590000000", "conv", "HDB", conv, {}, crm))
        conv["appointment_state"] = patched_states[-1]
        assert "how many aircon units" in replies[-1].lower()

        assert asyncio.run(wa._handle_whatsapp_appointment_state("+6590000000", "conv", "2 units", conv, {}, crm))
        conv["appointment_state"] = patched_states[-1]
        assert "reply confirm" in replies[-1].lower()
        assert "14:00" in conv["appointment_state"]
        assert '"units": "2"' in conv["appointment_state"]
        assert '"property_type": "HDB"' in conv["appointment_state"]
        assert "team will check" not in replies[-1].lower()
        pending_before_confirm = conv["appointment_state"]

        assert asyncio.run(wa._handle_whatsapp_appointment_state("+6590000000", "conv", "yes", conv, {}, crm))
        booked_state = patched_states[-1]
        assert '"booking_id": "appt-1400-id"' in booked_state
        assert len(inserted) == 1
        assert inserted[0]["date"] == "2026-08-12"
        assert inserted[0]["time"] == "14:00"
        assert inserted[0]["extra"]["service_address"].endswith("822217")
        assert "appointment is confirmed" in replies[-1].lower()
        assert "units: 2" in replies[-1].lower()
        assert "technician: test staff" in replies[-1].lower()
        booking_crm.assert_awaited_once()
        assert booking_crm.await_args.args[0] == "+6590000000"
        assert booking_crm.await_args.args[4] == "Aircon servicing"

        duplicate_conv = {"id": "conv", "contact_name": "Karuna", "appointment_state": pending_before_confirm}
        assert asyncio.run(wa._handle_whatsapp_appointment_state("+6590000000", "conv", "yes", duplicate_conv, {}, crm))
        assert len(inserted) == 1


def test_mixed_datetime_address_message_does_not_reask_for_address():
    sg = ZoneInfo("Asia/Singapore")
    settings = {
        "appointment_name": "Service appointment",
        "default_service_type": "General servicing",
        "delivery_mode": "onsite",
        "staff_label": "Technician",
        "duration_minutes": 60,
        "timezone": "Asia/Singapore",
        "intake_prompt": "Before I confirm, please share property type and number of units.",
    }
    replies = []
    patched_states = []
    persisted_address = AsyncMock(return_value=True)

    async def fake_patch(_conv_id, payload):
        if "appointment_state" in payload:
            patched_states.append(payload["appointment_state"])
        return True

    async def fake_send(_phone, _conv_id, reply, _inbound, raw_payload=None):
        replies.append(reply)
        return {"success": True, "raw_payload": raw_payload or {}}

    with patch("db.get_appointment_settings", new=AsyncMock(return_value=settings)), patch(
        "db.get_next_active_appointment", new=AsyncMock(return_value=None)
    ), patch("db.get_appointment_staff", new=AsyncMock(return_value=[{
        "id": "staff-1", "name": "Test Staff", "role": "service_staff", "active": True,
    }])), patch("db.get_slot_availability", new=AsyncMock(return_value={
        "available": True,
        "reason": "available",
        "timezone": "Asia/Singapore",
        "staff": {"id": "staff-1", "name": "Test Staff", "role": "service_staff"},
    })), patch.object(wa, "patch_conversation", new=fake_patch), patch.object(
        wa, "_send_and_save_whatsapp_ai_text", new=fake_send
    ), patch.object(wa, "_persist_whatsapp_service_address", new=persisted_address), patch.object(
        wa, "_diagnostic_event", new=AsyncMock()
    ), patch.object(wa, "_log_whatsapp_ai_event", new=AsyncMock()), patch.object(
        wa, "_remember_whatsapp_insight", new=AsyncMock()
    ):
        inbound = {"created_at": datetime(2026, 8, 13, 14, 0, tzinfo=sg).isoformat()}
        conv = {"id": "conv", "contact_name": "Karuna", "appointment_state": {}}
        crm = {"lead_name": "Karuna", "phone_number": "+6589134956"}

        assert asyncio.run(wa._handle_whatsapp_appointment_state(
            "+6589134956",
            "conv",
            "aircon service Tomorrow 3pm 02-240, 217B sumang walk, pincode 822217",
            conv,
            inbound,
            crm,
        ))

    final_state = json.loads(patched_states[-1])
    assert final_state["requested_date"] == "2026-08-14"
    assert final_state["requested_time"] == "15:00"
    assert final_state["service_address"] == "02-240, 217B sumang walk, pincode 822217"
    assert final_state["postal_code"] == "822217"
    assert final_state["status"] == "awaiting_intake"
    assert "share the service address" not in replies[-1].lower()
    assert "property type" in replies[-1].lower()
    assert "units" in replies[-1].lower()
    persisted_address.assert_awaited_once_with("+6589134956", "02-240, 217B sumang walk, pincode 822217", "822217")


def test_bare_number_reply_is_units_when_booking_waits_for_units():
    settings = {
        "appointment_name": "Service appointment",
        "default_service_type": "General servicing",
        "delivery_mode": "onsite",
        "staff_label": "Technician",
        "intake_prompt": "Before I confirm, please share property type and number of units.",
    }
    replies = []
    patched_states = []

    async def fake_patch(_conv_id, payload):
        if "appointment_state" in payload:
            patched_states.append(payload["appointment_state"])
        return True

    async def fake_send(_phone, _conv_id, reply, _inbound, raw_payload=None):
        replies.append(reply)
        return {"success": True, "raw_payload": raw_payload or {}}

    state = {
        "status": "awaiting_intake",
        "requested_date": "2026-08-14",
        "requested_time": "15:00",
        "timezone": "Asia/Singapore",
        "service": "General servicing",
        "service_address": "02-240, 217B sumang walk, pincode 822217",
        "postal_code": "822217",
        "property_type": "HDB",
        "intake_data": {"property_type": "HDB"},
    }
    conv = {"id": "conv", "contact_name": "Karuna", "appointment_state": json.dumps(state)}
    crm = {"lead_name": "Karuna", "phone_number": "+6589134956"}

    with patch("db.get_appointment_settings", new=AsyncMock(return_value=settings)), patch(
        "db.get_next_active_appointment", new=AsyncMock(return_value=None)
    ), patch.object(wa, "patch_conversation", new=fake_patch), patch.object(
        wa, "_send_and_save_whatsapp_ai_text", new=fake_send
    ), patch.object(wa, "_diagnostic_event", new=AsyncMock()), patch.object(
        wa, "_log_whatsapp_ai_event", new=AsyncMock()
    ):
        assert asyncio.run(wa._handle_whatsapp_appointment_state("+6589134956", "conv", "4", conv, {}, crm))

    final_state = json.loads(patched_states[-1])
    assert final_state["units"] == "4"
    assert final_state["requested_time"] == "15:00"
    assert final_state["status"] == "pending_confirmation"
    assert "do you mean 4 pm" not in replies[-1].lower()
    assert "confirm" in replies[-1].lower()


def test_address_turn_preserves_checked_datetime_and_asks_next_missing_field():
    sg = ZoneInfo("Asia/Singapore")
    settings = {
        "appointment_name": "Service appointment",
        "default_service_type": "General servicing",
        "delivery_mode": "onsite",
        "staff_label": "Technician",
        "duration_minutes": 60,
        "timezone": "Asia/Singapore",
        "intake_prompt": "Before I confirm, please share property type and number of units.",
    }
    replies = []
    patched_states = []

    async def fake_patch(_conv_id, payload):
        if "appointment_state" in payload:
            patched_states.append(payload["appointment_state"])
        return True

    async def fake_send(_phone, _conv_id, reply, _inbound, raw_payload=None):
        replies.append(reply)
        return {"success": True, "raw_payload": raw_payload or {}}

    with patch("db.get_appointment_settings", new=AsyncMock(return_value=settings)), patch(
        "db.get_next_active_appointment", new=AsyncMock(return_value=None)
    ), patch("db.get_appointment_staff", new=AsyncMock(return_value=[{
        "id": "staff-1", "name": "Test Staff", "role": "service_staff", "active": True,
    }])), patch("db.get_slot_availability", new=AsyncMock(return_value={
        "available": True,
        "reason": "available",
        "timezone": "Asia/Singapore",
        "staff": {"id": "staff-1", "name": "Test Staff", "role": "service_staff"},
    })), patch.object(wa, "patch_conversation", new=fake_patch), patch.object(
        wa, "_send_and_save_whatsapp_ai_text", new=fake_send
    ), patch.object(wa, "_persist_whatsapp_service_address", new=AsyncMock(return_value=True)), patch.object(
        wa, "_diagnostic_event", new=AsyncMock()
    ), patch.object(wa, "_log_whatsapp_ai_event", new=AsyncMock()), patch.object(
        wa, "_remember_whatsapp_insight", new=AsyncMock()
    ):
        inbound = {"created_at": datetime(2026, 8, 13, 14, 0, tzinfo=sg).isoformat()}
        conv = {"id": "conv", "contact_name": "Karuna", "appointment_state": {}}
        crm = {"lead_name": "Karuna", "phone_number": "+6589134956"}

        assert asyncio.run(wa._handle_whatsapp_appointment_state(
            "+6589134956",
            "conv",
            "tomorrow morning 10 am, my address 02-240, 217B Sumang walk, 822217",
            conv,
            inbound,
            crm,
        ))
        state_after_time = json.loads(patched_states[-1])
        assert state_after_time["requested_date"] == "2026-08-14"
        assert state_after_time["requested_time"] == "10:00"
        assert state_after_time["availability_checked"] is True
        assert state_after_time["availability_status"] == "available"
        assert state_after_time["timezone"] == "Asia/Singapore"
        assert state_after_time["resolved_timezone"] == "Asia/Singapore"
        assert state_after_time["staff_id"] == "staff-1"
        assert state_after_time["staff_role"] == "service_staff"

        conv["appointment_state"] = patched_states[-1]
        assert asyncio.run(wa._handle_whatsapp_appointment_state(
            "+6589134956",
            "conv",
            "02-240, 217B Sumang walk, 822217",
            conv,
            {},
            crm,
        ))

    final_state = json.loads(patched_states[-1])
    assert final_state["requested_date"] == "2026-08-14"
    assert final_state["requested_time"] == "10:00"
    assert final_state["timezone"] == "Asia/Singapore"
    assert final_state["service_address"] == "02-240, 217B Sumang walk, 822217"
    assert final_state["postal_code"] == "822217"
    assert final_state["availability_checked"] is True
    assert final_state["staff_id"] == "staff-1"
    assert "preferred date and time" not in replies[-1].lower()
    assert "could not continue" not in replies[-1].lower()
    assert "property type" in replies[-1].lower()
    assert "units" in replies[-1].lower()


def test_state_handler_exception_with_persisted_datetime_does_not_ask_for_datetime_again():
    async def broken_state_handler(*_args, **_kwargs):
        raise NameError("_APPOINTMENT_ACTIVE_STATUSES")

    sent = AsyncMock(return_value={"success": True})
    patched = AsyncMock(return_value=True)
    latest_conv = {"id": "conv", "appointment_state": json.dumps({
        "status": "awaiting_address",
        "requested_date": "2026-08-14",
        "requested_time": "10:00",
        "timezone": "Asia/Singapore",
        "service": "General servicing",
    })}
    with patch.object(wa, "get_or_create_conversation", new=AsyncMock(return_value=latest_conv)), patch.object(
        wa, "get_conversation_by_id", new=AsyncMock(return_value=latest_conv)
    ), patch.object(wa, "link_conversation_to_crm", new=AsyncMock(return_value={"crm_id": "crm"})), patch(
        "db.get_crm_contact_by_phone", new=AsyncMock(return_value={"lead_name": "Karuna", "phone_number": "+6589134956"})
    ), patch.object(wa, "save_wa_message", new=AsyncMock(return_value={"id": "inbound-row"})), patch.object(
        wa, "_provider_message_record", new=AsyncMock(return_value={})
    ), patch.object(wa, "_log_wa", new=AsyncMock()), patch.object(
        wa, "update_conversation_last_message", new=AsyncMock()
    ), patch.object(wa, "is_whatsapp_service_window_open", new=AsyncMock(return_value=True)), patch.object(
        wa, "_handle_whatsapp_appointment_state", new=broken_state_handler
    ), patch.object(wa, "_send_and_save_whatsapp_ai_text", new=sent), patch.object(
        wa, "patch_conversation", new=patched
    ), patch.object(wa, "_diagnostic_event", new=AsyncMock()), patch.object(
        wa, "_log_whatsapp_ai_event", new=AsyncMock()
    ), patch("db.get_appointment_staff", new=AsyncMock(return_value=[])), patch.object(
        wa, "get_automation_actions", new=AsyncMock(return_value=[])
    ), patch("db.log_error", new=AsyncMock()), patch(
        "db.update_lead_journey", new=AsyncMock()
    ), patch(
        "db.get_setting", new=AsyncMock(return_value="Asia/Singapore")
    ), patch("db.get_appointment_settings", new=AsyncMock(return_value={
        "appointment_name": "Service appointment",
        "delivery_mode": "onsite",
        "intake_prompt": "Before I confirm, please share property type and number of units.",
    })):
        asyncio.run(wa._handle_inbound_whatsapp_message_once({
            "phone": "+6589134956",
            "message_id": "wamid.address",
            "message_type": "text",
            "text": "02-240, 217B Sumang walk, 822217",
            "provider": "meta",
        }))

    reply = sent.await_args.args[2].lower()
    assert "preferred date and time" not in reply
    assert "could not continue" not in reply
    assert "service address" in reply or "property type" in reply or "units" in reply
