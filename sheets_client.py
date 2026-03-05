from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass

import gspread
from gspread.exceptions import WorksheetNotFound

from budget_checker import BudgetLine
from utils import clamp_nonnegative, coerce_money

logger = logging.getLogger(__name__)


class SheetsClientError(RuntimeError):
    pass


@dataclass(frozen=True)
class CachedTab:
    fetched_at: float
    lines: list[BudgetLine]


class SheetsClient:
    """
    Read-only Sheets client. Do NOT add write helpers in this MVP.
    """

    def __init__(
        self,
        *,
        spreadsheet_id: str,
        service_account_file: str | None = None,
        service_account_json: str | None = None,
        cache_ttl_seconds: int = 30,
    ) -> None:
        self._spreadsheet_id = spreadsheet_id
        self._cache_ttl_seconds = cache_ttl_seconds
        self._cache: dict[str, CachedTab] = {}

        if service_account_file:
            self._gc = gspread.service_account(filename=service_account_file)
        elif service_account_json:
            try:
                info = json.loads(service_account_json)
            except json.JSONDecodeError as e:
                raise SheetsClientError("GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON") from e
            self._gc = gspread.service_account_from_dict(info)
        else:
            raise SheetsClientError("Missing Sheets credentials (file or json).")

        self._sh = self._gc.open_by_key(self._spreadsheet_id)

    def get_budget_lines(self, *, tab_name: str, force_refresh: bool = False) -> list[BudgetLine]:
        now = time.time()
        cached = self._cache.get(tab_name)
        if (
            not force_refresh
            and cached is not None
            and (now - cached.fetched_at) <= self._cache_ttl_seconds
        ):
            return cached.lines

        try:
            ws = self._sh.worksheet(tab_name)
        except WorksheetNotFound:
            raise
        except Exception as e:
            raise SheetsClientError(f"Failed to open tab: {tab_name}") from e

        try:
            values = ws.get_all_values()
        except Exception as e:
            raise SheetsClientError(f"Failed to read tab values: {tab_name}") from e

        # Expect: Row 1 header, columns A-C: item name, estimated, actual
        lines: list[BudgetLine] = []
        for i, row in enumerate(values[1:], start=2):  # sheet row numbers
            item_name = (row[0] if len(row) > 0 else "").strip()
            if not item_name:
                continue

            est_raw = row[1] if len(row) > 1 else ""
            act_raw = row[2] if len(row) > 2 else ""
            try:
                est = coerce_money(est_raw, default=None)
            except Exception:
                est = None
            try:
                act = coerce_money(act_raw, default=0.0)
            except Exception:
                act = None

            if act is not None:
                act = clamp_nonnegative(act)

            lines.append(
                BudgetLine(
                    item_name=item_name,
                    estimated_budget=est,
                    actual_spending=act,
                    row_number=i,
                )
            )

        self._cache[tab_name] = CachedTab(fetched_at=now, lines=lines)
        logger.info("Fetched %s budget lines from tab %r", len(lines), tab_name)
        return lines

