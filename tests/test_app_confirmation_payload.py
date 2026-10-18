import json

from app import (
    REIMBURSEMENT_INFO_LINE,
    SLACK_ACTION_VALUE_MAX_LENGTH,
    _serialize_confirmation_button_value,
)


def test_reimbursement_info_line_highlights_next_steps():
    assert ":rotating_light: *NEXT STEPS:*" in REIMBURSEMENT_INFO_LINE
    assert "|Don't forget to fill out the reimbursement form!>" in REIMBURSEMENT_INFO_LINE


def test_confirmation_button_value_allows_slack_character_limit():
    empty_value_length = len(json.dumps({"details": ""}))
    confirmation_data = {"details": "x" * (SLACK_ACTION_VALUE_MAX_LENGTH - empty_value_length)}

    value = _serialize_confirmation_button_value(confirmation_data)

    assert value is not None
    assert len(value) == SLACK_ACTION_VALUE_MAX_LENGTH


def test_confirmation_button_value_rejects_payload_over_slack_character_limit():
    empty_value_length = len(json.dumps({"details": ""}))
    confirmation_data = {"details": "x" * (SLACK_ACTION_VALUE_MAX_LENGTH - empty_value_length + 1)}

    assert _serialize_confirmation_button_value(confirmation_data) is None