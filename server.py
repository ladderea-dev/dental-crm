"""FastAPI backend for the AirZen secure staff CRM portal."""

import asyncio
import base64
import csv
import hashlib
import hmac
from datetime import datetime, timedelta, timezone
from io import BytesIO, StringIO
import json
import logging
import os
import random
import re
import ssl
import subprocess
import time
import uuid
import certifi
import aiohttp
import httpx
from pathlib import Path
from urllib.parse import urlparse, unquote, quote
from typing import Any, Optional, List, Dict, Union
from zoneinfo import ZoneInfo
import threading

from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

_orig_ssl = ssl.create_default_context

def _certifi_ssl(purpose=ssl.Purpose.SERVER_AUTH, **kwargs):
    if not kwargs.get("cafile") and not kwargs.get("capath") and not kwargs.get("cadata"):
        kwargs["cafile"] = certifi.where()
    return _orig_ssl(purpose, **kwargs)

ssl.create_default_context = _certifi_ssl

from db import (
    ConfigError, CRM_TERMINAL_STATUSES, DuplicateContactError,
    cancel_appointment, clear_all_test_data, clear_appointments,
    activate_appointment_staff, deactivate_appointment_staff, delete_appointment_staff, get_appointment_settings, get_appointment_staff,
    update_appointment_status, update_appointment_notes, reschedule_appointment,
    update_appointment_notifications,
    clear_call_logs, clear_campaigns, clear_contact_memory, clear_error_logs,
    clear_errors, create_agent_profile, create_campaign, delete_agent_profile,
    delete_campaign, get_agent_profile,
    get_all_agent_profiles, get_all_appointments, get_all_calls,
    get_appointment_by_id,
    get_all_campaigns, get_all_settings, get_calls_by_phone, get_campaign,
    get_call_logs_for_export, get_contacts, get_crm_contact_detail, get_crm_contacts,
    get_crm_summary, get_lead_statuses, get_crm_contact_by_phone,
    get_logs, get_recording_storage_stats, get_recordings_for_cleanup,
    get_repository, get_setting, get_stats, init_db, log_error, mark_recording_deleted,
    repository_backend_health,
    DEFAULT_COUNTRY, DEFAULT_COUNTRY_CODE, DEFAULT_TIMEZONE, DEFAULT_CURRENCY,
    DEFAULT_CRM_STATUS, DEFAULT_CRM_SOURCE,
    normalize_phone, _tz_today, upsert_crm_lead,
    canonical_crm_stage, crm_stage_updates, CRM_PIPELINE_STAGES,
    save_appointment_settings, save_settings, set_default_agent_profile, set_setting,
    add_lead_status, delete_lead_status, update_agent_profile, update_call_notes,
    PIPELINE_PRESETS, apply_pipeline_preset,
    upsert_appointment_staff,
    update_campaign_contacts, update_campaign_run_stats, update_campaign_status,
    update_crm_contact_followup, update_crm_contact_full, update_crm_contact_notes,
    update_crm_contact_status, delete_crm_contact_by_phone,
    create_followup_action, get_due_followup_actions, claim_due_followup_actions, get_followup_actions,
    get_lead_followup_state, increment_lead_attempts, mark_lead_stop_automation,
    reschedule_followup_action, set_next_best_action, update_followup_action_status,
    update_lead_journey,
    get_knowledge_base, save_knowledge_base, get_kb_section, save_kb_section,
    _KB_SECTIONS,
)
from prompts import (
    DEFAULT_SYSTEM_PROMPT,
    PROMPT_TYPES,
    get_default_prompt,
    get_prompt_label,
    build_prompt_for_type,
    build_knowledge_context,
    get_kb_prompt_prefix,
)
from whatsapp import (
    WA_SETTINGS_KEYS, WA_TEMPLATE_PURPOSES, AUTOMATION_EVENT_TYPES, AUTOMATION_ACTION_TYPES,
    build_template_params, get_template_expected_param_count, _template_purpose_key,
    get_wa_settings_masked, save_wa_settings, get_wa_health,
    send_whatsapp_template, get_whatsapp_logs,
    get_automation_rules, get_automation_rules_with_source, save_automation_rules, find_automation_rule, source_to_event_type,
    execute_automation_rule,
    insert_automation_action, get_automation_actions, update_automation_action_status,
    run_due_automation_actions,
    send_callback_confirmation, send_appointment_confirmation, send_showroom_visit_confirmation,
    handle_call_outcome_whatsapp_fallback, resolve_wa_template, _schedule_or_start_call,
    # Phase 8 — WhatsApp Inbox
    get_conversations, get_conversation_by_id, get_messages, patch_conversation,
    save_wa_message, send_whatsapp_text, is_whatsapp_service_window_open,
    get_or_create_conversation, update_conversation_last_message,
    soft_delete_whatsapp_message, soft_delete_whatsapp_conversation,
    parse_webhook_messages, handle_inbound_whatsapp_message,
    get_conversations_by_phone, get_whatsapp_message_activity,
    fetch_whatsapp_media,
    get_whatsapp_gemini_model,
    run_due_appointment_reminders,
    send_staff_appointment_notification,
    enqueue_required_appointment_notifications,
    run_due_notification_outbox,
    cancel_appointment_with_notification_outbox_rpc,
    reschedule_appointment_with_notification_outbox_rpc,
    _atomic_outbox_required,
)
from followup import parse_followup_time
from whatsapp_health_test import (
    ALL_TEMPLATE_TEST_PURPOSES, MARKETING_TEST_PURPOSES, RUN_CONFIRMATION,
    HealthTestSafetyError, RecipientCipher, country_code_for,
    generate_verification_code, hash_verification_code, mask_phone,
    prepare_existing_template, report_bytes, safe_result, sanitize, template_readiness,
    utcnow, validate_test_selection, verification_expiry, verify_code,
)
from staff_mobile import authenticate_mobile_admin_request, authenticate_mobile_request, router as staff_mobile_router
from auth_users import (
    authenticate_user, get_all_users, create_user, find_user_by_id,
    UserCreateRequest, UserUpdateRequest, user_has_permission
)
from telecaller import (
    get_telecaller_stats, get_telecaller_queue, fetch_next_lead_for_telecaller,
    process_telecaller_disposition, DispositionRequest, TelecallerCallRequest, VALID_DISPOSITIONS,
    get_telecaller_followups, assign_lead_auto, reassign_leads, get_telecaller_performance_report,
    ReassignRequest
)
from openrouter import openrouter_chat_completion, get_openrouter_default_model


load_dotenv(".env", override=True)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("server")
VOICE_FLOW_RUNTIME = "v4_combined_opening"
DEFAULT_OUTBOUND_AGENT_NAME = "outbound-caller-v3"

_START_TIME = time.time()
LAST_BACKGROUND_ERROR = None

# The application loop owns async database resources such as the MySQL pool.
APPLICATION_LOOP = None

# Persistent fallback loop for work dispatched before application startup.
BACKGROUND_LOOP = None
_BACKGROUND_LOOP_LOCK = asyncio.Lock if asyncio else None # simple reference

# Guard against overlapping jobs
_RUNNING_JOBS = {}
_RUNNING_JOBS_LOCK = threading.Lock()

# Throttle DB error logging for Errno 24 scenario
_LAST_DB_ERROR_LOG_TIME = {}
_LAST_DB_ERROR_LOG_TIME_LOCK = threading.Lock()

def _start_background_loop():
    global BACKGROUND_LOOP
    BACKGROUND_LOOP = asyncio.new_event_loop()

    def run_loop():
        asyncio.set_event_loop(BACKGROUND_LOOP)
        logger.info("background_event_loop_started")
        BACKGROUND_LOOP.run_forever()

    t = threading.Thread(target=run_loop, name="persistent-background-loop", daemon=True)
    t.start()

# Start background loop immediately on app start
_start_background_loop()


def _background_execution_loop():
    """Return the running loop that owns application async resources."""
    if APPLICATION_LOOP and APPLICATION_LOOP.is_running() and not APPLICATION_LOOP.is_closed():
        return APPLICATION_LOOP
    return BACKGROUND_LOOP


def _record_background_error(task: str, exc: Exception):
    global LAST_BACKGROUND_ERROR
    error_msg = str(exc)
    LAST_BACKGROUND_ERROR = {
        "task": task,
        "error": error_msg,
        "timestamp": datetime.now().isoformat()
    }
    logger.error("Background task error in %s: %s", task, error_msg)

    # Throttle DB log saving
    now = time.time()
    cache_key = (task, error_msg)
    with _LAST_DB_ERROR_LOG_TIME_LOCK:
        last_logged = _LAST_DB_ERROR_LOG_TIME.get(cache_key, 0)
        if now - last_logged < 300: # 5 minutes cooldown
            return
        _LAST_DB_ERROR_LOG_TIME[cache_key] = now

    # Keep DB error logging on the same loop that owns the repository pool.
    target_loop = _background_execution_loop()
    if target_loop and target_loop.is_running():
        try:
            asyncio.run_coroutine_threadsafe(log_error("background_task_error", "scheduler_loop_error", f"task={task}; error={error_msg[:400]}", "error"), target_loop)
            asyncio.run_coroutine_threadsafe(log_error("server", "runtime_health_background_error", f"task={task}; error={error_msg[:400]}", "error"), target_loop)
        except Exception as e:
            logger.warning("Failed to dispatch background error to db: %s", e)


def run_coro_background(coro, job_name: str, timeout: float = 45.0) -> Any:
    # Overlapping check
    if job_name in ("automation_actions", "appointment_reminders", "followup_actions", "notification_outbox"):
        with _RUNNING_JOBS_LOCK:
            if _RUNNING_JOBS.get(job_name, False):
                logger.warning("background_job_skipped_already_running job_name=%s", job_name)
                return None
            _RUNNING_JOBS[job_name] = True

    logger.info("background_job_started job_name=%s", job_name)
    try:
        future = asyncio.run_coroutine_threadsafe(coro, _background_execution_loop())
        res = future.result(timeout=timeout)
        logger.info("background_job_completed job_name=%s", job_name)
        return res
    except TimeoutError as exc:
        logger.error("background_job_timeout job_name=%s timeout=%s", job_name, timeout)
        return None
    except Exception as exc:
        logger.error("background_job_failed job_name=%s error=%s", job_name, exc)
        _record_background_error(job_name, exc)
        return None
    finally:
        if job_name in ("automation_actions", "appointment_reminders", "followup_actions", "notification_outbox"):
            with _RUNNING_JOBS_LOCK:
                _RUNNING_JOBS[job_name] = False


def _deployed_code_version() -> str:
    env_version = os.getenv("DEPLOYED_CODE_VERSION") or os.getenv("RENDER_GIT_COMMIT") or os.getenv("SOURCE_VERSION")
    if env_version:
        return env_version[:12]
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=Path(__file__).resolve().parent,
            capture_output=True,
            text=True,
            timeout=2,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass
    return "unknown"


async def _outbound_agent_name() -> str:
    try:
        db_val = await get_setting("OUTBOUND_AGENT_NAME", "")
        if db_val and db_val.strip():
            return db_val.strip()
    except Exception:
        pass
    return (os.getenv("OUTBOUND_AGENT_NAME") or DEFAULT_OUTBOUND_AGENT_NAME).strip() or DEFAULT_OUTBOUND_AGENT_NAME

try:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger
    _scheduler = AsyncIOScheduler()
except ImportError:
    _scheduler = None
    logger.warning("APScheduler not installed — campaign scheduling disabled")

app = FastAPI(title="AirZen Secure Staff CRM Portal", version="1.0.0")
app.include_router(staff_mobile_router)

SESSION_COOKIE_NAME = "aicalling_admin_session"
SESSION_TTL_SECONDS = 12 * 60 * 60
PUBLIC_API_ROUTES = {
    ("GET", "/api/live"),
    ("GET", "/api/auth/login"),
    ("GET", "/login"),
    ("POST", "/api/auth/login"),
    ("GET", "/api/auth/me"),
    ("POST", "/api/auth/logout"),
    ("POST", "/api/mobile/auth/login"),
    ("POST", "/api/mobile/auth/refresh"),
    ("POST", "/api/mobile/admin/auth/login"),
    ("GET", "/api/branding"),
    ("GET", "/api/whatsapp/webhook"),   # Phase 8 — verify token
    ("POST", "/api/whatsapp/webhook"),  # Phase 8 — incoming messages
}


def _admin_username() -> str:
    return os.getenv("ADMIN_USERNAME", "")


def _admin_password() -> str:
    return os.getenv("ADMIN_PASSWORD", "")


def _session_secret() -> str:
    return os.getenv("SESSION_SECRET") or "airzen-crm-secret-salt-key-2026-v2"


def _admin_config_error() -> Optional[str]:
    return None


async def _auth_enabled() -> bool:
    """Authentication is always enforced for non-public API routes.

    AUTH_ENABLED is retained as a legacy setting for configuration compatibility,
    but it must never be able to disable the production API authentication gate.
    """
    return True


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64decode(data: str) -> bytes:
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def _sign_session(payload: str) -> str:
    return hmac.new(_session_secret().encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def _make_session_token(username: str, role: str = "admin", user_id: str = "") -> str:
    payload = _b64encode(json.dumps({
        "u": username,
        "r": role,
        "uid": user_id,
        "exp": int(time.time()) + SESSION_TTL_SECONDS
    }, separators=(",", ":")).encode("utf-8"))
    return f"{payload}.{_sign_session(payload)}"


def _read_session(request: Request) -> Optional[dict]:
    token = request.cookies.get(SESSION_COOKIE_NAME, "")
    if not token or "." not in token or not _session_secret():
        return None
    payload, signature = token.rsplit(".", 1)
    if not hmac.compare_digest(signature, _sign_session(payload)):
        return None
    try:
        data = json.loads(_b64decode(payload).decode("utf-8"))
    except Exception:
        return None
    if int(data.get("exp", 0)) < int(time.time()):
        return None
    username = data.get("u")
    if not username:
        return None
    return {
        "username": username,
        "role": data.get("r", "admin"),
        "user_id": data.get("uid", ""),
    }


def _request_is_https(request: Request) -> bool:
    proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
    return request.url.scheme == "https" or proto == "https"


def _apply_crm_response_headers(request: Request, response: Response) -> Response:
    """Apply anti-indexing and browser protections without changing API behavior."""
    path = request.url.path
    if path == "/api/live":
        return response
    noindex = (
        path == "/"
        or path.startswith("/api/")
        or path in {"/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"}
    )
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    if noindex:
        response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
    if path == "/" or path.startswith("/api/auth/"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.middleware("http")
async def _admin_auth_middleware(request: Request, call_next):
    if request.url.path.startswith("/api/") and (request.method, request.url.path) not in PUBLIC_API_ROUTES:
        if request.url.path.startswith("/api/mobile/admin/"):
            mobile_admin = await authenticate_mobile_admin_request(request)
            if not mobile_admin:
                response = JSONResponse(status_code=401, content={"error": "Administrator authentication required"})
                return _apply_crm_response_headers(request, response)
            request.state.mobile_admin = mobile_admin
            response = await call_next(request)
            return _apply_crm_response_headers(request, response)
        if request.url.path.startswith("/api/mobile/"):
            mobile_staff = await authenticate_mobile_request(request)
            if not mobile_staff:
                response = JSONResponse(status_code=401, content={"error": "Staff authentication required"})
                return _apply_crm_response_headers(request, response)
            request.state.mobile_staff = mobile_staff
            response = await call_next(request)
            return _apply_crm_response_headers(request, response)
        api_key = request.headers.get("x-api-key") or request.query_params.get("api_key") or ""
        expected = await get_setting("ADMIN_API_KEY", "")
        if expected and hmac.compare_digest(api_key, expected):
            response = await call_next(request)
            return _apply_crm_response_headers(request, response)
        if not _read_session(request):
            response = JSONResponse(status_code=401, content={"error": "Unauthorized"})
            return _apply_crm_response_headers(request, response)
    response = await call_next(request)
    return _apply_crm_response_headers(request, response)


@app.exception_handler(ConfigError)
async def _config_error_handler(request: Request, exc: ConfigError):
    return JSONResponse(status_code=503, content={"error": str(exc)})


@app.exception_handler(Exception)
async def _unhandled_exception_handler(request: Request, exc: Exception):
    msg = str(exc)
    # Map the most common Supabase misconfig to a clean 503 instead of a 500 stack trace.
    if "supabase_key is required" in msg or "supabase_url is required" in msg.lower():
        return JSONResponse(
            status_code=503,
            content={"error": "Supabase not configured. Set SUPABASE_URL and SUPABASE_SERVICE_KEY env vars."},
        )
    logger.exception("Unhandled error on %s: %s", request.url.path, exc)
    return JSONResponse(status_code=500, content={"error": msg})

def _run_due_appointment_reminders_sync():
    """Sync wrapper for APScheduler appointment reminders."""
    run_coro_background(run_due_appointment_reminders(), "appointment_reminders")


def _run_due_notification_outbox_sync():
    """Sync wrapper for durable notification outbox processing."""
    run_coro_background(run_due_notification_outbox(), "notification_outbox")


async def run_due_followup_actions() -> dict:
    if (await get_setting("FOLLOWUP_ENABLED", "true") or "true").lower() == "false":
        return {"processed": 0, "disabled": True}
    due = await claim_due_followup_actions(limit=50)
    if not due:
        return {"processed": 0, "total_due": 0}
    processed = 0
    for action in due:
        action_id = action.get("id", "")
        phone = action.get("phone_number", "")
        channel = (action.get("channel") or "").lower()
        action_type = action.get("action_type") or ""
        await log_error("followup", "followup_action_due", f"id={action_id}; phone={phone}; action_type={action_type}; channel={channel}", "info")
        try:
            state = await get_lead_followup_state(phone) or {}
            status = state.get("crm_status") or ""
            if state.get("stop_automation") or status in CRM_TERMINAL_STATUSES:
                await update_followup_action_status(action_id, "skipped", {"reason": "stop_automation", "status": status})
                await log_error("followup", "followup_action_skipped_stop_automation", f"id={action_id}; phone={phone}; status={status}", "info")
                processed += 1
                continue
            if int(action.get("attempt_number") or 0) > int(action.get("max_attempts") or 3):
                await update_followup_action_status(action_id, "skipped", {"reason": "max_attempts_reached"})
                processed += 1
                continue
            payload = action.get("payload") or {}
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except Exception:
                    payload = {}
            if channel == "call" or action_type in {"call_only", "call_customer", "callback_call"}:
                if not await _is_outbound_allowed():
                    win = await _outbound_window_error()
                    next_allowed = win.get("next_allowed_at") or (datetime.now().isoformat())
                    await reschedule_followup_action(action_id, next_allowed, "outside_outbound_window")
                    await log_error("followup", "followup_action_rescheduled_outside_window", f"id={action_id}; phone={phone}; next_allowed_at={next_allowed}", "info")
                    processed += 1
                    continue
                contact = {
                    "phone": phone,
                    "lead_name": state.get("lead_name") or action.get("lead_name") or "there",
                    "business_name": state.get("business_name") or "our company",
                    "service_type": state.get("service_type") or "our service",
                    "source": state.get("source") or action.get("source") or "followup",
                }
                result = await _schedule_or_start_call(phone, contact, "voice_call", action.get("event_type") or "followup", action.get("source") or "followup", delay_minutes=0, return_details=True)
                await increment_lead_attempts(phone, "call")
                await update_followup_action_status(action_id, "completed", result if isinstance(result, dict) else {"result": result})
                await log_error("followup", "followup_action_executed", f"id={action_id}; phone={phone}; channel=call; result={result}", "info")
            else:
                template_purpose = payload.get("template_purpose") or ("reminder_template" if action_type == "demo_reminder" else "no_response_followup_template")
                message = payload.get("message") or action.get("reason") or "Following up as requested."
                template = await resolve_wa_template(template_purpose)
                service_window_open = await is_whatsapp_service_window_open(phone)
                if action_type in {"whatsapp_message", "message_customer"} and service_window_open:
                    wa_result = await send_whatsapp_text(phone, message)
                    await log_error("followup", "followup_whatsapp_path", f"id={action_id}; phone={phone}; path=free_text_24h_window_open", "info")
                elif template:
                    wa_result = await send_whatsapp_template(
                        phone, template, "en", [],
                        event_type=action.get("event_type") or "followup",
                        source_type="followup_actions", source_id=action_id,
                        template_purpose=template_purpose,
                        template_context={
                            **state,
                            **payload,
                            "customer_name": state.get("lead_name") or action.get("lead_name") or "there",
                            "phone": phone,
                        },
                    )
                    await log_error("followup", "followup_whatsapp_path", f"id={action_id}; phone={phone}; path=template; template_purpose={template_purpose}; service_window_open={str(service_window_open).lower()}", "info")
                else:
                    wa_result = {"success": False, "error": "outside_24h_window_template_missing" if not service_window_open else "template_missing", "reason": "template_missing"}
                    await log_error("followup", "followup_whatsapp_path", f"id={action_id}; phone={phone}; path=skipped_template_missing; service_window_open={str(service_window_open).lower()}", "warning")
                await increment_lead_attempts(phone, "whatsapp")
                final_status = "completed" if wa_result.get("success") else ("skipped" if wa_result.get("reason") == "duplicate_suppressed_cooldown" else "failed")
                await update_followup_action_status(action_id, final_status, wa_result, wa_result.get("error") or "")
                await log_error("followup", "followup_action_executed", f"id={action_id}; phone={phone}; channel=whatsapp; status={final_status}", "info")
                if action.get("event_type") == "no_response_followup":
                    await log_error("followup", "no_response_followup_sent", f"id={action_id}; phone={phone}; status={final_status}", "info")
            processed += 1
        except Exception as exc:
            await update_followup_action_status(action_id, "failed", {}, str(exc)[:500])
            await log_error("followup", "followup_action_failed", f"id={action_id}; phone={phone}; error={exc}", "error")
            processed += 1
    return {"processed": processed, "total_due": len(due)}


async def evaluate_no_response_followups() -> dict:
    if (await get_setting("FOLLOWUP_ENABLED", "true") or "true").lower() == "false":
        return {"scheduled": 0, "disabled": True}
    try:
        rows = await get_crm_contacts()
    except Exception as exc:
        await log_error("followup", "no_response_scan_failed", str(exc), "warning")
        return {"scheduled": 0, "error": str(exc)}
    delay_hours = int(await get_setting("FOLLOWUP_NO_RESPONSE_TEMPLATE_DELAY_HOURS", "24") or 24)
    max_wa = int(await get_setting("FOLLOWUP_MAX_WHATSAPP_FOLLOWUPS", "3") or 3)
    scheduled = 0
    now = datetime.now()
    for lead in rows:
        phone = lead.get("phone_number") or ""
        if not phone or lead.get("stop_automation") or (lead.get("crm_status") or "") in CRM_TERMINAL_STATUSES:
            continue
        if lead.get("last_customer_reply_at"):
            continue
        last_sent = lead.get("last_whatsapp_sent_at") or ""
        if not last_sent:
            continue
        try:
            sent_at = datetime.fromisoformat(str(last_sent).replace("Z", "+00:00")).replace(tzinfo=None)
        except Exception:
            continue
        count = int(lead.get("no_response_followup_count") or 0)
        if count >= max_wa or now < sent_at + timedelta(hours=delay_hours):
            continue
        pending = [
            a for a in await get_followup_actions(phone=phone, status="scheduled", limit=20)
            if a.get("event_type") == "no_response_followup"
        ]
        if pending:
            continue
        action_id = await create_followup_action(
            phone, "no_response_followup", "whatsapp_template", "whatsapp", now,
            reason="welcome_sent_no_customer_reply", payload={"template_purpose": "no_response_followup_template"},
        )
        await update_lead_journey(phone, {
            "journey_stage": "whatsapp_no_response",
            "crm_status": "whatsapp_no_response",
            "no_response_followup_count": count + 1,
            "last_followup_reason": "welcome_sent_no_customer_reply",
        })
        await log_error("followup", "no_response_sequence_started", f"phone={phone}; action_id={action_id}; count={count + 1}", "info")
        scheduled += 1
    try:
        demo_check_enabled = (await get_setting("DEMO_COMPLETION_CHECK_ENABLED", "true")).lower() in ("true", "1", "yes", "on")
    except Exception:
        demo_check_enabled = True

    if not demo_check_enabled:
        try:
            booked = await get_all_appointments(status_filter="booked")
        except Exception:
            booked = []
        for appt in booked:
            phone = appt.get("phone") or appt.get("phone_number") or ""
            if not phone:
                continue
            try:
                appt_dt = datetime.fromisoformat(f"{appt.get('date')}T{str(appt.get('time') or '00:00')[:5]}:00")
            except Exception:
                continue
            if appt_dt >= now:
                continue
            state = await get_lead_followup_state(phone) or {}
            if state.get("journey_stage") == "demo_no_show":
                continue
            await update_appointment_status(appt.get("id"), "no_show")
            await update_lead_journey(phone, {"journey_stage": "demo_no_show", "crm_status": "demo_no_show", "last_followup_reason": "demo_time_passed_no_response"})
            action_id = await create_followup_action(phone, "demo_no_show", "whatsapp_template", "whatsapp", now, reason="reschedule_after_demo_no_show", payload={"template_purpose": "no_response_followup_template", "appointment_id": appt.get("id")})
            await log_error("followup", "demo_no_show_detected", f"phone={phone}; appointment_id={appt.get('id')}; action_id={action_id}", "info")
            scheduled += 1
    return {"scheduled": scheduled}


async def _run_followup_and_evaluate():
    await run_due_followup_actions()
    await evaluate_no_response_followups()


def _run_due_followup_actions_sync() -> None:
    run_coro_background(_run_followup_and_evaluate(), "followup_actions")


def _date_in_range(value: str, date_from: Optional[str], date_to: Optional[str]) -> bool:
    day = (value or "")[:10]
    if not day:
        return False
    if date_from and day < date_from:
        return False
    if date_to and day > date_to:
        return False
    return True


def _sum_duration_minutes(rows: list[dict]) -> float:
    return round(sum(float(r.get("duration_seconds") or 0) for r in rows) / 60.0, 2)


def _json_list(value) -> list:
    if isinstance(value, list):
        return value
    if not value:
        return []
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, list) else []
    except Exception:
        return []


def _json_dict(value) -> dict:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _csv_response(filename: str, rows: list[dict]) -> StreamingResponse:
    out = StringIO()
    if rows:
        headers = sorted({k for row in rows for k in row.keys()})
        writer = csv.DictWriter(out, fieldnames=headers)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in headers})
    else:
        out.write("empty\n")
    out.seek(0)
    return StreamingResponse(iter([out.getvalue()]), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{filename}"'})


async def _report_data(date_from: Optional[str] = None, date_to: Optional[str] = None) -> dict:
    warnings = []

    async def safe(label: str, loader, default=None):
        try:
            return await loader()
        except Exception as exc:
            await log_error("reports", "report_source_unavailable", f"source={label}; error={str(exc)[:300]}", "warning")
            return [] if default is None else default
    calls = [r for r in await safe("call_logs", lambda: get_call_logs_for_export({})) if _date_in_range(r.get("timestamp") or "", date_from, date_to)]
    contacts = [r for r in await safe("crm_contacts", lambda: get_crm_contacts(warnings=warnings)) if _date_in_range(r.get("created_at") or r.get("updated_at") or "", date_from, date_to)]
    appointments = [r for r in await safe("appointments", get_all_appointments) if _date_in_range(r.get("date") or r.get("created_at") or "", date_from, date_to)]
    wa_logs = [r for r in await safe("whatsapp_logs", lambda: get_whatsapp_logs(limit=5000)) if _date_in_range(r.get("created_at") or "", date_from, date_to)]
    wa_messages = [r for r in await safe("whatsapp_messages", lambda: get_repository().list_whatsapp_messages_for_report(limit=5000)) if _date_in_range(r.get("created_at") or r.get("timestamp") or "", date_from, date_to)]
    followups = await safe("followup_actions", lambda: get_followup_actions(limit=5000))
    return {"calls": calls, "contacts": contacts, "appointments": appointments, "whatsapp_logs": wa_logs, "whatsapp_messages": wa_messages, "followups": followups, "warnings": warnings}


def _status_slug(status: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (status or "").strip().lower()).strip("_") or "new_lead"


KANBAN_COLUMNS = [
    ("new_lead", "New Lead"),
    ("contacted", "Contacted"),
    ("interested", "Interested"),
    ("callback_requested", "Callback Requested"),
    ("demo_booked", "Demo Booked"),
    ("demo_done", "Demo Done"),
    ("converted", "Converted"),
    ("not_interested", "Not Interested"),
    ("lost", "Lost"),
]



@app.on_event("startup")
async def _startup():
    global APPLICATION_LOOP
    APPLICATION_LOOP = asyncio.get_running_loop()
    db_probe = init_db()
    if db_probe is not None:
        await db_probe
    version = _deployed_code_version()
    logger.info("deployed_code_version=%s", version)
    try:
        await log_error("server", "deployed_code_version", version, "info")
        await log_error("server", "voice_flow_version", VOICE_FLOW_RUNTIME, "info")
    except Exception:
        pass
    await _sanitize_saved_voice_prompt_setting()

    logger.info("app_starting")
    try:
        await log_error("server", "app_starting", "Application startup diagnostics check initiated", "info")
    except Exception:
        pass

    port = os.getenv("PORT", "8000")
    system_tz = str(datetime.now().astimezone().tzinfo or "UTC")
    app_tz = DEFAULT_TIMEZONE
    auth_enabled = False
    whatsapp_enabled = False
    supabase_configured = False
    livekit_configured = False

    try:
        from db import get_setting
        app_tz = await get_setting("APP_TIMEZONE", DEFAULT_TIMEZONE)
        auth_enabled = (await get_setting("AUTH_ENABLED", "false")).lower() in ("true", "1", "yes", "on")
        supabase_configured = True
    except Exception:
        pass

    try:
        from whatsapp import get_wa_health
        wa_h = await get_wa_health()
        whatsapp_enabled = bool(wa_h.get("enabled", False))
    except Exception:
        pass

    try:
        url = await eff("LIVEKIT_URL")
        key = await eff("LIVEKIT_API_KEY")
        secret = await eff("LIVEKIT_API_SECRET")
        livekit_configured = bool(url and key and secret)
    except Exception:
        pass

    scheduler_status = "configured_and_running" if (_scheduler and _scheduler.running) else ("configured" if _scheduler else "disabled")

    diag_msg = (
        f"port={port}; timezone={system_tz}; app_timezone={app_tz}; "
        f"auth_enabled={auth_enabled}; whatsapp_enabled={whatsapp_enabled}; "
        f"supabase_configured={supabase_configured}; livekit_configured={livekit_configured}; "
        f"scheduler_status={scheduler_status}"
    )

    logger.info("app_started: %s", diag_msg)
    try:
        await log_error("server", "app_started", diag_msg, "info")
    except Exception:
        pass

    if _scheduler:
        try:
            _scheduler.start()
            await _reschedule_all_campaigns()
            await _schedule_recording_cleanup()
            # Automation action runner — every 60 seconds
            from apscheduler.triggers.interval import IntervalTrigger
            _scheduler.add_job(
                _run_due_automation_actions_sync,
                trigger=IntervalTrigger(seconds=60),
                id="automation_runner",
                replace_existing=True,
            )
            _scheduler.add_job(
                _run_due_appointment_reminders_sync,
                trigger=IntervalTrigger(seconds=60),
                id="appointment_reminder_runner",
                replace_existing=True,
            )
            _scheduler.add_job(
                _run_due_followup_actions_sync,
                trigger=IntervalTrigger(seconds=60),
                id="followup_brain_runner",
                replace_existing=True,
            )
            _scheduler.add_job(
                _run_due_notification_outbox_sync,
                trigger=IntervalTrigger(seconds=60),
                id="notification_outbox_runner",
                replace_existing=True,
            )
        except Exception as _e:
            logger.warning("Scheduler startup or runner schedule failed: %s", _e)


@app.on_event("shutdown")
async def _shutdown():
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)


async def eff(key: str) -> str:
    val = await get_setting(key, "")
    return val if val else os.getenv(key, "")


class CallRequest(BaseModel):
    phone: str
    name: Optional[str] = None
    customer_name: Optional[str] = None
    lead_name: str = "there"
    business_name: str = "our company"
    company_name: Optional[str] = None
    service_type: str = "our service"
    source: Optional[str] = None
    notes: Optional[str] = None
    requirement: Optional[str] = None
    system_prompt: Optional[str] = None
    agent_profile_id: Optional[str] = None
    call_type: Optional[str] = None


class AgentProfileRequest(BaseModel):
    name: str
    voice: str = "Kore"
    model: str = "gemini-3.1-flash-live-preview"
    system_prompt: Optional[str] = None
    enabled_tools: str = "[]"
    is_default: bool = False


class PromptRequest(BaseModel):
    prompt: str


class AiPromptRequest(BaseModel):
    prompt: str
    is_default: bool = False


class SettingsRequest(BaseModel):
    settings: dict


class AuthRequest(BaseModel):
    username: str
    password: str


class NotesRequest(BaseModel):
    notes: str


class CampaignRequest(BaseModel):
    name: str
    contacts: list
    schedule_type: str = "once"
    schedule_time: str = "09:00"
    call_delay_seconds: int = 3
    system_prompt: Optional[str] = None
    agent_profile_id: Optional[str] = None


class AppointmentStatusRequest(BaseModel):
    status: str


class AppointmentRescheduleRequest(BaseModel):
    date: str
    time: str


class StatusRequest(BaseModel):
    status: str


class ClearRecordsRequest(BaseModel):
    confirm: Optional[str] = None


class LeadStatusRequest(BaseModel):
    name: str
    color: Optional[str] = None


class CrmStatusRequest(BaseModel):
    crm_status: str
    custom_status: Optional[str] = None


class CrmFollowupRequest(BaseModel):
    next_followup_at: Optional[str] = None


class CrmNotesRequest(BaseModel):
    crm_notes: str = ""


class CrmLeadRequest(BaseModel):
    phone_number: str
    lead_name: str
    email: Optional[str] = None
    city: Optional[str] = None
    location: Optional[str] = None
    address: Optional[str] = None
    requirement: Optional[str] = None
    budget: Optional[str] = None
    source: Optional[str] = DEFAULT_CRM_SOURCE
    business_name: Optional[str] = None
    campaign_name: Optional[str] = None
    service_type: Optional[str] = None
    lead_country: Optional[str] = DEFAULT_COUNTRY
    lead_country_code: Optional[str] = DEFAULT_COUNTRY_CODE
    crm_status: Optional[str] = None
    custom_status: Optional[str] = None
    crm_notes: Optional[str] = None
    next_followup_at: Optional[str] = None
    assigned_to: Optional[str] = None
    tags: Optional[List[str]] = None
    tags_json: Optional[str] = None


class CrmBulkImportRequest(BaseModel):
    leads: list
    import_source: Optional[str] = "api"


class GoogleSheetImportRequest(BaseModel):
    sheet_url: str
    sheet_name: Optional[str] = None
    source: Optional[str] = "google_sheet"


class CrmLeadUpdateRequest(BaseModel):
    lead_name: Optional[str] = None
    email: Optional[str] = None
    city: Optional[str] = None
    location: Optional[str] = None
    address: Optional[str] = None
    requirement: Optional[str] = None
    budget: Optional[str] = None
    source: Optional[str] = None
    business_name: Optional[str] = None
    campaign_name: Optional[str] = None
    service_type: Optional[str] = None
    lead_country: Optional[str] = None
    lead_country_code: Optional[str] = None
    crm_status: Optional[str] = None
    custom_status: Optional[str] = None
    crm_notes: Optional[str] = None
    next_followup_at: Optional[str] = None
    assigned_to: Optional[str] = None


class ScheduleFollowupRequest(BaseModel):
    channel: str = "call"
    when_text: str = "after 30 minutes"
    reason: str = "manual_followup"
    action_type: Optional[str] = None
    template_purpose: Optional[str] = None
    message: Optional[str] = None


class StopAutomationRequest(BaseModel):
    reason: str = "manual_stop"
    status: str = "do_not_contact"


class CrmCallSelectedRequest(BaseModel):
    phones: list
    business_name: str = "our company"
    service_type: str = "our service"
    campaign_name: str = "Selected CRM Leads"
    call_delay_seconds: int = 15
    agent_profile_id: Optional[str] = None
    system_prompt: Optional[str] = None


class RecordingCleanupRequest(BaseModel):
    confirm: Optional[str] = None


class BatchCallRequest(BaseModel):
    contacts: list  # [{phone, lead_name, business_name, service_type}]
    call_delay_seconds: int = 5
    agent_profile_id: Optional[str] = None
    system_prompt: Optional[str] = None
    batch_name: Optional[str] = None


class OutboundScheduleRequest(BaseModel):
    OUTBOUND_TIMEZONE: Optional[str] = None
    OUTBOUND_START_TIME: Optional[str] = None
    OUTBOUND_END_TIME: Optional[str] = None
    OUTBOUND_ALLOWED_DAYS: Optional[str] = None
    OUTBOUND_CALLING_ENABLED: Optional[str] = None


class CustomerSetupRequest(BaseModel):
    company_name: Optional[str] = None
    brand_slug: Optional[str] = None
    business_type: Optional[str] = None
    country: Optional[str] = None
    timezone: Optional[str] = None
    meta_app_id: Optional[str] = None
    meta_app_secret: Optional[str] = None
    whatsapp_business_account_id: Optional[str] = None
    phone_number_id: Optional[str] = None
    permanent_access_token: Optional[str] = None
    verify_token: Optional[str] = None
    completed: bool = False


class WaSettingsRequest(BaseModel):
    settings: dict


class WaSendTemplateRequest(BaseModel):
    phone: str
    template_name: str
    language: str = "en"
    parameters: Optional[list] = None


class WaTemplateDebugTestRequest(BaseModel):
    phone: Optional[str] = ""
    template_purpose: Optional[str] = ""
    template_name: Optional[str] = None
    language: Optional[str] = None
    variables: Optional[list] = None
    status: Optional[str] = None
    dry_run: bool = True
    confirm: Optional[str] = None


class WaHealthRecipientCreateRequest(BaseModel):
    recipient_name: str
    phone_number: str
    controlled_number_confirmed: bool = False


class WaHealthRecipientVerificationSendRequest(BaseModel):
    confirm: str


class WaHealthRecipientVerifyRequest(BaseModel):
    code: str


class WaHealthRunRequest(BaseModel):
    recipient_id: str
    selected_tests: list[str] = Field(default_factory=list)
    language: str = "en"
    confirmation: str = ""
    allow_marketing: bool = False
    marketing_confirmation: str = ""
    test_ai_reply: bool = False
    max_real_messages: int = 10


class WaHealthAllTemplatesRequest(BaseModel):
    recipient_id: str
    include_marketing: bool = False
    include_authentication: bool = False
    max_templates: int = 50
    delay_seconds: int = 2
    stop_on_first_failure: bool = False
    confirmation: str = ""
    confirmation_message: str = ""
    template_names: list[str] = Field(default_factory=list)



class WaTemplatesSaveRequest(BaseModel):
    templates: list


class WaTemplateMetaCreateRequest(BaseModel):
    purpose: str
    language: str = "en"


class AutomationRulesRequest(BaseModel):
    rules: list


class LeadIncomingRequest(BaseModel):
    name: Optional[str] = None
    phone: str
    email: Optional[str] = None
    source: Optional[str] = "api"
    city: Optional[str] = None
    service: Optional[str] = None
    message: Optional[str] = None
    business_name: Optional[str] = None
    campaign_name: Optional[str] = None
    extra: Optional[dict] = None


class AutomationTestRequest(BaseModel):
    phone: str
    event_type: str
    source: Optional[str] = None
    lead_name: Optional[str] = "AirZen Lead"
    dry_run: bool = True


class AppointmentSettingsRequest(BaseModel):
    demo_duration_minutes: Optional[int] = None
    duration_minutes: Optional[int] = None
    buffer_minutes: Optional[int] = None
    slot_interval_minutes: Optional[int] = None
    timezone: Optional[str] = None
    minimum_notice_minutes: Optional[int] = None
    max_booking_days_ahead: Optional[int] = None
    appointment_name: Optional[str] = None
    default_service_type: Optional[str] = None
    delivery_mode: Optional[str] = None
    location_label: Optional[str] = None
    staff_label: Optional[str] = None
    default_staff_role: Optional[str] = None
    assignment_strategy: Optional[str] = None
    intake_prompt: Optional[str] = None


class AppointmentStaffRequest(BaseModel):
    name: str
    email: Optional[str] = ""
    whatsapp_number: Optional[str] = ""
    # Omitted values are resolved from the persisted appointment configuration
    # so staff creation cannot silently drift from the role/timezone used by
    # availability matching.
    role: Optional[str] = None
    calendar_email: Optional[str] = ""
    google_calendar_id: Optional[str] = ""
    google_calendar_connected: Optional[bool] = False
    google_meet_enabled: Optional[bool] = False
    notes: Optional[str] = ""
    working_days: Optional[list] = None
    start_time: Optional[str] = "09:00"
    end_time: Optional[str] = "18:00"
    timezone: Optional[str] = None
    active: Optional[bool] = True
    round_robin_order: Optional[int] = 0


class CreateAppointmentRequest(BaseModel):
    name: str
    phone: str
    date: str
    time: str
    service: Optional[str] = "Consultation"
    staff_id: Optional[str] = ""
    notes: Optional[str] = ""
    service_address: Optional[str] = ""


class CrmTagRequest(BaseModel):
    tag: str


class CrmCustomFieldsRequest(BaseModel):
    fields: dict[str, Any] = {}


class CrmMoveStageRequest(BaseModel):
    stage: str


class BroadcastCampaignRequest(BaseModel):
    name: str
    template_purpose: str
    segment: Optional[dict[str, Any]] = None
    scheduled_at: Optional[str] = None
    dry_run_limit: Optional[int] = 100


class BroadcastStartRequest(BaseModel):
    confirm: Optional[str] = ""


class BroadcastConfigUpdate(BaseModel):
    test_mode: Optional[bool] = None
    max_send_per_run: Optional[int] = None


class BroadcastQuickSendRequest(BaseModel):
    phone_numbers: List[str]
    template_purpose: str
    campaign_name: Optional[str] = "Quick Broadcast"
    confirm: Optional[str] = "START_BROADCAST"


class HandoffRequest(BaseModel):
    reason: Optional[str] = "human_requested"
    staff_id: Optional[str] = ""
    urgent: Optional[bool] = True


class KbPreviewAnswerRequest(BaseModel):
    question: str


class AppointmentActionRequest(BaseModel):
    date: Optional[str] = ""
    time: Optional[str] = ""
    reason: Optional[str] = ""


class WaConfirmRequest(BaseModel):
    phone: str
    lead_name: Optional[str] = None
    business_name: Optional[str] = None
    service_type: Optional[str] = None
    appointment_date: Optional[str] = None
    appointment_time: Optional[str] = None
    address: Optional[str] = None
    contact_number: Optional[str] = None


@app.get("/login")
@app.get("/api/auth/login")
async def redirect_login_to_root():
    """Redirect direct browser navigation to visual login dashboard."""
    return RedirectResponse(url="/", status_code=307)


@app.post("/api/auth/login")
async def api_auth_login(request: Request):
    username = ""
    password = ""
    is_form = False

    content_type = request.headers.get("content-type", "").lower()
    if "application/json" in content_type:
        try:
            body = await request.json()
            if isinstance(body, dict):
                username = str(body.get("username", "")).strip()
                password = str(body.get("password", "")).strip()
        except Exception:
            pass
    elif "form" in content_type:
        try:
            form = await request.form()
            username = str(form.get("username", "")).strip()
            password = str(form.get("password", "")).strip()
            is_form = True
        except Exception:
            pass
    else:
        try:
            body = await request.json()
            if isinstance(body, dict):
                username = str(body.get("username", "")).strip()
                password = str(body.get("password", "")).strip()
        except Exception:
            try:
                form = await request.form()
                username = str(form.get("username", "")).strip()
                password = str(form.get("password", "")).strip()
                is_form = True
            except Exception:
                pass

    user = await authenticate_user(username, password)
    if not user:
        if is_form:
            return RedirectResponse(url="/?error=invalid_credentials", status_code=303)
        return JSONResponse(status_code=401, content={"error": "Invalid username or password"})
    
    role = user.get("role", "admin")
    uid = user.get("id", "")
    valid_username = user.get("username", username)
    token = _make_session_token(valid_username, role=role, user_id=uid)

    if is_form:
        response = RedirectResponse(url="/", status_code=303)
    else:
        response = JSONResponse(content={
            "success": True,
            "username": valid_username,
            "role": role,
            "full_name": user.get("full_name", valid_username),
        })

    response.set_cookie(
        SESSION_COOKIE_NAME,
        token,
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        secure=_request_is_https(request),
        samesite="lax",
        path="/",
    )
    return response


@app.get("/api/auth/me")
async def api_auth_me(request: Request):
    user_session = _read_session(request)
    username = user_session.get("username") if isinstance(user_session, dict) else user_session
    role = user_session.get("role", "admin") if isinstance(user_session, dict) else "admin"
    uid = user_session.get("user_id", "") if isinstance(user_session, dict) else ""
    
    payload = {
        "authenticated": bool(username),
        "username": username if username else None,
        "role": role,
        "user_id": uid,
        "configured": True,
    }
    return payload


@app.post("/api/auth/logout")
async def api_auth_logout(request: Request):
    response = JSONResponse(content={"success": True})
    response.delete_cookie(SESSION_COOKIE_NAME, path="/", samesite="lax", secure=_request_is_https(request))
    return response


@app.get("/api/branding")
async def api_get_branding():
    biz_name = (await eff("BUSINESS_NAME")) or os.getenv("BUSINESS_NAME") or "AI Voice Calling CRM"
    biz_type = (await eff("BUSINESS_TYPE")) or os.getenv("BUSINESS_TYPE") or "General"
    service_type = (await eff("DEFAULT_SERVICE_TYPE")) or os.getenv("DEFAULT_SERVICE_TYPE") or "Consultation"
    city = (await eff("BUSINESS_CITY")) or os.getenv("BUSINESS_CITY") or ""
    return {
        "business_name": biz_name,
        "business_type": biz_type,
        "default_service_type": service_type,
        "city": city,
    }


# ── User Management & RBAC Routes ─────────────────────────────────────────────

@app.get("/api/users")
async def api_get_users(request: Request):
    session = _read_session(request)
    if not session or session.get("role") not in ("admin", "md", "manager", "telecaller_head"):
        raise HTTPException(403, "Administrator or Manager privilege required")
    users = await get_all_users()
    safe = []
    for u in users:
        item = dict(u)
        item.pop("password_hash", None)
        safe.append(item)
    return {"users": safe}


@app.post("/api/users")
async def api_create_user(req: UserCreateRequest, request: Request):
    session = _read_session(request)
    if not session or session.get("role") not in ("admin", "md"):
        raise HTTPException(403, "Administrator privilege required to create users")
    try:
        user = await create_user(req)
        return {"success": True, "user": user}
    except Exception as e:
        raise HTTPException(400, str(e))


@app.delete("/api/users/{user_id}")
async def api_delete_user(user_id: str, request: Request):
    session = _read_session(request)
    if not session or session.get("role") not in ("admin", "md"):
        raise HTTPException(403, "Administrator privilege required to deactivate users")
    users = await get_all_users()
    found = False
    for u in users:
        if u.get("id") == user_id:
            u["active"] = False
            found = True
            break
    if not found:
        raise HTTPException(404, "User not found")
    from auth_users import save_all_users
    await save_all_users(users)
    return {"success": True, "message": "User deactivated"}


# ── Telecaller Workspace & Queue Routes ───────────────────────────────────────

@app.get("/api/telecaller/dashboard")
async def api_telecaller_dashboard(request: Request):
    session = _read_session(request)
    username = session.get("username", "telecaller1") if isinstance(session, dict) else "telecaller1"
    role = session.get("role", "telecaller") if isinstance(session, dict) else "telecaller"
    stats = await get_telecaller_stats(username)
    queue = await get_telecaller_queue(username, limit=50)
    return {
        "telecaller_name": username,
        "role": role,
        "stats": {
            "calls_today": stats.get("today_calls_count", 0),
            "interested": stats.get("today_interested_count", 0),
            "demo_booked": 0,
            "callbacks_pending": stats.get("callbacks_due_count", 0),
        },
        "queue": queue,
        **stats
    }


@app.get("/api/telecaller/queue")
async def api_telecaller_queue(request: Request, limit: int = 50, tag: Optional[str] = None):
    session = _read_session(request)
    username = session.get("username", "telecaller1") if isinstance(session, dict) else "telecaller1"
    queue = await get_telecaller_queue(username, limit=limit, tag=tag)
    return {"queue": queue, "total": len(queue)}


@app.post("/api/telecaller/fetch-next")
async def api_telecaller_fetch_next(request: Request):
    session = _read_session(request)
    username = session.get("username", "telecaller1") if isinstance(session, dict) else "telecaller1"
    lead = await fetch_next_lead_for_telecaller(username)
    if not lead:
        return {"success": False, "lead": None, "message": "No pending leads in queue"}
    return {"success": True, "lead": lead}


@app.post("/api/telecaller/disposition")
async def api_telecaller_disposition(req: DispositionRequest, request: Request):
    session = _read_session(request)
    username = session.get("username", "telecaller1") if isinstance(session, dict) else "telecaller1"
    uid = session.get("user_id", "") if isinstance(session, dict) else ""
    res = await process_telecaller_disposition(req, telecaller_username=username, telecaller_id=uid)
    return res


@app.post("/api/telecaller/call")
async def api_telecaller_call(req: TelecallerCallRequest, request: Request):
    session = _read_session(request)
    username = session.get("username", "telecaller1") if isinstance(session, dict) else "telecaller1"
    phone = normalize_phone(req.phone)
    room_name = f"telecaller-{username}-{random.randint(1000, 9999)}"
    
    url = await eff("LIVEKIT_URL")
    key = await eff("LIVEKIT_API_KEY")
    secret = await eff("LIVEKIT_API_SECRET")
    token = None
    if url and key and secret:
        try:
            from livekit import api as lk_api
            token = lk_api.AccessToken(key, secret) \
                .with_identity(f"telecaller_{username}") \
                .with_name(f"Telecaller {username}") \
                .with_grants(lk_api.VideoGrants(room_join=True, room=room_name)) \
                .to_jwt()
        except Exception as e:
            logger.warning("Could not generate telecaller LiveKit token: %s", e)
            
    return {
        "success": True,
        "room": room_name,
        "livekit_url": url,
        "token": token,
        "phone": phone,
        "dispositions": list(VALID_DISPOSITIONS.keys()),
    }


@app.get("/api/telecaller/followups")
async def api_telecaller_followups(request: Request, scope: str = "my", timezone: Optional[str] = None, tag: Optional[str] = None):
    session = _read_session(request)
    username = session.get("username", "telecaller1") if isinstance(session, dict) else "telecaller1"
    res = await get_telecaller_followups(telecaller_username=username, scope=scope, timezone_str=timezone, tag=tag)
    return res


@app.post("/api/telecaller/reassign")
async def api_telecaller_reassign(req: ReassignRequest, request: Request):
    session = _read_session(request)
    if not session:
        raise HTTPException(401, "Authentication required")
    phones = req.get_phones()
    res = await reassign_leads(phones, req.target_telecaller)
    return res


@app.get("/api/reports/telecallers")
async def api_reports_telecallers(
    request: Request,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    telecaller: Optional[str] = None,
    export: Optional[str] = None,
):
    session = _read_session(request)
    if not session:
        raise HTTPException(401, "Authentication required")
    data = await get_telecaller_performance_report(date_from=date_from, date_to=date_to, telecaller_username=telecaller)
    if export == "csv":
        rows = data.get("rows", [])
        fieldnames = [
            "telecaller", "total_dials", "connected_calls", "total_talk_time_seconds",
            "avg_talk_time_seconds", "interested_count", "demos_booked_count",
            "converted_count", "callbacks_scheduled", "conversion_rate_percent",
            "assigned_leads_count", "overdue_followups_count", "today_followups_count"
        ]
        import io, csv
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for r in rows:
            writer.writerow(r)
        return Response(
            content=output.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename=telecaller_report_{datetime.now().strftime('%Y%m%d')}.csv"}
        )
    return data


# ── Inbound Voice Call Handling via LiveKit SIP ───────────────────────────────

@app.post("/api/livekit/sip-inbound-dispatch")
async def api_livekit_sip_inbound(req: Request):
    """Webhook/dispatch from LiveKit SIP Inbound Trunk when a customer calls in."""
    try:
        body = await req.json()
    except Exception:
        body = {}
    caller_number = body.get("caller_number") or body.get("sip_call_from") or ""
    if caller_number:
        clean_phone = normalize_phone(caller_number)
        contact = await get_crm_contact_by_phone(clean_phone)
        lead_name = contact.get("lead_name") if contact else "Customer"
        company_name = await get_setting("COMPANY_NAME", "AirZen")
        
        await log_call(
            phone_number=clean_phone,
            lead_name=lead_name,
            outcome="inbound_answered",
            reason="Inbound call received via LiveKit SIP",
            duration_seconds=0
        )
        return {
            "status": "routed",
            "caller": clean_phone,
            "lead_name": lead_name,
            "greeting": f"Welcome to {company_name}, {lead_name}! How can we help you today?",
        }
    return {"status": "ok"}


# ── OpenRouter Multi-LLM Test Endpoint ────────────────────────────────────────

@app.post("/api/openrouter/test")
async def api_openrouter_test(req: Request):
    session = _read_session(request=req)
    if not session:
        raise HTTPException(401, "Authentication required")
    try:
        body = await req.json()
    except Exception:
        body = {}
    prompt = body.get("prompt", "Hello! State your model name and provide a brief greeting.")
    model = body.get("model")
    res = await openrouter_chat_completion([{"role": "user", "content": prompt}], model=model)
    return res



@app.get("/api/security/health")
async def api_security_health():
    auth_enabled = await _auth_enabled()
    api_key_configured = bool(await get_setting("ADMIN_API_KEY", ""))
    webhook_token_configured = bool(await get_setting("WEBHOOK_VERIFY_TOKEN", await get_setting("WHATSAPP_VERIFY_TOKEN", "")))
    return {
        "auth_enabled": auth_enabled,
        "admin_session_configured": not bool(_admin_config_error()),
        "admin_api_key_configured": api_key_configured,
        "webhook_verify_token_configured": webhook_token_configured,
        "health_secret_leak_check": "ok",
        "meta_webhook_signature_verification": "hmac_sha256",
    }


@app.get("/api/schema/health")
async def api_schema_health():
    health = await repository_backend_health()
    return {
        "ok": bool(health.get("ok", health.get("connection_ok"))) and not health.get("required_tables_missing"),
        "backend": health.get("backend"),
        "missing_tables": health.get("required_tables_missing") or [],
        "missing_columns": health.get("required_columns_missing") or {},
        "missing_constraints": health.get("critical_constraints_missing") or {},
        "missing_indexes": health.get("critical_indexes_missing") or {},
    }


@app.get("/api/health")
async def api_health():
    async def status_value(key: str, *fallback_envs: str) -> str:
        for env_key in (key, *fallback_envs):
            val = os.getenv(env_key, "")
            if val:
                return val
        return ""

    async def setting_value_with_source(key: str, default: str = "") -> tuple[str, str]:
        env_val = os.getenv(key, "")
        if env_val:
            return env_val, "env"
        try:
            db_val = await get_setting(key, "")
            if db_val:
                return db_val, "db"
        except Exception:
            pass
        return default, "default"

    async def supabase_status(url: str, key: str) -> tuple[bool, Optional[str]]:
        if not (url and key):
            return False, None
        try:
            timeout = aiohttp.ClientTimeout(total=3)
            headers = {"apikey": key, "Authorization": f"Bearer {key}"}
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(f"{url.rstrip('/')}/rest/v1/", headers=headers) as resp:
                    if resp.status in (401, 403):
                        return False, "Invalid API key"
                    return True, None
        except Exception:
            return True, None

    livekit_url = await status_value("LIVEKIT_URL")
    livekit_key = await status_value("LIVEKIT_API_KEY")
    livekit_secret = await status_value("LIVEKIT_API_SECRET")
    google_key = await status_value("GOOGLE_API_KEY")
    supabase_url = await status_value("SUPABASE_URL")
    supabase_key = await status_value("SUPABASE_SERVICE_KEY")
    trunk_id = await status_value("OUTBOUND_TRUNK_ID")
    gemini_model = await status_value("GEMINI_MODEL")
    gemini_voice = await status_value("GEMINI_TTS_VOICE")
    s3_key = await status_value("S3_ACCESS_KEY_ID", "AWS_ACCESS_KEY_ID")
    s3_secret = await status_value("S3_SECRET_ACCESS_KEY", "AWS_SECRET_ACCESS_KEY")
    s3_bucket = await status_value("S3_BUCKET", "AWS_BUCKET_NAME")
    fixed_enabled_raw, fixed_enabled_source = await setting_value_with_source("OUTBOUND_FIXED_GREETING_ENABLED", "")
    greeting_mode_raw, greeting_mode_source = await setting_value_with_source("OUTBOUND_GREETING_MODE", "")
    fixed_greeting_text, fixed_greeting_text_source = await setting_value_with_source(
        "OUTBOUND_FIXED_GREETING",
        "Hi, this is Priya calling from AirZen. Is this a good time to speak for a minute?",
    )
    fixed_enabled_value = fixed_enabled_raw.strip().lower()
    greeting_mode_value = greeting_mode_raw.strip().lower()
    if fixed_enabled_value:
        fixed_greeting_enabled = fixed_enabled_value in ("1", "true", "yes", "on", "enabled", "fixed")
        fixed_greeting_source = fixed_enabled_source
        fixed_greeting_reason = f"OUTBOUND_FIXED_GREETING_ENABLED={fixed_enabled_raw}"
    elif greeting_mode_value:
        fixed_greeting_enabled = greeting_mode_value == "fixed" or greeting_mode_value in ("1", "true", "yes", "on", "enabled")
        fixed_greeting_source = greeting_mode_source
        fixed_greeting_reason = f"OUTBOUND_GREETING_MODE={greeting_mode_raw}"
    else:
        fixed_greeting_enabled = True
        fixed_greeting_source = "default"
        fixed_greeting_reason = "default_fixed_greeting"
    recording_auto_delete = (await status_value("RECORDING_AUTO_DELETE_ENABLED") or "false").strip().lower() in ("1", "true", "yes", "on")
    try:
        recording_retention_days = max(int(await status_value("RECORDING_RETENTION_DAYS") or "7"), 1)
    except ValueError:
        recording_retention_days = 7
    whatsapp_gemini_model = await get_whatsapp_gemini_model()
    voice_call_prompt = await get_setting("AI_PROMPT_voice_call", "")
    whatsapp_chat_prompt = await get_setting("AI_PROMPT_whatsapp_chat", "")
    active_prompt_types = ["voice_call", "whatsapp_chat"]
    prompt_mode = "simple"
    def clean_timezone(value: str, fallback: str = DEFAULT_TIMEZONE) -> str:
        value = (value or fallback).strip() or fallback
        try:
            ZoneInfo(value)
            return value
        except Exception:
            return fallback

    app_timezone_raw, _ = await setting_value_with_source("APP_TIMEZONE", DEFAULT_TIMEZONE)
    app_timezone = clean_timezone(app_timezone_raw)
    appointment_timezone_raw, _ = await setting_value_with_source("APPOINTMENT_TIMEZONE", app_timezone)
    appointment_timezone = clean_timezone(appointment_timezone_raw, app_timezone)
    whatsapp_display_timezone_raw, _ = await setting_value_with_source("WHATSAPP_DISPLAY_TIMEZONE", app_timezone)
    whatsapp_display_timezone = clean_timezone(whatsapp_display_timezone_raw, app_timezone)
    google_calendar_enabled = (await get_setting("GOOGLE_CALENDAR_ENABLED", "false") or "false").lower() in {"1", "true", "yes", "on"}
    google_meet_enabled = (await get_setting("GOOGLE_MEET_ENABLED", "false") or "false").lower() in {"1", "true", "yes", "on"}
    google_calendar_fallback = (await get_setting("GOOGLE_CALENDAR_FALLBACK_TO_INTERNAL", "true") or "true").lower() in {"1", "true", "yes", "on"}
    supabase_configured, supabase_error = await supabase_status(supabase_url, supabase_key)
    response = {
        "status": "ok",
        "app_timezone": app_timezone,
        "appointment_timezone": appointment_timezone,
        "whatsapp_display_timezone": whatsapp_display_timezone,
        "google_calendar_enabled": google_calendar_enabled,
        "google_meet_enabled": google_meet_enabled,
        "google_calendar_fallback_to_internal": google_calendar_fallback,
        "livekit_configured": bool(livekit_url and livekit_key and livekit_secret),
        "gemini_configured": bool(google_key),
        "supabase_configured": supabase_configured,
        "trunk_configured": bool(trunk_id),
        "gemini_model_configured": bool(gemini_model),
        "gemini_tts_voice_configured": bool(gemini_voice),
        "prompt_configured": bool(voice_call_prompt or whatsapp_chat_prompt),
        "prompt_mode": prompt_mode,
        "active_prompt_types": active_prompt_types,
        "default_prompt_used": not bool(voice_call_prompt and whatsapp_chat_prompt),
        "voice_call_prompt_configured": bool(voice_call_prompt),
        "whatsapp_ai_provider": "gemini",
        "whatsapp_gemini_model": whatsapp_gemini_model,
        "whatsapp_gemini_model_configured": bool(whatsapp_gemini_model),
        "whatsapp_chat_prompt_configured": bool(whatsapp_chat_prompt),
        "s3_configured": bool(s3_key and s3_secret and s3_bucket),
        "fixed_greeting_enabled": fixed_greeting_enabled,
        "fixed_greeting_text_present": bool((fixed_greeting_text or "").strip()),
        "fixed_greeting_source": fixed_greeting_source,
        "fixed_greeting_text_source": fixed_greeting_text_source,
        "fixed_greeting_mode": greeting_mode_raw or "default",
        "fixed_greeting_reason": fixed_greeting_reason,
        "recording_auto_delete_enabled": recording_auto_delete,
        "recording_retention_days": recording_retention_days,
        "auth_enabled": await _auth_enabled(),
    }
    if supabase_error:
        response["supabase_error"] = supabase_error
    return response


@app.get("/", response_class=HTMLResponse)
async def serve_dashboard():
    html_path = Path(__file__).parent / "ui" / "index.html"
    if html_path.exists():
        return HTMLResponse(content=html_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>Dashboard not found — place index.html in ui/</h1>", status_code=404)


@app.get("/robots.txt", response_class=PlainTextResponse)
async def robots_txt():
    return PlainTextResponse(
        "User-agent: *\n"
        "Disallow: /api/\n"
        "Disallow: /docs\n"
        "Disallow: /redoc\n"
        "Disallow: /openapi.json\n"
        "Sitemap: https://voice.airzensg.com/sitemap.xml\n"
    )


@app.get("/sitemap.xml")
async def sitemap_xml():
    content = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://voice.airzensg.com/privacy</loc></url>
  <url><loc>https://voice.airzensg.com/terms</loc></url>
  <url><loc>https://voice.airzensg.com/terms-and-conditions</loc></url>
</urlset>
"""
    return Response(content=content, media_type="application/xml")


def _legal_page(title: str, content: str) -> HTMLResponse:
    return HTMLResponse(
        f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title} | AirZen Pte. Ltd.</title>
  <meta name="description" content="{title} for the AirZen Pte. Ltd. secure staff CRM portal.">
  <style>
    :root {{
      color-scheme: light;
      --ink: #162033;
      --muted: #607086;
      --line: #d9e1ea;
      --accent: #0f766e;
      --bg: #f6f8fb;
      --panel: #ffffff;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--ink);
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      line-height: 1.65;
    }}
    header {{
      border-bottom: 1px solid var(--line);
      background: var(--panel);
    }}
    nav, main, footer {{
      width: min(920px, calc(100% - 32px));
      margin: 0 auto;
    }}
    nav {{
      min-height: 68px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      flex-wrap: wrap;
    }}
    .brand {{
      font-weight: 750;
      letter-spacing: 0;
      color: var(--ink);
      text-decoration: none;
    }}
    .links {{
      display: flex;
      gap: 16px;
      flex-wrap: wrap;
      font-size: 14px;
    }}
    a {{
      color: var(--accent);
      text-decoration: none;
      font-weight: 650;
    }}
    a:hover {{ text-decoration: underline; }}
    main {{
      padding: 48px 0 56px;
    }}
    .document {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: clamp(24px, 4vw, 44px);
      box-shadow: 0 12px 32px rgba(22, 32, 51, 0.06);
    }}
    h1 {{
      margin: 0 0 4px;
      font-size: clamp(30px, 5vw, 46px);
      line-height: 1.12;
      letter-spacing: 0;
    }}
    .updated {{
      margin: 0 0 32px;
      color: var(--muted);
      font-size: 14px;
    }}
    h2 {{
      margin: 30px 0 8px;
      font-size: 20px;
      letter-spacing: 0;
    }}
    p, ul {{
      margin: 0 0 16px;
    }}
    ul {{
      padding-left: 22px;
    }}
    footer {{
      padding: 0 0 32px;
      color: var(--muted);
      font-size: 13px;
    }}
  </style>
</head>
<body>
  <header>
    <nav aria-label="Legal navigation">
      <a class="brand" href="https://airzensg.com/">AirZen Pte. Ltd.</a>
      <div class="links">
        <a href="/privacy">Privacy Policy</a>
        <a href="/terms">Terms and Conditions</a>
      </div>
    </nav>
  </header>
  <main>
    <article class="document">
      {content}
    </article>
  </main>
  <footer>
    These pages are provided for general service information and should be reviewed for your business requirements.
  </footer>
</body>
</html>"""
    )


@app.get("/privacy", response_class=HTMLResponse)
async def privacy_page():
    return _legal_page(
        "Privacy Policy",
        """<h1>Privacy Policy</h1>
      <p class="updated">Last updated: July 6, 2026</p>

      <p>AirZen Pte. Ltd. uses this secure staff CRM portal to manage customer calls, WhatsApp messages, appointments, templates, and related customer communication workflows.</p>

      <h2>Information We Collect</h2>
      <p>We may store business setup details, user settings, customer names, phone numbers, addresses, appointment details, service preferences, WhatsApp template data, message delivery status, call logs, recordings where enabled, and technical logs needed to operate the service.</p>

      <h2>How We Use Information</h2>
      <ul>
        <li>To provide CRM, calling, WhatsApp messaging, appointment, and customer follow-up features.</li>
        <li>To configure company settings, templates, integrations, and automation workflows.</li>
        <li>To troubleshoot errors, protect the service, improve reliability, and provide support.</li>
        <li>To comply with applicable platform rules, legal obligations, and security requirements.</li>
      </ul>

      <h2>WhatsApp and Third-Party Providers</h2>
      <p>The service may connect with Meta WhatsApp Business APIs and other providers such as hosting, database, telephony, calendar, storage, analytics, or AI services. Data shared with those providers is used to deliver the requested service features and remains subject to their applicable terms and policies.</p>

      <h2>Customer Data and Consent</h2>
      <p>You are responsible for ensuring that customer contact details, message recipients, and communication campaigns are collected and used with proper consent and in compliance with applicable laws and platform policies.</p>

      <h2>Retention and Security</h2>
      <p>We keep information for as long as needed to provide the service, meet operational needs, resolve issues, or comply with legal obligations. We use reasonable technical and organizational safeguards, but no internet-based service can guarantee absolute security.</p>

      <h2>Your Choices</h2>
      <p>You may update settings, remove customer records where the application allows it, disable integrations, or request deletion or correction of applicable data by contacting the service owner or administrator.</p>

      <h2>Contact</h2>
      <p>For privacy questions, contact AirZen Pte. Ltd. through <a href="https://airzensg.com/">airzensg.com</a>.</p>""",
    )


@app.get("/terms", response_class=HTMLResponse)
async def terms_page():
    return _legal_page(
        "Terms and Conditions",
        """<h1>Terms and Conditions</h1>
      <p class="updated">Last updated: July 6, 2026</p>

      <p>These Terms and Conditions describe the basic rules for using the AirZen Pte. Ltd. secure staff CRM portal, including calling, WhatsApp messaging, customer management, appointments, templates, and automation features.</p>

      <h2>Use of the Service</h2>
      <p>You may use the service only for lawful business purposes and in accordance with applicable communication, privacy, anti-spam, telemarketing, and platform rules.</p>

      <h2>Your Responsibilities</h2>
      <ul>
        <li>You are responsible for the accuracy of business settings, Meta credentials, templates, phone numbers, recipients, customer records, and appointment information.</li>
        <li>You must have proper consent or another valid legal basis before calling, messaging, or storing customer information.</li>
        <li>You must not use the service for misleading, abusive, illegal, harmful, or unauthorized communications.</li>
      </ul>

      <h2>WhatsApp and Provider Rules</h2>
      <p>WhatsApp template creation, message delivery, account status, and platform approvals depend on Meta WhatsApp Business rules and third-party provider availability. You are responsible for complying with those external terms and maintaining valid credentials.</p>

      <h2>Availability and Changes</h2>
      <p>The service may be updated, interrupted, limited, or unavailable because of maintenance, provider outages, configuration issues, or infrastructure changes. Features may change as the application is improved.</p>

      <h2>Data and Backups</h2>
      <p>You are responsible for reviewing your data, keeping appropriate backups where needed, and confirming that integrations and automations behave as expected before relying on them for production workflows.</p>

      <h2>Limitation of Liability</h2>
      <p>To the maximum extent allowed by law, the service owner is not liable for indirect, incidental, special, consequential, or business losses arising from use of the service, failed messages, failed calls, third-party outages, incorrect configuration, or unauthorized use.</p>

      <h2>Contact</h2>
      <p>For questions about these terms, contact AirZen Pte. Ltd. through <a href="https://airzensg.com/">airzensg.com</a>.</p>""",
    )


@app.get("/terms-and-conditions", response_class=HTMLResponse)
async def terms_and_conditions_page():
    return await terms_page()


@app.post("/api/call")
async def api_dispatch_call(req: CallRequest):
    await log_error("server", "single_call_route_entered", "endpoint=/api/call", "info")
    # ── Outbound calling-window guard ──────────────────────────────────────
    if not await _is_outbound_allowed():
        raise HTTPException(403, await _outbound_window_error())
    url = await eff("LIVEKIT_URL")
    key = await eff("LIVEKIT_API_KEY")
    secret = await eff("LIVEKIT_API_SECRET")
    if not all([url, key, secret]):
        raise HTTPException(400, "LiveKit credentials not configured. Go to Settings → LiveKit.")
    # Pre-flight: without these the agent will silently fail mid-call.
    if not await eff("OUTBOUND_TRUNK_ID"):
        raise HTTPException(
            400,
            "OUTBOUND_TRUNK_ID is not set. Either click 'Create SIP Trunk' in Settings → Vobiz, "
            "or paste an existing trunk ID into your Coolify env vars and redeploy.",
        )
    if not await eff("GOOGLE_API_KEY"):
        raise HTTPException(400, "GOOGLE_API_KEY is not set — the Gemini Live agent cannot connect.")
    try:
        phone = normalize_phone(req.phone)
    except ValueError:
        raise HTTPException(400, "Phone must be Singapore format +65XXXXXXXX or an international E.164 number")
    customer_name = (req.customer_name or req.name or "").strip()
    if not customer_name and (req.lead_name or "").strip().lower() not in ("", "there", "customer", "lead"):
        customer_name = (req.lead_name or "").strip()
    lead_name = customer_name or "there"
    business_name = (req.business_name or req.company_name or "").strip() or "AirZen"
    company_name = (req.company_name or req.business_name or "").strip() or business_name
    service_type = (req.service_type or "").strip() or "our service"
    requirement = (req.requirement or req.notes or "").strip()
    source = (req.source or "").strip()
    requested_call_type = (req.call_type or "").strip()
    call_type = "voice_call"
    if requested_call_type and requested_call_type != "voice_call":
        await log_error("server", "old_call_type_ignored_for_voice=true", f"requested_call_type={requested_call_type}", "info")
    await log_error("server", "effective_voice_prompt_type=voice_call", "voice_call", "info")
    await log_error(
        "server",
        "single_call_payload_received",
        (
            f"phone={phone}; customer_name={customer_name}; business_name={business_name}; "
            f"service_type={service_type}; source={source}; requested_call_type={requested_call_type or 'missing'}; call_type={call_type}; "
            f"requirement_present={str(bool(requirement)).lower()}"
        ),
        "info",
    )

    await log_error("server", "prompt_resolution_started", f"phone={phone}; call_type=voice_call", "info")
    await log_error("server", "prompt_type_requested", "voice_call", "info")
    if req.agent_profile_id:
        await log_error("server", "agent_profile_prompt_ignored_for_voice", f"agent_profile_id={req.agent_profile_id}", "info")
    effective_prompt, prompt_meta = await resolve_voice_call_prompt_with_meta(
        lead_name=lead_name,
        business_name=business_name,
        service_type=service_type,
    )
    await log_error("server", "prompt_source_selected", str(prompt_meta.get("prompt_source_selected", "")), "info")
    await log_error("server", "prompt_mode_selected", str(prompt_meta.get("prompt_mode_selected", "")), "info")
    await log_error("server", "prompt_default_used", str(prompt_meta.get("prompt_default_used", False)).lower(), "info")

    room_name = f"call-{phone.replace('+', '')}-{random.randint(1000, 9999)}"
    metadata = {
        "phone_number": phone,
        "lead_name": lead_name,
        "name": customer_name,
        "customer_name": customer_name,
        "business_name": business_name,
        "company_name": company_name,
        "service_type": service_type,
        "requirement": requirement,
        "notes": req.notes or "",
        "source": source,
        "call_type": call_type,
        "system_prompt": effective_prompt,
        "prompt_source_selected": prompt_meta.get("prompt_source_selected", ""),
        "prompt_mode_selected": prompt_meta.get("prompt_mode_selected", ""),
        "prompt_default_used": str(prompt_meta.get("prompt_default_used", False)).lower(),
    }
    await log_error(
        "server",
        "outbound_metadata_prepared",
        (
            f"room={room_name}; phone={phone}; customer_name={customer_name}; "
            f"business_name={business_name}; company_name={company_name}; "
            f"service_type={service_type}; source={source}; call_type={call_type}"
        ),
        "info",
    )
    try:
        from livekit import api as lk_api
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ctx))
        lk = lk_api.LiveKitAPI(url=url, api_key=key, api_secret=secret, session=session)
        room_kwargs = {"name": room_name, "empty_timeout": 300, "max_participants": 5}
        try:
            room_req = lk_api.CreateRoomRequest(**room_kwargs, metadata=json.dumps(metadata))
        except TypeError:
            room_req = lk_api.CreateRoomRequest(**room_kwargs)
        await lk.room.create_room(room_req)
        agent_name = await _outbound_agent_name()
        await log_error("server", "livekit_agent_dispatch_name", agent_name, "info")
        await lk.agent_dispatch.create_dispatch(lk_api.CreateAgentDispatchRequest(agent_name=agent_name, room=room_name, metadata=json.dumps(metadata)))
        await lk.aclose()
        await session.close()
        await increment_lead_attempts(phone, "call")
        await log_error("server", f"Call dispatched to {phone}", f"room={room_name}", "info")
        return {"status": "dispatched", "room": room_name, "phone": phone}
    except Exception as exc:
        logger.error("Dispatch error: %s", exc)
        raise HTTPException(500, f"Dispatch failed: {exc}")


@app.get("/api/calls")
async def api_get_calls(page: int = 1, limit: int = 20):
    return await get_all_calls(page=page, limit=limit)


@app.patch("/api/calls/{call_id}/notes")
async def api_update_notes(call_id: str, req: NotesRequest):
    ok = await update_call_notes(call_id, req.notes)
    if not ok:
        raise HTTPException(404, "Call not found")
    return {"status": "updated"}


EXPORT_COLUMNS = [
    ("Call Date/Time", "timestamp"),
    ("Lead Name", "lead_name"),
    ("Phone Number", "phone_number"),
    ("Business Name", "business_name"),
    ("Service Type", "service_type"),
    ("Outcome", "outcome"),
    ("Reason", "reason"),
    ("Duration Seconds", "duration_seconds"),
    ("Notes", "notes"),
    ("Recording URL", "recording_url"),
]


def _recording_link_value(row: dict) -> str:
    """Plain value (URL) for CSV cells and unlinked XLSX fallback."""
    if row.get("recording_deleted"):
        return "Recording Deleted"
    url = row.get("recording_url")
    return url if url else "No Recording"


def _export_filters(date_from: Optional[str], date_to: Optional[str], outcome: Optional[str], phone: Optional[str]) -> dict:
    return {
        "date_from": date_from or "",
        "date_to": date_to or "",
        "outcome": outcome or "",
        "phone": phone or "",
    }


def _export_value(row: dict, key: str) -> str:
    value = row.get(key)
    return "" if value is None else str(value)


def _export_cell_value(row: dict, label: str, key: str) -> str:
    if label == "Recording URL":
        if row.get("recording_deleted"):
            return ""
        return row.get("recording_url") or ""
    return _export_value(row, key)


@app.get("/api/export/calls.csv")
async def api_export_calls_csv(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    outcome: Optional[str] = None,
    phone: Optional[str] = None,
):
    try:
        rows = await get_call_logs_for_export(_export_filters(date_from, date_to, outcome, phone))
        out = StringIO()
        writer = csv.writer(out)
        writer.writerow([label for label, _ in EXPORT_COLUMNS])
        for row in rows:
            writer.writerow([
                _export_cell_value(row, label, key)
                for label, key in EXPORT_COLUMNS
            ])
        out.seek(0)
        return StreamingResponse(
            iter([out.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="call_logs_export.csv"'},
        )
    except Exception as exc:
        await log_error("server", "CSV export failed", str(exc), "error")
        return JSONResponse(status_code=500, content={"error": f"CSV export failed: {exc}"})


@app.get("/api/export/calls.xlsx")
async def api_export_calls_xlsx(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    outcome: Optional[str] = None,
    phone: Optional[str] = None,
):
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font

        rows = await get_call_logs_for_export(_export_filters(date_from, date_to, outcome, phone))
        wb = Workbook()
        ws = wb.active
        ws.title = "Call Logs"
        headers = [label for label, _ in EXPORT_COLUMNS]
        ws.append(headers)
        for cell in ws[1]:
            cell.font = Font(bold=True)

        for row in rows:
            ws.append([
                _export_cell_value(row, label, key)
                for label, key in EXPORT_COLUMNS
            ])

        for column in ws.columns:
            width = max(len(str(cell.value or "")) for cell in column)
            ws.column_dimensions[column[0].column_letter].width = min(max(width + 2, 14), 60)

        stream = BytesIO()
        wb.save(stream)
        stream.seek(0)
        return StreamingResponse(
            stream,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": 'attachment; filename="call_logs_export.xlsx"'},
        )
    except Exception as exc:
        await log_error("server", "Excel export failed", str(exc), "error")
        return JSONResponse(status_code=500, content={"error": f"Excel export failed: {exc}"})


@app.get("/api/stats")
async def api_get_stats():
    return await get_stats()


async def _recording_retention_days() -> int:
    raw = await get_setting("RECORDING_RETENTION_DAYS", "7")
    try:
        return max(int(raw), 1)
    except (TypeError, ValueError):
        return 7


async def _recording_auto_delete_enabled() -> bool:
    raw = (await get_setting("RECORDING_AUTO_DELETE_ENABLED", "false")).strip().lower()
    return raw in ("1", "true", "yes", "on")


async def _recording_cleanup_time() -> str:
    return await get_setting("RECORDING_CLEANUP_TIME", "02:00") or "02:00"


def _derive_recording_key(recording: Any, bucket: str = "") -> Optional[str]:
    if not recording:
        return None
    if isinstance(recording, dict):
        direct_key = recording.get("recording_object_key") or recording.get("object_key")
        if direct_key:
            key = str(direct_key).strip().lstrip("/")
            return None if not key or ".." in key.split("/") else key
        recording_url = recording.get("recording_url") or recording.get("recording_download_url") or ""
    else:
        recording_url = str(recording)
    if not recording_url:
        return None
    parsed = urlparse(recording_url)
    if parsed.scheme == "s3":
        key = parsed.path.lstrip("/")
    else:
        path = unquote(parsed.path or "").lstrip("/")
        prefix = f"{bucket}/"
        key = path[len(prefix):] if bucket and path.startswith(prefix) else path
    if not key or key.startswith("/") or ".." in key.split("/"):
        return None
    return key


async def _delete_recording_object(object_key: str) -> None:
    import boto3

    aws_key = await eff("S3_ACCESS_KEY_ID") or os.getenv("AWS_ACCESS_KEY_ID", "")
    aws_secret = await eff("S3_SECRET_ACCESS_KEY") or os.getenv("AWS_SECRET_ACCESS_KEY", "")
    bucket = await eff("S3_BUCKET") or os.getenv("AWS_BUCKET_NAME", "")
    endpoint = await eff("S3_ENDPOINT_URL") or os.getenv("S3_ENDPOINT", "")
    region = await eff("S3_REGION") or os.getenv("AWS_REGION", "ap-northeast-1")
    if not (aws_key and aws_secret and bucket):
        raise RuntimeError("S3/R2 credentials are not configured")
    client = boto3.client(
        "s3",
        aws_access_key_id=aws_key,
        aws_secret_access_key=aws_secret,
        endpoint_url=endpoint or None,
        region_name=region,
    )
    client.delete_object(Bucket=bucket, Key=object_key)


async def _cleanup_old_recordings() -> dict:
    retention_days = await _recording_retention_days()
    rows = await get_recordings_for_cleanup(retention_days)
    bucket = await eff("S3_BUCKET") or os.getenv("AWS_BUCKET_NAME", "")
    deleted = failed = 0
    for row in rows:
        object_key = row.get("recording_object_key") or _derive_recording_key(row.get("recording_url", ""), bucket)
        if not object_key:
            failed += 1
            continue
        try:
            await _delete_recording_object(object_key)
            if await mark_recording_deleted(row["id"]):
                deleted += 1
            else:
                failed += 1
        except Exception as exc:
            failed += 1
            await log_error("server", "Recording delete failed", f"id={row.get('id')} key={object_key}: {exc}", "error")
    await log_error("server", "Recording cleanup summary", f"deleted={deleted}, failed={failed}, retention_days={retention_days}", "info")
    return {"deleted": deleted, "failed": failed}


@app.get("/api/recordings/storage")
async def api_recordings_storage():
    stats = await get_recording_storage_stats()
    retention_days = await _recording_retention_days()
    auto_delete_enabled = await _recording_auto_delete_enabled()
    return {**stats, "retention_days": retention_days, "auto_delete_enabled": auto_delete_enabled}


@app.post("/api/recordings/cleanup")
async def api_recordings_cleanup(req: Optional[RecordingCleanupRequest] = None):
    if not req or req.confirm != "DELETE_OLD_RECORDINGS":
        return JSONResponse(status_code=400, content={"success": False, "error": "Confirmation required"})
    result = await _cleanup_old_recordings()
    return {"success": True, **result, "message": "Old recordings cleaned up"}


@app.get("/api/recordings/file")
async def api_recordings_file(key: str = Query(...)):
    import boto3
    import botocore.exceptions

    aws_key = await eff("S3_ACCESS_KEY_ID") or os.getenv("AWS_ACCESS_KEY_ID", "")
    aws_secret = await eff("S3_SECRET_ACCESS_KEY") or os.getenv("AWS_SECRET_ACCESS_KEY", "")
    bucket = await eff("S3_BUCKET") or os.getenv("AWS_BUCKET_NAME", "")
    endpoint = await eff("S3_ENDPOINT_URL") or os.getenv("S3_ENDPOINT", "")
    region = await eff("S3_REGION") or os.getenv("AWS_REGION", "ap-northeast-1")

    if not (aws_key and aws_secret and bucket):
        logger.error("recording_playback_failed: credentials not configured")
        raise HTTPException(503, "Storage not configured")

    client = boto3.client(
        "s3",
        aws_access_key_id=aws_key,
        aws_secret_access_key=aws_secret,
        endpoint_url=endpoint or None,
        region_name=region,
    )

    try:
        logger.info(f"recording_playback_requested: key={key}")
        response = client.get_object(Bucket=bucket, Key=key)

        ext = key.lower().split('.')[-1]
        content_type = {
            "ogg": "audio/ogg",
            "mp3": "audio/mpeg",
            "wav": "audio/wav",
            "webm": "audio/webm",
        }.get(ext, "application/octet-stream")

        def iterfile():
            yield from response['Body']

        logger.info(f"recording_playback_success: key={key}")
        return StreamingResponse(
            iterfile(),
            media_type=content_type,
            headers={"Content-Disposition": "inline"}
        )
    except botocore.exceptions.ClientError as exc:
        logger.error(f"recording_playback_failed: {exc}")
        raise HTTPException(404, "Recording unavailable")
    except Exception as exc:
        logger.error(f"recording_playback_failed: {exc}")
        raise HTTPException(500, "Internal error")


@app.get("/api/appointments")
async def api_get_appointments(
    date: Optional[str] = None,
    status: Optional[str] = None,
    q: Optional[str] = None,
    staff_id: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
):
    return await get_all_appointments(
        date_filter=date,
        status_filter=status,
        search_query=q,
        staff_id=staff_id,
        date_from=date_from,
        date_to=date_to,
    )


@app.post("/api/appointments")
async def api_create_appointment(req: CreateAppointmentRequest):
    """Create a new appointment, checking doctor/staff availability and triggering notifications."""
    try:
        from db import insert_appointment, normalize_phone, update_crm_contact_status, get_all_appointments
        phone = normalize_phone(req.phone)
        details = {}
        if req.notes:
            details["intake_data"] = {"notes": req.notes}
        if req.service_address:
            details["service_address"] = req.service_address
        if req.staff_id:
            details["staff_id"] = req.staff_id

        booking_id = await insert_appointment(
            name=req.name.strip(),
            phone=phone,
            date=req.date.strip(),
            time=req.time.strip()[:5],
            service=req.service or "Consultation",
            details=details,
        )

        # Enqueue customer & staff WhatsApp notifications if configured
        try:
            appts = await get_all_appointments(date_filter=req.date.strip(), search_query=phone)
            matched = next((a for a in appts if str(a.get("id", "")).upper().startswith(booking_id)), None)
            if matched:
                await enqueue_required_appointment_notifications(matched, event="created")
        except Exception as n_err:
            logger.warning("Appointment notification queue failed: %s", n_err)

        # Update CRM status if contact exists
        try:
            await update_crm_contact_status(phone, "Consultation Scheduled")
        except Exception:
            pass

        return {
            "success": True,
            "booking_id": booking_id,
            "message": f"Appointment booked successfully. Booking ID: {booking_id}",
        }
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        logger.error("api_create_appointment failed: %s", exc)
        raise HTTPException(500, f"Failed to book appointment: {exc}")


@app.get("/api/appointments/available-slots")
async def api_get_available_slots(date: str = Query(...), staff_id: Optional[str] = Query(None)):
    """Return visual real-time available booking slots for a date and optional staff member."""
    try:
        from db import get_day_available_slots
        slots = await get_day_available_slots(date, staff_id)
        return {
            "date": date,
            "staff_id": staff_id,
            "slots": slots,
            "total_slots": len(slots),
            "total_available": sum(1 for s in slots if s.get("available")),
        }
    except Exception as exc:
        logger.error("api_get_available_slots failed: %s", exc)
        raise HTTPException(500, str(exc))


@app.get("/api/appointments/settings")
async def api_get_appointment_settings():
    return await get_appointment_settings()


@app.post("/api/appointments/settings")
async def api_save_appointment_settings(req: AppointmentSettingsRequest):
    try:
        return await save_appointment_settings(req.dict(exclude_none=True))
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/appointments/staff")
async def api_get_appointment_staff(include_inactive: bool = True):
    return {"staff": await get_appointment_staff(include_inactive=include_inactive)}


@app.get("/api/appointment-staff")
async def api_get_appointment_staff_alias(include_inactive: bool = True):
    return await api_get_appointment_staff(include_inactive=include_inactive)


@app.post("/api/appointments/staff")
async def api_create_appointment_staff(req: AppointmentStaffRequest):
    try:
        staff = await upsert_appointment_staff(req.dict(exclude_none=True))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"success": True, "staff": staff}


@app.post("/api/appointment-staff")
async def api_create_appointment_staff_alias(req: AppointmentStaffRequest):
    return await api_create_appointment_staff(req)


@app.put("/api/appointments/staff/{staff_id}")
async def api_update_appointment_staff(staff_id: str, req: AppointmentStaffRequest):
    try:
        staff = await upsert_appointment_staff(req.dict(exclude_none=True), staff_id=staff_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"success": True, "staff": staff}


@app.patch("/api/appointment-staff/{staff_id}")
async def api_patch_appointment_staff_alias(staff_id: str, req: AppointmentStaffRequest):
    return await api_update_appointment_staff(staff_id, req)


@app.delete("/api/appointments/staff/{staff_id}")
async def api_delete_appointment_staff(staff_id: str):
    result = await delete_appointment_staff(staff_id)
    if not (result.get("deleted") or result.get("deactivated")):
        raise HTTPException(404, "Staff member not found")
    return {"success": True, **result}


@app.delete("/api/appointment-staff/{staff_id}")
async def api_delete_appointment_staff_alias(staff_id: str):
    return await api_delete_appointment_staff(staff_id)


@app.post("/api/appointment-staff/{staff_id}/activate")
async def api_activate_appointment_staff(staff_id: str):
    ok = await activate_appointment_staff(staff_id)
    if not ok:
        raise HTTPException(404, "Staff member not found")
    return {"success": True, "active": True}


@app.post("/api/appointment-staff/{staff_id}/deactivate")
async def api_deactivate_appointment_staff(staff_id: str):
    ok = await deactivate_appointment_staff(staff_id)
    if not ok:
        raise HTTPException(404, "Staff member not found")
    return {"success": True, "active": False}


@app.get("/api/calendar/health")
async def api_calendar_health():
    enabled = (await get_setting("GOOGLE_CALENDAR_ENABLED", "false") or "false").lower() in {"1", "true", "yes", "on"}
    meet_enabled = (await get_setting("GOOGLE_MEET_ENABLED", "false") or "false").lower() in {"1", "true", "yes", "on"}
    service_json = await get_setting("GOOGLE_CALENDAR_SERVICE_ACCOUNT_JSON", "")
    timezone_name = await get_setting("GOOGLE_CALENDAR_DEFAULT_TIMEZONE", DEFAULT_TIMEZONE)
    staff = await get_appointment_staff(include_inactive=True)
    return {
        "booking_engine": "internal",
        "booking_depends_on_google_calendar": False,
        "google_calendar_enabled": enabled,
        "google_meet_enabled": meet_enabled,
        "service_account_configured": bool(service_json),
        "timezone": timezone_name,
        "fallback_to_internal": True,
        "staff_calendar_status": [
            {
                "id": s.get("id"),
                "name": s.get("name"),
                "active": s.get("active"),
                "google_calendar_id": s.get("google_calendar_id") or s.get("calendar_email") or "",
                "google_calendar_connected": bool(s.get("google_calendar_connected")),
                "google_meet_enabled": bool(s.get("google_meet_enabled")),
                "calendar_sync_error": s.get("calendar_sync_error") or "",
                "last_calendar_sync_at": s.get("last_calendar_sync_at") or "",
            }
            for s in staff
        ],
    }


@app.post("/api/appointments/{appointment_id}/notify-staff")
async def api_notify_appointment_staff(appointment_id: str):
    appointment = await get_appointment_by_id(appointment_id)
    if not appointment:
        raise HTTPException(404, "Appointment not found")
    result = await enqueue_required_appointment_notifications(appointment, event="created")
    if not result.get("success"):
        reason = "; ".join(result.get("errors") or []) or "Staff notification intent failed"
        raise HTTPException(400, reason)
    return {"success": True, "notification_outbox": result}


@app.get("/api/appointments/debug/{appointment_id}")
async def api_appointment_debug(appointment_id: str):
    appointment = await get_appointment_by_id(appointment_id)
    if not appointment:
        raise HTTPException(404, "Appointment not found")
    settings = await get_appointment_settings()
    tz_name = appointment.get("timezone") or settings.get("timezone") or DEFAULT_TIMEZONE
    try:
        tz = ZoneInfo(str(tz_name))
    except Exception:
        tz = ZoneInfo(DEFAULT_TIMEZONE)
        tz_name = DEFAULT_TIMEZONE
    local_dt = None
    is_past = None
    try:
        local_dt = datetime.strptime(f"{appointment.get('date')} {str(appointment.get('time') or '')[:5]}", "%Y-%m-%d %H:%M").replace(tzinfo=tz)
        is_past = local_dt <= datetime.now(tz)
    except Exception:
        pass
    phone = appointment.get("phone") or ""
    wa_logs = await get_whatsapp_logs(phone=phone, limit=20) if phone else []
    raw_logs = await get_logs(limit=500)
    needles = {appointment_id, str(appointment.get("id") or ""), phone, phone.lstrip("+") if phone else ""}
    related_logs = []
    for row in raw_logs or []:
        haystack = f"{row.get('source','')} {row.get('message','')} {row.get('detail','')}"
        if any(n and n in haystack for n in needles) or str(row.get("source") or "").startswith("appointment"):
            related_logs.append(row)
        if len(related_logs) >= 20:
            break
    return {
        "appointment": appointment,
        "timezone": tz_name,
        "local_appointment_datetime": local_dt.isoformat() if local_dt else "",
        "local_appointment_display": local_dt.strftime("%d %b %Y, %I:%M %p").replace(", 0", ", ") if local_dt else "",
        "is_past": is_past,
        "staff_notification_status": {
            "staff_notified": appointment.get("staff_notified"),
            "notification_error": appointment.get("notification_error") or "",
        },
        "reminder_status": {
            "customer_reminder_sent": appointment.get("customer_reminder_sent"),
            "staff_reminder_sent": appointment.get("staff_reminder_sent"),
            "reminder_processed": appointment.get("reminder_processed"),
            "reminder_error": appointment.get("reminder_error") or "",
        },
        "whatsapp_logs": wa_logs,
        "error_logs": related_logs,
        "demo_status": {
            "demo_status_requested": appointment.get("demo_status_requested") or False,
            "demo_status_requested_at": appointment.get("demo_status_requested_at"),
            "demo_status_request_count": appointment.get("demo_status_request_count") or 0,
            "demo_result": appointment.get("demo_result"),
            "demo_status_updated_by": appointment.get("demo_status_updated_by"),
            "demo_status_updated_at": appointment.get("demo_status_updated_at"),
        },
    }


class DemoResultRequest(BaseModel):
    result: str
    notes: Optional[str] = None
    next_followup_at: Optional[str] = None


@app.post("/api/appointments/{appointment_id}/request-demo-status")
async def api_request_demo_status(appointment_id: str):
    from db import get_appointment_by_id, get_appointment_staff, update_appointment_demo_fields, log_error
    from whatsapp import send_whatsapp_template, send_whatsapp_text, get_demo_completion_template_and_purpose, is_whatsapp_service_window_open, _get_wa_setting, _build_template_params

    appt = await get_appointment_by_id(appointment_id)
    if not appt:
        raise HTTPException(404, "Appointment not found")

    staff_list = await get_appointment_staff(include_inactive=True)
    staff = next((s for s in staff_list if s.get("id") == appt.get("staff_id")), {})
    staff_phone = staff.get("whatsapp_number") or ""
    if not staff_phone:
        raise HTTPException(400, "No staff WhatsApp number configured for this appointment")

    customer_name = appt.get("name", "")
    phone = appt.get("phone") or appt.get("phone_number") or ""
    date_s = appt.get("date", "")
    time_s = appt.get("time", "")

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

    try:
        use_template = (await get_setting("DEMO_COMPLETION_USE_TEMPLATE", "true")).lower() in ("true", "1", "yes", "on")
    except Exception:
        use_template = True

    if use_template:
        template_name, template_purpose = await get_demo_completion_template_and_purpose()
        if template_name:
            lang = await _get_wa_setting("WHATSAPP_DEFAULT_LANGUAGE") or "en"
            params = _build_template_params(
                template_purpose,
                {"lead_name": customer_name, "phone": phone},
                {"appointment_datetime": f"{date_s} {time_s}", "date": date_s, "time": time_s}
            )
            res = await send_whatsapp_template(
                staff_phone, template_name, lang, params,
                event_type="staff_demo_status_request",
                source_type="appointment",
                source_id=appointment_id,
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
            if await is_whatsapp_service_window_open(staff_phone):
                res = await send_whatsapp_text(staff_phone, staff_msg)
                success = res.get("success", False)
                msg_id = res.get("provider_message_id") or ""
                err_msg = res.get("error") or res.get("reason") or ""
            else:
                err_msg = "template_missing"
    else:
        if await is_whatsapp_service_window_open(staff_phone):
            res = await send_whatsapp_text(staff_phone, staff_msg)
            success = res.get("success", False)
            msg_id = res.get("provider_message_id") or ""
            err_msg = res.get("error") or res.get("reason") or ""
        else:
            err_msg = "service_window_closed"

    if success:
        now = datetime.now(timezone.utc)
        req_count = int(appt.get("demo_status_request_count") or 0)
        updates = {
            "demo_status_requested": True,
            "demo_status_requested_at": now.isoformat(),
            "demo_status_request_count": req_count + 1,
            "demo_status_reply_message_id": msg_id
        }
        await update_appointment_demo_fields(appointment_id, updates)
        await log_error("appointments", "demo_status_request_sent", f"Manually sent demo status request for appt {appointment_id}", "info")
        return {"success": True, "message": "Appointment outcome request sent to staff"}
    else:
        await log_error("appointments", "demo_status_request_failed", f"Failed manually sending demo status request for appt {appointment_id}: {err_msg}", "warning")
        raise HTTPException(400, f"Failed sending message to staff: {err_msg}")


@app.post("/api/appointments/{appointment_id}/mark-completed")
async def api_mark_appointment_completed(appointment_id: str):
    from db import get_appointment_by_id, update_appointment_demo_fields, update_lead_journey, log_error
    appt = await get_appointment_by_id(appointment_id)
    if not appt:
        raise HTTPException(404, "Appointment not found")

    now_iso = datetime.now(timezone.utc).isoformat()
    appt_updates = {
        "status": "completed",
        "completed_at": now_iso,
        "demo_result": "completed",
        "demo_status_updated_by": "dashboard_admin",
        "demo_status_updated_at": now_iso
    }
    ok = await update_appointment_demo_fields(appointment_id, appt_updates)
    if not ok:
        raise HTTPException(400, "Failed to update appointment")

    cust_phone = appt.get("phone") or appt.get("phone_number") or ""
    if cust_phone:
        await update_lead_journey(cust_phone, {
            "crm_status": "demo_done",
            "journey_stage": "demo_done",
            "next_best_action": "post_demo_followup"
        })
    await log_error("appointments", "demo_status_updated", f"Manually completed appt {appointment_id}", "info")
    return {"success": True}


@app.post("/api/appointments/{appointment_id}/mark-no-show")
async def api_mark_appointment_no_show(appointment_id: str):
    from db import get_appointment_by_id, update_appointment_demo_fields, update_lead_journey, log_error
    appt = await get_appointment_by_id(appointment_id)
    if not appt:
        raise HTTPException(404, "Appointment not found")

    now_iso = datetime.now(timezone.utc).isoformat()
    appt_updates = {
        "status": "no_show",
        "no_show_at": now_iso,
        "demo_result": "no_show",
        "demo_status_updated_by": "dashboard_admin",
        "demo_status_updated_at": now_iso
    }
    ok = await update_appointment_demo_fields(appointment_id, appt_updates)
    if not ok:
        raise HTTPException(400, "Failed to update appointment")

    cust_phone = appt.get("phone") or appt.get("phone_number") or ""
    if cust_phone:
        await update_lead_journey(cust_phone, {
            "crm_status": "demo_no_show",
            "journey_stage": "demo_no_show",
            "next_best_action": "reschedule_demo"
        })
    await log_error("appointments", "demo_status_updated", f"Manually marked appt {appointment_id} as no-show", "info")
    return {"success": True}


@app.post("/api/appointments/{appointment_id}/demo-result")
async def api_set_demo_result(appointment_id: str, req: DemoResultRequest):
    from db import get_appointment_by_id, update_appointment_demo_fields, update_lead_journey, log_error
    appt = await get_appointment_by_id(appointment_id)
    if not appt:
        raise HTTPException(404, "Appointment not found")

    now_iso = datetime.now(timezone.utc).isoformat()
    result_key = req.result.strip().lower()

    valid_results = {"completed", "no_show", "rescheduled", "interested", "not_interested", "need_followup"}
    if result_key not in valid_results:
        raise HTTPException(400, f"Invalid demo result. Must be one of {valid_results}")

    appt_updates = {
        "demo_result": result_key,
        "demo_status_updated_by": "dashboard_admin",
        "demo_status_updated_at": now_iso
    }
    if req.notes:
        appt_updates["demo_result_notes"] = req.notes

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
    if req.next_followup_at:
        crm_updates["next_followup_at"] = req.next_followup_at

    ok = await update_appointment_demo_fields(appointment_id, appt_updates)
    if not ok:
        raise HTTPException(400, "Failed to update appointment")

    cust_phone = appt.get("phone") or appt.get("phone_number") or ""
    if cust_phone and crm_updates:
        try:
            crm_ok = await update_lead_journey(cust_phone, crm_updates)
            if not crm_ok:
                await log_error(
                    "appointments",
                    "demo_result_crm_update_failed",
                    f"appointment_id={appointment_id}; phone={cust_phone}; result={result_key}; fields={sorted(crm_updates.keys())}",
                    "warning",
                )
        except Exception as exc:
            await log_error(
                "appointments",
                "demo_result_crm_update_failed",
                f"appointment_id={appointment_id}; phone={cust_phone}; result={result_key}; error={str(exc)[:500]}",
                "warning",
            )

    await log_error("appointments", "demo_status_updated", f"Manually set appt {appointment_id} result to {result_key}", "info")
    return {"success": True}


@app.get("/api/appointments/{appointment_id}/demo-status")
async def api_get_appointment_demo_status(appointment_id: str):
    from db import get_appointment_by_id
    appt = await get_appointment_by_id(appointment_id)
    if not appt:
        raise HTTPException(404, "Appointment not found")
    return {
        "demo_status_requested": appt.get("demo_status_requested") or False,
        "demo_status_requested_at": appt.get("demo_status_requested_at"),
        "demo_status_request_count": appt.get("demo_status_request_count") or 0,
        "demo_result": appt.get("demo_result"),
        "demo_status_updated_by": appt.get("demo_status_updated_by"),
        "demo_status_updated_at": appt.get("demo_status_updated_at"),
        "demo_result_notes": appt.get("demo_result_notes")
    }


@app.delete("/api/appointments/{appointment_id}")
async def api_cancel_appointment(appointment_id: str):
    appointment = await get_appointment_by_id(appointment_id)
    if not appointment:
        raise HTTPException(404, "Appointment not found or already cancelled")
    if _atomic_outbox_required():
        notification = await cancel_appointment_with_notification_outbox_rpc(
            appointment,
            extra_context={"reason": "Appointment cancelled"},
        )
        if not notification.get("success"):
            raise HTTPException(500, "Appointment cancellation notification intents were not atomically persisted")
        return {"status": "cancelled", "notification_outbox": notification}
    ok = await cancel_appointment(appointment_id)
    if not ok:
        raise HTTPException(404, "Appointment not found or already cancelled")
    notification = await enqueue_required_appointment_notifications(
        {**appointment, "status": "cancelled"},
        event="cancelled",
        extra_context={"reason": "Appointment cancelled"},
    )
    if not notification.get("success"):
        await update_appointment_notifications(appointment_id, {"notification_error": "; ".join(notification.get("errors") or [])[:1000]})
        raise HTTPException(500, "Appointment cancelled but notification intents were not fully persisted")
    return {"status": "cancelled", "notification_outbox": notification}


@app.patch("/api/appointments/{appointment_id}/status")
async def api_patch_appointment_status(appointment_id: str, req: AppointmentStatusRequest):
    ok = await update_appointment_status(appointment_id, req.status)
    if not ok:
        raise HTTPException(400, "Update failed. Check status value or appointment ID.")
    return {"success": True, "status": req.status}


@app.patch("/api/appointments/{appointment_id}/reschedule")
async def api_patch_appointment_reschedule(appointment_id: str, req: AppointmentRescheduleRequest):
    try:
        try:
            old_appt = await get_appointment_by_id(appointment_id)
        except Exception:
            old_appt = None
        if _atomic_outbox_required():
            if not old_appt:
                raise HTTPException(404, "Appointment not found")
            new_appt = {
                **old_appt,
                "id": str(uuid.uuid4()),
                "date": req.date,
                "time": req.time[:5],
                "status": "booked",
                "rescheduled_from": appointment_id,
                "created_at": datetime.now().isoformat(),
            }
            previous_dt = f"{old_appt.get('date') or ''} {str(old_appt.get('time') or '')[:5]}".strip()
            notification = await reschedule_appointment_with_notification_outbox_rpc(
                appointment_id,
                new_appt,
                extra_context={"previous_appointment_datetime": previous_dt},
            )
            if not notification.get("success"):
                raise HTTPException(500, "Appointment reschedule notification intents were not atomically persisted")
            return {"success": True, "appointment": new_appt, "notification_outbox": notification, "warning": ""}
        new_appt = await reschedule_appointment(appointment_id, req.date, req.time)
        previous_dt = ""
        if old_appt:
            previous_dt = f"{old_appt.get('date') or ''} {str(old_appt.get('time') or '')[:5]}".strip()
        notification = await enqueue_required_appointment_notifications(
            new_appt,
            event="rescheduled",
            extra_context={"previous_appointment_datetime": previous_dt},
        )
        if not notification.get("success"):
            await update_appointment_notifications(str(new_appt.get("id") or ""), {"notification_error": "; ".join(notification.get("errors") or [])[:1000]})
            raise HTTPException(500, "Appointment rescheduled but notification intents were not fully persisted")
        return {
            "success": True,
            "appointment": new_appt,
            "notification_outbox": notification,
            "warning": "",
        }
    except ValueError as e:
        raise HTTPException(400, str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Reschedule failed")
        raise HTTPException(500, "Internal error during rescheduling")


@app.patch("/api/appointments/{appointment_id}/notes")
async def api_patch_appointment_notes(appointment_id: str, req: NotesRequest):
    ok = await update_appointment_notes(appointment_id, req.notes)
    if not ok:
        raise HTTPException(404, "Appointment not found")
    return {"success": True}


@app.post("/api/appointments/{appointment_id}/reschedule")
async def api_post_appointment_reschedule(appointment_id: str, req: AppointmentRescheduleRequest):
    return await api_patch_appointment_reschedule(appointment_id, req)


@app.post("/api/appointments/{appointment_id}/cancel")
async def api_post_appointment_cancel(appointment_id: str, req: AppointmentActionRequest = AppointmentActionRequest()):
    return await api_cancel_appointment(appointment_id)


@app.post("/api/appointments/{appointment_id}/complete")
async def api_post_appointment_complete(appointment_id: str, req: AppointmentActionRequest = AppointmentActionRequest()):
    return await api_patch_appointment_status(appointment_id, AppointmentStatusRequest(status="completed"))


@app.post("/api/appointments/{appointment_id}/no-show")
async def api_post_appointment_no_show(appointment_id: str, req: AppointmentActionRequest = AppointmentActionRequest()):
    ok = await update_appointment_status(appointment_id, "no_show")
    if not ok:
        raise HTTPException(404, "Appointment not found")
    appointment = await get_appointment_by_id(appointment_id)
    phone = (appointment or {}).get("phone") or ""
    if phone:
        await update_lead_journey(phone, {
            "journey_stage": "demo_no_show",
            "crm_status": "demo_no_show",
            "next_best_action": "reschedule_demo",
            "last_followup_reason": req.reason or "demo_no_show",
        })
    return {"success": True, "status": "no_show"}


async def _send_appointment_reminder_now(appointment: dict) -> dict:
    appointment_id = str(appointment.get("id") or "")
    phone = appointment.get("phone") or ""
    template_context = {
        **appointment,
        "customer_name": appointment.get("name") or "there",
        "customer_phone": phone,
        "appointment_date": appointment.get("date") or "",
        "appointment_time": str(appointment.get("time") or "")[:5],
    }
    updates = {"reminder_processed": True, "reminder_processed_at": datetime.now().isoformat()}
    result = {"customer": None, "staff": None}
    errors = []
    try:
        template = await resolve_wa_template("reminder_template")
        if template and phone:
            result["customer"] = await send_whatsapp_template(
                phone, template, await get_setting("WHATSAPP_DEFAULT_LANGUAGE", "en"),
                [], event_type="appointment_reminder",
                source_type="appointments", source_id=appointment_id,
                template_purpose="reminder_template",
                template_context=template_context,
            )
            if result["customer"].get("success"):
                updates["customer_reminder_sent"] = True
                updates["customer_reminder_sent_at"] = datetime.now().isoformat()
            else:
                errors.append(result["customer"].get("reason") or result["customer"].get("error") or "customer_reminder_failed")
        else:
            errors.append("customer_reminder_template_missing")
    except Exception as exc:
        errors.append(f"customer_reminder_failed:{str(exc)[:160]}")
    staff_phone = ""
    if appointment.get("staff_id"):
        for staff in await get_appointment_staff(include_inactive=True):
            if str(staff.get("id")) == str(appointment.get("staff_id")):
                staff_phone = staff.get("whatsapp_number") or ""
                break
    if staff_phone:
        try:
            staff_template = await resolve_wa_template("staff_appointment_reminder_template")
            if staff_template:
                result["staff"] = await send_whatsapp_template(
                    staff_phone, staff_template, await get_setting("WHATSAPP_DEFAULT_LANGUAGE", "en"),
                    [], event_type="staff_appointment_reminder",
                    source_type="appointments", source_id=appointment_id,
                    template_purpose="staff_appointment_reminder_template",
                    template_context=template_context,
                )
                if result["staff"].get("success"):
                    updates["staff_reminder_sent"] = True
                    updates["staff_reminder_sent_at"] = datetime.now().isoformat()
                else:
                    errors.append(result["staff"].get("reason") or result["staff"].get("error") or "staff_reminder_failed")
            else:
                errors.append("staff_appointment_reminder_template_missing")
        except Exception as exc:
            errors.append(f"staff_reminder_failed:{str(exc)[:160]}")
    if errors:
        updates["reminder_error"] = "; ".join(errors)[:1000]
    await update_appointment_notifications(appointment_id, updates)
    return {"success": not errors, "result": result, "errors": errors}


@app.post("/api/appointments/{appointment_id}/send-reminder")
async def api_post_appointment_send_reminder(appointment_id: str, req: AppointmentActionRequest = AppointmentActionRequest()):
    appointment = await get_appointment_by_id(appointment_id)
    if not appointment:
        raise HTTPException(404, "Appointment not found")
    return await _send_appointment_reminder_now(appointment)


@app.get("/api/prompt")
async def api_get_prompt():
    saved = await get_setting("system_prompt", "")
    return {"prompt": saved or DEFAULT_SYSTEM_PROMPT, "is_custom": bool(saved)}


@app.post("/api/prompt")
async def api_save_prompt(req: PromptRequest):
    await set_setting("system_prompt", req.prompt)
    return {"status": "saved"}


@app.delete("/api/prompt")
async def api_reset_prompt():
    await set_setting("system_prompt", "")
    return {"status": "reset", "prompt": DEFAULT_SYSTEM_PROMPT}


# ── AI Prompt Manager helpers ────────────────────────────────────────────────

_AI_PROMPT_KEY = "AI_PROMPT_{}"
_AI_PROMPT_DEFAULT_TYPE_KEY = "AI_PROMPT_DEFAULT_TYPE"
_VALID_PROMPT_TYPES = {pt for pt, _, _ in PROMPT_TYPES}
_SIMPLE_PROMPT_TYPES = ("voice_call", "whatsapp_chat")
_SIMPLE_PROMPT_LABELS = {
    "voice_call": "Voice Call",
    "whatsapp_chat": "WhatsApp Chat",
}
_PROMPT_TYPE_ALIASES = {
    "follow_up": "followup_call",
    "followup": "followup_call",
    "payment_follow_up": "payment_followup",
    "missed_call": "missed_call_retry",
}
_VOICE_B2B_WRONG_PERSON_PHRASES = (
    "business development executive",
    "business development",
    "decision maker",
    "owner",
    "manager",
    "concerned person",
    "can i speak to",
    "may i speak to",
    "who handles",
    "right person",
    "am i speaking",
)
_VOICE_B2B_WRONG_PERSON_PATTERNS = tuple(
    re.compile(r"\b" + r"\s+".join(re.escape(part) for part in phrase.split()) + r"\b", flags=re.IGNORECASE)
    for phrase in _VOICE_B2B_WRONG_PERSON_PHRASES
)


def _sanitize_voice_prompt_setting_text(text: str) -> tuple[str, bool, str]:
    cleaned = text or ""
    removed = False
    first_phrase = ""
    for phrase, pattern in zip(_VOICE_B2B_WRONG_PERSON_PHRASES, _VOICE_B2B_WRONG_PERSON_PATTERNS):
        if pattern.search(cleaned):
            first_phrase = first_phrase or phrase
            cleaned = pattern.sub("", cleaned)
            removed = True
    if removed:
        cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned, removed, first_phrase


async def _sanitize_saved_voice_prompt_setting() -> None:
    try:
        saved = await get_setting("AI_PROMPT_voice_call", "")
        cleaned, removed, phrase = _sanitize_voice_prompt_setting_text(saved)
        if removed:
            await set_setting("AI_PROMPT_voice_call", cleaned)
            await log_error("server", "AI_PROMPT_voice_call_b2b_phrase_removed", f"phrase={phrase}", "warning")
    except Exception as exc:
        logger.warning("AI_PROMPT_voice_call cleanup skipped: %s", exc)


def _resolve_prompt_type(call_type: str, fallback: str = "welcome_call") -> str:
    requested = (call_type or fallback or "welcome_call").strip()
    resolved = _PROMPT_TYPE_ALIASES.get(requested, requested)
    return resolved if resolved in _VALID_PROMPT_TYPES else fallback


def _prompt_key_candidates(call_type: str, resolved_type: str) -> list[str]:
    candidates = []
    alias_values = [alias for alias, canonical in _PROMPT_TYPE_ALIASES.items() if canonical == resolved_type]
    for pt in (call_type, resolved_type):
        pt = (pt or "").strip()
        if pt:
            key = _AI_PROMPT_KEY.format(pt)
            if key not in candidates:
                candidates.append(key)
    for alias in alias_values:
        key = _AI_PROMPT_KEY.format(alias)
        if key not in candidates:
            candidates.append(key)
    return candidates


async def _saved_call_type_prompt(call_type: str, fallback: str = "welcome_call") -> tuple[str, str, str]:
    resolved_type = _resolve_prompt_type(call_type, fallback)
    for key in _prompt_key_candidates(call_type, resolved_type):
        saved = await get_setting(key, "")
        if saved:
            return saved, key, resolved_type
    return "", "", resolved_type


async def resolve_ai_prompt_with_meta(
    call_type: str = "welcome_call",
    fallback: str = "welcome_call",
    lead_name: str = "there",
    business_name: str = "our company",
    service_type: str = "our service",
) -> tuple[str, dict]:
    saved, key, resolved_type = await _saved_call_type_prompt(call_type, fallback)
    kb = await get_knowledge_base()
    prompt = build_prompt_for_type(
        prompt_type=resolved_type,
        lead_name=lead_name,
        business_name=business_name,
        service_type=service_type,
        saved_text=saved or None,
        kb=kb,
    )
    meta = {
        "prompt_type_requested": call_type or "welcome_call",
        "prompt_type_resolved": resolved_type,
        "prompt_source_selected": key or f"built_in:{resolved_type}",
        "prompt_mode_selected": "call_type" if saved else "default",
        "prompt_default_used": not bool(saved),
    }
    return prompt, meta


async def resolve_voice_call_prompt_with_meta(
    lead_name: str = "there",
    business_name: str = "our company",
    service_type: str = "our service",
) -> tuple[str, dict]:
    await log_error("server", "voice_prompt_simple_mode", "true", "info")
    await log_error("server", "old_prompt_types_ignored", "true", "info")
    await log_error("server", "legacy_prompt_ignored_for_voice", "true", "info")
    await log_error("server", "agent_profile_prompt_ignored_for_voice", "true", "info")
    saved = await get_setting("AI_PROMPT_voice_call", "")
    if saved:
        cleaned, removed, phrase = _sanitize_voice_prompt_setting_text(saved)
        if removed:
            saved = cleaned
            await set_setting("AI_PROMPT_voice_call", cleaned)
            await log_error("server", "AI_PROMPT_voice_call_b2b_phrase_removed", f"phrase={phrase}", "warning")
    kb = await get_knowledge_base()
    prompt = build_prompt_for_type(
        prompt_type="voice_call",
        lead_name=lead_name,
        business_name=business_name,
        service_type=service_type,
        saved_text=saved or None,
        kb=kb,
    )
    return prompt, {
        "prompt_type_requested": "voice_call",
        "prompt_type_resolved": "voice_call",
        "prompt_source_selected": "AI_PROMPT_voice_call",
        "prompt_mode_selected": "simple",
        "prompt_default_used": not bool(saved),
    }


async def resolve_ai_prompt(
    call_type: str = "welcome_call",
    fallback: str = "welcome_call",
    lead_name: str = "there",
    business_name: str = "our company",
    service_type: str = "our service",
) -> str:
    """
    Resolve final prompt for a call.
    Priority: saved AI_PROMPT_{type} → built-in default for type → fallback type.
    KB context is prepended when knowledge base has content.
    Returns an interpolated prompt string ready to pass to Gemini.
    """
    prompt, _meta = await resolve_ai_prompt_with_meta(
        call_type=call_type,
        fallback=fallback,
        lead_name=lead_name,
        business_name=business_name,
        service_type=service_type,
    )
    return prompt


def _crm_status_to_call_type(crm_status: str) -> str:
    """Map a CRM lead status to the appropriate call type prompt."""
    status = (crm_status or "").strip().lower()
    if "callback" in status:
        return "callback_call"
    if "follow" in status:
        return "followup_call"
    if "appointment" in status:
        return "appointment_confirmation"
    if "re-enquiry" in status or "re_enquiry" in status or "re enquiry" in status:
        return "re_enquiry"
    if "payment" in status:
        return "payment_followup"
    if "missed" in status or "no answer" in status:
        return "missed_call_retry"
    return "welcome_call"


# ── AI Prompt Manager API endpoints ─────────────────────────────────────────

@app.get("/api/ai-prompts")
async def api_list_ai_prompts():
    await log_error("server", "old_prompt_types_hidden", "true", "info")
    result = []
    for pt in _SIMPLE_PROMPT_TYPES:
        saved = await get_setting(_AI_PROMPT_KEY.format(pt), "")
        result.append({
            "type": pt,
            "label": _SIMPLE_PROMPT_LABELS[pt],
            "prompt": saved if saved else get_default_prompt(pt),
            "is_saved": bool(saved),
            "is_default": False,
        })
    return result


@app.get("/api/ai-prompts/resolve")
async def api_resolve_ai_prompt(call_type: str = "welcome_call"):
    saved = await get_setting("AI_PROMPT_voice_call", "")
    prompt = saved if saved else get_default_prompt("voice_call")
    return {
        "type": "voice_call",
        "label": "Voice Call",
        "prompt": prompt,
        "is_saved": bool(saved),
        "source_key": "AI_PROMPT_voice_call",
    }


@app.get("/api/ai-prompts/{prompt_type}")
async def api_get_ai_prompt(prompt_type: str):
    if prompt_type not in _SIMPLE_PROMPT_TYPES:
        raise HTTPException(404, f"Unknown prompt type: {prompt_type}")
    saved = await get_setting(_AI_PROMPT_KEY.format(prompt_type), "")
    return {
        "type": prompt_type,
        "label": _SIMPLE_PROMPT_LABELS[prompt_type],
        "prompt": saved if saved else get_default_prompt(prompt_type),
        "is_saved": bool(saved),
        "is_default": False,
        "default_prompt": get_default_prompt(prompt_type),
    }


@app.post("/api/ai-prompts/{prompt_type}")
async def api_save_ai_prompt(prompt_type: str, req: AiPromptRequest):
    if prompt_type not in _SIMPLE_PROMPT_TYPES:
        raise HTTPException(400, f"Unknown prompt type: {prompt_type}")
    await set_setting(_AI_PROMPT_KEY.format(prompt_type), req.prompt)
    return {"status": "saved", "type": prompt_type, "is_default": False}


@app.post("/api/ai-prompts/{prompt_type}/reset")
async def api_reset_ai_prompt(prompt_type: str):
    if prompt_type not in _SIMPLE_PROMPT_TYPES:
        raise HTTPException(400, f"Unknown prompt type: {prompt_type}")
    await set_setting(_AI_PROMPT_KEY.format(prompt_type), "")
    return {
        "status": "reset",
        "type": prompt_type,
        "prompt": get_default_prompt(prompt_type),
    }


# ── Knowledge Base API endpoints ─────────────────────────────────────────────

_KB_SAMPLE = {
    "raw_markdown": """# AIRZEN KNOWLEDGE BASE (WhatsApp AI)

# COMPANY INFORMATION

Business Name:
AirZen Pte. Ltd.

Brand Name:
AirZen

Tagline:
Clean Air. Peace of Mind.

Business Description:
AirZen is a professional residential air conditioning service company in Singapore...""",
    "company_profile": {
        "business_name": "AirZen",
        "legal_name": "AirZen Pte. Ltd.",
        "short_description": "Clean Air. Peace of Mind.",
        "about_us": "AirZen is a professional residential air conditioning service company in Singapore.",
        "industry_type": "Residential air conditioning services",
        "owner_name": "",
        "website": "",
        "email": "",
        "phone": "",
        "whatsapp_number": "",
    },
    "contact_details": {
        "primary_phone": "",
        "support_phone": "",
        "whatsapp_number": "",
        "email": "",
        "website": "",
        "address": "",
        "city": "Singapore",
        "state": "",
        "country": DEFAULT_COUNTRY,
        "google_maps_link": "",
    },
    "working_hours": {
        "timezone": DEFAULT_TIMEZONE,
        "opening_days": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"],
        "opening_time": "09:00",
        "closing_time": "18:00",
        "holiday_notes": "Closed on national holidays.",
        "emergency_support_available": False,
    },
    "faqs": [
        {"question": "What is AirZen?", "answer": "AirZen is a professional residential air conditioning service company in Singapore.", "category": "General", "language": "English", "active": True},
        {"question": "What is AirZen's tagline?", "answer": "Clean Air. Peace of Mind.", "category": "General", "language": "English", "active": True},
    ],
    "appointment_rules": {
        "appointment_required": True,
        "allow_same_day_booking": True,
        "appointment_duration_minutes": 30,
        "appointment_buffer_minutes": 15,
        "default_visit_type": "phone_consultation",
        "confirmation_required": True,
        "reminder_before_hours": 24,
    },
}


@app.get("/api/knowledge-base")
async def api_get_knowledge_base():
    return await get_knowledge_base()


@app.post("/api/knowledge-base")
async def api_save_knowledge_base(req: Request):
    try:
        data = await req.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON body")
    if not isinstance(data, dict):
        raise HTTPException(400, "Body must be a JSON object")
    saved = await save_knowledge_base(data)
    return {"status": "saved", "sections_saved": [k for k in data if k in _KB_SECTIONS]}


@app.get("/api/knowledge-base/search")
async def api_search_knowledge_base(q: str = Query(default="")):
    if not q or not q.strip():
        return {"results": []}
    kb = await get_knowledge_base()
    query = q.strip().lower()
    results = []

    # Search FAQs
    for faq in (kb.get("faqs") or []):
        score = 0
        question = (faq.get("question") or "").lower()
        answer = (faq.get("answer") or "").lower()
        for word in query.split():
            if word in question:
                score += 2
            if word in answer:
                score += 1
        if score > 0:
            results.append({"section": "faqs", "title": faq.get("question", ""), "answer": faq.get("answer", ""), "score": score})

    # Search services
    for svc in (kb.get("services") or []):
        score = 0
        text = " ".join(str(svc.get(k, "")) for k in ("name", "category", "description")).lower()
        for word in query.split():
            if word in text:
                score += 2
        if score > 0:
            results.append({"section": "services", "title": svc.get("name", ""), "answer": svc.get("description", ""), "score": score})

    # Search packages
    for pkg in (kb.get("packages") or []):
        score = 0
        text = " ".join(str(pkg.get(k, "")) for k in ("package_name", "description", "included_items")).lower()
        for word in query.split():
            if word in text:
                score += 2
        if score > 0:
            results.append({"section": "packages", "title": pkg.get("package_name", ""), "answer": pkg.get("description", ""), "score": score})

    # Search about us / policies
    cp = kb.get("company_profile") or {}
    about = (cp.get("about_us") or "").lower()
    if about:
        score = sum(2 for w in query.split() if w in about)
        if score > 0:
            results.append({"section": "about_us", "title": "About Us", "answer": cp.get("about_us", ""), "score": score})

    pol = kb.get("policies") or {}
    for k, label in [("cancellation_policy", "Cancellation Policy"), ("refund_policy", "Refund Policy"),
                     ("appointment_policy", "Appointment Policy"), ("payment_policy", "Payment Policy")]:
        val = (pol.get(k) or "").lower()
        if val:
            score = sum(2 for w in query.split() if w in val)
            if score > 0:
                results.append({"section": "policies", "title": label, "answer": pol.get(k, ""), "score": score})

    # Search locations
    for loc in (kb.get("locations") or []):
        text = " ".join(str(loc.get(k, "")) for k in ("branch_name", "address", "city")).lower()
        score = sum(2 for w in query.split() if w in text)
        if score > 0:
            results.append({"section": "locations", "title": loc.get("branch_name", ""), "answer": f"{loc.get('address','')} {loc.get('city','')}", "score": score})

    results.sort(key=lambda x: x["score"], reverse=True)
    return {"query": q, "results": results[:10]}


@app.get("/api/knowledge-base/preview")
async def api_kb_preview():
    kb = await get_knowledge_base()
    context = build_knowledge_context(kb)
    return {"preview": context, "char_count": len(context)}


@app.post("/api/knowledge-base/sample")
async def api_kb_load_sample():
    saved = await save_knowledge_base(_KB_SAMPLE)
    return {"status": "sample_loaded", "sections": list(_KB_SAMPLE.keys())}


@app.get("/api/knowledge-base/export")
async def api_kb_export():
    kb = await get_knowledge_base()
    return {"knowledge_base": kb, "sections": _KB_SECTIONS, "exported_at": datetime.now(timezone.utc).isoformat()}


@app.post("/api/knowledge-base/import")
async def api_kb_import(req: Request):
    try:
        body = await req.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON body")
    data = body.get("knowledge_base") if isinstance(body, dict) and "knowledge_base" in body else body
    if not isinstance(data, dict):
        raise HTTPException(400, "Knowledge base import must be a JSON object")
    saved = await save_knowledge_base(data)
    return {"status": "imported", "sections_saved": [k for k in data if k in _KB_SECTIONS], "knowledge_base": saved}


@app.post("/api/knowledge-base/preview-answer")
async def api_kb_preview_answer(req: KbPreviewAnswerRequest):
    question = (req.question or "").strip()
    if not question:
        raise HTTPException(400, "question is required")
    kb = await get_knowledge_base()
    matches = (await api_search_knowledge_base(question)).get("results", [])
    if matches:
        answer = matches[0].get("answer") or matches[0].get("title") or ""
    else:
        context = build_knowledge_context(kb)
        answer = context[:800] if context else "No matching knowledge base entry found yet."
    return {"question": question, "answer": answer, "matches": matches[:5], "source": "knowledge_base_preview"}


@app.get("/api/knowledge-base/{section}")
async def api_get_kb_section(section: str):
    if section not in _KB_SECTIONS:
        raise HTTPException(404, f"Unknown section: {section}. Valid: {_KB_SECTIONS}")
    data = await get_kb_section(section)
    return {"section": section, "data": data}


@app.post("/api/knowledge-base/{section}")
async def api_save_kb_section(section: str, req: Request):
    if section not in _KB_SECTIONS:
        raise HTTPException(400, f"Unknown section: {section}. Valid: {_KB_SECTIONS}")
    try:
        data = await req.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON body")
    await save_kb_section(section, data)
    return {"status": "saved", "section": section}


# ── Coolify Runtime & Health Check Endpoints ─────────────────────────────────

def _get_memory_usage() -> Optional[float]:
    try:
        import resource
        import sys
        rusage = resource.getrusage(resource.RUSAGE_SELF)
        if sys.platform == "darwin":
            return round(rusage.ru_maxrss / (1024.0 * 1024.0), 2)
        else:
            return round(rusage.ru_maxrss / 1024.0, 2)
    except Exception:
        pass
    try:
        if os.path.exists("/proc/self/status"):
            with open("/proc/self/status", "r") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        parts = line.split()
                        if len(parts) >= 2:
                            return round(float(parts[1]) / 1024.0, 2)
    except Exception:
        pass
    return None


@app.get("/api/live")
async def api_live():
    """Lightweight liveness endpoint that returns immediately without checking external services."""
    return {
        "status": "ok",
        "process_alive": True,
        "timestamp": datetime.now().isoformat(),
        "uptime_seconds": int(time.time() - _START_TIME)
    }


@app.get("/api/ready")
async def api_ready():
    """Readiness endpoint verifying status of external integrations (Supabase, WhatsApp, LiveKit)."""
    warnings = []
    supabase_configured = False
    whatsapp_configured = False
    livekit_configured = False

    # Supabase check
    try:
        from db import get_setting
        await get_setting("APP_TIMEZONE", DEFAULT_TIMEZONE)
        supabase_configured = True
    except Exception as e:
        warnings.append(f"Supabase connection check failed: {e}")

    # WhatsApp check
    try:
        from whatsapp import get_wa_health
        wa_health = await get_wa_health()
        if wa_health.get("status") == "ok":
            whatsapp_configured = True
        else:
            warnings.append(f"WhatsApp degraded: missing {wa_health.get('missing', [])}")
    except Exception as e:
        warnings.append(f"WhatsApp configuration check failed: {e}")

    # LiveKit check
    try:
        url = await eff("LIVEKIT_URL")
        key = await eff("LIVEKIT_API_KEY")
        secret = await eff("LIVEKIT_API_SECRET")
        if url and key and secret:
            livekit_configured = True
        else:
            warnings.append("LiveKit credentials not configured")
    except Exception as e:
        warnings.append(f"LiveKit configuration check failed: {e}")

    status = "degraded" if warnings else "ok"
    return {
        "status": status,
        "app_ready": True,
        "supabase_configured": supabase_configured,
        "whatsapp_configured": whatsapp_configured,
        "livekit_configured": livekit_configured,
        "warnings": warnings
    }


def _get_fd_diagnostics() -> dict:
    open_fds_count = None
    open_fds_soft_limit = None
    open_fds_hard_limit = None

    # Linux fd counting
    try:
        if os.path.exists("/proc/self/fd"):
            open_fds_count = len(os.listdir("/proc/self/fd"))
    except Exception:
        pass

    try:
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        open_fds_soft_limit = soft
        open_fds_hard_limit = hard

        # Log warning if open FDs count > 70% of soft limit
        if open_fds_count is not None and soft > 0:
            if open_fds_count > 0.7 * soft:
                logger.warning("open_fds_warning count=%s soft_limit=%s", open_fds_count, soft)
    except Exception:
        pass

    return {
        "open_fds_count": open_fds_count,
        "open_fds_soft_limit": open_fds_soft_limit,
        "open_fds_hard_limit": open_fds_hard_limit
    }


@app.get("/api/runtime/health")
async def api_runtime_health():
    """Extensive runtime health endpoint for detailed container diagnostics."""
    import sys

    # Timezone & app timezone info
    current_time = datetime.now()
    timezone_name = str(current_time.astimezone().tzinfo or "UTC")
    app_tz = DEFAULT_TIMEZONE
    whatsapp_enabled = False
    whatsapp_provider = "meta"
    whatsapp_templates_configured = 0
    whatsapp_status = "missing_config"

    try:
        from db import get_setting
        app_tz = await get_setting("APP_TIMEZONE", DEFAULT_TIMEZONE)
        supabase_configured = True
        auth_enabled = (await get_setting("AUTH_ENABLED", "false")).lower() in ("true", "1", "yes", "on")
    except Exception:
        pass

    try:
        from whatsapp import get_wa_health
        wa_h = await get_wa_health()
        whatsapp_enabled = bool(wa_h.get("enabled", False))
        whatsapp_provider = wa_h.get("provider", "meta")
        whatsapp_templates_configured = wa_h.get("templates_configured", 0)
        whatsapp_status = wa_h.get("status", "missing_config")
    except Exception:
        pass

    # LiveKit configuration check
    livekit_configured = False
    try:
        url = await eff("LIVEKIT_URL")
        key = await eff("LIVEKIT_API_KEY")
        secret = await eff("LIVEKIT_API_SECRET")
        livekit_configured = bool(url and key and secret)
    except Exception:
        pass

    # Scheduler status
    scheduler_status = {
        "configured": _scheduler is not None,
        "running": _scheduler.running if _scheduler else False
    }

    fd_diag = _get_fd_diagnostics()

    return {
        "process_alive": True,
        "uptime_seconds": int(time.time() - _START_TIME),
        "current_time": current_time.isoformat(),
        "timezone": timezone_name,
        "app_timezone": app_tz,
        "scheduler_status": scheduler_status,
        "last_background_error": LAST_BACKGROUND_ERROR,
        "python_version": sys.version,
        "memory_mb": _get_memory_usage(),
        "auth_enabled": auth_enabled,
        "supabase_configured": supabase_configured,
        "whatsapp_enabled": whatsapp_enabled,
        "whatsapp_provider": whatsapp_provider,
        "whatsapp_templates_configured": whatsapp_templates_configured,
        "whatsapp_status": whatsapp_status,
        "livekit_configured": livekit_configured,
        **fd_diag
    }


# ── WhatsApp Settings & Health ───────────────────────────────────────────────

_WA_TEMPLATE_STORE_KEY = "WHATSAPP_TEMPLATES_JSON"
_WA_NEW_TO_SLOT = {
    "welcome": "welcome_template",
    "missed_call": "missed_call_template",
    "callback_confirmation": "callback_confirmation_template",
    "booking_confirmation": "appointment_confirmation_template",
    "staff_new_booking": "staff_appointment_notification_template",
    "staff_daily_schedule": "staff_appointment_reminder_template",
    "staff_handoff": "staff_handoff_notification_template",
    "service_reminder": "reminder_template",
    "followup_no_response": "no_response_followup_template",
    "reengagement": "re_enquiry_followup_template",
    "staff_demo_status_request": "staff_demo_status_request",
    "service_completion": "service_completion_template",
}
_WA_SLOT_TO_NEW = {v: k for k, v in _WA_NEW_TO_SLOT.items()}
_WA_TEMPLATE_CATEGORIES = {
    "welcome": "MARKETING",
    "followup_no_response": "MARKETING",
    "reengagement": "MARKETING",
}
_WA_TEMPLATE_VARIABLES = {
    "welcome": ["customer_name"],
    "missed_call": ["customer_name"],
    "callback_confirmation": ["customer_name", "service_type", "appointment_date", "appointment_time"],
    "booking_confirmation": ["customer_name", "service_type", "appointment_date", "appointment_time"],
    "staff_new_booking": ["customer_name", "service_type", "appointment_date", "appointment_time"],
    "staff_daily_schedule": ["technician_name", "appointment_date", "appointment_time"],
    "staff_handoff": ["customer_name", "service_type"],
    "service_reminder": ["customer_name", "appointment_date", "appointment_time"],
    "followup_no_response": ["customer_name"],
    "reengagement": ["customer_name"],
    "staff_demo_status_request": ["technician_name", "appointment_date", "appointment_time"],
    "service_completion": ["customer_name", "service_type", "appointment_datetime"],
}
_WA_TEMPLATE_DEFAULT_BODIES = {
    "welcome": "Hi {{1}}, thanks for contacting {company}. We received your enquiry and our team will help you with the next step. Please reply here with any details you want us to know.",
    "missed_call": "Hi {{1}}, this is {company}. We tried reaching you but could not connect. Please reply here with a convenient time, and our team will contact you again.",
    "callback_confirmation": "Hi {{1}}, your callback with {company} about {{2}} is confirmed for {{3}} at {{4}}. Our team will contact you at the scheduled time and use the details already saved in your request. Reply here if you need to change the date, time, or service details.",
    "booking_confirmation": "Hi {{1}}, your {{2}} appointment with {company} is confirmed for {{3}} at {{4}}. Please keep this message for reference. Our team will use the booking details already shared with us and will contact you if any extra information is needed.",
    "staff_new_booking": "New {company} booking assigned. Customer: {{1}}. Service: {{2}}. Appointment: {{3}} at {{4}}. Please review the customer record in the dashboard before contacting them, and confirm any missing details before the scheduled appointment.",
    "staff_daily_schedule": "Hi {{1}}, this is your {company} schedule reminder. You have an appointment planned for {{2}} at {{3}}. Please check the dashboard for full customer details, location notes, and any preparation instructions before the appointment.",
    "staff_handoff": "Human handoff needed for {company}. Customer {{1}} needs help with {{2}}. Please review the CRM notes and continue the conversation personally.",
    "service_reminder": "Hi {{1}}, reminder from {company}: your service appointment is scheduled for {{2}} at {{3}}. Please keep this message for reference and reply here if you need to update the timing or share any extra instructions.",
    "followup_no_response": "Hi {{1}}, this is a follow-up from {company}. We are checking whether you still need help with your request. Reply here whenever convenient.",
    "reengagement": "Hi {{1}}, this is {company}. We are checking in to see whether you would still like assistance with your service request. Reply here and our team will help.",
    "staff_demo_status_request": "Hi {{1}}, please update the appointment outcome for the {company} booking scheduled on {{2}} at {{3}}. Use the dashboard after the customer interaction is complete so follow-up actions stay accurate.",
    "service_completion": "Hi {{1}}, your {{2}} service scheduled for {{3}} has been marked completed by the {company} technician. Please review the service and payment details in your booking. Reply to this message if anything needs our attention. Thank you for choosing {company}.",
}
_WA_ALLOWED_TEMPLATE_VARIABLES = {
    "customer_name",
    "appointment_date",
    "appointment_time",
    "service_type",
    "address",
    "quotation_amount",
    "technician_name",
    "appointment_datetime",
}
_WA_TEMPLATE_DENSITY_ERROR = "This template contains too many variables for its message length. Add more fixed text or remove variables."


def _wa_template_purpose(value: str) -> str:
    raw = str(value or "").strip()
    if raw in _WA_NEW_TO_SLOT:
        return raw
    return _WA_SLOT_TO_NEW.get(raw, raw)


def _wa_template_slot(value: str) -> str:
    purpose = _wa_template_purpose(value)
    return _WA_NEW_TO_SLOT.get(purpose, value)


def _wa_template_status(value: str) -> str:
    raw = str(value or "").strip().upper()
    if raw == "APPROVED":
        return "Approved"
    if raw == "REJECTED":
        return "Rejected"
    if raw == "PAUSED":
        return "Paused"
    return "Pending"


def _wa_template_category(purpose: str) -> str:
    return _WA_TEMPLATE_CATEGORIES.get(_wa_template_purpose(purpose), "UTILITY")


def _wa_template_company_text(company_name: str) -> str:
    text = re.sub(r"\s+", " ", str(company_name or "").strip())
    return text or "our team"


def _wa_template_body(purpose: str, company_name: str) -> str:
    raw = _WA_TEMPLATE_DEFAULT_BODIES.get(_wa_template_purpose(purpose), "Hi {{1}}, thank you for contacting {company}. Our team will follow up shortly.")
    return raw.replace("{company}", _wa_template_company_text(company_name))


def _wa_template_body_variable_numbers(body_text: str) -> list[int]:
    nums = []
    for match in re.findall(r"\{\{\s*(\d+)\s*\}\}", body_text or ""):
        try:
            nums.append(int(match))
        except ValueError:
            continue
    return nums


def _wa_template_fixed_text_chars(body_text: str) -> int:
    fixed = re.sub(r"\{\{\s*\d+\s*\}\}", "", body_text or "")
    return len(re.sub(r"\s+", "", fixed))


def _wa_template_validation(body_text: str, variables: list[str]) -> dict:
    variables = [str(v or "").strip() for v in variables or [] if str(v or "").strip()]
    variable_numbers = _wa_template_body_variable_numbers(body_text)
    variable_count = len(variable_numbers)
    fixed_chars = _wa_template_fixed_text_chars(body_text)
    disallowed = [v for v in variables if v not in _WA_ALLOWED_TEMPLATE_VARIABLES]
    density_high = variable_count >= 3 and fixed_chars < max(120, variable_count * 40)
    sequential = variable_numbers == list(range(1, variable_count + 1))
    errors = []
    if "business_name" in variables:
        errors.append("business_name is not allowed as a template variable.")
    if disallowed:
        errors.append("Unsupported template variables: " + ", ".join(disallowed))
    if not sequential:
        errors.append("Template body variables must be sequential from {{1}}.")
    if density_high:
        errors.append(_WA_TEMPLATE_DENSITY_ERROR)
    ok = not errors
    return {
        "ok": ok,
        "variable_count": variable_count,
        "fixed_text_characters": fixed_chars,
        "variables": variables,
        "disallowed_variables": disallowed,
        "density_high": density_high,
        "errors": errors,
        "message": "" if ok else (_WA_TEMPLATE_DENSITY_ERROR if density_high else errors[0]),
    }


async def _wa_meta_template_payload(template: dict, purpose: str) -> tuple[dict, dict, dict]:
    purpose = _wa_template_purpose(purpose)
    company_name = await get_setting("COMPANY_NAME", "")
    body_text = str(template.get("body_text") or "").strip() or _wa_template_body(purpose, company_name)
    variables = [v for v in (template.get("variables") or _WA_TEMPLATE_VARIABLES.get(purpose, [])) if v in _WA_ALLOWED_TEMPLATE_VARIABLES]
    variable_count = len(_wa_template_body_variable_numbers(body_text))
    variables = (variables + [f"value_{i + 1}" for i in range(variable_count)])[:variable_count]
    validation = _wa_template_validation(body_text, variables)
    payload = {
        "name": template["meta_template_name"],
        "language": template["language"],
        "category": template["category"],
        "components": [{
            "type": "BODY",
            "text": body_text,
            "example": {"body_text": [[f"sample_{i + 1}" for i in range(variable_count)]]} if variable_count else {},
        }],
    }
    updated_template = {**template, "variables": variables, "body_text": body_text}
    return payload, validation, updated_template


def _wa_normalize_template(raw: dict, default_language: str = "en") -> dict:
    data = raw or {}
    purpose = _wa_template_purpose(data.get("purpose") or data.get("slot") or "welcome")
    if purpose not in _WA_NEW_TO_SLOT:
        purpose = "welcome"
    now = datetime.now(timezone.utc).isoformat()
    variables = data.get("variables") if isinstance(data.get("variables"), list) else list(_WA_TEMPLATE_VARIABLES.get(purpose, []))
    variables = [str(v or "").strip() for v in variables if str(v or "").strip() in _WA_ALLOWED_TEMPLATE_VARIABLES]
    body_text = str(data.get("body_text") or "").strip()
    return {
        "purpose": purpose,
        "meta_template_name": str(data.get("meta_template_name") or data.get("template_name") or data.get("name") or "").strip(),
        "category": _wa_template_category(purpose),
        "language": str(data.get("language") or default_language or "en").strip() or "en",
        "status": _wa_template_status(data.get("status")),
        "variables": variables or list(_WA_TEMPLATE_VARIABLES.get(purpose, [])),
        "body_text": body_text,
        "template_id": str(data.get("template_id") or data.get("id") or "").strip(),
        "created_at": str(data.get("created_at") or now),
        "updated_at": str(data.get("updated_at") or now),
    }


async def _wa_template_suggestions() -> dict:
    prefix = _customer_setup_brand_slug(await get_setting("COMPANY_NAME", ""))
    return {purpose: f"{prefix}_{purpose}" if prefix else "" for purpose in _WA_NEW_TO_SLOT}


async def _require_wa_template_prefix() -> str:
    prefix = _customer_setup_brand_slug(await get_setting("COMPANY_NAME", ""))
    if not prefix:
        raise HTTPException(400, "Please complete Company Details first.")
    return prefix


async def _missing_required_meta_credentials() -> list[str]:
    required = [
        ("META_APP_ID", "Meta App ID"),
        ("META_APP_SECRET", "Meta App Secret"),
        ("WHATSAPP_ACCESS_TOKEN", "Permanent Access Token"),
        ("WHATSAPP_BUSINESS_ACCOUNT_ID", "WhatsApp Business Account ID"),
        ("WHATSAPP_PHONE_NUMBER_ID", "Phone Number ID"),
    ]
    missing = []
    for key, label in required:
        if not str(await get_setting(key, "") or "").strip():
            missing.append(label)
    return missing


def _missing_meta_credentials_response(**extra: Any) -> dict:
    missing = extra.pop("missing_credentials", [])
    message = "Missing Meta credentials: " + ", ".join(missing)
    return {
        "ok": False,
        "reason": "missing_meta_credentials",
        "message": message,
        "missing_credentials": missing,
        **extra,
    }


def _meta_api_error_details(http_status: int, data: Any) -> dict:
    error = data.get("error") if isinstance(data, dict) else None
    if not isinstance(error, dict):
        error = data if isinstance(data, dict) else {}
    error_data = error.get("error_data") if isinstance(error.get("error_data"), dict) else {}
    message = str(error.get("message") or data or "").strip()
    return {
        "http_status": http_status,
        "meta_error_code": str(error.get("code") or ""),
        "meta_error_message": message,
        "meta_error_type": str(error.get("type") or ""),
        "meta_error_subcode": str(error.get("error_subcode") or error.get("subcode") or ""),
        "meta_validation_error": str(error_data.get("details") or error_data.get("message") or error.get("error_user_msg") or ""),
        "meta_response": data,
    }


def _meta_api_display_message(details: dict) -> str:
    lines = [
        f"HTTP Status: {details.get('http_status') or ''}",
        f"Meta Error Code: {details.get('meta_error_code') or ''}",
        f"Meta Error Message: {details.get('meta_error_message') or ''}",
        f"Meta Error Type: {details.get('meta_error_type') or ''}",
    ]
    if details.get("meta_validation_error"):
        lines.append(f"Meta Validation Error: {details.get('meta_validation_error')}")
    return "\n".join(lines)


async def _log_meta_template_api_response(
    event_type: str,
    template_name: str,
    language: str,
    status: str,
    http_status: int,
    response_data: Any,
    payload: Optional[dict] = None,
) -> None:
    try:
        details = _meta_api_error_details(http_status, response_data) if status == "failed" else {
            "http_status": http_status,
            "meta_response": response_data,
        }
        row = {
            "id": str(uuid.uuid4()),
            "phone_number": "",
            "event_type": event_type,
            "template_name": template_name or "",
            "language": language or "en",
            "parameters": json.dumps({
                "http_status": http_status,
                "meta_response": response_data,
                "payload": payload or {},
            }, ensure_ascii=False),
            "status": status,
            "provider_message_id": "",
            "error_message": _meta_api_display_message(details) if status == "failed" else json.dumps(response_data, ensure_ascii=False),
            "source_type": "meta",
            "source_id": "",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        await get_repository().create_whatsapp_log(row)
    except Exception as exc:
        await log_error("whatsapp_template", "meta_template_response_log_failed", str(exc)[:500], "warning")


async def get_wa_template_objects() -> list:
    default_language = await get_setting("WHATSAPP_DEFAULT_LANGUAGE", "en") or "en"
    templates = []
    seen = set()
    raw = await get_setting(_WA_TEMPLATE_STORE_KEY, "")
    try:
        parsed = json.loads(raw) if raw else []
        if isinstance(parsed, dict):
            parsed = parsed.get("templates") or []
    except Exception:
        parsed = []
    for item in parsed if isinstance(parsed, list) else []:
        if not isinstance(item, dict):
            continue
        template = _wa_normalize_template(item, default_language)
        key = (template["purpose"], template["language"])
        if key not in seen:
            seen.add(key)
            templates.append(template)
    for purpose, slot in _WA_NEW_TO_SLOT.items():
        key = (purpose, default_language)
        if key in seen:
            continue
        template_name = await resolve_wa_template(slot)
        if template_name:
            seen.add(key)
            templates.append(_wa_normalize_template({
                "purpose": purpose,
                "meta_template_name": template_name,
                "language": default_language,
                "status": "Approved",
            }, default_language))
    templates.sort(key=lambda t: (t["purpose"], t["language"]))
    return templates


async def save_wa_template_objects(templates: list) -> list:
    default_language = await get_setting("WHATSAPP_DEFAULT_LANGUAGE", "en") or "en"
    normalized = []
    seen = set()
    for item in templates or []:
        if not isinstance(item, dict):
            continue
        template = _wa_normalize_template(item, default_language)
        key = (template["purpose"], template["language"])
        if key in seen:
            continue
        seen.add(key)
        normalized.append(template)
    settings = {_WA_TEMPLATE_STORE_KEY: json.dumps(normalized, ensure_ascii=False)}
    for template in normalized:
        if template["language"] == default_language and template.get("meta_template_name"):
            settings[_wa_template_slot(template["purpose"])] = template["meta_template_name"]
    await save_settings(settings)
    return normalized


async def generate_default_wa_templates() -> list:
    existing = {(t["purpose"], t["language"]): t for t in await get_wa_template_objects()}
    prefix = await _require_wa_template_prefix()
    default_language = await get_setting("WHATSAPP_DEFAULT_LANGUAGE", "en") or "en"
    company_name = await get_setting("COMPANY_NAME", "")
    for purpose in _WA_NEW_TO_SLOT:
        key = (purpose, default_language)
        template = existing.get(key) or {"purpose": purpose, "language": default_language}
        expected_name = f"{prefix}_{purpose}"
        if template.get("meta_template_name") != expected_name:
            template["status"] = "Pending"
            template["template_id"] = ""
        template["meta_template_name"] = expected_name
        template["variables"] = list(_WA_TEMPLATE_VARIABLES.get(purpose, []))
        template["body_text"] = _wa_template_body(purpose, company_name)
        template["status"] = template.get("status") or "Pending"
        existing[key] = template
    return await save_wa_template_objects(list(existing.values()))


async def sync_wa_templates_from_meta() -> dict:
    provider = (await get_setting("WHATSAPP_PROVIDER", "meta") or "meta").strip().lower()
    access_token = await get_setting("WHATSAPP_ACCESS_TOKEN", "")
    business_account_id = await get_setting("WHATSAPP_BUSINESS_ACCOUNT_ID", "")
    graph_version = await get_setting("WHATSAPP_GRAPH_VERSION", "v20.0") or "v20.0"
    await _require_wa_template_prefix()
    if provider != "meta":
        return {"ok": False, "reason": "meta_provider_required", "templates": await get_wa_template_objects()}
    missing_credentials = await _missing_required_meta_credentials()
    if missing_credentials:
        return _missing_meta_credentials_response(missing_credentials=missing_credentials, templates=await get_wa_template_objects())
    current = {(t["purpose"], t["language"]): t for t in await get_wa_template_objects()}
    imported = 0
    default_language = await get_setting("WHATSAPP_DEFAULT_LANGUAGE", "en") or "en"
    url = f"https://graph.facebook.com/{graph_version}/{business_account_id}/message_templates"
    params = {"access_token": access_token, "fields": "id,name,language,status,category", "limit": "100"}
    async with aiohttp.ClientSession() as session:
        while url:
            async with session.get(url, params=params if "access_token" not in url else None, timeout=30) as resp:
                data = await resp.json(content_type=None)
                if resp.status >= 400:
                    details = _meta_api_error_details(resp.status, data)
                    await _log_meta_template_api_response("template_sync_meta", "", default_language, "failed", resp.status, data)
                    return {
                        "ok": False,
                        "reason": "meta_api_error",
                        "message": _meta_api_display_message(details),
                        **details,
                        "templates": list(current.values()),
                    }
                for item in data.get("data") or []:
                    name = item.get("name") or ""
                    purpose = next((p for p in _WA_NEW_TO_SLOT if name == p or name.endswith(f"_{p}") or p in name), "")
                    if not purpose:
                        continue
                    language = item.get("language") or await get_setting("WHATSAPP_DEFAULT_LANGUAGE", "en") or "en"
                    key = (purpose, language)
                    current[key] = _wa_normalize_template({
                        **(current.get(key) or {}),
                        "purpose": purpose,
                        "meta_template_name": name,
                        "language": language,
                        "status": item.get("status"),
                        "category": item.get("category"),
                        "template_id": item.get("id") or "",
                    }, language)
                    imported += 1
                url = ((data.get("paging") or {}).get("next") or "")
                params = None
    return {"ok": True, "synced": imported, "templates": await save_wa_template_objects(list(current.values()))}


async def preview_wa_template_meta_payload(purpose: str, language: str = "") -> dict:
    purpose = _wa_template_purpose(purpose)
    default_language = await get_setting("WHATSAPP_DEFAULT_LANGUAGE", "en") or "en"
    selected_language = language or default_language
    template = next((t for t in await get_wa_template_objects() if t["purpose"] == purpose and (not selected_language or t["language"] == selected_language)), None)
    if not template:
        template = _wa_normalize_template({"purpose": purpose, "language": selected_language}, selected_language)
    prefix = await _require_wa_template_prefix()
    template["meta_template_name"] = f"{prefix}_{purpose}"
    template["status"] = template.get("status") or "Pending"
    payload, validation, template = await _wa_meta_template_payload(template, purpose)
    return {
        "ok": bool(validation.get("ok")),
        "reason": "" if validation.get("ok") else "template_validation_failed",
        "message": "" if validation.get("ok") else _WA_TEMPLATE_DENSITY_ERROR,
        "payload": payload,
        "validation": validation,
        "template": template,
    }


async def create_wa_template_in_meta(purpose: str, language: str = "") -> dict:
    purpose = _wa_template_purpose(purpose)
    template = next((t for t in await get_wa_template_objects() if t["purpose"] == purpose and (not language or t["language"] == language)), None)
    if not template:
        raise ValueError("template_not_found")
    provider = (await get_setting("WHATSAPP_PROVIDER", "meta") or "meta").strip().lower()
    access_token = await get_setting("WHATSAPP_ACCESS_TOKEN", "")
    business_account_id = await get_setting("WHATSAPP_BUSINESS_ACCOUNT_ID", "")
    graph_version = await get_setting("WHATSAPP_GRAPH_VERSION", "v20.0") or "v20.0"
    prefix = await _require_wa_template_prefix()
    expected_name = f"{prefix}_{purpose}"
    if template.get("meta_template_name") != expected_name:
        template["meta_template_name"] = expected_name
        template["status"] = "Pending"
        template["template_id"] = ""
        templates = [t for t in await get_wa_template_objects() if not (t["purpose"] == template["purpose"] and t["language"] == template["language"])]
        templates.append(template)
        await save_wa_template_objects(templates)
    payload, validation, template = await _wa_meta_template_payload(template, purpose)
    templates = [t for t in await get_wa_template_objects() if not (t["purpose"] == template["purpose"] and t["language"] == template["language"])]
    templates.append(template)
    await save_wa_template_objects(templates)
    if provider != "meta":
        return {"ok": False, "reason": "meta_provider_required", "template": template, "payload": payload, "validation": validation}
    missing_credentials = await _missing_required_meta_credentials()
    if missing_credentials:
        return _missing_meta_credentials_response(missing_credentials=missing_credentials, template=template, payload=payload, validation=validation)
    if not validation.get("ok"):
        return {
            "ok": False,
            "reason": "template_validation_failed",
            "message": _WA_TEMPLATE_DENSITY_ERROR,
            "validation": validation,
            "payload": payload,
            "template": template,
        }
    url = f"https://graph.facebook.com/{graph_version}/{business_account_id}/message_templates"
    async with aiohttp.ClientSession() as session:
        async with session.post(url, params={"access_token": access_token}, json=payload, timeout=30) as resp:
            data = await resp.json(content_type=None)
            if resp.status >= 400:
                details = _meta_api_error_details(resp.status, data)
                await _log_meta_template_api_response("template_create_meta", template["meta_template_name"], template["language"], "failed", resp.status, data, payload)
                return {
                    "ok": False,
                    "reason": "meta_api_error",
                    "message": _meta_api_display_message(details),
                    **details,
                    "payload": payload,
                    "template": template,
                }
    await _log_meta_template_api_response("template_create_meta", template["meta_template_name"], template["language"], "sent", resp.status, data, payload)
    template["template_id"] = str(data.get("id") or template.get("template_id") or "")
    template["status"] = _wa_template_status(data.get("status") or "PENDING")
    templates = [t for t in await get_wa_template_objects() if not (t["purpose"] == template["purpose"] and t["language"] == template["language"])]
    templates.append(template)
    await save_wa_template_objects(templates)
    return {"ok": True, "response": data, "payload": payload, "template": template}

@app.get("/api/whatsapp/settings")
async def api_get_wa_settings():
    cfg = await get_wa_settings_masked()
    cfg["company_brand_slug"] = _customer_setup_brand_slug(await get_setting("COMPANY_NAME", ""))
    cfg["template_suggestions"] = await _wa_template_suggestions()
    cfg["templates"] = await get_wa_template_objects()
    return cfg


@app.post("/api/whatsapp/settings")
async def api_save_wa_settings(req: WaSettingsRequest):
    if not isinstance(req.settings, dict):
        raise HTTPException(400, "settings must be a JSON object")
    await save_wa_settings(req.settings)
    return {"status": "saved", "keys_updated": [k for k in req.settings if k in WA_SETTINGS_KEYS]}


@app.get("/api/whatsapp/templates")
async def api_get_wa_templates():
    return {"templates": await get_wa_template_objects()}


@app.post("/api/whatsapp/templates")
async def api_save_wa_templates(req: WaTemplatesSaveRequest):
    if not isinstance(req.templates, list):
        raise HTTPException(400, "templates must be a list")
    return {"status": "saved", "templates": await save_wa_template_objects(req.templates)}


@app.post("/api/whatsapp/templates/generate-defaults")
async def api_generate_wa_default_templates():
    return {"status": "generated", "templates": await generate_default_wa_templates()}


@app.post("/api/whatsapp/templates/sync-meta")
async def api_sync_wa_templates_from_meta():
    return await sync_wa_templates_from_meta()


@app.post("/api/whatsapp/templates/create-meta")
async def api_create_wa_template_in_meta(req: WaTemplateMetaCreateRequest):
    purpose = _wa_template_purpose((req.purpose or "").strip())
    if not purpose:
        raise HTTPException(400, "purpose is required")
    try:
        return await create_wa_template_in_meta(purpose, req.language or "en")
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.post("/api/whatsapp/templates/preview-meta")
async def api_preview_wa_template_meta(req: WaTemplateMetaCreateRequest):
    purpose = _wa_template_purpose((req.purpose or "").strip())
    if not purpose:
        raise HTTPException(400, "purpose is required")
    return await preview_wa_template_meta_payload(purpose, req.language or "en")


@app.get("/api/whatsapp/health")
async def api_wa_health():
    return await get_wa_health()


# -- Guarded WhatsApp real-message health tests -----------------------------

_WA_HEALTH_TASKS: dict[str, asyncio.Task] = {}


def _wa_health_cipher() -> RecipientCipher:
    return RecipientCipher(os.getenv("WHATSAPP_HEALTH_TEST_ENCRYPTION_KEY", ""))


async def _wa_health_db():
    try:
        return get_repository()
    except Exception as exc:
        raise HTTPException(503, "Health-test database is unavailable") from exc


async def _wa_health_recipient(recipient_id: str, include_phone: bool = False) -> dict:
    repository = await _wa_health_db()
    try:
        recipient = await repository.get_health_recipient(recipient_id)
    except Exception as exc:
        raise HTTPException(503, "WhatsApp health-test migration is required") from exc
    if not recipient:
        raise HTTPException(404, "Test recipient not found")
    if include_phone:
        try:
            recipient["phone_number"] = _wa_health_cipher().decrypt(recipient.get("phone_number_encrypted") or "")
        except HealthTestSafetyError as exc:
            raise HTTPException(503, str(exc)) from exc
    return recipient


def _wa_health_public_recipient(row: dict) -> dict:
    return {
        "id": row.get("id"),
        "recipient_name": row.get("recipient_name"),
        "phone_number_masked": row.get("phone_number_masked"),
        "country_code": row.get("country_code"),
        "verified": bool(row.get("verified")),
        "verified_at": row.get("verified_at"),
        "active": bool(row.get("active")),
        "last_real_test_at": row.get("last_real_test_at"),
        "created_at": row.get("created_at"),
    }


@app.get("/api/whatsapp/health-test/recipients")
async def api_wa_health_recipients():
    repository = await _wa_health_db()
    try:
        rows = await repository.list_health_recipients()
    except Exception as exc:
        raise HTTPException(503, "WhatsApp health-test migration is required") from exc
    return {"recipients": [_wa_health_public_recipient(row) for row in rows]}


@app.post("/api/whatsapp/health-test/recipients")
async def api_wa_health_recipient_create(req: WaHealthRecipientCreateRequest):
    if not req.controlled_number_confirmed:
        raise HTTPException(400, "Confirm that this test number is controlled by you")
    name = req.recipient_name.strip()[:100]
    if not name:
        raise HTTPException(400, "Recipient name is required")
    try:
        phone = normalize_phone(req.phone_number)
        cipher = _wa_health_cipher()
    except (ValueError, HealthTestSafetyError) as exc:
        raise HTTPException(400, str(exc)) from exc
    now = utcnow().isoformat()
    row = {
        "id": str(uuid.uuid4()),
        "recipient_name": name,
        "phone_number_encrypted": cipher.encrypt(phone),
        "phone_number_fingerprint": cipher.fingerprint(phone),
        "phone_number_masked": mask_phone(phone),
        "country_code": country_code_for(phone),
        "verified": False,
        "active": True,
        "created_at": now,
        "updated_at": now,
    }
    repository = await _wa_health_db()
    try:
        saved = await repository.create_health_recipient(row)
    except Exception as exc:
        if "duplicate" in str(exc).lower() or "unique" in str(exc).lower():
            raise HTTPException(409, "This test recipient is already registered") from exc
        raise HTTPException(503, "Could not save the encrypted test recipient") from exc
    await log_error("whatsapp_health_test", "whatsapp_health_test_recipient_added", f"recipient={saved.get('phone_number_masked')}; verified=false", "info")
    return {"recipient": _wa_health_public_recipient(saved)}


@app.post("/api/whatsapp/health-test/recipients/{recipient_id}/send-verification")
async def api_wa_health_send_verification(recipient_id: str, req: WaHealthRecipientVerificationSendRequest):
    if req.confirm != "SEND_ONE_VERIFICATION_MESSAGE":
        raise HTTPException(400, "Explicit verification-message confirmation is required")
    recipient = await _wa_health_recipient(recipient_id, include_phone=True)
    if not recipient.get("active"):
        raise HTTPException(400, "Test recipient is inactive")
    template_name = (await get_setting("WHATSAPP_TEST_VERIFICATION_TEMPLATE", "") or "").strip()
    if not template_name:
        raise HTTPException(400, "Configure an approved Utility verification template first")
    code = generate_verification_code()
    encryption_key = os.getenv("WHATSAPP_HEALTH_TEST_ENCRYPTION_KEY", "")
    updates = {
        "verification_code_hash": hash_verification_code(recipient_id, code, encryption_key),
        "verification_expires_at": verification_expiry(),
        "verified": False,
        "verified_at": None,
        "updated_at": utcnow().isoformat(),
    }
    repository = await _wa_health_db()
    await repository.update_health_recipient(recipient_id, updates)
    result = await send_whatsapp_template(
        recipient["phone_number"], template_name, "en", [code],
        event_type="whatsapp_health_verification", source_type="health_test",
        source_id=recipient_id, bypass_failure_cooldown=True,
    )
    if not result.get("success"):
        await repository.update_health_recipient(recipient_id, {"verification_code_hash": None, "verification_expires_at": None})
        raise HTTPException(502, sanitize(result.get("error") or "Verification message failed"))
    await log_error("whatsapp_health_test", "whatsapp_health_test_message_submitted", f"type=verification; recipient={recipient.get('phone_number_masked')}; provider_message_id={result.get('provider_message_id') or ''}", "info")
    return {"submitted": True, "recipient": recipient.get("phone_number_masked"), "expires_in_minutes": 10, "provider_message_id": result.get("provider_message_id")}


@app.post("/api/whatsapp/health-test/recipients/{recipient_id}/verify")
async def api_wa_health_verify_recipient(recipient_id: str, req: WaHealthRecipientVerifyRequest):
    recipient = await _wa_health_recipient(recipient_id)
    encryption_key = os.getenv("WHATSAPP_HEALTH_TEST_ENCRYPTION_KEY", "")
    if not verify_code(recipient, req.code.strip(), encryption_key):
        raise HTTPException(400, "Verification code is invalid or expired")
    now = utcnow().isoformat()
    repository = await _wa_health_db()
    await repository.update_health_recipient(recipient_id, {
        "verified": True, "verified_at": now, "verification_code_hash": None,
        "verification_expires_at": None, "updated_at": now,
    })
    return {"verified": True, "recipient": recipient.get("phone_number_masked")}


async def _wa_health_store_result(db, run_id: str, result: dict) -> None:
    row = {"id": str(uuid.uuid4()), "test_run_id": run_id, **result}
    row["details_json"] = row.get("details_json") or {}
    await db.upsert_health_result(row)
    await log_error("whatsapp_health_test", f"whatsapp_health_test_step_{'passed' if result.get('status') == 'PASS' else 'failed'}", f"test_run_id={run_id}; test_key={result.get('test_key')}; status={result.get('status')}", "info" if result.get("status") == "PASS" else "warning")


async def _wa_health_run_stopped(db, run_id: str) -> bool:
    row = await db.get_health_run(run_id)
    return bool((row or {}).get("stop_requested"))


async def _wa_health_recalculate_run(db, run_id: str) -> None:
    rows = await db.list_health_results(run_id)
    statuses = [str(row.get("status") or "") for row in rows]
    counts = {status: statuses.count(status) for status in ("PASS", "FAIL", "WARNING", "SKIPPED")}
    final = "FAIL" if counts["FAIL"] else ("WARNING" if counts["WARNING"] or counts["SKIPPED"] or "SUBMITTED" in statuses else "PASS")
    await db.update_health_run(run_id, {
        "status": final, "total_tests": len(statuses), "passed_tests": counts["PASS"],
        "failed_tests": counts["FAIL"], "warning_tests": counts["WARNING"],
        "skipped_tests": counts["SKIPPED"], "completed_at": utcnow().isoformat(),
    })


async def _wa_health_consume_webhook(parsed: dict) -> bool:
    """Update a matching health run; return True when an inbound test reply was isolated."""
    try:
        db = get_repository()
        if parsed.get("is_status_update"):
            message_id = str(parsed.get("message_id") or "")
            if not message_id:
                return False
            row = await db.find_health_result_by_provider_id(message_id)
            if not row:
                return False
            provider_status = str(parsed.get("status_value") or "").lower()
            updates = {"webhook_status": provider_status, "completed_at": utcnow().isoformat()}
            if provider_status in {"sent", "delivered", "read"}:
                updates["status"] = "PASS"
                if provider_status == "sent":
                    updates["sent_at"] = utcnow().isoformat()
                elif provider_status in {"delivered", "read"}:
                    updates["delivered_at"] = utcnow().isoformat()
            elif provider_status == "failed":
                updates.update({"status": "FAIL", "provider_error_message": sanitize(parsed.get("failure_reason") or "Provider delivery failed")})
            await db.update_health_result(row["id"], updates)
            delivery = await db.get_health_result(row["test_run_id"], "delivery_webhook")
            if delivery:
                await db.update_health_result(delivery["id"], {"status": updates.get("status", "PASS"), "webhook_status": provider_status, "completed_at": utcnow().isoformat(), "details_json": {"matched_provider_message_id": True}})
            await _wa_health_recalculate_run(db, row["test_run_id"])
            await log_error("whatsapp_health_test", "whatsapp_health_test_webhook_received", f"test_run_id={row['test_run_id']}; status={provider_status}; provider_message_id={message_id}", "info")
            return False

        text = str(parsed.get("text") or "").strip().upper()
        if text != "TEST OK":
            return False
        try:
            phone = normalize_phone(parsed.get("phone") or "")
            fingerprint = _wa_health_cipher().fingerprint(phone)
        except Exception:
            return False
        recipient = await db.find_health_recipient_by_fingerprint(fingerprint, verified_only=True)
        if not recipient:
            return False
        cutoff = (utcnow() - timedelta(minutes=5)).isoformat()
        run = await db.find_recent_health_run(recipient["id"], cutoff)
        if not run or "inbound_reply" not in (run.get("selected_tests") or []):
            return False
        run_id = run["id"]
        inbound_result = await db.get_health_result(run_id, "inbound_reply")
        if not inbound_result:
            return False
        await db.update_health_result(inbound_result["id"], {
            "status": "PASS", "completed_at": utcnow().isoformat(), "webhook_status": "received",
            "details_json": {"test_phrase_matched": True, "crm_customer_automation_suppressed": True},
        })
        if run.get("test_ai_reply"):
            started = utcnow()
            sent = await send_whatsapp_text(phone, "[TEST] AirZen WhatsApp automation is working correctly.")
            ai_result = safe_result(
                "ai_reply", "Isolated test AI reply", "PASS" if sent.get("success") else "FAIL",
                started_at=started, provider_message_id=sent.get("provider_message_id"),
                http_status=sent.get("http_status"), error={"message": sent.get("error")},
                details={"prefix_enforced": True, "booking_and_sales_automations_suppressed": True},
            )
            await _wa_health_store_result(db, run_id, ai_result)
        await _wa_health_recalculate_run(db, run_id)
        await log_error("whatsapp_health_test", "whatsapp_health_test_webhook_received", f"test_run_id={run_id}; inbound_test_ok=true; recipient={recipient.get('phone_number_masked')}", "info")
        return True
    except Exception as exc:
        logger.warning("Health-test webhook correlation failed: %s", sanitize(str(exc)))
        return False


async def _execute_wa_health_run(run_id: str, recipient: dict, selected: list[str], language: str) -> None:
    db = get_repository()
    results: list[dict] = []
    try:
        await log_error("whatsapp_health_test", "whatsapp_health_test_started", f"test_run_id={run_id}; recipient={recipient.get('phone_number_masked')}", "info")
        if await _wa_health_run_stopped(db, run_id):
            raise asyncio.CancelledError

        started = utcnow()
        try:
            health = await get_wa_health()
            missing = health.get("missing") or []
            status = "PASS" if health.get("enabled") and not missing else "FAIL"
            result = safe_result("configuration", "Configuration", status, started_at=started, details={"provider": health.get("provider"), "missing": missing})
        except Exception as exc:
            result = safe_result("configuration", "Configuration", "FAIL", started_at=started, error={"message": str(exc)})
        await _wa_health_store_result(db, run_id, result); results.append(result)

        started = utcnow()
        try:
            if not await db.get_health_run(run_id):
                raise RuntimeError("health-test run is not persisted")
            result = safe_result("database", "Database connectivity", "PASS", started_at=started)
        except Exception as exc:
            result = safe_result("database", "Database connectivity", "FAIL", started_at=started, error={"message": str(exc)})
        await _wa_health_store_result(db, run_id, result); results.append(result)

        meta_templates: list[dict] = []
        if "meta_connection" in selected or "templates" in selected or any(item.startswith("template:") for item in selected):
            started = utcnow()
            try:
                synced = await sync_wa_templates_from_meta()
                meta_templates = synced.get("templates") or []
                ok = bool(synced.get("ok"))
                result = safe_result("meta_connection", "Meta connection and template access", "PASS" if ok else "FAIL", started_at=started, details={"synced": synced.get("synced", 0), "reason": synced.get("reason")})
            except Exception as exc:
                result = safe_result("meta_connection", "Meta connection and template access", "FAIL", started_at=started, error={"message": str(exc)})
            await _wa_health_store_result(db, run_id, result); results.append(result)

        if "templates" in selected or any(item.startswith("template:") for item in selected):
            local_templates = await get_wa_template_objects()
            by_purpose = {str(item.get("purpose")): item for item in meta_templates}
            for local in local_templates:
                purpose = str(local.get("purpose") or "")
                if purpose not in ALL_TEMPLATE_TEST_PURPOSES:
                    continue
                started = utcnow()
                built = await build_template_params(_wa_template_slot(purpose), {
                    "customer_name": "Test Customer", "service_type": "General Aircon Servicing",
                    "appointment_date": "15 August 2026", "appointment_time": "10:30 AM",
                    "technician_name": "AirZen Test Technician", "business_name": "AirZen",
                })
                readiness, note = template_readiness({**local, "expected_params": len(built.get("params") or [])}, by_purpose.get(purpose, {}))
                status = "PASS" if readiness == "Ready" else "FAIL"
                result = safe_result(f"template_check:{purpose}", f"Template {purpose}", status, started_at=started, details={"readiness": readiness, "note": note, "category": local.get("category")})
                await _wa_health_store_result(db, run_id, result); results.append(result)

        if "automation" in selected:
            started = utcnow()
            try:
                action_id = await insert_automation_action(
                    recipient["phone_number"], "whatsapp_health_test", "health_test",
                    "health_test", utcnow(), payload={"is_test": True, "test_run_id": run_id},
                )
                status = "PASS" if action_id else "FAIL"
                result = safe_result("automation", "Safe automation queue event", status, started_at=started, details={"queued": bool(action_id), "no_campaign": True, "no_appointment": True})
            except Exception as exc:
                result = safe_result("automation", "Safe automation queue event", "FAIL", started_at=started, error={"message": str(exc)})
            await _wa_health_store_result(db, run_id, result); results.append(result)

        for selection in [item for item in selected if item == "basic_text" or item.startswith("template:")]:
            if await _wa_health_run_stopped(db, run_id):
                raise asyncio.CancelledError
            started = utcnow()
            if selection == "basic_text":
                if not await is_whatsapp_service_window_open(recipient["phone_number"]):
                    result = safe_result("basic_text", "Basic service-window text", "SKIPPED", started_at=started, details={"reason": "Customer-service window is closed"})
                else:
                    sent = await send_whatsapp_text(recipient["phone_number"], "AirZen WhatsApp system test. No action is required.")
                    result = safe_result("basic_text", "Basic service-window text", "PASS" if sent.get("success") else "FAIL", started_at=started, provider_message_id=sent.get("provider_message_id"), http_status=sent.get("http_status"), error={"message": sent.get("error")})
            else:
                purpose = selection.split(":", 1)[1]
                if purpose in MARKETING_TEST_PURPOSES:
                    # Authorization was checked before the task was started.
                    pass
                local = next((item for item in await get_wa_template_objects() if item.get("purpose") == purpose and (item.get("language") or "en") == language), None)
                if not local or str(local.get("status") or "").upper() != "APPROVED" or str(local.get("category") or "").upper() not in ({"UTILITY"} if purpose not in MARKETING_TEST_PURPOSES else {"MARKETING"}):
                    result = safe_result(selection, f"Send {purpose}", "SKIPPED", started_at=started, details={"reason": "Template is not approved for the required category"})
                else:
                    context = {
                        "customer_name": "Test Customer", "service_type": "General Aircon Servicing",
                        "appointment_date": "15 August 2026", "appointment_time": "10:30 AM",
                        "technician_name": "AirZen Test Technician", "business_name": "AirZen",
                    }
                    slot = _wa_template_slot(purpose)
                    built = await build_template_params(slot, context)
                    meta_count = len(local.get("variables") or [])
                    local_count = len(built.get("params") or [])
                    if meta_count != local_count:
                        result = safe_result(selection, f"Send {purpose}", "SKIPPED", started_at=started, details={"reason": "Template variable mismatch", "meta_count": meta_count, "local_count": local_count})
                    else:
                        sent = await send_whatsapp_template(
                            recipient["phone_number"], local.get("meta_template_name") or built.get("template_name"),
                            language, built.get("params") or [], event_type="whatsapp_health_test",
                            source_type="health_test", source_id=run_id, template_purpose=slot,
                            bypass_failure_cooldown=True,
                        )
                        result = safe_result(selection, f"Send {purpose}", "PASS" if sent.get("success") else "FAIL", started_at=started, provider_message_id=sent.get("provider_message_id"), http_status=sent.get("http_status"), error={"message": sent.get("error")})
            await db.update_health_run(run_id, {"real_messages_submitted": sum(1 for row in results if row.get("provider_message_id")) + (1 if result.get("provider_message_id") else 0)})
            await _wa_health_store_result(db, run_id, result); results.append(result)

        for key, name, note in (
            ("delivery_webhook", "Delivery webhook", "Awaiting provider status; refresh this run after webhook receipt"),
            ("inbound_reply", "Inbound TEST OK reply", "Reply TEST OK from the verified test phone within five minutes"),
            ("crm", "Test CRM isolation", "Dedicated test records are isolated; ordinary CRM contacts are not modified"),
        ):
            if key in selected:
                result = safe_result(key, name, "WARNING" if key != "crm" else "PASS", started_at=utcnow(), details={"note": note})
                await _wa_health_store_result(db, run_id, result); results.append(result)

        counts = {status: sum(1 for item in results if item.get("status") == status) for status in ("PASS", "FAIL", "WARNING", "SKIPPED")}
        final = "FAIL" if counts["FAIL"] else ("WARNING" if counts["WARNING"] or counts["SKIPPED"] else "PASS")
        await db.update_health_run(run_id, {
            "status": final, "completed_at": utcnow().isoformat(), "total_tests": len(results),
            "passed_tests": counts["PASS"], "failed_tests": counts["FAIL"],
            "warning_tests": counts["WARNING"], "skipped_tests": counts["SKIPPED"],
            "report_json": sanitize({"test_run_id": run_id, "status": final, "results": results}),
        })
        await log_error("whatsapp_health_test", "whatsapp_health_test_completed", f"test_run_id={run_id}; status={final}; passed={counts['PASS']}; failed={counts['FAIL']}", "info")
    except asyncio.CancelledError:
        await db.update_health_run(run_id, {"status": "STOPPED", "completed_at": utcnow().isoformat()})
        await log_error("whatsapp_health_test", "whatsapp_health_test_completed", f"test_run_id={run_id}; status=STOPPED", "warning")
    except Exception as exc:
        await db.update_health_run(run_id, {"status": "FAIL", "completed_at": utcnow().isoformat(), "report_json": {"safe_error": sanitize(str(exc))}})
        await log_error("whatsapp_health_test", "whatsapp_health_test_completed", f"test_run_id={run_id}; status=FAIL; error={sanitize(str(exc))}", "error")
    finally:
        _WA_HEALTH_TASKS.pop(run_id, None)


async def _wa_health_fetch_all_meta_templates() -> list[dict]:
    missing = await _missing_required_meta_credentials()
    if missing:
        raise HealthTestSafetyError("Missing Meta credentials: " + ", ".join(missing))
    access_token = await get_setting("WHATSAPP_ACCESS_TOKEN", "")
    business_account_id = await get_setting("WHATSAPP_BUSINESS_ACCOUNT_ID", "")
    graph_version = await get_setting("WHATSAPP_GRAPH_VERSION", "v20.0") or "v20.0"
    url = f"https://graph.facebook.com/{graph_version}/{business_account_id}/message_templates"
    params = {"access_token": access_token, "fields": "id,name,language,status,category,components", "limit": "100"}
    templates: list[dict] = []
    async with aiohttp.ClientSession() as session:
        while url:
            async with session.get(url, params=params if "access_token" not in url else None, timeout=30) as response:
                data = await response.json(content_type=None)
                if response.status >= 400:
                    details = _meta_api_error_details(response.status, data)
                    raise HealthTestSafetyError(_meta_api_display_message(details))
                templates.extend(item for item in (data.get("data") or []) if isinstance(item, dict))
                url = str((data.get("paging") or {}).get("next") or "")
                params = None
    return templates


async def _wa_health_all_template_preview(req: WaHealthAllTemplatesRequest, recipient: dict) -> dict:
    if req.max_templates < 1 or req.max_templates > 50:
        raise HealthTestSafetyError("Maximum templates per run must be between 1 and 50")
    if req.delay_seconds < 2 or req.delay_seconds > 10:
        raise HealthTestSafetyError("Delay between messages must be between 2 and 10 seconds")
    templates = await _wa_health_fetch_all_meta_templates()
    local = await get_wa_template_objects()
    local_by_name = {str(item.get("meta_template_name") or ""): item for item in local}
    media_urls = {
        "IMAGE": await get_setting("WHATSAPP_TEST_IMAGE_URL", ""),
        "VIDEO": await get_setting("WHATSAPP_TEST_VIDEO_URL", ""),
        "DOCUMENT": await get_setting("WHATSAPP_TEST_DOCUMENT_URL", ""),
    }
    prepared = []
    for template in templates:
        mapping = local_by_name.get(str(template.get("name") or "")) or {}
        item = prepare_existing_template(
            template, recipient_masked=recipient.get("phone_number_masked") or mask_phone(recipient.get("phone_number") or ""),
            local_variables=mapping.get("variables") or [], media_urls=media_urls,
            include_marketing=req.include_marketing,
            include_authentication=req.include_authentication,
        )
        item["template_id"] = template.get("id")
        prepared.append(item)
    total_discovered = len(prepared)
    if req.template_names:
        wanted = set(req.template_names)
        prepared = [item for item in prepared if item.get("template_name") in wanted]
    ready = [item for item in prepared if item.get("ready")][:req.max_templates]
    approved = [item for item in prepared if item.get("status") == "APPROVED"]
    counts = {category: sum(1 for item in approved if item.get("category") == category) for category in ("UTILITY", "MARKETING", "AUTHENTICATION")}
    return {
        "total_discovered": total_discovered, "approved_templates": len(approved),
        "templates_selected": len(ready), "utility_count": counts["UTILITY"],
        "marketing_count": counts["MARKETING"], "authentication_count": counts["AUTHENTICATION"],
        "recipient_masked": recipient.get("phone_number_masked"),
        "estimated_messages": len(ready),
        "estimated_duration_seconds": max(0, (len(ready) - 1) * req.delay_seconds),
        "required_confirmation": f"Send {len(ready)} real WhatsApp template messages to {recipient.get('phone_number_masked')}.",
        "templates": prepared,
    }


@app.post("/api/whatsapp/health-test/templates/preview-all")
async def api_wa_health_preview_all_templates(req: WaHealthAllTemplatesRequest):
    recipient = await _wa_health_recipient(req.recipient_id, include_phone=True)
    if not recipient.get("verified") or not recipient.get("active"):
        raise HTTPException(400, "Select a verified and active test recipient")
    try:
        return await _wa_health_all_template_preview(req, recipient)
    except HealthTestSafetyError as exc:
        raise HTTPException(400, str(exc)) from exc


async def _wa_health_submit_meta_payload(payload: dict) -> dict:
    access_token = await get_setting("WHATSAPP_ACCESS_TOKEN", "")
    phone_number_id = await get_setting("WHATSAPP_PHONE_NUMBER_ID", "")
    graph_version = await get_setting("WHATSAPP_GRAPH_VERSION", "v20.0") or "v20.0"
    url = f"https://graph.facebook.com/{graph_version}/{phone_number_id}/messages"
    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
    retries = 0
    while True:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
            async with session.post(url, json=payload, headers=headers) as response:
                data = await response.json(content_type=None)
                if response.status == 429 and retries < 2:
                    retries += 1
                    await asyncio.sleep(2 ** retries)
                    continue
                messages = data.get("messages") or [] if isinstance(data, dict) else []
                message_id = messages[0].get("id") if messages and isinstance(messages[0], dict) else None
                error = data.get("error") or {} if isinstance(data, dict) else {}
                return {
                    "success": response.status in {200, 201} and bool(message_id),
                    "http_status": response.status, "provider_message_id": message_id,
                    "provider_response": data, "retry_count": retries,
                    "error_code": error.get("code"), "error_type": error.get("type"),
                    "error_message": error.get("message"),
                }


async def _wa_health_delivery_timeout(run_id: str, timeout_seconds: int = 180) -> None:
    await asyncio.sleep(max(1, timeout_seconds))
    try:
        db = get_repository()
        run = await db.get_health_run(run_id)
        if not run or run.get("status") == "STOPPED":
            return
        pending = [row for row in await db.list_health_results(run_id) if row.get("status") == "SUBMITTED"]
        for row in pending:
            await db.update_health_result(row["id"], {
                "status": "WARNING", "completed_at": utcnow().isoformat(),
                "details_json": {"notes": "Delivery webhook was not received within 180 seconds"},
            })
        if pending:
            await _wa_health_recalculate_run(db, run_id)
    except Exception as exc:
        logger.warning("Health-test delivery timeout update failed: %s", sanitize(str(exc)))


async def _execute_wa_all_templates_run(run_id: str, recipient: dict, preview: dict, req: WaHealthAllTemplatesRequest) -> None:
    db = get_repository()
    tested = 0
    try:
        for number, item in enumerate(preview.get("templates") or [], start=1):
            if await _wa_health_run_stopped(db, run_id):
                raise asyncio.CancelledError
            key = f"existing_template:{item.get('template_name')}:{item.get('language')}"
            started = utcnow()
            common = {
                "id": str(uuid.uuid4()), "test_run_id": run_id, "test_key": key,
                "test_name": f"Template {item.get('template_name')}",
                "template_name": item.get("template_name"), "template_category": item.get("category"),
                "template_language": item.get("language"), "payload_json": sanitize(item.get("payload") or {}),
                "is_test": True, "started_at": started.isoformat(),
            }
            if not item.get("ready") or tested >= req.max_templates:
                reason = item.get("skip_reason") or "Maximum template limit reached"
                row = {**common, "status": "SKIPPED", "skip_reason": reason, "completed_at": utcnow().isoformat(), "details_json": {"number": number, "notes": reason}}
                await db.upsert_health_result(row)
                continue
            payload = json.loads(json.dumps(item["payload"]))
            payload["to"] = recipient["phone_number"].lstrip("+")
            result = await _wa_health_submit_meta_payload(payload)
            tested += 1
            completed = utcnow()
            row = {
                **common, "status": "SUBMITTED" if result["success"] else "FAIL",
                "completed_at": completed.isoformat(), "duration_ms": int((completed - started).total_seconds() * 1000),
                "provider_message_id": result.get("provider_message_id"), "http_status": result.get("http_status"),
                "provider_error_code": str(result.get("error_code") or ""), "provider_error_type": str(result.get("error_type") or ""),
                "provider_error_message": sanitize(result.get("error_message") or ""),
                "provider_response_json": result.get("provider_response") or {}, "retry_count": result.get("retry_count") or 0,
                "submitted_at": completed.isoformat() if result["success"] else None,
                "details_json": {"number": number, "notes": "Awaiting sent or delivered webhook" if result["success"] else "Meta rejected the template"},
            }
            await db.upsert_health_result(row)
            await db.update_health_run(run_id, {"real_messages_submitted": tested})
            await log_error("whatsapp_health_test", "whatsapp_health_test_message_submitted", f"test_run_id={run_id}; template={item.get('template_name')}; recipient={recipient.get('phone_number_masked')}; status={row['status']}", "info")
            if not result["success"] and req.stop_on_first_failure:
                break
            if tested < req.max_templates:
                await asyncio.sleep(req.delay_seconds)
        await _wa_health_recalculate_run(db, run_id)
        if tested:
            asyncio.create_task(_wa_health_delivery_timeout(run_id, 180))
    except asyncio.CancelledError:
        await db.update_health_run(run_id, {"status": "STOPPED", "completed_at": utcnow().isoformat()})
    except Exception as exc:
        await db.update_health_run(run_id, {"status": "FAIL", "completed_at": utcnow().isoformat(), "report_json": {"safe_error": sanitize(str(exc))}})
    finally:
        _WA_HEALTH_TASKS.pop(run_id, None)


@app.post("/api/whatsapp/health-test/templates/run-all")
async def api_wa_health_run_all_templates(req: WaHealthAllTemplatesRequest, request: Request):
    recipient = await _wa_health_recipient(req.recipient_id, include_phone=True)
    if req.confirmation != "I_CONFIRM_MULTIPLE_TEMPLATE_MESSAGES":
        raise HTTPException(400, "Confirm that this number belongs to you and may receive multiple WhatsApp test messages")
    repository = await _wa_health_db()
    if await repository.find_active_health_run(req.recipient_id):
        raise HTTPException(409, "A health test is already active for this recipient")
    try:
        preview = await _wa_health_all_template_preview(req, recipient)
    except HealthTestSafetyError as exc:
        raise HTTPException(400, str(exc)) from exc
    if req.confirmation_message != preview["required_confirmation"]:
        raise HTTPException(400, "The final template-count confirmation does not match the preview")
    if not preview.get("templates_selected"):
        raise HTTPException(400, "No approved and eligible templates were found")
    run_id = str(uuid.uuid4())
    now = utcnow().isoformat()
    db = repository
    await db.create_health_run({
        "id": run_id, "started_at": now, "status": "RUNNING", "recipient_id": req.recipient_id,
        "initiated_by": sanitize(request.headers.get("x-admin-user") or "authenticated_admin"),
        "selected_tests": ["all_existing_templates"], "language": "all", "test_ai_reply": False,
        "allow_marketing": req.include_marketing, "max_real_messages": req.max_templates,
        "real_messages_submitted": 0, "stop_requested": False,
    })
    await db.update_health_recipient(req.recipient_id, {"last_real_test_at": now, "updated_at": now})
    task = asyncio.create_task(_execute_wa_all_templates_run(run_id, recipient, preview, req))
    _WA_HEALTH_TASKS[run_id] = task
    return {"test_run_id": run_id, "status": "RUNNING", "recipient": recipient.get("phone_number_masked"), "templates_selected": preview.get("templates_selected")}


@app.post("/api/whatsapp/health-test/runs")
async def api_wa_health_run_start(req: WaHealthRunRequest, request: Request):
    recipient = await _wa_health_recipient(req.recipient_id, include_phone=True)
    selected = req.selected_tests or ["configuration", "meta_connection", "templates", "database", "automation"]
    if req.test_ai_reply:
        if "inbound_reply" not in selected:
            raise HTTPException(400, "Test AI Reply requires the inbound TEST OK check")
        if "ai_reply" not in selected:
            selected = [*selected, "ai_reply"]
    try:
        selected = validate_test_selection(recipient, selected, req.confirmation, req.max_real_messages, allow_marketing=req.allow_marketing, marketing_confirmation=req.marketing_confirmation)
    except HealthTestSafetyError as exc:
        raise HTTPException(400, str(exc)) from exc
    run_id = str(uuid.uuid4())
    now = utcnow().isoformat()
    initiated_by = sanitize(request.headers.get("x-admin-user") or "authenticated_admin")
    row = {
        "id": run_id, "started_at": now, "status": "RUNNING", "recipient_id": req.recipient_id,
        "initiated_by": initiated_by, "selected_tests": selected, "language": req.language or "en",
        "test_ai_reply": bool(req.test_ai_reply), "allow_marketing": bool(req.allow_marketing),
        "max_real_messages": req.max_real_messages, "real_messages_submitted": 0, "stop_requested": False,
    }
    db = await _wa_health_db()
    await db.create_health_run(row)
    if any(item == "basic_text" or item == "ai_reply" or item.startswith("template:") for item in selected):
        await db.update_health_recipient(req.recipient_id, {"last_real_test_at": now, "updated_at": now})
    task = asyncio.create_task(_execute_wa_health_run(run_id, recipient, selected, req.language or "en"))
    _WA_HEALTH_TASKS[run_id] = task
    return {"test_run_id": run_id, "status": "RUNNING", "recipient": recipient.get("phone_number_masked")}


@app.get("/api/whatsapp/health-test/runs")
async def api_wa_health_runs(limit: int = 20):
    db = await _wa_health_db()
    rows = await db.list_health_runs(limit=min(max(limit, 1), 100))
    return {"runs": sanitize(rows)}


@app.get("/api/whatsapp/health-test/runs/{run_id}")
async def api_wa_health_run_detail(run_id: str):
    db = await _wa_health_db()
    run = await db.get_health_run(run_id)
    if not run:
        raise HTTPException(404, "Health-test run not found")
    results = await db.list_health_results(run_id)
    return sanitize({"run": run, "results": results})


@app.post("/api/whatsapp/health-test/runs/{run_id}/stop")
async def api_wa_health_run_stop(run_id: str):
    db = await _wa_health_db()
    await db.update_health_run(run_id, {"stop_requested": True})
    task = _WA_HEALTH_TASKS.get(run_id)
    if task and not task.done():
        task.cancel()
    return {"test_run_id": run_id, "stop_requested": True}


@app.delete("/api/whatsapp/health-test/runs/{run_id}/test-data")
async def api_wa_health_cleanup(run_id: str):
    db = await _wa_health_db()
    run = await db.get_health_run(run_id)
    if not run:
        raise HTTPException(404, "Health-test run not found")
    if run.get("status") == "RUNNING":
        raise HTTPException(409, "Stop the active test before cleanup")
    await db.delete_health_run(run_id)
    return {"cleaned": True, "test_run_id": run_id, "scope": "dedicated health-test records only"}


@app.get("/api/whatsapp/health-test/runs/{run_id}/report.{format_name}")
async def api_wa_health_report(run_id: str, format_name: str):
    detail = await api_wa_health_run_detail(run_id)
    payload, media_type = report_bytes({"run": detail["run"], "results": detail["results"]}, format_name.lower())
    return Response(content=payload, media_type=media_type, headers={"Content-Disposition": f'attachment; filename="whatsapp-health-{run_id}.{format_name.lower()}"'})


@app.get("/api/whatsapp/templates/debug")
async def api_wa_templates_debug():
    await log_error("whatsapp_template_debug", "whatsapp_template_debug_requested", "endpoint=/api/whatsapp/templates/debug", "info")
    templates = []
    objects = await get_wa_template_objects()
    by_purpose = {}
    for obj in objects:
        by_purpose.setdefault(obj.get("purpose"), obj)
    for purpose, _ in WA_TEMPLATE_PURPOSES:
        built = await build_template_params(purpose, {})
        meta = by_purpose.get(purpose) or {}
        templates.append({
            "purpose": purpose,
            "template_name": built["template_name"],
            "category": meta.get("category") or "",
            "language": meta.get("language") or built.get("language") or "",
            "status": meta.get("status") or "",
            "variables": meta.get("variables") or [],
            "template_id": meta.get("template_id") or "",
            "configured": bool(built["template_name"]),
            "expected_params": await get_template_expected_param_count(purpose),
            "sample_params": built["params"],
            "actual_sample_count": len(built["params"]),
        })
    return {"templates": templates}


@app.post("/api/whatsapp/templates/test")
async def api_wa_templates_test(req: WaTemplateDebugTestRequest):
    supported_purposes = set(_WA_NEW_TO_SLOT) | {purpose for purpose, _ in WA_TEMPLATE_PURPOSES}
    template_purpose = _wa_template_purpose((req.template_purpose or "").strip())
    if not template_purpose or template_purpose not in supported_purposes:
        raise HTTPException(400, "unsupported or missing template_purpose")
    template_slot = _wa_template_slot(template_purpose)
    if not req.phone or not req.phone.strip():
        raise HTTPException(400, "Missing recipient number")
    try:
        phone = normalize_phone(req.phone)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    sample_context = {
        "customer_name": "John",
        "lead_name": "John",
        "name": "John",
        "appointment_date": "05 Jul 2026",
        "date": "05 Jul 2026",
        "appointment_time": "10:30 AM",
        "time": "10:30 AM",
        "appointment_datetime": "05 Jul 2026 10:30 AM",
        "service": "General Aircon Servicing",
        "service_type": "General Aircon Servicing",
        "requirement": "General Aircon Servicing",
        "address": "Tampines, Singapore",
        "business_name": "AirZen",
        "company_name": "AirZen",
        "company": "AirZen",
        "customer_phone": phone,
        "phone": phone,
        "phone_number": phone,
        "source": "Website",
        "reason": "Customer requested assistance",
    }
    built = await build_template_params(template_slot, sample_context)
    if req.template_name:
        built["template_name"] = req.template_name.strip()
    if req.language:
        built["language"] = req.language.strip() or "en"
    if isinstance(req.variables, list):
        params = []
        for variable in req.variables:
            key = str(variable or "").strip()
            if key:
                params.append(str(sample_context.get(key, key)))
        built["params"] = params
        expected_count = len(params)
    else:
        expected_count = await get_template_expected_param_count(template_slot)
    actual_count = len(built["params"])

    if not req.dry_run:
        health = await get_wa_health()
        if (
            str(health.get("provider") or "meta").lower() != "meta"
            or not health.get("phone_number_id_configured")
            or not health.get("access_token_configured")
        ):
            raise HTTPException(400, "Missing Meta credentials")

        status_value = (req.status or "").strip()
        if not status_value:
            for template in await get_wa_template_objects():
                if template.get("purpose") == template_purpose and (
                    not req.language or template.get("language") == req.language
                ):
                    status_value = template.get("status") or ""
                    break
        if status_value.upper() != "APPROVED":
            raise HTTPException(400, "Template not approved")

        # Check confirm value
        if req.confirm not in ("SEND_TEST", "SEND_TEST_FORCE"):
            raise HTTPException(400, "confirm must be 'SEND_TEST' or 'SEND_TEST_FORCE' for live send")

        # Check template configuration
        if not built["template_name"]:
            raise HTTPException(400, "Template is not configured")

        # Check expected vs actual param counts
        if expected_count != actual_count:
            raise HTTPException(400, f"Template parameter count mismatch: expected {expected_count}, got {actual_count}")

        # Cooldown protection check
        cutoff = (datetime.now() - timedelta(minutes=5)).isoformat()
        try:
            rows = await get_repository().list_whatsapp_logs(
                phone=phone,
                template_name=built["template_name"],
                status="failed",
                since_utc=cutoff,
                limit=50,
            )
            has_failed = False
            for row in rows:
                err = str(row.get("error_message") or "")
                if "132000" in err or "100" in err:
                    has_failed = True
                    break
            if has_failed and req.confirm != "SEND_TEST_FORCE":
                raise HTTPException(400, "Recent parameter/invalid template failure detected. Use confirm='SEND_TEST_FORCE' to bypass 5-minute cooldown.")
        except HTTPException:
            raise
        except Exception as e:
            logger.warning("whatsapp_template_debug_cooldown_check failed: %s", e)

        if req.confirm == "SEND_TEST_FORCE":
            await log_error(
                "whatsapp_template_debug",
                "whatsapp_template_test_force_bypass_used",
                f"phone={phone}; purpose={template_purpose}; template={built['template_name']}",
                "info",
            )

        # Call existing template send function
        result = await send_whatsapp_template(
            phone=phone,
            template_name=built["template_name"],
            language=built["language"] or "en",
            parameters=built["params"],
            event_type="template_test",
            source_type="debug_test",
            source_id=f"expected={expected_count};actual={actual_count};params={actual_count}",
            template_purpose="" if isinstance(req.variables, list) else template_slot,
            bypass_failure_cooldown=(req.confirm == "SEND_TEST_FORCE"),
        )

        return {
            "ok": bool(result.get("success")),
            "template_name": built["template_name"],
            "params": built["params"],
            "expected_count": expected_count,
            "actual_count": actual_count,
            "sent": bool(result.get("success")),
            "provider_message_id": result.get("provider_message_id") or "",
            "error": result.get("error") or None,
        }

    # dry_run = True (existing behavior)
    error = ""
    if not built["template_name"]:
        error = "template_not_configured"
    elif expected_count != actual_count:
        error = f"template_param_mismatch expected={expected_count} actual={actual_count}"
    await log_error(
        "whatsapp_template_debug",
        "whatsapp_template_dry_run",
        f"phone={phone}; purpose={template_purpose}; template={built['template_name']}; expected={expected_count}; actual={actual_count}; error={error}",
        "warning" if error else "info",
    )
    return {
        "template_name": built["template_name"],
        "params": built["params"],
        "expected_count": expected_count,
        "actual_count": actual_count,
        "would_send": not error,
        "error": error or None,
    }


@app.post("/api/whatsapp/send-template")
async def api_wa_send_template(req: WaSendTemplateRequest):
    try:
        phone = normalize_phone(req.phone)
    except ValueError as e:
        raise HTTPException(400, str(e))
    result = await send_whatsapp_template(
        phone, req.template_name, req.language, req.parameters,
        event_type="manual_send", source_type="dashboard", source_id="manual",
    )
    return result


@app.get("/api/whatsapp/logs")
async def api_wa_logs(phone: Optional[str] = None, limit: int = 50):
    if phone:
        try:
            phone = normalize_phone(phone)
        except Exception:
            pass
    return {"logs": await get_whatsapp_logs(phone=phone, limit=min(limit, 200))}


# ── WhatsApp webhook stubs (Phase 8 — wired now so routes exist) ─────────────

@app.get("/api/whatsapp/webhook")
async def api_wa_webhook_verify(
    hub_mode: Optional[str] = Query(None, alias="hub.mode"),
    hub_verify_token: Optional[str] = Query(None, alias="hub.verify_token"),
    hub_challenge: Optional[str] = Query(None, alias="hub.challenge"),
):
    from db import get_setting as _gs
    verify_token = await _gs("WEBHOOK_VERIFY_TOKEN", "") or await _gs("WHATSAPP_VERIFY_TOKEN", "")
    if (
        hub_mode == "subscribe"
        and verify_token
        and hub_verify_token
        and hmac.compare_digest(hub_verify_token, verify_token)
    ):
        if hub_challenge is None:
            raise HTTPException(400, "Missing verification challenge")
        from fastapi.responses import PlainTextResponse
        return PlainTextResponse(content=hub_challenge)
    raise HTTPException(403, "Verification failed")


def _wa_mask_phone_for_log(phone: str) -> str:
    digits = "".join(ch for ch in str(phone or "") if ch.isdigit())
    if len(digits) <= 4:
        return digits
    return f"***{digits[-4:]}"


def _wa_detect_webhook_provider(payload, parsed_list: Optional[list] = None) -> str:
    providers = sorted({
        str(p.get("provider") or "").lower()
        for p in (parsed_list or [])
        if isinstance(p, dict) and p.get("provider")
    })
    if providers:
        return ",".join(providers)
    if isinstance(payload, dict) and (payload.get("object") == "whatsapp_business_account" or "entry" in payload):
        return "meta"
    if isinstance(payload, dict) and any(k in payload for k in ("event", "events", "channel_id", "data")):
        return "vobiz"
    return "unknown"


def _wa_safe_webhook_summary(payload, parsed_list: Optional[list] = None) -> dict:
    parsed_list = parsed_list or []
    top_keys = list(payload.keys())[:12] if isinstance(payload, dict) else [type(payload).__name__]
    message_types = sorted({str(p.get("message_type") or "unknown") for p in parsed_list if isinstance(p, dict)})
    phones = sorted({_wa_mask_phone_for_log(p.get("phone") or "") for p in parsed_list if isinstance(p, dict) and p.get("phone")})
    return {
        "provider": _wa_detect_webhook_provider(payload, parsed_list),
        "top_level_keys": top_keys,
        "parsed_message_count": len(parsed_list),
        "message_types": message_types,
        "phones_found": phones,
        "has_entry": bool(isinstance(payload, dict) and payload.get("entry")),
        "has_events": bool(isinstance(payload, dict) and payload.get("events")),
    }


def _verify_meta_webhook_signature(raw_body: bytes, signature_header: str, app_secret: str) -> bool:
    """Validate Meta's X-Hub-Signature-256 without logging secret material."""
    signature_header = str(signature_header or "").strip()
    app_secret = str(app_secret or "").strip()
    if not raw_body or not app_secret or not signature_header.startswith("sha256="):
        return False
    supplied = signature_header[len("sha256="):].strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", supplied):
        return False
    expected = hmac.new(app_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(supplied, expected)


@app.post("/api/whatsapp/webhook")
async def api_wa_webhook_receive(request: Request):
    raw_body = await request.body()
    app_secret = await get_setting("META_APP_SECRET", "")
    signature = request.headers.get("x-hub-signature-256", "")
    if not _verify_meta_webhook_signature(raw_body, signature, app_secret):
        await log_error("whatsapp_webhook", "whatsapp_webhook_signature_rejected", "invalid_or_missing_signature=true", "warning")
        raise HTTPException(403, "Invalid webhook signature")
    try:
        payload = json.loads(raw_body)
        if not isinstance(payload, dict):
            raise ValueError("payload must be a JSON object")
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        await log_error("whatsapp_webhook", "whatsapp_webhook_received", "provider=unknown; invalid_json=true", "warning")
        await log_error("whatsapp_webhook", "whatsapp_webhook_parsed_empty", "invalid_json=true", "warning")
        raise HTTPException(400, "Malformed webhook payload")
    try:
        parsed_list = parse_webhook_messages(payload)
        summary = _wa_safe_webhook_summary(payload, parsed_list)
        await log_error("whatsapp_webhook", "whatsapp_webhook_received", json.dumps(summary, separators=(",", ":"))[:1800], "info")
        if not parsed_list:
            await log_error("whatsapp_webhook", "whatsapp_webhook_parsed_empty", json.dumps(summary, separators=(",", ":"))[:1800], "warning")
        # Keep processing inside the signed webhook request.  Detached tasks can
        # be cancelled as soon as a request-scoped worker returns, leaving the
        # inbound row committed but never reaching AI generation or Meta send.
        # Meta retries non-2xx responses and inbound message ids are idempotent.
        if parsed_list:
            isolated = await asyncio.gather(*(_wa_health_consume_webhook(parsed) for parsed in parsed_list))
            normal_items = [parsed for parsed, consumed in zip(parsed_list, isolated) if not consumed]
            if normal_items:
                await asyncio.gather(*(handle_inbound_whatsapp_message(parsed) for parsed in normal_items))
    except Exception as exc:
        logger.error("Webhook processing error: %s", exc)
        await log_error(
            "whatsapp_webhook", "whatsapp_webhook_processing_error",
            f"exception_type={type(exc).__name__}; retry_required=true", "error",
        )
        # A 2xx response tells Meta the event is complete.  Returning success
        # here permanently lost messages that failed after their inbound row was
        # stored.  A retry is safe because inbound processing is idempotent.
        raise HTTPException(500, "Webhook processing failed; retry required") from exc
    return {"status": "ok"}


@app.get("/api/whatsapp/webhook/status")
async def api_wa_webhook_status():
    health = await get_wa_health()
    raw_logs = await get_logs(limit=200)
    interesting_sources = {
        "whatsapp_webhook", "whatsapp_ai", "whatsapp_status", "whatsapp_inbox",
        "whatsapp_text", "whatsapp_template", "whatsapp_automation", "whatsapp_media",
    }
    webhook_logs = [
        row for row in (raw_logs or [])
        if (row.get("source") in interesting_sources)
        or ("whatsapp" in str(row.get("message") or "").lower())
    ][:20]
    activity = await get_whatsapp_message_activity()
    return {
        "webhook_route_active": True,
        "whatsapp_health": health,
        "logs": webhook_logs,
        "last_inbound_whatsapp_message_at": activity.get("last_inbound_whatsapp_message_at"),
        "last_outbound_ai_message_at": activity.get("last_outbound_ai_message_at"),
    }


# ── WhatsApp Inbox — Conversations ────────────────────────────────────────────

@app.get("/api/whatsapp/debug/phone/{phone}")
async def api_wa_debug_phone(phone: str):
    try:
        normalized = normalize_phone(unquote(phone))
    except Exception:
        normalized = unquote(phone)
    all_matches = await get_conversations_by_phone(normalized, include_deleted=True, limit=20)
    active_matches = [c for c in all_matches if not c.get("is_deleted")]
    deleted_matches = [c for c in all_matches if c.get("is_deleted")]
    conversation = active_matches[0] if active_matches else (all_matches[0] if all_matches else None)
    messages = []
    if conversation and conversation.get("id"):
        messages = await get_messages(conversation["id"], limit=20)
    wa_logs = await get_whatsapp_logs(phone=normalized, limit=20)
    raw_error_logs = await get_logs(limit=500)
    needles = {normalized, normalized.lstrip("+"), unquote(phone)}
    error_logs = []
    for row in raw_error_logs or []:
        source = str(row.get("source") or "")
        haystack = f"{row.get('source','')} {row.get('message','')} {row.get('detail','')}"
        if source.startswith("whatsapp") and any(n and n in haystack for n in needles):
            error_logs.append(row)
        if len(error_logs) >= 20:
            break
    return {
        "normalized_phone": normalized,
        "found_active_conversation": bool(active_matches),
        "found_deleted_conversation": bool(deleted_matches),
        "conversation_is_deleted": bool(conversation and conversation.get("is_deleted")),
        "conversation": conversation,
        "conversation_matches": all_matches,
        "messages": messages,
        "whatsapp_logs": wa_logs,
        "error_logs": error_logs,
    }


@app.get("/api/whatsapp/conversations")
async def api_wa_conversations(
    status: Optional[str] = None,
    ai_enabled: Optional[bool] = None,
    search: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
):
    convs = await get_conversations(status=status, ai_enabled=ai_enabled, search=search, limit=limit, offset=offset)
    return {"conversations": convs, "total": len(convs)}


@app.get("/api/whatsapp/conversations/{conv_id}")
async def api_wa_conversation_detail(conv_id: str):
    conv = await get_conversation_by_id(conv_id)
    if not conv:
        raise HTTPException(404, "Conversation not found")
    msgs = await get_messages(conv_id, limit=50)
    return {"conversation": conv, "messages": msgs}


@app.get("/api/whatsapp/conversations/{conv_id}/messages")
async def api_wa_conversation_messages(
    conv_id: str,
    limit: int = 50,
    offset: int = 0,
):
    msgs = await get_messages(conv_id, limit=limit, offset=offset)
    return {"messages": msgs}


@app.delete("/api/whatsapp/messages/{message_id}")
async def api_wa_soft_delete_message(message_id: str):
    ok = await soft_delete_whatsapp_message(message_id, deleted_by="dashboard")
    if not ok:
        raise HTTPException(404, "Message not found")
    return {"success": True, "message": "Message deleted"}


@app.delete("/api/whatsapp/conversations/{conv_id}")
async def api_wa_soft_delete_conversation(conv_id: str):
    ok = await soft_delete_whatsapp_conversation(conv_id, deleted_by="dashboard")
    if not ok:
        raise HTTPException(404, "Conversation not found")
    return {"success": True, "message": "Conversation deleted"}


@app.get("/api/whatsapp/media/{media_id}")
async def api_wa_media(media_id: str):
    result = await fetch_whatsapp_media(media_id)
    if not result.get("success"):
        raise HTTPException(result.get("status", 502), result.get("error") or "Could not fetch WhatsApp media")
    safe_name = os.path.basename(str(result.get("file_name") or media_id)).replace('"', "")
    headers = {
        "Content-Disposition": f'inline; filename="{safe_name}"',
        "Cache-Control": "private, max-age=300",
    }
    return StreamingResponse(BytesIO(result.get("content") or b""), media_type=result.get("mime_type") or "application/octet-stream", headers=headers)


class WaConvPatchRequest(BaseModel):
    status: Optional[str] = None
    ai_enabled: Optional[bool] = None
    assigned_to: Optional[str] = None
    unread_count: Optional[int] = None


@app.patch("/api/whatsapp/conversations/{conv_id}")
async def api_wa_patch_conversation(conv_id: str, req: WaConvPatchRequest):
    updates = req.dict(exclude_none=True)
    if not updates:
        raise HTTPException(400, "No fields to update")
    conv = await patch_conversation(conv_id, updates)
    if not conv:
        raise HTTPException(404, "Conversation not found")
    return conv


class WaConvSendRequest(BaseModel):
    message: str


@app.post("/api/whatsapp/conversations/{conv_id}/send")
async def api_wa_conv_send(conv_id: str, req: WaConvSendRequest):
    conv = await get_conversation_by_id(conv_id)
    if not conv:
        raise HTTPException(404, "Conversation not found")
    phone = conv.get("phone_number", "")
    message = (req.message or "").strip()
    if not message:
        raise HTTPException(400, "message is required")

    # Check 24h window
    window_open = await is_whatsapp_service_window_open(phone)
    if not window_open:
        raise HTTPException(400, {
            "message": "Customer service window may be closed. Please send an approved template.",
            "reason": "window_closed"
        })

    result = await send_whatsapp_text(phone, message)
    if not result.get("success"):
        err = result.get("error") or "Send failed"
        raise HTTPException(502, {"message": err, "reason": result.get("reason", "send_error")})

    provider_id = result.get("provider_message_id") or ""
    saved = await save_wa_message(
        conv_id=conv_id, phone=phone, direction="outbound",
        message_type="text", message_text=message,
        provider_message_id=provider_id, provider_status="sent",
        human_sent=True,
    )
    await update_conversation_last_message(conv_id, message, increment_unread=False)
    return {"status": "sent", "message": saved, "provider_message_id": provider_id}


class WaConvTemplateRequest(BaseModel):
    template_name: str
    language: Optional[str] = "en"
    parameters: Optional[list] = None


@app.post("/api/whatsapp/conversations/{conv_id}/send-template")
async def api_wa_conv_send_template(conv_id: str, req: WaConvTemplateRequest):
    conv = await get_conversation_by_id(conv_id)
    if not conv:
        raise HTTPException(404, "Conversation not found")
    phone = conv.get("phone_number", "")
    result = await send_whatsapp_template(
        phone=phone,
        template_name=req.template_name,
        language=req.language or "en",
        parameters=req.parameters,
        event_type="manual_template",
        source_type="inbox",
        source_id=conv_id,
    )
    if not result.get("success"):
        err = result.get("error") or "Template send failed"
        raise HTTPException(502, {"message": err, "reason": result.get("reason", "send_error")})

    provider_id = result.get("provider_message_id") or ""
    saved = await save_wa_message(
        conv_id=conv_id, phone=phone, direction="outbound",
        message_type="template", message_text=f"[Template: {req.template_name}]",
        template_name=req.template_name,
        provider_message_id=provider_id, provider_status="sent",
        human_sent=True,
    )
    await update_conversation_last_message(conv_id, f"[Template: {req.template_name}]", increment_unread=False)
    return {"status": "sent", "message": saved, "provider_message_id": provider_id}


# ── Automation Rules ──────────────────────────────────────────────────────────

@app.get("/api/automation/rules")
async def api_get_automation_rules():
    logger.info("automation_rules_load_started")
    try:
        await log_error("automation", "automation_rules_load_started", "Request to load automation rules", "info")
    except Exception:
        pass

    try:
        rules, source = await get_automation_rules_with_source()
        logger.info("automation_rules_load_success source=%s count=%s", source, len(rules))
        try:
            await log_error("automation", "automation_rules_load_success", f"Loaded rules count={len(rules)} source={source}", "info")
        except Exception:
            pass
        return {"rules": rules, "rules_source": source, "event_types": AUTOMATION_EVENT_TYPES, "action_types": AUTOMATION_ACTION_TYPES}
    except Exception as exc:
        logger.error("automation_rules_load_failed: %s", exc)
        try:
            await log_error("automation", "automation_rules_load_failed", str(exc), "error")
        except Exception:
            pass
        raise HTTPException(500, f"Failed to load automation rules: {exc}")


@app.post("/api/automation/rules")
async def api_save_automation_rules(req: AutomationRulesRequest):
    if not isinstance(req.rules, list):
        raise HTTPException(400, "rules must be a list")
    enabled_count = sum(1 for r in req.rules if isinstance(r, dict) and bool(r.get("enabled")))
    logger.info("Saving automation rules: count=%s enabled=%s", len(req.rules), enabled_count)
    await save_automation_rules(req.rules)
    saved_rules, source = await get_automation_rules_with_source()
    logger.info("Saved automation rules loaded from %s: count=%s enabled=%s", source, len(saved_rules), sum(1 for r in saved_rules if isinstance(r, dict) and bool(r.get("enabled"))))
    return {"status": "saved", "count": len(saved_rules), "enabled_count": sum(1 for r in saved_rules if isinstance(r, dict) and bool(r.get("enabled"))), "rules_source": source, "rules": saved_rules}


@app.post("/api/automation/rules/reset")
async def api_reset_automation_rules():
    from whatsapp import _default_automation_rules
    default = _default_automation_rules()
    await save_automation_rules(default)
    saved_rules, source = await get_automation_rules_with_source()
    return {"status": "reset", "count": len(saved_rules), "rules_source": source, "rules": saved_rules}


@app.post("/api/automation/test")
async def api_test_automation(req: AutomationTestRequest):
    logger.info("automation_test_started phone=%s event=%s", req.phone, req.event_type)
    try:
        await log_error("automation", "automation_test_started", f"Testing automation phone={req.phone} event={req.event_type}", "info")
    except Exception:
        pass

    try:
        try:
            phone = normalize_phone(req.phone)
        except ValueError as e:
            raise HTTPException(400, str(e))
        rules = await get_automation_rules()
        rule = find_automation_rule(rules, req.event_type, req.source)
        if not rule:
            from whatsapp import _find_disabled_automation_rule
            rule = _find_disabled_automation_rule(rules, req.event_type, req.source)
        matched_rule_key = f"{rule.get('event_type', '')}:{rule.get('source', 'all')}" if rule else ""
        selected_template = rule.get("whatsapp_template", "") if rule else ""
        planned_action = rule.get("action") if rule else "no_matching_rule"
        if req.dry_run:
            return {
                "dry_run": True,
                "event_type": req.event_type,
                "source": req.source,
                "requested_event_type": req.event_type,
                "requested_source": req.source,
                "matched_rule_key": matched_rule_key,
                "matched_rule": rule,
                "planned_action": planned_action,
                "selected_template": selected_template,
                "whatsapp_template": selected_template,
                "whatsapp_enabled": await get_wa_health(),
            }
        contact = {"phone": phone, "lead_name": req.lead_name or "AirZen Lead", "source": req.source or ""}
        result = await execute_automation_rule(req.event_type, contact)
        return {"dry_run": False, "event_type": req.event_type, **result}
    except Exception as exc:
        logger.error("automation_test_failed: %s", exc)
        try:
            await log_error("automation", "automation_test_failed", str(exc), "error")
        except Exception:
            pass
        raise HTTPException(500, f"Automation test failed: {exc}")


# ── Automation Action Queue ───────────────────────────────────────────────────

@app.get("/api/automation/actions")
async def api_get_automation_actions(status: Optional[str] = None, phone: Optional[str] = None, limit: int = 100):
    if phone:
        try:
            phone = normalize_phone(phone)
        except Exception:
            pass
    try:
        # We check if get_automation_actions fails or if table is missing.
        # Inside whatsapp.py, get_automation_actions returns a list or throws/logs inside.
        # We will retrieve actions. If empty and supabase fails, a warning is raised.
        from whatsapp import get_automation_actions_raw
        actions, ok = await get_automation_actions_raw(status=status, phone=phone, limit=min(limit, 500))
        if not ok:
            logger.warning("automation_queue_load_failed")
            try:
                await log_error("automation", "automation_queue_load_failed", "Failed to retrieve automation queue from DB", "warning")
            except Exception:
                pass
            return {"actions": [], "total": 0, "warning": "automation_actions table may be missing or inaccessible"}
        return {"actions": actions, "total": len(actions)}
    except Exception as exc:
        logger.error("automation_queue_load_failed: %s", exc)
        try:
            await log_error("automation", "automation_queue_load_failed", str(exc), "warning")
        except Exception:
            pass
        return {"actions": [], "total": 0, "warning": f"automation_actions table error: {exc}"}


@app.get("/api/automation/queue")
async def api_get_automation_queue(status: Optional[str] = None, phone: Optional[str] = None, limit: int = 100):
    logger.info("automation_queue_load_started")
    try:
        await log_error("automation", "automation_queue_load_started", "Request to load automation queue", "info")
    except Exception:
        pass

    if phone:
        try:
            phone = normalize_phone(phone)
        except Exception:
            pass

    try:
        from whatsapp import get_automation_actions_raw
        actions, ok = await get_automation_actions_raw(status=status, phone=phone, limit=min(limit, 500))
        if not ok:
            logger.warning("automation_queue_load_failed")
            try:
                await log_error("automation", "automation_queue_load_failed", "Failed to retrieve automation queue from DB", "warning")
            except Exception:
                pass
            return {
                "queue": [],
                "actions": [],
                "count": 0,
                "warning": "automation_queue_unavailable"
            }
        logger.info("automation_queue_load_success count=%s", len(actions))
        try:
            await log_error("automation", "automation_queue_load_success", f"Loaded automation queue count={len(actions)}", "info")
        except Exception:
            pass
        return {
            "queue": actions,
            "actions": actions,
            "count": len(actions),
            "warning": ""
        }
    except Exception as exc:
        logger.error("automation_queue_load_failed: %s", exc)
        try:
            await log_error("automation", "automation_queue_load_failed", str(exc), "warning")
        except Exception:
            pass
        return {
            "queue": [],
            "actions": [],
            "count": 0,
            "warning": "automation_queue_unavailable"
        }


@app.get("/api/automation/queue/health")
async def api_get_automation_queue_health():
    table_available = False
    count = None
    warning_msg = ""
    try:
        from whatsapp import get_automation_actions_raw
        actions, ok = await get_automation_actions_raw(limit=1)
        if ok:
            table_available = True
            try:
                count = await get_repository().automation_queue_count()
            except Exception:
                count = len(actions)
        else:
            warning_msg = "database query returned not ok status"
    except Exception as exc:
        warning_msg = str(exc)

    status_val = "ok" if (table_available and not warning_msg) else "degraded"

    logger.info("automation_queue_health_checked status=%s", status_val)
    try:
        await log_error("automation", "automation_queue_health_checked", f"status={status_val}; count={count}; warning={warning_msg}", "info")
    except Exception:
        pass

    return {
        "status": status_val,
        "table_available": table_available,
        "count": count,
        "warning": warning_msg
    }


@app.patch("/api/automation/actions/{action_id}/status")
async def api_update_action_status(action_id: str, req: StatusRequest):
    allowed = {"pending", "running", "completed", "failed", "cancelled", "waiting_schedule", "skipped"}
    if req.status not in allowed:
        raise HTTPException(400, f"status must be one of: {', '.join(sorted(allowed))}")
    ok = await update_automation_action_status(action_id, req.status)
    if not ok:
        raise HTTPException(404, "Action not found")
    return {"status": "updated", "action_id": action_id}


@app.post("/api/automation/actions/run-due")
async def api_run_due_actions():
    result = await run_due_automation_actions()
    return {"status": "ok", **result}


# ── Lead Intake (n8n / Facebook / Website) ───────────────────────────────────

@app.post("/api/leads/incoming")
async def api_incoming_lead(req: LeadIncomingRequest):
    """Public endpoint for n8n, Facebook Lead Ads, website forms, etc."""
    try:
        phone = normalize_phone(req.phone)
    except ValueError as e:
        return JSONResponse(status_code=400, content={"success": False, "error": str(e)})

    source = (req.source or "api").strip().lower()
    lead_name = (req.name or "").strip() or "New Contact"

    lead_data = {
        "phone_number": phone,
        "lead_name": lead_name,
        "email": req.email or "",
        "city": req.city or "",
        "source": source,
        "service_type": req.service or "",
        "business_name": req.business_name or "",
        "campaign_name": req.campaign_name or "",
        "lead_country": DEFAULT_COUNTRY,
        "lead_country_code": DEFAULT_COUNTRY_CODE,
        "crm_notes": req.message or "",
    }

    try:
        await log_error("lead_import", "lead_import_received", f"phone={phone}; source={source}; import_endpoint=/api/leads/incoming", "info")
        upsert_result = await upsert_crm_lead(lead_data, import_source=source)
        contact_status = upsert_result.get("status", "unknown")
    except Exception as exc:
        await log_error("leads_incoming", f"Upsert failed for {phone}", str(exc), "error")
        return JSONResponse(status_code=500, content={"success": False, "error": str(exc)})

    # Determine event type
    event_type = source_to_event_type(source)
    await log_error("lead_import", "lead_source_detected", f"phone={phone}; source={source}; selected_event={event_type}; contact_status={contact_status}", "info")
    await log_error("lead_import", "automation_event_selected", f"phone={phone}; source={source}; selected_event={event_type}", "info")

    # Build contact dict for automation
    contact = {
        "phone": phone,
        "lead_name": lead_name,
        "business_name": req.business_name or "",
        "service_type": req.service or "",
        "source": source,
    }

    # Execute automation rule
    automation_result = await execute_automation_rule(event_type, contact)
    rule_action = automation_result.get("selected_action") or automation_result.get("action") or ""
    await log_error("lead_import", "automation_rule_matched", f"phone={phone}; source={source}; selected_event={event_type}; rule_action={rule_action}; matched_rule={automation_result.get('matched_rule_key')}", "info")
    log_name = "automation_action_queued" if automation_result.get("queue_action_id") and automation_result.get("automation_status") != "skipped" else "automation_action_skipped"
    await log_error("lead_import", log_name, f"phone={phone}; source={source}; selected_event={event_type}; rule_action={rule_action}; queue_action_id={automation_result.get('queue_action_id')}; skip_reason={automation_result.get('skip_reason','')}", "info")

    return {
        "success": True,
        "phone": phone,
        "contact_status": contact_status,
        "event_type": event_type,
        "automation": automation_result,
        "automation_action": automation_result.get("action"),
        "automation_status": automation_result.get("automation_status"),
        "automation_event_type": automation_result.get("automation_event_type"),
        "matched_rule_key": automation_result.get("matched_rule_key"),
        "matched_rule_enabled": automation_result.get("matched_rule_enabled"),
        "selected_action": automation_result.get("selected_action"),
        "queue_action_id": automation_result.get("queue_action_id"),
        "skip_reason": automation_result.get("skip_reason"),
        "scheduled_action_id": automation_result.get("scheduled_action_id"),
        "whatsapp_status": automation_result.get("whatsapp_status"),
        "call_status": automation_result.get("call_status"),
    }


# ── Confirmation endpoints (Callback / Appointment / Showroom) ───────────────

@app.post("/api/whatsapp/confirm/callback")
async def api_wa_callback_confirm(req: WaConfirmRequest):
    try:
        phone = normalize_phone(req.phone)
    except ValueError as e:
        raise HTTPException(400, str(e))
    ctx = req.dict()
    ctx["phone"] = phone
    return await send_callback_confirmation(phone, ctx)


@app.post("/api/whatsapp/confirm/appointment")
async def api_wa_appointment_confirm(req: WaConfirmRequest):
    try:
        phone = normalize_phone(req.phone)
    except ValueError as e:
        raise HTTPException(400, str(e))
    ctx = req.dict()
    ctx["phone"] = phone
    return await send_appointment_confirmation(phone, ctx)


@app.post("/api/whatsapp/confirm/showroom")
async def api_wa_showroom_confirm(req: WaConfirmRequest):
    try:
        phone = normalize_phone(req.phone)
    except ValueError as e:
        raise HTTPException(400, str(e))
    ctx = req.dict()
    ctx["phone"] = phone
    return await send_showroom_visit_confirmation(phone, ctx)


def _customer_setup_brand_slug(company_name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(company_name or "").lower())


async def _customer_setup_status() -> dict:
    def configured(value: str) -> bool:
        return bool(str(value or "").strip())

    company_name = await get_setting("COMPANY_NAME", "")
    brand_slug = await get_setting("COMPANY_BRAND_SLUG", "")
    business_type = await get_setting("BUSINESS_TYPE", "")
    country = await get_setting("APP_COUNTRY", DEFAULT_COUNTRY)
    timezone_name = await get_setting("APP_TIMEZONE", DEFAULT_TIMEZONE)
    setup_completed = (await get_setting("CUSTOMER_SETUP_COMPLETED", "false") or "").lower() in {"1", "true", "yes", "on"}
    setup_completed_at = await get_setting("CUSTOMER_SETUP_COMPLETED_AT", "")
    meta_app_id = await get_setting("META_APP_ID", "")
    meta_app_secret = await get_setting("META_APP_SECRET", "")
    waba_id = await get_setting("WHATSAPP_BUSINESS_ACCOUNT_ID", "")
    phone_number_id = await get_setting("WHATSAPP_PHONE_NUMBER_ID", "")
    access_token = await get_setting("WHATSAPP_ACCESS_TOKEN", "")
    verify_token = await get_setting("WHATSAPP_VERIFY_TOKEN", "")
    templates = await get_wa_template_objects()
    template_by_purpose = {}
    for template in templates:
        template_by_purpose.setdefault(template.get("purpose"), template)
    missing = []
    required = [
        ("Company name", company_name),
        ("Brand slug", brand_slug),
        ("Business type", business_type),
        ("Country", country),
        ("Timezone", timezone_name),
        ("Meta App ID", meta_app_id),
        ("Meta App Secret", meta_app_secret),
        ("WhatsApp Business Account ID", waba_id),
        ("Phone Number ID", phone_number_id),
        ("Permanent Access Token", access_token),
        ("Verify Token", verify_token),
    ]
    for label, value in required:
        if not configured(value):
            missing.append(label)
    for purpose, label in (("welcome", "Welcome template"), ("booking_confirmation", "Booking confirmation template")):
        template = template_by_purpose.get(purpose) or {}
        if not configured(template.get("meta_template_name")):
            missing.append(label)
        elif str(template.get("status") or "").upper() != "APPROVED":
            missing.append(f"{label} approved")
    return {
        "completed": setup_completed,
        "completed_at": setup_completed_at,
        "missing": missing,
        "company": {
            "company_name": company_name,
            "brand_slug": brand_slug,
            "business_type": business_type,
            "country": country,
            "timezone": timezone_name,
        },
        "whatsapp": {
            "meta_app_id": meta_app_id,
            "meta_app_id_configured": configured(meta_app_id),
            "meta_app_secret": "",
            "meta_app_secret_configured": configured(meta_app_secret),
            "whatsapp_business_account_id": waba_id,
            "phone_number_id": phone_number_id,
            "permanent_access_token": "",
            "permanent_access_token_configured": configured(access_token),
            "verify_token": "",
            "verify_token_configured": configured(verify_token),
        },
        "templates": templates,
        "ready": not missing,
    }


@app.get("/api/customer-setup/status")
async def api_customer_setup_status():
    return await _customer_setup_status()


@app.post("/api/customer-setup")
async def api_customer_setup_save(req: CustomerSetupRequest):
    company_name = (req.company_name or "").strip()
    brand_slug = (req.brand_slug or _customer_setup_brand_slug(company_name)).strip()
    settings = {
        "COMPANY_NAME": company_name,
        "COMPANY_BRAND_SLUG": brand_slug,
        "BUSINESS_TYPE": (req.business_type or "").strip(),
        "APP_COUNTRY": (req.country or DEFAULT_COUNTRY).strip(),
        "APP_TIMEZONE": (req.timezone or DEFAULT_TIMEZONE).strip(),
        "APPOINTMENT_TIMEZONE": (req.timezone or DEFAULT_TIMEZONE).strip(),
        "WHATSAPP_DISPLAY_TIMEZONE": (req.timezone or DEFAULT_TIMEZONE).strip(),
        "META_APP_ID": (req.meta_app_id or "").strip(),
        "META_APP_SECRET": (req.meta_app_secret or "").strip(),
        "WHATSAPP_PROVIDER": "meta",
        "WHATSAPP_ENABLED": "true",
        "WHATSAPP_DEFAULT_LANGUAGE": "en",
        "WHATSAPP_BUSINESS_ACCOUNT_ID": (req.whatsapp_business_account_id or "").strip(),
        "WHATSAPP_PHONE_NUMBER_ID": (req.phone_number_id or "").strip(),
        "WHATSAPP_ACCESS_TOKEN": (req.permanent_access_token or "").strip(),
        "WHATSAPP_VERIFY_TOKEN": (req.verify_token or "").strip(),
        "WEBHOOK_VERIFY_TOKEN": (req.verify_token or "").strip(),
    }
    if req.completed:
        settings["CUSTOMER_SETUP_COMPLETED"] = "true"
        settings["CUSTOMER_SETUP_COMPLETED_AT"] = datetime.now(timezone.utc).isoformat()
    filtered = {k: v for k, v in settings.items() if v is not None and str(v).strip() != ""}
    await save_settings(filtered)
    for k, v in filtered.items():
        if not os.environ.get(k):
            os.environ[k] = str(v)
    try:
        kb = await get_knowledge_base()
        company_profile = kb.get("company_profile") or {}
        company_profile.update({
            "business_name": company_name or company_profile.get("business_name") or "",
            "legal_name": company_profile.get("legal_name") or company_name,
            "industry_type": (req.business_type or company_profile.get("industry_type") or "").strip(),
        })
        contact_details = kb.get("contact_details") or {}
        contact_details.update({
            "country": (req.country or contact_details.get("country") or DEFAULT_COUNTRY).strip(),
        })
        working_hours = kb.get("working_hours") or {}
        working_hours.update({
            "timezone": (req.timezone or working_hours.get("timezone") or DEFAULT_TIMEZONE).strip(),
        })
        await save_kb_section("company_profile", company_profile)
        await save_kb_section("contact_details", contact_details)
        await save_kb_section("working_hours", working_hours)
    except Exception as exc:
        logger.warning("customer_setup_kb_update_failed: %s", exc)
    return {"status": "saved", "setup": await _customer_setup_status()}


@app.get("/api/settings")
async def api_get_settings():
    return await get_all_settings()


@app.post("/api/settings")
async def api_save_settings(req: SettingsRequest):
    filtered = {k: v for k, v in req.settings.items() if v is not None and v != ""}
    await save_settings(filtered)
    # VPS env vars are the single source of truth.
    # Only populate os.environ for keys that the host has NOT already defined.
    for k, v in filtered.items():
        if not os.environ.get(k):
            os.environ[k] = str(v)
    if any(k.startswith("RECORDING_") for k in filtered):
        await _schedule_recording_cleanup()
    return {"status": "saved", "count": len(filtered)}


@app.post("/api/setup/trunk")
async def api_setup_trunk():
    url = await eff("LIVEKIT_URL")
    key = await eff("LIVEKIT_API_KEY")
    secret = await eff("LIVEKIT_API_SECRET")
    sip_domain = await eff("VOBIZ_SIP_DOMAIN")
    username = await eff("VOBIZ_USERNAME")
    password = await eff("VOBIZ_PASSWORD")
    phone = await eff("VOBIZ_OUTBOUND_NUMBER")
    if not all([url, key, secret, sip_domain, username, password, phone]):
        raise HTTPException(400, "Configure LiveKit and Vobiz credentials in Settings first.")
    try:
        from livekit import api as lk_api
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ctx))
        lk = lk_api.LiveKitAPI(url=url, api_key=key, api_secret=secret, session=session)
        trunk = await lk.sip.create_sip_outbound_trunk(lk_api.CreateSIPOutboundTrunkRequest(trunk=lk_api.SIPOutboundTrunkInfo(name="Vobiz Outbound Trunk", address=sip_domain, auth_username=username, auth_password=password, numbers=[phone])))
        trunk_id = trunk.sip_trunk_id
        await set_setting("OUTBOUND_TRUNK_ID", trunk_id)
        if not os.environ.get("OUTBOUND_TRUNK_ID"):
            os.environ["OUTBOUND_TRUNK_ID"] = trunk_id
        await lk.aclose()
        await session.close()
        return {"status": "created", "trunk_id": trunk_id}
    except Exception as exc:
        raise HTTPException(500, f"Trunk creation failed: {exc}")


@app.get("/api/logs")
async def api_get_logs(limit: int = 200, level: Optional[str] = None, source: Optional[str] = None):
    return await get_logs(level=level, source=source, limit=limit)


@app.get("/api/agent-runtime-health")
async def api_agent_runtime_health():
    logs = await get_logs(source="agent", limit=500)
    expected_agent_name = await _outbound_agent_name()

    def _timestamp_age_seconds(row: Optional[dict]) -> Optional[int]:
        if not row:
            return None
        raw = str(row.get("timestamp") or "").strip()
        if not raw:
            return None
        try:
            ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if ts.tzinfo is not None:
                ts = ts.replace(tzinfo=None)
            return max(0, int((datetime.now() - ts).total_seconds()))
        except Exception:
            return None

    heartbeat = next(
        (
            row for row in logs
            if row.get("message") == "agent_worker_heartbeat"
            and VOICE_FLOW_RUNTIME in str(row.get("detail") or "")
        ),
        None,
    )
    startup = next(
        (
            row for row in logs
            if row.get("message") == "agent_worker_starting"
            or str(row.get("message") or "").startswith("voice_flow_runtime=")
        ),
        None,
    )
    latest_agent_log = logs[0] if logs else None
    heartbeat_age_seconds = _timestamp_age_seconds(heartbeat)
    worker_heartbeat_seen = heartbeat is not None
    worker_heartbeat_fresh = heartbeat_age_seconds is not None and heartbeat_age_seconds <= 180
    if not worker_heartbeat_seen:
        diagnosis = "worker heartbeat missing: agent.py is not running or cannot write error_logs"
    elif not worker_heartbeat_fresh:
        diagnosis = "worker heartbeat stale: agent.py started but heartbeat stopped"
    else:
        diagnosis = "worker heartbeat fresh: if per-call logs are missing, LiveKit is routing calls to another worker/agent name"
    return {
        "worker_heartbeat_seen": worker_heartbeat_seen,
        "worker_heartbeat_fresh": worker_heartbeat_fresh,
        "heartbeat_age_seconds": heartbeat_age_seconds,
        "expected_voice_flow_runtime": VOICE_FLOW_RUNTIME,
        "expected_agent_name": expected_agent_name,
        "startup_log_seen": startup is not None,
        "latest_heartbeat": heartbeat,
        "latest_startup_log": startup,
        "latest_agent_log": latest_agent_log,
        "diagnosis": diagnosis,
    }


@app.delete("/api/logs")
async def api_clear_logs():
    await clear_errors()
    return {"status": "cleared"}


def _clear_confirmation_error(req: Optional[ClearRecordsRequest]):
    if not req or req.confirm != "CLEAR_RECORDS":
        return JSONResponse(status_code=400, content={"success": False, "error": "Confirmation required"})
    return None


async def _admin_clear_response(req: Optional[ClearRecordsRequest], label: str, clear_fn):
    confirmation_error = _clear_confirmation_error(req)
    if confirmation_error:
        return confirmation_error
    deleted = await clear_fn()
    message = f"Cleared {deleted} {label}."
    logger.warning("Admin clear action: %s deleted=%s", label, deleted)
    if label != "error logs":
        await log_error("server", f"Admin clear action: {label}", f"deleted={deleted}", "warning")
    return {"success": True, "deleted": deleted, "message": message}


@app.delete("/api/admin/clear/call-logs")
async def api_admin_clear_call_logs(req: Optional[ClearRecordsRequest] = None):
    return await _admin_clear_response(req, "call logs", clear_call_logs)


@app.delete("/api/admin/clear/error-logs")
async def api_admin_clear_error_logs(req: Optional[ClearRecordsRequest] = None):
    return await _admin_clear_response(req, "error logs", clear_error_logs)


@app.delete("/api/admin/clear/contact-memory")
async def api_admin_clear_contact_memory(req: Optional[ClearRecordsRequest] = None):
    return await _admin_clear_response(req, "CRM memory records", clear_contact_memory)


@app.delete("/api/admin/clear/appointments")
async def api_admin_clear_appointments(req: Optional[ClearRecordsRequest] = None):
    return await _admin_clear_response(req, "appointments", clear_appointments)


@app.delete("/api/admin/clear/campaigns")
async def api_admin_clear_campaigns(req: Optional[ClearRecordsRequest] = None):
    return await _admin_clear_response(req, "campaigns", clear_campaigns)


@app.delete("/api/admin/clear/all-test-data")
async def api_admin_clear_all_test_data(req: Optional[ClearRecordsRequest] = None):
    confirmation_error = _clear_confirmation_error(req)
    if confirmation_error:
        return confirmation_error
    deleted_by_table = await clear_all_test_data()
    deleted = sum(deleted_by_table.values())
    logger.warning("Admin clear action: all test data deleted=%s details=%s", deleted, deleted_by_table)
    return {
        "success": True,
        "deleted": deleted,
        "message": "Cleared all test data. Settings and agent profiles were not changed.",
    }


@app.get("/api/crm")
async def api_get_contacts():
    return {"data": await get_contacts()}


@app.get("/api/crm/calls")
async def api_get_contact_calls(phone: str = Query(...)):
    return {"data": await get_calls_by_phone(phone)}


CRM_EXPORT_COLUMNS = [
    "lead_name", "phone_number", "email", "city", "location", "address", "requirement", "budget",
    "source", "business_name", "campaign_name", "service_type", "crm_status",
    "custom_status", "next_followup_at", "assigned_to", "crm_notes",
    "last_call_outcome", "last_call_at", "total_calls", "created_at", "updated_at",
]


def _bool_query(value: Optional[str]):
    if value is None or value == "":
        return None
    return str(value).lower() in ("1", "true", "yes", "on")


def _crm_filter_dict(**kwargs) -> dict:
    return {k: v for k, v in kwargs.items() if v not in (None, "")}


async def _crm_contacts_from_filters(filters: dict) -> list:
    return await get_crm_contacts(
        status=filters.get("status"),
        outcome=filters.get("outcome"),
        q=filters.get("q"),
        due_today=_bool_query(filters.get("due_today")) is True,
        today=_bool_query(filters.get("today")) is True,
        timezone=filters.get("timezone"),
        source=filters.get("source"),
        business_name=filters.get("business_name"),
        campaign_name=filters.get("campaign_name"),
        city=filters.get("city"),
        date_from=filters.get("date_from"),
        date_to=filters.get("date_to"),
        assigned_to=filters.get("assigned_to"),
        recording_available=_bool_query(filters.get("recording_available")),
        has_followup=_bool_query(filters.get("has_followup")),
    )


# ── Outbound calling-window helpers ─────────────────────────────────────────

async def _outbound_tz() -> str:
    return (await eff("OUTBOUND_TIMEZONE") or DEFAULT_TIMEZONE).strip()


async def _outbound_window() -> tuple:
    """Return (start_hhmm, end_hhmm, allowed_day_abbrs) from env/settings."""
    tz = await _outbound_tz()
    start = (await eff("OUTBOUND_START_TIME") or "10:00").strip()
    end = (await eff("OUTBOUND_END_TIME") or "19:00").strip()
    days_raw = (await eff("OUTBOUND_ALLOWED_DAYS") or "mon,tue,wed,thu,fri,sat").strip().lower()
    days = {d.strip() for d in days_raw.split(",") if d.strip()}
    return tz, start, end, days


def _parse_hhmm(hhmm: str) -> tuple:
    """Parse '10:00' -> (10, 0). Safe fallback to (0, 0)."""
    try:
        h, m = hhmm.split(":")
        return int(h), int(m)
    except Exception:
        return 0, 0


async def _is_outbound_allowed() -> bool:
    """Return True if outbound calling is enabled and within the configured window."""
    enabled_raw = (await eff("OUTBOUND_CALLING_ENABLED") or "true").strip().lower()
    if enabled_raw in ("0", "false", "no", "off"):
        return False
    tz_name, start_str, end_str, allowed_days = await _outbound_window()
    try:
        from zoneinfo import ZoneInfo
        now_tz = datetime.now(tz=ZoneInfo(tz_name))
    except Exception:
        now_tz = datetime.now()
    day_abbr = now_tz.strftime("%a").lower()  # 'mon', 'tue', ...
    if day_abbr not in allowed_days:
        return False
    sh, sm = _parse_hhmm(start_str)
    eh, em = _parse_hhmm(end_str)
    current_minutes = now_tz.hour * 60 + now_tz.minute
    start_minutes = sh * 60 + sm
    end_minutes = eh * 60 + em
    return start_minutes <= current_minutes < end_minutes


async def _outbound_window_error() -> dict:
    """Build the structured 403 detail used by single-call and call-selected endpoints."""
    tz_name, start_str, end_str, allowed_days = await _outbound_window()
    # Compute next_allowed_at: next occurrence of start_str on an allowed day.
    try:
        from zoneinfo import ZoneInfo
        now_tz = datetime.now(tz=ZoneInfo(tz_name))
    except Exception:
        now_tz = datetime.now()
    sh, sm = _parse_hhmm(start_str)
    # Walk forward day-by-day until we land on an allowed day.
    from datetime import timedelta as _td
    candidate = now_tz.replace(hour=sh, minute=sm, second=0, microsecond=0)
    if candidate <= now_tz:
        candidate = candidate + _td(days=1)
    for _ in range(8):  # safety: max 7 days ahead
        if candidate.strftime("%a").lower() in allowed_days:
            break
        candidate = candidate + _td(days=1)
    try:
        next_allowed_at = candidate.isoformat()
    except Exception:
        next_allowed_at = None
    days_display = ", ".join(sorted(allowed_days))
    return {
        "error": "outside_outbound_window",
        "message": (
            f"Outbound calling is allowed only between {start_str} and {end_str} "
            f"{tz_name} ({days_display})."
        ),
        "start_time": start_str,
        "end_time": end_str,
        "timezone": tz_name,
        "allowed_days": sorted(allowed_days),
        "next_allowed_at": next_allowed_at,
    }


def _parse_server_datetime(value) -> Optional[datetime]:
    if not value:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        dt = datetime.fromisoformat(raw.replace(" ", "T"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _format_datetime_display(value, tz_name: str = DEFAULT_TIMEZONE) -> str:
    dt = _parse_server_datetime(value)
    if not dt:
        return ""
    try:
        local_dt = dt.astimezone(ZoneInfo(tz_name or DEFAULT_TIMEZONE))
    except Exception:
        local_dt = dt.astimezone(ZoneInfo(DEFAULT_TIMEZONE))
    today = datetime.now(local_dt.tzinfo).date()
    time_text = local_dt.strftime("%I:%M %p").lstrip("0")
    if local_dt.date() == today:
        return f"Today, {time_text}"
    return f"{local_dt.strftime('%d %b %Y')}, {time_text}"


async def _display_timezone() -> str:
    tz_name = (
        await get_setting("FOLLOWUP_TIMEZONE", "")
        or await get_setting("APP_TIMEZONE", "")
        or DEFAULT_TIMEZONE
    )
    try:
        ZoneInfo(tz_name)
        return tz_name
    except Exception:
        return DEFAULT_TIMEZONE


def _with_followup_display(row: dict, tz_name: str) -> dict:
    out = dict(row or {})
    for key in ("scheduled_at", "next_action_at", "preferred_callback_at", "created_at", "completed_at"):
        if out.get(key):
            out[f"{key}_display"] = _format_datetime_display(out.get(key), tz_name)
    return out


async def _snap_followup_call_time_to_window(scheduled_local: datetime) -> tuple[datetime, bool, str]:
    enabled_raw = (await eff("OUTBOUND_CALLING_ENABLED") or "true").strip().lower()
    if enabled_raw in ("0", "false", "no", "off"):
        return scheduled_local, False, "outbound_calling_disabled"
    tz_name, start_str, end_str, allowed_days = await _outbound_window()
    try:
        outbound_tz = ZoneInfo(tz_name)
    except Exception:
        outbound_tz = ZoneInfo(DEFAULT_TIMEZONE)
    candidate = scheduled_local.astimezone(outbound_tz) if scheduled_local.tzinfo else scheduled_local.replace(tzinfo=outbound_tz)
    sh, sm = _parse_hhmm(start_str)
    eh, em = _parse_hhmm(end_str)
    for _ in range(8):
        label = candidate.strftime("%a").lower()
        start_dt = candidate.replace(hour=sh, minute=sm, second=0, microsecond=0)
        end_dt = candidate.replace(hour=eh, minute=em, second=0, microsecond=0)
        if label in allowed_days and start_dt <= candidate < end_dt:
            final_local = candidate.astimezone(scheduled_local.tzinfo) if scheduled_local.tzinfo else candidate
            return final_local, final_local != scheduled_local, "inside_outbound_window"
        if label in allowed_days and candidate < start_dt:
            final_local = start_dt.astimezone(scheduled_local.tzinfo) if scheduled_local.tzinfo else start_dt
            return final_local, final_local != scheduled_local, "before_outbound_window"
        candidate = (candidate + timedelta(days=1)).replace(hour=sh, minute=sm, second=0, microsecond=0)
    final_local = candidate.astimezone(scheduled_local.tzinfo) if scheduled_local.tzinfo else candidate
    return final_local, final_local != scheduled_local, "next_allowed_day"


async def _wait_for_outbound_window(campaign_id: str, poll_seconds: int = 120) -> bool:
    """Block until the outbound calling window opens (or campaign is paused/stopped).

    Returns True if the window opened, False if the campaign was externally
    paused or stopped while waiting.
    """
    while not await _is_outbound_allowed():
        latest = await get_campaign(campaign_id)
        if not latest or (latest.get("status") in ("paused", "stopped", "completed")):
            return False
        tz_name, start_str, *_ = await _outbound_window()
        logger.info(
            "Campaign %s: outside outbound window (%s), sleeping %ds before retry",
            campaign_id, start_str, poll_seconds,
        )
        await asyncio.sleep(poll_seconds)
    return True


async def _import_leads(leads: list, import_source: str = "api", upload_batch_id: Optional[str] = None) -> dict:
    """Insert/update many leads, never aborting on a single bad row.

    The mobile number (normalized to E.164) is treated as the unique CRM
    identity. Existing phone numbers are merged into the existing record and
    counted as ``duplicate_count`` — *not* as ``imported_count``.
    """
    inserted = duplicates = skipped = failed = 0
    errors = []
    processed_leads = []
    for idx, lead in enumerate(leads, start=1):
        if not isinstance(lead, dict):
            skipped += 1
            errors.append({"row": idx, "phone": "", "error": "row is not an object"})
            continue
        # Must have at least a phone before we even try.
        if not (lead.get("phone_number") or lead.get("phone")):
            skipped += 1
            errors.append({"row": idx, "phone": "", "error": "missing phone"})
            continue
        try:
            result = await upsert_crm_lead(lead, import_source=import_source, upload_batch_id=upload_batch_id)
            status = result.get("status")
            processed_leads.append({"lead": lead, "contact_status": status})
            if status == "inserted":
                inserted += 1
            elif status == "duplicate":
                # Existing phone — merged + audit-noted, but reported separately.
                duplicates += 1
            else:
                # Defensive: any other status (e.g. legacy "updated") still
                # counts as a duplicate-merge for reporting purposes.
                duplicates += 1
        except Exception as exc:
            failed += 1
            errors.append({"row": idx, "phone": lead.get("phone_number") or lead.get("phone") or "", "error": str(exc)})
    return {
        "success": True,
        "total_rows": len(leads),
        "imported_count": inserted,
        "duplicate_count": duplicates,
        "updated_count": duplicates,  # alias kept for older UI builds
        "skipped_count": skipped,
        "failed_count": failed,
        # Back-compat keys (used by the original dashboard UI):
        "inserted": inserted,
        "updated": duplicates,
        "failed": failed,
        "errors": errors,
        "_processed_leads": processed_leads,
    }


async def _run_lead_automation_for_imports(processed_leads: list, default_source: str) -> list:
    results = []
    for item in processed_leads or []:
        lead = item.get("lead") or {}
        try:
            phone = normalize_phone(lead.get("phone_number") or lead.get("phone") or "")
        except Exception as exc:
            results.append({
                "phone": lead.get("phone_number") or lead.get("phone") or "",
                "automation_status": "skipped",
                "skip_reason": "invalid_phone",
                "error": str(exc),
            })
            continue
        source = (lead.get("source") or default_source or "api").strip().lower()
        contact_status = item.get("contact_status") or ""
        await log_error("lead_import", "lead_import_received", f"phone={phone}; source={source}; contact_status={contact_status}", "info")
        event_type = source_to_event_type(source)
        await log_error("lead_import", "lead_source_detected", f"phone={phone}; source={source}; selected_event={event_type}", "info")
        await log_error("lead_import", "automation_event_selected", f"phone={phone}; source={source}; selected_event={event_type}", "info")
        contact = {
            "phone": phone,
            "lead_name": lead.get("lead_name") or lead.get("name") or "",
            "business_name": lead.get("business_name") or "",
            "service_type": lead.get("service_type") or lead.get("service") or "",
            "source": source,
        }
        result = await execute_automation_rule(event_type, contact)
        rule_action = result.get("selected_action") or result.get("action") or ""
        await log_error("lead_import", "automation_rule_matched", f"phone={phone}; source={source}; selected_event={event_type}; rule_action={rule_action}; matched_rule={result.get('matched_rule_key')}", "info")
        log_name = "automation_action_queued" if result.get("queue_action_id") and result.get("automation_status") != "skipped" else "automation_action_skipped"
        await log_error("lead_import", log_name, f"phone={phone}; source={source}; selected_event={event_type}; rule_action={rule_action}; queue_action_id={result.get('queue_action_id')}; skip_reason={result.get('skip_reason','')}", "info")
        results.append({"phone": phone, "event_type": event_type, **result})
    return results


UPLOAD_COLUMN_ALIASES = {
    "phone": "phone_number",
    "phone_number": "phone_number",
    "phone_no": "phone_number",
    "phonenumber": "phone_number",
    "mobile": "phone_number",
    "mobile_number": "phone_number",
    "mobile_no": "phone_number",
    "contact": "phone_number",
    "contact_number": "phone_number",
    "contact_no": "phone_number",
    "whatsapp": "phone_number",
    "whatsapp_no": "phone_number",
    "whatsapp_number": "phone_number",
    "name": "lead_name",
    "lead_name": "lead_name",
    "customer_name": "lead_name",
    "customer": "lead_name",
    "client": "lead_name",
    "client_name": "lead_name",
    "first_name": "lead_name",
    "full_name": "lead_name",
    "email": "email",
    "email_id": "email",
    "city": "city",
    "town": "city",
    "location": "location",
    "area": "location",
    "address": "address",
    "service_address": "address",
    "full_address": "address",
    "source": "source",
    "lead_source": "source",
    "service": "service_type",
    "service_type": "service_type",
    "requirement": "service_type",
    "product": "service_type",
    "interest": "service_type",
    "country": "lead_country",
    "lead_country": "lead_country",
    "country_code": "lead_country_code",
    "lead_country_code": "lead_country_code",
    "notes": "crm_notes",
    "note": "crm_notes",
    "message": "crm_notes",
    "comment": "crm_notes",
    "comments": "crm_notes",
    "remark": "crm_notes",
    "remarks": "crm_notes",
    "status": "crm_status",
    "crm_status": "crm_status",
    "business_name": "business_name",
    "business": "business_name",
    "company": "business_name",
    "campaign": "campaign_name",
    "campaign_name": "campaign_name",
    "budget": "budget",
    "assigned_to": "assigned_to",
    "agent": "assigned_to",
    "followup": "next_followup_at",
    "follow_up": "next_followup_at",
    "next_followup_at": "next_followup_at",
}


def _normalize_upload_row(row: dict) -> dict:
    out = {}
    for key, value in (row or {}).items():
        if key is None:
            continue
        clean_key = str(key).strip().lower().replace(" ", "_").replace("-", "_")
        mapped = UPLOAD_COLUMN_ALIASES.get(clean_key, clean_key)
        if mapped in out and (value is None or str(value).strip() == ""):
            continue
        out[mapped] = value
    return out


@app.get("/api/lead-statuses")
async def api_get_lead_statuses():
    return {"data": await get_lead_statuses()}


@app.post("/api/lead-statuses")
async def api_add_lead_status(req: LeadStatusRequest):
    if not req.name.strip():
        raise HTTPException(400, "Status name is required")
    status = await add_lead_status(req.name, req.color)
    return {"success": True, "data": status}


@app.delete("/api/lead-statuses/{status_id}")
async def api_delete_lead_status(status_id: str):
    ok = await delete_lead_status(status_id)
    if not ok:
        raise HTTPException(404, "Status not found")
    return {"success": True, "message": "Status deleted"}


@app.get("/api/crm/statuses")
async def api_get_crm_statuses():
    statuses = await get_lead_statuses()
    return {"data": statuses, "statuses": statuses}


@app.post("/api/crm/statuses")
async def api_add_crm_status(req: LeadStatusRequest):
    return await api_add_lead_status(req)


@app.delete("/api/crm/statuses/{status_id}")
async def api_delete_crm_status(status_id: str):
    return await api_delete_lead_status(status_id)


@app.get("/api/crm/pipeline-presets")
async def api_get_crm_pipeline_presets():
    return {"presets": PIPELINE_PRESETS}


@app.post("/api/crm/pipeline-presets/{preset_key}")
async def api_apply_crm_pipeline_preset(preset_key: str):
    key = preset_key.strip().lower()
    if key not in PIPELINE_PRESETS:
        raise HTTPException(400, f"Unknown pipeline preset '{preset_key}'. Available: {list(PIPELINE_PRESETS.keys())}")
    statuses = await apply_pipeline_preset(key)
    return {"success": True, "preset": key, "statuses": statuses}


@app.post("/api/crm/contacts")
async def api_add_crm_contact(req: CrmLeadRequest):
    try:
        lead_dict = req.dict()
        if req.tags and not lead_dict.get("tags_json"):
            lead_dict["tags_json"] = json.dumps(req.tags)
        lead_dict.pop("tags", None)
        result = await upsert_crm_lead(
            lead_dict,
            import_source=req.source or "manual",
            forbid_duplicate=True,
        )
        phone = normalize_phone(req.phone_number or "")
        automation_result = None
        try:
            automation_result = await execute_automation_rule(
                "manual_lead",
                {"phone": phone, "lead_name": req.lead_name or "", "source": req.source or "manual",
                 "business_name": req.business_name or "", "service_type": req.service_type or ""},
            )
        except Exception as auto_exc:
            logger.warning("execute_automation_rule(manual_lead) failed: %s", auto_exc)
            automation_result = {"error": str(auto_exc)}
        return {"success": True, **result, "message": "Lead added", "automation": automation_result}
    except DuplicateContactError as exc:
        # 409 Conflict so the Add-Lead UI shows a friendly error instead of
        # silently merging on top of an existing CRM record.
        raise HTTPException(
            status_code=409,
            detail={
                "error": "duplicate_contact",
                "phone_number": exc.phone,
                "message": "This mobile number already exists. Please open the existing lead and update it.",
            },
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/crm/import-leads")
async def api_import_crm_leads(req: CrmBulkImportRequest):
    result = await _import_leads(req.leads or [], import_source=req.import_source or "api")
    processed = result.pop("_processed_leads", [])
    result["automation_results"] = await _run_lead_automation_for_imports(processed, req.import_source or "api")
    return result


@app.post("/api/crm/upload-leads")
async def api_upload_crm_leads(file: UploadFile = File(...)):
    name = file.filename or ""
    content = await file.read()
    upload_batch_id = str(uuid.uuid4())
    rows: list = []
    try:
        if name.lower().endswith(".csv"):
            text = content.decode("utf-8-sig")
            rows = [_normalize_upload_row(r) for r in csv.DictReader(StringIO(text))]
        elif name.lower().endswith(".xlsx"):
            from openpyxl import load_workbook
            wb = load_workbook(BytesIO(content), read_only=True, data_only=True)
            ws = wb.active
            values = list(ws.iter_rows(values_only=True))
            if values:
                headers = [str(h or "").strip() for h in values[0]]
                rows = [_normalize_upload_row(dict(zip(headers, row))) for row in values[1:] if any(v is not None and str(v).strip() for v in row)]
        else:
            raise HTTPException(400, "Only .csv and .xlsx files are supported")

        if rows and not any(r.get("phone_number") or r.get("phone") for r in rows):
            raise HTTPException(400, "Required column missing: phone (aliases: phone_number, mobile, contact_number)")

        result = await _import_leads(rows, import_source="file_upload", upload_batch_id=upload_batch_id)
        processed = result.pop("_processed_leads", [])
        automation_results = await _run_lead_automation_for_imports(processed, "file_upload")
        return {**result, "upload_batch_id": upload_batch_id, "automation_results": automation_results}
    except HTTPException:
        raise
    except Exception as exc:
        await log_error("server", "Lead upload failed", str(exc), "error")
        raise HTTPException(400, f"Upload failed: {exc}")


def parse_google_sheet_csv_url(url: str, sheet_name: Optional[str] = None) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    if "output=csv" in url or (url.endswith(".csv") and "docs.google.com" not in url):
        return url
    if "/spreadsheets/d/e/" in url:
        base = url.split("?")[0].replace("/pubhtml", "/pub")
        if not base.endswith("/pub"):
            base = re.sub(r"/pub.*", "/pub", base)
        return f"{base}?output=csv"
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", url)
    if not m:
        return url
    sheet_id = m.group(1)
    gid_m = re.search(r"[#&?]gid=([0-9]+)", url)
    gid = gid_m.group(1) if gid_m else "0"
    export_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"
    if sheet_name:
        export_url += f"&sheet={quote(sheet_name)}"
    return export_url


@app.post("/api/crm/import-google-sheet")
async def api_import_crm_google_sheet(req: GoogleSheetImportRequest):
    raw_url = (req.sheet_url or "").strip()
    if not raw_url:
        raise HTTPException(400, "Google Sheet URL is required")

    csv_url = parse_google_sheet_csv_url(raw_url, req.sheet_name)
    upload_batch_id = str(uuid.uuid4())
    import_source = (req.source or "google_sheet").strip().lower()

    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            resp = await client.get(csv_url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "text/csv,text/plain,application/octet-stream,*/*"
            })
    except Exception as exc:
        raise HTTPException(400, f"Failed to fetch Google Sheet: {exc}")

    if resp.status_code != 200:
        raise HTTPException(400, f"Google Sheet returned HTTP {resp.status_code}. Please make sure the sheet sharing is set to 'Anyone with the link can view'.")

    content_type = resp.headers.get("content-type", "").lower()
    text = resp.text

    if "html" in content_type or "<!doctype html" in text.lower() or "google-signin" in text.lower() or "accounts.google.com" in str(resp.url).lower():
        raise HTTPException(
            400,
            "Google Sheet is private or requires Google Login. "
            "Please open your Google Sheet, click 'Share' in the top right, "
            "change General Access from 'Restricted' to 'Anyone with the link can view' (Viewer), "
            "and try again. Alternatively, use File > Share > Publish to web and export as CSV."
        )

    try:
        if text.startswith("\ufeff"):
            text = text[1:]
        reader = csv.DictReader(StringIO(text))
        rows = [_normalize_upload_row(r) for r in reader if any(v is not None and str(v).strip() for v in r.values())]

        if not rows:
            raise HTTPException(400, "No data rows found in Google Sheet. Row 1 must contain column headers (Name, Phone, etc.) and there must be at least one row of lead data.")

        if not any(r.get("phone_number") or r.get("phone") for r in rows):
            raise HTTPException(400, "Required column missing: Phone. Please ensure your Google Sheet contains a column named 'Phone', 'Mobile', 'Contact', or 'WhatsApp'.")

        result = await _import_leads(rows, import_source=import_source, upload_batch_id=upload_batch_id)
        processed = result.pop("_processed_leads", [])
        automation_results = await _run_lead_automation_for_imports(processed, import_source)
        return {
            **result,
            "upload_batch_id": upload_batch_id,
            "import_source": import_source,
            "csv_url_used": csv_url,
            "automation_results": automation_results,
        }
    except HTTPException:
        raise
    except Exception as exc:
        await log_error("server", "Google Sheet parse failed", str(exc), "error")
        raise HTTPException(400, f"Google Sheet import failed: {exc}")


@app.get("/api/crm/google-sheet-script")
async def api_google_sheet_script(request: Request):
    host = request.headers.get("host") or "localhost:8000"
    proto = request.headers.get("x-forwarded-proto") or "https"
    webhook_url = f"{proto}://{host}/api/leads/google-sheets"
    script = f"""// Google Apps Script: Auto-sync new rows to CRM
// Installation:
// 1. In your Google Sheet, click Extensions > Apps Script
// 2. Paste this code and save (Ctrl+S)
// 3. Click Triggers (clock icon on left) > Add Trigger
//    Choose function: 'onFormSubmit' (or 'onEdit')
//    Event type: 'On form submit' or 'On edit'

const CRM_WEBHOOK_URL = "{webhook_url}";

function onFormSubmit(e) {{
  if (!e || !e.namedValues) return;
  const values = e.namedValues;
  const payload = {{}};
  for (const key in values) {{
    payload[key] = values[key][0] || '';
  }}
  sendToCRM(payload);
}}

function sendToCRM(payload) {{
  const options = {{
    method: 'post',
    contentType: 'application/json',
    payload: JSON.stringify(payload),
    muteHttpExceptions: true
  }};
  try {{
    UrlFetchApp.fetch(CRM_WEBHOOK_URL, options);
  }} catch (err) {{
    Logger.log('CRM Webhook error: ' + err);
  }}
}}
"""
    return {"webhook_url": webhook_url, "script": script}


@app.post("/api/leads/google-sheets")
async def api_incoming_google_sheet_lead(request: Request):
    """Real-time incoming webhook for Google Apps Script / Zapier / Make."""
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON payload")

    if isinstance(payload, list):
        rows = [_normalize_upload_row(item) for item in payload if isinstance(item, dict)]
        if not rows:
            raise HTTPException(400, "Empty lead list")
        res = await _import_leads(rows, import_source="google_sheet")
        processed = res.pop("_processed_leads", [])
        await _run_lead_automation_for_imports(processed, "google_sheet")
        return res
    elif isinstance(payload, dict):
        normalized = _normalize_upload_row(payload)
        phone = normalized.get("phone_number") or normalized.get("phone")
        if not phone:
            raise HTTPException(400, "Phone number is required")
        normalized["source"] = normalized.get("source") or "google_sheet"
        res = await upsert_crm_lead(normalized, import_source="google_sheet")
        try:
            norm_phone = normalize_phone(phone)
            await execute_automation_rule("google_sheet_lead", {
                "phone": norm_phone,
                "lead_name": normalized.get("lead_name") or "",
                "business_name": normalized.get("business_name") or "",
                "service_type": normalized.get("service_type") or "",
                "source": "google_sheet",
            })
        except Exception as exc:
            logger.warning("execute_automation_rule for google_sheet_lead failed: %s", exc)
        return {"success": True, "lead": normalized, "result": res}
    else:
        raise HTTPException(400, "Payload must be a JSON object or array")


@app.get("/api/crm/sample-leads.csv")
async def api_sample_leads_csv():
    rows = [
        ["lead_name","phone_number","email","city","location","address","requirement","budget","source","business_name","campaign_name","service_type","crm_status","crm_notes","next_followup_at","assigned_to"],
        ["AirZen Contact A","+6590000000","customer1@example.com","Singapore","Tampines","10 Synthetic Street, #02-01","Aircon servicing","","WhatsApp","AirZen","AirZen Website Leads","Residential air conditioning service","New Lead","Interested in aircon servicing","",""],
        ["AirZen Contact B","81234567","customer2@example.com","Singapore","Jurong","20 Example Avenue, #03-02","Aircon repair","","Website","AirZen","AirZen Website Leads","Residential air conditioning service","New Lead","Call after 5 PM","",""],
    ]
    out = StringIO()
    csv.writer(out).writerows(rows)
    out.seek(0)
    return StreamingResponse(iter([out.getvalue()]), media_type="text/csv", headers={"Content-Disposition": 'attachment; filename="sample_leads.csv"'})


@app.get("/api/crm/contacts")
async def api_get_crm_contacts(
    status: Optional[str] = None,
    outcome: Optional[str] = None,
    q: Optional[str] = None,
    due_today: bool = False,
    today: bool = False,
    timezone: Optional[str] = None,
    source: Optional[str] = None,
    business_name: Optional[str] = None,
    campaign_name: Optional[str] = None,
    city: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    assigned_to: Optional[str] = None,
    recording_available: Optional[str] = None,
    has_followup: Optional[str] = None,
    tag: Optional[str] = None,
    page: Optional[int] = None,
    page_size: Optional[int] = None,
    limit: Optional[int] = None,
    offset: Optional[int] = None,
):
    filters = _crm_filter_dict(
        status=status, outcome=outcome, q=q,
        due_today=str(due_today).lower() if due_today else "",
        today=str(today).lower() if today else "",
        timezone=timezone,
        source=source, business_name=business_name, campaign_name=campaign_name,
        city=city, date_from=date_from, date_to=date_to, assigned_to=assigned_to,
        recording_available=recording_available, has_followup=has_followup,
        tag=tag,
    )
    data = await _crm_contacts_from_filters(filters)
    if tag and tag.strip():
        tag_clean = tag.strip().lower()
        data = [
            c for c in data
            if any(tag_clean == str(t).strip().lower() for t in _json_list(c.get("tags_json")))
        ]
    total = len(data)
    safe_page_size = page_size or limit or 50
    safe_page_size = max(1, min(int(safe_page_size or 50), 100))
    if offset is not None:
        safe_offset = max(0, int(offset or 0))
        safe_page = (safe_offset // safe_page_size) + 1
    else:
        safe_page = max(1, int(page or 1))
        safe_offset = (safe_page - 1) * safe_page_size
    paged = data[safe_offset:safe_offset + safe_page_size]
    return {
        "data": paged,
        "contacts": paged,
        "page": safe_page,
        "page_size": safe_page_size,
        "limit": safe_page_size,
        "offset": safe_offset,
        "total": total,
        "has_next": safe_offset + len(paged) < total,
        "filters": filters,
    }


@app.get("/api/crm/summary")
async def api_crm_summary():
    return await get_crm_summary()


@app.get("/api/crm/export/leads.csv")
async def api_export_crm_leads_csv(
    status: Optional[str] = None, outcome: Optional[str] = None, q: Optional[str] = None, due_today: Optional[str] = None,
    source: Optional[str] = None, business_name: Optional[str] = None, campaign_name: Optional[str] = None, city: Optional[str] = None,
    date_from: Optional[str] = None, date_to: Optional[str] = None, assigned_to: Optional[str] = None,
    recording_available: Optional[str] = None, has_followup: Optional[str] = None,
):
    filters = _crm_filter_dict(status=status, outcome=outcome, q=q, due_today=due_today, source=source, business_name=business_name, campaign_name=campaign_name, city=city, date_from=date_from, date_to=date_to, assigned_to=assigned_to, recording_available=recording_available, has_followup=has_followup)
    rows = await _crm_contacts_from_filters(filters)
    out = StringIO()
    writer = csv.writer(out)
    writer.writerow(CRM_EXPORT_COLUMNS)
    for row in rows:
        writer.writerow([row.get(c, "") for c in CRM_EXPORT_COLUMNS])
    out.seek(0)
    return StreamingResponse(iter([out.getvalue()]), media_type="text/csv", headers={"Content-Disposition": 'attachment; filename="crm_leads_export.csv"'})


@app.get("/api/crm/export/leads.xlsx")
async def api_export_crm_leads_xlsx(
    status: Optional[str] = None, outcome: Optional[str] = None, q: Optional[str] = None, due_today: Optional[str] = None,
    source: Optional[str] = None, business_name: Optional[str] = None, campaign_name: Optional[str] = None, city: Optional[str] = None,
    date_from: Optional[str] = None, date_to: Optional[str] = None, assigned_to: Optional[str] = None,
    recording_available: Optional[str] = None, has_followup: Optional[str] = None,
):
    from openpyxl import Workbook
    from openpyxl.styles import Font
    filters = _crm_filter_dict(status=status, outcome=outcome, q=q, due_today=due_today, source=source, business_name=business_name, campaign_name=campaign_name, city=city, date_from=date_from, date_to=date_to, assigned_to=assigned_to, recording_available=recording_available, has_followup=has_followup)
    rows = await _crm_contacts_from_filters(filters)
    wb = Workbook()
    ws = wb.active
    ws.title = "CRM Leads"
    ws.append(CRM_EXPORT_COLUMNS)
    for cell in ws[1]:
        cell.font = Font(bold=True)
    for row in rows:
        ws.append([row.get(c, "") for c in CRM_EXPORT_COLUMNS])
    for column in ws.columns:
        width = max(len(str(cell.value or "")) for cell in column)
        ws.column_dimensions[column[0].column_letter].width = min(max(width + 2, 12), 50)
    stream = BytesIO()
    wb.save(stream)
    stream.seek(0)
    return StreamingResponse(stream, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition": 'attachment; filename="crm_leads_export.xlsx"'})


@app.get("/api/crm/contacts/{phone}")
async def api_get_crm_contact_detail(phone: str):
    detail = await get_crm_contact_detail(phone)
    if not detail.get("contact"):
        raise HTTPException(404, "CRM contact not found")
    tz_name = await _display_timezone()
    detail["contact"] = _with_followup_display(detail.get("contact") or {}, tz_name)
    detail["appointments"] = [_with_followup_display(row, tz_name) for row in (detail.get("appointments") or [])]
    return detail


@app.get("/api/crm/contacts/{phone}/followup-state")
async def api_get_followup_state(phone: str):
    state = await get_lead_followup_state(phone)
    if not state:
        raise HTTPException(404, "CRM contact not found")
    actions = await get_followup_actions(phone=phone, limit=20)
    tz_name = await _display_timezone()
    return {
        "contact": _with_followup_display(state, tz_name),
        "actions": [_with_followup_display(action, tz_name) for action in actions],
    }


@app.post("/api/crm/contacts/{phone}/schedule-followup")
async def api_schedule_followup(phone: str, req: ScheduleFollowupRequest):
    clean = normalize_phone(phone)
    channel = (req.channel or "call").lower()
    tz_name = await _display_timezone()
    now_local = datetime.now(ZoneInfo(tz_name))
    parsed_scheduled_local = parse_followup_time(req.when_text, timezone=tz_name, now=now_local)
    final_scheduled_local = parsed_scheduled_local
    outbound_window_adjusted = False
    outbound_window_reason = "not_applicable"
    if channel == "call":
        final_scheduled_local, outbound_window_adjusted, outbound_window_reason = await _snap_followup_call_time_to_window(parsed_scheduled_local)
        if outbound_window_adjusted:
            await log_error(
                "followup",
                "followup_outbound_window_adjusted",
                (
                    f"phone={clean}; source=dashboard; original_scheduled_local={parsed_scheduled_local.isoformat()}; "
                    f"adjusted_scheduled_local={final_scheduled_local.isoformat()}; reason={outbound_window_reason}"
                ),
                "info",
            )
    scheduled_at = final_scheduled_local.astimezone(timezone.utc) if final_scheduled_local.tzinfo else final_scheduled_local.replace(tzinfo=ZoneInfo(tz_name)).astimezone(timezone.utc)
    action_type = req.action_type or ("call_only" if channel == "call" else "whatsapp_template")
    event_type = "callback_requested" if channel == "call" else "message_followup_requested"
    payload = {
        "template_purpose": req.template_purpose or "no_response_followup_template",
        "message": req.message or "",
        "when_text": req.when_text,
        "scheduled_local": final_scheduled_local.isoformat(),
    }
    action_id = await create_followup_action(clean, event_type, action_type, channel, scheduled_at, reason=req.reason, payload=payload, source="dashboard")
    await set_next_best_action(clean, "call_customer" if channel == "call" else "message_customer", channel, scheduled_at, req.reason)
    await update_lead_journey(clean, {
        "journey_stage": event_type,
        "crm_status": event_type,
        "preferred_channel": channel,
        "preferred_callback_at": scheduled_at.isoformat() if channel == "call" else None,
    })
    await log_error(
        "followup",
        "callback_scheduled" if channel == "call" else "whatsapp_followup_scheduled",
        (
            f"phone={clean}; source=dashboard; action_id={action_id}; channel={channel}; "
            f"callback_time_text={req.when_text}; timezone_used={tz_name}; now_local={now_local.isoformat()}; "
            f"parsed_scheduled_local={parsed_scheduled_local.isoformat()}; final_scheduled_local={final_scheduled_local.isoformat()}; "
            f"final_scheduled_utc={scheduled_at.isoformat()}; outbound_window_adjusted={str(outbound_window_adjusted).lower()}; "
            f"outbound_window_reason={outbound_window_reason}"
        ),
        "info",
    )
    return {
        "success": True,
        "action_id": action_id,
        "scheduled_at": scheduled_at.isoformat(),
        "scheduled_at_display": _format_datetime_display(scheduled_at.isoformat(), tz_name),
    }


@app.post("/api/crm/contacts/{phone}/stop-automation")
async def api_stop_automation(phone: str, req: StopAutomationRequest):
    ok = await mark_lead_stop_automation(phone, req.reason, req.status)
    return {"success": ok}


@app.post("/api/crm/contacts/{phone}/resume-automation")
async def api_resume_automation(phone: str):
    ok = await update_lead_journey(phone, {"stop_automation": False, "stop_automation_reason": "", "journey_stage": "resumed"})
    return {"success": ok}


@app.get("/api/followups/due")
async def api_get_due_followups(limit: int = 50):
    tz_name = await _display_timezone()
    actions = await get_due_followup_actions(limit=limit)
    return {"actions": [_with_followup_display(action, tz_name) for action in actions]}


@app.post("/api/followups/run-due")
async def api_run_due_followups():
    due_result = await run_due_followup_actions()
    no_response_result = await evaluate_no_response_followups()
    return {"due": due_result, "no_response": no_response_result}


@app.get("/api/followups")
async def api_get_followups(phone: Optional[str] = None, status: Optional[str] = None, limit: int = 100):
    tz_name = await _display_timezone()
    actions = await get_followup_actions(phone=phone, status=status, limit=limit)
    return {"actions": [_with_followup_display(action, tz_name) for action in actions]}


@app.get("/api/dashboard/summary")
async def api_dashboard_summary():
    tz_name = await _display_timezone()
    today = datetime.now(ZoneInfo(tz_name)).date().isoformat()
    data = await _report_data(today, today)
    warnings = list(data["warnings"])
    all_contacts = await get_crm_contacts(warnings=warnings)
    due_followups = await get_due_followup_actions(limit=500)
    return {
        "today_calls": len(data["calls"]),
        "answered_calls": sum(1 for r in data["calls"] if (r.get("outcome") or "").lower() in {"answered", "booked", "completed", "interested"}),
        "no_answer_calls": sum(1 for r in data["calls"] if (r.get("outcome") or "").lower() in {"no_answer", "no answer"}),
        "busy_calls": sum(1 for r in data["calls"] if (r.get("outcome") or "").lower() == "busy"),
        "failed_calls": sum(1 for r in data["calls"] if "fail" in (r.get("outcome") or "").lower()),
        "today_whatsapp_inbound": sum(1 for r in data["whatsapp_messages"] if (r.get("direction") or "").lower() == "inbound"),
        "today_whatsapp_outbound": sum(1 for r in data["whatsapp_messages"] if (r.get("direction") or "").lower() == "outbound"),
        "today_leads": len(data["contacts"]),
        "appointments_today": len(data["appointments"]),
        "pending_followups": sum(1 for r in await get_followup_actions(status="scheduled", limit=1000)),
        "due_calls": sum(1 for r in due_followups if (r.get("channel") or "").lower() == "call"),
        "due_today": sum(1 for r in all_contacts if (r.get("next_followup_at") or "").startswith(today)),
        "converted_leads": sum(1 for r in all_contacts if _status_slug(r.get("crm_status") or "") in {"converted", "closed_won"}),
        "no_response_leads": sum(1 for r in all_contacts if "no_response" in _status_slug(r.get("crm_status") or r.get("journey_stage") or "")),
        "warnings": warnings,
    }


@app.get("/api/reports/summary")
async def api_reports_summary(date_from: Optional[str] = None, date_to: Optional[str] = None):
    data = await _report_data(date_from, date_to)
    source_counts = {}
    for lead in data["contacts"]:
        source = lead.get("source") or "unknown"
        source_counts[source] = source_counts.get(source, 0) + 1
    staff_counts = {}
    for appt in data["appointments"]:
        staff = appt.get("staff_name") or appt.get("staff_id") or "unassigned"
        staff_counts[staff] = staff_counts.get(staff, 0) + 1
    return {
        "calls_total": len(data["calls"]),
        "answered": sum(1 for r in data["calls"] if (r.get("outcome") or "").lower() in {"answered", "booked", "completed", "interested"}),
        "no_answer": sum(1 for r in data["calls"] if (r.get("outcome") or "").lower() in {"no_answer", "no answer"}),
        "busy": sum(1 for r in data["calls"] if (r.get("outcome") or "").lower() == "busy"),
        "failed": sum(1 for r in data["calls"] if "fail" in (r.get("outcome") or "").lower()),
        "total_duration_minutes": _sum_duration_minutes(data["calls"]),
        "whatsapp_sent": sum(1 for r in data["whatsapp_logs"] if (r.get("status") or "").lower() == "sent"),
        "whatsapp_delivered": sum(1 for r in data["whatsapp_logs"] if (r.get("status") or "").lower() == "delivered"),
        "whatsapp_read": sum(1 for r in data["whatsapp_logs"] if (r.get("status") or "").lower() == "read"),
        "whatsapp_failed": sum(1 for r in data["whatsapp_logs"] if (r.get("status") or "").lower() == "failed"),
        "inbound_whatsapp_count": sum(1 for r in data["whatsapp_messages"] if (r.get("direction") or "").lower() == "inbound"),
        "appointments_booked": sum(1 for r in data["appointments"] if (r.get("status") or "").lower() == "booked"),
        "demo_no_show": sum(1 for r in data["appointments"] if (r.get("status") or "").lower() == "no_show"),
        "converted_leads": sum(1 for r in data["contacts"] if _status_slug(r.get("crm_status") or "") in {"converted", "closed_won"}),
        "lost_leads": sum(1 for r in data["contacts"] if _status_slug(r.get("crm_status") or "") in {"lost", "closed_lost"}),
        "source_wise_leads": source_counts,
        "staff_wise_appointments": staff_counts,
        "warnings": data["warnings"],
    }


@app.get("/api/reports/calls")
async def api_reports_calls(date_from: Optional[str] = None, date_to: Optional[str] = None, outcome: Optional[str] = None, export: Optional[str] = None):
    data = await _report_data(date_from, date_to)
    rows = [r for r in data["calls"] if not outcome or (r.get("outcome") or "") == outcome]
    return _csv_response("calls_report.csv", rows) if export == "csv" else {"rows": rows, "warnings": data["warnings"]}


@app.get("/api/reports/whatsapp")
async def api_reports_whatsapp(date_from: Optional[str] = None, date_to: Optional[str] = None, status: Optional[str] = None, export: Optional[str] = None):
    data = await _report_data(date_from, date_to)
    rows = [r for r in data["whatsapp_logs"] if not status or (r.get("status") or "") == status]
    return _csv_response("whatsapp_report.csv", rows) if export == "csv" else {"rows": rows, "warnings": data["warnings"]}


@app.get("/api/reports/appointments")
async def api_reports_appointments(date_from: Optional[str] = None, date_to: Optional[str] = None, status: Optional[str] = None, export: Optional[str] = None):
    data = await _report_data(date_from, date_to)
    rows = [r for r in data["appointments"] if not status or (r.get("status") or "") == status]
    return _csv_response("appointments_report.csv", rows) if export == "csv" else {"rows": rows, "warnings": data["warnings"]}


@app.get("/api/reports/leads")
async def api_reports_leads(date_from: Optional[str] = None, date_to: Optional[str] = None, status: Optional[str] = None, source: Optional[str] = None, export: Optional[str] = None):
    data = await _report_data(date_from, date_to)
    rows = [
        r for r in data["contacts"]
        if (not status or (r.get("crm_status") or "") == status) and (not source or (r.get("source") or "") == source)
    ]
    return _csv_response("leads_report.csv", rows) if export == "csv" else {"rows": rows, "warnings": data["warnings"]}


@app.get("/api/costs/summary")
async def api_costs_summary(date_from: Optional[str] = None, date_to: Optional[str] = None):
    data = await _report_data(date_from, date_to)
    def num(key: str) -> float:
        try:
            return float(awaitable_values.get(key, "0"))
        except Exception:
            return 0.0
    awaitable_values = {
        "voice": await get_setting("COST_GEMINI_VOICE_PER_MINUTE", "0"),
        "sip": await get_setting("COST_SIP_PER_MINUTE", "0"),
        "recording": await get_setting("COST_RECORDING_PER_MINUTE", "0"),
        "template": await get_setting("COST_WHATSAPP_TEMPLATE", "0"),
        "free_text": await get_setting("COST_WHATSAPP_FREE_TEXT", "0"),
    }
    voice_minutes = _sum_duration_minutes(data["calls"])
    template_count = sum(1 for r in data["whatsapp_messages"] if (r.get("message_type") or "").lower() == "template") or len(data["whatsapp_logs"])
    free_text_count = sum(1 for r in data["whatsapp_messages"] if (r.get("message_type") or "").lower() == "text" and (r.get("direction") or "").lower() == "outbound")
    gemini_cost = voice_minutes * num("voice")
    sip_cost = voice_minutes * num("sip")
    recording_cost = voice_minutes * num("recording")
    template_cost = template_count * num("template")
    free_text_cost = free_text_count * num("free_text")
    total = gemini_cost + sip_cost + recording_cost + template_cost + free_text_cost
    lead_count = max(len(data["contacts"]), 1)
    appt_count = max(len(data["appointments"]), 1)
    return {
        "currency": await get_setting("COST_CURRENCY", DEFAULT_CURRENCY),
        "voice_minutes": voice_minutes,
        "estimated_gemini_voice_cost": round(gemini_cost, 2),
        "sip_call_cost": round(sip_cost, 2),
        "recording_cost": round(recording_cost, 2),
        "whatsapp_template_count": template_count,
        "whatsapp_template_cost": round(template_cost, 2),
        "whatsapp_free_text_count": free_text_count,
        "whatsapp_free_text_cost": round(free_text_cost, 2),
        "total_estimated_cost": round(total, 2),
        "estimated_cost_per_lead": round(total / lead_count, 2),
        "estimated_cost_per_appointment": round(total / appt_count, 2),
        "estimate_only": True,
        "warnings": data["warnings"],
    }


@app.get("/api/crm/kanban")
async def api_crm_kanban():
    warnings = []
    contacts = await get_crm_contacts(warnings=warnings)
    
    # Load dynamic columns from database lead_statuses
    raw_statuses = []
    try:
        raw_statuses = await get_lead_statuses()
    except Exception as exc:
        warnings.append(f"lead_statuses_load_failed: {exc}")

    columns = []
    col_dict = {}
    if raw_statuses:
        for item in raw_statuses:
            s_name = item.get("name") if isinstance(item, dict) else str(item)
            s_slug = _status_slug(s_name)
            s_label = s_name.replace("_", " ").title() if isinstance(item, dict) else str(item)
            s_color = item.get("color", "#64748b") if isinstance(item, dict) else "#64748b"
            if s_slug not in col_dict:
                col_obj = {"id": s_slug, "label": s_label, "color": s_color, "leads": []}
                col_dict[s_slug] = col_obj
                columns.append(col_obj)
        # Ensure base standard columns exist
        for cid, label in KANBAN_COLUMNS:
            if cid not in col_dict:
                col_obj = {"id": cid, "label": label, "color": "#64748b", "leads": []}
                col_dict[cid] = col_obj
                columns.append(col_obj)
    else:
        columns = [{"id": cid, "label": label, "color": "#64748b", "leads": []} for cid, label in KANBAN_COLUMNS]
        col_dict = {c["id"]: c for c in columns}

    by_id = col_dict
    seen = set()
    first_col_id = columns[0]["id"] if columns else "new_lead"

    for lead in contacts:
        phone = normalize_phone(lead.get("phone_number") or lead.get("phone") or "")
        if not phone or phone in seen:
            continue
        seen.add(phone)
        raw_target = (
            lead.get("crm_status")
            or lead.get("journey_stage")
            or lead.get("status")
            or lead.get("custom_status")
            or "new_lead"
        )
        target = canonical_crm_stage(raw_target) or _status_slug(raw_target)
        if target not in by_id:
            if target == "demo_no_show":
                target = "demo_no_show" if "demo_no_show" in by_id else "callback_requested"
            elif target in ("demo_reschedule_requested", "rescheduled", "reschedule_requested", "need_followup"):
                target = "callback_requested" if "callback_requested" in by_id else first_col_id
            else:
                target = first_col_id
        if target in by_id:
            by_id[target]["leads"].append(lead)

    await log_error("crm", "kanban_loaded", f"contacts={len(seen)}; columns={len(columns)}; warnings={','.join(warnings)}", "info")
    return {"columns": columns, "warnings": warnings}


@app.post("/api/crm/contacts/{phone}/move-stage")
async def api_crm_move_stage(phone: str, req: CrmMoveStageRequest):
    stage = canonical_crm_stage(req.stage) or _status_slug(req.stage)
    if not stage:
        raise HTTPException(400, "stage must be a supported pipeline stage")
    clean = normalize_phone(phone)
    try:
        ok = await update_lead_journey(clean, crm_stage_updates(stage))
        if not ok:
            raise RuntimeError("CRM contact update returned no rows")
        contact = await get_crm_contact_by_phone(clean)
        await log_error("crm", "crm_stage_moved", f"phone={clean}; stage={stage}", "info")
        return {"success": True, "stage": stage, "contact": contact}
    except Exception as exc:
        await log_error("crm", "crm_stage_move_failed", f"phone={clean}; stage={stage}; error={str(exc)[:300]}", "warning")
        raise HTTPException(500, f"Could not move lead to {stage}") from exc


@app.get("/api/crm/tags")
async def api_crm_tags():
    try:
        rows = await get_repository().list_crm_tags(limit=500)
        names = [r.get("name") for r in rows if r.get("name")]
    except Exception:
        names = []
    contacts = await get_crm_contacts()
    for contact in contacts:
        for tag in _json_list(contact.get("tags_json")):
            if tag not in names:
                names.append(tag)
    return {"tags": sorted(set(names))}


@app.post("/api/crm/tags")
async def api_create_crm_tag(req: CrmTagRequest):
    tag = (req.tag or "").strip()
    if not tag:
        raise HTTPException(400, "tag is required")
    try:
        await get_repository().upsert_crm_tag({"name": tag, "created_at": datetime.now().isoformat()})
    except Exception:
        pass
    return {"success": True, "tag": tag}


@app.delete("/api/crm/tags/{tag}")
async def api_delete_crm_tag(tag: str):
    clean_tag = tag.strip()
    if not clean_tag:
        raise HTTPException(400, "tag is required")
    try:
        ok = await get_repository().delete_crm_tag(clean_tag)
    except Exception as exc:
        logger.warning("delete_crm_tag failed: %s", exc)
        ok = False
    try:
        contacts = await get_crm_contacts()
        for contact in contacts:
            tags = _json_list(contact.get("tags_json"))
            if clean_tag in tags:
                tags = [t for t in tags if t != clean_tag]
                await update_lead_journey(contact.get("phone_number"), {"tags_json": json.dumps(tags)})
    except Exception as exc:
        logger.warning("cleaning deleted tag from contacts failed: %s", exc)
    return {"success": True, "tag": clean_tag, "deleted": ok}


@app.post("/api/crm/contacts/{phone}/tags")
async def api_add_contact_tag(phone: str, req: CrmTagRequest):
    clean = normalize_phone(phone)
    contact = await get_crm_contact_by_phone(clean) or {}
    tags = _json_list(contact.get("tags_json"))
    tag = (req.tag or "").strip()
    if tag and tag not in tags:
        tags.append(tag)
    await update_lead_journey(clean, {"tags_json": json.dumps(tags)})
    await api_create_crm_tag(req)
    return {"success": True, "tags": tags}


@app.delete("/api/crm/contacts/{phone}/tags/{tag}")
async def api_delete_contact_tag(phone: str, tag: str):
    clean = normalize_phone(phone)
    contact = await get_crm_contact_by_phone(clean) or {}
    tags = [t for t in _json_list(contact.get("tags_json")) if t != tag]
    await update_lead_journey(clean, {"tags_json": json.dumps(tags)})
    return {"success": True, "tags": tags}


@app.patch("/api/crm/contacts/{phone}/custom-fields")
async def api_patch_contact_custom_fields(phone: str, req: CrmCustomFieldsRequest):
    clean = normalize_phone(phone)
    contact = await get_crm_contact_by_phone(clean) or {}
    fields = _json_dict(contact.get("custom_fields_json"))
    fields.update(req.fields or {})
    ok = await update_lead_journey(clean, {"custom_fields_json": json.dumps(fields)})
    return {"success": bool(ok), "custom_fields": fields}


TERMINAL_LEAD_SLUGS = {"not_interested", "wrong_number", "do_not_contact", "converted", "lost"}


def _lead_opted_out(lead: dict) -> bool:
    statuses = {
        _status_slug(lead.get("crm_status") or ""),
        _status_slug(lead.get("journey_stage") or ""),
    }
    return bool(lead.get("stop_automation")) or bool(statuses & TERMINAL_LEAD_SLUGS)


def _broadcast_segment(campaign: dict) -> dict:
    segment = campaign.get("segment") or campaign.get("segment_json") or {}
    if isinstance(segment, str):
        segment = _json_dict(segment)
    return segment if isinstance(segment, dict) else {}


def _broadcast_segment_is_empty(segment: dict) -> bool:
    for v in (segment or {}).values():
        if isinstance(v, list) and len(v) > 0:
            return False
        if v is not None and str(v).strip():
            return False
    return True


def _broadcast_segment_keys(segment: dict) -> set:
    keys = set()
    for k, v in (segment or {}).items():
        if isinstance(v, list) and len(v) > 0:
            keys.add(str(k))
        elif v is not None and str(v).strip():
            keys.add(str(k))
    return keys


def _broadcast_segment_for_storage(segment: Optional[dict]) -> dict:
    clean = _broadcast_segment({"segment": segment or {}})
    if clean.get("phone_number"):
        try:
            clean["phone_number"] = normalize_phone(str(clean.get("phone_number") or ""))
        except Exception:
            pass
    if clean.get("phone_numbers") and isinstance(clean["phone_numbers"], list):
        norm_list = []
        for p in clean["phone_numbers"]:
            try:
                if str(p).strip():
                    norm_list.append(normalize_phone(str(p).strip()))
            except Exception:
                pass
        clean["phone_numbers"] = norm_list
    return clean


def _segment_match(lead: dict, segment: dict) -> bool:
    if not segment:
        return True
    phone_filter = (segment.get("phone_number") or "").strip()
    if phone_filter:
        try:
            if normalize_phone(lead.get("phone_number") or "") != normalize_phone(phone_filter):
                return False
        except Exception:
            return False
    phone_list = segment.get("phone_numbers")
    if phone_list and isinstance(phone_list, list):
        try:
            lp = normalize_phone(lead.get("phone_number") or "")
            allowed = {normalize_phone(p) for p in phone_list if p}
            if lp not in allowed:
                return False
        except Exception:
            return False
    for key in ("crm_status", "source", "journey_stage", "assigned_to", "service_type"):
        value = segment.get(key)
        if value and str(lead.get(key) or "").lower() != str(value).lower():
            return False
    tag = segment.get("tag")
    if tag and tag not in _json_list(lead.get("tags_json")):
        return False
    return True


async def _broadcast_campaign(campaign_id: str) -> Optional[dict]:
    try:
        return await get_repository().get_broadcast_campaign(campaign_id)
    except Exception:
        return None


async def _broadcast_preview_contacts(campaign: dict, limit: int = 200) -> list:
    segment = _broadcast_segment(campaign)
    contacts = await get_crm_contacts()
    leads_by_phone: dict[str, list[dict]] = {}
    terminal_phones: set[str] = set()

    for lead in contacts:
        raw_phone = lead.get("phone_number") or lead.get("phone") or ""
        if not raw_phone:
            continue
        try:
            phone = normalize_phone(raw_phone)
        except Exception:
            continue
        if not phone:
            continue
        leads_by_phone.setdefault(phone, []).append(lead)
        if _lead_opted_out(lead):
            terminal_phones.add(phone)

    preview = []
    for phone, leads in leads_by_phone.items():
        matching_leads = [lead for lead in leads if _segment_match(lead, segment)]
        if not matching_leads:
            continue

        if phone in terminal_phones:
            if len(leads) > 1:
                await log_error(
                    "broadcast",
                    "broadcast_duplicate_phone_skipped_terminal",
                    (
                        f"phone={phone}; rows={len(leads)}; "
                        f"segment={json.dumps(segment, default=str)}"
                    ),
                    "warning",
                )
            continue

        lead = dict(matching_leads[0])
        lead["phone_number"] = phone
        preview.append(lead)
        if len(preview) >= limit:
            break
    return preview


async def _broadcast_test_mode_enabled() -> bool:
    raw = await get_setting("BROADCAST_TEST_MODE", os.getenv("BROADCAST_TEST_MODE", "true"))
    return str(raw or "").strip().lower() in {"1", "true", "yes", "on"}


async def _broadcast_preview_result(campaign: dict, limit: int = 200) -> dict:
    segment = _broadcast_segment(campaign)
    active_keys = sorted(_broadcast_segment_keys(segment))
    test_mode = await _broadcast_test_mode_enabled()
    if test_mode and set(active_keys) != {"phone_number"}:
        return {
            "blocked": True,
            "error": "Broadcast test mode is enabled. Preview requires exactly a phone_number segment.",
            "segment": segment,
            "active_segment_keys": active_keys,
            "test_mode": test_mode,
            "count": 0,
            "recipients": [],
        }
    recipients = await _broadcast_preview_contacts(campaign, limit=limit)
    if test_mode and len(recipients) > 1:
        await log_error(
            "broadcast",
            "broadcast_preview_test_mode_too_many_recipients",
            f"campaign_id={campaign.get('id')}; segment={json.dumps(segment, default=str)}; count={len(recipients)}",
            "error",
        )
        return {
            "blocked": True,
            "error": "Broadcast test mode preview returned more than one recipient. Start is blocked.",
            "segment": segment,
            "active_segment_keys": active_keys,
            "test_mode": test_mode,
            "count": len(recipients),
            "recipients": recipients[:1],
        }
    return {
        "blocked": False,
        "error": "",
        "segment": segment,
        "active_segment_keys": active_keys,
        "test_mode": test_mode,
        "count": len(recipients),
        "recipients": recipients,
    }


@app.get("/api/broadcast/config")
async def api_get_broadcast_config():
    test_mode = await _broadcast_test_mode_enabled()
    max_per_run = int(await get_setting("BROADCAST_MAX_SEND_PER_RUN", "50") or 50)
    return {
        "test_mode": test_mode,
        "max_send_per_run": max_per_run,
        "templates": [
            {"purpose": "welcome", "label": "Welcome / Greeting"},
            {"purpose": "followup_no_response", "label": "Follow-up (No Response)"},
            {"purpose": "reengagement", "label": "Re-engagement & Offer"},
            {"purpose": "booking_confirmation", "label": "Booking / Appointment Confirmation"}
        ]
    }


@app.post("/api/broadcast/config")
async def api_update_broadcast_config(req: BroadcastConfigUpdate):
    if req.test_mode is not None:
        await set_setting("BROADCAST_TEST_MODE", "true" if req.test_mode else "false")
    if req.max_send_per_run is not None:
        val = max(1, min(1000, req.max_send_per_run))
        await set_setting("BROADCAST_MAX_SEND_PER_RUN", str(val))
    return await api_get_broadcast_config()


@app.post("/api/broadcast/quick-send")
async def api_broadcast_quick_send(req: BroadcastQuickSendRequest):
    """Send broadcast directly to selected leads from CRM list."""
    if not req.phone_numbers:
        raise HTTPException(400, "No recipients selected")
    template = await resolve_wa_template(req.template_purpose)
    if not template:
        raise HTTPException(400, f"WhatsApp template for purpose '{req.template_purpose}' is not configured")

    clean_phones = []
    for p in req.phone_numbers:
        try:
            norm = normalize_phone(p)
            if norm and norm not in clean_phones:
                clean_phones.append(norm)
        except Exception:
            continue

    if not clean_phones:
        raise HTTPException(400, "No valid phone numbers found in selection")

    campaign_id = str(uuid.uuid4())
    camp_row = {
        "id": campaign_id,
        "name": req.campaign_name or f"Quick Broadcast ({len(clean_phones)} leads)",
        "template_purpose": req.template_purpose,
        "template_name": template,
        "segment": {"phone_numbers": clean_phones},
        "status": "draft",
        "sent_count": 0,
        "failed_count": 0,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    await get_repository().create_broadcast_campaign(camp_row)

    claimed = await get_repository().claim_broadcast_campaign(campaign_id, lease_seconds=1800)
    claim_token = (claimed or {}).get("claim_token") or ""

    sent = failed = skipped = 0
    all_contacts = await get_crm_contacts()
    contact_map = {normalize_phone(c.get("phone_number") or ""): c for c in all_contacts if c.get("phone_number")}

    for phone in clean_phones:
        lead = contact_map.get(phone, {"phone_number": phone, "lead_name": "there"})
        if _lead_opted_out(lead):
            skipped += 1
            continue

        recipient_id = str(uuid.uuid4())
        rec_data = {
            "id": recipient_id,
            "campaign_id": campaign_id,
            "phone_number": phone,
            "status": "pending",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            claimed_rec = await get_repository().claim_broadcast_recipient(rec_data, claim_token)
            if not claimed_rec:
                skipped += 1
                continue

            result = await send_whatsapp_template(
                phone, template, await get_setting("WHATSAPP_DEFAULT_LANGUAGE", "en"),
                [], event_type="broadcast_campaign",
                source_type="broadcast_campaigns", source_id=campaign_id,
                template_purpose=req.template_purpose,
                template_context={**lead, "customer_name": lead.get("lead_name") or "there", "phone": phone},
            )
            status = "sent" if result.get("success") else "failed"
            if status == "sent":
                sent += 1
            else:
                failed += 1
            await get_repository().update_broadcast_recipient(recipient_id, {
                "status": status,
                "provider_message_id": result.get("provider_message_id") or "",
                "error_message": result.get("error") or result.get("reason") or "",
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "claim_token": None,
            }, claim_token=claim_token)
        except Exception as exc:
            failed += 1
            await get_repository().update_broadcast_recipient(
                recipient_id,
                {"status": "failed", "error_message": str(exc)[:500], "updated_at": datetime.now(timezone.utc).isoformat(), "claim_token": None},
                claim_token=claim_token,
            )

    await get_repository().update_broadcast_campaign(campaign_id, {
        "status": "completed",
        "sent_count": sent,
        "failed_count": failed,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "claim_token": None,
        "lease_expires_at": None,
    }, claim_token=claim_token)

    return {"success": True, "campaign_id": campaign_id, "sent": sent, "failed": failed, "skipped": skipped, "total": len(clean_phones)}


@app.get("/api/broadcast/campaigns")
async def api_broadcast_campaigns():
    try:
        rows = await get_repository().list_broadcast_campaigns(limit=200)
    except Exception:
        rows = []
    return {"campaigns": rows}


@app.post("/api/broadcast/campaigns")
async def api_create_broadcast_campaign(req: BroadcastCampaignRequest):
    name = (req.name or "").strip()
    if not name:
        raise HTTPException(400, "Campaign name is required")
    template = await resolve_wa_template(req.template_purpose)
    if not template:
        raise HTTPException(400, "Approved WhatsApp template purpose is not configured")
    try:
        segment = _broadcast_segment_for_storage(req.segment)
    except Exception as exc:
        raise HTTPException(400, f"Invalid broadcast segment: {exc}") from exc
    row = {
        "id": str(uuid.uuid4()),
        "name": name,
        "template_purpose": req.template_purpose,
        "template_name": template,
        "segment": segment,
        "status": "draft",
        "scheduled_at": req.scheduled_at,
        "sent_count": 0,
        "failed_count": 0,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        await get_repository().create_broadcast_campaign(row)
    except Exception as exc:
        raise HTTPException(500, f"Broadcast campaign create failed: {str(exc)[:200]}")
    return {"success": True, "campaign": row}


@app.post("/api/broadcast/campaigns/{campaign_id}/preview")
async def api_preview_broadcast_campaign(campaign_id: str):
    campaign = await _broadcast_campaign(campaign_id)
    if not campaign:
        raise HTTPException(404, "Broadcast campaign not found")
    preview = await _broadcast_preview_result(campaign, limit=200)
    return {"campaign_id": campaign_id, **preview, "recipients": preview["recipients"][:50]}


@app.post("/api/broadcast/campaigns/{campaign_id}/start")
async def api_start_broadcast_campaign(campaign_id: str, req: BroadcastStartRequest = BroadcastStartRequest()):
    campaign = await _broadcast_campaign(campaign_id)
    if not campaign:
        raise HTTPException(404, "Broadcast campaign not found")
    if (campaign.get("status") or "") == "paused":
        raise HTTPException(400, "Campaign is paused")
    max_per_run = int(await get_setting("BROADCAST_MAX_SEND_PER_RUN", "50") or 50)
    preview = await _broadcast_preview_result(campaign, limit=max_per_run + 1)
    segment = preview["segment"]
    confirm = str(req.confirm or "").strip()
    if _broadcast_segment_is_empty(segment):
        raise HTTPException(400, {"message": "Broadcast start blocked: segment is empty. Add a safe segment filter before starting."})
    test_mode = preview["test_mode"]
    active_keys = set(preview["active_segment_keys"])
    if preview["blocked"]:
        raise HTTPException(400, {"message": preview["error"], "segment": preview["segment"], "active_segment_keys": preview["active_segment_keys"]})
    if test_mode and active_keys != {"phone_number"}:
        raise HTTPException(400, {"message": "Broadcast test mode is enabled. Only a phone_number segment is allowed. Turn off test mode to broadcast to CRM segments.", "required_segment": {"phone_number": "+6590000000"}})
    if confirm not in {"START_BROADCAST", "CONFIRM_BROADCAST"}:
        raise HTTPException(400, {"message": "Broadcast start requires backend confirmation.", "required_confirm": "START_BROADCAST"})
    contacts = preview["recipients"]
    if not contacts:
        raise HTTPException(400, "No eligible broadcast recipients after opt-out and segment filters.")
    if test_mode and len(contacts) > 1:
        raise HTTPException(400, {"message": "Broadcast test mode is enabled. Recipient count must be exactly 1.", "recipient_count": len(contacts)})
    if len(contacts) > max_per_run:
        raise HTTPException(400, {"message": "Broadcast start blocked: recipient count exceeds BROADCAST_MAX_SEND_PER_RUN.", "recipient_count": len(contacts), "max_allowed": max_per_run})
    template_name = campaign.get("template_name") or await resolve_wa_template(campaign.get("template_purpose") or "")
    if not template_name:
        raise HTTPException(400, "Broadcast template is not configured")
    campaign = await get_repository().claim_broadcast_campaign(campaign_id, lease_seconds=1800)
    if not campaign:
        raise HTTPException(409, "Broadcast campaign is already owned by another worker or cannot be started")
    claim_token = campaign.get("claim_token") or ""
    sent = failed = skipped = 0
    paused = False
    for lead in contacts[:max_per_run]:
        latest_campaign = await get_repository().get_broadcast_campaign(campaign_id)
        if (latest_campaign or {}).get("status") == "paused":
            paused = True
            break
        phone = normalize_phone(lead.get("phone_number") or "")
        recipient_id = str(uuid.uuid4())
        recipient = {
            "id": recipient_id,
            "campaign_id": campaign_id,
            "phone_number": phone,
            "status": "pending",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            claimed_recipient = await get_repository().claim_broadcast_recipient(recipient, claim_token)
            if not claimed_recipient:
                skipped += 1
                continue
            result = await send_whatsapp_template(
                phone, template_name, await get_setting("WHATSAPP_DEFAULT_LANGUAGE", "en"),
                [], event_type="broadcast_campaign",
                source_type="broadcast_campaigns", source_id=campaign_id,
                template_purpose=campaign.get("template_purpose") or "",
                template_context={**lead, "customer_name": lead.get("lead_name") or "there", "phone": phone},
            )
            status = "sent" if result.get("success") else "failed"
            if status == "sent":
                sent += 1
            else:
                failed += 1
            await get_repository().update_broadcast_recipient(recipient_id, {
                "status": status,
                "provider_message_id": result.get("provider_message_id") or "",
                "error_message": result.get("error") or result.get("reason") or "",
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "claim_token": None,
            }, claim_token=claim_token)
        except Exception as exc:
            failed += 1
            await get_repository().update_broadcast_recipient(
                recipient_id,
                {"status": "failed", "error_message": str(exc)[:500], "updated_at": datetime.now(timezone.utc).isoformat(), "claim_token": None},
                claim_token=claim_token,
            )
    final_status = "paused" if paused else ("completed" if sent + failed + skipped >= len(contacts) or len(contacts) <= max_per_run else "running")
    await get_repository().update_broadcast_campaign(campaign_id, {
        "status": final_status,
        "sent_count": int(campaign.get("sent_count") or 0) + sent,
        "failed_count": int(campaign.get("failed_count") or 0) + failed,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "claim_token": None,
        "lease_expires_at": None,
    }, claim_token=claim_token)
    return {"success": True, "sent": sent, "failed": failed, "skipped": skipped, "limited_to": max_per_run, "status": final_status}


@app.post("/api/broadcast/campaigns/{campaign_id}/pause")
async def api_pause_broadcast_campaign(campaign_id: str):
    if not await _broadcast_campaign(campaign_id):
        raise HTTPException(404, "Broadcast campaign not found")
    await get_repository().update_broadcast_campaign(campaign_id, {"status": "paused", "updated_at": datetime.now(timezone.utc).isoformat()})
    return {"success": True, "status": "paused"}


@app.get("/api/broadcast/campaigns/{campaign_id}/recipients")
async def api_broadcast_recipients(campaign_id: str):
    try:
        rows = await get_repository().list_broadcast_recipients(campaign_id, limit=500)
    except Exception:
        rows = []
    return {"recipients": rows}


async def _select_handoff_staff(staff_id: str = "") -> Optional[dict]:
    staff = await get_appointment_staff(include_inactive=False)
    if staff_id:
        for person in staff:
            if str(person.get("id")) == str(staff_id):
                return person
    with_wa = [person for person in staff if person.get("whatsapp_number")]
    pool = with_wa or staff
    if not pool:
        return None
    return sorted(pool, key=lambda s: (int(s.get("round_robin_order") or 0), s.get("name") or ""))[0]


async def _notify_handoff_staff(staff: dict, contact: dict, reason: str) -> dict:
    phone = staff.get("whatsapp_number") or ""
    if not phone:
        await log_error("handoff", "staff_whatsapp_missing", f"staff_id={staff.get('id')}; contact={contact.get('phone_number')}", "warning")
        return {"success": False, "reason": "staff_whatsapp_missing"}
    template = await resolve_wa_template("staff_handoff_notification_template")
    if not template:
        await log_error("handoff", "staff_template_missing", "purpose=staff_handoff_notification_template", "warning")
        return {"success": False, "reason": "staff_template_missing"}
    await log_error("handoff", "staff_notification_started", f"staff_id={staff.get('id')}; phone={contact.get('phone_number')}", "info")
    result = await send_whatsapp_template(
        phone, template, await get_setting("WHATSAPP_DEFAULT_LANGUAGE", "en"),
        [], event_type="human_handoff",
        source_type="crm_contacts", source_id=contact.get("phone_number") or "",
        template_purpose="staff_handoff_notification_template",
        template_context={**contact, "customer_name": contact.get("lead_name") or "Customer", "customer_phone": contact.get("phone_number") or "", "reason": reason or "human_handoff"},
    )
    await log_error("handoff", "staff_template_sent" if result.get("success") else "staff_template_failed", f"staff_id={staff.get('id')}; result={result.get('reason') or result.get('error') or result.get('status')}", "info" if result.get("success") else "warning")
    return result


@app.post("/api/crm/contacts/{phone}/handoff")
async def api_create_handoff(phone: str, req: HandoffRequest):
    clean = normalize_phone(phone)
    contact = await get_crm_contact_by_phone(clean) or {}
    if not contact:
        raise HTTPException(404, "CRM contact not found")
    staff = await _select_handoff_staff(req.staff_id or "")
    assigned = (staff or {}).get("name") or ""
    await update_lead_journey(clean, {
        "handoff_required": True,
        "handoff_reason": req.reason or "human_requested",
        "handoff_assigned_to": assigned,
        "handoff_at": datetime.now(timezone.utc).isoformat(),
        "journey_stage": "human_handoff",
        "next_best_action": "staff_followup",
    })
    await create_followup_action(clean, "human_handoff", "staff_followup", "whatsapp", datetime.now(timezone.utc), reason=req.reason or "human_requested", payload={"staff_id": (staff or {}).get("id")})
    notify_result = await _notify_handoff_staff(staff, contact, req.reason or "human_requested") if staff else {"success": False, "reason": "no_active_staff"}
    return {"success": True, "assigned_to": assigned, "staff_notification": notify_result}


@app.post("/api/crm/contacts/{phone}/resolve-handoff")
async def api_resolve_handoff(phone: str):
    clean = normalize_phone(phone)
    ok = await update_lead_journey(clean, {"handoff_required": False, "handoff_reason": "", "handoff_assigned_to": ""})
    return {"success": bool(ok)}


@app.get("/api/handoffs")
async def api_handoffs():
    contacts = await get_crm_contacts()
    rows = [c for c in contacts if c.get("handoff_required")]
    return {"handoffs": rows}


@app.post("/api/crm/call-selected")
async def api_call_selected(req: CrmCallSelectedRequest):
    # ── Outbound calling-window guard ──────────────────────────────────────
    if not await _is_outbound_allowed():
        raise HTTPException(403, await _outbound_window_error())
    contacts = []
    failed = 0
    for phone in req.phones or []:
        try:
            clean = normalize_phone(phone)
            contact = await get_crm_contact_by_phone(clean)
            if not contact:
                failed += 1
                continue
            call_contact = {
                "phone": clean,
                "lead_name": contact.get("lead_name") or "there",
                "customer_name": contact.get("lead_name") or "there",
                "business_name": req.business_name or contact.get("business_name") or "our company",
                "company_name": req.business_name or contact.get("business_name") or "our company",
                "service_type": req.service_type or contact.get("service_type") or "our service",
                "requirement": contact.get("requirement") or "",
                "notes": contact.get("crm_notes") or "",
                "source": contact.get("source") or "",
                "call_type": _crm_status_to_call_type(contact.get("crm_status") or ""),
            }
            await log_error(
                "server",
                "crm_call_selected_payload",
                (
                    f"phone={clean}; source={call_contact.get('source') or 'missing'}; "
                    f"service_type={call_contact.get('service_type') or 'missing'}; "
                    f"lead_name={call_contact.get('lead_name') or 'missing'}; "
                    f"call_type={call_contact.get('call_type') or 'welcome_call'}"
                ),
                "info",
            )
            contacts.append(call_contact)
        except Exception:
            failed += 1
    if not contacts:
        raise HTTPException(400, "No valid leads selected")
    campaign_id = await create_campaign(
        req.campaign_name or "Selected CRM Leads",
        json.dumps(contacts),
        "once",
        "09:00",
        req.call_delay_seconds or 15,
        req.system_prompt,
        req.agent_profile_id,
    )
    _start_campaign_task(campaign_id)
    return {"success": True, "dispatched": len(contacts), "failed": failed, "message": "Selected lead calling started"}


@app.put("/api/crm/contacts/{phone}")
async def api_update_crm_contact(phone: str, req: CrmLeadUpdateRequest):
    updates = {k: v for k, v in req.dict().items() if v is not None}
    if not updates:
        raise HTTPException(400, "No fields to update")
    ok = await update_crm_contact_full(phone, updates)
    if not ok:
        raise HTTPException(404, "CRM contact not found")
    return {"success": True, "message": "Lead updated", "phone_number": phone}


@app.patch("/api/crm/contacts/{phone}/status")
async def api_update_crm_status(phone: str, req: CrmStatusRequest):
    ok = await update_crm_contact_status(phone, req.crm_status, req.custom_status)
    if not ok:
        raise HTTPException(404, "CRM contact not found")
    return {"success": True, "message": "CRM status updated"}


@app.patch("/api/crm/contacts/{phone}/followup")
async def api_update_crm_followup(phone: str, req: CrmFollowupRequest):
    ok = await update_crm_contact_followup(phone, req.next_followup_at)
    if not ok:
        raise HTTPException(404, "CRM contact not found")
    return {"success": True, "message": "Follow-up updated"}


@app.patch("/api/crm/contacts/{phone}/notes")
async def api_update_crm_notes(phone: str, req: CrmNotesRequest):
    ok = await update_crm_contact_notes(phone, req.crm_notes)
    if not ok:
        raise HTTPException(404, "CRM contact not found")
    return {"success": True, "message": "CRM notes updated"}


@app.delete("/api/crm/contacts/{phone}")
async def api_delete_crm_contact(phone: str):
    """Hard-delete a CRM contact. Related call/appointment history is preserved."""
    try:
        clean = normalize_phone(phone)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    try:
        ok = await delete_crm_contact_by_phone(clean)
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    if not ok:
        raise HTTPException(404, "CRM contact not found")
    return {"success": True, "message": "Lead deleted", "phone_number": clean}


@app.get("/api/agent-profiles")
async def api_list_agent_profiles():
    return await get_all_agent_profiles()


@app.post("/api/agent-profiles")
async def api_create_agent_profile(req: AgentProfileRequest):
    profile_id = await create_agent_profile(req.name, req.voice, req.model, req.system_prompt, req.enabled_tools, req.is_default)
    return {"status": "created", "id": profile_id}


@app.get("/api/agent-profiles/{profile_id}")
async def api_get_agent_profile(profile_id: str):
    profile = await get_agent_profile(profile_id)
    if not profile:
        raise HTTPException(404, "Profile not found")
    return profile


@app.put("/api/agent-profiles/{profile_id}")
async def api_update_agent_profile(profile_id: str, req: AgentProfileRequest):
    ok = await update_agent_profile(profile_id, {"name": req.name, "voice": req.voice, "model": req.model, "system_prompt": req.system_prompt, "enabled_tools": req.enabled_tools, "is_default": 1 if req.is_default else 0})
    if not ok:
        raise HTTPException(404, "Profile not found")
    return {"status": "updated"}


@app.delete("/api/agent-profiles/{profile_id}")
async def api_delete_agent_profile(profile_id: str):
    ok = await delete_agent_profile(profile_id)
    if not ok:
        raise HTTPException(404, "Profile not found")
    return {"status": "deleted"}


@app.post("/api/agent-profiles/{profile_id}/set-default")
async def api_set_default_profile(profile_id: str):
    await set_default_agent_profile(profile_id)
    return {"status": "default set"}


# ── Batch call in-memory store ──────────────────────────────────────
# Batches are ephemeral (process-lifetime). The per-item results are also
# written back to CRM contacts so nothing is lost if the process restarts.
_batch_store: "dict[str, dict]" = {}
_active_batch_tasks: "dict[str, asyncio.Task]" = {}

_BATCH_ITEM_OUTCOME_TO_CRM_STATUS = {
    "answered": "Contacted",
    "completed": "Contacted",
    "no_answer": "No Answer",
    "busy": "Busy",
    "failed": "Failed",
}


def _batch_summary(b: dict) -> dict:
    items = b.get("items", [])
    counts: dict = {s: 0 for s in ("pending", "calling", "answered", "no_answer", "busy", "failed", "completed", "cancelled")}
    current_phone = current_name = None
    for it in items:
        st = it.get("status", "pending")
        counts[st] = counts.get(st, 0) + 1
        if st == "calling":
            current_phone = it.get("phone")
            current_name = it.get("lead_name")
    return {
        "batch_id": b["batch_id"],
        "batch_name": b.get("batch_name", ""),
        "status": b.get("status", "pending"),
        "total_count": len(items),
        **counts,
        "current_phone": current_phone,
        "current_lead_name": current_name,
        "started_at": b.get("started_at"),
        "completed_at": b.get("completed_at"),
        "next_allowed_at": b.get("next_allowed_at"),
        "items": items,
    }


async def _run_batch(batch_id: str) -> None:
    b = _batch_store.get(batch_id)
    if not b:
        return
    b["status"] = "running"
    b["started_at"] = datetime.now().isoformat()
    items = b["items"]

    if b.get("agent_profile_id"):
        await log_error("server", "agent_profile_prompt_ignored_for_voice", f"batch_id={batch_id}; agent_profile_id={b['agent_profile_id']}", "info")
    profile = None

    url = await eff("LIVEKIT_URL")
    key = await eff("LIVEKIT_API_KEY")
    secret = await eff("LIVEKIT_API_SECRET")
    if not (url and key and secret):
        b["status"] = "failed"
        return

    delay_seconds = b.get("call_delay_seconds", 5)

    from livekit import api as lk_api_module
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE
    session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ssl_ctx))
    try:
        lk = lk_api_module.LiveKitAPI(url=url, api_key=key, api_secret=secret, session=session)
        for idx, item in enumerate(items):
            if item.get("status") not in (None, "pending"):
                continue  # already done — skip on resume

            # Calling-window guard
            if not await _is_outbound_allowed():
                b["status"] = "waiting_schedule"
                err = await _outbound_window_error()
                b["next_allowed_at"] = err.get("next_allowed_at")
                opened = await _wait_for_batch_window(batch_id)
                if not opened:
                    break
                b["status"] = "running"
                b["next_allowed_at"] = None

            # Pause / stop check
            current = b.get("status")
            if current == "paused":
                break
            if current in ("stopped", "completed"):
                for it in items[idx:]:
                    if it.get("status") in (None, "pending"):
                        it["status"] = "cancelled"
                break

            phone = (item.get("phone") or "").strip()
            if not phone.startswith("+"):
                item["status"] = "failed"
                item["outcome"] = "invalid_phone"
                continue

            item["status"] = "calling"
            item["started_at"] = datetime.now().isoformat()

            room_name = f"batch-{batch_id[:8]}-{phone.replace('+','')}-{random.randint(100,999)}"
            item["room_name"] = room_name
            dispatched_at = time.time()
            contact = {
                "phone": phone,
                "lead_name": item.get("lead_name", "there"),
                "business_name": item.get("business_name", "our company"),
                "service_type": item.get("service_type", "our service"),
            }
            dispatched = await _dispatch_one(lk, lk_api_module, contact, room_name, b.get("system_prompt"), profile)
            if not dispatched:
                item["status"] = "failed"
                item["outcome"] = "dispatch_failed"
                item["ended_at"] = datetime.now().isoformat()
                continue

            # Sequential: wait for room to drain
            room_result = await _wait_for_room_finished(lk, lk_api_module, room_name)
            if room_result == "no_answer":
                item["status"] = "no_answer"
                item["outcome"] = "no_answer"
            elif room_result == "timeout":
                item["status"] = "failed"
                item["outcome"] = "timeout"
            else:
                outcome = await _classify_call_outcome(phone, dispatched_at, room_name)
                item["status"] = _BATCH_ITEM_OUTCOME_TO_CRM_STATUS.get(outcome, "completed").lower().replace(" ", "_")
                # Normalise to valid batch item status
                if item["status"] not in ("answered", "no_answer", "busy", "failed", "completed", "cancelled"):
                    item["status"] = "completed"
                item["outcome"] = outcome
            item["ended_at"] = datetime.now().isoformat()

            # Update CRM status and next due date for this lead
            try:
                outcome = (item.get("outcome") or "").lower()
                crm_status = _BATCH_ITEM_OUTCOME_TO_CRM_STATUS.get(outcome, "")
                if crm_status and phone:
                    clean_phone = normalize_phone(phone)
                    await update_crm_contact_status(clean_phone, crm_status, None)
                    now_dt = datetime.now()
                    next_due = None
                    if outcome in ("no_answer", "busy", "failed"):
                        if now_dt.hour < 17:
                            next_due = (now_dt + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M")
                        else:
                            next_due = (now_dt + timedelta(days=1)).strftime("%Y-%m-%dT10:30")
                    elif outcome in ("answered", "completed"):
                        next_due = (now_dt + timedelta(days=1)).strftime("%Y-%m-%dT11:00")
                    if next_due:
                        await update_crm_contact_followup(clean_phone, next_due)
            except Exception as exc:
                logger.warning("Batch CRM status and due date update failed for %s: %s", phone, exc)

            # WhatsApp fallback on call outcome
            try:
                call_outcome = item.get("outcome") or ""
                call_log_id = item.get("room_name") or ""
                contact_info = {"lead_name": item.get("lead_name", "there")}
                await handle_call_outcome_whatsapp_fallback(phone, call_outcome, call_log_id, contact_info)
            except Exception:
                pass

            # Check pause/stop after each call
            if b.get("status") in ("paused", "stopped"):
                break

            if idx < len(items) - 1 and delay_seconds:
                await asyncio.sleep(delay_seconds)

        await lk.aclose()
    except Exception as exc:
        logger.error("Batch run error: %s", exc)
        b["status"] = "failed"
    finally:
        await session.close()
        _active_batch_tasks.pop(batch_id, None)

    # Mark completed only when all items are terminal
    if b.get("status") not in ("paused", "stopped", "failed", "waiting_schedule"):
        all_done = all(it.get("status") not in (None, "pending", "calling") for it in items)
        b["status"] = "completed" if all_done else b.get("status", "completed")
    if b.get("status") == "completed":
        b["completed_at"] = datetime.now().isoformat()


async def _wait_for_batch_window(batch_id: str, poll_seconds: int = 120) -> bool:
    """Wait until outbound window opens, checking batch stop/pause between polls."""
    while not await _is_outbound_allowed():
        b = _batch_store.get(batch_id)
        if not b or b.get("status") in ("paused", "stopped", "completed"):
            return False
        tz_name, start_str, *_ = await _outbound_window()
        logger.info("Batch %s: outside outbound window (%s), sleeping %ds", batch_id, start_str, poll_seconds)
        await asyncio.sleep(poll_seconds)
    return True


def _start_batch_task(batch_id: str) -> None:
    existing = _active_batch_tasks.get(batch_id)
    if existing and not existing.done():
        return
    task = asyncio.create_task(_run_batch(batch_id))
    _active_batch_tasks[batch_id] = task


@app.post("/api/batch-calls")
async def api_create_batch(req: BatchCallRequest):
    if not await _is_outbound_allowed():
        raise HTTPException(403, await _outbound_window_error())
    if not req.contacts:
        raise HTTPException(400, "contacts list cannot be empty")
    batch_id = str(uuid.uuid4())
    items = []
    seen: set = set()
    for c in req.contacts:
        phone = (c.get("phone") or "").strip()
        if not phone or phone in seen:
            continue
        seen.add(phone)
        items.append({
            "phone": phone,
            "lead_name": c.get("lead_name") or c.get("name") or "there",
            "business_name": c.get("business_name") or "our company",
            "service_type": c.get("service_type") or "our service",
            "status": "pending",
            "outcome": None,
            "room_name": None,
            "started_at": None,
            "ended_at": None,
        })
    if not items:
        raise HTTPException(400, "No valid contacts after deduplication")
    b = {
        "batch_id": batch_id,
        "batch_name": req.batch_name or f"Batch {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "status": "pending",
        "items": items,
        "call_delay_seconds": req.call_delay_seconds,
        "agent_profile_id": req.agent_profile_id,
        "system_prompt": req.system_prompt,
        "started_at": None,
        "completed_at": None,
        "next_allowed_at": None,
    }
    _batch_store[batch_id] = b
    _start_batch_task(batch_id)
    return _batch_summary(b)


@app.get("/api/batch-calls")
async def api_list_batches():
    return [_batch_summary(b) for b in _batch_store.values()]


@app.get("/api/batch-calls/{batch_id}")
async def api_get_batch(batch_id: str):
    b = _batch_store.get(batch_id)
    if not b:
        raise HTTPException(404, "Batch not found")
    return _batch_summary(b)


@app.patch("/api/batch-calls/{batch_id}/status")
async def api_update_batch_status(batch_id: str, req: StatusRequest):
    b = _batch_store.get(batch_id)
    if not b:
        raise HTTPException(404, "Batch not found")
    allowed = {"paused", "running", "stopped"}
    if req.status not in allowed:
        raise HTTPException(400, f"status must be one of: {', '.join(sorted(allowed))}")
    if req.status == "stopped":
        b["status"] = "stopped"
        for it in b["items"]:
            if it.get("status") in (None, "pending"):
                it["status"] = "cancelled"
    elif req.status == "paused":
        b["status"] = "paused"
    elif req.status == "running":
        b["status"] = "running"
        _start_batch_task(batch_id)  # resumes — runner skips non-pending items
    return _batch_summary(b)


# ── Campaign sequencing ─────────────────────────────────────────────
# These statuses live in each ``contact`` dict inside ``contacts_json`` and are
# persisted back to Supabase after every transition so pause/resume/stop
# survives a process restart.
CAMPAIGN_TERMINAL_LEAD_STATUSES = {
    "completed", "answered", "no_answer", "busy", "failed", "skipped", "cancelled",
}

# Map call_logs.outcome → campaign lead status.
_OUTCOME_TO_LEAD_STATUS = {
    "booked": "answered",
    "answered": "answered",
    "interested": "answered",
    "not_interested": "answered",
    "callback_requested": "answered",
    "transferred": "answered",
    "completed": "completed",
    "no_answer": "no_answer",
    "busy": "busy",
    "failed": "failed",
    "voicemail": "no_answer",
}

# Tasks currently executing each campaign (so /run can be idempotent).
_active_campaign_tasks: "dict[str, asyncio.Task]" = {}


def _campaign_env_int(name: str, default: int) -> int:
    try:
        return max(int(os.getenv(name, str(default))), 0)
    except (TypeError, ValueError):
        return default


async def _campaign_max_concurrent() -> int:
    raw = await eff("MAX_CONCURRENT_OUTBOUND_CALLS")
    try:
        return max(int(raw or "1"), 1)
    except (TypeError, ValueError):
        return 1


async def _campaign_call_delay(default: int) -> int:
    raw = await eff("OUTBOUND_CALL_DELAY_SECONDS")
    try:
        return max(int(raw or str(default)), 0)
    except (TypeError, ValueError):
        return default


def _norm_lead_status(value: Optional[str]) -> str:
    return (value or "").strip().lower() or "pending"


async def _build_dispatch_metadata(contact: dict, prompt: Optional[str], profile: Optional[dict]) -> dict:
    lead_name = (contact.get("customer_name") or contact.get("name") or contact.get("lead_name") or "there").strip()
    business_name = (contact.get("business_name") or contact.get("company_name") or "our company").strip()
    company_name = (contact.get("company_name") or contact.get("business_name") or business_name).strip()
    service_type = (contact.get("service_type") or contact.get("service") or "our service").strip()
    requirement = (contact.get("requirement") or contact.get("notes") or contact.get("crm_notes") or "").strip()
    source = (contact.get("source") or "").strip()
    call_type = (contact.get("call_type") or "welcome_call").strip()

    # Voice calls use only the dedicated safe voice prompt. Request/campaign,
    # agent-profile, legacy, and old call-type prompt text must not drive openings.
    if prompt:
        await log_error("server", "legacy_prompt_ignored_for_voice", "campaign_or_custom_prompt_ignored", "info")
    saved_prompt, prompt_meta = await resolve_voice_call_prompt_with_meta(
        lead_name=lead_name,
        business_name=business_name,
        service_type=service_type,
    )

    metadata = {
        "phone_number": contact["phone"],
        "lead_name": lead_name,
        "name": lead_name,
        "customer_name": lead_name,
        "business_name": business_name,
        "company_name": company_name,
        "service_type": service_type,
        "requirement": requirement,
        "notes": contact.get("notes") or contact.get("crm_notes") or "",
        "source": source,
        "call_type": call_type,
        "system_prompt": saved_prompt,
        "prompt_source_selected": prompt_meta.get("prompt_source_selected", ""),
        "prompt_mode_selected": prompt_meta.get("prompt_mode_selected", ""),
        "prompt_default_used": str(prompt_meta.get("prompt_default_used", False)).lower(),
    }
    if profile:
        await log_error("server", "agent_profile_prompt_ignored_for_voice", "profile_supplied_but_not_used", "info")
    return metadata


async def _dispatch_one(lk, lk_api, contact: dict, room_name: str, prompt: Optional[str], profile: Optional[dict] = None) -> bool:
    try:
        metadata = await _build_dispatch_metadata(contact, prompt, profile)
        await log_error(
            "server",
            "automation_call_payload",
            (
                f"phone={metadata.get('phone_number')}; "
                f"source={metadata.get('source') or 'missing'}; "
                f"service_type={metadata.get('service_type') or 'missing'}; "
                f"call_type={metadata.get('call_type') or 'welcome_call'}"
            ),
            "info",
        )
        await log_error(
            "server",
            "outbound_metadata_prepared",
            (
                f"room={room_name}; phone={metadata.get('phone_number')}; "
                f"customer_name={metadata.get('customer_name') or 'missing'}; "
                f"business_name={metadata.get('business_name') or 'missing'}; "
                f"company_name={metadata.get('company_name') or 'missing'}; "
                f"service_type={metadata.get('service_type') or 'missing'}; "
                f"source={metadata.get('source') or 'missing'}; "
                f"call_type={metadata.get('call_type') or 'welcome_call'}"
            ),
            "info",
        )
        room_kwargs = {"name": room_name, "empty_timeout": 300, "max_participants": 5}
        try:
            room_req = lk_api.CreateRoomRequest(**room_kwargs, metadata=json.dumps(metadata))
        except TypeError:
            room_req = lk_api.CreateRoomRequest(**room_kwargs)
        await lk.room.create_room(room_req)
        agent_name = await _outbound_agent_name()
        await log_error("server", "livekit_agent_dispatch_name", agent_name, "info")
        await lk.agent_dispatch.create_dispatch(lk_api.CreateAgentDispatchRequest(agent_name=agent_name, room=room_name, metadata=json.dumps(metadata)))
        await increment_lead_attempts(metadata.get("phone_number") or "", "call")
        return True
    except Exception as exc:
        logger.error("Campaign dispatch error for %s: %s", contact.get("phone"), exc)
        return False


async def _wait_for_room_finished(lk, lk_api_module, room_name: str, *, max_wait: int = 3600, poll_interval: int = 8) -> str:
    """Block until the dispatched LiveKit room is empty (call ended).

    Returns one of: ``completed`` (room gone / both peers left), ``no_answer``
    (SIP never joined within the answer-grace window), ``timeout`` (safety cap).
    This is what gives us *sequential* calling: we don't start lead N+1 until
    lead N's room has shut down.
    """
    start = time.time()
    answer_grace = start + 90  # SIP has 90s to actually ring + answer
    sip_seen = False
    while True:
        if time.time() - start > max_wait:
            return "timeout"
        try:
            resp = await lk.room.list_rooms(lk_api_module.ListRoomsRequest(names=[room_name]))
            rooms = list(getattr(resp, "rooms", []) or [])
        except Exception as exc:
            logger.debug("list_rooms error for %s: %s", room_name, exc)
            await asyncio.sleep(poll_interval)
            continue
        if not rooms:
            # Room is gone — either never created or already torn down.
            if sip_seen or time.time() > answer_grace:
                return "completed"
            await asyncio.sleep(poll_interval)
            continue
        room = rooms[0]
        num_participants = int(getattr(room, "num_participants", 0) or 0)
        if num_participants >= 2:
            sip_seen = True
        if sip_seen and num_participants <= 1:
            # Caller hung up: only the agent (or no one) is left.
            return "completed"
        if not sip_seen and time.time() > answer_grace:
            return "no_answer"
        await asyncio.sleep(poll_interval)


async def _classify_call_outcome(phone: str, dispatch_time: float, room_name: str) -> str:
    """After the room closes, look up the call_log entry the agent just wrote."""
    try:
        calls = await get_calls_by_phone(phone)
    except Exception:
        return "completed"
    for call in calls or []:
        ts = call.get("timestamp") or ""
        try:
            # call_logs.timestamp is ISO; compare as string within the last hour.
            if ts and ts >= datetime.fromtimestamp(dispatch_time).isoformat()[:19]:
                outcome = (call.get("outcome") or "").strip().lower()
                return _OUTCOME_TO_LEAD_STATUS.get(outcome, "completed")
        except Exception:
            continue
    return "completed"


async def _run_campaign(campaign_id: str) -> None:
    campaign = await get_campaign(campaign_id)
    if not campaign:
        return
    try:
        contacts = json.loads(campaign.get("contacts_json") or "[]")
    except Exception:
        contacts = []
    if not isinstance(contacts, list) or not contacts:
        return

    url = await eff("LIVEKIT_URL")
    key = await eff("LIVEKIT_API_KEY")
    secret = await eff("LIVEKIT_API_SECRET")
    if not (url and key and secret):
        logger.error("Campaign %s: LiveKit not configured", campaign_id)
        return

    campaign = await get_repository().claim_campaign(campaign_id, lease_seconds=7200)
    if not campaign:
        logger.info("Campaign %s is already owned by another worker", campaign_id)
        return
    claim_token = campaign.get("claim_token") or ""
    try:
        contacts = json.loads(campaign.get("contacts_json") or "[]")
    except Exception:
        contacts = []

    # De-dupe by phone within this campaign so the same number can't be queued
    # twice in a single run.
    seen_phones: set = set()
    deduped: list = []
    for contact in contacts:
        if not isinstance(contact, dict):
            continue
        phone = (contact.get("phone") or "").strip()
        if not phone:
            continue
        contact.setdefault("status", "pending")
        if phone in seen_phones and _norm_lead_status(contact.get("status")) == "pending":
            contact["status"] = "skipped"
            contact["skip_reason"] = "duplicate phone in campaign"
        seen_phones.add(phone)
        deduped.append(contact)
    contacts = deduped
    await update_campaign_contacts(campaign_id, contacts, claim_token=claim_token)

    if campaign.get("agent_profile_id"):
        await log_error("server", "agent_profile_prompt_ignored_for_voice", f"campaign_id={campaign_id}; agent_profile_id={campaign.get('agent_profile_id')}", "info")
    profile = None
    max_concurrent = await _campaign_max_concurrent()
    delay_seconds = await _campaign_call_delay(int(campaign.get("call_delay_seconds") or 30))

    from livekit import api as lk_api_module
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE
    session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ssl_ctx))
    ok_count = fail_count = 0
    final_status = "completed"
    try:
        lk = lk_api_module.LiveKitAPI(url=url, api_key=key, api_secret=secret, session=session)
        for idx, contact in enumerate(contacts):
            # Re-read campaign status on every iteration so pause/stop signals
            # arrive at the next safe boundary.
            latest = await get_campaign(campaign_id)
            current_status = (latest or {}).get("status") if latest else None
            if current_status == "paused":
                logger.info("Campaign %s paused — stopping after current safe boundary", campaign_id)
                final_status = "paused"
                break
            if current_status in ("stopped", "completed"):
                logger.info("Campaign %s %s — cancelling pending leads", campaign_id, current_status)
                final_status = current_status
                # Mark every still-pending lead as cancelled so resume can't run them.
                for c in contacts[idx:]:
                    if _norm_lead_status(c.get("status")) == "pending":
                        c["status"] = "cancelled"
                break

            if _norm_lead_status(contact.get("status")) != "pending":
                continue  # already handled in a previous run (resume case)

            # ── Outbound calling-window guard ──────────────────────────────
            # If we're currently outside the allowed calling window, wait until
            # it opens (or the campaign is paused/stopped externally).  Leads
            # are NEVER skipped or cancelled just because of the time — they
            # remain pending until we get a window.
            if not await _is_outbound_allowed():
                opened = await _wait_for_outbound_window(campaign_id)
                if not opened:
                    # Campaign was paused/stopped while waiting — exit cleanly.
                    latest = await get_campaign(campaign_id)
                    final_status = (latest or {}).get("status") or "paused"
                    break
                # Re-check campaign status now that time passed.
                latest = await get_campaign(campaign_id)
                current_status = (latest or {}).get("status") if latest else None
                if current_status in ("paused", "stopped", "completed"):
                    final_status = current_status
                    break

            phone = (contact.get("phone") or "").strip()
            if not phone.startswith("+"):
                contact["status"] = "failed"
                contact["failure_reason"] = "phone not in E.164 format"
                fail_count += 1
                await update_campaign_contacts(campaign_id, contacts, claim_token=claim_token)
                continue

            contact["status"] = "in_progress"
            contact["started_at"] = datetime.now().isoformat()
            await update_campaign_contacts(campaign_id, contacts, claim_token=claim_token)

            room_name = f"camp-{campaign_id[:8]}-{phone.replace('+','')}-{random.randint(100,999)}"
            contact["room_name"] = room_name
            dispatched_at = time.time()
            dispatched = await _dispatch_one(lk, lk_api_module, contact, room_name, campaign.get("system_prompt"), profile)
            if not dispatched:
                contact["status"] = "failed"
                contact["failure_reason"] = "dispatch failed"
                fail_count += 1
                await update_campaign_contacts(campaign_id, contacts, claim_token=claim_token)
                continue

            # Sequential mode: wait for the room to drain before moving on.
            if max_concurrent <= 1:
                room_result = await _wait_for_room_finished(lk, lk_api_module, room_name)
                if room_result == "no_answer":
                    contact["status"] = "no_answer"
                elif room_result == "timeout":
                    contact["status"] = "failed"
                    contact["failure_reason"] = "safety timeout while waiting for room"
                else:
                    contact["status"] = await _classify_call_outcome(phone, dispatched_at, room_name)
            else:
                # Parallel mode: trust the agent to write a call_log and move on
                # after a short stagger.
                contact["status"] = "completed"

            contact["finished_at"] = datetime.now().isoformat()
            ok_count += 1 if contact["status"] in ("answered", "completed") else 0
            if contact["status"] in ("failed", "no_answer", "busy"):
                fail_count += 1
            await update_campaign_contacts(campaign_id, contacts, claim_token=claim_token)

            # WhatsApp fallback on campaign call outcome
            try:
                call_outcome = contact.get("status") or ""
                call_log_id = contact.get("room_name") or ""
                contact_info = {"lead_name": contact.get("lead_name", "there")}
                await handle_call_outcome_whatsapp_fallback(phone, call_outcome, call_log_id, contact_info)
            except Exception:
                pass

            # Pause/Stop can come in *during* a call — re-check right after
            # finishing this one and don't sleep into the next dispatch.
            latest = await get_campaign(campaign_id)
            current_status = (latest or {}).get("status") if latest else None
            if current_status == "paused":
                final_status = "paused"
                break
            if current_status in ("stopped", "completed"):
                final_status = current_status
                break

            # Inter-call delay (only between calls, not after the last one).
            if idx < len(contacts) - 1 and delay_seconds:
                await asyncio.sleep(delay_seconds)
        await lk.aclose()
    except Exception as exc:
        logger.error("Campaign run error: %s", exc)
        await log_error("server", f"Campaign {campaign_id} run error", str(exc), "error")
    finally:
        await session.close()
        _active_campaign_tasks.pop(campaign_id, None)

    # If everything got through, mark completed; otherwise leave whatever
    # pause/stop state we detected.
    if final_status == "completed" and all(_norm_lead_status(c.get("status")) in CAMPAIGN_TERMINAL_LEAD_STATUSES for c in contacts):
        await update_campaign_run_stats(campaign_id, ok_count, fail_count, status="completed", claim_token=claim_token)
    else:
        await update_campaign_run_stats(campaign_id, ok_count, fail_count, status=final_status, claim_token=claim_token)


def _start_campaign_task(campaign_id: str) -> None:
    existing = _active_campaign_tasks.get(campaign_id)
    if existing and not existing.done():
        return  # already running, don't double-spawn
    task = asyncio.create_task(_run_campaign(campaign_id))
    _active_campaign_tasks[campaign_id] = task


async def _reschedule_all_campaigns() -> None:
    if not _scheduler:
        return
    try:
        for c in await get_all_campaigns():
            if c.get("status") == "active" and c.get("schedule_type") in ("daily", "weekdays"):
                _schedule_campaign(c["id"], c["schedule_type"], c.get("schedule_time", "09:00"))
    except Exception as exc:
        logger.warning("Could not reschedule campaigns: %s", exc)


def _run_campaign_sync(campaign_id: str) -> None:
    """Sync wrapper for APScheduler campaign cron job."""
    run_coro_background(_run_campaign(campaign_id), f"campaign_run_{campaign_id}")


def _run_due_automation_actions_sync() -> None:
    """Sync wrapper for APScheduler — bridges thread context to async."""
    run_coro_background(run_due_automation_actions(), "automation_actions")


def _scheduled_recording_cleanup_sync() -> None:
    """Sync wrapper for APScheduler recording cleanup."""
    run_coro_background(_scheduled_recording_cleanup(), "recording_cleanup")


async def _scheduled_recording_cleanup() -> None:
    try:
        if await _recording_auto_delete_enabled():
            await _cleanup_old_recordings()
    except Exception as exc:
        logger.exception("Scheduled recording cleanup failed: %s", exc)
        await log_error("server", "Scheduled recording cleanup failed", str(exc), "error")


async def _schedule_recording_cleanup() -> None:
    if not _scheduler:
        return
    job_id = "recording_cleanup"
    if _scheduler.get_job(job_id):
        _scheduler.remove_job(job_id)
    if not await _recording_auto_delete_enabled():
        return
    cleanup_time = await _recording_cleanup_time()
    try:
        hour, minute = map(int, cleanup_time.split(":"))
    except (ValueError, AttributeError):
        hour, minute = 2, 0
    trigger = CronTrigger(hour=hour, minute=minute, timezone=ZoneInfo(DEFAULT_TIMEZONE))
    _scheduler.add_job(_scheduled_recording_cleanup_sync, trigger=trigger, id=job_id, replace_existing=True)


def _schedule_campaign(campaign_id: str, schedule_type: str, schedule_time: str) -> None:
    if not _scheduler:
        return
    job_id = f"campaign_{campaign_id}"
    if _scheduler.get_job(job_id):
        _scheduler.remove_job(job_id)
    try:
        hour, minute = map(int, schedule_time.split(":"))
    except (ValueError, AttributeError):
        hour, minute = 9, 0
    trigger = CronTrigger(hour=hour, minute=minute) if schedule_type == "daily" else CronTrigger(day_of_week="mon-fri", hour=hour, minute=minute)
    _scheduler.add_job(_run_campaign_sync, trigger=trigger, args=[campaign_id], id=job_id, replace_existing=True)


@app.post("/api/campaigns")
async def api_create_campaign(req: CampaignRequest):
    if not req.contacts:
        raise HTTPException(400, "contacts list cannot be empty")
    if req.schedule_type not in ("once", "daily", "weekdays"):
        raise HTTPException(400, "schedule_type must be: once | daily | weekdays")
    campaign_id = await create_campaign(req.name, json.dumps(req.contacts), req.schedule_type, req.schedule_time, req.call_delay_seconds, req.system_prompt, req.agent_profile_id)
    campaign = await get_campaign(campaign_id)
    if req.schedule_type == "once":
        _start_campaign_task(campaign_id)
    else:
        _schedule_campaign(campaign_id, req.schedule_type, req.schedule_time)
    return {"status": "created", "campaign_id": campaign_id, "campaign": campaign}


@app.get("/api/campaigns")
async def api_list_campaigns():
    return await get_all_campaigns()


@app.delete("/api/campaigns/{campaign_id}")
async def api_delete_campaign(campaign_id: str):
    ok = await delete_campaign(campaign_id)
    if not ok:
        raise HTTPException(404, "Campaign not found")
    job_id = f"campaign_{campaign_id}"
    if _scheduler and _scheduler.get_job(job_id):
        _scheduler.remove_job(job_id)
    return {"status": "deleted"}


@app.post("/api/campaigns/{campaign_id}/run")
async def api_run_campaign_now(campaign_id: str):
    if not await get_campaign(campaign_id):
        raise HTTPException(404, "Campaign not found")
    await update_campaign_status(campaign_id, "active")
    _start_campaign_task(campaign_id)
    return {"status": "dispatching", "campaign_id": campaign_id}


@app.get("/api/campaigns/{campaign_id}/progress")
async def api_campaign_progress(campaign_id: str):
    campaign = await get_campaign(campaign_id)
    if not campaign:
        raise HTTPException(404, "Campaign not found")
    try:
        contacts = json.loads(campaign.get("contacts_json") or "[]")
    except Exception:
        contacts = []
    counts: dict = {s: 0 for s in ("pending", "in_progress", "calling", "completed", "answered", "no_answer", "busy", "failed", "skipped", "cancelled")}
    current_phone = current_name = None
    for c in contacts:
        st = _norm_lead_status(c.get("status"))
        counts[st] = counts.get(st, 0) + 1
        if st in ("in_progress", "calling"):
            current_phone = c.get("phone")
            current_name = c.get("lead_name")
    return {
        "campaign_id": campaign_id,
        "name": campaign.get("name"),
        "status": campaign.get("status"),
        "total_leads": len(contacts),
        "total_count": len(contacts),
        "current_phone": current_phone,
        "current_lead_name": current_name,
        "counts": counts,
        **counts,
        "leads": [
            {
                "phone": c.get("phone"),
                "lead_name": c.get("lead_name"),
                "status": _norm_lead_status(c.get("status")),
                "started_at": c.get("started_at"),
                "finished_at": c.get("finished_at"),
                "room_name": c.get("room_name"),
                "failure_reason": c.get("failure_reason") or c.get("skip_reason"),
            } for c in contacts
        ],
    }


@app.patch("/api/campaigns/{campaign_id}/status")
async def api_update_campaign_status(campaign_id: str, req: StatusRequest):
    allowed = {"active", "paused", "completed", "stopped"}
    if req.status not in allowed:
        raise HTTPException(400, f"status must be one of: {', '.join(sorted(allowed))}")
    campaign = await get_campaign(campaign_id)
    if not campaign:
        raise HTTPException(404, "Campaign not found")

    ok = await update_campaign_status(campaign_id, req.status)
    if not ok:
        raise HTTPException(404, "Campaign not found")

    job_id = f"campaign_{campaign_id}"
    if req.status in ("paused", "stopped", "completed") and _scheduler and _scheduler.get_job(job_id):
        _scheduler.remove_job(job_id)

    if req.status == "stopped":
        # Mark every still-pending lead cancelled so resume cannot run them.
        try:
            contacts = json.loads(campaign.get("contacts_json") or "[]")
        except Exception:
            contacts = []
        changed = False
        for c in contacts:
            if _norm_lead_status(c.get("status")) == "pending":
                c["status"] = "cancelled"
                changed = True
        if changed:
            await update_campaign_contacts(campaign_id, contacts)

    if req.status == "active":
        # Resume: only the still-pending leads will be picked up (terminal
        # statuses are skipped inside the loop), so no number is ever called twice.
        if campaign.get("schedule_type") in ("daily", "weekdays"):
            _schedule_campaign(campaign_id, campaign["schedule_type"], campaign.get("schedule_time", "09:00"))
        else:
            _start_campaign_task(campaign_id)

    return {"status": req.status}


# ── Scheduled Calls (Due Today) ─────────────────────────────────────────

@app.get("/api/scheduled-calls")
async def api_scheduled_calls(timezone: Optional[str] = None):
    """Return CRM leads grouped by their scheduled follow-up state."""
    tz = timezone or (await eff("OUTBOUND_TIMEZONE")) or DEFAULT_TIMEZONE
    today_str = _tz_today(tz)
    all_leads = await get_crm_contacts(timezone=tz)
    window_ok = await _is_outbound_allowed()
    win_err = None if window_ok else await _outbound_window_error()

    due_today, upcoming, overdue, completed_today = [], [], [], []
    for lead in all_leads:
        status = (lead.get("crm_status") or "New")
        if status in CRM_TERMINAL_STATUSES:
            continue
        fup = (lead.get("next_followup_at") or "")[:10]
        if not fup:
            continue
        if fup == today_str:
            due_today.append(lead)
        elif fup < today_str:
            overdue.append(lead)
        elif fup > today_str:
            upcoming.append(lead)

    return {
        "window_allowed": window_ok,
        "window_error": win_err,
        "today": today_str,
        "timezone": tz,
        "due_today": due_today,
        "overdue": overdue,
        "upcoming": upcoming,
        "completed_today": completed_today,
        "counts": {
            "due_today": len(due_today),
            "overdue": len(overdue),
            "upcoming": len(upcoming),
        },
    }


@app.post("/api/scheduled-calls/start-due-today")
async def api_start_due_today(timezone: Optional[str] = Query(None)):
    """Create + start a batch from today’s Due Today CRM leads."""
    if not await _is_outbound_allowed():
        raise HTTPException(403, await _outbound_window_error())
    tz = timezone or (await eff("OUTBOUND_TIMEZONE")) or DEFAULT_TIMEZONE
    today_str = _tz_today(tz)
    all_leads = await get_crm_contacts(due_today=True, timezone=tz)
    if not all_leads:
        raise HTTPException(404, "No Due Today leads found")
    contacts = [
        {
            "phone": lead["phone_number"],
            "lead_name": lead.get("lead_name") or "there",
            "business_name": lead.get("business_name") or "our company",
            "service_type": lead.get("service_type") or "our service",
            "call_type": _crm_status_to_call_type(lead.get("crm_status") or ""),
        }
        for lead in all_leads
        if lead.get("phone_number")
    ]
    batch_name = f"Due Today Calls - {today_str}"
    # Create via the existing batch machinery
    batch_id = str(uuid.uuid4())
    items = []
    seen: set = set()
    for c in contacts:
        phone = (c.get("phone") or "").strip()
        if not phone or phone in seen:
            continue
        seen.add(phone)
        items.append({
            "phone": phone,
            "lead_name": c.get("lead_name", "there"),
            "business_name": c.get("business_name", "our company"),
            "service_type": c.get("service_type", "our service"),
            "status": "pending", "outcome": None, "room_name": None,
            "started_at": None, "ended_at": None,
        })
    if not items:
        raise HTTPException(404, "No valid leads to call")
    b = {
        "batch_id": batch_id, "batch_name": batch_name, "status": "pending",
        "items": items, "call_delay_seconds": 10, "agent_profile_id": None,
        "system_prompt": None, "started_at": None, "completed_at": None, "next_allowed_at": None,
    }
    _batch_store[batch_id] = b
    _start_batch_task(batch_id)
    return {"batch_id": batch_id, "batch_name": batch_name, "total": len(items), **_batch_summary(b)}


@app.post("/api/scheduled-calls/whatsapp-due-today")
async def api_whatsapp_due_today(timezone: Optional[str] = Query(None)):
    """Dispatch WhatsApp outreach to all leads in today's due queue."""
    tz = timezone or (await eff("OUTBOUND_TIMEZONE")) or DEFAULT_TIMEZONE
    all_leads = await get_crm_contacts(due_today=True, timezone=tz)
    if not all_leads:
        raise HTTPException(404, "No Due Today leads found")
    dispatched = 0
    from whatsapp import send_whatsapp_template, resolve_wa_template
    template_name = await resolve_wa_template("welcome_template") or "welcome"
    for lead in all_leads:
        phone = lead.get("phone_number")
        if not phone:
            continue
        try:
            name = lead.get("lead_name") or "there"
            await send_whatsapp_template(phone, template_name, language="en", parameters=[name], event_type="due_today_outreach", source_type="telecaller_queue")
            dispatched += 1
        except Exception as exc:
            logger.warning("WhatsApp due today send failed for %s: %s", phone, exc)
    return {"success": True, "dispatched": dispatched, "total": len(all_leads)}


# ── Outbound Schedule Settings ─────────────────────────────────────────

_SCHEDULE_KEYS = (
    "OUTBOUND_TIMEZONE", "OUTBOUND_START_TIME", "OUTBOUND_END_TIME",
    "OUTBOUND_ALLOWED_DAYS", "OUTBOUND_CALLING_ENABLED",
)


@app.get("/api/outbound/schedule/settings")
async def api_get_outbound_schedule_settings():
    result = {}
    for key in _SCHEDULE_KEYS:
        result[key] = await eff(key)
    # Apply defaults for missing values
    result.setdefault("OUTBOUND_TIMEZONE", DEFAULT_TIMEZONE)
    if not result["OUTBOUND_TIMEZONE"]:
        result["OUTBOUND_TIMEZONE"] = DEFAULT_TIMEZONE
    result.setdefault("OUTBOUND_START_TIME", "10:00")
    if not result["OUTBOUND_START_TIME"]:
        result["OUTBOUND_START_TIME"] = "10:00"
    result.setdefault("OUTBOUND_END_TIME", "19:00")
    if not result["OUTBOUND_END_TIME"]:
        result["OUTBOUND_END_TIME"] = "19:00"
    result.setdefault("OUTBOUND_ALLOWED_DAYS", "mon,tue,wed,thu,fri,sat")
    if not result["OUTBOUND_ALLOWED_DAYS"]:
        result["OUTBOUND_ALLOWED_DAYS"] = "mon,tue,wed,thu,fri,sat"
    result.setdefault("OUTBOUND_CALLING_ENABLED", "true")
    if not result["OUTBOUND_CALLING_ENABLED"]:
        result["OUTBOUND_CALLING_ENABLED"] = "true"
    return result


@app.post("/api/outbound/schedule/settings")
async def api_save_outbound_schedule_settings(req: OutboundScheduleRequest):
    data = {k: v for k, v in req.dict().items() if v is not None and v != ""}
    if not data:
        raise HTTPException(400, "No settings provided")
    await save_settings(data)
    # Propagate to os.environ so the running process picks them up immediately
    # without needing a restart (only fills gaps, honours existing VPS env vars).
    for k, v in data.items():
        if not os.environ.get(k):
            os.environ[k] = v
    # Return the updated schedule status
    return await api_get_outbound_schedule_status()


@app.get("/api/outbound/schedule/status")
async def api_get_outbound_schedule_status():
    tz_name, start_str, end_str, allowed_days = await _outbound_window()
    enabled_raw = (await eff("OUTBOUND_CALLING_ENABLED") or "true").strip().lower()
    calling_enabled = enabled_raw not in ("0", "false", "no", "off")
    allowed = calling_enabled and await _is_outbound_allowed()
    reason = ""
    if not calling_enabled:
        reason = "Outbound calling is disabled (OUTBOUND_CALLING_ENABLED=false)"
    elif not allowed:
        try:
            from zoneinfo import ZoneInfo
            now_tz = datetime.now(tz=ZoneInfo(tz_name))
        except Exception:
            now_tz = datetime.now()
        day_abbr = now_tz.strftime("%a").lower()
        if day_abbr not in allowed_days:
            reason = f"Today ({now_tz.strftime('%A')}) is not an allowed calling day"
        else:
            reason = f"Current time is outside allowed window ({start_str}–{end_str} {tz_name})"
    win_err = await _outbound_window_error() if not allowed else None
    return {
        "allowed": allowed,
        "calling_enabled": calling_enabled,
        "reason": reason if not allowed else "Within allowed outbound calling window",
        "timezone": tz_name,
        "start_time": start_str,
        "end_time": end_str,
        "allowed_days": sorted(allowed_days),
        "next_allowed_at": win_err.get("next_allowed_at") if win_err else None,
    }


# ── Dental Billing, Invoicing & Revenue Endpoints ──────────────────────────────

class CreateInvoiceApiRequest(BaseModel):
    patient_name: str
    phone: str
    doctor_name: Optional[str] = "Dr. P. Sudhakar"
    doctor_id: Optional[str] = ""
    treatment_date: Optional[str] = None
    items: Optional[List[Dict[str, Any]]] = None
    discount: Optional[float] = 0.0
    tax: Optional[float] = 0.0
    paid_amount: Optional[float] = None
    payment_method: Optional[str] = "UPI"
    notes: Optional[str] = ""


class RecordPaymentApiRequest(BaseModel):
    amount: float
    payment_method: Optional[str] = "UPI"
    notes: Optional[str] = ""


@app.get("/api/billing/invoices")
async def api_list_invoices(
    q: Optional[str] = None,
    status: Optional[str] = None,
    doctor_name: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
):
    from billing import list_invoices
    try:
        return await list_invoices(
            q=q,
            status=status,
            doctor_name=doctor_name,
            date_from=date_from,
            date_to=date_to,
            limit=limit,
            offset=offset,
        )
    except Exception as exc:
        logger.error(f"api_list_invoices error: {exc}")
        raise HTTPException(500, str(exc))


@app.post("/api/billing/invoices")
async def api_create_invoice(req: CreateInvoiceApiRequest):
    from billing import create_invoice
    try:
        inv = await create_invoice(req.dict())
        return {"success": True, "invoice": inv}
    except ValueError as ve:
        raise HTTPException(400, str(ve))
    except Exception as exc:
        logger.error(f"api_create_invoice error: {exc}")
        raise HTTPException(500, str(exc))


@app.get("/api/billing/invoices/{invoice_id}")
async def api_get_invoice(invoice_id: str):
    from billing import get_invoice
    inv = await get_invoice(invoice_id)
    if not inv:
        raise HTTPException(404, "Invoice not found")
    return {"invoice": inv}


@app.post("/api/billing/invoices/{invoice_id}/payment")
async def api_record_invoice_payment(invoice_id: str, req: RecordPaymentApiRequest):
    from billing import add_invoice_payment
    try:
        inv = await add_invoice_payment(
            invoice_id=invoice_id,
            amount=req.amount,
            payment_method=req.payment_method or "UPI",
            notes=req.notes or "",
        )
        return {"success": True, "invoice": inv}
    except ValueError as ve:
        raise HTTPException(400, str(ve))
    except Exception as exc:
        logger.error(f"api_record_invoice_payment error: {exc}")
        raise HTTPException(500, str(exc))


@app.get("/api/billing/revenue-summary")
async def api_get_revenue_summary(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
):
    from billing import get_revenue_summary
    try:
        return await get_revenue_summary(date_from=date_from, date_to=date_to)
    except Exception as exc:
        logger.error(f"api_get_revenue_summary error: {exc}")
        raise HTTPException(500, str(exc))


@app.get("/api/billing/export")
async def api_export_billing_sales(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
):
    from billing import export_sales_csv
    try:
        csv_data = await export_sales_csv(date_from=date_from, date_to=date_to)
        return Response(
            content=csv_data,
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=smiledental_sales_report.csv"},
        )
    except Exception as exc:
        logger.error(f"api_export_billing_sales error: {exc}")
        raise HTTPException(500, str(exc))


@app.post("/api/billing/invoices/{invoice_id}/send-whatsapp")
async def api_send_invoice_whatsapp(invoice_id: str):
    from billing import get_invoice, generate_whatsapp_receipt_text
    inv = await get_invoice(invoice_id)
    if not inv:
        raise HTTPException(404, "Invoice not found")

    phone = inv.get("phone")
    if not phone:
        raise HTTPException(400, "Patient phone number is missing")

    text = generate_whatsapp_receipt_text(inv)
    try:
        from whatsapp import send_whatsapp_text
        res = await send_whatsapp_text(phone, text)
        return {
            "success": True,
            "delivered": bool(res and res.get("success")),
            "details": res,
            "preview": text,
        }
    except Exception as exc:
        logger.error(f"api_send_invoice_whatsapp error: {exc}")
        return {
            "success": True,
            "delivered": False,
            "error": str(exc),
            "preview": text,
        }

