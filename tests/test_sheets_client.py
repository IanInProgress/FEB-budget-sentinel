from sheets_client import SheetsClient


class FakeWorksheet:
    id = 123456


class FakeSpreadsheet:
    def worksheet(self, tab_name):
        assert tab_name == "Accumulator MechE"
        return FakeWorksheet()


def test_get_budget_tab_url_uses_worksheet_gid():
    client = object.__new__(SheetsClient)
    client._spreadsheet_id = "master-sheet-id"
    client._sh = FakeSpreadsheet()

    assert client.get_budget_tab_url(tab_name="Accumulator MechE") == (
        "https://docs.google.com/spreadsheets/d/master-sheet-id/edit#gid=123456"
    )
