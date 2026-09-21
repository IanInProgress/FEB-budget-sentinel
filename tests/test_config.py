import pytest

from config import ConfigError, load_settings


def test_load_settings_reads_custom_item_budget_threshold(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "token")
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "secret")
    monkeypatch.setenv("MANAGER_CHANNEL_ID", "C123")
    monkeypatch.setenv("GOOGLE_SHEET_ID", "sheet")
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_FILE", "google-service-account.json")
    monkeypatch.setenv("ITEM_BUDGET_REJECT_THRESHOLD_PERCENT", "112.5")
    monkeypatch.setenv("SUBTEAM_SHEET_IDS_JSON", '{"mech": "mech-sheet", "EECS": "eecs-sheet"}')

    settings = load_settings(load_env=False)

    assert settings.item_budget_reject_threshold_percent_of_estimate == 112.5
    assert settings.subteam_sheet_ids == {"MECH": "mech-sheet", "EECS": "eecs-sheet"}


def test_load_settings_rejects_negative_item_budget_threshold(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "token")
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "secret")
    monkeypatch.setenv("MANAGER_CHANNEL_ID", "C123")
    monkeypatch.setenv("GOOGLE_SHEET_ID", "sheet")
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_FILE", "google-service-account.json")
    monkeypatch.setenv("ITEM_BUDGET_REJECT_THRESHOLD_PERCENT", "-1")

    with pytest.raises(ConfigError):
        load_settings(load_env=False)


def test_load_settings_rejects_invalid_subteam_sheet_mapping(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "token")
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "secret")
    monkeypatch.setenv("MANAGER_CHANNEL_ID", "C123")
    monkeypatch.setenv("GOOGLE_SHEET_ID", "sheet")
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_FILE", "google-service-account.json")
    monkeypatch.setenv("SUBTEAM_SHEET_IDS_JSON", "not-json")

    with pytest.raises(ConfigError):
        load_settings(load_env=False)


def test_load_settings_uses_builtin_subteam_sheet_ids_when_env_is_missing(monkeypatch):
    monkeypatch.delenv("SUBTEAM_SHEET_IDS_JSON", raising=False)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "token")
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "secret")
    monkeypatch.setenv("MANAGER_CHANNEL_ID", "C123")
    monkeypatch.setenv("GOOGLE_SHEET_ID", "sheet")
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_FILE", "google-service-account.json")

    settings = load_settings(load_env=False)

    assert settings.subteam_sheet_ids["MECH"] == "1fO9CJElk0blio6DnT1a-BrrK2TxvQ7PZu3TWkDiH8wI"


