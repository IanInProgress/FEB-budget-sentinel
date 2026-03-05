from __future__ import annotations

import logging
import os
from concurrent.futures import ThreadPoolExecutor

from flask import Flask, request
from gspread.exceptions import WorksheetNotFound
from slack_bolt import App
from slack_bolt.adapter.flask import SlackRequestHandler

from budget_checker import BudgetReport, Status, build_budget_report
from config import ConfigError, Settings, load_settings
from formatters import format_plaintext_report
from parser import ParseResult, parse_purchase_text
from sheets_client import SheetsClient, SheetsClientError


EXECUTOR = ThreadPoolExecutor(max_workers=4)


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )


def _resolve_subteam_tab(subteam_input: str, aliases: dict[str, str]) -> str:
    raw = subteam_input.strip()
    key = raw.lower()

    if key in aliases:
        return aliases[key]

    # If user already typed the canonical tab name (any case), honor it.
    canonical_values = {v.strip(): v.strip() for v in aliases.values() if v and v.strip()}
    canonical_by_lower = {k.lower(): v for k, v in canonical_values.items()}
    if key in canonical_by_lower:
        return canonical_by_lower[key]

    # Fallback: try as-is (tab names might already be lowercase in some sheets).
    return raw


def _report_for_invalid(parse: ParseResult) -> BudgetReport:
    return BudgetReport(
        status=Status.INVALID_COMMAND,
        reason=parse.error_message or "Invalid command.",
        subteam=parse.subteam or "(unknown)",
        requested_item=parse.item_name or "(unknown)",
        requested_amount=parse.requested_amount or 0.0,
        matched_item=None,
        estimated_budget=None,
        actual_spending=None,
        remaining_budget=None,
        candidates=[],
        suggestions=[],
    )


def create_server(settings: Settings) -> tuple[Flask, App]:
    _configure_logging(settings.log_level)
    logger = logging.getLogger("purchase_bot")

    bolt_app = App(
        token=settings.slack_bot_token,
        signing_secret=settings.slack_signing_secret,
    )
    handler = SlackRequestHandler(bolt_app)

    sheets = SheetsClient(
        spreadsheet_id=settings.google_sheet_id,
        service_account_file=settings.google_service_account_file,
        service_account_json=settings.google_service_account_json,
    )

    @bolt_app.command("/purchase")
    def handle_purchase_command(ack, command, respond):
        # Ack immediately to avoid Slack timeouts.
        ack("Processing budget verification report...")

        text = (command.get("text") or "").strip()
        user_id = command.get("user_id")

        def run() -> None:
            try:
                parse = parse_purchase_text(text)
                if not parse.ok:
                    respond(format_plaintext_report(_report_for_invalid(parse)))
                    return

                subteam_tab = _resolve_subteam_tab(parse.subteam or "", settings.subteam_aliases)
                if not subteam_tab:
                    report = BudgetReport(
                        status=Status.INVALID_COMMAND,
                        reason="Missing subteam.",
                        subteam=parse.subteam or "(unknown)",
                        requested_item=parse.item_name or "(unknown)",
                        requested_amount=parse.requested_amount or 0.0,
                        matched_item=None,
                        estimated_budget=None,
                        actual_spending=None,
                        remaining_budget=None,
                        candidates=[],
                        suggestions=[],
                    )
                    respond(format_plaintext_report(report))
                    return

                try:
                    lines = sheets.get_budget_lines(tab_name=subteam_tab)
                except WorksheetNotFound:
                    report = BudgetReport(
                        status=Status.SUBTEAM_TAB_NOT_FOUND,
                        reason=f'No tab found for subteam "{parse.subteam}". (Resolved tab: "{subteam_tab}")',
                        subteam=subteam_tab,
                        requested_item=parse.item_name or "(unknown)",
                        requested_amount=parse.requested_amount or 0.0,
                        matched_item=None,
                        estimated_budget=None,
                        actual_spending=None,
                        remaining_budget=None,
                        candidates=[],
                        suggestions=[],
                    )
                    respond(format_plaintext_report(report))
                    return
                except SheetsClientError as e:
                    report = BudgetReport(
                        status=Status.DATA_ERROR,
                        reason=str(e),
                        subteam=subteam_tab,
                        requested_item=parse.item_name or "(unknown)",
                        requested_amount=parse.requested_amount or 0.0,
                        matched_item=None,
                        estimated_budget=None,
                        actual_spending=None,
                        remaining_budget=None,
                        candidates=[],
                        suggestions=[],
                    )
                    respond(format_plaintext_report(report))
                    return

                report = build_budget_report(
                    subteam=subteam_tab,
                    requested_item=parse.item_name or "",
                    requested_amount=float(parse.requested_amount or 0.0),
                    lines=lines,
                    fuzzy_suggestion_threshold=settings.fuzzy_suggestion_threshold,
                )
                respond(format_plaintext_report(report))
            except Exception:
                logger.exception("Unhandled error while handling /purchase (user=%s)", user_id)
                report = BudgetReport(
                    status=Status.DATA_ERROR,
                    reason="Unhandled error while generating report. Check server logs.",
                    subteam="(unknown)",
                    requested_item="(unknown)",
                    requested_amount=0.0,
                    matched_item=None,
                    estimated_budget=None,
                    actual_spending=None,
                    remaining_budget=None,
                    candidates=[],
                    suggestions=[],
                )
                respond(format_plaintext_report(report))

        EXECUTOR.submit(run)

    server = Flask(__name__)

    @server.get("/healthz")
    def healthz():
        return {"ok": True}

    @server.post(settings.slack_commands_path)
    def slack_commands():
        # Bolt performs Slack signature verification via signing_secret.
        return handler.handle(request)

    return server, bolt_app


if __name__ == "__main__":
    try:
        settings = load_settings()
    except ConfigError as e:
        raise SystemExit(f"Config error: {e}") from e

    server, _bolt_app = create_server(settings)
    port = int(os.getenv("PORT", str(settings.port)))
    server.run(host="0.0.0.0", port=port)
