import whatsapp as wa
from server import AppointmentStaffRequest


def test_ai_cannot_claim_booking_request_received_without_appointment_row():
    generated = (
        "For 2 units, General Servicing is SGD 60. Your booking request has been received. "
        "Our scheduling team will contact you shortly to confirm your appointment."
    )
    guarded, blocked = wa._guard_unpersisted_booking_claim(generated, {"appointment_state": "{}"})
    assert blocked is True
    assert "no appointment has been created yet" in guarded.lower()
    assert "continue with the requested booking details" in guarded.lower()
    assert "has been received" not in guarded.lower()


def test_known_slot_without_insert_requires_explicit_confirmation():
    conv = {"appointment_state": {
        "status": "pending_confirmation",
        "requested_date": "2026-08-14",
        "requested_time": "09:00",
    }}
    guarded, blocked = wa._guard_unpersisted_booking_claim(
        "Your appointment is confirmed.", conv
    )
    assert blocked is True
    assert "has not been created yet" in guarded.lower()
    assert "reply confirm" in guarded.lower()
    assert "14 aug 2026 at 9:00 am" in guarded.lower()


def test_persisted_booking_confirmation_is_not_rewritten():
    generated = "Your appointment is confirmed."
    conv = {"appointment_state": {
        "status": "booked", "booking_id": "appt-123",
        "requested_date": "2026-08-14", "requested_time": "09:00",
    }}
    guarded, blocked = wa._guard_unpersisted_booking_claim(generated, conv)
    assert blocked is False
    assert guarded == generated


def test_non_booking_sales_reply_is_not_rewritten():
    generated = "For 2 units, General Servicing is SGD 60."
    guarded, blocked = wa._guard_unpersisted_booking_claim(generated, {"appointment_state": "{}"})
    assert blocked is False
    assert guarded == generated


def test_new_appointment_staff_request_defers_role_and_timezone_to_configured_defaults():
    request = AppointmentStaffRequest(name="Technician")
    assert request.role is None
    assert request.timezone is None
