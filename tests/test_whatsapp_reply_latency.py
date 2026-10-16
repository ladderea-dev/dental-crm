import asyncio
import time
from unittest.mock import AsyncMock, patch

import whatsapp as wa


class SlowLogDb:
    async def log_error(self, *_args, **_kwargs):
        await asyncio.sleep(0.2)


def test_diagnostic_logging_does_not_block_reply_path():
    async def run():
        start = time.perf_counter()
        with patch.object(wa, "_db", return_value=SlowLogDb()):
            await wa._diagnostic_event("latency_probe", phone="+6590000000")
            await wa._log_whatsapp_ai_event("+6590000000", "latency_probe")
        return time.perf_counter() - start

    elapsed = asyncio.run(run())
    assert elapsed < 0.12


def test_existing_conversation_greeting_is_deterministic_without_gemini_or_kb():
    with patch("db.get_setting", new=AsyncMock(side_effect=AssertionError("settings should not be loaded for a greeting"))), patch(
        "db.get_knowledge_base", new=AsyncMock(side_effect=AssertionError("KB should not be loaded for a greeting"))
    ):
        result = asyncio.run(
            wa.generate_whatsapp_ai_reply(
                {"contact_name": "Karuna"},
                "Hello",
                [{"direction": "inbound", "message_text": "previous"}],
                conversation_mode="in_progress",
                crm_contact={"lead_name": "Karuna"},
            )
        )

    assert result["provider"] == "deterministic_greeting"
    assert "Karuna" in result["reply"]


def test_whatsapp_ai_timeout_is_short_and_configurable():
    source = open("whatsapp.py", encoding="utf-8").read()
    assert 'WHATSAPP_AI_TIMEOUT_SECONDS", "8"' in source
    assert "timeout=ai_timeout" in source
    assert "timeout=25" not in source
