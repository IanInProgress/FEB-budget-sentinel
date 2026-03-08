from __future__ import annotations

from budget_checker import BudgetReport, Status
from utils import format_usd


def _status_prefix(status: Status) -> str:
    if status == Status.WITHIN_BUDGET:
        return "✅"
    if status == Status.OVER_BUDGET:
        return "⚠️"
    if status == Status.UNACCOUNTED_ITEM:
        return "🆕"
    if status in (Status.ITEM_NOT_FOUND, Status.DATA_ERROR, Status.INVALID_COMMAND):
        return "❌"
    return "❌"


def _recommendation_header(report: BudgetReport) -> str:
    team_budget_known = report.available_budget is not None
    team_within = (
        team_budget_known and report.requested_amount <= float(report.available_budget) + 1e-9
    )

    if report.status in (Status.ITEM_NOT_FOUND, Status.DATA_ERROR, Status.INVALID_COMMAND):
        return "❌ RECOMMEND_MANUAL_REVIEW"

    if report.status == Status.OVER_BUDGET:
        if team_budget_known and not team_within:
            return "❌ RECOMMEND_REJECT"
        if team_within:
            return "⚠️ RECOMMEND_CONSIDER_APPROVAL"
        return "⚠️ RECOMMEND_CONSIDER_APPROVAL"

    if report.status == Status.WITHIN_BUDGET:
        if team_within:
            return "✅ RECOMMEND_APPROVE"
        if team_budget_known and not team_within:
            return "❌ RECOMMEND_REJECT"
        return "⚠️ RECOMMEND_CONSIDER_APPROVAL"

    if report.status == Status.UNACCOUNTED_ITEM:
        if team_within:
            return "✅ RECOMMEND_APPROVE"
        if team_budget_known and not team_within:
            return "❌ RECOMMEND_REJECT"
        return "⚠️ RECOMMEND_CONSIDER_APPROVAL"

    return f"{_status_prefix(report.status)} RECOMMEND_MANUAL_REVIEW"


def format_manager_notification_blocks(
    report: BudgetReport, 
    user_id: str, 
    request_id: str | None = None,
    purchase_reason: str | None = None
) -> list[dict]:
    """
    Format budget report as Slack Block Kit blocks for manager channel.
    """
    blocks: list[dict] = []

    # Header with request ID and status
    header_text = _recommendation_header(report)
    if request_id:
        header_text = f"{request_id}: {header_text}"
    
    blocks.append({
        "type": "header",
        "text": {
            "type": "plain_text",
            "text": header_text,
        }
    })

    # Main info
    fields = [
        {"type": "mrkdwn", "text": f"*Requester*\n<@{user_id}>"},
        {"type": "mrkdwn", "text": f"*Subteam*\n{report.subteam}"},
        {"type": "mrkdwn", "text": f"*Reference ID*\n{report.reference_id}"},
        {"type": "mrkdwn", "text": f"*Item*\n{report.item_name}"},
        {"type": "mrkdwn", "text": f"*Amount*\n{format_usd(report.requested_amount)}"},
    ]
    blocks.append({"type": "section", "fields": fields})
    
    # Add purchase reason if provided
    if purchase_reason:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Reason:* {purchase_reason}"}
        })

    # Item budget section - show item's remaining budget before/after (non-unaccounted items only)
    if report.status != Status.UNACCOUNTED_ITEM and report.remaining_budget is not None:
        after_approval_item_remaining = report.remaining_budget - report.requested_amount
        
        item_budget_fields = [
            {
                "type": "mrkdwn",
                "text": f"*Item Budget Remaining (Current)*\n{format_usd(report.remaining_budget)}"
            },
            {
                "type": "mrkdwn",
                "text": f"*Item Budget Remaining (After Approval)*\n{format_usd(after_approval_item_remaining)}"
            },
        ]
        blocks.append({"type": "section", "fields": item_budget_fields})

    # Subteam budget section - show available budget
    if report.available_budget is not None:
        # Calculate after-approval budget
        after_approval_budget = report.available_budget - report.requested_amount
        
        budget_fields = [
            {
                "type": "mrkdwn",
                "text": f"*Subteam Available Budget (Current)*\n{format_usd(report.available_budget)}"
            },
            {
                "type": "mrkdwn",
                "text": f"*Subteam Available Budget (After Approval)*\n{format_usd(after_approval_budget)}"
            },
        ]
        blocks.append({"type": "section", "fields": budget_fields})

    # Reason
    blocks.append({
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": f"_{report.reason}_"}]
    })

    blocks.append({"type": "divider"})

    return blocks

