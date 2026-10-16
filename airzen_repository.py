"""Database adapter foundation for AirZen.

Phase 1 deliberately keeps Supabase as the default backend while introducing a
small repository seam for the production-critical customer journey.  The MySQL
implementation uses async, parameterized SQL only and keeps provider-specific
behavior inside this module instead of leaking it into WhatsApp/CRM logic.
"""

from __future__ import annotations

import asyncio

import inspect
import hmac
import json
import os
import uuid
from datetime import date as dt_date, datetime, time as dt_time, timedelta, timezone
from typing import Any, Callable, Optional, Protocol


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_mysql() -> str:
    return utc_now().strftime("%Y-%m-%d %H:%M:%S.%f")


def iso_now() -> str:
    return utc_now().isoformat()


def new_uuid() -> str:
    return str(uuid.uuid4())


def _notification_retry_at(attempt_count: int) -> datetime:
    delay_minutes = min(60, max(1, 2 ** max(int(attempt_count) - 1, 0)))
    return utc_now() + timedelta(minutes=delay_minutes)


async def _rollback_quietly(conn: Any) -> None:
    """Best-effort rollback that preserves the original database error."""
    try:
        await conn.rollback()
    except Exception:
        pass


def _json_value(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return default


def _json_text(value: Any, default: Any) -> str:
    if value in (None, ""):
        value = default
    if isinstance(value, str):
        try:
            json.loads(value)
            return value
        except Exception:
            return json.dumps(default, separators=(",", ":"), default=str)
    return json.dumps(value, separators=(",", ":"), default=str)


def _dt(value: Any) -> Optional[str]:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        current = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return current.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")
    text = str(value).strip()
    if not text:
        return None
    try:
        current = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        return current.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")
    except Exception:
        return text


def _mysql_result_value(value: Any) -> Any:
    """Normalize driver-specific MySQL temporal values at the repository boundary."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dt_date):
        return value.isoformat()
    if isinstance(value, timedelta):
        total_seconds = int(value.total_seconds())
        sign = "-" if total_seconds < 0 else ""
        total_seconds = abs(total_seconds)
        hours, remainder = divmod(total_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{sign}{hours:02d}:{minutes:02d}:{seconds:02d}"
    if isinstance(value, dt_time):
        return value.isoformat()
    return value


def _mysql_result_row(row: Optional[dict]) -> Optional[dict]:
    if row is None:
        return None
    return {key: _mysql_result_value(value) for key, value in row.items()}


def _mysql_payload(row: dict, json_fields: set[str] | None = None) -> dict:
    payload = dict(row or {})
    json_fields = json_fields or set()
    for key in list(payload.keys()):
        if key in json_fields:
            payload[key] = _json_text(payload.get(key), {})
        elif key.endswith("_at") or key in {"created_at", "updated_at", "scheduled_at", "completed_at", "timestamp"}:
            converted = _dt(payload.get(key))
            if converted is not None:
                payload[key] = converted
    return payload


CRM_CONTACT_WRITABLE_FIELDS = frozenset({
    "id",
    "phone_number",
    "lead_name",
    "email",
    "city",
    "location",
    "address",
    "requirement",
    "budget",
    "source",
    "business_name",
    "campaign_name",
    "service_type",
    "lead_country",
    "lead_country_code",
    "upload_batch_id",
    "import_source",
    "crm_status",
    "custom_status",
    "next_followup_at",
    "assigned_to",
    "crm_notes",
    "last_call_outcome",
    "last_call_at",
    "total_calls",
    "last_synced_at",
    "journey_stage",
    "next_best_action",
    "next_action_at",
    "next_action_channel",
    "last_customer_reply_at",
    "last_whatsapp_sent_at",
    "last_call_attempt_at",
    "call_attempt_count",
    "whatsapp_followup_count",
    "no_response_followup_count",
    "demo_reminder_count",
    "stop_automation",
    "stop_automation_reason",
    "last_followup_reason",
    "last_intent",
    "preferred_channel",
    "preferred_callback_at",
    "tags_json",
    "custom_fields_json",
    "handoff_required",
    "handoff_reason",
    "handoff_assigned_to",
    "handoff_at",
    "created_at",
    "updated_at",
})


def _crm_contact_payload(row: dict | None) -> dict:
    return {
        key: value
        for key, value in dict(row or {}).items()
        if key in CRM_CONTACT_WRITABLE_FIELDS
    }


def _mysql_appointment_payload(row: dict) -> dict:
    payload = _mysql_payload(row, {"intake_data"})
    # Runtime booking context belongs on the WhatsApp conversation and outbox
    # intents; it is not an appointments-table column.
    payload.pop("conversation_id", None)
    return payload


def _appointment_core(row: dict) -> dict:
    current = row or {}
    return {
        "name": str(current.get("name") or ""),
        "phone": str(current.get("phone") or ""),
        "date": str(current.get("date") or "")[:10],
        "time": str(current.get("time") or "")[:5],
        "service": str(current.get("service") or "Appointment"),
        "staff_id": str(current.get("staff_id") or ""),
        "staff_name": str(current.get("staff_name") or ""),
        "duration_minutes": int(current.get("duration_minutes") or 30),
        "buffer_minutes": int(current.get("buffer_minutes") or 15),
        "timezone": str(current.get("timezone") or "Asia/Singapore"),
    }


def _supabase_payload(row: dict) -> dict:
    payload = dict(row or {})
    for key in ("payload", "result", "raw_payload", "parameters", "appointment_state", "intake_data"):
        if key in payload and isinstance(payload[key], (dict, list)):
            payload[key] = json.dumps(payload[key], separators=(",", ":"), default=str)
    return payload


def _followup_row(row: dict) -> dict:
    payload = dict(row or {})
    for key in ("payload", "result"):
        payload[key] = _json_value(payload.get(key), {})
    return payload


def _decoded_row(row: Optional[dict], fields: set[str]) -> Optional[dict]:
    if row is None:
        return None
    payload = dict(row)
    for key in fields:
        if key in payload:
            payload[key] = _json_value(payload.get(key), [] if key in {"selected_tests"} else {})
    return payload


def _is_duplicate_error(exc: Exception) -> bool:
    text = str(exc).lower()
    args = getattr(exc, "args", ())
    return bool(args and args[0] == 1062) or "duplicate" in text or "unique" in text


class AirzenRepository(Protocol):
    # Settings
    async def get_setting(self, key: str) -> Optional[str]: ...
    async def set_setting(self, key: str, value: str) -> None: ...
    async def list_settings(self) -> dict[str, str]: ...
    async def set_settings(self, settings: dict[str, str]) -> None: ...

    # Diagnostics and reference data
    async def create_error_log(self, row: dict) -> None: ...
    async def create_error_logs(self, rows: list[dict]) -> None: ...
    async def list_error_logs(self, level: str = "", source: str = "", limit: int = 200) -> list[dict]: ...
    async def clear_error_logs(self) -> int: ...
    async def list_lead_statuses(self) -> list[dict]: ...
    async def upsert_lead_status(self, row: dict) -> dict: ...
    async def delete_lead_status(self, status_id: str) -> bool: ...
    async def list_crm_tags(self, limit: int = 500) -> list[dict]: ...
    async def upsert_crm_tag(self, row: dict) -> dict: ...
    async def delete_crm_tag(self, name: str) -> bool: ...

    # Calls and agent profiles
    async def create_call_log(self, row: dict) -> dict: ...
    async def list_call_logs(self, phone: str = "", outcome: str = "", date_from: str = "", date_to: str = "", limit: int = 1000, offset: int = 0) -> list[dict]: ...
    async def list_recordings_for_cleanup(self, cutoff: datetime | str) -> list[dict]: ...
    async def update_call_log(self, call_id: str, updates: dict) -> bool: ...
    async def get_recording_storage_stats(self) -> dict: ...
    async def clear_call_logs(self) -> int: ...
    async def list_agent_profiles(self) -> list[dict]: ...
    async def get_agent_profile(self, profile_id: str) -> Optional[dict]: ...
    async def create_agent_profile(self, row: dict, set_default: bool = False) -> dict: ...
    async def update_agent_profile(self, profile_id: str, updates: dict) -> bool: ...
    async def delete_agent_profile(self, profile_id: str) -> bool: ...
    async def set_default_agent_profile(self, profile_id: str) -> bool: ...

    # Campaigns
    async def create_campaign(self, row: dict) -> dict: ...
    async def list_campaigns(self) -> list[dict]: ...
    async def get_campaign(self, campaign_id: str) -> Optional[dict]: ...
    async def update_campaign(self, campaign_id: str, updates: dict, claim_token: str = "") -> bool: ...
    async def delete_campaign(self, campaign_id: str) -> bool: ...
    async def clear_campaigns(self) -> int: ...
    async def claim_campaign(self, campaign_id: str, lease_seconds: int = 3600) -> Optional[dict]: ...

    # Automation queue
    async def create_automation_action(self, row: dict) -> dict: ...
    async def list_automation_actions(self, status: str = "", phone: str = "", limit: int = 100) -> list[dict]: ...
    async def claim_automation_actions(self, due_before: datetime | str, limit: int = 20, lease_seconds: int = 300) -> list[dict]: ...
    async def update_automation_action(self, action_id: str, updates: dict, claim_token: str = "") -> bool: ...
    async def automation_queue_count(self) -> int: ...

    # Broadcasts
    async def create_broadcast_campaign(self, row: dict) -> dict: ...
    async def list_broadcast_campaigns(self, limit: int = 200) -> list[dict]: ...
    async def get_broadcast_campaign(self, campaign_id: str) -> Optional[dict]: ...
    async def update_broadcast_campaign(self, campaign_id: str, updates: dict, claim_token: str = "") -> bool: ...
    async def claim_broadcast_campaign(self, campaign_id: str, lease_seconds: int = 1800) -> Optional[dict]: ...
    async def claim_broadcast_recipient(self, row: dict, claim_token: str) -> Optional[dict]: ...
    async def update_broadcast_recipient(self, recipient_id: str, updates: dict, claim_token: str = "") -> bool: ...
    async def list_broadcast_recipients(self, campaign_id: str, limit: int = 500) -> list[dict]: ...

    # WhatsApp diagnostic health tests
    async def create_health_recipient(self, row: dict) -> dict: ...
    async def get_health_recipient(self, recipient_id: str) -> Optional[dict]: ...
    async def find_health_recipient_by_fingerprint(self, fingerprint: str, verified_only: bool = False) -> Optional[dict]: ...
    async def list_health_recipients(self) -> list[dict]: ...
    async def update_health_recipient(self, recipient_id: str, updates: dict) -> bool: ...
    async def create_health_run(self, row: dict) -> dict: ...
    async def get_health_run(self, run_id: str) -> Optional[dict]: ...
    async def find_recent_health_run(self, recipient_id: str, started_after: datetime | str) -> Optional[dict]: ...
    async def find_active_health_run(self, recipient_id: str) -> Optional[dict]: ...
    async def list_health_runs(self, limit: int = 100) -> list[dict]: ...
    async def update_health_run(self, run_id: str, updates: dict) -> bool: ...
    async def upsert_health_result(self, row: dict) -> dict: ...
    async def get_health_result(self, run_id: str, test_key: str) -> Optional[dict]: ...
    async def find_health_result_by_provider_id(self, provider_message_id: str) -> Optional[dict]: ...
    async def list_health_results(self, run_id: str) -> list[dict]: ...
    async def update_health_result(self, result_id: str, updates: dict) -> bool: ...
    async def delete_health_run(self, run_id: str) -> bool: ...

    # Reports and explicit admin clears
    async def list_whatsapp_messages_for_report(self, limit: int = 5000) -> list[dict]: ...
    async def clear_contact_memory(self) -> int: ...

    # CRM
    async def get_crm_contact_by_phone(self, phone: str) -> Optional[dict]: ...
    async def list_crm_contacts(self, status: str = "", outcome: str = "") -> list[dict]: ...
    async def upsert_crm_contact(self, row: dict) -> dict: ...
    async def update_crm_contact(self, phone: str, updates: dict) -> bool: ...
    async def delete_crm_contact_by_phone(self, phone: str) -> bool: ...
    async def add_contact_memory(self, phone: str, insight: str) -> str: ...
    async def get_contact_memory(self, phone: str, limit: int = 20) -> list[dict]: ...
    async def replace_contact_memory(self, phone: str, compressed: str) -> str: ...

    # WhatsApp
    async def get_or_create_whatsapp_conversation(self, phone: str, contact_name: str = "") -> dict: ...
    async def save_whatsapp_message(self, row: dict) -> dict: ...
    async def get_inbound_whatsapp_message_by_provider_id(self, provider_message_id: str) -> dict: ...
    async def update_inbound_whatsapp_message_status(self, provider_message_id: str, status: str) -> bool: ...
    async def update_whatsapp_conversation(self, conv_id: str, updates: dict) -> dict: ...
    async def update_whatsapp_conversation_last_message(self, conv_id: str, text: str, increment_unread: bool = True) -> dict: ...
    async def has_recent_inbound_whatsapp_message(self, phone_or_conv_id: str, since_utc: datetime) -> bool: ...
    async def create_whatsapp_log(self, row: dict) -> dict: ...
    async def list_whatsapp_logs(
        self,
        phone: str = "",
        event_type: str = "",
        template_name: str = "",
        status: str = "",
        since_utc: datetime | str | None = None,
        provider_message_id: str = "",
        limit: int = 50,
    ) -> list[dict]: ...
    async def get_whatsapp_message_activity(self) -> dict: ...
    async def record_whatsapp_delivery_status(
        self,
        provider_message_id: str,
        status: str,
        status_time: datetime | str,
        failure_reason: str = "",
        error_code: str = "",
    ) -> dict: ...
    async def list_whatsapp_conversations(
        self,
        status: str = "",
        ai_enabled: Optional[bool] = None,
        search: str = "",
        phone: str = "",
        include_deleted: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]: ...
    async def get_whatsapp_conversation_by_id(self, conv_id: str, include_deleted: bool = False) -> Optional[dict]: ...
    async def list_whatsapp_messages(
        self,
        conv_id: str,
        include_deleted: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]: ...
    async def soft_delete_whatsapp_message(self, message_id: str, deleted_by: str = "dashboard") -> bool: ...
    async def soft_delete_whatsapp_conversation(self, conv_id: str, deleted_by: str = "dashboard") -> bool: ...
    async def persist_appointment_state(self, conv_id: str, state: dict | str) -> dict: ...
    async def load_appointment_state(self, conv_id: str) -> dict: ...

    # Booking
    async def create_appointment(self, row: dict) -> dict: ...
    async def update_appointment(self, appointment_id: str, updates: dict) -> bool: ...
    async def clear_appointments(self) -> int: ...
    async def list_appointments(
        self,
        date: str = "",
        statuses: Optional[list[str]] = None,
        staff_id: str = "",
        date_from: str = "",
        date_to: str = "",
        ascending: bool = False,
    ) -> list[dict]: ...
    async def get_appointment(self, appointment_id: str, allow_prefix: bool = True) -> Optional[dict]: ...
    async def list_appointments_by_phone(
        self,
        phone: str,
        date: str = "",
        time: str = "",
        limit: int = 0,
    ) -> list[dict]: ...
    async def reschedule_appointment_record(self, old_appointment_id: str, new_appointment: dict) -> dict: ...
    async def get_appointments_for_slot(self, date: str, time: str, staff_id: str = "") -> list[dict]: ...
    async def get_booked_appointments_for_staff(self, staff_id: str) -> list[dict]: ...
    async def get_active_staff(self) -> list[dict]: ...
    async def list_appointment_staff(self, include_inactive: bool = False) -> list[dict]: ...
    async def get_appointment_staff(self, staff_id: str) -> Optional[dict]: ...
    async def upsert_appointment_staff(self, row: dict, staff_id: Optional[str] = None) -> dict: ...
    async def set_appointment_staff_active(self, staff_id: str, active: bool) -> bool: ...
    async def delete_appointment_staff(self, staff_id: str) -> bool: ...
    async def update_appointment_staff_calendar(self, staff_id: str, updates: dict) -> bool: ...

    # Staff mobile field service (MySQL-only feature)
    async def get_staff_mobile_credential(self, identifier: str) -> Optional[dict]: ...
    async def upsert_staff_mobile_credential(
        self, staff_id: str, username: str, pin_hash: str, active: bool = True,
    ) -> dict: ...
    async def disable_staff_mobile_credential(self, staff_id: str) -> bool: ...
    async def update_staff_mobile_login_state(
        self, staff_id: str, failed_attempts: int, locked_until: datetime | str | None = None,
    ) -> bool: ...
    async def create_staff_mobile_session(self, row: dict) -> dict: ...
    async def get_staff_mobile_session(self, session_id: str) -> Optional[dict]: ...
    async def rotate_staff_mobile_session(
        self, session_id: str, refresh_token_hash: str, expires_at: datetime | str,
    ) -> bool: ...
    async def revoke_staff_mobile_session(self, session_id: str) -> bool: ...
    async def list_staff_mobile_appointments(
        self, staff_id: str, date_from: str = "", date_to: str = "", statuses: Optional[list[str]] = None,
    ) -> list[dict]: ...
    async def get_staff_mobile_appointment(self, staff_id: str, appointment_id: str) -> Optional[dict]: ...
    async def begin_staff_mobile_job(self, appointment_id: str, staff_id: str, event: dict) -> dict: ...
    async def record_staff_mobile_location(self, row: dict) -> dict: ...
    async def mark_staff_mobile_arrived(
        self, appointment_id: str, staff_id: str, evidence: dict, event: dict,
    ) -> dict: ...
    async def verify_staff_mobile_start_code(
        self, appointment_id: str, staff_id: str, session_id: str, otp_hash: str, event: dict,
    ) -> dict: ...
    async def complete_staff_mobile_job(
        self, appointment_id: str, staff_id: str, evidence: dict,
        notification: dict, event: dict,
    ) -> dict: ...
    async def get_staff_mobile_job_detail(self, appointment_id: str) -> dict: ...
    async def get_staff_attendance(self, staff_id: str, work_date: str) -> Optional[dict]: ...
    async def check_in_staff_attendance(self, row: dict) -> dict: ...
    async def check_out_staff_attendance(self, staff_id: str, work_date: str, row: dict) -> dict: ...
    async def list_staff_attendance(
        self, date_from: str, date_to: str, staff_id: str = "",
    ) -> list[dict]: ...
    async def create_staff_leave_request(self, row: dict) -> dict: ...
    async def list_staff_leave_requests(self, staff_id: str = "", status: str = "") -> list[dict]: ...
    async def decide_staff_leave_request(
        self, request_id: str, status: str, admin_note: str, reviewed_by: str,
    ) -> dict: ...
    async def staff_mobile_schema_health(self) -> dict: ...
    async def create_appointment_with_outbox(
        self,
        appointment: dict,
        customer_intent: dict,
        staff_intent: dict,
        event_identity: str = "v1",
        start_code: Optional[dict] = None,
    ) -> dict: ...

    # Queues
    async def create_followup_action(self, row: dict) -> dict: ...
    async def list_followup_actions(
        self,
        phone: str = "",
        status: str = "",
        due_before: datetime | str | None = None,
        limit: int = 100,
        ascending: bool = False,
    ) -> list[dict]: ...
    async def claim_followup_actions(self, due_before: datetime | str, limit: int = 50) -> list[dict]: ...
    async def update_followup_action_status(
        self,
        action_id: str,
        status: str,
        result: Optional[dict] = None,
        error_message: str = "",
        expected_status: str = "processing",
    ) -> bool: ...
    async def reschedule_followup_action(
        self,
        action_id: str,
        scheduled_at: datetime | str,
        reason: str = "",
        expected_status: str = "processing",
    ) -> bool: ...
    async def enqueue_notification(self, row: dict) -> dict: ...
    async def claim_notification_outbox(self, limit: int = 10, lease_seconds: int = 300) -> list[dict]: ...
    async def complete_notification_outbox(self, row_id: str, claim_token: str, provider_message_id: str) -> bool: ...
    async def fail_notification_outbox(
        self,
        row_id: str,
        claim_token: str,
        status: str,
        safe_error: str,
        next_attempt_at: str = "",
    ) -> bool: ...
    async def recover_stuck_notification_outbox(self, max_processing_age_minutes: int = 15, limit: int = 50) -> int: ...
    async def reschedule_appointment_with_outbox(
        self,
        old_appointment_id: str,
        new_appointment: dict,
        customer_intent: dict,
        staff_intent: dict,
        event_identity: str = "v1",
        start_code: Optional[dict] = None,
    ) -> dict: ...
    async def cancel_appointment_with_outbox(
        self,
        appointment_id: str,
        customer_intent: dict,
        staff_intent: dict,
        event_identity: str = "v1",
    ) -> dict: ...
    async def probe_notification_outbox_storage(self, row: dict) -> bool: ...
    async def backend_health_check(self) -> dict: ...


class SupabaseRepository:
    def __init__(self, adb_factory: Callable[[], Any]):
        self._adb_factory = adb_factory

    async def _db(self):
        return await self._adb_factory()

    async def get_setting(self, key: str) -> Optional[str]:
        db = await self._db()
        result = await db.table("settings").select("value").eq("key", key).maybe_single().execute()
        return (result.data or {}).get("value") if result and result.data else None

    async def set_setting(self, key: str, value: str) -> None:
        db = await self._db()
        await db.table("settings").upsert({"key": key, "value": value, "updated_at": iso_now()}, on_conflict="key").execute()

    async def list_settings(self) -> dict[str, str]:
        db = await self._db()
        result = await db.table("settings").select("key, value").execute()
        return {
            str(row["key"]): row.get("value")
            for row in (result.data or [])
            if row.get("key") is not None
        }

    async def set_settings(self, settings: dict[str, str]) -> None:
        if not settings:
            return
        updated_at = iso_now()
        rows = [
            {"key": key, "value": value, "updated_at": updated_at}
            for key, value in settings.items()
        ]
        db = await self._db()
        await db.table("settings").upsert(rows, on_conflict="key").execute()

    async def create_error_log(self, row: dict) -> None:
        db = await self._db()
        await db.table("error_logs").insert(row).execute()

    async def create_error_logs(self, rows: list[dict]) -> None:
        if rows:
            db = await self._db()
            await db.table("error_logs").insert(rows).execute()

    async def list_error_logs(self, level: str = "", source: str = "", limit: int = 200) -> list[dict]:
        db = await self._db()
        query = db.table("error_logs").select("*").order("timestamp", desc=True).limit(limit)
        if level:
            query = query.eq("level", level)
        if source:
            query = query.eq("source", source)
        result = await query.execute()
        return result.data or []

    async def clear_error_logs(self) -> int:
        db = await self._db()
        result = await db.table("error_logs").delete().neq("id", "").execute()
        return len(result.data or [])

    async def list_lead_statuses(self) -> list[dict]:
        db = await self._db()
        result = await db.table("lead_statuses").select("*").order("created_at").execute()
        return result.data or []

    async def upsert_lead_status(self, row: dict) -> dict:
        payload = {**row}
        payload.setdefault("id", new_uuid())
        payload.setdefault("created_at", iso_now())
        db = await self._db()
        result = await db.table("lead_statuses").upsert(payload, on_conflict="name").execute()
        return (result.data or [payload])[0]

    async def delete_lead_status(self, status_id: str) -> bool:
        db = await self._db()
        result = await db.table("lead_statuses").delete().eq("id", status_id).execute()
        return bool(result.data)

    async def list_crm_tags(self, limit: int = 500) -> list[dict]:
        db = await self._db()
        result = await db.table("crm_tags").select("*").order("created_at", desc=True).limit(limit).execute()
        return result.data or []

    async def upsert_crm_tag(self, row: dict) -> dict:
        payload = {**row}
        payload.setdefault("created_at", iso_now())
        db = await self._db()
        result = await db.table("crm_tags").upsert(payload, on_conflict="name").execute()
        return (result.data or [payload])[0]

    async def delete_crm_tag(self, name: str) -> bool:
        db = await self._db()
        result = await db.table("crm_tags").delete().eq("name", name).execute()
        return bool(result.data)

    async def create_call_log(self, row: dict) -> dict:
        db = await self._db()
        result = await db.table("call_logs").insert(row).execute()
        return (result.data or [row])[0]

    async def list_call_logs(self, phone: str = "", outcome: str = "", date_from: str = "", date_to: str = "", limit: int = 1000, offset: int = 0) -> list[dict]:
        db = await self._db()
        query = db.table("call_logs").select("*").order("timestamp", desc=True).range(offset, offset + max(1, limit) - 1)
        if phone:
            query = query.eq("phone_number", phone)
        if outcome:
            query = query.eq("outcome", outcome)
        if date_from:
            query = query.gte("timestamp", date_from)
        if date_to:
            query = query.lte("timestamp", date_to)
        result = await query.execute()
        return result.data or []

    async def list_recordings_for_cleanup(self, cutoff: datetime | str) -> list[dict]:
        db = await self._db()
        result = await db.table("call_logs").select("id,recording_url,recording_object_key,recording_deleted,timestamp").eq("recording_deleted", False).lt("timestamp", str(cutoff)).execute()
        return [row for row in (result.data or []) if row.get("recording_url") or row.get("recording_object_key")]

    async def update_call_log(self, call_id: str, updates: dict) -> bool:
        db = await self._db()
        result = await db.table("call_logs").update(updates).eq("id", call_id).execute()
        return bool(result.data)

    async def get_recording_storage_stats(self) -> dict:
        db = await self._db()
        result = await db.table("call_logs").select("recording_url,recording_deleted,recording_size_bytes").execute()
        rows = result.data or []
        active = [row for row in rows if row.get("recording_url") and not row.get("recording_deleted")]
        return {"active_recordings": len(active), "total_size_bytes": sum(int(row.get("recording_size_bytes") or 0) for row in active)}

    async def clear_call_logs(self) -> int:
        db = await self._db()
        result = await db.table("call_logs").delete().neq("id", "").execute()
        return len(result.data or [])

    async def list_agent_profiles(self) -> list[dict]:
        db = await self._db()
        result = await db.table("agent_profiles").select("*").order("created_at").execute()
        return result.data or []

    async def get_agent_profile(self, profile_id: str) -> Optional[dict]:
        db = await self._db()
        result = await db.table("agent_profiles").select("*").eq("id", profile_id).maybe_single().execute()
        return result.data if result and result.data else None

    async def create_agent_profile(self, row: dict, set_default: bool = False) -> dict:
        payload = {**row, "is_default": 0}
        db = await self._db()
        result = await db.table("agent_profiles").insert(payload).execute()
        if set_default:
            await self.set_default_agent_profile(payload["id"])
            payload["is_default"] = 1
        return (result.data or [payload])[0] | {"is_default": payload["is_default"]}

    async def update_agent_profile(self, profile_id: str, updates: dict) -> bool:
        payload = dict(updates or {})
        make_default = payload.pop("is_default", None) in (1, True)
        db = await self._db()
        result = await db.table("agent_profiles").update(payload).eq("id", profile_id).execute() if payload else None
        if make_default:
            return await self.set_default_agent_profile(profile_id)
        return bool(result and result.data)

    async def delete_agent_profile(self, profile_id: str) -> bool:
        db = await self._db()
        result = await db.table("agent_profiles").delete().eq("id", profile_id).execute()
        return bool(result.data)

    async def set_default_agent_profile(self, profile_id: str) -> bool:
        db = await self._db()
        result = await db.rpc("set_default_agent_profile_atomic", {"target_profile_id": profile_id}).execute()
        return bool(result.data)

    async def create_campaign(self, row: dict) -> dict:
        db = await self._db()
        result = await db.table("campaigns").insert(row).execute()
        return (result.data or [row])[0]

    async def list_campaigns(self) -> list[dict]:
        db = await self._db()
        result = await db.table("campaigns").select("*").order("created_at", desc=True).execute()
        return result.data or []

    async def get_campaign(self, campaign_id: str) -> Optional[dict]:
        db = await self._db()
        result = await db.table("campaigns").select("*").eq("id", campaign_id).maybe_single().execute()
        return result.data if result and result.data else None

    async def update_campaign(self, campaign_id: str, updates: dict, claim_token: str = "") -> bool:
        db = await self._db()
        payload = dict(updates or {})
        if claim_token:
            payload.setdefault("lease_expires_at", (utc_now() + timedelta(hours=2)).isoformat())
        query = db.table("campaigns").update(payload).eq("id", campaign_id)
        if claim_token:
            query = query.eq("claim_token", claim_token)
        result = await query.execute()
        return bool(result.data)

    async def delete_campaign(self, campaign_id: str) -> bool:
        db = await self._db()
        result = await db.table("campaigns").delete().eq("id", campaign_id).execute()
        return bool(result.data)

    async def clear_campaigns(self) -> int:
        db = await self._db()
        result = await db.table("campaigns").delete().neq("id", "").execute()
        return len(result.data or [])

    async def claim_campaign(self, campaign_id: str, lease_seconds: int = 3600) -> Optional[dict]:
        db = await self._db()
        result = await db.rpc("claim_campaign_run", {"target_campaign_id": campaign_id, "lease_seconds": lease_seconds}).execute()
        rows = result.data or []
        return rows[0] if isinstance(rows, list) and rows else None

    async def create_automation_action(self, row: dict) -> dict:
        db = await self._db()
        result = await db.table("automation_actions").insert(row).execute()
        return (result.data or [row])[0]

    async def list_automation_actions(self, status: str = "", phone: str = "", limit: int = 100) -> list[dict]:
        db = await self._db()
        query = db.table("automation_actions").select("*").order("scheduled_at", desc=True).limit(limit)
        if status:
            query = query.eq("status", status)
        if phone:
            query = query.eq("phone_number", phone)
        result = await query.execute()
        return result.data or []

    async def claim_automation_actions(self, due_before: datetime | str, limit: int = 20, lease_seconds: int = 300) -> list[dict]:
        db = await self._db()
        result = await db.rpc("claim_automation_actions", {"due_before": str(due_before), "batch_limit": limit, "lease_seconds": lease_seconds}).execute()
        return result.data or []

    async def update_automation_action(self, action_id: str, updates: dict, claim_token: str = "") -> bool:
        db = await self._db()
        query = db.table("automation_actions").update(updates).eq("id", action_id)
        if claim_token:
            query = query.eq("claim_token", claim_token).eq("status", "running")
        result = await query.execute()
        return bool(result.data)

    async def automation_queue_count(self) -> int:
        db = await self._db()
        result = await db.table("automation_actions").select("id", count="exact").limit(1).execute()
        return int(getattr(result, "count", 0) or len(result.data or []))

    async def create_broadcast_campaign(self, row: dict) -> dict:
        db = await self._db()
        result = await db.table("broadcast_campaigns").insert(row).execute()
        return (result.data or [row])[0]

    async def list_broadcast_campaigns(self, limit: int = 200) -> list[dict]:
        db = await self._db()
        result = await db.table("broadcast_campaigns").select("*").order("created_at", desc=True).limit(limit).execute()
        return result.data or []

    async def get_broadcast_campaign(self, campaign_id: str) -> Optional[dict]:
        db = await self._db()
        result = await db.table("broadcast_campaigns").select("*").eq("id", campaign_id).maybe_single().execute()
        return result.data if result and result.data else None

    async def update_broadcast_campaign(self, campaign_id: str, updates: dict, claim_token: str = "") -> bool:
        db = await self._db()
        query = db.table("broadcast_campaigns").update(updates).eq("id", campaign_id)
        if claim_token:
            query = query.eq("claim_token", claim_token)
        result = await query.execute()
        return bool(result.data)

    async def claim_broadcast_campaign(self, campaign_id: str, lease_seconds: int = 1800) -> Optional[dict]:
        db = await self._db()
        result = await db.rpc("claim_broadcast_campaign", {"target_campaign_id": campaign_id, "lease_seconds": lease_seconds}).execute()
        rows = result.data or []
        return rows[0] if isinstance(rows, list) and rows else None

    async def claim_broadcast_recipient(self, row: dict, claim_token: str) -> Optional[dict]:
        payload = {**row, "status": "sending", "claim_token": claim_token, "claimed_at": iso_now()}
        db = await self._db()
        try:
            result = await db.table("broadcast_recipients").insert(payload).execute()
        except Exception as exc:
            if "duplicate" in str(exc).lower() or "unique" in str(exc).lower():
                return None
            raise
        return (result.data or [payload])[0]

    async def update_broadcast_recipient(self, recipient_id: str, updates: dict, claim_token: str = "") -> bool:
        db = await self._db()
        query = db.table("broadcast_recipients").update(updates).eq("id", recipient_id)
        if claim_token:
            query = query.eq("claim_token", claim_token).eq("status", "sending")
        result = await query.execute()
        return bool(result.data)

    async def list_broadcast_recipients(self, campaign_id: str, limit: int = 500) -> list[dict]:
        db = await self._db()
        result = await db.table("broadcast_recipients").select("*").eq("campaign_id", campaign_id).order("created_at", desc=True).limit(limit).execute()
        return result.data or []

    async def create_health_recipient(self, row: dict) -> dict:
        db = await self._db()
        result = await db.table("whatsapp_test_recipients").insert(row).execute()
        return (result.data or [row])[0]

    async def get_health_recipient(self, recipient_id: str) -> Optional[dict]:
        db = await self._db()
        result = await db.table("whatsapp_test_recipients").select("*").eq("id", recipient_id).maybe_single().execute()
        return result.data if result and result.data else None

    async def find_health_recipient_by_fingerprint(self, fingerprint: str, verified_only: bool = False) -> Optional[dict]:
        db = await self._db()
        query = db.table("whatsapp_test_recipients").select("*").eq("phone_number_fingerprint", fingerprint)
        if verified_only:
            query = query.eq("verified", True).eq("active", True)
        result = await query.maybe_single().execute()
        return result.data if result and result.data else None

    async def list_health_recipients(self) -> list[dict]:
        db = await self._db()
        result = await db.table("whatsapp_test_recipients").select("*").order("created_at", desc=True).execute()
        return result.data or []

    async def update_health_recipient(self, recipient_id: str, updates: dict) -> bool:
        db = await self._db()
        result = await db.table("whatsapp_test_recipients").update(updates).eq("id", recipient_id).execute()
        return bool(result.data)

    async def create_health_run(self, row: dict) -> dict:
        db = await self._db()
        result = await db.table("whatsapp_health_test_runs").insert(row).execute()
        return (result.data or [row])[0]

    async def get_health_run(self, run_id: str) -> Optional[dict]:
        db = await self._db()
        result = await db.table("whatsapp_health_test_runs").select("*").eq("id", run_id).maybe_single().execute()
        return result.data if result and result.data else None

    async def find_recent_health_run(self, recipient_id: str, started_after: datetime | str) -> Optional[dict]:
        db = await self._db()
        result = await db.table("whatsapp_health_test_runs").select("*").eq("recipient_id", recipient_id).gte("started_at", str(started_after)).order("started_at", desc=True).limit(1).execute()
        return (result.data or [None])[0]

    async def find_active_health_run(self, recipient_id: str) -> Optional[dict]:
        db = await self._db()
        result = await db.table("whatsapp_health_test_runs").select("*").eq("recipient_id", recipient_id).eq("status", "RUNNING").order("started_at", desc=True).limit(1).execute()
        return (result.data or [None])[0]

    async def list_health_runs(self, limit: int = 100) -> list[dict]:
        db = await self._db()
        result = await db.table("whatsapp_health_test_runs").select("*").order("started_at", desc=True).limit(limit).execute()
        return result.data or []

    async def update_health_run(self, run_id: str, updates: dict) -> bool:
        db = await self._db()
        result = await db.table("whatsapp_health_test_runs").update(updates).eq("id", run_id).execute()
        return bool(result.data)

    async def upsert_health_result(self, row: dict) -> dict:
        db = await self._db()
        result = await db.table("whatsapp_health_test_results").upsert(row, on_conflict="test_run_id,test_key").execute()
        return (result.data or [row])[0]

    async def get_health_result(self, run_id: str, test_key: str) -> Optional[dict]:
        db = await self._db()
        result = await db.table("whatsapp_health_test_results").select("*").eq("test_run_id", run_id).eq("test_key", test_key).maybe_single().execute()
        return result.data if result and result.data else None

    async def find_health_result_by_provider_id(self, provider_message_id: str) -> Optional[dict]:
        db = await self._db()
        result = await db.table("whatsapp_health_test_results").select("*").eq("provider_message_id", provider_message_id).limit(1).execute()
        return (result.data or [None])[0]

    async def list_health_results(self, run_id: str) -> list[dict]:
        db = await self._db()
        result = await db.table("whatsapp_health_test_results").select("*").eq("test_run_id", run_id).order("started_at").execute()
        return result.data or []

    async def update_health_result(self, result_id: str, updates: dict) -> bool:
        db = await self._db()
        result = await db.table("whatsapp_health_test_results").update(updates).eq("id", result_id).execute()
        return bool(result.data)

    async def delete_health_run(self, run_id: str) -> bool:
        db = await self._db()
        result = await db.table("whatsapp_health_test_runs").delete().eq("id", run_id).execute()
        return bool(result.data)

    async def list_whatsapp_messages_for_report(self, limit: int = 5000) -> list[dict]:
        db = await self._db()
        result = await db.table("whatsapp_messages").select("*").order("created_at", desc=True).limit(limit).execute()
        return result.data or []

    async def clear_contact_memory(self) -> int:
        db = await self._db()
        result = await db.table("contact_memory").delete().neq("id", "").execute()
        return len(result.data or [])

    async def get_crm_contact_by_phone(self, phone: str) -> Optional[dict]:
        db = await self._db()
        result = await db.table("crm_contacts").select("*").eq("phone_number", phone).maybe_single().execute()
        return result.data if result and result.data else None

    async def list_crm_contacts(self, status: str = "", outcome: str = "") -> list[dict]:
        db = await self._db()
        query = db.table("crm_contacts").select("*").order("updated_at", desc=True)
        if status:
            query = query.eq("crm_status", status)
        if outcome:
            query = query.eq("last_call_outcome", outcome)
        result = await query.execute()
        return result.data or []

    async def upsert_crm_contact(self, row: dict) -> dict:
        db = await self._db()
        payload = _crm_contact_payload(row)
        phone = str(payload.get("phone_number") or "").strip()
        if not phone:
            raise ValueError("CRM contact phone_number is required")
        existing = await self.get_crm_contact_by_phone(phone)
        if existing:
            merged = dict(existing)
            merged.update(payload)
            if existing.get("id"):
                merged["id"] = existing["id"]
            if existing.get("created_at"):
                merged["created_at"] = existing["created_at"]
            payload = merged
        payload.setdefault("updated_at", iso_now())
        result = await db.table("crm_contacts").upsert(payload, on_conflict="phone_number").execute()
        data = result.data or [payload]
        return data[0] if isinstance(data, list) and data else payload

    async def update_crm_contact(self, phone: str, updates: dict) -> bool:
        db = await self._db()
        payload = _crm_contact_payload(updates)
        if not payload:
            return False
        payload["updated_at"] = iso_now()
        result = await db.table("crm_contacts").update(payload).eq("phone_number", phone).execute()
        return bool(result.data)

    async def delete_crm_contact_by_phone(self, phone: str) -> bool:
        db = await self._db()
        result = await db.table("crm_contacts").delete().eq("phone_number", phone).execute()
        return bool(result.data)

    async def add_contact_memory(self, phone: str, insight: str) -> str:
        db = await self._db()
        row = {"id": new_uuid(), "phone_number": phone, "insight": insight[:1000], "created_at": iso_now()}
        await db.table("contact_memory").insert(row).execute()
        return row["id"]

    async def get_contact_memory(self, phone: str, limit: int = 20) -> list[dict]:
        db = await self._db()
        result = await db.table("contact_memory").select("insight, created_at").eq("phone_number", phone).order("created_at", desc=True).limit(limit).execute()
        return result.data or []

    async def replace_contact_memory(self, phone: str, compressed: str) -> str:
        db = await self._db()
        row = {"id": new_uuid(), "phone_number": phone, "insight": compressed[:2000], "created_at": iso_now()}
        await db.table("contact_memory").delete().eq("phone_number", phone).execute()
        await db.table("contact_memory").insert(row).execute()
        return row["id"]

    async def get_or_create_whatsapp_conversation(self, phone: str, contact_name: str = "") -> dict:
        db = await self._db()

        async def restore_existing(conv: dict) -> dict:
            updates = {}
            if conv.get("is_deleted"):
                updates.update({"is_deleted": False, "deleted_at": None, "deleted_by": None, "status": "open"})
            if conv.get("ai_enabled") is None:
                updates["ai_enabled"] = True
            if contact_name and (not conv.get("contact_name") or conv.get("contact_name") == conv.get("phone_number")):
                updates["contact_name"] = contact_name
            if updates:
                updates["updated_at"] = iso_now()
                try:
                    await db.table("whatsapp_conversations").update(updates).eq("id", conv["id"]).execute()
                except Exception:
                    legacy_updates = dict(updates)
                    if "deleted_at" in legacy_updates:
                        legacy_updates["deleted_at"] = ""
                    if "deleted_by" in legacy_updates:
                        legacy_updates["deleted_by"] = ""
                    await db.table("whatsapp_conversations").update(legacy_updates).eq("id", conv["id"]).execute()
                    updates = legacy_updates
                conv.update(updates)
            return conv

        result = await db.table("whatsapp_conversations").select("*").eq("phone_number", phone).order("created_at", desc=True).limit(1).execute()
        rows = result.data or []
        if rows:
            return await restore_existing(rows[0])
        now = iso_now()
        row = {
            "id": new_uuid(),
            "phone_number": phone,
            "contact_name": contact_name or phone,
            "crm_contact_id": "",
            "status": "open",
            "ai_enabled": True,
            "assigned_to": "",
            "last_message": "",
            "last_message_at": now,
            "unread_count": 0,
            "source": "whatsapp",
            "created_at": now,
            "updated_at": now,
            "appointment_state": "{}",
        }
        try:
            await db.table("whatsapp_conversations").insert(row).execute()
            return row
        except Exception:
            result = await db.table("whatsapp_conversations").select("*").eq("phone_number", phone).order("created_at", desc=True).limit(1).execute()
            rows = result.data or []
            if not rows:
                raise
            return await restore_existing(rows[0])

    async def save_whatsapp_message(self, row: dict) -> dict:
        db = await self._db()
        payload = _supabase_payload(dict(row))
        payload.setdefault("id", new_uuid())
        payload.setdefault("created_at", iso_now())
        await db.table("whatsapp_messages").insert(payload).execute()
        return payload

    async def get_inbound_whatsapp_message_by_provider_id(self, provider_message_id: str) -> dict:
        if not provider_message_id:
            return {}
        db = await self._db()
        result = await db.table("whatsapp_messages").select("*").eq("provider_message_id", provider_message_id).eq("direction", "inbound").limit(1).execute()
        rows = result.data or []
        return rows[0] if rows else {}

    async def update_inbound_whatsapp_message_status(self, provider_message_id: str, status: str) -> bool:
        if not provider_message_id:
            return False
        db = await self._db()
        result = await db.table("whatsapp_messages").update({"provider_status": status}).eq("provider_message_id", provider_message_id).eq("direction", "inbound").execute()
        return bool(result.data)

    async def update_whatsapp_conversation(self, conv_id: str, updates: dict) -> dict:
        db = await self._db()
        payload = _supabase_payload(dict(updates or {}))
        payload["updated_at"] = iso_now()
        await db.table("whatsapp_conversations").update(payload).eq("id", conv_id).execute()
        result = await db.table("whatsapp_conversations").select("*").eq("id", conv_id).execute()
        rows = result.data or []
        return rows[0] if rows else {}

    async def update_whatsapp_conversation_last_message(self, conv_id: str, text: str, increment_unread: bool = True) -> dict:
        db = await self._db()
        result = await db.table("whatsapp_conversations").select("unread_count").eq("id", conv_id).limit(1).execute()
        rows = result.data or []
        current = rows[0] if rows else {}
        try:
            unread_count = int(current.get("unread_count") or 0)
        except Exception:
            unread_count = 0
        payload = {
            "last_message": text[:200],
            "last_message_at": iso_now(),
            "status": "open",
            "updated_at": iso_now(),
        }
        if increment_unread:
            payload["unread_count"] = unread_count + 1
        await db.table("whatsapp_conversations").update(payload).eq("id", conv_id).execute()
        result = await db.table("whatsapp_conversations").select("*").eq("id", conv_id).execute()
        rows = result.data or []
        return rows[0] if rows else {}

    async def has_recent_inbound_whatsapp_message(self, phone_or_conv_id: str, since_utc: datetime) -> bool:
        db = await self._db()
        cutoff = since_utc.isoformat()
        result = await (
            db.table("whatsapp_messages")
            .select("created_at")
            .eq("phone_number", phone_or_conv_id)
            .eq("direction", "inbound")
            .gte("created_at", cutoff)
            .limit(1)
            .execute()
        )
        if result.data:
            return True
        result = await (
            db.table("whatsapp_messages")
            .select("created_at")
            .eq("conversation_id", phone_or_conv_id)
            .eq("direction", "inbound")
            .gte("created_at", cutoff)
            .limit(1)
            .execute()
        )
        return bool(result.data)

    async def create_whatsapp_log(self, row: dict) -> dict:
        db = await self._db()
        payload = _supabase_payload(dict(row or {}))
        payload.setdefault("id", new_uuid())
        payload.setdefault("created_at", iso_now())
        try:
            await db.table("whatsapp_logs").insert(payload).execute()
        except Exception:
            legacy_payload = dict(payload)
            legacy_payload.pop("idempotency_key", None)
            legacy_payload.pop("cooldown_until", None)
            await db.table("whatsapp_logs").insert(legacy_payload).execute()
            payload = legacy_payload
        return payload

    async def list_whatsapp_logs(
        self,
        phone: str = "",
        event_type: str = "",
        template_name: str = "",
        status: str = "",
        since_utc: datetime | str | None = None,
        provider_message_id: str = "",
        limit: int = 50,
    ) -> list[dict]:
        db = await self._db()
        query = db.table("whatsapp_logs").select("*")
        if phone:
            query = query.eq("phone_number", phone)
        if event_type:
            query = query.eq("event_type", event_type)
        if template_name:
            query = query.eq("template_name", template_name)
        if status:
            query = query.eq("status", status)
        if since_utc:
            cutoff = since_utc.isoformat() if isinstance(since_utc, datetime) else str(since_utc)
            query = query.gte("created_at", cutoff)
        if provider_message_id:
            query = query.eq("provider_message_id", provider_message_id)
        result = await query.order("created_at", desc=True).limit(max(1, int(limit))).execute()
        return result.data or []

    async def get_whatsapp_message_activity(self) -> dict:
        db = await self._db()

        async def latest(direction: str, ai_generated: Optional[bool] = None) -> Optional[str]:
            query = db.table("whatsapp_messages").select("*").eq("direction", direction)
            if ai_generated is not None:
                query = query.eq("ai_generated", ai_generated)
            try:
                result = await query.order("created_at", desc=True).limit(1).execute()
            except Exception:
                query = db.table("whatsapp_messages").select("*").eq("direction", direction)
                if ai_generated is not None:
                    query = query.eq("ai_generated", ai_generated)
                result = await query.order("timestamp", desc=True).limit(1).execute()
            rows = result.data or []
            if not rows:
                return None
            row = rows[0]
            return row.get("created_at") or row.get("updated_at") or row.get("timestamp")

        return {
            "last_inbound_whatsapp_message_at": await latest("inbound"),
            "last_outbound_ai_message_at": await latest("outbound", True),
        }

    async def record_whatsapp_delivery_status(
        self,
        provider_message_id: str,
        status: str,
        status_time: datetime | str,
        failure_reason: str = "",
        error_code: str = "",
    ) -> dict:
        if not provider_message_id:
            return {"matched": False, "message_matched": False, "log_matched": False}
        if status not in {"sent", "delivered", "read", "failed"}:
            raise ValueError("invalid WhatsApp delivery status")
        db = await self._db()
        message_result = await db.table("whatsapp_messages").select("id").eq("provider_message_id", provider_message_id).limit(1).execute()
        log_result = await db.table("whatsapp_logs").select("id").eq("provider_message_id", provider_message_id).limit(1).execute()
        message_matched = bool(message_result.data)
        log_matched = bool(log_result.data)
        timestamp = status_time.isoformat() if isinstance(status_time, datetime) else str(status_time)
        time_fields = {
            "delivered": {"delivered_at": timestamp},
            "read": {"read_at": timestamp},
            "failed": {
                "failed_at": timestamp,
                "failure_reason": failure_reason,
                "error_code": error_code,
            },
        }.get(status, {})
        if message_matched:
            message_update = {"provider_status": status, **time_fields}
            try:
                await db.table("whatsapp_messages").update(message_update).eq("provider_message_id", provider_message_id).execute()
            except Exception:
                await db.table("whatsapp_messages").update({"provider_status": status}).eq("provider_message_id", provider_message_id).execute()
        log_update = {"status": status, "error_message": failure_reason, **time_fields}
        try:
            await db.table("whatsapp_logs").update(log_update).eq("provider_message_id", provider_message_id).execute()
        except Exception:
            await db.table("whatsapp_logs").update({"status": status, "error_message": failure_reason}).eq("provider_message_id", provider_message_id).execute()
        return {
            "matched": message_matched or log_matched,
            "message_matched": message_matched,
            "log_matched": log_matched,
        }

    async def list_whatsapp_conversations(
        self,
        status: str = "",
        ai_enabled: Optional[bool] = None,
        search: str = "",
        phone: str = "",
        include_deleted: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        db = await self._db()
        query = db.table("whatsapp_conversations").select("*")
        if not include_deleted:
            query = query.eq("is_deleted", False)
        if phone:
            query = query.eq("phone_number", phone)
        if status and status != "all":
            query = query.eq("status", status)
        if ai_enabled is not None:
            query = query.eq("ai_enabled", ai_enabled)
        if search:
            query = query.ilike("phone_number", f"%{search}%")
        order_column = "created_at" if phone else "last_message_at"
        result = await query.order(order_column, desc=True).limit(max(1, int(limit))).offset(max(0, int(offset))).execute()
        return result.data or []

    async def get_whatsapp_conversation_by_id(self, conv_id: str, include_deleted: bool = False) -> Optional[dict]:
        db = await self._db()
        query = db.table("whatsapp_conversations").select("*").eq("id", conv_id)
        if not include_deleted:
            query = query.eq("is_deleted", False)
        result = await query.limit(1).execute()
        rows = result.data or []
        return rows[0] if rows else None

    async def list_whatsapp_messages(
        self,
        conv_id: str,
        include_deleted: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        db = await self._db()
        query = db.table("whatsapp_messages").select("*").eq("conversation_id", conv_id)
        if not include_deleted:
            query = query.eq("is_deleted", False)
        result = await query.order("created_at", desc=True).limit(max(1, int(limit))).offset(max(0, int(offset))).execute()
        return list(reversed(result.data or []))

    async def soft_delete_whatsapp_message(self, message_id: str, deleted_by: str = "dashboard") -> bool:
        db = await self._db()
        result = await db.table("whatsapp_messages").update({
            "is_deleted": True,
            "deleted_at": iso_now(),
            "deleted_by": deleted_by or "dashboard",
        }).eq("id", message_id).execute()
        return bool(result.data)

    async def soft_delete_whatsapp_conversation(self, conv_id: str, deleted_by: str = "dashboard") -> bool:
        db = await self._db()
        now = iso_now()
        actor = deleted_by or "dashboard"
        conversation_result = await db.table("whatsapp_conversations").update({
            "is_deleted": True,
            "deleted_at": now,
            "deleted_by": actor,
            "updated_at": now,
        }).eq("id", conv_id).execute()
        await db.table("whatsapp_messages").update({
            "is_deleted": True,
            "deleted_at": now,
            "deleted_by": actor,
        }).eq("conversation_id", conv_id).execute()
        return bool(conversation_result.data)

    async def persist_appointment_state(self, conv_id: str, state: dict | str) -> dict:
        return await self.update_whatsapp_conversation(conv_id, {"appointment_state": _json_text(state, {})})

    async def load_appointment_state(self, conv_id: str) -> dict:
        db = await self._db()
        result = await db.table("whatsapp_conversations").select("appointment_state").eq("id", conv_id).limit(1).execute()
        rows = result.data or []
        return _json_value((rows[0] if rows else {}).get("appointment_state"), {})

    async def create_appointment(self, row: dict) -> dict:
        db = await self._db()
        payload = _supabase_payload(dict(row))
        payload.setdefault("id", new_uuid())
        payload.setdefault("created_at", iso_now())
        await db.table("appointments").insert(payload).execute()
        return payload

    async def update_appointment(self, appointment_id: str, updates: dict) -> bool:
        db = await self._db()
        result = await db.table("appointments").update(_supabase_payload(updates)).eq("id", appointment_id).execute()
        return bool(result.data)

    async def clear_appointments(self) -> int:
        db = await self._db()
        result = await db.table("appointments").delete().neq("id", "").execute()
        return len(result.data or [])

    async def list_appointments(
        self,
        date: str = "",
        statuses: Optional[list[str]] = None,
        staff_id: str = "",
        date_from: str = "",
        date_to: str = "",
        ascending: bool = False,
    ) -> list[dict]:
        db = await self._db()
        query = db.table("appointments").select("*")
        query = query.order("date", desc=not ascending).order("time", desc=not ascending)
        if date:
            query = query.eq("date", date)
        if date_from:
            query = query.gte("date", date_from)
        if date_to:
            query = query.lte("date", date_to)
        if staff_id:
            query = query.eq("staff_id", staff_id)
        clean_statuses = [str(item) for item in (statuses or []) if str(item)]
        if len(clean_statuses) == 1:
            query = query.eq("status", clean_statuses[0])
        elif clean_statuses:
            query = query.in_("status", clean_statuses)
        result = await query.execute()
        return result.data or []

    async def get_appointment(self, appointment_id: str, allow_prefix: bool = True) -> Optional[dict]:
        db = await self._db()
        exact = await db.table("appointments").select("*").eq("id", appointment_id).limit(1).execute()
        rows = exact.data or []
        if rows:
            return rows[0]
        if allow_prefix and len(str(appointment_id or "")) >= 4:
            prefix = await db.table("appointments").select("*").ilike("id", f"{appointment_id}%").limit(1).execute()
            prefix_rows = prefix.data or []
            return prefix_rows[0] if prefix_rows else None
        return None

    async def list_appointments_by_phone(
        self,
        phone: str,
        date: str = "",
        time: str = "",
        limit: int = 0,
    ) -> list[dict]:
        db = await self._db()
        query = db.table("appointments").select("*").eq("phone", phone).order("date", desc=True)
        if date:
            query = query.eq("date", date)
        if time:
            query = query.eq("time", time[:5])
        if limit:
            query = query.limit(max(1, int(limit)))
        result = await query.execute()
        return result.data or []

    async def reschedule_appointment_record(self, old_appointment_id: str, new_appointment: dict) -> dict:
        db = await self._db()
        payload = _supabase_payload(dict(new_appointment))
        try:
            await db.table("appointments").insert(payload).execute()
        except Exception:
            legacy_keys = ("id", "name", "phone", "date", "time", "service", "status", "created_at")
            payload = {key: payload[key] for key in legacy_keys if key in payload}
            await db.table("appointments").insert(payload).execute()
            payload["rescheduled_from"] = old_appointment_id
        updates = {
            "status": "rescheduled",
            "rescheduled_at": iso_now(),
            "reminder_processed": True,
        }
        try:
            await db.table("appointments").update(updates).eq("id", old_appointment_id).execute()
        except Exception:
            await db.table("appointments").update({
                "status": "rescheduled",
                "rescheduled_at": updates["rescheduled_at"],
            }).eq("id", old_appointment_id).execute()
        return payload

    async def get_appointments_for_slot(self, date: str, time: str, staff_id: str = "") -> list[dict]:
        db = await self._db()
        query = db.table("appointments").select("*").eq("date", date).eq("time", time[:5]).eq("status", "booked")
        if staff_id:
            query = query.eq("staff_id", staff_id)
        result = await query.execute()
        return result.data or []

    async def get_booked_appointments_for_staff(self, staff_id: str) -> list[dict]:
        db = await self._db()
        result = await db.table("appointments").select("*").eq("staff_id", staff_id).eq("status", "booked").execute()
        return result.data or []

    async def get_active_staff(self) -> list[dict]:
        return await self.list_appointment_staff(include_inactive=False)

    async def list_appointment_staff(self, include_inactive: bool = False) -> list[dict]:
        db = await self._db()
        query = db.table("appointment_staff").select("*")
        if not include_inactive:
            query = query.eq("active", True)
        result = await query.order("round_robin_order").order("created_at").execute()
        return result.data or []

    async def get_appointment_staff(self, staff_id: str) -> Optional[dict]:
        db = await self._db()
        result = await db.table("appointment_staff").select("*").eq("id", staff_id).limit(1).execute()
        rows = result.data or []
        return rows[0] if rows else None

    async def upsert_appointment_staff(self, row: dict, staff_id: Optional[str] = None) -> dict:
        db = await self._db()
        payload = _supabase_payload(dict(row))
        payload["id"] = staff_id or payload.get("id") or new_uuid()
        if staff_id:
            existing = await db.table("appointment_staff").select("id").eq("id", staff_id).limit(1).execute()
            if not (existing.data or []):
                raise ValueError("Staff member not found")
            await db.table("appointment_staff").update(payload).eq("id", staff_id).execute()
        else:
            await db.table("appointment_staff").insert(payload).execute()
        saved = await self.get_appointment_staff(payload["id"])
        if not saved:
            raise RuntimeError("Staff save could not be verified")
        return saved

    async def set_appointment_staff_active(self, staff_id: str, active: bool) -> bool:
        db = await self._db()
        result = await db.table("appointment_staff").update({
            "active": bool(active),
            "updated_at": iso_now(),
        }).eq("id", staff_id).execute()
        return bool(result.data)

    async def delete_appointment_staff(self, staff_id: str) -> bool:
        db = await self._db()
        await db.table("appointment_staff").delete().eq("id", staff_id).execute()
        remaining = await db.table("appointment_staff").select("id").eq("id", staff_id).limit(1).execute()
        return not bool(remaining.data or [])

    async def update_appointment_staff_calendar(self, staff_id: str, updates: dict) -> bool:
        allowed = {"calendar_sync_error", "last_calendar_sync_at"}
        payload = {key: value for key, value in _supabase_payload(updates).items() if key in allowed}
        if not payload:
            return False
        db = await self._db()
        result = await db.table("appointment_staff").update(payload).eq("id", staff_id).execute()
        return bool(result.data)

    async def _rpc_payload(self, appointment: dict, customer_intent: dict, staff_intent: dict) -> tuple[dict, dict, dict]:
        return appointment, customer_intent, staff_intent

    async def create_appointment_with_outbox(self, appointment: dict, customer_intent: dict, staff_intent: dict, event_identity: str = "v1", start_code: Optional[dict] = None) -> dict:
        if start_code:
            raise RuntimeError("Service start codes require the MySQL backend")
        db = await self._db()
        result = await db.rpc("create_appointment_with_notification_outbox", {
            "p_appointment": appointment,
            "p_customer_intent": customer_intent,
            "p_staff_intent": staff_intent,
            "p_event_identity": event_identity,
        }).execute()
        return {"success": True, "result": result.data}

    async def create_followup_action(self, row: dict) -> dict:
        db = await self._db()
        # PostgREST accepts Python objects for jsonb; keep payload/result native.
        payload = dict(row)
        payload.setdefault("id", new_uuid())
        await db.table("followup_actions").insert(payload).execute()
        return payload

    async def list_followup_actions(
        self,
        phone: str = "",
        status: str = "",
        due_before: datetime | str | None = None,
        limit: int = 100,
        ascending: bool = False,
    ) -> list[dict]:
        db = await self._db()
        query = db.table("followup_actions").select("*")
        if phone:
            query = query.eq("phone_number", phone)
        if status:
            query = query.eq("status", status)
        if due_before is not None:
            due_value = due_before.isoformat() if hasattr(due_before, "isoformat") else str(due_before)
            query = query.lte("scheduled_at", due_value)
        if ascending:
            query = query.order("priority", desc=False).order("scheduled_at", desc=False)
        else:
            query = query.order("scheduled_at", desc=True)
        result = await query.limit(max(1, int(limit))).execute()
        return [_followup_row(item) for item in (result.data or [])]

    async def claim_followup_actions(self, due_before: datetime | str, limit: int = 50) -> list[dict]:
        db = await self._db()
        due_value = due_before.isoformat() if hasattr(due_before, "isoformat") else str(due_before)
        candidates = await self.list_followup_actions(
            status="scheduled", due_before=due_value, limit=limit, ascending=True,
        )
        claimed = []
        for row in candidates:
            attempt_number = int(row.get("attempt_number") or 0) + 1
            result = await (
                db.table("followup_actions")
                .update({"status": "processing", "attempt_number": attempt_number, "updated_at": iso_now()})
                .eq("id", row.get("id"))
                .eq("status", "scheduled")
                .lte("scheduled_at", due_value)
                .execute()
            )
            rows = result.data or []
            if rows:
                claimed.append(_followup_row(rows[0]))
        return claimed

    async def update_followup_action_status(
        self,
        action_id: str,
        status: str,
        result: Optional[dict] = None,
        error_message: str = "",
        expected_status: str = "processing",
    ) -> bool:
        db = await self._db()
        payload = {
            "status": status,
            "updated_at": iso_now(),
            "result": result or {},
            "error_message": error_message or "",
        }
        if status in {"completed", "skipped", "failed"}:
            payload["completed_at"] = iso_now()
        query = db.table("followup_actions").update(payload).eq("id", action_id)
        if expected_status:
            query = query.eq("status", expected_status)
        updated = await query.execute()
        return bool(updated.data)

    async def reschedule_followup_action(
        self,
        action_id: str,
        scheduled_at: datetime | str,
        reason: str = "",
        expected_status: str = "processing",
    ) -> bool:
        db = await self._db()
        scheduled_value = scheduled_at.isoformat() if hasattr(scheduled_at, "isoformat") else str(scheduled_at)
        query = db.table("followup_actions").update({
            "status": "scheduled",
            "scheduled_at": scheduled_value,
            "updated_at": iso_now(),
            "error_message": reason or "",
        }).eq("id", action_id)
        if expected_status:
            query = query.eq("status", expected_status)
        updated = await query.execute()
        return bool(updated.data)

    async def enqueue_notification(self, row: dict) -> dict:
        db = await self._db()
        # Keep JSON values as objects, matching the original PostgREST insert.
        payload = dict(row or {})
        dedupe_key = str(payload.get("dedupe_key") or "").strip()
        if not dedupe_key:
            raise ValueError("notification dedupe_key is required")
        payload.setdefault("id", new_uuid())
        existing = await db.table("notification_outbox").select("*").eq("dedupe_key", dedupe_key).limit(1).execute()
        if existing.data:
            return existing.data[0]
        try:
            inserted = await db.table("notification_outbox").insert(payload).execute()
            rows = inserted.data or [payload]
            return rows[0] if isinstance(rows, list) and rows else payload
        except Exception as insert_exc:
            existing = await db.table("notification_outbox").select("*").eq("dedupe_key", dedupe_key).limit(1).execute()
            if existing.data:
                return existing.data[0]
            raise insert_exc

    async def claim_notification_outbox(self, limit: int = 10, lease_seconds: int = 300) -> list[dict]:
        db = await self._db()
        result = await db.rpc("claim_notification_outbox", {"batch_limit": limit, "lease_seconds": lease_seconds}).execute()
        return result.data or []

    async def complete_notification_outbox(self, row_id: str, claim_token: str, provider_message_id: str) -> bool:
        db = await self._db()
        if claim_token:
            await db.rpc("mark_notification_outbox_sent", {
                "p_id": row_id,
                "p_claim_token": claim_token,
                "p_provider_message_id": provider_message_id or "",
            }).execute()
        else:
            await db.table("notification_outbox").update({
                "status": "sent",
                "provider_message_id": provider_message_id or "",
                "sent_at": iso_now(),
                "last_error": "",
            }).eq("id", row_id).execute()
        return True

    async def fail_notification_outbox(self, row_id: str, claim_token: str, status: str, safe_error: str, next_attempt_at: str = "") -> bool:
        db = await self._db()
        if claim_token:
            await db.rpc("mark_notification_outbox_failed", {
                "p_id": row_id,
                "p_claim_token": claim_token,
                "p_status": status,
                "p_last_error": safe_error,
                "p_next_attempt_at": next_attempt_at or None,
            }).execute()
        else:
            payload = {"status": status, "last_error": safe_error[:500]}
            if status == "failed" and next_attempt_at:
                payload["next_attempt_at"] = next_attempt_at
            await db.table("notification_outbox").update(payload).eq("id", row_id).execute()
        return True

    async def recover_stuck_notification_outbox(self, max_processing_age_minutes: int = 15, limit: int = 50) -> int:
        db = await self._db()
        now = utc_now()
        cutoff = (now - timedelta(minutes=max(1, int(max_processing_age_minutes)))).isoformat()
        result = await db.table("notification_outbox").select("*").eq("status", "processing").lte("updated_at", cutoff).limit(max(1, min(int(limit), 100))).execute()
        recovered = 0
        for row in result.data or []:
            lease = row.get("lease_expires_at")
            if lease:
                try:
                    lease_at = datetime.fromisoformat(str(lease).replace("Z", "+00:00"))
                    if lease_at.tzinfo is None:
                        lease_at = lease_at.replace(tzinfo=timezone.utc)
                    if lease_at > now:
                        continue
                except (TypeError, ValueError):
                    continue
            attempts = int(row.get("attempt_count") or 0)
            max_attempts = int(row.get("max_attempts") or 5)
            status = "permanently_failed" if attempts >= max_attempts else "failed"
            retry_at = _notification_retry_at(attempts).isoformat()
            query = db.table("notification_outbox").update({
                "status": status,
                "next_attempt_at": retry_at,
                "claim_token": None,
                "lease_expires_at": None,
                "processing_started_at": None,
                "updated_at": iso_now(),
            }).eq("id", row.get("id")).eq("status", "processing")
            # Optimistic ownership check: a worker reclaim updates this value,
            # so recovery cannot overwrite a newer active claim.
            if row.get("updated_at"):
                query = query.eq("updated_at", row.get("updated_at"))
            updated = await query.execute()
            if updated.data:
                recovered += 1
        return recovered

    async def reschedule_appointment_with_outbox(
        self,
        old_appointment_id: str,
        new_appointment: dict,
        customer_intent: dict,
        staff_intent: dict,
        event_identity: str = "v1",
        start_code: Optional[dict] = None,
    ) -> dict:
        if start_code:
            raise RuntimeError("Service start codes require the MySQL backend")
        db = await self._db()
        result = await db.rpc("reschedule_appointment_with_notification_outbox", {
            "p_old_appointment_id": old_appointment_id,
            "p_new_appointment": new_appointment,
            "p_customer_intent": customer_intent,
            "p_staff_intent": staff_intent,
            "p_event_identity": event_identity,
        }).execute()
        return {"success": True, "result": result.data}

    async def cancel_appointment_with_outbox(
        self,
        appointment_id: str,
        customer_intent: dict,
        staff_intent: dict,
        event_identity: str = "v1",
    ) -> dict:
        db = await self._db()
        result = await db.rpc("cancel_appointment_with_notification_outbox", {
            "p_appointment_id": appointment_id,
            "p_customer_intent": customer_intent,
            "p_staff_intent": staff_intent,
            "p_event_identity": event_identity,
        }).execute()
        return {"success": True, "result": result.data}

    async def probe_notification_outbox_storage(self, row: dict) -> bool:
        db = await self._db()
        payload = dict(row or {})
        payload.setdefault("id", new_uuid())
        dedupe_key = str(payload.get("dedupe_key") or "").strip()
        if not dedupe_key:
            raise ValueError("notification dedupe_key is required")
        try:
            await db.table("notification_outbox").insert(payload).execute()
            result = await db.table("notification_outbox").select("id,status,dedupe_key").eq("dedupe_key", dedupe_key).limit(1).execute()
            return bool(result.data)
        finally:
            await db.table("notification_outbox").delete().eq("dedupe_key", dedupe_key).execute()

    async def backend_health_check(self) -> dict:
        db = await self._db()
        try:
            rpc_result = await db.rpc("airzen_schema_health").execute()
            payload = rpc_result.data or {}
            if isinstance(payload, list):
                payload = payload[0] if payload else {}
            if isinstance(payload, dict):
                return {"backend": "supabase", "connection_ok": True, **payload}
        except Exception:
            pass
        required_tables = [
            "crm_contacts", "contact_memory", "whatsapp_conversations", "whatsapp_messages", "whatsapp_logs",
            "appointments", "appointment_staff", "followup_actions", "notification_outbox",
            "settings", "error_logs", "lead_statuses", "crm_tags", "call_logs", "agent_profiles",
            "campaigns", "automation_actions", "broadcast_campaigns", "broadcast_recipients",
            "whatsapp_test_recipients", "whatsapp_health_test_runs", "whatsapp_health_test_results",
        ]
        present = []
        missing = []
        for table in required_tables:
            try:
                await db.table(table).select("*").limit(1).execute()
                present.append(table)
            except Exception:
                missing.append(table)
        return {
            "backend": "supabase",
            "connection_ok": not missing,
            "required_tables_present": present,
            "required_tables_missing": missing,
            "required_columns_missing": {},
            "critical_constraints_missing": {"schema_manifest": ["airzen_schema_health RPC unavailable"]},
            "critical_indexes_missing": {},
            "validation_mode": "table_fallback",
            "ok": False,
        }

    async def staff_mobile_schema_health(self) -> dict:
        return {
            "backend": "supabase",
            "ok": False,
            "required_tables_present": [],
            "required_tables_missing": [
                "staff_mobile_credentials",
                "staff_mobile_sessions",
                "appointment_job_events",
                "appointment_tracking_events",
                "appointment_evidence",
                "appointment_start_otps",
                "staff_attendance",
                "staff_leave_requests",
            ],
            "required_columns_missing": {},
            "critical_constraints_missing": {},
            "critical_indexes_missing": {},
            "reason": "The staff mobile application requires the MySQL backend",
        }

    async def disable_staff_mobile_credential(self, staff_id: str) -> bool:
        return False


class MySQLRepository:
    def __init__(self, pool: Any = None):
        self._pool = pool

    async def _ensure_pool(self):
        if self._pool is not None:
            return self._pool
        try:
            import aiomysql as mysql_driver
        except Exception:
            try:
                import asyncmy as mysql_driver
            except Exception as exc:
                raise RuntimeError("MySQL backend requires aiomysql or asyncmy") from exc
        self._pool = await mysql_driver.create_pool(
            host=os.getenv("MYSQL_HOST", "127.0.0.1"),
            port=int(os.getenv("MYSQL_PORT", "3306") or 3306),
            user=os.getenv("MYSQL_USER", ""),
            password=os.getenv("MYSQL_PASSWORD", ""),
            db=os.getenv("MYSQL_DATABASE", ""),
            autocommit=False,
            charset="utf8mb4",
        )
        return self._pool

    async def close(self) -> None:
        """Close the connection pool on the event loop that owns it."""
        pool = self._pool
        if pool is None:
            return

        try:
            close_result = pool.close()
            if inspect.isawaitable(close_result):
                await close_result

            wait_closed = getattr(pool, "wait_closed", None)
            if wait_closed is not None:
                wait_result = wait_closed()
                if inspect.isawaitable(wait_result):
                    await wait_result
        finally:
            if self._pool is pool:
                self._pool = None

    async def _fetchone(self, sql: str, params: tuple = ()) -> Optional[dict]:
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            try:
                async with conn.cursor() as cur:
                    await cur.execute(sql, params)
                    row = await cur.fetchone()
                    result = None
                    if row:
                        cols = [d[0] for d in cur.description]
                        result = _mysql_result_row(dict(zip(cols, row)))
                await conn.rollback()
                return result
            except BaseException:
                await _rollback_quietly(conn)
                raise

    async def _fetchall(self, sql: str, params: tuple = ()) -> list[dict]:
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            try:
                async with conn.cursor() as cur:
                    await cur.execute(sql, params)
                    rows = await cur.fetchall()
                    cols = [d[0] for d in cur.description]
                    result = [_mysql_result_row(dict(zip(cols, row))) or {} for row in rows]
                await conn.rollback()
                return result
            except BaseException:
                await _rollback_quietly(conn)
                raise

    async def _execute(self, sql: str, params: tuple = ()) -> int:
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            try:
                async with conn.cursor() as cur:
                    await cur.execute(sql, params)
                    rowcount = cur.rowcount
                await conn.commit()
                return rowcount
            except BaseException:
                await _rollback_quietly(conn)
                raise

    async def _insert_row(self, table: str, row: dict, json_fields: set[str] | None = None) -> dict:
        payload = _mysql_payload(row, json_fields)
        columns = list(payload.keys())
        await self._execute(
            f"INSERT INTO `{table}` ({','.join(f'`{column}`' for column in columns)}) "
            f"VALUES ({','.join(['%s'] * len(columns))})",
            tuple(payload[column] for column in columns),
        )
        return payload

    async def _update_row(
        self,
        table: str,
        row_id: str,
        updates: dict,
        json_fields: set[str] | None = None,
        claim_token: str = "",
        owned_status: str = "",
    ) -> bool:
        payload = _mysql_payload(updates, json_fields)
        if not payload:
            return False
        assignments = ",".join(f"`{key}`=%s" for key in payload)
        where = "id=%s"
        params = tuple(payload.values()) + (row_id,)
        if claim_token:
            where += " AND claim_token=%s"
            params += (claim_token,)
        if owned_status:
            where += " AND status=%s"
            params += (owned_status,)
        return await self._execute(f"UPDATE `{table}` SET {assignments} WHERE {where}", params) > 0

    async def get_setting(self, key: str) -> Optional[str]:
        row = await self._fetchone("SELECT value FROM settings WHERE `key`=%s LIMIT 1", (key,))
        return row.get("value") if row else None

    async def set_setting(self, key: str, value: str) -> None:
        await self._execute(
            "INSERT INTO settings (`key`, value, updated_at) VALUES (%s,%s,%s) "
            "ON DUPLICATE KEY UPDATE value=VALUES(value), updated_at=VALUES(updated_at)",
            (key, value, utc_now_mysql()),
        )

    async def list_settings(self) -> dict[str, str]:
        rows = await self._fetchall("SELECT `key`, value FROM settings ORDER BY `key`")
        return {str(row["key"]): row.get("value") for row in rows}

    async def set_settings(self, settings: dict[str, str]) -> None:
        if not settings:
            return
        updated_at = utc_now_mysql()
        values_sql = ",".join(["(%s,%s,%s)"] * len(settings))
        params = tuple(
            value
            for key, setting_value in settings.items()
            for value in (key, setting_value, updated_at)
        )
        await self._execute(
            f"INSERT INTO settings (`key`, value, updated_at) VALUES {values_sql} "
            "ON DUPLICATE KEY UPDATE value=VALUES(value), updated_at=VALUES(updated_at)",
            params,
        )

    async def create_error_log(self, row: dict) -> None:
        payload = dict(row or {})
        payload.setdefault("id", new_uuid())
        payload.setdefault("timestamp", utc_now_mysql())
        await self._insert_row("error_logs", payload)

    async def create_error_logs(self, rows: list[dict]) -> None:
        if not rows:
            return
        for row in rows:
            await self.create_error_log(row)

    async def list_error_logs(self, level: str = "", source: str = "", limit: int = 200) -> list[dict]:
        clauses: list[str] = []
        params: list[Any] = []
        if level:
            clauses.append("level=%s")
            params.append(level)
        if source:
            clauses.append("source=%s")
            params.append(source)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, min(int(limit), 5000)))
        return await self._fetchall(f"SELECT * FROM error_logs{where} ORDER BY `timestamp` DESC LIMIT %s", tuple(params))

    async def clear_error_logs(self) -> int:
        return await self._execute("DELETE FROM error_logs")

    async def list_lead_statuses(self) -> list[dict]:
        return await self._fetchall("SELECT * FROM lead_statuses ORDER BY created_at")

    async def upsert_lead_status(self, row: dict) -> dict:
        payload = _mysql_payload(row)
        payload.setdefault("id", new_uuid())
        payload.setdefault("created_at", utc_now_mysql())
        await self._execute(
            "INSERT INTO lead_statuses (id,name,color,created_at) VALUES (%s,%s,%s,%s) "
            "ON DUPLICATE KEY UPDATE color=VALUES(color)",
            (payload["id"], payload["name"], payload.get("color"), payload["created_at"]),
        )
        return await self._fetchone("SELECT * FROM lead_statuses WHERE name=%s LIMIT 1", (payload["name"],)) or payload

    async def delete_lead_status(self, status_id: str) -> bool:
        return await self._execute("DELETE FROM lead_statuses WHERE id=%s OR name=%s", (status_id, status_id)) > 0

    async def list_crm_tags(self, limit: int = 500) -> list[dict]:
        return await self._fetchall("SELECT * FROM crm_tags ORDER BY created_at DESC LIMIT %s", (max(1, min(int(limit), 5000)),))

    async def upsert_crm_tag(self, row: dict) -> dict:
        payload = _mysql_payload(row)
        payload.setdefault("color", "#6366f1")
        payload.setdefault("created_at", utc_now_mysql())
        await self._execute(
            "INSERT INTO crm_tags (name,color,created_at) VALUES (%s,%s,%s) "
            "ON DUPLICATE KEY UPDATE color=VALUES(color)",
            (payload["name"], payload["color"], payload["created_at"]),
        )
        return await self._fetchone("SELECT * FROM crm_tags WHERE name=%s LIMIT 1", (payload["name"],)) or payload

    async def delete_crm_tag(self, name: str) -> bool:
        return await self._execute("DELETE FROM crm_tags WHERE name=%s", (name,)) > 0

    async def create_call_log(self, row: dict) -> dict:
        payload = dict(row or {})
        payload.setdefault("id", new_uuid())
        payload.setdefault("timestamp", utc_now_mysql())
        return await self._insert_row("call_logs", payload)

    async def list_call_logs(self, phone: str = "", outcome: str = "", date_from: str = "", date_to: str = "", limit: int = 1000, offset: int = 0) -> list[dict]:
        clauses: list[str] = []
        params: list[Any] = []
        for column, value in (("phone_number", phone), ("outcome", outcome)):
            if value:
                clauses.append(f"`{column}`=%s")
                params.append(value)
        if date_from:
            clauses.append("`timestamp` >= %s")
            params.append(_dt(date_from))
        if date_to:
            clauses.append("`timestamp` <= %s")
            params.append(_dt(date_to))
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        params.extend((max(1, min(int(limit), 5000)), max(0, int(offset))))
        return await self._fetchall(f"SELECT * FROM call_logs{where} ORDER BY `timestamp` DESC LIMIT %s OFFSET %s", tuple(params))

    async def list_recordings_for_cleanup(self, cutoff: datetime | str) -> list[dict]:
        return await self._fetchall(
            "SELECT id,recording_url,recording_object_key,recording_deleted,`timestamp` FROM call_logs "
            "WHERE recording_deleted=FALSE AND `timestamp` < %s "
            "AND (COALESCE(recording_url,'') <> '' OR COALESCE(recording_object_key,'') <> '')",
            (_dt(cutoff),),
        )

    async def update_call_log(self, call_id: str, updates: dict) -> bool:
        return await self._update_row("call_logs", call_id, updates)

    async def get_recording_storage_stats(self) -> dict:
        row = await self._fetchone(
            "SELECT SUM(CASE WHEN COALESCE(recording_url,'') <> '' OR recording_deleted=TRUE THEN 1 ELSE 0 END) AS total_recordings, "
            "SUM(CASE WHEN recording_deleted=TRUE THEN 1 ELSE 0 END) AS deleted_recordings, "
            "SUM(CASE WHEN recording_deleted=FALSE AND COALESCE(recording_url,'') <> '' THEN 1 ELSE 0 END) AS active_recordings, "
            "COALESCE(SUM(CASE WHEN recording_deleted=FALSE THEN recording_size_bytes ELSE 0 END),0) AS total_size_bytes FROM call_logs"
        ) or {}
        return {key: int(row.get(key) or 0) for key in ("total_recordings", "deleted_recordings", "active_recordings", "total_size_bytes")}

    async def clear_call_logs(self) -> int:
        return await self._execute("DELETE FROM call_logs")

    async def list_agent_profiles(self) -> list[dict]:
        return await self._fetchall("SELECT * FROM agent_profiles ORDER BY created_at")

    async def get_agent_profile(self, profile_id: str) -> Optional[dict]:
        return await self._fetchone("SELECT * FROM agent_profiles WHERE id=%s LIMIT 1", (profile_id,))

    async def create_agent_profile(self, row: dict, set_default: bool = False) -> dict:
        payload = _mysql_payload(row)
        payload.setdefault("id", new_uuid())
        payload.setdefault("created_at", utc_now_mysql())
        payload["is_default"] = False
        await self._insert_row("agent_profiles", payload)
        if set_default:
            await self.set_default_agent_profile(payload["id"])
        return await self.get_agent_profile(payload["id"]) or payload

    async def update_agent_profile(self, profile_id: str, updates: dict) -> bool:
        payload = dict(updates or {})
        make_default = payload.pop("is_default", None) in (1, True)
        changed = await self._update_row("agent_profiles", profile_id, payload) if payload else False
        if make_default:
            return await self.set_default_agent_profile(profile_id)
        return changed

    async def delete_agent_profile(self, profile_id: str) -> bool:
        return await self._execute("DELETE FROM agent_profiles WHERE id=%s", (profile_id,)) > 0

    async def set_default_agent_profile(self, profile_id: str) -> bool:
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                try:
                    await conn.begin()
                    await cur.execute("SELECT id FROM agent_profiles WHERE id=%s FOR UPDATE", (profile_id,))
                    if not await cur.fetchone():
                        await conn.rollback()
                        return False
                    await cur.execute("UPDATE agent_profiles SET is_default=FALSE WHERE is_default=TRUE")
                    await cur.execute("UPDATE agent_profiles SET is_default=TRUE WHERE id=%s", (profile_id,))
                    await conn.commit()
                    return cur.rowcount > 0
                except BaseException:
                    await _rollback_quietly(conn)
                    raise

    async def create_campaign(self, row: dict) -> dict:
        payload = dict(row or {})
        payload.setdefault("id", new_uuid())
        payload.setdefault("created_at", utc_now_mysql())
        payload.setdefault("updated_at", utc_now_mysql())
        return await self._insert_row("campaigns", payload, {"contacts_json"})

    async def list_campaigns(self) -> list[dict]:
        return await self._fetchall("SELECT * FROM campaigns ORDER BY created_at DESC")

    async def get_campaign(self, campaign_id: str) -> Optional[dict]:
        return await self._fetchone("SELECT * FROM campaigns WHERE id=%s LIMIT 1", (campaign_id,))

    async def update_campaign(self, campaign_id: str, updates: dict, claim_token: str = "") -> bool:
        payload = dict(updates or {})
        payload.setdefault("updated_at", utc_now_mysql())
        if claim_token:
            payload.setdefault("lease_expires_at", (utc_now() + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S.%f"))
        return await self._update_row("campaigns", campaign_id, payload, {"contacts_json"}, claim_token)

    async def delete_campaign(self, campaign_id: str) -> bool:
        return await self._execute("DELETE FROM campaigns WHERE id=%s", (campaign_id,)) > 0

    async def clear_campaigns(self) -> int:
        return await self._execute("DELETE FROM campaigns")

    async def claim_campaign(self, campaign_id: str, lease_seconds: int = 3600) -> Optional[dict]:
        token = new_uuid()
        now_utc = utc_now()
        now = now_utc.strftime("%Y-%m-%d %H:%M:%S.%f")
        lease = (now_utc + timedelta(seconds=max(30, min(int(lease_seconds), 86400)))).strftime("%Y-%m-%d %H:%M:%S.%f")
        now_mysql = now_utc.replace(tzinfo=None)
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                try:
                    await conn.begin()
                    await cur.execute("SELECT * FROM campaigns WHERE id=%s FOR UPDATE", (campaign_id,))
                    row = await cur.fetchone()
                    if not row:
                        await conn.rollback()
                        return None
                    columns = [d[0] for d in cur.description]
                    campaign = dict(zip(columns, row))
                    if campaign.get("claim_token") and campaign.get("lease_expires_at") and campaign["lease_expires_at"] > now_mysql:
                        await conn.rollback()
                        return None
                    if campaign.get("status") not in {"active", "scheduled", "running"}:
                        await conn.rollback()
                        return None
                    await cur.execute(
                        "UPDATE campaigns SET status='running',claim_token=%s,lease_expires_at=%s,updated_at=%s WHERE id=%s",
                        (token, lease, now, campaign_id),
                    )
                    await cur.execute("SELECT * FROM campaigns WHERE id=%s", (campaign_id,))
                    claimed_row = await cur.fetchone()
                    claimed = dict(zip([d[0] for d in cur.description], claimed_row))
                    await conn.commit()
                    return claimed
                except BaseException:
                    await _rollback_quietly(conn)
                    raise

    async def create_automation_action(self, row: dict) -> dict:
        payload = dict(row or {})
        payload.setdefault("id", new_uuid())
        payload.setdefault("created_at", utc_now_mysql())
        payload.setdefault("updated_at", utc_now_mysql())
        return await self._insert_row("automation_actions", payload, {"payload", "result"})

    async def list_automation_actions(self, status: str = "", phone: str = "", limit: int = 100) -> list[dict]:
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("status=%s")
            params.append(status)
        if phone:
            clauses.append("phone_number=%s")
            params.append(phone)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, min(int(limit), 5000)))
        rows = await self._fetchall(f"SELECT * FROM automation_actions{where} ORDER BY scheduled_at DESC LIMIT %s", tuple(params))
        return [_decoded_row(row, {"payload", "result"}) or {} for row in rows]

    async def claim_automation_actions(self, due_before: datetime | str, limit: int = 20, lease_seconds: int = 300) -> list[dict]:
        token = new_uuid()
        now = utc_now_mysql()
        lease = (utc_now() + timedelta(seconds=max(30, min(int(lease_seconds), 3600)))).strftime("%Y-%m-%d %H:%M:%S.%f")
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                try:
                    await conn.begin()
                    await cur.execute(
                        "SELECT id FROM automation_actions WHERE scheduled_at <= %s AND attempt_count < max_attempts "
                        "AND (status IN ('pending','waiting','waiting_schedule') OR (status='running' AND lease_expires_at <= %s)) "
                        "ORDER BY scheduled_at LIMIT %s FOR UPDATE SKIP LOCKED",
                        (_dt(due_before), now, max(1, min(int(limit), 100))),
                    )
                    ids = [row[0] for row in await cur.fetchall()]
                    if not ids:
                        await conn.commit()
                        return []
                    placeholders = ",".join(["%s"] * len(ids))
                    await cur.execute(
                        f"UPDATE automation_actions SET status='running',action_status='running',attempt_count=attempt_count+1,"
                        f"claim_token=%s,lease_expires_at=%s,processing_started_at=%s,updated_at=%s WHERE id IN ({placeholders})",
                        (token, lease, now, now, *ids),
                    )
                    await cur.execute(f"SELECT * FROM automation_actions WHERE id IN ({placeholders}) ORDER BY scheduled_at", tuple(ids))
                    rows = await cur.fetchall()
                    columns = [d[0] for d in cur.description]
                    await conn.commit()
                    return [_decoded_row(dict(zip(columns, row)), {"payload", "result"}) or {} for row in rows]
                except BaseException:
                    await _rollback_quietly(conn)
                    raise

    async def update_automation_action(self, action_id: str, updates: dict, claim_token: str = "") -> bool:
        payload = dict(updates or {})
        payload.setdefault("updated_at", utc_now_mysql())
        return await self._update_row("automation_actions", action_id, payload, {"payload", "result"}, claim_token, "running" if claim_token else "")

    async def automation_queue_count(self) -> int:
        row = await self._fetchone("SELECT COUNT(*) AS count FROM automation_actions") or {}
        return int(row.get("count") or 0)

    async def create_broadcast_campaign(self, row: dict) -> dict:
        payload = dict(row or {})
        payload.setdefault("id", new_uuid())
        payload.setdefault("created_at", utc_now_mysql())
        payload.setdefault("updated_at", utc_now_mysql())
        return await self._insert_row("broadcast_campaigns", payload, {"segment"})

    async def list_broadcast_campaigns(self, limit: int = 200) -> list[dict]:
        rows = await self._fetchall("SELECT * FROM broadcast_campaigns ORDER BY created_at DESC LIMIT %s", (max(1, min(int(limit), 5000)),))
        return [_decoded_row(row, {"segment"}) or {} for row in rows]

    async def get_broadcast_campaign(self, campaign_id: str) -> Optional[dict]:
        return _decoded_row(await self._fetchone("SELECT * FROM broadcast_campaigns WHERE id=%s LIMIT 1", (campaign_id,)), {"segment"})

    async def update_broadcast_campaign(self, campaign_id: str, updates: dict, claim_token: str = "") -> bool:
        payload = dict(updates or {})
        payload.setdefault("updated_at", utc_now_mysql())
        return await self._update_row("broadcast_campaigns", campaign_id, payload, {"segment"}, claim_token)

    async def claim_broadcast_campaign(self, campaign_id: str, lease_seconds: int = 1800) -> Optional[dict]:
        token = new_uuid()
        now_utc = utc_now()
        now = now_utc.strftime("%Y-%m-%d %H:%M:%S.%f")
        lease = (now_utc + timedelta(seconds=max(30, min(int(lease_seconds), 86400)))).strftime("%Y-%m-%d %H:%M:%S.%f")
        now_mysql = now_utc.replace(tzinfo=None)
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                try:
                    await conn.begin()
                    await cur.execute("SELECT * FROM broadcast_campaigns WHERE id=%s FOR UPDATE", (campaign_id,))
                    row = await cur.fetchone()
                    if not row:
                        await conn.rollback()
                        return None
                    columns = [d[0] for d in cur.description]
                    current = dict(zip(columns, row))
                    if current.get("claim_token") and current.get("lease_expires_at") and current["lease_expires_at"] > now_mysql:
                        await conn.rollback()
                        return None
                    if current.get("status") not in {"draft", "scheduled", "running"}:
                        await conn.rollback()
                        return None
                    await cur.execute(
                        "UPDATE broadcast_campaigns SET status='running',claim_token=%s,lease_expires_at=%s,updated_at=%s WHERE id=%s",
                        (token, lease, now, campaign_id),
                    )
                    await cur.execute("SELECT * FROM broadcast_campaigns WHERE id=%s", (campaign_id,))
                    claimed_row = await cur.fetchone()
                    claimed = dict(zip([d[0] for d in cur.description], claimed_row))
                    await conn.commit()
                    return _decoded_row(claimed, {"segment"})
                except BaseException:
                    await _rollback_quietly(conn)
                    raise

    async def claim_broadcast_recipient(self, row: dict, claim_token: str) -> Optional[dict]:
        payload = dict(row or {})
        payload.setdefault("id", new_uuid())
        payload.update({"status": "sending", "claim_token": claim_token, "claimed_at": utc_now_mysql()})
        payload.setdefault("created_at", utc_now_mysql())
        payload.setdefault("updated_at", utc_now_mysql())
        try:
            return await self._insert_row("broadcast_recipients", payload)
        except Exception as exc:
            if _is_duplicate_error(exc):
                return None
            raise

    async def update_broadcast_recipient(self, recipient_id: str, updates: dict, claim_token: str = "") -> bool:
        payload = dict(updates or {})
        payload.setdefault("updated_at", utc_now_mysql())
        return await self._update_row("broadcast_recipients", recipient_id, payload, claim_token=claim_token, owned_status="sending" if claim_token else "")

    async def list_broadcast_recipients(self, campaign_id: str, limit: int = 500) -> list[dict]:
        return await self._fetchall(
            "SELECT * FROM broadcast_recipients WHERE campaign_id=%s ORDER BY created_at DESC LIMIT %s",
            (campaign_id, max(1, min(int(limit), 5000))),
        )

    async def create_health_recipient(self, row: dict) -> dict:
        payload = dict(row or {})
        payload.setdefault("id", new_uuid())
        payload.setdefault("created_at", utc_now_mysql())
        payload.setdefault("updated_at", utc_now_mysql())
        return await self._insert_row("whatsapp_test_recipients", payload)

    async def get_health_recipient(self, recipient_id: str) -> Optional[dict]:
        return await self._fetchone("SELECT * FROM whatsapp_test_recipients WHERE id=%s LIMIT 1", (recipient_id,))

    async def find_health_recipient_by_fingerprint(self, fingerprint: str, verified_only: bool = False) -> Optional[dict]:
        condition = " AND verified=TRUE AND active=TRUE" if verified_only else ""
        return await self._fetchone(f"SELECT * FROM whatsapp_test_recipients WHERE phone_number_fingerprint=%s{condition} LIMIT 1", (fingerprint,))

    async def list_health_recipients(self) -> list[dict]:
        return await self._fetchall("SELECT * FROM whatsapp_test_recipients ORDER BY created_at DESC")

    async def update_health_recipient(self, recipient_id: str, updates: dict) -> bool:
        payload = dict(updates or {})
        payload.setdefault("updated_at", utc_now_mysql())
        return await self._update_row("whatsapp_test_recipients", recipient_id, payload)

    async def create_health_run(self, row: dict) -> dict:
        payload = dict(row or {})
        payload.setdefault("id", new_uuid())
        payload.setdefault("started_at", utc_now_mysql())
        payload.setdefault("selected_tests", [])
        payload.setdefault("report_json", {})
        return await self._insert_row("whatsapp_health_test_runs", payload, {"selected_tests", "report_json"})

    async def get_health_run(self, run_id: str) -> Optional[dict]:
        return _decoded_row(await self._fetchone("SELECT * FROM whatsapp_health_test_runs WHERE id=%s LIMIT 1", (run_id,)), {"selected_tests", "report_json"})

    async def find_recent_health_run(self, recipient_id: str, started_after: datetime | str) -> Optional[dict]:
        row = await self._fetchone(
            "SELECT * FROM whatsapp_health_test_runs WHERE recipient_id=%s AND started_at >= %s ORDER BY started_at DESC LIMIT 1",
            (recipient_id, _dt(started_after)),
        )
        return _decoded_row(row, {"selected_tests", "report_json"})

    async def find_active_health_run(self, recipient_id: str) -> Optional[dict]:
        row = await self._fetchone(
            "SELECT * FROM whatsapp_health_test_runs WHERE recipient_id=%s AND status='RUNNING' ORDER BY started_at DESC LIMIT 1",
            (recipient_id,),
        )
        return _decoded_row(row, {"selected_tests", "report_json"})

    async def list_health_runs(self, limit: int = 100) -> list[dict]:
        rows = await self._fetchall("SELECT * FROM whatsapp_health_test_runs ORDER BY started_at DESC LIMIT %s", (max(1, min(int(limit), 5000)),))
        return [_decoded_row(row, {"selected_tests", "report_json"}) or {} for row in rows]

    async def update_health_run(self, run_id: str, updates: dict) -> bool:
        return await self._update_row("whatsapp_health_test_runs", run_id, updates, {"selected_tests", "report_json"})

    async def upsert_health_result(self, row: dict) -> dict:
        source = dict(row or {})
        source.setdefault("details_json", {})
        payload = _mysql_payload(source, {"details_json", "payload_json", "provider_response_json"})
        payload.setdefault("id", new_uuid())
        columns = list(payload.keys())
        updates = ",".join(f"`{column}`=VALUES(`{column}`)" for column in columns if column not in {"id", "test_run_id", "test_key"})
        await self._execute(
            f"INSERT INTO whatsapp_health_test_results ({','.join(f'`{column}`' for column in columns)}) "
            f"VALUES ({','.join(['%s'] * len(columns))}) ON DUPLICATE KEY UPDATE {updates}",
            tuple(payload[column] for column in columns),
        )
        return await self.get_health_result(payload["test_run_id"], payload["test_key"]) or payload

    async def get_health_result(self, run_id: str, test_key: str) -> Optional[dict]:
        row = await self._fetchone("SELECT * FROM whatsapp_health_test_results WHERE test_run_id=%s AND test_key=%s LIMIT 1", (run_id, test_key))
        return _decoded_row(row, {"details_json", "payload_json", "provider_response_json"})

    async def find_health_result_by_provider_id(self, provider_message_id: str) -> Optional[dict]:
        row = await self._fetchone("SELECT * FROM whatsapp_health_test_results WHERE provider_message_id=%s LIMIT 1", (provider_message_id,))
        return _decoded_row(row, {"details_json", "payload_json", "provider_response_json"})

    async def list_health_results(self, run_id: str) -> list[dict]:
        rows = await self._fetchall("SELECT * FROM whatsapp_health_test_results WHERE test_run_id=%s ORDER BY started_at", (run_id,))
        return [_decoded_row(row, {"details_json", "payload_json", "provider_response_json"}) or {} for row in rows]

    async def update_health_result(self, result_id: str, updates: dict) -> bool:
        return await self._update_row("whatsapp_health_test_results", result_id, updates, {"details_json", "payload_json", "provider_response_json"})

    async def delete_health_run(self, run_id: str) -> bool:
        return await self._execute("DELETE FROM whatsapp_health_test_runs WHERE id=%s", (run_id,)) > 0

    async def list_whatsapp_messages_for_report(self, limit: int = 5000) -> list[dict]:
        return await self._fetchall("SELECT * FROM whatsapp_messages ORDER BY created_at DESC LIMIT %s", (max(1, min(int(limit), 10000)),))

    async def clear_contact_memory(self) -> int:
        return await self._execute("DELETE FROM contact_memory")

    async def get_crm_contact_by_phone(self, phone: str) -> Optional[dict]:
        return await self._fetchone("SELECT * FROM crm_contacts WHERE phone_number=%s LIMIT 1", (phone,))

    async def list_crm_contacts(self, status: str = "", outcome: str = "") -> list[dict]:
        conditions = []
        params = []
        if status:
            conditions.append("crm_status=%s")
            params.append(status)
        if outcome:
            conditions.append("last_call_outcome=%s")
            params.append(outcome)
        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        return await self._fetchall(
            f"SELECT * FROM crm_contacts{where} ORDER BY updated_at DESC",
            tuple(params),
        )

    async def upsert_crm_contact(self, row: dict) -> dict:
        payload = _mysql_payload(_crm_contact_payload(row), {"tags_json", "custom_fields_json"})
        phone = str(payload.get("phone_number") or "").strip()
        if not phone:
            raise ValueError("CRM contact phone_number is required")
        payload["phone_number"] = phone
        payload.setdefault("id", new_uuid())
        payload.setdefault("updated_at", utc_now_mysql())
        payload.setdefault("created_at", utc_now_mysql())
        columns = list(payload.keys())
        placeholders = ",".join(["%s"] * len(columns))
        updates = ",".join([f"`{c}`=VALUES(`{c}`)" for c in columns if c not in {"id", "phone_number", "created_at"}])
        await self._execute(
            f"INSERT INTO crm_contacts ({','.join(f'`{c}`' for c in columns)}) VALUES ({placeholders}) "
            f"ON DUPLICATE KEY UPDATE {updates}",
            tuple(payload[c] for c in columns),
        )
        return await self.get_crm_contact_by_phone(payload["phone_number"]) or payload

    async def update_crm_contact(self, phone: str, updates: dict) -> bool:
        payload = _mysql_payload(_crm_contact_payload(updates), {"tags_json", "custom_fields_json"})
        if not payload:
            return False
        payload["updated_at"] = utc_now_mysql()
        assignments = ",".join([f"`{k}`=%s" for k in payload])
        count = await self._execute(f"UPDATE crm_contacts SET {assignments} WHERE phone_number=%s", tuple(payload.values()) + (phone,))
        return count > 0

    async def delete_crm_contact_by_phone(self, phone: str) -> bool:
        return await self._execute("DELETE FROM crm_contacts WHERE phone_number=%s", (phone,)) > 0

    async def add_contact_memory(self, phone: str, insight: str) -> str:
        row_id = new_uuid()
        await self._execute(
            "INSERT INTO contact_memory (id, phone_number, insight, created_at) VALUES (%s,%s,%s,%s)",
            (row_id, phone, insight[:1000], utc_now_mysql()),
        )
        return row_id

    async def get_contact_memory(self, phone: str, limit: int = 20) -> list[dict]:
        return await self._fetchall(
            "SELECT insight, created_at FROM contact_memory WHERE phone_number=%s ORDER BY created_at DESC LIMIT %s",
            (phone, int(limit)),
        )

    async def replace_contact_memory(self, phone: str, compressed: str) -> str:
        row_id = new_uuid()
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                try:
                    await conn.begin()
                    await cur.execute("DELETE FROM contact_memory WHERE phone_number=%s", (phone,))
                    await cur.execute(
                        "INSERT INTO contact_memory (id, phone_number, insight, created_at) VALUES (%s,%s,%s,%s)",
                        (row_id, phone, compressed[:2000], utc_now_mysql()),
                    )
                    await conn.commit()
                    return row_id
                except BaseException:
                    await _rollback_quietly(conn)
                    raise

    async def get_or_create_whatsapp_conversation(self, phone: str, contact_name: str = "") -> dict:
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                try:
                    await conn.begin()
                    await cur.execute(
                        "SELECT * FROM whatsapp_conversations WHERE phone_number=%s ORDER BY created_at DESC LIMIT 1 FOR UPDATE",
                        (phone,),
                    )
                    row = await cur.fetchone()
                    if row:
                        cols = [d[0] for d in cur.description]
                        conv = dict(zip(cols, row))
                        updates = {}
                        if conv.get("is_deleted"):
                            updates.update({"is_deleted": 0, "deleted_at": None, "deleted_by": "", "status": "open"})
                        if conv.get("ai_enabled") is None:
                            updates["ai_enabled"] = 1
                        if contact_name and (not conv.get("contact_name") or conv.get("contact_name") == conv.get("phone_number")):
                            updates["contact_name"] = contact_name
                        if updates:
                            updates["updated_at"] = utc_now_mysql()
                            assignments = ",".join([f"`{k}`=%s" for k in updates])
                            await cur.execute(f"UPDATE whatsapp_conversations SET {assignments} WHERE id=%s", tuple(updates.values()) + (conv["id"],))
                            conv.update(updates)
                        await conn.commit()
                        return conv
                    now = utc_now_mysql()
                    conv = {
                        "id": new_uuid(),
                        "phone_number": phone,
                        "contact_name": contact_name or phone,
                        "crm_contact_id": "",
                        "status": "open",
                        "ai_enabled": 1,
                        "assigned_to": "",
                        "last_message": "",
                        "last_message_at": now,
                        "unread_count": 0,
                        "source": "whatsapp",
                        "created_at": now,
                        "updated_at": now,
                        "appointment_state": "{}",
                    }
                    columns = list(conv.keys())
                    await cur.execute(
                        f"INSERT INTO whatsapp_conversations ({','.join(f'`{c}`' for c in columns)}) "
                        f"VALUES ({','.join(['%s'] * len(columns))}) "
                        "ON DUPLICATE KEY UPDATE updated_at=updated_at",
                        tuple(conv[c] for c in columns),
                    )
                    await cur.execute(
                        "SELECT * FROM whatsapp_conversations WHERE phone_number=%s ORDER BY created_at DESC LIMIT 1",
                        (phone,),
                    )
                    stored = await cur.fetchone()
                    if stored:
                        cols = [d[0] for d in cur.description]
                        conv = dict(zip(cols, stored))
                    await conn.commit()
                    return conv
                except BaseException:
                    await _rollback_quietly(conn)
                    raise

    async def save_whatsapp_message(self, row: dict) -> dict:
        payload = _mysql_payload(row, {"raw_payload"})
        payload.setdefault("id", new_uuid())
        payload.setdefault("created_at", utc_now_mysql())
        payload["raw_payload"] = _json_text(payload.get("raw_payload"), {})
        columns = list(payload.keys())
        await self._execute(
            f"INSERT INTO whatsapp_messages ({','.join(f'`{c}`' for c in columns)}) VALUES ({','.join(['%s'] * len(columns))})",
            tuple(payload[c] for c in columns),
        )
        return payload

    async def get_inbound_whatsapp_message_by_provider_id(self, provider_message_id: str) -> dict:
        if not provider_message_id:
            return {}
        return await self._fetchone(
            "SELECT * FROM whatsapp_messages WHERE provider_message_id=%s AND direction='inbound' LIMIT 1",
            (provider_message_id,),
        ) or {}

    async def update_inbound_whatsapp_message_status(self, provider_message_id: str, status: str) -> bool:
        if not provider_message_id:
            return False
        count = await self._execute(
            "UPDATE whatsapp_messages SET provider_status=%s WHERE provider_message_id=%s AND direction='inbound'",
            (status, provider_message_id),
        )
        return count > 0

    async def update_whatsapp_conversation(self, conv_id: str, updates: dict) -> dict:
        payload = _mysql_payload(updates, {"appointment_state"})
        if "appointment_state" in payload:
            payload["appointment_state"] = _json_text(payload["appointment_state"], {})
        payload["updated_at"] = utc_now_mysql()
        assignments = ",".join([f"`{k}`=%s" for k in payload])
        await self._execute(f"UPDATE whatsapp_conversations SET {assignments} WHERE id=%s", tuple(payload.values()) + (conv_id,))
        return await self._fetchone("SELECT * FROM whatsapp_conversations WHERE id=%s LIMIT 1", (conv_id,)) or {}

    async def update_whatsapp_conversation_last_message(self, conv_id: str, text: str, increment_unread: bool = True) -> dict:
        payload = {
            "last_message": text[:200],
            "last_message_at": utc_now_mysql(),
            "status": "open",
            "updated_at": utc_now_mysql(),
        }
        assignments = [f"`{key}`=%s" for key in payload]
        values: list[Any] = list(payload.values())
        if increment_unread:
            assignments.append("unread_count=COALESCE(unread_count, 0)+1")
        await self._execute(
            f"UPDATE whatsapp_conversations SET {','.join(assignments)} WHERE id=%s",
            tuple(values + [conv_id]),
        )
        return await self._fetchone("SELECT * FROM whatsapp_conversations WHERE id=%s LIMIT 1", (conv_id,)) or {}

    async def has_recent_inbound_whatsapp_message(self, phone_or_conv_id: str, since_utc: datetime) -> bool:
        row = await self._fetchone(
            """
            SELECT id FROM whatsapp_messages
            WHERE direction='inbound'
              AND created_at >= %s
              AND (phone_number=%s OR conversation_id=%s)
            LIMIT 1
            """,
            (_dt(since_utc), phone_or_conv_id, phone_or_conv_id),
        )
        return bool(row)

    async def create_whatsapp_log(self, row: dict) -> dict:
        payload = _mysql_payload(row, {"parameters"})
        payload.setdefault("id", new_uuid())
        payload.setdefault("created_at", utc_now_mysql())
        payload["parameters"] = _json_text(payload.get("parameters"), [])
        if "cooldown_until" in payload:
            payload["cooldown_until"] = _dt(payload.get("cooldown_until"))
        columns = list(payload.keys())
        await self._execute(
            f"INSERT INTO whatsapp_logs ({','.join(f'`{column}`' for column in columns)}) "
            f"VALUES ({','.join(['%s'] * len(columns))})",
            tuple(payload[column] for column in columns),
        )
        return payload

    async def list_whatsapp_logs(
        self,
        phone: str = "",
        event_type: str = "",
        template_name: str = "",
        status: str = "",
        since_utc: datetime | str | None = None,
        provider_message_id: str = "",
        limit: int = 50,
    ) -> list[dict]:
        clauses: list[str] = []
        params: list[Any] = []
        filters = (
            ("phone_number", phone),
            ("event_type", event_type),
            ("template_name", template_name),
            ("status", status),
            ("provider_message_id", provider_message_id),
        )
        for column, value in filters:
            if value:
                clauses.append(f"`{column}`=%s")
                params.append(value)
        if since_utc:
            clauses.append("created_at >= %s")
            params.append(_dt(since_utc))
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, int(limit)))
        return await self._fetchall(
            f"SELECT * FROM whatsapp_logs{where} ORDER BY created_at DESC LIMIT %s",
            tuple(params),
        )

    async def get_whatsapp_message_activity(self) -> dict:
        inbound = await self._fetchone(
            "SELECT created_at FROM whatsapp_messages WHERE direction=%s ORDER BY created_at DESC LIMIT 1",
            ("inbound",),
        )
        outbound = await self._fetchone(
            "SELECT created_at FROM whatsapp_messages WHERE direction=%s AND ai_generated=%s ORDER BY created_at DESC LIMIT 1",
            ("outbound", True),
        )
        return {
            "last_inbound_whatsapp_message_at": (inbound or {}).get("created_at"),
            "last_outbound_ai_message_at": (outbound or {}).get("created_at"),
        }

    async def record_whatsapp_delivery_status(
        self,
        provider_message_id: str,
        status: str,
        status_time: datetime | str,
        failure_reason: str = "",
        error_code: str = "",
    ) -> dict:
        if not provider_message_id:
            return {"matched": False, "message_matched": False, "log_matched": False}
        if status not in {"sent", "delivered", "read", "failed"}:
            raise ValueError("invalid WhatsApp delivery status")
        timestamp = _dt(status_time) or utc_now_mysql()
        time_fields = {
            "delivered": {"delivered_at": timestamp},
            "read": {"read_at": timestamp},
            "failed": {
                "failed_at": timestamp,
                "failure_reason": failure_reason,
                "error_code": error_code,
            },
        }.get(status, {})
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                try:
                    await conn.begin()
                    await cur.execute(
                        "SELECT id FROM whatsapp_messages WHERE provider_message_id=%s LIMIT 1 FOR UPDATE",
                        (provider_message_id,),
                    )
                    message_matched = bool(await cur.fetchone())
                    await cur.execute(
                        "SELECT id FROM whatsapp_logs WHERE provider_message_id=%s LIMIT 1 FOR UPDATE",
                        (provider_message_id,),
                    )
                    log_matched = bool(await cur.fetchone())
                    if message_matched:
                        message_update = {"provider_status": status, **time_fields}
                        assignments = ",".join(f"`{key}`=%s" for key in message_update)
                        await cur.execute(
                            f"UPDATE whatsapp_messages SET {assignments} WHERE provider_message_id=%s",
                            tuple(message_update.values()) + (provider_message_id,),
                        )
                    if log_matched:
                        log_update = {
                            "status": status,
                            "error_message": failure_reason,
                            **time_fields,
                        }
                        assignments = ",".join(f"`{key}`=%s" for key in log_update)
                        await cur.execute(
                            f"UPDATE whatsapp_logs SET {assignments} WHERE provider_message_id=%s",
                            tuple(log_update.values()) + (provider_message_id,),
                        )
                    await conn.commit()
                    return {
                        "matched": message_matched or log_matched,
                        "message_matched": message_matched,
                        "log_matched": log_matched,
                    }
                except BaseException:
                    await _rollback_quietly(conn)
                    raise

    async def list_whatsapp_conversations(
        self,
        status: str = "",
        ai_enabled: Optional[bool] = None,
        search: str = "",
        phone: str = "",
        include_deleted: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        clauses: list[str] = []
        params: list[Any] = []
        if not include_deleted:
            clauses.append("is_deleted=%s")
            params.append(False)
        if phone:
            clauses.append("phone_number=%s")
            params.append(phone)
        if status and status != "all":
            clauses.append("status=%s")
            params.append(status)
        if ai_enabled is not None:
            clauses.append("ai_enabled=%s")
            params.append(ai_enabled)
        if search:
            clauses.append("phone_number LIKE %s")
            params.append(f"%{search}%")
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        params.extend((max(1, int(limit)), max(0, int(offset))))
        order_column = "created_at" if phone else "last_message_at"
        return await self._fetchall(
            f"SELECT * FROM whatsapp_conversations{where} "
            f"ORDER BY `{order_column}` DESC LIMIT %s OFFSET %s",
            tuple(params),
        )

    async def get_whatsapp_conversation_by_id(self, conv_id: str, include_deleted: bool = False) -> Optional[dict]:
        sql = "SELECT * FROM whatsapp_conversations WHERE id=%s"
        params: list[Any] = [conv_id]
        if not include_deleted:
            sql += " AND is_deleted=%s"
            params.append(False)
        return await self._fetchone(sql + " LIMIT 1", tuple(params))

    async def list_whatsapp_messages(
        self,
        conv_id: str,
        include_deleted: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        sql = "SELECT * FROM whatsapp_messages WHERE conversation_id=%s"
        params: list[Any] = [conv_id]
        if not include_deleted:
            sql += " AND is_deleted=%s"
            params.append(False)
        params.extend((max(1, int(limit)), max(0, int(offset))))
        rows = await self._fetchall(sql + " ORDER BY created_at DESC LIMIT %s OFFSET %s", tuple(params))
        return list(reversed(rows))

    async def soft_delete_whatsapp_message(self, message_id: str, deleted_by: str = "dashboard") -> bool:
        count = await self._execute(
            "UPDATE whatsapp_messages SET is_deleted=%s, deleted_at=%s, deleted_by=%s WHERE id=%s",
            (True, utc_now_mysql(), deleted_by or "dashboard", message_id),
        )
        return count > 0

    async def soft_delete_whatsapp_conversation(self, conv_id: str, deleted_by: str = "dashboard") -> bool:
        now = utc_now_mysql()
        actor = deleted_by or "dashboard"
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                try:
                    await conn.begin()
                    await cur.execute(
                        "UPDATE whatsapp_conversations SET is_deleted=%s, deleted_at=%s, deleted_by=%s, updated_at=%s WHERE id=%s",
                        (True, now, actor, now, conv_id),
                    )
                    conversation_matched = cur.rowcount > 0
                    await cur.execute(
                        "UPDATE whatsapp_messages SET is_deleted=%s, deleted_at=%s, deleted_by=%s WHERE conversation_id=%s",
                        (True, now, actor, conv_id),
                    )
                    await conn.commit()
                    return conversation_matched
                except BaseException:
                    await _rollback_quietly(conn)
                    raise

    async def persist_appointment_state(self, conv_id: str, state: dict | str) -> dict:
        return await self.update_whatsapp_conversation(conv_id, {"appointment_state": _json_text(state, {})})

    async def load_appointment_state(self, conv_id: str) -> dict:
        row = await self._fetchone("SELECT appointment_state FROM whatsapp_conversations WHERE id=%s LIMIT 1", (conv_id,))
        return _json_value((row or {}).get("appointment_state"), {})

    async def create_appointment(self, row: dict) -> dict:
        payload = _mysql_appointment_payload(row)
        payload.setdefault("id", new_uuid())
        payload.setdefault("created_at", utc_now_mysql())
        columns = list(payload.keys())
        await self._execute(
            f"INSERT INTO appointments ({','.join(f'`{c}`' for c in columns)}) VALUES ({','.join(['%s'] * len(columns))})",
            tuple(payload[c] for c in columns),
        )
        return payload

    async def update_appointment(self, appointment_id: str, updates: dict) -> bool:
        payload = _mysql_appointment_payload(updates)
        if not payload:
            return False
        assignments = ",".join([f"`{k}`=%s" for k in payload])
        count = await self._execute(f"UPDATE appointments SET {assignments} WHERE id=%s", tuple(payload.values()) + (appointment_id,))
        return count > 0

    async def clear_appointments(self) -> int:
        return await self._execute("DELETE FROM appointments")

    async def list_appointments(
        self,
        date: str = "",
        statuses: Optional[list[str]] = None,
        staff_id: str = "",
        date_from: str = "",
        date_to: str = "",
        ascending: bool = False,
    ) -> list[dict]:
        clauses: list[str] = []
        params: list[Any] = []
        if date:
            clauses.append("`date`=%s")
            params.append(date)
        if date_from:
            clauses.append("`date`>=%s")
            params.append(date_from)
        if date_to:
            clauses.append("`date`<=%s")
            params.append(date_to)
        if staff_id:
            clauses.append("staff_id=%s")
            params.append(staff_id)
        clean_statuses = [str(item) for item in (statuses or []) if str(item)]
        if clean_statuses:
            clauses.append(f"status IN ({','.join(['%s'] * len(clean_statuses))})")
            params.extend(clean_statuses)
        direction = "ASC" if ascending else "DESC"
        sql = "SELECT * FROM appointments"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += f" ORDER BY `date` {direction}, `time` {direction}"
        return await self._fetchall(sql, tuple(params))

    async def get_appointment(self, appointment_id: str, allow_prefix: bool = True) -> Optional[dict]:
        row = await self._fetchone("SELECT * FROM appointments WHERE id=%s LIMIT 1", (appointment_id,))
        if row or not allow_prefix or len(str(appointment_id or "")) < 4:
            return row
        return await self._fetchone("SELECT * FROM appointments WHERE id LIKE %s LIMIT 1", (f"{appointment_id}%",))

    async def list_appointments_by_phone(
        self,
        phone: str,
        date: str = "",
        time: str = "",
        limit: int = 0,
    ) -> list[dict]:
        sql = "SELECT * FROM appointments WHERE phone=%s"
        params: list[Any] = [phone]
        if date:
            sql += " AND `date`=%s"
            params.append(date)
        if time:
            sql += " AND `time`=%s"
            params.append(time[:5])
        sql += " ORDER BY `date` DESC"
        if limit:
            sql += " LIMIT %s"
            params.append(max(1, int(limit)))
        return await self._fetchall(sql, tuple(params))

    async def reschedule_appointment_record(self, old_appointment_id: str, new_appointment: dict) -> dict:
        old_id = str(old_appointment_id or "").strip()
        payload = _mysql_appointment_payload(new_appointment)
        new_id = str(payload.get("id") or "").strip()
        if not old_id or not new_id:
            raise ValueError("old and new appointment ids are required")
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                try:
                    await conn.begin()
                    await cur.execute("SELECT id FROM appointments WHERE id=%s FOR UPDATE", (old_id,))
                    if not await cur.fetchone():
                        raise ValueError("Appointment not found")
                    await cur.execute("SELECT id FROM appointments WHERE id=%s FOR UPDATE", (new_id,))
                    if await cur.fetchone():
                        raise ValueError("Replacement appointment already exists")
                    columns = list(payload.keys())
                    await cur.execute(
                        f"INSERT INTO appointments ({','.join(f'`{c}`' for c in columns)}) VALUES ({','.join(['%s'] * len(columns))})",
                        tuple(payload[column] for column in columns),
                    )
                    await cur.execute(
                        "UPDATE appointments SET status=%s,rescheduled_at=%s,reminder_processed=%s WHERE id=%s",
                        ("rescheduled", utc_now_mysql(), True, old_id),
                    )
                    await conn.commit()
                    return payload
                except BaseException:
                    await _rollback_quietly(conn)
                    raise

    async def get_appointments_for_slot(self, date: str, time: str, staff_id: str = "") -> list[dict]:
        params: list[Any] = [date, time[:5]]
        sql = "SELECT * FROM appointments WHERE `date`=%s AND `time`=%s AND status='booked'"
        if staff_id:
            sql += " AND staff_id=%s"
            params.append(staff_id)
        return await self._fetchall(sql, tuple(params))

    async def get_booked_appointments_for_staff(self, staff_id: str) -> list[dict]:
        return await self._fetchall(
            "SELECT * FROM appointments WHERE staff_id=%s AND status='booked'",
            (staff_id,),
        )

    async def get_active_staff(self) -> list[dict]:
        return await self.list_appointment_staff(include_inactive=False)

    async def list_appointment_staff(self, include_inactive: bool = False) -> list[dict]:
        sql = "SELECT * FROM appointment_staff"
        if not include_inactive:
            sql += " WHERE active=%s"
            return await self._fetchall(sql + " ORDER BY round_robin_order ASC, created_at ASC", (True,))
        return await self._fetchall(sql + " ORDER BY round_robin_order ASC, created_at ASC")

    async def get_appointment_staff(self, staff_id: str) -> Optional[dict]:
        return await self._fetchone("SELECT * FROM appointment_staff WHERE id=%s LIMIT 1", (staff_id,))

    async def upsert_appointment_staff(self, row: dict, staff_id: Optional[str] = None) -> dict:
        payload = _mysql_payload(row, {"working_days"})
        payload["id"] = staff_id or payload.get("id") or new_uuid()
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                try:
                    await conn.begin()
                    await cur.execute("SELECT id FROM appointment_staff WHERE id=%s FOR UPDATE", (payload["id"],))
                    exists = bool(await cur.fetchone())
                    if staff_id and not exists:
                        raise ValueError("Staff member not found")
                    if exists:
                        update_payload = {key: value for key, value in payload.items() if key not in {"id", "created_at"}}
                        assignments = ",".join(f"`{key}`=%s" for key in update_payload)
                        await cur.execute(
                            f"UPDATE appointment_staff SET {assignments} WHERE id=%s",
                            tuple(update_payload.values()) + (payload["id"],),
                        )
                    else:
                        columns = list(payload.keys())
                        await cur.execute(
                            f"INSERT INTO appointment_staff ({','.join(f'`{c}`' for c in columns)}) VALUES ({','.join(['%s'] * len(columns))})",
                            tuple(payload[column] for column in columns),
                        )
                    await cur.execute("SELECT * FROM appointment_staff WHERE id=%s LIMIT 1", (payload["id"],))
                    saved_row = await cur.fetchone()
                    if not saved_row:
                        raise RuntimeError("Staff save could not be verified")
                    columns = [item[0] for item in cur.description]
                    await conn.commit()
                    return dict(zip(columns, saved_row))
                except BaseException:
                    await _rollback_quietly(conn)
                    raise

    async def set_appointment_staff_active(self, staff_id: str, active: bool) -> bool:
        count = await self._execute(
            "UPDATE appointment_staff SET active=%s,updated_at=%s WHERE id=%s",
            (bool(active), utc_now_mysql(), staff_id),
        )
        return count > 0

    async def delete_appointment_staff(self, staff_id: str) -> bool:
        return await self._execute("DELETE FROM appointment_staff WHERE id=%s", (staff_id,)) > 0

    async def update_appointment_staff_calendar(self, staff_id: str, updates: dict) -> bool:
        allowed = {"calendar_sync_error", "last_calendar_sync_at"}
        payload = {
            key: _dt(value) if key == "last_calendar_sync_at" else value
            for key, value in (updates or {}).items()
            if key in allowed
        }
        if not payload:
            return False
        assignments = ",".join(f"`{key}`=%s" for key in payload)
        count = await self._execute(
            f"UPDATE appointment_staff SET {assignments} WHERE id=%s",
            tuple(payload.values()) + (staff_id,),
        )
        return count > 0

    async def get_staff_mobile_credential(self, identifier: str) -> Optional[dict]:
        value = str(identifier or "").strip()
        if not value:
            return None
        return await self._fetchone(
            """
            SELECT c.*,s.name,s.email,s.whatsapp_number,s.role,s.timezone,s.active AS staff_active
            FROM staff_mobile_credentials c
            JOIN appointment_staff s ON s.id=c.staff_id
            WHERE LOWER(c.username)=LOWER(%s) OR s.id=%s OR LOWER(s.email)=LOWER(%s) OR s.whatsapp_number=%s
            LIMIT 1
            """,
            (value, value, value, value),
        )

    async def upsert_staff_mobile_credential(
        self, staff_id: str, username: str, pin_hash: str, active: bool = True,
    ) -> dict:
        clean_username = str(username or "").strip().lower()
        if not clean_username:
            raise ValueError("Staff mobile username is required")
        now = utc_now_mysql()
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                try:
                    await conn.begin()
                    await cur.execute(
                        "SELECT staff_id FROM staff_mobile_credentials WHERE username=%s FOR UPDATE",
                        (clean_username,),
                    )
                    owner = await cur.fetchone()
                    if owner and str(owner[0]) != str(staff_id):
                        raise ValueError("That mobile username is already assigned to another staff member")
                    await cur.execute(
                        """
                        INSERT INTO staff_mobile_credentials
                          (staff_id,username,pin_hash,active,failed_attempts,locked_until,pin_changed_at,created_at,updated_at)
                        VALUES (%s,%s,%s,%s,0,NULL,%s,%s,%s)
                        ON DUPLICATE KEY UPDATE
                          username=VALUES(username),pin_hash=VALUES(pin_hash),active=VALUES(active),
                          failed_attempts=0,locked_until=NULL,pin_changed_at=VALUES(pin_changed_at),updated_at=VALUES(updated_at)
                        """,
                        (staff_id, clean_username, pin_hash, bool(active), now, now, now),
                    )
                    await cur.execute(
                        "UPDATE staff_mobile_sessions SET revoked_at=%s,last_seen_at=%s "
                        "WHERE staff_id=%s AND revoked_at IS NULL",
                        (now, now, staff_id),
                    )
                    await conn.commit()
                except BaseException:
                    await _rollback_quietly(conn)
                    raise
        return await self.get_staff_mobile_credential(staff_id) or {}

    async def disable_staff_mobile_credential(self, staff_id: str) -> bool:
        now = utc_now_mysql()
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                try:
                    await conn.begin()
                    await cur.execute(
                        "UPDATE staff_mobile_credentials SET active=FALSE,updated_at=%s WHERE staff_id=%s",
                        (now, staff_id),
                    )
                    changed = cur.rowcount > 0
                    await cur.execute(
                        "UPDATE staff_mobile_sessions SET revoked_at=%s,last_seen_at=%s "
                        "WHERE staff_id=%s AND revoked_at IS NULL",
                        (now, now, staff_id),
                    )
                    await conn.commit()
                    return changed
                except BaseException:
                    await _rollback_quietly(conn)
                    raise

    async def update_staff_mobile_login_state(
        self, staff_id: str, failed_attempts: int, locked_until: datetime | str | None = None,
    ) -> bool:
        return await self._execute(
            "UPDATE staff_mobile_credentials SET failed_attempts=%s,locked_until=%s,updated_at=%s WHERE staff_id=%s",
            (max(0, int(failed_attempts)), _dt(locked_until), utc_now_mysql(), staff_id),
        ) > 0

    async def create_staff_mobile_session(self, row: dict) -> dict:
        payload = _mysql_payload(row)
        payload.setdefault("id", new_uuid())
        payload.setdefault("created_at", utc_now_mysql())
        payload.setdefault("last_seen_at", utc_now_mysql())
        columns = list(payload)
        await self._execute(
            f"INSERT INTO staff_mobile_sessions ({','.join(f'`{column}`' for column in columns)}) "
            f"VALUES ({','.join(['%s'] * len(columns))})",
            tuple(payload[column] for column in columns),
        )
        return await self.get_staff_mobile_session(payload["id"]) or payload

    async def get_staff_mobile_session(self, session_id: str) -> Optional[dict]:
        return await self._fetchone(
            """
            SELECT ms.*,s.name,s.email,s.whatsapp_number,s.role,s.timezone,s.active AS staff_active,
                   c.active AS credential_active
            FROM staff_mobile_sessions ms
            JOIN appointment_staff s ON s.id=ms.staff_id
            JOIN staff_mobile_credentials c ON c.staff_id=ms.staff_id
            WHERE ms.id=%s LIMIT 1
            """,
            (session_id,),
        )

    async def rotate_staff_mobile_session(
        self, session_id: str, refresh_token_hash: str, expires_at: datetime | str,
    ) -> bool:
        return await self._execute(
            """
            UPDATE staff_mobile_sessions
            SET refresh_token_hash=%s,expires_at=%s,last_seen_at=%s
            WHERE id=%s AND revoked_at IS NULL
            """,
            (refresh_token_hash, _dt(expires_at), utc_now_mysql(), session_id),
        ) > 0

    async def revoke_staff_mobile_session(self, session_id: str) -> bool:
        return await self._execute(
            "UPDATE staff_mobile_sessions SET revoked_at=%s,last_seen_at=%s WHERE id=%s AND revoked_at IS NULL",
            (utc_now_mysql(), utc_now_mysql(), session_id),
        ) > 0

    async def list_staff_mobile_appointments(
        self, staff_id: str, date_from: str = "", date_to: str = "", statuses: Optional[list[str]] = None,
    ) -> list[dict]:
        clauses = ["a.staff_id=%s"]
        params: list[Any] = [staff_id]
        if date_from:
            clauses.append("a.`date`>=%s")
            params.append(date_from)
        if date_to:
            clauses.append("a.`date`<=%s")
            params.append(date_to)
        clean_statuses = [str(value).strip() for value in (statuses or []) if str(value).strip()]
        if clean_statuses:
            clauses.append(f"a.status IN ({','.join(['%s'] * len(clean_statuses))})")
            params.extend(clean_statuses)
        return await self._fetchall(
            """
            SELECT a.*,
              (SELECT je.event_type FROM appointment_job_events je
               WHERE je.appointment_id=a.id
               ORDER BY je.created_at DESC,
                 CASE je.event_type
                   WHEN 'completed' THEN 4 WHEN 'job_started' THEN 3
                   WHEN 'arrived' THEN 2 WHEN 'en_route' THEN 1 ELSE 0
                 END DESC,
                 je.id DESC
               LIMIT 1) AS mobile_job_state,
              (SELECT te.captured_at FROM appointment_tracking_events te
               WHERE te.appointment_id=a.id ORDER BY te.captured_at DESC LIMIT 1) AS last_location_at
            FROM appointments a
            WHERE """ + " AND ".join(clauses) + " ORDER BY a.`date` ASC,a.`time` ASC",
            tuple(params),
        )

    async def get_staff_mobile_appointment(self, staff_id: str, appointment_id: str) -> Optional[dict]:
        return await self._fetchone(
            """
            SELECT a.*,
              (SELECT je.event_type FROM appointment_job_events je
               WHERE je.appointment_id=a.id
               ORDER BY je.created_at DESC,
                 CASE je.event_type
                   WHEN 'completed' THEN 4 WHEN 'job_started' THEN 3
                   WHEN 'arrived' THEN 2 WHEN 'en_route' THEN 1 ELSE 0
                 END DESC,
                 je.id DESC
               LIMIT 1) AS mobile_job_state
            FROM appointments a WHERE a.id=%s AND a.staff_id=%s LIMIT 1
            """,
            (appointment_id, staff_id),
        )

    @staticmethod
    async def _mobile_insert_event(cur: Any, event: dict) -> None:
        payload = _mysql_payload(event, {"event_data"})
        columns = list(payload)
        await cur.execute(
            f"INSERT INTO appointment_job_events ({','.join(f'`{column}`' for column in columns)}) "
            f"VALUES ({','.join(['%s'] * len(columns))}) ON DUPLICATE KEY UPDATE id=id",
            tuple(payload[column] for column in columns),
        )

    @staticmethod
    async def _mobile_insert_evidence(cur: Any, evidence: dict) -> None:
        payload = _mysql_payload(evidence, {"metadata_json"})
        columns = list(payload)
        await cur.execute(
            f"INSERT INTO appointment_evidence ({','.join(f'`{column}`' for column in columns)}) "
            f"VALUES ({','.join(['%s'] * len(columns))}) ON DUPLICATE KEY UPDATE id=id",
            tuple(payload[column] for column in columns),
        )

    @staticmethod
    async def _mobile_locked_appointment(cur: Any, appointment_id: str, staff_id: str) -> dict:
        await cur.execute(
            "SELECT * FROM appointments WHERE id=%s AND staff_id=%s FOR UPDATE",
            (appointment_id, staff_id),
        )
        row = await cur.fetchone()
        if not row:
            raise ValueError("Appointment is not assigned to this staff member")
        columns = [column[0] for column in cur.description]
        return dict(zip(columns, row))

    async def begin_staff_mobile_job(self, appointment_id: str, staff_id: str, event: dict) -> dict:
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                try:
                    await conn.begin()
                    appointment = await self._mobile_locked_appointment(cur, appointment_id, staff_id)
                    await cur.execute(
                        "SELECT event_type FROM appointment_job_events "
                        "WHERE appointment_id=%s AND staff_id=%s AND idempotency_key=%s LIMIT 1",
                        (appointment_id, staff_id, event["idempotency_key"]),
                    )
                    existing = await cur.fetchone()
                    if existing and existing[0] == "en_route":
                        await conn.commit()
                        return {"appointment_id": appointment_id, "job_state": "en_route", "already_applied": True}
                    if str(appointment.get("status") or "").lower() not in {"booked", "confirmed"}:
                        raise ValueError("Only a booked appointment can start travel")
                    await self._mobile_insert_event(cur, event)
                    await cur.execute(
                        "UPDATE appointments SET staff_status_updated_at=%s,staff_status_updated_by=%s WHERE id=%s",
                        (utc_now_mysql(), staff_id, appointment_id),
                    )
                    await conn.commit()
                    return {"appointment_id": appointment_id, "job_state": "en_route"}
                except BaseException:
                    await _rollback_quietly(conn)
                    raise

    async def record_staff_mobile_location(self, row: dict) -> dict:
        payload = _mysql_payload(row)
        payload.setdefault("id", new_uuid())
        payload.setdefault("created_at", utc_now_mysql())
        columns = list(payload)
        await self._execute(
            f"INSERT INTO appointment_tracking_events ({','.join(f'`{column}`' for column in columns)}) "
            f"VALUES ({','.join(['%s'] * len(columns))}) ON DUPLICATE KEY UPDATE id=id",
            tuple(payload[column] for column in columns),
        )
        return payload

    async def mark_staff_mobile_arrived(
        self, appointment_id: str, staff_id: str, evidence: dict, event: dict,
    ) -> dict:
        if (
            evidence.get("appointment_id") != appointment_id
            or evidence.get("staff_id") != staff_id
            or evidence.get("evidence_type") != "arrival_selfie"
            or event.get("appointment_id") != appointment_id
            or event.get("staff_id") != staff_id
            or event.get("event_type") != "arrived"
        ):
            raise ValueError("Arrival evidence or event identity does not match the assigned appointment")
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                try:
                    await conn.begin()
                    appointment = await self._mobile_locked_appointment(cur, appointment_id, staff_id)
                    await cur.execute(
                        "SELECT event_type FROM appointment_job_events "
                        "WHERE appointment_id=%s AND staff_id=%s AND idempotency_key=%s LIMIT 1",
                        (appointment_id, staff_id, event["idempotency_key"]),
                    )
                    existing = await cur.fetchone()
                    if existing and existing[0] == "arrived":
                        await conn.commit()
                        return {
                            "appointment_id": appointment_id,
                            "job_state": "arrived",
                            "start_code_status": "pending",
                            "already_applied": True,
                        }
                    if str(appointment.get("status") or "").lower() not in {"booked", "confirmed"}:
                        raise ValueError("Appointment is not ready for arrival verification")
                    await cur.execute(
                        "SELECT event_type FROM appointment_job_events WHERE appointment_id=%s "
                        "ORDER BY created_at DESC, CASE event_type "
                        "WHEN 'completed' THEN 4 WHEN 'job_started' THEN 3 "
                        "WHEN 'arrived' THEN 2 WHEN 'en_route' THEN 1 ELSE 0 END DESC, id DESC "
                        "LIMIT 1 FOR UPDATE",
                        (appointment_id,),
                    )
                    latest = await cur.fetchone()
                    if not latest or latest[0] not in {"en_route", "arrived"}:
                        raise ValueError("Start travel before confirming arrival")
                    await cur.execute(
                        "SELECT status,expires_at FROM appointment_start_otps "
                        "WHERE appointment_id=%s AND staff_id=%s FOR UPDATE",
                        (appointment_id, staff_id),
                    )
                    start_code = await cur.fetchone()
                    if not start_code:
                        raise ValueError("Service start code was not issued when the appointment was booked")
                    expiry = start_code[1]
                    expiry_dt = expiry.replace(tzinfo=None) if isinstance(expiry, datetime) else datetime.fromisoformat(str(expiry)).replace(tzinfo=None)
                    if str(start_code[0]) != "pending" or expiry_dt < utc_now().replace(tzinfo=None):
                        raise ValueError("Service start code is no longer valid")
                    await self._mobile_insert_evidence(cur, evidence)
                    now = utc_now_mysql()
                    await self._mobile_insert_event(cur, event)
                    await cur.execute(
                        "UPDATE appointments SET staff_status_updated_at=%s,staff_status_updated_by=%s WHERE id=%s",
                        (now, staff_id, appointment_id),
                    )
                    await conn.commit()
                    return {"appointment_id": appointment_id, "job_state": "arrived", "start_code_status": "pending"}
                except BaseException:
                    await _rollback_quietly(conn)
                    raise

    async def verify_staff_mobile_start_code(
        self, appointment_id: str, staff_id: str, session_id: str, otp_hash: str, event: dict,
    ) -> dict:
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                try:
                    await conn.begin()
                    appointment = await self._mobile_locked_appointment(cur, appointment_id, staff_id)
                    if str(appointment.get("status") or "").lower() not in {"booked", "confirmed"}:
                        raise ValueError("Appointment is not awaiting start verification")
                    await cur.execute(
                        "SELECT otp_hash,status,attempt_count,max_attempts,expires_at FROM appointment_start_otps "
                        "WHERE appointment_id=%s AND staff_id=%s FOR UPDATE",
                        (appointment_id, staff_id),
                    )
                    otp_row = await cur.fetchone()
                    if not otp_row:
                        raise ValueError("Service start code has not been issued")
                    stored_hash, status, attempt_count, max_attempts, expires_at = otp_row
                    now_dt = utc_now().replace(tzinfo=None)
                    expiry_dt = expires_at.replace(tzinfo=None) if isinstance(expires_at, datetime) else datetime.fromisoformat(str(expires_at)).replace(tzinfo=None)
                    if status == "verified":
                        await conn.commit()
                        return {"appointment_id": appointment_id, "job_state": "in_progress", "already_verified": True}
                    if status != "pending" or expiry_dt < now_dt:
                        await cur.execute(
                            "UPDATE appointment_start_otps SET status='expired',updated_at=%s WHERE appointment_id=%s",
                            (utc_now_mysql(), appointment_id),
                        )
                        await conn.commit()
                        raise ValueError("Service start code has expired")
                    next_attempt = int(attempt_count or 0) + 1
                    if not hmac.compare_digest(str(stored_hash), str(otp_hash)):
                        next_status = "locked" if next_attempt >= int(max_attempts or 5) else "pending"
                        await cur.execute(
                            "UPDATE appointment_start_otps SET attempt_count=%s,status=%s,updated_at=%s WHERE appointment_id=%s",
                            (next_attempt, next_status, utc_now_mysql(), appointment_id),
                        )
                        await conn.commit()
                        return {"appointment_id": appointment_id, "verified": False, "attempts_remaining": max(0, int(max_attempts or 5) - next_attempt)}
                    now = utc_now_mysql()
                    await cur.execute(
                        "UPDATE appointment_start_otps SET status='verified',attempt_count=%s,verified_at=%s,verified_by_session_id=%s,updated_at=%s WHERE appointment_id=%s",
                        (next_attempt, now, session_id, now, appointment_id),
                    )
                    await cur.execute(
                        "UPDATE appointments SET status='in_progress',service_started_at=%s,staff_status_updated_at=%s,staff_status_updated_by=%s WHERE id=%s",
                        (now, now, staff_id, appointment_id),
                    )
                    await self._mobile_insert_event(cur, event)
                    await conn.commit()
                    return {"appointment_id": appointment_id, "job_state": "in_progress", "verified": True}
                except ValueError:
                    await _rollback_quietly(conn)
                    raise

                except BaseException:
                    await _rollback_quietly(conn)
                    raise

    async def verify_staff_mobile_otp(
        self, appointment_id: str, staff_id: str, session_id: str, otp_hash: str, event: dict,
    ) -> dict:
        """Compatibility alias for APKs installed before service-start-code naming."""
        return await self.verify_staff_mobile_start_code(appointment_id, staff_id, session_id, otp_hash, event)

    async def complete_staff_mobile_job(
        self, appointment_id: str, staff_id: str, evidence: dict,
        notification: dict, event: dict,
    ) -> dict:
        if (
            evidence.get("appointment_id") != appointment_id
            or evidence.get("staff_id") != staff_id
            or evidence.get("evidence_type") != "payment_proof"
            or event.get("appointment_id") != appointment_id
            or event.get("staff_id") != staff_id
            or event.get("event_type") != "completed"
        ):
            raise ValueError("Completion evidence or event identity does not match the assigned appointment")
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                try:
                    await conn.begin()
                    appointment = await self._mobile_locked_appointment(cur, appointment_id, staff_id)
                    await cur.execute(
                        "SELECT event_type FROM appointment_job_events "
                        "WHERE appointment_id=%s AND staff_id=%s AND idempotency_key=%s LIMIT 1",
                        (appointment_id, staff_id, event["idempotency_key"]),
                    )
                    existing = await cur.fetchone()
                    if existing and existing[0] == "completed":
                        await conn.commit()
                        return {
                            "appointment_id": appointment_id,
                            "job_state": "completed",
                            "crm_closed": True,
                            "already_applied": True,
                        }
                    if str(appointment.get("status") or "").lower() != "in_progress":
                        raise ValueError("Only an in-progress appointment can be completed")
                    await cur.execute(
                        "SELECT status FROM appointment_start_otps "
                        "WHERE appointment_id=%s AND staff_id=%s FOR UPDATE",
                        (appointment_id, staff_id),
                    )
                    verified_otp = await cur.fetchone()
                    if not verified_otp or verified_otp[0] != "verified":
                        raise ValueError("Customer start OTP has not been verified")
                    await cur.execute(
                        "SELECT id FROM appointment_evidence "
                        "WHERE appointment_id=%s AND staff_id=%s AND evidence_type='arrival_selfie' LIMIT 1",
                        (appointment_id, staff_id),
                    )
                    if not await cur.fetchone():
                        raise ValueError("Arrival selfie evidence is required before completion")
                    await self._mobile_insert_evidence(cur, evidence)
                    now = utc_now_mysql()
                    await cur.execute(
                        """
                        UPDATE appointments
                        SET status='completed',completed_at=%s,staff_status_updated_at=%s,
                            staff_status_updated_by=%s,reminder_processed=TRUE
                        WHERE id=%s
                        """,
                        (now, now, staff_id, appointment_id),
                    )
                    phone = str(appointment.get("phone") or "")
                    if phone:
                        await cur.execute(
                            """
                            UPDATE crm_contacts
                            SET crm_status='Converted',journey_stage='service_completed',
                                next_best_action='closed',updated_at=%s
                            WHERE phone_number=%s
                            """,
                            (now, phone),
                        )
                    await self._insert_outbox_intent(
                        cur,
                        "appointment_customer_completion",
                        notification,
                        appointment_id,
                        "customer",
                        str(event.get("idempotency_key") or "v1"),
                    )
                    await self._mobile_insert_event(cur, event)
                    await conn.commit()
                    return {"appointment_id": appointment_id, "job_state": "completed", "crm_closed": True}
                except BaseException:
                    await _rollback_quietly(conn)
                    raise

    async def get_staff_mobile_job_detail(self, appointment_id: str) -> dict:
        latest_location = await self._fetchone(
            "SELECT * FROM appointment_tracking_events WHERE appointment_id=%s ORDER BY captured_at DESC LIMIT 1",
            (appointment_id,),
        )
        events = await self._fetchall(
            "SELECT * FROM appointment_job_events WHERE appointment_id=%s "
            "ORDER BY created_at ASC, CASE event_type "
            "WHEN 'en_route' THEN 1 WHEN 'arrived' THEN 2 "
            "WHEN 'job_started' THEN 3 WHEN 'completed' THEN 4 ELSE 0 END ASC, id ASC",
            (appointment_id,),
        )
        evidence = await self._fetchall(
            "SELECT * FROM appointment_evidence WHERE appointment_id=%s ORDER BY created_at ASC",
            (appointment_id,),
        )
        return {"latest_location": latest_location, "events": events, "evidence": evidence}

    async def get_staff_attendance(self, staff_id: str, work_date: str) -> Optional[dict]:
        return await self._fetchone(
            "SELECT * FROM staff_attendance WHERE staff_id=%s AND work_date=%s LIMIT 1",
            (staff_id, work_date),
        )

    async def check_in_staff_attendance(self, row: dict) -> dict:
        payload = _mysql_payload(row)
        columns = list(payload)
        await self._execute(
            f"INSERT INTO staff_attendance ({','.join(f'`{column}`' for column in columns)}) "
            f"VALUES ({','.join(['%s'] * len(columns))}) ON DUPLICATE KEY UPDATE id=id",
            tuple(payload[column] for column in columns),
        )
        saved = await self.get_staff_attendance(str(row["staff_id"]), str(row["work_date"]))
        if not saved:
            raise RuntimeError("Staff check-in could not be verified")
        saved["already_checked_in"] = str(saved.get("id")) != str(row.get("id"))
        return saved

    async def check_out_staff_attendance(self, staff_id: str, work_date: str, row: dict) -> dict:
        now = _dt(row.get("check_out_at")) or utc_now_mysql()
        count = await self._execute(
            """
            UPDATE staff_attendance
            SET check_out_at=%s,check_out_latitude=%s,check_out_longitude=%s,
                check_out_accuracy_meters=%s,check_out_session_id=%s,updated_at=%s
            WHERE staff_id=%s AND work_date=%s AND check_out_at IS NULL
            """,
            (
                now,
                row.get("check_out_latitude"),
                row.get("check_out_longitude"),
                row.get("check_out_accuracy_meters"),
                row.get("check_out_session_id"),
                now,
                staff_id,
                work_date,
            ),
        )
        saved = await self.get_staff_attendance(staff_id, work_date)
        if not saved:
            raise ValueError("Check in before checking out")
        saved["already_checked_out"] = count == 0 and bool(saved.get("check_out_at"))
        return saved

    async def list_staff_attendance(
        self, date_from: str, date_to: str, staff_id: str = "",
    ) -> list[dict]:
        clauses = ["a.work_date>=%s", "a.work_date<=%s"]
        params: list[Any] = [date_from, date_to]
        if staff_id:
            clauses.append("a.staff_id=%s")
            params.append(staff_id)
        return await self._fetchall(
            """
            SELECT a.*,s.name AS staff_name,s.role AS staff_role,s.timezone AS staff_timezone
            FROM staff_attendance a JOIN appointment_staff s ON s.id=a.staff_id
            WHERE """ + " AND ".join(clauses) + " ORDER BY a.work_date DESC,a.check_in_at DESC",
            tuple(params),
        )

    async def create_staff_leave_request(self, row: dict) -> dict:
        payload = _mysql_payload(row)
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                try:
                    await conn.begin()
                    await cur.execute(
                        """
                        SELECT id FROM staff_leave_requests
                        WHERE staff_id=%s AND status IN ('pending','approved')
                          AND start_date<=%s AND end_date>=%s
                        LIMIT 1 FOR UPDATE
                        """,
                        (payload["staff_id"], payload["end_date"], payload["start_date"]),
                    )
                    if await cur.fetchone():
                        raise ValueError("A pending or approved leave request already overlaps these dates")
                    columns = list(payload)
                    await cur.execute(
                        f"INSERT INTO staff_leave_requests ({','.join(f'`{column}`' for column in columns)}) "
                        f"VALUES ({','.join(['%s'] * len(columns))})",
                        tuple(payload[column] for column in columns),
                    )
                    await conn.commit()
                except BaseException:
                    await _rollback_quietly(conn)
                    raise
        saved = await self._fetchone("SELECT * FROM staff_leave_requests WHERE id=%s", (payload["id"],))
        if not saved:
            raise RuntimeError("Leave request could not be verified")
        return saved

    async def list_staff_leave_requests(self, staff_id: str = "", status: str = "") -> list[dict]:
        clauses: list[str] = []
        params: list[Any] = []
        if staff_id:
            clauses.append("r.staff_id=%s")
            params.append(staff_id)
        if status:
            clauses.append("r.status=%s")
            params.append(status)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        return await self._fetchall(
            """
            SELECT r.*,s.name AS staff_name,s.role AS staff_role,s.timezone AS staff_timezone
            FROM staff_leave_requests r JOIN appointment_staff s ON s.id=r.staff_id
            """ + where + " ORDER BY r.created_at DESC",
            tuple(params),
        )

    async def decide_staff_leave_request(
        self, request_id: str, status: str, admin_note: str, reviewed_by: str,
    ) -> dict:
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                try:
                    await conn.begin()
                    await cur.execute("SELECT status FROM staff_leave_requests WHERE id=%s FOR UPDATE", (request_id,))
                    existing = await cur.fetchone()
                    if not existing:
                        raise ValueError("Leave request not found")
                    if str(existing[0]) != "pending":
                        raise ValueError("Only a pending leave request can be reviewed")
                    now = utc_now_mysql()
                    await cur.execute(
                        "UPDATE staff_leave_requests SET status=%s,admin_note=%s,reviewed_at=%s,"
                        "reviewed_by=%s,updated_at=%s WHERE id=%s",
                        (status, admin_note, now, reviewed_by, now, request_id),
                    )
                    await conn.commit()
                except BaseException:
                    await _rollback_quietly(conn)
                    raise
        return await self._fetchone("SELECT * FROM staff_leave_requests WHERE id=%s", (request_id,)) or {}

    async def staff_mobile_schema_health(self) -> dict:
        tables = {
            "staff_mobile_credentials": ["staff_id", "username", "pin_hash", "active", "locked_until"],
            "staff_mobile_sessions": ["id", "staff_id", "refresh_token_hash", "expires_at", "revoked_at"],
            "appointment_job_events": ["id", "appointment_id", "staff_id", "event_type", "idempotency_key"],
            "appointment_tracking_events": ["id", "appointment_id", "staff_id", "session_id", "captured_at"],
            "appointment_evidence": ["id", "appointment_id", "staff_id", "evidence_type", "storage_key", "sha256"],
            "appointment_start_otps": ["appointment_id", "staff_id", "otp_hash", "status", "expires_at"],
            "staff_attendance": [
                "id", "staff_id", "work_date", "check_in_at", "check_out_at",
                "check_in_session_id", "check_out_session_id",
            ],
            "staff_leave_requests": [
                "id", "staff_id", "leave_type", "start_date", "end_date", "status", "reviewed_at",
            ],
        }
        constraints = {
            "staff_mobile_credentials": ["fk_staff_mobile_credentials_staff", "uq_staff_mobile_username"],
            "staff_mobile_sessions": ["fk_staff_mobile_sessions_staff", "uq_staff_mobile_session_refresh"],
            "appointment_job_events": [
                "fk_appointment_job_events_appointment",
                "fk_appointment_job_events_staff",
                "uq_appointment_job_events_idempotency",
            ],
            "appointment_tracking_events": [
                "fk_appointment_tracking_appointment",
                "fk_appointment_tracking_staff",
                "fk_appointment_tracking_session",
                "uq_appointment_tracking_idempotency",
            ],
            "appointment_evidence": [
                "fk_appointment_evidence_appointment",
                "fk_appointment_evidence_staff",
                "fk_appointment_evidence_session",
                "uq_appointment_evidence_storage",
                "uq_appointment_evidence_idempotency",
            ],
            "appointment_start_otps": [
                "fk_appointment_start_otps_appointment",
                "fk_appointment_start_otps_staff",
                "fk_appointment_start_otps_session",
            ],
            "staff_attendance": [
                "fk_staff_attendance_staff",
                "fk_staff_attendance_check_in_session",
                "fk_staff_attendance_check_out_session",
                "chk_staff_attendance_checkout_order",
                "uq_staff_attendance_day",
            ],
            "staff_leave_requests": [
                "fk_staff_leave_requests_staff",
                "chk_staff_leave_date_order",
                "chk_staff_leave_type",
                "chk_staff_leave_status",
            ],
        }
        indexes = {
            "staff_mobile_credentials": ["idx_staff_mobile_credentials_active"],
            "staff_mobile_sessions": ["idx_staff_mobile_sessions_staff_active", "idx_staff_mobile_sessions_device"],
            "appointment_job_events": ["idx_appointment_job_events_timeline", "idx_appointment_job_events_staff"],
            "appointment_tracking_events": ["idx_appointment_tracking_latest", "idx_appointment_tracking_staff"],
            "appointment_evidence": ["idx_appointment_evidence_timeline"],
            "appointment_start_otps": ["idx_appointment_start_otps_due", "idx_appointment_start_otps_staff"],
            "staff_attendance": ["idx_staff_attendance_daily", "idx_staff_attendance_open"],
            "staff_leave_requests": ["idx_staff_leave_staff_dates", "idx_staff_leave_review_queue"],
        }
        result = {
            "backend": "mysql",
            "required_tables_present": [],
            "required_tables_missing": [],
            "required_columns_missing": {},
            "critical_constraints_missing": {},
            "critical_indexes_missing": {},
        }
        current_db = os.getenv("MYSQL_DATABASE", "")
        for table, columns in tables.items():
            table_row = await self._fetchone(
                "SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s",
                (current_db, table),
            )
            if not table_row:
                result["required_tables_missing"].append(table)
                result["required_columns_missing"][table] = columns
                continue
            result["required_tables_present"].append(table)
            existing_cols = await self._fetchall(
                "SELECT COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s",
                (current_db, table),
            )
            column_names = {row["COLUMN_NAME"] for row in existing_cols}
            missing_columns = [column for column in columns if column not in column_names]
            if missing_columns:
                result["required_columns_missing"][table] = missing_columns
            existing_constraints = await self._fetchall(
                "SELECT CONSTRAINT_NAME FROM information_schema.TABLE_CONSTRAINTS "
                "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s",
                (current_db, table),
            )
            constraint_names = {row["CONSTRAINT_NAME"] for row in existing_constraints}
            missing_constraints = [name for name in constraints[table] if name not in constraint_names]
            if missing_constraints:
                result["critical_constraints_missing"][table] = missing_constraints
            existing_indexes = await self._fetchall(
                "SELECT INDEX_NAME FROM information_schema.STATISTICS WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s",
                (current_db, table),
            )
            index_names = {row["INDEX_NAME"] for row in existing_indexes}
            missing_indexes = [name for name in indexes[table] if name not in index_names]
            if missing_indexes:
                result["critical_indexes_missing"][table] = missing_indexes
        result["ok"] = not any(
            result[key]
            for key in (
                "required_tables_missing",
                "required_columns_missing",
                "critical_constraints_missing",
                "critical_indexes_missing",
            )
        )
        return result

    @staticmethod
    def _dedupe_key(operation: str, appointment_id: str, recipient_type: str, event_identity: str = "v1") -> str:
        import hashlib

        raw = f"{operation or ''}:{appointment_id or ''}:{recipient_type or ''}:{event_identity or 'v1'}".lower()
        return hashlib.md5(raw.encode("utf-8")).hexdigest()

    async def _insert_outbox_intent(self, cur, operation: str, intent: dict, appointment_id: str, recipient_type: str, event_identity: str) -> str:
        if not (intent or {}).get("recipient_phone"):
            raise ValueError("notification recipient_phone is required")
        if not (intent or {}).get("notification_type"):
            raise ValueError("notification_type is required")
        if not (intent or {}).get("template_purpose"):
            raise ValueError("template_purpose is required")
        dedupe_key = self._dedupe_key(operation, appointment_id, recipient_type, event_identity)
        row_id = new_uuid()
        now = utc_now_mysql()
        payload = _json_text(intent.get("payload"), {})
        await cur.execute(
            """
            INSERT INTO notification_outbox
              (id,dedupe_key,status,recipient_type,recipient_phone,notification_type,template_purpose,
               appointment_id,conversation_id,contact_id,payload,attempt_count,max_attempts,next_attempt_at,created_at,updated_at)
            VALUES (%s,%s,'pending',%s,%s,%s,%s,%s,%s,%s,%s,0,5,%s,%s,%s)
            ON DUPLICATE KEY UPDATE id=id
            """,
            (
                row_id,
                dedupe_key,
                recipient_type,
                intent.get("recipient_phone") or "",
                intent.get("notification_type") or "",
                intent.get("template_purpose") or "",
                appointment_id,
                intent.get("conversation_id") or "",
                intent.get("contact_id") or "",
                payload,
                _dt(intent.get("next_attempt_at")) or now,
                now,
                now,
            ),
        )
        await cur.execute("SELECT id,status FROM notification_outbox WHERE dedupe_key=%s LIMIT 1", (dedupe_key,))
        found = await cur.fetchone()
        if not found:
            raise RuntimeError("notification intent was not persisted")
        return found[0]

    async def _insert_appointment_start_code(self, cur: Any, appointment: dict, start_code: Optional[dict]) -> None:
        if not start_code:
            return
        appointment_id = str((appointment or {}).get("id") or "").strip()
        staff_id = str((appointment or {}).get("staff_id") or "").strip()
        if not appointment_id or not staff_id or staff_id != str(start_code.get("staff_id") or "").strip():
            raise ValueError("Service start code must match the appointment and assigned staff")
        await cur.execute(
            """
            INSERT INTO appointment_start_otps
              (appointment_id,staff_id,otp_hash,status,attempt_count,max_attempts,issued_at,expires_at,
               verified_at,verified_by_session_id,updated_at)
            VALUES (%s,%s,%s,'pending',0,%s,%s,%s,NULL,NULL,%s)
            ON DUPLICATE KEY UPDATE appointment_id=appointment_id
            """,
            (
                appointment_id,
                staff_id,
                start_code["otp_hash"],
                int(start_code.get("max_attempts") or 5),
                _dt(start_code["issued_at"]),
                _dt(start_code["expires_at"]),
                utc_now_mysql(),
            ),
        )
        await cur.execute(
            "SELECT staff_id,otp_hash FROM appointment_start_otps WHERE appointment_id=%s FOR UPDATE",
            (appointment_id,),
        )
        stored = await cur.fetchone()
        if not stored or str(stored[0]) != staff_id or not hmac.compare_digest(str(stored[1]), str(start_code["otp_hash"])):
            raise ValueError("Conflicting service start code already exists for appointment")

    async def create_appointment_with_outbox(
        self, appointment: dict, customer_intent: dict, staff_intent: dict,
        event_identity: str = "v1", start_code: Optional[dict] = None,
    ) -> dict:
        appointment_id = str((appointment or {}).get("id") or "")
        if not appointment_id:
            raise ValueError("appointment id is required")
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                try:
                    await conn.begin()
                    await cur.execute("SELECT * FROM appointments WHERE id=%s FOR UPDATE", (appointment_id,))
                    existing = await cur.fetchone()
                    if not existing:
                        payload = _mysql_appointment_payload(appointment)
                        payload.setdefault("id", appointment_id)
                        payload.setdefault("created_at", utc_now_mysql())
                        columns = list(payload.keys())
                        await cur.execute(
                            f"INSERT INTO appointments ({','.join(f'`{c}`' for c in columns)}) VALUES ({','.join(['%s'] * len(columns))})",
                            tuple(payload[c] for c in columns),
                        )
                    await self._insert_appointment_start_code(cur, appointment, start_code)
                    customer_id = await self._insert_outbox_intent(cur, "appointment_create", customer_intent, appointment_id, "customer", event_identity)
                    staff_id = await self._insert_outbox_intent(cur, "appointment_create", staff_intent, appointment_id, "staff", event_identity)
                    await conn.commit()
                    return {"success": True, "appointment_id": appointment_id, "customer_outbox_id": customer_id, "staff_outbox_id": staff_id}
                except BaseException:
                    await _rollback_quietly(conn)
                    raise

    async def reschedule_appointment_with_outbox(
        self,
        old_appointment_id: str,
        new_appointment: dict,
        customer_intent: dict,
        staff_intent: dict,
        event_identity: str = "v1",
        start_code: Optional[dict] = None,
    ) -> dict:
        old_id = str(old_appointment_id or "").strip()
        new_id = str((new_appointment or {}).get("id") or "").strip()
        if not old_id or not new_id:
            raise ValueError("old and new appointment ids are required")
        expected = _appointment_core(new_appointment)
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                try:
                    await conn.begin()
                    await cur.execute("SELECT * FROM appointments WHERE id=%s FOR UPDATE", (old_id,))
                    old_row = await cur.fetchone()
                    if not old_row:
                        raise ValueError("original appointment does not exist")
                    old_cols = [d[0] for d in cur.description]
                    old = dict(zip(old_cols, old_row))
                    if old.get("status") == "rescheduled":
                        await cur.execute("SELECT * FROM appointments WHERE rescheduled_from=%s FOR UPDATE", (old_id,))
                        child_row = await cur.fetchone()
                        if not child_row:
                            raise ValueError("original appointment is rescheduled but replacement is missing")
                        child_cols = [d[0] for d in cur.description]
                        child = dict(zip(child_cols, child_row))
                        if str(child.get("id") or "") != new_id:
                            raise ValueError("original appointment already has different replacement")
                    if old.get("status") in {"cancelled", "completed", "no_show"}:
                        raise ValueError(f"cannot reschedule appointment from status {old.get('status')}")

                    await cur.execute("SELECT * FROM appointments WHERE id=%s FOR UPDATE", (new_id,))
                    new_row = await cur.fetchone()
                    if new_row:
                        new_cols = [d[0] for d in cur.description]
                        stored = dict(zip(new_cols, new_row))
                        if str(stored.get("rescheduled_from") or "") != old_id or _appointment_core(stored) != expected:
                            raise ValueError("conflicting rescheduled appointment already exists")
                    else:
                        await cur.execute("SELECT id FROM appointments WHERE rescheduled_from=%s AND id<>%s FOR UPDATE", (old_id, new_id))
                        if await cur.fetchone():
                            raise ValueError("original appointment already has different replacement")
                        await cur.execute(
                            """
                            INSERT INTO appointments
                              (id,name,phone,`date`,`time`,service,status,staff_id,staff_name,duration_minutes,
                               buffer_minutes,timezone,created_at,rescheduled_from)
                            VALUES (%s,%s,%s,%s,%s,%s,'booked',%s,%s,%s,%s,%s,%s,%s)
                            """,
                            (
                                new_id,
                                expected["name"],
                                expected["phone"],
                                expected["date"],
                                expected["time"],
                                expected["service"],
                                expected["staff_id"],
                                expected["staff_name"],
                                expected["duration_minutes"],
                                expected["buffer_minutes"],
                                expected["timezone"],
                                _dt((new_appointment or {}).get("created_at")) or utc_now_mysql(),
                                old_id,
                            ),
                        )

                    now = utc_now_mysql()
                    await cur.execute(
                        "UPDATE appointments SET status='rescheduled', rescheduled_at=COALESCE(rescheduled_at,%s) WHERE id=%s AND status<>'rescheduled'",
                        (now, old_id),
                    )
                    await self._insert_appointment_start_code(cur, new_appointment, start_code)
                    customer_id = await self._insert_outbox_intent(cur, "appointment_reschedule", customer_intent, new_id, "customer", event_identity)
                    staff_id = await self._insert_outbox_intent(cur, "appointment_reschedule", staff_intent, new_id, "staff", event_identity)
                    await conn.commit()
                    return {
                        "success": True,
                        "old_appointment_id": old_id,
                        "new_appointment_id": new_id,
                        "customer_outbox_id": customer_id,
                        "staff_outbox_id": staff_id,
                    }
                except BaseException:
                    await _rollback_quietly(conn)
                    raise

    async def cancel_appointment_with_outbox(
        self,
        appointment_id: str,
        customer_intent: dict,
        staff_intent: dict,
        event_identity: str = "v1",
    ) -> dict:
        current_id = str(appointment_id or "").strip()
        if not current_id:
            raise ValueError("appointment id is required")
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                try:
                    await conn.begin()
                    await cur.execute("SELECT status FROM appointments WHERE id=%s FOR UPDATE", (current_id,))
                    row = await cur.fetchone()
                    if not row:
                        raise ValueError("appointment does not exist")
                    if row[0] in {"completed", "no_show"}:
                        raise ValueError(f"cannot cancel appointment from status {row[0]}")
                    now = utc_now_mysql()
                    await cur.execute(
                        "UPDATE appointments SET status='cancelled', cancelled_at=COALESCE(cancelled_at,%s) WHERE id=%s AND status<>'cancelled'",
                        (now, current_id),
                    )
                    customer_id = await self._insert_outbox_intent(cur, "appointment_cancel", customer_intent, current_id, "customer", event_identity)
                    staff_id = await self._insert_outbox_intent(cur, "appointment_cancel", staff_intent, current_id, "staff", event_identity)
                    await conn.commit()
                    return {
                        "success": True,
                        "appointment_id": current_id,
                        "customer_outbox_id": customer_id,
                        "staff_outbox_id": staff_id,
                    }
                except BaseException:
                    await _rollback_quietly(conn)
                    raise

    async def create_followup_action(self, row: dict) -> dict:
        payload = _mysql_payload(row, {"payload", "result"})
        payload.setdefault("id", new_uuid())
        payload.setdefault("created_at", utc_now_mysql())
        payload.setdefault("updated_at", utc_now_mysql())
        columns = list(payload.keys())
        await self._execute(
            f"INSERT INTO followup_actions ({','.join(f'`{c}`' for c in columns)}) VALUES ({','.join(['%s'] * len(columns))})",
            tuple(payload[c] for c in columns),
        )
        return _followup_row(payload)

    async def list_followup_actions(
        self,
        phone: str = "",
        status: str = "",
        due_before: datetime | str | None = None,
        limit: int = 100,
        ascending: bool = False,
    ) -> list[dict]:
        conditions = []
        params = []
        if phone:
            conditions.append("phone_number=%s")
            params.append(phone)
        if status:
            conditions.append("status=%s")
            params.append(status)
        if due_before is not None:
            conditions.append("scheduled_at<=%s")
            params.append(_dt(due_before) or str(due_before))
        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        order = "priority ASC, scheduled_at ASC" if ascending else "scheduled_at DESC"
        params.append(max(1, int(limit)))
        rows = await self._fetchall(
            f"SELECT * FROM followup_actions{where} ORDER BY {order} LIMIT %s",
            tuple(params),
        )
        return [_followup_row(row) for row in rows]

    async def claim_followup_actions(self, due_before: datetime | str, limit: int = 50) -> list[dict]:
        due_value = _dt(due_before) or str(due_before)
        batch_limit = max(1, int(limit))
        now = utc_now_mysql()
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                try:
                    await conn.begin()
                    await cur.execute(
                        "SELECT * FROM followup_actions "
                        "WHERE status='scheduled' AND scheduled_at<=%s "
                        "ORDER BY priority ASC, scheduled_at ASC LIMIT %s FOR UPDATE SKIP LOCKED",
                        (due_value, batch_limit),
                    )
                    raw_rows = await cur.fetchall()
                    columns = [description[0] for description in cur.description]
                    rows = [dict(zip(columns, row)) for row in raw_rows]
                    if rows:
                        row_ids = [row["id"] for row in rows]
                        placeholders = ",".join(["%s"] * len(row_ids))
                        await cur.execute(
                            f"UPDATE followup_actions SET status='processing', "
                            f"attempt_number=COALESCE(attempt_number,0)+1, updated_at=%s "
                            f"WHERE id IN ({placeholders}) AND status='scheduled'",
                            (now, *row_ids),
                        )
                        for row in rows:
                            row["status"] = "processing"
                            row["attempt_number"] = int(row.get("attempt_number") or 0) + 1
                            row["updated_at"] = now
                    await conn.commit()
                    return [_followup_row(row) for row in rows]
                except BaseException:
                    await _rollback_quietly(conn)
                    raise

    async def update_followup_action_status(
        self,
        action_id: str,
        status: str,
        result: Optional[dict] = None,
        error_message: str = "",
        expected_status: str = "processing",
    ) -> bool:
        payload = {
            "status": status,
            "updated_at": utc_now_mysql(),
            "result": result or {},
            "error_message": error_message or "",
        }
        if status in {"completed", "skipped", "failed"}:
            payload["completed_at"] = utc_now_mysql()
        payload = _mysql_payload(payload, {"result"})
        assignments = ",".join([f"`{key}`=%s" for key in payload])
        where = "id=%s"
        params = list(payload.values()) + [action_id]
        if expected_status:
            where += " AND status=%s"
            params.append(expected_status)
        return await self._execute(
            f"UPDATE followup_actions SET {assignments} WHERE {where}", tuple(params),
        ) > 0

    async def reschedule_followup_action(
        self,
        action_id: str,
        scheduled_at: datetime | str,
        reason: str = "",
        expected_status: str = "processing",
    ) -> bool:
        payload = _mysql_payload({
            "status": "scheduled",
            "scheduled_at": scheduled_at,
            "updated_at": utc_now_mysql(),
            "error_message": reason or "",
        })
        assignments = ",".join([f"`{key}`=%s" for key in payload])
        where = "id=%s"
        params = list(payload.values()) + [action_id]
        if expected_status:
            where += " AND status=%s"
            params.append(expected_status)
        return await self._execute(
            f"UPDATE followup_actions SET {assignments} WHERE {where}", tuple(params),
        ) > 0

    async def enqueue_notification(self, row: dict) -> dict:
        payload = _mysql_payload(dict(row or {}), {"payload"})
        dedupe_key = str(payload.get("dedupe_key") or "").strip()
        if not dedupe_key:
            raise ValueError("notification dedupe_key is required")
        payload.setdefault("id", new_uuid())
        payload.setdefault("status", "pending")
        payload.setdefault("attempt_count", 0)
        payload.setdefault("max_attempts", 5)
        payload.setdefault("next_attempt_at", utc_now_mysql())
        payload.setdefault("created_at", utc_now_mysql())
        payload.setdefault("updated_at", utc_now_mysql())
        columns = list(payload.keys())
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                try:
                    await conn.begin()
                    await cur.execute(
                        f"INSERT INTO notification_outbox ({','.join(f'`{column}`' for column in columns)}) "
                        f"VALUES ({','.join(['%s'] * len(columns))}) ON DUPLICATE KEY UPDATE id=id",
                        tuple(payload[column] for column in columns),
                    )
                    await cur.execute("SELECT * FROM notification_outbox WHERE dedupe_key=%s LIMIT 1", (dedupe_key,))
                    stored = await cur.fetchone()
                    if not stored:
                        raise RuntimeError("notification intent was not persisted")
                    stored_columns = [d[0] for d in cur.description]
                    result = _mysql_result_row(dict(zip(stored_columns, stored))) or {}
                    await conn.commit()
                    return result
                except BaseException:
                    await _rollback_quietly(conn)
                    raise

    async def claim_notification_outbox(self, limit: int = 10, lease_seconds: int = 300) -> list[dict]:
        claim_token = new_uuid()
        now = utc_now_mysql()
        lease_until = (utc_now() + timedelta(seconds=max(30, min(int(lease_seconds), 3600)))).strftime("%Y-%m-%d %H:%M:%S.%f")
        batch_limit = max(1, min(int(limit), 100))
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                try:
                    await conn.begin()
                    await cur.execute(
                        """
                        SELECT id FROM notification_outbox
                        WHERE (status IN ('pending','failed') OR (status='processing' AND lease_expires_at <= %s))
                          AND next_attempt_at <= %s
                          AND attempt_count < max_attempts
                        ORDER BY next_attempt_at ASC, created_at ASC
                        LIMIT %s
                        FOR UPDATE SKIP LOCKED
                        """,
                        (now, now, batch_limit),
                    )
                    ids = [row[0] for row in await cur.fetchall()]
                    if not ids:
                        await conn.commit()
                        return []
                    placeholders = ",".join(["%s"] * len(ids))
                    await cur.execute(
                        f"""
                        UPDATE notification_outbox
                        SET status='processing',
                            attempt_count=attempt_count+1,
                            claim_token=%s,
                            lease_expires_at=%s,
                            processing_started_at=%s,
                            updated_at=%s
                        WHERE id IN ({placeholders})
                        """,
                        (claim_token, lease_until, now, now, *ids),
                    )
                    await cur.execute(f"SELECT * FROM notification_outbox WHERE id IN ({placeholders})", tuple(ids))
                    rows = await cur.fetchall()
                    cols = [d[0] for d in cur.description]
                    await conn.commit()
                    return [dict(zip(cols, row)) for row in rows]
                except BaseException:
                    await _rollback_quietly(conn)
                    raise

    async def complete_notification_outbox(self, row_id: str, claim_token: str, provider_message_id: str) -> bool:
        count = await self._execute(
            """
            UPDATE notification_outbox
            SET status='sent', provider_message_id=%s, sent_at=%s, last_error='', claim_token=NULL,
                lease_expires_at=NULL, updated_at=%s,
                payload=CASE
                    WHEN notification_type='appointment_job_start_otp'
                    THEN JSON_SET(COALESCE(payload, JSON_OBJECT()), '$.otp', '[redacted]')
                    ELSE payload
                END
            WHERE id=%s AND claim_token=%s AND status='processing'
            """,
            (provider_message_id or "", utc_now_mysql(), utc_now_mysql(), row_id, claim_token),
        )
        return count > 0

    async def fail_notification_outbox(self, row_id: str, claim_token: str, status: str, safe_error: str, next_attempt_at: str = "") -> bool:
        if status not in {"failed", "permanently_failed", "delivery_unknown"}:
            raise ValueError("invalid notification failure status")
        count = await self._execute(
            """
            UPDATE notification_outbox
            SET status=%s, last_error=%s, next_attempt_at=COALESCE(%s,next_attempt_at), claim_token=NULL,
                lease_expires_at=NULL, updated_at=%s
            WHERE id=%s AND claim_token=%s AND status='processing'
            """,
            (status, (safe_error or "")[:500], _dt(next_attempt_at), utc_now_mysql(), row_id, claim_token),
        )
        return count > 0

    async def recover_stuck_notification_outbox(self, max_processing_age_minutes: int = 15, limit: int = 50) -> int:
        now = utc_now_mysql()
        cutoff = (utc_now() - timedelta(minutes=max(1, int(max_processing_age_minutes)))).strftime("%Y-%m-%d %H:%M:%S.%f")
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                try:
                    await conn.begin()
                    await cur.execute(
                        """
                        SELECT id,attempt_count,max_attempts FROM notification_outbox
                        WHERE status='processing' AND updated_at<=%s
                          AND (lease_expires_at IS NULL OR lease_expires_at<=%s)
                        ORDER BY updated_at ASC LIMIT %s FOR UPDATE SKIP LOCKED
                        """,
                        (cutoff, now, max(1, min(int(limit), 100))),
                    )
                    rows = await cur.fetchall()
                    for row_id, attempts, max_attempts in rows:
                        attempts = int(attempts or 0)
                        status = "permanently_failed" if attempts >= int(max_attempts or 5) else "failed"
                        retry_at = _notification_retry_at(attempts).strftime("%Y-%m-%d %H:%M:%S.%f")
                        await cur.execute(
                            """
                            UPDATE notification_outbox
                            SET status=%s,next_attempt_at=%s,claim_token=NULL,lease_expires_at=NULL,
                                processing_started_at=NULL,updated_at=%s
                            WHERE id=%s AND status='processing'
                            """,
                            (status, retry_at, now, row_id),
                        )
                    await conn.commit()
                    return len(rows)
                except BaseException:
                    await _rollback_quietly(conn)
                    raise

    async def probe_notification_outbox_storage(self, row: dict) -> bool:
        payload = _mysql_payload(dict(row or {}), {"payload"})
        payload.setdefault("id", new_uuid())
        payload.setdefault("status", "pending")
        payload.setdefault("attempt_count", 0)
        payload.setdefault("max_attempts", 5)
        payload.setdefault("next_attempt_at", utc_now_mysql())
        payload.setdefault("created_at", utc_now_mysql())
        payload.setdefault("updated_at", utc_now_mysql())
        dedupe_key = str(payload.get("dedupe_key") or "").strip()
        if not dedupe_key:
            raise ValueError("notification dedupe_key is required")
        columns = list(payload.keys())
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                try:
                    await conn.begin()
                    await cur.execute(
                        f"INSERT INTO notification_outbox ({','.join(f'`{column}`' for column in columns)}) VALUES ({','.join(['%s'] * len(columns))})",
                        tuple(payload[column] for column in columns),
                    )
                    await cur.execute("SELECT id FROM notification_outbox WHERE dedupe_key=%s LIMIT 1", (dedupe_key,))
                    return bool(await cur.fetchone())
                finally:
                    await _rollback_quietly(conn)

    async def backend_health_check(self) -> dict:
        tables = {
            "crm_contacts": ["id", "phone_number", "created_at", "updated_at"],
            "contact_memory": ["id", "phone_number", "insight", "created_at"],
            "whatsapp_conversations": ["id", "phone_number", "appointment_state"],
            "whatsapp_messages": ["id", "conversation_id", "provider_message_id"],
            "whatsapp_logs": ["id", "phone_number", "provider_message_id", "status"],
            "appointment_staff": ["id", "active", "working_days"],
            "appointments": [
                "id", "phone", "date", "time", "status",
                "telegram_notified", "telegram_reminder_sent", "telegram_reminder_sent_at",
                "calcom_booking_uid", "calendar_sync_error", "last_calendar_sync_at",
            ],
            "followup_actions": ["id", "phone_number", "scheduled_at", "status"],
            "notification_outbox": ["id", "dedupe_key", "status", "claim_token"],
            "settings": ["key", "value"],
            "error_logs": ["id", "source", "timestamp"],
            "lead_statuses": ["id", "name"],
            "crm_tags": ["name", "created_at"],
            "call_logs": ["id", "phone_number", "timestamp"],
            "agent_profiles": ["id", "is_default"],
            "campaigns": ["id", "contacts_json", "claim_token", "lease_expires_at"],
            "automation_actions": ["id", "status", "claim_token", "lease_expires_at"],
            "broadcast_campaigns": ["id", "status", "claim_token", "lease_expires_at"],
            "broadcast_recipients": ["id", "campaign_id", "phone_number", "claim_token"],
            "whatsapp_test_recipients": ["id", "phone_number_fingerprint"],
            "whatsapp_health_test_runs": ["id", "status", "selected_tests"],
            "whatsapp_health_test_results": ["id", "test_run_id", "test_key"],
        }
        constraints = {
            "crm_contacts": ["uq_crm_contacts_phone"],
            "whatsapp_conversations": ["uq_wa_conv_phone"],
            "notification_outbox": ["uq_notification_outbox_dedupe"],
            "appointments": ["uq_appointments_rescheduled_from"],
            "lead_statuses": ["uq_lead_statuses_name"],
            "broadcast_recipients": ["uq_broadcast_recipient"],
            "whatsapp_test_recipients": ["uq_whatsapp_test_recipient_fingerprint"],
            "whatsapp_health_test_results": ["uq_health_result_run_key"],
        }
        indexes = {
            "call_logs": ["idx_call_logs_phone_timestamp", "idx_call_logs_recording_cleanup"],
            "campaigns": ["idx_campaigns_status_schedule", "idx_campaigns_claim"],
            "automation_actions": ["idx_automation_due", "idx_automation_claim"],
            "broadcast_campaigns": ["idx_broadcast_campaigns_status_scheduled", "idx_broadcast_campaigns_claim"],
            "broadcast_recipients": ["idx_broadcast_recipients_campaign", "idx_broadcast_recipients_claim"],
            "whatsapp_health_test_runs": ["idx_whatsapp_health_runs_recipient_started"],
            "whatsapp_health_test_results": ["idx_whatsapp_health_results_run_started", "idx_whatsapp_health_results_provider"],
            "notification_outbox": ["idx_notification_outbox_due", "idx_notification_outbox_claim"],
        }
        result = {
            "backend": "mysql",
            "connection_ok": False,
            "server_version": "",
            "required_tables_present": [],
            "required_tables_missing": [],
            "required_columns_missing": {},
            "critical_constraints_missing": {},
            "critical_indexes_missing": {},
        }
        version = await self._fetchone("SELECT VERSION() AS version")
        result["connection_ok"] = True
        result["server_version"] = (version or {}).get("version", "")
        current_db = os.getenv("MYSQL_DATABASE", "")
        for table, columns in tables.items():
            table_row = await self._fetchone(
                "SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s",
                (current_db, table),
            )
            if not table_row:
                result["required_tables_missing"].append(table)
                result["required_columns_missing"][table] = columns
                continue
            result["required_tables_present"].append(table)
            existing_cols = await self._fetchall(
                "SELECT COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s",
                (current_db, table),
            )
            names = {row["COLUMN_NAME"] for row in existing_cols}
            missing_cols = [col for col in columns if col not in names]
            if missing_cols:
                result["required_columns_missing"][table] = missing_cols
            if table in constraints:
                existing_cons = await self._fetchall(
                    """
                    SELECT CONSTRAINT_NAME
                    FROM information_schema.TABLE_CONSTRAINTS
                    WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s
                    """,
                    (current_db, table),
                )
                cons_names = {row["CONSTRAINT_NAME"] for row in existing_cons}
                missing_cons = [name for name in constraints[table] if name not in cons_names]
                if missing_cons:
                    result["critical_constraints_missing"][table] = missing_cons
            if table in indexes:
                existing_indexes = await self._fetchall(
                    "SELECT INDEX_NAME FROM information_schema.STATISTICS WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s",
                    (current_db, table),
                )
                index_names = {row["INDEX_NAME"] for row in existing_indexes}
                missing_indexes = [name for name in indexes[table] if name not in index_names]
                if missing_indexes:
                    result["critical_indexes_missing"][table] = missing_indexes
        result["ok"] = (
            result["connection_ok"]
            and not result["required_tables_missing"]
            and not result["required_columns_missing"]
            and not result["critical_constraints_missing"]
            and not result["critical_indexes_missing"]
        )
        return result


_repo_cache: dict[str, AirzenRepository] = {}


def configured_backend() -> str:
    return (os.getenv("CRM_DB_BACKEND") or os.getenv("AIRZEN_DB_BACKEND") or "supabase").strip().lower() or "supabase"


def create_repository(adb_factory: Optional[Callable[[], Any]] = None, *, pool: Any = None) -> AirzenRepository:
    backend = configured_backend()
    if backend == "mysql":
        return MySQLRepository(pool=pool)
    if not adb_factory:
        raise RuntimeError("Supabase repository requires an async Supabase client factory")
    return SupabaseRepository(adb_factory)


def get_cached_repository(adb_factory: Callable[[], Any]) -> AirzenRepository:
    backend = configured_backend()
    cache_key = f"{backend}:{id(adb_factory)}"
    if backend == "mysql":
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        cache_key = f"{cache_key}:loop:{id(loop) if loop is not None else 'none'}"
    if cache_key not in _repo_cache:
        _repo_cache[cache_key] = create_repository(adb_factory)
    return _repo_cache[cache_key]


def reset_repository_cache() -> None:
    _repo_cache.clear()
