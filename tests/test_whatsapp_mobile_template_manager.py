import json
from pathlib import Path

import pytest

import server
import whatsapp


SERVICE_COMPLETION = {
    "slot": "service_completion_template",
    "variables": ["customer_name", "service_type", "appointment_datetime"],
}


def test_mobile_completion_template_is_supported_but_job_start_otp_is_retired():
    assert server._WA_NEW_TO_SLOT["service_completion"] == SERVICE_COMPLETION["slot"]
    assert server._WA_TEMPLATE_VARIABLES["service_completion"] == SERVICE_COMPLETION["variables"]
    assert "job_start_otp" not in server._WA_NEW_TO_SLOT
    assert "job_start_otp_template" not in whatsapp.WA_SETTINGS_KEYS
    assert "appointment_job_start_otp" not in whatsapp.NOTIFICATION_OUTBOX_TYPES

    body = server._wa_template_body("service_completion", "AirZen")
    assert server._wa_template_validation(body, SERVICE_COMPLETION["variables"])["ok"] is True


@pytest.mark.asyncio
async def test_generate_defaults_includes_completion_and_excludes_job_start_otp(monkeypatch):
    captured = {}

    async def get_setting(key, default=""):
        return {"WHATSAPP_DEFAULT_LANGUAGE": "en", "COMPANY_NAME": "AirZen"}.get(key, default)

    async def prefix():
        return "airzen"

    async def no_templates():
        return []

    async def save_templates(templates):
        captured["templates"] = templates
        return templates

    monkeypatch.setattr(server, "get_setting", get_setting)
    monkeypatch.setattr(server, "_require_wa_template_prefix", prefix)
    monkeypatch.setattr(server, "get_wa_template_objects", no_templates)
    monkeypatch.setattr(server, "save_wa_template_objects", save_templates)

    generated = await server.generate_default_wa_templates()
    by_purpose = {item["purpose"]: item for item in generated}
    assert "job_start_otp" not in by_purpose
    assert by_purpose["service_completion"]["meta_template_name"] == "airzen_service_completion"
    assert captured["templates"] == generated


@pytest.mark.asyncio
async def test_saving_completion_template_updates_only_completion_runtime_slot(monkeypatch):
    captured = {}

    async def get_setting(key, default=""):
        return "en" if key == "WHATSAPP_DEFAULT_LANGUAGE" else default

    async def save_settings(settings):
        captured.update(settings)

    monkeypatch.setattr(server, "get_setting", get_setting)
    monkeypatch.setattr(server, "save_settings", save_settings)
    saved = await server.save_wa_template_objects([{
        "purpose": "service_completion",
        "meta_template_name": "airzen_service_completion",
        "language": "en",
        "status": "Pending",
        "variables": SERVICE_COMPLETION["variables"],
        "body_text": server._wa_template_body("service_completion", "AirZen"),
    }])

    assert captured["service_completion_template"] == "airzen_service_completion"
    assert "job_start_otp_template" not in captured
    assert {item["purpose"] for item in json.loads(captured[server._WA_TEMPLATE_STORE_KEY])} == {"service_completion"}
    assert {item["purpose"] for item in saved} == {"service_completion"}


def test_crm_template_manager_does_not_offer_a_job_start_otp_template():
    ui = Path("ui/index.html").read_text(encoding="utf-8")
    keys_line = next(line for line in ui.splitlines() if line.startswith("const _WA_TEMPLATE_KEYS="))
    labels_line = next(line for line in ui.splitlines() if line.startswith("const _WA_TPL_PURPOSE_LABELS="))
    assert "job_start_otp" not in keys_line
    assert "Customer Job Start OTP" not in labels_line
    assert "'service_completion':'Customer Service Completion'" in labels_line


@pytest.mark.asyncio
async def test_retired_job_start_notification_cannot_send(monkeypatch):
    async def must_not_resolve(_purpose):
        raise AssertionError("retired notification must not resolve or send a template")

    monkeypatch.setattr(whatsapp, "resolve_wa_template", must_not_resolve)
    result = await whatsapp._send_notification_outbox_row({
        "notification_type": "appointment_job_start_otp",
        "template_purpose": "job_start_otp_template",
        "recipient_phone": "+6591234567",
    })

    assert result == {
        "success": False,
        "reason": "retired_notification_type",
        "error": "service_start_code_is_delivered_at_booking",
    }
