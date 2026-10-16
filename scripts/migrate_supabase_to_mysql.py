"""One-time, read-safe AirZen Supabase to MySQL data migration tool.

The default ``plan`` command only reads both databases.  ``apply`` requires an
explicit confirmation of the destination database name and never deletes data
from Supabase or MySQL.  ``verify`` compares normalized row digests and the
business-level metrics required for the AirZen cutover rehearsal.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, AsyncIterator, Iterable, Protocol


TABLE_ORDER = (
    "settings",
    "lead_statuses",
    "crm_tags",
    "crm_contacts",
    "contact_memory",
    "appointment_staff",
    "agent_profiles",
    "whatsapp_conversations",
    "whatsapp_messages",
    "whatsapp_logs",
    "appointments",
    "followup_actions",
    "notification_outbox",
    "error_logs",
    "call_logs",
    "campaigns",
    "automation_actions",
    "broadcast_campaigns",
    "broadcast_recipients",
    "whatsapp_test_recipients",
    "whatsapp_health_test_runs",
    "whatsapp_health_test_results",
)

PRIMARY_KEYS = {table: "id" for table in TABLE_ORDER}
PRIMARY_KEYS.update({"settings": "key", "crm_tags": "name"})

PHONE_COLUMNS = {
    "crm_contacts": "phone_number",
    "contact_memory": "phone_number",
    "whatsapp_conversations": "phone_number",
    "whatsapp_messages": "phone_number",
    "whatsapp_logs": "phone_number",
    "appointments": "phone",
    "followup_actions": "phone_number",
    "call_logs": "phone_number",
    "automation_actions": "phone_number",
    "broadcast_recipients": "phone_number",
}

ACTIVE_BOOKING_TERMINAL_STATES = {"", "booked", "cancelled", "canceled", "completed"}
IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class MigrationError(RuntimeError):
    """Raised when migration safety or data validation fails."""


@dataclass(frozen=True)
class ColumnInfo:
    name: str
    data_type: str
    column_type: str
    nullable: bool
    default: Any = None

    @property
    def is_json(self) -> bool:
        return self.data_type.lower() == "json"

    @property
    def is_datetime(self) -> bool:
        return self.data_type.lower() in {"datetime", "timestamp"}

    @property
    def is_date(self) -> bool:
        return self.data_type.lower() == "date"

    @property
    def is_time(self) -> bool:
        return self.data_type.lower() == "time"

    @property
    def is_boolean(self) -> bool:
        return self.column_type.lower() == "tinyint(1)"


@dataclass
class TableSummary:
    table: str
    row_count: int = 0
    primary_key_digest: str = ""
    row_digest: str = ""
    compared_columns: list[str] = field(default_factory=list)


@dataclass
class BusinessMetrics:
    unique_phone_counts: dict[str, int] = field(default_factory=dict)
    latest_appointment_phone_count: int = 0
    latest_appointment_digest: str = ""
    active_booking_state_count: int = 0
    pending_followup_count: int = 0
    pending_automation_count: int = 0
    pending_outbox_count: int = 0


@dataclass
class DatasetSummary:
    tables: dict[str, TableSummary]
    metrics: BusinessMetrics


class SourceDatabase(Protocol):
    async def iter_rows(self, table: str, primary_key: str, page_size: int) -> AsyncIterator[list[dict]]: ...


class TargetDatabase(Protocol):
    database_name: str

    async def columns(self, tables: Iterable[str]) -> dict[str, list[ColumnInfo]]: ...

    async def row_count(self, table: str) -> int: ...

    async def iter_rows(self, table: str, primary_key: str, page_size: int) -> AsyncIterator[list[dict]]: ...

    async def upsert_rows(
        self,
        table: str,
        primary_key: str,
        rows: list[dict],
        *,
        resume: bool,
    ) -> None: ...


def _identifier(value: str) -> str:
    if not IDENTIFIER_RE.fullmatch(value):
        raise MigrationError(f"Unsafe SQL identifier: {value!r}")
    return value


def _json_logical_value(value: Any, *, table: str, column: str) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise MigrationError(f"Invalid JSON in {table}.{column}: {exc.msg}") from exc
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise MigrationError(f"Invalid JSON value in {table}.{column}: {exc}") from exc
    return value


def _utc_datetime(value: Any, *, table: str, column: str) -> str | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, datetime):
        current = value
    else:
        text = str(value).strip().replace("Z", "+00:00")
        try:
            current = datetime.fromisoformat(text)
        except ValueError as exc:
            raise MigrationError(f"Invalid timestamp in {table}.{column}: {value!r}") from exc
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc).replace(tzinfo=None)
    return current.strftime("%Y-%m-%d %H:%M:%S.%f")


def _date_value(value: Any, *, table: str, column: str) -> str | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    try:
        return date.fromisoformat(text[:10]).isoformat()
    except ValueError as exc:
        raise MigrationError(f"Invalid date in {table}.{column}: {value!r}") from exc


def _time_value(value: Any, *, table: str, column: str) -> str | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, timedelta):
        total_microseconds = int(value.total_seconds() * 1_000_000)
        hours, remainder = divmod(total_microseconds, 3_600_000_000)
        minutes, remainder = divmod(remainder, 60_000_000)
        seconds, microseconds = divmod(remainder, 1_000_000)
        base = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        return f"{base}.{microseconds:06d}" if microseconds else base
    if isinstance(value, datetime):
        value = value.time()
    if isinstance(value, time):
        base = value.strftime("%H:%M:%S")
        return f"{base}.{value.microsecond:06d}" if value.microsecond else base
    text = str(value).strip()
    try:
        parsed = time.fromisoformat(text)
    except ValueError as exc:
        raise MigrationError(f"Invalid time in {table}.{column}: {value!r}") from exc
    base = parsed.strftime("%H:%M:%S")
    return f"{base}.{parsed.microsecond:06d}" if parsed.microsecond else base


def normalize_value(value: Any, column: ColumnInfo, *, table: str) -> Any:
    """Normalize a source or target value to AirZen's MySQL representation."""
    if column.is_json:
        return _json_logical_value(value, table=table, column=column.name)
    if column.is_datetime:
        return _utc_datetime(value, table=table, column=column.name)
    if column.is_date:
        return _date_value(value, table=table, column=column.name)
    if column.is_time:
        return _time_value(value, table=table, column=column.name)
    if column.is_boolean and value is not None:
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in {"true", "1", "yes", "on"}:
                return True
            if lowered in {"false", "0", "no", "off", ""}:
                return False
            raise MigrationError(f"Invalid boolean in {table}.{column.name}: {value!r}")
        return bool(value)
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else str(value.normalize())
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return value


def prepare_row(row: dict, columns: list[ColumnInfo], *, table: str) -> dict:
    """Validate one source row and convert it to target-compatible values."""
    target = {column.name: column for column in columns}
    unknown = sorted(set(row) - set(target))
    if unknown:
        raise MigrationError(
            f"Source table {table} has columns missing from MySQL: {', '.join(unknown)}"
        )
    missing_required = sorted(
        column.name
        for column in columns
        if column.name not in row and not column.nullable and column.default is None
    )
    if missing_required:
        raise MigrationError(
            f"Source table {table} is missing required MySQL columns: {', '.join(missing_required)}"
        )
    result: dict[str, Any] = {}
    for column in columns:
        name = column.name
        if name not in row:
            continue
        value = row[name]
        normalized = normalize_value(value, column, table=table)
        if normalized is None and not column.nullable and column.default is None:
            raise MigrationError(f"NULL is not allowed for {table}.{name}")
        result[name] = normalized
    return result


def sql_value(value: Any, column: ColumnInfo, *, table: str) -> Any:
    normalized = normalize_value(value, column, table=table)
    if column.is_json and normalized is not None:
        return json.dumps(
            normalized,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    return normalized


def _canonical(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _canonical(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else str(value.normalize())
    if isinstance(value, float):
        return format(value, ".17g")
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return value


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        _canonical(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


class MetricsAccumulator:
    def __init__(self) -> None:
        self.phone_hashes: dict[str, set[bytes]] = {table: set() for table in PHONE_COLUMNS}
        self.latest_appointments: dict[bytes, tuple[tuple[str, ...], dict]] = {}
        self.active_booking_state_count = 0
        self.pending_followup_count = 0
        self.pending_automation_count = 0
        self.pending_outbox_count = 0

    def add(self, table: str, row: dict) -> None:
        phone_column = PHONE_COLUMNS.get(table)
        phone = str(row.get(phone_column) or "") if phone_column else ""
        if phone:
            phone_hash = hashlib.sha256(phone.encode("utf-8")).digest()
            self.phone_hashes[table].add(phone_hash)
        else:
            phone_hash = b""

        if table == "appointments" and phone_hash:
            ordering = (
                str(row.get("date") or ""),
                str(row.get("time") or ""),
                str(row.get("created_at") or ""),
                str(row.get("id") or ""),
            )
            payload = {
                "phone_hash": phone_hash.hex(),
                "id": row.get("id"),
                "date": row.get("date"),
                "time": row.get("time"),
                "status": row.get("status"),
                "staff_id": row.get("staff_id"),
            }
            previous = self.latest_appointments.get(phone_hash)
            if previous is None or ordering > previous[0]:
                self.latest_appointments[phone_hash] = (ordering, payload)

        if table == "whatsapp_conversations":
            state = row.get("appointment_state")
            if isinstance(state, dict):
                status = str(state.get("status") or "").strip().lower()
                if status not in ACTIVE_BOOKING_TERMINAL_STATES:
                    self.active_booking_state_count += 1
        elif table == "followup_actions" and str(row.get("status") or "").lower() == "scheduled":
            self.pending_followup_count += 1
        elif table == "automation_actions" and str(row.get("status") or "").lower() == "pending":
            self.pending_automation_count += 1
        elif table == "notification_outbox" and str(row.get("status") or "").lower() == "pending":
            self.pending_outbox_count += 1

    def finish(self) -> BusinessMetrics:
        latest_rows = [
            payload
            for _, payload in sorted(
                self.latest_appointments.values(), key=lambda item: item[1]["phone_hash"]
            )
        ]
        latest_digest = hashlib.sha256(_canonical_bytes(latest_rows)).hexdigest()
        return BusinessMetrics(
            unique_phone_counts={
                table: len(values) for table, values in self.phone_hashes.items()
            },
            latest_appointment_phone_count=len(latest_rows),
            latest_appointment_digest=latest_digest,
            active_booking_state_count=self.active_booking_state_count,
            pending_followup_count=self.pending_followup_count,
            pending_automation_count=self.pending_automation_count,
            pending_outbox_count=self.pending_outbox_count,
        )


async def analyze_dataset(
    database: SourceDatabase,
    schema: dict[str, list[ColumnInfo]],
    *,
    page_size: int,
    compared_columns: dict[str, list[str]] | None = None,
) -> DatasetSummary:
    summaries: dict[str, TableSummary] = {}
    metrics = MetricsAccumulator()

    for table in TABLE_ORDER:
        primary_key = PRIMARY_KEYS[table]
        table_columns = schema[table]
        column_lookup = {column.name: column for column in table_columns}
        requested_columns = (compared_columns or {}).get(table)
        observed_columns: list[str] | None = list(requested_columns) if requested_columns else None
        primary_digest = hashlib.sha256()
        row_digest = hashlib.sha256()
        row_count = 0

        async for page in database.iter_rows(table, primary_key, page_size):
            for raw_row in page:
                row = prepare_row(raw_row, table_columns, table=table)
                if observed_columns is None:
                    observed_columns = [column.name for column in table_columns if column.name in row]
                missing = sorted(set(observed_columns) - set(row))
                if missing:
                    raise MigrationError(f"{table} row is missing compared columns: {', '.join(missing)}")
                normalized = {
                    name: normalize_value(row.get(name), column_lookup[name], table=table)
                    for name in observed_columns
                }
                if primary_key not in normalized:
                    raise MigrationError(f"{table} row is missing primary key {primary_key}")
                primary_digest.update(_canonical_bytes(normalized[primary_key]) + b"\n")
                row_digest.update(_canonical_bytes(normalized) + b"\n")
                metrics.add(table, normalized)
                row_count += 1

        if observed_columns is None:
            observed_columns = [column.name for column in table_columns]
        summaries[table] = TableSummary(
            table=table,
            row_count=row_count,
            primary_key_digest=primary_digest.hexdigest(),
            row_digest=row_digest.hexdigest(),
            compared_columns=observed_columns,
        )

    return DatasetSummary(tables=summaries, metrics=metrics.finish())


def compare_summaries(source: DatasetSummary, target: DatasetSummary) -> list[str]:
    failures: list[str] = []
    for table in TABLE_ORDER:
        left = source.tables[table]
        right = target.tables[table]
        if left.row_count != right.row_count:
            failures.append(f"{table}: row count {left.row_count} != {right.row_count}")
        if left.primary_key_digest != right.primary_key_digest:
            failures.append(f"{table}: primary-key digest differs")
        if left.row_digest != right.row_digest:
            failures.append(f"{table}: normalized row digest differs")
    if source.metrics != target.metrics:
        for key in asdict(source.metrics):
            left = getattr(source.metrics, key)
            right = getattr(target.metrics, key)
            if left != right:
                failures.append(f"metrics.{key}: source and target differ")
    return failures


async def migrate_rows(
    source: SourceDatabase,
    target: TargetDatabase,
    schema: dict[str, list[ColumnInfo]],
    *,
    page_size: int,
    batch_size: int,
    resume: bool,
) -> dict[str, int]:
    migrated: dict[str, int] = {}
    for table in TABLE_ORDER:
        primary_key = PRIMARY_KEYS[table]
        column_lookup = {column.name: column for column in schema[table]}
        pending: list[dict] = []
        total = 0
        async for page in source.iter_rows(table, primary_key, page_size):
            for raw_row in page:
                prepared = prepare_row(raw_row, schema[table], table=table)
                pending.append(
                    {
                        name: sql_value(value, column_lookup[name], table=table)
                        for name, value in prepared.items()
                    }
                )
                if len(pending) >= batch_size:
                    await target.upsert_rows(table, primary_key, pending, resume=resume)
                    total += len(pending)
                    pending = []
        if pending:
            await target.upsert_rows(table, primary_key, pending, resume=resume)
            total += len(pending)
        migrated[table] = total
        print(f"[migrated] {table}: {total}")
    return migrated


class SupabaseSource:
    def __init__(self, client: Any) -> None:
        self.client = client

    async def iter_rows(self, table: str, primary_key: str, page_size: int) -> AsyncIterator[list[dict]]:
        offset = 0
        while True:
            def fetch() -> list[dict]:
                result = (
                    self.client.table(table)
                    .select("*")
                    .order(primary_key)
                    .range(offset, offset + page_size - 1)
                    .execute()
                )
                data = getattr(result, "data", None)
                if not isinstance(data, list):
                    raise MigrationError(f"Supabase returned invalid data for {table}")
                return data

            try:
                page = await asyncio.to_thread(fetch)
            except Exception as exc:
                if isinstance(exc, MigrationError):
                    raise
                raise MigrationError(f"Unable to read Supabase table {table}: {exc}") from exc
            if not page:
                return
            yield page
            if len(page) < page_size:
                return
            offset += len(page)


class MySQLTarget:
    def __init__(self, connection: Any, database_name: str) -> None:
        self.connection = connection
        self.database_name = database_name

    async def columns(self, tables: Iterable[str]) -> dict[str, list[ColumnInfo]]:
        table_list = list(tables)
        placeholders = ",".join(["%s"] * len(table_list))
        async with self.connection.cursor() as cursor:
            await cursor.execute(
                "SELECT TABLE_NAME,COLUMN_NAME,DATA_TYPE,COLUMN_TYPE,IS_NULLABLE,COLUMN_DEFAULT "
                "FROM information_schema.COLUMNS "
                f"WHERE TABLE_SCHEMA=%s AND TABLE_NAME IN ({placeholders}) "
                "ORDER BY TABLE_NAME,ORDINAL_POSITION",
                (self.database_name, *table_list),
            )
            rows = await cursor.fetchall()
        schema: dict[str, list[ColumnInfo]] = {table: [] for table in table_list}
        for row in rows:
            schema[row["TABLE_NAME"]].append(
                ColumnInfo(
                    name=row["COLUMN_NAME"],
                    data_type=row["DATA_TYPE"],
                    column_type=row["COLUMN_TYPE"],
                    nullable=row["IS_NULLABLE"] == "YES",
                    default=row["COLUMN_DEFAULT"],
                )
            )
        missing = [table for table, columns in schema.items() if not columns]
        if missing:
            raise MigrationError(
                "MySQL schema is incomplete; apply mysql_schema.sql first. Missing: "
                + ", ".join(missing)
            )
        for table in table_list:
            if PRIMARY_KEYS[table] not in {column.name for column in schema[table]}:
                raise MigrationError(f"MySQL table {table} is missing primary key column {PRIMARY_KEYS[table]}")
        return schema

    async def row_count(self, table: str) -> int:
        table = _identifier(table)
        async with self.connection.cursor() as cursor:
            await cursor.execute(f"SELECT COUNT(*) AS row_count FROM `{table}`")
            row = await cursor.fetchone()
        return int(row["row_count"])

    async def iter_rows(self, table: str, primary_key: str, page_size: int) -> AsyncIterator[list[dict]]:
        table = _identifier(table)
        primary_key = _identifier(primary_key)
        offset = 0
        while True:
            async with self.connection.cursor() as cursor:
                await cursor.execute(
                    f"SELECT * FROM `{table}` ORDER BY `{primary_key}` LIMIT %s OFFSET %s",
                    (page_size, offset),
                )
                rows = list(await cursor.fetchall())
            if not rows:
                return
            yield rows
            if len(rows) < page_size:
                return
            offset += len(rows)

    async def upsert_rows(
        self,
        table: str,
        primary_key: str,
        rows: list[dict],
        *,
        resume: bool,
    ) -> None:
        if not rows:
            return
        table = _identifier(table)
        primary_key = _identifier(primary_key)
        columns = list(rows[0])
        if any(list(row) != columns for row in rows):
            raise MigrationError(f"Inconsistent source columns in {table}")
        for column in columns:
            _identifier(column)
        placeholders = ",".join(["%s"] * len(columns))
        column_sql = ",".join(f"`{column}`" for column in columns)
        insert_statement = f"INSERT INTO `{table}` ({column_sql}) VALUES ({placeholders})"
        try:
            async with self.connection.cursor() as cursor:
                if not resume:
                    values = [tuple(row[column] for column in columns) for row in rows]
                    await cursor.executemany(insert_statement, values)
                else:
                    # Resume by primary key only.  MySQL's generic
                    # ON DUPLICATE KEY can match an unrelated alternate unique
                    # key (for example a phone number) and silently retain the
                    # wrong ID, so inserts and updates are deliberately split.
                    key_placeholders = ",".join(["%s"] * len(rows))
                    await cursor.execute(
                        f"SELECT `{primary_key}` FROM `{table}` "
                        f"WHERE `{primary_key}` IN ({key_placeholders})",
                        tuple(row[primary_key] for row in rows),
                    )
                    existing = {
                        row[primary_key] for row in await cursor.fetchall()
                    }
                    inserts = [row for row in rows if row[primary_key] not in existing]
                    updates = [row for row in rows if row[primary_key] in existing]
                    if inserts:
                        await cursor.executemany(
                            insert_statement,
                            [tuple(row[column] for column in columns) for row in inserts],
                        )
                    update_columns = [column for column in columns if column != primary_key]
                    if updates and update_columns:
                        update_statement = (
                            f"UPDATE `{table}` SET "
                            + ",".join(f"`{column}`=%s" for column in update_columns)
                            + f" WHERE `{primary_key}`=%s"
                        )
                        await cursor.executemany(
                            update_statement,
                            [
                                tuple(row[column] for column in update_columns)
                                + (row[primary_key],)
                                for row in updates
                            ],
                        )
            await self.connection.commit()
        except Exception as exc:
            await self.connection.rollback()
            raise MigrationError(f"MySQL write failed for {table}: {exc}") from exc


def validate_schema(schema: dict[str, list[ColumnInfo]]) -> None:
    for table in TABLE_ORDER:
        if table not in schema or not schema[table]:
            raise MigrationError(f"Target schema is missing {table}")
        names = {column.name for column in schema[table]}
        if PRIMARY_KEYS[table] not in names:
            raise MigrationError(f"Target schema is missing {table}.{PRIMARY_KEYS[table]}")


async def target_counts(target: TargetDatabase) -> dict[str, int]:
    return {table: await target.row_count(table) for table in TABLE_ORDER}


def _summary_payload(summary: DatasetSummary) -> dict:
    return {
        "tables": {table: asdict(item) for table, item in summary.tables.items()},
        "metrics": asdict(summary.metrics),
    }


def _write_report(path: str, payload: dict) -> None:
    if not path:
        return
    report_path = Path(path).resolve()
    report_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"[report] {report_path}")


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise MigrationError(f"Missing required environment variable: {name}")
    return value


async def _open_databases() -> tuple[SupabaseSource, MySQLTarget, Any]:
    from aiomysql import DictCursor, connect
    from supabase import create_client

    supabase_url = _required_env("SUPABASE_URL")
    supabase_key = _required_env("SUPABASE_SERVICE_KEY")
    mysql_database = _required_env("MYSQL_DATABASE")
    source = SupabaseSource(create_client(supabase_url, supabase_key))
    connection = await connect(
        host=_required_env("MYSQL_HOST"),
        port=int(os.getenv("MYSQL_PORT", "3306")),
        user=_required_env("MYSQL_USER"),
        password=_required_env("MYSQL_PASSWORD"),
        db=mysql_database,
        charset="utf8mb4",
        autocommit=False,
        cursorclass=DictCursor,
    )
    async with connection.cursor() as cursor:
        await cursor.execute("SET time_zone = '+00:00'")
        await cursor.execute("SELECT DATABASE() AS database_name")
        actual_database = (await cursor.fetchone())["database_name"]
    if actual_database != mysql_database:
        connection.close()
        raise MigrationError(
            f"Connected database {actual_database!r} does not match MYSQL_DATABASE {mysql_database!r}"
        )
    return source, MySQLTarget(connection, mysql_database), connection


async def run(args: argparse.Namespace) -> int:
    source, target, connection = await _open_databases()
    try:
        schema = await target.columns(TABLE_ORDER)
        validate_schema(schema)
        counts = await target_counts(target)

        if args.command == "plan":
            print("[plan] read-only source validation started")
            source_summary = await analyze_dataset(source, schema, page_size=args.page_size)
            ready = not any(counts.values())
            payload = {
                "command": "plan",
                "write_performed": False,
                "target_database": target.database_name,
                "target_row_counts": counts,
                "target_ready": ready,
                "source": _summary_payload(source_summary),
            }
            _write_report(args.report, payload)
            print(f"[plan] source validation passed; target_ready={str(ready).lower()}")
            return 0 if ready else 2

        if args.command == "apply":
            if args.confirm_target != target.database_name:
                raise MigrationError(
                    "Write refused: --confirm-target must exactly match MYSQL_DATABASE "
                    f"({target.database_name!r})"
                )
            nonempty = {table: count for table, count in counts.items() if count}
            if nonempty and not args.resume:
                detail = ", ".join(f"{table}={count}" for table, count in nonempty.items())
                raise MigrationError(
                    "Write refused because target tables are not empty. Use --resume only for an "
                    f"interrupted run against the same source snapshot. Existing: {detail}"
                )
            print("[apply] preflight source validation started; no writes yet")
            source_summary = await analyze_dataset(source, schema, page_size=args.page_size)
            print("[apply] preflight passed; migration started")
            migrated = await migrate_rows(
                source,
                target,
                schema,
                page_size=args.page_size,
                batch_size=args.batch_size,
                resume=args.resume,
            )
            compared_columns = {
                table: item.compared_columns for table, item in source_summary.tables.items()
            }
            target_summary = await analyze_dataset(
                target,
                schema,
                page_size=args.page_size,
                compared_columns=compared_columns,
            )
            failures = compare_summaries(source_summary, target_summary)
            payload = {
                "command": "apply",
                "write_performed": True,
                "target_database": target.database_name,
                "migrated_rows": migrated,
                "verified": not failures,
                "failures": failures,
                "source": _summary_payload(source_summary),
                "target": _summary_payload(target_summary),
            }
            _write_report(args.report, payload)
            if failures:
                raise MigrationError("Post-migration verification failed: " + "; ".join(failures))
            print("[apply] migration and verification passed")
            return 0

        print("[verify] read-only comparison started")
        source_summary = await analyze_dataset(source, schema, page_size=args.page_size)
        compared_columns = {
            table: item.compared_columns for table, item in source_summary.tables.items()
        }
        target_summary = await analyze_dataset(
            target,
            schema,
            page_size=args.page_size,
            compared_columns=compared_columns,
        )
        failures = compare_summaries(source_summary, target_summary)
        payload = {
            "command": "verify",
            "write_performed": False,
            "target_database": target.database_name,
            "verified": not failures,
            "failures": failures,
            "source": _summary_payload(source_summary),
            "target": _summary_payload(target_summary),
        }
        _write_report(args.report, payload)
        if failures:
            print("[verify] FAILED")
            for failure in failures:
                print(f"  - {failure}")
            return 3
        print("[verify] PASS")
        return 0
    finally:
        await connection.ensure_closed()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Plan, apply, or verify a one-time AirZen Supabase-to-MySQL migration."
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=("plan", "apply", "verify"),
        default="plan",
        help="plan is read-only and is the default",
    )
    parser.add_argument("--confirm-target", default="", help="required by apply; must equal MYSQL_DATABASE")
    parser.add_argument(
        "--resume",
        action="store_true",
        help="allow idempotent upserts only when resuming the same source snapshot",
    )
    parser.add_argument("--page-size", type=int, default=500)
    parser.add_argument("--batch-size", type=int, default=200)
    parser.add_argument("--report", default="", help="optional JSON report path; contains no row values")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.page_size < 1 or args.page_size > 1000:
        parser.error("--page-size must be between 1 and 1000")
    if args.batch_size < 1 or args.batch_size > 1000:
        parser.error("--batch-size must be between 1 and 1000")
    if args.command == "apply" and not args.confirm_target:
        parser.error("apply requires --confirm-target with the exact MYSQL_DATABASE value")
    if args.command != "apply" and args.confirm_target:
        parser.error("--confirm-target is only valid with apply")
    if args.command != "apply" and args.resume:
        parser.error("--resume is only valid with apply")
    try:
        return asyncio.run(run(args))
    except MigrationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
