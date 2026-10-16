import pytest

import scripts.run_airzen_production_gate as gate


STAGING_URL = "https://abcdefghijklmnopqrst.supabase.co"
STAGING_REF = "abcdefghijklmnopqrst"
OTHER_URL = "https://zzzzzzzzzzzzzzzzzzzz.supabase.co"
OTHER_REF = "zzzzzzzzzzzzzzzzzzzz"


@pytest.fixture(autouse=True)
def clear_staging_guard_env(monkeypatch):
    for key in (
        "STAGING_SUPABASE_URL",
        "SUPABASE_URL",
        "AIRZEN_ALLOWED_STAGING_PROJECT_REF",
        "AIRZEN_PRODUCTION_PROJECT_REF",
        "AIRZEN_TEST_ENVIRONMENT",
        "ALLOW_STAGING_DB_WRITES",
    ):
        monkeypatch.delenv(key, raising=False)


def _enable_guard(monkeypatch, *, staging_url=STAGING_URL, allowed_ref=STAGING_REF, production_ref=""):
    monkeypatch.setenv("STAGING_SUPABASE_URL", staging_url)
    monkeypatch.setenv("AIRZEN_ALLOWED_STAGING_PROJECT_REF", allowed_ref)
    monkeypatch.setenv("AIRZEN_TEST_ENVIRONMENT", "staging")
    monkeypatch.setenv("ALLOW_STAGING_DB_WRITES", "true")
    if production_ref:
        monkeypatch.setenv("AIRZEN_PRODUCTION_PROJECT_REF", production_ref)


def test_extract_supabase_project_ref():
    assert gate.extract_supabase_project_ref(STAGING_URL) == STAGING_REF
    assert gate.extract_supabase_project_ref("https://example.com") == ""
    assert gate.extract_supabase_project_ref("") == ""


def test_staging_guard_accepts_exact_allowed_project_ref(monkeypatch):
    _enable_guard(monkeypatch)

    ok, project_ref, detail = gate.validate_staging_database_target()

    assert ok is True
    assert project_ref == STAGING_REF
    assert STAGING_REF in detail


def test_staging_guard_rejects_missing_allowlist(monkeypatch):
    _enable_guard(monkeypatch)
    monkeypatch.delenv("AIRZEN_ALLOWED_STAGING_PROJECT_REF", raising=False)

    ok, project_ref, detail = gate.validate_staging_database_target()

    assert ok is False
    assert project_ref == STAGING_REF
    assert "AIRZEN_ALLOWED_STAGING_PROJECT_REF" in detail


def test_staging_guard_rejects_mismatched_allowed_ref(monkeypatch):
    _enable_guard(monkeypatch, allowed_ref=OTHER_REF)

    ok, project_ref, detail = gate.validate_staging_database_target()

    assert ok is False
    assert project_ref == STAGING_REF
    assert "does not match" in detail


def test_staging_guard_rejects_production_reference(monkeypatch):
    _enable_guard(monkeypatch, production_ref=STAGING_REF)

    ok, project_ref, detail = gate.validate_staging_database_target()

    assert ok is False
    assert project_ref == STAGING_REF
    assert "production project ref" in detail


def test_staging_guard_rejects_supabase_url_equal_to_staging_url(monkeypatch):
    _enable_guard(monkeypatch)
    monkeypatch.setenv("SUPABASE_URL", STAGING_URL)

    ok, project_ref, detail = gate.validate_staging_database_target()

    assert ok is False
    assert project_ref == STAGING_REF
    assert "production project ref" in detail or "matches SUPABASE_URL" in detail


def test_staging_guard_rejects_derived_production_reference(monkeypatch):
    _enable_guard(monkeypatch)
    monkeypatch.setenv("SUPABASE_URL", STAGING_URL)

    ok, project_ref, detail = gate.validate_staging_database_target()

    assert ok is False
    assert project_ref == STAGING_REF
    assert "production project ref" in detail or "matches SUPABASE_URL" in detail


def test_staging_guard_does_not_require_human_readable_staging_name(monkeypatch):
    _enable_guard(monkeypatch, staging_url=OTHER_URL, allowed_ref=OTHER_REF)

    ok, project_ref, detail = gate.validate_staging_database_target()

    assert ok is True
    assert project_ref == OTHER_REF
    assert "staging project ref" in detail


def test_staging_gate_invokes_postgres_integration_when_allowlist_passes(monkeypatch):
    _enable_guard(monkeypatch)
    monkeypatch.setenv("STAGING_SUPABASE_SERVICE_KEY", "redacted-staging-service-key")
    monkeypatch.setattr(gate, "config_checks", lambda mode: [])
    monkeypatch.setattr(gate, "staging_database_checks", lambda mode: [])
    commands = []

    def fake_run_command(name, command):
        commands.append((name, command))
        return {
            "name": name,
            "status": "passed",
            "returncode": 0,
            "started_at": "2026-07-18T00:00:00+00:00",
            "stdout_tail": "",
            "stderr_tail": "",
        }

    monkeypatch.setattr(gate, "run_command", fake_run_command)

    result = gate.main(["--mode", "staging"])

    assert result == 0
    assert any(name == "postgres_atomic_outbox_integration" for name, _ in commands)
