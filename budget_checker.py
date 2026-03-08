from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable


class Status(str, Enum):
    WITHIN_BUDGET = "WITHIN_BUDGET"
    OVER_BUDGET = "OVER_BUDGET"
    ITEM_NOT_FOUND = "ITEM_NOT_FOUND"
    UNACCOUNTED_ITEM = "UNACCOUNTED_ITEM"
    DATA_ERROR = "DATA_ERROR"
    INVALID_COMMAND = "INVALID_COMMAND"


@dataclass(frozen=True)
class BudgetLine:
    reference_id: str
    item_name: str
    estimated_budget: float | None
    actual_spending: float | None
    available_budget: float | None = None
    row_number: int | None = None  # 1-based sheet row, if known


@dataclass(frozen=True)
class BudgetReport:
    status: Status
    reason: str

    subteam: str
    reference_id: str
    item_name: str
    requested_amount: float

    estimated_budget: float | None
    actual_spending: float | None
    remaining_budget: float | None
    available_budget: float | None = None


def find_budget_match(
    *,
    reference_id: str,
    lines: Iterable[BudgetLine],
) -> BudgetReport | None:
    """
    Find a budget line by exact reference_id match.
    Returns BudgetReport if found, None if not found.
    """
    all_lines = list(lines)
    
    for line in all_lines:
        if line.reference_id.upper() == reference_id.upper():
            est = line.estimated_budget
            act = line.actual_spending
            
            if est is None or act is None:
                return BudgetReport(
                    status=Status.DATA_ERROR,
                    reason="Budget data is missing or non-numeric for this item.",
                    subteam=line.item_name,  # Using item_name as display
                    reference_id=reference_id,
                    item_name=line.item_name,
                    requested_amount=0,
                    estimated_budget=est,
                    actual_spending=act,
                    remaining_budget=None,
                    available_budget=line.available_budget,
                )
            
            remaining = est - act
            
            return BudgetReport(
                status=Status.WITHIN_BUDGET,
                reason="Item found.",
                subteam=line.item_name,
                reference_id=reference_id,
                item_name=line.item_name,
                requested_amount=0,  # Will be filled in by caller
                estimated_budget=est,
                actual_spending=act,
                remaining_budget=remaining,
                available_budget=line.available_budget,
            )
    
    # Not found
    return None


def build_budget_report(
    *,
    subteam: str,
    reference_id: str,
    item_name: str,
    requested_amount: float,
    lines: Iterable[BudgetLine],
    is_unaccounted: bool = False,
) -> BudgetReport:
    """
    Build a budget report by looking up an item by reference_id.
    If is_unaccounted=True, skip lookup and return UNACCOUNTED_ITEM status.
    """
    all_lines = list(lines)

    def _subteam_available_budget() -> float | None:
        for line in all_lines:
            if line.available_budget is not None:
                return float(line.available_budget)
        return None

    # Handle unaccounted items (e.g., ADMIN-000)
    if is_unaccounted:
        subteam_available = _subteam_available_budget()
        if subteam_available is None:
            reason = "Unaccounted item in planned budget; subteam available budget is unavailable."
        elif requested_amount <= subteam_available + 1e-9:
            reason = "Unaccounted item in planned budget, but within subteam available budget."
        else:
            reason = "Unaccounted item in planned budget and exceeds subteam available budget."

        return BudgetReport(
            status=Status.UNACCOUNTED_ITEM,
            reason=reason,
            subteam=subteam,
            reference_id=reference_id,
            item_name=item_name,
            requested_amount=requested_amount,
            estimated_budget=None,
            actual_spending=None,
            remaining_budget=None,
            available_budget=subteam_available,
        )
    
    match = find_budget_match(reference_id=reference_id, lines=all_lines)
    
    if match is None:
        return BudgetReport(
            status=Status.ITEM_NOT_FOUND,
            reason=f"Item with reference ID {reference_id} not found.",
            subteam=subteam,
            reference_id=reference_id,
            item_name=item_name,
            requested_amount=requested_amount,
            estimated_budget=None,
            actual_spending=None,
            remaining_budget=None,
            available_budget=None,
        )
    
    # Update with requested amount for budget comparison
    est = match.estimated_budget
    act = match.actual_spending
    
    if est is None or act is None:
        return BudgetReport(
            status=Status.DATA_ERROR,
            reason="Budget data is missing or non-numeric for this item.",
            subteam=subteam,
            reference_id=reference_id,
            item_name=match.item_name,
            requested_amount=requested_amount,
            estimated_budget=est,
            actual_spending=act,
            remaining_budget=None,
            available_budget=None,
        )
    
    remaining = est - act
    
    if requested_amount <= remaining + 1e-9:
        if match.available_budget is None:
            within_reason = "Within item budget; subteam available budget is unavailable."
        elif requested_amount <= match.available_budget + 1e-9:
            within_reason = "Within both item budget and subteam available budget."
        else:
            within_reason = "Within item budget, but exceeds subteam available budget."

        return BudgetReport(
            status=Status.WITHIN_BUDGET,
            reason=within_reason,
            subteam=subteam,
            reference_id=reference_id,
            item_name=match.item_name,
            requested_amount=requested_amount,
            estimated_budget=est,
            actual_spending=act,
            remaining_budget=remaining,
            available_budget=match.available_budget,
        )
    
    if match.available_budget is None:
        over_reason = "Exceeds item budget; subteam available budget is unavailable."
    elif requested_amount <= match.available_budget + 1e-9:
        over_reason = "Exceeds item budget, but within subteam available budget."
    else:
        over_reason = "Exceeds both item budget and subteam available budget."

    return BudgetReport(
        status=Status.OVER_BUDGET,
        reason=over_reason,
        subteam=subteam,
        reference_id=reference_id,
        item_name=match.item_name,
        requested_amount=requested_amount,
        estimated_budget=est,
        actual_spending=act,
        remaining_budget=remaining,
        available_budget=match.available_budget,
    )

