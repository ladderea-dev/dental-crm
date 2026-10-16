import asyncio
from unittest.mock import AsyncMock, patch

import server


def test_report_data_reads_whatsapp_messages_through_repository():
    repository = type("ReportRepository", (), {})()
    repository.list_whatsapp_messages_for_report = AsyncMock(
        return_value=[{"id": "message-1", "created_at": "2026-08-27T00:00:00+00:00"}]
    )

    with patch.object(server, "get_repository", return_value=repository), patch.object(
        server, "get_call_logs_for_export", new=AsyncMock(return_value=[])
    ), patch.object(server, "get_crm_contacts", new=AsyncMock(return_value=[])), patch.object(
        server, "get_all_appointments", new=AsyncMock(return_value=[])
    ), patch.object(server, "get_whatsapp_logs", new=AsyncMock(return_value=[])), patch.object(
        server, "get_followup_actions", new=AsyncMock(return_value=[])
    ), patch.object(server, "log_error", new=AsyncMock()) as log_error:
        report = asyncio.run(server._report_data())

    assert report["whatsapp_messages"] == [
        {"id": "message-1", "created_at": "2026-08-27T00:00:00+00:00"}
    ]
    repository.list_whatsapp_messages_for_report.assert_awaited_once_with(limit=5000)
    log_error.assert_not_awaited()
