import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scripts import migrate_supabase_to_mysql as migration


class FakeDatabase:
    def __init__(self, rows=None):
        self.rows = {table: [] for table in migration.TABLE_ORDER}
        self.rows.update(rows or {})

    async def iter_rows(self, table, primary_key, page_size):
        rows = sorted(self.rows[table], key=lambda row: str(row[primary_key]))
        for start in range(0, len(rows), page_size):
            yield [dict(row) for row in rows[start : start + page_size]]


class FakeWritableDatabase(FakeDatabase):
    database_name = "airzen_staging"

    def __init__(self):
        super().__init__()
        self.writes = []

    async def upsert_rows(self, table, primary_key, rows, *, resume):
        self.writes.append((table, primary_key, [dict(row) for row in rows], resume))


def column(name, data_type="varchar", *, nullable=True, default=None, column_type=None):
    return migration.ColumnInfo(
        name=name,
        data_type=data_type,
        column_type=column_type or data_type,
        nullable=nullable,
        default=default,
    )


def minimal_schema():
    schema = {
        table: [column(migration.PRIMARY_KEYS[table], nullable=False)]
        for table in migration.TABLE_ORDER
    }
    schema["crm_contacts"].append(column("phone_number"))
    schema["contact_memory"].append(column("phone_number"))
    schema["whatsapp_conversations"].extend(
        [column("phone_number"), column("appointment_state", "json")]
    )
    schema["whatsapp_messages"].append(column("phone_number"))
    schema["whatsapp_logs"].append(column("phone_number"))
    schema["appointments"].extend(
        [
            column("phone"),
            column("date", "date"),
            column("time", "time"),
            column("created_at", "datetime"),
            column("status"),
            column("staff_id"),
        ]
    )
    schema["followup_actions"].extend([column("phone_number"), column("status")])
    schema["call_logs"].append(column("phone_number"))
    schema["automation_actions"].extend([column("phone_number"), column("status")])
    schema["broadcast_recipients"].append(column("phone_number"))
    schema["notification_outbox"].append(column("status"))
    return schema


def test_timestamp_normalization_uses_utc_and_empty_values_become_null():
    info = column("created_at", "datetime")

    assert migration.normalize_value("2026-08-24T08:30:00+08:00", info, table="sample") == (
        "2026-08-24 00:30:00.000000"
    )
    assert migration.normalize_value("", info, table="sample") is None
    assert migration.normalize_value(
        datetime(2026, 8, 24, 1, 2, 3, 4, tzinfo=timezone.utc), info, table="sample"
    ) == "2026-08-24 01:02:03.000004"


def test_invalid_timestamp_fails_before_any_database_write():
    with pytest.raises(migration.MigrationError, match="Invalid timestamp"):
        migration.normalize_value("not-a-timestamp", column("created_at", "datetime"), table="sample")


def test_json_is_validated_and_logically_preserved_without_double_encoding():
    info = column("appointment_state", "json")
    encoded_state = '{"status":"awaiting_address","requested_time":"14:00"}'

    logical = migration.normalize_value(encoded_state, info, table="whatsapp_conversations")
    stored = migration.sql_value(encoded_state, info, table="whatsapp_conversations")

    assert logical == {"status": "awaiting_address", "requested_time": "14:00"}
    assert json.loads(stored) == logical
    assert not isinstance(json.loads(stored), str)
    with pytest.raises(migration.MigrationError, match="Invalid JSON"):
        migration.normalize_value("{broken", info, table="whatsapp_conversations")


def test_source_schema_drift_is_not_silently_discarded():
    schema = [column("id", nullable=False), column("created_at", "datetime", nullable=False)]

    with pytest.raises(migration.MigrationError, match="missing from MySQL: legacy_value"):
        migration.prepare_row(
            {"id": "one", "created_at": "2026-08-24T00:00:00Z", "legacy_value": "keep me"},
            schema,
            table="sample",
        )
    with pytest.raises(migration.MigrationError, match="missing required MySQL columns: created_at"):
        migration.prepare_row({"id": "one"}, schema, table="sample")


def dataset_rows(*, target_types=False):
    timestamp = (
        datetime(2026, 8, 24, 0, 0, tzinfo=timezone.utc)
        if target_types
        else "2026-08-24T00:00:00Z"
    )
    appointment_state = (
        {"status": "awaiting_intake", "requested_time": "14:00"}
        if target_types
        else '{"requested_time":"14:00","status":"awaiting_intake"}'
    )
    return {
        "crm_contacts": [
            {"id": "contact-1", "phone_number": "+6590000001"},
            {"id": "contact-2", "phone_number": "+6590000002"},
        ],
        "whatsapp_conversations": [
            {
                "id": "conversation-1",
                "phone_number": "+6590000001",
                "appointment_state": appointment_state,
            }
        ],
        "appointments": [
            {
                "id": "appointment-1",
                "phone": "+6590000001",
                "date": "2026-08-25",
                "time": "10:00:00",
                "created_at": timestamp,
                "status": "booked",
                "staff_id": "staff-1",
            },
            {
                "id": "appointment-2",
                "phone": "+6590000001",
                "date": "2026-08-26",
                "time": "11:00:00",
                "created_at": timestamp,
                "status": "booked",
                "staff_id": "staff-1",
            },
        ],
        "followup_actions": [
            {"id": "followup-1", "phone_number": "+6590000001", "status": "scheduled"}
        ],
        "automation_actions": [
            {"id": "automation-1", "phone_number": "+6590000001", "status": "pending"}
        ],
        "notification_outbox": [{"id": "outbox-1", "status": "pending"}],
    }


def test_full_digest_and_required_business_metrics_match_across_database_types():
    schema = minimal_schema()
    source = asyncio.run(
        migration.analyze_dataset(FakeDatabase(dataset_rows()), schema, page_size=1)
    )
    compared = {table: item.compared_columns for table, item in source.tables.items()}
    target = asyncio.run(
        migration.analyze_dataset(
            FakeDatabase(dataset_rows(target_types=True)),
            schema,
            page_size=2,
            compared_columns=compared,
        )
    )

    assert migration.compare_summaries(source, target) == []
    assert source.metrics.unique_phone_counts["crm_contacts"] == 2
    assert source.metrics.latest_appointment_phone_count == 1
    assert source.metrics.active_booking_state_count == 1
    assert source.metrics.pending_followup_count == 1
    assert source.metrics.pending_automation_count == 1
    assert source.metrics.pending_outbox_count == 1


def test_verification_detects_changed_rows_and_ids():
    schema = minimal_schema()
    source = asyncio.run(
        migration.analyze_dataset(FakeDatabase(dataset_rows()), schema, page_size=500)
    )
    changed = dataset_rows(target_types=True)
    changed["crm_contacts"][0]["id"] = "different-id"
    compared = {table: item.compared_columns for table, item in source.tables.items()}
    target = asyncio.run(
        migration.analyze_dataset(
            FakeDatabase(changed), schema, page_size=500, compared_columns=compared
        )
    )

    failures = migration.compare_summaries(source, target)
    assert "crm_contacts: primary-key digest differs" in failures
    assert "crm_contacts: normalized row digest differs" in failures


def test_migration_serializes_json_for_mysql_and_preserves_resume_mode():
    schema = minimal_schema()
    source = FakeDatabase(
        {
            "whatsapp_conversations": [
                {
                    "id": "conversation-1",
                    "phone_number": "+6590000001",
                    "appointment_state": '{"status":"awaiting_address"}',
                }
            ]
        }
    )
    target = FakeWritableDatabase()

    migrated = asyncio.run(
        migration.migrate_rows(
            source, target, schema, page_size=10, batch_size=1, resume=True
        )
    )

    assert migrated["whatsapp_conversations"] == 1
    write = next(item for item in target.writes if item[0] == "whatsapp_conversations")
    assert write[3] is True
    assert json.loads(write[2][0]["appointment_state"]) == {"status": "awaiting_address"}


def test_cli_defaults_to_read_only_plan():
    args = migration.build_parser().parse_args([])

    assert args.command == "plan"
    assert args.confirm_target == ""
    assert args.resume is False


def test_cli_refuses_apply_before_connecting_without_target_confirmation():
    with pytest.raises(SystemExit) as exc:
        migration.main(["apply"])

    assert exc.value.code == 2


def test_mysql_schema_upgrades_preexisting_appointment_tables_idempotently():
    schema = Path("mysql_schema.sql").read_text(encoding="utf-8")

    for column_name in (
        "telegram_notified",
        "telegram_reminder_sent",
        "telegram_reminder_sent_at",
        "calcom_booking_uid",
        "calendar_sync_error",
        "last_calendar_sync_at",
    ):
        assert f"COLUMN_NAME='{column_name}'" in schema
        assert f"ALTER TABLE appointments ADD COLUMN {column_name}" in schema

    assert "ALTER TABLE appointments ADD COLUMN IF NOT EXISTS" not in schema


def test_mysql_schema_widens_legacy_notification_dedupe_keys_idempotently():
    schema = Path("mysql_schema.sql").read_text(encoding="utf-8")

    assert "TABLE_NAME='notification_outbox'" in schema
    assert "COLUMN_NAME='dedupe_key'" in schema
    assert "LOWER(COLUMN_TYPE) <> 'char(64)'" in schema
    assert "ALTER TABLE notification_outbox MODIFY COLUMN dedupe_key CHAR(64) NOT NULL" in schema
