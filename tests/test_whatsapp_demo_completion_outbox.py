from unittest.mock import AsyncMock

import pytest

import db
import whatsapp


@pytest.mark.asyncio
async def test_demo_completion_outbox_increments_existing_request_count(monkeypatch):
    appointment = {
        "id": "appointment-1",
        "name": "Test Customer",
        "phone": "+6590000001",
        "date": "2020-01-01",
        "time": "09:00:00",
        "timezone": "Asia/Singapore",
        "staff_id": "staff-1",
        "demo_status_request_count": 1,
    }
    updates = AsyncMock()

    async def get_setting(_key, default=""):
        return default

    monkeypatch.setattr(db, "get_setting", get_setting)
    monkeypatch.setattr(db, "get_all_appointments", AsyncMock(return_value=[appointment]))
    monkeypatch.setattr(
        db,
        "get_appointment_staff",
        AsyncMock(return_value=[{"id": "staff-1", "whatsapp_number": "+6590000002"}]),
    )
    monkeypatch.setattr(db, "update_appointment_demo_fields", updates)
    monkeypatch.setattr(db, "log_error", AsyncMock())
    monkeypatch.setattr(db, "normalize_phone", lambda value: value)
    monkeypatch.setattr(
        whatsapp,
        "enqueue_appointment_notification",
        AsyncMock(return_value={"success": True}),
    )

    await whatsapp.check_demo_completions()

    updates.assert_awaited_once()
    assert updates.await_args.args == (
        "appointment-1",
        {
            "demo_status_requested": True,
            "demo_status_requested_at": updates.await_args.args[1]["demo_status_requested_at"],
            "demo_status_request_count": 2,
        },
    )
