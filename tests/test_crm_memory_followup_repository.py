import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import db


def test_crm_application_wrappers_use_repository_methods():
    row = {
        "id": "crm-1",
        "phone_number": "+6590000000",
        "lead_name": "Karuna",
        "crm_status": "New Lead",
        "last_call_outcome": "interested",
        "address": "217B Sumang Walk",
        "location": "Singapore 822217",
        "service_type": "Aircon servicing",
    }
    repo = SimpleNamespace(
        list_crm_contacts=AsyncMock(return_value=[row]),
        update_crm_contact=AsyncMock(return_value=True),
        delete_crm_contact_by_phone=AsyncMock(return_value=True),
    )

    with patch.object(db, "get_repository", return_value=repo), patch.object(
        db, "_ensure_crm_contact", new=AsyncMock()
    ):
        listed = asyncio.run(db.get_crm_contacts(status="New Lead", outcome="interested", q="kar"))
        assert asyncio.run(db.update_crm_contact_status("+6590000000", "Contacted", None))
        assert asyncio.run(db.update_crm_contact_notes("+6590000000", "Call tomorrow"))
        assert asyncio.run(db.update_crm_contact_followup("+6590000000", "2026-08-24T10:00:00"))
        assert asyncio.run(db.delete_crm_contact_by_phone("+6590000000"))

    assert listed[0]["address"] == "217B Sumang Walk"
    assert listed[0]["location"] == "Singapore 822217"
    assert listed[0]["service_type"] == "Aircon servicing"
    repo.list_crm_contacts.assert_awaited_once_with(status="New Lead", outcome="interested")
    assert repo.update_crm_contact.await_count == 3
    repo.delete_crm_contact_by_phone.assert_awaited_once_with("+6590000000")


def test_contact_memory_application_wrappers_use_repository_methods():
    repo = SimpleNamespace(
        add_contact_memory=AsyncMock(return_value="memory-1"),
        get_contact_memory=AsyncMock(return_value=[{"insight": "Customer prefers mornings"}]),
        replace_contact_memory=AsyncMock(return_value="memory-2"),
    )

    with patch.object(db, "get_repository", return_value=repo):
        asyncio.run(db.add_contact_memory("+6590000000", "Customer prefers mornings"))
        memories = asyncio.run(db.get_contact_memory("+6590000000"))
        asyncio.run(db.compress_contact_memory("+6590000000", "Compressed summary"))

    assert memories == [{"insight": "Customer prefers mornings"}]
    repo.add_contact_memory.assert_awaited_once_with("+6590000000", "Customer prefers mornings")
    repo.get_contact_memory.assert_awaited_once_with("+6590000000", limit=20)
    repo.replace_contact_memory.assert_awaited_once_with("+6590000000", "Compressed summary")


def test_followup_application_wrappers_use_repository_claim_and_owned_updates():
    action = {
        "id": "followup-1",
        "phone_number": "+6590000000",
        "status": "processing",
        "attempt_number": 1,
    }
    repo = SimpleNamespace(
        create_followup_action=AsyncMock(return_value=action),
        list_followup_actions=AsyncMock(return_value=[action]),
        claim_followup_actions=AsyncMock(return_value=[action]),
        update_followup_action_status=AsyncMock(return_value=True),
        reschedule_followup_action=AsyncMock(return_value=True),
    )

    with patch.object(db, "get_repository", return_value=repo), patch.object(
        db, "log_error", new=AsyncMock()
    ):
        action_id = asyncio.run(db.create_followup_action(
            "+6590000000", "no_response", "whatsapp_message", "whatsapp",
            "2026-08-23T10:00:00", payload={"message": "Hello"},
        ))
        listed = asyncio.run(db.get_followup_actions(phone="+6590000000", status="scheduled", limit=20))
        due = asyncio.run(db.get_due_followup_actions(limit=10))
        claimed = asyncio.run(db.claim_due_followup_actions(limit=5))
        completed = asyncio.run(db.update_followup_action_status("followup-1", "completed", {"sent": True}))
        rescheduled = asyncio.run(db.reschedule_followup_action("followup-1", "2026-08-24T10:00:00", "retry"))

    assert action_id == "followup-1"
    assert listed == due == claimed == [action]
    assert completed and rescheduled
    repo.create_followup_action.assert_awaited_once()
    repo.claim_followup_actions.assert_awaited_once()
    repo.update_followup_action_status.assert_awaited_once_with(
        "followup-1", "completed", result={"sent": True}, error_message="",
    )
    repo.reschedule_followup_action.assert_awaited_once_with(
        "followup-1", "2026-08-24T10:00:00", reason="retry",
    )
