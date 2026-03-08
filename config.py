from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    slack_bot_token: str
    slack_signing_secret: str
    manager_channel_id: str

    google_sheet_id: str
    google_service_account_file: str | None
    google_service_account_json: str | None

    log_level: str
    slack_commands_path: str
    port: int

    purchase_command_keyword: str


class ConfigError(RuntimeError):
    pass


def _require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ConfigError(f"Missing required environment variable: {name}")
    return value


def _parse_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as e:
        raise ConfigError(f"Invalid integer for {name}: {raw}") from e
    return value


def load_settings(*, load_env: bool = True) -> Settings:
    if load_env:
        load_dotenv()

    slack_bot_token = _require_env("SLACK_BOT_TOKEN")
    slack_signing_secret = _require_env("SLACK_SIGNING_SECRET")
    manager_channel_id = _require_env("MANAGER_CHANNEL_ID")

    google_sheet_id = _require_env("GOOGLE_SHEET_ID")
    google_service_account_file = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "").strip() or None
    google_service_account_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip() or None
    if not google_service_account_file and not google_service_account_json:
        raise ConfigError(
            "Provide GOOGLE_SERVICE_ACCOUNT_FILE or GOOGLE_SERVICE_ACCOUNT_JSON for Sheets access."
        )

    log_level = os.getenv("LOG_LEVEL", "INFO").strip() or "INFO"
    slack_commands_path = os.getenv("SLACK_COMMANDS_PATH", "/slack/commands").strip() or "/slack/commands"
    port = _parse_int("PORT", 3000)

    purchase_command_keyword = os.getenv("PURCHASE_COMMAND_KEYWORD", "command_purchase:").strip() or "command_purchase:"

    return Settings(
        slack_bot_token=slack_bot_token,
        slack_signing_secret=slack_signing_secret,
        manager_channel_id=manager_channel_id,
        google_sheet_id=google_sheet_id,
        google_service_account_file=google_service_account_file,
        google_service_account_json=google_service_account_json,
        log_level=log_level,
        slack_commands_path=slack_commands_path,
        port=port,
        purchase_command_keyword=purchase_command_keyword,
    )
