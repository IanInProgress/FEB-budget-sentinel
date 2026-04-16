from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any

from flask import Flask, request
from gspread.exceptions import WorksheetNotFound
from slack_bolt import App
from slack_bolt.adapter.flask import SlackRequestHandler

from budget_checker import build_budget_report
from config import Settings, load_settings
from formatters import (
    _recommendation_header,
    format_reference_lookup_dm,
    format_manager_bundle_notification_blocks,
    format_manager_notification_blocks,
)
from parser import REFERENCE_ID_PREFIX_TO_TAB, parse_bulk_purchase_text, parse_purchase_text
from sheets_client import SheetsClient, SheetsClientError
from utils import coerce_money, format_usd


EXECUTOR = ThreadPoolExecutor(max_workers=4)
PENDING_APPROVALS: dict[str, dict[str, Any]] = {}  # message_ts -> request metadata
PENDING_REJECTION_REASONS: dict[str, dict[str, Any]] = {}  # manager message_ts -> rejection metadata
PENDING_CONFIRMATIONS: set[tuple[str, str, str]] = set()  # (user_id, channel_id, original_message_ts) -> in-flight
REJECTION_REASON_TIMEOUT_SECONDS = 600
REQUEST_ID_PATTERN = re.compile(r"\bREQ-[A-Z0-9]{6,}\b", re.IGNORECASE)
MANAGER_APPROVE_TOKENS = ("✅", ":white_check_mark:", ":heavy_check_mark:")
MANAGER_REJECT_TOKENS = ("❌", ":x:", ":no_entry:")
MANAGER_DECISION_SCAN_INTERVAL_SECONDS = 30
MANAGER_DECISION_SCAN_HISTORY_LIMIT = 100
RECEIPT_AUTO_LOOKBACK_SECONDS = 900


def _configure_logging(level: str) -> None:
    resolved_level = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        level=resolved_level,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
        force=True,
    )
    # Keep Flask/Werkzeug request logging consistent with configured LOG_LEVEL.
    logging.getLogger("werkzeug").setLevel(resolved_level)


def _prune_pending_rejection_reasons() -> None:
    now = time.time()
    expired = [
        message_ts
        for message_ts, payload in PENDING_REJECTION_REASONS.items()
        if now - float(payload.get("created_at", 0)) > REJECTION_REASON_TIMEOUT_SECONDS
    ]
    for message_ts in expired:
        PENDING_REJECTION_REASONS.pop(message_ts, None)


def _extract_request_id_from_text(text: str | None) -> str | None:
    if not text:
        return None
    m = REQUEST_ID_PATTERN.search(text)
    if not m:
        return None
    return m.group(0).upper()


def _extract_request_id_from_blocks(blocks: Any) -> str | None:
    if not isinstance(blocks, list):
        return None

    for block in blocks:
        if not isinstance(block, dict):
            continue

        text_obj = block.get("text")
        if isinstance(text_obj, dict):
            request_id = _extract_request_id_from_text(text_obj.get("text"))
            if request_id:
                return request_id

        fields = block.get("fields")
        if isinstance(fields, list):
            for field in fields:
                if not isinstance(field, dict):
                    continue
                request_id = _extract_request_id_from_text(field.get("text"))
                if request_id:
                    return request_id

        elements = block.get("elements")
        if isinstance(elements, list):
            for element in elements:
                if not isinstance(element, dict):
                    continue
                request_id = _extract_request_id_from_text(element.get("text"))
                if request_id:
                    return request_id

    return None


def _resolve_request_id_for_manager_thread(client, channel_id: str, thread_ts: str) -> str | None:
    """
    Recover request_id from the manager root message for this thread.
    """
    try:
        replies = client.conversations_replies(
            channel=channel_id,
            ts=thread_ts,
            oldest=thread_ts,
            inclusive=True,
            limit=1,
        )
        messages = replies.get("messages") or []
        if not messages:
            return None

        root = messages[0]
        request_id = _extract_request_id_from_text(root.get("text"))
        if request_id:
            return request_id

        request_id = _extract_request_id_from_blocks(root.get("blocks"))
        if request_id:
            return request_id
    except Exception:
        return None

    return None


def _recover_manager_decision_from_thread(
    client,
    channel_id: str,
    thread_ts: str,
) -> tuple[bool, str, set[int] | None] | None:
    """
    Recover the latest explicit manager decision (approve/reject) from thread history.
    Returns (is_approved, manager_user_id, approved_line_numbers) or None
    if no decision is found.
    """
    try:
        replies = client.conversations_replies(
            channel=channel_id,
            ts=thread_ts,
            limit=200,
        )
    except Exception:
        return None

    messages = replies.get("messages") or []
    # Walk newest -> oldest and pick the latest explicit manager decision token.
    for msg in reversed(messages):
        if msg.get("bot_id"):
            continue
        user_id = (msg.get("user") or "").strip()
        if not user_id:
            continue
        text = (msg.get("text") or "").strip()
        is_approved, is_rejected, approved_line_numbers = _parse_manager_decision_text(text)
        if is_approved:
            return True, user_id, approved_line_numbers
        if is_rejected:
            return False, user_id, None

    return None


def _parse_manager_decision_text(text: str | None) -> tuple[bool, bool, set[int] | None]:
    """
    Parse manager thread decision text.

    Returns (is_approved, is_rejected, approved_line_numbers).
    approved_line_numbers is only populated for approval messages that include numbers,
    e.g. "✅ 1 2 3".
    """
    candidate = (text or "").strip()
    if not candidate:
        return False, False, None

    has_approve = any(token in candidate for token in MANAGER_APPROVE_TOKENS)
    has_reject = any(token in candidate for token in MANAGER_REJECT_TOKENS)

    # Ignore ambiguous messages containing both approve and reject tokens.
    if has_approve and has_reject:
        return False, False, None

    if has_reject:
        return False, True, None

    if has_approve:
        # Prevent request IDs like REQ-000123 from being interpreted as item numbers.
        text_without_req_ids = REQUEST_ID_PATTERN.sub(" ", candidate)
        line_numbers = {int(m.group(0)) for m in re.finditer(r"\b\d+\b", text_without_req_ids)}
        return True, False, (line_numbers or None)

    return False, False, None


def _build_deletable_message_blocks(
    message_text: str,
    button_text: str = "Delete message",
    target_message_ts: str | None = None,
    target_channel_id: str | None = None,
) -> list[dict[str, Any]]:
    value_payload = {
        "target_message_ts": target_message_ts,
        "target_channel_id": target_channel_id,
    }
    return [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": message_text},
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "*The button below will only remove this bot reply. Please manually delete your original request message above, including any attached receipt image, then resend your request.*",
            },
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": button_text},
                    "style": "danger",
                    "action_id": "delete_bot_message",
                    "value": json.dumps(value_payload),
                }
            ],
        },
    ]


def _post_thread_message_with_delete_button(client, channel_id: str, thread_ts: str, message_text: str) -> None:
    client.chat_postMessage(
        channel=channel_id,
        thread_ts=thread_ts,
        text=message_text,
        blocks=_build_deletable_message_blocks(
            message_text,
            target_message_ts=thread_ts,
            target_channel_id=channel_id,
        ),
    )


def _bundle_total_amount(items: list[dict[str, Any]]) -> float:
    return float(sum(float(item["requested_amount"]) for item in items))


def _format_item_lines_for_message(items: list[dict[str, Any]]) -> str:
    parts = []
    for item in items:
        parts.append(
            f"Item {item['line_number']}: {item['reference_id']} | {item['item_name']} | {format_usd(float(item['requested_amount']))}"
        )
    return "\n".join(parts)


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
    sheets.ensure_purchase_log_schema_on_startup()
    sheets.ensure_reimbursements_log_schema_on_startup()

    decision_inflight_threads: set[str] = set()
    decision_inflight_lock = threading.Lock()

    def _submit_manager_decision_processing(
        *,
        approval_data: dict[str, Any],
        is_approved: bool,
        approved_line_numbers: set[int] | None,
        manager_id: str | None,
        thread_ts: str,
    ) -> None:
        if not manager_id:
            return

        with decision_inflight_lock:
            if thread_ts in decision_inflight_threads:
                return
            decision_inflight_threads.add(thread_ts)

        def run_approval() -> None:
            try:
                requester_id = approval_data["user_id"]
                request_id = approval_data["request_id"]
                original_channel_id = approval_data.get("original_channel_id")
                original_message_ts = approval_data.get("original_message_ts")
                items = approval_data.get("items") or [
                    {
                        "line_number": 1,
                        "subteam_tab": approval_data["subteam_tab"],
                        "reference_id": approval_data["reference_id"],
                        "item_name": approval_data["item_name"],
                        "requested_amount": approval_data["requested_amount"],
                        "is_unaccounted": approval_data.get("is_unaccounted", False),
                        "reason": approval_data.get("purchase_reason", ""),
                    }
                ]
                reviewed_at_utc = datetime.now(timezone.utc).isoformat()
                total_amount = _bundle_total_amount(items)
                approved_total = 0.0
                rejected_total = 0.0

                bank_before_for_copy = None
                running_bank_available_for_log = None
                try:
                    bank_before_for_copy = sheets.get_bank_available()
                    # Bank cash balance changes on reimbursement, not on approval.
                    running_bank_available_for_log = bank_before_for_copy
                except Exception as e:
                    logger.warning("Failed to read bank_available: %s", e)

                approved_item_summaries: list[str] = []
                rejected_item_summaries: list[str] = []
                for item in items:
                    line_number = int(item["line_number"])
                    subteam_tab = str(item["subteam_tab"])
                    reference_id = str(item["reference_id"])
                    item_name = str(item["item_name"])
                    amount = float(item["requested_amount"])
                    is_unaccounted = bool(item.get("is_unaccounted", False))
                    should_approve = is_approved and (
                        approved_line_numbers is None or line_number in approved_line_numbers
                    )
                    available_budget_before: float | None = None

                    try:
                        latest_lines = sheets.get_budget_lines(tab_name=subteam_tab, force_refresh=True)
                        if is_unaccounted:
                            matching_line = next(
                                (line for line in latest_lines if line.available_budget is not None),
                                None,
                            )
                        else:
                            matching_line = next(
                                (
                                    line
                                    for line in latest_lines
                                    if line.reference_id.upper() == reference_id.upper()
                                ),
                                None,
                            )
                        if matching_line and matching_line.available_budget is not None:
                            available_budget_before = float(matching_line.available_budget)
                    except Exception:
                        logger.warning(
                            "Could not read available budget before approval for %s in %s",
                            reference_id,
                            subteam_tab,
                        )

                    if should_approve:
                        final_reference_id = reference_id
                        if is_unaccounted:
                            try:
                                final_reference_id = sheets.append_budget_line(
                                    tab_name=subteam_tab,
                                    item_name=item_name,
                                    initial_spending=amount,
                                )
                                sheets.update_purchase_log_reference_id(
                                    request_id=request_id,
                                    bundle_line_number=line_number,
                                    reference_id=final_reference_id,
                                )
                                item["reference_id"] = final_reference_id
                                logger.info("Appended unaccounted item %r as %s", item_name, final_reference_id)
                            except Exception:
                                logger.exception("Failed to append unaccounted item to sheet")
                        else:
                            try:
                                success = sheets.update_pending_spending_by_id(
                                    tab_name=subteam_tab,
                                    reference_id=reference_id,
                                    amount_to_add=amount,
                                )
                                if not success:
                                    logger.warning("Could not find reference_id %r in sheet for update", reference_id)
                            except Exception:
                                logger.exception("Failed to update sheet for approved purchase")

                        subteam_available_after = (
                            available_budget_before - amount if available_budget_before is not None else None
                        )
                        bank_available_after_for_line = None
                        if running_bank_available_for_log is not None:
                            running_bank_available_for_log -= amount
                            bank_available_after_for_line = running_bank_available_for_log

                        sheets.update_purchase_log_status(
                            request_id=request_id,
                            bundle_line_number=line_number,
                            status="approved",
                            reviewed_at_utc=reviewed_at_utc,
                            manager_id=manager_id,
                            subteam_available_after=subteam_available_after,
                            bank_available_after=bank_available_after_for_line,
                        )
                        approved_total += amount
                        approved_item_summaries.append(
                            f"Item {line_number}: {final_reference_id} | {item_name} | {format_usd(amount)}"
                        )
                    else:
                        subteam_after = available_budget_before
                        sheets.update_purchase_log_status(
                            request_id=request_id,
                            bundle_line_number=line_number,
                            status="rejected",
                            reviewed_at_utc=reviewed_at_utc,
                            manager_id=manager_id,
                            subteam_available_after=subteam_after,
                            bank_available_after=bank_before_for_copy,
                        )
                        rejected_total += amount
                        rejected_item_summaries.append(
                            f"Item {line_number}: {reference_id} | {item_name} | {format_usd(amount)}"
                        )

                if approved_item_summaries and rejected_item_summaries:
                    bolt_app.client.chat_postMessage(
                        channel=str(requester_id),
                        text=(
                            f"⚠️ Your purchase request was *partially approved* by <@{manager_id}>.\n\n"
                            f"*Request ID:* {request_id}\n"
                            f"*Approved Amount:* {format_usd(approved_total)}\n"
                            f"*Rejected Amount:* {format_usd(rejected_total)}\n"
                            f"*Approved Items:*\n{chr(10).join(approved_item_summaries)}\n\n"
                            f"*Rejected Items:*\n{chr(10).join(rejected_item_summaries)}\n\n"
                            "Items not listed in the manager's approval message were rejected."
                        ),
                    )
                    bolt_app.client.chat_postMessage(
                        channel=settings.manager_channel_id,
                        thread_ts=thread_ts,
                        text=(
                            f"✅ Partially approved and logged for <@{requester_id}>.\n"
                            f"*Approved:* {len(approved_item_summaries)} item(s), {format_usd(approved_total)}\n"
                            f"*Rejected:* {len(rejected_item_summaries)} item(s), {format_usd(rejected_total)}"
                        ),
                    )
                elif approved_item_summaries:
                    bolt_app.client.chat_postMessage(
                        channel=str(requester_id),
                        text=(
                            f"✅ Your purchase request was *approved* by <@{manager_id}>!\n\n"
                            f"*Request ID:* {request_id}\n"
                            f"*Total Amount:* {format_usd(total_amount)}\n"
                            f"*Items:*\n{chr(10).join(approved_item_summaries)}\n\n"
                            f"The amount is now in Pending Spend."
                        ),
                    )
                    if original_channel_id and original_message_ts:
                        try:
                            bolt_app.client.reactions_add(
                                channel=original_channel_id,
                                timestamp=original_message_ts,
                                name="white_check_mark",
                            )
                        except Exception as e:
                            logger.warning("Failed to add checkmark reaction: %s", e)
                    bolt_app.client.chat_postMessage(
                        channel=settings.manager_channel_id,
                        thread_ts=thread_ts,
                        text=(
                            f"✅ Approved and logged {len(items)} item(s). Notified <@{requester_id}>.\n"
                            f"*Total:* {format_usd(total_amount)}"
                        ),
                    )
                else:
                    PENDING_REJECTION_REASONS[thread_ts] = {
                        "created_at": time.time(),
                        "request_id": request_id,
                        "requester_id": requester_id,
                        "manager_id": manager_id,
                        "items": items,
                        "total_amount": total_amount,
                        "original_channel_id": original_channel_id,
                        "original_message_ts": original_message_ts,
                    }
                    bolt_app.client.chat_postMessage(
                        channel=settings.manager_channel_id,
                        thread_ts=thread_ts,
                        text=(
                            f"<@{manager_id}> please reply with the rejection reason.\n"
                            f"I'll forward it to <@{requester_id}> via DM.\n\n"
                            f"*Request ID:* {request_id}\n"
                            f"*Items:* {len(items)}\n"
                            f"*Total:* {format_usd(total_amount)}"
                        ),
                    )
            except Exception:
                logger.exception("Error processing approval/rejection from manager thread")
            finally:
                with decision_inflight_lock:
                    decision_inflight_threads.discard(thread_ts)

        EXECUTOR.submit(run_approval)

    def _scan_manager_channel_for_missed_decisions(interval_seconds: int) -> None:
        """
        Periodically scan manager thread roots and process missed decisions for under_review requests.
        """
        while True:
            try:
                history = bolt_app.client.conversations_history(
                    channel=settings.manager_channel_id,
                    limit=MANAGER_DECISION_SCAN_HISTORY_LIMIT,
                )
                messages = history.get("messages") or []
                request_threads: dict[str, str] = {}

                for msg in messages:
                    message_ts = (msg.get("ts") or "").strip()
                    if not message_ts:
                        continue

                    # Only scan root messages.
                    if msg.get("thread_ts") and msg.get("thread_ts") != message_ts:
                        continue

                    request_id = _extract_request_id_from_text(msg.get("text"))
                    if not request_id:
                        request_id = _extract_request_id_from_blocks(msg.get("blocks"))
                    if not request_id or request_id in request_threads:
                        continue
                    thread_ts = (msg.get("thread_ts") or message_ts).strip()
                    if not thread_ts:
                        continue
                    request_threads[request_id] = thread_ts

                if not request_threads:
                    time.sleep(interval_seconds)
                    continue

                try:
                    entries_by_request_id = sheets.get_purchase_log_entries_for_request_ids(
                        request_ids=set(request_threads.keys())
                    )
                except Exception:
                    logger.exception(
                        "Scanner failed reading Purchases_Log for %s request(s)",
                        len(request_threads),
                    )
                    time.sleep(interval_seconds)
                    continue

                for request_id, log_entries in entries_by_request_id.items():
                    if not log_entries or not all(entry.status == "under_review" for entry in log_entries):
                        continue

                    thread_ts = request_threads.get(request_id, "").strip()
                    if not thread_ts:
                        continue

                    recovered_decision = _recover_manager_decision_from_thread(
                        client=bolt_app.client,
                        channel_id=settings.manager_channel_id,
                        thread_ts=thread_ts,
                    )
                    if not recovered_decision:
                        continue

                    recovered_is_approved, recovered_manager_id, recovered_approved_line_numbers = recovered_decision
                    approval_data = {
                        "user_id": log_entries[0].requester_id,
                        "request_id": log_entries[0].request_id,
                        "items": [
                            {
                                "line_number": entry.bundle_line_number,
                                "subteam_tab": entry.subteam,
                                "reference_id": entry.reference_id,
                                "item_name": entry.item_name,
                                "requested_amount": entry.amount_usd,
                                "is_unaccounted": entry.is_unaccounted,
                                "reason": entry.purchase_reason,
                            }
                            for entry in log_entries
                        ],
                        "original_channel_id": None,
                        "original_message_ts": None,
                    }

                    _submit_manager_decision_processing(
                        approval_data=approval_data,
                        is_approved=recovered_is_approved,
                        approved_line_numbers=recovered_approved_line_numbers,
                        manager_id=recovered_manager_id,
                        thread_ts=thread_ts,
                    )
            except Exception:
                logger.exception("Manager decision scanner iteration failed")

            time.sleep(interval_seconds)

    def _extract_receipt_link_from_file_obj(file_obj: dict[str, Any]) -> str | None:
        permalink = file_obj.get("permalink")
        if isinstance(permalink, str) and permalink.strip():
            return permalink.strip()

        url_private = file_obj.get("url_private")
        if isinstance(url_private, str) and url_private.strip():
            return url_private.strip()

        file_id = file_obj.get("id")
        if isinstance(file_id, str) and file_id.strip():
            return f"https://files.slack.com/files-pri/{file_id.strip()}"

        return None

    def _find_recent_receipt_link_for_user(
        client,
        channel_id: str,
        user_id: str,
        *,
        min_message_ts: str | None = None,
    ) -> tuple[str | None, str | None]:
        """
        Return (receipt_link, source_message_ts) for the latest user-posted image
        from channel-level messages (not thread replies).
        """
        try:
            history = client.conversations_history(channel=channel_id, limit=30)
        except Exception:
            logger.exception("Failed to load channel history for receipt lookup")
            return None, None

        for msg in history.get("messages") or []:
            # Only accept top-level channel messages, not thread replies.
            if msg.get("thread_ts") and msg.get("thread_ts") != msg.get("ts"):
                continue
            if (msg.get("user") or "") != user_id:
                continue
            message_ts = str(msg.get("ts") or "")
            if min_message_ts:
                try:
                    if float(message_ts) <= float(min_message_ts):
                        continue
                except Exception:
                    continue
            files = msg.get("files") or []
            if not files:
                continue
            for file_obj in files:
                if not isinstance(file_obj, dict):
                    continue
                mimetype = str(file_obj.get("mimetype") or "").lower()
                filetype = str(file_obj.get("filetype") or "").lower()
                if mimetype.startswith("image/") or filetype in {"png", "jpg", "jpeg", "gif", "webp", "heic", "heif"}:
                    link = _extract_receipt_link_from_file_obj(file_obj)
                    if link:
                        return link, message_ts

        return None, None

    def _is_recent_slack_message_ts(message_ts: str | None, max_age_seconds: int) -> bool:
        if not message_ts:
            return False
        try:
            posted_at = float(message_ts)
        except Exception:
            return False
        return (time.time() - posted_at) <= float(max_age_seconds)

    def _open_purchase_modal(client, *, trigger_id: str, channel_id: str, user_id: str, is_bulk_order: bool, initial_text: str) -> None:
        callback_id = "bigorder_request_modal" if is_bulk_order else "purchase_request_modal"
        title_text = "Bigorder Request" if is_bulk_order else "Purchase Request"
        submit_text = "Review"
        details_label = "Bundle Lines" if is_bulk_order else "Request Details"
        details_hint = (
            "One item per line: reference_id, amount, reason" if is_bulk_order
            else "Format: reference_id, amount, reason"
        )

        client.views_open(
            trigger_id=trigger_id,
            view={
                "type": "modal",
                "callback_id": callback_id,
                "private_metadata": json.dumps(
                    {
                        "channel_id": channel_id,
                        "user_id": user_id,
                        "is_bulk_order": is_bulk_order,
                    }
                ),
                "title": {"type": "plain_text", "text": title_text},
                "submit": {"type": "plain_text", "text": submit_text},
                "close": {"type": "plain_text", "text": "Cancel"},
                "blocks": [
                    {
                        "type": "input",
                        "block_id": "command_text_block",
                        "label": {"type": "plain_text", "text": details_label},
                        "element": {
                            "type": "plain_text_input",
                            "action_id": "command_text_input",
                            "multiline": True,
                            "initial_value": initial_text,
                            "placeholder": {"type": "plain_text", "text": details_hint},
                        },
                        "hint": {"type": "plain_text", "text": details_hint},
                    },
                ],
            },
        )

    def _handle_purchase_modal_submission(ack, body, client, *, is_bulk_order: bool) -> None:
        view = body.get("view") or {}
        state_values = ((view.get("state") or {}).get("values") or {})
        command_text = (
            ((state_values.get("command_text_block") or {}).get("command_text_input") or {}).get("value")
            or ""
        ).strip()

        errors: dict[str, str] = {}
        parsed_single = None
        parsed_bulk = None
        if is_bulk_order:
            parsed_bulk = parse_bulk_purchase_text(command_text, command_keyword="/bigorder")
            if not parsed_bulk.ok:
                errors["command_text_block"] = parsed_bulk.error_message or "Invalid bigorder request."
        else:
            parsed_single = parse_purchase_text(command_text, command_keyword="/purchase")
            if not parsed_single.ok:
                errors["command_text_block"] = parsed_single.error_message or "Invalid purchase request."

        if errors:
            ack(response_action="errors", errors=errors)
            return

        ack()

        private_metadata_raw = view.get("private_metadata") or "{}"
        try:
            private_metadata = json.loads(private_metadata_raw)
        except Exception:
            logger.warning("Invalid purchase modal private metadata")
            return

        channel_id = private_metadata.get("channel_id")
        user_id = private_metadata.get("user_id")
        if not channel_id or not user_id:
            logger.warning("Missing required private metadata for purchase modal submission")
            return

        # Intentionally defer receipt detection until Confirm so users can post
        # draft details first, then upload receipt image.
        receipt_link = ""

        if is_bulk_order and parsed_bulk and parsed_bulk.ok:
            total_amount = sum(float(item.requested_amount or 0.0) for item in parsed_bulk.items)
            draft_lines = [
                f"Item {item.line_number}: {item.reference_id} | {format_usd(float(item.requested_amount or 0.0))} | {item.reason}"
                for item in parsed_bulk.items
            ]
            draft_text = (
                f"Bulk purchase request draft by <@{user_id}>\n"
                f"Items: {len(parsed_bulk.items)} | Total: {format_usd(float(total_amount))}\n"
                + "\n".join(draft_lines)
            )
        elif parsed_single and parsed_single.ok:
            draft_text = (
                f"Purchase request draft by <@{user_id}>\n"
                f"{parsed_single.reference_id} | {format_usd(float(parsed_single.requested_amount or 0.0))} | {parsed_single.reason}"
            )
        else:
            draft_text = (
                f"Bulk purchase request draft created by <@{user_id}>."
                if is_bulk_order
                else f"Purchase request draft created by <@{user_id}>."
            )

        try:
            anchor = client.chat_postMessage(
                channel=channel_id,
                text=draft_text,
            )
        except Exception:
            logger.exception("Failed to create purchase draft anchor message from modal submission")
            return

        _start_purchase_confirmation_flow(
            client=client,
            user_id=user_id,
            channel_id=channel_id,
            command_text=command_text,
            is_bulk_order=is_bulk_order,
            original_message_ts=str(anchor.get("ts") or ""),
            receipt_link=receipt_link,
        )

    def _start_purchase_confirmation_flow(
        *,
        client,
        user_id: str,
        channel_id: str,
        command_text: str,
        is_bulk_order: bool,
        original_message_ts: str,
        receipt_link: str,
    ) -> None:
        def run() -> None:
            try:
                if is_bulk_order:
                    parsed_bulk = parse_bulk_purchase_text(
                        command_text,
                        command_keyword="/bigorder",
                    )
                    if not parsed_bulk.ok:
                        _post_thread_message_with_delete_button(
                            client=client,
                            channel_id=channel_id,
                            thread_ts=original_message_ts,
                            message_text=parsed_bulk.error_message or "Invalid bulk order request.",
                        )
                        return
                    parsed_items = parsed_bulk.items
                else:
                    parsed_single = parse_purchase_text(
                        command_text,
                        command_keyword="/purchase",
                    )
                    if not parsed_single.ok:
                        _post_thread_message_with_delete_button(
                            client=client,
                            channel_id=channel_id,
                            thread_ts=original_message_ts,
                            message_text="Invalid purchase request. Use `/tutorial` to learn how to use the bot.",
                        )
                        return
                    parsed_items = [parsed_single]

                confirmation_items: list[dict[str, Any]] = []
                for idx, parsed in enumerate(parsed_items, start=1):
                    try:
                        budget_lines = sheets.get_budget_lines(tab_name=parsed.subteam_tab)
                        item_name = ""
                        if parsed.is_unaccounted:
                            item_name = parsed.provided_item_name or ""
                        else:
                            for line in budget_lines:
                                if line.reference_id.upper() == parsed.reference_id.upper():
                                    item_name = line.item_name
                                    break
                            if not item_name:
                                _post_thread_message_with_delete_button(
                                    client=client,
                                    channel_id=channel_id,
                                    thread_ts=original_message_ts,
                                    message_text=(
                                        f"Reference ID `{parsed.reference_id}` was not found in `{parsed.subteam_tab}`.\n"
                                        "Use an existing reference ID, or use a `-000` ID for unaccounted items."
                                    ),
                                )
                                return
                    except WorksheetNotFound:
                        client.chat_postMessage(
                            channel=channel_id,
                            thread_ts=original_message_ts,
                            text=f'No tab found for subteam "{parsed.subteam_tab}".',
                        )
                        return
                    except SheetsClientError as e:
                        client.chat_postMessage(
                            channel=channel_id,
                            thread_ts=original_message_ts,
                            text=f"Error accessing budget data: {str(e)}",
                        )
                        return

                    confirmation_items.append(
                        {
                            "line_number": parsed.line_number or idx,
                            "reference_id": parsed.reference_id,
                            "subteam_tab": parsed.subteam_tab,
                            "item_name": item_name,
                            "requested_amount": parsed.requested_amount,
                            "reason": parsed.reason,
                            "is_unaccounted": parsed.is_unaccounted,
                        }
                    )

                confirmation_data = {
                    "items": confirmation_items,
                    "channel_id": channel_id,
                    "user_id": user_id,
                    "original_message_ts": original_message_ts,
                    "receipt_link": receipt_link,
                    "is_bulk_order": is_bulk_order,
                }

                blocks = [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"*<@{user_id}>, please confirm your purchase request:*"
                        }
                    },
                ]
                if len(confirmation_items) == 1:
                    item = confirmation_items[0]
                    blocks.append(
                        {
                            "type": "section",
                            "fields": [
                                {"type": "mrkdwn", "text": f"*Reference ID:*\n{item['reference_id']}"},
                                {"type": "mrkdwn", "text": f"*Item:*\n{item['item_name']}"},
                                {"type": "mrkdwn", "text": f"*Amount:*\n{format_usd(float(item['requested_amount']))}"},
                                {"type": "mrkdwn", "text": f"*Reason:*\n{item['reason']}"},
                            ]
                        }
                    )
                else:
                    blocks.append(
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": (
                                    f"*Items:* {len(confirmation_items)}\n"
                                    f"*Total:* {format_usd(_bundle_total_amount(confirmation_items))}\n"
                                    f"{_format_item_lines_for_message(confirmation_items)}"
                                ),
                            },
                        }
                    )
                blocks.extend(
                    [
                        {
                            "type": "context",
                            "elements": [
                                {
                                    "type": "mrkdwn",
                                    "text": (
                                        "✅ Receipt link provided"
                                        if receipt_link
                                        else "📎 No receipt link yet. Upload receipt image in channel, then click Confirm."
                                    ),
                                }
                            ]
                        },
                        {
                            "type": "actions",
                            "elements": [
                                {
                                    "type": "button",
                                    "text": {"type": "plain_text", "text": "Confirm"},
                                    "style": "primary",
                                    "action_id": "confirm_purchase",
                                    "value": json.dumps(confirmation_data)
                                },
                                {
                                    "type": "button",
                                    "text": {"type": "plain_text", "text": "Cancel"},
                                    "style": "danger",
                                    "action_id": "cancel_purchase",
                                    "value": json.dumps(confirmation_data)
                                }
                            ]
                        }
                    ]
                )

                client.chat_postMessage(
                    channel=channel_id,
                    thread_ts=original_message_ts,
                    text="Please confirm your purchase request:",
                    blocks=blocks
                )
            except Exception:
                logger.exception("Error processing purchase request command (user=%s)", user_id)
                try:
                    _post_thread_message_with_delete_button(
                        client=client,
                        channel_id=channel_id,
                        thread_ts=original_message_ts,
                        message_text="An unexpected error occurred while processing your request. Please try again.",
                    )
                except Exception:
                    pass

        EXECUTOR.submit(run)

    @bolt_app.command("/purchase")
    def handle_purchase_command(ack, body, client):
        ack()

        user_id = body.get("user_id")
        channel_id = body.get("channel_id")
        trigger_id = body.get("trigger_id")
        command_text = (body.get("text") or "").strip()

        if not user_id or not channel_id or not trigger_id:
            return
        try:
            _open_purchase_modal(
                client,
                trigger_id=trigger_id,
                channel_id=channel_id,
                user_id=user_id,
                is_bulk_order=False,
                initial_text=command_text,
            )
        except Exception:
            logger.exception("Failed to open /purchase modal")

    @bolt_app.command("/bigorder")
    def handle_bigorder_command(ack, body, client):
        ack()

        user_id = body.get("user_id")
        channel_id = body.get("channel_id")
        trigger_id = body.get("trigger_id")
        command_text = (body.get("text") or "").strip()

        if not user_id or not channel_id or not trigger_id:
            return
        try:
            _open_purchase_modal(
                client,
                trigger_id=trigger_id,
                channel_id=channel_id,
                user_id=user_id,
                is_bulk_order=True,
                initial_text=command_text,
            )
        except Exception:
            logger.exception("Failed to open /bigorder modal")

    @bolt_app.command("/reference")
    def handle_reference_command(ack, body, client):
        ack()

        user_id = body.get("user_id")
        channel_id = body.get("channel_id")
        raw_text = (body.get("text") or "").strip()

        if not user_id or not channel_id:
            return

        tokens = raw_text.split()
        if len(tokens) != 1:
            client.chat_postEphemeral(
                channel=channel_id,
                user=user_id,
                text="Usage: `/reference <subteam_prefix>` (example: `/reference MECH`)",
            )
            return

        prefix = tokens[0].upper()
        tab_name = REFERENCE_ID_PREFIX_TO_TAB.get(prefix)
        if not tab_name:
            valid_prefixes = ", ".join(REFERENCE_ID_PREFIX_TO_TAB.keys())
            client.chat_postEphemeral(
                channel=channel_id,
                user=user_id,
                text=f"Unknown prefix `{prefix}`. Valid prefixes: {valid_prefixes}",
            )
            return

        def run_reference_lookup() -> None:
            try:
                lines = sheets.get_budget_lines(tab_name=tab_name, force_refresh=True)
                rows = [
                    (line.reference_id, line.item_name or "(no item name)")
                    for line in lines
                    if line.reference_id
                ]
                dm_text = format_reference_lookup_dm(prefix=prefix, tab_name=tab_name, rows=rows)

                client.chat_postMessage(
                    channel=user_id,
                    text=dm_text,
                    mrkdwn=True,
                    unfurl_links=False,
                    unfurl_media=False,
                )

                client.chat_postEphemeral(
                    channel=channel_id,
                    user=user_id,
                    text=f"Sent you a DM with the *{prefix}* reference list.",
                )
            except WorksheetNotFound:
                client.chat_postEphemeral(
                    channel=channel_id,
                    user=user_id,
                    text=f"Could not find the *{tab_name}* tab in Google Sheets.",
                )
            except Exception:
                logger.exception("Failed to process /reference for prefix=%s", prefix)
                client.chat_postEphemeral(
                    channel=channel_id,
                    user=user_id,
                    text="Could not fetch references right now. Please try again.",
                )

        EXECUTOR.submit(run_reference_lookup)

    @bolt_app.view("purchase_request_modal")
    def handle_purchase_request_modal(ack, body, client):
        _handle_purchase_modal_submission(ack, body, client, is_bulk_order=False)

    @bolt_app.view("bigorder_request_modal")
    def handle_bigorder_request_modal(ack, body, client):
        _handle_purchase_modal_submission(ack, body, client, is_bulk_order=True)

    def _build_tutorial_blocks(owner_id: str, selected: str) -> list[dict[str, Any]]:
            selected_mode = selected if selected in {"purchase", "bigorder"} else "purchase"
            purchase_value = json.dumps({"owner_id": owner_id, "selected": "purchase"})
            bigorder_value = json.dumps({"owner_id": owner_id, "selected": "bigorder"})
            delete_value = json.dumps({"owner_id": owner_id})

            if selected_mode == "purchase":
                guide_title = "Help: /purchase"
                guide_text = (
                    "*How to use `/purchase`*\n"
                    "1. Run `/purchase` to open the request form\n"
                    "2. Enter `reference_id, amount, reason`\n"
                    "3. Click *Review* to post your draft details\n"
                    "4. Upload your receipt image in the channel\n"
                    "5. Click *Confirm*"
                )
                examples_text = (
                    "*Examples*\n"
                    "`/purchase` (opens blank form)\n"
                    "`/purchase EECS-025, 42.50, Zipties for cable management` (prefills details)\n"
                    "`/purchase ADMIN-000 Office Supplies, 50.00, Need for workspace`\n"
                    "(for `-000` IDs, include an item name after the reference ID)"
                )
            else:
                guide_title = "Help: /bigorder"
                guide_text = (
                    "*How to use `/bigorder`*\n"
                    "1. Run `/bigorder` to open the request form\n"
                    "2. Enter one item per line\n"
                    "3. Click *Review* to post your draft details\n"
                    "4. Upload your receipt image in the channel\n"
                    "5. Click *Confirm*"
                )
                examples_text = (
                    "*Example*\n"
                    "```\n"
                    "/bigorder\n"
                    "EECS-025, 42.50, Zipties\n"
                    "EECS-010, 15.00, Ferrules\n"
                    "ADMIN-000 Office Supplies, 50.00, Team workspace\n"
                    "```\n"
                    "All lines are logged under one Request ID."
                )

            return [
                {
                    "type": "header",
                    "text": {"type": "plain_text", "text": "FEB Purchase Bot Tutorial"},
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "Choose which command you want help with:",
                    },
                },
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "Purchase Help"},
                            **({"style": "primary"} if selected_mode == "purchase" else {}),
                            "action_id": "tutorial_help_purchase",
                            "value": purchase_value,
                        },
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "Bigorder Help"},
                            **({"style": "primary"} if selected_mode == "bigorder" else {}),
                            "action_id": "tutorial_help_bigorder",
                            "value": bigorder_value,
                        },
                    ],
                },
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": f"*{guide_title}*\n{guide_text}"},
                },
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": examples_text},
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            "*Receipt link tip*\n"
                            "No link paste needed. Upload receipt after the draft appears, then click Confirm."
                        ),
                    },
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            "*What happens next*\n"
                            "- Your request is sent to managers for review\n"
                            "- You receive a DM if approved or rejected\n"
                            "- If approved, amount is added to Pending Spend\n"
                            "- Need subteam item IDs? Use `/reference <subteam_prefix>` (example: `/reference MECH`)\n"
                            "- Use `/reimburse reference_id, amount` when reimbursement is completed"
                        ),
                    },
                },
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "Delete tutorial"},
                            "style": "danger",
                            "action_id": "delete_tutorial_message",
                            "value": delete_value,
                        }
                    ],
                },
            ]

    @bolt_app.command("/tutorial")
    def handle_tutorial_command(ack, body, client):
        ack()

        requester_id = body.get("user_id")
        channel_id = body.get("channel_id")

        if not requester_id or not channel_id:
            return

        blocks = _build_tutorial_blocks(requester_id, "purchase")

        client.chat_postMessage(
            channel=channel_id,
            text="How to submit a purchase request",
            blocks=blocks,
        )

    def _handle_tutorial_help_action(ack, body, client, selected: str) -> None:
        ack()

        channel_id = body.get("channel", {}).get("id")
        message_ts = body.get("message", {}).get("ts")
        actor_id = body.get("user", {}).get("id")
        raw_value = (body.get("actions") or [{}])[0].get("value")

        if not channel_id or not message_ts or not actor_id or not raw_value:
            return

        try:
            payload = json.loads(raw_value)
        except Exception:
            logger.warning("Invalid tutorial help payload")
            return

        owner_id = payload.get("owner_id")
        if not owner_id:
            return

        if actor_id != owner_id:
            client.chat_postEphemeral(
                channel=channel_id,
                user=actor_id,
                text=f"Only <@{owner_id}> can switch this tutorial view.",
            )
            return

        blocks = _build_tutorial_blocks(owner_id, selected)

        try:
            client.chat_update(
                channel=channel_id,
                ts=message_ts,
                text="How to submit a purchase request",
                blocks=blocks,
            )
        except Exception:
            logger.exception("Failed to update tutorial help view")

    @bolt_app.action("tutorial_help_purchase")
    def handle_tutorial_help_purchase(ack, body, client):
        _handle_tutorial_help_action(ack, body, client, "purchase")

    @bolt_app.action("tutorial_help_bigorder")
    def handle_tutorial_help_bigorder(ack, body, client):
        _handle_tutorial_help_action(ack, body, client, "bigorder")

    @bolt_app.action("delete_tutorial_message")
    def handle_delete_tutorial_message(ack, body, client):
        ack()

        channel_id = body.get("channel", {}).get("id")
        message_ts = body.get("message", {}).get("ts")
        actor_id = body.get("user", {}).get("id")
        raw_value = (body.get("actions") or [{}])[0].get("value")

        if not channel_id or not message_ts or not actor_id or not raw_value:
            return

        try:
            payload = json.loads(raw_value)
        except Exception:
            logger.warning("Invalid delete_tutorial_message payload")
            return

        owner_id = payload.get("owner_id") if isinstance(payload, dict) else None
        if owner_id and actor_id != owner_id:
            client.chat_postEphemeral(
                channel=channel_id,
                user=actor_id,
                text=f"Only <@{owner_id}> can delete this tutorial.",
            )
            return

        try:
            client.chat_delete(channel=channel_id, ts=message_ts)
        except Exception:
            logger.exception("Failed to delete tutorial message")

    @bolt_app.command("/reimburse")
    def handle_reimburse_command(ack, body, client):
        ack()

        user_id = body.get("user_id")
        channel_id = body.get("channel_id")
        raw_text = (body.get("text") or "").strip()

        if not user_id or not channel_id:
            return

        if channel_id != settings.manager_channel_id:
            client.chat_postEphemeral(
                channel=channel_id,
                user=user_id,
                text="Please run `/reimburse` in the manager channel.",
            )
            return

        m = re.match(r"^\s*([A-Za-z0-9_-]+)\s*,\s*(\$?-?\d+(?:\.\d+)?)\s*$", raw_text)
        if not m:
            client.chat_postEphemeral(
                channel=channel_id,
                user=user_id,
                text="Usage: `/reimburse reference_id, amount` (example: `/reimburse ADMIN-013, 20`)",
            )
            return

        reference_id = m.group(1).strip().upper()
        amount_raw = m.group(2).strip()

        prefix_match = re.match(r"^([A-Z]+)", reference_id)
        if not prefix_match:
            client.chat_postEphemeral(
                channel=channel_id,
                user=user_id,
                text=f"Invalid reference ID format: {reference_id}",
            )
            return

        prefix = prefix_match.group(1)
        tab_name = REFERENCE_ID_PREFIX_TO_TAB.get(prefix)
        if not tab_name:
            valid_prefixes = ", ".join(REFERENCE_ID_PREFIX_TO_TAB.keys())
            client.chat_postEphemeral(
                channel=channel_id,
                user=user_id,
                text=f"Unknown prefix `{prefix}`. Valid prefixes: {valid_prefixes}",
            )
            return

        try:
            amount = coerce_money(amount_raw)
        except Exception:
            amount = None

        if amount is None or amount <= 0:
            client.chat_postEphemeral(
                channel=channel_id,
                user=user_id,
                text=f"Invalid reimbursement amount: {amount_raw}",
            )
            return

        def run_reimburse() -> None:
            try:
                reimbursement_result = sheets.reimburse_by_id(
                    tab_name=tab_name,
                    reference_id=reference_id,
                    amount=float(amount),
                )
                if reimbursement_result is None:
                    client.chat_postMessage(
                        channel=channel_id,
                        text=f"❌ Could not find `{reference_id}` in *{tab_name}*.",
                    )
                    return

                bank_before = sheets.get_bank_available()
                bank_after = bank_before - float(reimbursement_result.amount_reimbursed)
                bank_updated = sheets.update_bank_available(bank_after)
                if not bank_updated:
                    client.chat_postMessage(
                        channel=channel_id,
                        text="❌ Reimbursement updated item spend, but failed to update bank balance. Please check _Config manually.",
                    )
                    return

                completed_at_utc = datetime.now(timezone.utc).isoformat()
                reimbursement_id = f"RB-{reference_id}-{int(time.time())}"
                sheets.append_reimbursement_log(
                    reimbursement_id=reimbursement_id,
                    requested_at_utc=completed_at_utc,
                    completed_at_utc=completed_at_utc,
                    status="completed",
                    manager_id=user_id,
                    person_reimbursed="unknown",
                    reference_id=reference_id,
                    subteam=tab_name,
                    item_name=reimbursement_result.item_name,
                    amount_requested_usd=float(reimbursement_result.amount_requested),
                    amount_reimbursed_usd=float(reimbursement_result.amount_reimbursed),
                    bank_before=float(bank_before),
                    bank_after=float(bank_after),
                    notes="",
                )

                client.chat_postMessage(
                    channel=channel_id,
                    text=(
                        f"✅ Reimbursement recorded for `{reference_id}` in *{tab_name}*.\n"
                        f"Moved {format_usd(float(reimbursement_result.amount_reimbursed))} from *Pending Spend* to *Actual Spend*.\n"
                        f"Bank: {format_usd(float(bank_before))} → {format_usd(float(bank_after))}"
                    ),
                )
            except Exception:
                logger.exception("Failed to process /reimburse for %s", reference_id)
                client.chat_postMessage(
                    channel=channel_id,
                    text="❌ Failed to process reimbursement. Please try again.",
                )

        EXECUTOR.submit(run_reimburse)

    @bolt_app.action("delete_bot_message")
    def handle_delete_bot_message(ack, body, client):
        ack()

        channel_id = body.get("channel", {}).get("id")
        message_ts = body.get("message", {}).get("ts")

        if not channel_id or not message_ts:
            return

        target_channel_id = None
        target_message_ts = None
        raw_value = (body.get("actions") or [{}])[0].get("value")
        if raw_value:
            try:
                payload = json.loads(raw_value)
                if isinstance(payload, dict):
                    target_channel_id = payload.get("target_channel_id")
                    target_message_ts = payload.get("target_message_ts")
            except Exception:
                logger.warning("Could not parse delete button payload")

        # If this button was posted for an invalid purchase request, also try
        # deleting the original request message that started the thread.
        if target_channel_id and target_message_ts:
            try:
                client.chat_delete(channel=target_channel_id, ts=target_message_ts)
            except Exception as e:
                logger.warning("Failed to delete target message %s: %s", target_message_ts, e)

        try:
            client.chat_delete(channel=channel_id, ts=message_ts)
        except Exception:
            logger.exception("Failed to delete bot message")

    @bolt_app.action("confirm_purchase")
    def handle_confirm_purchase(ack, body, client):
        ack()

        # Parse the confirmation data from the button payload.
        raw_value = (body.get("actions") or [{}])[0].get("value")
        if not raw_value:
            return

        try:
            confirmation_data = json.loads(raw_value)
        except Exception:
            logger.warning("Invalid confirm_purchase payload")
            return

        channel_id = confirmation_data.get("channel_id")
        user_id = confirmation_data.get("user_id")
        original_message_ts = confirmation_data.get("original_message_ts")
        if not all([channel_id, user_id, original_message_ts]):
            logger.warning("Missing required fields in confirm_purchase payload")
            return

        items = confirmation_data.get("items")
        if not isinstance(items, list) or not items:
            reference_id = confirmation_data.get("reference_id")
            subteam_tab = confirmation_data.get("subteam_tab")
            item_name = confirmation_data.get("item_name")
            requested_amount = confirmation_data.get("requested_amount")
            reason = confirmation_data.get("reason")
            is_unaccounted = confirmation_data.get("is_unaccounted", False)
            if not all([reference_id, subteam_tab, item_name, reason, requested_amount]):
                logger.warning("Missing item details in confirm_purchase payload")
                return
            items = [
                {
                    "line_number": 1,
                    "reference_id": reference_id,
                    "subteam_tab": subteam_tab,
                    "item_name": item_name,
                    "requested_amount": requested_amount,
                    "reason": reason,
                    "is_unaccounted": is_unaccounted,
                }
            ]
        receipt_link = confirmation_data.get("receipt_link")

        # Get the confirmation message timestamp from the action body (the message containing the button)
        confirmation_message_ts = body.get("message", {}).get("ts")

        # Verify that only the original requester can confirm
        button_clicked_by = body["user"]["id"]
        if button_clicked_by != user_id:
            client.chat_postMessage(
                channel=channel_id,
                thread_ts=original_message_ts,
                text=f"❌ This button can only be clicked by <@{user_id}> (the person who submitted the request). Please don't click this button if you're not the requester!"
            )
            return

        confirmation_key = (user_id, channel_id, original_message_ts)
        if confirmation_key in PENDING_CONFIRMATIONS:
            client.chat_postMessage(
                channel=channel_id,
                thread_ts=original_message_ts,
                text="⏳ Your purchase request is already being processed. Please wait for the manager's response."
            )
            return
        PENDING_CONFIRMATIONS.add(confirmation_key)

        if not receipt_link:
            lookup_min_ts = confirmation_message_ts or original_message_ts
            auto_link, auto_link_ts = _find_recent_receipt_link_for_user(
                client,
                channel_id,
                user_id,
                min_message_ts=lookup_min_ts,
            )
            if auto_link and _is_recent_slack_message_ts(auto_link_ts, RECEIPT_AUTO_LOOKBACK_SECONDS):
                receipt_link = auto_link
                confirmation_data["receipt_link"] = auto_link
            else:
                PENDING_CONFIRMATIONS.discard(confirmation_key)
                client.chat_postMessage(
                    channel=channel_id,
                    thread_ts=original_message_ts,
                    text="Please send your receipt image in the channel (not in this thread), then click Confirm again.",
                )
                return

        def run() -> None:
            try:
                manager_bundle_items: list[dict[str, Any]] = []
                for item in items:
                    subteam_tab = str(item["subteam_tab"])
                    lines = sheets.get_budget_lines(tab_name=subteam_tab)
                    report = build_budget_report(
                        subteam=subteam_tab,
                        reference_id=str(item["reference_id"]),
                        item_name=str(item["item_name"]),
                        requested_amount=float(item["requested_amount"]),
                        lines=lines,
                        is_unaccounted=bool(item.get("is_unaccounted", False)),
                    )
                    if report.status.value == "ITEM_NOT_FOUND" and not bool(item.get("is_unaccounted", False)):
                        _post_thread_message_with_delete_button(
                            client=client,
                            channel_id=channel_id,
                            thread_ts=original_message_ts,
                            message_text=(
                                f"Reference ID `{item['reference_id']}` was not found in `{subteam_tab}` at confirmation time. "
                                "Please verify and submit again."
                            ),
                        )
                        return
                    manager_bundle_items.append(
                        {
                            "line_number": int(item["line_number"]),
                            "report": report,
                            "reason": str(item["reason"]),
                            "raw_item": item,
                        }
                    )

                try:
                    counter = sheets.get_and_increment_request_counter()
                    request_id = f"REQ-{counter:06d}"
                except Exception as e:
                    logger.warning("Failed to get request counter from Sheets: %s", e)
                    client.chat_postMessage(
                        channel=channel_id,
                        thread_ts=original_message_ts,
                        text=(
                            "⚠️ Could not submit your request right now because the request counter is temporarily unavailable. "
                            "Please try again in a minute."
                        ),
                    )
                    return

                total_amount = _bundle_total_amount(items)
                if len(manager_bundle_items) == 1:
                    only_item = manager_bundle_items[0]
                    blocks = format_manager_notification_blocks(
                        only_item["report"],
                        user_id,
                        request_id=request_id,
                        purchase_reason=only_item["reason"],
                        item_budget_reject_threshold_percent_of_estimate=settings.item_budget_reject_threshold_percent_of_estimate,
                    )
                else:
                    blocks = format_manager_bundle_notification_blocks(
                        manager_bundle_items,
                        user_id,
                        request_id=request_id,
                        total_amount=total_amount,
                        item_budget_reject_threshold_percent_of_estimate=settings.item_budget_reject_threshold_percent_of_estimate,
                    )

                manager_post = client.chat_postMessage(
                    channel=settings.manager_channel_id,
                    text=f"Purchase request {request_id} from <@{user_id}>",
                    blocks=blocks,
                    unfurl_links=False,
                    unfurl_media=False,
                )

                manager_msg_ts = manager_post["ts"]
                client.chat_postMessage(
                    channel=settings.manager_channel_id,
                    thread_ts=manager_msg_ts,
                    text=f"Receipt for {request_id}:",
                    attachments=[{
                        "fallback": "Receipt image",
                        "image_url": receipt_link,
                    }],
                )

                PENDING_APPROVALS[manager_msg_ts] = {
                    "request_id": request_id,
                    "user_id": user_id,
                    "items": items,
                    "original_channel_id": channel_id,
                    "original_message_ts": original_message_ts,
                }

                submitted_at_utc = datetime.now(timezone.utc).isoformat()
                try:
                    bank_available_before = sheets.get_bank_available()
                except Exception as e:
                    logger.warning("Failed to read bank_available: %s", e)
                    bank_available_before = None

                for bundle_item in manager_bundle_items:
                    report = bundle_item["report"]
                    raw_item = bundle_item["raw_item"]
                    sheets.append_purchase_log(
                        request_id=request_id,
                        bundle_line_number=int(raw_item["line_number"]),
                        submitted_at_utc=submitted_at_utc,
                        requester_id=user_id,
                        subteam=str(raw_item["subteam_tab"]),
                        reference_id=str(raw_item["reference_id"]),
                        item_name=str(raw_item["item_name"]),
                        purchase_reason=str(raw_item["reason"]),
                        amount_usd=float(raw_item["requested_amount"]),
                        is_unaccounted=bool(raw_item.get("is_unaccounted", False)),
                        subteam_available_before=report.available_budget,
                        bank_available_before=bank_available_before,
                        receipt_link=receipt_link,
                        bot_assessment=_recommendation_header(
                            report,
                            settings.item_budget_reject_threshold_percent_of_estimate,
                        ),
                    )

                client.chat_postMessage(
                    channel=channel_id,
                    thread_ts=original_message_ts,
                    text=(
                        f"✅ Purchase request submitted! Request ID: *{request_id}*\n"
                        f"Items: *{len(items)}* | Total: *{format_usd(total_amount)}*\n"
                        "This has been forwarded to the manager channel for review."
                    )
                )

                if confirmation_message_ts:
                    try:
                        client.chat_delete(channel=channel_id, ts=confirmation_message_ts)
                    except Exception as e:
                        logger.warning("Failed to delete confirmation message: %s", e)

            except Exception:
                logger.exception("Error processing purchase request (user=%s)", user_id)
                _post_thread_message_with_delete_button(
                    client=client,
                    channel_id=channel_id,
                    thread_ts=original_message_ts,
                    message_text="An error occurred while processing your request. Please try again.",
                )
            finally:
                PENDING_CONFIRMATIONS.discard(confirmation_key)

        EXECUTOR.submit(run)
    
    @bolt_app.action("cancel_purchase")
    def handle_cancel_purchase(ack, body, client):
        ack()

        raw_value = (body.get("actions") or [{}])[0].get("value")
        if not raw_value:
            return

        try:
            confirmation_data = json.loads(raw_value)
        except Exception:
            logger.warning("Invalid cancel_purchase payload")
            return

        channel_id = confirmation_data.get("channel_id")
        user_id = confirmation_data.get("user_id")
        original_message_ts = confirmation_data.get("original_message_ts")
        if not channel_id or not user_id or not original_message_ts:
            return

        confirmation_message_ts = body.get("message", {}).get("ts")
        
        # Verify that only the original requester can cancel
        button_clicked_by = body["user"]["id"]
        if button_clicked_by != user_id:
            client.chat_postMessage(
                channel=channel_id,
                thread_ts=original_message_ts,
                text=f"❌ Only <@{user_id}> can cancel this purchase request."
            )
            return

        confirmation_key = (user_id, channel_id, original_message_ts)
        if confirmation_key in PENDING_CONFIRMATIONS:
            client.chat_postMessage(
                channel=channel_id,
                thread_ts=original_message_ts,
                text="⏳ This request has already been acted on."
            )
            return
        PENDING_CONFIRMATIONS.add(confirmation_key)

        if confirmation_message_ts:
            try:
                client.chat_delete(channel=channel_id, ts=confirmation_message_ts)
            except Exception as e:
                logger.warning("Failed to delete cancellation confirmation message: %s", e)
        
        client.chat_postMessage(
            channel=channel_id,
            thread_ts=original_message_ts,
            text="Purchase request cancelled, please delete your original message."
        )

    @bolt_app.event("message")
    def handle_message_event(ack, event, client):
        ack()

        # Ignore bot messages
        if event.get("bot_id"):
            return

        thread_ts = event.get("thread_ts")

        # Non-thread channel messages no longer trigger purchase parsing.
        # Intake now happens via /purchase and /bigorder slash commands.
        if not thread_ts:
            return
        
        # Handle thread messages in manager channel
        channel_id = event.get("channel")
        
        # Check if this is a thread in the manager channel
        if channel_id == settings.manager_channel_id:
            # Check for approval/rejection messages
            approval_data = PENDING_APPROVALS.get(thread_ts)
            text = (event.get("text") or "").strip()
            manager_id = event.get("user")

            is_approved, is_rejected, approved_line_numbers = _parse_manager_decision_text(text)

            if not approval_data and not (is_approved or is_rejected):
                # Downtime recovery: if a manager decision was posted while the bot was down,
                # recover it from thread history and process it now.
                request_id = _resolve_request_id_for_manager_thread(client, channel_id, thread_ts)
                if request_id:
                    try:
                        log_entries = sheets.get_purchase_log_entries(request_id=request_id)
                    except Exception:
                        logger.exception("Failed loading Purchases_Log entry for decision recovery: %s", request_id)
                        log_entries = []

                    if log_entries and all(entry.status == "under_review" for entry in log_entries):
                        recovered_decision = _recover_manager_decision_from_thread(
                            client=client,
                            channel_id=channel_id,
                            thread_ts=thread_ts,
                        )
                        if recovered_decision:
                            recovered_is_approved, recovered_manager_id, recovered_approved_line_numbers = recovered_decision
                            is_approved = recovered_is_approved
                            is_rejected = not recovered_is_approved
                            approved_line_numbers = recovered_approved_line_numbers
                            manager_id = recovered_manager_id

            if (is_approved or is_rejected) and not approval_data:
                request_id = _resolve_request_id_for_manager_thread(client, channel_id, thread_ts)
                if request_id:
                    try:
                        log_entries = sheets.get_purchase_log_entries(request_id=request_id)
                    except Exception:
                        logger.exception("Failed loading Purchases_Log entry for %s", request_id)
                        log_entries = []

                    if log_entries and all(entry.status == "under_review" for entry in log_entries):
                        first_entry = log_entries[0]
                        approval_data = {
                            "user_id": first_entry.requester_id,
                            "request_id": first_entry.request_id,
                            "items": [
                                {
                                    "line_number": entry.bundle_line_number,
                                    "subteam_tab": entry.subteam,
                                    "reference_id": entry.reference_id,
                                    "item_name": entry.item_name,
                                    "requested_amount": entry.amount_usd,
                                    "is_unaccounted": entry.is_unaccounted,
                                    "reason": entry.purchase_reason,
                                }
                                for entry in log_entries
                            ],
                            "original_channel_id": None,
                            "original_message_ts": None,
                        }
                        PENDING_APPROVALS[thread_ts] = approval_data
                    elif log_entries and all(entry.status in {"approved", "rejected"} for entry in log_entries):
                        client.chat_postMessage(
                            channel=settings.manager_channel_id,
                            thread_ts=thread_ts,
                            text=(
                                f"Request *{request_id}* has already been reviewed "
                                f"(status: *{log_entries[0].status}*)."
                            ),
                        )
                        return

            if approval_data and (is_approved or is_rejected):
                if is_approved and approved_line_numbers is not None:
                    valid_line_numbers = {int(item["line_number"]) for item in approval_data.get("items") or []}
                    invalid_line_numbers = sorted(
                        line_number
                        for line_number in approved_line_numbers
                        if line_number not in valid_line_numbers
                    )
                    if invalid_line_numbers:
                        client.chat_postMessage(
                            channel=settings.manager_channel_id,
                            thread_ts=thread_ts,
                            text=(
                                "Invalid item number(s) in approval message: "
                                f"{', '.join(str(n) for n in invalid_line_numbers)}.\n"
                                "Use item numbers shown in the request (example: `✅ 1 2 3`)."
                            ),
                        )
                        return

                # Remove from pending to prevent duplicate processing
                PENDING_APPROVALS.pop(thread_ts, None)

                _submit_manager_decision_processing(
                    approval_data=approval_data,
                    is_approved=is_approved,
                    approved_line_numbers=approved_line_numbers,
                    manager_id=manager_id,
                    thread_ts=thread_ts,
                )
                return
        
        # Handle manager rejection-reason capture flow
        _prune_pending_rejection_reasons()
        
        pending_rejection = PENDING_REJECTION_REASONS.get(thread_ts)
        if not pending_rejection and channel_id == settings.manager_channel_id and thread_ts:
            # Restart recovery: if rejected-without-reason exists in Purchases_Log,
            # rebuild pending rejection context from sheet data.
            candidate_text = (event.get("text") or "").strip()
            decision_is_approved, decision_is_rejected, _ = _parse_manager_decision_text(candidate_text)
            if candidate_text and not (decision_is_approved or decision_is_rejected):
                request_id = _resolve_request_id_for_manager_thread(client, channel_id, thread_ts)
                if request_id:
                    try:
                        log_entries = sheets.get_purchase_log_entries(request_id=request_id)
                    except Exception:
                        logger.exception("Failed loading Purchases_Log entry for rejection recovery: %s", request_id)
                        log_entries = []

                    manager_id = event.get("user")
                    first_entry = log_entries[0] if log_entries else None
                    if (
                        first_entry
                        and all(entry.status == "rejected" for entry in log_entries)
                        and all(not entry.rejection_reason for entry in log_entries)
                        and first_entry.manager_id
                        and manager_id == first_entry.manager_id
                    ):
                        pending_rejection = {
                            "created_at": time.time(),
                            "request_id": first_entry.request_id,
                            "requester_id": first_entry.requester_id,
                            "manager_id": first_entry.manager_id,
                            "items": [
                                {
                                    "line_number": entry.bundle_line_number,
                                    "reference_id": entry.reference_id,
                                    "item_name": entry.item_name,
                                    "requested_amount": entry.amount_usd,
                                }
                                for entry in log_entries
                            ],
                            "total_amount": sum(entry.amount_usd for entry in log_entries),
                            "original_channel_id": None,
                            "original_message_ts": None,
                        }
                        PENDING_REJECTION_REASONS[thread_ts] = pending_rejection

        if not pending_rejection:
            return

        if event.get("user") != pending_rejection["manager_id"]:
            return

        reason_text = (event.get("text") or "").strip()
        if not reason_text:
            return

        def run() -> None:
            try:
                requester_id = str(pending_rejection["requester_id"])
                manager_id = str(pending_rejection["manager_id"])
                request_id = str(pending_rejection["request_id"])
                original_channel_id = pending_rejection.get("original_channel_id")
                original_message_ts = pending_rejection.get("original_message_ts")
                items = pending_rejection.get("items") or []
                total_amount = float(pending_rejection.get("total_amount") or 0.0)

                if original_channel_id and original_message_ts:
                    try:
                        client.reactions_add(
                            channel=original_channel_id,
                            timestamp=original_message_ts,
                            name="x"
                        )
                    except Exception as e:
                        logger.warning("Failed to add X reaction: %s", e)

                item_lines = "\n".join(
                    f"Item {item['line_number']}: {item['reference_id']} | {item['item_name']} | {format_usd(float(item['requested_amount']))}"
                    for item in items
                )
                client.chat_postMessage(
                    channel=requester_id,
                    text=(
                        f"❌ Your purchase request was *rejected* by <@{manager_id}>.\n\n"
                        f"*Request ID:* {request_id}\n"
                        f"*Total Amount:* {format_usd(total_amount)}\n"
                        f"*Items:*\n{item_lines}\n"
                        f"*Reason:* {reason_text}"
                    ),
                )

                sheets.update_purchase_log_rejection_reason(
                    request_id=request_id,
                    rejection_reason=reason_text,
                )

                client.chat_postMessage(
                    channel=settings.manager_channel_id,
                    thread_ts=thread_ts,
                    text=f"Sent rejection reason to <@{requester_id}>.",
                )
            except Exception:
                logger.exception("Error forwarding rejection reason for message_ts=%s", thread_ts)
            finally:
                PENDING_REJECTION_REASONS.pop(thread_ts, None)

        EXECUTOR.submit(run)

    server = Flask(__name__)

    @server.get("/healthz")
    def healthz():
        return {"ok": True}

    @server.post(settings.slack_commands_path)
    def slack_commands():
        # Bolt performs Slack signature verification via signing_secret.
        return handler.handle(request)

    scanner_enabled_raw = os.getenv("ENABLE_MANAGER_DECISION_SCANNER", "false").strip().lower()
    scanner_enabled = scanner_enabled_raw not in {"0", "false", "no", "off"}
    scan_interval = MANAGER_DECISION_SCAN_INTERVAL_SECONDS
    scan_interval_raw = os.getenv("MANAGER_DECISION_SCAN_INTERVAL_SECONDS", "").strip()
    if scan_interval_raw:
        try:
            scan_interval = max(5, int(scan_interval_raw))
        except ValueError:
            logger.warning(
                "Invalid MANAGER_DECISION_SCAN_INTERVAL_SECONDS=%r, using default %s",
                scan_interval_raw,
                MANAGER_DECISION_SCAN_INTERVAL_SECONDS,
            )

    if scanner_enabled:
        scanner_thread = threading.Thread(
            target=_scan_manager_channel_for_missed_decisions,
            args=(scan_interval,),
            name="manager-decision-scanner",
            daemon=True,
        )
        scanner_thread.start()
        logger.info(
            "Started manager decision scanner (interval=%ss, history_limit=%s)",
            scan_interval,
            MANAGER_DECISION_SCAN_HISTORY_LIMIT,
        )

    return server, bolt_app


# Module-level server initialization for gunicorn/production WSGI servers
settings = load_settings()
server, bolt_app = create_server(settings)


if __name__ == "__main__":
    # When running locally with `python app.py`, use the module-level server
    port = int(os.getenv("PORT", str(settings.port)))
    server.run(host="0.0.0.0", port=port)
