from sheets_client import SheetsClient


class FakeWorksheet:
    def __init__(self, values):
        self.values = values
        self.updated = []

    def get_all_values(self):
        return self.values

    def update(self, *, values, range_name):
        self.updated.append((values, range_name))


class FakeSpreadsheet:
    def __init__(self, worksheet):
        self.worksheet_value = worksheet

    def worksheet(self, tab_name):
        return self.worksheet_value


def make_client(worksheet):
    client = SheetsClient.__new__(SheetsClient)
    client._sh = FakeSpreadsheet(worksheet)
    client._cache = {}
    return client


def test_append_budget_line_writes_after_last_reference_id_row():
    worksheet = FakeWorksheet(
        [
            ["Reference ID", "Specific Items", "Estimated Cost", "Pending Spend", "Amount Reimbursed", "", "Summary"],
            ["DYN-001", "Existing item", "$100", "", "", "", "Budget Remaining"],
            ["DYN-004", "Another item", "$200", "", "", "", "100%"],
            ["", "", "", "", "", "", "More summary"],
        ]
    )
    client = make_client(worksheet)

    reference_id = client.append_budget_line(
        tab_name="Dynamics",
        item_name="New item",
        initial_spending=345.0,
    )

    assert reference_id == "DYN-005"
    assert worksheet.updated == [
        (
            [["DYN-005", "New item", "", 345.0]],
            "A4:D4",
        )
    ]
