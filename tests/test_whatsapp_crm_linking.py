import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import whatsapp as wa


ROOT = Path(__file__).resolve().parents[1]


class FakeCrmDB:
    def __init__(self, contacts=None):
        self.tables = {
            "crm_contacts": [dict(row) for row in (contacts or [])],
            "whatsapp_conversations": [{"id": "conv-1", "phone_number": "+6590000000"}],
        }


class FakeRepository:
    def __init__(self, fake_db):
        self.fake_db = fake_db

    async def get_crm_contact_by_phone(self, phone):
        for row in self.fake_db.tables.setdefault("crm_contacts", []):
            if row.get("phone_number") == phone:
                return dict(row)
        return None

    async def upsert_crm_contact(self, contact):
        rows = self.fake_db.tables.setdefault("crm_contacts", [])
        phone = contact.get("phone_number")
        for row in rows:
            if row.get("phone_number") == phone:
                row.update(contact)
                return dict(row)
        rows.append(dict(contact))
        return dict(contact)

    async def update_crm_contact(self, phone, updates):
        updated = False
        for row in self.fake_db.tables.setdefault("crm_contacts", []):
            if row.get("phone_number") == phone:
                row.update(updates)
                updated = True
        return updated

    async def update_whatsapp_conversation(self, conversation_id, updates):
        updated = False
        for row in self.fake_db.tables.setdefault("whatsapp_conversations", []):
            if row.get("id") == conversation_id:
                row.update(updates)
                updated = True
        return updated

    async def update_whatsapp_conversation_last_message(self, conversation_id, text, increment_unread=True):
        updates = {"last_message": text[:200], "status": "open"}
        if increment_unread:
            for row in self.fake_db.tables.setdefault("whatsapp_conversations", []):
                if row.get("id") == conversation_id:
                    row["unread_count"] = int(row.get("unread_count") or 0) + 1
        return await self.update_whatsapp_conversation(conversation_id, updates)


class FakeDbModule:
    def __init__(self, fake_db):
        self.fake_db = fake_db
        self.fake_repo = FakeRepository(fake_db)
        self.log_error = AsyncMock()

    def get_repository(self):
        return self.fake_repo


def test_whatsapp_crm_link_uses_canonical_upsert_before_direct_table_insert():
    fake = FakeCrmDB()
    fake_module = FakeDbModule(fake)
    upsert = AsyncMock()
    journey = AsyncMock()
    with patch.object(wa, "_db", return_value=fake_module), patch(
        "db.upsert_crm_lead", new=upsert
    ), patch("db.update_lead_journey", new=journey), patch(
        "db.get_crm_contact_by_phone", new=fake_module.fake_repo.get_crm_contact_by_phone
    ):
        result = asyncio.run(wa.link_conversation_to_crm("conv-1", "+6590000000", "Karuna"))
    upsert.assert_awaited_once()
    payload = upsert.await_args.args[0]
    assert payload["phone_number"] == "+6590000000"
    assert payload["lead_name"] == "Karuna"
    assert payload["source"] == "WhatsApp"
    assert upsert.await_args.kwargs["import_source"] == "inbound"
    journey.assert_awaited()
    assert result["phone"] == "+6590000000"


def test_whatsapp_crm_link_preserves_notes_and_upgrades_placeholder_contact():
    fake = FakeCrmDB([{
        "id": "crm-1",
        "phone_number": "+6590000000",
        "lead_name": "",
        "source": "Voice Call",
        "crm_notes": "[2026-08-01] Existing note",
        "crm_status": "New Lead",
    }])
    fake_module = FakeDbModule(fake)
    with patch.object(wa, "_db", return_value=fake_module), patch(
        "db.get_crm_contact_by_phone", new=fake_module.fake_repo.get_crm_contact_by_phone
    ), patch(
        "db.upsert_crm_lead", new=AsyncMock(return_value={})
    ), patch(
        "db.update_lead_journey", new=AsyncMock(return_value=True)
    ):
        result = asyncio.run(wa.link_conversation_to_crm("conv-1", "+6590000000", "Karuna"))
    assert result["crm_id"] == "crm-1"
    row = fake.tables["crm_contacts"][0]
    assert row["lead_name"] == "Karuna"
    assert row["source"] == "WhatsApp"
    assert row["preferred_channel"] == "whatsapp"
    assert "[2026-08-01] Existing note" in row["crm_notes"]
    assert "WhatsApp message received" in row["crm_notes"]


def test_whatsapp_crm_link_creates_new_lead_for_new_inbound_user():
    fake = FakeCrmDB()
    fake_module = FakeDbModule(fake)
    with patch.object(wa, "_db", return_value=fake_module), patch(
        "db.get_crm_contact_by_phone", new=fake_module.fake_repo.get_crm_contact_by_phone
    ), patch(
        "db.upsert_crm_lead", new=AsyncMock(return_value={})
    ), patch(
        "db.update_lead_journey", new=AsyncMock(return_value=True)
    ):
        result = asyncio.run(wa.link_conversation_to_crm("conv-1", "+6590000000", "Karuna"))
    assert result["phone"] == "+6590000000"
    row = fake.tables["crm_contacts"][0]
    assert row["phone_number"] == "+6590000000"
    assert row["lead_name"] == "Karuna"
    assert row["source"] == "WhatsApp"
    assert row["journey_stage"] == "new_lead"
    assert row["preferred_channel"] == "whatsapp"


def test_booking_confirmation_persists_crm_fields_and_memory():
    update = AsyncMock(return_value=True)
    journey = AsyncMock(return_value=True)
    remember = AsyncMock()
    with patch("db.update_crm_contact_full", new=update), patch(
        "db.update_lead_journey", new=journey
    ), patch.object(wa, "_remember_whatsapp_insight", new=remember), patch.object(
        wa, "_diagnostic_event", new=AsyncMock()
    ):
        asyncio.run(wa._persist_whatsapp_booking_to_crm(
            "+6590000000",
            "Karuna",
            {
                "requested_date": "2026-08-14",
                "requested_time": "10:00",
                "service_address": "02-240, 217B Sumang walk, 822217",
                "units": "2",
                "property_type": "HDB",
            },
            {"id": "appt-1", "date": "2026-08-14", "time": "10:00"},
            "General Servicing",
            "APPT-1",
        ))
    update.assert_awaited_once()
    updates = update.await_args.args[1]
    assert updates["source"] == "WhatsApp"
    assert updates["service_type"] == "General Servicing"
    assert updates["address"] == "02-240, 217B Sumang walk, 822217"
    journey.assert_awaited_once()
    assert journey.await_args.args[1]["journey_stage"] == "demo_booked"
    remember.assert_awaited_once()
    assert "booking_id=APPT-1" in remember.await_args.args[1]


def test_inbound_handler_does_not_background_crm_link_before_ai_prompt():
    source = (ROOT / "whatsapp.py").read_text(encoding="utf-8")
    assert "asyncio.create_task(link_conversation_to_crm" not in source
    assert "crm_link = await link_conversation_to_crm" in source


def test_google_sheet_sources_route_to_google_sheet_automation_event():
    for source in ("google_sheet", "Google Sheets", "googlesheet", "n8n", "n8n_google_sheet"):
        assert wa.source_to_event_type(source) == "google_sheet_lead"


def test_inbound_whatsapp_stops_ai_when_required_crm_link_fails():
    sent = AsyncMock(return_value={"success": True})
    diagnostics = AsyncMock()
    appointment_handler = AsyncMock(side_effect=AssertionError("booking should not run without CRM"))

    with patch.object(wa, "handle_staff_appointment_chat_reply", new=AsyncMock(return_value=False)), patch.object(
        wa, "handle_staff_demo_status_reply", new=AsyncMock(return_value=False)
    ), patch.object(wa, "_provider_message_record", new=AsyncMock(return_value={})), patch.object(
        wa, "get_or_create_conversation", new=AsyncMock(return_value={
            "id": "conv-1",
            "phone_number": "+6590000000",
            "ai_enabled": True,
            "status": "open",
        })
    ), patch.object(wa, "link_conversation_to_crm", new=AsyncMock(return_value=None)), patch(
        "db.get_crm_contact_by_phone", new=AsyncMock(return_value=None)
    ), patch.object(wa, "save_wa_message", new=AsyncMock(return_value={"id": "msg-1"})), patch.object(
        wa, "_log_wa", new=AsyncMock()
    ), patch.object(wa, "update_conversation_last_message", new=AsyncMock()), patch.object(
        wa, "_send_and_save_whatsapp_ai_text", new=sent
    ), patch.object(wa, "_diagnostic_event", new=diagnostics), patch.object(
        wa, "_log_whatsapp_ai_event", new=AsyncMock()
    ), patch.object(wa, "_log_whatsapp_ai_reason", new=AsyncMock()), patch.object(
        wa, "_handle_whatsapp_appointment_state", new=appointment_handler
    ), patch.object(wa, "generate_whatsapp_ai_reply", new=AsyncMock(side_effect=AssertionError("AI should not run without CRM"))), patch.object(
        wa, "_db", return_value=FakeDbModule(FakeCrmDB())
    ):
        asyncio.run(wa._handle_inbound_whatsapp_message_once({
            "phone": "+6590000000",
            "message_id": "wamid.crm-fail",
            "message_type": "text",
            "text": "I need aircon service",
            "provider": "meta",
        }))

    appointment_handler.assert_not_awaited()
    sent.assert_awaited_once()
    assert "team has received your message" in sent.await_args.args[2]
    assert sent.await_args.args[4]["crm_required_failed"] is True
    event_names = [call.args[0] for call in diagnostics.await_args_list if call.args]
    assert "crm_required_failed" in event_names
