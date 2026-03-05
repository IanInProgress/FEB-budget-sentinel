from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

from dotenv import load_dotenv


DEFAULT_SUBTEAM_ALIASES: dict[str, str] = {
    # Admin
    "admin": "Admin",
    # Dynamics
    "dynamics": "Dynamics",
    "dyn": "Dynamics",
    # Chassis
    "chassis": "Chassis",
    # Powertrain
    "powertrain": "Powertrain",
    "pt": "Powertrain",
    # Composites
    "composites": "Composites",
    "comp": "Composites",
    # Brakes/Ergo
    "brakes": "Brakes/Ergo",
    "ergo": "Brakes/Ergo",
    # Accumulator MechE
    "meche": "Accumulator MechE",
    "mech": "Accumulator MechE",
    "accumulator": "Accumulator MechE",
    # EECS
    "eecs": "EECS",
    # Aero
    "aero": "Aero",
    # Auto
    "auto": "Auto",
    # Manufacturing
    "manufacturing": "Manufacturing",
    "mfg": "Manufacturing",
}


@dataclass(frozen=True)
class Settings:
    slack_bot_token: str
    slack_signing_secret: str

    google_sheet_id: str
    google_service_account_file: str | None
    google_service_account_json: str | None

    log_level: str
    slack_commands_path: str
    port: int

    subteam_aliases: dict[str, str]
    fuzzy_suggestion_threshold: int


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


def _load_aliases() -> dict[str, str]:
    raw = os.getenv("SUBTEAM_ALIASES_JSON", "").strip()
    aliases: dict[str, str] = dict(DEFAULT_SUBTEAM_ALIASES)
    if not raw:
        return aliases
    try:
        parsed: Any = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ConfigError("SUBTEAM_ALIASES_JSON is not valid JSON") from e
    if not isinstance(parsed, dict):
        raise ConfigError("SUBTEAM_ALIASES_JSON must be a JSON object (mapping)")
    for k, v in parsed.items():
        if not isinstance(k, str) or not isinstance(v, str):
            raise ConfigError("SUBTEAM_ALIASES_JSON keys and values must be strings")
        aliases[k.strip().lower()] = v.strip()
    return aliases


def load_settings(*, load_env: bool = True) -> Settings:
    if load_env:
        load_dotenv()

    slack_bot_token = _require_env("SLACK_BOT_TOKEN")
    slack_signing_secret = _require_env("SLACK_SIGNING_SECRET")

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

    subteam_aliases = _load_aliases()
    fuzzy_suggestion_threshold = _parse_int("FUZZY_SUGGESTION_THRESHOLD", 84)
    if not (0 <= fuzzy_suggestion_threshold <= 100):
        raise ConfigError("FUZZY_SUGGESTION_THRESHOLD must be between 0 and 100")

    return Settings(
        slack_bot_token=slack_bot_token,
        slack_signing_secret=slack_signing_secret,
        google_sheet_id=google_sheet_id,
        google_service_account_file=google_service_account_file,
        google_service_account_json=google_service_account_json,
        log_level=log_level,
        slack_commands_path=slack_commands_path,
        port=port,
        subteam_aliases=subteam_aliases,
        fuzzy_suggestion_threshold=fuzzy_suggestion_threshold,
    )
