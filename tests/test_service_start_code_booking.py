from types import SimpleNamespace

import pytest

import whatsapp


class BookingRepository:
    def __init__(self):
        self.start_code = None

    async def create_appointment_with_outbox(
        self, appointment, customer_intent, staff_intent, event_identity="v1", start_code=None,
    ):
        self.start_code = start_code
        return {"appointment_id": appointment["id"]}


@pytest.mark.asyncio
async def test_mysql_booking_atomically_receives_hash_only_start_code(monkeypatch):
    monkeypatch.setenv("AIRZEN_DB_BACKEND", "mysql")
    monkeypatch.setenv("STAFF_MOBILE_TOKEN_SECRET", "test-mobile-secret-that-is-at-least-32-characters")
    repository = BookingRepository()
    monkeypatch.setattr(whatsapp, "_db", lambda: SimpleNamespace(get_repository=lambda: repository))

    async def staff_phone(_appointment):
        return "+6592222222"

    monkeypatch.setattr(whatsapp, "_appointment_staff_phone", staff_phone)
    appointment = {
        "id": "appointment-code-test",
        "name": "Test Customer",
        "phone": "+6591111111",
        "date": "2030-09-01",
        "time": "10:00",
        "timezone": "Asia/Singapore",
        "staff_id": "staff-code-test",
    }

    result = await whatsapp.create_appointment_with_notification_outbox_rpc(appointment)

    code = result["service_start_code"]
    assert result["success"] is True
    assert code.isdigit() and len(code) == 6
    assert repository.start_code["staff_id"] == appointment["staff_id"]
    assert len(repository.start_code["otp_hash"]) == 64
    assert code not in repository.start_code["otp_hash"]
    assert code not in str(repository.start_code)


def test_booking_confirmation_instruction_is_clear_and_rejects_invalid_codes():
    text = whatsapp._service_start_code_customer_instruction("482615", "technician")
    assert text.startswith("Service start code: 482615")
    assert "only after they arrive" in text
    assert "AirZen staff app" in text
    assert whatsapp._service_start_code_customer_instruction("12345") == ""
