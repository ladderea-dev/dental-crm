import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException, UploadFile
from starlette.requests import Request

import staff_mobile


class FakeMobileRepository:
    def __init__(self, *, credential=None, appointment=None):
        self.credential = credential
        self.appointment = appointment
        self.login_updates = []
        self.created_sessions = []
        self.arrival_args = None
        self.completion_args = None
        self.mobile_access_args = None
        self.mobile_access_disabled = False
        self.attendance = None
        self.leave_requests = []
        self.leave_decision = None
        self.staff = []
        self.operation_appointments = []
        self.job_details = {}

    async def get_staff_mobile_credential(self, identifier):
        return self.credential

    async def update_staff_mobile_login_state(self, staff_id, failed_attempts, locked_until=None):
        self.login_updates.append((staff_id, failed_attempts, locked_until))
        return True

    async def create_staff_mobile_session(self, row):
        self.created_sessions.append(row)
        return row

    async def get_staff_mobile_appointment(self, staff_id, appointment_id):
        if self.appointment and self.appointment["staff_id"] == staff_id and self.appointment["id"] == appointment_id:
            return dict(self.appointment)
        return None

    async def mark_staff_mobile_arrived(self, *args):
        self.arrival_args = args
        return {"appointment_id": args[0], "job_state": "arrived", "start_code_status": "pending"}

    async def complete_staff_mobile_job(self, *args):
        self.completion_args = args
        return {"appointment_id": args[0], "job_state": "completed", "crm_closed": True}

    async def get_appointment_staff(self, staff_id):
        return {"id": staff_id, "name": "Technician One", "active": True}

    async def upsert_staff_mobile_credential(self, staff_id, username, pin_hash, active=True):
        self.mobile_access_args = (staff_id, username, pin_hash, active)
        return {
            "staff_id": staff_id, "username": username, "pin_hash": pin_hash, "active": active,
            "name": "Technician One", "staff_active": True,
        }

    async def disable_staff_mobile_credential(self, _staff_id):
        self.mobile_access_disabled = True
        return True

    async def get_staff_attendance(self, staff_id, work_date):
        if self.attendance and self.attendance["staff_id"] == staff_id and self.attendance["work_date"] == work_date:
            return dict(self.attendance)
        return None

    async def check_in_staff_attendance(self, row):
        if not self.attendance:
            self.attendance = dict(row)
            self.attendance["check_out_at"] = None
        return dict(self.attendance)

    async def check_out_staff_attendance(self, staff_id, work_date, row):
        if not self.attendance:
            raise ValueError("Check in before checking out")
        self.attendance.update(row)
        return dict(self.attendance)

    async def create_staff_leave_request(self, row):
        self.leave_requests.append(dict(row))
        return dict(row)

    async def list_staff_leave_requests(self, staff_id="", status=""):
        return [row for row in self.leave_requests if (not staff_id or row["staff_id"] == staff_id) and (not status or row["status"] == status)]

    async def decide_staff_leave_request(self, request_id, status, admin_note, reviewed_by):
        self.leave_decision = (request_id, status, admin_note, reviewed_by)
        return {"id": request_id, "status": status, "admin_note": admin_note, "reviewed_by": reviewed_by}

    async def list_appointment_staff(self, include_inactive=False):
        return [row for row in self.staff if include_inactive or row.get("active")]

    async def list_staff_attendance(self, date_from, date_to, staff_id=""):
        rows = [self.attendance] if self.attendance else []
        return [row for row in rows if date_from <= row["work_date"] <= date_to and (not staff_id or row["staff_id"] == staff_id)]

    async def list_appointments(self, date="", statuses=None, staff_id="", date_from="", date_to="", ascending=False):
        return [row for row in self.operation_appointments if (not date or row.get("date") == date)]

    async def get_staff_mobile_job_detail(self, appointment_id):
        return self.job_details.get(appointment_id, {"latest_location": None, "events": [], "evidence": []})


@pytest.fixture(autouse=True)
def mobile_secret(monkeypatch):
    monkeypatch.setenv("STAFF_MOBILE_TOKEN_SECRET", "test-mobile-secret-that-is-at-least-32-characters")


def test_staff_pin_hash_is_salted_and_verifiable():
    first = staff_mobile._hash_pin("123456")
    second = staff_mobile._hash_pin("123456")

    assert first != second
    assert staff_mobile._verify_pin("123456", first)
    assert not staff_mobile._verify_pin("654321", first)
    assert "123456" not in first


def test_staff_access_token_detects_tampering():
    token = staff_mobile._sign_access_token("staff-1", "session-1")
    claims = staff_mobile._decode_access_token(token)

    assert claims["sub"] == "staff-1"
    assert claims["sid"] == "session-1"
    assert staff_mobile._decode_access_token(token[:-1] + ("0" if token[-1] != "0" else "1")) is None


def test_admin_mobile_token_is_separate_short_lived_and_bound_to_current_credentials(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET", "test-admin-session-secret-that-is-at-least-32-characters")
    monkeypatch.setenv("ADMIN_USERNAME", "airzen.admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "first-test-password")

    token = staff_mobile._sign_admin_access_token("airzen.admin")
    claims = staff_mobile._decode_admin_access_token(token)

    assert claims["sub"] == "airzen.admin"
    assert claims["role"] == "admin"
    assert claims["aud"] == "airzen-admin-mobile"
    assert claims["exp"] - claims["iat"] == staff_mobile.ADMIN_ACCESS_TOKEN_SECONDS
    assert staff_mobile._decode_access_token(token) is None

    monkeypatch.setenv("ADMIN_PASSWORD", "changed-test-password")
    assert staff_mobile._decode_admin_access_token(token) is None


def test_admin_mobile_login_never_returns_or_stores_password(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET", "test-admin-session-secret-that-is-at-least-32-characters")
    monkeypatch.setenv("ADMIN_USERNAME", "airzen.admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "correct-test-password")
    request = staff_mobile.AdminMobileLoginRequest(
        username="airzen.admin", password="correct-test-password",
        device_id="admin-device-1", device_name="Test Android", app_version="1.0.0",
    )

    result = asyncio.run(staff_mobile.mobile_admin_login(request))

    assert result["role"] == "admin"
    assert result["admin"]["username"] == "airzen.admin"
    assert result["access_token"]
    assert "correct-test-password" not in json.dumps(result)

    with pytest.raises(HTTPException) as error:
        asyncio.run(staff_mobile.mobile_admin_login(request.model_copy(update={"password": "wrong"})))
    assert error.value.status_code == 401


def test_staff_bearer_token_cannot_authenticate_as_mobile_admin(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET", "test-admin-session-secret-that-is-at-least-32-characters")
    monkeypatch.setenv("ADMIN_USERNAME", "airzen.admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "correct-test-password")
    staff_token = staff_mobile._sign_access_token("staff-1", "session-1")
    request = Request({
        "type": "http", "method": "GET", "path": "/api/mobile/admin/overview",
        "headers": [(b"authorization", f"Bearer {staff_token}".encode("ascii"))],
    })

    assert asyncio.run(staff_mobile.authenticate_mobile_admin_request(request)) is None


def test_mobile_login_creates_revocable_device_session(monkeypatch):
    pin_hash = staff_mobile._hash_pin("123456")
    credential = {
        "staff_id": "staff-1",
        "pin_hash": pin_hash,
        "active": True,
        "staff_active": True,
        "failed_attempts": 0,
        "username": "tech.one",
        "name": "Technician One",
        "email": "staff@example.com",
        "whatsapp_number": "+6590000000",
        "role": "technician",
        "timezone": "Asia/Singapore",
    }
    repo = FakeMobileRepository(credential=credential)
    monkeypatch.setattr(staff_mobile, "get_repository", lambda: repo)

    result = asyncio.run(staff_mobile.mobile_login(staff_mobile.MobileLoginRequest(
        username="tech.one", pin="123456", device_id="android-device-1",
        device_name="Test Android", app_version="1.0.0",
    )))

    assert result["staff"]["id"] == "staff-1"
    assert result["refresh_token"]
    assert result["refresh_token"] not in repo.created_sessions[0]["refresh_token_hash"]
    assert repo.created_sessions[0]["device_id"] == "android-device-1"
    assert repo.login_updates == [("staff-1", 0, None)]


def test_mobile_login_locks_after_fifth_bad_pin(monkeypatch):
    credential = {
        "staff_id": "staff-1", "username": "tech.one", "pin_hash": staff_mobile._hash_pin("123456"),
        "active": True, "staff_active": True, "failed_attempts": 4,
    }
    repo = FakeMobileRepository(credential=credential)
    monkeypatch.setattr(staff_mobile, "get_repository", lambda: repo)

    with pytest.raises(HTTPException) as error:
        asyncio.run(staff_mobile.mobile_login(staff_mobile.MobileLoginRequest(
            username="tech.one", pin="999999", device_id="android-device-1",
        )))

    assert error.value.status_code == 401
    assert repo.login_updates[0][0:2] == ("staff-1", 5)
    assert repo.login_updates[0][2] > datetime.now(timezone.utc)


def test_booking_service_start_code_is_stable_hashed_and_valid_through_appointment():
    appointment = {
        "id": "appointment-1", "staff_id": "staff-1", "date": "2030-08-28",
        "time": "16:00", "timezone": "Asia/Singapore",
    }

    first_code, first_record = staff_mobile.build_service_start_code(appointment)
    second_code, second_record = staff_mobile.build_service_start_code(appointment)

    assert first_code == second_code
    assert first_code.isdigit() and len(first_code) == 6
    assert first_code not in first_record["otp_hash"]
    assert first_record["otp_hash"] == second_record["otp_hash"]
    assert first_record["staff_id"] == "staff-1"
    assert datetime.fromisoformat(first_record["expires_at"]) > datetime(2030, 8, 28, 16, 0, tzinfo=timezone(timedelta(hours=8)))


def test_arrival_persists_only_selfie_and_event_without_sending_a_second_code(monkeypatch):
    appointment = {
        "id": "appointment-1", "staff_id": "staff-1", "status": "booked",
        "phone": "+6591111111", "name": "Test Customer", "date": "2026-08-28",
        "time": "16:00", "service": "Air-conditioning service",
    }
    repo = FakeMobileRepository(appointment=appointment)
    monkeypatch.setattr(staff_mobile, "get_repository", lambda: repo)
    evidence = {
        "id": "evidence-1", "storage_key": "staff-evidence/appointment-1/arrival_selfie/test.jpg",
    }
    monkeypatch.setattr(staff_mobile, "_upload_evidence", AsyncMock(return_value=(evidence, object(), "bucket")))

    result = asyncio.run(staff_mobile.mobile_arrive(
        "appointment-1", UploadFile(filename="selfie.jpg", file=None), 1.3001, 103.8001,
        8.0, "arrival-request-1", {"sub": "staff-1", "sid": "session-1"},
    ))

    assert result["job_state"] == "arrived"
    _, _, stored_evidence, event = repo.arrival_args
    assert stored_evidence["id"] == "evidence-1"
    assert event["event_type"] == "arrived"
    assert event["event_data"]["service_start_code_required"] is True


def test_completion_failure_removes_only_newly_uploaded_object(monkeypatch):
    appointment = {
        "id": "appointment-1", "staff_id": "staff-1", "status": "in_progress",
        "phone": "+6591111111", "name": "Test Customer", "date": "2026-08-28",
        "time": "16:00", "service": "Air-conditioning service",
    }
    repo = FakeMobileRepository(appointment=appointment)

    async def fail_complete(*_args):
        raise ValueError("Only an in-progress appointment can be completed")

    repo.complete_staff_mobile_job = fail_complete
    delete_mock = AsyncMock()
    evidence = {
        "id": "evidence-2", "storage_key": "staff-evidence/appointment-1/payment_proof/test.jpg",
    }
    monkeypatch.setattr(staff_mobile, "get_repository", lambda: repo)
    monkeypatch.setattr(staff_mobile, "_upload_evidence", AsyncMock(return_value=(evidence, "client", "bucket")))
    monkeypatch.setattr(staff_mobile, "_delete_evidence_object", delete_mock)

    with pytest.raises(HTTPException) as error:
        asyncio.run(staff_mobile.mobile_complete(
            "appointment-1", UploadFile(filename="proof.jpg", file=None), "120.00", "PayNow",
            "PAYNOW-REF", "Completed successfully", 1.3001, 103.8001, 7.0,
            "completion-request-1", {"sub": "staff-1", "sid": "session-1"},
        ))

    assert error.value.status_code == 409
    delete_mock.assert_awaited_once_with("client", "bucket", evidence["storage_key"])


def test_arrival_retry_does_not_leave_a_second_uploaded_object(monkeypatch):
    appointment = {
        "id": "appointment-1", "staff_id": "staff-1", "status": "booked",
        "phone": "+6591111111", "name": "Test Customer", "date": "2026-08-28",
        "time": "16:00", "service": "Air-conditioning service",
    }
    repo = FakeMobileRepository(appointment=appointment)

    async def already_arrived(*args):
        repo.arrival_args = args
        return {
            "appointment_id": args[0], "job_state": "arrived",
            "start_code_status": "pending", "already_applied": True,
        }

    repo.mark_staff_mobile_arrived = already_arrived
    evidence = {
        "id": "retry-evidence", "storage_key": "staff-evidence/appointment-1/arrival_selfie/retry.jpg",
    }
    delete_mock = AsyncMock()
    monkeypatch.setattr(staff_mobile, "get_repository", lambda: repo)
    monkeypatch.setattr(staff_mobile, "_upload_evidence", AsyncMock(return_value=(evidence, "client", "bucket")))
    monkeypatch.setattr(staff_mobile, "_delete_evidence_object", delete_mock)

    result = asyncio.run(staff_mobile.mobile_arrive(
        "appointment-1", UploadFile(filename="selfie.jpg", file=None), 1.3001, 103.8001,
        8.0, "same-arrival-request", {"sub": "staff-1", "sid": "session-1"},
    ))

    assert result["already_applied"] is True
    delete_mock.assert_awaited_once_with("client", "bucket", evidence["storage_key"])


def test_completion_retry_does_not_leave_a_second_uploaded_object(monkeypatch):
    appointment = {
        "id": "appointment-1", "staff_id": "staff-1", "status": "completed",
        "phone": "+6591111111", "name": "Test Customer", "date": "2026-08-28",
        "time": "16:00", "service": "Air-conditioning service",
    }
    repo = FakeMobileRepository(appointment=appointment)

    async def already_completed(*args):
        repo.completion_args = args
        return {
            "appointment_id": args[0], "job_state": "completed",
            "crm_closed": True, "already_applied": True,
        }

    repo.complete_staff_mobile_job = already_completed
    evidence = {
        "id": "retry-evidence", "storage_key": "staff-evidence/appointment-1/payment_proof/retry.jpg",
    }
    delete_mock = AsyncMock()
    monkeypatch.setattr(staff_mobile, "get_repository", lambda: repo)
    monkeypatch.setattr(staff_mobile, "_upload_evidence", AsyncMock(return_value=(evidence, "client", "bucket")))
    monkeypatch.setattr(staff_mobile, "_delete_evidence_object", delete_mock)

    result = asyncio.run(staff_mobile.mobile_complete(
        "appointment-1", UploadFile(filename="proof.jpg", file=None), "120.00", "PayNow",
        "PAYNOW-REF", "Completed successfully", 1.3001, 103.8001, 7.0,
        "same-completion-request", {"sub": "staff-1", "sid": "session-1"},
    ))

    assert result["already_applied"] is True
    delete_mock.assert_awaited_once_with("client", "bucket", evidence["storage_key"])


def test_expired_session_is_rejected(monkeypatch):
    class SessionRepo:
        async def get_staff_mobile_session(self, _session_id):
            return {
                "id": "session-1", "staff_id": "staff-1", "revoked_at": None,
                "expires_at": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
                "staff_active": True, "credential_active": True,
            }

    monkeypatch.setattr(staff_mobile, "get_repository", lambda: SessionRepo())
    assert asyncio.run(staff_mobile._valid_session("session-1", "staff-1")) is None


def test_crm_can_create_a_staff_username_and_pin_without_exposing_hash(monkeypatch):
    repo = FakeMobileRepository()
    monkeypatch.setattr(staff_mobile, "get_repository", lambda: repo)

    result = asyncio.run(staff_mobile.provision_staff_mobile_access(
        "staff-1", staff_mobile.MobilePinRequest(username="Tech.One", pin="123456", active=True),
    ))

    assert result["staff"]["username"] == "tech.one"
    assert "pin_hash" not in result["staff"]
    assert repo.mobile_access_args[0:2] == ("staff-1", "tech.one")
    assert "123456" not in repo.mobile_access_args[2]


def test_disabling_mobile_access_revokes_the_staff_login(monkeypatch):
    repo = FakeMobileRepository()
    monkeypatch.setattr(staff_mobile, "get_repository", lambda: repo)

    result = asyncio.run(staff_mobile.disable_staff_mobile_access("staff-1"))

    assert result == {"success": True, "disabled": True}
    assert repo.mobile_access_disabled is True


def test_staff_check_in_and_out_use_authenticated_identity_and_staff_timezone(monkeypatch):
    repo = FakeMobileRepository()
    monkeypatch.setattr(staff_mobile, "get_repository", lambda: repo)
    monkeypatch.setattr(staff_mobile, "_staff_today", lambda _identity: "2026-08-28")
    identity = {"sub": "staff-1", "sid": "session-1", "session": {"timezone": "Asia/Singapore"}}
    request = staff_mobile.MobileAttendanceRequest(
        latitude=1.3001, longitude=103.8001, accuracy_meters=7.5, notes="Morning shift",
    )

    checked_in = asyncio.run(staff_mobile.mobile_attendance_check_in(request, identity))
    checked_out = asyncio.run(staff_mobile.mobile_attendance_check_out(request, identity))

    assert checked_in["attendance"]["staff_id"] == "staff-1"
    assert checked_in["attendance"]["work_date"] == "2026-08-28"
    assert checked_in["attendance"]["check_in_session_id"] == "session-1"
    assert checked_out["attendance"]["check_out_session_id"] == "session-1"
    assert checked_out["attendance"]["check_out_at"]


def test_staff_leave_request_is_pending_and_admin_can_decide(monkeypatch):
    repo = FakeMobileRepository()
    monkeypatch.setattr(staff_mobile, "get_repository", lambda: repo)
    monkeypatch.setattr(staff_mobile, "_staff_today", lambda _identity: "2026-08-28")
    identity = {"sub": "staff-1", "sid": "session-1", "session": {"timezone": "Asia/Singapore"}}

    created = asyncio.run(staff_mobile.mobile_create_leave_request(
        staff_mobile.MobileLeaveRequest(
            leave_type="annual", start_date="2026-09-01", end_date="2026-09-02", reason="Family event",
        ),
        identity,
    ))
    decided = asyncio.run(staff_mobile.admin_decide_staff_leave(
        created["leave_request"]["id"],
        staff_mobile.AdminLeaveDecisionRequest(decision="approved", admin_note="Approved coverage"),
    ))

    assert created["leave_request"]["status"] == "pending"
    assert created["leave_request"]["staff_id"] == "staff-1"
    assert decided["leave_request"]["status"] == "approved"
    assert repo.leave_decision[1:] == ("approved", "Approved coverage", "admin")


def test_staff_operations_overview_combines_attendance_jobs_tracking_and_leave(monkeypatch):
    repo = FakeMobileRepository()
    repo.staff = [
        {"id": "staff-1", "name": "Technician One", "role": "technician", "active": True},
        {"id": "staff-2", "name": "Technician Two", "role": "technician", "active": True},
    ]
    repo.attendance = {
        "id": "attendance-1", "staff_id": "staff-1", "work_date": "2026-08-28",
        "check_in_at": "2026-08-28T01:00:00+00:00", "check_out_at": None,
        "check_in_latitude": 1.3001, "check_in_longitude": 103.8001,
    }
    repo.operation_appointments = [{
        "id": "appointment-1", "staff_id": "staff-1", "staff_name": "Technician One",
        "name": "Synthetic Customer", "date": "2026-08-28", "time": "10:00",
        "service": "Synthetic service", "status": "booked", "mobile_job_state": "en_route",
    }]
    repo.job_details["appointment-1"] = {
        "latest_location": {"latitude": 1.3521, "longitude": 103.8198, "captured_at": staff_mobile._iso(staff_mobile._utcnow())},
        "events": [{"event_type": "en_route"}], "evidence": [],
    }
    repo.leave_requests = [
        {"id": "leave-current", "staff_id": "staff-2", "staff_name": "Technician Two", "leave_type": "annual", "start_date": "2026-08-28", "end_date": "2026-08-28", "status": "approved"},
        {"id": "leave-pending", "staff_id": "staff-2", "staff_name": "Technician Two", "leave_type": "medical", "start_date": "2026-09-01", "end_date": "2026-09-01", "status": "pending"},
    ]
    monkeypatch.setattr(staff_mobile, "get_repository", lambda: repo)

    result = asyncio.run(staff_mobile.admin_staff_operations_overview("2026-08-28"))

    assert result["summary"] == {
        "staff_total": 2, "checked_in": 1, "checked_out": 0, "absent": 0,
        "on_leave": 1, "active_jobs": 1, "tracking_jobs": 1, "pending_leave": 1,
        "pending_jobs": 0, "completed_jobs": 0, "cancelled_jobs": 0,
        "appointments_total": 1,
    }
    assert result["staff"][0]["attendance_state"] == "checked_in"
    assert result["staff"][1]["attendance_state"] == "on_leave"
    assert result["active_jobs"][0]["tracking_active"] is True
    assert result["pending_leave"][0]["id"] == "leave-pending"
    assert result["appointments"][0]["id"] == "appointment-1"


def test_staff_mobile_schema_is_additive_and_idempotent():
    schema = Path("staff_mobile_schema.sql").read_text(encoding="utf-8")
    expected_tables = {
        "staff_mobile_credentials",
        "staff_mobile_sessions",
        "appointment_job_events",
        "appointment_tracking_events",
        "appointment_evidence",
        "appointment_start_otps",
        "staff_attendance",
        "staff_leave_requests",
    }

    assert schema.count("CREATE TABLE IF NOT EXISTS") == len(expected_tables)
    assert all(f"CREATE TABLE IF NOT EXISTS {table}" in schema for table in expected_tables)
    assert "DROP " not in schema.upper()
    assert "TRUNCATE " not in schema.upper()
    assert "DELETE FROM" not in schema.upper()
    assert "ALTER TABLE" not in schema.upper()


def test_staff_apk_workflow_builds_a_standalone_bundle():
    workflow = Path(".github/workflows/staff-mobile-apk.yml").read_text(encoding="utf-8")

    assert "assembleRelease" in workflow
    assert "assembleDebug" not in workflow
    assert "assets/index.android.bundle" in workflow
    assert "app-release.apk" in workflow


def test_staff_mobile_has_home_navigation_and_crm_operations_center():
    mobile = Path("staff-mobile/App.tsx").read_text(encoding="utf-8")
    package = json.loads(Path("staff-mobile/package.json").read_text(encoding="utf-8"))
    crm = Path("ui/index.html").read_text(encoding="utf-8")

    assert "function HomeScreen" in mobile
    assert "function BottomNav" in mobile
    assert "{ id: 'home'" in mobile
    assert "{ id: 'jobs'" in mobile
    assert "{ id: 'attendance'" in mobile
    assert "{ id: 'leave'" in mobile
    assert "SafeAreaProvider" in mobile
    assert "useSafeAreaInsets" in mobile
    assert "Math.max(insets.bottom, 8)" in mobile
    assert "testID={`bottom-nav-${item.id}`}" in mobile
    assert 'testID="login-role-admin"' in mobile
    assert "function AdminWorkspace" in mobile
    assert "testID={`admin-nav-${item.id}`}" in mobile
    assert "/api/mobile/admin/overview" in Path("staff-mobile/src/api.ts").read_text(encoding="utf-8")
    assert "adminDecideLeave" in mobile
    assert package["dependencies"]["react-native-safe-area-context"]
    assert 'id="staff-ops"' in crm
    assert "/api/staff-operations/overview" in crm
    assert "Approve leave" in crm
    assert "openFieldServiceTracking" in crm


def test_staff_mobile_readiness_requires_every_release_gate(monkeypatch):
    class ReadyRepository:
        async def backend_health_check(self):
            return {"backend": "mysql", "ok": True}

        async def staff_mobile_schema_health(self):
            return {"backend": "mysql", "ok": True, "required_tables_missing": []}

    async def setting(key, default=""):
        return {
            "service_completion_template": "airzen_service_completed",
        }.get(key, default)

    monkeypatch.setattr(staff_mobile, "get_repository", lambda: ReadyRepository())
    monkeypatch.setattr(staff_mobile, "get_setting", setting)
    monkeypatch.setattr(staff_mobile, "_storage_setting", AsyncMock(return_value="configured"))

    result = asyncio.run(staff_mobile.staff_mobile_readiness())

    assert result["ready"] is True
    assert result["backend_ok"] is True
    assert result["staff_mobile_schema_ok"] is True
    assert result["service_start_code_enabled"] is True
    assert "job_start_otp_template_configured" not in result
