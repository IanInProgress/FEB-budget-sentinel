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

