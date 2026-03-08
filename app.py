from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any

from flask import Flask, request
from gspread.exceptions import WorksheetNotFound
from slack_bolt import App
from slack_bolt.adapter.flask import SlackRequestHandler

from budget_checker import build_budget_report
from config import ConfigError, Settings, load_settings
from formatters import _recommendation_header, format_manager_notification_blocks
from parser import REFERENCE_ID_PREFIX_TO_TAB, parse_purchase_text
from sheets_client import SheetsClient, SheetsClientError
from utils import coerce_money, format_usd


def _request_id_fallback() -> str:
    """Fallback UUID when Sheets counter fails."""
    return f"REQ-{uuid.uuid4().hex[:8].upper()}"


EXECUTOR = ThreadPoolExecutor(max_workers=4)
PENDING_APPROVALS: dict[str, dict[str, Any]] = {}  # message_ts -> request metadata
PENDING_REJECTION_REASONS: dict[str, dict[str, Any]] = {}  # manager message_ts -> rejection metadata
PENDING_CONFIRMATIONS: set[tuple[str, str, str]] = set()  # (user_id, channel_id, original_message_ts) -> in-flight
REJECTION_REASON_TIMEOUT_SECONDS = 600


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )


def _prune_pending_rejection_reasons() -> None:
    now = time.time()
    expired = [
        message_ts
        for message_ts, payload in PENDING_REJECTION_REASONS.items()
        if now - float(payload.get("created_at", 0)) > REJECTION_REASON_TIMEOUT_SECONDS
    ]
    for message_ts in expired:
        PENDING_REJECTION_REASONS.pop(message_ts, None)


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

    @bolt_app.command("/tutorial")
    def handle_tutorial_command(ack, body, client):
        ack()

        requester_id = body.get("user_id")
        channel_id = body.get("channel_id")
        command_keyword = settings.purchase_command_keyword

        if not requester_id or not channel_id:
            return

        delete_value = json.dumps({"owner_id": requester_id})
        blocks = [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": "FEB Purchase Bot Tutorial"},
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        "*How to submit a purchase request*\n"
                        f"1. Send a message that starts with `{command_keyword}`\n"
                        "2. Use this format: `<reference_id>, <amount>, <reason>`\n"
                        "3. Attach your receipt image to the same message\n"
                        "4. Click *Confirm* when the bot asks for confirmation"
                    ),
                },
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        "*Example*\n"
                        f"`{command_keyword} ADMIN-001, 50.00, Need for supplies`\n"
                        "(with a receipt image attached to the same message)"
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
                        "- Use `/reimburse <reference_id>, <amount>` when reimbursement is completed\n"
                        "- If rejected, the manager must provide a reason"
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
                        "action_id": "delete_bot_message",
                        "value": delete_value,
                    }
                ],
            },
        ]

        client.chat_postMessage(
            channel=channel_id,
            text="How to submit a purchase request",
            blocks=blocks,
        )

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
                text="Usage: `/reimburse <reference_id>, <amount>` (example: `/reimburse ADMIN-013, 20`)",
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
                success = sheets.reimburse_by_id(
                    tab_name=tab_name,
                    reference_id=reference_id,
                    amount=float(amount),
                )
                if not success:
                    client.chat_postMessage(
                        channel=channel_id,
                        text=f"❌ Could not find `{reference_id}` in *{tab_name}*.",
                    )
                    return

                client.chat_postMessage(
                    channel=channel_id,
                    text=(
                        f"✅ Reimbursement recorded for `{reference_id}` in *{tab_name}*.\n"
                        f"Moved {format_usd(float(amount))} from *Pending Spend* to *Actual Spend*."
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
        confirmation_data = json.loads(body["actions"][0]["value"])
        reference_id = confirmation_data["reference_id"]
        subteam_tab = confirmation_data["subteam_tab"]
        item_name = confirmation_data["item_name"]
        requested_amount = confirmation_data["requested_amount"]
        reason = confirmation_data["reason"]
        channel_id = confirmation_data["channel_id"]
        user_id = confirmation_data["user_id"]
        original_message_ts = confirmation_data["original_message_ts"]
        receipt_link = confirmation_data.get("receipt_link")
        is_unaccounted = confirmation_data.get("is_unaccounted", False)
        
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

        # Check idempotency: prevent duplicate confirmation processing from the original requester
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
            PENDING_CONFIRMATIONS.discard(confirmation_key)
            _post_thread_message_with_delete_button(
                client=client,
                channel_id=channel_id,
                thread_ts=original_message_ts,
                message_text="No receipt image found. Please attach an image to your purchase request message.",
            )
            return

        def run() -> None:
            try:
                # Build budget report by reference_id lookup
                lines = sheets.get_budget_lines(tab_name=subteam_tab)
                report = build_budget_report(
                    subteam=subteam_tab,
                    reference_id=reference_id,
                    item_name=item_name,
                    requested_amount=float(requested_amount),
                    lines=lines,
                    is_unaccounted=is_unaccounted,
                )

                if report.status.value == "ITEM_NOT_FOUND" and not is_unaccounted:
                    _post_thread_message_with_delete_button(
                        client=client,
                        channel_id=channel_id,
                        thread_ts=original_message_ts,
                        message_text=(
                            f"Reference ID `{reference_id}` was not found in `{subteam_tab}` at confirmation time. "
                            "Please verify and submit again."
                        ),
                    )
                    return

                # Assign request ID
                try:
                    counter = sheets.get_and_increment_request_counter()
                    request_id = f"REQ-{counter:06d}"
                except Exception as e:
                    logger.warning("Failed to get request counter from Sheets, using fallback: %s", e)
                    request_id = _request_id_fallback()

                # Post to manager channel with report and receipt image
                blocks = format_manager_notification_blocks(
                    report, 
                    user_id, 
                    request_id=request_id,
                    purchase_reason=reason
                )
                manager_post = client.chat_postMessage(
                    channel=settings.manager_channel_id,
                    text=f"Purchase request {request_id} from <@{user_id}>",
                    blocks=blocks,
                    unfurl_links=False,
                    unfurl_media=False,
                )

                # Post receipt in manager thread
                manager_msg_ts = manager_post["ts"]
                if receipt_link:
                    client.chat_postMessage(
                        channel=settings.manager_channel_id,
                        thread_ts=manager_msg_ts,
                        text=f"Receipt for {request_id}:",
                        attachments=[{
                            "fallback": "Receipt image",
                            "image_url": receipt_link,
                        }],
                    )
                else:
                    client.chat_postMessage(
                        channel=settings.manager_channel_id,
                        thread_ts=manager_msg_ts,
                        text="No receipt image available",
                    )

                # Store metadata for approval tracking
                PENDING_APPROVALS[manager_msg_ts] = {
                    "request_id": request_id,
                    "user_id": user_id,
                    "subteam_tab": subteam_tab,
                    "reference_id": reference_id,
                    "item_name": item_name,
                    "requested_amount": float(requested_amount),
                    "original_channel_id": channel_id,
                    "original_message_ts": original_message_ts,
                    "is_unaccounted": is_unaccounted,
                }

                # Log purchase request to Purchases_Log tab
                submitted_at_utc = datetime.now(timezone.utc).isoformat()
                
                # Read before values for budget tracking
                subteam_available_before = report.available_budget
                try:
                    bank_available_before = sheets.get_bank_available()
                except Exception as e:
                    logger.warning("Failed to read bank_available: %s", e)
                    bank_available_before = None
                
                sheets.append_purchase_log(
                    request_id=request_id,
                    submitted_at_utc=submitted_at_utc,
                    requester_id=user_id,
                    subteam=subteam_tab,
                    reference_id=reference_id,
                    item_name=item_name,
                    purchase_reason=reason,
                    amount_usd=float(requested_amount),
                    subteam_available_before=subteam_available_before,
                    bank_available_before=bank_available_before,
                    receipt_link=receipt_link,
                    bot_assessment=_recommendation_header(report),
                )

                # Send confirmation in thread
                client.chat_postMessage(
                    channel=channel_id,
                    thread_ts=original_message_ts,
                    text=f"✅ Purchase request submitted! Request ID: *{request_id}*\nThis has been forwarded to the manager channel for review."
                )

                # Delete the confirmation button message so it can't be clicked again
                if confirmation_message_ts:
                    try:
                        client.chat_delete(
                            channel=channel_id,
                            ts=confirmation_message_ts
                        )
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
                # Clear from pending confirmations once processing completes
                PENDING_CONFIRMATIONS.discard(confirmation_key)

        EXECUTOR.submit(run)
    
    @bolt_app.action("cancel_purchase")
    def handle_cancel_purchase(ack, body, client):
        ack()
        
        confirmation_data = json.loads(body["actions"][0]["value"])
        channel_id = confirmation_data["channel_id"]
        user_id = confirmation_data["user_id"]
        original_message_ts = confirmation_data["original_message_ts"]
        
        # Verify that only the original requester can cancel
        button_clicked_by = body["user"]["id"]
        if button_clicked_by != user_id:
            client.chat_postMessage(
                channel=channel_id,
                thread_ts=original_message_ts,
                text=f"❌ Only <@{user_id}> can cancel this purchase request."
            )
            return
        
        client.chat_postMessage(
            channel=channel_id,
            thread_ts=original_message_ts,
            text="Purchase request cancelled."
        )

    @bolt_app.event("message")
    def handle_message_event(ack, event, client):
        ack()

        # Ignore bot messages
        if event.get("bot_id"):
            return

        thread_ts = event.get("thread_ts")
        
        # Handle non-thread messages (purchase requests)
        if not thread_ts:
            text = (event.get("text") or "").strip()
            
            # Check if this is a purchase request (starts with configured keyword)
            keyword_lower = settings.purchase_command_keyword.lower()
            keyword_with_space = keyword_lower.rstrip(":")
            if text.lower().startswith(keyword_lower) or text.lower().startswith(keyword_with_space + " "):
                # Extract the purchase details after the keyword
                if text.lower().startswith(keyword_lower):
                    purchase_text = text[len(keyword_lower):].strip()
                else:
                    purchase_text = text[len(keyword_with_space):].strip()
                
                user_id = event.get("user")
                channel_id = event.get("channel")
                files = event.get("files") or []
                
                def run() -> None:
                    try:
                        # Validate purchase format
                        parse = parse_purchase_text(purchase_text)
                        if not parse.ok:
                            _post_thread_message_with_delete_button(
                                client=client,
                                channel_id=channel_id,
                                thread_ts=event["ts"],
                                message_text="Invalid purchase request. Use `/tutorial` to learn how to use the bot.",
                            )
                            return
                        
                        # Validate image attachment
                        if not files:
                            _post_thread_message_with_delete_button(
                                client=client,
                                channel_id=channel_id,
                                thread_ts=event["ts"],
                                message_text="Please attach a receipt image with your purchase request.",
                            )
                            return
                        
                        # Verify subteam tab exists and fetch item details
                        try:
                            budget_lines = sheets.get_budget_lines(tab_name=parse.subteam_tab)
                            # Find the matching reference_id to get item_name
                            item_name = ""
                            
                            # For unaccounted items (-000), use the provided item name
                            if parse.is_unaccounted:
                                item_name = parse.provided_item_name or ""
                            else:
                                # Look up existing item in budget
                                for line in budget_lines:
                                    if line.reference_id.upper() == parse.reference_id.upper():
                                        item_name = line.item_name
                                        break
                                if not item_name:
                                    _post_thread_message_with_delete_button(
                                        client=client,
                                        channel_id=channel_id,
                                        thread_ts=event["ts"],
                                        message_text=(
                                            f"Reference ID `{parse.reference_id}` was not found in "
                                            f"`{parse.subteam_tab}`.\n"
                                            "Use an existing reference ID, or use a `-000` ID for unaccounted items."
                                        ),
                                    )
                                    return
                        except WorksheetNotFound:
                            _post_thread_message_with_delete_button(
                                client=client,
                                channel_id=channel_id,
                                thread_ts=event["ts"],
                                message_text=f'No tab found for subteam "{parse.subteam_tab}".',
                            )
                            return
                        except SheetsClientError as e:
                            _post_thread_message_with_delete_button(
                                client=client,
                                channel_id=channel_id,
                                thread_ts=event["ts"],
                                message_text=f"Error accessing budget data: {str(e)}",
                            )
                            return
                        
                        # Extract receipt link from file object
                        receipt_link = None
                        if files:
                            file_obj = files[0]
                            # Try to get permalink from file object
                            if "permalink" in file_obj:
                                receipt_link = file_obj["permalink"]
                            elif "url_private" in file_obj:
                                receipt_link = file_obj["url_private"]
                            elif "id" in file_obj:
                                # Construct Slack file URL from file ID as fallback
                                receipt_link = f"https://files.slack.com/files-pri/{file_obj.get('id', '')}"
                            
                            # If still no link, try to fetch via files.info API
                            if not receipt_link and "id" in file_obj:
                                try:
                                    file_info = client.files_info(file=file_obj["id"])
                                    if file_info.get("file"):
                                        receipt_link = file_info["file"].get("permalink") or file_info["file"].get("url_private")
                                except Exception as e:
                                    logger.warning("Failed to get file info for %s: %s", file_obj.get("id"), e)
                        
                        # Build confirmation data
                        confirmation_data = {
                            "reference_id": parse.reference_id,
                            "subteam_tab": parse.subteam_tab,
                            "item_name": item_name,  # Fetched from sheet above
                            "requested_amount": parse.requested_amount,
                            "reason": parse.reason,
                            "channel_id": channel_id,
                            "user_id": user_id,
                            "original_message_ts": event["ts"],
                            "receipt_link": receipt_link,
                            "is_unaccounted": parse.is_unaccounted,
                        }
                        
                        # Show confirmation blocks
                        blocks = [
                            {
                                "type": "section",
                                "text": {
                                    "type": "mrkdwn",
                                    "text": f"*<@{user_id}>, please confirm your purchase request:*"
                                }
                            },
                            {
                                "type": "section",
                                "fields": [
                                    {"type": "mrkdwn", "text": f"*Reference ID:*\n{parse.reference_id}"},
                                    {"type": "mrkdwn", "text": f"*Item:*\n{item_name}"},
                                    {"type": "mrkdwn", "text": f"*Amount:*\n{format_usd(parse.requested_amount)}"},
                                    {"type": "mrkdwn", "text": f"*Reason:*\n{parse.reason}"},
                                ]
                            },
                            {
                                "type": "context",
                                "elements": [{"type": "mrkdwn", "text": "✅ Receipt image detected"}]
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
                                    }
                                ]
                            }
                        ]
                        
                        client.chat_postMessage(
                            channel=channel_id,
                            thread_ts=event["ts"],
                            text="Please confirm your purchase request:",
                            blocks=blocks
                        )
                        
                    except Exception:
                        logger.exception("Error processing purchase request message (user=%s)", user_id)
                        try:
                            _post_thread_message_with_delete_button(
                                client=client,
                                channel_id=channel_id,
                                thread_ts=event["ts"],
                                message_text="An unexpected error occurred while processing your request. Please try again.",
                            )
                        except Exception:
                            pass
                
                EXECUTOR.submit(run)
            return
        
        # Handle thread messages in manager channel
        channel_id = event.get("channel")
        
        # Check if this is a thread in the manager channel
        if channel_id == settings.manager_channel_id:
            # Check for approval/rejection messages
            approval_data = PENDING_APPROVALS.get(thread_ts)
            if approval_data:
                text = (event.get("text") or "").strip()
                manager_id = event.get("user")
                
                # Manager decisions are accepted only as a single emoji message.
                approve_tokens = {"✅", ":white_check_mark:", ":heavy_check_mark:"}
                reject_tokens = {"❌", ":x:", ":no_entry:"}
                is_approved = text in approve_tokens
                is_rejected = text in reject_tokens
                
                if is_approved or is_rejected:
                    # Remove from pending to prevent duplicate processing
                    PENDING_APPROVALS.pop(thread_ts, None)
                    
                    def run_approval() -> None:
                        try:
                            requester_id = approval_data["user_id"]
                            request_id = approval_data["request_id"]
                            item_name = approval_data["item_name"]
                            amount = approval_data["requested_amount"]
                            subteam_tab = approval_data["subteam_tab"]
                            reference_id = approval_data["reference_id"]
                            original_channel_id = approval_data.get("original_channel_id")
                            original_message_ts = approval_data.get("original_message_ts")
                            is_unaccounted = approval_data.get("is_unaccounted", False)
                            available_budget_before: float | None = None

                            if not is_unaccounted:
                                try:
                                    latest_lines = sheets.get_budget_lines(tab_name=subteam_tab, force_refresh=True)
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
                            
                            if is_approved:
                                # For unaccounted items, append to sheet first
                                if is_unaccounted:
                                    try:
                                        new_ref_id = sheets.append_budget_line(
                                            tab_name=subteam_tab,
                                            item_name=item_name,
                                            initial_spending=amount,
                                        )
                                        logger.info("Appended unaccounted item %r as %s", item_name, new_ref_id)
                                    except Exception:
                                        logger.exception("Failed to append unaccounted item to sheet")
                                else:
                                    # Update Google Sheet pending spending by reference_id
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

                                # Log approval to Purchases_Log
                                reviewed_at_utc = datetime.now(timezone.utc).isoformat()
                                
                                # Calculate after values
                                subteam_available_after = (
                                    available_budget_before - amount if available_budget_before is not None else None
                                )
                                
                                # Update bank balance
                                bank_available_after = None
                                try:
                                    current_bank = sheets.get_bank_available()
                                    bank_available_after = current_bank - amount
                                    sheets.update_bank_available(bank_available_after)
                                except Exception as e:
                                    logger.warning("Failed to update bank_available: %s", e)
                                
                                sheets.update_purchase_log_status(
                                    request_id=request_id,
                                    status="approved",
                                    reviewed_at_utc=reviewed_at_utc,
                                    manager_id=manager_id,
                                    subteam_available_after=subteam_available_after,
                                    bank_available_after=bank_available_after,
                                )

                                # Notify member of approval
                                client.chat_postMessage(
                                    channel=requester_id,
                                    text=(
                                        f"✅ Your purchase request was *approved* by <@{manager_id}>!\n\n"
                                        f"*Request ID:* {request_id}\n"
                                        f"*Item:* {item_name}\n"
                                        f"*Amount:* {format_usd(amount)}\n\n"
                                        f"The amount is now in Pending Spend."
                                    ),
                                )
                                
                                # React with checkmark on original purchase request message
                                if original_channel_id and original_message_ts:
                                    try:
                                        client.reactions_add(
                                            channel=original_channel_id,
                                            timestamp=original_message_ts,
                                            name="white_check_mark"
                                        )
                                    except Exception as e:
                                        logger.warning("Failed to add checkmark reaction: %s", e)
                                
                                # Confirm in manager thread
                                budget_change_text = ""
                                if available_budget_before is not None:
                                    available_budget_after = available_budget_before - amount
                                    budget_change_text = (
                                        "\n"
                                        f"*Subteam Available Budget:* {format_usd(available_budget_before)} "
                                        f"-> {format_usd(available_budget_after)}"
                                    )

                                client.chat_postMessage(
                                    channel=settings.manager_channel_id,
                                    thread_ts=thread_ts,
                                    text=(
                                        f"✅ Approved and logged. Notified <@{requester_id}>."
                                        f"{budget_change_text}"
                                    ),
                                )

                            else:  # is_rejected
                                # Log rejection to Purchases_Log (without reason yet, will be added later)
                                # For rejections, before = after (no budget change)
                                reviewed_at_utc = datetime.now(timezone.utc).isoformat()
                                
                                # Read before values from log to copy to after
                                subteam_before_for_copy = available_budget_before
                                try:
                                    bank_before_for_copy = sheets.get_bank_available()
                                except Exception:
                                    bank_before_for_copy = None
                                
                                sheets.update_purchase_log_status(
                                    request_id=request_id,
                                    status="rejected",
                                    reviewed_at_utc=reviewed_at_utc,
                                    manager_id=manager_id,
                                    subteam_available_after=subteam_before_for_copy,
                                    bank_available_after=bank_before_for_copy,
                                )

                                PENDING_REJECTION_REASONS[thread_ts] = {
                                    "created_at": time.time(),
                                    "request_id": request_id,
                                    "requester_id": requester_id,
                                    "manager_id": manager_id,
                                    "item_name": item_name,
                                    "requested_amount": amount,
                                    "original_channel_id": original_channel_id,
                                    "original_message_ts": original_message_ts,
                                }
                                
                                # Ask manager to provide reason in next message
                                client.chat_postMessage(
                                    channel=settings.manager_channel_id,
                                    thread_ts=thread_ts,
                                    text=(
                                        f"<@{manager_id}> please reply with the rejection reason.\n"
                                        f"I'll forward it to <@{requester_id}> via DM.\n\n"
                                        f"*Request ID:* {request_id}"
                                    ),
                                )
                        except Exception:
                            logger.exception("Error processing approval/rejection from manager thread")
                    
                    EXECUTOR.submit(run_approval)
                    return
        
        # Handle manager rejection-reason capture flow
        _prune_pending_rejection_reasons()
        
        pending_rejection = PENDING_REJECTION_REASONS.get(thread_ts)
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
                item_name = str(pending_rejection["item_name"])
                amount = float(pending_rejection["requested_amount"])
                request_id = str(pending_rejection["request_id"])
                original_channel_id = pending_rejection.get("original_channel_id")
                original_message_ts = pending_rejection.get("original_message_ts")

                # React with X on original purchase request message now that reason is provided
                if original_channel_id and original_message_ts:
                    try:
                        client.reactions_add(
                            channel=original_channel_id,
                            timestamp=original_message_ts,
                            name="x"
                        )
                    except Exception as e:
                        logger.warning("Failed to add X reaction: %s", e)

                client.chat_postMessage(
                    channel=requester_id,
                    text=(
                        f"❌ Your purchase request was *rejected* by <@{manager_id}>.\n\n"
                        f"*Request ID:* {request_id}\n"
                        f"*Item:* {item_name}\n"
                        f"*Amount:* {format_usd(amount)}\n"
                        f"*Reason:* {reason_text}"
                    ),
                )

                # Update log with rejection reason
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

    return server, bolt_app


if __name__ == "__main__":
    try:
        settings = load_settings()
    except ConfigError as e:
        raise SystemExit(f"Config error: {e}") from e

    server, _bolt_app = create_server(settings)
    port = int(os.getenv("PORT", str(settings.port)))
    server.run(host="0.0.0.0", port=port)
