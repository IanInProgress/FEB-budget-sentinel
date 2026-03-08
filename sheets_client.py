from __future__ import annotations

import json
import logging
import re
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
    Sheets client for reading budget data and updating pending/actual spending.
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
        """
        Fetch budget lines from a subteam tab.
        Expected columns: A=Reference ID, B=Item Name, C=Estimated Budget,
        D=Pending Spend, E=Actual Spend, F=Available Budget, G=Total Budget.

        For budget checks, we treat committed spend as pending + actual.
        """
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

        # Some sheets store Available Budget (col F) once per subteam tab rather than per row.
        # If exactly one numeric value exists in col F, use it as fallback for rows without F.
        tab_available_budget: float | None = None
        available_candidates: list[float] = []
        for row in values[1:]:
            available_raw = row[5] if len(row) > 5 else ""
            if not str(available_raw).strip():
                continue
            try:
                parsed_available = coerce_money(available_raw, default=None)
            except Exception:
                parsed_available = None
            if parsed_available is not None:
                available_candidates.append(parsed_available)

        if len(available_candidates) == 1:
            tab_available_budget = available_candidates[0]

        # Row 1 is header; skip it
        lines: list[BudgetLine] = []
        for i, row in enumerate(values[1:], start=2):  # sheet row numbers
            ref_id = (row[0] if len(row) > 0 else "").strip()
            if not ref_id:
                continue

            item_name = (row[1] if len(row) > 1 else "").strip()
            est_raw = row[2] if len(row) > 2 else ""
            pending_raw = row[3] if len(row) > 3 else ""
            actual_raw = row[4] if len(row) > 4 else ""
            available_raw = row[5] if len(row) > 5 else ""
            
            try:
                est = coerce_money(est_raw, default=None)
            except Exception:
                est = None
            try:
                pending_spend = coerce_money(pending_raw, default=0.0)
            except Exception:
                pending_spend = 0.0

            try:
                actual_spend = coerce_money(actual_raw, default=0.0)
            except Exception:
                actual_spend = 0.0
            
            try:
                available_budget = coerce_money(available_raw, default=None)
            except Exception:
                available_budget = None

            if available_budget is None and tab_available_budget is not None:
                available_budget = tab_available_budget

            committed_spend = clamp_nonnegative(pending_spend + actual_spend)

            lines.append(
                BudgetLine(
                    reference_id=ref_id,
                    item_name=item_name,
                    estimated_budget=est,
                    actual_spending=committed_spend,
                    available_budget=available_budget,
                    row_number=i,
                )
            )

        self._cache[tab_name] = CachedTab(fetched_at=now, lines=lines)
        logger.info("Fetched %s budget lines from tab %r", len(lines), tab_name)
        return lines

    def _ensure_config_tab(self) -> None:
        """
        Ensure _Config tab exists with request_counter and bank_available. Create if missing.
        """
        try:
            self._sh.worksheet("_Config")
        except WorksheetNotFound:
            try:
                ws = self._sh.add_worksheet(title="_Config", rows=10, cols=2)
                ws.append_row(["key", "value"])
                ws.append_row(["request_counter", "0"])
                ws.append_row(["bank_available", "0"])
                logger.info("Created _Config tab with request_counter and bank_available initialized to 0")
            except Exception as e:
                logger.error("Failed to create _Config tab: %s", e)
                raise SheetsClientError("Could not create _Config tab") from e

    def get_and_increment_request_counter(self) -> int:
        """
        Get the current request counter from _Config tab and increment it.
        Returns the new counter value.
        """
        self._ensure_config_tab()

        try:
            ws = self._sh.worksheet("_Config")
        except WorksheetNotFound as e:
            raise SheetsClientError("_Config tab not found") from e
        except Exception as e:
            raise SheetsClientError("Failed to open _Config tab") from e

        try:
            values = ws.get_all_values()
        except Exception as e:
            raise SheetsClientError("Failed to read _Config tab") from e

        current_counter = 0
        config_row_num = None
        for i, row in enumerate(values):
            if len(row) > 0 and row[0] == "request_counter":
                config_row_num = i + 1
                try:
                    current_counter = int(row[1]) if len(row) > 1 else 0
                except (ValueError, IndexError):
                    current_counter = 0
                break

        if config_row_num is None:
            logger.warning("request_counter not found in _Config tab, reinitializing")
            config_row_num = 2
            current_counter = 0

        new_counter = current_counter + 1

        try:
            ws.update_cell(config_row_num, 2, new_counter)
            logger.info("Incremented request counter: %d -> %d", current_counter, new_counter)
        except Exception as e:
            logger.error("Failed to update request_counter in _Config tab: %s", e)
            raise SheetsClientError("Could not update request counter") from e

        return new_counter

    def get_bank_available(self) -> float:
        """
        Get the current bank_available from _Config tab.
        Returns the bank balance.
        """
        self._ensure_config_tab()

        try:
            ws = self._sh.worksheet("_Config")
        except WorksheetNotFound as e:
            raise SheetsClientError("_Config tab not found") from e
        except Exception as e:
            raise SheetsClientError("Failed to open _Config tab") from e

        try:
            values = ws.get_all_values()
        except Exception as e:
            raise SheetsClientError("Failed to read _Config tab") from e

        for row in values:
            if len(row) > 0 and row[0] == "bank_available":
                try:
                    return float(row[1]) if len(row) > 1 else 0.0
                except (ValueError, IndexError):
                    return 0.0

        logger.warning("bank_available not found in _Config tab, returning 0")
        return 0.0

    def update_bank_available(self, new_amount: float) -> bool:
        """
        Update bank_available in _Config tab.
        Returns True if successful.
        """
        self._ensure_config_tab()

        try:
            ws = self._sh.worksheet("_Config")
        except WorksheetNotFound as e:
            raise SheetsClientError("_Config tab not found") from e
        except Exception as e:
            raise SheetsClientError("Failed to open _Config tab") from e

        try:
            values = ws.get_all_values()
        except Exception as e:
            raise SheetsClientError("Failed to read _Config tab") from e

        config_row_num = None
        for i, row in enumerate(values):
            if len(row) > 0 and row[0] == "bank_available":
                config_row_num = i + 1
                break

        if config_row_num is None:
            logger.warning("bank_available not found in _Config tab, appending")
            try:
                ws.append_row(["bank_available", new_amount])
                logger.info("Added bank_available to _Config: %s", new_amount)
                return True
            except Exception as e:
                logger.error("Failed to append bank_available: %s", e)
                return False

        try:
            ws.update_cell(config_row_num, 2, new_amount)
            logger.info("Updated bank_available in _Config: %s", new_amount)
            return True
        except Exception as e:
            logger.error("Failed to update bank_available in _Config: %s", e)
            return False

    def _ensure_purchases_log_tab(self) -> None:
        """
        Ensure Purchases_Log tab exists with proper headers. Create if missing.
        """
        expected_headers = [
            "request_id",
            "submitted_at_utc",
            "reviewed_at_utc",
            "status",
            "requester_id",
            "manager_id",
            "subteam",
            "reference_id",
            "item_name",
            "purchase_reason",
            "amount_usd",
            "subteam_available_before",
            "subteam_available_after",
            "bank_available_before",
            "bank_available_after",
            "receipt_link",
            "rejection_reason",
            "bot_assessment",
        ]
        
        try:
            ws = self._sh.worksheet("Purchases_Log")
            try:
                # Ensure worksheet has enough columns
                if ws.col_count < len(expected_headers):
                    ws.add_cols(len(expected_headers) - ws.col_count)
                    logger.info("Expanded Purchases_Log to %d columns", len(expected_headers))
                
                actual_headers = ws.row_values(1)
                if actual_headers != expected_headers:
                    logger.warning("Purchases_Log headers don't match expected structure. Updating...")
                    for i, header in enumerate(expected_headers, start=1):
                        ws.update_cell(1, i, header)
                    logger.info("Updated Purchases_Log headers")
            except Exception as e:
                logger.error("Failed to check/update Purchases_Log headers: %s", e)
        except WorksheetNotFound:
            try:
                ws = self._sh.add_worksheet(title="Purchases_Log", rows=1000, cols=18)
                ws.append_row(expected_headers)
                logger.info("Created Purchases_Log tab with headers")
            except Exception as e:
                logger.error("Failed to create Purchases_Log tab: %s", e)
                raise SheetsClientError("Could not create Purchases_Log tab") from e

    def append_purchase_log(
        self,
        *,
        request_id: str,
        submitted_at_utc: str,
        requester_id: str,
        subteam: str,
        reference_id: str,
        item_name: str,
        purchase_reason: str,
        amount_usd: float,
        subteam_available_before: float | None,
        bank_available_before: float | None,
        receipt_link: str | None,
        bot_assessment: str,
    ) -> bool:
        """
        Append a new purchase request row to Purchases_Log.
        """
        self._ensure_purchases_log_tab()

        try:
            ws = self._sh.worksheet("Purchases_Log")
        except WorksheetNotFound as e:
            raise SheetsClientError("Purchases_Log tab not found") from e
        except Exception as e:
            raise SheetsClientError("Failed to open Purchases_Log tab") from e

        row = [
            request_id,
            submitted_at_utc,
            "",  # reviewed_at_utc
            "under_review",  # status
            requester_id,
            "",  # manager_id
            subteam,
            reference_id,
            item_name,
            purchase_reason,
            amount_usd,
            subteam_available_before if subteam_available_before is not None else "",
            "",  # subteam_available_after (filled on approval/rejection)
            bank_available_before if bank_available_before is not None else "",
            "",  # bank_available_after (filled on approval/rejection)
            receipt_link or "",
            "",  # rejection_reason
            bot_assessment,
        ]

        try:
            ws.append_row(row)
            logger.info("Logged purchase request %s to Purchases_Log", request_id)
            return True
        except Exception as e:
            logger.error("Failed to append purchase log for %s: %s", request_id, e)
            return False

    def update_purchase_log_status(
        self,
        *,
        request_id: str,
        status: str,
        reviewed_at_utc: str,
        manager_id: str,
        subteam_available_after: float | None = None,
        bank_available_after: float | None = None,
    ) -> bool:
        """
        Update purchase log row with approval/rejection details.
        Returns True if successful, False if row not found.
        """
        try:
            ws = self._sh.worksheet("Purchases_Log")
        except WorksheetNotFound as e:
            raise SheetsClientError("Purchases_Log tab not found") from e
        except Exception as e:
            raise SheetsClientError("Failed to open Purchases_Log tab") from e

        try:
            values = ws.get_all_values()
        except Exception as e:
            raise SheetsClientError("Failed to read Purchases_Log") from e

        # Find row with matching request_id
        row_num = None
        for i, row in enumerate(values[1:], start=2):
            if len(row) > 0 and row[0] == request_id:
                row_num = i
                break

        if row_num is None:
            logger.warning("Request %s not found in Purchases_Log", request_id)
            return False

        try:
            # Column C: reviewed_at_utc, Column D: status, Column F: manager_id
            ws.update_cell(row_num, 3, reviewed_at_utc)
            ws.update_cell(row_num, 4, status)
            ws.update_cell(row_num, 6, manager_id)
            
            # Column M: subteam_available_after, Column O: bank_available_after
            if subteam_available_after is not None:
                ws.update_cell(row_num, 13, subteam_available_after)
            if bank_available_after is not None:
                ws.update_cell(row_num, 15, bank_available_after)
            
            logger.info("Updated purchase log status for %s: %s by %s", request_id, status, manager_id)
            return True
        except Exception as e:
            logger.error("Failed to update purchase log for %s: %s", request_id, e)
            return False

    def update_purchase_log_rejection_reason(
        self,
        *,
        request_id: str,
        rejection_reason: str,
    ) -> bool:
        """
        Update rejection_reason field for a purchase log row.
        Returns True if successful, False if row not found.
        """
        try:
            ws = self._sh.worksheet("Purchases_Log")
        except WorksheetNotFound as e:
            raise SheetsClientError("Purchases_Log tab not found") from e
        except Exception as e:
            raise SheetsClientError("Failed to open Purchases_Log tab") from e

        try:
            values = ws.get_all_values()
        except Exception as e:
            raise SheetsClientError("Failed to read Purchases_Log") from e

        # Find row with matching request_id
        row_num = None
        for i, row in enumerate(values[1:], start=2):
            if len(row) > 0 and row[0] == request_id:
                row_num = i
                break

        if row_num is None:
            logger.warning("Request %s not found in Purchases_Log for rejection reason update", request_id)
            return False

        try:
            # Column Q (17): rejection_reason
            ws.update_cell(row_num, 17, rejection_reason)
            logger.info("Updated rejection reason for %s", request_id)
            return True
        except Exception as e:
            logger.error("Failed to update rejection reason for %s: %s", request_id, e)
            return False

    def update_purchase_log_receipt_link(
        self,
        *,
        request_id: str,
        receipt_link: str,
    ) -> bool:
        """
        Update receipt_link field for a purchase log row.
        Returns True if successful, False if row not found.
        """
        try:
            ws = self._sh.worksheet("Purchases_Log")
        except WorksheetNotFound as e:
            raise SheetsClientError("Purchases_Log tab not found") from e
        except Exception as e:
            raise SheetsClientError("Failed to open Purchases_Log tab") from e

        try:
            values = ws.get_all_values()
        except Exception as e:
            raise SheetsClientError("Failed to read Purchases_Log") from e

        # Find row with matching request_id
        row_num = None
        for i, row in enumerate(values[1:], start=2):
            if len(row) > 0 and row[0] == request_id:
                row_num = i
                break

        if row_num is None:
            logger.warning("Request %s not found in Purchases_Log for receipt link update", request_id)
            return False

        try:
            # Column P (16): receipt_link
            ws.update_cell(row_num, 16, receipt_link)
            logger.info("Updated receipt link for %s", request_id)
            return True
        except Exception as e:
            logger.error("Failed to update receipt link for %s: %s", request_id, e)
            return False

    def update_pending_spending_by_id(
        self,
        *,
        tab_name: str,
        reference_id: str,
        amount_to_add: float,
    ) -> bool:
        """
        Update pending spending (column D) for a budget line by reference_id.
        """
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

        matched_row = None
        for i, row in enumerate(values[1:], start=2):
            row_ref_id = (row[0] if len(row) > 0 else "").strip()
            if row_ref_id.upper() == reference_id.upper():
                matched_row = i
                break
        
        if matched_row is None:
            logger.warning("Reference ID %r not found in tab %r for spending update", reference_id, tab_name)
            return False

        # Column D (index 3) is pending spend
        try:
            current_pending_raw = values[matched_row - 1][3] if len(values[matched_row - 1]) > 3 else ""
            current_pending = coerce_money(current_pending_raw, default=0.0)
        except Exception:
            current_pending = 0.0
        
        new_pending = clamp_nonnegative(current_pending + amount_to_add)
        
        try:
            ws.update_cell(matched_row, 4, new_pending)
            logger.info(
                "Updated pending spend for %r in tab %r: %s -> %s (+%s)",
                reference_id, tab_name, current_pending, new_pending, amount_to_add
            )
        except Exception as e:
            raise SheetsClientError(f"Failed to update cell in tab {tab_name}") from e
        
        self._cache.pop(tab_name, None)
        
        return True

    def reimburse_by_id(
        self,
        *,
        tab_name: str,
        reference_id: str,
        amount: float,
    ) -> bool:
        """
        Move reimbursed amount from Pending Spend (D) to Actual Spend (E).
        """
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

        matched_row = None
        for i, row in enumerate(values[1:], start=2):
            row_ref_id = (row[0] if len(row) > 0 else "").strip()
            if row_ref_id.upper() == reference_id.upper():
                matched_row = i
                break

        if matched_row is None:
            logger.warning("Reference ID %r not found in tab %r for reimbursement", reference_id, tab_name)
            return False

        try:
            row = values[matched_row - 1]
            current_pending = coerce_money(row[3] if len(row) > 3 else "", default=0.0)
            current_actual = coerce_money(row[4] if len(row) > 4 else "", default=0.0)
        except Exception:
            current_pending = 0.0
            current_actual = 0.0

        transfer_amount = clamp_nonnegative(amount)
        new_pending = clamp_nonnegative(current_pending - transfer_amount)
        actual_added = current_pending - new_pending
        new_actual = clamp_nonnegative(current_actual + actual_added)

        try:
            ws.update_cell(matched_row, 4, new_pending)
            ws.update_cell(matched_row, 5, new_actual)
            logger.info(
                "Reimbursed %s for %r in %r: pending %s->%s, actual %s->%s",
                actual_added,
                reference_id,
                tab_name,
                current_pending,
                new_pending,
                current_actual,
                new_actual,
            )
        except Exception as e:
            raise SheetsClientError(f"Failed to update reimbursement cells in tab {tab_name}") from e

        self._cache.pop(tab_name, None)
        return True

    def append_budget_line(
        self,
        *,
        tab_name: str,
        item_name: str,
        initial_spending: float,
    ) -> str:
        """
        Append a new budget line to the subteam tab.
        Generates the next reference ID based on existing items.
        Returns the new reference_id (e.g., "ADMIN-013").
        """
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

        # Find the highest numeric suffix for this tab's reference IDs
        # E.g., if tab has ADMIN-001, ADMIN-012, find prefix and max number
        prefix = None
        max_num = 0
        
        for row in values[1:]:  # Skip header
            ref_id = (row[0] if len(row) > 0 else "").strip()
            if not ref_id:
                continue
            
            # Extract prefix and number (e.g., "ADMIN-012" -> "ADMIN", 12)
            match = re.match(r'^([A-Z]+)-(\d+)$', ref_id.upper())
            if match:
                row_prefix = match.group(1)
                row_num = int(match.group(2))
                
                if prefix is None:
                    prefix = row_prefix
                
                if row_prefix == prefix and row_num > max_num:
                    max_num = row_num
        
        # Generate new reference_id
        if prefix is None:
            # No items in tab yet - extract prefix from tab name or use default
            # This is a fallback; normally tabs should have at least one item
            raise SheetsClientError(f"Cannot determine reference ID prefix for tab {tab_name}")
        
        new_num = max_num + 1
        new_ref_id = f"{prefix}-{new_num:03d}"
        
        # Append new row: [ref_id, item_name, estimated_budget, pending_spend, actual_spend]
        # Approved unaccounted items start as pending until reimbursement.
        new_row = [new_ref_id, item_name, 0.0, initial_spending, 0.0]
        
        try:
            ws.append_row(new_row)
            logger.info(
                "Appended new budget line to tab %r: %s - %s (initial spending: %s)",
                tab_name, new_ref_id, item_name, initial_spending
            )
        except Exception as e:
            raise SheetsClientError(f"Failed to append row to tab {tab_name}") from e
        
        # Invalidate cache
        self._cache.pop(tab_name, None)
        
        return new_ref_id

