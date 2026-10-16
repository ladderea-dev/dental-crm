import asyncio
import threading

import server


class _LoopBoundRepository:
    """Minimal stand-in for an async pool bound to one event loop."""

    def __init__(self, owner_loop):
        self.owner_loop = owner_loop
        self.calls = []

    async def execute(self, job_name):
        if asyncio.get_running_loop() is not self.owner_loop:
            raise RuntimeError("database operation ran on a different event loop")
        self.calls.append(job_name)


def _start_loop_thread():
    loop = asyncio.new_event_loop()
    started = threading.Event()

    def run():
        asyncio.set_event_loop(loop)
        started.set()
        loop.run_forever()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert started.wait(timeout=5)
    return loop, thread


def test_all_scheduler_wrappers_use_application_database_event_loop(monkeypatch):
    application_loop, thread = _start_loop_thread()
    repository = _LoopBoundRepository(application_loop)

    def job(name):
        async def run(*_args):
            await repository.execute(name)

        return run

    monkeypatch.setattr(server, "APPLICATION_LOOP", application_loop)
    monkeypatch.setattr(server, "run_due_automation_actions", job("automation_actions"))
    monkeypatch.setattr(server, "run_due_appointment_reminders", job("appointment_reminders"))
    monkeypatch.setattr(server, "_run_followup_and_evaluate", job("followup_actions"))
    monkeypatch.setattr(server, "run_due_notification_outbox", job("notification_outbox"))
    monkeypatch.setattr(server, "_scheduled_recording_cleanup", job("recording_cleanup"))
    monkeypatch.setattr(server, "_run_campaign", job("campaign"))

    try:
        server._run_due_automation_actions_sync()
        server._run_due_appointment_reminders_sync()
        server._run_due_followup_actions_sync()
        server._run_due_notification_outbox_sync()
        server._scheduled_recording_cleanup_sync()
        server._run_campaign_sync("campaign-test")
    finally:
        application_loop.call_soon_threadsafe(application_loop.stop)
        thread.join(timeout=5)
        application_loop.close()

    assert repository.calls == [
        "automation_actions",
        "appointment_reminders",
        "followup_actions",
        "notification_outbox",
        "recording_cleanup",
        "campaign",
    ]


def test_scheduler_error_logging_uses_application_database_event_loop(monkeypatch):
    application_loop, thread = _start_loop_thread()
    repository = _LoopBoundRepository(application_loop)

    async def log_error(*args):
        await repository.execute(args[1])

    monkeypatch.setattr(server, "APPLICATION_LOOP", application_loop)
    monkeypatch.setattr(server, "log_error", log_error)
    server._LAST_DB_ERROR_LOG_TIME.clear()

    try:
        server._record_background_error("test_job", RuntimeError("test failure"))
        asyncio.run_coroutine_threadsafe(asyncio.sleep(0), application_loop).result(timeout=5)
    finally:
        application_loop.call_soon_threadsafe(application_loop.stop)
        thread.join(timeout=5)
        application_loop.close()

    assert repository.calls == [
        "scheduler_loop_error",
        "runtime_health_background_error",
    ]
