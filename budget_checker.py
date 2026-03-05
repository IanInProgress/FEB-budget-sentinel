from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable

from utils import clamp_nonnegative, normalize_item_name


class Status(str, Enum):
    WITHIN_BUDGET = "WITHIN_BUDGET"
    OVER_BUDGET = "OVER_BUDGET"
    ITEM_NOT_FOUND = "ITEM_NOT_FOUND"
    AMBIGUOUS_MATCH = "AMBIGUOUS_MATCH"
    DATA_ERROR = "DATA_ERROR"
    SUBTEAM_TAB_NOT_FOUND = "SUBTEAM_TAB_NOT_FOUND"
    INVALID_COMMAND = "INVALID_COMMAND"


@dataclass(frozen=True)
class BudgetLine:
    item_name: str
    estimated_budget: float | None
    actual_spending: float | None
    row_number: int | None = None  # 1-based sheet row, if known


@dataclass(frozen=True)
class MatchResult:
    status: Status
    matched: BudgetLine | None
    candidates: list[BudgetLine]
    reason: str
    suggestions: list[str]


@dataclass(frozen=True)
class BudgetReport:
    status: Status
    reason: str

    subteam: str
    requested_item: str
    requested_amount: float

    matched_item: str | None
    estimated_budget: float | None
    actual_spending: float | None
    remaining_budget: float | None

    candidates: list[str]
    suggestions: list[str]


def _try_fuzzy_suggestions(
    *,
    requested_item: str,
    lines: list[BudgetLine],
    threshold: int,
    limit: int = 3,
) -> list[str]:
    try:
        from rapidfuzz import process, fuzz  # type: ignore
    except Exception:
        return []

    choices = [ln.item_name for ln in lines if ln.item_name.strip()]
    if not choices:
        return []

    # WRatio works well across small punctuation/word-order differences.
    results = process.extract(
        requested_item,
        choices,
        scorer=fuzz.WRatio,
        limit=limit,
    )
    suggestions: list[str] = []
    for name, score, _idx in results:
        if score >= threshold and name not in suggestions:
            suggestions.append(name)
    return suggestions


def find_budget_match(
    *,
    requested_item: str,
    lines: Iterable[BudgetLine],
    fuzzy_suggestion_threshold: int = 84,
) -> MatchResult:
    all_lines = [ln for ln in lines if ln.item_name and ln.item_name.strip()]
    if not all_lines:
        return MatchResult(
            status=Status.ITEM_NOT_FOUND,
            matched=None,
            candidates=[],
            reason="No budget lines found in this tab.",
            suggestions=[],
        )

    req_raw = requested_item.strip()
    req_norm = normalize_item_name(req_raw)

    exact_matches = [ln for ln in all_lines if ln.item_name.strip().lower() == req_raw.lower()]
    if len(exact_matches) == 1:
        return MatchResult(
            status=Status.WITHIN_BUDGET,
            matched=exact_matches[0],
            candidates=[exact_matches[0]],
            reason="Exact item match (case-insensitive).",
            suggestions=[],
        )
    if len(exact_matches) > 1:
        return MatchResult(
            status=Status.AMBIGUOUS_MATCH,
            matched=None,
            candidates=exact_matches,
            reason="Multiple exact matches found for this item name.",
            suggestions=[],
        )

    norm_matches = [ln for ln in all_lines if normalize_item_name(ln.item_name) == req_norm]
    if len(norm_matches) == 1:
        return MatchResult(
            status=Status.WITHIN_BUDGET,
            matched=norm_matches[0],
            candidates=[norm_matches[0]],
            reason="Normalized item match (punctuation/spacing-insensitive).",
            suggestions=[],
        )
    if len(norm_matches) > 1:
        return MatchResult(
            status=Status.AMBIGUOUS_MATCH,
            matched=None,
            candidates=norm_matches,
            reason="Multiple normalized matches found for this item name.",
            suggestions=[],
        )

    suggestions = _try_fuzzy_suggestions(
        requested_item=req_raw,
        lines=all_lines,
        threshold=fuzzy_suggestion_threshold,
    )
    return MatchResult(
        status=Status.ITEM_NOT_FOUND,
        matched=None,
        candidates=[],
        reason="No matching item found in the selected subteam budget tab.",
        suggestions=suggestions,
    )


def build_budget_report(
    *,
    subteam: str,
    requested_item: str,
    requested_amount: float,
    lines: Iterable[BudgetLine],
    fuzzy_suggestion_threshold: int = 84,
) -> BudgetReport:
    match = find_budget_match(
        requested_item=requested_item,
        lines=lines,
        fuzzy_suggestion_threshold=fuzzy_suggestion_threshold,
    )

    if match.status in (Status.ITEM_NOT_FOUND, Status.AMBIGUOUS_MATCH):
        return BudgetReport(
            status=match.status,
            reason=match.reason,
            subteam=subteam,
            requested_item=requested_item,
            requested_amount=requested_amount,
            matched_item=None,
            estimated_budget=None,
            actual_spending=None,
            remaining_budget=None,
            candidates=[c.item_name for c in match.candidates],
            suggestions=match.suggestions,
        )

    if match.matched is None:
        return BudgetReport(
            status=Status.DATA_ERROR,
            reason="Unexpected matching state (no matched line).",
            subteam=subteam,
            requested_item=requested_item,
            requested_amount=requested_amount,
            matched_item=None,
            estimated_budget=None,
            actual_spending=None,
            remaining_budget=None,
            candidates=[],
            suggestions=[],
        )

    est = match.matched.estimated_budget
    act = match.matched.actual_spending
    if est is None or act is None:
        return BudgetReport(
            status=Status.DATA_ERROR,
            reason="Budget data is missing or non-numeric for the matched line item.",
            subteam=subteam,
            requested_item=requested_item,
            requested_amount=requested_amount,
            matched_item=match.matched.item_name,
            estimated_budget=est,
            actual_spending=act,
            remaining_budget=None,
            candidates=[],
            suggestions=[],
        )

    remaining = clamp_nonnegative(est - act)
    if requested_amount <= remaining + 1e-9:
        return BudgetReport(
            status=Status.WITHIN_BUDGET,
            reason="Requested amount is within remaining budget for this item.",
            subteam=subteam,
            requested_item=requested_item,
            requested_amount=requested_amount,
            matched_item=match.matched.item_name,
            estimated_budget=est,
            actual_spending=act,
            remaining_budget=remaining,
            candidates=[],
            suggestions=[],
        )

    return BudgetReport(
        status=Status.OVER_BUDGET,
        reason="Requested amount exceeds remaining budget for this item.",
        subteam=subteam,
        requested_item=requested_item,
        requested_amount=requested_amount,
        matched_item=match.matched.item_name,
        estimated_budget=est,
        actual_spending=act,
        remaining_budget=remaining,
        candidates=[],
        suggestions=[],
    )

