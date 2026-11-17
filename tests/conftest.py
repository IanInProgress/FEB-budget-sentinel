import contextlib
import importlib.abc
import importlib.machinery
import os
import sys
from pathlib import Path
from unittest import mock


# Ensure repo root (feb_purchase_bot_py/) is importable in tests.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("CHIEF_USERGROUP_ID", "S_CHIEF")
os.environ.setdefault("PRESIDENT_VP_USERGROUP_ID", "S_PRESIDENT_VP")


# `app` builds the Slack app and Google clients at import time. Whenever a test
# imports it, run that import with fake settings and stubbed clients, so tests
# never read a developer's .env or reach the real Slack workspace, budget
# spreadsheet, or Drive folder. Tests that don't import `app` never load it.
_FAKE_APP_ENV = {
    "SLACK_BOT_TOKEN": "xoxb-test-token",
    "SLACK_SIGNING_SECRET": "test-signing-secret",
    "MANAGER_CHANNEL_ID": "C_TEST_MANAGERS",
    "CHIEF_USERGROUP_ID": "S_CHIEF",
    "PRESIDENT_VP_USERGROUP_ID": "S_PRESIDENT_VP",
    "GOOGLE_SHEET_ID": "test-sheet-id",
    "GOOGLE_SERVICE_ACCOUNT_JSON": "{}",
    "GOOGLE_SERVICE_ACCOUNT_FILE": "",
    "GOOGLE_DRIVE_OAUTH_CLIENT_FILE": "",
    "GOOGLE_DRIVE_OAUTH_TOKEN_FILE": "",
    "ENABLE_MANAGER_DECISION_SCANNER": "false",
}


def _fake_auth_test(client, **_kwargs):
    from slack_sdk.web.slack_response import SlackResponse

    return SlackResponse(
        client=client,
        http_verb="POST",
        api_url="https://slack.com/api/auth.test",
        req_args={},
        data={"ok": True, "user_id": "U_TEST_BOT", "bot_id": "B_TEST_BOT", "team_id": "T_TEST"},
        headers={},
        status_code=200,
    )


def _fake_sheets_init(self, *, spreadsheet_id, **_kwargs):
    self._spreadsheet_id = spreadsheet_id
    self._cache_ttl_seconds = 30
    self._cache = {}
    self._gc = None
    self._sh = None


@contextlib.contextmanager
def _offline_app_import():
    import receipts
    import sheets_client

    def _no_drive(self, *_args, **_kwargs):
        raise receipts.ReceiptStorageError("Drive is disabled in tests")

    with mock.patch.dict(os.environ, _FAKE_APP_ENV), \
            mock.patch("config.load_dotenv", lambda *a, **k: False), \
            mock.patch("slack_sdk.WebClient.auth_test", _fake_auth_test), \
            mock.patch.object(sheets_client.SheetsClient, "__init__", _fake_sheets_init), \
            mock.patch.object(sheets_client.SheetsClient, "ensure_purchase_log_schema_on_startup", lambda self: None), \
            mock.patch.object(sheets_client.SheetsClient, "ensure_reimbursements_log_schema_on_startup", lambda self: None), \
            mock.patch.object(receipts.ReceiptDriveStorage, "__init__", _no_drive):
        yield


class _OfflineAppFinder(importlib.abc.MetaPathFinder):
    """Runs the import of the top-level `app` module inside `_offline_app_import`."""

    def find_spec(self, fullname, path, target=None):
        if fullname != "app":
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is None or spec.loader is None:
            return spec
        exec_module = spec.loader.exec_module

        def exec_module_offline(module):
            with _offline_app_import():
                exec_module(module)

        spec.loader.exec_module = exec_module_offline
        return spec


sys.meta_path.insert(0, _OfflineAppFinder())
