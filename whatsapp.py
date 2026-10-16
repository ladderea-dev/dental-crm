"""WhatsApp helper — Phase 7.

Handles:
- Reading WhatsApp settings safely from the settings table
- send_whatsapp_template()  — Meta Cloud API template send
- send_whatsapp_text_if_allowed()  — free-text only when customer has messaged first
- Automation rule helpers: get/save/find/execute rules
- Automation action queue: insert/list/run-due
- Confirmation helpers: callback / appointment / showroom
- Structured for Phase 8 webhook/inbox without any Phase 8 code yet
"""

import asyncio
import hashlib
import json as _json
import logging
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import aiohttp

from followup import detect_followup_intent, parse_followup_time

logger = logging.getLogger("whatsapp")


_background_log_tasks: set[asyncio.Task] = set()


def _schedule_background_log(coro, label: str = "whatsapp_background_log") -> None:
    """Run best-effort diagnostic logging without delaying customer replies."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    task = loop.create_task(coro)
    _background_log_tasks.add(task)

    def _done(done_task: asyncio.Task) -> None:
        _background_log_tasks.discard(done_task)
        try:
            done_task.result()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.debug("%s failed: %s", label, exc)

    task.add_done_callback(_done)


def _diagnostic_context(phone: str = "", conversation_id: str = "", message_id: str = "", **fields) -> str:
    """Build correlation-only diagnostic context without customer content/PII."""
    safe = {}
    if phone:
        safe["contact_ref"] = hashlib.sha256(str(phone).encode("utf-8")).hexdigest()[:12]
    if conversation_id:
        safe["conversation_ref"] = hashlib.sha256(str(conversation_id).encode("utf-8")).hexdigest()[:12]
    if message_id:
        safe["message_ref"] = hashlib.sha256(str(message_id).encode("utf-8")).hexdigest()[:12]
    for key, value in fields.items():
        if value is not None and key not in {"phone", "text", "message", "body", "token", "secret"}:
            safe[key] = str(value)[:120]
    return _json.dumps(safe, separators=(",", ":"), sort_keys=True)


async def _diagnostic_event(event: str, *, phone: str = "", conversation_id: str = "", message_id: str = "", level: str = "info", **fields) -> None:
    detail = _diagnostic_context(phone, conversation_id, message_id, **fields)
    getattr(logger, "error" if level == "error" else "warning" if level == "warning" else "info")("whatsapp_diagnostic event=%s context=%s", event, detail)
    _schedule_background_log(_db().log_error("whatsapp_diagnostic", event, detail, level), "whatsapp_diagnostic_log")

# ── WhatsApp settings keys ─────────────────────────────────────────────────
WA_SETTINGS_KEYS = [
    "WHATSAPP_ENABLED",
    "WHATSAPP_PROVIDER",
    "META_APP_ID",
    "META_APP_SECRET",
    "WHATSAPP_ACCESS_TOKEN",
    "WHATSAPP_PHONE_NUMBER_ID",
    "WHATSAPP_BUSINESS_ACCOUNT_ID",
    "WHATSAPP_VERIFY_TOKEN",
    "WHATSAPP_GRAPH_VERSION",
    "WHATSAPP_DEFAULT_LANGUAGE",
    # Vobiz provider settings
    "VOBIZ_AUTH_ID",
    "VOBIZ_AUTH_TOKEN",
    "VOBIZ_CHANNEL_ID",
    "VOBIZ_WEBHOOK_SECRET",
    # Template purpose slots — admin enters actual Meta/Vobiz template name per purpose
    "welcome_template",
    "missed_call_template",
    "callback_confirmation_template",
    "appointment_confirmation_template",
    "staff_appointment_notification_template",
    "staff_appointment_reminder_template",
    "staff_handoff_notification_template",
    "reminder_template",
    "no_response_followup_template",
    "re_enquiry_followup_template",
    "staff_demo_status_request",
    "service_completion_template",
]

WA_DEFAULTS = {
    "WHATSAPP_ENABLED": "false",
    "WHATSAPP_PROVIDER": "meta",
    "WHATSAPP_GRAPH_VERSION": "v20.0",
    "WHATSAPP_DEFAULT_LANGUAGE": "en",
    "staff_appointment_notification_template": "staff_appointment_notification",
    "staff_appointment_reminder_template": "staff_appointment_reminder",
    "staff_handoff_notification_template": "staff_appointment_alert",
    "staff_demo_status_request": "staff_demo_status_request",
}

# ── Template purpose slot labels (for UI and health reporting) ───────────────
WA_TEMPLATE_PURPOSES = [
    ("welcome_template",                  "Welcome Message"),
    ("missed_call_template",              "Missed Call Follow-up"),
    ("callback_confirmation_template",    "Callback Confirmation"),
    ("appointment_confirmation_template", "Appointment Confirmation"),
    ("staff_appointment_notification_template", "Staff Appointment Notification"),
    ("staff_appointment_reminder_template", "Staff Appointment Reminder"),
    ("staff_handoff_notification_template", "Staff Handoff Alert"),
    ("reminder_template",                 "Reminder"),
    ("no_response_followup_template",     "No Response Follow-up"),
    ("re_enquiry_followup_template",      "Re-enquiry Follow-up"),
    ("staff_demo_status_request",         "Staff Appointment Outcome Request"),
    ("service_completion_template",       "Customer Service Completion"),
]

WA_TEMPLATE_PARAM_COUNTS = {
    "welcome_template": 2,
    "missed_call_template": 2,
    "callback_confirmation_template": 4,
    "appointment_confirmation_template": 4,
    "staff_appointment_notification_template": 4,
    "staff_appointment_reminder_template": 3,
    "staff_handoff_notification_template": 2,
    "reminder_template": 3,
    "no_response_followup_template": 2,
    "re_enquiry_followup_template": 2,
    "staff_demo_status_request": 3,
    "service_completion_template": 3,
}

WA_TEMPLATE_DEBUG_PARAM_COUNTS = {
    "no_response_followup_template": 1,
    "reminder_template": 3,
    "appointment_confirmation_template": 3,
    "staff_appointment_notification_template": 4,
    "staff_appointment_reminder_template": 3,
    "re_enquiry_followup_template": 2,
    "callback_confirmation_template": 2,
    "welcome_template": 2,
    "missed_call_template": 2,
    "staff_handoff_notification_template": 2,
    "staff_demo_status_request": 3,
    "service_completion_template": 3,
}

WA_TEMPLATE_COOLDOWNS = {
    "re_enquiry_followup_template": 24 * 60,
    "no_response_followup_template": 24 * 60,
    "missed_call_template": 2 * 60,
    "missed_call_followup": 2 * 60,
    "appointment_confirmation_template": 30 * 24 * 60,
    "staff_appointment_notification_template": 0,
    "staff_appointment_reminder_template": 0,
    "staff_handoff_notification_template": 0,
    "service_completion_template": 0,
}

# ── Backward-compat: old key → new purpose slot ───────────────────────────
_WA_LEGACY_KEY_MAP = {
    # Old WA_SETTINGS_KEYS that clients may still have saved
    "WHATSAPP_WELCOME_TEMPLATE":       "welcome_template",
    "WHATSAPP_MISSED_CALL_TEMPLATE":   "missed_call_template",
    "WHATSAPP_BUSY_CALL_TEMPLATE":     "missed_call_template",
    "WHATSAPP_FAILED_CALL_TEMPLATE":   "no_response_followup_template",
    "WHATSAPP_CALLBACK_TEMPLATE":      "callback_confirmation_template",
    "WHATSAPP_APPOINTMENT_TEMPLATE":   "appointment_confirmation_template",
    "WHATSAPP_STAFF_APPOINTMENT_NOTIFICATION_TEMPLATE": "staff_appointment_notification_template",
    "WHATSAPP_STAFF_APPOINTMENT_REMINDER_TEMPLATE": "staff_appointment_reminder_template",
    "WHATSAPP_STAFF_HANDOFF_NOTIFICATION_TEMPLATE": "staff_handoff_notification_template",
    "WHATSAPP_SHOWROOM_VISIT_TEMPLATE":"appointment_confirmation_template",
    "WHATSAPP_RE_ENQUIRY_TEMPLATE":    "re_enquiry_followup_template",
    "WHATSAPP_FOLLOWUP_TEMPLATE":      "no_response_followup_template",
    # Old literal purpose names used in automation rule whatsapp_template field
    "callback_template":               "callback_confirmation_template",
    "appointment_template":            "appointment_confirmation_template",
    "showroom_visit_template":         "appointment_confirmation_template",
    "re_enquiry_template":             "re_enquiry_followup_template",
    "followup_template":               "no_response_followup_template",
    # Old literal template name values that some clients had in settings
    "voice_ai_demo_welcome":           "welcome_template",
    "missed_call_followup":            "missed_call_template",
    "appointment_confirmation":        "appointment_confirmation_template",
    "staff_appointment_notification":  "staff_appointment_notification_template",
    "staff_appointment_reminder":      "staff_appointment_reminder_template",
    "staff_appointment_alert":         "staff_handoff_notification_template",
    "demo_reminder":                   "reminder_template",
    "callback_confirmation":           "callback_confirmation_template",
    "re_enquiry_followup":             "re_enquiry_followup_template",
    "no_response_followup":            "no_response_followup_template",
}

# ── Automation rule settings key ───────────────────────────────────────────
_AUTOMATION_RULES_KEY = "AUTOMATION_RULES_JSON"

# ── Automation event types ─────────────────────────────────────────────────
AUTOMATION_EVENT_TYPES = [
    "new_lead",
    "manual_lead",
    "uploaded_lead",
    "facebook_lead",
    "instagram_lead",
    "website_lead",
    "google_sheet_lead",
    "api_lead",
    "followup_lead",
    "callback_scheduled",
    "appointment_confirmed",
    "showroom_visit_confirmed",
    "missed_call_retry",
    "re_enquiry",
]

# ── Automation action types ────────────────────────────────────────────────
AUTOMATION_ACTION_TYPES = [
    "manual_only",
    "call_only",
    "whatsapp_only",
    "whatsapp_and_call_now",
    "whatsapp_then_call_after_delay",
    "call_then_whatsapp_on_failure",
    "call_then_whatsapp_always",
    "whatsapp_then_call_on_reply",
]

# Call outcomes that trigger fallback WhatsApp when configured
WA_FALLBACK_OUTCOMES = {"no_answer", "busy", "failed", "failed_call", "unreachable", "switched_off"}

NOTIFICATION_OUTBOX_STATUSES = {"pending", "processing", "sent", "failed", "permanently_failed"}
NOTIFICATION_OUTBOX_TYPES = {
    "appointment_customer_confirmation": "appointment_confirmation_template",
    "appointment_staff_confirmation": "staff_appointment_notification_template",
    "appointment_customer_reschedule": "appointment_confirmation_template",
    "appointment_staff_reschedule": "staff_appointment_notification_template",
    "appointment_customer_cancellation": "appointment_confirmation_template",
    "appointment_staff_cancellation": "staff_appointment_notification_template",
    "appointment_customer_reminder": "reminder_template",
    "appointment_staff_reminder": "staff_appointment_reminder_template",
    "staff_outcome_request": "staff_demo_status_request",
    "no_active_staff_escalation": "staff_handoff_notification_template",
    "booking_failure_escalation": "staff_handoff_notification_template",
    "appointment_customer_completion": "service_completion_template",
}
NOTIFICATION_OUTBOX_MAX_ATTEMPTS = 5


def _safe_ref(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()[:12]


def _atomic_outbox_required() -> bool:
    from airzen_repository import configured_backend

    return configured_backend() == "mysql" or (
        (os.getenv("AIRZEN_REQUIRE_ATOMIC_OUTBOX", "false") or "").strip().lower() in {"1", "true", "yes", "on"}
    )


def _sanitize_outbox_error(error: str) -> str:
    text = str(error or "")
    text = re.sub(r"\+?\d[\d\s().-]{6,}\d", "[phone]", text)
    text = re.sub(r"Bearer\s+[A-Za-z0-9._~-]+", "Bearer [redacted]", text, flags=re.I)
    text = re.sub(r"(token|secret|key)=([^;\s]+)", r"\1=[redacted]", text, flags=re.I)
    return text[:500]


def classify_notification_send_failure(result: dict) -> str:
    reason = str((result or {}).get("reason") or "").lower()
    error = str((result or {}).get("error") or "").lower()
    combined = f"{reason} {error}"
    unknown_markers = (
        "delivery_unknown", "status_update_failed_after_send", "timeout_after_submit",
        "unknown_delivery", "accepted_but_unconfirmed",
    )
    permanent_markers = (
        "invalid_phone", "template_missing", "template_param_mismatch",
        "invalid template", "language", "parameter", "recipient not on whatsapp",
        "whatsapp_not_configured", "vobiz_template_format_not_configured",
        "meta_ecosystem_engagement_restriction",
    )
    temporary_markers = (
        "timeout", "temporar", "rate", "429", "500", "502", "503", "504",
        "send_error", "network", "unavailable",
    )
    if any(marker in combined for marker in unknown_markers):
        return "unknown"
    if any(marker in combined for marker in permanent_markers):
        return "permanent"
    if any(marker in combined for marker in temporary_markers):
        return "temporary"
    return "temporary"


def _outbox_next_attempt_at(attempts: int) -> str:
    delay_minutes = min(60, max(1, 2 ** max(attempts - 1, 0)))
    return (datetime.now(timezone.utc) + timedelta(minutes=delay_minutes)).isoformat()


def appointment_notification_dedupe_key(appointment_id: str, notification_type: str, recipient_type: str) -> str:
    raw = f"{appointment_id}:{notification_type}:{recipient_type}".lower()
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ── Default automation rules ───────────────────────────────────────────────
def _default_automation_rules() -> list:
    # whatsapp_template values are purpose-slot keys (welcome_template, etc.) or
    # "custom:<actual_template_name>" for one-off overrides.
    return [
        {"event_type": "new_lead",               "source": "all", "enabled": False, "action": "manual_only", "delay_minutes": 0,  "call_type": "welcome_call",            "whatsapp_template": "welcome_template",                "fallback_whatsapp_template": "missed_call_template",    "send_on_no_answer": True,  "send_on_busy": True,  "send_on_failed": True,  "respect_outbound_schedule": True},
        {"event_type": "manual_lead",            "source": "all", "enabled": False, "action": "manual_only", "delay_minutes": 0,  "call_type": "welcome_call",            "whatsapp_template": "welcome_template",                "fallback_whatsapp_template": "missed_call_template",    "send_on_no_answer": True,  "send_on_busy": True,  "send_on_failed": True,  "respect_outbound_schedule": True},
        {"event_type": "uploaded_lead",          "source": "all", "enabled": False, "action": "manual_only", "delay_minutes": 0,  "call_type": "welcome_call",            "whatsapp_template": "welcome_template",                "fallback_whatsapp_template": "missed_call_template",    "send_on_no_answer": True,  "send_on_busy": True,  "send_on_failed": True,  "respect_outbound_schedule": True},
        {"event_type": "facebook_lead",          "source": "all", "enabled": False, "action": "manual_only", "delay_minutes": 30, "call_type": "welcome_call",            "whatsapp_template": "welcome_template",                "fallback_whatsapp_template": "missed_call_template",    "send_on_no_answer": True,  "send_on_busy": True,  "send_on_failed": True,  "respect_outbound_schedule": True},
        {"event_type": "instagram_lead",         "source": "all", "enabled": False, "action": "manual_only", "delay_minutes": 30, "call_type": "welcome_call",            "whatsapp_template": "welcome_template",                "fallback_whatsapp_template": "missed_call_template",    "send_on_no_answer": True,  "send_on_busy": True,  "send_on_failed": True,  "respect_outbound_schedule": True},
        {"event_type": "website_lead",           "source": "all", "enabled": False, "action": "manual_only", "delay_minutes": 0,  "call_type": "welcome_call",            "whatsapp_template": "welcome_template",                "fallback_whatsapp_template": "missed_call_template",    "send_on_no_answer": True,  "send_on_busy": True,  "send_on_failed": True,  "respect_outbound_schedule": True},
        {"event_type": "google_sheet_lead",      "source": "all", "enabled": False, "action": "manual_only", "delay_minutes": 0,  "call_type": "welcome_call",            "whatsapp_template": "welcome_template",                "fallback_whatsapp_template": "missed_call_template",    "send_on_no_answer": True,  "send_on_busy": True,  "send_on_failed": True,  "respect_outbound_schedule": True},
        {"event_type": "api_lead",               "source": "all", "enabled": False, "action": "manual_only", "delay_minutes": 0,  "call_type": "welcome_call",            "whatsapp_template": "welcome_template",                "fallback_whatsapp_template": "missed_call_template",    "send_on_no_answer": True,  "send_on_busy": True,  "send_on_failed": True,  "respect_outbound_schedule": True},
        {"event_type": "followup_lead",          "source": "all", "enabled": False, "action": "manual_only", "delay_minutes": 0,  "call_type": "followup_call",           "whatsapp_template": "no_response_followup_template",  "fallback_whatsapp_template": "missed_call_template",    "send_on_no_answer": True,  "send_on_busy": True,  "send_on_failed": True,  "respect_outbound_schedule": True},
        {"event_type": "callback_scheduled",     "source": "all", "enabled": False, "action": "manual_only", "delay_minutes": 0,  "call_type": "callback_call",           "whatsapp_template": "callback_confirmation_template", "fallback_whatsapp_template": "missed_call_template",    "send_on_no_answer": False, "send_on_busy": False, "send_on_failed": False, "respect_outbound_schedule": True},
        {"event_type": "appointment_confirmed",  "source": "all", "enabled": False, "action": "manual_only", "delay_minutes": 0,  "call_type": "appointment_confirmation", "whatsapp_template": "appointment_confirmation_template","fallback_whatsapp_template": "",                      "send_on_no_answer": False, "send_on_busy": False, "send_on_failed": False, "respect_outbound_schedule": False},
        {"event_type": "showroom_visit_confirmed","source": "all", "enabled": False, "action": "manual_only", "delay_minutes": 0,  "call_type": "appointment_confirmation", "whatsapp_template": "appointment_confirmation_template","fallback_whatsapp_template": "",                      "send_on_no_answer": False, "send_on_busy": False, "send_on_failed": False, "respect_outbound_schedule": False},
        {"event_type": "re_enquiry",             "source": "all", "enabled": False, "action": "manual_only", "delay_minutes": 0,  "call_type": "re_enquiry",              "whatsapp_template": "re_enquiry_followup_template",   "fallback_whatsapp_template": "missed_call_template",    "send_on_no_answer": True,  "send_on_busy": True,  "send_on_failed": True,  "respect_outbound_schedule": True},
        {"event_type": "missed_call_retry",      "source": "all", "enabled": False, "action": "manual_only", "delay_minutes": 0,  "call_type": "missed_call_retry",       "whatsapp_template": "missed_call_template",           "fallback_whatsapp_template": "",                      "send_on_no_answer": False, "send_on_busy": False, "send_on_failed": False, "respect_outbound_schedule": True},
    ]


# ── Lazy import of db helpers to avoid circular imports ────────────────────
def _db():
    import db as _db_mod
    return _db_mod


async def _get_wa_setting(key: str) -> str:
    """Read a WhatsApp setting.

    Precedence: settings table (user-saved via UI) → env var → WA_DEFAULTS.
    This is intentionally different from db.get_setting() which prefers env first;
    for WhatsApp we want the dashboard Save button to be the source of truth so
    Coolify/host env vars don't silently override user choices like
    WHATSAPP_ENABLED, WHATSAPP_PROVIDER, VOBIZ_* credentials, etc.
    """
    try:
        value = await _db().get_repository().get_setting(key)
        if value not in (None, ""):
            return value
    except Exception as exc:
        logger.debug("_get_wa_setting DB read failed for %s: %s", key, exc)
    env_val = os.getenv(key, "")
    if env_val:
        return env_val
    return WA_DEFAULTS.get(key, "")


def _is_truthy(val) -> bool:
    if isinstance(val, bool):
        return val
    s = str(val or "").strip().lower()
    return s in ("1", "true", "yes", "on", "enabled", "y", "t")


async def _is_wa_enabled() -> bool:
    return _is_truthy(await _get_wa_setting("WHATSAPP_ENABLED"))


async def _wa_config() -> dict:
    """Return all WhatsApp settings (token NOT masked here — masked at API layer)."""
    out = {}
    for k in WA_SETTINGS_KEYS:
        out[k] = await _get_wa_setting(k) or WA_DEFAULTS.get(k, "")
    return out


def _mask_secret(val: str) -> str:
    val = val or ""
    if len(val) >= 4:
        return "********" + val[-4:]
    if val:
        return "****"
    return ""


async def get_wa_settings_masked() -> dict:
    """Return settings with provider secrets masked for frontend display."""
    cfg = await _wa_config()
    cfg["META_APP_SECRET"] = _mask_secret(cfg.get("META_APP_SECRET") or "")
    cfg["WHATSAPP_ACCESS_TOKEN"] = _mask_secret(cfg.get("WHATSAPP_ACCESS_TOKEN") or "")
    cfg["WHATSAPP_VERIFY_TOKEN"] = _mask_secret(cfg.get("WHATSAPP_VERIFY_TOKEN") or "")
    cfg["VOBIZ_AUTH_TOKEN"] = _mask_secret(cfg.get("VOBIZ_AUTH_TOKEN") or "")
    cfg["VOBIZ_WEBHOOK_SECRET"] = _mask_secret(cfg.get("VOBIZ_WEBHOOK_SECRET") or "")
    return cfg


async def save_wa_settings(data: dict) -> None:
    """Save WhatsApp settings to settings table. Skips masked secret placeholders."""
    from db import set_setting
    _SECRET_KEYS = {"META_APP_SECRET", "WHATSAPP_ACCESS_TOKEN", "WHATSAPP_VERIFY_TOKEN", "VOBIZ_AUTH_TOKEN", "VOBIZ_WEBHOOK_SECRET"}
    _BOOL_KEYS = {"WHATSAPP_ENABLED"}
    for k in WA_SETTINGS_KEYS:
        v = data.get(k)
        if v is None:
            continue
        if k in _SECRET_KEYS and (not v or "****" in str(v)):
            continue  # do not overwrite with masked value
        if k in _BOOL_KEYS:
            v = "true" if _is_truthy(v) else "false"
        await set_setting(k, str(v))


async def resolve_wa_template(purpose_or_name: str) -> str:
    """Resolve a template purpose key or custom override to the actual Meta/Vobiz template name.

    Rules (in order):
    1. If value starts with 'custom:' strip prefix and return the literal name.
    2. If value is a purpose slot key (welcome_template, etc.), look it up in settings.
    3. If value is a legacy key name, map to new slot and look up.
    4. Otherwise treat value as a literal template name (pass-through).
    Returns empty string if nothing is configured.
    """
    if not purpose_or_name:
        return ""
    # Custom override: "custom:actual_template_name"
    if purpose_or_name.startswith("custom:"):
        return purpose_or_name[len("custom:"):].strip()
    # Direct purpose slot (welcome_template, missed_call_template, …)
    if purpose_or_name in {k for k, _ in WA_TEMPLATE_PURPOSES}:
        return (await _get_wa_setting(purpose_or_name) or "").strip()
    # Legacy purpose/key name — map to new slot then fetch
    mapped = _WA_LEGACY_KEY_MAP.get(purpose_or_name)
    if mapped:
        val = (await _get_wa_setting(mapped) or "").strip()
        if val:
            return val
        # If new slot is empty, try also reading the legacy key directly as fallback
        legacy_val = (await _get_wa_setting(purpose_or_name) or "").strip()
        return legacy_val
    # Literal name pass-through (admin typed actual template name in old system)
    return purpose_or_name.strip()


async def get_wa_health() -> dict:
    cfg = await _wa_config()
    try:
        from db import get_setting
        display_timezone = await get_setting("WHATSAPP_DISPLAY_TIMEZONE", await get_setting("APP_TIMEZONE", "Asia/Singapore")) or "Asia/Singapore"
    except Exception:
        display_timezone = "Asia/Singapore"
    enabled = _is_truthy(cfg.get("WHATSAPP_ENABLED"))
    provider = (cfg.get("WHATSAPP_PROVIDER") or "meta").strip().lower()
    templates = [
        key for key, _ in WA_TEMPLATE_PURPOSES
        if cfg.get(key, "").strip()
    ]
    def configured(key: str) -> bool:
        return bool(str(cfg.get(key, "") or "").strip())

    def checks(items: list[tuple[str, str, bool]]) -> list[dict]:
        return [
            {"key": key, "label": label, "configured": ok}
            for key, label, ok in items
        ]

    missing = []
    if provider == "vobiz":
        auth_id = bool(cfg.get("VOBIZ_AUTH_ID", "").strip())
        auth_token = bool(cfg.get("VOBIZ_AUTH_TOKEN", "").strip())
        channel_id = bool(cfg.get("VOBIZ_CHANNEL_ID", "").strip())
        webhook_secret = bool(cfg.get("VOBIZ_WEBHOOK_SECRET", "").strip())
        credential_checks = checks([
            ("VOBIZ_AUTH_ID", "Vobiz Auth ID", auth_id),
            ("VOBIZ_AUTH_TOKEN", "Vobiz Auth Token", auth_token),
            ("VOBIZ_CHANNEL_ID", "Vobiz Channel ID", channel_id),
            ("VOBIZ_WEBHOOK_SECRET", "Vobiz Webhook Secret", webhook_secret),
        ])
        if not auth_id:
            missing.append("Vobiz Auth ID")
        if not auth_token:
            missing.append("Vobiz Auth Token")
        if not channel_id:
            missing.append("Vobiz Channel ID")
        if not webhook_secret:
            missing.append("Vobiz Webhook Secret")
        ok = enabled and auth_id and auth_token and channel_id and webhook_secret
        return {
            "enabled": enabled,
            "provider": "vobiz",
            "whatsapp_display_timezone": display_timezone,
            "vobiz_auth_id_configured": auth_id,
            "vobiz_auth_token_configured": auth_token,
            "vobiz_channel_id_configured": channel_id,
            "vobiz_webhook_secret_configured": webhook_secret,
            # Keep these keys for back-compat with existing UI badges
            "phone_number_id_configured": channel_id,
            "access_token_configured": auth_token,
            "credential_checks": credential_checks,
            "staff_appointment_notification_template_configured": bool(cfg.get("staff_appointment_notification_template", "").strip()),
            "staff_appointment_reminder_template_configured": bool(cfg.get("staff_appointment_reminder_template", "").strip()),
            "templates_configured": len(templates),
            "template_purposes": [
                {"key": k, "label": lbl, "configured": bool(cfg.get(k, "").strip())}
                for k, lbl in WA_TEMPLATE_PURPOSES
            ],
            "missing": missing,
            "status": "ok" if ok else "missing_config",
        }
    # Meta provider (default)
    meta_app_id = configured("META_APP_ID")
    meta_app_secret = configured("META_APP_SECRET")
    token = configured("WHATSAPP_ACCESS_TOKEN")
    business_account_id = configured("WHATSAPP_BUSINESS_ACCOUNT_ID")
    phone_id = configured("WHATSAPP_PHONE_NUMBER_ID")
    verify_token = configured("WHATSAPP_VERIFY_TOKEN")
    credential_checks = checks([
        ("META_APP_ID", "Meta App ID", meta_app_id),
        ("META_APP_SECRET", "Meta App Secret", meta_app_secret),
        ("WHATSAPP_ACCESS_TOKEN", "Permanent Access Token", token),
        ("WHATSAPP_BUSINESS_ACCOUNT_ID", "WhatsApp Business Account ID", business_account_id),
        ("WHATSAPP_PHONE_NUMBER_ID", "Phone Number ID", phone_id),
        ("WHATSAPP_VERIFY_TOKEN", "Verify Token", verify_token),
    ])
    missing = [item["label"] for item in credential_checks if not item["configured"]]
    status = "ok" if (enabled and not missing) else "missing_config"
    return {
        "enabled": enabled,
        "provider": "meta",
        "whatsapp_display_timezone": display_timezone,
        "meta_app_id_configured": meta_app_id,
        "meta_app_secret_configured": meta_app_secret,
        "whatsapp_business_account_id_configured": business_account_id,
        "phone_number_id_configured": phone_id,
        "access_token_configured": token,
        "verify_token_configured": verify_token,
        "credential_checks": credential_checks,
        "staff_appointment_notification_template_configured": bool(cfg.get("staff_appointment_notification_template", "").strip()),
        "staff_appointment_reminder_template_configured": bool(cfg.get("staff_appointment_reminder_template", "").strip()),
        "templates_configured": len(templates),
        "template_purposes": [
            {"key": k, "label": lbl, "configured": bool(cfg.get(k, "").strip())}
            for k, lbl in WA_TEMPLATE_PURPOSES
        ],
        "missing": missing,
        "status": status,
    }


async def _get_provider() -> str:
    cfg = await _wa_config()
    return (cfg.get("WHATSAPP_PROVIDER") or "meta").strip().lower()


def build_meta_template_payload(
    phone: str,
    template_name: str,
    language: str = "en",
    parameters: Optional[list] = None,
    template_purpose: str = "",
) -> dict:
    """Build a validated Meta template payload without performing network I/O."""
    from db import normalize_phone

    normalized = normalize_phone(phone)
    name = str(template_name or "").strip()
    if not name:
        raise ValueError("template_name is required")
    payload: dict = {
        "messaging_product": "whatsapp",
        "to": normalized[1:],
        "type": "template",
        "template": {
            "name": name,
            "language": {"code": str(language or "en").strip() or "en"},
        },
    }
    if parameters:
        payload["template"]["components"] = [{
            "type": "body",
            "parameters": [{"type": "text", "text": str(value)} for value in parameters],
        }]
    return payload


# ── Core send functions ────────────────────────────────────────────────────

async def send_whatsapp_template(
    phone: str,
    template_name: str,
    language: str = "en",
    parameters: Optional[list] = None,
    event_type: str = "",
    source_type: str = "",
    source_id: str = "",
    template_purpose: str = "",
    template_context: Optional[dict] = None,
    bypass_failure_cooldown: bool = False,
) -> dict:
    """Send an approved WhatsApp template message via Meta Cloud API.

    Returns: {success, provider_message_id, error, reason}
    Also writes to whatsapp_logs table.
    """
    async def log_and_record(status: str, provider_message_id: Optional[str], error_message: Optional[str]) -> None:
        await _log_wa(phone, event_type, template_name, language, parameters, status, provider_message_id, error_message, source_type, source_id)
        if phone:
            await record_outbound_template_message(
                phone, template_name, language, parameters, status,
                provider_message_id, error_message, source_type, source_id,
            )

    if not template_name:
        result = {"success": False, "provider_message_id": None, "error": "template_name is required", "reason": "template_missing"}
        await log_and_record("failed", None, "template_name is required")
        return result

    purpose = await _template_purpose_for_send(template_purpose, template_name)
    if purpose:
        context = _template_context_from_legacy_params(purpose, parameters)
        context.update(template_context or {})
        built = await build_template_params(purpose, context)
        template_name = built["template_name"] or template_name
        language = built["language"] or language
        parameters = built["params"]
        expected_count = await get_template_expected_param_count(purpose)
        actual_count = len(parameters)
        await _db().log_error(
            "whatsapp_template",
            "whatsapp_template_send_prepared",
            (
                f"phone={_mask_phone(phone)}; event={event_type}; source={source_type}; "
                f"template_purpose={purpose}; template_name={template_name}; "
                f"params_count={actual_count}; expected_count={expected_count}"
            ),
            "info",
        )
        if not bypass_failure_cooldown:
            failure_cooldown = await _template_failure_cooldown_check(phone, event_type, template_name)
            if failure_cooldown.get("active"):
                await _db().log_error(
                    "whatsapp_template",
                    "whatsapp_template_failure_cooldown_active",
                    f"phone={_mask_phone(phone)}; event={event_type}; source={source_type}; template_name={template_name}; cooldown_until={failure_cooldown.get('cooldown_until')}",
                    "warning",
                )
                return {
                    "success": False,
                    "provider_message_id": None,
                    "error": "whatsapp_template_failure_cooldown_active",
                    "reason": "failure_cooldown_active",
                    "cooldown_until": failure_cooldown.get("cooldown_until"),
                }
        if expected_count != actual_count:
            err = f"template_param_mismatch expected={expected_count} actual={actual_count}"
            await log_and_record("failed", None, err)
            await _db().log_error(
                "whatsapp_template",
                "whatsapp_template_param_mismatch",
                f"phone={_mask_phone(phone)}; event={event_type}; source={source_type}; template_purpose={purpose}; template_name={template_name}; {err}",
                "warning",
            )
            return {"success": False, "provider_message_id": None, "error": err, "reason": "template_param_mismatch"}

    cooldown_minutes = _template_cooldown_minutes(event_type, template_name, template_purpose)
    if cooldown_minutes and not _template_cooldown_bypassed(source_type, event_type) and not bypass_failure_cooldown:
        dup = await _template_duplicate_check(phone, event_type, template_name, cooldown_minutes, source_id=source_id)
        if dup.get("duplicate"):
            reason = "duplicate_suppressed_cooldown"
            await _log_wa(
                phone, event_type, template_name, language, parameters,
                "skipped", None, reason, source_type, source_id,
                idempotency_key=dup.get("idempotency_key", ""),
                cooldown_until=dup.get("cooldown_until", ""),
            )
            return {
                "success": False,
                "provider_message_id": None,
                "error": reason,
                "reason": reason,
                "cooldown_until": dup.get("cooldown_until", ""),
            }

    if not await _is_wa_enabled():
        result = {"success": False, "provider_message_id": None, "error": "WhatsApp is disabled", "reason": "whatsapp_disabled"}
        await log_and_record("skipped", None, "WhatsApp disabled")
        return result

    cfg = await _wa_config()
    provider = (cfg.get("WHATSAPP_PROVIDER") or "meta").strip().lower()

    # Vobiz template send is provider-specific. Until Vobiz template payload is
    # confirmed in their docs, we surface a clear, non-crashing error so the
    # caller (automation rules / UI) can fall back gracefully.
    if provider == "vobiz":
        err = "Vobiz template send not configured — please configure template payload format"
        await log_and_record("failed", None, err)
        return {"success": False, "provider_message_id": None, "error": err, "reason": "vobiz_template_format_not_configured"}

    token = cfg.get("WHATSAPP_ACCESS_TOKEN", "").strip()
    phone_number_id = cfg.get("WHATSAPP_PHONE_NUMBER_ID", "").strip()
    graph_version = cfg.get("WHATSAPP_GRAPH_VERSION", "v20.0").strip() or "v20.0"

    if not token or not phone_number_id:
        missing = []
        if not token:
            missing.append("WHATSAPP_ACCESS_TOKEN")
        if not phone_number_id:
            missing.append("WHATSAPP_PHONE_NUMBER_ID")
        err = f"Missing config: {', '.join(missing)}"
        result = {"success": False, "provider_message_id": None, "error": err, "reason": "whatsapp_not_configured"}
        await log_and_record("failed", None, err)
        return result

    try:
        payload = build_meta_template_payload(phone, template_name, language, parameters, purpose)
    except ValueError as exc:
        error = str(exc) or "Invalid template payload"
        reason = "invalid_phone" if "phone" in error.lower() else "invalid_template_payload"
        result = {"success": False, "provider_message_id": None, "error": error, "reason": reason}
        await log_and_record("failed", None, error)
        return result

    url = f"https://graph.facebook.com/{graph_version}/{phone_number_id}/messages"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=payload, headers=headers) as resp:
                resp_json = {}
                try:
                    resp_json = await resp.json()
                except Exception:
                    resp_json = {"raw": await resp.text()}

                if resp.status in (200, 201):
                    msg_id = None
                    messages = resp_json.get("messages") or []
                    if messages and isinstance(messages, list):
                        msg_id = messages[0].get("id")
                    await log_and_record("sent", msg_id, None)
                    try:
                        from db import crm_stage_updates, update_lead_journey
                        updates = {"last_whatsapp_sent_at": datetime.now().isoformat()}
                        if _template_purpose_key(template_purpose or template_name) == "welcome_template":
                            updates.update(crm_stage_updates("contacted"))
                        await update_lead_journey(phone, updates)
                    except Exception:
                        pass
                    return {"success": True, "provider_message_id": msg_id, "error": None, "reason": None}
                else:
                    error_data = resp_json.get("error") or resp_json
                    err_msg = str(error_data.get("message", "") if isinstance(error_data, dict) else error_data)[:500]
                    if "132000" in err_msg or "Number of parameters" in err_msg:
                        purpose = _template_purpose_key(template_purpose)
                        expected = await get_template_expected_param_count(purpose)
                        sent = len(parameters or [])
                        detail = f" sent_params={sent}"
                        if expected is not None:
                            detail += f" expected_params={expected}"
                        err_msg = (err_msg + detail)[:500]
                    reason, friendly_error = _meta_error_reason(err_msg)
                    stored_error = f"{reason}: {friendly_error}" if reason == "meta_ecosystem_engagement_restriction" else friendly_error
                    await log_and_record("failed", None, stored_error)
                    await _db().log_error("whatsapp_template", "whatsapp_template_send_failed", f"phone={_mask_phone(phone)}; http_status={resp.status}; reason={reason}; error={friendly_error[:300]}", "warning")
                    return {"success": False, "provider_message_id": None, "error": friendly_error, "reason": reason}
    except Exception as exc:
        err_msg = str(exc)[:500]
        logger.error("WhatsApp send error for %s: %s", _mask_phone(phone), exc)
        await log_and_record("failed", None, err_msg)
        return {"success": False, "provider_message_id": None, "error": err_msg, "reason": "send_error"}


async def send_whatsapp_text_if_allowed(phone: str, message: str) -> dict:
    """Send free-form text — only safe after customer has messaged first (Phase 8).
    For now logs intent but does not send to comply with Meta policy.
    """
    await _log_wa(phone, "text_message", "", "en", [message[:100]], "skipped", None, "Free-form text not allowed until Phase 8 inbox", "manual", "")
    return {"success": False, "provider_message_id": None, "error": "Free-form text requires incoming message first (Phase 8)", "reason": "policy_restriction"}


# ── WhatsApp Logs ──────────────────────────────────────────────────────────

async def _log_wa(
    phone: str,
    event_type: str,
    template_name: str,
    language: str,
    parameters,
    status: str,
    provider_message_id: Optional[str],
    error_message: Optional[str],
    source_type: str = "",
    source_id: str = "",
    idempotency_key: str = "",
    cooldown_until: str = "",
) -> None:
    try:
        row = {
            "id": str(uuid.uuid4()),
            "phone_number": phone or "",
            "event_type": event_type or "",
            "template_name": template_name or "",
            "language": language or "en",
            "parameters": parameters or [],
            "status": status or "unknown",
            "provider_message_id": provider_message_id or "",
            "error_message": (error_message or "")[:500],
            "source_type": source_type or "",
            "source_id": source_id or "",
            "idempotency_key": idempotency_key or "",
            "cooldown_until": cooldown_until or "",
            "created_at": datetime.now().isoformat(),
        }
        await _db().get_repository().create_whatsapp_log(row)
    except Exception as exc:
        logger.debug("WA log insert failed: %s", exc)


async def get_whatsapp_logs(phone: Optional[str] = None, limit: int = 50) -> list:
    try:
        return await _db().get_repository().list_whatsapp_logs(phone=phone or "", limit=limit)
    except Exception as exc:
        logger.debug("WA logs fetch failed: %s", exc)
        return []


async def get_whatsapp_message_activity() -> dict:
    """Return latest inbound customer and outbound AI message timestamps for diagnostics."""
    out = {"last_inbound_whatsapp_message_at": None, "last_outbound_ai_message_at": None}
    try:
        return await _db().get_repository().get_whatsapp_message_activity()
    except Exception as exc:
        logger.debug("WA message activity fetch failed: %s", exc)
    return out


def _meta_error_reason(error_message: str) -> tuple[str, str]:
    raw = str(error_message or "")
    lower = raw.lower()
    if "healthy ecosystem engagement" in lower or "maintain healthy ecosystem" in lower:
        return (
            "meta_ecosystem_engagement_restriction",
            "Meta restricted this template delivery for this recipient. Try after customer replies or use another approved template.",
        )
    return "provider_error", raw[:500]


def _mask_phone(phone: str) -> str:
    digits = "".join(ch for ch in str(phone or "") if ch.isdigit())
    return f"***{digits[-4:]}" if len(digits) > 4 else digits


async def _template_failure_cooldown_check(phone: str, event_type: str, template_name: str) -> dict:
    if not (phone and template_name):
        return {"active": False}
    cutoff = (datetime.now() - timedelta(hours=6)).isoformat()
    try:
        rows = await _db().get_repository().list_whatsapp_logs(
            phone=phone,
            event_type=event_type or "",
            template_name=template_name,
            status="failed",
            since_utc=cutoff,
            limit=10,
        )
        for row in rows:
            error = str(row.get("error_message") or "")
            lower = error.lower()
            if "template_param_mismatch" not in lower and "132000" not in lower and not ("#100" in lower and "invalid parameter" in lower):
                continue
            try:
                base = datetime.fromisoformat(str(row.get("created_at") or "").replace("Z", "+00:00")).replace(tzinfo=None)
            except Exception:
                base = datetime.now()
            return {"active": True, "cooldown_until": (base + timedelta(hours=6)).isoformat()}
    except Exception as exc:
        logger.warning("whatsapp_template_failure_cooldown_check failed: %s", exc)
    return {"active": False}


def _template_cooldown_minutes(event_type: str, template_name: str, template_purpose: str = "") -> int:
    event = (event_type or "").strip().lower()
    purpose = _template_purpose_key(template_purpose or template_name)
    name = (template_name or "").strip().lower()
    if event in ("appointment_reminder", "reminder_scheduled"):
        return 0
    if event in ("appointment_confirmed", "showroom_visit_confirmed"):
        return WA_TEMPLATE_COOLDOWNS["appointment_confirmation_template"]
    if purpose in WA_TEMPLATE_COOLDOWNS:
        return WA_TEMPLATE_COOLDOWNS[purpose]
    if name in WA_TEMPLATE_COOLDOWNS:
        return WA_TEMPLATE_COOLDOWNS[name]
    if "re_enquiry" in event or "re-enquiry" in event or "re_enquiry" in name:
        return WA_TEMPLATE_COOLDOWNS["re_enquiry_followup_template"]
    if "no_response" in name or "followup" in event:
        return WA_TEMPLATE_COOLDOWNS["no_response_followup_template"]
    if "missed" in event or "missed" in name:
        return WA_TEMPLATE_COOLDOWNS["missed_call_template"]
    return 0


def _template_cooldown_bypassed(source_type: str, event_type: str) -> bool:
    source = (source_type or "").strip().lower()
    event = (event_type or "").strip().lower()
    return source in {"manual", "dashboard", "inbox"} or event in {"manual_send", "manual_template"}


async def _template_duplicate_check(phone: str, event_type: str, template_name: str, cooldown_minutes: int, source_id: str = "") -> dict:
    if not (phone and template_name and cooldown_minutes > 0):
        return {"duplicate": False}
    cutoff = (datetime.now() - timedelta(minutes=cooldown_minutes)).isoformat()
    idempotency_key = f"{phone}:{event_type}:{template_name}"
    await _db().log_error(
        "whatsapp_automation",
        "whatsapp_template_duplicate_check",
        f"phone={phone} event={event_type} template={template_name} cooldown_minutes={cooldown_minutes}",
        "info",
    )
    try:
        rows = await _db().get_repository().list_whatsapp_logs(
            phone=phone,
            event_type=event_type or "",
            template_name=template_name or "",
            since_utc=cutoff,
            limit=5,
        )
        if (event_type or "").strip().lower() in {"appointment_confirmed", "showroom_visit_confirmed"} and source_id:
            rows = [r for r in rows if (r.get("source_id") or "") == source_id]
        for row in rows:
            if (row.get("status") or "").lower() in {"sent", "delivered", "read"}:
                try:
                    base = datetime.fromisoformat((row.get("created_at") or "").replace("Z", "+00:00")).replace(tzinfo=None)
                except Exception:
                    base = datetime.now()
                cooldown_until = (base + timedelta(minutes=cooldown_minutes)).isoformat()
                await _db().log_error(
                    "whatsapp_automation",
                    "whatsapp_template_duplicate_suppressed",
                    f"phone={phone} event={event_type} template={template_name} cooldown_until={cooldown_until}",
                    "warning",
                )
                await _db().log_error(
                    "whatsapp_automation",
                    "automation_cooldown_active",
                    f"idempotency_key={idempotency_key} cooldown_until={cooldown_until}",
                    "warning",
                )
                return {"duplicate": True, "cooldown_until": cooldown_until, "idempotency_key": idempotency_key}
    except Exception as exc:
        logger.warning("whatsapp_template_duplicate_check failed: %s", exc)
    return {"duplicate": False, "idempotency_key": idempotency_key}


# ── Automation Rules ───────────────────────────────────────────────────────

def _extract_whatsapp_status_failure(raw: dict) -> tuple[str, str]:
    errors = raw.get("errors") if isinstance(raw, dict) else None
    if isinstance(errors, list) and errors:
        err = errors[0] if isinstance(errors[0], dict) else {}
        code = str(err.get("code") or err.get("error_code") or "")
        message = (
            err.get("message")
            or err.get("title")
            or (err.get("error_data") or {}).get("details")
            or str(err)
        )
        reason, friendly = _meta_error_reason(str(message or ""))
        return code or reason, friendly[:500]
    return "", ""


async def update_whatsapp_delivery_status(parsed: dict) -> dict:
    """Update WhatsApp message/log delivery status from provider receipts."""
    msg_id = parsed.get("message_id") or ""
    status_val = (parsed.get("status_value") or "").strip().lower()
    raw = parsed.get("raw") if isinstance(parsed.get("raw"), dict) else {}
    phone = parsed.get("phone") or raw.get("recipient_id") or ""
    now = datetime.now().isoformat()
    ts = raw.get("timestamp") or parsed.get("timestamp") or ""
    status_time = datetime.fromtimestamp(int(ts)).isoformat() if str(ts).isdigit() else now
    failure_code, failure_reason = _extract_whatsapp_status_failure(raw)
    await _db().log_error("whatsapp_status", "whatsapp_status_webhook_received", f"id={msg_id} status={status_val} phone={phone}", "info")
    if not msg_id or status_val not in {"sent", "delivered", "read", "failed"}:
        await _db().log_error("whatsapp_status", "whatsapp_status_unmatched", f"invalid id/status id={msg_id} status={status_val}", "warning")
        return {"matched": False, "status": status_val, "reason": "invalid_status_webhook"}

    if status_val == "failed":
        await _db().log_error("whatsapp_status", "whatsapp_status_failed_reason", f"id={msg_id} code={failure_code} reason={failure_reason}", "error")

    try:
        persistence = await _db().get_repository().record_whatsapp_delivery_status(
            msg_id,
            status_val,
            status_time,
            failure_reason=failure_reason,
            error_code=failure_code,
        )
        matched = bool(persistence.get("matched"))
        if persistence.get("message_matched"):
            await _db().log_error("whatsapp_status", "whatsapp_status_matched", f"message_id={msg_id}", "info")
        elif persistence.get("log_matched"):
            await _db().log_error("whatsapp_status", "whatsapp_status_matched", f"log_id={msg_id}", "info")
    except Exception as exc:
        logger.error("update_whatsapp_delivery_status failed: %s", exc)
        await _db().log_error("whatsapp_status", "whatsapp_status_unmatched", f"id={msg_id} error={str(exc)[:500]}", "warning")
        return {"matched": False, "status": status_val, "reason": "update_failed"}

    event = "whatsapp_status_updated" if matched else "whatsapp_status_unmatched"
    level = "info" if matched else "warning"
    await _db().log_error("whatsapp_status", event, f"id={msg_id} status={status_val}", level)
    return {"matched": matched, "status": status_val, "provider_message_id": msg_id}


async def get_automation_rules_with_source() -> tuple[list, str]:
    raw = ""
    source = "default"
    try:
        value = await _db().get_repository().get_setting(_AUTOMATION_RULES_KEY)
        if value:
            raw = value
            source = "db"
    except Exception as exc:
        logger.debug("Automation rules DB read failed: %s", exc)
    if not raw:
        raw = os.getenv(_AUTOMATION_RULES_KEY, "")
        if raw:
            source = "env"
    if raw:
        try:
            import json as _json
            rules = _json.loads(raw)
            if isinstance(rules, list):
                return rules, source
        except Exception as exc:
            logger.warning("Automation rules parse failed from %s: %s", source, exc)
    return _default_automation_rules(), "default"


async def get_automation_rules() -> list:
    rules, _source = await get_automation_rules_with_source()
    return rules


async def save_automation_rules(rules: list) -> None:
    import json as _json
    await _db().set_setting(_AUTOMATION_RULES_KEY, _json.dumps(rules))


def _is_enabled_rule(rule: dict) -> bool:
    val = rule.get("enabled")
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.strip().lower() in ("1", "true", "yes", "on", "enabled", "y", "t")
    return bool(val)


def _find_disabled_automation_rule(rules: list, event_type: str, source: Optional[str] = None) -> Optional[dict]:
    requested_event_exists = any(r.get("event_type") == event_type for r in rules)

    if source:
        for r in rules:
            if r.get("event_type") == event_type and r.get("source") == source and not _is_enabled_rule(r):
                return r
    for r in rules:
        if r.get("event_type") == event_type and r.get("source") == "all" and not _is_enabled_rule(r):
            return r

    if requested_event_exists:
        return None

    if event_type != "new_lead":
        for r in rules:
            if r.get("event_type") == "new_lead" and r.get("source") == "all" and not _is_enabled_rule(r):
                return r
    for r in rules:
        if r.get("event_type") == "all" and r.get("source") == "all" and not _is_enabled_rule(r):
            return r
    return None


def find_automation_rule(rules: list, event_type: str, source: Optional[str] = None) -> Optional[dict]:
    """Priority: exact event+source > event+all > safe fallback > None."""
    requested_event_exists = any(r.get("event_type") == event_type for r in rules)

    # 1. Exact match
    if source:
        for r in rules:
            if r.get("event_type") == event_type and r.get("source") == source and _is_enabled_rule(r):
                return r
    # 2. event_type + source=all
    for r in rules:
        if r.get("event_type") == event_type and r.get("source") == "all" and _is_enabled_rule(r):
            return r

    # If the requested event has configured rules, do not silently execute a
    # different event's automation. Let callers surface disabled/no-match state.
    if requested_event_exists:
        return None

    # 3. new_lead + source=all as generic fallback for unknown event types
    if event_type != "new_lead":
        for r in rules:
            if r.get("event_type") == "new_lead" and r.get("source") == "all" and _is_enabled_rule(r):
                return r
    # 4. global all+all fallback for API-created/custom rules
    for r in rules:
        if r.get("event_type") == "all" and r.get("source") == "all" and _is_enabled_rule(r):
            return r
    return None


def source_to_event_type(source: str) -> str:
    """Map a lead source string to an automation event type."""
    s = (source or "").strip().lower()
    norm = s.replace("-", "_").replace(" ", "_")
    compact = norm.replace("_", "")
    if norm in {"facebook", "fb", "meta", "facebook_ads", "facebook_ad", "facebook_lead", "fb_lead", "fb_ads", "meta_ads", "meta_lead"} or compact in {"facebookads", "facebooklead", "fblead", "fbads", "metalead", "metaads"}:
        return "facebook_lead"
    if norm in {"instagram", "ig", "instagram_ads", "instagram_ad", "instagram_lead", "ig_lead", "ig_ads"} or compact in {"instagramads", "instagramlead", "iglead", "igads"}:
        return "instagram_lead"
    if norm in {"whatsapp", "wa", "whatsapp_lead"}:
        return "whatsapp_lead" if "whatsapp_lead" in AUTOMATION_EVENT_TYPES else "uploaded_lead"
    mapping = {
        "website": "website_lead",
        "web": "website_lead",
        "google_sheet": "google_sheet_lead",
        "google_sheets": "google_sheet_lead",
        "googlesheet": "google_sheet_lead",
        "googlesheets": "google_sheet_lead",
        "n8n": "google_sheet_lead",
        "n8n_google_sheet": "google_sheet_lead",
        "n8n_google_sheets": "google_sheet_lead",
        "manual": "uploaded_lead",
        "uploaded": "uploaded_lead",
        "csv": "uploaded_lead",
        "xlsx": "uploaded_lead",
        "file_upload": "uploaded_lead",
        "csv_upload": "uploaded_lead",
        "api": "api_lead",
        "re_enquiry": "re_enquiry",
        "re-enquiry": "re_enquiry",
    }
    return mapping.get(norm, mapping.get(s, "uploaded_lead"))


# ── Automation Action Queue ────────────────────────────────────────────────

async def insert_automation_action(
    phone: str,
    event_type: str,
    source: str,
    action_type: str,
    scheduled_at: datetime,
    payload: Optional[dict] = None,
    status: str = "pending",
    result: Optional[dict] = None,
    error: str = "",
) -> str:
    action_id = str(uuid.uuid4())
    try:
        row = {
            "id": action_id,
            "phone_number": phone,
            "event_type": event_type,
            "source": source or "",
            "action_type": action_type,
            "scheduled_at": scheduled_at.isoformat(),
            "status": status or "pending",
            "action_status": status or "pending",
            "idempotency_key": (payload or {}).get("idempotency_key", ""),
            "cooldown_until": (payload or {}).get("cooldown_until", ""),
            "payload": payload or {},
            "result": result or {},
            "error_message": (error or "")[:500],
            "created_at": datetime.now().isoformat(),
            "completed_at": datetime.now().isoformat() if status in ("completed", "failed", "cancelled", "skipped") else "",
        }
        await _db().get_repository().create_automation_action(row)
    except Exception as exc:
        logger.warning("insert_automation_action failed: %s", exc)
        # Queue persistence is part of the automation contract.  Returning the
        # generated UUID here made callers report work as queued even though no
        # worker could ever claim it.  Fail closed with a stable, non-secret
        # error so the caller can report/retry the persistence failure.
        raise RuntimeError("automation_action_persistence_failed") from exc
    return action_id


async def get_automation_actions_raw(
    status: Optional[str] = None,
    phone: Optional[str] = None,
    limit: int = 100,
) -> tuple[list, bool]:
    try:
        rows = await _db().get_repository().list_automation_actions(status=status or "", phone=phone or "", limit=limit)
        return rows, True
    except Exception as exc:
        logger.warning("get_automation_actions_raw failed: %s", exc)
        return [], False


async def get_automation_actions(
    status: Optional[str] = None,
    phone: Optional[str] = None,
    limit: int = 100,
) -> list:
    actions, _ok = await get_automation_actions_raw(status=status, phone=phone, limit=limit)
    return actions


async def update_automation_action_status(action_id: str, status: str, result: Optional[dict] = None, error: str = "", claim_token: str = "") -> bool:
    try:
        updates: dict = {"status": status, "action_status": status}
        if result is not None:
            updates["result"] = result
        if error:
            updates["error_message"] = error[:500]
        if status in ("completed", "failed", "cancelled", "skipped"):
            updates["completed_at"] = datetime.now().isoformat()
        if status in ("completed", "failed", "cancelled", "skipped"):
            updates.update({"claim_token": None, "lease_expires_at": None})
        return await _db().get_repository().update_automation_action(action_id, updates, claim_token=claim_token)
    except Exception as exc:
        logger.debug("update_automation_action_status failed: %s", exc)
        return False


# ── Execute automation rule for a contact ─────────────────────────────────

def _rule_key(rule: Optional[dict]) -> str:
    if not rule:
        return ""
    return f"{rule.get('event_type', '')}:{rule.get('source', 'all')}"


def _automation_result(
    event_type: str,
    source: str,
    rule: Optional[dict],
    action: str = "manual_only",
    automation_status: str = "",
    whatsapp_status: Optional[str] = None,
    call_status: Optional[str] = None,
    queue_action_id: Optional[str] = None,
    skip_reason: str = "",
    error: str = "",
) -> dict:
    return {
        "automation_status": automation_status,
        "automation_event_type": event_type,
        "matched_rule_key": _rule_key(rule),
        "matched_rule_enabled": _is_enabled_rule(rule) if rule else False,
        "selected_action": action,
        "action": action,
        "call_status": call_status,
        "whatsapp_status": whatsapp_status,
        "queue_action_id": queue_action_id,
        "scheduled_action_id": queue_action_id,
        "skip_reason": skip_reason,
        "error": error,
    }


async def _send_rule_whatsapp(
    phone: str,
    event_type: str,
    source_id: str,
    selected_template: str,
    template: str,
    language: str,
    params: list,
) -> dict:
    if not template:
        if selected_template in {k for k, _ in WA_TEMPLATE_PURPOSES}:
            err = f"WhatsApp template purpose slot '{selected_template}' is not configured"
        else:
            err = "WhatsApp template is not configured for this automation rule"
        await _log_wa(
            phone, event_type, selected_template, language, params,
            "failed", None, err, "automation", source_id,
        )
        return {"success": False, "provider_message_id": None, "error": err, "reason": "template_missing"}
    return await send_whatsapp_template(
        phone, template, language, params,
        event_type=event_type, source_type="automation", source_id=source_id,
        template_purpose=selected_template,
    )


def _template_purpose_key(purpose_or_name: str) -> str:
    if not purpose_or_name:
        return ""
    if purpose_or_name.startswith("custom:"):
        return ""
    if purpose_or_name in {k for k, _ in WA_TEMPLATE_PURPOSES}:
        return purpose_or_name
    return _WA_LEGACY_KEY_MAP.get(purpose_or_name, "")


async def _template_purpose_for_send(template_purpose: str, template_name: str) -> str:
    purpose = _template_purpose_key(template_purpose or template_name)
    if purpose:
        return purpose
    for candidate, _ in WA_TEMPLATE_PURPOSES:
        if template_name and template_name == await resolve_wa_template(candidate):
            return candidate
    return ""


async def execute_automation_rule(
    event_type: str,
    contact: dict,
    context: Optional[dict] = None,
) -> dict:
    """Main entry point. Loads rules, finds match, executes action.

    contact keys: phone, lead_name, business_name, service_type, source, ...
    context: extra info like call_type override, appointment details, etc.
    Returns: {action, whatsapp_status, call_status, automation_status, scheduled_action_id}
    """
    phone = (contact.get("phone") or contact.get("phone_number") or "").strip()
    source = (contact.get("source") or "").strip()
    if not phone:
        return _automation_result(event_type, source, None, "skip", "skipped", skip_reason="invalid_phone", error="Phone number is missing")

    rules = await get_automation_rules()
    rule = (context or {}).get("rule") or find_automation_rule(rules, event_type, source)
    if not rule:
        disabled_rule = _find_disabled_automation_rule(rules, event_type, source)
        if disabled_rule:
            action = disabled_rule.get("action", "manual_only")
            result = _automation_result(event_type, source, disabled_rule, action, "skipped", skip_reason="rule_disabled")
            action_id = await insert_automation_action(
                phone, event_type, source, action, datetime.now(),
                payload={**contact, "rule": disabled_rule},
                status="skipped", result=result, error="rule_disabled",
            )
            result["queue_action_id"] = action_id
            result["scheduled_action_id"] = action_id
            logger.info("Automation skipped: %s", result)
            return result
        result = _automation_result(event_type, source, None, "manual_only", "skipped", skip_reason="no_matching_rule")
        action_id = await insert_automation_action(
            phone, event_type, source, "manual_only", datetime.now(),
            payload={**contact}, status="skipped", result=result, error="no_matching_rule",
        )
        result["queue_action_id"] = action_id
        result["scheduled_action_id"] = action_id
        logger.info("Automation skipped: %s", result)
        return result

    action = rule.get("action", "manual_only")
    selected_template = rule.get("whatsapp_template", "") or ""
    if action in ("whatsapp_only", "whatsapp_and_call_now", "whatsapp_then_call_after_delay", "whatsapp_then_call_on_reply") and rule.get("event_type") == "new_lead" and not selected_template:
        selected_template = "welcome_template"
    template = await resolve_wa_template(selected_template)
    language = await _get_wa_setting("WHATSAPP_DEFAULT_LANGUAGE") or "en"
    lead_name = contact.get("lead_name") or "there"
    business_name = contact.get("business_name") or ""
    service_type = contact.get("service_type") or ""
    delay_minutes = int(rule.get("delay_minutes") or 0)
    call_type = (context or {}).get("call_type") or rule.get("call_type") or "welcome_call"

    params = _build_template_params(selected_template, contact, context)
    action_id = await insert_automation_action(
        phone, event_type, source, action, datetime.now(),
        payload={**contact, "rule": rule, "call_type": call_type, "selected_template": selected_template},
        status="running",
    )

    async def finish(status: str, result: dict, error: str = "") -> dict:
        result["queue_action_id"] = result.get("queue_action_id") or action_id
        result["scheduled_action_id"] = result.get("scheduled_action_id") or result["queue_action_id"]
        result["parent_queue_action_id"] = action_id
        await update_automation_action_status(action_id, status, result, error)
        logger.info("Automation result: %s", result)
        return result

    if action in ("manual_only", "manual_calling"):
        now_iso = datetime.now().isoformat()
        try:
            from db import update_lead_journey, update_crm_contact_followup
            sched_due = _next_allowed_time(delay_minutes, rule).strftime("%Y-%m-%dT%H:%M") if delay_minutes else now_iso[:16]
            await update_lead_journey(phone, {
                "journey_stage": "telecaller_queue",
                "preferred_channel": "manual_call",
                "next_followup_at": sched_due
            })
            await update_crm_contact_followup(phone, sched_due)
        except Exception as exc:
            logger.warning("Manual calling journey update failed for %s: %s", phone, exc)
        result = _automation_result(event_type, source, rule, action, "completed", call_status="queued_for_telecaller")
        return await finish("completed", result, "")

    elif action == "whatsapp_then_manual_call":
        wa_result = await _send_rule_whatsapp(phone, event_type, action_id, selected_template, template, language, params)
        sched_at = _next_allowed_time(delay_minutes or 30, rule)
        sched_due = sched_at.strftime("%Y-%m-%dT%H:%M")
        try:
            from db import update_lead_journey, update_crm_contact_followup
            await update_lead_journey(phone, {
                "journey_stage": "telecaller_queue",
                "preferred_channel": "manual_call",
                "next_followup_at": sched_due
            })
            await update_crm_contact_followup(phone, sched_due)
        except Exception:
            pass
        whatsapp_status = "sent" if wa_result["success"] else (wa_result.get("reason") or "failed")
        result = _automation_result(event_type, source, rule, action, "completed", whatsapp_status=whatsapp_status, call_status="queued_for_telecaller")
        result["next_allowed_at"] = sched_due
        return await finish("completed", result, wa_result.get("error") or "")

    elif action == "manual_call_then_whatsapp":
        now_iso = datetime.now().isoformat()
        try:
            from db import update_lead_journey, update_crm_contact_followup
            await update_lead_journey(phone, {
                "journey_stage": "telecaller_queue",
                "preferred_channel": "manual_call",
                "next_followup_at": now_iso[:16]
            })
            await update_crm_contact_followup(phone, now_iso[:16])
        except Exception:
            pass
        result = _automation_result(event_type, source, rule, action, "completed", call_status="queued_for_telecaller", whatsapp_status="pending_call_outcome")
        return await finish("completed", result, "")

    elif action == "whatsapp_only":
        wa_result = await _send_rule_whatsapp(phone, event_type, action_id, selected_template, template, language, params)
        whatsapp_status = "sent" if wa_result["success"] else (wa_result.get("reason") or "failed")
        skipped_duplicate = wa_result.get("reason") == "duplicate_suppressed_cooldown"
        result = _automation_result(event_type, source, rule, action, "executed" if wa_result["success"] else ("skipped" if skipped_duplicate else "failed"), whatsapp_status=whatsapp_status, skip_reason="duplicate_suppressed_cooldown" if skipped_duplicate else "", error=wa_result.get("error") or "")
        if wa_result.get("cooldown_until"):
            result["cooldown_until"] = wa_result.get("cooldown_until")
        return await finish("completed" if wa_result["success"] else ("skipped" if skipped_duplicate else "failed"), result, wa_result.get("error") or "")

    elif action == "call_only":
        call_result = await _schedule_or_start_call(phone, contact, call_type, event_type, source, delay_minutes=0, rule=rule, return_details=True)
        result = _automation_result(event_type, source, rule, action, "executed", call_status=call_result.get("call_status"), queue_action_id=call_result.get("action_id"))
        result["next_allowed_at"] = call_result.get("next_allowed_at")
        result["skip_reason"] = call_result.get("skip_reason", "")
        result["error"] = call_result.get("error", "")
        return await finish("completed", result, result.get("error", ""))

    elif action == "whatsapp_and_call_now":
        wa_result = await _send_rule_whatsapp(phone, event_type, action_id, selected_template, template, language, params)
        call_result = await _schedule_or_start_call(phone, contact, call_type, event_type, source, delay_minutes=0, rule=rule, return_details=True)
        whatsapp_status = "sent" if wa_result["success"] else (wa_result.get("reason") or "failed")
        skipped_duplicate = wa_result.get("reason") == "duplicate_suppressed_cooldown"
        result = _automation_result(event_type, source, rule, action, "executed" if wa_result["success"] else ("skipped" if skipped_duplicate else "failed"), whatsapp_status=whatsapp_status, call_status=call_result.get("call_status"), queue_action_id=call_result.get("action_id"), skip_reason="duplicate_suppressed_cooldown" if skipped_duplicate else "", error=wa_result.get("error") or call_result.get("error", ""))
        result["next_allowed_at"] = call_result.get("next_allowed_at")
        result["skip_reason"] = call_result.get("skip_reason") or result.get("skip_reason", "")
        return await finish("completed" if wa_result["success"] else ("skipped" if skipped_duplicate else "failed"), result, result.get("error", ""))

    elif action == "whatsapp_then_call_after_delay":
        wa_result = await _send_rule_whatsapp(phone, event_type, action_id, selected_template, template, language, params)
        sched_at = _next_allowed_time(delay_minutes, rule)
        scheduled_id = await insert_automation_action(
            phone, event_type, source, "call_only", sched_at,
            payload={**contact, "call_type": call_type, "rule": rule, "parent_action_id": action_id},
        )
        whatsapp_status = "sent" if wa_result["success"] else (wa_result.get("reason") or "failed")
        skipped_duplicate = wa_result.get("reason") == "duplicate_suppressed_cooldown"
        result = _automation_result(event_type, source, rule, action, "queued" if wa_result["success"] else ("skipped" if skipped_duplicate else "failed"), whatsapp_status=whatsapp_status, call_status="scheduled", queue_action_id=scheduled_id, skip_reason="duplicate_suppressed_cooldown" if skipped_duplicate else "", error=wa_result.get("error") or "")
        result["parent_queue_action_id"] = action_id
        result["next_allowed_at"] = sched_at.isoformat()
        await update_automation_action_status(action_id, "completed" if wa_result["success"] else ("skipped" if skipped_duplicate else "failed"), result, wa_result.get("error") or "")
        logger.info("Automation result: %s", result)
        return result

    elif action in ("call_then_whatsapp_on_failure", "call_then_whatsapp_always"):
        call_result = await _schedule_or_start_call(
            phone, contact, call_type, event_type, source, delay_minutes=0, rule=rule,
            fallback_action=action, return_details=True,
        )
        result = _automation_result(event_type, source, rule, action, "executed", whatsapp_status="pending_call_outcome", call_status=call_result.get("call_status"), queue_action_id=call_result.get("action_id"), error=call_result.get("error", ""))
        result["next_allowed_at"] = call_result.get("next_allowed_at")
        result["skip_reason"] = call_result.get("skip_reason", "")
        return await finish("completed", result, result.get("error", ""))

    elif action == "whatsapp_then_call_on_reply":
        wa_result = await _send_rule_whatsapp(phone, event_type, action_id, selected_template, template, language, params)
        waiting_id = await insert_automation_action(
            phone, event_type, source, "waiting_for_whatsapp_reply", datetime.now(),
            payload={**contact, "call_type": call_type, "rule": rule, "parent_action_id": action_id},
        )
        await update_automation_action_status(waiting_id, "waiting_schedule", {"waiting_for": "whatsapp_reply", "parent_action_id": action_id})
        whatsapp_status = "sent" if wa_result["success"] else (wa_result.get("reason") or "failed")
        skipped_duplicate = wa_result.get("reason") == "duplicate_suppressed_cooldown"
        result = _automation_result(event_type, source, rule, action, "waiting_reply" if wa_result["success"] else ("skipped" if skipped_duplicate else "failed"), whatsapp_status=whatsapp_status, call_status="waiting_reply", queue_action_id=waiting_id, skip_reason="duplicate_suppressed_cooldown" if skipped_duplicate else "", error=wa_result.get("error") or "")
        result["parent_queue_action_id"] = action_id
        await update_automation_action_status(action_id, "completed" if wa_result["success"] else ("skipped" if skipped_duplicate else "failed"), result, wa_result.get("error") or "")
        logger.info("Automation result: %s", result)
        return result

    result = _automation_result(event_type, source, rule, action, "skipped", skip_reason="unknown_action")
    return await finish("skipped", result, "unknown_action")


def _first_value(*values, fallback: str = "") -> str:
    for value in values:
        if value is not None and str(value).strip():
            return str(value).strip()
    return fallback


async def get_template_expected_param_count(template_purpose: str) -> int:
    purpose = _template_purpose_key(template_purpose) or (template_purpose or "").strip()
    default = WA_TEMPLATE_PARAM_COUNTS.get(purpose, 0)
    raw = await _get_wa_setting(f"TEMPLATE_PARAM_COUNT_{purpose}")
    if not str(raw or "").strip():
        return default
    try:
        return max(int(str(raw).strip()), 0)
    except ValueError:
        return default


async def build_template_params(template_purpose: str, context: Optional[dict] = None) -> dict:
    context = context or {}
    purpose = _template_purpose_key(template_purpose) or (template_purpose or "").strip()
    template_name = await resolve_wa_template(purpose)
    language = await _get_wa_setting("WHATSAPP_DEFAULT_LANGUAGE") or "en"
    default_business = os.getenv("BUSINESS_NAME", "our team").strip() or "our team"
    name = _first_value(context.get("customer_name"), context.get("lead_name"), context.get("name"), fallback="Valued Customer")
    company = _first_value(context.get("business_name"), context.get("company_name"), context.get("company"), fallback=default_business)
    service = _first_value(context.get("service_type"), context.get("service"), context.get("requirement"), fallback="service")
    date = _first_value(context.get("appointment_date"), context.get("date"), fallback="2026-06-15")
    time = _first_value(context.get("appointment_time"), context.get("time"), fallback="10:00 AM")
    appointment_datetime = _first_value(context.get("appointment_datetime"), fallback=f"{date} {time}")
    customer_phone = _first_value(context.get("customer_phone"), context.get("phone_number"), context.get("phone"), fallback="")
    staff_name = _first_value(context.get("staff_name"), fallback=f"{default_business} Team")
    source = _first_value(context.get("source"), fallback="CRM")
    reason = _first_value(context.get("reason"), fallback="Customer requested assistance")
    variants = {
        "no_response_followup_template": [name, company, service],
        "reminder_template": [name, date, time, company or staff_name],
        "appointment_confirmation_template": [name, date, time, company],
        "staff_appointment_notification_template": [name, service, date, time],
        "staff_appointment_reminder_template": [staff_name, date, time],
        "re_enquiry_followup_template": [name, company, service],
        "callback_confirmation_template": [name, appointment_datetime, service, company],
        "welcome_template": [name, company, service],
        "missed_call_template": [name, company, service],
        "staff_handoff_notification_template": [name, service],
        "staff_demo_status_request": [name, customer_phone, appointment_datetime],
        "service_completion_template": [name, service, appointment_datetime],
    }
    params = variants.get(purpose, [])[:expected]
    return {
        "template_purpose": purpose,
        "template_name": template_name,
        "language": language,
        "params": params,
    }


def _template_context_from_legacy_params(template_purpose: str, parameters: Optional[list]) -> dict:
    values = list(parameters or [])
    context = {}
    purpose_fields = {
        "no_response_followup_template": ["customer_name", "business_name", "service_type"],
        "reminder_template": ["customer_name", "appointment_date", "appointment_time", "business_name"],
        "appointment_confirmation_template": ["customer_name", "business_name", "appointment_date", "appointment_time"],
        "staff_appointment_notification_template": ["customer_name", "service_type", "appointment_date", "appointment_time"],
        "staff_appointment_reminder_template": ["staff_name", "appointment_date", "appointment_time"],
        "re_enquiry_followup_template": ["customer_name", "business_name", "service_type"],
        "callback_confirmation_template": ["customer_name", "service_type", "appointment_date", "appointment_time"],
        "welcome_template": ["customer_name", "business_name", "service_type"],
        "missed_call_template": ["customer_name", "business_name", "service_type"],
        "staff_handoff_notification_template": ["customer_name", "service_type"],
        "service_completion_template": ["customer_name", "service_type", "appointment_datetime"],
    }
    for key, value in zip(purpose_fields.get(template_purpose, []), values):
        context[key] = value
    return context


def _build_template_params(purpose_or_name: str, contact: Optional[dict], context: Optional[dict] = None) -> list:
    contact = contact or {}
    context = context or {}
    purpose = _template_purpose_key(purpose_or_name)
    name = _first_value(
        context.get("name"), context.get("lead_name"), context.get("customer_name"),
        contact.get("name"), contact.get("lead_name"), contact.get("customer_name"),
        fallback="there",
    )
    company = _first_value(
        context.get("business_name"), context.get("company_name"), context.get("business"), context.get("company"), context.get("service_company"),
        contact.get("business_name"), contact.get("company_name"), contact.get("business"), contact.get("company"), contact.get("service_company"),
        contact.get("service_type"), context.get("service_type"), context.get("service"),
        fallback="your business",
    )
    service = _first_value(
        context.get("purpose"), context.get("service_type"), context.get("service"), context.get("requirement"),
        contact.get("service_type"), contact.get("service"), contact.get("requirement"),
        fallback=company,
    )
    date = _first_value(
        context.get("date"), context.get("appointment_date"), context.get("callback_date"),
        contact.get("date"), contact.get("appointment_date"), contact.get("callback_date"),
        fallback="the scheduled date",
    )
    time = _first_value(
        context.get("time"), context.get("appointment_time"), context.get("callback_time"),
        contact.get("time"), contact.get("appointment_time"), contact.get("callback_time"),
        fallback="the scheduled time",
    )

    if purpose in ("welcome_template", "missed_call_template", "no_response_followup_template", "re_enquiry_followup_template"):
        return [name, company]
    if purpose == "callback_confirmation_template":
        return [name, service, date, time]
    if purpose == "appointment_confirmation_template":
        return [name, company, date, time]
    if purpose == "staff_appointment_notification_template":
        return [name, service or company, date, time]
    if purpose == "staff_appointment_reminder_template":
        staff_name = _first_value(
            context.get("staff_name"), contact.get("staff_name"),
            fallback="Team member",
        )
        return [staff_name, date, time]
    if purpose == "reminder_template":
        return [name, company, time]
    if purpose == "staff_demo_status_request":
        customer_phone = _first_value(
            context.get("customer_phone"), context.get("phone"), context.get("phone_number"),
            contact.get("customer_phone"), contact.get("phone"), contact.get("phone_number"),
            fallback="",
        )
        appointment_datetime = _first_value(
            context.get("appointment_datetime"),
            f"{date} {time}".strip(),
            fallback="the scheduled time",
        )
        return [name, customer_phone, appointment_datetime]

    params = [name]
    if service:
        params.append(service)
    if company and company != service:
        params.append(company)
    return params


def _next_allowed_time(delay_minutes: int, rule: Optional[dict] = None) -> datetime:
    """Calculate scheduled_at for a delayed action, snapping to outbound window if needed."""
    target = datetime.now() + timedelta(minutes=max(delay_minutes, 0))
    return target


async def _schedule_or_start_call(
    phone: str,
    contact: dict,
    call_type: str,
    event_type: str,
    source: str,
    delay_minutes: int = 0,
    rule: Optional[dict] = None,
    fallback_action: Optional[str] = None,
    return_details: bool = False,
) -> str:
    """Schedule or immediately start an outbound call.

    Returns: 'scheduled' | 'queued' | 'dispatched' | 'skipped_no_livekit'
    """
    contact = {**(contact or {}), "source": (contact or {}).get("source") or source}

    def done(call_status: str, action_id: Optional[str] = None, next_allowed_at=None, skip_reason: str = "", error: str = ""):
        if return_details:
            return {
                "call_status": call_status,
                "action_id": action_id,
                "next_allowed_at": next_allowed_at.isoformat() if hasattr(next_allowed_at, "isoformat") else next_allowed_at,
                "skip_reason": skip_reason,
                "error": error,
            }
        return call_status

    try:
        from server import _is_outbound_allowed, _dispatch_one, _outbound_window_error
        from livekit import api as lk_api_module
        import ssl, aiohttp, random
        from db import get_agent_profile, get_setting
    except Exception:
        # Safe fallback — log a scheduled action for the runner to pick up
        sched_at = _next_allowed_time(delay_minutes, rule)
        action_id = await insert_automation_action(
            phone, event_type, source, "call_only", sched_at,
            payload={**contact, "call_type": call_type, "rule": rule, "fallback_action": fallback_action},
        )
        return done("scheduled", action_id, sched_at, error="call_dispatch_import_failed")

    if delay_minutes > 0:
        sched_at = _next_allowed_time(delay_minutes, rule)
        action_id = await insert_automation_action(
            phone, event_type, source, "call_only", sched_at,
            payload={**contact, "call_type": call_type, "rule": rule, "fallback_action": fallback_action},
        )
        return done("scheduled", action_id, sched_at)

    # Immediate call
    if not await _is_outbound_allowed():
        win_err = await _outbound_window_error()
        sched_at = datetime.now() + timedelta(minutes=60)
        try:
            from datetime import datetime as _dt
            na = win_err.get("next_allowed_at")
            if na:
                sched_at = _dt.fromisoformat(na)
        except Exception:
            pass
        action_id = await insert_automation_action(
            phone, event_type, source, "call_only", sched_at,
            payload={**contact, "call_type": call_type, "rule": rule, "fallback_action": fallback_action},
        )
        await update_automation_action_status(action_id, "waiting_schedule")
        return done("waiting_schedule", action_id, sched_at, skip_reason="outside_outbound_window")

    # Try to dispatch via LiveKit
    try:
        from db import get_setting as _gs
        lk_url = os.getenv("LIVEKIT_URL") or await _gs("LIVEKIT_URL", "")
        lk_key = os.getenv("LIVEKIT_API_KEY") or await _gs("LIVEKIT_API_KEY", "")
        lk_secret = os.getenv("LIVEKIT_API_SECRET") or await _gs("LIVEKIT_API_SECRET", "")
        if not (lk_url and lk_key and lk_secret):
            raise RuntimeError("LiveKit not configured")

        contact_with_type = {**contact, "call_type": call_type}
        room_name = f"auto-{phone.replace('+', '')}-{random.randint(100, 999)}"
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE
        session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ssl_ctx))
        try:
            lk = lk_api_module.LiveKitAPI(url=lk_url, api_key=lk_key, api_secret=lk_secret, session=session)
            ok = await _dispatch_one(lk, lk_api_module, contact_with_type, room_name, None, None)
            await lk.aclose()
        finally:
            await session.close()

        if ok:
            # Schedule fallback WhatsApp action linked to this call's outcome
            fallback_id = None
            if fallback_action:
                fallback_id = await insert_automation_action(
                    phone, event_type, source,
                    "whatsapp_fallback_on_outcome",
                    datetime.now() + timedelta(days=30),
                    payload={**contact, "call_type": call_type, "rule": rule, "fallback_action": fallback_action, "room_name": room_name},
                )
            return done("dispatched", fallback_id)
        else:
            sched_at = _next_allowed_time(15, rule)
            action_id = await insert_automation_action(
                phone, event_type, source, "call_only", sched_at,
                payload={**contact, "call_type": call_type, "rule": rule, "fallback_action": fallback_action},
            )
            return done("scheduled", action_id, sched_at, error="call_dispatch_failed")
    except Exception as exc:
        logger.error("_schedule_or_start_call error for %s: %s", phone, exc)
        sched_at = _next_allowed_time(5, rule)
        action_id = await insert_automation_action(
            phone, event_type, source, "call_only", sched_at,
            payload={**contact, "call_type": call_type, "rule": rule, "fallback_action": fallback_action},
        )
        return done("scheduled", action_id, sched_at, error="call_dispatch_failed")


# ── Call-outcome WhatsApp fallback ─────────────────────────────────────────

async def handle_call_outcome_whatsapp_fallback(
    phone: str,
    outcome: str,
    call_log_id: str,
    contact: Optional[dict] = None,
) -> Optional[dict]:
    """After a call outcome is saved, check if a fallback WhatsApp should be sent.

    Deduplication: uses source_id = call_log_id so same call never sends twice.
    """
    if not phone or not outcome:
        return None
    try:
        # Check if a pending whatsapp_fallback_on_outcome action exists for this phone
        actions = await get_automation_actions(phone=phone, limit=20)
        fallback_action = None
        for a in reversed(actions):
            if a.get("action_type") == "whatsapp_fallback_on_outcome" and a.get("status") == "pending":
                fallback_action = a
                break
        if not fallback_action:
            return None

        import json as _json
        payload = {}
        try:
            payload = _json.loads(fallback_action.get("payload") or "{}")
        except Exception:
            pass

        rule = payload.get("rule") or {}
        fa = payload.get("fallback_action") or "call_then_whatsapp_on_failure"
        outcome_norm = outcome.strip().lower()

        should_send = False

        if fa == "call_then_whatsapp_always":
            should_send = True
        elif fa == "call_then_whatsapp_on_failure":
            if outcome_norm in WA_FALLBACK_OUTCOMES:
                should_send = True
            elif outcome_norm == "no_answer" and rule.get("send_on_no_answer"):
                should_send = True
            elif outcome_norm == "busy" and rule.get("send_on_busy"):
                should_send = True

        if not should_send:
            await update_automation_action_status(fallback_action["id"], "skipped", {"skipped": "outcome_not_triggering", "outcome": outcome_norm}, "outcome_not_triggering")
            return {"sent": False, "reason": "outcome_not_triggering"}

        selected_template = rule.get("fallback_whatsapp_template") or rule.get("whatsapp_template") or "missed_call_template"
        fallback_template = await resolve_wa_template(selected_template)

        language = await _get_wa_setting("WHATSAPP_DEFAULT_LANGUAGE") or "en"
        fallback_contact = contact or {}
        if "lead_name" not in fallback_contact:
            fallback_contact = {**fallback_contact, "lead_name": "there"}
        params = _build_template_params(selected_template, fallback_contact, None)
        wa_result = await _send_rule_whatsapp(
            phone, "call_fallback", fallback_action["id"],
            selected_template, fallback_template, language, params,
        )
        status = "completed" if wa_result["success"] else "failed"
        await update_automation_action_status(fallback_action["id"], status, wa_result, wa_result.get("error") or "")
        return wa_result
    except Exception as exc:
        logger.error("handle_call_outcome_whatsapp_fallback error: %s", exc)
        return None


# ── Due automation actions runner ──────────────────────────────────────────

async def run_due_automation_actions() -> dict:
    """Called by APScheduler every minute. Processes pending automation actions."""
    now = datetime.now().isoformat()
    try:
        due = await _db().get_repository().claim_automation_actions(now, limit=20, lease_seconds=300)
    except Exception as exc:
        logger.debug("run_due_automation_actions fetch failed: %s", exc)
        return {"processed": 0, "error": str(exc)}

    if not due:
        await _db().log_error("automation", "automation_queue_empty", "automation_queue_empty=true", "info")
        return {"processed": 0, "total_due": 0, "automation_queue_empty": True}

    processed = 0
    for action in due:
        action_id = action.get("id", "")
        claim_token = action.get("claim_token") or ""
        try:
            import json as _json
            raw_payload = action.get("payload") or {}
            payload = raw_payload if isinstance(raw_payload, dict) else _json.loads(raw_payload or "{}")

            action_type = action.get("action_type", "")
            phone = action.get("phone_number", "")
            event_type = action.get("event_type", "")
            source = action.get("source", "")

            if action_type == "call_only":
                rule = payload.get("rule") or {}
                call_type = payload.get("call_type") or "welcome_call"
                contact = {k: payload.get(k, "") for k in ("phone", "lead_name", "business_name", "service_type", "source")}
                contact["phone"] = phone
                contact["source"] = contact.get("source") or source
                call_status = await _schedule_or_start_call(
                    phone, contact, call_type, event_type, source, delay_minutes=0, rule=rule,
                    fallback_action=payload.get("fallback_action"),
                )
                await update_automation_action_status(action_id, "completed", {"call_status": call_status}, claim_token=claim_token)

            elif action_type == "whatsapp_only":
                template = payload.get("template_name") or ""
                language = payload.get("language") or "en"
                parameters = payload.get("parameters") or []
                wa_result = await send_whatsapp_template(
                    phone, template, language, parameters,
                    event_type=event_type, source_type="automation_runner", source_id=action_id,
                    template_purpose=payload.get("template_purpose") or payload.get("selected_template") or "",
                )
                status = "completed" if wa_result["success"] else ("skipped" if wa_result.get("reason") == "duplicate_suppressed_cooldown" else "failed")
                await update_automation_action_status(action_id, status, wa_result, wa_result.get("error") or "", claim_token=claim_token)

            elif action_type == "health_test" and event_type == "whatsapp_health_test":
                # Dedicated no-side-effect worker heartbeat. It must never call,
                # message, create appointments, or start campaign processing.
                if payload.get("is_test") is True and payload.get("test_run_id"):
                    await update_automation_action_status(action_id, "completed", {
                        "is_test": True,
                        "test_run_id": payload.get("test_run_id"),
                        "worker_picked_up": True,
                        "production_action_triggered": False,
                    }, claim_token=claim_token)
                else:
                    await update_automation_action_status(action_id, "failed", {}, "invalid health-test payload", claim_token=claim_token)

            else:
                await update_automation_action_status(action_id, "skipped", {"skipped": f"unhandled type: {action_type}"}, f"unhandled type: {action_type}", claim_token=claim_token)

            processed += 1
        except Exception as exc:
            logger.error("Automation action %s failed: %s", action_id, exc)
            await update_automation_action_status(action_id, "failed", {}, str(exc)[:500], claim_token=claim_token)

    return {"processed": processed, "total_due": len(due)}


# ── Confirmation helpers ───────────────────────────────────────────────────

async def send_callback_confirmation(phone: str, context: Optional[dict] = None) -> dict:
    template = await resolve_wa_template("callback_confirmation_template")
    language = await _get_wa_setting("WHATSAPP_DEFAULT_LANGUAGE") or "en"
    params = _build_template_params("callback_confirmation_template", context or {}, context)
    result = await execute_automation_rule("callback_scheduled", {"phone": phone, **(context or {})})
    if result.get("action") != "manual_only":
        return result
    if template:
        return await send_whatsapp_template(phone, template, language, params, event_type="callback_scheduled", source_type="manual", source_id=phone, template_purpose="callback_confirmation_template")
    return {"success": False, "error": "No callback template configured", "reason": "template_missing"}


async def send_appointment_confirmation(phone: str, context: Optional[dict] = None) -> dict:
    template = await resolve_wa_template("appointment_confirmation_template")
    language = await _get_wa_setting("WHATSAPP_DEFAULT_LANGUAGE") or "en"
    params = _build_template_params("appointment_confirmation_template", context or {}, context)
    result = await execute_automation_rule("appointment_confirmed", {"phone": phone, **(context or {})})
    if result.get("action") != "manual_only":
        return result
    if template:
        return await send_whatsapp_template(phone, template, language, params, event_type="appointment_confirmed", source_type="manual", source_id=phone, template_purpose="appointment_confirmation_template")
    return {"success": False, "error": "No appointment template configured", "reason": "template_missing"}


async def send_showroom_visit_confirmation(phone: str, context: Optional[dict] = None) -> dict:
    # Showroom visit reuses the appointment confirmation slot (single confirmation template covers both).
    template = await resolve_wa_template("appointment_confirmation_template")
    language = await _get_wa_setting("WHATSAPP_DEFAULT_LANGUAGE") or "en"
    params = _build_template_params("appointment_confirmation_template", context or {}, context)
    result = await execute_automation_rule("showroom_visit_confirmed", {"phone": phone, **(context or {})})
    if result.get("action") != "manual_only":
        return result
    if template:
        return await send_whatsapp_template(phone, template, language, params, event_type="showroom_visit_confirmed", source_type="manual", source_id=phone, template_purpose="appointment_confirmation_template")
    return {"success": False, "error": "No showroom template configured", "reason": "template_missing"}


# ══════════════════════════════════════════════════════════════════════════════
# PHASE 8 — WhatsApp Chat Inbox
# ══════════════════════════════════════════════════════════════════════════════

_OPT_OUT_KEYWORDS = {"stop", "unsubscribe", "optout", "opt out", "opt-out",
                     "don't message", "dont message", "do not message",
                     "no message", "remove me", "block"}


# ── Conversation helpers ───────────────────────────────────────────────────

_wa_ai_locks: dict[str, asyncio.Lock] = {}


def _conversation_lock(phone: str) -> asyncio.Lock:
    lock = _wa_ai_locks.get(phone)
    if lock is None:
        lock = asyncio.Lock()
        _wa_ai_locks[phone] = lock
    return lock


async def _log_whatsapp_ai_reason(phone: str, reason: str, detail: str = "", level: str = "warning") -> None:
    msg = f"WhatsApp AI reply skipped for {phone}: {reason}"
    if level == "error":
        logger.error("%s %s", msg, detail)
    elif level == "info":
        logger.info("%s %s", msg, detail)
    else:
        logger.warning("%s %s", msg, detail)
    _schedule_background_log(_db().log_error("whatsapp_ai", msg, detail or reason, level), "whatsapp_ai_reason_log")


async def _log_whatsapp_ai_event(phone: str, event: str, detail: str = "", level: str = "info") -> None:
    msg = f"WhatsApp AI event for {phone}: {event}"
    if level == "error":
        logger.error("%s %s", msg, detail)
    elif level == "warning":
        logger.warning("%s %s", msg, detail)
    else:
        logger.info("%s %s", msg, detail)
    _schedule_background_log(_db().log_error("whatsapp_ai", msg, detail or event, level), "whatsapp_ai_event_log")


async def _remember_whatsapp_insight(phone: str, insight: str) -> None:
    """Persist compact durable facts, not raw chat transcripts."""
    try:
        from db import add_contact_memory
        await add_contact_memory(phone, insight[:1000])
    except Exception as exc:
        await _log_whatsapp_ai_event(phone, "contact_memory_save_failed", str(exc)[:300], "warning")


def _safe_media_auto_reply_text(message_type: str) -> str:
    msg_type = (message_type or "").lower()
    if msg_type == "image":
        return "Thanks, I received the image. Our team will check and confirm shortly."
    if msg_type == "document":
        return "Thanks, I received the document. Our team will review it and confirm shortly."
    if msg_type in ("audio", "voice"):
        return "Thanks, I received your voice message. Our team will check and get back shortly."
    if msg_type == "video":
        return "Thanks, I received the video. Our team will check and confirm shortly."
    return "Thanks, I received your message. Our team will check and confirm shortly."


async def _send_whatsapp_media_auto_reply(phone: str, conv_id: str, msg_type: str, inbound_saved: Optional[dict]) -> None:
    reply_text = _safe_media_auto_reply_text(msg_type)
    await _log_whatsapp_ai_event(phone, "whatsapp_media_auto_reply_started", f"type={msg_type} conversation_id={conv_id}")
    send_result = await send_whatsapp_text(phone, reply_text)
    provider_id = send_result.get("provider_message_id") or ""
    await save_wa_message(
        conv_id=conv_id,
        phone=phone,
        direction="outbound",
        message_type="text",
        message_text=reply_text,
        provider_message_id=provider_id,
        provider_status="sent" if send_result.get("success") else "failed",
        failure_reason="" if send_result.get("success") else (send_result.get("reason") or send_result.get("error") or ""),
        raw_payload={"reply_to_message_id": inbound_saved.get("id") if inbound_saved else "", "auto_reply_for": msg_type, "send_result": send_result},
        ai_generated=True,
    )
    if send_result.get("success"):
        await _log_whatsapp_ai_event(phone, "whatsapp_media_auto_reply_success", f"type={msg_type} provider_message_id={provider_id}")
        await update_conversation_last_message(conv_id, reply_text, increment_unread=False)
    else:
        await _log_whatsapp_ai_event(phone, "whatsapp_media_auto_reply_failed", send_result.get("error") or "", "error")


async def get_whatsapp_gemini_model() -> str:
    from db import get_setting as _gs

    for key in ("WHATSAPP_GEMINI_MODEL", "GEMINI_TEXT_MODEL"):
        model = (await _gs(key, "") or "").strip()
        if model and "live" not in model.lower():
            return model
    return "gemini-2.5-flash"


def _usable_whatsapp_name(*values: str) -> str:
    for value in values:
        name = str(value or "").strip()
        if not name:
            continue
        digits = "".join(ch for ch in name if ch.isdigit())
        if len(digits) >= 8:
            continue
        lowered = name.lower()
        if lowered in {"there", "customer", "unknown"} or lowered.startswith("whatsapp +"):
            continue
        return name
    return ""


def _safe_exception_detail(exc: Exception) -> str:
    parts = [f"type={type(exc).__name__}", f"message={str(exc)[:500]}"]
    for attr in ("code", "status_code", "status", "details", "hint"):
        val = getattr(exc, attr, None)
        if val:
            parts.append(f"{attr}={str(val)[:300]}")
    return "; ".join(parts)


async def get_conversations_by_phone(phone: str, include_deleted: bool = False, limit: int = 20) -> list:
    try:
        try:
            from db import normalize_phone
            phone = normalize_phone(phone)
        except Exception:
            phone = phone or ""
        return await _db().get_repository().list_whatsapp_conversations(
            phone=phone,
            include_deleted=include_deleted,
            limit=limit,
        )
    except Exception as exc:
        await _db().log_error("whatsapp_inbox", "whatsapp_conversations_by_phone_fetch_failed", f"phone={phone}; {_safe_exception_detail(exc)}", "warning")
        return []


async def get_or_create_conversation(phone: str, contact_name: str = "") -> dict:
    """Serialize canonical conversation lookup/creation for a normalized phone."""
    try:
        from db import normalize_phone
        canonical_phone = normalize_phone(phone)
    except Exception:
        canonical_phone = phone or ""
    async with _conversation_lock(f"conversation-row:{canonical_phone}"):
        try:
            return await _db().get_repository().get_or_create_whatsapp_conversation(canonical_phone, contact_name)
        except Exception as exc:
            detail = _safe_exception_detail(exc)
            logger.error("repository conversation get/create failed: %s", detail)
            try:
                await _db().log_error("whatsapp_inbox", "whatsapp_conversation_get_or_create_failed", f"phone={canonical_phone}; {detail}", "error")
            except Exception:
                pass
            return {}


async def update_conversation_last_message(conv_id: str, text: str, increment_unread: bool = True) -> None:
    try:
        await _db().get_repository().update_whatsapp_conversation_last_message(
            conv_id,
            text,
            increment_unread=increment_unread,
        )
    except Exception as exc:
        logger.debug("update_conversation_last_message error: %s", exc)


async def _provider_message_record(provider_message_id: str) -> dict:
    if not provider_message_id:
        return {}
    try:
        return await _db().get_repository().get_inbound_whatsapp_message_by_provider_id(provider_message_id)
    except Exception as exc:
        await _diagnostic_event(
            "duplicate_check_failed", message_id=provider_message_id,
            exception_type=type(exc).__name__, level="warning",
        )
        return {}


class InboundProcessingInProgress(RuntimeError):
    """A concurrent delivery is still handling this provider message."""


def _inbound_processing_retryable(message: Optional[dict], *, now: Optional[datetime] = None) -> bool:
    """Only failed or abandoned inbound work may be processed again.

    A fresh ``processing`` row can be a concurrent delivery and must be skipped.
    An old one means the worker disappeared after persisting the inbound message.
    """
    message = message or {}
    status = str(message.get("provider_status") or "").strip().lower()
    if status == "processing_failed":
        return True
    if status != "processing":
        return False
    created_at = message.get("created_at")
    try:
        created = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        return (current.astimezone(timezone.utc) - created.astimezone(timezone.utc)).total_seconds() >= 60
    except (TypeError, ValueError):
        return False


async def _set_inbound_processing_status(provider_message_id: str, status: str) -> None:
    if not provider_message_id:
        return
    try:
        await _db().get_repository().update_inbound_whatsapp_message_status(provider_message_id, status)
    except Exception as exc:
        await _diagnostic_event(
            "inbound_processing_status_update_failed", message_id=provider_message_id,
            target_status=status, exception_type=type(exc).__name__, level="error",
        )


async def patch_conversation(conv_id: str, updates: dict) -> dict:
    """Patch status/ai_enabled/assigned_to on a conversation."""
    try:
        allowed = {"status", "ai_enabled", "assigned_to", "unread_count", "appointment_state"}
        upd = {k: v for k, v in updates.items() if k in allowed}
        return await _db().get_repository().update_whatsapp_conversation(conv_id, upd)
    except Exception as exc:
        logger.error("patch_conversation error: %s", exc)
        return {}


async def get_conversations(
    status: Optional[str] = None,
    ai_enabled: Optional[bool] = None,
    search: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> list:
    try:
        conversations = await _db().get_repository().list_whatsapp_conversations(
            status=status or "",
            ai_enabled=ai_enabled,
            search=search or "",
            limit=limit,
            offset=offset,
        )
        canonical = []
        seen_phones = set()
        for conversation in conversations:
            try:
                from db import normalize_phone
                phone_key = normalize_phone(conversation.get("phone_number") or "")
            except Exception:
                phone_key = conversation.get("phone_number") or ""
            if phone_key in seen_phones:
                continue
            seen_phones.add(phone_key)
            canonical.append(conversation)
        return canonical
    except Exception as exc:
        logger.error("get_conversations error: %s", exc)
        return []


async def get_conversation_by_id(conv_id: str) -> Optional[dict]:
    try:
        return await _db().get_repository().get_whatsapp_conversation_by_id(conv_id)
    except Exception as exc:
        logger.error("get_conversation_by_id error: %s", exc)
        return None


# ── Message helpers ────────────────────────────────────────────────────────

async def save_wa_message(
    conv_id: str,
    phone: str,
    direction: str,
    message_type: str = "text",
    message_text: str = "",
    template_name: str = "",
    media_url: str = "",
    media_id: str = "",
    mime_type: str = "",
    file_name: str = "",
    caption: str = "",
    provider_message_id: str = "",
    provider_status: str = "",
    failure_reason: str = "",
    raw_payload: Optional[dict] = None,
    ai_generated: bool = False,
    human_sent: bool = False,
) -> dict:
    try:
        now = datetime.now().isoformat()
        row = {
            "id": str(uuid.uuid4()),
            "conversation_id": conv_id,
            "phone_number": phone,
            "direction": direction,
            "message_type": message_type,
            "message_text": (message_text or "")[:4000],
            "template_name": template_name or "",
            "media_url": media_url or "",
            "media_id": media_id or "",
            "mime_type": mime_type or "",
            "file_name": file_name or "",
            "caption": caption or "",
            "provider_message_id": provider_message_id or "",
            "provider_status": provider_status or "",
            "failure_reason": (failure_reason or "")[:500],
            "raw_payload": _json.dumps(raw_payload or {}),
            "ai_generated": ai_generated,
            "human_sent": human_sent,
            "created_at": now,
        }
        try:
            await _db().get_repository().save_whatsapp_message(row)
        except Exception:
            fallback = dict(row)
            for key in ("media_id", "mime_type", "file_name", "caption", "failure_reason"):
                fallback.pop(key, None)
            await _db().get_repository().save_whatsapp_message(fallback)
        await _diagnostic_event(
            "whatsapp_message_persisted", phone=phone, conversation_id=conv_id,
            message_id=provider_message_id, direction=direction, message_type=message_type,
        )
        return row
    except Exception as exc:
        logger.error("save_wa_message error: %s", exc)
        await _diagnostic_event(
            "whatsapp_message_persist_failed", phone=phone, conversation_id=conv_id,
            message_id=provider_message_id, direction=direction,
            exception_type=type(exc).__name__, level="error",
        )
        return {}


async def record_outbound_template_message(
    phone: str,
    template_name: str,
    language: str,
    parameters,
    status: str,
    provider_message_id: Optional[str],
    error_message: Optional[str],
    source_type: str = "",
    source_id: str = "",
) -> dict:
    try:
        conv = await get_or_create_conversation(phone)
        if not conv:
            return {}
        preview = _render_template_preview(template_name, parameters)
        if status == "failed" and error_message:
            preview += f" (failed: {error_message[:120]})"
        saved = await save_wa_message(
            conv_id=conv["id"],
            phone=phone,
            direction="outbound",
            message_type="template",
            message_text=preview,
            template_name=template_name,
            provider_message_id=provider_message_id or "",
            provider_status=status,
            raw_payload={
                "language": language or "en",
                "parameters": parameters or [],
                "template_name": template_name or "",
                "source_type": source_type,
                "source_id": source_id,
                "error_message": error_message or "",
            },
        )
        await update_conversation_last_message(conv["id"], preview, increment_unread=False)
        return saved
    except Exception as exc:
        logger.error("record_outbound_template_message error for %s: %s", phone, exc)
        return {}


def _render_template_preview(template_name: str, parameters) -> str:
    params = [str(p).strip() for p in (parameters or [])]
    name = params[0] if len(params) > 0 and params[0] else "there"
    business_name = params[1] if len(params) > 1 and params[1] else "AirZen"
    if (template_name or "").strip() == "voice_ai_demo_welcome":
        return (
            f"Hi {name}, thank you for contacting {business_name}.\n\n"
            "To route your enquiry correctly, may I know whether you need "
            "aircon servicing, repair, maintenance, or another air conditioning enquiry?"
        )
    preview = f"[Template: {template_name}]"
    if params:
        preview += " " + ", ".join(p for p in params if p)[:250]
    return preview


async def get_messages(conv_id: str, limit: int = 50, offset: int = 0) -> list:
    """Return the newest page in chronological display order."""
    try:
        return await _db().get_repository().list_whatsapp_messages(
            conv_id,
            limit=limit,
            offset=offset,
        )
    except Exception as exc:
        logger.error("get_messages error: %s", exc)
        return []


async def soft_delete_whatsapp_message(message_id: str, deleted_by: str = "dashboard") -> bool:
    try:
        ok = await _db().get_repository().soft_delete_whatsapp_message(message_id, deleted_by)
        await _db().log_error("whatsapp_inbox", "whatsapp_message_soft_deleted", f"message_id={message_id} deleted_by={deleted_by}", "info")
        return ok
    except Exception as exc:
        logger.error("soft_delete_whatsapp_message error: %s", exc)
        return False


async def soft_delete_whatsapp_conversation(conv_id: str, deleted_by: str = "dashboard") -> bool:
    try:
        ok = await _db().get_repository().soft_delete_whatsapp_conversation(conv_id, deleted_by)
        await _db().log_error("whatsapp_inbox", "whatsapp_conversation_soft_deleted", f"conversation_id={conv_id} deleted_by={deleted_by}", "info")
        return ok
    except Exception as exc:
        logger.error("soft_delete_whatsapp_conversation error: %s", exc)
        return False


# ── CRM linking ────────────────────────────────────────────────────────────

def _meta_media_proxy_url(media_id: str) -> str:
    return f"/api/whatsapp/media/{media_id}" if media_id else ""


async def fetch_whatsapp_media(media_id: str) -> dict:
    """Fetch Meta WhatsApp media through the backend without exposing tokens."""
    media_id = (media_id or "").strip()
    if not media_id:
        return {"success": False, "error": "media_id is required", "status": 400}
    cfg = await _wa_config()
    provider = (cfg.get("WHATSAPP_PROVIDER") or "meta").strip().lower()
    token = (cfg.get("WHATSAPP_ACCESS_TOKEN") or "").strip()
    graph_version = (cfg.get("WHATSAPP_GRAPH_VERSION") or "v20.0").strip() or "v20.0"
    await _db().log_error("whatsapp_media", "whatsapp_media_fetch_started", f"media_id={media_id} provider={provider}", "info")
    if not token:
        err = "WhatsApp access token is not configured"
        await _db().log_error("whatsapp_media", "whatsapp_media_fetch_failed", f"media_id={media_id} http_status=400 error={err}", "error")
        return {"success": False, "error": err, "status": 400}
    headers = {"Authorization": f"Bearer {token}"}
    try:
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(f"https://graph.facebook.com/{graph_version}/{media_id}", headers=headers) as resp:
                meta = await resp.json(content_type=None)
                if resp.status >= 400:
                    err = str(meta)[:500]
                    await _db().log_error("whatsapp_media", "whatsapp_media_fetch_failed", f"media_id={media_id} http_status={resp.status} error={err}", "error")
                    return {"success": False, "error": err, "status": resp.status}
            media_url = meta.get("url") or ""
            mime_type = meta.get("mime_type") or "application/octet-stream"
            if not media_url:
                err = "Media URL not returned by Meta"
                await _db().log_error("whatsapp_media", "whatsapp_media_fetch_failed", f"media_id={media_id} mime_type={mime_type} http_status=502 error={err}", "error")
                return {"success": False, "error": err, "status": 502}
            async with session.get(media_url, headers=headers) as media_resp:
                content = await media_resp.read()
                if media_resp.status >= 400:
                    err = content[:300].decode("utf-8", errors="ignore")
                    await _db().log_error("whatsapp_media", "whatsapp_media_fetch_failed", f"media_id={media_id} mime_type={mime_type} http_status={media_resp.status} error={err}", "error")
                    return {"success": False, "error": err, "status": media_resp.status}
                final_mime = media_resp.headers.get("Content-Type") or mime_type
                await _db().log_error("whatsapp_media", "whatsapp_media_fetch_success", f"media_id={media_id} mime_type={final_mime} http_status={media_resp.status}", "info")
                return {
                    "success": True,
                    "content": content,
                    "mime_type": final_mime,
                    "file_name": meta.get("file_name") or media_id,
                    "status": 200,
                }
    except Exception as exc:
        logger.error("fetch_whatsapp_media error for %s: %s", media_id, exc)
        await _db().log_error("whatsapp_media", "whatsapp_media_fetch_failed", f"media_id={media_id} http_status=500 error={str(exc)[:500]}", "error")
        return {"success": False, "error": str(exc)[:500], "status": 500}


async def link_conversation_to_crm(conv_id: str, phone: str, contact_name: str = "") -> Optional[dict]:
    """Find or create CRM contact, link to conversation, add note."""
    try:
        from db import get_crm_contact_by_phone, upsert_crm_lead, update_lead_journey

        today = datetime.now().strftime("%Y-%m-%d")
        note_line = f"[{today}] WhatsApp message received."
        name = contact_name or f"WhatsApp {phone}"
        try:
            await upsert_crm_lead(
                {
                    "phone_number": phone,
                    "lead_name": name,
                    "source": "WhatsApp",
                    "crm_status": "New Lead",
                    "crm_notes": "WhatsApp message received.",
                },
                import_source="inbound",
            )
            await update_lead_journey(phone, {
                "preferred_channel": "whatsapp",
                "last_customer_reply_at": datetime.now().isoformat(),
                "last_intent": "message_received",
            })
        except Exception as upsert_exc:
            await _db().log_error(
                "whatsapp_inbox",
                "whatsapp_crm_canonical_upsert_failed",
                f"phone={phone}; conversation_id={conv_id}; error={str(upsert_exc)[:300]}",
                "warning",
            )

        contact = await get_crm_contact_by_phone(phone)
        crm_id = ""

        if contact:
            crm_id = contact.get("id") or contact.get("phone_number") or phone
            updates = {}
            existing_name = str(contact.get("lead_name") or "").strip()
            if contact_name and (not existing_name or existing_name == phone or existing_name.lower().startswith("whatsapp ")):
                updates["lead_name"] = contact_name
            source = str(contact.get("source") or "").strip()
            if not source or source.lower() == "voice call":
                updates["source"] = "WhatsApp"
            if not contact.get("preferred_channel"):
                updates["preferred_channel"] = "whatsapp"
            if not contact.get("journey_stage"):
                updates["journey_stage"] = "new_lead"

            existing_notes = contact.get("crm_notes") or ""
            if note_line not in existing_notes:
                updates["crm_notes"] = (existing_notes.strip() + "\n" + note_line).strip() if existing_notes else note_line
            if updates:
                updates["updated_at"] = datetime.now().isoformat()
                await _db().get_repository().update_crm_contact(phone, updates)
        else:
            # Create new CRM contact
            new_contact = {
                "id": str(uuid.uuid4()),
                "phone_number": phone,
                "lead_name": name,
                "source": "WhatsApp",
                "crm_status": "New Lead",
                "lead_country": "Singapore",
                "lead_country_code": "+65",
                "crm_notes": note_line,
                "journey_stage": "new_lead",
                "preferred_channel": "whatsapp",
                "last_customer_reply_at": datetime.now().isoformat(),
                "created_at": datetime.now().isoformat(),
                "updated_at": datetime.now().isoformat(),
                "next_followup_at": datetime.now().strftime("%Y-%m-%d"),
            }
            inserted = await _db().get_repository().upsert_crm_contact(new_contact)
            crm_id = inserted.get("id", phone) if inserted else phone

        # Update conversation with crm_contact_id
        if crm_id:
            await _db().get_repository().update_whatsapp_conversation(conv_id, {"crm_contact_id": str(crm_id)})
            await _db().log_error("whatsapp_inbox", "whatsapp_conversation_linked_to_crm", f"phone={phone}; conversation_id={conv_id}; crm_id={crm_id}", "info")

        return {"crm_id": crm_id, "phone": phone}
    except Exception as exc:
        logger.error("link_conversation_to_crm error: %s", exc)
        await _db().log_error("whatsapp_inbox", "whatsapp_conversation_crm_link_failed", f"phone={phone}; conversation_id={conv_id}; error={str(exc)[:300]}", "warning")
        return None


# ── 24-hour service window ─────────────────────────────────────────────────

async def is_whatsapp_service_window_open(phone_or_conv_id: str) -> bool:
    """Return True if customer sent inbound message within last 24 hours."""
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        return await _db().get_repository().has_recent_inbound_whatsapp_message(phone_or_conv_id, cutoff)
    except Exception as exc:
        logger.debug("is_whatsapp_service_window_open error: %s", exc)
        return False


# ── Send free-form text (Phase 8 real implementation) ─────────────────────

async def _send_vobiz_text(phone: str, message: str, cfg: dict) -> dict:
    """Send free-form WhatsApp text via Vobiz API.

    POST https://api.vobiz.ai/v1/messaging/messages
    Headers: X-Auth-ID, X-Auth-Token, Content-Type: application/json
    Payload: {channel_id, to, type:'text', text:{body}}
    """
    auth_id = cfg.get("VOBIZ_AUTH_ID", "").strip()
    auth_token = cfg.get("VOBIZ_AUTH_TOKEN", "").strip()
    channel_id = cfg.get("VOBIZ_CHANNEL_ID", "").strip()

    missing = []
    if not auth_id: missing.append("VOBIZ_AUTH_ID")
    if not auth_token: missing.append("VOBIZ_AUTH_TOKEN")
    if not channel_id: missing.append("VOBIZ_CHANNEL_ID")
    if missing:
        await _db().log_error("whatsapp_text", "whatsapp_text_missing_config", f"phone={phone}; provider=vobiz; missing={','.join(missing)}", "error")
        return {"success": False, "error": f"Missing config: {', '.join(missing)}", "reason": "vobiz_not_configured"}

    # Vobiz expects E.164 with leading +
    to_phone = phone if phone.startswith("+") else ("+" + phone) if phone else ""
    if not to_phone:
        return {"success": False, "error": "Invalid phone number", "reason": "invalid_phone"}

    url = "https://api.vobiz.ai/v1/messaging/messages"
    headers = {
        "X-Auth-ID": auth_id,
        "X-Auth-Token": auth_token,
        "Content-Type": "application/json",
    }
    payload = {
        "channel_id": channel_id,
        "to": to_phone,
        "type": "text",
        "text": {"body": message},
    }
    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=payload, headers=headers) as resp:
                try:
                    resp_json = await resp.json()
                except Exception:
                    resp_json = {"raw": await resp.text()}
                if resp.status in (200, 201):
                    # Try several common id field shapes
                    msg_id = (
                        resp_json.get("message_id")
                        or resp_json.get("id")
                        or (resp_json.get("data") or {}).get("message_id")
                        or (resp_json.get("data") or {}).get("id")
                        or ""
                    )
                    await _db().log_error("whatsapp_text", "whatsapp_text_send_response", f"phone={phone}; provider=vobiz; http_status={resp.status}; provider_message_id={msg_id}", "info")
                    return {"success": True, "provider_message_id": msg_id, "error": None}
                error_data = resp_json.get("error") or resp_json.get("message") or resp_json
                err_msg = (
                    error_data.get("message") if isinstance(error_data, dict) else str(error_data)
                ) or f"HTTP {resp.status}"
                await _db().log_error("whatsapp_text", "whatsapp_text_send_response", f"phone={phone}; provider=vobiz; http_status={resp.status}; error={str(err_msg)[:300]}", "error")
                return {"success": False, "provider_message_id": None, "error": str(err_msg)[:500], "reason": "vobiz_provider_error"}
    except Exception as exc:
        logger.error("vobiz send_text error for %s: %s", phone, exc)
        return {"success": False, "error": str(exc)[:500], "reason": "send_error"}


async def send_whatsapp_text(phone: str, message: str) -> dict:
    """Send free-form WhatsApp text message. Routes to Vobiz or Meta based on provider."""
    await _diagnostic_event("outbound_send_started", phone=phone, message_kind="text")
    if not await _is_wa_enabled():
        await _db().log_error("whatsapp_text", "whatsapp_text_send_skipped", f"phone={phone}; reason=whatsapp_disabled", "warning")
        result = {"success": False, "error": "WhatsApp is disabled", "reason": "whatsapp_disabled"}
        await _log_wa(phone, "outbound_text", "", "en", [], "failed", None, result["reason"], "auto_reply", "")
        await _diagnostic_event("outbound_send_failed", phone=phone, level="error", reason=result["reason"])
        return result

    cfg = await _wa_config()
    provider = (cfg.get("WHATSAPP_PROVIDER") or "meta").strip().lower()
    await _db().log_error("whatsapp_text", "whatsapp_text_provider_selected", f"phone={phone}; provider={provider}", "info")
    if provider == "vobiz":
        result = await _send_vobiz_text(phone, message, cfg)
        if result.get("success"):
            await _db().log_error("whatsapp_text", "whatsapp_text_send_success", f"phone={phone}; provider=vobiz; provider_message_id={result.get('provider_message_id') or ''}", "info")
        else:
            await _db().log_error("whatsapp_text", "whatsapp_text_send_failed", f"phone={phone}; provider=vobiz; reason={result.get('reason')}; error={(result.get('error') or '')[:300]}", "error")
        await _log_wa(phone, "outbound_text", "", "en", [], "sent" if result.get("success") else "failed", result.get("provider_message_id"), result.get("reason") or result.get("error"), "auto_reply", "")
        await _diagnostic_event("outbound_send_succeeded" if result.get("success") else "outbound_send_failed", phone=phone, message_id=result.get("provider_message_id") or "", level="info" if result.get("success") else "error", provider="vobiz", reason=result.get("reason") or "")
        return result

    token = cfg.get("WHATSAPP_ACCESS_TOKEN", "").strip()
    phone_number_id = cfg.get("WHATSAPP_PHONE_NUMBER_ID", "").strip()
    graph_version = cfg.get("WHATSAPP_GRAPH_VERSION", "v20.0").strip() or "v20.0"

    if not token or not phone_number_id:
        missing = [k for k in ("WHATSAPP_ACCESS_TOKEN", "WHATSAPP_PHONE_NUMBER_ID") if not cfg.get(k, "").strip()]
        await _db().log_error("whatsapp_text", "whatsapp_text_missing_config", f"phone={phone}; provider=meta; missing={','.join(missing)}", "error")
        result = {"success": False, "error": f"Missing config: {', '.join(missing)}", "reason": "whatsapp_not_configured"}
        await _log_wa(phone, "outbound_text", "", "en", [], "failed", None, result["reason"], "auto_reply", "")
        await _diagnostic_event("outbound_send_failed", phone=phone, level="error", reason=result["reason"], missing_config_count=len(missing))
        return result

    to_phone = phone.lstrip("+") if phone else ""
    if not to_phone:
        await _db().log_error("whatsapp_text", "whatsapp_text_send_failed", "reason=invalid_phone", "error")
        result = {"success": False, "error": "Invalid phone number", "reason": "invalid_phone"}
        await _log_wa(phone, "outbound_text", "", "en", [], "failed", None, result["reason"], "auto_reply", "")
        await _diagnostic_event("outbound_send_failed", phone=phone, level="error", reason=result["reason"])
        return result

    payload = {
        "messaging_product": "whatsapp",
        "to": to_phone,
        "type": "text",
        "text": {"body": message},
    }
    url = f"https://graph.facebook.com/{graph_version}/{phone_number_id}/messages"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=payload, headers=headers) as resp:
                resp_json = {}
                try:
                    resp_json = await resp.json()
                except Exception:
                    resp_json = {"raw": await resp.text()}
                if resp.status in (200, 201):
                    messages = resp_json.get("messages") or []
                    msg_id = messages[0].get("id") if messages else None
                    await _db().log_error("whatsapp_text", "whatsapp_text_send_response", f"phone={phone}; provider=meta; http_status={resp.status}; provider_message_id={msg_id or ''}", "info")
                    try:
                        from db import update_lead_journey
                        await update_lead_journey(phone, {"last_whatsapp_sent_at": datetime.now().isoformat()})
                    except Exception:
                        pass
                    await _db().log_error("whatsapp_text", "whatsapp_text_send_success", f"phone={phone}; provider=meta; provider_message_id={msg_id or ''}", "info")
                    await _log_wa(phone, "outbound_text", "", "en", [], "sent", msg_id, None, "auto_reply", "")
                    await _diagnostic_event("outbound_send_succeeded", phone=phone, message_id=msg_id or "", provider="meta")
                    return {"success": True, "provider_message_id": msg_id, "error": None}
                else:
                    error_data = resp_json.get("error") or resp_json
                    err_msg = str(error_data.get("message", "") if isinstance(error_data, dict) else error_data)[:500]
                    await _db().log_error("whatsapp_text", "whatsapp_text_send_response", f"phone={phone}; provider=meta; http_status={resp.status}; error={err_msg[:300]}", "error")
                    await _log_wa(phone, "outbound_text", "", "en", [], "failed", None, "provider_error", "auto_reply", "")
                    await _diagnostic_event("outbound_send_failed", phone=phone, level="error", provider="meta", reason="provider_error", http_status=resp.status)
                    return {"success": False, "provider_message_id": None, "error": err_msg, "reason": "provider_error", "http_status": resp.status}
    except Exception as exc:
        logger.error("send_whatsapp_text error for %s: %s", phone, exc)
        await _db().log_error("whatsapp_text", "whatsapp_text_send_failed", f"phone={phone}; provider=meta; reason=send_error; error={str(exc)[:300]}", "error")
        await _log_wa(phone, "outbound_text", "", "en", [], "failed", None, "send_error", "auto_reply", "")
        await _diagnostic_event("outbound_send_failed", phone=phone, level="error", provider="meta", reason="send_error", exception_type=type(exc).__name__)
        return {"success": False, "error": str(exc)[:500], "reason": "send_error"}


# ── Opt-out detection ──────────────────────────────────────────────────────

def _is_opt_out(text: str) -> bool:
    t = re.sub(r"\s+", " ", (text or "").lower()).strip(" \t\r\n.!?,;:")
    if t in _OPT_OUT_KEYWORDS:
        return True
    explicit_phrases = ("don't message", "dont message", "do not message", "remove me")
    if any(phrase in t for phrase in explicit_phrases):
        return True
    return bool(re.fullmatch(r"(?:please\s+)?(?:stop|unsubscribe|opt[ -]?out)(?:\s+(?:messages?|messaging|contacting me|this))?", t))


# ── AI reply generation ────────────────────────────────────────────────────

async def _legacy_generate_whatsapp_ai_reply(
    conversation: dict,
    inbound_text: str,
    recent_messages: list,
) -> Optional[str]:
    """Generate a short WhatsApp AI reply using KB and Gemini."""
    try:
        from prompts import build_knowledge_context, build_prompt_for_type
        from db import get_knowledge_base, get_setting as _gs

        kb = await get_knowledge_base()
        kb_context = build_knowledge_context(kb)

        # Build conversation history snippet (last 6 messages)
        history_lines = []
        for m in recent_messages[-6:]:
            role = "Customer" if m.get("direction") == "inbound" else "Agent"
            history_lines.append(f"{role}: {m.get('message_text', '')}")
        history = "\n".join(history_lines)

        business_name = kb.get("company_profile", {}).get("name", "") or "our company"
        service_type = kb.get("company_profile", {}).get("services_summary", "") or "our service"

        system_prompt = (
            f"You are a helpful WhatsApp support agent for {business_name}.\n"
            f"Answer using ONLY the following company knowledge base. "
            f"If the answer is not available in the knowledge base, reply exactly: "
            f"\"Our team will confirm and get back to you.\"\n"
            f"Keep replies SHORT — max 3 sentences. Do not hallucinate pricing, policies, or offers.\n\n"
            f"{kb_context}"
        )

        user_prompt = (
            f"Conversation history:\n{history}\n\n"
            f"Customer just said: {inbound_text}\n\n"
            f"Reply:"
        )

        import google.generativeai as genai
        api_key = await _gs("GOOGLE_API_KEY", "")
        if not api_key:
            return None
        genai.configure(api_key=api_key)
        safe_model = await get_whatsapp_gemini_model()
        model = genai.GenerativeModel(safe_model, system_instruction=system_prompt)
        response = model.generate_content(user_prompt)
        reply = (response.text or "").strip()
        if not reply:
            return None
        # Truncate to safe WhatsApp length
        return reply[:1000]
    except Exception as exc:
        logger.error("generate_whatsapp_ai_reply error: %s", exc)
        return None


# ── Webhook payload parsing ────────────────────────────────────────────────

async def generate_whatsapp_ai_reply(
    conversation: dict,
    inbound_text: str,
    recent_messages: list,
    conversation_mode: str = "in_progress",
    crm_contact: Optional[dict] = None,
    durable_memory: Optional[list] = None,
) -> dict:
    """Generate a short WhatsApp chat reply using the WhatsApp-specific prompt."""
    try:
        from prompts import build_prompt_for_type
        from db import get_knowledge_base, get_setting as _gs

        lower_text = (inbound_text or "").strip().lower()
        known_name = _usable_whatsapp_name(
            (crm_contact or {}).get("lead_name"),
            (crm_contact or {}).get("name"),
            conversation.get("contact_name"),
        )
        prompt_source = "default"
        if re.fullmatch(r"(hi|hello|hey|hai|helo|good morning|good afternoon|good evening|thanks|thank you|ok|okay)[\s!.?]*", lower_text):
            if lower_text.startswith(("thanks", "thank you")):
                reply = "You're welcome. How else can I help you today?"
            elif lower_text in {"ok", "okay"}:
                reply = "Okay. Please tell me how I can help with your service request."
            elif known_name:
                reply = f"Hi {known_name}, how can I help you today?"
            else:
                reply = "Hi, thanks for contacting AirZen. How can I help you today?"
            return {
                "reply": reply,
                "reason": "",
                "provider": "deterministic_greeting",
                "prompt_type": "whatsapp_chat",
                "prompt_source": prompt_source,
            }
        if "lead follow" in lower_text or "follow-up" in lower_text or "follow up" in lower_text:
            return {
                "reply": "The AirZen team can help with this. May I know your preferred time for a quick call or appointment?",
                "reason": "",
                "provider": "deterministic_intent",
                "prompt_type": "whatsapp_chat",
                "prompt_source": prompt_source,
            }
        if "customer support" in lower_text or "support" in lower_text:
            return {
                "reply": "The AirZen team can confirm the right support option for you. May I know your preferred time for a quick call?",
                "reason": "",
                "provider": "deterministic_intent",
                "prompt_type": "whatsapp_chat",
                "prompt_source": prompt_source,
            }
        if "payment reminder" in lower_text or "payment follow" in lower_text or "payment" in lower_text:
            return {
                "reply": "The AirZen team will confirm the payment details and next step. May I know your preferred time for a quick call?",
                "reason": "",
                "provider": "deterministic_intent",
                "prompt_type": "whatsapp_chat",
                "prompt_source": prompt_source,
            }
        if any(word in lower_text for word in ("booking", "book demo", "demo", "appointment", "meeting", "google meet", "interested")):
            return {
                "reply": "Sure, I can help. May I know your preferred date and time?",
                "reason": "",
                "provider": "deterministic_intent",
                "prompt_type": "whatsapp_chat",
                "prompt_source": prompt_source,
            }
        if any(word in lower_text for word in ("ai voice", "voice agent", "ai calling", "voice bot", "calling agent")):
            return {
                "reply": "I can help with AirZen service details from our knowledge base. Which service would you like to know about?",
                "reason": "",
                "provider": "deterministic_intent",
                "prompt_type": "whatsapp_chat",
                "prompt_source": prompt_source,
            }
        if any(word in lower_text for word in ("price", "pricing", "cost", "charges", "rate", "per minute")):
            return {
                "reply": "The AirZen team will confirm pricing based on your requirement. May I know which service or package you are asking about?",
                "reason": "",
                "provider": "deterministic_intent",
                "prompt_type": "whatsapp_chat",
                "prompt_source": prompt_source,
            }
        if any(phrase in lower_text for phrase in ("call me", "callback", "call back", "please call")):
            return {
                "reply": "Sure, our team will call you shortly. If now is not suitable, please share your preferred time.",
                "reason": "",
                "provider": "deterministic_intent",
                "prompt_type": "whatsapp_chat",
                "prompt_source": prompt_source,
            }
        if conversation_mode == "new":
            if known_name:
                reply = f"Hi {known_name}, thanks for contacting us. How can I assist you?"
            else:
                reply = "Hi, thanks for contacting AirZen. How can I assist you today?"
            return {
                "reply": reply,
                "reason": "",
                "provider": "deterministic_greeting",
                "prompt_type": "whatsapp_chat",
                "prompt_source": prompt_source,
            }

        saved_prompt = await _gs("AI_PROMPT_whatsapp_chat", "") or None
        prompt_source = "db" if saved_prompt else "default"
        kb = await get_knowledge_base()
        history_lines = []
        for m in recent_messages[-8:]:
            role = "Customer" if m.get("direction") == "inbound" else "Agent"
            history_lines.append(f"{role}: {m.get('message_text', '')}")
        history = "\n".join(history_lines)
        memory_lines = [str(item.get("insight") or "").strip() for item in (durable_memory or [])[:5] if str(item.get("insight") or "").strip()]
        memory = "\n".join(f"- {line}" for line in memory_lines)
        appointment_state = _conversation_appointment_state(conversation)
        known_booking = _booking_known_fields(crm_contact, appointment_state)
        missing_booking = get_missing_booking_fields(crm_contact, appointment_state)
        known_lines = [
            f"- {key}: confirmed"
            for key in ("customer_name", "phone", "service", "units", "problem", "service_address", "postal_code", "property_type", "preferred_date", "preferred_time", "assigned_staff", "appointment_id")
            if known_booking.get(key)
        ]

        company_profile = kb.get("company_profile", {}) or {}
        business_name = company_profile.get("business_name") or company_profile.get("name") or "our company"
        service_type = company_profile.get("services_summary") or "our service"
        system_prompt = build_prompt_for_type(
            "whatsapp_chat",
            lead_name=known_name or "there",
            business_name=business_name,
            service_type=service_type,
            saved_text=saved_prompt,
            kb=kb,
        )
        verified_phone = str(conversation.get("phone_number") or (crm_contact or {}).get("phone_number") or "").strip()
        system_prompt += (
            "\n\nVerified WhatsApp channel rules:\n"
            "- The sender's WhatsApp number is already the verified customer contact number. Never ask for their mobile number again.\n"
            "- If the customer says 'same number' or 'this number', acknowledge that the WhatsApp number will be used.\n"
            "- Never invent an appointment date, time, availability, address, or booking confirmation.\n"
            "- Never say a booking/appointment request was received, recorded, created, scheduled, booked, or confirmed unless the known booking information includes an appointment_id.\n"
            "- Before an appointment_id exists, do not claim the appointment is booked; ask only for the next missing field or confirmation.\n"
            "- Appointment state and availability are handled by the booking system; ask for missing information only.\n"
            "- Information listed as known is already confirmed. Do not ask the customer for it again unless the customer explicitly indicates it has changed."
        )
        user_prompt = (
            f"Customer name: {known_name or 'there'}\n"
            f"Verified WhatsApp contact: {verified_phone or 'available from the conversation'}\n"
            f"Conversation mode: {conversation_mode}\n"
            f"Conversation history:\n{history}\n\n"
            f"Durable customer facts:\n{memory or '- None saved'}\n\n"
            f"Known booking information (values withheld for privacy):\n{chr(10).join(known_lines) or '- None'}\n"
            f"Missing booking fields: {', '.join(missing_booking) or 'none'}\n\n"
            f"Customer just said: {inbound_text}\n\n"
            f"Reply as a WhatsApp sales assistant. Keep it short. "
            f"Do not ask the name again when a customer name is known. "
            f"Do not ask for a mobile number; use the verified WhatsApp contact. "
            f"If this is about booking/demo/appointment/meeting, ask for preferred date and time. "
            f"If this is a price question, use the knowledge base or say the team will confirm."
        )

        import google.generativeai as genai
        api_key = await _gs("GOOGLE_API_KEY", "")
        if not api_key:
            return {"reply": None, "reason": "gemini_not_configured", "provider": "gemini", "prompt_type": "whatsapp_chat", "prompt_source": prompt_source}
        genai.configure(api_key=api_key)
        safe_model = await get_whatsapp_gemini_model()
        model = genai.GenerativeModel(safe_model, system_instruction=system_prompt)
        try:
            ai_timeout = max(3.0, min(float(await _gs("WHATSAPP_AI_TIMEOUT_SECONDS", "8") or 8), 12.0))
        except Exception:
            ai_timeout = 8.0
        response = await asyncio.wait_for(
            asyncio.to_thread(model.generate_content, user_prompt),
            timeout=ai_timeout,
        )
        reply = (response.text or "").strip()
        if not reply:
            return {"reply": None, "reason": "ai_generation_failed", "provider": "gemini", "prompt_type": "whatsapp_chat", "model": safe_model, "prompt_source": prompt_source}
        return {"reply": reply[:1000], "reason": "", "provider": "gemini", "prompt_type": "whatsapp_chat", "model": safe_model, "prompt_source": prompt_source}
    except Exception as exc:
        logger.error("generate_whatsapp_ai_reply error: %s", exc)
        return {"reply": None, "reason": "ai_generation_failed", "error": str(exc)[:500], "provider": "gemini", "prompt_type": "whatsapp_chat", "prompt_source": "default"}


def parse_vobiz_webhook_messages(payload: dict) -> list:
    """Extract list of inbound message dicts from a Vobiz webhook payload.

    Vobiz events: message.received, message.sent, message.delivered,
                  message.read, message.failed

    Payload shape (best-effort, tolerant to multiple wrappings):
      { "event": "message.received", "data": {
          "message": {"id":..., "from":..., "text":{"body":...}, "type":"text", ...},
          "channel_id": ..., "conversation_id": ..., "timestamp": ...
      }}
    Some integrations send it as a flat object, or as a list of events.
    """
    results: list = []
    try:
        # Normalize to a list of event dicts
        events: list = []
        if isinstance(payload, list):
            events = payload
        elif isinstance(payload, dict):
            if "events" in payload and isinstance(payload["events"], list):
                events = payload["events"]
            else:
                events = [payload]

        for ev in events:
            if not isinstance(ev, dict):
                continue
            event_type = (ev.get("event") or ev.get("type") or ev.get("event_type") or "").strip().lower()
            data = ev.get("data") if isinstance(ev.get("data"), dict) else ev
            msg = data.get("message") if isinstance(data.get("message"), dict) else data
            channel_id = data.get("channel_id") or msg.get("channel_id") or ""
            conversation_id = data.get("conversation_id") or msg.get("conversation_id") or ""
            timestamp = data.get("timestamp") or msg.get("timestamp") or ""

            # Status events: message.sent / delivered / read / failed
            if event_type in ("message.sent", "message.delivered", "message.read", "message.failed"):
                msg_id = msg.get("id") or msg.get("message_id") or data.get("message_id") or ""
                phone_raw = msg.get("to") or msg.get("recipient") or data.get("to") or ""
                phone = phone_raw if str(phone_raw).startswith("+") else ("+" + str(phone_raw)) if phone_raw else ""
                status_value = event_type.split(".", 1)[-1]  # sent/delivered/read/failed
                results.append({
                    "phone": phone,
                    "message_id": msg_id,
                    "message_type": "status_update",
                    "text": "",
                    "raw": ev,
                    "is_status_update": True,
                    "status_value": status_value,
                    "timestamp": timestamp,
                    "provider": "vobiz",
                })
                continue

            # Inbound message: message.received
            if event_type in ("message.received", "message_received", "received", ""):
                phone_raw = msg.get("from") or data.get("from") or ""
                phone = str(phone_raw)
                if phone and not phone.startswith("+"):
                    phone = "+" + phone
                msg_id = msg.get("id") or msg.get("message_id") or ""
                msg_type = (msg.get("type") or "text").lower()
                contact_name = (msg.get("contact") or {}).get("name", "") or msg.get("from_name", "") or ""

                text = ""
                media_url = ""
                media_id = ""
                mime_type = ""
                file_name = ""
                caption = ""
                if msg_type == "text":
                    text = (msg.get("text") or {}).get("body", "") or msg.get("body", "")
                elif msg_type in ("button", "interactive"):
                    inter = msg.get("interactive") or msg.get("button") or {}
                    btn = inter.get("button_reply") or inter.get("list_reply") or inter
                    text = (btn.get("title") if isinstance(btn, dict) else "") or (btn.get("id") if isinstance(btn, dict) else "") or ""
                elif msg_type == "image":
                    media = msg.get("image") or msg.get("media") or {}
                    media_id = media.get("id", "") or msg.get("media_id", "")
                    mime_type = media.get("mime_type", "") or msg.get("mime_type", "")
                    caption = media.get("caption", "") or msg.get("caption", "")
                    text = caption or "[image received]"
                    media_url = media.get("url", "") or msg.get("media_url", "")
                elif msg_type in ("audio", "voice"):
                    media = msg.get("audio") or msg.get("media") or {}
                    media_id = media.get("id", "") or msg.get("media_id", "")
                    mime_type = media.get("mime_type", "") or msg.get("mime_type", "")
                    msg_type = "voice" if msg_type == "voice" or media.get("voice") or msg.get("voice") else "audio"
                    text = "[voice message received]" if msg_type == "voice" else "[audio received]"
                    media_url = media.get("url", "") or msg.get("media_url", "")
                elif msg_type == "document":
                    media = msg.get("document") or msg.get("media") or {}
                    media_id = media.get("id", "") or msg.get("media_id", "")
                    mime_type = media.get("mime_type", "") or msg.get("mime_type", "")
                    file_name = media.get("filename", "") or media.get("file_name", "") or msg.get("file_name", "")
                    caption = media.get("caption", "") or msg.get("caption", "")
                    text = caption or f"[document received: {file_name or 'file'}]"
                    media_url = media.get("url", "") or msg.get("media_url", "")
                elif msg_type == "video":
                    text = "[Video received]"
                    media_url = (msg.get("video") or {}).get("url", "")
                else:
                    text = f"[unsupported message received: {msg_type}]"

                results.append({
                    "phone": phone,
                    "message_id": msg_id,
                    "message_type": msg_type,
                    "text": text,
                    "media_url": media_url,
                    "media_id": media_id,
                    "mime_type": mime_type,
                    "file_name": file_name,
                    "caption": caption,
                    "contact_name": contact_name,
                    "channel_id": channel_id,
                    "conversation_id": conversation_id,
                    "timestamp": timestamp,
                    "raw": ev,
                    "is_status_update": False,
                    "provider": "vobiz",
                })
    except Exception as exc:
        logger.error("parse_vobiz_webhook_messages error: %s", exc)
    return results


def _looks_like_vobiz_payload(payload: dict) -> bool:
    if not isinstance(payload, dict):
        return isinstance(payload, list)
    # Meta payloads always have an 'object' = 'whatsapp_business_account' field
    # and an 'entry' list. Anything else with 'event' or 'channel_id' is Vobiz.
    if payload.get("object") == "whatsapp_business_account" or "entry" in payload:
        return False
    if any(k in payload for k in ("event", "events", "channel_id", "data")):
        return True
    return False


def parse_webhook_messages(payload) -> list:
    """Dispatch to provider-specific parser. Auto-detects Meta vs Vobiz shape."""
    if _looks_like_vobiz_payload(payload):
        return parse_vobiz_webhook_messages(payload)
    return _parse_meta_webhook_messages(payload)


def _parse_meta_webhook_messages(payload: dict) -> list:
    """Extract list of inbound message dicts from a Meta webhook payload.

    Returns list of:
      {phone, message_id, message_type, text, raw, is_status_update}
    """
    results = []
    try:
        entries = payload.get("entry") or []
        for entry in entries:
            for change in (entry.get("changes") or []):
                value = change.get("value") or {}

                # Status updates (delivery/read receipts)
                for status in (value.get("statuses") or []):
                    results.append({
                        "phone": status.get("recipient_id", ""),
                        "message_id": status.get("id", ""),
                        "message_type": "status_update",
                        "text": "",
                        "raw": status,
                        "is_status_update": True,
                        "status_value": status.get("status", ""),
                        "timestamp": status.get("timestamp", ""),
                        "provider": "meta",
                    })

                # Contacts metadata
                contacts = value.get("contacts") or []
                contact_map = {}
                for c in contacts:
                    wa_id = c.get("wa_id", "")
                    name = (c.get("profile") or {}).get("name", "")
                    if wa_id:
                        contact_map[wa_id] = name

                # Messages
                for msg in (value.get("messages") or []):
                    phone_raw = msg.get("from", "")
                    # Normalize: Meta sends without +
                    phone = "+" + phone_raw if phone_raw and not phone_raw.startswith("+") else phone_raw
                    msg_id = msg.get("id", "")
                    msg_type = msg.get("type", "text")
                    contact_name = contact_map.get(phone_raw, "")

                    text = ""
                    media_url = ""
                    media_id = ""
                    mime_type = ""
                    file_name = ""
                    caption = ""
                    location = {}
                    if msg_type == "text":
                        text = (msg.get("text") or {}).get("body", "")
                    elif msg_type in ("button", "interactive"):
                        inter = msg.get("interactive") or {}
                        btn = inter.get("button_reply") or inter.get("list_reply") or {}
                        text = btn.get("title", "") or btn.get("id", "")
                    elif msg_type == "image":
                        media = msg.get("image") or {}
                        media_id = media.get("id", "")
                        mime_type = media.get("mime_type", "")
                        caption = media.get("caption", "")
                        text = caption or "[image received]"
                        media_url = media.get("url", "") or _meta_media_proxy_url(media_id)
                    elif msg_type in ("audio", "voice"):
                        media = msg.get("audio") or {}
                        media_id = media.get("id", "")
                        mime_type = media.get("mime_type", "")
                        msg_type = "voice" if msg_type == "voice" or media.get("voice") else "audio"
                        text = "[voice message received]" if msg_type == "voice" else "[audio received]"
                        media_url = media.get("url", "") or _meta_media_proxy_url(media_id)
                    elif msg_type == "document":
                        media = msg.get("document") or {}
                        media_id = media.get("id", "")
                        mime_type = media.get("mime_type", "")
                        file_name = media.get("filename", "") or media.get("file_name", "")
                        caption = media.get("caption", "")
                        text = caption or f"[document received: {file_name or 'file'}]"
                        media_url = media.get("url", "") or _meta_media_proxy_url(media_id)
                    elif msg_type == "video":
                        text = "[Video received]"
                    elif msg_type == "location":
                        loc = msg.get("location") or {}
                        latitude = loc.get("latitude")
                        longitude = loc.get("longitude")
                        name = str(loc.get("name") or "").strip()
                        address = str(loc.get("address") or "").strip()
                        location_url = ""
                        if latitude is not None and longitude is not None:
                            location_url = f"https://maps.google.com/?q={latitude},{longitude}"
                        location = {
                            "latitude": latitude,
                            "longitude": longitude,
                            "name": name,
                            "address": address,
                            "url": location_url,
                        }
                        text = address or name or "Location pin shared"
                    else:
                        text = f"[unsupported message received: {msg_type}]"

                    results.append({
                        "phone": phone,
                        "message_id": msg_id,
                        "message_type": msg_type,
                        "text": text,
                        "media_url": media_url,
                        "media_id": media_id,
                        "mime_type": mime_type,
                        "file_name": file_name,
                        "caption": caption,
                        "location": location,
                        "contact_name": contact_name,
                        "timestamp": msg.get("timestamp", ""),
                        "raw": msg,
                        "is_status_update": False,
                        "provider": "meta",
                    })
    except Exception as exc:
        logger.error("parse_webhook_messages error: %s", exc)
    return results


# ── Inbound intent detection (Fix 2) ───────────────────────────────────────

_CALLBACK_INTENT_PATTERNS = (
    "call me", "callback", "call back", "please call", "kindly call",
    "ring me", "phone me", "give me a call", "call now",
)
_APPOINTMENT_INTENT_PATTERNS = (
    "appointment", "meeting", "book demo", "schedule demo", "book a demo",
    "schedule a demo", "demo", "google meet", "zoom", "showroom visit",
    "showroom", "book a slot", "book slot", "book a meeting",
    "book service", "service booking", "schedule service",
    "reschedule", "re-schedule", "change my booking", "change appointment",
    "move my booking", "move appointment",
)
_APPOINTMENT_CONFIRM_WORDS = ("yes", "y", "ok", "okay", "done", "confirm", "confirmed", "book", "book it", "please book", "go ahead", "sure", "proceed")
_APPOINTMENT_CANCEL_WORDS = (
    "cancel", "stop", "never mind", "nevermind", "don't book", "do not book",
    "leave it", "forget it", "not now", "maybe later",
)
_BOOKING_TRANSIENT_STATES = {
    "awaiting_datetime", "alternatives_offered", "slot_unavailable", "booking_failed",
    "pending_confirmation", "awaiting_address", "awaiting_intake",
}
_APPOINTMENT_ACTIVE_STATUSES = {
    "awaiting_datetime", "alternatives_offered", "slot_unavailable",
    "pending_confirmation", "awaiting_address", "awaiting_intake",
}
_BOOKING_SESSION_MAX_AGE_HOURS = 6


def is_explicit_new_booking_intent(text: str) -> bool:
    clean = re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()
    return bool(re.search(
        r"\b(?:book|make|start|create|schedule)\b.{0,35}\b(?:new\s+)?(?:appointment|booking|service|servicing)\b|"
        r"\b(?:i\s+want|i(?:'d| would)\s+like)\s+to\s+book\b",
        clean,
    ))


def _booking_session_expired(state: dict, now: Optional[datetime] = None) -> bool:
    if str((state or {}).get("status") or "") not in _BOOKING_TRANSIENT_STATES:
        return False
    stamp = (state or {}).get("updated_at") or (state or {}).get("created_at")
    if not stamp:
        return False
    try:
        parsed = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            # Legacy booking states used datetime.now(), so a naive timestamp
            # represents the runtime's local clock, not the appointment zone.
            parsed = parsed.replace(tzinfo=datetime.now().astimezone().tzinfo or timezone.utc)
        current = now or datetime.now(ZoneInfo("Asia/Singapore"))
        if current.tzinfo is None:
            current = current.replace(tzinfo=ZoneInfo("Asia/Singapore"))
        return current.astimezone(timezone.utc) > parsed.astimezone(timezone.utc) + timedelta(hours=_BOOKING_SESSION_MAX_AGE_HOURS)
    except Exception:
        return True


def _reset_booking_session_state(state: dict) -> dict:
    """Keep durable customer facts while discarding one booking attempt."""
    durable_keys = {
        "customer_name", "contact_number", "service", "service_address", "postal_code",
        "property_type", "units", "problem", "intake_data", "location_latitude",
        "location_longitude", "location_name", "location_url",
    }
    return {key: value for key, value in (state or {}).items() if key in durable_keys and value not in (None, "", {}, [])}


def _detect_inbound_intent(text: str) -> Optional[str]:
    """Keyword-based intent detection for inbound WhatsApp messages.

    Returns 'callback', 'appointment', or None.
    Callback patterns take priority over appointment when both match.
    """
    if not text:
        return None
    lower = text.lower()
    for pat in _CALLBACK_INTENT_PATTERNS:
        if pat in lower:
            return "callback"
    for pat in _APPOINTMENT_INTENT_PATTERNS:
        if pat in lower:
            return "appointment"
    return None


def _conversation_appointment_state(conv: dict) -> dict:
    raw = (conv or {}).get("appointment_state") or "{}"
    if isinstance(raw, dict):
        return raw
    try:
        parsed = _json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _is_booking_cancel_reply(text: str) -> bool:
    normalized = re.sub(r"[^a-z0-9']+", " ", str(text or "").lower()).strip()
    return any(re.search(rf"\b{re.escape(phrase)}\b", normalized) for phrase in _APPOINTMENT_CANCEL_WORDS)


def _is_flexible_booking_time_reply(text: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()
    return any(phrase in normalized for phrase in (
        "any time", "anytime", "whenever", "suggest a time", "suggest me a time",
        "suggest me the time", "you suggest", "available time", "next available",
    ))


def _booking_intake_context_from_history(recent_messages: Optional[list], inbound_text: str) -> dict:
    """Recover a deterministic unit answer from the immediately preceding AI question."""
    units = _parse_booking_units(inbound_text, expecting_units=True)
    if not units:
        return {}
    explicit_units = bool(_parse_booking_units(inbound_text, expecting_units=False))
    history = list(recent_messages or [])
    for index in range(len(history) - 1, -1, -1):
        message = history[index] or {}
        if message.get("direction") != "outbound":
            continue
        question = str(message.get("message_text") or "").lower()
        if not ("how many" in question and re.search(r"\b(?:units?|air\s*con|aircon)\b", question)):
            if explicit_units:
                continue
            return {}
        problem = ""
        for earlier in range(index - 1, -1, -1):
            prior = history[earlier] or {}
            if prior.get("direction") == "inbound":
                candidate = _normalize_booking_text(prior.get("message_text"))
                if candidate and candidate.lower() != _normalize_booking_text(inbound_text).lower():
                    problem = candidate
                    break
        return {"units": units, "problem": problem}
    return {}


def _is_whatsapp_number_reference(text: str) -> bool:
    """Recognize replies that mean "use the number I am messaging from"."""
    normalized = re.sub(r"[^a-z0-9]+", " ", str(text or "").strip().lower()).strip()
    return normalized in {
        "same number", "this number", "my number", "current number",
        "whatsapp number", "same whatsapp number", "use this number",
        "use same number", "use my number",
    }


def _looks_like_service_address(text: str) -> bool:
    """Conservatively identify a service address without treating a time as one."""
    value = " ".join(str(text or "").strip().split())
    lower = value.lower()
    if len(value) < 6 or _is_whatsapp_number_reference(value):
        return False
    if re.fullmatch(r"\+?[\d\s()-]{8,18}", value):
        return False
    if re.search(r"\b(today|tomorrow|am|pm|morning|afternoon|evening)\b", lower):
        return False
    address_words = (
        "street", "st ", "road", "rd ", "avenue", "ave ", "lane", "walk",
        "drive", "dr ", "block", "blk", "unit", "floor", "building", "postal",
        "singapore", "sg ", "house", "apartment", "condo",
    )
    has_address_word = any(word in f"{lower} " for word in address_words)
    has_number_and_place = bool(re.search(r"\b\d{1,4}(?:[-/]\d{1,4})?\s*[,#-]?\s*[a-z]{3,}", lower))
    return has_address_word or has_number_and_place or ("," in value and bool(re.search(r"\d", value)) and bool(re.search(r"[a-z]", lower)))


def _is_appointment_confirmation_reply(text: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", " ", str(text or "").strip().lower()).strip()
    if not normalized:
        return False
    exact = {
        "yes", "yes please", "y", "ok", "okay", "sure",
        "confirm", "confirmed", "book", "book it", "please book",
        "go ahead", "proceed", "done",
    }
    if normalized in exact:
        return True
    return bool(re.fullmatch(r"(yes|ok|okay|sure|confirm|please book|book it|go ahead|proceed)( please)?", normalized))


def _normalize_booking_text(value) -> str:
    return " ".join(str(value or "").strip().split())


def _normalize_address_key(value) -> str:
    return re.sub(r"[^a-z0-9]", "", _normalize_booking_text(value).lower())


def _extract_postal_code(value) -> str:
    match = re.search(r"(?<!\d)(\d{6})(?!\d)", str(value or ""))
    return match.group(1) if match else ""


def _extract_service_address_from_booking_text(text: str) -> str:
    """Extract an address embedded inside a booking message.

    _looks_like_service_address intentionally rejects whole messages that also
    contain date/time words such as "tomorrow 3pm". Customers commonly send the
    date/time and address together, so this helper extracts only the address
    segment before running the conservative address check.
    """
    value = _normalize_booking_text(text)
    if not value or _is_whatsapp_number_reference(value):
        return ""

    candidates = []
    marker = re.search(
        r"\b(?:my\s+address|service\s+address|address|addr|location)\b\s*(?:is|:|-|,)?\s*(.+)$",
        value,
        re.IGNORECASE,
    )
    if marker:
        candidates.append(marker.group(1))

    unit_or_block = re.search(
        r"((?:#?\d{1,3}\s*[-/]\s*\d{1,4}|(?:blk|block)\s*\d+[a-z]?|"
        r"\d{1,4}[a-z]?\s*,?\s*\d{1,4}[a-z]?\s+"
        r"(?:[a-z0-9'-]+\s+){0,8}"
        r"(?:street|st|road|rd|avenue|ave|lane|walk|drive|dr|close|crescent|place|sumang)\b)"
        r".*)",
        value,
        re.IGNORECASE,
    )
    if unit_or_block:
        candidates.append(unit_or_block.group(1))

    for candidate in candidates:
        cleaned = _normalize_booking_text(re.sub(r"^(?:is|at|,|:|-)+\s*", "", str(candidate or ""), flags=re.IGNORECASE))
        if _looks_like_service_address(cleaned):
            return cleaned
        if _extract_postal_code(cleaned) and re.search(r"[a-z]", cleaned, re.IGNORECASE):
            return cleaned
    if _looks_like_service_address(value):
        return value
    return ""


def _booking_known_fields(crm_contact: Optional[dict] = None, appointment_state: Optional[dict] = None, appointment: Optional[dict] = None) -> dict:
    """Merge durable booking facts without allowing empty values to erase them."""
    crm, state, appt = crm_contact or {}, appointment_state or {}, appointment or {}
    intake = {}
    for source in (appt.get("intake_data"), state.get("intake_data")):
        if isinstance(source, dict):
            intake.update({k: v for k, v in source.items() if v not in (None, "", [])})
    address = _first_value(state.get("service_address"), appt.get("service_address"), crm.get("address"), crm.get("location"))
    return {
        "customer_name": _first_value(state.get("customer_name"), appt.get("name"), crm.get("lead_name"), crm.get("name")),
        "phone": _first_value(state.get("contact_number"), appt.get("phone"), crm.get("phone_number")),
        "service": _first_value(state.get("service"), appt.get("service"), crm.get("service_type"), crm.get("requirement")),
        "units": _first_value(state.get("units"), intake.get("units")),
        "problem": _first_value(state.get("problem"), intake.get("problem"), intake.get("service_summary"), state.get("service"), appt.get("service"), crm.get("service_type"), crm.get("requirement")),
        "service_address": address,
        "postal_code": _first_value(state.get("postal_code"), intake.get("postal_code"), _extract_postal_code(address)),
        "property_type": _first_value(state.get("property_type"), intake.get("property_type")),
        "preferred_date": _first_value(state.get("requested_date"), appt.get("date")),
        "preferred_time": _first_value(state.get("requested_time"), str(appt.get("time") or "")[:5]),
        "assigned_staff": _first_value(state.get("staff_name"), appt.get("staff_name"), crm.get("assigned_to")),
        "appointment_id": _first_value(state.get("booking_id"), state.get("existing_appointment_id"), appt.get("id")),
        "intake_data": intake,
    }


def get_missing_booking_fields(crm_contact: Optional[dict] = None, appointment_state: Optional[dict] = None, appointment: Optional[dict] = None, *, delivery_mode: str = "onsite", require_intake: bool = False) -> list:
    """Canonical missing-field calculation from persisted CRM + appointment state."""
    known = _booking_known_fields(crm_contact, appointment_state, appointment)
    ordered = ["service"]
    if require_intake:
        ordered.extend(["units", "problem"])
    if str(delivery_mode or "onsite").lower() == "onsite":
        ordered.append("service_address")
    if require_intake:
        ordered.append("property_type")
    ordered.extend(["preferred_date", "preferred_time"])
    return [field for field in ordered if not known.get(field)]


_BOOKING_NUMBER_WORDS = {
    "one": "1", "a": "1", "single": "1",
    "two": "2", "three": "3", "four": "4", "five": "5",
    "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10",
}


def _parse_booking_units(text: str, *, expecting_units: bool = False) -> str:
    lower = str(text or "").strip().lower()
    if not lower:
        return ""
    has_unit_marker = bool(re.search(r"\b(?:units?|air\s*con|aircon|ac|fcu|fcus)\b", lower))
    if re.search(r"\b(?:am|pm|morning|afternoon|evening|today|tomorrow)\b", lower) and not has_unit_marker:
        return ""
    numeric = re.search(r"\b(\d{1,2})\s*(?:air\s*con|aircon|ac)?\s*(?:units?|fcu|fcus)\b", lower)
    if numeric:
        return numeric.group(1)
    word = re.search(r"\b(one|two|three|four|five|six|seven|eight|nine|ten|single|a)\s*(?:air\s*con|aircon|ac)?\s*(?:units?|fcu|fcus)\b", lower)
    if word:
        return _BOOKING_NUMBER_WORDS.get(word.group(1), "")
    if expecting_units:
        exact_number = re.fullmatch(r"\s*(\d{1,2})\s*", lower)
        if exact_number:
            return exact_number.group(1)
        exact_word = re.fullmatch(r"\s*(one|two|three|four|five|six|seven|eight|nine|ten|single|a)\s*", lower)
        if exact_word:
            return _BOOKING_NUMBER_WORDS.get(exact_word.group(1), "")
    return ""


def _extract_booking_intake_updates(text: str, state: Optional[dict], missing_fields: Optional[list] = None) -> dict:
    lower = str(text or "").strip().lower()
    summary = " ".join(str(text or "").strip().split())
    missing = set(missing_fields or [])
    updates = {}
    property_match = re.search(r"\b(hdb|condo(?:minium)?|landed|commercial|office|shop|warehouse)\b", lower)
    if property_match:
        value = property_match.group(1)
        updates["property_type"] = value.upper() if value == "hdb" else value.title()
    units = _parse_booking_units(text, expecting_units="units" in missing)
    if units:
        updates["units"] = units
    if summary and not property_match and not units and len(summary) >= 4 and not _looks_like_service_address(summary):
        datetime_probe = merge_booking_datetime(state or {}, summary)
        if not (datetime_probe.get("date_extracted") or datetime_probe.get("time_extracted") or datetime_probe.get("ambiguous_time")):
            updates["problem"] = summary
            updates["service_summary"] = summary
    return updates


def _next_booking_missing_field_prompt(missing_fields: list, appointment_name: str = "appointment") -> str:
    if "service" in missing_fields:
        return f"What service do you need for this {appointment_name.lower()}?"
    if "service_address" in missing_fields:
        return "Please share the full service address, including the block or unit and postal code if available. I will use this WhatsApp number as your contact."
    if "property_type" in missing_fields and "units" in missing_fields:
        return "What is the property type: HDB, Condo, Landed, or Commercial? Also, how many units need servicing?"
    if "property_type" in missing_fields:
        return "What is the property type: HDB, Condo, Landed, or Commercial?"
    if "units" in missing_fields:
        return "How many aircon units need servicing?"
    if "problem" in missing_fields:
        return "What service issue or reason should we note for the booking?"
    if "preferred_date" in missing_fields or "preferred_time" in missing_fields:
        return f"Please share your preferred date and time for the {appointment_name.lower()}, for example: tomorrow at 2 PM."
    return f"Please reply CONFIRM so I can complete this {appointment_name.lower()} booking."


def _booking_confirmation_prompt(state: dict, crm_contact: Optional[dict], appointment_name: str = "appointment", staff_label: str = "staff member") -> str:
    known = _booking_known_fields(crm_contact, state)
    lines = [
        f"Date: {known.get('preferred_date') or '-'}",
        f"Time: {str(known.get('preferred_time') or '-')[:5]}",
        f"Service: {known.get('service') or '-'}",
        f"Units: {known.get('units') or '-'}",
        f"Address: {known.get('service_address') or '-'}",
        f"Property type: {known.get('property_type') or '-'}",
    ]
    if known.get("assigned_staff"):
        lines.append(f"{staff_label.title()}: {known.get('assigned_staff')}")
    return f"Shall I confirm your {appointment_name.lower()} with these details?\n" + "\n".join(lines) + "\n\nReply CONFIRM to book this appointment."


_UNVERIFIED_BOOKING_CLAIM_RE = re.compile(
    r"\b(?:booking|appointment)\s+(?:request\s+)?(?:has\s+been\s+|is\s+)?"
    r"(?:received|recorded|created|booked|confirmed|scheduled)\b|"
    r"\b(?:your|the)\s+(?:booking|appointment)\s+(?:is|has been)\s+"
    r"(?:booked|confirmed|scheduled|created)\b",
    re.IGNORECASE,
)


def _guard_unpersisted_booking_claim(reply_text: str, conversation: Optional[dict], crm_contact: Optional[dict] = None) -> tuple[str, bool]:
    """Never let generative text imply a DB booking that does not exist.

    Actual booking confirmations are emitted by the deterministic appointment
    transaction and do not pass through this AI-output guard.
    """
    reply = str(reply_text or "").strip()
    state = _conversation_appointment_state(conversation or {})
    known = _booking_known_fields(crm_contact, state)
    persisted_id = _first_value(state.get("booking_id"), state.get("appointment_id"), state.get("existing_appointment_id"))
    terminal_status = str(state.get("status") or "").strip().lower() in {"booked", "confirmed", "rescheduled"}
    if not reply or persisted_id or terminal_status or not _UNVERIFIED_BOOKING_CLAIM_RE.search(reply):
        return reply, False
    if known.get("preferred_date") and known.get("preferred_time"):
        try:
            dt = datetime.strptime(f"{known['preferred_date']} {str(known['preferred_time'])[:5]}", "%Y-%m-%d %H:%M")
            slot = f"{dt.strftime('%d %b %Y')} at {dt.strftime('%I:%M %p').lstrip('0')}"
        except Exception:
            slot = _format_demo_slot(str(known["preferred_date"]), str(known["preferred_time"]))
        return (
            f"I have your request details for {slot}, but the appointment has not been created yet. "
            "Please reply CONFIRM so I can complete the booking, subject to current availability.",
            True,
        )
    return (
        "I have your service enquiry, but no appointment has been created yet. "
        "Please share your preferred date and time so I can check availability and continue with the requested booking details.",
        True,
    )


def _restore_booking_state(state: dict, crm_contact: Optional[dict], appointment: Optional[dict] = None) -> dict:
    known = _booking_known_fields(crm_contact, state, appointment)
    restored = dict(state or {})
    mapping = {
        "customer_name": "customer_name", "phone": "contact_number", "service": "service",
        "service_address": "service_address", "postal_code": "postal_code", "property_type": "property_type",
        "preferred_date": "requested_date", "preferred_time": "requested_time",
        "assigned_staff": "staff_name", "appointment_id": "booking_id",
    }
    for source, target in mapping.items():
        if known.get(source) and not restored.get(target):
            restored[target] = known[source]
    restored_intake = dict(known.get("intake_data") or {})
    for key in ("units", "problem", "property_type", "postal_code"):
        if known.get(key):
            restored_intake[key] = known[key]
    if restored_intake:
        restored["intake_data"] = restored_intake
    return restored


async def _persist_whatsapp_service_address(phone: str, address: str, postal_code: str = "") -> bool:
    """Idempotently save normalized address to CRM without logging customer data."""
    normalized = _normalize_booking_text(address)
    try:
        from db import update_crm_contact_full
        saved = await update_crm_contact_full(phone, {"address": normalized})
        await _diagnostic_event("booking_field_persisted", phone=phone, field="service_address", target="crm", success=bool(saved))
        if not saved:
            return False
    except Exception as exc:
        await _diagnostic_event("booking_field_persisted", phone=phone, field="service_address", target="crm", success=False, level="warning", exception_type=type(exc).__name__)
        return False
    await _remember_whatsapp_insight(phone, "Service address confirmed")
    return True


async def _persist_whatsapp_booking_to_crm(phone: str, lead_name: str, state: dict, booked: dict, service_name: str, booking_id: str, action_text: str = "booked") -> None:
    """Keep CRM and durable memory aligned with WhatsApp booking state."""
    try:
        from db import update_crm_contact_full, update_lead_journey
        known = _booking_known_fields({}, state, booked)
        updates = {
            "lead_name": lead_name or (booked or {}).get("name") or "AirZen Lead",
            "source": "WhatsApp",
            "service_type": service_name or known.get("service") or "",
            "requirement": service_name or known.get("problem") or "",
            "address": known.get("service_address") or "",
        }
        await update_crm_contact_full(phone, {k: v for k, v in updates.items() if v})
        await update_lead_journey(phone, {
            "journey_stage": "demo_booked",
            "crm_status": "demo_booked",
            "preferred_channel": "whatsapp",
            "last_intent": "appointment_booked",
            "last_customer_reply_at": datetime.now().isoformat(),
        })
        await _diagnostic_event("crm_booking_persisted", phone=phone, success=True, has_address=bool(updates.get("address")))
    except Exception as exc:
        await _diagnostic_event("crm_booking_persisted", phone=phone, success=False, level="warning", exception_type=type(exc).__name__)
    try:
        known = _booking_known_fields({}, state, booked)
        memory = (
            f"Appointment {action_text}: {known.get('preferred_date') or (booked or {}).get('date') or ''} "
            f"{str(known.get('preferred_time') or (booked or {}).get('time') or '')[:5]}; "
            f"service={service_name or known.get('service') or ''}; "
            f"units={known.get('units') or ''}; property_type={known.get('property_type') or ''}; "
            f"address_confirmed={bool(known.get('service_address'))}; booking_id={booking_id}"
        )
        await _remember_whatsapp_insight(phone, memory)
    except Exception as exc:
        await _diagnostic_event("crm_booking_memory_failed", phone=phone, level="warning", exception_type=type(exc).__name__)


def _appointment_local_now(tz_name: str = "Asia/Singapore") -> datetime:
    try:
        tz = ZoneInfo(str(tz_name or "Asia/Singapore"))
    except Exception:
        tz = ZoneInfo("Asia/Singapore")
    return datetime.now(tz).replace(tzinfo=None)


def _appointment_local_dt(date_s: str, time_s: str, tz_name: str = "Asia/Singapore") -> Optional[datetime]:
    try:
        return datetime.strptime(f"{date_s} {str(time_s or '')[:5]}", "%Y-%m-%d %H:%M")
    except Exception:
        return None


def _parse_whatsapp_requested_datetime(text: str, appointment_context: bool = False, reference_time=None) -> Optional[dict]:
    lower = (text or "").lower().strip()
    has_time_context = appointment_context or bool(
        re.search(r"\b(today|tomorrow|morning|afternoon|evening|night|demo|appointment|meeting|meet|book|schedule|call|message)\b", lower)
        or re.search(r"\b\d{1,2}:\d{2}\b", lower)
        or re.search(r"\b\d{1,2}\s*(am|pm)\b", lower)
    )
    if not has_time_context:
        return None
    singapore_tz = ZoneInfo("Asia/Singapore")
    if reference_time:
        try:
            base_aware = reference_time if isinstance(reference_time, datetime) else datetime.fromisoformat(str(reference_time).replace("Z", "+00:00"))
            if base_aware.tzinfo is None:
                base_aware = base_aware.replace(tzinfo=singapore_tz)
            base = base_aware.astimezone(singapore_tz).replace(tzinfo=None)
        except Exception:
            base = _appointment_local_now("Asia/Singapore")
    else:
        base = _appointment_local_now("Asia/Singapore")
    target_date = base.date()
    time_text = lower
    if "tomorrow" in lower:
        target_date = (base + timedelta(days=1)).date()
    elif "today" in lower:
        target_date = base.date()
    else:
        explicit_date = re.search(r"\b(\d{1,2})\s+(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)(?:\s+(\d{4}))?\b", lower)
        if explicit_date:
            month = datetime.strptime(explicit_date.group(2)[:3], "%b").month
            year = int(explicit_date.group(3) or base.year)
            try:
                target_date = datetime(year, month, int(explicit_date.group(1))).date()
                if not explicit_date.group(3) and target_date < base.date():
                    target_date = datetime(year + 1, month, int(explicit_date.group(1))).date()
                time_text = lower[:explicit_date.start()] + " " + lower[explicit_date.end():]
            except ValueError:
                return None
        day_names = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
        for idx, name in enumerate(day_names):
            if name in lower or name[:3] in lower:
                days_ahead = (idx - base.weekday()) % 7
                if days_ahead == 0:
                    days_ahead = 7
                target_date = (base + timedelta(days=days_ahead)).date()
                break

    candidates = []
    seen = set()
    global_pm = bool(re.search(r"\bpm\b", lower)) and not bool(re.search(r"\bam\b", lower))
    context_pm = any(word in lower for word in ("afternoon", "evening", "night"))
    for match in re.finditer(r"(?<!\d)(\d{1,2})(?::(\d{2}))?\s*(am|pm)?(?!\d)", time_text):
        hour = int(match.group(1))
        minute = int(match.group(2) or 0)
        meridiem = match.group(3)
        if hour > 23 or minute > 59:
            continue
        if meridiem == "pm" and hour < 12:
            hour += 12
        elif meridiem == "am" and hour == 12:
            hour = 0
        elif not meridiem:
            if (context_pm or global_pm) and hour < 12:
                hour += 12
            elif target_date == base.date() and 1 <= hour <= 7:
                afternoon_hour = hour + 12
                if datetime.combine(target_date, datetime.min.time()).replace(hour=afternoon_hour, minute=minute) > base:
                    hour = afternoon_hour
        scheduled = datetime.combine(target_date, datetime.min.time()).replace(hour=hour, minute=minute)
        if scheduled < base and "today" not in lower and "tomorrow" not in lower:
            scheduled += timedelta(days=1)
        key = scheduled.strftime("%Y-%m-%d %H:%M")
        if key in seen:
            continue
        seen.add(key)
        candidates.append({
            "requested_date": scheduled.strftime("%Y-%m-%d"),
            "requested_time": scheduled.strftime("%H:%M"),
            "parsed_scheduled_at": scheduled.isoformat(),
        })
    if not candidates:
        return None
    scheduled = datetime.fromisoformat(candidates[0]["parsed_scheduled_at"])
    return {
        "requested_date": scheduled.strftime("%Y-%m-%d"),
        "requested_time": scheduled.strftime("%H:%M"),
        "parsed_scheduled_at": scheduled.isoformat(),
        "candidate_slots": candidates,
        "multi_slot_request": len(candidates) > 1,
        "resolved_timezone": "Asia/Singapore",
    }


def merge_booking_datetime(existing_state: Optional[dict], inbound_text: str, inbound_timestamp=None) -> dict:
    """Merge partial date/time input without clearing persisted components."""
    state = existing_state or {}
    lower = " ".join(str(inbound_text or "").strip().lower().split())
    existing_date = str(state.get("requested_date") or "").strip()
    existing_time = str(state.get("requested_time") or "").strip()[:5]
    date_explicit = bool(
        re.search(r"\b(today|tomorrow|next\s+(?:mon|tue|wed|thu|fri|sat|sun)|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", lower)
        or re.search(r"\b\d{1,2}\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*(?:\s+\d{4})?\b", lower)
    )
    ambiguous = re.fullmatch(r"(?:at|around)?\s*(\d{1,2})", lower)
    if ambiguous and 1 <= int(ambiguous.group(1)) <= 12:
        return {
            "requested_date": existing_date, "requested_time": existing_time,
            "date_extracted": False, "time_extracted": False,
            "ambiguous_time": int(ambiguous.group(1)), "complete": bool(existing_date and existing_time),
            "parsed": None,
        }
    time_explicit = bool(
        re.search(r"\b\d{1,2}(?::\d{2})?\s*(?:am|pm)\b", lower)
        or re.search(r"\b(?:[01]?\d|2[0-3]):[0-5]\d\b", lower)
    )
    parsed = _parse_whatsapp_requested_datetime(
        inbound_text,
        appointment_context=True,
        reference_time=inbound_timestamp,
    ) if time_explicit else None
    new_date = existing_date
    new_time = existing_time
    if parsed and time_explicit:
        new_time = parsed.get("requested_time") or existing_time
        if date_explicit or not existing_date:
            new_date = parsed.get("requested_date") or existing_date
    elif date_explicit:
        date_probe = _parse_whatsapp_requested_datetime(
            f"{inbound_text} 12:00",
            appointment_context=True,
            reference_time=inbound_timestamp,
        )
        if date_probe:
            new_date = date_probe.get("requested_date") or existing_date
    merged_parsed = None
    if new_date and new_time:
        scheduled = datetime.strptime(f"{new_date} {new_time}", "%Y-%m-%d %H:%M")
        merged_parsed = {
            "requested_date": new_date,
            "requested_time": new_time,
            "parsed_scheduled_at": scheduled.isoformat(),
            "candidate_slots": [{
                "requested_date": new_date,
                "requested_time": new_time,
                "parsed_scheduled_at": scheduled.isoformat(),
            }],
            "multi_slot_request": False,
            "resolved_timezone": "Asia/Singapore",
        }
    return {
        "requested_date": new_date,
        "requested_time": new_time,
        "date_extracted": date_explicit and new_date != existing_date,
        "time_extracted": time_explicit and bool(new_time),
        "ambiguous_time": None,
        "complete": bool(new_date and new_time),
        "parsed": merged_parsed,
    }


def _format_demo_slot(date_str: str, time_str: str) -> str:
    try:
        dt = datetime.strptime(f"{date_str} {time_str[:5]}", "%Y-%m-%d %H:%M")
        day = "tomorrow" if dt.date() == (_appointment_local_now() + timedelta(days=1)).date() else dt.strftime("%d %b %Y")
        return f"{day} at {dt.strftime('%I:%M %p').lstrip('0')}"
    except Exception:
        return f"{date_str} at {time_str}"


def _appointment_booking_failure_message(error: Exception, appointment_name: str = "appointment") -> str:
    """Return a specific, customer-safe booking failure without database details."""
    detail = str(error or "").strip().lower()
    label = str(appointment_name or "appointment").strip().lower()
    if "no staff available" in detail:
        return f"No staff member is available for that {label} slot. Please choose another time."
    if "outside working hours" in detail:
        return "That time is outside our working hours. Please choose a time during business hours."
    if "too soon" in detail or "in the past" in detail:
        return "That time is too soon or has already passed. Please choose a later time."
    if "too far ahead" in detail:
        return "That date is outside our booking window. Please choose an earlier date."
    if "slot is unavailable" in detail or "already exists" in detail or "conflict" in detail:
        return "That slot is no longer available. Please choose another time."
    if "invalid appointment date or time" in detail or "malformed" in detail:
        return "I could not understand that date or time. Please send the date and time again, for example: tomorrow at 3 PM."
    return "Sorry, I could not complete the booking right now. Our team will check it and confirm shortly."


def _booking_status_for_missing_fields(missing_fields: list) -> str:
    if any(field in missing_fields for field in ("units", "problem", "property_type")):
        return "awaiting_intake"
    if "service_address" in missing_fields:
        return "awaiting_address"
    if "preferred_date" in missing_fields or "preferred_time" in missing_fields:
        return "awaiting_datetime"
    return "pending_confirmation"


def _booking_recovery_prompt(state: dict, crm_contact: Optional[dict], appointment_name: str, staff_label: str, delivery_mode: str, intake_prompt: str = "") -> tuple[str, dict, list]:
    missing = get_missing_booking_fields(
        crm_contact,
        state,
        delivery_mode=delivery_mode,
        require_intake=bool(intake_prompt),
    )
    recovered = {**(state or {}), "status": _booking_status_for_missing_fields(missing), "updated_at": datetime.now().isoformat()}
    if missing:
        reply = _next_booking_missing_field_prompt(missing, appointment_name)
    else:
        reply = _booking_confirmation_prompt(recovered, crm_contact, appointment_name, staff_label)
    return reply, recovered, missing


async def _send_and_save_whatsapp_ai_text(phone: str, conv_id: str, reply_text: str, inbound_saved: Optional[dict], raw_payload: Optional[dict] = None) -> dict:
    await _diagnostic_event("response_generated", phone=phone, conversation_id=conv_id, source="appointment_or_fallback")
    await _log_whatsapp_ai_event(phone, "whatsapp_text_send_started", f"chars={len(reply_text)}")
    send_result = await send_whatsapp_text(phone, reply_text)
    provider_id = send_result.get("provider_message_id") or ""
    if send_result.get("success"):
        await _log_whatsapp_ai_event(phone, "whatsapp_text_send_success", f"provider_message_id={provider_id}")
    else:
        await _log_whatsapp_ai_event(phone, "whatsapp_text_send_failed", send_result.get("error") or "", "error")
    payload = {"reply_to_message_id": inbound_saved.get("id") if inbound_saved else "", "send_result": send_result}
    payload.update(raw_payload or {})
    await save_wa_message(
        conv_id=conv_id,
        phone=phone,
        direction="outbound",
        message_type="text",
        message_text=reply_text,
        provider_message_id=provider_id,
        provider_status="sent" if send_result.get("success") else "failed",
        failure_reason="" if send_result.get("success") else (send_result.get("reason") or send_result.get("error") or ""),
        raw_payload=payload,
        ai_generated=True,
    )
    await _log_whatsapp_ai_event(phone, "outbound_ai_message_saved", f"provider_status={'sent' if send_result.get('success') else 'failed'}")
    if send_result.get("success"):
        await update_conversation_last_message(conv_id, reply_text, increment_unread=False)
    return send_result


async def _suggest_available_demo_slots(start_date: str, start_time: str, count: int = 3) -> list:
    try:
        from db import get_appointment_settings, get_slot_availability
        settings = await get_appointment_settings()
    except Exception:
        return []
    try:
        cursor = datetime.strptime(f"{start_date} {start_time[:5]}", "%Y-%m-%d %H:%M")
    except Exception:
        cursor = datetime.now()
    interval = max(int(settings.get("slot_interval_minutes") or 45), 5)
    slots = []
    # This helper runs before the customer receives a reply.  The previous loop
    # could issue 8,640 sequential database calls (30 days at five-minute
    # granularity), which explains multi-hour replies after an unavailable slot.
    # Keep recommendations best-effort and bounded; the main requested-slot
    # decision remains authoritative.
    max_checks = 32
    deadline = asyncio.get_running_loop().time() + 5.0
    for _ in range(max_checks):
        cursor += timedelta(minutes=interval)
        date_s, time_s = cursor.strftime("%Y-%m-%d"), cursor.strftime("%H:%M")
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            break
        try:
            availability = await asyncio.wait_for(
                get_slot_availability(date_s, time_s), timeout=min(1.0, remaining)
            )
        except Exception:
            break
        if availability.get("available"):
            slots.append({"date": date_s, "time": time_s, "label": cursor.strftime("%I:%M %p").lstrip("0")})
            if len(slots) >= count:
                break
    return slots


def _appointment_confirmation_text(customer_name: str, appointment: dict, staff_name: str, duration_minutes: int, meet_link: str = "", appointment_name: str = "Appointment", delivery_mode: str = "onsite", staff_label: str = "staff member") -> str:
    slot = _format_demo_slot(appointment.get("date", ""), appointment.get("time", ""))
    if delivery_mode == "online":
        next_step = meet_link or "The meeting link will be shared shortly."
    elif delivery_mode == "phone":
        next_step = "Our team will call you at the booked time."
    elif delivery_mode == "onsite":
        next_step = f"Our {staff_label.lower()} will attend the service location or contact you to confirm it."
    else:
        next_step = "Our team will confirm the appointment format and details."
    return (
        f"Done, your {appointment_name.lower()} is booked for {slot} with {staff_name}. "
        f"Duration: {duration_minutes} minutes. {next_step}"
    )


def _service_start_code_customer_instruction(code: str, staff_label: str = "staff member") -> str:
    code = str(code or "").strip()
    if not re.fullmatch(r"\d{6}", code):
        return ""
    return (
        f"Service start code: {code}\n"
        f"Give this code to the assigned {staff_label.lower()} only after they arrive and show their AirZen staff app."
    )


def _appointment_internal_notification_text(customer_name: str, phone: str, company: str, appointment: dict, staff_name: str, meet_link: str = "") -> str:
    slot = _format_demo_slot(appointment.get("date", ""), appointment.get("time", ""))
    return (
        "New appointment booked ✅\n"
        f"Customer: {customer_name}\n"
        f"Phone: {phone}\n"
        f"Company: {company or 'Not provided'}\n"
        f"Time: {slot}\n"
        f"Assigned to: {staff_name}\n"
        "Source: WhatsApp AI\n"
        f"Meet link: {meet_link or 'Will be shared shortly'}"
    )


async def _send_telegram_appointment_notification(message: str) -> dict:
    try:
        from db import get_setting
        enabled = (await get_setting("TELEGRAM_NOTIFICATIONS_ENABLED", "false") or "false").strip().lower() in {"1", "true", "yes", "on"}
        token = (await get_setting("TELEGRAM_BOT_TOKEN", "") or "").strip()
        chat_id = (await get_setting("TELEGRAM_CHAT_ID", "") or "").strip()
    except Exception as exc:
        await _db().log_error("appointments", "telegram_notification_failed", str(exc)[:500], "error")
        return {"success": False, "error": str(exc)[:500], "reason": "settings_error"}
    if not (enabled and token and chat_id):
        await _db().log_error("appointments", "telegram_not_configured", "Telegram notifications disabled or missing TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID", "warning")
        return {"success": False, "error": "telegram_not_configured", "reason": "telegram_not_configured"}
    await _db().log_error("appointments", "telegram_notification_started", f"chat_id={chat_id}", "info")
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat_id, "text": message}) as resp:
                data = await resp.json(content_type=None)
                if resp.status in (200, 201) and data.get("ok", True):
                    await _db().log_error("appointments", "telegram_notification_sent", f"chat_id={chat_id}", "info")
                    return {"success": True}
                err = str(data)[:500]
                await _db().log_error("appointments", "telegram_notification_failed", f"http_status={resp.status} error={err}", "error")
                return {"success": False, "error": err, "reason": "telegram_send_failed"}
    except Exception as exc:
        await _db().log_error("appointments", "telegram_notification_failed", str(exc)[:500], "error")
        return {"success": False, "error": str(exc)[:500], "reason": "telegram_send_failed"}


def _format_appointment_datetime_for_staff(appointment: dict) -> str:
    tz_name = (
        appointment.get("timezone")
        or appointment.get("appointment_timezone")
        or appointment.get("tz")
        or "Asia/Singapore"
    )
    try:
        tz = ZoneInfo(str(tz_name))
    except Exception:
        tz = ZoneInfo("Asia/Singapore")

    date_s = str(appointment.get("date") or appointment.get("appointment_date") or "").strip()
    time_s = str(appointment.get("time") or appointment.get("appointment_time") or "").strip()[:5]
    try:
        local_dt = datetime.strptime(f"{date_s} {time_s}", "%Y-%m-%d %H:%M").replace(tzinfo=tz)
        return local_dt.strftime("%d %b %Y, %I:%M %p").replace(", 0", ", ")
    except Exception:
        return _first_value(
            appointment.get("appointment_datetime"),
            f"{date_s} {time_s}".strip(),
            fallback="the scheduled time",
        )


async def send_staff_appointment_notification(appointment: dict, *, source: str = "", update_appointment: bool = True) -> dict:
    """Notify the assigned staff member about a booked appointment via approved WhatsApp template."""
    appointment = appointment or {}
    appointment_id = appointment.get("id") or appointment.get("appointment_id") or ""
    customer_phone = appointment.get("phone") or appointment.get("customer_phone") or ""
    customer_name = _first_value(
        appointment.get("name"),
        appointment.get("customer_name"),
        appointment.get("lead_name"),
        fallback="Customer",
    )
    service = _first_value(
        appointment.get("service"),
        appointment.get("service_type"),
        appointment.get("requirement"),
        fallback="Appointment",
    )
    source_label = _first_value(
        source,
        appointment.get("source"),
        appointment.get("lead_source"),
        appointment.get("source_label"),
        fallback="WhatsApp",
    )

    async def finish(success: bool, reason: str = "", error: str = "", provider_message_id: str = "") -> dict:
        if update_appointment and appointment_id:
            try:
                from db import update_appointment_notifications
                await update_appointment_notifications(appointment_id, {
                    "staff_notified": bool(success),
                    "notification_error": "" if success else (error or reason),
                    "staff_notification_message_id": provider_message_id if success else "",
                    "staff_notification_sent_at": datetime.now(timezone.utc).isoformat() if success else None,
                })
            except Exception as exc:
                await _db().log_error("appointments", "staff_template_failed", f"appointment_id={appointment_id}; update_failed={str(exc)[:300]}", "warning")
        return {
            "success": bool(success),
            "reason": reason,
            "error": error,
            "provider_message_id": provider_message_id,
            "template_purpose": "staff_appointment_notification_template",
        }

    await _db().log_error(
        "appointments",
        "staff_notification_started",
        f"appointment_id={appointment_id}; customer_phone={customer_phone}; staff_id={appointment.get('staff_id') or ''}",
        "info",
    )

    staff_phone = ""
    try:
        from db import get_appointment_staff, normalize_phone
        staff_id = appointment.get("staff_id") or ""
        staff = next((s for s in await get_appointment_staff(include_inactive=True) if s.get("id") == staff_id), {})
        staff_phone_raw = staff.get("whatsapp_number") or appointment.get("staff_whatsapp_number") or ""
        if staff_phone_raw:
            staff_phone = normalize_phone(staff_phone_raw)
    except Exception as exc:
        await _db().log_error("appointments", "staff_template_failed", f"appointment_id={appointment_id}; staff_lookup={str(exc)[:500]}", "error")
        return await finish(False, "staff_lookup_failed", str(exc)[:500])

    if not staff_phone:
        await _db().log_error("appointments", "staff_whatsapp_missing", f"appointment_id={appointment_id}; staff_id={appointment.get('staff_id') or ''}", "warning")
        return await finish(False, "staff_whatsapp_missing", "Assigned staff WhatsApp number is missing")

    template = await resolve_wa_template("staff_appointment_notification_template")
    if not template:
        await _db().log_error("appointments", "staff_template_missing", f"appointment_id={appointment_id}; purpose=staff_appointment_notification_template", "warning")
        return await finish(False, "staff_template_missing", "staff_appointment_notification_template is not configured")

    params = [
        customer_name,
        service,
        str(appointment.get("date") or appointment.get("appointment_date") or ""),
        str(appointment.get("time") or appointment.get("appointment_time") or "")[:5],
    ]
    language = await _get_wa_setting("WHATSAPP_DEFAULT_LANGUAGE") or "en"
    result = await send_whatsapp_template(
        staff_phone,
        template,
        language,
        params,
        event_type="staff_appointment_notification",
        source_type="appointment",
        source_id=appointment_id or customer_phone,
        template_purpose="staff_appointment_notification_template",
        template_context={
            **appointment,
            "customer_name": customer_name,
            "customer_phone": customer_phone,
            "service_type": service,
            "appointment_date": str(appointment.get("date") or appointment.get("appointment_date") or ""),
            "appointment_time": str(appointment.get("time") or appointment.get("appointment_time") or "")[:5],
            "appointment_datetime": _format_appointment_datetime_for_staff(appointment),
            "source": source_label,
        },
    )
    if result.get("success"):
        await _db().log_error("appointments", "staff_template_sent", f"appointment_id={appointment_id}; staff_phone={staff_phone}; template={template}", "info")
        return await finish(True, "", "", result.get("provider_message_id") or "")

    error = result.get("error") or result.get("reason") or "staff_template_failed"
    await _db().log_error("appointments", "staff_template_failed", f"appointment_id={appointment_id}; staff_phone={staff_phone}; template={template}; error={str(error)[:500]}", "error")
    return await finish(False, result.get("reason") or "staff_template_failed", str(error)[:500])


async def _appointment_staff_phone(appointment: dict) -> str:
    try:
        from db import get_appointment_staff, normalize_phone
        staff_id = str((appointment or {}).get("staff_id") or "")
        staff = next((s for s in await get_appointment_staff(include_inactive=True) if str(s.get("id") or "") == staff_id), {})
        raw = staff.get("whatsapp_number") or (appointment or {}).get("staff_whatsapp_number") or ""
        return normalize_phone(raw) if raw else ""
    except Exception:
        return ""


def _appointment_outbox_context(appointment: dict, notification_type: str, extra_context: Optional[dict] = None) -> dict:
    appointment = appointment or {}
    extra_context = extra_context or {}
    date_s = str(appointment.get("date") or appointment.get("appointment_date") or extra_context.get("appointment_date") or "")
    time_s = str(appointment.get("time") or appointment.get("appointment_time") or extra_context.get("appointment_time") or "")[:5]
    customer_name = _first_value(
        appointment.get("name"), appointment.get("customer_name"), appointment.get("lead_name"),
        extra_context.get("customer_name"), fallback="Customer",
    )
    service = _first_value(
        appointment.get("service"), appointment.get("service_type"), appointment.get("requirement"),
        extra_context.get("service_type"), fallback="Appointment",
    )
    context = {
        **appointment,
        **extra_context,
        "customer_name": customer_name,
        "lead_name": customer_name,
        "customer_phone": appointment.get("phone") or appointment.get("customer_phone") or extra_context.get("customer_phone") or "",
        "service_type": service,
        "service": service,
        "appointment_date": date_s,
        "appointment_time": time_s,
        "appointment_datetime": _first_value(extra_context.get("appointment_datetime"), _format_appointment_datetime_for_staff(appointment), fallback=f"{date_s} {time_s}".strip()),
        "notification_type": notification_type,
    }
    if notification_type.endswith("_cancellation"):
        context["reason"] = _first_value(extra_context.get("reason"), appointment.get("cancellation_reason"), fallback="Appointment cancelled")
    if notification_type.endswith("_reschedule"):
        context["previous_appointment_datetime"] = _first_value(extra_context.get("previous_appointment_datetime"), fallback="")
    return context


async def enqueue_appointment_notification(
    appointment: dict,
    notification_type: str,
    recipient_type: str,
    *,
    recipient_phone: str = "",
    conversation_id: str = "",
    contact_id: str = "",
    dedupe_suffix: str = "",
    extra_context: Optional[dict] = None,
    scheduled_at: Optional[str] = None,
) -> dict:
    """Persist one critical appointment notification intent without sending it."""
    if notification_type not in NOTIFICATION_OUTBOX_TYPES:
        return {"success": False, "reason": "unsupported_notification_type", "error": notification_type}
    appointment = appointment or {}
    appointment_id = str(appointment.get("id") or appointment.get("appointment_id") or "").strip()
    if not appointment_id:
        return {"success": False, "reason": "appointment_id_missing", "error": "appointment_id_missing"}
    recipient_type = str(recipient_type or "").strip().lower()
    if recipient_type not in {"customer", "staff"}:
        return {"success": False, "reason": "invalid_recipient_type", "error": "invalid_recipient_type"}
    if not recipient_phone:
        recipient_phone = appointment.get("phone") or appointment.get("customer_phone") or "" if recipient_type == "customer" else await _appointment_staff_phone(appointment)
    try:
        from db import normalize_phone
        normalized_phone = normalize_phone(recipient_phone)
    except Exception:
        normalized_phone = str(recipient_phone or "").strip()
    if not normalized_phone:
        return {"success": False, "reason": "recipient_phone_missing", "error": "recipient_phone_missing"}

    now = datetime.now(timezone.utc).isoformat()
    suffix = f":{dedupe_suffix}" if dedupe_suffix else ""
    dedupe_key = appointment_notification_dedupe_key(appointment_id, f"{notification_type}{suffix}", recipient_type)
    template_purpose = NOTIFICATION_OUTBOX_TYPES[notification_type]
    context = _appointment_outbox_context(appointment, notification_type, extra_context)
    row = {
        "id": str(uuid.uuid4()),
        "dedupe_key": dedupe_key,
        "status": "pending",
        "recipient_type": recipient_type,
        "recipient_phone": normalized_phone,
        "notification_type": notification_type,
        "template_purpose": template_purpose,
        "appointment_id": appointment_id,
        "conversation_id": conversation_id or appointment.get("conversation_id") or "",
        "contact_id": contact_id or appointment.get("contact_id") or "",
        "payload": _json.dumps(context, separators=(",", ":"), default=str),
        "attempt_count": 0,
        "max_attempts": NOTIFICATION_OUTBOX_MAX_ATTEMPTS,
        "next_attempt_at": scheduled_at or now,
        "created_at": now,
        "updated_at": now,
    }
    try:
        stored = await _db().get_repository().enqueue_notification(row)
        await _db().log_error(
            "notification_outbox",
            "notification_intent_persisted",
            f"appointment_ref={_safe_ref(appointment_id)}; notification_type={notification_type}; recipient_type={recipient_type}; dedupe_ref={_safe_ref(dedupe_key)}",
            "info",
        )
        return {"success": True, "row": stored, "dedupe_key": dedupe_key, "status": stored.get("status") or "pending"}
    except Exception as exc:
        safe_error = _sanitize_outbox_error(str(exc))
        await _db().log_error(
            "notification_outbox",
            "notification_intent_persist_failed",
            f"appointment_ref={_safe_ref(appointment_id)}; notification_type={notification_type}; recipient_type={recipient_type}; error={safe_error}",
            "error",
        )
        return {"success": False, "reason": "outbox_insert_failed", "error": safe_error}


async def enqueue_required_appointment_notifications(
    appointment: dict,
    *,
    event: str,
    conversation_id: str = "",
    extra_context: Optional[dict] = None,
) -> dict:
    mapping = {
        "created": [
            ("appointment_customer_confirmation", "customer"),
            ("appointment_staff_confirmation", "staff"),
        ],
        "rescheduled": [
            ("appointment_customer_reschedule", "customer"),
            ("appointment_staff_reschedule", "staff"),
        ],
        "cancelled": [
            ("appointment_customer_cancellation", "customer"),
            ("appointment_staff_cancellation", "staff"),
        ],
    }
    intents = []
    errors = []
    for notification_type, recipient_type in mapping.get(event, []):
        result = await enqueue_appointment_notification(
            appointment,
            notification_type,
            recipient_type,
            conversation_id=conversation_id,
            extra_context=extra_context,
        )
        intents.append({"notification_type": notification_type, "recipient_type": recipient_type, **result})
        if not result.get("success"):
            errors.append(f"{notification_type}:{result.get('reason') or result.get('error')}")
    return {"success": not errors, "event": event, "intents": intents, "errors": errors}


def _outbox_intent_payload(appointment: dict, notification_type: str, recipient_type: str, recipient_phone: str, extra_context: Optional[dict] = None) -> dict:
    return {
        "recipient_phone": recipient_phone,
        "notification_type": notification_type,
        "template_purpose": NOTIFICATION_OUTBOX_TYPES[notification_type],
        "conversation_id": appointment.get("conversation_id") or "",
        "contact_id": appointment.get("contact_id") or "",
        "payload": _appointment_outbox_context(appointment, notification_type, extra_context),
    }


def _service_start_code_for_booking(appointment: dict) -> tuple[str, Optional[dict]]:
    from airzen_repository import configured_backend

    if configured_backend() != "mysql" or not str((appointment or {}).get("staff_id") or "").strip():
        return "", None
    from staff_mobile import build_service_start_code

    return build_service_start_code(appointment)


async def create_appointment_with_notification_outbox_rpc(appointment: dict, *, conversation_id: str = "", extra_context: Optional[dict] = None) -> dict:
    customer_phone = appointment.get("phone") or appointment.get("customer_phone") or ""
    staff_phone = await _appointment_staff_phone(appointment)
    appointment = {**appointment, "conversation_id": conversation_id or appointment.get("conversation_id") or ""}
    if not customer_phone or not staff_phone:
        return {"success": False, "reason": "recipient_phone_missing", "error": "customer_or_staff_phone_missing"}
    customer_intent = _outbox_intent_payload(appointment, "appointment_customer_confirmation", "customer", customer_phone, extra_context)
    staff_intent = _outbox_intent_payload(appointment, "appointment_staff_confirmation", "staff", staff_phone, extra_context)
    try:
        service_start_code, start_code_record = _service_start_code_for_booking(appointment)
        start_code_args = {"start_code": start_code_record} if start_code_record else {}
        res = await _db().get_repository().create_appointment_with_outbox(
            appointment,
            customer_intent,
            staff_intent,
            event_identity="v1",
            **start_code_args,
        )
        return {"success": True, "rpc": res.get("result", res), "service_start_code": service_start_code}
    except Exception as exc:
        return {"success": False, "reason": "atomic_rpc_failed", "error": _sanitize_outbox_error(str(exc))}


async def reschedule_appointment_with_notification_outbox_rpc(old_appointment_id: str, new_appointment: dict, *, extra_context: Optional[dict] = None) -> dict:
    customer_phone = new_appointment.get("phone") or new_appointment.get("customer_phone") or ""
    staff_phone = await _appointment_staff_phone(new_appointment)
    if not customer_phone or not staff_phone:
        return {"success": False, "reason": "recipient_phone_missing", "error": "customer_or_staff_phone_missing"}
    customer_intent = _outbox_intent_payload(new_appointment, "appointment_customer_reschedule", "customer", customer_phone, extra_context)
    staff_intent = _outbox_intent_payload(new_appointment, "appointment_staff_reschedule", "staff", staff_phone, extra_context)
    try:
        service_start_code, start_code_record = _service_start_code_for_booking(new_appointment)
        start_code_args = {"start_code": start_code_record} if start_code_record else {}
        res = await _db().get_repository().reschedule_appointment_with_outbox(
            old_appointment_id,
            new_appointment,
            customer_intent,
            staff_intent,
            event_identity="v1",
            **start_code_args,
        )
        return {"success": True, "rpc": res.get("result", res), "service_start_code": service_start_code}
    except Exception as exc:
        return {"success": False, "reason": "atomic_rpc_failed", "error": _sanitize_outbox_error(str(exc))}


async def cancel_appointment_with_notification_outbox_rpc(appointment: dict, *, extra_context: Optional[dict] = None) -> dict:
    appointment_id = appointment.get("id") or appointment.get("appointment_id") or ""
    customer_phone = appointment.get("phone") or appointment.get("customer_phone") or ""
    staff_phone = await _appointment_staff_phone(appointment)
    if not appointment_id or not customer_phone or not staff_phone:
        return {"success": False, "reason": "missing_required_fields", "error": "appointment/customer/staff missing"}
    customer_intent = _outbox_intent_payload(appointment, "appointment_customer_cancellation", "customer", customer_phone, extra_context)
    staff_intent = _outbox_intent_payload(appointment, "appointment_staff_cancellation", "staff", staff_phone, extra_context)
    try:
        res = await _db().get_repository().cancel_appointment_with_outbox(
            appointment_id,
            customer_intent,
            staff_intent,
            event_identity="v1",
        )
        return {"success": True, "rpc": res.get("result", res)}
    except Exception as exc:
        return {"success": False, "reason": "atomic_rpc_failed", "error": _sanitize_outbox_error(str(exc))}


async def recover_stuck_notification_outbox(max_processing_age_minutes: int = 15) -> int:
    try:
        count = await _db().get_repository().recover_stuck_notification_outbox(max_processing_age_minutes, limit=50)
        if count:
            await _db().log_error("notification_outbox", "notification_processing_rows_recovered", f"count={count}", "warning")
        return count
    except Exception as exc:
        await _db().log_error("notification_outbox", "notification_recovery_failed", _sanitize_outbox_error(str(exc)), "error")
        return 0


async def claim_notification_outbox_batch(limit: int = 10) -> list:
    try:
        return await _db().get_repository().claim_notification_outbox(limit=limit, lease_seconds=300)
    except Exception as exc:
        await _db().log_error("notification_outbox", "notification_claim_failed", _sanitize_outbox_error(str(exc)), "error")
        return []


async def _mark_outbox_sent(row: dict, provider_message_id: str) -> bool:
    row_id = row.get("id") or ""
    claim_token = row.get("claim_token") or ""
    try:
        if not claim_token and _atomic_outbox_required():
            raise RuntimeError("claim_token_missing")
        return await _db().get_repository().complete_notification_outbox(row_id, claim_token, provider_message_id or "")
    except Exception as exc:
        if _atomic_outbox_required():
            await _db().log_error("notification_outbox", "notification_mark_sent_failed", f"row_ref={_safe_ref(row_id)}; error={_sanitize_outbox_error(str(exc))}", "error")
            return False
        return False


async def _mark_outbox_failed(row: dict, status: str, safe_error: str, next_attempt_at: str = "") -> bool:
    row_id = row.get("id") or ""
    claim_token = row.get("claim_token") or ""
    try:
        if not claim_token and _atomic_outbox_required():
            raise RuntimeError("claim_token_missing")
        return await _db().get_repository().fail_notification_outbox(row_id, claim_token, status, safe_error, next_attempt_at)
    except Exception as exc:
        if _atomic_outbox_required():
            await _db().log_error("notification_outbox", "notification_mark_failed_failed", f"row_ref={_safe_ref(row_id)}; status={status}; error={_sanitize_outbox_error(str(exc))}", "error")
            return False
        return False


async def _send_notification_outbox_row(row: dict) -> dict:
    notification_type = str(row.get("notification_type") or "")
    if notification_type == "appointment_job_start_otp":
        return {
            "success": False,
            "reason": "retired_notification_type",
            "error": "service_start_code_is_delivered_at_booking",
        }
    template_purpose = row.get("template_purpose") or NOTIFICATION_OUTBOX_TYPES.get(notification_type, "")
    phone = row.get("recipient_phone") or ""
    payload = {}
    try:
        payload = _json.loads(row.get("payload") or "{}")
    except Exception:
        payload = {}
    template = await resolve_wa_template(template_purpose)
    if not template:
        return {"success": False, "reason": "template_missing", "error": f"{template_purpose} missing"}
    built = await build_template_params(template_purpose, payload)
    return await send_whatsapp_template(
        phone,
        built["template_name"] or template,
        built["language"],
        built["params"],
        event_type=notification_type,
        source_type="notification_outbox",
        source_id=row.get("id") or row.get("dedupe_key") or "",
        template_purpose=template_purpose,
        template_context=payload,
        bypass_failure_cooldown=True,
    )


async def process_notification_outbox_row(row: dict) -> dict:
    row_id = row.get("id") or ""
    appointment_id = row.get("appointment_id") or ""
    attempts = int(row.get("attempt_count") or 1)
    max_attempts = int(row.get("max_attempts") or NOTIFICATION_OUTBOX_MAX_ATTEMPTS)
    await _db().log_error(
        "notification_outbox",
        "notification_send_started",
        f"row_ref={_safe_ref(row_id)}; appointment_ref={_safe_ref(appointment_id)}; notification_type={row.get('notification_type')}; attempt={attempts}",
        "info",
    )
    result = await _send_notification_outbox_row(row)
    if result.get("success"):
        provider_id = result.get("provider_message_id") or ""
        updated = await _mark_outbox_sent(row, provider_id)
        try:
            from db import update_appointment_notifications, update_appointment_demo_fields
            notification_type = str(row.get("notification_type") or "")
            if appointment_id and notification_type in {"appointment_customer_confirmation", "appointment_customer_reschedule", "appointment_customer_cancellation"}:
                await update_appointment_notifications(appointment_id, {"confirmation_sent": True, "confirmation_sent_at": datetime.now(timezone.utc).isoformat(), "notification_error": ""})
            elif appointment_id and notification_type in {"appointment_staff_confirmation", "appointment_staff_reschedule", "appointment_staff_cancellation"}:
                await update_appointment_notifications(appointment_id, {"staff_notified": True, "staff_notification_message_id": provider_id, "staff_notification_sent_at": datetime.now(timezone.utc).isoformat(), "notification_error": ""})
            elif appointment_id and notification_type == "appointment_customer_reminder":
                await update_appointment_notifications(appointment_id, {"customer_reminder_sent": True, "customer_reminder_sent_at": datetime.now(timezone.utc).isoformat()})
            elif appointment_id and notification_type == "appointment_staff_reminder":
                await update_appointment_notifications(appointment_id, {"staff_reminder_sent": True, "staff_reminder_sent_at": datetime.now(timezone.utc).isoformat()})
            elif appointment_id and notification_type == "staff_outcome_request":
                await update_appointment_demo_fields(appointment_id, {"demo_status_requested": True, "demo_status_requested_at": datetime.now(timezone.utc).isoformat()})
        except Exception as exc:
            await _db().log_error("notification_outbox", "notification_appointment_flag_update_failed", f"row_ref={_safe_ref(row_id)}; appointment_ref={_safe_ref(appointment_id)}; error={_sanitize_outbox_error(str(exc))}", "warning")
        await _db().log_error("notification_outbox", "notification_send_succeeded", f"row_ref={_safe_ref(row_id)}; provider_ref={_safe_ref(provider_id)}", "info")
        if not updated:
            return {"success": False, "reason": "status_update_failed_after_send", "provider_message_id": provider_id}
        return {"success": True, "provider_message_id": provider_id}

    failure_kind = classify_notification_send_failure(result)
    safe_error = _sanitize_outbox_error(result.get("error") or result.get("reason") or "send_failed")
    permanent = False
    if failure_kind == "unknown":
        status = "delivery_unknown"
        next_attempt_at = ""
    else:
        permanent = failure_kind == "permanent" or attempts >= max_attempts
        status = "permanently_failed" if permanent else "failed"
        next_attempt_at = "" if permanent else _outbox_next_attempt_at(attempts)
    await _mark_outbox_failed(row, status, safe_error, next_attempt_at)
    await _db().log_error("notification_outbox", "notification_send_failed", f"row_ref={_safe_ref(row_id)}; failure_kind={failure_kind}; status={status}; error={safe_error}", "warning" if not permanent else "error")
    return {"success": False, "reason": result.get("reason") or "send_failed", "failure_kind": failure_kind, "status": status, "error": safe_error}


async def run_due_notification_outbox(limit: int = 20) -> dict:
    recovered = await recover_stuck_notification_outbox()
    rows = await claim_notification_outbox_batch(limit)
    processed = 0
    sent = 0
    failed = 0
    for row in rows:
        result = await process_notification_outbox_row(row)
        processed += 1
        if result.get("success"):
            sent += 1
        else:
            failed += 1
    return {"processed": processed, "sent": sent, "failed": failed, "recovered": recovered}


async def _notify_appointment_booked(phone: str, appointment: dict, customer_name: str, company: str, staff_name: str, inbound_saved: Optional[dict]) -> dict:
    appointment_id = appointment.get("id", "")
    outbox_result = await enqueue_required_appointment_notifications(
        {**appointment, "name": customer_name, "phone": phone, "service": appointment.get("service") or company or "Appointment"},
        event="created",
        conversation_id=appointment.get("conversation_id", ""),
        extra_context={
            "customer_name": customer_name,
            "business_name": company or "your business",
            "staff_name": staff_name,
        },
    )
    if not outbox_result.get("success"):
        try:
            from db import update_appointment_notifications
            if appointment_id:
                await update_appointment_notifications(appointment_id, {
                    "notification_error": "; ".join(outbox_result.get("errors") or ["notification_outbox_failed"])[:1000],
                })
        except Exception:
            pass
    return {
        "success": bool(outbox_result.get("success")),
        "queued": bool(outbox_result.get("success")),
        "outbox": outbox_result,
        "errors": outbox_result.get("errors") or [],
        "confirmation_sent": False,
        "staff_notified": False,
        "delivery": "notification_outbox",
    }
    duration_minutes = int(appointment.get("duration_minutes") or 30)
    meet_link = appointment.get("google_meet_link") or appointment.get("meet_link") or ""
    errors = []
    confirmation_sent = False
    staff_notified = False
    telegram_notified = False

    try:
        from db import get_appointment_settings
        appointment_settings = await get_appointment_settings()
    except Exception:
        appointment_settings = {}

    await _log_whatsapp_ai_event(phone, "appointment_confirmation_started", f"appointment_id={appointment_id}")
    customer_text = _appointment_confirmation_text(
        customer_name,
        appointment,
        staff_name,
        duration_minutes,
        meet_link,
        str(appointment_settings.get("appointment_name") or "Appointment"),
        str(appointment_settings.get("delivery_mode") or "onsite"),
        str(appointment_settings.get("staff_label") or "staff member"),
    )
    try:
        template = await resolve_wa_template("appointment_confirmation_template")
        if template:
            language = await _get_wa_setting("WHATSAPP_DEFAULT_LANGUAGE") or "en"
            params = _build_template_params(
                "appointment_confirmation_template",
                {"lead_name": customer_name, "business_name": company or "your business"},
                {"appointment_date": appointment.get("date"), "appointment_time": appointment.get("time")},
            )
            result = await send_whatsapp_template(
                phone, template, language, params,
                event_type="appointment_confirmed",
                source_type="whatsapp_ai",
                source_id=appointment_id or phone,
                template_purpose="appointment_confirmation_template",
                template_context={
                    **appointment,
                    "customer_name": customer_name,
                    "business_name": company or "your business",
                    "appointment_date": appointment.get("date"),
                    "appointment_time": appointment.get("time"),
                },
            )
            confirmation_sent = bool(result.get("success"))
            if confirmation_sent:
                await _log_whatsapp_ai_event(phone, "appointment_confirmation_sent", f"template={template}")
            else:
                errors.append(f"customer_confirmation_template: {result.get('error') or result.get('reason')}")
                await _log_whatsapp_ai_event(phone, "appointment_confirmation_failed", result.get("error") or result.get("reason") or "", "error")
        else:
            errors.append("customer_confirmation_template: template_missing")
            await _log_whatsapp_ai_event(phone, "appointment_confirmation_failed", "template_missing", "warning")
        if not confirmation_sent:
            fallback = await _send_and_save_whatsapp_ai_text(phone, appointment.get("conversation_id", "") or "", customer_text, inbound_saved, {"appointment_id": appointment_id, "notification_type": "appointment_confirmation_fallback"})
            confirmation_sent = bool(fallback.get("success"))
            if not confirmation_sent:
                errors.append(f"customer_confirmation_text: {fallback.get('error') or fallback.get('reason')}")
    except Exception as exc:
        errors.append(f"customer_confirmation: {str(exc)[:200]}")
        await _log_whatsapp_ai_event(phone, "appointment_confirmation_failed", str(exc)[:500], "error")

    staff_result = {}
    try:
        staff_result = await send_staff_appointment_notification(
            {**appointment, "name": customer_name, "phone": phone, "service": appointment.get("service") or company or "Appointment"},
            source=appointment.get("source") or "WhatsApp",
            update_appointment=False,
        )
        staff_notified = bool(staff_result.get("success"))
        if not staff_notified:
            errors.append(f"staff_whatsapp: {staff_result.get('error') or staff_result.get('reason')}")
    except Exception as exc:
        errors.append(f"staff_whatsapp: {str(exc)[:200]}")
        await _db().log_error("appointments", "staff_template_failed", f"appointment_id={appointment_id}; error={str(exc)[:500]}", "error")

    try:
        from db import update_appointment_notifications
        if appointment_id:
            await update_appointment_notifications(appointment_id, {
                "confirmation_sent": confirmation_sent,
                "confirmation_sent_at": datetime.now().isoformat() if confirmation_sent else "",
                "staff_notified": staff_notified,
                "staff_notification_message_id": staff_result.get("provider_message_id") or "" if staff_notified else "",
                "staff_notification_sent_at": datetime.now(timezone.utc).isoformat() if staff_notified else None,
                "telegram_notified": telegram_notified,
                "notification_error": "; ".join(errors)[:1000],
            })
    except Exception as exc:
        await _log_whatsapp_ai_event(phone, "appointment_notification_update_failed", str(exc)[:500], "error")

    return {
        "confirmation_sent": confirmation_sent,
        "staff_notified": staff_notified,
        "telegram_notified": telegram_notified,
        "notification_error": "; ".join(errors)[:1000],
    }


async def _handle_whatsapp_appointment_state(phone: str, conv_id: str, text: str, conv: dict, inbound_saved: Optional[dict], crm_contact: Optional[dict], shared_location: Optional[dict] = None, recent_messages: Optional[list] = None) -> bool:
    lower = (text or "").lower().strip()
    if not lower:
        return False
    message_service_address = _extract_service_address_from_booking_text(text)
    try:
        from db import get_appointment_settings
        appointment_settings = await get_appointment_settings()
    except Exception:
        appointment_settings = {}
    duration = int(appointment_settings.get("duration_minutes") or appointment_settings.get("demo_duration_minutes") or 30)
    appointment_name = str(appointment_settings.get("appointment_name") or "Appointment").strip()
    default_service = str(appointment_settings.get("default_service_type") or appointment_name).strip()
    delivery_mode = str(appointment_settings.get("delivery_mode") or "onsite").strip().lower()
    staff_label = str(appointment_settings.get("staff_label") or "staff member").strip().lower()
    intake_prompt = str(appointment_settings.get("intake_prompt") or "").strip()
    state = _conversation_appointment_state(conv)
    active_appointment = None
    try:
        from db import get_next_active_appointment
        active_appointment = await get_next_active_appointment(phone)
    except Exception:
        pass
    restored_state = _restore_booking_state(state, crm_contact, active_appointment)
    if restored_state != state:
        state = restored_state
        await patch_conversation(conv_id, {"appointment_state": _json.dumps(state)})
    intake_context = _booking_intake_context_from_history(recent_messages, text)
    if intake_context and str(state.get("status") or "") not in _APPOINTMENT_ACTIVE_STATUSES:
        problem = intake_context.get("problem") or ""
        state = {
            **state,
            "status": "awaiting_intake",
            "service": state.get("service") or default_service,
            "units": intake_context["units"],
            "problem": problem,
            "intake_data": {
                **(state.get("intake_data") or {}),
                "units": intake_context["units"],
                **({"problem": problem, "service_summary": problem} if problem else {}),
            },
            "updated_at": datetime.now(ZoneInfo("Asia/Singapore")).isoformat(),
        }
        await patch_conversation(conv_id, {"appointment_state": _json.dumps(state)})
        try:
            from db import update_crm_contact_full
            crm_updates = {"source": "WhatsApp", "service_type": state["service"]}
            if problem:
                crm_updates["requirement"] = problem
            await update_crm_contact_full(phone, crm_updates)
        except Exception as exc:
            await _diagnostic_event("crm_booking_intake_persisted", phone=phone, success=False, level="warning", exception_type=type(exc).__name__)
        memory_parts = [f"units={state['units']}"]
        if problem:
            memory_parts.append(f"issue={problem}")
        await _remember_whatsapp_insight(phone, "Service intake: " + "; ".join(memory_parts))
        await _diagnostic_event("booking_history_context_restored", phone=phone, conversation_id=conv_id, field="units")
    if state:
        await _diagnostic_event("booking_session_restored", phone=phone, conversation_id=conv_id, state_status=state.get("status") or "unknown")
    await _diagnostic_event(
        "booking_state_loaded", phone=phone, conversation_id=conv_id,
        known_field_count=sum(bool(v) for k, v in _booking_known_fields(crm_contact, state, active_appointment).items() if k != "intake_data"),
    )
    if state:
        await _diagnostic_event("appointment_state_entered", phone=phone, conversation_id=conv_id, state_status=state.get("status") or "unknown")
    requested_service = _first_value(
        (crm_contact or {}).get("service_type"),
        (crm_contact or {}).get("requirement"),
        state.get("service"),
        fallback=default_service,
    )
    explicit_new_booking = is_explicit_new_booking_intent(text)
    if explicit_new_booking:
        await _diagnostic_event("new_booking_intent_detected", phone=phone, conversation_id=conv_id, has_active_appointment=bool(active_appointment))
        if active_appointment:
            reply = "You already have an upcoming appointment. Would you like to make a new booking or change the existing appointment?"
            await _send_and_save_whatsapp_ai_text(phone, conv_id, reply, inbound_saved, {"appointment_state": state})
            return True
        if state:
            previous_status = state.get("status") or "unknown"
            state = _reset_booking_session_state(state)
            await patch_conversation(conv_id, {"appointment_state": _json.dumps(state)})
            await _diagnostic_event("booking_session_reset", phone=phone, conversation_id=conv_id, previous_status=previous_status, reason="explicit_new_booking")
        preview_merge = merge_booking_datetime(state, text, (inbound_saved or {}).get("created_at") if isinstance(inbound_saved, dict) else None)
        if not preview_merge.get("complete"):
            fresh_state = {
                **state, "status": "awaiting_datetime", "service": requested_service,
                "requested_date": preview_merge.get("requested_date") or "",
                "requested_time": preview_merge.get("requested_time") or "",
                "service_address": message_service_address or state.get("service_address") or "",
                "postal_code": _extract_postal_code(message_service_address) or state.get("postal_code") or "",
                "updated_at": datetime.now(ZoneInfo("Asia/Singapore")).isoformat(),
            }
            await patch_conversation(conv_id, {"appointment_state": _json.dumps(fresh_state)})
            if fresh_state.get("requested_date"):
                reply = f"Thank you, I saved the preferred date. What time would you prefer for the {appointment_name.lower()}?"
            else:
                reply = f"Sure, what preferred date and time would you like for the {appointment_name.lower()}?"
            await _send_and_save_whatsapp_ai_text(phone, conv_id, reply, inbound_saved, {"appointment_state": fresh_state})
            return True
    elif _booking_session_expired(state):
        previous_status = state.get("status") or "unknown"
        state = _reset_booking_session_state(state)
        await patch_conversation(conv_id, {"appointment_state": _json.dumps(state)})
        await _diagnostic_event("booking_session_expired", phone=phone, conversation_id=conv_id, previous_status=previous_status)
        await _diagnostic_event("booking_session_reset", phone=phone, conversation_id=conv_id, previous_status=previous_status, reason="expired")
    state_status = str(state.get("status") or "")
    missing_fields = get_missing_booking_fields(
        crm_contact, state, active_appointment, delivery_mode=delivery_mode, require_intake=bool(intake_prompt)
    )
    await _diagnostic_event("booking_missing_fields_calculated", phone=phone, conversation_id=conv_id, fields=",".join(missing_fields), count=len(missing_fields))
    if state_status == "awaiting_address" and "service_address" not in missing_fields:
        state_status = "awaiting_intake" if intake_prompt and any(field in missing_fields for field in ("units", "problem", "property_type")) else "pending_confirmation"
        state = {**state, "status": state_status, "updated_at": datetime.now().isoformat()}
        await patch_conversation(conv_id, {"appointment_state": _json.dumps(state)})

    if state.get("proposed_service_address") and _is_appointment_confirmation_reply(text):
        proposed = state.get("proposed_service_address")
        postal_code = _extract_postal_code(proposed)
        state = {**state, "service_address": proposed, "postal_code": postal_code, "proposed_service_address": "", "updated_at": datetime.now().isoformat()}
        await patch_conversation(conv_id, {"appointment_state": _json.dumps(state)})
        await _persist_whatsapp_service_address(phone, proposed, postal_code)
        await _send_and_save_whatsapp_ai_text(phone, conv_id, "Thanks, I updated the service address.", inbound_saved, {"appointment_state": state})
        return True
    if _is_booking_cancel_reply(text) and state_status in _BOOKING_TRANSIENT_STATES:
        await patch_conversation(conv_id, {"appointment_state": "{}"})
        await _log_whatsapp_ai_event(phone, "appointment_pending_time_saved", "cleared_by_cancel")
        await _send_and_save_whatsapp_ai_text(phone, conv_id, f"No problem, I have not booked that {appointment_name.lower()} slot.", inbound_saved, {"appointment_state": "cancelled"})
        return True

    intake_missing = [field for field in missing_fields if field in {"units", "problem", "property_type"}]
    if intake_prompt and state_status in {"awaiting_intake", "pending_confirmation"} and intake_missing:
        intake_updates = _extract_booking_intake_updates(text, state, intake_missing)
        if not intake_updates and state_status == "awaiting_intake":
            reply = _next_booking_missing_field_prompt(intake_missing, appointment_name)
            await _send_and_save_whatsapp_ai_text(phone, conv_id, reply, inbound_saved, {"appointment_state": state})
            return True
        if intake_updates:
            updated_intake = {**(state.get("intake_data") or {}), **intake_updates}
            updated_state = {
                **state,
                "property_type": intake_updates.get("property_type") or state.get("property_type") or "",
                "units": intake_updates.get("units") or state.get("units") or "",
                "problem": intake_updates.get("problem") or state.get("problem") or "",
                "intake_data": updated_intake,
                "updated_at": datetime.now().isoformat(),
            }
            post_missing = get_missing_booking_fields(
                crm_contact, updated_state, active_appointment, delivery_mode=delivery_mode, require_intake=True
            )
            next_status = "awaiting_intake" if any(field in post_missing for field in ("units", "problem", "property_type")) else "pending_confirmation"
            updated_state = {**updated_state, "status": next_status}
            await patch_conversation(conv_id, {"appointment_state": _json.dumps(updated_state)})
            for field in ("property_type", "units", "problem"):
                if field in intake_updates:
                    await _diagnostic_event("booking_field_persisted", phone=phone, conversation_id=conv_id, field=field, target="appointment_state", success=True)
            await _diagnostic_event("booking_missing_fields_calculated", phone=phone, conversation_id=conv_id, fields=",".join(post_missing), count=len(post_missing))
            if post_missing:
                reply = _next_booking_missing_field_prompt(post_missing, appointment_name)
            else:
                reply = _booking_confirmation_prompt(updated_state, crm_contact, appointment_name, staff_label)
            await _send_and_save_whatsapp_ai_text(phone, conv_id, reply, inbound_saved, {"appointment_state": updated_state})
            return True
        if state_status == "pending_confirmation":
            reply = _next_booking_missing_field_prompt(intake_missing, appointment_name)
            updated_state = {**state, "status": "awaiting_intake", "updated_at": datetime.now().isoformat()}
            await patch_conversation(conv_id, {"appointment_state": _json.dumps(updated_state)})
            await _send_and_save_whatsapp_ai_text(phone, conv_id, reply, inbound_saved, {"appointment_state": updated_state})
            return True

    # Address/contact replies must be handled before date parsing. Unit numbers
    # such as "02-240" would otherwise be mistaken for a 2:00 AM time.
    if state_status in {"awaiting_address", "pending_confirmation"}:
        if _is_whatsapp_number_reference(text):
            updated_state = {**state, "contact_number": phone, "updated_at": datetime.now().isoformat()}
            await patch_conversation(conv_id, {"appointment_state": _json.dumps(updated_state)})
            if state_status == "awaiting_address":
                reply = "I’ll use this WhatsApp number as your contact number. Please share the service address."
            else:
                reply = f"I’ll use this WhatsApp number. Shall I confirm your {appointment_name.lower()} for {_format_demo_slot(state.get('requested_date', ''), state.get('requested_time', ''))}?"
            await _send_and_save_whatsapp_ai_text(phone, conv_id, reply, inbound_saved, {"appointment_state": updated_state})
            return True
        location = shared_location or {}
        extracted_address = _normalize_booking_text(
            location.get("address") or location.get("name") or message_service_address or text
        )
        if shared_location or message_service_address or _looks_like_service_address(text):
            service_address = extracted_address
            existing_address = _first_value(state.get("service_address"), (crm_contact or {}).get("address"), (active_appointment or {}).get("service_address"))
            explicitly_changed = bool(re.search(r"\b(change|changed|new|different|update|instead)\b", lower))
            if existing_address and _normalize_address_key(existing_address) != _normalize_address_key(service_address) and not explicitly_changed:
                conflict_state = {**state, "proposed_service_address": service_address, "updated_at": datetime.now().isoformat()}
                await patch_conversation(conv_id, {"appointment_state": _json.dumps(conflict_state)})
                await _diagnostic_event("booking_field_conflict_detected", phone=phone, conversation_id=conv_id, field="service_address")
                await _send_and_save_whatsapp_ai_text(phone, conv_id, "I already have a different service address saved. Would you like me to replace it with the new address?", inbound_saved, {"appointment_state": conflict_state})
                return True
            postal_code = _extract_postal_code(service_address)
            updated_state = {
                **state,
                "contact_number": phone,
                "service_address": service_address,
                "postal_code": postal_code,
                "location_latitude": location.get("latitude"),
                "location_longitude": location.get("longitude"),
                "location_name": location.get("name") or "",
                "location_url": location.get("url") or "",
                "updated_at": datetime.now().isoformat(),
            }
            post_address_missing = get_missing_booking_fields(
                crm_contact, updated_state, active_appointment, delivery_mode=delivery_mode, require_intake=bool(intake_prompt)
            )
            next_status = "awaiting_intake" if intake_prompt and any(field in post_address_missing for field in ("units", "problem", "property_type")) else "pending_confirmation"
            updated_state = {**updated_state, "status": next_status}
            await _diagnostic_event("booking_field_extracted", phone=phone, conversation_id=conv_id, field="service_address", has_postal_code=bool(postal_code))
            await patch_conversation(conv_id, {"appointment_state": _json.dumps(updated_state)})
            await _diagnostic_event("booking_field_persisted", phone=phone, conversation_id=conv_id, field="service_address", target="appointment_state", success=True)
            if postal_code:
                await _persist_whatsapp_service_address(phone, updated_state["service_address"], postal_code)
            else:
                await _persist_whatsapp_service_address(phone, updated_state["service_address"])
            if next_status == "awaiting_intake":
                next_missing = get_missing_booking_fields(
                    crm_contact, updated_state, active_appointment, delivery_mode=delivery_mode, require_intake=True
                )
                reply = "Thanks, I saved the service location. " + _next_booking_missing_field_prompt(next_missing, appointment_name)
            else:
                reply = "Thanks, I saved the service address and will use this WhatsApp number. " + _booking_confirmation_prompt(updated_state, crm_contact, appointment_name, staff_label)
            await _send_and_save_whatsapp_ai_text(phone, conv_id, reply, inbound_saved, {"appointment_state": updated_state})
            return True
        if state_status == "awaiting_address":
            reply = "Please share the full service address, including the block or unit and postal code if available. I’ll use this WhatsApp number as your contact."
            await _send_and_save_whatsapp_ai_text(phone, conv_id, reply, inbound_saved, {"appointment_state": state})
            return True

    inbound_timestamp = (inbound_saved or {}).get("created_at") if isinstance(inbound_saved, dict) else None
    datetime_merge = merge_booking_datetime(state, text, inbound_timestamp)
    if datetime_merge.get("ambiguous_time"):
        reply = f"Do you mean {datetime_merge['ambiguous_time']} PM?"
        await _send_and_save_whatsapp_ai_text(phone, conv_id, reply, inbound_saved, {"appointment_state": state})
        return True
    if datetime_merge.get("date_extracted") or datetime_merge.get("time_extracted"):
        merged_state = {
            **state,
            "requested_date": datetime_merge.get("requested_date") or state.get("requested_date") or "",
            "requested_time": datetime_merge.get("requested_time") or state.get("requested_time") or "",
            "updated_at": datetime.now(ZoneInfo("Asia/Singapore")).isoformat(),
        }
        if not datetime_merge.get("complete"):
            merged_state["status"] = "awaiting_datetime"
        state = merged_state
        state_status = str(state.get("status") or "")
        await patch_conversation(conv_id, {"appointment_state": _json.dumps(state)})
        if datetime_merge.get("time_extracted"):
            await _diagnostic_event("booking_time_extracted", phone=phone, conversation_id=conv_id, requested_time=state.get("requested_time"))
        await _diagnostic_event(
            "booking_datetime_merged", phone=phone, conversation_id=conv_id,
            has_date=bool(state.get("requested_date")), has_time=bool(state.get("requested_time")),
        )
        await _diagnostic_event(
            "booking_datetime_persisted", phone=phone, conversation_id=conv_id,
            has_date=bool(state.get("requested_date")), has_time=bool(state.get("requested_time")),
        )
    flexible_date = state.get("requested_date") or next((
        slot.get("requested_date") for slot in (state.get("requested_slots") or [])
        if isinstance(slot, dict) and slot.get("requested_date")
    ), "")
    if _is_flexible_booking_time_reply(text) and flexible_date and not state.get("requested_time"):
        slots = await _suggest_available_demo_slots(flexible_date, "08:59", 3)
        if slots:
            labels = [_format_demo_slot(slot["date"], slot["time"]) for slot in slots]
            updated_state = {
                **state,
                "status": "alternatives_offered",
                "requested_date": flexible_date,
                "suggested_slots": slots,
                "updated_at": datetime.now(ZoneInfo("Asia/Singapore")).isoformat(),
            }
            await patch_conversation(conv_id, {"appointment_state": _json.dumps(updated_state)})
            reply = f"Available times are {', '.join(labels[:-1])}{' or ' if len(labels) > 1 else ''}{labels[-1]}. Which one would you prefer?"
        else:
            updated_state = state
            reply = f"I could not find an open time on {_format_demo_slot(flexible_date, '09:00').split(' at ')[0]}. Please share another preferred date."
        await _send_and_save_whatsapp_ai_text(phone, conv_id, reply, inbound_saved, {"appointment_state": updated_state})
        return True
    if not datetime_merge.get("complete") and state.get("requested_date") and not state.get("requested_time") and state_status == "awaiting_datetime":
        await _send_and_save_whatsapp_ai_text(phone, conv_id, "What exact time would you prefer? Our appointment hours are between 9 AM and 6 PM.", inbound_saved, {"appointment_state": state})
        return True
    parsed_dt = datetime_merge.get("parsed") if (datetime_merge.get("date_extracted") or datetime_merge.get("time_extracted")) else None
    if parsed_dt:
        await _diagnostic_event(
            "relative_datetime_resolved", phone=phone, conversation_id=conv_id,
            requested_date=parsed_dt.get("requested_date"), requested_time=parsed_dt.get("requested_time"), timezone="Asia/Singapore",
        )
        await _log_whatsapp_ai_event(phone, "whatsapp_appointment_intent_detected", f"parsed_scheduled_at={parsed_dt['parsed_scheduled_at']}")
        try:
            from db import get_appointment_staff
            configured_staff = await get_appointment_staff(include_inactive=True)
            active_staff = [person for person in configured_staff if person.get("active", True)]
            if not configured_staff or not active_staff:
                failed_state = {**parsed_dt, "status": "booking_failed", "reason": "no_active_staff", "updated_at": datetime.now().isoformat()}
                await patch_conversation(conv_id, {"appointment_state": _json.dumps(failed_state)})
                reply = "Our scheduling team is not available for automatic booking right now. Please leave your preferred time and our team will confirm it."
                await _send_and_save_whatsapp_ai_text(phone, conv_id, reply, inbound_saved, {"appointment_state": failed_state})
                return True
        except Exception as exc:
            await _log_whatsapp_ai_event(phone, "appointment_staff_preflight_failed", str(exc)[:500], "warning")
        candidate_slots = parsed_dt.get("candidate_slots") or [parsed_dt]
        if len(candidate_slots) > 1:
            await _log_whatsapp_ai_event(phone, "appointment_multi_slot_request_detected", f"slots={candidate_slots}")
        selected_slot = None
        unavailable = []
        unavailable_reasons = []
        requested_action = "reschedule" if active_appointment else "book"
        if active_appointment:
            old_date = str(active_appointment.get("date") or "")
            old_time = str(active_appointment.get("time") or "")[:5]
            if any(slot.get("requested_date") == old_date and slot.get("requested_time") == old_time for slot in candidate_slots):
                booked_state = {
                    "status": "booked", "action": "existing",
                    "booking_id": active_appointment.get("id") or "",
                    "requested_date": old_date, "requested_time": old_time,
                    "service": active_appointment.get("service") or requested_service,
                    "staff_name": active_appointment.get("staff_name") or "our team",
                }
                await patch_conversation(conv_id, {"appointment_state": _json.dumps(booked_state)})
                await _send_and_save_whatsapp_ai_text(phone, conv_id, f"Your {appointment_name.lower()} is already booked for {_format_demo_slot(old_date, old_time)}. I have kept that booking unchanged.", inbound_saved, {"appointment_state": booked_state})
                return True
        try:
            from db import get_slot_availability
            for slot in candidate_slots:
                date_s = slot.get("requested_date", "")
                time_s = slot.get("requested_time", "")
                await _diagnostic_event("availability_check_started", phone=phone, conversation_id=conv_id, requested_date=date_s, requested_time=time_s)
                await _log_whatsapp_ai_event(phone, "appointment_candidate_slot_checked", f"date={date_s} time={time_s}")
                availability = await get_slot_availability(date_s, time_s)
                available = bool(availability.get("available"))
                await _diagnostic_event("availability_check_completed", phone=phone, conversation_id=conv_id, requested_date=date_s, requested_time=time_s, available=bool(available))
                if available:
                    selected_slot = slot
                    selected_slot = {
                        **selected_slot,
                        "availability_checked": True,
                        "availability_status": "available",
                        "availability_reason": str(availability.get("reason") or "available"),
                        "timezone": availability.get("timezone") or slot.get("timezone") or slot.get("resolved_timezone") or "Asia/Singapore",
                        "resolved_timezone": slot.get("resolved_timezone") or availability.get("timezone") or "Asia/Singapore",
                        "staff_id": ((availability.get("staff") or {}) if isinstance(availability.get("staff"), dict) else {}).get("id") or slot.get("staff_id") or "",
                        "staff_name": ((availability.get("staff") or {}) if isinstance(availability.get("staff"), dict) else {}).get("name") or slot.get("staff_name") or "",
                        "staff_role": ((availability.get("staff") or {}) if isinstance(availability.get("staff"), dict) else {}).get("role") or slot.get("staff_role") or "",
                    }
                    await _log_whatsapp_ai_event(phone, "appointment_candidate_slot_available", f"date={date_s} time={time_s}")
                    break
                unavailable.append(slot)
                unavailable_reasons.append(str(availability.get("reason") or "unknown"))
                await _log_whatsapp_ai_event(phone, "appointment_candidate_slot_unavailable", f"date={date_s} time={time_s} reason={availability.get('reason') or 'unknown'}", "warning")
        except Exception as exc:
            await _diagnostic_event("appointment_route_failed", phone=phone, conversation_id=conv_id, stage="availability_check", exception_type=type(exc).__name__, level="error")
            reply = "I could not verify appointment availability right now. Please try again shortly, or our team can confirm the slot for you."
            await _send_and_save_whatsapp_ai_text(phone, conv_id, reply, inbound_saved, {"appointment_state": state})
            await _diagnostic_event("appointment_safe_fallback_sent", phone=phone, conversation_id=conv_id, stage="availability_check")
            return True
        if not selected_slot:
            first = candidate_slots[0]
            slots = await _suggest_available_demo_slots(first.get("requested_date", ""), first.get("requested_time", ""), 3)
            labels = [s["label"] for s in slots]
            await _log_whatsapp_ai_event(phone, "appointment_all_requested_slots_unavailable", f"requested={unavailable}", "warning")
            await _log_whatsapp_ai_event(phone, "appointment_alternatives_offered", f"suggestions={labels}", "info")
            reason_set = set(unavailable_reasons)
            if reason_set and reason_set <= {"past_or_too_soon"}:
                reply = f"That requested time has already passed or is too close to arrange safely. Please share a later preferred date and time for the {appointment_name.lower()}."
            elif reason_set and reason_set <= {"outside_staff_hours", "no_matching_staff_role", "no_active_staff"}:
                reply = f"That time is outside the currently configured {staff_label} availability. Please share another preferred date and time for the {appointment_name.lower()}."
            elif labels and reason_set == {"appointment_conflict"}:
                reply = f"Those slots are already booked. Available slots are {', '.join(labels[:-1])}{' or ' if len(labels)>1 else ''}{labels[-1]}. Which one do you prefer?"
            elif labels:
                reply = f"That requested time cannot be offered under the current booking schedule. Available times are {', '.join(labels[:-1])}{' or ' if len(labels)>1 else ''}{labels[-1]}. Which one do you prefer?"
            else:
                reply = f"That requested time cannot be offered under the current booking schedule. Please share another preferred date and time for the {appointment_name.lower()}."
            alternative_state = {
                "status": "alternatives_offered",
                "action": requested_action,
                "existing_appointment_id": (active_appointment or {}).get("id") or "",
                "existing_date": (active_appointment or {}).get("date") or "",
                "existing_time": str((active_appointment or {}).get("time") or "")[:5],
                "requested_slots": candidate_slots,
                "requested_date": first.get("requested_date") or "",
                "requested_time": "",
                "unavailable_reasons": unavailable_reasons,
                "service": requested_service,
                "updated_at": datetime.now().isoformat(),
            }
            await patch_conversation(conv_id, {"appointment_state": _json.dumps(alternative_state)})
            await _send_and_save_whatsapp_ai_text(phone, conv_id, reply, inbound_saved, {"appointment_state": alternative_state})
            return True
        parsed_dt = selected_slot
        known_service_address = _first_value(
            state.get("service_address"),
            message_service_address,
            (crm_contact or {}).get("address"),
            (crm_contact or {}).get("location"),
            fallback="",
        )
        known_postal_code = _extract_postal_code(known_service_address)
        known_intake = state.get("intake_data") or (active_appointment or {}).get("intake_data") or {}
        pending_probe = {
            **state,
            **parsed_dt,
            "service": requested_service,
            "service_address": known_service_address,
            "postal_code": known_postal_code or state.get("postal_code") or "",
            "intake_data": known_intake,
        }
        pending_missing = get_missing_booking_fields(
            crm_contact, pending_probe, active_appointment, delivery_mode=delivery_mode, require_intake=bool(intake_prompt)
        )
        if delivery_mode == "onsite" and "service_address" in pending_missing:
            pending_status = "awaiting_address"
        elif intake_prompt and any(field in pending_missing for field in ("units", "problem", "property_type")):
            pending_status = "awaiting_intake"
        else:
            pending_status = "pending_confirmation"
        pending = {
            **parsed_dt,
            "appointment_type": appointment_name,
            "service": requested_service,
            "delivery_mode": delivery_mode,
            "status": pending_status,
            "availability_checked": bool(parsed_dt.get("availability_checked", True)),
            "availability_status": parsed_dt.get("availability_status") or "available",
            "availability_reason": parsed_dt.get("availability_reason") or "available",
            "timezone": parsed_dt.get("timezone") or parsed_dt.get("resolved_timezone") or "Asia/Singapore",
            "resolved_timezone": parsed_dt.get("resolved_timezone") or parsed_dt.get("timezone") or "Asia/Singapore",
            "staff_id": parsed_dt.get("staff_id") or "",
            "staff_name": parsed_dt.get("staff_name") or "",
            "staff_role": parsed_dt.get("staff_role") or "",
            "contact_number": phone,
            "service_address": known_service_address,
            "postal_code": known_postal_code or state.get("postal_code") or "",
            "intake_data": known_intake,
            "action": requested_action,
            "existing_appointment_id": (active_appointment or {}).get("id") or "",
            "existing_date": (active_appointment or {}).get("date") or "",
            "existing_time": str((active_appointment or {}).get("time") or "")[:5],
            "updated_at": datetime.now().isoformat(),
        }
        await patch_conversation(conv_id, {"appointment_state": _json.dumps(pending)})
        if message_service_address:
            await _diagnostic_event("booking_field_extracted", phone=phone, conversation_id=conv_id, field="service_address", has_postal_code=bool(known_postal_code))
            await _diagnostic_event("booking_field_persisted", phone=phone, conversation_id=conv_id, field="service_address", target="appointment_state", success=True)
            await _persist_whatsapp_service_address(phone, pending["service_address"], pending.get("postal_code") or "")
        await _log_whatsapp_ai_event(phone, "appointment_pending_time_saved", f"date={pending['requested_date']} time={pending['requested_time']}")
        mode_text = {"online": "online", "phone": "by phone", "onsite": "on-site", "hybrid": "with the format confirmed by our team"}.get(delivery_mode, "")
        if pending["status"] == "awaiting_address":
            reply = f"A {duration}-minute {appointment_name.lower()} {_format_demo_slot(pending['requested_date'], pending['requested_time'])} is available. I’ll use this WhatsApp number as your contact. Please share the service address, then I’ll ask you to confirm."
        elif pending["status"] == "awaiting_intake":
            reply = f"A {duration}-minute {appointment_name.lower()} {_format_demo_slot(pending['requested_date'], pending['requested_time'])} is available. {intake_prompt}"
        elif requested_action == "reschedule":
            old_slot = _format_demo_slot(pending["existing_date"], pending["existing_time"])
            reply = f"I can move your {appointment_name.lower()} from {old_slot} to {_format_demo_slot(pending['requested_date'], pending['requested_time'])}. Shall I confirm the reschedule?"
        else:
            reply = f"A {duration}-minute {appointment_name.lower()} {_format_demo_slot(pending['requested_date'], pending['requested_time'])} {mode_text} works for you. Shall I confirm this slot?"
        await _send_and_save_whatsapp_ai_text(phone, conv_id, reply, inbound_saved, {"appointment_state": pending})
        return True

    if state_status == "alternatives_offered":
        await _diagnostic_event("appointment_state_unhandled", phone=phone, conversation_id=conv_id, state_status=state_status, action="safe_clarification")
        reply = f"Please share another preferred date and time for the {appointment_name.lower()}, for example: tomorrow at 4 PM."
        await _send_and_save_whatsapp_ai_text(phone, conv_id, reply, inbound_saved, {"appointment_state": state})
        return True

    if state.get("status") == "pending_confirmation" and _is_appointment_confirmation_reply(text):
        await _log_whatsapp_ai_event(phone, "appointment_confirmation_detected", f"state={state}")
        date_s = state.get("requested_date", "")
        time_s = state.get("requested_time", "")
        if not (date_s and time_s):
            await patch_conversation(conv_id, {"appointment_state": "{}"})
            return False
        await _log_whatsapp_ai_event(phone, "appointment_booking_started", f"date={date_s} time={time_s}")
        try:
            from db import check_slot, get_appointment_by_id, get_appointments_by_phone, get_existing_active_appointment, insert_appointment, reschedule_appointment, update_appointment_notes
            existing = await get_existing_active_appointment(phone, date_s, time_s)
            if existing:
                staff_name = existing.get("staff_name") or "our team"
                booking_id = str(existing.get("id") or "")[:8].upper()
                booked_state = {**state, "status": "booked", "booking_id": booking_id, "staff_name": staff_name, "duplicate_prevented": True, "booked_at": datetime.now().isoformat()}
                await patch_conversation(conv_id, {"appointment_state": _json.dumps(booked_state)})
                await _log_whatsapp_ai_event(phone, "appointment_duplicate_prevented", f"booking_id={booking_id} staff={staff_name}", "warning")
                reply = f"This {appointment_name.lower()} is already booked for {_format_demo_slot(date_s, time_s)} with {staff_name}. I will not create another booking."
                await _send_and_save_whatsapp_ai_text(phone, conv_id, reply, inbound_saved, {"appointment_state": booked_state})
                return True
            if not await check_slot(date_s, time_s):
                slots = await _suggest_available_demo_slots(date_s, time_s, 3)
                labels = [s["label"] for s in slots]
                await _log_whatsapp_ai_event(phone, "appointment_slot_unavailable", f"date={date_s} time={time_s} suggestions={labels}", "warning")
                if labels:
                    reply = f"That slot is already booked. Available slots are {', '.join(labels[:-1])}{' or ' if len(labels)>1 else ''}{labels[-1]}. Which one do you prefer?"
                else:
                    reply = "That slot is already booked. Please share another preferred time."
                await _send_and_save_whatsapp_ai_text(phone, conv_id, reply, inbound_saved, {"appointment_state": state})
                return True
            lead_name = _usable_whatsapp_name((crm_contact or {}).get("lead_name"), (crm_contact or {}).get("name"), conv.get("contact_name")) or "AirZen Lead"
            service_name = str(state.get("service") or requested_service or default_service).strip()
            is_reschedule = state.get("action") == "reschedule" and state.get("existing_appointment_id")
            service_start_code = ""
            if is_reschedule:
                if _atomic_outbox_required():
                    from db import _select_staff_round_robin, get_appointment_settings, get_appointment_by_id
                    settings = await get_appointment_settings()
                    start = datetime.strptime(f"{date_s} {time_s[:5]}", "%Y-%m-%d %H:%M")
                    staff = await _select_staff_round_robin(start, preferred_role=settings.get("default_staff_role") or "")
                    old_row = await get_appointment_by_id(state["existing_appointment_id"]) or {}
                    full_id = str(uuid.uuid4())
                    booked = {
                        "id": full_id,
                        "name": lead_name,
                        "phone": old_row.get("phone") or phone,
                        "date": date_s,
                        "time": time_s[:5],
                        "service": service_name,
                        "status": "booked",
                        "staff_id": (staff or {}).get("id", ""),
                        "staff_name": (staff or {}).get("name", ""),
                        "duration_minutes": settings.get("demo_duration_minutes") or 30,
                        "buffer_minutes": settings.get("buffer_minutes") or 15,
                        "timezone": settings.get("timezone") or "Asia/Singapore",
                        "created_at": datetime.now().isoformat(),
                        "rescheduled_from": state["existing_appointment_id"],
                        "conversation_id": conv_id,
                    }
                    rpc_result = await reschedule_appointment_with_notification_outbox_rpc(
                        state["existing_appointment_id"],
                        booked,
                        extra_context={"previous_appointment_datetime": state.get("previous_appointment_datetime") or ""},
                    )
                    if not rpc_result.get("success"):
                        raise RuntimeError(f"atomic_reschedule_outbox_failed:{rpc_result.get('reason') or rpc_result.get('error')}")
                    service_start_code = str(rpc_result.get("service_start_code") or "")
                    booking_id = full_id[:8].upper()
                else:
                    booked = await reschedule_appointment(state["existing_appointment_id"], date_s, time_s)
                    booking_id = str(booked.get("id") or "")[:8].upper()
            else:
                if _atomic_outbox_required():
                    from db import _select_staff_round_robin, get_appointment_settings
                    settings = await get_appointment_settings()
                    start = datetime.strptime(f"{date_s} {time_s[:5]}", "%Y-%m-%d %H:%M")
                    staff = await _select_staff_round_robin(start, preferred_role=settings.get("default_staff_role") or "")
                    full_id = str(uuid.uuid4())
                    booked = {
                        "id": full_id,
                        "name": lead_name,
                        "phone": phone,
                        "date": date_s,
                        "time": time_s[:5],
                        "service": service_name,
                        "status": "booked",
                        "staff_id": (staff or {}).get("id", ""),
                        "staff_name": (staff or {}).get("name", ""),
                        "duration_minutes": settings.get("demo_duration_minutes") or 30,
                        "buffer_minutes": settings.get("buffer_minutes") or 15,
                        "timezone": settings.get("timezone") or "Asia/Singapore",
                        "created_at": datetime.now().isoformat(),
                        "conversation_id": conv_id,
                    }
                    rpc_result = await create_appointment_with_notification_outbox_rpc(
                        booked,
                        conversation_id=conv_id,
                        extra_context={"customer_name": lead_name, "business_name": (crm_contact or {}).get("business_name") or "your business"},
                    )
                    if not rpc_result.get("success"):
                        raise RuntimeError(f"atomic_appointment_outbox_failed:{rpc_result.get('reason') or rpc_result.get('error')}")
                    service_start_code = str(rpc_result.get("service_start_code") or "")
                    booking_id = full_id[:8].upper()
                else:
                    booking_id = await insert_appointment(lead_name, phone, date_s, time_s, service_name, {
                        "service_address": state.get("service_address") or "",
                        "location_latitude": state.get("location_latitude"),
                        "location_longitude": state.get("location_longitude"),
                        "location_name": state.get("location_name") or "",
                        "location_url": state.get("location_url") or "",
                        "intake_data": state.get("intake_data") or {},
                    })
                    booked = {}
            try:
                booked = booked or await get_appointment_by_id(booking_id) or {}
                if not booked:
                    appts = await get_appointments_by_phone(phone)
                    booked = next((a for a in appts if a.get("date") == date_s and (a.get("time") or "")[:5] == time_s and a.get("status") == "booked"), {})
            except Exception as lookup_exc:
                # The appointment insert already succeeded. A secondary read or
                # schema-compatibility failure must not be reported as a failed booking.
                await _log_whatsapp_ai_event(phone, "appointment_post_insert_lookup_failed", str(lookup_exc)[:500], "warning")
            if not booked:
                booked = {
                    "id": booking_id,
                    "name": lead_name,
                    "phone": phone,
                    "date": date_s,
                    "time": time_s[:5],
                    "service": service_name,
                    "status": "booked",
                }
            staff_name = booked.get("staff_name") or "our team"
            booked = {**booked, "conversation_id": conv_id, "source": "WhatsApp reschedule" if is_reschedule else "WhatsApp booking"}
            service_address = str(state.get("service_address") or "").strip()
            if service_address:
                existing_notes = str(booked.get("notes") or "").strip()
                address_note = f"Service address: {service_address}"
                notes = existing_notes if address_note.lower() in existing_notes.lower() else "\n".join(filter(None, (existing_notes, address_note)))
                try:
                    await update_appointment_notes(str(booked.get("id") or booking_id), notes)
                    booked["notes"] = notes
                except Exception as notes_exc:
                    await _log_whatsapp_ai_event(phone, "appointment_address_note_save_failed", str(notes_exc)[:300], "warning")
            booked_state = {**state, "status": "booked", "booking_id": booking_id, "staff_name": staff_name, "booked_at": datetime.now().isoformat()}
            await patch_conversation(conv_id, {"appointment_state": _json.dumps(booked_state)})
            await _log_whatsapp_ai_event(phone, "appointment_booked", f"booking_id={booking_id} staff={staff_name}")
            company = _first_value((crm_contact or {}).get("business_name"), (crm_contact or {}).get("company_name"), (crm_contact or {}).get("business"), fallback="Not provided")
            if is_reschedule:
                notification_result = await enqueue_required_appointment_notifications(
                    {**booked, "name": lead_name, "phone": phone, "service": booked.get("service") or service_name},
                    event="rescheduled",
                    conversation_id=conv_id,
                    extra_context={
                        "customer_name": lead_name,
                        "business_name": company or "your business",
                        "staff_name": staff_name,
                        "previous_appointment_datetime": state.get("previous_appointment_datetime") or "",
                    },
                )
            else:
                notification_result = await _notify_appointment_booked(phone, booked, lead_name, company, staff_name, inbound_saved)
            booked_state["notifications"] = notification_result
            await patch_conversation(conv_id, {"appointment_state": _json.dumps(booked_state)})
            if notification_result.get("success") is False:
                await _log_whatsapp_ai_event(phone, "appointment_notification_outbox_failed", "; ".join(notification_result.get("errors") or [])[:500], "error")
                reply = f"Your {appointment_name.lower()} details were saved, but confirmation notifications are still pending. Our team will check and confirm shortly."
                await _send_and_save_whatsapp_ai_text(phone, conv_id, reply, inbound_saved, {"appointment_state": booked_state})
                return True
            action_text = "rescheduled" if is_reschedule else "booked"
            await _persist_whatsapp_booking_to_crm(phone, lead_name, booked_state, booked, service_name, booking_id, action_text)
            await _remember_whatsapp_insight(phone, f"Appointment {action_text}: {date_s} {time_s[:5]}; service={service_name}; booking_id={booking_id}")
            if delivery_mode == "online":
                next_step = "The meeting link will be shared shortly."
            elif delivery_mode == "phone":
                next_step = "Our team will call you at the booked time."
            elif delivery_mode == "onsite":
                next_step = f"Our {staff_label} will use the service location saved with your request, or contact you to confirm it."
            else:
                next_step = "Our team will confirm whether this is on-site, online, or by phone."
            if is_reschedule:
                reply = f"Done, your {appointment_name.lower()} is rescheduled for {_format_demo_slot(date_s, time_s)} with {staff_name}. {next_step}"
                start_instruction = _service_start_code_customer_instruction(service_start_code, staff_label)
                if start_instruction:
                    reply += f"\n\n{start_instruction}"
            else:
                known = _booking_known_fields(crm_contact, booked_state, booked)
                reply_lines = [
                    "Your appointment is confirmed.",
                    f"booked for: {_format_demo_slot(date_s, time_s)}",
                    "",
                    f"Service: {service_name}",
                    f"Units: {known.get('units') or '-'}",
                    f"Date: {date_s}",
                    f"Time: {time_s[:5]}",
                ]
                if known.get("service_address"):
                    reply_lines.append(f"Address: {known.get('service_address')}")
                if known.get("property_type"):
                    reply_lines.append(f"Property type: {known.get('property_type')}")
                reply_lines.append(f"{staff_label.title()}: {staff_name}")
                start_instruction = _service_start_code_customer_instruction(service_start_code, staff_label)
                if start_instruction:
                    reply_lines.extend(["", start_instruction])
                reply_lines.extend(["", "We look forward to serving you."])
                reply = "\n".join(reply_lines)
            await _send_and_save_whatsapp_ai_text(phone, conv_id, reply, inbound_saved, {"appointment_state": booked_state})
            return True
        except Exception as exc:
            await _log_whatsapp_ai_event(phone, "appointment_booking_failed", str(exc)[:500], "error")
            failed_state = {**state, "status": "booking_failed", "failed_at": datetime.now().isoformat()}
            await patch_conversation(conv_id, {"appointment_state": _json.dumps(failed_state)})
            safe_reply = _appointment_booking_failure_message(exc, appointment_name)
            await _send_and_save_whatsapp_ai_text(phone, conv_id, safe_reply, inbound_saved, {"appointment_state": failed_state, "error": str(exc)[:300]})
            return True

    if state_status in _APPOINTMENT_ACTIVE_STATUSES:
        recovery_missing = get_missing_booking_fields(
            crm_contact, state, active_appointment, delivery_mode=delivery_mode, require_intake=bool(intake_prompt)
        )
        recovery_state = dict(state)
        if recovery_missing:
            if any(field in recovery_missing for field in ("units", "problem", "property_type")):
                recovery_state["status"] = "awaiting_intake"
            elif "service_address" in recovery_missing:
                recovery_state["status"] = "awaiting_address"
            elif "preferred_date" in recovery_missing or "preferred_time" in recovery_missing:
                recovery_state["status"] = "awaiting_datetime"
            recovery_state["updated_at"] = datetime.now().isoformat()
            await patch_conversation(conv_id, {"appointment_state": _json.dumps(recovery_state)})
            reply = _next_booking_missing_field_prompt(recovery_missing, appointment_name)
        else:
            recovery_state["status"] = "pending_confirmation"
            recovery_state["updated_at"] = datetime.now().isoformat()
            await patch_conversation(conv_id, {"appointment_state": _json.dumps(recovery_state)})
            reply = _booking_confirmation_prompt(recovery_state, crm_contact, appointment_name, staff_label)
        await _diagnostic_event(
            "appointment_state_unhandled", phone=phone, conversation_id=conv_id,
            state_status=state_status or "unknown", action="deterministic_recovery",
        )
        await _send_and_save_whatsapp_ai_text(phone, conv_id, reply, inbound_saved, {"appointment_state": recovery_state})
        return True

    if _detect_inbound_intent(text) == "appointment":
        await _log_whatsapp_ai_event(phone, "whatsapp_appointment_intent_detected", "appointment_keyword")
    return False


async def _handle_inbound_intent(phone: str, intent: str, conv: dict) -> bool:
    """Dispatch (or schedule) a call for the detected intent and optionally
    send the matching confirmation template.

    Always tries to create a call action: immediately if outbound window is
    open, otherwise queued for the next allowed time (handled inside
    _schedule_or_start_call).

    Returns True if a confirmation template was successfully sent — in that
    case the caller should skip the AI free-form reply to avoid double-replying.
    """
    if intent not in ("callback", "appointment"):
        return False
    lead_name = conv.get("contact_name") or "there"
    if intent == "callback":
        event_type = "callback_scheduled"
        call_type = "callback_call"
        slot = "callback_confirmation_template"
    else:
        event_type = "appointment_confirmed"
        call_type = "appointment_confirmation"
        slot = "appointment_confirmation_template"

    contact = {
        "phone": phone,
        "lead_name": lead_name,
        "business_name": "",
        "service_type": "",
        "source": "whatsapp_inbound",
    }

    # Dispatch or schedule a call (respects outbound window inside the helper).
    try:
        call_status = await _schedule_or_start_call(
            phone, contact, call_type, event_type, "whatsapp_inbound",
            delay_minutes=0, rule=None,
        )
        logger.info("Inbound %s intent for %s → call_status=%s", intent, phone, call_status)
    except Exception as exc:
        logger.error("Inbound intent call dispatch failed for %s: %s", phone, exc)

    # Send confirmation template if configured for this slot.
    template = await resolve_wa_template(slot)
    if not template:
        return False
    language = await _get_wa_setting("WHATSAPP_DEFAULT_LANGUAGE") or "en"
    params = _build_template_params(slot, {"lead_name": lead_name}, None)
    try:
        result = await send_whatsapp_template(
            phone, template, language, params,
            event_type=event_type, source_type="whatsapp_intent",
            source_id=conv.get("id", "") or phone,
            template_purpose=slot,
        )
        return bool(result.get("success"))
    except Exception as exc:
        logger.error("Inbound intent template send failed for %s: %s", phone, exc)
        return False


async def _promote_waiting_for_reply_calls(phone: str) -> int:
    """Fix 3: When the customer replies on WhatsApp, promote any queued
    ``waiting_for_whatsapp_reply`` rows for this phone into actual call
    dispatches via _schedule_or_start_call (immediate if outbound window
    is open, otherwise queued for the next allowed time).

    Returns the number of rows promoted.
    """
    rows = [
        row for row in await get_automation_actions(phone=phone, limit=20)
        if row.get("action_type") == "waiting_for_whatsapp_reply"
        and row.get("status") == "waiting_schedule"
    ]

    if not rows:
        return 0

    promoted = 0
    import json as _json
    for row in rows:
        action_id = row.get("id", "")
        try:
            raw_payload = row.get("payload") or {}
            if isinstance(raw_payload, dict):
                payload = raw_payload
            else:
                payload = {}
                try:
                    payload = _json.loads(raw_payload or "{}")
                except Exception:
                    pass
            call_type = payload.get("call_type") or "welcome_call"
            event_type = row.get("event_type") or ""
            source = row.get("source") or ""
            contact = {k: payload.get(k, "") for k in ("phone", "lead_name", "business_name", "service_type", "source")}
            contact["phone"] = phone
            contact["source"] = contact.get("source") or source
            rule = payload.get("rule") or {}

            call_status = await _schedule_or_start_call(
                phone, contact, call_type, event_type, source,
                delay_minutes=0, rule=rule,
            )
            await update_automation_action_status(
                action_id, "completed",
                {"promoted_by_inbound_reply": True, "call_status": call_status},
            )
            promoted += 1
            logger.info("Promoted waiting_for_whatsapp_reply action %s for %s → %s", action_id, phone, call_status)
        except Exception as exc:
            logger.error("Failed to promote action %s: %s", action_id, exc)
            try:
                await update_automation_action_status(action_id, "failed", {}, str(exc)[:500])
            except Exception:
                pass

    return promoted


# ── Main inbound message handler ───────────────────────────────────────────

async def _handle_inbound_whatsapp_message_once(parsed: dict) -> None:
    """Process a single parsed inbound WhatsApp message:
    1. Normalize phone
    2. Get/create conversation
    3. CRM link
    4. Save message
    5. Check opt-out
    6. AI reply if enabled and window open
    """
    if parsed.get("is_status_update"):
        await _db().log_error("whatsapp_webhook", "whatsapp_status_update_received", f"provider={parsed.get('provider') or ''}; message_id={parsed.get('message_id') or ''}; status={parsed.get('status_value') or ''}", "info")
        await update_whatsapp_delivery_status(parsed)
        return

    provider_timestamp = str(parsed.get("timestamp") or "").strip()
    if provider_timestamp:
        try:
            provider_received_at = datetime.fromtimestamp(int(provider_timestamp), tz=timezone.utc)
            ingress_lag_seconds = max(0, int((datetime.now(timezone.utc) - provider_received_at).total_seconds()))
            await _diagnostic_event(
                "inbound_processing_lag", phone=parsed.get("phone") or "", message_id=parsed.get("message_id") or "",
                lag_seconds=ingress_lag_seconds,
                delayed=ingress_lag_seconds > 60,
                level="warning" if ingress_lag_seconds > 60 else "info",
            )
        except (TypeError, ValueError, OSError):
            await _diagnostic_event("inbound_timestamp_invalid", phone=parsed.get("phone") or "", message_id=parsed.get("message_id") or "", level="warning")

    phone_raw = parsed.get("phone", "")
    if not phone_raw:
        await _log_whatsapp_ai_reason("", "webhook_not_received", "Inbound webhook had no phone", "warning")
        return

    try:
        from db import normalize_phone
        phone = normalize_phone(phone_raw)
    except Exception:
        phone = phone_raw  # use as-is if normalization fails

    text = parsed.get("text", "")
    raw = parsed.get("raw", {})
    if text:
        try:
            if await handle_staff_appointment_chat_reply(phone, text, raw):
                return
            if await handle_staff_demo_status_reply(phone, text, raw):
                return
        except Exception as e:
            logger.error("Failed handling staff demo status reply: %s", e)
    await _diagnostic_event("customer_route_selected", phone=phone, route="customer_inbox")
    msg_type = parsed.get("message_type", "text")
    media_url = parsed.get("media_url", "")
    media_id = parsed.get("media_id", "")
    mime_type = parsed.get("mime_type", "")
    file_name = parsed.get("file_name", "")
    caption = parsed.get("caption", "")
    shared_location = parsed.get("location") or {}
    contact_name = parsed.get("contact_name", "")
    provider_msg_id = parsed.get("message_id", "")
    await _log_whatsapp_ai_event(phone, "inbound_received", f"type={msg_type} message_id={provider_msg_id} text={text[:120]}")
    await _db().log_error("whatsapp_inbox", "whatsapp_inbound_received", f"phone={phone}; provider={parsed.get('provider') or ''}; type={msg_type}; message_id={provider_msg_id}; has_text={bool((text or '').strip())}", "info")
    existing_inbound = await _provider_message_record(provider_msg_id) if provider_msg_id else {}
    retry_existing = _inbound_processing_retryable(existing_inbound)
    if existing_inbound and not retry_existing:
        await _diagnostic_event("duplicate_skipped", phone=phone, message_id=provider_msg_id, message_type=msg_type)
        if str(existing_inbound.get("provider_status") or "").strip().lower() == "processing":
            # Do not acknowledge a concurrent or recently interrupted delivery
            # as complete. Meta must retry until the first worker marks it
            # processed or the 60-second abandonment threshold permits recovery.
            raise InboundProcessingInProgress("inbound processing is still in progress")
        await _log_whatsapp_ai_reason(phone, "duplicate_message", _diagnostic_context(message_id=provider_msg_id), "info")
        return
    if retry_existing:
        await _diagnostic_event("inbound_processing_retried", phone=phone, message_id=provider_msg_id, message_type=msg_type)

    # Get/create conversation
    await _db().log_error("whatsapp_inbox", "whatsapp_conversation_get_or_create_started", f"phone={phone}; contact_name={contact_name[:80]}", "info")
    conv = await get_or_create_conversation(phone, contact_name)
    if not conv:
        logger.error("Could not get/create conversation for %s", phone)
        await _db().log_error("whatsapp_inbox", "whatsapp_conversation_get_or_create_failed", f"phone={phone}; reason=helper_returned_empty", "error")
        raise RuntimeError("conversation persistence failed")
    conv_id = conv["id"]
    await _diagnostic_event("inbound_accepted", phone=phone, conversation_id=conv_id, message_id=provider_msg_id, message_type=msg_type)
    await _db().log_error("whatsapp_inbox", "whatsapp_conversation_get_or_create_success", f"phone={phone}; conv_id={conv_id}; status={conv.get('status', '')}; ai_enabled={conv.get('ai_enabled', True)}", "info")
    await _log_whatsapp_ai_event(phone, "conversation_found_or_created", f"conversation_id={conv_id} status={conv.get('status', '')}")

    crm_link = None
    try:
        crm_link = await link_conversation_to_crm(conv_id, phone, contact_name)
        await _diagnostic_event("crm_contact_linked", phone=phone, conversation_id=conv_id, success=bool(crm_link))
    except Exception as exc:
        await _diagnostic_event("crm_contact_link_failed", phone=phone, conversation_id=conv_id, level="warning", exception_type=type(exc).__name__)

    crm_contact = None
    try:
        from db import get_crm_contact_by_phone
        crm_contact = await get_crm_contact_by_phone(phone)
    except Exception as exc:
        await _log_whatsapp_ai_event(phone, "crm_contact_found", f"found=false error={str(exc)[:200]}", "warning")
    else:
        crm_name = _usable_whatsapp_name((crm_contact or {}).get("lead_name"), (crm_contact or {}).get("name"))
        await _log_whatsapp_ai_event(phone, "crm_contact_found", f"found={bool(crm_contact)} name={crm_name}")
        if crm_name and not _usable_whatsapp_name(conv.get("contact_name")):
            conv["contact_name"] = crm_name

        # Sticky Telecaller Auto-Assignment: Link WhatsApp thread to the CRM contact's owner
        assigned_user = (crm_contact or {}).get("assigned_to") or (crm_contact or {}).get("assigned_telecaller")
        if assigned_user and not conv.get("assigned_to"):
            try:
                await patch_conversation(conv_id, {"assigned_to": str(assigned_user).strip()})
                conv["assigned_to"] = str(assigned_user).strip()
            except Exception as e:
                logger.warning("Could not auto-assign whatsapp conversation to %s: %s", assigned_user, e)

    # Save inbound message
    inbound_saved = dict(existing_inbound or {}) if retry_existing else {}
    await _db().log_error("whatsapp_inbox", "whatsapp_inbound_save_started", f"phone={phone}; conv_id={conv_id}; provider_message_id={provider_msg_id}", "info")
    try:
        if retry_existing:
            await _set_inbound_processing_status(provider_msg_id, "processing")
        else:
            inbound_saved = await save_wa_message(
                conv_id=conv_id,
                phone=phone,
                direction="inbound",
                message_type=msg_type,
                message_text=text,
                media_url=media_url,
                media_id=media_id,
                mime_type=mime_type,
                file_name=file_name,
                caption=caption,
                provider_message_id=provider_msg_id,
                provider_status="processing",
                raw_payload={
                    "raw": raw,
                    "media_id": media_id,
                    "media_url": media_url,
                    "mime_type": mime_type,
                    "file_name": file_name,
                    "caption": caption,
                    "image": raw.get("image") if isinstance(raw, dict) else {},
                    "document": raw.get("document") if isinstance(raw, dict) else {},
                    "audio": raw.get("audio") if isinstance(raw, dict) else {},
                    "location": shared_location,
                },
            )
        if inbound_saved:
            await _db().log_error("whatsapp_inbox", "whatsapp_inbound_message_saved", f"phone={phone}; conv_id={conv_id}; message_db_id={inbound_saved.get('id') or ''}; provider_message_id={provider_msg_id}", "info")
            await _db().log_error("whatsapp_inbox", "whatsapp_inbound_saved", f"phone={phone}; conversation_id={conv_id}; message_id={inbound_saved.get('id') or ''}; provider_message_id={provider_msg_id}", "info")
            await _log_wa(phone, "inbound_message", "", "en", [], "received", provider_msg_id, None, "webhook", parsed.get("provider") or "")
        else:
            await _db().log_error("whatsapp_inbox", "whatsapp_inbound_message_save_failed", f"phone={phone}; conv_id={conv_id}; provider_message_id={provider_msg_id}; error=save_returned_empty", "error")
    except Exception as exc:
        inbound_saved = {}
        await _db().log_error("whatsapp_inbox", "whatsapp_inbound_message_save_failed", f"phone={phone}; conv_id={conv_id}; provider_message_id={provider_msg_id}; error={str(exc)[:500]}", "error")

    if not inbound_saved:
        await _diagnostic_event(
            "whatsapp_message_persist_failed", phone=phone, conversation_id=conv_id,
            message_id=provider_msg_id, direction="inbound", reason="authoritative_insert_missing", level="error",
        )
        raise RuntimeError("inbound message persistence failed")

    # Update conversation last message
    try:
        await update_conversation_last_message(conv_id, text or f"[{msg_type}]", increment_unread=True)
        await _db().log_error("whatsapp_inbox", "whatsapp_conversation_last_message_updated", f"phone={phone}; conv_id={conv_id}; last_message={(text or f'[{msg_type}]')[:120]}", "info")
    except Exception as exc:
        await _db().log_error("whatsapp_inbox", "whatsapp_conversation_last_message_update_failed", f"phone={phone}; conv_id={conv_id}; error={str(exc)[:500]}", "error")

    if not crm_link:
        await _diagnostic_event(
            "crm_required_failed",
            phone=phone,
            conversation_id=conv_id,
            message_id=provider_msg_id,
            stage="inbound_before_ai",
            level="error",
        )
        await _db().log_error(
            "whatsapp_inbox",
            "whatsapp_crm_required_failed",
            f"phone={phone}; conv_id={conv_id}; message_id={provider_msg_id}; action=ai_suppressed",
            "error",
        )
        reply = "Thanks for contacting AirZen. Our team has received your message and will review it shortly."
        await _send_and_save_whatsapp_ai_text(
            phone,
            conv_id,
            reply,
            inbound_saved,
            {"crm_required_failed": True},
        )
        return

    followup_intent = detect_followup_intent(text)
    try:
        from db import (
            create_followup_action, get_setting, mark_lead_stop_automation,
            set_next_best_action, update_lead_journey, update_crm_contact_followup,
            update_crm_contact_status,
        )
        now_iso = datetime.now().isoformat()
        today_due = now_iso[:16]
        # Update CRM status & next due date for customer reply
        reply_status = "WhatsApp Replied"
        if followup_intent == "callback_request":
            reply_status = "Callback Requested"
        elif followup_intent == "demo_request":
            reply_status = "Site Visit / Demo Requested"

        await update_crm_contact_status(phone, reply_status, None)
        await update_crm_contact_followup(phone, today_due)
        await update_lead_journey(phone, {
            "last_customer_reply_at": now_iso,
            "last_intent": followup_intent or "whatsapp_replied",
            "crm_status": reply_status,
            "next_followup_at": today_due,
        })
        if followup_intent:
            await _db().log_error("followup", "customer_intent_detected", f"phone={phone}; intent={followup_intent}; text={text[:120]}", "info")
        tz_name = await get_setting("FOLLOWUP_TIMEZONE", "Asia/Singapore") or "Asia/Singapore"
        try:
            tzinfo = ZoneInfo(tz_name)
        except Exception:
            tz_name = "Asia/Singapore"
            tzinfo = ZoneInfo(tz_name)
        if followup_intent == "callback_request":
            now_local = datetime.now(tzinfo)
            scheduled_local = parse_followup_time(text, timezone=tz_name, now=now_local)
            scheduled_utc = scheduled_local.astimezone(timezone.utc)
            action_id = await create_followup_action(phone, "callback_requested", "call_only", "call", scheduled_utc, reason="whatsapp_callback_request", payload={"text": text, "scheduled_local": scheduled_local.isoformat()}, source="whatsapp", source_id=provider_msg_id)
            await update_lead_journey(phone, {"journey_stage": "callback_requested", "crm_status": "callback_requested", "preferred_channel": "call", "preferred_callback_at": scheduled_utc.isoformat()})
            await set_next_best_action(phone, "call_customer", "call", scheduled_utc, "whatsapp_callback_request")
            await _db().log_error("followup", "callback_scheduled", f"phone={phone}; source=whatsapp; action_id={action_id}; callback_time_text={text[:120]}; timezone_used={tz_name}; now_local={now_local.isoformat()}; parsed_scheduled_local={scheduled_local.isoformat()}; final_scheduled_local={scheduled_local.isoformat()}; final_scheduled_utc={scheduled_utc.isoformat()}; outbound_window_adjusted=false", "info")
        elif followup_intent == "message_later":
            scheduled_local = parse_followup_time(text, timezone=tz_name)
            scheduled_at = scheduled_local.astimezone(timezone.utc)
            action_id = await create_followup_action(phone, "message_followup_requested", "whatsapp_template", "whatsapp", scheduled_at, reason="whatsapp_message_later", payload={"template_purpose": "no_response_followup_template", "text": text, "scheduled_local": scheduled_local.isoformat()}, source="whatsapp", source_id=provider_msg_id)
            await update_lead_journey(phone, {"journey_stage": "message_followup_requested", "crm_status": "message_followup_requested", "preferred_channel": "whatsapp"})
            await set_next_best_action(phone, "message_customer", "whatsapp", scheduled_at, "whatsapp_message_later")
            await _db().log_error("followup", "whatsapp_followup_scheduled", f"phone={phone}; action_id={action_id}; scheduled_at={scheduled_at.isoformat()}", "info")
        elif followup_intent == "not_interested":
            await mark_lead_stop_automation(phone, "not_interested", "not_interested")
            await _db().log_error("followup", "automation_stopped_not_interested", f"phone={phone}", "info")
            return
        elif followup_intent == "wrong_number":
            await mark_lead_stop_automation(phone, "wrong_number", "wrong_number")
            await _db().log_error("followup", "automation_stopped_wrong_number", f"phone={phone}", "info")
            return
        elif followup_intent == "details_request":
            await update_lead_journey(phone, {"journey_stage": "details_requested", "next_best_action": "send_details", "next_action_channel": "whatsapp"})
        elif followup_intent == "demo_request":
            await update_lead_journey(phone, {"journey_stage": "demo_requested", "next_best_action": "book_demo", "next_action_channel": "whatsapp"})
        elif followup_intent == "reschedule_request":
            await update_lead_journey(phone, {"journey_stage": "demo_reschedule_requested", "crm_status": "demo_reschedule_requested", "next_best_action": "reschedule_demo"})
    except Exception as exc:
        await _log_whatsapp_ai_event(phone, "followup_intent_update_failed", str(exc)[:500], "warning")

    # Opt-out check
    if _is_opt_out(text):
        await patch_conversation(conv_id, {"ai_enabled": False, "status": "opted_out"})
        await _diagnostic_event("opted_out", phone=phone, conversation_id=conv_id, message_id=provider_msg_id)
        logger.info("Opt-out detected for %s", phone)
        return

    # Fix 3: Promote any waiting_for_whatsapp_reply rows for this phone
    # (runs regardless of ai_enabled — the call was already queued by the admin's rule).
    try:
        promoted = await _promote_waiting_for_reply_calls(phone)
    except Exception as exc:
        logger.warning("promote_waiting_for_reply_calls failed for %s: %s", phone, exc)
        promoted = 0

    # AI auto-reply
    await _db().log_error("whatsapp_ai", "whatsapp_ai_decision_started", f"phone={phone}; conv_id={conv_id}; msg_type={msg_type}", "info")
    ai_enabled = conv.get("ai_enabled", True)
    await _log_whatsapp_ai_event(phone, "ai_enabled_status", f"ai_enabled={ai_enabled} conversation_id={conv_id}")
    await _log_whatsapp_ai_event(phone, "takeover_status", f"takeover_enabled={bool(conv.get('assigned_to'))} assigned_to={conv.get('assigned_to', '')}")
    if not ai_enabled or conv.get("assigned_to"):
        reason = "takeover_enabled" if conv.get("assigned_to") else "ai_disabled"
        await _diagnostic_event("human_takeover_active" if conv.get("assigned_to") else "ai_disabled", phone=phone, conversation_id=conv_id, message_id=provider_msg_id)
        await _db().log_error("whatsapp_ai", "whatsapp_ai_enabled_false", f"phone={phone}; conv_id={conv_id}; reason={reason}", "info")
        await _log_whatsapp_ai_reason(phone, reason, f"conversation_id={conv_id}", "info")
        return
    await _db().log_error("whatsapp_ai", "whatsapp_ai_enabled_true", f"phone={phone}; conv_id={conv_id}", "info")

    # Only reply to text/button/interactive messages
    await _log_whatsapp_ai_event(phone, "message_type", f"type={msg_type}")
    if msg_type not in ("text", "button", "interactive"):
        await _db().log_error("whatsapp_ai", "whatsapp_ai_reply_skipped", f"phone={phone}; conv_id={conv_id}; reason=unsupported_message_type; type={msg_type}", "info")
        lock = _conversation_lock(phone)
        if lock.locked():
            await _log_whatsapp_ai_reason(phone, "already_processing_same_conversation", f"conversation_id={conv_id}", "info")
        async with lock:
            latest_conv = await get_conversation_by_id(conv_id) or conv
            if not latest_conv.get("ai_enabled", True) or latest_conv.get("assigned_to"):
                reason = "takeover_enabled" if latest_conv.get("assigned_to") else "ai_disabled"
                await _diagnostic_event("human_takeover_active" if latest_conv.get("assigned_to") else "ai_disabled", phone=phone, conversation_id=conv_id, message_id=provider_msg_id)
                await _db().log_error("whatsapp_ai", "whatsapp_ai_enabled_false", f"phone={phone}; conv_id={conv_id}; reason={reason}", "info")
                await _log_whatsapp_ai_reason(phone, reason, f"conversation_id={conv_id}", "info")
                return
            window_open = await is_whatsapp_service_window_open(phone)
            if not window_open and not inbound_saved:
                window_open = True
                await _db().log_error("whatsapp_ai", "whatsapp_ai_service_window_open", f"phone={phone}; conv_id={conv_id}; source=current_webhook_save_failed", "warning")
            await _log_whatsapp_ai_event(phone, "service_window_status", f"open={window_open} conversation_id={conv_id}")
            if not window_open:
                await _db().log_error("whatsapp_ai", "whatsapp_ai_service_window_closed", f"phone={phone}; conv_id={conv_id}; reason=service_window_closed", "info")
                await _log_whatsapp_ai_reason(phone, "outside_24h_window", f"conversation_id={conv_id}", "info")
                return
            await _db().log_error("whatsapp_ai", "whatsapp_ai_service_window_open", f"phone={phone}; conv_id={conv_id}", "info")
            await _send_whatsapp_media_auto_reply(phone, conv_id, msg_type, inbound_saved)
        return

    # Fix 2: Intent detection (skip if we already promoted a waiting call
    # for this phone — that call covers the customer's request).
    intent = _detect_inbound_intent(text)
    if promoted == 0 and intent == "callback" and followup_intent != "callback_request":
        try:
            await _handle_inbound_intent(phone, intent, conv)
        except Exception as exc:
            logger.error("Inbound callback intent handling failed for %s: %s", phone, exc)

    lock = _conversation_lock(phone)
    if lock.locked():
        await _log_whatsapp_ai_reason(phone, "already_processing_same_conversation", f"conversation_id={conv_id}", "info")

    async with lock:
        # Check the latest conversation state inside the per-phone lock.
        latest_conv = await get_conversation_by_id(conv_id) or conv
        await _log_whatsapp_ai_event(phone, "ai_enabled_status", f"latest_ai_enabled={latest_conv.get('ai_enabled', True)} conversation_id={conv_id}")
        await _log_whatsapp_ai_event(phone, "takeover_status", f"latest_takeover_enabled={bool(latest_conv.get('assigned_to'))} assigned_to={latest_conv.get('assigned_to', '')}")
        if not latest_conv.get("ai_enabled", True) or latest_conv.get("assigned_to"):
            reason = "takeover_enabled" if latest_conv.get("assigned_to") else "ai_disabled"
            await _diagnostic_event("human_takeover_active" if latest_conv.get("assigned_to") else "ai_disabled", phone=phone, conversation_id=conv_id, message_id=provider_msg_id)
            await _db().log_error("whatsapp_ai", "whatsapp_ai_enabled_false", f"phone={phone}; conv_id={conv_id}; reason={reason}", "info")
            await _log_whatsapp_ai_reason(phone, reason, f"conversation_id={conv_id}", "info")
            return
        await _db().log_error("whatsapp_ai", "whatsapp_ai_enabled_true", f"phone={phone}; conv_id={conv_id}; latest=true", "info")

        # Check 24h window
        window_open = await is_whatsapp_service_window_open(phone)
        if not window_open and not inbound_saved:
            window_open = True
            await _db().log_error("whatsapp_ai", "whatsapp_ai_service_window_open", f"phone={phone}; conv_id={conv_id}; source=current_webhook_save_failed", "warning")
        await _log_whatsapp_ai_event(phone, "service_window_status", f"open={window_open} conversation_id={conv_id}")
        if not window_open:
            await _db().log_error("whatsapp_ai", "whatsapp_ai_service_window_closed", f"phone={phone}; conv_id={conv_id}; reason=service_window_closed", "info")
            await _log_whatsapp_ai_reason(phone, "outside_24h_window", f"conversation_id={conv_id}", "info")
            return
        await _db().log_error("whatsapp_ai", "whatsapp_ai_service_window_open", f"phone={phone}; conv_id={conv_id}", "info")

        if not (text or "").strip():
            await _db().log_error("whatsapp_ai", "whatsapp_ai_reply_skipped", f"phone={phone}; conv_id={conv_id}; reason=no_text_message", "info")
            await _log_whatsapp_ai_reason(phone, "no_text_message", f"conversation_id={conv_id}", "info")
            return

        recent = await get_messages(conv_id, limit=12)
        try:
            appointment_inbound = dict(inbound_saved or {})
            if provider_timestamp:
                try:
                    appointment_inbound["created_at"] = datetime.fromtimestamp(int(provider_timestamp), tz=timezone.utc).isoformat()
                except (TypeError, ValueError, OSError):
                    pass
            if await _handle_whatsapp_appointment_state(phone, conv_id, text, latest_conv, appointment_inbound, crm_contact, shared_location, recent):
                return
        except Exception as exc:
            failed_state = _conversation_appointment_state(latest_conv)
            await _diagnostic_event(
                "appointment_route_failed", phone=phone, conversation_id=conv_id,
                message_id=provider_msg_id, stage="state_handler", exception_type=type(exc).__name__,
                has_date=bool(failed_state.get("requested_date")), has_time=bool(failed_state.get("requested_time")),
                state_status=failed_state.get("status") or "unknown", level="error",
            )
            recovered = _restore_booking_state(failed_state, crm_contact)
            if recovered.get("requested_date") and recovered.get("requested_time"):
                try:
                    from db import get_appointment_settings
                    fallback_settings = await get_appointment_settings()
                except Exception:
                    fallback_settings = {}
                fallback_delivery_mode = str(fallback_settings.get("delivery_mode") or "onsite").strip().lower()
                fallback_appointment_name = str(fallback_settings.get("appointment_name") or "appointment").strip()
                fallback_staff_label = str(fallback_settings.get("staff_label") or "staff member").strip().lower()
                fallback_intake_prompt = str(fallback_settings.get("intake_prompt") or "").strip()
                fallback, recovered, missing_fields = _booking_recovery_prompt(
                    recovered,
                    crm_contact,
                    fallback_appointment_name,
                    fallback_staff_label,
                    fallback_delivery_mode,
                    fallback_intake_prompt,
                )
                await _diagnostic_event(
                    "appointment_state_unhandled", phone=phone, conversation_id=conv_id,
                    stage="state_handler_exception_recovery", action="persisted_datetime_recovery",
                    fields_missing=",".join(missing_fields), state_status=recovered.get("status") or "unknown",
                )
                try:
                    await patch_conversation(conv_id, {"appointment_state": _json.dumps(recovered)})
                except Exception as patch_exc:
                    await _diagnostic_event(
                        "appointment_route_failed", phone=phone, conversation_id=conv_id,
                        stage="exception_recovery_patch", exception_type=type(patch_exc).__name__, level="warning",
                    )
            else:
                missing = get_missing_booking_fields(crm_contact, recovered)
                await _diagnostic_event(
                    "appointment_state_unhandled", phone=phone, conversation_id=conv_id,
                    stage="state_handler_exception_recovery", action="unusable_state",
                    fields_missing=",".join(missing), level="warning",
                )
                fallback = "I’m sorry, I could not continue the appointment request safely. Please resend your preferred date and time, and our team will help confirm it."
            try:
                await _send_and_save_whatsapp_ai_text(phone, conv_id, fallback, inbound_saved, {"appointment_state": recovered})
                await _diagnostic_event("appointment_safe_fallback_sent", phone=phone, conversation_id=conv_id, stage="state_handler")
            except Exception as send_exc:
                await _diagnostic_event(
                    "appointment_route_failed", phone=phone, conversation_id=conv_id,
                    stage="safe_fallback", exception_type=type(send_exc).__name__, level="error",
                )
            return

        inbound_row_id = (inbound_saved or {}).get("id") if isinstance(inbound_saved, dict) else ""
        prior_messages = [
            m for m in recent
            if not inbound_row_id or str(m.get("id") or "") != str(inbound_row_id)
        ]
        useful_history = [
            m for m in prior_messages
            if (m.get("message_text") or "").strip() or m.get("message_type") in ("template", "button", "interactive")
        ]
        conversation_mode = "in_progress" if useful_history else "new"
        await _log_whatsapp_ai_event(phone, "history_loaded", f"messages={len(recent)} prior_useful={len(useful_history)}")
        await _log_whatsapp_ai_event(phone, "conversation_mode", conversation_mode)
        await _log_whatsapp_ai_event(phone, "prompt_type_used", "whatsapp_chat")
        await _log_whatsapp_ai_event(phone, "ai_provider_selected", "gemini")
        whatsapp_model = await get_whatsapp_gemini_model()
        await _log_whatsapp_ai_event(phone, "whatsapp_gemini_model", f"whatsapp_gemini_model={whatsapp_model}")
        await _db().log_error("whatsapp_ai", "whatsapp_ai_generation_started", f"phone={phone}; conv_id={conv_id}; provider=gemini; model={whatsapp_model}; mode={conversation_mode}", "info")
        await _log_whatsapp_ai_event(phone, "ai_generation_started", f"history_messages={len(recent)} mode={conversation_mode}")
        latest_conv = dict(latest_conv or {})
        crm_name = _usable_whatsapp_name((crm_contact or {}).get("lead_name"), (crm_contact or {}).get("name"))
        if crm_name and not _usable_whatsapp_name(latest_conv.get("contact_name")):
            latest_conv["contact_name"] = crm_name
        durable_memory = []
        try:
            from db import get_contact_memory
            durable_memory = await get_contact_memory(phone)
        except Exception as exc:
            await _log_whatsapp_ai_event(phone, "contact_memory_load_failed", str(exc)[:300], "warning")
        ai_result = await generate_whatsapp_ai_reply(latest_conv, text, recent, conversation_mode, crm_contact, durable_memory)
        if isinstance(ai_result, dict):
            reply_text = (ai_result.get("reply") or "").strip()
            ai_reason = ai_result.get("reason") or ""
            ai_error = ai_result.get("error") or ""
        else:
            reply_text = (ai_result or "").strip()
            ai_reason = "" if reply_text else "ai_generation_failed"
            ai_error = ""
        if isinstance(ai_result, dict):
            await _log_whatsapp_ai_event(phone, "ai_provider_selected", ai_result.get("provider") or "gemini")
            await _log_whatsapp_ai_event(phone, "prompt_type_used", ai_result.get("prompt_type") or "whatsapp_chat")
            await _log_whatsapp_ai_event(phone, "prompt_source", ai_result.get("prompt_source") or "default")
            if ai_result.get("model"):
                await _log_whatsapp_ai_event(phone, "whatsapp_gemini_model", f"whatsapp_gemini_model={ai_result.get('model')}")
        if not reply_text:
            reason = ai_reason or "ai_generation_failed"
            await _db().log_error("whatsapp_ai", "whatsapp_ai_generation_failed", f"phone={phone}; conv_id={conv_id}; reason={reason}; error={ai_error[:300]}", "error" if reason == "ai_generation_failed" else "warning")
            await _log_whatsapp_ai_event(phone, "ai_generation_failed", ai_error or reason, "error" if reason == "ai_generation_failed" else "warning")
            await _log_whatsapp_ai_reason(phone, reason, ai_error or f"conversation_id={conv_id}", "error" if reason == "ai_generation_failed" else "warning")
            reply_text = "Thanks, I received your message. Our team will check and get back shortly."
        else:
            await _db().log_error("whatsapp_ai", "whatsapp_ai_generation_success", f"phone={phone}; conv_id={conv_id}; chars={len(reply_text)}", "info")
            await _log_whatsapp_ai_event(phone, "ai_generation_success", f"chars={len(reply_text)}")

        reply_text, booking_claim_blocked = _guard_unpersisted_booking_claim(reply_text, latest_conv, crm_contact)
        if booking_claim_blocked:
            await _diagnostic_event(
                "unpersisted_booking_claim_blocked", phone=phone, conversation_id=conv_id,
                message_id=provider_msg_id, replacement="safe_booking_clarification", level="warning",
            )
            await _log_whatsapp_ai_event(phone, "unpersisted_booking_claim_blocked", "replacement=safe_booking_clarification", "warning")

        await _diagnostic_event("response_generated", phone=phone, conversation_id=conv_id, message_id=provider_msg_id, source="fallback" if not (ai_result.get("reply") if isinstance(ai_result, dict) else ai_result) else "ai")

        await _db().log_error("whatsapp_ai", "whatsapp_ai_send_started", f"phone={phone}; conv_id={conv_id}; chars={len(reply_text)}", "info")
        await _log_whatsapp_ai_event(phone, "whatsapp_text_send_started", f"chars={len(reply_text)}")
        send_result = await send_whatsapp_text(phone, reply_text)
        provider_id = send_result.get("provider_message_id") or ""
        if send_result.get("success"):
            await _db().log_error("whatsapp_ai", "whatsapp_ai_send_success", f"phone={phone}; conv_id={conv_id}; provider_message_id={provider_id}", "info")
            await _log_whatsapp_ai_event(phone, "whatsapp_text_send_success", f"provider_message_id={provider_id}")
        else:
            await _db().log_error("whatsapp_ai", "whatsapp_ai_send_failed", f"phone={phone}; conv_id={conv_id}; reason={send_result.get('reason')}; error={(send_result.get('error') or '')[:300]}", "error")
            await _log_whatsapp_ai_event(phone, "whatsapp_text_send_failed", send_result.get("error") or "", "error")
        outbound_saved = await save_wa_message(
            conv_id=conv_id,
            phone=phone,
            direction="outbound",
            message_type="text",
            message_text=reply_text,
            provider_message_id=provider_id,
            provider_status="sent" if send_result.get("success") else "failed",
            failure_reason="" if send_result.get("success") else (send_result.get("reason") or send_result.get("error") or ""),
            raw_payload={"reply_to_message_id": inbound_saved.get("id") if inbound_saved else "", "send_result": send_result},
            ai_generated=True,
        )
        await _db().log_error("whatsapp_ai", "whatsapp_ai_outbound_message_saved", f"phone={phone}; conv_id={conv_id}; message_db_id={(outbound_saved or {}).get('id') or ''}; provider_status={'sent' if send_result.get('success') else 'failed'}", "info" if outbound_saved else "error")
        await _log_whatsapp_ai_event(phone, "outbound_ai_message_saved", f"provider_status={'sent' if send_result.get('success') else 'failed'}")
        if send_result.get("success") and outbound_saved:
            await update_conversation_last_message(conv_id, reply_text, increment_unread=False)
        elif send_result.get("success"):
            await _diagnostic_event(
                "whatsapp_message_persist_failed", phone=phone, conversation_id=conv_id,
                message_id=provider_id, direction="outbound", reason="authoritative_insert_missing", level="error",
            )
        else:
            await _log_whatsapp_ai_reason(phone, "whatsapp_send_failed", send_result.get("error") or "", "error")
        return

    if False:
        # Legacy flow below is intentionally unreachable; retained for context
        # after the lock-safe flow above.
        if intent:
            try:
                template_sent = await _handle_inbound_intent(phone, intent, conv)
            except Exception as exc:
                logger.error("Inbound intent handling failed for %s: %s", phone, exc)
                template_sent = False
            if template_sent:
                # Confirmation template already sent — don't double-reply with AI.
                return

    # Check 24h window
    window_open = await is_whatsapp_service_window_open(phone)
    if not window_open:
        logger.info("24h window closed for %s — skipping AI reply", phone)
        return

    # Get recent messages for context
    recent = await get_messages(conv_id, limit=10)

    # Generate AI reply
    reply_text = await generate_whatsapp_ai_reply(conv, text, recent)
    if not reply_text:
        return

    # Send reply
    send_result = await send_whatsapp_text(phone, reply_text)
    provider_id = send_result.get("provider_message_id") or ""

    # Save outbound AI message
    await save_wa_message(
        conv_id=conv_id,
        phone=phone,
        direction="outbound",
        message_type="text",
        message_text=reply_text,
        provider_message_id=provider_id,
        provider_status="sent" if send_result.get("success") else "failed",
        ai_generated=True,
    )
    if send_result.get("success"):
        await update_conversation_last_message(conv_id, reply_text, increment_unread=False)
    else:
        logger.warning("AI reply send failed for %s: %s", phone, send_result.get("error"))


async def handle_inbound_whatsapp_message(parsed: dict) -> None:
    """Run one inbound message and persist whether processing reached a terminal state."""
    if parsed.get("is_status_update"):
        await _handle_inbound_whatsapp_message_once(parsed)
        return
    provider_message_id = str(parsed.get("message_id") or "").strip()
    try:
        await _handle_inbound_whatsapp_message_once(parsed)
    except InboundProcessingInProgress:
        raise
    except Exception as exc:
        await _set_inbound_processing_status(provider_message_id, "processing_failed")
        await _diagnostic_event(
            "inbound_processing_failed", phone=parsed.get("phone") or "",
            message_id=provider_message_id, exception_type=type(exc).__name__, level="error",
        )
        raise
    else:
        await _set_inbound_processing_status(provider_message_id, "processed")
        await _diagnostic_event(
            "inbound_processing_completed", phone=parsed.get("phone") or "",
            message_id=provider_message_id,
        )


# ── Appointment Reminders Runner ──────────────────────────────────────────

async def run_due_appointment_reminders() -> None:
    try:
        from db import get_due_reminder_appointments, get_appointment_settings, get_appointment_staff, update_appointment_notifications, log_error
        settings = await get_appointment_settings()
        window_minutes = settings.get("reminder_before_minutes", 60)
        due_appointments = await get_due_reminder_appointments(window_minutes)

        if not due_appointments:
            await check_demo_completions()
            return

        await log_error("appointment_reminder", "appointment_reminder_scan_started", f"Found {len(due_appointments)} due appointments", "info")

        staff_list = await get_appointment_staff(include_inactive=True)

        for appt in due_appointments:
            appt_id = appt.get("id")
            phone = appt.get("phone", "")
            customer_name = appt.get("name", "")
            date_s = appt.get("date", "")
            time_s = appt.get("time", "")
            staff_id = appt.get("staff_id", "")
            staff_name = appt.get("staff_name", "")

            # Get staff details
            staff = next((s for s in staff_list if s.get("id") == staff_id), {})
            staff_phone = staff.get("whatsapp_number") or ""

            updates = {}
            errors = []
            now_str = datetime.now(timezone.utc).isoformat()
            tz_name = appt.get("timezone") or settings.get("timezone") or "Asia/Singapore"
            appt_dt = _appointment_local_dt(date_s, time_s, tz_name)
            now_local = _appointment_local_now(tz_name)
            if appt_dt and appt_dt <= now_local:
                updates["reminder_processed"] = True
                updates["reminder_processed_at"] = now_str
                updates["reminder_error"] = "appointment_time_in_past"
                await update_appointment_notifications(appt_id, updates)
                await log_error("appointment_reminder", "reminder_skipped_past_appointment", f"appointment_id={appt_id}; date={date_s}; time={time_s}; timezone={tz_name}", "warning")
                continue

            queued_errors = []
            if settings.get("customer_reminder_enabled") and not appt.get("customer_reminder_sent"):
                customer_intent = await enqueue_appointment_notification(
                    appt,
                    "appointment_customer_reminder",
                    "customer",
                    scheduled_at=now_str,
                )
                if not customer_intent.get("success"):
                    queued_errors.append(customer_intent.get("reason") or customer_intent.get("error") or "customer_reminder_outbox_failed")
            if settings.get("staff_reminder_enabled") and not appt.get("staff_reminder_sent"):
                staff_intent = await enqueue_appointment_notification(
                    appt,
                    "appointment_staff_reminder",
                    "staff",
                    scheduled_at=now_str,
                )
                if not staff_intent.get("success"):
                    queued_errors.append(staff_intent.get("reason") or staff_intent.get("error") or "staff_reminder_outbox_failed")
            if not queued_errors:
                updates["reminder_processed"] = True
                updates["reminder_processed_at"] = now_str
                await update_appointment_notifications(appt_id, updates)
                await log_error("appointment_reminder", "appointment_reminder_outbox_queued", f"appointment_ref={_safe_ref(appt_id)}", "info")
                continue
            updates["reminder_error"] = "; ".join(queued_errors)[:1000]
            await update_appointment_notifications(appt_id, updates)
            await log_error("appointment_reminder", "appointment_reminder_outbox_failed", f"appointment_ref={_safe_ref(appt_id)}; errors={updates['reminder_error']}", "error")
            continue

            # 1. Customer Reminder
            if settings.get("customer_reminder_enabled") and not appt.get("customer_reminder_sent"):
                template_purpose = "reminder_template"
                template = await resolve_wa_template(template_purpose)
                if template:
                    language = await _get_wa_setting("WHATSAPP_DEFAULT_LANGUAGE") or "en"
                    params = _build_template_params(
                        template_purpose,
                        {"lead_name": customer_name, "business_name": "our company"},
                        {"appointment_date": date_s, "appointment_time": time_s},
                    )
                    await log_error("appointment_reminder", "reminder_template_send_started", f"phone={phone}; reminder_template_name={template}; params_count={len(params)}; template_purpose={template_purpose}", "info")
                    result = await send_whatsapp_template(
                        phone, template, language, params,
                        event_type="appointment_reminder",
                        source_type="whatsapp_ai",
                        source_id=appt_id or phone,
                        template_purpose=template_purpose,
                        template_context={
                            **appt,
                            "customer_name": customer_name,
                            "appointment_date": date_s,
                            "appointment_time": time_s,
                            "staff_name": staff_name,
                        },
                    )
                    if result.get("success"):
                        updates["customer_reminder_sent"] = True
                        updates["customer_reminder_sent_at"] = now_str
                    else:
                        err = result.get("error") or result.get("reason")
                        errors.append(f"customer_template_failed: {err}")
                        if "#100" in str(err) or "parameter" in str(err).lower():
                            await log_error("appointment_reminder", "reminder_template_param_mismatch", f"phone={phone}; reminder_template_name={template}; params_count={len(params)}; template_purpose={template_purpose}; error={str(err)[:300]}", "error")
                        # Fallback to normal text
                        customer_text = f"Hi {customer_name}, this is a reminder for your upcoming appointment on {_format_demo_slot(date_s, time_s)} with {staff_name}. See you soon!"
                        fallback = await send_whatsapp_text(phone, customer_text)
                        if fallback.get("success"):
                            updates["customer_reminder_sent"] = True
                            updates["customer_reminder_sent_at"] = now_str
                        else:
                            errors.append(f"customer_text_failed: {fallback.get('error') or fallback.get('reason')}")
                else:
                    # Template missing, fallback to text directly
                    errors.append("customer_reminder_template_missing")
                    customer_text = f"Hi {customer_name}, this is a reminder for your upcoming appointment on {_format_demo_slot(date_s, time_s)} with {staff_name}. See you soon!"
                    fallback = await send_whatsapp_text(phone, customer_text)
                    if fallback.get("success"):
                        updates["customer_reminder_sent"] = True
                        updates["customer_reminder_sent_at"] = now_str
                    else:
                        errors.append(f"customer_text_failed: {fallback.get('error') or fallback.get('reason')}")
            elif not settings.get("customer_reminder_enabled"):
                errors.append("skipped_customer_disabled")

            # 2. Staff Reminder
            if settings.get("staff_reminder_enabled") and not appt.get("staff_reminder_sent"):
                if staff_phone:
                    staff_template_purpose = "staff_appointment_reminder_template"
                    staff_template = await resolve_wa_template(staff_template_purpose)
                    staff_msg = f"Reminder: You have an upcoming appointment with {customer_name} on {_format_demo_slot(date_s, time_s)}."
                    if staff_template:
                        staff_params = _build_template_params(
                            staff_template_purpose,
                            {"lead_name": customer_name, "phone": phone, "service": appt.get("service") or "Appointment", "source": appt.get("source") or "WhatsApp"},
                            {"appointment_datetime": _format_appointment_datetime_for_staff(appt), "appointment_date": date_s, "appointment_time": time_s},
                        )
                        await log_error("appointment_reminder", "staff_reminder_template_send_started", f"phone={staff_phone}; reminder_template_name={staff_template}; params_count={len(staff_params)}; template_purpose={staff_template_purpose}", "info")
                        staff_result = await send_whatsapp_template(
                            staff_phone,
                            staff_template,
                            await _get_wa_setting("WHATSAPP_DEFAULT_LANGUAGE") or "en",
                            staff_params,
                            event_type="staff_appointment_reminder",
                            source_type="appointment",
                            source_id=appt_id or phone,
                            template_purpose=staff_template_purpose,
                            template_context={
                                **appt,
                                "customer_name": customer_name,
                                "customer_phone": phone,
                                "appointment_datetime": _format_appointment_datetime_for_staff(appt),
                                "appointment_date": date_s,
                                "appointment_time": time_s,
                            },
                        )
                    elif await is_whatsapp_service_window_open(staff_phone):
                        staff_result = await send_whatsapp_text(staff_phone, staff_msg)
                    else:
                        staff_result = {"success": False, "error": "staff_reminder_template_missing_service_window_closed", "reason": "template_missing"}
                    if staff_result.get("success"):
                        updates["staff_reminder_sent"] = True
                        updates["staff_reminder_sent_at"] = now_str
                    else:
                        errors.append(f"staff_whatsapp_failed: {staff_result.get('error') or staff_result.get('reason')}")
                else:
                    errors.append("staff_whatsapp_missing")
            elif not settings.get("staff_reminder_enabled"):
                errors.append("skipped_staff_disabled")

            updates["reminder_processed"] = True
            updates["reminder_processed_at"] = now_str
            if errors:
                updates["reminder_error"] = "; ".join(errors)[:1000]

            await update_appointment_notifications(appt_id, updates)
            await log_error("appointment_reminder", "appointment_reminder_processed", f"Processed reminder for {appt_id}, errors: {updates.get('reminder_error')}", "info")

        await check_demo_completions()

    except Exception as exc:
        import traceback
        trace = traceback.format_exc()
        logger.error("run_due_appointment_reminders error: %s", exc)
        from db import log_error
        await log_error("appointment_reminder", "appointment_reminder_runner_failed", str(trace)[:2000], "error")


async def get_demo_completion_template_and_purpose() -> tuple[str, str]:
    # 1. Try staff_demo_status_request
    template = await resolve_wa_template("staff_demo_status_request")
    if template:
        return template, "staff_demo_status_request"
    # 2. Try override in settings: DEMO_COMPLETION_STAFF_TEMPLATE
    from db import get_setting
    setting_val = await get_setting("DEMO_COMPLETION_STAFF_TEMPLATE", "staff_demo_status_request")
    if setting_val and setting_val != "staff_demo_status_request":
        resolved = await resolve_wa_template(setting_val)
        if resolved:
            return resolved, _template_purpose_key(setting_val)
    # 3. Fallbacks:
    fallback_1 = await resolve_wa_template("staff_handoff_notification_template")
    if fallback_1:
        return fallback_1, "staff_handoff_notification_template"

    fallback_2 = await resolve_wa_template("staff_appointment_reminder_template")
    if fallback_2:
        return fallback_2, "staff_appointment_reminder_template"

    return "", ""


async def check_demo_completions() -> None:
    try:
        from db import (
            get_setting, get_all_appointments, get_appointment_staff,
            update_appointment_demo_fields, log_error, normalize_phone
        )

        enabled = (await get_setting("DEMO_COMPLETION_CHECK_ENABLED", "true")).lower() in ("true", "1", "yes", "on")
        if not enabled:
            return

        await log_error("appointment_reminder", "demo_completion_check_due", "Starting demo completion checks", "info")

        delay_min = int(await get_setting("DEMO_COMPLETION_CHECK_DELAY_MINUTES", "15"))
        max_requests = int(await get_setting("DEMO_COMPLETION_MAX_REQUESTS", "2"))
        cooldown_min = int(await get_setting("DEMO_COMPLETION_REQUEST_COOLDOWN_MINUTES", "60"))

        use_template = (await get_setting("DEMO_COMPLETION_USE_TEMPLATE", "true")).lower() in ("true", "1", "yes", "on")

        booked = await get_all_appointments(status_filter="booked")
        if not booked:
            return

        staff_list = await get_appointment_staff(include_inactive=True)
        now = datetime.now(timezone.utc)

        for appt in booked:
            appt_id = appt.get("id")
            phone = appt.get("phone") or appt.get("phone_number") or ""
            customer_name = appt.get("name", "")
            date_s = appt.get("date", "")
            time_s = appt.get("time", "")
            staff_id = appt.get("staff_id", "")

            try:
                tz_name = appt.get("timezone") or "Asia/Singapore"
                tzinfo = ZoneInfo(tz_name)
                dt_str = f"{date_s} {time_s[:5]}"
                local_dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M").replace(tzinfo=tzinfo)
                appt_utc = local_dt.astimezone(timezone.utc)
            except Exception as e:
                logger.error("Failed parsing datetime for appointment %s: %s", appt_id, e)
                continue

            if now < appt_utc + timedelta(minutes=delay_min):
                continue

            if appt.get("demo_result"):
                continue

            req_count = int(appt.get("demo_status_request_count") or 0)
            if req_count >= max_requests:
                continue

            req_at_str = appt.get("demo_status_requested_at")
            if req_at_str:
                try:
                    req_at = datetime.fromisoformat(req_at_str)
                    if req_at.tzinfo is None:
                        req_at = req_at.replace(tzinfo=timezone.utc)
                    if now < req_at + timedelta(minutes=cooldown_min):
                        continue
                except Exception:
                    pass

            staff = next((s for s in staff_list if s.get("id") == staff_id), {})
            staff_phone = staff.get("whatsapp_number") or ""
            if not staff_phone:
                continue

            success = False
            msg_id = ""
            err_msg = ""

            staff_msg = (
                f"Appointment outcome update needed.\n\n"
                f"Customer: {customer_name}\n"
                f"Phone: {phone}\n"
                f"Appointment Time: {date_s} {time_s}\n\n"
                f"Please reply:\n"
                f"1 - Demo completed\n"
                f"2 - Customer no-show\n"
                f"3 - Rescheduled\n"
                f"4 - Customer interested\n"
                f"5 - Not interested\n"
                f"6 - Need follow-up"
            )

            outcome_intent = await enqueue_appointment_notification(
                {**appt, "name": customer_name, "phone": phone, "staff_whatsapp_number": staff_phone},
                "staff_outcome_request",
                "staff",
                recipient_phone=staff_phone,
                extra_context={
                    "customer_name": customer_name,
                    "customer_phone": phone,
                    "appointment_datetime": f"{date_s} {time_s}",
                    "appointment_date": date_s,
                    "appointment_time": time_s,
                },
            )
            if outcome_intent.get("success"):
                updates = {
                    "demo_status_requested": True,
                    "demo_status_requested_at": datetime.now(timezone.utc).isoformat(),
                    "demo_status_request_count": req_count + 1,
                }
                await update_appointment_demo_fields(appt_id, updates)
                await log_error("appointment_reminder", "demo_status_request_outbox_queued", f"appointment_ref={_safe_ref(appt_id)}", "info")
                continue
            err_msg = outcome_intent.get("error") or outcome_intent.get("reason") or "staff_outcome_request_outbox_failed"
            await log_error("appointment_reminder", "demo_status_request_failed", f"appointment_ref={_safe_ref(appt_id)}; error={_sanitize_outbox_error(err_msg)}", "error")
            continue

            if use_template:
                template_name, template_purpose = await get_demo_completion_template_and_purpose()
                if template_name:
                    lang = await _get_wa_setting("WHATSAPP_DEFAULT_LANGUAGE") or "en"
                    params = _build_template_params(
                        template_purpose,
                        {"lead_name": customer_name, "phone": phone},
                        {"appointment_datetime": f"{date_s} {time_s}", "date": date_s, "time": time_s}
                    )
                    await log_error("appointment_reminder", "demo_status_request_template_send_started", f"phone={staff_phone}; template={template_name}", "info")
                    res = await send_whatsapp_template(
                        staff_phone, template_name, lang, params,
                        event_type="staff_demo_status_request",
                        source_type="appointment",
                        source_id=appt_id,
                        template_purpose=template_purpose,
                        template_context={
                            **appt,
                            "customer_name": customer_name,
                            "customer_phone": phone,
                            "appointment_datetime": f"{date_s} {time_s}"
                        }
                    )
                    success = res.get("success", False)
                    msg_id = res.get("provider_message_id") or ""
                    err_msg = res.get("error") or res.get("reason") or ""
                else:
                    await log_error("appointment_reminder", "template_missing", f"No template configured for demo status request or fallbacks. Skipped template send.", "warning")
                    if await is_whatsapp_service_window_open(staff_phone):
                        res = await send_whatsapp_text(staff_phone, staff_msg)
                        success = res.get("success", False)
                        msg_id = res.get("provider_message_id") or ""
                        err_msg = res.get("error") or res.get("reason") or ""
            else:
                if await is_whatsapp_service_window_open(staff_phone):
                    res = await send_whatsapp_text(staff_phone, staff_msg)
                    success = res.get("success", False)
                    msg_id = res.get("provider_message_id") or ""
                    err_msg = res.get("error") or res.get("reason") or ""
                else:
                    err_msg = "service_window_closed"

            if success:
                await log_error("appointment_reminder", "demo_status_request_sent", f"Sent demo status request to staff {staff_phone} for appointment {appt_id}", "info")
                updates = {
                    "demo_status_requested": True,
                    "demo_status_requested_at": now.isoformat(),
                    "demo_status_request_count": req_count + 1,
                    "demo_status_reply_message_id": msg_id
                }
                await update_appointment_demo_fields(appt_id, updates)
            else:
                await log_error("appointment_reminder", "demo_status_request_failed", f"Failed sending request to staff {staff_phone} for appointment {appt_id}: {err_msg}", "warning")

    except Exception as exc:
        logger.error("check_demo_completions error: %s", exc)
        try:
            from db import log_error
            await log_error("appointment_reminder", "demo_status_request_failed", f"check_demo_completions run failed: {exc}", "error")
        except Exception:
            pass


def parse_staff_reply(reply: str) -> Optional[str]:
    clean = reply.strip().lower()
    if clean in ("1", "completed", "done", "demo completed"):
        return "completed"
    if clean in ("2", "no show", "not attended", "customer no-show", "no_show"):
        return "no_show"
    if clean in ("3", "rescheduled", "reschedule"):
        return "rescheduled"
    if clean in ("4", "interested", "customer interested"):
        return "interested"
    if clean in ("5", "not interested", "not_interested"):
        return "not_interested"
    if clean in ("6", "follow-up", "follow up", "need follow-up", "need_followup"):
        return "need_followup"
    if "1" in clean or "completed" in clean or "done" in clean:
        return "completed"
    if "2" in clean or "no show" in clean or "not attended" in clean:
        return "no_show"
    if "3" in clean or "rescheduled" in clean:
        return "rescheduled"
    if "4" in clean or "interested" in clean:
        return "interested"
    if "5" in clean or "not interested" in clean:
        return "not_interested"
    if "6" in clean or "follow-up" in clean or "follow up" in clean:
        return "need_followup"
    return None


def _staff_appointment_command(text: str) -> str:
    clean = " ".join(str(text or "").strip().lower().split())
    if re.search(r"\b(details?|summary|job info|service info)\b", clean):
        return "details"
    if re.match(r"^(start|started|service started|start service)\b", clean):
        return "start"
    if re.match(r"^(service\s+)?(is\s+)?(complete|completed|done)\b", clean):
        return "complete"
    return ""


def _appointment_intake_summary(appointment: dict) -> str:
    intake = appointment.get("intake_data") or {}
    if isinstance(intake, str):
        try:
            intake = _json.loads(intake)
        except Exception:
            intake = {"service_summary": intake}
    return str(intake.get("service_summary") or "").strip() if isinstance(intake, dict) else ""


def _format_staff_service_summary(appointment: dict) -> str:
    booking_ref = str(appointment.get("id") or "")[:8].upper() or "Pending"
    lines = [
        "AirZen Service Summary",
        f"Booking: {booking_ref}",
        f"Customer: {appointment.get('name') or 'Customer'}",
        f"Contact: {appointment.get('phone') or 'Not provided'}",
        f"Service: {appointment.get('service') or 'Service appointment'}",
        f"Appointment: {_format_demo_slot(str(appointment.get('date') or ''), str(appointment.get('time') or ''))}",
        f"Address: {appointment.get('service_address') or 'Not provided'}",
    ]
    if appointment.get("location_url"):
        lines.append(f"Map: {appointment['location_url']}")
    intake_summary = _appointment_intake_summary(appointment)
    if intake_summary:
        lines.append(f"Service details: {intake_summary}")
    if appointment.get("notes"):
        lines.append(f"Notes: {str(appointment['notes']).strip()}")
    lines.extend(("", "Reply START when work begins or COMPLETE followed by service notes when finished."))
    return "\n".join(lines)


async def handle_staff_appointment_chat_reply(staff_phone: str, text: str, raw: Optional[dict] = None) -> bool:
    """Allow assigned staff to request details and update service status in WhatsApp."""
    command = _staff_appointment_command(text)
    if not command:
        return False
    try:
        from db import (
            get_all_appointments, get_appointment_staff, normalize_phone,
            update_appointment_service_fields, update_appointment_status,
        )
        staff = next((person for person in await get_appointment_staff(include_inactive=False)
                      if normalize_phone(person.get("whatsapp_number") or "") == normalize_phone(staff_phone)), None)
        if not staff:
            return False
        appointments = [row for row in await get_all_appointments() if row.get("staff_id") == staff.get("id")]
        context_id = str(((raw or {}).get("context") or {}).get("id") or "").strip()
        selected = next((row for row in appointments if context_id and row.get("staff_notification_message_id") == context_id), None)
        reference_match = re.search(r"\b(?:az[-\s]?)?([0-9a-f]{8})\b", str(text or ""), re.I)
        if not selected and reference_match:
            ref = reference_match.group(1).lower()
            selected = next((row for row in appointments if str(row.get("id") or "").lower().startswith(ref)), None)
        if not selected:
            active = [row for row in appointments if str(row.get("status") or "").lower() in {"pending", "confirmed", "booked", "in_progress"}]
            active.sort(key=lambda row: f"{row.get('date') or ''} {row.get('time') or ''}")
            if len(active) == 1:
                selected = active[0]
            elif len(active) > 1:
                choices = "\n".join(f"{str(row.get('id') or '')[:8].upper()} - {row.get('date')} {str(row.get('time') or '')[:5]} - {row.get('name') or 'Customer'}" for row in active[:6])
                await send_whatsapp_text(staff_phone, f"Please include the booking reference, for example DETAILS {str(active[0].get('id') or '')[:8].upper()}.\n\n{choices}")
                return True
        if not selected:
            await send_whatsapp_text(staff_phone, "No active appointment assigned to you was found.")
            return True

        appointment_id = str(selected.get("id") or "")
        if command == "details":
            await send_whatsapp_text(staff_phone, _format_staff_service_summary(selected))
            return True
        if command == "start":
            current = str(selected.get("status") or "").lower()
            if current in {"completed", "cancelled", "no_show"}:
                await send_whatsapp_text(staff_phone, f"Booking {appointment_id[:8].upper()} is already {current.replace('_', ' ')} and cannot be started.")
                return True
            await update_appointment_status(appointment_id, "in_progress")
            await update_appointment_service_fields(appointment_id, {
                "staff_status_updated_at": datetime.now(timezone.utc).isoformat(),
                "staff_status_updated_by": staff.get("name") or "staff_whatsapp",
            })
            await send_whatsapp_text(staff_phone, f"Booking {appointment_id[:8].upper()} is now in progress.")
            return True

        if str(selected.get("status") or "").lower() == "completed":
            await send_whatsapp_text(staff_phone, f"Booking {appointment_id[:8].upper()} is already completed.")
            return True
        service_notes = re.sub(r"^(service\s+)?(is\s+)?(complete|completed|done)\b[\s:,-]*", "", str(text or "").strip(), flags=re.I).strip()
        existing_notes = str(selected.get("notes") or "").strip()
        notes = "\n".join(filter(None, (existing_notes, f"Staff completion notes: {service_notes}" if service_notes else "")))
        if not await update_appointment_status(appointment_id, "completed"):
            await send_whatsapp_text(staff_phone, "I could not update that appointment. Please try again or contact the administrator.")
            return True
        await update_appointment_service_fields(appointment_id, {
            "notes": notes,
            "staff_status_updated_at": datetime.now(timezone.utc).isoformat(),
            "staff_status_updated_by": staff.get("name") or "staff_whatsapp",
            "reminder_processed": True,
        })
        await send_whatsapp_text(staff_phone, f"Booking {appointment_id[:8].upper()} has been completed and moved to appointment history. Service notes were saved.")
        return True
    except Exception as exc:
        logger.error("Staff appointment chat update failed: %s", exc)
        return False


def _staff_outcome_request_active(appointment: dict, staff_id: str, sender_phone: str, now: Optional[datetime] = None, max_age_hours: int = 24) -> tuple[bool, str]:
    """Validate a staff outcome request without trusting historical flags alone."""
    now = now or datetime.now(timezone.utc)
    if str(appointment.get("staff_id") or "") != str(staff_id or ""):
        return False, "wrong_staff"
    if not bool(appointment.get("demo_status_requested")):
        return False, "not_requested"
    if str(appointment.get("demo_result") or "").strip():
        return False, "completed_request"
    if str(appointment.get("status") or "").lower() not in {"booked", "confirmed", "in_progress"}:
        return False, "inactive_appointment"
    try:
        from db import normalize_phone
        customer_phone = normalize_phone(appointment.get("phone") or appointment.get("phone_number") or "")
        if customer_phone and customer_phone == normalize_phone(sender_phone):
            return False, "sender_is_customer"
    except Exception:
        pass
    requested_at = str(appointment.get("demo_status_requested_at") or "").strip()
    if not requested_at:
        return False, "missing_request_timestamp"
    try:
        requested_dt = datetime.fromisoformat(requested_at.replace("Z", "+00:00"))
        if requested_dt.tzinfo is None:
            requested_dt = requested_dt.replace(tzinfo=timezone.utc)
        if requested_dt > now + timedelta(minutes=5) or now > requested_dt + timedelta(hours=max_age_hours):
            return False, "expired_request"
    except Exception:
        return False, "invalid_request_timestamp"
    return True, "active"


async def handle_staff_demo_status_reply(staff_phone: str, text: str, raw: Optional[dict] = None) -> bool:
    try:
        from db import (
            get_appointment_staff, get_all_appointments,
            update_appointment_demo_fields, update_lead_journey, log_error, normalize_phone
        )

        staff_list = await get_appointment_staff(include_inactive=False)
        staff_member = next((s for s in staff_list if normalize_phone(s.get("whatsapp_number") or "") == normalize_phone(staff_phone)), None)
        if not staff_member:
            await _diagnostic_event("staff_outcome_route_rejected", phone=staff_phone, reason="sender_not_staff")
            return False

        staff_id = staff_member.get("id")

        appointments = await get_all_appointments()
        pending = []
        rejected_reasons = []
        for appointment in appointments:
            if str(appointment.get("staff_id") or "") != str(staff_id or ""):
                continue
            active, reason = _staff_outcome_request_active(appointment, staff_id, staff_phone)
            if active:
                pending.append(appointment)
            elif bool(appointment.get("demo_status_requested")):
                rejected_reasons.append(reason)
        if not pending:
            reason = rejected_reasons[0] if rejected_reasons else "no_active_request"
            await _diagnostic_event("staff_outcome_route_rejected", phone=staff_phone, reason=reason)
            if reason in {"expired_request", "missing_request_timestamp", "invalid_request_timestamp", "completed_request", "inactive_appointment"}:
                await _diagnostic_event("stale_pending_action_ignored", phone=staff_phone, reason=reason, action_type="staff_outcome")
            return False

        context_id = str(((raw or {}).get("context") or {}).get("id") or "").strip()
        if context_id:
            matched = [a for a in pending if str(a.get("demo_status_reply_message_id") or "") == context_id]
            if not matched:
                await _diagnostic_event("staff_outcome_route_rejected", phone=staff_phone, reason="wrong_request_context")
                return False
            pending = matched
        if len(pending) != 1:
            await _diagnostic_event("staff_outcome_route_rejected", phone=staff_phone, reason="ambiguous_active_requests", active_count=len(pending))
            return False
        appt = pending[0]
        appt_id = appt.get("id")
        cust_phone = appt.get("phone") or appt.get("phone_number") or ""
        cust_name = appt.get("name") or "Customer"

        await _diagnostic_event("staff_outcome_route_selected", phone=staff_phone, appointment_ref=_safe_ref(appt_id))

        result_key = parse_staff_reply(text)
        if not result_key:
            await _diagnostic_event("staff_outcome_route_rejected", phone=staff_phone, reason="invalid_status_input", appointment_ref=_safe_ref(appt_id))
            ack = "Please reply with 1, 2, 3, 4, 5, or 6."
            res = await send_whatsapp_text(staff_phone, ack)
            if res.get("success"):
                await log_error("appointment_reminder", "demo_status_ack_sent", f"Sent unclear reply ack to staff {staff_phone}", "info")
            else:
                await log_error("appointment_reminder", "demo_status_ack_failed", f"Failed sending unclear reply ack to staff {staff_phone}: {res.get('error')}", "warning")
            return True

        now_iso = datetime.now(timezone.utc).isoformat()

        appt_updates = {
            "demo_result": result_key,
            "demo_status_updated_by": staff_member.get("name") or "staff_whatsapp",
            "demo_status_updated_at": now_iso
        }

        crm_updates = {}

        if result_key == "completed":
            appt_updates["status"] = "completed"
            appt_updates["completed_at"] = now_iso
            crm_updates = {
                "crm_status": "demo_done",
                "journey_stage": "demo_done",
                "next_best_action": "post_demo_followup"
            }
        elif result_key == "no_show":
            appt_updates["status"] = "no_show"
            appt_updates["no_show_at"] = now_iso
            crm_updates = {
                "crm_status": "demo_no_show",
                "journey_stage": "demo_no_show",
                "next_best_action": "reschedule_demo"
            }
        elif result_key == "rescheduled":
            appt_updates["status"] = "reschedule_requested"
            appt_updates["rescheduled_at"] = now_iso
            crm_updates = {
                "crm_status": "callback_requested",
                "journey_stage": "callback_requested",
                "next_best_action": "reschedule_demo"
            }
        elif result_key == "interested":
            crm_updates = {
                "crm_status": "interested",
                "journey_stage": "interested",
                "next_best_action": "sales_followup"
            }
        elif result_key == "not_interested":
            crm_updates = {
                "crm_status": "not_interested",
                "journey_stage": "not_interested",
                "stop_automation": True,
                "stop_automation_reason": "staff_marked_not_interested"
            }
        elif result_key == "need_followup":
            crm_updates = {
                "crm_status": "callback_requested",
                "journey_stage": "callback_requested",
                "next_best_action": "follow_up_customer"
            }

        await update_appointment_demo_fields(appt_id, appt_updates)
        await log_error("appointment_reminder", "demo_status_updated", f"Updated appointment {appt_id} demo status to {result_key}", "info")

        if cust_phone and crm_updates:
            await update_lead_journey(cust_phone, crm_updates)

        ack = f"Updated. Demo marked as {result_key.replace('_', ' ')} for {cust_name}."
        res = await send_whatsapp_text(staff_phone, ack)
        if res.get("success"):
            await log_error("appointment_reminder", "demo_status_ack_sent", f"Sent success ack to staff {staff_phone}", "info")
        else:
            await log_error("appointment_reminder", "demo_status_ack_failed", f"Failed sending success ack to staff {staff_phone}: {res.get('error')}", "warning")

        if cust_phone and not crm_updates.get("stop_automation"):
            try:
                from db import create_followup_action
                followup_scheduled = False

                window_open = await is_whatsapp_service_window_open(cust_phone)

                if result_key == "no_show":
                    action_type = "whatsapp_template"
                    payload = {"template_purpose": "no_response_followup_template", "appointment_id": appt_id}
                    if window_open:
                        action_type = "whatsapp_text"
                        payload = {"text": f"Hi {cust_name}, we missed you for our demo appointment. Would you like to reschedule?", "appointment_id": appt_id}

                    await create_followup_action(
                        cust_phone, "demo_no_show", action_type, "whatsapp",
                        datetime.now(timezone.utc),
                        reason="reschedule_after_demo_no_show",
                        payload=payload
                    )
                    followup_scheduled = True

                elif result_key == "completed":
                    action_type = "whatsapp_template"
                    payload = {"template_purpose": "welcome_template", "appointment_id": appt_id}
                    if window_open:
                        action_type = "whatsapp_text"
                        payload = {"text": f"Hi {cust_name}, thank you for attending the demo today! Do you have any questions?", "appointment_id": appt_id}

                    await create_followup_action(
                        cust_phone, "post_demo_followup", action_type, "whatsapp",
                        datetime.now(timezone.utc) + timedelta(minutes=10),
                        reason="post_demo_thanks",
                        payload=payload
                    )
                    followup_scheduled = True

                elif result_key == "interested":
                    action_type = "whatsapp_template"
                    payload = {"template_purpose": "welcome_template", "appointment_id": appt_id}
                    if window_open:
                        action_type = "whatsapp_text"
                        payload = {"text": f"Hi {cust_name}, great to hear you are interested! Let's schedule a call to discuss the next steps.", "appointment_id": appt_id}

                    await create_followup_action(
                        cust_phone, "sales_followup", action_type, "whatsapp",
                        datetime.now(timezone.utc) + timedelta(hours=1),
                        reason="sales_followup",
                        payload=payload
                    )
                    followup_scheduled = True

                if followup_scheduled:
                    await log_error("appointment_reminder", "demo_status_customer_followup_scheduled", f"Scheduled customer follow-up for {cust_phone} after {result_key}", "info")

            except Exception as e:
                await log_error("appointment_reminder", "demo_status_customer_followup_failed", f"Failed scheduling follow-up for {cust_phone}: {e}", "warning")

        return True

    except Exception as exc:
        logger.error("handle_staff_demo_status_reply error: %s", exc)
        return False
