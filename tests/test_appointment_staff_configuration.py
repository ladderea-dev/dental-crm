import asyncio
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

import db
import server


ROOT = Path(__file__).resolve().parents[1]


class FakeStaffRepository:
    def __init__(self, rows=None):
        self.rows = [dict(row) for row in (rows or [])]

    async def list_appointment_staff(self, include_inactive=False):
        rows = self.rows if include_inactive else [row for row in self.rows if row.get("active", True)]
        return [dict(row) for row in rows]

    async def get_appointment_staff(self, staff_id):
        row = next((row for row in self.rows if row.get("id") == staff_id), None)
        return dict(row) if row else None

    async def upsert_appointment_staff(self, row, staff_id=None):
        payload = dict(row)
        target_id = staff_id or payload["id"]
        existing = next((item for item in self.rows if item.get("id") == target_id), None)
        if staff_id and not existing:
            raise ValueError("Staff member not found")
        payload["id"] = target_id
        if existing:
            existing.update(payload)
            return dict(existing)
        self.rows.append(payload)
        return dict(payload)

    async def set_appointment_staff_active(self, staff_id, active):
        existing = next((item for item in self.rows if item.get("id") == staff_id), None)
        if not existing:
            return False
        existing["active"] = bool(active)
        return True

    async def delete_appointment_staff(self, staff_id):
        before = len(self.rows)
        self.rows[:] = [row for row in self.rows if row.get("id") != staff_id]
        return len(self.rows) < before

    async def update_appointment_staff_calendar(self, staff_id, updates):
        existing = next((item for item in self.rows if item.get("id") == staff_id), None)
        if not existing:
            return False
        existing.update(updates)
        return True


class FakeAppointmentRepository:
    def __init__(self):
        self.tables = {"appointments": []}
        self.operations = []

    async def create_appointment(self, row):
        payload = dict(row)
        self.operations.append(("insert", "appointments", payload, {}))
        self.tables["appointments"].append(payload)
        return dict(payload)

    async def update_appointment(self, appointment_id, updates):
        payload = dict(updates)
        filters = {"id": appointment_id}
        self.operations.append(("update", "appointments", payload, filters))
        matched = False
        for row in self.tables["appointments"]:
            if row.get("id") == appointment_id:
                row.update(payload)
                matched = True
        return matched


def staff_payload(**updates):
    payload = {
        "name": "Test Staff", "role": "service_staff", "active": True,
        "working_days": ["mon", "tue", "wed", "thu", "fri", "sat"],
        "start_time": "09:00", "end_time": "18:00", "timezone": "Asia/Singapore",
    }
    payload.update(updates)
    return payload


def test_edit_updates_same_id_survives_refetch_and_does_not_insert_duplicate():
    fake = FakeStaffRepository([{"id": "staff-1", **staff_payload(name="Old Name"), "working_days": '["mon"]'}])
    with patch.object(db, "get_repository", return_value=fake), patch.object(
        db, "get_appointment_settings", new=AsyncMock(return_value={
            "default_staff_role": "service_staff", "timezone": "Asia/Singapore"
        })
    ):
        saved = asyncio.run(db.upsert_appointment_staff(staff_payload(name="Updated Name"), staff_id="staff-1"))
        reloaded = asyncio.run(db.get_appointment_staff(include_inactive=True))
    assert saved["id"] == "staff-1"
    assert saved["name"] == "Updated Name"
    assert saved["timezone"] == "Asia/Singapore"
    assert saved["working_days"] == ["mon", "tue", "wed", "thu", "fri", "sat"]
    assert len(fake.rows) == 1
    assert reloaded[0]["id"] == "staff-1"
    assert reloaded[0]["name"] == "Updated Name"


def test_new_staff_uses_configured_role_and_appointment_timezone():
    fake = FakeStaffRepository()
    with patch.object(db, "get_repository", return_value=fake), patch.object(
        db, "get_appointment_settings", new=AsyncMock(return_value={
            "default_staff_role": "service_staff", "timezone": "Asia/Singapore"
        })
    ):
        saved = asyncio.run(db.upsert_appointment_staff({
            "name": "New Staff", "working_days": ["thu"],
            "start_time": "09:00", "end_time": "18:00",
        }))
    assert saved["role"] == "service_staff"
    assert saved["timezone"] == "Asia/Singapore"
    assert saved["working_days"] == ["thu"]


def test_invalid_working_day_is_rejected_and_legacy_value_is_safe():
    with pytest.raises(ValueError, match="tus"):
        db._normalize_staff_working_days(["mon", "tus"])
    assert db._normalize_staff_working_days(["thu"]) == ["thu"]
    assert db._staff_days({"working_days": '["wed","tus","fri"]'}) == ["wed", "fri"]


def test_staff_timezone_is_used_for_working_day_and_hours():
    singapore_staff = staff_payload(working_days=["wed"], timezone="Asia/Singapore")
    kolkata_staff = staff_payload(working_days=["wed"], timezone="Asia/Kolkata")
    request_at_9_sg = datetime(2026, 8, 12, 9, 0)
    assert db._staff_available_for(singapore_staff, request_at_9_sg, "Asia/Singapore")
    assert not db._staff_available_for(kolkata_staff, request_at_9_sg, "Asia/Singapore")
    assert not db._staff_available_for({**singapore_staff, "active": False}, request_at_9_sg, "Asia/Singapore")


def test_role_filter_is_exact_and_inactive_staff_never_matches():
    service = {"id": "service", **staff_payload(working_days=["wed"])}
    technician = {"id": "tech", **staff_payload(role="technician", working_days=["wed"])}
    inactive = {"id": "inactive", **staff_payload(active=False, working_days=["wed"])}
    with patch.object(db, "get_appointment_settings", new=AsyncMock(return_value={
        "default_staff_role": "service_staff", "timezone": "Asia/Singapore"
    })), patch.object(db, "get_appointment_staff", new=AsyncMock(return_value=[service, technician, inactive])), patch.object(
        db, "_appointment_conflicts", new=AsyncMock(return_value=False)
    ), patch.object(db, "_calendar_settings", new=AsyncMock(return_value={
        "enabled": False, "fallback_internal": True, "timezone": "Asia/Singapore",
    })), patch.object(db, "log_error", new=AsyncMock()):
        matches = asyncio.run(db._booking_candidate_staff(datetime(2026, 8, 12, 9, 0), "service_staff"))
    assert [row["id"] for row in matches] == ["service"]


def test_calendar_busy_staff_is_excluded_from_booking_candidates():
    service = {"id": "service", **staff_payload(working_days=["wed"], google_calendar_connected=True, google_calendar_id="staff@example.com")}
    with patch.object(db, "get_appointment_settings", new=AsyncMock(return_value={
        "default_staff_role": "service_staff", "timezone": "Asia/Singapore",
        "demo_duration_minutes": 60,
    })), patch.object(db, "get_appointment_staff", new=AsyncMock(return_value=[service])), patch.object(
        db, "_appointment_conflicts", new=AsyncMock(return_value=False)
    ), patch.object(db, "_calendar_settings", new=AsyncMock(return_value={
        "enabled": True, "fallback_internal": True, "timezone": "Asia/Singapore",
    })), patch.object(db, "_google_calendar_busy", new=AsyncMock(return_value=True)) as busy:
        matches = asyncio.run(db._booking_candidate_staff(datetime(2026, 8, 12, 9, 0), "service_staff"))
    assert matches == []
    busy.assert_awaited_once()


def test_insert_appointment_creates_calendar_event_after_db_insert():
    fake = FakeAppointmentRepository()
    staff = {"id": "staff-1", "name": "Test Staff", "google_calendar_connected": True, "google_calendar_id": "staff@example.com"}
    calendar_create = AsyncMock(return_value={"calendar_event_id": "event-1", "meet_link": "https://meet.test"})
    with patch.object(db, "get_repository", return_value=fake), patch.object(
        db, "get_appointment_settings", new=AsyncMock(return_value={
            "timezone": "Asia/Singapore", "default_staff_role": "service_staff",
            "demo_duration_minutes": 60, "buffer_minutes": 15,
            "minimum_notice_minutes": 0, "max_booking_days_ahead": 30,
            "delivery_mode": "onsite", "default_service_type": "Aircon servicing",
        })
    ), patch.object(db, "_appointment_now_local", return_value=datetime(2026, 8, 11, 9, 0)), patch.object(
        db, "get_existing_active_appointment", new=AsyncMock(return_value=None)
    ), patch.object(db, "_select_staff_round_robin", new=AsyncMock(return_value=staff)), patch.object(
        db, "_create_google_calendar_event", new=calendar_create
    ), patch.object(db, "set_setting", new=AsyncMock()), patch.object(db, "update_lead_journey", new=AsyncMock()):
        booking_id = asyncio.run(db.insert_appointment(
            "Karuna", "+6590000000", "2026-08-12", "14:00", "Aircon servicing",
            {"service_address": "Test address", "intake_data": {"units": "2"}},
        ))
    assert booking_id
    assert [op[0] for op in fake.operations][:2] == ["insert", "update"]
    assert fake.operations[1][2]["calendar_event_id"] == "event-1"
    calendar_create.assert_awaited_once()


def test_calendar_event_failure_keeps_single_insert_and_records_sync_error():
    fake = FakeAppointmentRepository()
    staff = {"id": "staff-1", "name": "Test Staff", "google_calendar_connected": True, "google_calendar_id": "staff@example.com"}
    with patch.object(db, "get_repository", return_value=fake), patch.object(
        db, "get_appointment_settings", new=AsyncMock(return_value={
            "timezone": "Asia/Singapore", "default_staff_role": "service_staff",
            "demo_duration_minutes": 60, "buffer_minutes": 15,
            "minimum_notice_minutes": 0, "max_booking_days_ahead": 30,
            "delivery_mode": "onsite", "default_service_type": "Aircon servicing",
        })
    ), patch.object(db, "_appointment_now_local", return_value=datetime(2026, 8, 11, 9, 0)), patch.object(
        db, "get_existing_active_appointment", new=AsyncMock(return_value=None)
    ), patch.object(db, "_select_staff_round_robin", new=AsyncMock(return_value=staff)), patch.object(
        db, "_create_google_calendar_event", new=AsyncMock(return_value={"error": "mock calendar failure"})
    ), patch.object(db, "set_setting", new=AsyncMock()), patch.object(db, "update_lead_journey", new=AsyncMock()):
        asyncio.run(db.insert_appointment("Karuna", "+6590000000", "2026-08-12", "14:00", "Aircon servicing", {}))
    assert [op[0] for op in fake.operations].count("insert") == 1
    assert fake.operations[1][2]["calendar_sync_error"] == "mock calendar failure"


def test_availability_role_mismatch_is_truthful_and_sanitized():
    settings = {
        "minimum_notice_minutes": 30, "max_booking_days_ahead": 30,
        "default_staff_role": "service_staff", "timezone": "Asia/Singapore",
        "demo_duration_minutes": 60, "buffer_minutes": 15,
    }
    wrong_role = {"id": "staff-id", **staff_payload(role="technician", working_days=["wed"])}
    diagnostic = AsyncMock()
    with patch.object(db, "get_appointment_settings", new=AsyncMock(return_value=settings)), patch.object(
        db, "_appointment_now_local", return_value=datetime(2026, 8, 12, 8, 0)
    ), patch.object(db, "get_appointment_staff", new=AsyncMock(return_value=[wrong_role])), patch.object(
        db, "log_error", new=diagnostic
    ):
        result = asyncio.run(db.get_slot_availability("2026-08-12", "09:00"))
    assert result == {"available": False, "reason": "no_matching_staff_role", "conflict": False}
    diagnostic.assert_awaited_once()
    logged = " ".join(str(value) for value in diagnostic.await_args.args)
    assert "appointment_staff_role_mismatch" in logged
    assert "configured_role=service_staff" in logged
    assert "staff-id" not in logged


def test_staff_ui_preserves_edit_id_and_supports_singapore_timezone():
    html = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
    assert "textContent='Editing: '+(s.name||'Staff')" in html
    assert "$('staff-id').value=s.id" in html
    assert "method:id?'PATCH':'POST'" in html
    assert "await loadAppointmentStaff()" in html
    assert "Cancel Edit" in html
    assert "Asia/Singapore" in html
    assert "const preferred=['Asia/Singapore','Asia/Kolkata','UTC','Etc/UTC']" in html
    assert "default_staff_role:'service_staff'" in html
    assert "STAFF_DAY_OPTIONS" in html
    assert "renderAppointmentStaffCards" in html
    assert "Permanently delete this staff member" in html


def test_staff_create_update_activation_and_delete_apis_use_intended_identity():
    created = {"id": "new-id", **staff_payload()}
    updated = {"id": "staff-1", **staff_payload(name="Updated")}
    upsert = AsyncMock(side_effect=[created, updated])
    with patch.object(server, "upsert_appointment_staff", new=upsert), patch.object(
        server, "deactivate_appointment_staff", new=AsyncMock(return_value=True)
    ) as deactivate, patch.object(
        server, "activate_appointment_staff", new=AsyncMock(return_value=True)
    ) as activate, patch.object(
        server, "delete_appointment_staff", new=AsyncMock(return_value={"deleted": True, "deactivated": False})
    ) as delete_staff:
        create_result = asyncio.run(server.api_create_appointment_staff(
            server.AppointmentStaffRequest(**staff_payload())
        ))
        update_result = asyncio.run(server.api_update_appointment_staff(
            "staff-1", server.AppointmentStaffRequest(**staff_payload(name="Updated"))
        ))
        asyncio.run(server.api_deactivate_appointment_staff("staff-1"))
        asyncio.run(server.api_activate_appointment_staff("staff-1"))
        delete_result = asyncio.run(server.api_delete_appointment_staff("staff-1"))
    assert create_result["staff"]["id"] == "new-id"
    assert update_result["staff"]["id"] == "staff-1"
    assert delete_result["deleted"] is True
    assert upsert.await_args_list[0].kwargs.get("staff_id") is None
    assert upsert.await_args_list[1].kwargs["staff_id"] == "staff-1"
    deactivate.assert_awaited_once_with("staff-1")
    activate.assert_awaited_once_with("staff-1")
    delete_staff.assert_awaited_once_with("staff-1")


def test_delete_appointment_staff_removes_row_when_possible():
    fake = FakeStaffRepository([{"id": "staff-1", **staff_payload()}])
    with patch.object(db, "get_repository", return_value=fake):
        result = asyncio.run(db.delete_appointment_staff("staff-1"))
    assert result == {"deleted": True, "deactivated": False}
    assert fake.rows == []
