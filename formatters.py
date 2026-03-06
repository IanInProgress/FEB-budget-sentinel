from __future__ import annotations

from budget_checker import BudgetReport, Status
from utils import format_usd


def _status_prefix(status: Status) -> str:
    # Plain-text first; keep lightweight emoji for quick scanning.
    if status == Status.WITHIN_BUDGET:
        return "✅"
    if status == Status.OVER_BUDGET:
        return "⚠️"
    if status in (Status.ITEM_NOT_FOUND, Status.AMBIGUOUS_MATCH, Status.SUBTEAM_TAB_NOT_FOUND, Status.INVALID_COMMAND):
        return "❌"
    return "❌"


def format_plaintext_report(report: BudgetReport) -> str:
    lines: list[str] = []
    lines.append(f"Status: {_status_prefix(report.status)} {report.status.value}")
    lines.append(f"Reason: {report.reason}")
    lines.append("")
    lines.append(f"Subteam: {report.subteam}")
    lines.append(f'Requested Item: "{report.requested_item}"')
    lines.append(f"Requested Amount: {format_usd(report.requested_amount)}")

    if report.matched_item:
        lines.append(f'Matched Budget Item: "{report.matched_item}"')
    else:
        lines.append("Matched Budget Item: (none)")

    if report.estimated_budget is not None:
        lines.append(f"Estimated Budget: {format_usd(report.estimated_budget)}")
    else:
        lines.append("Estimated Budget: (unknown)")

    if report.actual_spending is not None:
        lines.append(f"Actual Spending: {format_usd(report.actual_spending)}")
    else:
        lines.append("Actual Spending: (unknown)")

    if report.remaining_budget is not None:
        lines.append(f"Remaining Budget: {format_usd(report.remaining_budget)}")
    else:
        lines.append("Remaining Budget: (unknown)")

    if report.candidates:
        lines.append("")
        lines.append("Candidates:")
        for c in report.candidates[:10]:
            lines.append(f"- {c}")
        if len(report.candidates) > 10:
            lines.append(f"- ... and {len(report.candidates) - 10} more")

    if report.suggestions:
        lines.append("")
        lines.append("Did you mean:")
        for s in report.suggestions:
            lines.append(f"- {s}")

    return "\n".join(lines).strip() + "\n"


def format_block_kit(report: BudgetReport) -> dict:
    """
    Optional Block Kit formatter. Keep it simple and compatible.
    """
    fields = [
        {"type": "mrkdwn", "text": f"*Subteam*\n{report.subteam}"},
        {"type": "mrkdwn", "text": f'*Requested Item*\n"{report.requested_item}"'},
        {"type": "mrkdwn", "text": f"*Requested Amount*\n{format_usd(report.requested_amount)}"},
    ]
    if report.matched_item:
        fields.append({"type": "mrkdwn", "text": f'*Matched Budget Item*\n"{report.matched_item}"'})
    if report.estimated_budget is not None:
        fields.append({"type": "mrkdwn", "text": f"*Estimated*\n{format_usd(report.estimated_budget)}"})
    if report.actual_spending is not None:
        fields.append({"type": "mrkdwn", "text": f"*Actual*\n{format_usd(report.actual_spending)}"})
    if report.remaining_budget is not None:
        fields.append({"type": "mrkdwn", "text": f"*Remaining*\n{format_usd(report.remaining_budget)}"})

    blocks: list[dict] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"{_status_prefix(report.status)} {report.status.value}"},
        },
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Reason:* {report.reason}"}},
        {"type": "section", "fields": fields},
    ]

    if report.candidates:
        cand = "\n".join(f"- {c}" for c in report.candidates[:10])
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*Candidates:*\n{cand}"}})
    if report.suggestions:
        sug = "\n".join(f"- {s}" for s in report.suggestions)
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*Did you mean:*\n{sug}"}})

    return {"blocks": blocks}


def format_manager_notification_blocks(report: BudgetReport, user_id: str) -> list[dict]:
    """
    Format budget report as Slack Block Kit blocks for manager channel.
    Compact and visually organized format with conditional layout for long item names.
    """
    blocks: list[dict] = []

    # Header with status
    blocks.append({
        "type": "header",
        "text": {
            "type": "plain_text",
            "text": f"{_status_prefix(report.status)} Purchase Request: {report.status.value}",
        }
    })

    # Check item name length to determine layout
    item_is_long = len(report.requested_item) > 50
    
    if item_is_long:
        # Long item gets its own row for better readability
        blocks.append({
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Requester*\n<@{user_id}>"},
                {"type": "mrkdwn", "text": f"*Subteam*\n{report.subteam}"},
            ]
        })
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Item*\n{report.requested_item}"}
        })
        blocks.append({
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Amount*\n{format_usd(report.requested_amount)}"},
            ]
        })
    else:
        # Short item uses compact 2-column layout
        fields = [
            {"type": "mrkdwn", "text": f"*Requester*\n<@{user_id}>"},
            {"type": "mrkdwn", "text": f"*Subteam*\n{report.subteam}"},
            {"type": "mrkdwn", "text": f"*Item*\n{report.requested_item}"},
            {"type": "mrkdwn", "text": f"*Amount*\n{format_usd(report.requested_amount)}"},
        ]
        blocks.append({"type": "section", "fields": fields})

    # Budget analysis section (only if we have budget data)
    if report.matched_item or report.estimated_budget is not None:
        budget_fields = []
        
        if report.matched_item:
            budget_fields.append({
                "type": "mrkdwn",
                "text": f"*Matched Line*\n{report.matched_item}"
            })
        
        if report.estimated_budget is not None and report.remaining_budget is not None:
            budget_fields.append({
                "type": "mrkdwn",
                "text": f"*Budget Status*\n{format_usd(report.remaining_budget)} remaining of {format_usd(report.estimated_budget)}"
            })
        elif report.estimated_budget is not None:
            budget_fields.append({
                "type": "mrkdwn",
                "text": f"*Estimated Budget*\n{format_usd(report.estimated_budget)}"
            })
        
        if budget_fields:
            blocks.append({"type": "section", "fields": budget_fields})

    # Context/reason (compact single line with context styling)
    blocks.append({
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": f"_{report.reason}_"}]
    })

    # Suggestions/candidates (only if relevant)
    if report.suggestions:
        suggestions_text = ", ".join(report.suggestions[:5])
        if len(report.suggestions) > 5:
            suggestions_text += f" (+{len(report.suggestions) - 5} more)"
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Did you mean:* {suggestions_text}"}
        })
    elif report.candidates:
        candidates_text = ", ".join(report.candidates[:5])
        if len(report.candidates) > 5:
            candidates_text += f" (+{len(report.candidates) - 5} more)"
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Similar items:* {candidates_text}"}
        })

    blocks.append({"type": "divider"})

    return blocks

