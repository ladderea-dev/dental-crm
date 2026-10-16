"""Secure field-service API for the AirZen Android staff application.

The mobile workflow is deliberately separate from the browser admin session.
Staff receive short-lived signed access tokens backed by revocable device
sessions. Mobile administrators receive separate short-lived tokens bound to
the current CRM administrator credentials. Arrival and completion evidence is
stored in the configured private S3-compatible bucket; only metadata and
short-lived signed URLs enter the CRM.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import mimetypes
import os
import secrets
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import PurePosixPath
from typing import Any, Literal, Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from pydantic import AliasChoices, BaseModel, Field

from db import get_appointment_by_id, get_repository, get_setting


router = APIRouter()

ACCESS_TOKEN_SECONDS = 15 * 60
ADMIN_ACCESS_TOKEN_SECONDS = 30 * 60
REFRESH_TOKEN_DAYS = 30
SERVICE_START_CODE_GRACE_HOURS = 24
MAX_PIN_FAILURES = 5
PIN_LOCK_MINUTES = 15
MAX_EVIDENCE_BYTES = 10 * 1024 * 1024
ALLOWED_SELFIE_TYPES = {"image/jpeg", "image/png", "image/webp"}
ALLOWED_PAYMENT_TYPES = ALLOWED_SELFIE_TYPES | {"application/pdf"}


class MobileLoginRequest(BaseModel):
    username: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    pin: str = Field(pattern=r"^\d{6}$")
    device_id: str = Field(min_length=4, max_length=191)
    device_name: str = Field(default="", max_length=255)
    app_version: str = Field(default="", max_length=64)


class MobileRefreshRequest(BaseModel):
    session_id: str = Field(min_length=36, max_length=36)
    refresh_token: str = Field(min_length=32, max_length=512)


class AdminMobileLoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=191)
    password: str = Field(min_length=1, max_length=512)
    device_id: str = Field(min_length=4, max_length=191)
    device_name: str = Field(default="", max_length=255)
    app_version: str = Field(default="", max_length=64)


class MobilePinRequest(BaseModel):
    username: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    pin: str = Field(pattern=r"^\d{6}$")
    active: bool = True


class MobileLocationRequest(BaseModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    accuracy_meters: Optional[float] = Field(default=None, ge=0, le=10000)
    speed_mps: Optional[float] = Field(default=None, ge=0, le=300)
    heading_degrees: Optional[float] = Field(default=None, ge=0, le=360)
    battery_percent: Optional[int] = Field(default=None, ge=0, le=100)
    is_mocked: bool = False
    captured_at: datetime
    idempotency_key: str = Field(min_length=8, max_length=255)


class MobileStartCodeRequest(BaseModel):
    code: str = Field(pattern=r"^\d{6}$", validation_alias=AliasChoices("code", "otp"))
    idempotency_key: str = Field(min_length=8, max_length=255)


class MobileJobActionRequest(BaseModel):
    latitude: Optional[float] = Field(default=None, ge=-90, le=90)
    longitude: Optional[float] = Field(default=None, ge=-180, le=180)
    accuracy_meters: Optional[float] = Field(default=None, ge=0, le=10000)
    idempotency_key: str = Field(min_length=8, max_length=255)


class MobileAttendanceRequest(BaseModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    accuracy_meters: Optional[float] = Field(default=None, ge=0, le=10000)
    notes: str = Field(default="", max_length=1000)


class MobileLeaveRequest(BaseModel):
    leave_type: Literal["annual", "medical", "emergency", "unpaid", "other"]
    start_date: date
    end_date: date
    reason: str = Field(min_length=3, max_length=2000)


class AdminLeaveDecisionRequest(BaseModel):
    decision: Literal["approved", "declined"]
    admin_note: str = Field(default="", max_length=2000)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _parse_datetime(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        current = value
    else:
        try:
            current = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _staff_today(identity: dict) -> str:
    timezone_name = str((identity.get("session") or {}).get("timezone") or "Asia/Singapore")
    try:
        local_timezone = ZoneInfo(timezone_name)
    except Exception:
        local_timezone = ZoneInfo("Asia/Singapore")
    return _utcnow().astimezone(local_timezone).date().isoformat()


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64decode(data: str) -> bytes:
    return base64.urlsafe_b64decode((data + "=" * (-len(data) % 4)).encode("ascii"))


def _mobile_secret() -> str:
    secret = (os.getenv("STAFF_MOBILE_TOKEN_SECRET") or "").strip()
    if len(secret) < 32:
        raise HTTPException(503, "Staff mobile authentication is not configured")
    return secret


def _admin_mobile_secret() -> str:
    secret = (os.getenv("SESSION_SECRET") or "").strip()
    if len(secret) < 32:
        raise HTTPException(503, "Admin mobile authentication is not configured")
    return secret


def _admin_credential_fingerprint() -> str:
    username = os.getenv("ADMIN_USERNAME", "")
    password = os.getenv("ADMIN_PASSWORD", "")
    if not username or not password:
        raise HTTPException(503, "Admin mobile authentication is not configured")
    return hmac.new(
        _admin_mobile_secret().encode("utf-8"),
        f"{username}:{password}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _sign_access_token(staff_id: str, session_id: str) -> str:
    now = int(time.time())
    payload = _b64encode(json.dumps({
        "sub": staff_id,
        "sid": session_id,
        "iat": now,
        "exp": now + ACCESS_TOKEN_SECONDS,
        "aud": "airzen-staff-mobile",
    }, separators=(",", ":")).encode("utf-8"))
    signature = hmac.new(_mobile_secret().encode("utf-8"), payload.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"


def _decode_access_token(token: str) -> Optional[dict]:
    if not token or "." not in token:
        return None
    payload, signature = token.rsplit(".", 1)
    expected = hmac.new(_mobile_secret().encode("utf-8"), payload.encode("ascii"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return None
    try:
        claims = json.loads(_b64decode(payload).decode("utf-8"))
    except Exception:
        return None
    if claims.get("aud") != "airzen-staff-mobile" or int(claims.get("exp") or 0) <= int(time.time()):
        return None
    if not claims.get("sub") or not claims.get("sid"):
        return None
    return claims


def _sign_admin_access_token(username: str) -> str:
    now = int(time.time())
    payload = _b64encode(json.dumps({
        "sub": username,
        "role": "admin",
        "credential": _admin_credential_fingerprint(),
        "iat": now,
        "exp": now + ADMIN_ACCESS_TOKEN_SECONDS,
        "aud": "airzen-admin-mobile",
    }, separators=(",", ":")).encode("utf-8"))
    signature = hmac.new(_admin_mobile_secret().encode("utf-8"), payload.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"


def _decode_admin_access_token(token: str) -> Optional[dict]:
    if not token or "." not in token:
        return None
    payload, signature = token.rsplit(".", 1)
    expected = hmac.new(_admin_mobile_secret().encode("utf-8"), payload.encode("ascii"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return None
    try:
        claims = json.loads(_b64decode(payload).decode("utf-8"))
    except Exception:
        return None
    if claims.get("aud") != "airzen-admin-mobile" or claims.get("role") != "admin":
        return None
    if int(claims.get("exp") or 0) <= int(time.time()):
        return None
    username = os.getenv("ADMIN_USERNAME", "")
    if not username or not hmac.compare_digest(str(claims.get("sub") or ""), username):
        return None
    if not hmac.compare_digest(str(claims.get("credential") or ""), _admin_credential_fingerprint()):
        return None
    return claims


def _hash_pin(pin: str, *, iterations: int = 310_000) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256${iterations}${_b64encode(salt)}${_b64encode(digest)}"


def _verify_pin(pin: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt, expected = str(encoded).split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), _b64decode(salt), int(iterations))
        return hmac.compare_digest(_b64encode(digest), expected)
    except Exception:
        return False


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _idempotency_hash(*parts: Any) -> str:
    raw = ":".join(str(part or "") for part in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _otp_hash(appointment_id: str, otp: str) -> str:
    raw = f"{appointment_id}:{otp}".encode("utf-8")
    return hmac.new(_mobile_secret().encode("utf-8"), raw, hashlib.sha256).hexdigest()


def build_service_start_code(appointment: dict) -> tuple[str, dict]:
    """Return a stable six-digit code and its hash-only persistence record.

    Stability lets an interrupted booking response be retried without storing
    the customer code in MySQL. The HMAC secret keeps the code unpredictable.
    """
    appointment_id = str((appointment or {}).get("id") or "").strip()
    staff_id = str((appointment or {}).get("staff_id") or "").strip()
    if not appointment_id or not staff_id:
        raise ValueError("Appointment and assigned staff are required for a service start code")
    digest = hmac.new(
        _mobile_secret().encode("utf-8"),
        f"service-start:{appointment_id}".encode("utf-8"),
        hashlib.sha256,
    ).digest()
    code = f"{int.from_bytes(digest[:8], 'big') % 1_000_000:06d}"
    now = _utcnow()
    expires_at = now + timedelta(hours=SERVICE_START_CODE_GRACE_HOURS)
    try:
        timezone_name = str(appointment.get("timezone") or "Asia/Singapore")
        scheduled_local = datetime.strptime(
            f"{appointment.get('date')} {str(appointment.get('time') or '')[:5]}",
            "%Y-%m-%d %H:%M",
        ).replace(tzinfo=ZoneInfo(timezone_name))
        expires_at = max(expires_at, scheduled_local.astimezone(timezone.utc) + timedelta(hours=SERVICE_START_CODE_GRACE_HOURS))
    except Exception:
        pass
    return code, {
        "staff_id": staff_id,
        "otp_hash": _otp_hash(appointment_id, code),
        "max_attempts": 5,
        "issued_at": _iso(now),
        "expires_at": _iso(expires_at),
    }


def _safe_staff(row: dict) -> dict:
    return {
        "id": row.get("staff_id") or row.get("id"),
        "name": row.get("name") or "",
        "email": row.get("email") or "",
        "whatsapp_number": row.get("whatsapp_number") or "",
        "role": row.get("role") or "service_staff",
        "timezone": row.get("timezone") or "Asia/Singapore",
        "username": row.get("username") or "",
    }


async def _valid_session(session_id: str, staff_id: str = "") -> Optional[dict]:
    session = await get_repository().get_staff_mobile_session(session_id)
    if not session or session.get("revoked_at"):
        return None
    if staff_id and not hmac.compare_digest(str(session.get("staff_id") or ""), staff_id):
        return None
    expires = _parse_datetime(session.get("expires_at"))
    if not expires or expires <= _utcnow():
        return None
    if not bool(session.get("staff_active")) or not bool(session.get("credential_active")):
        return None
    return session


async def authenticate_mobile_request(request: Request) -> Optional[dict]:
    header = request.headers.get("authorization") or ""
    if not header.lower().startswith("bearer "):
        return None
    try:
        claims = _decode_access_token(header.split(" ", 1)[1].strip())
    except HTTPException:
        return None
    if not claims:
        return None
    session = await _valid_session(str(claims["sid"]), str(claims["sub"]))
    if not session:
        return None
    return {**claims, "session": session}


async def require_mobile_staff(request: Request) -> dict:
    identity = getattr(request.state, "mobile_staff", None)
    if not identity:
        identity = await authenticate_mobile_request(request)
    if not identity:
        raise HTTPException(401, "Staff authentication required")
    return identity


async def authenticate_mobile_admin_request(request: Request) -> Optional[dict]:
    header = request.headers.get("authorization") or ""
    if not header.lower().startswith("bearer "):
        return None
    try:
        return _decode_admin_access_token(header.split(" ", 1)[1].strip())
    except HTTPException:
        return None


async def require_mobile_admin(request: Request) -> dict:
    identity = getattr(request.state, "mobile_admin", None)
    if not identity:
        identity = await authenticate_mobile_admin_request(request)
    if not identity:
        raise HTTPException(401, "Administrator authentication required")
    return identity


async def _issue_session(credential: dict, request: MobileLoginRequest) -> dict:
    now = _utcnow()
    refresh_token = secrets.token_urlsafe(48)
    row = {
        "id": str(uuid.uuid4()),
        "staff_id": credential["staff_id"],
        "refresh_token_hash": _token_hash(refresh_token),
        "device_id": request.device_id,
        "device_name": request.device_name,
        "platform": "android",
        "app_version": request.app_version,
        "last_seen_at": _iso(now),
        "expires_at": _iso(now + timedelta(days=REFRESH_TOKEN_DAYS)),
        "created_at": _iso(now),
    }
    session = await get_repository().create_staff_mobile_session(row)
    return {
        "role": "staff",
        "access_token": _sign_access_token(credential["staff_id"], session["id"]),
        "access_token_expires_in": ACCESS_TOKEN_SECONDS,
        "refresh_token": refresh_token,
        "refresh_token_expires_in": REFRESH_TOKEN_DAYS * 86400,
        "session_id": session["id"],
        "staff": _safe_staff(credential),
    }


@router.post("/api/mobile/auth/login")
async def mobile_login(req: MobileLoginRequest):
    repository = get_repository()
    credential = await repository.get_staff_mobile_credential(req.username.strip().lower())
    generic_error = HTTPException(401, "Invalid staff login")
    if not credential or not bool(credential.get("active")) or not bool(credential.get("staff_active")):
        raise generic_error
    locked_until = _parse_datetime(credential.get("locked_until"))
    if locked_until and locked_until > _utcnow():
        raise HTTPException(423, "Staff login is temporarily locked")
    if not _verify_pin(req.pin, str(credential.get("pin_hash") or "")):
        failures = int(credential.get("failed_attempts") or 0) + 1
        lock = _utcnow() + timedelta(minutes=PIN_LOCK_MINUTES) if failures >= MAX_PIN_FAILURES else None
        await repository.update_staff_mobile_login_state(credential["staff_id"], failures, lock)
        raise generic_error
    await repository.update_staff_mobile_login_state(credential["staff_id"], 0, None)
    return await _issue_session(credential, req)


@router.post("/api/mobile/admin/auth/login")
async def mobile_admin_login(req: AdminMobileLoginRequest):
    configured_username = os.getenv("ADMIN_USERNAME", "")
    configured_password = os.getenv("ADMIN_PASSWORD", "")
    if not configured_username or not configured_password:
        raise HTTPException(503, "Admin mobile authentication is not configured")
    if not (
        hmac.compare_digest(req.username, configured_username)
        and hmac.compare_digest(req.password, configured_password)
    ):
        raise HTTPException(401, "Invalid administrator login")
    return {
        "role": "admin",
        "access_token": _sign_admin_access_token(configured_username),
        "access_token_expires_in": ADMIN_ACCESS_TOKEN_SECONDS,
        "admin": {"username": configured_username, "name": "AirZen Administrator"},
    }


@router.get("/api/mobile/admin/me")
async def mobile_admin_me(identity: dict = Depends(require_mobile_admin)):
    return {"role": "admin", "admin": {"username": identity["sub"], "name": "AirZen Administrator"}}


@router.post("/api/mobile/admin/auth/logout")
async def mobile_admin_logout(_identity: dict = Depends(require_mobile_admin)):
    return {"success": True}


@router.post("/api/mobile/auth/refresh")
async def mobile_refresh(req: MobileRefreshRequest):
    session = await _valid_session(req.session_id)
    if not session or not hmac.compare_digest(str(session.get("refresh_token_hash") or ""), _token_hash(req.refresh_token)):
        raise HTTPException(401, "Invalid or expired staff session")
    new_refresh = secrets.token_urlsafe(48)
    expires_at = _utcnow() + timedelta(days=REFRESH_TOKEN_DAYS)
    if not await get_repository().rotate_staff_mobile_session(req.session_id, _token_hash(new_refresh), expires_at):
        raise HTTPException(401, "Invalid or expired staff session")
    return {
        "role": "staff",
        "access_token": _sign_access_token(session["staff_id"], req.session_id),
        "access_token_expires_in": ACCESS_TOKEN_SECONDS,
        "refresh_token": new_refresh,
        "refresh_token_expires_in": REFRESH_TOKEN_DAYS * 86400,
        "session_id": req.session_id,
        "staff": _safe_staff(session),
    }


@router.post("/api/mobile/auth/logout")
async def mobile_logout(identity: dict = Depends(require_mobile_staff)):
    await get_repository().revoke_staff_mobile_session(identity["sid"])
    return {"success": True}


@router.get("/api/mobile/me")
async def mobile_me(identity: dict = Depends(require_mobile_staff)):
    return {"staff": _safe_staff(identity["session"]), "session_id": identity["sid"]}


@router.get("/api/mobile/attendance/today")
async def mobile_attendance_today(identity: dict = Depends(require_mobile_staff)):
    work_date = _staff_today(identity)
    attendance = await get_repository().get_staff_attendance(identity["sub"], work_date)
    return {"work_date": work_date, "attendance": attendance}


@router.post("/api/mobile/attendance/check-in")
async def mobile_attendance_check_in(
    req: MobileAttendanceRequest, identity: dict = Depends(require_mobile_staff),
):
    now = _utcnow()
    work_date = _staff_today(identity)
    row = {
        "id": str(uuid.uuid4()),
        "staff_id": identity["sub"],
        "work_date": work_date,
        "check_in_at": _iso(now),
        "check_in_latitude": req.latitude,
        "check_in_longitude": req.longitude,
        "check_in_accuracy_meters": req.accuracy_meters,
        "check_in_session_id": identity["sid"],
        "notes": req.notes.strip(),
        "created_at": _iso(now),
        "updated_at": _iso(now),
    }
    attendance = await get_repository().check_in_staff_attendance(row)
    return {"success": True, "work_date": work_date, "attendance": attendance}


@router.post("/api/mobile/attendance/check-out")
async def mobile_attendance_check_out(
    req: MobileAttendanceRequest, identity: dict = Depends(require_mobile_staff),
):
    work_date = _staff_today(identity)
    try:
        attendance = await get_repository().check_out_staff_attendance(
            identity["sub"],
            work_date,
            {
                "check_out_at": _iso(_utcnow()),
                "check_out_latitude": req.latitude,
                "check_out_longitude": req.longitude,
                "check_out_accuracy_meters": req.accuracy_meters,
                "check_out_session_id": identity["sid"],
            },
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"success": True, "work_date": work_date, "attendance": attendance}


@router.get("/api/mobile/leave-requests")
async def mobile_leave_requests(identity: dict = Depends(require_mobile_staff)):
    rows = await get_repository().list_staff_leave_requests(staff_id=identity["sub"])
    return {"leave_requests": rows}


@router.post("/api/mobile/leave-requests")
async def mobile_create_leave_request(
    req: MobileLeaveRequest, identity: dict = Depends(require_mobile_staff),
):
    if req.end_date < req.start_date:
        raise HTTPException(422, "Leave end date cannot be before the start date")
    if req.start_date < date.fromisoformat(_staff_today(identity)):
        raise HTTPException(422, "Leave cannot start in the past")
    now = _utcnow()
    try:
        saved = await get_repository().create_staff_leave_request({
            "id": str(uuid.uuid4()),
            "staff_id": identity["sub"],
            "leave_type": req.leave_type,
            "start_date": req.start_date.isoformat(),
            "end_date": req.end_date.isoformat(),
            "reason": req.reason.strip(),
            "status": "pending",
            "admin_note": "",
            "created_at": _iso(now),
            "updated_at": _iso(now),
        })
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"success": True, "leave_request": saved}


@router.get("/api/mobile/appointments")
async def mobile_appointments(
    date_from: str = "", date_to: str = "", status: str = "",
    identity: dict = Depends(require_mobile_staff),
):
    statuses = [item.strip() for item in status.split(",") if item.strip()]
    rows = await get_repository().list_staff_mobile_appointments(identity["sub"], date_from, date_to, statuses)
    return {"appointments": rows}


@router.get("/api/mobile/appointments/{appointment_id}")
async def mobile_appointment(appointment_id: str, identity: dict = Depends(require_mobile_staff)):
    appointment = await get_repository().get_staff_mobile_appointment(identity["sub"], appointment_id)
    if not appointment:
        raise HTTPException(404, "Appointment not found")
    detail = await get_repository().get_staff_mobile_job_detail(appointment_id)
    return {"appointment": appointment, **detail}


def _event_row(
    appointment_id: str, identity: dict, event_type: str, idempotency_key: str,
    data: Optional[dict] = None, latitude: Optional[float] = None,
    longitude: Optional[float] = None, accuracy: Optional[float] = None,
) -> dict:
    return {
        "id": str(uuid.uuid4()),
        "appointment_id": appointment_id,
        "staff_id": identity["sub"],
        "session_id": identity["sid"],
        "event_type": event_type,
        "event_data": data or {},
        "latitude": latitude,
        "longitude": longitude,
        "accuracy_meters": accuracy,
        "idempotency_key": _idempotency_hash(appointment_id, identity["sub"], event_type, idempotency_key),
        "created_at": _iso(_utcnow()),
    }


@router.post("/api/mobile/appointments/{appointment_id}/en-route")
async def mobile_en_route(
    appointment_id: str, req: MobileJobActionRequest,
    identity: dict = Depends(require_mobile_staff),
):
    event = _event_row(
        appointment_id, identity, "en_route", req.idempotency_key,
        latitude=req.latitude, longitude=req.longitude, accuracy=req.accuracy_meters,
    )
    try:
        return await get_repository().begin_staff_mobile_job(appointment_id, identity["sub"], event)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/api/mobile/appointments/{appointment_id}/location")
async def mobile_location(
    appointment_id: str, req: MobileLocationRequest,
    identity: dict = Depends(require_mobile_staff),
):
    appointment = await get_repository().get_staff_mobile_appointment(identity["sub"], appointment_id)
    if not appointment:
        raise HTTPException(404, "Appointment not found")
    if str(appointment.get("status") or "").lower() in {"completed", "cancelled", "no_show", "rescheduled"}:
        raise HTTPException(409, "Location tracking is closed for this appointment")
    row = {
        "id": str(uuid.uuid4()),
        "appointment_id": appointment_id,
        "staff_id": identity["sub"],
        "session_id": identity["sid"],
        "latitude": req.latitude,
        "longitude": req.longitude,
        "accuracy_meters": req.accuracy_meters,
        "speed_mps": req.speed_mps,
        "heading_degrees": req.heading_degrees,
        "battery_percent": req.battery_percent,
        "is_mocked": req.is_mocked,
        "captured_at": _iso(req.captured_at),
        "idempotency_key": _idempotency_hash(appointment_id, identity["sub"], "location", req.idempotency_key),
        "created_at": _iso(_utcnow()),
    }
    await get_repository().record_staff_mobile_location(row)
    return {"success": True, "captured_at": row["captured_at"]}


async def _storage_setting(primary: str, fallback: str = "") -> str:
    value = await get_setting(primary, "")
    return str(value or os.getenv(primary, "") or (os.getenv(fallback, "") if fallback else "")).strip()


async def _storage_client():
    import boto3

    access_key = await _storage_setting("S3_ACCESS_KEY_ID", "AWS_ACCESS_KEY_ID")
    secret_key = await _storage_setting("S3_SECRET_ACCESS_KEY", "AWS_SECRET_ACCESS_KEY")
    bucket = await _storage_setting("S3_BUCKET", "AWS_BUCKET_NAME")
    endpoint = await _storage_setting("S3_ENDPOINT_URL", "S3_ENDPOINT")
    region = await _storage_setting("S3_REGION", "AWS_REGION") or "ap-northeast-1"
    if not access_key or not secret_key or not bucket:
        raise HTTPException(503, "Private evidence storage is not configured")
    client = boto3.client(
        "s3", aws_access_key_id=access_key, aws_secret_access_key=secret_key,
        endpoint_url=endpoint or None, region_name=region,
    )
    return client, bucket


async def _upload_evidence(
    upload: UploadFile, appointment_id: str, identity: dict, evidence_type: str,
    idempotency_key: str, latitude: Optional[float], longitude: Optional[float],
    accuracy: Optional[float], metadata: dict,
) -> tuple[dict, Any, str]:
    allowed = ALLOWED_SELFIE_TYPES if evidence_type == "arrival_selfie" else ALLOWED_PAYMENT_TYPES
    content_type = str(upload.content_type or "").lower()
    if content_type not in allowed:
        raise HTTPException(415, "Unsupported evidence file type")
    content = await upload.read(MAX_EVIDENCE_BYTES + 1)
    if not content or len(content) > MAX_EVIDENCE_BYTES:
        raise HTTPException(413, "Evidence file must be between 1 byte and 10 MB")
    extension = mimetypes.guess_extension(content_type) or PurePosixPath(upload.filename or "").suffix or ".bin"
    storage_key = f"staff-evidence/{appointment_id}/{evidence_type}/{uuid.uuid4()}{extension}"
    client, bucket = await _storage_client()
    await asyncio.to_thread(
        client.put_object,
        Bucket=bucket,
        Key=storage_key,
        Body=content,
        ContentType=content_type,
        Metadata={"appointment-id": appointment_id, "staff-id": identity["sub"], "evidence-type": evidence_type},
        ServerSideEncryption="AES256",
    )
    now = _iso(_utcnow())
    row = {
        "id": str(uuid.uuid4()),
        "appointment_id": appointment_id,
        "staff_id": identity["sub"],
        "session_id": identity["sid"],
        "evidence_type": evidence_type,
        "storage_key": storage_key,
        "sha256": hashlib.sha256(content).hexdigest(),
        "content_type": content_type,
        "file_size": len(content),
        "latitude": latitude,
        "longitude": longitude,
        "accuracy_meters": accuracy,
        "captured_at": now,
        "metadata_json": metadata,
        "idempotency_key": _idempotency_hash(appointment_id, identity["sub"], evidence_type, idempotency_key),
        "created_at": now,
    }
    return row, client, bucket


async def _delete_evidence_object(client: Any, bucket: str, key: str) -> None:
    try:
        await asyncio.to_thread(client.delete_object, Bucket=bucket, Key=key)
    except Exception:
        pass


@router.post("/api/mobile/appointments/{appointment_id}/arrive")
async def mobile_arrive(
    appointment_id: str,
    selfie: UploadFile = File(...),
    latitude: float = Form(..., ge=-90, le=90),
    longitude: float = Form(..., ge=-180, le=180),
    accuracy_meters: Optional[float] = Form(default=None, ge=0, le=10000),
    idempotency_key: str = Form(..., min_length=8, max_length=255),
    identity: dict = Depends(require_mobile_staff),
):
    appointment = await get_repository().get_staff_mobile_appointment(identity["sub"], appointment_id)
    if not appointment:
        raise HTTPException(404, "Appointment not found")
    evidence, storage_client, bucket = await _upload_evidence(
        selfie, appointment_id, identity, "arrival_selfie", idempotency_key,
        latitude, longitude, accuracy_meters, {"capture": "front_camera", "purpose": "arrival_confirmation"},
    )
    event = _event_row(
        appointment_id, identity, "arrived", idempotency_key,
        {"arrival_selfie_id": evidence["id"], "service_start_code_required": True},
        latitude, longitude, accuracy_meters,
    )
    try:
        result = await get_repository().mark_staff_mobile_arrived(
            appointment_id, identity["sub"], evidence, event,
        )
        if result.get("already_applied"):
            await _delete_evidence_object(storage_client, bucket, evidence["storage_key"])
        return result
    except ValueError as exc:
        await _delete_evidence_object(storage_client, bucket, evidence["storage_key"])
        raise HTTPException(409, str(exc)) from exc
    except Exception:
        await _delete_evidence_object(storage_client, bucket, evidence["storage_key"])
        raise


@router.post("/api/mobile/appointments/{appointment_id}/verify-start-code")
@router.post("/api/mobile/appointments/{appointment_id}/verify-otp", include_in_schema=False)
async def mobile_verify_start_code(
    appointment_id: str, req: MobileStartCodeRequest,
    identity: dict = Depends(require_mobile_staff),
):
    event = _event_row(appointment_id, identity, "job_started", req.idempotency_key, {"service_start_code_verified": True})
    try:
        result = await get_repository().verify_staff_mobile_start_code(
            appointment_id, identity["sub"], identity["sid"], _otp_hash(appointment_id, req.code), event,
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    if not result.get("verified") and not result.get("already_verified"):
        raise HTTPException(401, f"Incorrect service start code. {result.get('attempts_remaining', 0)} attempts remaining")
    return result


@router.post("/api/mobile/appointments/{appointment_id}/complete")
async def mobile_complete(
    appointment_id: str,
    payment_proof: UploadFile = File(...),
    payment_amount: str = Form(..., min_length=1, max_length=32),
    payment_method: str = Form(..., min_length=2, max_length=64),
    payment_reference: str = Form(default="", max_length=191),
    notes: str = Form(default="", max_length=2000),
    latitude: Optional[float] = Form(default=None, ge=-90, le=90),
    longitude: Optional[float] = Form(default=None, ge=-180, le=180),
    accuracy_meters: Optional[float] = Form(default=None, ge=0, le=10000),
    idempotency_key: str = Form(..., min_length=8, max_length=255),
    identity: dict = Depends(require_mobile_staff),
):
    appointment = await get_repository().get_staff_mobile_appointment(identity["sub"], appointment_id)
    if not appointment:
        raise HTTPException(404, "Appointment not found")
    try:
        amount = float(payment_amount)
        if amount < 0 or amount > 10_000_000:
            raise ValueError
    except ValueError as exc:
        raise HTTPException(422, "Invalid payment amount") from exc
    metadata = {
        "payment_amount": f"{amount:.2f}",
        "payment_method": payment_method.strip().lower(),
        "payment_reference": payment_reference.strip(),
        "notes": notes.strip(),
    }
    evidence, storage_client, bucket = await _upload_evidence(
        payment_proof, appointment_id, identity, "payment_proof", idempotency_key,
        latitude, longitude, accuracy_meters, metadata,
    )
    appointment_datetime = f"{appointment.get('date') or ''} {str(appointment.get('time') or '')[:5]}".strip()
    notification = {
        "recipient_phone": appointment.get("phone") or "",
        "notification_type": "appointment_customer_completion",
        "template_purpose": "service_completion_template",
        "conversation_id": appointment.get("conversation_id") or "",
        "contact_id": appointment.get("contact_id") or "",
        "payload": {
            **appointment,
            "customer_name": appointment.get("name") or "AirZen Customer",
            "appointment_datetime": appointment_datetime,
            "payment_method": metadata["payment_method"],
        },
    }
    event = _event_row(
        appointment_id, identity, "completed", idempotency_key,
        {"payment_proof_id": evidence["id"], **metadata},
        latitude, longitude, accuracy_meters,
    )
    try:
        result = await get_repository().complete_staff_mobile_job(
            appointment_id, identity["sub"], evidence, notification, event,
        )
        if result.get("already_applied"):
            await _delete_evidence_object(storage_client, bucket, evidence["storage_key"])
        return result
    except ValueError as exc:
        await _delete_evidence_object(storage_client, bucket, evidence["storage_key"])
        raise HTTPException(409, str(exc)) from exc
    except Exception:
        await _delete_evidence_object(storage_client, bucket, evidence["storage_key"])
        raise


@router.post("/api/appointment-staff/{staff_id}/mobile-access")
async def provision_staff_mobile_access(staff_id: str, req: MobilePinRequest):
    staff = await get_repository().get_appointment_staff(staff_id)
    if not staff:
        raise HTTPException(404, "Staff member not found")
    try:
        saved = await get_repository().upsert_staff_mobile_credential(
            staff_id, req.username.strip().lower(), _hash_pin(req.pin), req.active,
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"success": True, "staff": _safe_staff(saved), "mobile_access_active": bool(saved.get("active"))}


@router.get("/api/appointment-staff/{staff_id}/mobile-access")
async def get_staff_mobile_access(staff_id: str):
    staff = await get_repository().get_appointment_staff(staff_id)
    if not staff:
        raise HTTPException(404, "Staff member not found")
    credential = await get_repository().get_staff_mobile_credential(staff_id)
    return {
        "configured": bool(credential),
        "username": (credential or {}).get("username") or "",
        "active": bool((credential or {}).get("active")),
    }


@router.post("/api/appointment-staff/{staff_id}/mobile-access/disable")
async def disable_staff_mobile_access(staff_id: str):
    staff = await get_repository().get_appointment_staff(staff_id)
    if not staff:
        raise HTTPException(404, "Staff member not found")
    disabled = await get_repository().disable_staff_mobile_credential(staff_id)
    return {"success": True, "disabled": disabled}


@router.get("/api/appointments/{appointment_id}/field-service")
async def admin_field_service_detail(appointment_id: str):
    appointment = await get_appointment_by_id(appointment_id)
    if not appointment:
        raise HTTPException(404, "Appointment not found")
    detail = await get_repository().get_staff_mobile_job_detail(appointment_id)
    try:
        client, bucket = await _storage_client()
        for evidence in detail.get("evidence") or []:
            evidence["view_url"] = await asyncio.to_thread(
                client.generate_presigned_url,
                "get_object",
                Params={"Bucket": bucket, "Key": evidence.get("storage_key")},
                ExpiresIn=300,
            )
    except HTTPException:
        for evidence in detail.get("evidence") or []:
            evidence["view_url"] = ""
    return {"appointment": appointment, **detail}


def _operations_job_state(appointment: dict) -> str:
    status = str(appointment.get("status") or "").strip().lower()
    if status == "completed":
        return "completed"
    if status == "in_progress":
        return "in_progress"
    return str(appointment.get("mobile_job_state") or "booked").strip().lower()


@router.get("/api/staff-operations/overview")
async def admin_staff_operations_overview(work_date: str = ""):
    """Return one dispatch-ready view without browser-side N+1 API requests."""
    selected = work_date or _utcnow().astimezone(ZoneInfo("Asia/Singapore")).date().isoformat()
    try:
        selected_date = date.fromisoformat(selected)
    except ValueError as exc:
        raise HTTPException(422, "Operations date must use YYYY-MM-DD") from exc

    repository = get_repository()
    try:
        staff, attendance, leave_requests, appointments = await asyncio.gather(
            repository.list_appointment_staff(include_inactive=True),
            repository.list_staff_attendance(selected, selected),
            repository.list_staff_leave_requests(),
            repository.list_appointments(date=selected, ascending=True),
        )
    except AttributeError as exc:
        raise HTTPException(409, "Staff operations requires the MySQL staff-mobile backend") from exc

    terminal_statuses = {"completed", "cancelled", "no_show", "rescheduled"}
    active_jobs = [row for row in appointments if str(row.get("status") or "").lower() not in terminal_statuses]

    async def with_live_detail(appointment: dict) -> dict:
        detail = await repository.get_staff_mobile_job_detail(str(appointment.get("id") or ""))
        events = detail.get("events") or []
        latest_location = detail.get("latest_location")
        captured_at = _parse_datetime((latest_location or {}).get("captured_at"))
        location_age_seconds = max(0, int((_utcnow() - captured_at).total_seconds())) if captured_at else None
        moving_state = _operations_job_state(appointment) in {"en_route", "arrived", "in_progress", "job_started"}
        tracking_active = bool(latest_location) and moving_state and location_age_seconds is not None and location_age_seconds <= 180
        return {
            **appointment,
            "job_state": _operations_job_state(appointment),
            "latest_location": latest_location,
            "latest_event": events[-1] if events else None,
            "event_count": len(events),
            "evidence_count": len(detail.get("evidence") or []),
            "tracking_active": tracking_active,
            "location_age_seconds": location_age_seconds,
            "location_state": "live" if tracking_active else "stale" if latest_location else "waiting",
        }

    active_jobs_with_detail = list(await asyncio.gather(*(with_live_detail(row) for row in active_jobs)))
    attendance_by_staff = {str(row.get("staff_id") or ""): row for row in attendance}
    jobs_by_staff: dict[str, list[dict]] = {}
    for job in active_jobs_with_detail:
        jobs_by_staff.setdefault(str(job.get("staff_id") or ""), []).append(job)

    approved_leave_by_staff: dict[str, dict] = {}
    for row in leave_requests:
        try:
            covers_date = date.fromisoformat(str(row.get("start_date"))) <= selected_date <= date.fromisoformat(str(row.get("end_date")))
        except (TypeError, ValueError):
            covers_date = False
        if covers_date and str(row.get("status") or "").lower() == "approved":
            approved_leave_by_staff[str(row.get("staff_id") or "")] = row

    staff_activity = []
    for member in staff:
        staff_id = str(member.get("id") or "")
        record = attendance_by_staff.get(staff_id)
        member_jobs = jobs_by_staff.get(staff_id, [])
        leave = approved_leave_by_staff.get(staff_id)
        attendance_state = "on_leave" if leave else "absent"
        if record:
            attendance_state = "checked_out" if record.get("check_out_at") else "checked_in"
        attendance_location = None
        if record:
            prefix = "check_out" if record.get("check_out_at") else "check_in"
            if record.get(f"{prefix}_latitude") is not None and record.get(f"{prefix}_longitude") is not None:
                attendance_location = {
                    "latitude": record.get(f"{prefix}_latitude"),
                    "longitude": record.get(f"{prefix}_longitude"),
                    "accuracy_meters": record.get(f"{prefix}_accuracy_meters"),
                    "captured_at": record.get(f"{prefix}_at"),
                }
        staff_activity.append({
            "id": staff_id,
            "name": member.get("name") or "Staff member",
            "role": member.get("role") or "service_staff",
            "active": bool(member.get("active")),
            "timezone": member.get("timezone") or "Asia/Singapore",
            "attendance_state": attendance_state,
            "attendance": record,
            "leave": leave,
            "active_jobs": member_jobs,
            "active_job_count": len(member_jobs),
            "latest_location": next((job.get("latest_location") for job in member_jobs if job.get("latest_location")), attendance_location),
        })

    active_staff = [row for row in staff_activity if row["active"]]
    pending_leave = [row for row in leave_requests if str(row.get("status") or "").lower() == "pending"]
    summary = {
        "staff_total": len(active_staff),
        "checked_in": sum(row["attendance_state"] == "checked_in" for row in active_staff),
        "checked_out": sum(row["attendance_state"] == "checked_out" for row in active_staff),
        "absent": sum(row["attendance_state"] == "absent" for row in active_staff),
        "on_leave": sum(row["attendance_state"] == "on_leave" for row in active_staff),
        "active_jobs": len(active_jobs_with_detail),
        "tracking_jobs": sum(bool(row.get("tracking_active")) for row in active_jobs_with_detail),
        "pending_leave": len(pending_leave),
        "pending_jobs": sum(
            str(row.get("status") or "").lower() in {"booked", "confirmed"}
            and _operations_job_state(row) == "booked"
            for row in appointments
        ),
        "completed_jobs": sum(str(row.get("status") or "").lower() == "completed" for row in appointments),
        "cancelled_jobs": sum(str(row.get("status") or "").lower() == "cancelled" for row in appointments),
        "appointments_total": len(appointments),
    }
    return {
        "work_date": selected,
        "generated_at": _iso(_utcnow()),
        "summary": summary,
        "staff": staff_activity,
        "appointments": appointments,
        "active_jobs": active_jobs_with_detail,
        "pending_leave": pending_leave,
    }


@router.get("/api/mobile/admin/overview")
async def mobile_admin_overview(
    work_date: str = "", _identity: dict = Depends(require_mobile_admin),
):
    return await admin_staff_operations_overview(work_date)


@router.post("/api/mobile/admin/leave-requests/{request_id}/decision")
async def mobile_admin_decide_leave(
    request_id: str, req: AdminLeaveDecisionRequest,
    _identity: dict = Depends(require_mobile_admin),
):
    return await admin_decide_staff_leave(request_id, req)


@router.get("/api/staff-attendance")
async def admin_staff_attendance(date_from: str = "", date_to: str = "", staff_id: str = ""):
    singapore_today = _utcnow().astimezone(ZoneInfo("Asia/Singapore")).date()
    start = date_from or singapore_today.isoformat()
    end = date_to or start
    try:
        start_date = date.fromisoformat(start)
        end_date = date.fromisoformat(end)
    except ValueError as exc:
        raise HTTPException(422, "Attendance dates must use YYYY-MM-DD") from exc
    if end_date < start_date or (end_date - start_date).days > 366:
        raise HTTPException(422, "Attendance date range must be between 0 and 366 days")
    rows = await get_repository().list_staff_attendance(start, end, staff_id)
    return {"date_from": start, "date_to": end, "attendance": rows}


class QuickAttendanceRequest(BaseModel):
    staff_id: str
    action: str = "check_in"
    work_date: str = ""
    notes: str = ""


class CreateLeaveRequest(BaseModel):
    staff_id: str
    leave_type: str = "medical"
    start_date: str
    end_date: str
    reason: str = ""


@router.post("/api/staff-attendance/quick-record")
async def admin_quick_record_attendance(req: QuickAttendanceRequest):
    repo = get_repository()
    now = _utcnow()
    work_date = req.work_date or now.strftime("%Y-%m-%d")
    if req.action == "check_in":
        row = {
            "id": str(uuid.uuid4()),
            "staff_id": req.staff_id,
            "work_date": work_date,
            "check_in_at": _iso(now),
            "check_in_latitude": None,
            "check_in_longitude": None,
            "check_in_accuracy_meters": None,
            "check_in_session_id": None,
            "notes": req.notes.strip() or "Quick check-in recorded via clinic operations desk",
            "created_at": _iso(now),
            "updated_at": _iso(now),
        }
        attendance = await repo.check_in_staff_attendance(row)
        return {"success": True, "action": "check_in", "attendance": attendance}
    else:
        try:
            attendance = await repo.check_out_staff_attendance(
                req.staff_id,
                work_date,
                {
                    "check_out_at": _iso(now),
                    "check_out_latitude": None,
                    "check_out_longitude": None,
                    "check_out_accuracy_meters": None,
                    "check_out_session_id": None,
                },
            )
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"success": True, "action": "check_out", "attendance": attendance}


@router.get("/api/staff-leave-requests")
async def admin_staff_leave_requests(status: str = "", staff_id: str = ""):
    clean_status = status.strip().lower()
    if clean_status and clean_status not in {"pending", "approved", "declined", "cancelled"}:
        raise HTTPException(422, "Invalid leave status")
    rows = await get_repository().list_staff_leave_requests(staff_id=staff_id, status=clean_status)
    return {"leave_requests": rows}


@router.post("/api/staff-leave-requests")
async def admin_create_staff_leave_request(req: CreateLeaveRequest):
    repo = get_repository()
    now = _utcnow()
    raw_type = (req.leave_type or "medical").lower().strip()
    type_map = {
        "casual": "other",
        "sick": "medical",
        "vacation": "annual",
    }
    leave_type = type_map.get(raw_type, raw_type)
    if leave_type not in {"annual", "medical", "emergency", "unpaid", "other"}:
        leave_type = "other"

    row = {
        "id": str(uuid.uuid4()),
        "staff_id": req.staff_id,
        "leave_type": leave_type,
        "start_date": req.start_date,
        "end_date": req.end_date,
        "reason": req.reason.strip(),
        "status": "pending",
        "admin_note": "",
        "created_at": _iso(now),
        "updated_at": _iso(now),
    }
    saved = await repo.create_staff_leave_request(row)
    return {"success": True, "leave_request": saved}


@router.post("/api/staff-leave-requests/{request_id}/decision")
async def admin_decide_staff_leave(request_id: str, req: AdminLeaveDecisionRequest):
    try:
        saved = await get_repository().decide_staff_leave_request(
            request_id, req.decision, req.admin_note.strip(), "admin",
        )
    except ValueError as exc:
        message = str(exc)
        raise HTTPException(404 if message == "Leave request not found" else 409, message) from exc
    return {"success": True, "leave_request": saved}


@router.get("/api/staff-mobile/readiness")
async def staff_mobile_readiness():
    health = await get_repository().backend_health_check()
    schema = await get_repository().staff_mobile_schema_health()
    result = {
        "backend": health.get("backend"),
        "backend_ok": bool(health.get("ok")),
        "staff_mobile_schema_ok": bool(schema.get("ok")),
        "staff_mobile_schema": schema,
        "token_secret_configured": len((os.getenv("STAFF_MOBILE_TOKEN_SECRET") or "").strip()) >= 32,
        "service_start_code_enabled": bool(schema.get("ok")) and len((os.getenv("STAFF_MOBILE_TOKEN_SECRET") or "").strip()) >= 32,
        "service_completion_template_configured": bool(await get_setting("service_completion_template", "")),
        "evidence_storage_configured": bool(
            (await _storage_setting("S3_ACCESS_KEY_ID", "AWS_ACCESS_KEY_ID"))
            and (await _storage_setting("S3_SECRET_ACCESS_KEY", "AWS_SECRET_ACCESS_KEY"))
            and (await _storage_setting("S3_BUCKET", "AWS_BUCKET_NAME"))
        ),
        "required_schema": "staff_mobile_schema.sql",
    }
    result["ready"] = all(
        result[key]
        for key in (
            "backend_ok",
            "staff_mobile_schema_ok",
            "token_secret_configured",
            "service_start_code_enabled",
            "service_completion_template_configured",
            "evidence_storage_configured",
        )
    )
    return result
