from __future__ import annotations

from budget_checker import BudgetReport, Status
from utils import format_usd


def format_reference_lookup_dm(*, prefix: str, tab_name: str, rows: list[tuple[str, str]]) -> str:
    """
    Build a DM-friendly monospace table of reference IDs and item names.
    """
    if not rows:
        return f"No reference IDs found for *{prefix}* ({tab_name})."

    cleaned_rows = [(ref_id.strip(), item_name.strip()) for ref_id, item_name in rows]

    header_ref = "Reference ID"
    header_item = "Item Name"
    ref_width = max(len(header_ref), *(len(ref_id) for ref_id, _ in cleaned_rows))
    item_width = max(len(header_item), *(len(item_name) for _, item_name in cleaned_rows))

    def _line(left: str, right: str) -> str:
        return f"| {left.ljust(ref_width)} | {right.ljust(item_width)} |"

    divider = f"| {'-' * ref_width} | {'-' * item_width} |"
    table_lines = [_line(header_ref, header_item), divider]
    table_lines.extend(_line(ref_id, item_name) for ref_id, item_name in cleaned_rows)
    table = "\n".join(table_lines)

    return f"*Reference list for {prefix} ({tab_name})*\n```\n{table}\n```"


def _status_prefix(status: Status) -> str:
    if status == Status.WITHIN_BUDGET:
        return "✅"
    if status == Status.OVER_BUDGET:
        return "⚠️"
    if status == Status.UNACCOUNTED_ITEM:
        return "🆕"
    if status in (Status.ITEM_NOT_FOUND, Status.DATA_ERROR, Status.INVALID_COMMAND):
        return "❌"
    return "⚠️"


def _accounted_item_exceeds_reject_threshold(
    report: BudgetReport,
    item_budget_reject_threshold_percent_of_estimate: float = 125.0,
) -> bool:
    if report.estimated_budget is None or report.actual_spending is None:
        return False
    projected_total_spend = float(report.actual_spending) + float(report.requested_amount)
    reject_threshold = float(report.estimated_budget) * (
        float(item_budget_reject_threshold_percent_of_estimate) / 100.0
    )
    return projected_total_spend >= reject_threshold - 1e-9


def _recommendation_header(
    report: BudgetReport,
    item_budget_reject_threshold_percent_of_estimate: float = 125.0,
) -> str:
    team_budget_known = report.available_budget is not None
    team_within = (
        team_budget_known and report.requested_amount <= float(report.available_budget) + 1e-9
    )

    if report.status in (Status.ITEM_NOT_FOUND, Status.DATA_ERROR, Status.INVALID_COMMAND):
        return "❌ Manual Review Required"

    if report.status == Status.OVER_BUDGET:
        if team_budget_known and not team_within:
            return "❌ Reject Recommended"
        if team_within:
            if _accounted_item_exceeds_reject_threshold(report, item_budget_reject_threshold_percent_of_estimate):
                return "❌ Reject Recommended"
            return "✅ Approve Recommended"
        return "⚠️ Consider Approval"

    if report.status == Status.WITHIN_BUDGET:
        if team_budget_known and not team_within:
            return "❌ Reject Recommended"
        if team_within:
            if _accounted_item_exceeds_reject_threshold(report, item_budget_reject_threshold_percent_of_estimate):
                return "❌ Reject Recommended"
            return "✅ Approve Recommended"
        return "⚠️ Consider Approval"

    if report.status == Status.UNACCOUNTED_ITEM:
        if team_within:
            return "✅ Approve Recommended"
        if team_budget_known and not team_within:
            return "❌ Reject Recommended"
        return "⚠️ Consider Approval"

    return f"{_status_prefix(report.status)} Manual Review Required"


def _bundle_recommendation_header(item_headers: list[str]) -> str:
    if any("Reject Recommended" in header for header in item_headers):
        return "❌ Reject Recommended"
    if any("Manual Review Required" in header or "Consider Approval" in header for header in item_headers):
        return "⚠️ Consider Approval"
    return "✅ Approve Recommended"


def format_manager_notification_blocks(
    report: BudgetReport,
    user_id: str,
    request_id: str | None = None,
    purchase_reason: str | None = None,
    item_budget_reject_threshold_percent_of_estimate: float = 125.0,
) -> list[dict]:
    """
    Format budget report as Slack Block Kit blocks for manager channel.
    """
    blocks: list[dict] = []

    # Header: "REQ-000001 · ✅ Approve Recommended"
    rec = _recommendation_header(report, item_budget_reject_threshold_percent_of_estimate)
    header_text = f"{request_id} · {rec}" if request_id else rec
    blocks.append({
        "type": "header",
        "text": {"type": "plain_text", "text": header_text},
    })

    # Key details on one line: requester · amount · ref ID
    blocks.append({
        "type": "section",
        "text": {
            "type": "mrkdwn",
            "text": f"<@{user_id}>  ·  *{format_usd(report.requested_amount)}*  ·  `{report.reference_id}`",
        },
    })

    # Item name + purchase reason (full-width so long names don't get clipped)
    item_lines = [f"*Item:* {report.item_name}"]
    if purchase_reason:
        item_lines.append(f"*Reason:* {purchase_reason}")
    blocks.append({
        "type": "section",
        "text": {"type": "mrkdwn", "text": "\n".join(item_lines)},
    })

    # Budget impact — compact "before → after" per row
    budget_fields: list[dict] = []
    if report.status != Status.UNACCOUNTED_ITEM and report.remaining_budget is not None:
        after_item = report.remaining_budget - report.requested_amount
        after_item_str = format_usd(after_item)
        if after_item < 0:
            after_item_str += " ⚠️"
        budget_fields.append({
            "type": "mrkdwn",
            "text": f"*Item Budget Remaining*\n{format_usd(report.remaining_budget)} → {after_item_str}",
        })
    if report.available_budget is not None:
        after_subteam = report.available_budget - report.requested_amount
        after_subteam_str = format_usd(after_subteam)
        if after_subteam < 0:
            after_subteam_str += " ⚠️"
        budget_fields.append({
            "type": "mrkdwn",
            "text": f"*Available Budget*\n{format_usd(report.available_budget)} → {after_subteam_str}",
        })
    if budget_fields:
        blocks.append({"type": "section", "fields": budget_fields})

    # Bot assessment — small gray context text at the bottom
    blocks.append({
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": f"_{report.reason}_"}],
    })

    blocks.append({"type": "divider"})
    return blocks


def format_manager_bundle_notification_blocks(
    bundle_items: list[dict],
    user_id: str,
    request_id: str,
    total_amount: float,
    item_budget_reject_threshold_percent_of_estimate: float = 125.0,
) -> list[dict]:
    """
    Format a multi-item purchase bundle for manager review.
    Expects bundle_items with keys: line_number, report, reason.
    """
    item_headers = [
        _recommendation_header(item["report"], item_budget_reject_threshold_percent_of_estimate)
        for item in bundle_items
    ]
    bundle_header = f"{request_id} · {_bundle_recommendation_header(item_headers)}"
    n = len(bundle_items)

    blocks: list[dict] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": bundle_header},
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"<@{user_id}>  ·  *{format_usd(total_amount)}*  ·  {n} item{'s' if n != 1 else ''}",
            },
        },
        {"type": "divider"},
    ]

    for i, (item, header_text) in enumerate(zip(bundle_items, item_headers)):
        report = item["report"]
        line_number = item["line_number"]
        reason = item.get("reason") or ""

        # Per-item sub-header: "Line 1 of 3 · ✅ Approve Recommended"
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Item {line_number} of {n}* · {header_text}",
            },
        })

        # Ref ID · amount · item name on one line
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"`{report.reference_id}`  ·  *{format_usd(report.requested_amount)}*  ·  {report.item_name}",
            },
        })

        # Reason + bot assessment, both compact in a context block
        context_parts: list[str] = []
        if reason:
            context_parts.append(f"Reason: _{reason}_")
        if report.reason:
            context_parts.append(f"Assessment: _{report.reason}_")
        if context_parts:
            blocks.append({
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": "\n".join(context_parts)}],
            })

        # Divider between items, but not after the last one
        if i < n - 1:
            blocks.append({"type": "divider"})

    blocks.append({"type": "divider"})
    return blocks

