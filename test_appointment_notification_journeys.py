from pathlib import Path


ROOT = Path(__file__).resolve().parent


def test_appointment_request_paths_queue_outbox_instead_of_direct_staff_send():
    server_source = (ROOT / "server.py").read_text(encoding="utf-8")
    assert "notification_outbox_runner" in server_source
    assert "run_due_notification_outbox()" in server_source
    assert "await send_staff_appointment_notification(" not in server_source


def test_whatsapp_booking_and_reminder_paths_use_notification_outbox():
    source = (ROOT / "whatsapp.py").read_text(encoding="utf-8")
    assert "enqueue_required_appointment_notifications(" in source
    assert "appointment_customer_reschedule" in source
    assert "appointment_staff_cancellation" in source
    assert "appointment_reminder_outbox_queued" in source
    assert "staff_outcome_request" in source
