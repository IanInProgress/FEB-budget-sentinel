from __future__ import annotations

import re
from dataclasses import dataclass

from budget_checker import Status
from utils import coerce_money


USAGE = 'Usage: /purchase <subteam> "<item name>" <amount>\nExample: /purchase electronics "CAN transceiver" 42.50'

_CMD_RE = re.compile(
    r'^\s*(?P<subteam>\S+)\s+"(?P<item>[^"]+)"\s+(?P<amount>\$?-?\d+(?:\.\d+)?)\s*$'
)


@dataclass(frozen=True)
class ParseResult:
    ok: bool
    status: Status
    error_message: str | None
    subteam: str | None
    item_name: str | None
    requested_amount: float | None


def parse_purchase_text(text: str) -> ParseResult:
    if not text or not text.strip():
        return ParseResult(
            ok=False,
            status=Status.INVALID_COMMAND,
            error_message=f"Missing arguments.\n{USAGE}",
            subteam=None,
            item_name=None,
            requested_amount=None,
        )

    m = _CMD_RE.match(text)
    if not m:
        return ParseResult(
            ok=False,
            status=Status.INVALID_COMMAND,
            error_message=f"Could not parse command.\n{USAGE}",
            subteam=None,
            item_name=None,
            requested_amount=None,
        )

    subteam = m.group("subteam").strip()
    item_name = m.group("item").strip()
    amount_raw = m.group("amount").strip()

    try:
        requested_amount = coerce_money(amount_raw)
    except Exception:
        requested_amount = None

    if requested_amount is None:
        return ParseResult(
            ok=False,
            status=Status.INVALID_COMMAND,
            error_message=f"Invalid amount: {amount_raw!r}.\n{USAGE}",
            subteam=None,
            item_name=None,
            requested_amount=None,
        )

    if requested_amount <= 0:
        return ParseResult(
            ok=False,
            status=Status.INVALID_COMMAND,
            error_message="Amount must be greater than 0.",
            subteam=None,
            item_name=None,
            requested_amount=None,
        )

    if not subteam:
        return ParseResult(
            ok=False,
            status=Status.INVALID_COMMAND,
            error_message=f"Missing subteam.\n{USAGE}",
            subteam=None,
            item_name=None,
            requested_amount=None,
        )

    if not item_name:
        return ParseResult(
            ok=False,
            status=Status.INVALID_COMMAND,
            error_message=f"Missing item name.\n{USAGE}",
            subteam=None,
            item_name=None,
            requested_amount=None,
        )

    return ParseResult(
        ok=True,
        status=Status.WITHIN_BUDGET,  # placeholder; real status computed later
        error_message=None,
        subteam=subteam,
        item_name=item_name,
        requested_amount=float(requested_amount),
    )

