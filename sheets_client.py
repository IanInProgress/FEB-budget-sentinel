from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass

import gspread
from gspread.exceptions import APIError, WorksheetNotFound

from budget_checker import BudgetLine
from utils import clamp_nonnegative, coerce_money

logger = logging.getLogger(__name__)

SHEETS_OPEN_MAX_ATTEMPTS = 6
SHEETS_OPEN_INITIAL_BACKOFF_SECONDS = 1.0
REQUEST_COUNTER_MAX_ATTEMPTS = 5
REQUEST_COUNTER_INITIAL_BACKOFF_SECONDS = 0.5
PURCHASE_LOG_READ_MAX_ATTEMPTS = 5
PURCHASE_LOG_READ_INITIAL_BACKOFF_SECONDS = 0.5

PURCHASE_LOG_HEADERS = [
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
    "is_unaccounted",
    "subteam_available_before",
    "subteam_available_after",
    "bank_available_before",
    "bank_available_after",
    "receipt_link",
    "rejection_reason",
    "bot_assessment",
    "bundle_line_number",
]

PURCHASE_LOG_HEADERS_LEGACY = [
    "request_id",
    "bundle_line_number",
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
    "is_unaccounted",
    "subteam_available_before",
    "subteam_available_after",
    "bank_available_before",
    "bank_available_after",
    "receipt_link",
    "rejection_reason",
    "bot_assessment",
]

REIMBURSEMENTS_LOG_HEADERS = [
    "reimbursement_id",
    "requested_at_utc",
    "completed_at_utc",
    "status",
    "manager_id",
    "person_reimbursed",
    "reference_id",
    "subteam",
    "item_name",
    "amount_requested_usd",
    "amount_reimbursed_usd",
    "bank_before",
    "bank_after",
    "notes",
]


class SheetsClientError(RuntimeError):
    pass


@dataclass(frozen=True)
class CachedTab:
    fetched_at: float
    lines: list[BudgetLine]


@dataclass(frozen=True)
class PurchaseLogEntry:
    request_id: str
    bundle_line_number: int
    status: str
    requester_id: str
    manager_id: str
    subteam: str
    reference_id: str
    item_name: str
    purchase_reason: str
    amount_usd: float
    is_unaccounted: bool
    rejection_reason: str


@dataclass(frozen=True)
class ReimbursementResult:
    reference_id: str
    tab_name: str
    item_name: str
    amount_requested: float
    amount_reimbursed: float
    pending_before: float
    pending_after: float
    actual_before: float
    actual_after: float


def _row_to_purchase_log_entry(row: list[str]) -> PurchaseLogEntry:
    amount_raw = row[10] if len(row) > 10 else ""
    amount = coerce_money(amount_raw, default=0.0)
    try:
        bundle_line_number = int(row[19]) if len(row) > 19 and row[19] else 0
    except ValueError:
        bundle_line_number = 0
    return PurchaseLogEntry(
        request_id=row[0],
        bundle_line_number=bundle_line_number,
        status=(row[3] if len(row) > 3 else "").strip(),
        requester_id=(row[4] if len(row) > 4 else "").strip(),
        manager_id=(row[5] if len(row) > 5 else "").strip(),
        subteam=(row[6] if len(row) > 6 else "").strip(),
        reference_id=(row[7] if len(row) > 7 else "").strip(),
        item_name=(row[8] if len(row) > 8 else "").strip(),
        purchase_reason=(row[9] if len(row) > 9 else "").strip(),
        amount_usd=float(amount),
        is_unaccounted=((row[11] if len(row) > 11 else "").strip().lower() == "true"),
        rejection_reason=(row[17] if len(row) > 17 else "").strip(),
    )


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

        self._sh = self._open_spreadsheet_with_retry(self._spreadsheet_id)

    @staticmethod
    def _is_rate_limit_error(exc: Exception) -> bool:
        if isinstance(exc, APIError):
            response = getattr(exc, "response", None)
            if response is not None and getattr(response, "status_code", None) == 429:
                return True
        return "429" in str(exc)

    def _open_spreadsheet_with_retry(self, spreadsheet_id: str):
        last_error: Exception | None = None
        backoff_seconds = SHEETS_OPEN_INITIAL_BACKOFF_SECONDS

        for attempt in range(1, SHEETS_OPEN_MAX_ATTEMPTS + 1):
            try:
                return self._gc.open_by_key(spreadsheet_id)
            except Exception as e:
                last_error = e
                if not self._is_rate_limit_error(e) or attempt == SHEETS_OPEN_MAX_ATTEMPTS:
                    break

                logger.warning(
                    "Sheets API rate limited while opening spreadsheet (attempt %s/%s). Retrying in %.1fs.",
                    attempt,
                    SHEETS_OPEN_MAX_ATTEMPTS,
                    backoff_seconds,
                )
                time.sleep(backoff_seconds)
                backoff_seconds = min(backoff_seconds * 2, 20.0)

        if last_error and self._is_rate_limit_error(last_error):
            raise SheetsClientError(
                "Google Sheets API rate limit reached (429). Please wait a minute and retry."
            ) from last_error
        if last_error:
            raise SheetsClientError("Failed to open Google Sheet by key.") from last_error

        raise SheetsClientError("Failed to open Google Sheet by key.")

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
        last_error: Exception | None = None
        backoff_seconds = REQUEST_COUNTER_INITIAL_BACKOFF_SECONDS

        for attempt in range(1, REQUEST_COUNTER_MAX_ATTEMPTS + 1):
            try:
                self._ensure_config_tab()

                ws = self._sh.worksheet("_Config")
                values = ws.get_all_values()

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
                ws.update_cell(config_row_num, 2, new_counter)
                logger.info("Incremented request counter: %d -> %d", current_counter, new_counter)
                return new_counter
            except Exception as e:
                last_error = e
                response = getattr(e, "response", None)
                status_code = getattr(response, "status_code", None)
                is_transient = self._is_rate_limit_error(e) or (isinstance(status_code, int) and 500 <= status_code <= 599)

                if not is_transient or attempt == REQUEST_COUNTER_MAX_ATTEMPTS:
                    break

                logger.warning(
                    "Transient error incrementing request counter (attempt %s/%s). Retrying in %.1fs.",
                    attempt,
                    REQUEST_COUNTER_MAX_ATTEMPTS,
                    backoff_seconds,
                )
                time.sleep(backoff_seconds)
                backoff_seconds = min(backoff_seconds * 2, 8.0)

        if last_error and self._is_rate_limit_error(last_error):
            raise SheetsClientError("Request counter temporarily unavailable due to Sheets rate limits.") from last_error
        if last_error:
            raise SheetsClientError("Could not update request counter") from last_error

        raise SheetsClientError("Could not update request counter")

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
                    raw_value = row[1] if len(row) > 1 else ""
                    return float(coerce_money(raw_value, default=0.0))
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

    def _ensure_purchases_log_tab(self) -> str:
        """
        Ensure Purchases_Log tab exists with proper headers. Create if missing.
        """
        expected_headers = PURCHASE_LOG_HEADERS
        migrated = False

        try:
            ws = self._sh.worksheet("Purchases_Log")
            try:
                # Ensure worksheet has enough columns
                if ws.col_count < len(expected_headers):
                    ws.add_cols(len(expected_headers) - ws.col_count)
                    logger.info("Expanded Purchases_Log to %d columns", len(expected_headers))
                
                actual_headers = ws.row_values(1)
                if actual_headers == PURCHASE_LOG_HEADERS_LEGACY:
                    self._migrate_purchases_log_headers_legacy_to_current(ws)
                    migrated = True
                    actual_headers = ws.row_values(1)

                if actual_headers != expected_headers:
                    values = ws.get_all_values()
                    has_data = len(values) > 1
                    if has_data:
                        logger.warning(
                            "Purchases_Log headers differ from expected but contain data; leaving as-is to avoid data misalignment."
                        )
                        return "skipped_nonstandard_with_data"
                    else:
                        logger.warning("Purchases_Log headers don't match expected structure. Updating...")
                        for i, header in enumerate(expected_headers, start=1):
                            ws.update_cell(1, i, header)
                        logger.info("Updated Purchases_Log headers")
                        return "updated_empty_headers"
                return "migrated_legacy_layout" if migrated else "up_to_date"
            except Exception as e:
                logger.error("Failed to check/update Purchases_Log headers: %s", e)
                return "check_failed"
        except WorksheetNotFound:
            try:
                ws = self._sh.add_worksheet(title="Purchases_Log", rows=1000, cols=len(expected_headers))
                ws.append_row(expected_headers)
                logger.info("Created Purchases_Log tab with headers")
                return "created"
            except Exception as e:
                logger.error("Failed to create Purchases_Log tab: %s", e)
                raise SheetsClientError("Could not create Purchases_Log tab") from e

    def ensure_purchase_log_schema_on_startup(self) -> None:
        """
        Run one startup schema check/migration for Purchases_Log and log the outcome.
        """
        status = self._ensure_purchases_log_tab()
        if status in {"migrated_legacy_layout", "skipped_nonstandard_with_data", "check_failed"}:
            logger.warning("Purchases_Log schema startup check: %s", status)
        else:
            logger.info("Purchases_Log schema startup check: %s", status)

    def _ensure_reimbursements_log_tab(self) -> str:
        """
        Ensure Reimbursements_Log tab exists with proper headers. Create if missing.
        """
        expected_headers = REIMBURSEMENTS_LOG_HEADERS

        try:
            ws = self._sh.worksheet("Reimbursements_Log")
            try:
                if ws.col_count < len(expected_headers):
                    ws.add_cols(len(expected_headers) - ws.col_count)
                    logger.info("Expanded Reimbursements_Log to %d columns", len(expected_headers))

                actual_headers = ws.row_values(1)
                if actual_headers != expected_headers:
                    values = ws.get_all_values()
                    has_data = len(values) > 1
                    if has_data:
                        logger.warning(
                            "Reimbursements_Log headers differ from expected but contain data; leaving as-is to avoid data misalignment."
                        )
                        return "skipped_nonstandard_with_data"

                    logger.warning("Reimbursements_Log headers don't match expected structure. Updating...")
                    for i, header in enumerate(expected_headers, start=1):
                        ws.update_cell(1, i, header)
                    logger.info("Updated Reimbursements_Log headers")
                    return "updated_empty_headers"

                return "up_to_date"
            except Exception as e:
                logger.error("Failed to check/update Reimbursements_Log headers: %s", e)
                return "check_failed"
        except WorksheetNotFound:
            try:
                ws = self._sh.add_worksheet(
                    title="Reimbursements_Log",
                    rows=1000,
                    cols=len(expected_headers),
                )
                ws.append_row(expected_headers)
                logger.info("Created Reimbursements_Log tab with headers")
                return "created"
            except Exception as e:
                logger.error("Failed to create Reimbursements_Log tab: %s", e)
                raise SheetsClientError("Could not create Reimbursements_Log tab") from e

    def ensure_reimbursements_log_schema_on_startup(self) -> None:
        """
        Run one startup schema check for Reimbursements_Log and log the outcome.
        """
        status = self._ensure_reimbursements_log_tab()
        if status in {"skipped_nonstandard_with_data", "check_failed"}:
            logger.warning("Reimbursements_Log schema startup check: %s", status)
        else:
            logger.info("Reimbursements_Log schema startup check: %s", status)

    def append_reimbursement_log(
        self,
        *,
        reimbursement_id: str,
        requested_at_utc: str,
        completed_at_utc: str,
        status: str,
        manager_id: str,
        person_reimbursed: str,
        reference_id: str,
        subteam: str,
        item_name: str,
        amount_requested_usd: float,
        amount_reimbursed_usd: float,
        bank_before: float,
        bank_after: float,
        notes: str = "",
    ) -> bool:
        """
        Append a reimbursement event row to Reimbursements_Log.
        """
        self._ensure_reimbursements_log_tab()

        try:
            ws = self._sh.worksheet("Reimbursements_Log")
        except WorksheetNotFound as e:
            raise SheetsClientError("Reimbursements_Log tab not found") from e
        except Exception as e:
            raise SheetsClientError("Failed to open Reimbursements_Log tab") from e

        row = [
            reimbursement_id,
            requested_at_utc,
            completed_at_utc,
            status,
            manager_id,
            person_reimbursed,
            reference_id,
            subteam,
            item_name,
            amount_requested_usd,
            amount_reimbursed_usd,
            bank_before,
            bank_after,
            notes,
        ]

        try:
            ws.append_row(row)
            logger.info("Logged reimbursement %s for %s", reimbursement_id, reference_id)
            return True
        except Exception as e:
            logger.error("Failed to append reimbursement log for %s: %s", reference_id, e)
            return False

    def _migrate_purchases_log_headers_legacy_to_current(self, ws) -> None:
        """
        Migrate Purchases_Log from legacy header order to current order.
        """
        values = ws.get_all_values()
        if not values:
            ws.append_row(PURCHASE_LOG_HEADERS)
            return

        actual_headers = values[0]
        if actual_headers != PURCHASE_LOG_HEADERS_LEGACY:
            return

        migrated_values: list[list[str]] = [PURCHASE_LOG_HEADERS]
        legacy_index = {name: i for i, name in enumerate(PURCHASE_LOG_HEADERS_LEGACY)}

        for row in values[1:]:
            row_map = {
                header: (row[idx] if idx < len(row) else "")
                for header, idx in legacy_index.items()
            }
            migrated_values.append([row_map.get(header, "") for header in PURCHASE_LOG_HEADERS])

        ws.clear()
        ws.update(values=migrated_values, range_name="A1")
        logger.info("Migrated Purchases_Log header order to current format")

    def append_purchase_log(
        self,
        *,
        request_id: str,
        bundle_line_number: int,
        submitted_at_utc: str,
        requester_id: str,
        subteam: str,
        reference_id: str,
        item_name: str,
        purchase_reason: str,
        amount_usd: float,
        is_unaccounted: bool,
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
            str(bool(is_unaccounted)).lower(),
            subteam_available_before if subteam_available_before is not None else "",
            "",  # subteam_available_after (filled on approval/rejection)
            bank_available_before if bank_available_before is not None else "",
            "",  # bank_available_after (filled on approval/rejection)
            receipt_link or "",
            "",  # rejection_reason
            bot_assessment,
            bundle_line_number,
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
        bundle_line_number: int | None = None,
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

        row_nums: list[int] = []
        for i, row in enumerate(values[1:], start=2):
            if len(row) > 0 and row[0] == request_id:
                row_line_number = 0
                if len(row) > 19:
                    try:
                        row_line_number = int(row[19])
                    except ValueError:
                        row_line_number = 0
                if bundle_line_number is None or row_line_number == bundle_line_number:
                    row_nums.append(i)

        if not row_nums:
            logger.warning("Request %s not found in Purchases_Log", request_id)
            return False

        try:
            batch_data = []
            for row_num in row_nums:
                # Columns C, D, F: reviewed_at_utc, status, manager_id
                batch_data.extend([
                    {"range": f"C{row_num}", "values": [[reviewed_at_utc]]},
                    {"range": f"D{row_num}", "values": [[status]]},
                    {"range": f"F{row_num}", "values": [[manager_id]]},
                ])
                # Column N: subteam_available_after, Column P: bank_available_after
                if subteam_available_after is not None:
                    batch_data.append({"range": f"N{row_num}", "values": [[subteam_available_after]]})
                if bank_available_after is not None:
                    batch_data.append({"range": f"P{row_num}", "values": [[bank_available_after]]})
            ws.batch_update(batch_data)
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

        row_nums: list[int] = []
        for i, row in enumerate(values[1:], start=2):
            if len(row) > 0 and row[0] == request_id:
                row_nums.append(i)

        if not row_nums:
            logger.warning("Request %s not found in Purchases_Log for rejection reason update", request_id)
            return False

        try:
            for row_num in row_nums:
                # Column R (18): rejection_reason
                ws.update_cell(row_num, 18, rejection_reason)
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

        row_nums: list[int] = []
        for i, row in enumerate(values[1:], start=2):
            if len(row) > 0 and row[0] == request_id:
                row_nums.append(i)

        if not row_nums:
            logger.warning("Request %s not found in Purchases_Log for receipt link update", request_id)
            return False

        try:
            for row_num in row_nums:
                # Column Q (17): receipt_link
                ws.update_cell(row_num, 17, receipt_link)
            logger.info("Updated receipt link for %s", request_id)
            return True
        except Exception as e:
            logger.error("Failed to update receipt link for %s: %s", request_id, e)
            return False

    def update_purchase_log_reference_id(
        self,
        *,
        request_id: str,
        bundle_line_number: int,
        reference_id: str,
    ) -> bool:
        """
        Update reference_id for a specific purchase log line.
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

        row_num = None
        for i, row in enumerate(values[1:], start=2):
            if len(row) > 19 and row[0] == request_id:
                try:
                    row_line_number = int(row[19])
                except ValueError:
                    continue
                if row_line_number == bundle_line_number:
                    row_num = i
                    break

        if row_num is None:
            logger.warning(
                "Request %s line %s not found in Purchases_Log for reference_id update",
                request_id,
                bundle_line_number,
            )
            return False

        try:
            ws.update_cell(row_num, 8, reference_id)
            logger.info("Updated reference_id for %s line %s to %s", request_id, bundle_line_number, reference_id)
            return True
        except Exception as e:
            logger.error("Failed to update reference_id for %s line %s: %s", request_id, bundle_line_number, e)
            return False

    def get_purchase_log_entries(self, *, request_id: str) -> list[PurchaseLogEntry]:
        """
        Read all purchase request rows by request_id from Purchases_Log.
        """
        values = self._read_purchases_log_values_with_retry()

        entries: list[PurchaseLogEntry] = []
        for row in values[1:]:
            if not row or row[0] != request_id:
                continue
            entries.append(_row_to_purchase_log_entry(row))

        entries.sort(key=lambda entry: entry.bundle_line_number)
        return entries

    def get_purchase_log_entries_for_request_ids(
        self,
        *,
        request_ids: set[str],
    ) -> dict[str, list[PurchaseLogEntry]]:
        """
        Read purchase log entries for many request_ids with a single sheet read.
        """
        if not request_ids:
            return {}

        values = self._read_purchases_log_values_with_retry()
        entries_by_request_id: dict[str, list[PurchaseLogEntry]] = {rid: [] for rid in request_ids}

        for row in values[1:]:
            if not row:
                continue
            row_request_id = (row[0] if len(row) > 0 else "").strip()
            if row_request_id not in request_ids:
                continue
            entries_by_request_id[row_request_id].append(_row_to_purchase_log_entry(row))

        for entries in entries_by_request_id.values():
            entries.sort(key=lambda entry: entry.bundle_line_number)

        return entries_by_request_id

    def _read_purchases_log_values_with_retry(self) -> list[list[str]]:
        """
        Read Purchases_Log values with retry/backoff on transient 429/5xx failures.
        """
        backoff_seconds = PURCHASE_LOG_READ_INITIAL_BACKOFF_SECONDS

        for attempt in range(1, PURCHASE_LOG_READ_MAX_ATTEMPTS + 1):
            try:
                ws = self._sh.worksheet("Purchases_Log")
                return ws.get_all_values()
            except WorksheetNotFound as e:
                raise SheetsClientError("Purchases_Log tab not found") from e
            except Exception as e:
                response = getattr(e, "response", None)
                status_code = getattr(response, "status_code", None)
                is_transient = self._is_rate_limit_error(e) or (
                    isinstance(status_code, int) and 500 <= status_code <= 599
                )

                if not is_transient or attempt == PURCHASE_LOG_READ_MAX_ATTEMPTS:
                    raise SheetsClientError("Failed to read Purchases_Log") from e

                logger.warning(
                    "Transient error reading Purchases_Log (attempt %s/%s). Retrying in %.1fs.",
                    attempt,
                    PURCHASE_LOG_READ_MAX_ATTEMPTS,
                    backoff_seconds,
                )
                time.sleep(backoff_seconds)
                backoff_seconds = min(backoff_seconds * 2, 8.0)

        raise SheetsClientError("Failed to read Purchases_Log")

    def get_purchase_log_entry(self, *, request_id: str) -> PurchaseLogEntry | None:
        """
        Read a purchase request row by request_id from Purchases_Log.
        Returns None when not found.
        """
        entries = self.get_purchase_log_entries(request_id=request_id)
        return entries[0] if entries else None

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
    ) -> ReimbursementResult | None:
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
            return None

        try:
            row = values[matched_row - 1]
            item_name = (row[1] if len(row) > 1 else "").strip()
            current_pending = coerce_money(row[3] if len(row) > 3 else "", default=0.0)
            current_actual = coerce_money(row[4] if len(row) > 4 else "", default=0.0)
        except Exception:
            item_name = ""
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
        return ReimbursementResult(
            reference_id=reference_id,
            tab_name=tab_name,
            item_name=item_name,
            amount_requested=float(amount),
            amount_reimbursed=float(actual_added),
            pending_before=float(current_pending),
            pending_after=float(new_pending),
            actual_before=float(current_actual),
            actual_after=float(new_actual),
        )

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

