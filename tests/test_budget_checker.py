from budget_checker import BudgetLine, Status, build_budget_report


def test_exact_match_within_budget():
    lines = [
        BudgetLine(
            reference_id="ADMIN-001",
            item_name="Office supplies",
            estimated_budget=500.0,
            actual_spending=100.0
        ),
    ]
    report = build_budget_report(
        subteam="Admin",
        reference_id="ADMIN-001",
        item_name="Office supplies",
        requested_amount=200.0,
        lines=lines,
    )
    assert report.status == Status.WITHIN_BUDGET
    assert report.remaining_budget == 400.0
    assert report.reference_id == "ADMIN-001"


def test_over_budget():
    lines = [
        BudgetLine(
            reference_id="EECS-001",
            item_name="Microcontroller",
            estimated_budget=100.0,
            actual_spending=90.0
        ),
    ]
    report = build_budget_report(
        subteam="EECS",
        reference_id="EECS-001",
        item_name="Microcontroller",
        requested_amount=20.0,
        lines=lines,
    )
    assert report.status == Status.OVER_BUDGET
    assert report.remaining_budget == 10.0


def test_item_not_found():
    lines = [
        BudgetLine(
            reference_id="ADMIN-001",
            item_name="Supplies",
            estimated_budget=500.0,
            actual_spending=0.0
        ),
    ]
    report = build_budget_report(
        subteam="Admin",
        reference_id="ADMIN-999",
        item_name="Unknown",
        requested_amount=100.0,
        lines=lines,
    )
    assert report.status == Status.ITEM_NOT_FOUND


def test_case_insensitive_id_match():
    lines = [
        BudgetLine(
            reference_id="ADMIN-001",
            item_name="Supplies",
            estimated_budget=500.0,
            actual_spending=0.0
        ),
    ]
    report = build_budget_report(
        subteam="Admin",
        reference_id="admin-001",
        item_name="Supplies",
        requested_amount=100.0,
        lines=lines,
    )
    assert report.status == Status.WITHIN_BUDGET


def test_unaccounted_item():
    lines = [
        BudgetLine(
            reference_id="ADMIN-001",
            item_name="Supplies",
            estimated_budget=500.0,
            actual_spending=0.0
        ),
    ]
    report = build_budget_report(
        subteam="Admin",
        reference_id="ADMIN-000",
        item_name="Toilet Paper",
        requested_amount=25.0,
        lines=lines,
        is_unaccounted=True,
    )
    assert report.status == Status.UNACCOUNTED_ITEM
    assert report.item_name == "Toilet Paper"
    assert report.estimated_budget is None
    assert report.remaining_budget is None


