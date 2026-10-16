from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import whatsapp


@pytest.mark.asyncio
async def test_unknown_outbox_delivery_is_quarantined_without_worker_crash(monkeypatch):
    log_error = AsyncMock()
    mark_failed = AsyncMock(return_value=True)
    monkeypatch.setattr(whatsapp, "_db", lambda: SimpleNamespace(log_error=log_error))
    monkeypatch.setattr(
        whatsapp,
        "_send_notification_outbox_row",
        AsyncMock(
            return_value={
                "success": False,
                "reason": "delivery_unknown",
                "error": "accepted_but_unconfirmed",
            }
        ),
    )
    monkeypatch.setattr(whatsapp, "_mark_outbox_failed", mark_failed)

    row = {
        "id": "outbox-1",
        "appointment_id": "appointment-1",
        "notification_type": "appointment_customer_confirmation",
        "attempt_count": 1,
        "max_attempts": 5,
        "claim_token": "claim-1",
    }
    result = await whatsapp.process_notification_outbox_row(row)

    assert result["success"] is False
    assert result["failure_kind"] == "unknown"
    assert result["status"] == "delivery_unknown"
    mark_failed.assert_awaited_once_with(
        row,
        "delivery_unknown",
        "accepted_but_unconfirmed",
        "",
    )
    assert log_error.await_args_list[-1].args[-1] == "warning"


@pytest.mark.asyncio
async def test_automation_action_insert_failure_never_returns_phantom_queue_id(monkeypatch):
    repository = SimpleNamespace(
        create_automation_action=AsyncMock(side_effect=RuntimeError("database unavailable"))
    )
    monkeypatch.setattr(
        whatsapp,
        "_db",
        lambda: SimpleNamespace(get_repository=lambda: repository),
    )

    with pytest.raises(RuntimeError, match="^automation_action_persistence_failed$"):
        await whatsapp.insert_automation_action(
            "+6590000000",
            "new_lead",
            "website",
            "whatsapp_only",
            datetime.now(timezone.utc),
        )
