from datetime import datetime, timedelta, timezone

import pytest
from cryptography.fernet import Fernet

from whatsapp_health_test import (
    MARKETING_CONFIRMATION,
    RUN_CONFIRMATION,
    HealthTestSafetyError,
    RecipientCipher,
    hash_verification_code,
    mask_phone,
    prepare_existing_template,
    report_bytes,
    sanitize,
    template_readiness,
    validate_test_selection,
    verify_code,
)


def recipient(**updates):
    row = {"id": "recipient-1", "verified": True, "active": True, "last_real_test_at": None}
    row.update(updates)
    return row


def test_recipient_encryption_round_trip_and_masking():
    cipher = RecipientCipher(Fernet.generate_key().decode())
    phone = "+919150151775"
    ciphertext = cipher.encrypt(phone)
    assert phone not in ciphertext
    assert cipher.decrypt(ciphertext) == phone
    assert len(cipher.fingerprint(phone)) == 64
    assert mask_phone(phone).endswith("1775")
    assert phone not in mask_phone(phone)


def test_missing_or_invalid_encryption_key_is_rejected():
    with pytest.raises(HealthTestSafetyError):
        RecipientCipher("")
    with pytest.raises(HealthTestSafetyError):
        RecipientCipher("not-a-fernet-key")


@pytest.mark.parametrize("row", [recipient(verified=False), recipient(active=False)])
def test_unverified_or_inactive_recipient_is_rejected(row):
    with pytest.raises(HealthTestSafetyError):
        validate_test_selection(row, ["template:booking_confirmation"], RUN_CONFIRMATION, 10)


def test_confirmation_message_limit_and_cooldown_are_enforced():
    with pytest.raises(HealthTestSafetyError):
        validate_test_selection(recipient(), ["template:booking_confirmation"], "", 10)
    with pytest.raises(HealthTestSafetyError):
        validate_test_selection(recipient(), ["template:booking_confirmation"], RUN_CONFIRMATION, 11)
    recent = datetime.now(timezone.utc) - timedelta(minutes=5)
    with pytest.raises(HealthTestSafetyError):
        validate_test_selection(recipient(last_real_test_at=recent.isoformat()), ["template:booking_confirmation"], RUN_CONFIRMATION, 10)


def test_configuration_only_check_is_not_blocked_by_real_message_cooldown():
    recent = datetime.now(timezone.utc).isoformat()
    assert validate_test_selection(recipient(last_real_test_at=recent), ["configuration", "database"], RUN_CONFIRMATION, 10)


def test_marketing_templates_need_separate_confirmation():
    with pytest.raises(HealthTestSafetyError):
        validate_test_selection(recipient(), ["template:welcome"], RUN_CONFIRMATION, 10)
    assert validate_test_selection(
        recipient(), ["template:welcome"], RUN_CONFIRMATION, 10,
        allow_marketing=True, marketing_confirmation=MARKETING_CONFIRMATION,
    ) == ["template:welcome"]


def test_unknown_template_and_too_many_sends_are_rejected():
    with pytest.raises(HealthTestSafetyError):
        validate_test_selection(recipient(), ["template:unknown"], RUN_CONFIRMATION, 10)
    selections = ["basic_text", "ai_reply"] + [f"template:{name}" for name in (
        "missed_call", "callback_confirmation", "booking_confirmation", "service_reminder",
        "staff_new_booking", "staff_daily_schedule", "staff_handoff", "staff_demo_status_request",
    )]
    with pytest.raises(HealthTestSafetyError):
        validate_test_selection(recipient(), selections, RUN_CONFIRMATION, 9)


def test_verification_code_is_hashed_and_expires():
    key = Fernet.generate_key().decode()
    now = datetime.now(timezone.utc)
    row = {
        "id": "recipient-1",
        "verification_code_hash": hash_verification_code("recipient-1", "123456", key),
        "verification_expires_at": (now + timedelta(minutes=10)).isoformat(),
    }
    assert verify_code(row, "123456", key, now=now)
    assert not verify_code(row, "654321", key, now=now)
    assert not verify_code(row, "123456", key, now=now + timedelta(minutes=11))


def test_template_readiness_detects_approval_and_variable_mismatch():
    local = {"template_name": "airzen_booking_confirmation", "language": "en", "expected_params": 4}
    meta = {"meta_template_name": "airzen_booking_confirmation", "language": "en", "status": "Approved", "variables": ["a", "b", "c", "d"]}
    assert template_readiness(local, meta)[0] == "Ready"
    meta["variables"] = ["a"]
    assert template_readiness(local, meta)[0] == "Variable mismatch"
    meta["status"] = "Rejected"
    assert template_readiness(local, meta)[0] == "Rejected"


def test_sanitization_masks_phone_tokens_and_nested_secrets():
    safe = sanitize({"phone": "+919150151775", "access_token": "secret", "detail": "Bearer abcdef"})
    assert "9150151775" not in str(safe)
    assert safe["access_token"] == "[REDACTED]"
    assert "abcdef" not in safe["detail"]


def test_reports_export_without_secrets():
    report = {"results": [{"test_key": "meta", "test_name": "Meta", "status": "FAIL", "provider_error_message": "token abc", "access_token": "secret"}]}
    json_data, json_type = report_bytes(report, "json")
    csv_data, csv_type = report_bytes(report, "csv")
    assert json_type == "application/json" and b"secret" not in json_data
    assert csv_type == "text/csv" and b"test_key" in csv_data


def test_sql_and_ui_contain_required_safety_controls():
    from pathlib import Path
    root = Path(__file__).resolve().parent
    sql = (root / "whatsapp_health_test_migration.sql").read_text(encoding="utf-8").lower()
    ui = (root / "ui" / "index.html").read_text(encoding="utf-8")
    server = (root / "server.py").read_text(encoding="utf-8")
    assert "revoke all on table public.whatsapp_test_recipients from public, anon, authenticated" in sql
    assert "max_real_messages between 1 and 50" in sql
    assert "MAX_REAL_MESSAGES_HARD_LIMIT = 10" in (root / "whatsapp_health_test.py").read_text(encoding="utf-8")
    assert "I confirm this is a test number controlled by me." in ui
    assert "SEND_ONE_VERIFICATION_MESSAGE" in server
    assert 'action_type == "health_test"' in (root / "whatsapp.py").read_text(encoding="utf-8")


def meta_template(category="UTILITY", components=None, status="APPROVED"):
    return {
        "id": "meta-1", "name": "airzen_test", "language": "en",
        "status": status, "category": category,
        "components": components if components is not None else [{"type": "BODY", "text": "Hi {{1}}"}],
    }


def test_existing_utility_template_builds_safe_body_payload():
    prepared = prepare_existing_template(meta_template(), recipient_masked="+*******1775", local_variables=["customer_name"])
    assert prepared["ready"] is True
    assert prepared["variable_count"] == 1
    assert prepared["payload"]["to"] == "+*******1775"
    assert prepared["components"][0]["parameters"][0]["text"] == "Test Customer"


def test_unapproved_template_is_skipped():
    prepared = prepare_existing_template(meta_template(status="REJECTED"), recipient_masked="***1775")
    assert prepared["ready"] is False
    assert "REJECTED" in prepared["skip_reason"]


def test_marketing_and_authentication_are_disabled_by_default():
    marketing = prepare_existing_template(meta_template(category="MARKETING"), recipient_masked="***1775")
    authentication = prepare_existing_template(meta_template(category="AUTHENTICATION"), recipient_masked="***1775")
    assert marketing["ready"] is False and "disabled" in marketing["skip_reason"]
    assert authentication["ready"] is False and "disabled" in authentication["skip_reason"]


def test_marketing_and_authentication_can_be_explicitly_enabled():
    marketing = prepare_existing_template(meta_template(category="MARKETING"), recipient_masked="***1775", include_marketing=True)
    authentication = prepare_existing_template(meta_template(category="AUTHENTICATION"), recipient_masked="***1775", include_authentication=True)
    assert marketing["ready"] is True
    assert authentication["ready"] is True


@pytest.mark.parametrize("media_type,setting", [("IMAGE", "IMAGE"), ("VIDEO", "VIDEO"), ("DOCUMENT", "DOCUMENT")])
def test_media_header_requires_configured_test_url(media_type, setting):
    template = meta_template(components=[{"type": "HEADER", "format": media_type}, {"type": "BODY", "text": "Test"}])
    missing = prepare_existing_template(template, recipient_masked="***1775")
    ready = prepare_existing_template(template, recipient_masked="***1775", media_urls={setting: "https://example.test/media"})
    assert missing["ready"] is False and "media URL" in missing["skip_reason"]
    assert ready["ready"] is True
    assert ready["header_type"] == media_type


def test_dynamic_url_and_quick_reply_buttons_are_supported():
    template = meta_template(components=[
        {"type": "BODY", "text": "Test"},
        {"type": "BUTTONS", "buttons": [
            {"type": "URL", "text": "Open", "url": "https://example.test/{{1}}"},
            {"type": "QUICK_REPLY", "text": "OK"},
        ]},
    ])
    prepared = prepare_existing_template(template, recipient_masked="***1775")
    assert prepared["ready"] is True
    assert prepared["button_count"] == 2
    button = next(item for item in prepared["components"] if item["type"] == "button")
    assert button["sub_type"] == "url"
    assert button["parameters"][0]["text"] == "airzen-health-test"


def test_copy_code_button_is_supported_and_unknown_button_is_skipped():
    copy_template = meta_template(components=[{"type": "BUTTONS", "buttons": [{"type": "COPY_CODE", "text": "Copy"}]}])
    unknown_template = meta_template(components=[{"type": "BUTTONS", "buttons": [{"type": "FLOW", "text": "Open"}]}])
    copy = prepare_existing_template(copy_template, recipient_masked="***1775")
    unknown = prepare_existing_template(unknown_template, recipient_masked="***1775")
    assert copy["ready"] is True
    assert copy["components"][0]["parameters"][0]["coupon_code"] == "TEST-1001"
    assert unknown["ready"] is False and "Unsupported button" in unknown["skip_reason"]


def test_examples_and_positional_fallbacks_fill_unnamed_variables():
    template = meta_template(components=[{"type": "BODY", "text": "{{1}} {{2}} {{3}}", "example": {"body_text": [["Example One"]]}}])
    prepared = prepare_existing_template(template, recipient_masked="***1775")
    values = [item["text"] for item in prepared["components"][0]["parameters"]]
    assert values == ["Example One", "15 August 2026", "10:30 AM"]


def test_all_template_static_guards_and_controls_exist():
    from pathlib import Path
    root = Path(__file__).resolve().parent
    server = (root / "server.py").read_text(encoding="utf-8")
    ui = (root / "ui" / "index.html").read_text(encoding="utf-8")
    sql = (root / "whatsapp_health_test_migration.sql").read_text(encoding="utf-8")
    assert "/api/whatsapp/health-test/templates/preview-all" in server
    assert "/api/whatsapp/health-test/templates/run-all" in server
    assert "response.status == 429 and retries < 2" in server
    assert "A health test is already active for this recipient" in server
    assert "Test All Approved Templates" in ui
    assert "Include Marketing Templates" in ui
    assert "Include Authentication Templates" in ui
    assert "provider_response_json" in sql
