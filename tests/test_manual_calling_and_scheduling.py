import pytest
from unittest.mock import AsyncMock, patch
from httpx import AsyncClient, ASGITransport
from datetime import datetime
from server import app, _make_session_token, SESSION_COOKIE_NAME
from whatsapp import execute_automation_rule


@pytest.mark.asyncio
async def test_manual_calling_automation_rule():
    contact = {
        "phone": "+919876543210",
        "lead_name": "Test Manual Lead",
        "service_type": "Interior",
    }
    rule = {
        "event_type": "new_lead",
        "source": "all",
        "enabled": True,
        "action": "manual_calling",
        "delay_minutes": 0,
    }

    with patch("db.update_lead_journey", new_callable=AsyncMock) as mock_journey, \
         patch("db.update_crm_contact_followup", new_callable=AsyncMock) as mock_fup, \
         patch("whatsapp.insert_automation_action", new_callable=AsyncMock, return_value="act_123"), \
         patch("whatsapp.update_automation_action_status", new_callable=AsyncMock):

        res = await execute_automation_rule(
            event_type="new_lead",
            contact=contact,
            context={"rule": rule, "phone": "+919876543210", "source": "website"}
        )

        assert res.get("action") == "manual_calling"
        assert res.get("call_status") == "queued_for_telecaller"
        assert mock_journey.called
        assert mock_fup.called


@pytest.mark.asyncio
async def test_whatsapp_due_today_endpoint():
    transport = ASGITransport(app=app)
    cookies = {SESSION_COOKIE_NAME: _make_session_token("admin")}
    leads = [
        {"phone_number": "+919876543210", "lead_name": "Due Customer 1"},
        {"phone_number": "+919876543211", "lead_name": "Due Customer 2"},
    ]

    with patch("server.get_crm_contacts", new_callable=AsyncMock, return_value=leads), \
         patch("whatsapp.send_whatsapp_template", new_callable=AsyncMock, return_value={"success": True}):

        async with AsyncClient(transport=transport, base_url="http://test", cookies=cookies) as client:
            res = await client.post("/api/scheduled-calls/whatsapp-due-today")
            assert res.status_code == 200
            data = res.json()
            assert data.get("success") is True
            assert data.get("total") == 2
            assert data.get("dispatched") == 2
