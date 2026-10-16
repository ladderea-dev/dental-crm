import asyncio
import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from airzen_repository import (
    MySQLRepository,
    SupabaseRepository,
    configured_backend,
    create_repository,
    get_cached_repository,
    reset_repository_cache,
)


class FakeResult:
    def __init__(self, data=None):
        self.data = data


class FakeSupabaseQuery:
    def __init__(self, table):
        self.table = table
        self.filters = {}
        self.payload = None
        self.single = False

    def select(self, *_args, **_kwargs):
        return self

    def eq(self, key, value):
        self.filters[key] = value
        return self

    def gte(self, key, value):
        self.filters[f"{key}__gte"] = value
        return self

    def maybe_single(self):
        self.single = True
        return self

    def order(self, *_args, **_kwargs):
        return self

    def limit(self, *_args, **_kwargs):
        return self

    def upsert(self, payload, **_kwargs):
        self.payload = payload
        if isinstance(payload, list):
            self.table.rows.extend(payload)
        else:
            self.table.rows.append(payload)
        return self

    def insert(self, payload):
        self.payload = payload
        self.table.rows.append(payload)
        return self

    def update(self, payload):
        self.payload = payload
        self.table.pending_update = payload
        return self

    async def execute(self):
        if self.table.name == "settings":
            if self.payload is not None:
                return FakeResult(self.payload if isinstance(self.payload, list) else [self.payload])
            rows = [
                row for row in self.table.rows
                if all(row.get(key) == value for key, value in self.filters.items())
            ]
            return FakeResult((rows[0] if rows else None) if self.single else rows)
        if self.table.name == "crm_contacts":
            phone = self.filters.get("phone_number")
            if self.table.pending_update and phone:
                for row in self.table.rows:
                    if row.get("phone_number") == phone:
                        row.update(self.table.pending_update)
                        return FakeResult([row])
            for row in self.table.rows:
                if not phone or row.get("phone_number") == phone:
                    return FakeResult(row)
        return FakeResult(self.payload if isinstance(self.payload, list) else ([self.payload] if self.payload else []))


class FakeSupabaseTable:
    def __init__(self, name, rows):
        self.name = name
        self.rows = rows
        self.pending_update = None

    def select(self, *args, **kwargs):
        return FakeSupabaseQuery(self).select(*args, **kwargs)

    def upsert(self, *args, **kwargs):
        return FakeSupabaseQuery(self).upsert(*args, **kwargs)

    def insert(self, *args, **kwargs):
        return FakeSupabaseQuery(self).insert(*args, **kwargs)

    def update(self, *args, **kwargs):
        return FakeSupabaseQuery(self).update(*args, **kwargs)


class FakeSupabaseClient:
    def __init__(self):
        self.tables = {
            "crm_contacts": [],
            "settings": [{"key": "GREETING", "value": "hello"}],
        }

    def table(self, name):
        return FakeSupabaseTable(name, self.tables.setdefault(name, []))


class RecordingRpcCall:
    def __init__(self, data):
        self.data = data

    async def execute(self):
        return FakeResult(self.data)


class RecordingRpcClient:
    def __init__(self):
        self.calls = []

    def rpc(self, name, params):
        self.calls.append((name, params))
        claim_names = {"claim_notification_outbox", "claim_campaign_run", "claim_automation_actions", "claim_broadcast_campaign"}
        data = [{"id": "outbox-1", "claim_token": "claim-1"}] if name in claim_names else {"ok": True}
        return RecordingRpcCall(data)


class OutboxQuery:
    def __init__(self, rows, operation="select", payload=None):
        self.rows = rows
        self.operation = operation
        self.payload = payload
        self.filters = []
        self.limit_value = None

    def select(self, *_args, **_kwargs):
        self.operation = "select"
        return self

    def insert(self, payload):
        self.operation = "insert"
        self.payload = dict(payload)
        return self

    def update(self, payload):
        self.operation = "update"
        self.payload = dict(payload)
        return self

    def delete(self):
        self.operation = "delete"
        return self

    def eq(self, key, value):
        self.filters.append(("eq", key, value))
        return self

    def lte(self, key, value):
        self.filters.append(("lte", key, value))
        return self

    def limit(self, value):
        self.limit_value = value
        return self

    def _matches(self, row):
        for operation, key, value in self.filters:
            if operation == "eq" and row.get(key) != value:
                return False
            if operation == "lte" and str(row.get(key) or "") > str(value):
                return False
        return True

    async def execute(self):
        matches = [row for row in self.rows if self._matches(row)]
        if self.limit_value is not None:
            matches = matches[: self.limit_value]
        if self.operation == "insert":
            if any(row.get("dedupe_key") == self.payload.get("dedupe_key") for row in self.rows):
                raise RuntimeError("duplicate dedupe key")
            self.rows.append(self.payload)
            return FakeResult([self.payload])
        if self.operation == "update":
            for row in matches:
                row.update(self.payload)
            return FakeResult(matches)
        if self.operation == "delete":
            for row in matches:
                self.rows.remove(row)
            return FakeResult(matches)
        return FakeResult(matches)


class OutboxSupabaseClient:
    def __init__(self, rows=None):
        self.rows = list(rows or [])

    def table(self, name):
        assert name == "notification_outbox"
        return OutboxQuery(self.rows)


class BookingQuery:
    def __init__(self, rows, operation="select", payload=None):
        self.rows = rows
        self.operation = operation
        self.payload = payload
        self.filters = []
        self.orders = []
        self.limit_value = None
        self.single_value = False
        self.conflict_key = ""

    def select(self, *_args, **_kwargs):
        self.operation = "select"
        return self

    def insert(self, payload):
        self.operation, self.payload = "insert", dict(payload)
        return self

    def upsert(self, payload, on_conflict=""):
        self.operation, self.payload = "upsert", dict(payload)
        self.conflict_key = on_conflict
        return self

    def update(self, payload):
        self.operation, self.payload = "update", dict(payload)
        return self

    def delete(self):
        self.operation = "delete"
        return self

    def eq(self, key, value):
        self.filters.append(("eq", key, value))
        return self

    def neq(self, key, value):
        self.filters.append(("neq", key, value))
        return self

    def gte(self, key, value):
        self.filters.append(("gte", key, value))
        return self

    def lte(self, key, value):
        self.filters.append(("lte", key, value))
        return self

    def in_(self, key, values):
        self.filters.append(("in", key, list(values)))
        return self

    def ilike(self, key, value):
        self.filters.append(("prefix", key, str(value).removesuffix("%")))
        return self

    def order(self, key, desc=False):
        self.orders.append((key, bool(desc)))
        return self

    def limit(self, value):
        self.limit_value = int(value)
        return self

    def maybe_single(self):
        self.single_value = True
        return self

    def _matches(self, row):
        for operation, key, value in self.filters:
            current = row.get(key)
            if operation == "eq" and current != value:
                return False
            if operation == "neq" and current == value:
                return False
            if operation == "gte" and str(current or "") < str(value):
                return False
            if operation == "lte" and str(current or "") > str(value):
                return False
            if operation == "in" and current not in value:
                return False
            if operation == "prefix" and not str(current or "").lower().startswith(str(value).lower()):
                return False
        return True

    async def execute(self):
        matches = [row for row in self.rows if self._matches(row)]
        for key, desc in reversed(self.orders):
            matches.sort(key=lambda row: str(row.get(key) or ""), reverse=desc)
        if self.limit_value is not None:
            matches = matches[: self.limit_value]
        if self.operation == "insert":
            self.rows.append(dict(self.payload))
            return FakeResult([dict(self.payload)])
        if self.operation == "upsert":
            existing = next((row for row in self.rows if self.conflict_key and row.get(self.conflict_key) == self.payload.get(self.conflict_key)), None)
            if existing is not None:
                existing.update(self.payload)
                return FakeResult([dict(existing)])
            self.rows.append(dict(self.payload))
            return FakeResult([dict(self.payload)])
        if self.operation == "update":
            for row in matches:
                row.update(self.payload)
            return FakeResult([dict(row) for row in matches])
        if self.operation == "delete":
            for row in matches:
                self.rows.remove(row)
            return FakeResult([dict(row) for row in matches])
        if self.single_value:
            return FakeResult(dict(matches[0]) if matches else None)
        return FakeResult([dict(row) for row in matches])


class BookingSupabaseClient:
    def __init__(self, appointments=None, staff=None):
        self.tables = {
            "appointments": list(appointments or []),
            "appointment_staff": list(staff or []),
        }

    def table(self, name):
        return BookingQuery(self.tables[name])


class DomainSupabaseClient:
    def __init__(self, crm_contacts=None, contact_memory=None, followup_actions=None):
        self.tables = {
            "crm_contacts": list(crm_contacts or []),
            "contact_memory": list(contact_memory or []),
            "followup_actions": list(followup_actions or []),
        }

    def table(self, name):
        return BookingQuery(self.tables[name])


async def fake_adb():
    return FakeSupabaseClient()


def test_backend_defaults_to_supabase(monkeypatch):
    monkeypatch.delenv("AIRZEN_DB_BACKEND", raising=False)
    reset_repository_cache()
    assert configured_backend() == "supabase"
    assert isinstance(create_repository(fake_adb), SupabaseRepository)


def test_backend_can_select_mysql(monkeypatch):
    monkeypatch.setenv("AIRZEN_DB_BACKEND", "mysql")
    reset_repository_cache()
    assert isinstance(create_repository(fake_adb), MySQLRepository)


def test_cached_mysql_repository_is_scoped_to_event_loop(monkeypatch):
    monkeypatch.setenv("AIRZEN_DB_BACKEND", "mysql")
    reset_repository_cache()
    loop_one = asyncio.new_event_loop()
    loop_two = asyncio.new_event_loop()
    try:
        async def cached_twice():
            first = get_cached_repository(fake_adb)
            second = get_cached_repository(fake_adb)
            assert first is second
            return first

        first_loop_repo = loop_one.run_until_complete(cached_twice())
        second_loop_repo = loop_two.run_until_complete(cached_twice())
        assert first_loop_repo is not second_loop_repo
    finally:
        loop_one.close()
        loop_two.close()
        reset_repository_cache()


def test_supabase_repository_setting_and_crm_update():
    client = FakeSupabaseClient()

    async def adb():
        return client

    repo = SupabaseRepository(adb)

    assert asyncio.run(repo.get_setting("GREETING")) == "hello"
    assert asyncio.run(repo.list_settings()) == {"GREETING": "hello"}
    asyncio.run(repo.set_settings({"LANGUAGE": "en", "VOICE": "Kore"}))
    assert asyncio.run(repo.list_settings()) == {
        "GREETING": "hello",
        "LANGUAGE": "en",
        "VOICE": "Kore",
    }
    inserted = asyncio.run(repo.upsert_crm_contact({"phone_number": "+6590000000", "lead_name": "Test"}))
    assert inserted["phone_number"] == "+6590000000"
    assert asyncio.run(repo.update_crm_contact("+6590000000", {"lead_name": "Updated"}))


def test_supabase_outbox_rpc_contracts_are_preserved():
    client = RecordingRpcClient()

    async def adb():
        return client

    repo = SupabaseRepository(adb)
    claimed = asyncio.run(repo.claim_notification_outbox(limit=3, lease_seconds=90))
    assert claimed[0]["claim_token"] == "claim-1"
    assert asyncio.run(repo.complete_notification_outbox("outbox-1", "claim-1", "wamid-1"))
    assert asyncio.run(repo.fail_notification_outbox("outbox-2", "claim-2", "failed", "safe", "2026-08-23T10:00:00+00:00"))
    rescheduled = asyncio.run(repo.reschedule_appointment_with_outbox("old-1", {"id": "new-1"}, {"recipient_phone": "+1"}, {"recipient_phone": "+2"}, "event-1"))
    cancelled = asyncio.run(repo.cancel_appointment_with_outbox("appt-1", {"recipient_phone": "+1"}, {"recipient_phone": "+2"}, "event-2"))

    assert rescheduled["success"] and cancelled["success"]
    assert client.calls == [
        ("claim_notification_outbox", {"batch_limit": 3, "lease_seconds": 90}),
        ("mark_notification_outbox_sent", {"p_id": "outbox-1", "p_claim_token": "claim-1", "p_provider_message_id": "wamid-1"}),
        ("mark_notification_outbox_failed", {"p_id": "outbox-2", "p_claim_token": "claim-2", "p_status": "failed", "p_last_error": "safe", "p_next_attempt_at": "2026-08-23T10:00:00+00:00"}),
        ("reschedule_appointment_with_notification_outbox", {
            "p_old_appointment_id": "old-1",
            "p_new_appointment": {"id": "new-1"},
            "p_customer_intent": {"recipient_phone": "+1"},
            "p_staff_intent": {"recipient_phone": "+2"},
            "p_event_identity": "event-1",
        }),
        ("cancel_appointment_with_notification_outbox", {
            "p_appointment_id": "appt-1",
            "p_customer_intent": {"recipient_phone": "+1"},
            "p_staff_intent": {"recipient_phone": "+2"},
            "p_event_identity": "event-2",
        }),
    ]


def test_supabase_remaining_worker_claim_rpc_contracts_are_preserved():
    client = RecordingRpcClient()

    async def adb():
        return client

    repo = SupabaseRepository(adb)
    assert asyncio.run(repo.set_default_agent_profile("profile-1"))
    assert asyncio.run(repo.claim_campaign("campaign-1", lease_seconds=600))["claim_token"] == "claim-1"
    assert asyncio.run(repo.claim_automation_actions("2026-08-23T10:00:00+00:00", limit=7, lease_seconds=120))[0]["claim_token"] == "claim-1"
    assert asyncio.run(repo.claim_broadcast_campaign("broadcast-1", lease_seconds=900))["claim_token"] == "claim-1"

    assert client.calls == [
        ("set_default_agent_profile_atomic", {"target_profile_id": "profile-1"}),
        ("claim_campaign_run", {"target_campaign_id": "campaign-1", "lease_seconds": 600}),
        ("claim_automation_actions", {"due_before": "2026-08-23T10:00:00+00:00", "batch_limit": 7, "lease_seconds": 120}),
        ("claim_broadcast_campaign", {"target_campaign_id": "broadcast-1", "lease_seconds": 900}),
    ]


def test_supabase_outbox_enqueue_recovery_and_probe_are_isolated():
    now = datetime.now(timezone.utc)
    expired = {
        "id": "expired",
        "dedupe_key": "expired-key",
        "status": "processing",
        "attempt_count": 2,
        "max_attempts": 5,
        "claim_token": "old-claim",
        "lease_expires_at": (now - timedelta(minutes=1)).isoformat(),
        "updated_at": (now - timedelta(minutes=20)).isoformat(),
    }
    active = {
        "id": "active",
        "dedupe_key": "active-key",
        "status": "processing",
        "attempt_count": 1,
        "max_attempts": 5,
        "claim_token": "active-claim",
        "lease_expires_at": (now + timedelta(minutes=5)).isoformat(),
        "updated_at": (now - timedelta(minutes=20)).isoformat(),
    }
    client = OutboxSupabaseClient([expired, active])

    async def adb():
        return client

    repo = SupabaseRepository(adb)
    row = {
        "id": "standalone",
        "dedupe_key": "a" * 64,
        "status": "pending",
        "payload": {"kind": "test"},
    }
    first = asyncio.run(repo.enqueue_notification(row))
    second = asyncio.run(repo.enqueue_notification({**row, "id": "duplicate"}))
    assert first["id"] == second["id"] == "standalone"
    assert first["payload"] == {"kind": "test"}

    assert asyncio.run(repo.recover_stuck_notification_outbox(15, 50)) == 1
    assert expired["status"] == "failed"
    assert expired["claim_token"] is None
    assert active["status"] == "processing"

    probe = {"id": "probe", "dedupe_key": "probe-key", "status": "pending", "payload": {"gate": True}}
    assert asyncio.run(repo.probe_notification_outbox_storage(probe))
    assert all(stored.get("dedupe_key") != "probe-key" for stored in client.rows)


def test_supabase_appointment_and_staff_repository_contract_preserves_identity():
    client = BookingSupabaseClient(
        appointments=[
            {"id": "appt-old", "phone": "+6590000000", "date": "2026-08-20", "time": "10:00", "status": "booked"},
            {"id": "appt-cancelled", "phone": "+6590000000", "date": "2026-08-19", "time": "09:00", "status": "cancelled"},
        ],
        staff=[{
            "id": "staff-1", "name": "Old", "active": True,
            "round_robin_order": 0, "created_at": "2026-08-01T00:00:00+00:00",
        }],
    )

    async def adb():
        return client

    repo = SupabaseRepository(adb)
    booked = asyncio.run(repo.list_appointments(
        statuses=["booked"], date_from="2026-08-20", date_to="2026-08-20",
    ))
    assert [row["id"] for row in booked] == ["appt-old"]
    assert asyncio.run(repo.get_appointment("appt-", allow_prefix=True))["id"] == "appt-old"
    assert len(asyncio.run(repo.list_appointments_by_phone("+6590000000"))) == 2

    saved = asyncio.run(repo.upsert_appointment_staff({
        "id": "staff-1", "name": "Updated", "active": True,
        "updated_at": "2026-08-23T00:00:00+00:00",
    }, staff_id="staff-1"))
    assert saved["id"] == "staff-1"
    assert saved["name"] == "Updated"
    assert asyncio.run(repo.set_appointment_staff_active("staff-1", False))
    assert asyncio.run(repo.list_appointment_staff(include_inactive=False)) == []
    assert asyncio.run(repo.delete_appointment_staff("staff-1"))

    replacement = asyncio.run(repo.reschedule_appointment_record("appt-old", {
        "id": "appt-new", "phone": "+6590000000", "date": "2026-08-21",
        "time": "11:00", "status": "booked", "created_at": "2026-08-23T00:00:00+00:00",
    }))
    assert replacement["id"] == "appt-new"
    assert next(row for row in client.tables["appointments"] if row["id"] == "appt-old")["status"] == "rescheduled"


def test_supabase_crm_repository_preserves_identity_and_partial_fields():
    client = DomainSupabaseClient(crm_contacts=[{
        "id": "crm-stable",
        "phone_number": "+6590000000",
        "lead_name": "Original",
        "address": "217B Sumang Walk",
        "location": "Singapore 822217",
        "service_type": "Aircon servicing",
        "crm_status": "New Lead",
        "last_call_outcome": "interested",
        "created_at": "2026-08-01T00:00:00+00:00",
        "updated_at": "2026-08-01T00:00:00+00:00",
    }])

    async def adb():
        return client

    repo = SupabaseRepository(adb)
    merged = asyncio.run(repo.upsert_crm_contact({
        "id": "crm-replacement",
        "phone_number": "+6590000000",
        "lead_name": "Updated",
        "notes": "runtime-only alias",
    }))
    assert merged["id"] == "crm-stable"
    assert merged["address"] == "217B Sumang Walk"
    assert merged["location"] == "Singapore 822217"
    assert merged["service_type"] == "Aircon servicing"
    assert "notes" not in merged

    rows = asyncio.run(repo.list_crm_contacts(status="New Lead", outcome="interested"))
    assert [row["id"] for row in rows] == ["crm-stable"]
    assert asyncio.run(repo.update_crm_contact("+6590000000", {
        "crm_notes": "Call tomorrow",
        "notes": "runtime-only alias",
    }))
    assert client.tables["crm_contacts"][0]["address"] == "217B Sumang Walk"
    assert "notes" not in client.tables["crm_contacts"][0]
    assert asyncio.run(repo.delete_crm_contact_by_phone("+6590000000"))
    assert client.tables["crm_contacts"] == []


def test_supabase_contact_memory_replace_preserves_text_semantics():
    client = DomainSupabaseClient(contact_memory=[
        {"id": "memory-1", "phone_number": "+6590000000", "insight": "Old one", "created_at": "2026-08-01T00:00:00+00:00"},
        {"id": "memory-2", "phone_number": "+6590000000", "insight": "Old two", "created_at": "2026-08-02T00:00:00+00:00"},
    ])

    async def adb():
        return client

    repo = SupabaseRepository(adb)
    replacement_id = asyncio.run(repo.replace_contact_memory("+6590000000", "Compressed durable summary"))
    rows = asyncio.run(repo.get_contact_memory("+6590000000"))
    assert replacement_id
    assert [row["insight"] for row in rows] == ["Compressed durable summary"]
    assert isinstance(client.tables["contact_memory"][0]["insight"], str)


def test_supabase_followup_claim_and_completion_preserve_json_and_ownership():
    action = {
        "id": "followup-1",
        "phone_number": "+6590000000",
        "event_type": "no_response",
        "action_type": "whatsapp_message",
        "channel": "whatsapp",
        "scheduled_at": "2026-08-01T00:00:00+00:00",
        "status": "scheduled",
        "priority": 1,
        "payload": {"message": "Hello"},
        "result": {},
        "attempt_number": 0,
        "max_attempts": 3,
    }
    client = DomainSupabaseClient(followup_actions=[action])

    async def adb():
        return client

    repo = SupabaseRepository(adb)
    created = asyncio.run(repo.create_followup_action({
        **action,
        "id": "followup-future",
        "scheduled_at": "2099-01-01T00:00:00+00:00",
    }))
    assert isinstance(created["payload"], dict)
    assert isinstance(client.tables["followup_actions"][-1]["payload"], dict)

    claimed = asyncio.run(repo.claim_followup_actions("2026-08-02T00:00:00+00:00", limit=1))
    assert [row["id"] for row in claimed] == ["followup-1"]
    assert claimed[0]["status"] == "processing"
    assert claimed[0]["attempt_number"] == 1
    assert asyncio.run(repo.claim_followup_actions("2026-08-02T00:00:00+00:00", limit=1)) == []
    assert asyncio.run(repo.update_followup_action_status("followup-1", "completed", {"sent": True}))
    assert not asyncio.run(repo.update_followup_action_status("followup-1", "failed", {"sent": False}))
    stored = next(row for row in client.tables["followup_actions"] if row["id"] == "followup-1")
    assert stored["result"] == {"sent": True}


class FakeCursor:
    def __init__(self, scripts):
        self.scripts = list(scripts)
        self.executed = []
        self.description = []
        self.rowcount = 1

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def execute(self, sql, params=()):
        self.executed.append((sql, params))
        if self.scripts:
            script = self.scripts.pop(0)
            if "error" in script:
                raise script["error"]
            self.description = [(name,) for name in script.get("columns", [])]
            self._rows = script.get("rows", [])
            self.rowcount = script.get("rowcount", 1)
        else:
            self._rows = []

    async def fetchone(self):
        return self._rows[0] if self._rows else None

    async def fetchall(self):
        return self._rows


class FakeConnection:
    def __init__(self, scripts):
        self.cursor_obj = FakeCursor(scripts)
        self.commits = 0
        self.begins = 0
        self.rollbacks = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    def cursor(self):
        return self.cursor_obj

    async def begin(self):
        self.begins += 1

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


class FakeAcquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *_exc):
        return False


class FakePool:
    def __init__(self, scripts):
        self.conn = FakeConnection(scripts)
        self.closed = False
        self.wait_closed_calls = 0

    def acquire(self):
        return FakeAcquire(self.conn)

    def close(self):
        self.closed = True

    async def wait_closed(self):
        self.wait_closed_calls += 1


def test_mysql_repository_closes_pool_and_can_close_twice():
    pool = FakePool([])
    repo = MySQLRepository(pool=pool)

    asyncio.run(repo.close())
    asyncio.run(repo.close())

    assert pool.closed is True
    assert pool.wait_closed_calls == 1
    assert repo._pool is None


def test_mysql_setting_uses_parameterized_sql():
    pool = FakePool([
        {"columns": ["value"], "rows": [("hello",)]},
        {"columns": ["key", "value"], "rows": [("GREETING", "hello"), ("VOICE", "Kore")]},
        {"rowcount": 2},
    ])
    repo = MySQLRepository(pool=pool)

    assert asyncio.run(repo.get_setting("GREETING")) == "hello"
    assert asyncio.run(repo.list_settings()) == {"GREETING": "hello", "VOICE": "Kore"}
    asyncio.run(repo.set_settings({"GREETING": "hi", "VOICE": "Kore"}))
    assert pool.conn.rollbacks == 2
    assert pool.conn.commits == 1
    sql, params = pool.conn.cursor_obj.executed[0]
    assert "WHERE `key`=%s" in sql
    assert params == ("GREETING",)
    list_sql, list_params = pool.conn.cursor_obj.executed[1]
    assert "SELECT `key`, value FROM settings" in list_sql
    assert list_params == ()
    bulk_sql, bulk_params = pool.conn.cursor_obj.executed[2]
    assert bulk_sql.count("(%s,%s,%s)") == 2
    assert "ON DUPLICATE KEY UPDATE" in bulk_sql
    assert bulk_params[0:2] == ("GREETING", "hi")
    assert bulk_params[3:5] == ("VOICE", "Kore")


def test_mysql_crm_writes_filter_runtime_keys_and_preserve_partial_fields():
    pool = FakePool([
        {"rowcount": 1},
        {
            "columns": ["id", "phone_number", "lead_name", "address", "crm_notes"],
            "rows": [("crm-stable", "+6590000000", "Updated", "217B Sumang Walk", "Durable note")],
        },
        {"rowcount": 1},
    ])
    repo = MySQLRepository(pool=pool)

    stored = asyncio.run(repo.upsert_crm_contact({
        "id": "crm-replacement",
        "phone_number": "+6590000000",
        "lead_name": "Updated",
        "notes": "runtime-only alias",
    }))
    assert stored["id"] == "crm-stable"
    assert stored["address"] == "217B Sumang Walk"
    assert stored["crm_notes"] == "Durable note"

    insert_sql, insert_params = pool.conn.cursor_obj.executed[0]
    assert "`lead_name`" in insert_sql
    assert "`notes`" not in insert_sql
    assert "`address`" not in insert_sql
    assert "runtime-only alias" not in insert_params

    assert asyncio.run(repo.update_crm_contact("+6590000000", {
        "crm_notes": "Updated canonical note",
        "notes": "runtime-only alias",
    }))
    update_sql, update_params = pool.conn.cursor_obj.executed[2]
    assert "`crm_notes`=%s" in update_sql
    assert "`notes`" not in update_sql
    assert "runtime-only alias" not in update_params


def test_mysql_appointment_reads_are_filtered_and_parameterized():
    pool = FakePool([{
        "columns": ["id", "phone", "status"],
        "rows": [("appt-1", "+6590000000", "booked")],
    }])
    repo = MySQLRepository(pool=pool)

    rows = asyncio.run(repo.list_appointments(
        statuses=["booked", "confirmed"],
        staff_id="staff-1",
        date_from="2026-08-20",
        date_to="2026-08-21",
        ascending=True,
    ))

    assert rows[0]["id"] == "appt-1"
    sql, params = pool.conn.cursor_obj.executed[0]
    assert "staff_id=%s" in sql
    assert "status IN (%s,%s)" in sql
    assert "ORDER BY `date` ASC, `time` ASC" in sql
    assert params == ("2026-08-20", "2026-08-21", "staff-1", "booked", "confirmed")
    assert "staff-1" not in sql


def test_mysql_staff_upsert_and_appointment_reschedule_are_transactional():
    staff_pool = FakePool([
        {"columns": ["id"], "rows": [("staff-1",)]},
        {"rowcount": 1},
        {"columns": ["id", "name", "active"], "rows": [("staff-1", "Updated", 1)]},
    ])
    staff_repo = MySQLRepository(pool=staff_pool)
    saved = asyncio.run(staff_repo.upsert_appointment_staff({
        "id": "staff-1", "name": "Updated", "active": True,
        "working_days": ["mon", "tue"], "created_at": "2026-08-01T00:00:00+00:00",
        "updated_at": "2026-08-23T00:00:00+00:00",
    }, staff_id="staff-1"))
    assert saved == {"id": "staff-1", "name": "Updated", "active": 1}
    assert staff_pool.conn.begins == 1
    assert staff_pool.conn.commits == 1
    update_sql, update_params = staff_pool.conn.cursor_obj.executed[1]
    assert "WHERE id=%s" in update_sql
    assert "`created_at`" not in update_sql
    assert update_params[-1] == "staff-1"
    assert "staff-1" not in update_sql

    staff_failure_pool = FakePool([
        {"columns": ["id"], "rows": [("staff-1",)]},
        {"error": RuntimeError("staff update failed")},
    ])
    with pytest.raises(RuntimeError, match="staff update failed"):
        asyncio.run(MySQLRepository(pool=staff_failure_pool).upsert_appointment_staff({
            "id": "staff-1", "name": "Updated",
        }, staff_id="staff-1"))
    assert staff_failure_pool.conn.commits == 0
    assert staff_failure_pool.conn.rollbacks == 1

    appointment_pool = FakePool([
        {"columns": ["id"], "rows": [("appt-old",)]},
        {"columns": ["id"], "rows": []},
        {"rowcount": 1},
        {"rowcount": 1},
    ])
    appointment_repo = MySQLRepository(pool=appointment_pool)
    replacement = asyncio.run(appointment_repo.reschedule_appointment_record("appt-old", {
        "id": "appt-new", "name": "Karuna", "phone": "+6590000000",
        "date": "2026-08-21", "time": "11:00", "service": "Servicing",
        "status": "booked", "intake_data": {"units": "2"},
        "created_at": "2026-08-23T00:00:00+00:00",
    }))
    assert replacement["id"] == "appt-new"
    assert appointment_pool.conn.begins == 1
    assert appointment_pool.conn.commits == 1
    assert appointment_pool.conn.rollbacks == 0
    statements = appointment_pool.conn.cursor_obj.executed
    assert statements[0][1] == ("appt-old",)
    assert statements[1][1] == ("appt-new",)
    assert statements[3][1][-1] == "appt-old"
    assert "+6590000000" not in statements[2][0]

    appointment_failure_pool = FakePool([
        {"columns": ["id"], "rows": [("appt-old",)]},
        {"columns": ["id"], "rows": []},
        {"error": RuntimeError("replacement insert failed")},
    ])
    with pytest.raises(RuntimeError, match="replacement insert failed"):
        asyncio.run(MySQLRepository(pool=appointment_failure_pool).reschedule_appointment_record("appt-old", {
            "id": "appt-new", "name": "Karuna", "phone": "+6590000000",
            "date": "2026-08-21", "time": "11:00", "service": "Servicing",
            "status": "booked", "created_at": "2026-08-23T00:00:00+00:00",
        }))
    assert appointment_failure_pool.conn.commits == 0
    assert appointment_failure_pool.conn.rollbacks == 1


def test_mysql_standalone_write_rolls_back_on_failure():
    pool = FakePool([{"error": RuntimeError("write failed")}])
    repo = MySQLRepository(pool=pool)

    with pytest.raises(RuntimeError, match="write failed"):
        asyncio.run(repo.set_setting("GREETING", "hello"))

    assert pool.conn.commits == 0
    assert pool.conn.rollbacks == 1


def test_mysql_conversation_insert_cancellation_rolls_back_transaction():
    pool = FakePool([
        {"columns": [], "rows": []},
        {"error": asyncio.CancelledError()},
    ])
    repo = MySQLRepository(pool=pool)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(repo.get_or_create_whatsapp_conversation("+6590000000", "Karuna"))

    assert pool.conn.begins == 1
    assert pool.conn.commits == 0
    assert pool.conn.rollbacks == 1


def test_mysql_persist_appointment_state_uses_json_and_parameterized_update():
    pool = FakePool([
        {"rowcount": 1},
        {"columns": ["id", "appointment_state"], "rows": [("conv-1", '{"status":"awaiting_intake"}')]},
    ])
    repo = MySQLRepository(pool=pool)

    row = asyncio.run(repo.persist_appointment_state("conv-1", {"status": "awaiting_intake"}))
    assert row["appointment_state"] == '{"status":"awaiting_intake"}'
    update_sql, update_params = pool.conn.cursor_obj.executed[0]
    assert "UPDATE whatsapp_conversations SET" in update_sql
    assert "%s" in update_sql
    assert update_params[-1] == "conv-1"


def test_mysql_last_message_update_increments_unread_atomically():
    pool = FakePool([
        {"rowcount": 1},
        {"columns": ["id", "last_message", "unread_count"], "rows": [("conv-1", "Hello", 3)]},
    ])
    repo = MySQLRepository(pool=pool)

    row = asyncio.run(repo.update_whatsapp_conversation_last_message("conv-1", "Hello", increment_unread=True))
    assert row["id"] == "conv-1"
    update_sql, update_params = pool.conn.cursor_obj.executed[0]
    assert "unread_count=COALESCE(unread_count, 0)+1" in update_sql
    assert "WHERE id=%s" in update_sql
    assert update_params[-1] == "conv-1"


def test_mysql_recent_inbound_window_uses_parameterized_sql():
    pool = FakePool([
        {"columns": ["id"], "rows": [("msg-1",)]},
    ])
    repo = MySQLRepository(pool=pool)

    assert asyncio.run(repo.has_recent_inbound_whatsapp_message("+6590000000", datetime.now(timezone.utc)))
    sql, params = pool.conn.cursor_obj.executed[0]
    assert "phone_number=%s OR conversation_id=%s" in sql
    assert params[1:] == ("+6590000000", "+6590000000")


def test_mysql_whatsapp_admin_reads_are_filtered_and_parameterized():
    pool = FakePool([
        {
            "columns": ["id", "phone_number", "is_deleted"],
            "rows": [("conv-1", "+6590000000", 0)],
        },
        {
            "columns": ["id", "message_text", "created_at"],
            "rows": [
                ("msg-new", "new", "2026-08-23 02:00:00"),
                ("msg-old", "old", "2026-08-23 01:00:00"),
            ],
        },
    ])
    repo = MySQLRepository(pool=pool)

    conversations = asyncio.run(repo.list_whatsapp_conversations(
        status="open",
        ai_enabled=True,
        search="9000",
        limit=10,
        offset=5,
    ))
    messages = asyncio.run(repo.list_whatsapp_messages("conv-1", limit=2))

    assert conversations[0]["id"] == "conv-1"
    conversation_sql, conversation_params = pool.conn.cursor_obj.executed[0]
    assert "is_deleted=%s" in conversation_sql
    assert "status=%s" in conversation_sql
    assert "ai_enabled=%s" in conversation_sql
    assert "phone_number LIKE %s" in conversation_sql
    assert conversation_params == (False, "open", True, "%9000%", 10, 5)
    assert [row["id"] for row in messages] == ["msg-old", "msg-new"]
    message_sql, message_params = pool.conn.cursor_obj.executed[1]
    assert "conversation_id=%s AND is_deleted=%s" in message_sql
    assert message_params == ("conv-1", False, 2, 0)


def test_mysql_delivery_status_updates_message_and_log_in_one_transaction():
    pool = FakePool([
        {"columns": ["id"], "rows": [("msg-1",)]},
        {"columns": ["id"], "rows": [("log-1",)]},
        {"rowcount": 1},
        {"rowcount": 1},
    ])
    repo = MySQLRepository(pool=pool)

    result = asyncio.run(repo.record_whatsapp_delivery_status(
        "provider-1",
        "failed",
        "2026-08-23T01:02:03+00:00",
        failure_reason="rejected",
        error_code="131000",
    ))

    assert result == {"matched": True, "message_matched": True, "log_matched": True}
    assert pool.conn.begins == 1
    assert pool.conn.commits == 1
    message_sql, message_params = pool.conn.cursor_obj.executed[2]
    log_sql, log_params = pool.conn.cursor_obj.executed[3]
    assert "UPDATE whatsapp_messages SET" in message_sql
    assert "provider_status" in message_sql and "failure_reason" in message_sql and "error_code" in message_sql
    assert message_params[-1] == "provider-1"
    assert "UPDATE whatsapp_logs SET" in log_sql
    assert "status" in log_sql and "error_message" in log_sql and "failed_at" in log_sql
    assert log_params[-1] == "provider-1"


def test_mysql_conversation_soft_delete_is_transactional():
    pool = FakePool([
        {"rowcount": 1},
        {"rowcount": 2},
    ])
    repo = MySQLRepository(pool=pool)

    assert asyncio.run(repo.soft_delete_whatsapp_conversation("conv-1", "operator"))
    assert pool.conn.begins == 1
    assert pool.conn.commits == 1
    assert pool.conn.rollbacks == 0
    conversation_sql, conversation_params = pool.conn.cursor_obj.executed[0]
    messages_sql, messages_params = pool.conn.cursor_obj.executed[1]
    assert "UPDATE whatsapp_conversations" in conversation_sql
    assert conversation_params[-1] == "conv-1"
    assert "UPDATE whatsapp_messages" in messages_sql
    assert messages_params[-1] == "conv-1"


def test_mysql_outbox_claim_is_transactional_and_skip_locked():
    pool = FakePool([
        {"columns": ["id"], "rows": [("outbox-1",)]},
        {"rowcount": 1},
        {
            "columns": ["id", "status", "claim_token"],
            "rows": [("outbox-1", "processing", "claim-1")],
        },
    ])
    repo = MySQLRepository(pool=pool)

    rows = asyncio.run(repo.claim_notification_outbox(limit=1, lease_seconds=60))
    assert rows[0]["id"] == "outbox-1"
    assert pool.conn.begins == 1
    assert pool.conn.commits == 1
    claim_sql, claim_params = pool.conn.cursor_obj.executed[0]
    assert "FOR UPDATE SKIP LOCKED" in claim_sql
    assert claim_params[-1] == 1


@pytest.mark.parametrize(
    ("method_name", "table_name"),
    (
        ("claim_campaign", "campaigns"),
        ("claim_broadcast_campaign", "broadcast_campaigns"),
    ),
)
def test_mysql_campaign_claims_compare_active_leases_against_utc(monkeypatch, method_name, table_name):
    fixed_utc = datetime(2026, 1, 1, tzinfo=timezone.utc)
    active_lease = fixed_utc.replace(tzinfo=None) + timedelta(minutes=5)
    monkeypatch.setattr("airzen_repository.utc_now", lambda: fixed_utc)
    pool = FakePool([{
        "columns": ["id", "status", "claim_token", "lease_expires_at"],
        "rows": [("campaign-1", "running", "active-owner", active_lease)],
    }])
    repo = MySQLRepository(pool=pool)

    assert asyncio.run(getattr(repo, method_name)("campaign-1", lease_seconds=60)) is None
    assert pool.conn.begins == 1
    assert pool.conn.commits == 0
    assert pool.conn.rollbacks == 1
    assert len(pool.conn.cursor_obj.executed) == 1
    assert f"SELECT * FROM {table_name}" in pool.conn.cursor_obj.executed[0][0]
    assert "FOR UPDATE" in pool.conn.cursor_obj.executed[0][0]


def test_mysql_outbox_enqueue_is_parameterized_and_idempotent():
    dedupe_key = "d" * 64
    stored_at = datetime(2026, 8, 27, 10, 26, 35)
    stored = ("stored-id", dedupe_key, "pending", stored_at)
    pool = FakePool([
        {"rowcount": 1},
        {"columns": ["id", "dedupe_key", "status", "created_at"], "rows": [stored]},
        {"rowcount": 0},
        {"columns": ["id", "dedupe_key", "status", "created_at"], "rows": [stored]},
    ])
    repo = MySQLRepository(pool=pool)
    row = {
        "id": "first-id",
        "dedupe_key": dedupe_key,
        "recipient_type": "customer",
        "recipient_phone": "+6590000000",
        "notification_type": "appointment_customer_confirmation",
        "template_purpose": "appointment_confirmation_template",
        "appointment_id": "appt-1",
        "payload": {"name": "Karuna"},
    }

    first = asyncio.run(repo.enqueue_notification(row))
    second = asyncio.run(repo.enqueue_notification({**row, "id": "second-id"}))

    assert first["id"] == second["id"] == "stored-id"
    assert first["created_at"] == second["created_at"] == "2026-08-27T10:26:35"
    assert json.loads(json.dumps(first))["created_at"] == "2026-08-27T10:26:35"
    assert pool.conn.begins == 2
    assert pool.conn.commits == 2
    insert_sql, insert_params = pool.conn.cursor_obj.executed[0]
    assert "ON DUPLICATE KEY UPDATE id=id" in insert_sql
    assert "%s" in insert_sql
    assert dedupe_key in insert_params
    assert "Karuna" in next(value for value in insert_params if isinstance(value, str) and "Karuna" in value)


def test_mysql_outbox_complete_and_fail_require_active_claim_token():
    pool = FakePool([
        {"rowcount": 0},
        {"rowcount": 1},
        {"rowcount": 0},
        {"rowcount": 1},
    ])
    repo = MySQLRepository(pool=pool)

    assert not asyncio.run(repo.complete_notification_outbox("outbox-1", "stale", "wamid-stale"))
    assert asyncio.run(repo.complete_notification_outbox("outbox-1", "active", "wamid-good"))
    assert not asyncio.run(repo.fail_notification_outbox("outbox-2", "stale", "failed", "safe"))
    assert asyncio.run(repo.fail_notification_outbox("outbox-2", "active", "failed", "safe", "2026-08-23T10:00:00+00:00"))

    for sql, _params in pool.conn.cursor_obj.executed:
        assert "claim_token=%s" in sql
        assert "status='processing'" in sql


def test_mysql_outbox_recovery_locks_expired_claims_transactionally():
    pool = FakePool([
        {"columns": ["id", "attempt_count", "max_attempts"], "rows": [("outbox-1", 2, 5), ("outbox-2", 5, 5)]},
        {"rowcount": 1},
        {"rowcount": 1},
    ])
    repo = MySQLRepository(pool=pool)

    assert asyncio.run(repo.recover_stuck_notification_outbox(15, 50)) == 2
    assert pool.conn.begins == 1
    assert pool.conn.commits == 1
    select_sql, _select_params = pool.conn.cursor_obj.executed[0]
    assert "lease_expires_at IS NULL OR lease_expires_at<=%s" in select_sql
    assert "FOR UPDATE SKIP LOCKED" in select_sql
    assert pool.conn.cursor_obj.executed[1][1][0] == "failed"
    assert pool.conn.cursor_obj.executed[2][1][0] == "permanently_failed"
    for sql, params in pool.conn.cursor_obj.executed[1:]:
        assert "claim_token=NULL" in sql
        assert "processing_started_at=NULL" in sql
        assert params[-1].startswith("outbox-")


def test_mysql_contact_memory_replace_commits_once_and_rolls_back_all_on_failure():
    pool = FakePool([
        {"rowcount": 2},
        {"rowcount": 1},
    ])
    repo = MySQLRepository(pool=pool)

    replacement_id = asyncio.run(repo.replace_contact_memory("+6590000000", "Compressed summary"))
    assert replacement_id
    assert pool.conn.begins == 1
    assert pool.conn.commits == 1
    assert pool.conn.rollbacks == 0
    assert pool.conn.cursor_obj.executed[0][1] == ("+6590000000",)
    assert pool.conn.cursor_obj.executed[1][1][2] == "Compressed summary"

    failure_pool = FakePool([
        {"rowcount": 2},
        {"error": RuntimeError("replacement insert failed")},
    ])
    with pytest.raises(RuntimeError, match="replacement insert failed"):
        asyncio.run(MySQLRepository(pool=failure_pool).replace_contact_memory("+6590000000", "Compressed summary"))
    assert failure_pool.conn.begins == 1
    assert failure_pool.conn.commits == 0
    assert failure_pool.conn.rollbacks == 1


def test_mysql_followup_claim_is_skip_locked_and_terminal_update_requires_ownership():
    pool = FakePool([
        {
            "columns": ["id", "phone_number", "scheduled_at", "status", "priority", "payload", "result", "attempt_number", "max_attempts"],
            "rows": [("followup-1", "+6590000000", "2026-08-01 00:00:00", "scheduled", 1, '{"message":"Hello"}', "{}", 0, 3)],
        },
        {"rowcount": 1},
        {"rowcount": 1},
    ])
    repo = MySQLRepository(pool=pool)

    claimed = asyncio.run(repo.claim_followup_actions("2026-08-02T00:00:00+00:00", limit=1))
    assert [row["id"] for row in claimed] == ["followup-1"]
    assert claimed[0]["status"] == "processing"
    assert claimed[0]["attempt_number"] == 1
    assert claimed[0]["payload"] == {"message": "Hello"}
    assert pool.conn.begins == 1
    assert pool.conn.commits == 1
    select_sql, select_params = pool.conn.cursor_obj.executed[0]
    assert "FOR UPDATE SKIP LOCKED" in select_sql
    assert select_params[-1] == 1

    assert asyncio.run(repo.update_followup_action_status("followup-1", "completed", {"sent": True}))
    update_sql, update_params = pool.conn.cursor_obj.executed[2]
    assert "WHERE id=%s AND status=%s" in update_sql
    assert update_params[-2:] == ("followup-1", "processing")
    assert "followup-1" not in update_sql
