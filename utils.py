from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any

_PUNCT_RE = re.compile(r"[^\w\s]+", re.UNICODE)
_WS_RE = re.compile(r"\s+")


def normalize_item_name(value: str) -> str:
    """
    Normalization rules:
    - lowercase
    - remove common punctuation
    - collapse whitespace
    """
    s = value.strip().lower()
    s = _PUNCT_RE.sub(" ", s)
    s = _WS_RE.sub(" ", s).strip()
    return s


def format_usd(amount: float) -> str:
    return f"${amount:,.2f}"


class DataCoercionError(ValueError):
    pass


def coerce_money(value: Any, *, default: float | None = None) -> float | None:
    """
    Convert a Sheets cell value to float.
    Accepts strings like "$1,234.56", "1234.56", "", None.
    """
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        raise DataCoercionError(f"Unsupported money type: {type(value)}")

    s = value.strip()
    if not s:
        return default
    s = s.replace("$", "").replace(",", "")
    try:
        return float(Decimal(s))
    except (InvalidOperation, ValueError) as e:
        raise DataCoercionError(f"Invalid money value: {value!r}") from e


def clamp_nonnegative(amount: float) -> float:
    return amount if amount >= 0 else 0.0

