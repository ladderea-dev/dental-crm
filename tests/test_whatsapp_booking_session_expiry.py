from datetime import datetime, timedelta

import whatsapp as wa


def test_naive_booking_timestamp_uses_runtime_timezone_for_expiry():
    now = datetime.now().astimezone()
    recent_state = {
        "status": "awaiting_address",
        "updated_at": (datetime.now() - timedelta(hours=5)).isoformat(),
    }
    stale_state = {
        "status": "awaiting_address",
        "updated_at": (datetime.now() - timedelta(hours=7)).isoformat(),
    }

    assert wa._booking_session_expired(recent_state, now=now) is False
    assert wa._booking_session_expired(stale_state, now=now) is True
