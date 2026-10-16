import whatsapp as wa


def test_notification_dedupe_key_is_stable_and_recipient_specific():
    a = wa.appointment_notification_dedupe_key("appt-1", "appointment_customer_confirmation", "customer")
    b = wa.appointment_notification_dedupe_key("appt-1", "appointment_customer_confirmation", "customer")
    c = wa.appointment_notification_dedupe_key("appt-1", "appointment_customer_confirmation", "staff")
    assert a == b
    assert a != c
    assert len(a) == 64
