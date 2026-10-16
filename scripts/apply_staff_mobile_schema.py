"""Safely inspect or apply the additive AirZen staff-mobile MySQL schema."""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

import aiomysql


SCHEMA_PATH = Path(__file__).resolve().parents[1] / "staff_mobile_schema.sql"
EXPECTED_TABLES = (
    "staff_mobile_credentials",
    "staff_mobile_sessions",
    "appointment_job_events",
    "appointment_tracking_events",
    "appointment_evidence",
    "appointment_start_otps",
    "staff_attendance",
    "staff_leave_requests",
)


def _statements(sql: str) -> list[str]:
    return [statement.strip() for statement in sql.split(";") if statement.strip()]


def _validate_schema(statements: list[str]) -> None:
    if len(statements) != len(EXPECTED_TABLES):
        raise RuntimeError(
            f"Expected {len(EXPECTED_TABLES)} additive CREATE statements, found {len(statements)}"
        )
    for statement in statements:
        normalized = " ".join(
            line for line in statement.upper().splitlines() if not line.lstrip().startswith("--")
        ).strip()
        if not normalized.startswith("CREATE TABLE IF NOT EXISTS "):
            raise RuntimeError("Schema contains a non-additive statement")


def _table_names(rows: list[dict[str, object]]) -> set[str]:
    """Return the single selected value without relying on driver key casing."""
    return {str(next(iter(row.values()))) for row in rows}


def _connection_settings() -> dict[str, object]:
    required = ("MYSQL_HOST", "MYSQL_DATABASE", "MYSQL_USER", "MYSQL_PASSWORD")
    missing = [name for name in required if not (os.getenv(name) or "").strip()]
    if missing:
        raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")
    return {
        "host": os.environ["MYSQL_HOST"],
        "port": int(os.getenv("MYSQL_PORT", "3306")),
        "db": os.environ["MYSQL_DATABASE"],
        "user": os.environ["MYSQL_USER"],
        "password": os.environ["MYSQL_PASSWORD"],
        "autocommit": True,
    }


async def _run(*, apply: bool, confirm_database: str | None) -> int:
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    statements = _statements(sql)
    _validate_schema(statements)

    connection = await aiomysql.connect(**_connection_settings())
    try:
        async with connection.cursor(aiomysql.DictCursor) as cursor:
            await cursor.execute("SELECT DATABASE() AS database_name, VERSION() AS mysql_version")
            identity = await cursor.fetchone()
            database_name = str(identity["database_name"])
            print(f"database={database_name}")
            print(f"mysql_version={identity['mysql_version']}")
            print(f"mode={'APPLY' if apply else 'PLAN'}")
            print(f"schema_statements={len(statements)}")

            if apply:
                if not confirm_database or confirm_database != database_name:
                    raise RuntimeError(
                        "Refusing APPLY: --confirm-database must exactly match the connected database"
                    )
                for statement in statements:
                    await cursor.execute(statement)

            placeholders = ",".join(["%s"] * len(EXPECTED_TABLES))
            await cursor.execute(
                f"""
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = %s AND table_name IN ({placeholders})
                ORDER BY table_name
                """,
                (database_name, *EXPECTED_TABLES),
            )
            present = _table_names(await cursor.fetchall())
            missing = sorted(set(EXPECTED_TABLES) - present)
            print(f"tables_present={len(present)}/{len(EXPECTED_TABLES)}")
            print(f"missing_tables={','.join(missing) if missing else 'none'}")
            if apply and missing:
                raise RuntimeError("Schema APPLY completed with missing required tables")
    finally:
        connection.close()

    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Apply the additive schema")
    parser.add_argument(
        "--confirm-database",
        help="Required in APPLY mode and must exactly match SELECT DATABASE()",
    )
    args = parser.parse_args()
    if args.confirm_database and not args.apply:
        parser.error("--confirm-database is only valid with --apply")
    return asyncio.run(_run(apply=args.apply, confirm_database=args.confirm_database))


if __name__ == "__main__":
    raise SystemExit(main())
