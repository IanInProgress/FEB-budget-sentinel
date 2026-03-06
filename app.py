from __future__ import annotations

import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from flask import Flask, request
from gspread.exceptions import WorksheetNotFound
from slack_sdk.errors import SlackApiError
from slack_bolt import App
from slack_bolt.adapter.flask import SlackRequestHandler

from budget_checker import build_budget_report
from config import ConfigError, Settings, load_settings
from formatters import format_manager_notification_blocks
from parser import parse_purchase_text
from sheets_client import SheetsClient, SheetsClientError
from utils import format_usd


EXECUTOR = ThreadPoolExecutor(max_workers=4)
PENDING_ATTACHMENTS: dict[str, dict[str, Any]] = {}
PENDING_APPROVALS: dict[str, dict[str, Any]] = {}  # message_ts -> request metadata
ATTACHMENT_TIMEOUT_SECONDS = 120


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


def _prune_pending_attachments() -> None:
    now = time.time()
    expired = [
        thread_ts
        for thread_ts, payload in PENDING_ATTACHMENTS.items()
        if now - float(payload.get("created_at", 0)) > ATTACHMENT_TIMEOUT_SECONDS
    ]
    for thread_ts in expired:
        PENDING_ATTACHMENTS.pop(thread_ts, None)


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
        ack()

        text = (command.get("text") or "").strip()
        user_id = command.get("user_id")

        def run() -> None:
            try:
                parse = parse_purchase_text(text)
                if not parse.ok:
                    # Invalid syntax - notify user directly, don't send to manager channel
                    respond(parse.error_message or "Invalid command.")
                    return

                subteam_tab = _resolve_subteam_tab(parse.subteam or "", settings.subteam_aliases)
                if not subteam_tab:
                    # Invalid subteam - notify user directly, don't send to manager channel
                    respond("Missing subteam. Please specify a valid subteam.")
                    return

                # Verify subteam tab exists before showing confirmation
                try:
                    sheets.get_budget_lines(tab_name=subteam_tab)
                except WorksheetNotFound:
                    # Invalid subteam - notify user directly, don't send to manager channel
                    respond(f'No tab found for subteam "{parse.subteam}". (Resolved tab: "{subteam_tab}")')
                    return
                except SheetsClientError as e:
                    # Data error - notify user directly, don't send to manager channel
                    respond(f"Error accessing budget data: {str(e)}")
                    return

                # Show confirmation with request details
                confirmation_data = {
                    "subteam": parse.subteam,
                    "subteam_tab": subteam_tab,
                    "item_name": parse.item_name,
                    "requested_amount": parse.requested_amount,
                }
                
                blocks = [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": "*Please confirm your purchase request:*"
                        }
                    },
                    {
                        "type": "section",
                        "fields": [
                            {"type": "mrkdwn", "text": f"*Subteam:*\n{parse.subteam}"},
                            {"type": "mrkdwn", "text": f"*Item:*\n{parse.item_name}"},
                            {"type": "mrkdwn", "text": f"*Amount:*\n{format_usd(parse.requested_amount)}"},
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
                            }
                        ]
                    }
                ]
                
                respond({"blocks": blocks})
            except Exception:
                logger.exception("Unhandled error while handling /purchase (user=%s)", user_id)
                # System error - notify user directly, don't send to manager channel
                respond("An unexpected error occurred while processing your request. Please try again or contact support.")

        EXECUTOR.submit(run)

    @bolt_app.action("confirm_purchase")
    def handle_confirm_purchase(ack, body, client, respond):
        ack()

        user_id = body["user"]["id"]
        channel_id = body["channel"]["id"]

        # Parse the confirmation data from the button payload.
        confirmation_data = json.loads(body["actions"][0]["value"])
        subteam = confirmation_data["subteam"]
        subteam_tab = confirmation_data["subteam_tab"]
        item_name = confirmation_data["item_name"]
        requested_amount = confirmation_data["requested_amount"]

        _prune_pending_attachments()

        # Update ephemeral confirmation response and ask for screenshot upload in a thread.
        respond(
            text="Confirmed. Next step: upload your purchase screenshot in the thread I just posted (within 2 minutes).",
            replace_original=True,
        )

        def run() -> None:
            try:
                try:
                    post = client.chat_postMessage(
                        channel=channel_id,
                        text=(
                            f"<@{user_id}> Please upload your purchase screenshot in this thread within "
                            f"{ATTACHMENT_TIMEOUT_SECONDS // 60} minutes."
                        ),
                    )
                except SlackApiError as e:
                    if e.response.get("error") != "not_in_channel":
                        raise

                    # For public channels, joining can resolve this automatically.
                    try:
                        client.conversations_join(channel=channel_id)
                        post = client.chat_postMessage(
                            channel=channel_id,
                            text=(
                                f"<@{user_id}> Please upload your purchase screenshot in this thread within "
                                f"{ATTACHMENT_TIMEOUT_SECONDS // 60} minutes."
                            ),
                        )
                    except SlackApiError:
                        respond(
                            text=(
                                "I couldn't post in this channel because I'm not a member. "
                                "Please invite me to this channel and try /purchase again."
                            ),
                            replace_original=True,
                        )
                        return

                thread_ts = post["ts"]
                PENDING_ATTACHMENTS[thread_ts] = {
                    "created_at": time.time(),
                    "user_id": user_id,
                    "channel_id": channel_id,
                    "subteam": subteam,
                    "subteam_tab": subteam_tab,
                    "item_name": item_name,
                    "requested_amount": float(requested_amount),
                }
                client.chat_postMessage(
                    channel=channel_id,
                    thread_ts=thread_ts,
                    text="I will submit to managers as soon as you upload an image in this thread.",
                )
            except Exception:
                logger.exception("Error initializing attachment flow (user=%s)", user_id)
                respond(
                    text="An error occurred while preparing screenshot upload. Please try again.",
                    replace_original=True,
                )

        EXECUTOR.submit(run)
    
    @bolt_app.action("cancel_purchase")
    def handle_cancel_purchase(ack, respond):
        ack()
        respond(
            text="Purchase request cancelled.",
            replace_original=True,
        )

    @bolt_app.event("message")
    def handle_message_event(ack, event, client):
        ack()

        # Ignore bot messages and non-thread replies.
        if event.get("bot_id"):
            return
        thread_ts = event.get("thread_ts")
        if not thread_ts:
            return

        _prune_pending_attachments()
        pending = PENDING_ATTACHMENTS.get(thread_ts)
        if not pending:
            return

        # Only the requester can complete the pending attachment step.
        if event.get("user") != pending["user_id"]:
            return

        files = event.get("files") or []
        if not files:
            return

        def run() -> None:
            try:
                lines = sheets.get_budget_lines(tab_name=str(pending["subteam_tab"]))
                report = build_budget_report(
                    subteam=str(pending["subteam_tab"]),
                    requested_item=str(pending["item_name"]),
                    requested_amount=float(pending["requested_amount"]),
                    lines=lines,
                    fuzzy_suggestion_threshold=settings.fuzzy_suggestion_threshold,
                )

                blocks = format_manager_notification_blocks(report, str(pending["user_id"]))
                manager_post = client.chat_postMessage(
                    channel=settings.manager_channel_id,
                    text=f"Purchase request from <@{pending['user_id']}>",
                    blocks=blocks,
                    unfurl_links=False,
                    unfurl_media=False,
                )
                
                # Store metadata for approval tracking
                manager_msg_ts = manager_post["ts"]
                PENDING_APPROVALS[manager_msg_ts] = {
                    "user_id": str(pending["user_id"]),
                    "subteam_tab": str(pending["subteam_tab"]),
                    "item_name": str(pending["item_name"]),
                    "requested_amount": float(pending["requested_amount"]),
                    "matched_item": report.matched_item,
                }

                # Forward screenshot reference to manager channel.
                first_file = files[0]
                permalink = first_file.get("permalink") or first_file.get("url_private")
                if permalink:
                    client.chat_postMessage(
                        channel=settings.manager_channel_id,
                        text=f"Purchase screenshot from <@{pending['user_id']}>: {permalink}",
                        unfurl_links=True,
                        unfurl_media=True,
                    )

                client.chat_postMessage(
                    channel=str(pending["channel_id"]),
                    thread_ts=thread_ts,
                    text="Thanks, your request and screenshot were submitted to the manager channel.",
                )
            except Exception:
                logger.exception("Error finalizing request with attachment (user=%s)", pending.get("user_id"))
                client.chat_postMessage(
                    channel=str(pending["channel_id"]),
                    thread_ts=thread_ts,
                    text="I couldn't submit this request right now. Please run /purchase again.",
                )
            finally:
                PENDING_ATTACHMENTS.pop(thread_ts, None)

        EXECUTOR.submit(run)

    @bolt_app.event("reaction_added")
    def handle_reaction_added(ack, event, client):
        ack()

        # Only process reactions in manager channel
        if event.get("item", {}).get("channel") != settings.manager_channel_id:
            return

        reaction = event.get("reaction")
        message_ts = event.get("item", {}).get("ts")
        reactor_id = event.get("user")

        if not message_ts or not reaction:
            return

        # Check if this is a pending approval
        approval_data = PENDING_APPROVALS.get(message_ts)
        if not approval_data:
            return

        # Approve: white_check_mark (✅) or +1 (👍)
        # Reject: x (❌) or -1 (👎)
        is_approved = reaction in ["white_check_mark", "+1", "heavy_check_mark"]
        is_rejected = reaction in ["x", "-1", "no_entry"]

        if not (is_approved or is_rejected):
            return

        # Remove from pending to prevent duplicate processing
        PENDING_APPROVALS.pop(message_ts, None)

        def run() -> None:
            try:
                requester_id = approval_data["user_id"]
                item_name = approval_data["item_name"]
                amount = approval_data["requested_amount"]
                subteam_tab = approval_data["subteam_tab"]
                matched_item = approval_data.get("matched_item") or item_name

                if is_approved:
                    # Update Google Sheet with actual spending
                    try:
                        success = sheets.update_actual_spending(
                            tab_name=subteam_tab,
                            item_name=matched_item,
                            amount_to_add=amount,
                        )
                        if not success:
                            logger.warning("Could not find item %r in sheet for update", matched_item)
                    except Exception:
                        logger.exception("Failed to update sheet for approved purchase")

                    # Notify member of approval
                    client.chat_postMessage(
                        channel=requester_id,
                        text=(
                            f"✅ Your purchase request was *approved* by <@{reactor_id}>!\n\n"
                            f"*Item:* {item_name}\n"
                            f"*Amount:* {format_usd(amount)}\n\n"
                            f"The budget has been updated."
                        ),
                    )
                    

                else:  # is_rejected
                    # Notify member of rejection
                    client.chat_postMessage(
                        channel=requester_id,
                        text=(
                            f"❌ Your purchase request was *rejected* by <@{reactor_id}>.\n\n"
                            f"*Item:* {item_name}\n"
                            f"*Amount:* {format_usd(amount)}\n\n"
                            f"Please contact your manager for more details."
                        ),
                    )

            except Exception:
                logger.exception("Error processing approval/rejection for message_ts=%s", message_ts)

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
