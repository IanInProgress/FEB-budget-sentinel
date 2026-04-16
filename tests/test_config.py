import pytest

from config import ConfigError, load_settings


def test_load_settings_reads_custom_item_budget_threshold(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "token")
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "secret")
    monkeypatch.setenv("MANAGER_CHANNEL_ID", "C123")
    monkeypatch.setenv("GOOGLE_SHEET_ID", "sheet")
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_FILE", "google-service-account.json")
    monkeypatch.setenv("ITEM_BUDGET_REJECT_THRESHOLD_PERCENT", "112.5")

    settings = load_settings(load_env=False)

    assert settings.item_budget_reject_threshold_percent_of_estimate == 112.5


def test_load_settings_rejects_negative_item_budget_threshold(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "token")
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "secret")
    monkeypatch.setenv("MANAGER_CHANNEL_ID", "C123")
    monkeypatch.setenv("GOOGLE_SHEET_ID", "sheet")
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_FILE", "google-service-account.json")
    monkeypatch.setenv("ITEM_BUDGET_REJECT_THRESHOLD_PERCENT", "-1")

    with pytest.raises(ConfigError):
        load_settings(load_env=False)


