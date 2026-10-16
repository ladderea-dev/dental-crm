import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import whatsapp as wa


def test_processing_retry_rules_distinguish_duplicate_failed_and_abandoned_work():
    now = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)
    assert not wa._inbound_processing_retryable({"provider_status": "processed"}, now=now)
    assert wa._inbound_processing_retryable({"provider_status": "processing_failed"}, now=now)
    assert not wa._inbound_processing_retryable({
        "provider_status": "processing", "created_at": (now - timedelta(seconds=20)).isoformat()
    }, now=now)
    assert wa._inbound_processing_retryable({
        "provider_status": "processing", "created_at": (now - timedelta(seconds=61)).isoformat()
    }, now=now)


def test_inbound_wrapper_records_success_for_terminal_processing():
    parsed = {"phone": "+6590000000", "message_id": "wamid.retry", "message_type": "text", "text": "9 am"}
    statuses = AsyncMock()
    with patch.object(wa, "_handle_inbound_whatsapp_message_once", new=AsyncMock()), \
         patch.object(wa, "_set_inbound_processing_status", new=statuses), \
         patch.object(wa, "_diagnostic_event", new=AsyncMock()):
        asyncio.run(wa.handle_inbound_whatsapp_message(parsed))
    statuses.assert_awaited_once_with("wamid.retry", "processed")


def test_inbound_wrapper_records_failure_and_reraises_for_meta_retry():
    parsed = {"phone": "+6590000000", "message_id": "wamid.retry", "message_type": "text", "text": "9 am"}
    statuses = AsyncMock()
    with patch.object(wa, "_handle_inbound_whatsapp_message_once", new=AsyncMock(side_effect=RuntimeError("synthetic"))), \
         patch.object(wa, "_set_inbound_processing_status", new=statuses), \
         patch.object(wa, "_diagnostic_event", new=AsyncMock()):
        try:
            asyncio.run(wa.handle_inbound_whatsapp_message(parsed))
        except RuntimeError:
            pass
        else:
            raise AssertionError("processing exception must reach the webhook so Meta retries")
    statuses.assert_awaited_once_with("wamid.retry", "processing_failed")


def test_alternative_slot_search_has_a_hard_database_check_limit():
    unavailable = AsyncMock(return_value={"available": False, "reason": "appointment_conflict"})
    with patch("db.get_appointment_settings", new=AsyncMock(return_value={
        "slot_interval_minutes": 45, "max_booking_days_ahead": 30
    })), patch("db.get_slot_availability", new=unavailable):
        result = asyncio.run(wa._suggest_available_demo_slots("2026-08-14", "09:00", 3))
    assert result == []
    assert unavailable.await_count == 32
