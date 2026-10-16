from pathlib import Path

import pytest

from scripts.apply_staff_mobile_schema import (
    EXPECTED_TABLES,
    _statements,
    _table_names,
    _validate_schema,
)


def test_staff_mobile_schema_is_additive_and_complete():
    schema = Path("staff_mobile_schema.sql").read_text(encoding="utf-8")
    statements = _statements(schema)

    _validate_schema(statements)

    assert len(statements) == len(EXPECTED_TABLES)
    for table in EXPECTED_TABLES:
        assert any(f"CREATE TABLE IF NOT EXISTS {table}" in statement for statement in statements)


@pytest.mark.parametrize(
    "statement",
    [
        "DROP TABLE staff_mobile_sessions",
        "ALTER TABLE appointments ADD COLUMN unsafe INT",
        "INSERT INTO settings (`key`) VALUES ('unsafe')",
    ],
)
def test_staff_mobile_schema_guard_rejects_non_additive_sql(statement):
    statements = [
        f"CREATE TABLE IF NOT EXISTS {table} (id INT)" for table in EXPECTED_TABLES
    ]
    statements[0] = statement

    with pytest.raises(RuntimeError):
        _validate_schema(statements)


def test_table_name_extraction_is_independent_of_driver_key_casing():
    assert _table_names(
        [
            {"table_name": "staff_mobile_credentials"},
            {"TABLE_NAME": "staff_mobile_sessions"},
        ]
    ) == {"staff_mobile_credentials", "staff_mobile_sessions"}
