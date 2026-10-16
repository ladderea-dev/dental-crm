"""Safety primitives for guarded WhatsApp end-to-end health tests.

The module deliberately contains no scheduler and performs no network I/O.  The
FastAPI layer supplies authenticated persistence and Meta send callbacks after
all guards have passed.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from cryptography.fernet import Fernet, InvalidToken


RUN_CONFIRMATION = "I_CONFIRM_THIS_IS_MY_TEST_NUMBER"
MARKETING_CONFIRMATION = "I_CONFIRM_MARKETING_TEMPLATE_TEST"
MAX_REAL_MESSAGES_HARD_LIMIT = 10
RECIPIENT_COOLDOWN_MINUTES = 10
VERIFICATION_TTL_MINUTES = 10

UTILITY_TEST_PURPOSES = frozenset({
    "missed_call",
    "callback_confirmation",
    "booking_confirmation",
    "service_reminder",
    "staff_new_booking",
    "staff_daily_schedule",
    "staff_handoff",
    "staff_demo_status_request",
})
MARKETING_TEST_PURPOSES = frozenset({"welcome", "followup_no_response", "reengagement"})
ALL_TEMPLATE_TEST_PURPOSES = UTILITY_TEST_PURPOSES | MARKETING_TEST_PURPOSES

SAFE_SAMPLE_VALUES = {
    "customer_name": "Test Customer",
    "service": "General Aircon Servicing",
    "service_type": "General Aircon Servicing",
    "appointment_date": "15 August 2026",
    "appointment_time": "10:30 AM",
    "callback_time": "3:00 PM",
    "address": "123 Test Street, Singapore 123456",
    "postal_code": "123456",
    "property_type": "HDB",
    "number_of_units": "3",
    "technician_name": "Test Technician",
    "quotation_amount": "SGD 75",
    "invoice_number": "TEST-1001",
    "booking_reference": "TEST-BOOKING-1001",
    "payment_amount": "SGD 75",
}
POSITIONAL_FALLBACKS = [
    "Test Customer", "15 August 2026", "10:30 AM",
    "General Aircon Servicing", "123 Test Street, Singapore 123456",
]

SECRET_KEYS = frozenset({
    "access_token", "token", "app_secret", "verify_token", "authorization",
    "apikey", "api_key", "service_key", "password", "phone_number_encrypted",
    "verification_code", "verification_code_hash",
})


class HealthTestSafetyError(ValueError):
    """A user-actionable guard rejection."""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def mask_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", str(phone or ""))
    if len(digits) < 4:
        return "****"
    return f"+{'*' * max(2, len(digits) - 4)}{digits[-4:]}"


def country_code_for(phone: str) -> str:
    digits = re.sub(r"\D", "", str(phone or ""))
    if digits.startswith("65"):
        return "+65"
    if digits.startswith("91"):
        return "+91"
    return "+" + digits[:3]


class RecipientCipher:
    """Encrypt recipient numbers and generate a non-reversible lookup key."""

    def __init__(self, key: str):
        raw = str(key or "").strip().encode("utf-8")
        if not raw:
            raise HealthTestSafetyError("WHATSAPP_HEALTH_TEST_ENCRYPTION_KEY is not configured")
        try:
            self._fernet = Fernet(raw)
        except (ValueError, TypeError) as exc:
            raise HealthTestSafetyError("WHATSAPP_HEALTH_TEST_ENCRYPTION_KEY must be a Fernet key") from exc
        self._fingerprint_key = hashlib.sha256(raw + b":fingerprint").digest()

    def encrypt(self, phone: str) -> str:
        return self._fernet.encrypt(phone.encode("utf-8")).decode("ascii")

    def decrypt(self, ciphertext: str) -> str:
        try:
            return self._fernet.decrypt(str(ciphertext).encode("ascii")).decode("utf-8")
        except (InvalidToken, ValueError, TypeError) as exc:
            raise HealthTestSafetyError("Test recipient data cannot be decrypted") from exc

    def fingerprint(self, phone: str) -> str:
        return hmac.new(self._fingerprint_key, phone.encode("utf-8"), hashlib.sha256).hexdigest()


def generate_verification_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def hash_verification_code(recipient_id: str, code: str, encryption_key: str) -> str:
    key = hashlib.sha256(str(encryption_key).encode("utf-8")).digest()
    return hmac.new(key, f"{recipient_id}:{code}".encode("utf-8"), hashlib.sha256).hexdigest()


def verification_expiry(now: datetime | None = None) -> str:
    return ((now or utcnow()) + timedelta(minutes=VERIFICATION_TTL_MINUTES)).isoformat()


def verify_code(recipient: dict, code: str, encryption_key: str, now: datetime | None = None) -> bool:
    expiry = parse_datetime(recipient.get("verification_expires_at"))
    if not expiry or expiry < (now or utcnow()):
        return False
    expected = str(recipient.get("verification_code_hash") or "")
    actual = hash_verification_code(str(recipient.get("id") or ""), str(code or ""), encryption_key)
    return bool(expected) and hmac.compare_digest(expected, actual)


def parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def sanitize(value: Any, key: str = "") -> Any:
    """Recursively redact credentials, numbers, and oversized provider data."""
    normalized_key = key.lower()
    if normalized_key in SECRET_KEYS or any(part in normalized_key for part in ("secret", "token", "password")):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(k): sanitize(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize(item, key) for item in value[:100]]
    if isinstance(value, str):
        text = value[:1000]
        text = re.sub(r"(?<!\d)\+?\d{8,15}(?!\d)", lambda m: mask_phone(m.group(0)), text)
        text = re.sub(r"Bearer\s+\S+", "Bearer [REDACTED]", text, flags=re.I)
        return text
    return value


def validate_test_selection(
    recipient: dict,
    selected_tests: Iterable[str],
    confirmation: str,
    max_real_messages: int,
    *,
    allow_marketing: bool = False,
    marketing_confirmation: str = "",
    now: datetime | None = None,
) -> list[str]:
    if confirmation != RUN_CONFIRMATION:
        raise HealthTestSafetyError("Explicit test-number confirmation is required")
    if not recipient.get("verified") or not recipient.get("active"):
        raise HealthTestSafetyError("Only verified and active test recipients are allowed")
    maximum = int(max_real_messages or 0)
    if maximum < 1 or maximum > MAX_REAL_MESSAGES_HARD_LIMIT:
        raise HealthTestSafetyError("Maximum real messages must be between 1 and 10")
    selected = list(dict.fromkeys(str(item or "").strip() for item in selected_tests if str(item or "").strip()))
    template_tests = [item.removeprefix("template:") for item in selected if item.startswith("template:")]
    unknown = set(template_tests) - ALL_TEMPLATE_TEST_PURPOSES
    if unknown:
        raise HealthTestSafetyError("Unsupported template test selected")
    marketing = set(template_tests) & MARKETING_TEST_PURPOSES
    if marketing and (not allow_marketing or marketing_confirmation != MARKETING_CONFIRMATION):
        raise HealthTestSafetyError("Marketing templates require separate administrator confirmation")
    outbound_count = sum(1 for item in selected if item == "basic_text" or item == "ai_reply" or item.startswith("template:"))
    last_run = parse_datetime(recipient.get("last_real_test_at"))
    if outbound_count and last_run and (now or utcnow()) < last_run + timedelta(minutes=RECIPIENT_COOLDOWN_MINUTES):
        raise HealthTestSafetyError("This test recipient is in the 10-minute cooldown")
    if outbound_count > maximum:
        raise HealthTestSafetyError("Selected tests exceed the configured real-message limit")
    return selected


def template_readiness(local: dict, meta: dict) -> tuple[str, str]:
    if not meta:
        return "Missing", "Template does not exist in Meta"
    status = str(meta.get("status") or "").upper()
    if status != "APPROVED":
        pretty = status.title() if status else "Missing"
        return pretty, f"Meta status is {status or 'unknown'}"
    if str(local.get("template_name") or local.get("meta_template_name") or "") != str(meta.get("meta_template_name") or meta.get("name") or ""):
        return "Missing", "Template name does not match"
    if str(local.get("language") or "en") != str(meta.get("language") or "en"):
        return "Variable mismatch", "Template language does not match"
    expected = int(local.get("expected_params") or 0)
    actual = len(meta.get("variables") or [])
    if expected != actual:
        return "Variable mismatch", f"Expected {expected} variables but Meta has {actual}"
    return "Ready", "Approved template matches local configuration"


def _component(template: dict, component_type: str) -> dict:
    return next((item for item in (template.get("components") or []) if str(item.get("type") or "").upper() == component_type), {})


def _variable_count(text: str) -> int:
    numbers = [int(item) for item in re.findall(r"\{\{(\d+)\}\}", str(text or ""))]
    return max(numbers, default=0)


def _example_values(component: dict, key: str) -> list[str]:
    example = component.get("example") or {}
    values = example.get(key) or []
    if values and isinstance(values[0], list):
        values = values[0]
    return [str(value) for value in values if value is not None]


def safe_template_values(count: int, variable_names: list[str] | None = None, examples: list[str] | None = None, recipient_masked: str = "") -> list[str]:
    names = variable_names or []
    examples = examples or []
    values = []
    for index in range(count):
        name = names[index] if index < len(names) else ""
        if name == "phone_number" and recipient_masked:
            values.append(recipient_masked)
        elif name in SAFE_SAMPLE_VALUES:
            values.append(SAFE_SAMPLE_VALUES[name])
        elif index < len(examples) and examples[index]:
            values.append(str(examples[index])[:200])
        elif index < len(POSITIONAL_FALLBACKS):
            values.append(POSITIONAL_FALLBACKS[index])
        else:
            values.append(f"Test value {index + 1}")
    return values


def prepare_existing_template(
    template: dict,
    *,
    recipient_masked: str,
    local_variables: list[str] | None = None,
    media_urls: dict[str, str] | None = None,
    include_marketing: bool = False,
    include_authentication: bool = False,
) -> dict:
    """Analyze an arbitrary Meta template and build its safe send components."""
    media_urls = {str(k).upper(): str(v or "").strip() for k, v in (media_urls or {}).items()}
    name = str(template.get("name") or template.get("meta_template_name") or "").strip()
    language = str(template.get("language") or "").strip()
    category = str(template.get("category") or "").upper()
    status = str(template.get("status") or "").upper()
    result = {
        "template_name": name,
        "category": category,
        "language": language,
        "status": status,
        "variable_count": 0,
        "header_type": "NONE",
        "button_count": 0,
        "ready": False,
        "skip_reason": "",
        "components": [],
    }
    if status != "APPROVED":
        result["skip_reason"] = f"Template status is {status or 'unknown'}"
        return result
    if not name or not language:
        result["skip_reason"] = "Template name or language is missing"
        return result
    if category == "MARKETING" and not include_marketing:
        result["skip_reason"] = "Marketing templates are disabled"
        return result
    if category == "AUTHENTICATION" and not include_authentication:
        result["skip_reason"] = "Authentication templates are disabled"
        return result
    if category not in {"UTILITY", "MARKETING", "AUTHENTICATION"}:
        result["skip_reason"] = f"Unsupported template category: {category or 'unknown'}"
        return result

    body = _component(template, "BODY")
    body_count = _variable_count(body.get("text") or "")
    body_examples = _example_values(body, "body_text")
    body_values = safe_template_values(body_count, local_variables, body_examples, recipient_masked)
    if body_count:
        result["components"].append({
            "type": "body",
            "parameters": [{"type": "text", "text": value} for value in body_values],
        })
    result["variable_count"] += body_count

    header = _component(template, "HEADER")
    if header:
        header_type = str(header.get("format") or "TEXT").upper()
        result["header_type"] = header_type
        if header_type == "TEXT":
            count = _variable_count(header.get("text") or "")
            values = safe_template_values(count, examples=_example_values(header, "header_text"))
            if count:
                result["components"].append({"type": "header", "parameters": [{"type": "text", "text": value} for value in values]})
            result["variable_count"] += count
        elif header_type in {"IMAGE", "VIDEO", "DOCUMENT"}:
            media_url = media_urls.get(header_type, "")
            if not media_url:
                result["skip_reason"] = "Skipped: test media URL not configured."
                return result
            parameter = {"type": header_type.lower(), header_type.lower(): {"link": media_url}}
            if header_type == "DOCUMENT":
                parameter["document"]["filename"] = "airzen-test-document.pdf"
            result["components"].append({"type": "header", "parameters": [parameter]})
        else:
            result["skip_reason"] = f"Unsupported header type: {header_type}"
            return result

    buttons_component = _component(template, "BUTTONS")
    buttons = buttons_component.get("buttons") or []
    result["button_count"] = len(buttons)
    for index, button in enumerate(buttons):
        button_type = str(button.get("type") or "").upper()
        if button_type in {"QUICK_REPLY", "PHONE_NUMBER"}:
            continue
        if button_type == "URL":
            url = str(button.get("url") or "")
            count = _variable_count(url)
            if count > 1:
                result["skip_reason"] = "Dynamic URL button has unsupported variable count"
                return result
            if count == 1:
                result["components"].append({"type": "button", "sub_type": "url", "index": str(index), "parameters": [{"type": "text", "text": "airzen-health-test"}]})
                result["variable_count"] += 1
            continue
        if button_type in {"COPY_CODE", "OTP"}:
            result["components"].append({"type": "button", "sub_type": "copy_code", "index": str(index), "parameters": [{"type": "coupon_code", "coupon_code": "TEST-1001"}]})
            result["variable_count"] += 1
            continue
        result["skip_reason"] = f"Unsupported button type: {button_type or 'unknown'}"
        return result

    result["ready"] = True
    result["payload"] = {
        "messaging_product": "whatsapp",
        "to": recipient_masked,
        "type": "template",
        "template": {"name": name, "language": {"code": language}, "components": result["components"]},
    }
    return result


def safe_result(
    key: str,
    name: str,
    status: str,
    *,
    started_at: datetime,
    details: dict | None = None,
    provider_message_id: str | None = None,
    http_status: int | None = None,
    error: dict | None = None,
) -> dict:
    completed = utcnow()
    error = error or {}
    return {
        "test_key": key,
        "test_name": name,
        "status": status,
        "started_at": started_at.isoformat(),
        "completed_at": completed.isoformat(),
        "duration_ms": max(0, int((completed - started_at).total_seconds() * 1000)),
        "provider_message_id": provider_message_id,
        "http_status": http_status,
        "provider_error_code": sanitize(error.get("code")),
        "provider_error_type": sanitize(error.get("type")),
        "provider_error_message": sanitize(error.get("message")),
        "details_json": sanitize(details or {}),
    }


def report_bytes(report: dict, format_name: str) -> tuple[bytes, str]:
    safe = sanitize(report)
    if format_name == "json":
        return json.dumps(safe, indent=2, ensure_ascii=False).encode("utf-8"), "application/json"
    if format_name != "csv":
        raise HealthTestSafetyError("Unsupported report format")
    import csv
    import io
    stream = io.StringIO()
    fields = ["test_key", "test_name", "template_name", "template_category", "template_language", "status", "started_at", "submitted_at", "sent_at", "delivered_at", "completed_at", "duration_ms", "provider_message_id", "http_status", "provider_error_code", "provider_error_type", "provider_error_message", "webhook_status", "retry_count", "skip_reason", "notes"]
    writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for row in safe.get("results", []):
        writer.writerow(row)
    return stream.getvalue().encode("utf-8"), "text/csv"


@dataclass(frozen=True)
class HealthTestTimeouts:
    sent_seconds: int = 60
    delivered_seconds: int = 180
    inbound_seconds: int = 300
