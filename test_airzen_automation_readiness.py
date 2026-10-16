from pathlib import Path

import db


ROOT = Path(__file__).resolve().parent


def test_default_timezone_is_singapore_for_airzen_appointments():
    assert db.DEFAULT_TIMEZONE == "Asia/Singapore"
    assert db.DEFAULTS["APP_TIMEZONE"] == "Asia/Singapore"
    assert db.DEFAULTS["APPOINTMENT_TIMEZONE"] == "Asia/Singapore"


def test_call_outcome_whatsapp_fallback_is_awaited_in_batch_and_campaign_paths():
    source = (ROOT / "server.py").read_text(encoding="utf-8")
    forbidden = "asyncio.create_task(handle_call_outcome_whatsapp_fallback"
    assert forbidden not in source
    assert source.count("await handle_call_outcome_whatsapp_fallback(") >= 2
