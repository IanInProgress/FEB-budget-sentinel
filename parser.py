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

USAGE = '''Usage: Send a message with image attachment
  command_purchase: <reference_id>, <amount>, <reason>
  Example: command_purchase: ADMIN-001, 50.00, Need for supplies
  For unaccounted items: ADMIN-000 Item Name, <amount>, <reason>
  Example: command_purchase: ADMIN-000 Toilet Paper, 25.00, Need for office
  [Attach receipt image to the same message]'''

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


def parse_purchase_text(text: str) -> ParseResult:
    if not text or not text.strip():
        return ParseResult(
            ok=False,
            status=Status.INVALID_COMMAND,
            error_message=f"Missing arguments.\n{USAGE}",
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
            error_message=f"Could not parse command.\n{USAGE}",
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
            error_message=f"Invalid amount: {amount_raw!r}.\n{USAGE}",
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
            error_message=f"Missing reason.\n{USAGE}",
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

