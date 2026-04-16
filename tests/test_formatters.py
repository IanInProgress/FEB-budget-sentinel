from budget_checker import BudgetReport, Status
from formatters import _recommendation_header, format_reference_lookup_dm


def _report(
    *,
    status: Status,
    requested_amount: float,
    estimated_budget: float | None,
    actual_spending: float | None,
    available_budget: float | None,
    remaining_budget: float | None,
) -> BudgetReport:
    return BudgetReport(
        status=status,
        reason="test",
        subteam="Admin",
        reference_id="ADMIN-001",
        item_name="Office supplies",
        requested_amount=requested_amount,
        estimated_budget=estimated_budget,
        actual_spending=actual_spending,
        remaining_budget=remaining_budget,
        available_budget=available_budget,
    )


def test_recommend_reject_when_subteam_budget_exceeded():
    report = _report(
        status=Status.WITHIN_BUDGET,
        requested_amount=120.0,
        estimated_budget=500.0,
        actual_spending=100.0,
        available_budget=100.0,
        remaining_budget=400.0,
    )
    assert _recommendation_header(report) == "❌ Reject Recommended"


def test_recommend_approve_when_within_subteam_and_below_25_percent_over_item_budget():
    report = _report(
        status=Status.OVER_BUDGET,
        requested_amount=20.0,
        estimated_budget=100.0,
        actual_spending=90.0,
        available_budget=200.0,
        remaining_budget=10.0,
    )
    assert _recommendation_header(report) == "✅ Approve Recommended"


def test_recommend_reject_at_exact_25_percent_over_item_budget_threshold():
    report = _report(
        status=Status.OVER_BUDGET,
        requested_amount=35.0,
        estimated_budget=100.0,
        actual_spending=90.0,
        available_budget=200.0,
        remaining_budget=10.0,
    )
    assert _recommendation_header(report) == "❌ Reject Recommended"


def test_recommend_reject_for_within_item_budget_if_total_reaches_25_percent_over_estimate():
    report = _report(
        status=Status.WITHIN_BUDGET,
        requested_amount=25.0,
        estimated_budget=100.0,
        actual_spending=100.0,
        available_budget=200.0,
        remaining_budget=0.0,
    )
    assert _recommendation_header(report) == "❌ Reject Recommended"


def test_unaccounted_item_still_uses_subteam_budget_rule():
    report = _report(
        status=Status.UNACCOUNTED_ITEM,
        requested_amount=20.0,
        estimated_budget=None,
        actual_spending=None,
        available_budget=200.0,
        remaining_budget=None,
    )
    assert _recommendation_header(report) == "✅ Approve Recommended"


def test_custom_threshold_percent_changes_recommendation():
    report = _report(
        status=Status.OVER_BUDGET,
        requested_amount=20.0,
        estimated_budget=100.0,
        actual_spending=90.0,
        available_budget=200.0,
        remaining_budget=10.0,
    )
    assert _recommendation_header(report, item_budget_reject_threshold_percent_of_estimate=105.0) == "❌ Reject Recommended"


def test_format_reference_lookup_dm_table_includes_rows():
    text = format_reference_lookup_dm(
        prefix="MECH",
        tab_name="Accumulator MechE",
        rows=[
            ("MECH-001", "Accumulator Box"),
            ("MECH-010", "Busbar Set"),
        ],
    )

    assert "Reference list for MECH (Accumulator MechE)" in text
    assert "| Reference ID | Item Name       |" in text
    assert "| MECH-001     | Accumulator Box |" in text
    assert "| MECH-010     | Busbar Set      |" in text


def test_format_reference_lookup_dm_when_no_rows():
    text = format_reference_lookup_dm(prefix="MANU", tab_name="Manufacturing", rows=[])

    assert text == "No reference IDs found for *MANU* (Manufacturing)."
