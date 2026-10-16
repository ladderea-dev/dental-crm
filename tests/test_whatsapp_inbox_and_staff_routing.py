import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import server
import whatsapp as wa


def _active_request(**overrides):
    row = {
        "id": "appointment-1", "staff_id": "staff-1", "status": "booked",
        "phone": "+6591111111", "name": "Customer", "demo_status_requested": True,
        "demo_status_requested_at": datetime.now(timezone.utc).isoformat(),
        "demo_result": "", "demo_status_reply_message_id": "request-message-1",
    }
    row.update(overrides)
    return row


def test_message_history_limits_newest_rows_then_returns_chronological_order():
    chronological = [
        {"id": "inbound", "created_at": "2026-08-10T10:01:00"},
        {"id": "outbound", "created_at": "2026-08-10T10:02:00"},
    ]
    list_messages = AsyncMock(return_value=chronological)
    repository = type("Repository", (), {})()
    repository.list_whatsapp_messages = list_messages
    fake_module = type("DBModule", (), {"get_repository": lambda _self: repository})()
    with patch.object(wa, "_db", return_value=fake_module):
        rows = asyncio.run(wa.get_messages("conversation-1", limit=60))
    assert [row["id"] for row in rows] == ["inbound", "outbound"]
    list_messages.assert_awaited_once_with("conversation-1", limit=60, offset=0)


def test_message_endpoint_returns_inbound_and_outbound_from_authoritative_store():
    messages = [{"id": "in", "direction": "inbound"}, {"id": "out", "direction": "outbound"}]
    with patch.object(server, "get_messages", new=AsyncMock(return_value=messages)):
        result = asyncio.run(server.api_wa_conversation_messages("conversation-1", limit=60))
    assert result["messages"] == messages


def test_delivery_receipt_uses_repository_persistence():
    record_status = AsyncMock(return_value={
        "matched": True,
        "message_matched": True,
        "log_matched": True,
    })
    repository = type("Repository", (), {})()
    repository.record_whatsapp_delivery_status = record_status
    log_error = AsyncMock()
    fake_module = type("DBModule", (), {"get_repository": lambda _self: repository})()
    fake_module.log_error = log_error

    with patch.object(wa, "_db", return_value=fake_module):
        result = asyncio.run(wa.update_whatsapp_delivery_status({
            "message_id": "provider-1",
            "status_value": "delivered",
            "raw": {"timestamp": "1787446923", "recipient_id": "+6590000000"},
        }))

    assert result == {
        "matched": True,
        "status": "delivered",
        "provider_message_id": "provider-1",
    }
    assert record_status.await_args.args[:2] == ("provider-1", "delivered")
    assert record_status.await_args.kwargs == {"failure_reason": "", "error_code": ""}


def test_customer_with_old_history_does_not_enter_staff_route():
    with patch("db.get_appointment_staff", new=AsyncMock(return_value=[{"id": "staff-1", "whatsapp_number": "+6592222222"}])), patch.object(wa, "_diagnostic_event", new=AsyncMock()), patch.object(wa, "send_whatsapp_text", new=AsyncMock()) as send:
        assert not asyncio.run(wa.handle_staff_demo_status_reply("+6591111111", "Hi"))
    send.assert_not_awaited()


def test_expired_and_completed_requests_do_not_intercept():
    expired = _active_request(demo_status_requested_at=(datetime.now(timezone.utc) - timedelta(hours=25)).isoformat())
    completed = _active_request(id="appointment-2", demo_result="completed")
    with patch("db.get_appointment_staff", new=AsyncMock(return_value=[{"id": "staff-1", "whatsapp_number": "+6592222222"}])), patch("db.get_all_appointments", new=AsyncMock(return_value=[expired, completed])), patch.object(wa, "_diagnostic_event", new=AsyncMock()), patch.object(wa, "send_whatsapp_text", new=AsyncMock()) as send:
        assert not asyncio.run(wa.handle_staff_demo_status_reply("+6592222222", "Hi"))
    send.assert_not_awaited()


def test_assigned_staff_valid_status_updates_correct_appointment():
    update = AsyncMock(return_value=True)
    send = AsyncMock(return_value={"success": True})
    with patch("db.get_appointment_staff", new=AsyncMock(return_value=[{"id": "staff-1", "name": "Tech", "whatsapp_number": "+6592222222"}])), patch("db.get_all_appointments", new=AsyncMock(return_value=[_active_request()])), patch("db.update_appointment_demo_fields", new=update), patch("db.update_lead_journey", new=AsyncMock()), patch("db.create_followup_action", new=AsyncMock()), patch("db.log_error", new=AsyncMock()), patch.object(wa, "_diagnostic_event", new=AsyncMock()), patch.object(wa, "send_whatsapp_text", new=send), patch.object(wa, "is_whatsapp_service_window_open", new=AsyncMock(return_value=False)):
        assert asyncio.run(wa.handle_staff_demo_status_reply("+65 9222 2222", "1", {"context": {"id": "request-message-1"}}))
    assert update.await_args_list[0].args[0] == "appointment-1"
    assert update.await_args_list[0].args[1]["demo_result"] == "completed"


def test_assigned_staff_unrelated_hi_gets_status_guidance_only():
    send = AsyncMock(return_value={"success": True})
    with patch("db.get_appointment_staff", new=AsyncMock(return_value=[{"id": "staff-1", "whatsapp_number": "+6592222222"}])), patch("db.get_all_appointments", new=AsyncMock(return_value=[_active_request()])), patch("db.log_error", new=AsyncMock()), patch.object(wa, "_diagnostic_event", new=AsyncMock()), patch.object(wa, "send_whatsapp_text", new=send):
        assert asyncio.run(wa.handle_staff_demo_status_reply("+6592222222", "Hi"))
    assert send.await_args.args[1] == "Please reply with 1, 2, 3, 4, 5, or 6."


def test_phone_normalization_cannot_treat_customer_as_assigned_staff():
    appointment = _active_request(phone="+65 9222 2222")
    active, reason = wa._staff_outcome_request_active(appointment, "staff-1", "+6592222222")
    assert not active
    assert reason == "sender_is_customer"


def test_active_ui_refresh_is_non_overlapping_and_renders_endpoint_result():
    source = Path("ui/index.html").read_text(encoding="utf-8")
    assert "async function _refreshWaActiveConversation()" in source
    assert "_waActiveRefreshBusy" in source
    assert "el.innerHTML=html.join('')" in source
