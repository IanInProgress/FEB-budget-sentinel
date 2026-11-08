from sheets_client import SheetsClient


class FakeWorksheet:
    def __init__(self, values):
        self.values = values
        self.updated = []
        self.batch_updated = []

    def get_all_values(self):
        return self.values

    def update(self, *, values, range_name):
        self.updated.append((values, range_name))

    def update_cell(self, row, column, value):
        self.updated.append(("cell", row, column, value))

    def batch_update(self, data):
        self.batch_updated.append(data)


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


def test_reverse_unaccounted_spending_clears_only_budget_columns():
    worksheet = FakeWorksheet(
        [
            ["Reference ID", "Specific Items", "Estimated Cost", "Pending Spend", "Amount Reimbursed", "Notes"],
            ["ADMIN-013", "Temporary item", "", "25", "", "Preserve this"],
        ]
    )
    client = make_client(worksheet)

    outcome = client.reverse_approved_spending(
        tab_name="Admin",
        reference_id="ADMIN-013",
        amount=25.0,
        delete_if_empty=True,
    )

    assert outcome == "cleared"
    assert worksheet.updated == [([["", "", "", "", ""]], "A2:E2")]
    assert worksheet.values[1][5] == "Preserve this"


def test_reverse_regular_spending_does_not_clear_item_id_or_name():
    worksheet = FakeWorksheet(
        [
            ["Reference ID", "Specific Items", "Estimated Cost", "Pending Spend", "Amount Reimbursed"],
            ["ADMIN-014", "Planned item", "", "25", ""],
        ]
    )
    client = make_client(worksheet)

    outcome = client.reverse_approved_spending(
        tab_name="Admin",
        reference_id="ADMIN-014",
        amount=25.0,
        delete_if_empty=False,
    )

    assert outcome == "reversed"
    assert worksheet.updated == [("cell", 2, 4, 0.0)]


def test_update_purchase_log_category_updates_all_under_review_lines():
    headers = ["request_id"] + [""] * 22
    row_one = ["REQ-000001", "", "", "under_review", "", "", "Old Tab", "OLD-001"]
    row_one.extend([""] * (23 - len(row_one)))
    row_one[18] = "1"
    row_two = ["REQ-000001", "", "", "under_review", "", "", "Old Tab", "OLD-002"]
    row_two.extend([""] * (23 - len(row_two)))
    row_two[18] = "2"
    worksheet = FakeWorksheet([headers, row_one, row_two])
    client = make_client(worksheet)

    assert client.update_purchase_log_category(
        request_id="REQ-000001",
        bundle_line_numbers=[1, 2],
        subteam="B&O",
        reference_id="BNO-003",
        item_name="New item",
        subteam_available_before=100.0,
        bot_assessment="✅ Approve Recommended",
        line_updates={
            1: ("New item", 100.0, "✅ Approve Recommended"),
            2: ("New item", 90.0, "⚠️ Consider Approval"),
        },
    )

    assert worksheet.batch_updated == [
        [
            {"range": "G2", "values": [["B&O"]]},
            {"range": "H2", "values": [["BNO-003"]]},
            {"range": "I2", "values": [["New item"]]},
            {"range": "N2", "values": [[100.0]]},
            {"range": "S2", "values": [["✅ Approve Recommended"]]},
            {"range": "G3", "values": [["B&O"]]},
            {"range": "H3", "values": [["BNO-003"]]},
            {"range": "I3", "values": [["New item"]]},
            {"range": "N3", "values": [[90.0]]},
            {"range": "S3", "values": [["⚠️ Consider Approval"]]},
        ]
    ]
