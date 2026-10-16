import pytest
from unittest.mock import AsyncMock, patch
from httpx import AsyncClient, ASGITransport
from datetime import datetime, timedelta
from server import app, _make_session_token, SESSION_COOKIE_NAME
from db import insert_appointment


@pytest.mark.asyncio
async def test_insert_appointment_with_specific_doctor():
    doctor = {
        "id": "doc_101",
        "name": "Dr. Anita Sharma",
        "role": "doctor",
        "active": True,
        "working_days": ["mon", "tue", "wed", "thu", "fri", "sat"],
        "start_time": "09:00",
        "end_time": "18:00",
        "timezone": "Asia/Kolkata",
    }
    
    mock_repo = AsyncMock()
    mock_repo.get_appointment_staff = AsyncMock(return_value=doctor)
    mock_repo.get_booked_appointments_for_staff = AsyncMock(return_value=[])
    mock_repo.list_appointments_by_phone = AsyncMock(return_value=[])
    mock_repo.create_appointment = AsyncMock()

    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")

    with patch("db.get_repository", return_value=mock_repo), \
         patch("db.get_appointment_settings", return_value={
             "minimum_notice_minutes": 10,
             "max_booking_days_ahead": 30,
             "demo_duration_minutes": 20,
             "buffer_minutes": 10,
             "timezone": "Asia/Kolkata",
             "default_staff_role": "doctor",
         }), \
         patch("db._staff_available_for", return_value=True), \
         patch("db.update_lead_journey", new_callable=AsyncMock):

        booking_id = await insert_appointment(
            name="Ramesh Patient",
            phone="+919876543210",
            date=tomorrow,
            time="11:30",
            service="Dental Cleaning",
            details={"staff_id": "doc_101"}
        )

        assert booking_id is not None
        assert mock_repo.create_appointment.called
        created_row = mock_repo.create_appointment.call_args[0][0]
        assert created_row["staff_id"] == "doc_101"
        assert created_row["staff_name"] == "Dr. Anita Sharma"


@pytest.mark.asyncio
async def test_api_create_appointment_endpoint():
    transport = ASGITransport(app=app)
    cookies = {SESSION_COOKIE_NAME: _make_session_token("admin")}
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")

    with patch("db.insert_appointment", new_callable=AsyncMock, return_value="BK123456"), \
         patch("db.get_all_appointments", new_callable=AsyncMock, return_value=[{"id": "BK123456", "phone": "+919876543210"}]), \
         patch("server.enqueue_required_appointment_notifications", new_callable=AsyncMock), \
         patch("db.update_crm_contact_status", new_callable=AsyncMock):

        async with AsyncClient(transport=transport, base_url="http://test", cookies=cookies) as client:
            res = await client.post("/api/appointments", json={
                "name": "Sunita Patel",
                "phone": "+919876543210",
                "date": tomorrow,
                "time": "14:00",
                "service": "Root Canal Consultation",
                "staff_id": "doc_101",
                "notes": "Severe toothache on lower molar",
            })
            assert res.status_code == 200
            data = res.json()
            assert data["success"] is True
            assert data["booking_id"] == "BK123456"


@pytest.mark.asyncio
async def test_api_get_available_slots_endpoint():
    transport = ASGITransport(app=app)
    cookies = {SESSION_COOKIE_NAME: _make_session_token("admin")}
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")

    mock_slots = [
        {"time": "09:00", "available": True, "conflict_reason": None},
        {"time": "09:30", "available": False, "conflict_reason": "Already booked"},
        {"time": "10:00", "available": True, "conflict_reason": None},
    ]

    with patch("db.get_day_available_slots", new_callable=AsyncMock, return_value=mock_slots):
        async with AsyncClient(transport=transport, base_url="http://test", cookies=cookies) as client:
            res = await client.get(f"/api/appointments/available-slots?date={tomorrow}&staff_id=doc_101")
            assert res.status_code == 200
            data = res.json()
            assert data["date"] == tomorrow
            assert data["staff_id"] == "doc_101"
            assert data["total_slots"] == 3
            assert data["total_available"] == 2
            assert data["slots"][0]["time"] == "09:00"
            assert data["slots"][0]["available"] is True

