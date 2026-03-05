from budget_checker import BudgetLine, Status, build_budget_report, find_budget_match


def test_exact_match_within_budget():
    lines = [
        BudgetLine(item_name="CAN transceiver", estimated_budget=100.0, actual_spending=40.0),
    ]
    report = build_budget_report(
        subteam="Electronics",
        requested_item="CAN transceiver",
        requested_amount=42.5,
        lines=lines,
        fuzzy_suggestion_threshold=90,
    )
    assert report.status == Status.WITHIN_BUDGET
    assert report.remaining_budget == 60.0


def test_over_budget():
    lines = [
        BudgetLine(item_name="Rod ends", estimated_budget=200.0, actual_spending=190.0),
    ]
    report = build_budget_report(
        subteam="Suspension",
        requested_item="rod ends",
        requested_amount=20.0,
        lines=lines,
    )
    assert report.status == Status.OVER_BUDGET
    assert report.remaining_budget == 10.0


def test_normalized_match_ignores_punctuation_and_spaces():
    lines = [
        BudgetLine(item_name="IMU, mount  bracket", estimated_budget=50.0, actual_spending=0.0),
    ]
    match = find_budget_match(requested_item="imu mount bracket", lines=lines)
    assert match.status == Status.WITHIN_BUDGET
    assert match.matched is not None
    assert match.matched.item_name == "IMU, mount  bracket"


def test_item_not_found():
    lines = [
        BudgetLine(item_name="Something else", estimated_budget=10.0, actual_spending=0.0),
    ]
    report = build_budget_report(
        subteam="Electronics",
        requested_item="CAN transceiver",
        requested_amount=1.0,
        lines=lines,
        fuzzy_suggestion_threshold=100,  # make suggestions unlikely in tests
    )
    assert report.status == Status.ITEM_NOT_FOUND


def test_ambiguous_match_on_duplicates():
    lines = [
        BudgetLine(item_name="Rod Ends", estimated_budget=50.0, actual_spending=0.0),
        BudgetLine(item_name="rod ends", estimated_budget=75.0, actual_spending=0.0),
    ]
    report = build_budget_report(
        subteam="Suspension",
        requested_item="rod ends",
        requested_amount=5.0,
        lines=lines,
    )
    assert report.status == Status.AMBIGUOUS_MATCH
    assert len(report.candidates) == 2

