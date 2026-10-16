import whatsapp as wa


def test_notification_failure_classification_permanent_and_temporary():
    assert wa.classify_notification_send_failure({"reason": "invalid_phone"}) == "permanent"
    assert wa.classify_notification_send_failure({"error": "Number of parameters does not match"}) == "permanent"
    assert wa.classify_notification_send_failure({"error": "network timeout"}) == "temporary"
    assert wa.classify_notification_send_failure({"error": "503 unavailable"}) == "temporary"


def test_outbox_error_sanitizer_redacts_phone_and_tokens():
    sanitized = wa._sanitize_outbox_error("failed for +6591234567 with token=secret and Bearer abc.def")
    assert "+6591234567" not in sanitized
    assert "secret" not in sanitized
    assert "abc.def" not in sanitized
