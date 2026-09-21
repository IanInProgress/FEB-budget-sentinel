from __future__ import annotations

import os
import json
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
    google_drive_receipts_folder_id: str | None
    google_drive_oauth_client_file: str | None
    google_drive_oauth_client_json: str | None
    google_drive_oauth_token_file: str | None
    google_drive_oauth_token_json: str | None
    subteam_sheet_ids: dict[str, str]

    log_level: str
    slack_commands_path: str
    port: int

    item_budget_reject_threshold_percent_of_estimate: float


class ConfigError(RuntimeError):
    pass


DEFAULT_SUBTEAM_SHEET_IDS = {
    "ADMIN": "1Z7y1sPVjgrh1Hv7p2G0KaLnbVfIGrmkJQsjd-b6t9DY",
    "DYNA": "1L7SjtR1RvmiM17HxsfWdoZ3-pcmCSnaN7Co-A6TqDiw",
    "CHAS": "1FMFwzHHR84jdmrzYFAouLnnT3BdDF3f7-fi79jCKLo4",
    "POWER": "1U7KJK-2KAj52SdZp-qlLJUPIdoORTjQ5zcz6Iey74EU",
    "COMP": "1SBUZjz8c_lJOB8mIUXVYJSsyxbfmZrD1_8ZcuKzf1Bs",
    "ERGO": "1zr_TupBRiXqdV5d-W7mHb40kIvQgq9SWHWVZM_bHgMg",
    "MECH": "1fO9CJElk0blio6DnT1a-BrrK2TxvQ7PZu3TWkDiH8wI",
    "EECS": "1YnRsW5ziCin71a2jQqqKW1-4F_s242VbWitlAbfkJyE",
    "AERO": "1SVcnrRdyfITd0HMP62KiyggGwdCdpyAH6dDHo8Jewec",
    "AUTO": "1ew1-L-yC7kOR2oaHbScDm50rIf18rCOTmD7RwG6KBR8",
    "MANU": "1u82aDfkjrZvCtxUyhSKDM7hI-KUNGvWOpn92_qvHh3Y",
    "BNO": "1U9Rv0fid8s_HP3DFkk2o478mEDhqAZxh6_LF-sYbGF4",
    "TRAN": "1AdiOUPQwTjeavkYYxxaqKlsoMNDYxMLafngPIAUG5Nc",
    "SIMS": "154Ci21FWA_2eCCKa17A3ASJUzC75nci6AKwQsdSKvGc",
}


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


def _parse_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as e:
        raise ConfigError(f"Invalid number for {name}: {raw}") from e
    return value


def load_settings(*, load_env: bool = True) -> Settings:
    if load_env:
        load_dotenv()

    slack_bot_token = _require_env("SLACK_BOT_TOKEN")
    slack_signing_secret = _require_env("SLACK_SIGNING_SECRET")
    manager_channel_id = (
        os.getenv("MANAGER_CHANNEL_ID", "").strip()
        or os.getenv("SLACK_MANAGER_CHANNEL_ID", "").strip()
    )
    if not manager_channel_id:
        raise ConfigError(
            "Missing required environment variable: MANAGER_CHANNEL_ID "
            "(or SLACK_MANAGER_CHANNEL_ID for backward compatibility)"
        )

    google_sheet_id = _require_env("GOOGLE_SHEET_ID")
    google_service_account_file = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "").strip() or None
    google_service_account_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip() or None
    google_drive_receipts_folder_id = os.getenv("GOOGLE_DRIVE_RECEIPTS_FOLDER_ID", "").strip() or None
    google_drive_oauth_client_file = os.getenv("GOOGLE_DRIVE_OAUTH_CLIENT_FILE", "").strip() or None
    google_drive_oauth_client_json = os.getenv("GOOGLE_DRIVE_OAUTH_CLIENT_JSON", "").strip() or None
    google_drive_oauth_token_file = os.getenv("GOOGLE_DRIVE_OAUTH_TOKEN_FILE", "").strip() or None
    google_drive_oauth_token_json = os.getenv("GOOGLE_DRIVE_OAUTH_TOKEN_JSON", "").strip() or None
    if not google_service_account_file and not google_service_account_json:
        raise ConfigError(
            "Provide GOOGLE_SERVICE_ACCOUNT_FILE or GOOGLE_SERVICE_ACCOUNT_JSON for Sheets access."
        )

    subteam_sheet_ids_raw = os.getenv("SUBTEAM_SHEET_IDS_JSON", "").strip()
    subteam_sheet_ids = dict(DEFAULT_SUBTEAM_SHEET_IDS)
    if subteam_sheet_ids_raw:
        subteam_sheet_ids = {}
        try:
            parsed_subteam_sheet_ids = json.loads(subteam_sheet_ids_raw)
        except json.JSONDecodeError as e:
            raise ConfigError("SUBTEAM_SHEET_IDS_JSON must be valid JSON") from e
        if not isinstance(parsed_subteam_sheet_ids, dict):
            raise ConfigError("SUBTEAM_SHEET_IDS_JSON must be a JSON object")
        for prefix, spreadsheet_id in parsed_subteam_sheet_ids.items():
            normalized_prefix = str(prefix).strip().upper()
            normalized_id = str(spreadsheet_id).strip()
            if not normalized_prefix or not normalized_id:
                raise ConfigError("SUBTEAM_SHEET_IDS_JSON cannot contain empty keys or values")
            subteam_sheet_ids[normalized_prefix] = normalized_id

    log_level = os.getenv("LOG_LEVEL", "INFO").strip() or "INFO"
    slack_commands_path = os.getenv("SLACK_COMMANDS_PATH", "/slack/commands").strip() or "/slack/commands"
    port = _parse_int("PORT", 3000)

    item_budget_reject_threshold_percent_of_estimate = _parse_float(
        "ITEM_BUDGET_REJECT_THRESHOLD_PERCENT",
        125.0,
    )
    if item_budget_reject_threshold_percent_of_estimate < 0:
        raise ConfigError("ITEM_BUDGET_REJECT_THRESHOLD_PERCENT must be >= 0")

    return Settings(
        slack_bot_token=slack_bot_token,
        slack_signing_secret=slack_signing_secret,
        manager_channel_id=manager_channel_id,
        google_sheet_id=google_sheet_id,
        google_service_account_file=google_service_account_file,
        google_service_account_json=google_service_account_json,
        google_drive_receipts_folder_id=google_drive_receipts_folder_id,
        google_drive_oauth_client_file=google_drive_oauth_client_file,
        google_drive_oauth_client_json=google_drive_oauth_client_json,
        google_drive_oauth_token_file=google_drive_oauth_token_file,
        google_drive_oauth_token_json=google_drive_oauth_token_json,
        subteam_sheet_ids=subteam_sheet_ids,
        log_level=log_level,
        slack_commands_path=slack_commands_path,
        port=port,
        item_budget_reject_threshold_percent_of_estimate=item_budget_reject_threshold_percent_of_estimate,
    )
