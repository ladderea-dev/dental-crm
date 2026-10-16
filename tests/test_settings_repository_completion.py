import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import agent
import db
import whatsapp as wa


def _repository(**methods):
    return SimpleNamespace(**methods)


def test_generic_setting_precedence_preserves_env_database_and_default(monkeypatch):
    get_setting = AsyncMock(return_value="database")
    repo = _repository(get_setting=get_setting)

    monkeypatch.setenv("BATCH5A_SETTING", "environment")
    with patch.object(db, "get_repository", return_value=repo):
        assert asyncio.run(db.get_setting("BATCH5A_SETTING", "default")) == "environment"
    get_setting.assert_not_awaited()

    monkeypatch.delenv("BATCH5A_SETTING", raising=False)
    with patch.object(db, "get_repository", return_value=repo):
        assert asyncio.run(db.get_setting("BATCH5A_SETTING", "default")) == "database"

    get_setting.return_value = ""
    with patch.object(db, "get_repository", return_value=repo):
        assert asyncio.run(db.get_setting("BATCH5A_SETTING", "default")) == "default"

    get_setting.return_value = None
    with patch.object(db, "get_repository", return_value=repo):
        assert asyncio.run(db.get_setting("BATCH5A_SETTING", "default")) == "default"


def test_settings_admin_view_and_bulk_save_use_repository(monkeypatch):
    list_settings = AsyncMock(return_value={
        "GEMINI_MODEL": "database-model",
        "BATCH5A_DB_ONLY": "database-only",
        "BATCH5A_EMPTY": "",
        "TEST_KEY": "hidden",
    })
    set_settings = AsyncMock()
    repo = _repository(list_settings=list_settings, set_settings=set_settings)
    monkeypatch.setenv("GEMINI_MODEL", "environment-model")

    with patch.object(db, "get_repository", return_value=repo):
        settings = asyncio.run(db.get_all_settings())
        asyncio.run(db.save_settings({
            "FIRST": "one",
            "NUMBER": 2,
            "EMPTY": "",
            "MISSING": None,
        }))

    assert settings["GEMINI_MODEL"] == {
        "value": "environment-model",
        "configured": True,
        "source": "env",
    }
    assert settings["BATCH5A_DB_ONLY"] == {
        "value": "database-only",
        "configured": True,
        "source": "db",
    }
    assert settings["BATCH5A_EMPTY"]["source"] == "none"
    assert "TEST_KEY" not in settings
    set_settings.assert_awaited_once_with({"FIRST": "one", "NUMBER": "2"})


def test_init_db_probes_settings_through_repository(monkeypatch, capsys):
    list_settings = AsyncMock(return_value={})
    repo = _repository(list_settings=list_settings)
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "test-service-key")

    with patch.object(db, "get_repository", return_value=repo), patch(
        "airzen_repository.configured_backend", return_value="supabase"
    ):
        db.init_db()

    list_settings.assert_awaited_once()
    assert "[OK] Supabase connected" in capsys.readouterr().out


def test_whatsapp_setting_remains_database_first(monkeypatch):
    get_setting = AsyncMock(return_value="database-value")
    repo = _repository(get_setting=get_setting)
    fake_db = SimpleNamespace(get_repository=lambda: repo)
    monkeypatch.setenv("WHATSAPP_PROVIDER", "environment-value")

    with patch.object(wa, "_db", return_value=fake_db):
        assert asyncio.run(wa._get_wa_setting("WHATSAPP_PROVIDER")) == "database-value"

    get_setting.return_value = ""
    with patch.object(wa, "_db", return_value=fake_db):
        assert asyncio.run(wa._get_wa_setting("WHATSAPP_PROVIDER")) == "environment-value"


def test_automation_rules_remain_database_first(monkeypatch):
    database_rules = [{"event_type": "database", "enabled": True}]
    environment_rules = [{"event_type": "environment", "enabled": True}]
    get_setting = AsyncMock(return_value=json.dumps(database_rules))
    repo = _repository(get_setting=get_setting)
    fake_db = SimpleNamespace(get_repository=lambda: repo)
    monkeypatch.setenv("AUTOMATION_RULES_JSON", json.dumps(environment_rules))

    with patch.object(wa, "_db", return_value=fake_db):
        rules, source = asyncio.run(wa.get_automation_rules_with_source())
    assert (rules, source) == (database_rules, "db")

    get_setting.return_value = ""
    with patch.object(wa, "_db", return_value=fake_db):
        rules, source = asyncio.run(wa.get_automation_rules_with_source())
    assert (rules, source) == (environment_rules, "env")


def test_agent_startup_only_fills_missing_environment(monkeypatch):
    list_settings = AsyncMock(return_value={
        "BATCH5A_EXISTING": "database-existing",
        "BATCH5A_MISSING": "database-missing",
        "BATCH5A_EMPTY": "",
    })
    repo = _repository(list_settings=list_settings)
    monkeypatch.setenv("BATCH5A_EXISTING", "environment-existing")
    monkeypatch.delenv("BATCH5A_MISSING", raising=False)
    monkeypatch.delenv("BATCH5A_EMPTY", raising=False)

    with patch.object(db, "get_repository", return_value=repo):
        asyncio.run(agent._load_db_settings_to_env())

    assert agent.os.environ["BATCH5A_EXISTING"] == "environment-existing"
    assert agent.os.environ["BATCH5A_MISSING"] == "database-missing"
    assert "BATCH5A_EMPTY" not in agent.os.environ


def test_agent_startup_repository_failure_is_nonfatal(caplog):
    list_settings = AsyncMock(side_effect=RuntimeError("settings unavailable"))
    repo = _repository(list_settings=list_settings)

    with patch.object(db, "get_repository", return_value=repo):
        asyncio.run(agent._load_db_settings_to_env())

    assert "Could not load settings from repository" in caplog.text


def test_agent_sync_startup_wrapper_runs_async_loader(monkeypatch):
    load = AsyncMock()
    monkeypatch.setenv("AIRZEN_DB_BACKEND", "mysql")

    with patch.object(agent, "_load_db_settings_to_env", new=load):
        agent.load_db_settings_to_env()

    load.assert_awaited_once()
