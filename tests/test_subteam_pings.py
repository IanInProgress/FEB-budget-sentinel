import pytest
from gspread.exceptions import WorksheetNotFound

from app import _build_subteam_ping_announcement, _parse_subteam_ping_command
from sheets_client import SheetsClient


class FakeWorksheet:
    def __init__(self, rows=None):
        self.rows = rows or []

    def get_all_values(self):
        return [row[:] for row in self.rows]

    def append_row(self, row):
        self.rows.append(list(row))

    def delete_rows(self, row_number):
        del self.rows[row_number - 1]


class FakeSpreadsheet:
    def __init__(self):
        self.worksheets = {}

    def worksheet(self, title):
        try:
            return self.worksheets[title]
        except KeyError as e:
            raise WorksheetNotFound(title) from e

    def add_worksheet(self, *, title, rows, cols):
        worksheet = FakeWorksheet()
        self.worksheets[title] = worksheet
        return worksheet


def build_sheets_client():
    client = SheetsClient.__new__(SheetsClient)
    client._sh = FakeSpreadsheet()
    return client


def test_parse_subteam_ping_commands_normalizes_prefix():
    assert _parse_subteam_ping_command("add eecs") == ("add", "EECS")
    assert _parse_subteam_ping_command("remove EECS") == ("remove", "EECS")
    assert _parse_subteam_ping_command("list") == ("list", None)


@pytest.mark.parametrize("text", ["", "add", "add UNKNOWN", "list EECS", "remove EECS extra"])
def test_parse_subteam_ping_command_rejects_invalid_input(text):
    with pytest.raises(ValueError):
        _parse_subteam_ping_command(text)


def test_subteam_ping_subscriptions_are_idempotent_and_removable():
    sheets = build_sheets_client()

    assert sheets.add_subteam_ping_subscriber(subteam_prefix="eecs", user_id="U123") is True
    assert sheets.add_subteam_ping_subscriber(subteam_prefix="EECS", user_id="U123") is False
    assert sheets.get_subteam_ping_subscribers() == {"EECS": {"U123"}}
    assert sheets.remove_subteam_ping_subscriber(subteam_prefix="eecs", user_id="U123") is True
    assert sheets.remove_subteam_ping_subscriber(subteam_prefix="EECS", user_id="U123") is False
    assert sheets.get_subteam_ping_subscribers() == {}


def test_watcher_announcement_deduplicates_across_bulk_subteams():
    result = _build_subteam_ping_announcement(
        [
            {"subteam_tab": "EECS"},
            {"subteam_tab": "Powertrain"},
            {"subteam_tab": "EECS"},
        ],
        {"EECS": {"U123", "U456"}, "POWER": {"U123"}},
    )

    assert result is not None
    text, block = result
    assert text.count("<@U123>") == 1
    assert "<@U123> (EECS, POWER)" in text
    assert "<@U456> (EECS)" in text
    assert block["text"]["text"] == f"*{text}*"


def test_watcher_announcement_omitted_without_subscribers():
    assert _build_subteam_ping_announcement(
        [{"subteam_tab": "EECS"}],
        {},
    ) is None