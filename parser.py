from __future__ import annotations

import re
from dataclasses import dataclass

from budget_checker import Status
from utils import coerce_money


# Mapping: Reference ID prefix → Tab name
REFERENCE_ID_PREFIX_TO_TAB = {
    "ADMIN": "Admin",
    "DYNA": "Dynamics",
    "CHAS": "Chassis",
    "POWER": "Powertrain",
    "COMP": "Composites",
    "ERGO": "Brakes/Ergo",
    "MECH": "Accumulator MechE",
    "EECS": "EECS",
    "AERO": "Aero",
    "AUTO": "Auto",
    "MANU": "Manufacturing",
}

# Match: [REF-ID Item Name], amount, reason  OR  [REF-ID], amount, reason
_CMD_RE = re.compile(
    r'^\s*(?P<ref_id>[A-Za-z0-9_-]+)(?:\s+(?P<item_name>[^,]+))?\s*,\s*(?P<amount>\$?-?\d+(?:\.\d+)?)\s*,\s*(?P<reason>.+?)\s*$'
)


@dataclass(frozen=True)
class ParseResult:
    ok: bool
    status: Status
    error_message: str | None
    reference_id: str | None
    subteam_tab: str | None
    requested_amount: float | None
    reason: str | None
    is_unaccounted: bool = False
    provided_item_name: str | None = None
    line_number: int | None = None


@dataclass(frozen=True)
class BulkOrderParseResult:
    ok: bool
    status: Status
    error_message: str | None
    items: list[ParseResult]


def _single_item_usage(command_keyword: str) -> str:
    return (
        "Usage: Open the request form and enter:\n"
        f"  {command_keyword} <reference_id>, <amount>, <reason>\n"
        f"  Example: {command_keyword} ADMIN-001, 50.00, Need for supplies\n"
        "  For unaccounted items: ADMIN-000 Item Name, <amount>, <reason>\n"
        f"  Example: {command_keyword} ADMIN-000 Toilet Paper, 25.00, Need for office\n"
        "  [After Review, upload receipt image in channel and click Confirm]"
    )


def _bulk_order_usage(command_keyword: str) -> str:
    return (
        "Usage: Open the request form and enter:\n"
        f"  {command_keyword}\n"
        "  <reference_id>, <amount>, <reason>\n"
        "  <reference_id>, <amount>, <reason>\n"
        "  ...\n"
        f"  Example:\n  {command_keyword}\n"
        "  EECS-001, 20.00, Connectors\n"
        "  EECS-010, 15.50, Ferrules\n"
        "  EECS-000 New Bin, 12.00, Storage for parts\n"
        "  [After Review, upload receipt image in channel and click Confirm]"
    )


def parse_purchase_text(text: str, *, command_keyword: str = "command_purchase:") -> ParseResult:
    usage = _single_item_usage(command_keyword)
    if not text or not text.strip():
        return ParseResult(
            ok=False,
            status=Status.INVALID_COMMAND,
            error_message=f"Missing arguments.\n{usage}",
            reference_id=None,
            subteam_tab=None,
            requested_amount=None,
            reason=None,
        )

    m = _CMD_RE.match(text)
    if not m:
        return ParseResult(
            ok=False,
            status=Status.INVALID_COMMAND,
            error_message=f"Could not parse command.\n{usage}",
            reference_id=None,
            subteam_tab=None,
            requested_amount=None,
            reason=None,
        )

    ref_id = m.group("ref_id").strip().upper()
    amount_raw = m.group("amount").strip()
    reason = m.group("reason").strip()
    provided_item_name = m.group("item_name").strip() if m.group("item_name") else None
    
    # Check if this is an unaccounted item (ends with -000)
    is_unaccounted = ref_id.endswith("-000")

    # Extract prefix from reference ID (e.g., "ADMIN" from "ADMIN-001")
    prefix_match = re.match(r'^([A-Z]+)', ref_id)
    if not prefix_match:
        return ParseResult(
            ok=False,
            status=Status.INVALID_COMMAND,
            error_message=f"Invalid reference ID format: {ref_id}. Expected format like ADMIN-001.",
            reference_id=None,
            subteam_tab=None,
            requested_amount=None,
            reason=None,
        )

    prefix = prefix_match.group(1)
    if prefix not in REFERENCE_ID_PREFIX_TO_TAB:
        valid_prefixes = ", ".join(REFERENCE_ID_PREFIX_TO_TAB.keys())
        return ParseResult(
            ok=False,
            status=Status.INVALID_COMMAND,
            error_message=f"Unknown subteam prefix: {prefix}. Valid prefixes: {valid_prefixes}",
            reference_id=None,
            subteam_tab=None,
            requested_amount=None,
            reason=None,
        )

    subteam_tab = REFERENCE_ID_PREFIX_TO_TAB[prefix]

    try:
        requested_amount = coerce_money(amount_raw)
    except Exception:
        requested_amount = None

    if requested_amount is None:
        return ParseResult(
            ok=False,
            status=Status.INVALID_COMMAND,
            error_message=f"Invalid amount: {amount_raw!r}.\n{usage}",
            reference_id=None,
            subteam_tab=None,
            requested_amount=None,
            reason=None,
        )

    if requested_amount <= 0:
        return ParseResult(
            ok=False,
            status=Status.INVALID_COMMAND,
            error_message="Amount must be greater than 0.",
            reference_id=None,
            subteam_tab=None,
            requested_amount=None,
            reason=None,
        )

    if not reason:
        return ParseResult(
            ok=False,
            status=Status.INVALID_COMMAND,
            error_message=f"Missing reason.\n{usage}",
            reference_id=None,
            subteam_tab=None,
            requested_amount=None,
            reason=None,
        )
    
    # For unaccounted items, require item name
    if is_unaccounted and not provided_item_name:
        return ParseResult(
            ok=False,
            status=Status.INVALID_COMMAND,
            error_message=f"Unaccounted items (ending in -000) require an item name.\nFormat: {ref_id} Item Name, amount, reason",
            reference_id=None,
            subteam_tab=None,
            requested_amount=None,
            reason=None,
        )

    return ParseResult(
        ok=True,
        status=Status.WITHIN_BUDGET,
        error_message=None,
        reference_id=ref_id,
        subteam_tab=subteam_tab,
        requested_amount=float(requested_amount),
        reason=reason,
        is_unaccounted=is_unaccounted,
        provided_item_name=provided_item_name,
    )


def parse_bulk_purchase_text(text: str, *, command_keyword: str = "bigorder:") -> BulkOrderParseResult:
    usage = _bulk_order_usage(command_keyword)
    if not text or not text.strip():
        return BulkOrderParseResult(
            ok=False,
            status=Status.INVALID_COMMAND,
            error_message=f"Missing bulk order items.\n{usage}",
            items=[],
        )

    raw_lines = [line.strip() for line in text.splitlines()]
    item_lines = [line for line in raw_lines if line]
    if not item_lines:
        return BulkOrderParseResult(
            ok=False,
            status=Status.INVALID_COMMAND,
            error_message=f"Missing bulk order items.\n{usage}",
            items=[],
        )

    parsed_items: list[ParseResult] = []
    for idx, line in enumerate(item_lines, start=1):
        parsed = parse_purchase_text(line, command_keyword=command_keyword)
        if not parsed.ok:
            line_error = parsed.error_message or "Invalid bulk order line."
            return BulkOrderParseResult(
                ok=False,
                status=parsed.status,
                error_message=f"Line {idx}: {line_error}",
                items=[],
            )
        parsed_items.append(
            ParseResult(
                ok=parsed.ok,
                status=parsed.status,
                error_message=parsed.error_message,
                reference_id=parsed.reference_id,
                subteam_tab=parsed.subteam_tab,
                requested_amount=parsed.requested_amount,
                reason=parsed.reason,
                is_unaccounted=parsed.is_unaccounted,
                provided_item_name=parsed.provided_item_name,
                line_number=idx,
            )
        )

    return BulkOrderParseResult(
        ok=True,
        status=Status.WITHIN_BUDGET,
        error_message=None,
        items=parsed_items,
    )

