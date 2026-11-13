from types import SimpleNamespace

from app import (
	_get_bot_version,
	_is_authorized_manager_decision,
	_edit_purchase_request_message,
	_parse_move_purchase_request_command,
	_parse_manager_decision_text,
	_replace_purchase_request_reaction,
	_required_approval_usergroup_ids,
	_should_notify_subteam_watchers,
)


def test_bot_version_uses_deployment_commit(monkeypatch):
	monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "abcdef123456")

	assert _get_bot_version() == "abcdef1"


def test_parse_manager_decision_full_approval():
	approved, rejected, line_numbers = _parse_manager_decision_text("✅")
	assert approved is True
	assert rejected is False
	assert line_numbers is None


def test_parse_manager_decision_selective_approval_numbers():
	approved, rejected, line_numbers = _parse_manager_decision_text("✅ 1 2 5")
	assert approved is True
	assert rejected is False
	assert line_numbers == {1, 2, 5}


def test_parse_manager_decision_reject_all():
	approved, rejected, line_numbers = _parse_manager_decision_text("❌")
	assert approved is False
	assert rejected is True
	assert line_numbers is None


def test_parse_manager_decision_ignores_req_id_digits():
	approved, rejected, line_numbers = _parse_manager_decision_text("✅ REQ-000123 1 2")
	assert approved is True
	assert rejected is False
	assert line_numbers == {1, 2}


def test_parse_manager_decision_accepts_shortcode_aliases():
	assert _parse_manager_decision_text(":white_check_mark:") == (True, False, None)
	assert _parse_manager_decision_text(":x:") == (False, True, None)


def test_parse_manager_decision_rejects_prose():
	assert _parse_manager_decision_text("Please ✅") == (False, False, None)
	assert _parse_manager_decision_text("Please :white_check_mark: this") == (False, False, None)


def test_parse_move_purchase_request_command_normalizes_category_id():
	assert _parse_move_purchase_request_command(" req-123, bno-003 ") == ("REQ-000123", "BNO-003", None, None)
	assert _parse_move_purchase_request_command("REQ-000123, BNO-000 test") == (
		"REQ-000123",
		"BNO-000",
		"test",
		None,
	)
	assert _parse_move_purchase_request_command("REQ-000123, BNO-003, 1") == (
		"REQ-000123",
		"BNO-003",
		None,
		1,
	)
	assert _parse_move_purchase_request_command("REQ-000123, BNO-000 Office, supplies, 1") == (
		"REQ-000123",
		"BNO-000",
		"Office, supplies",
		1,
	)


def test_parse_move_purchase_request_command_rejects_invalid_category_id():
	for text in (
		"",
		"BNO-003",
		"REQ-123",
		"REQ-123 BNO",
		"REQ-123, BNO_003",
		"REQ-123, BNO-003 extra",
		"REQ-123, BNO-000",
		"REQ-123, BNO-003, 0",
		"REQ-123, BNO-003, first",
	):
		try:
			_parse_move_purchase_request_command(text)
		except ValueError:
			pass
		else:
			raise AssertionError(f"Expected invalid command text: {text!r}")


def test_edit_purchase_request_message_updates_text_and_blocks():
	calls = []
	client = SimpleNamespace(
		conversations_replies=lambda **kwargs: {
			"messages": [
				{
					"text": "REQ-000001 | BNO-001 | Supplies",
					"blocks": [
						{"type": "section", "text": {"type": "mrkdwn", "text": "`BNO-001`"}}
					],
				}
			]
		},
		chat_update=lambda **kwargs: calls.append(kwargs),
	)

	_edit_purchase_request_message(
		client,
		channel_id="C123",
		message_ts="123.456",
		old_reference_ids={"BNO-001"},
		target_reference_id="EECS-003",
		category_name="EECS",
	)

	assert calls == [
		{
			"channel": "C123",
			"ts": "123.456",
			"text": "REQ-000001 | EECS-003 | Supplies\n🔄 Moved to `EECS-003` in *EECS*.",
			"blocks": [
				{"type": "section", "text": {"type": "mrkdwn", "text": "`EECS-003`"}}
			],
		}
	]


def test_voiding_replaces_purchase_message_checkmark_with_trash_can():
	calls = []
	client = SimpleNamespace(
		reactions_remove=lambda **kwargs: calls.append(("remove", kwargs)),
		reactions_add=lambda **kwargs: calls.append(("add", kwargs)),
	)

	_replace_purchase_request_reaction(client, channel_id="C123", message_ts="123.456")

	assert calls == [
		(
			"remove",
			{"channel": "C123", "timestamp": "123.456", "name": "white_check_mark"},
		),
		(
			"add",
			{"channel": "C123", "timestamp": "123.456", "name": "wastebasket"},
		),
	]


def test_unaccounted_requests_use_same_amount_based_approval_tiers():
	settings = SimpleNamespace(chief_usergroup_id="S_CHIEF", president_vp_usergroup_id="S_PRESIDENT_VP")
	assert _required_approval_usergroup_ids(
		{"items": [{"requested_amount": 99, "reference_id": "ADMIN-000", "is_unaccounted": True}]},
		settings,
	) == ()
	assert _required_approval_usergroup_ids(
		{"items": [{"requested_amount": 100, "reference_id": "ADMIN-000", "is_unaccounted": True}]},
		settings,
	) == ("S_CHIEF", "S_PRESIDENT_VP")
	assert _required_approval_usergroup_ids(
		{"items": [{"requested_amount": 251, "reference_id": "ADMIN-000", "is_unaccounted": True}]},
		settings,
	) == ("S_PRESIDENT_VP",)


def test_unaccounted_bulk_item_does_not_change_role_tier():
	settings = SimpleNamespace(chief_usergroup_id="S_CHIEF", president_vp_usergroup_id="S_PRESIDENT_VP")
	approval_data = {
		"items": [
			{"requested_amount": 40, "reference_id": "BNO-001", "is_unaccounted": False},
			{"requested_amount": 60, "reference_id": "EECS-000", "is_unaccounted": True},
		],
	}
	assert _required_approval_usergroup_ids(approval_data, settings) == ("S_CHIEF", "S_PRESIDENT_VP")


def test_bundled_amount_selects_approval_role_tier():
	settings = SimpleNamespace(
		chief_usergroup_id="S_CHIEF",
		president_vp_usergroup_id="S_PRESIDENT_VP",
	)
	assert _required_approval_usergroup_ids(
		{"items": [{"requested_amount": 60}, {"requested_amount": 39.99}]}, settings
	) == ()
	assert _required_approval_usergroup_ids(
		{"items": [{"requested_amount": 60}, {"requested_amount": 40}]}, settings
	) == ("S_CHIEF", "S_PRESIDENT_VP")
	assert _required_approval_usergroup_ids(
		{"items": [{"requested_amount": 125}, {"requested_amount": 125}]}, settings
	) == ("S_CHIEF", "S_PRESIDENT_VP")
	assert _required_approval_usergroup_ids(
		{"items": [{"requested_amount": 125}, {"requested_amount": 125.01}]}, settings
	) == ("S_PRESIDENT_VP",)


def test_tran005_and_tran006_always_require_president_vp_approval():
	settings = SimpleNamespace(
		chief_usergroup_id="S_CHIEF",
		president_vp_usergroup_id="S_PRESIDENT_VP",
	)
	for reference_id in ("TRAN005", "tran-006"):
		assert _required_approval_usergroup_ids(
			{"items": [{"reference_id": reference_id, "requested_amount": 1}]},
			settings,
		) == ("S_PRESIDENT_VP",)

	assert _required_approval_usergroup_ids(
		{"items": [{"reference_id": "TRAN-004", "requested_amount": 1}]},
		settings,
	) == ()


def test_subteam_watchers_are_only_notified_below_one_hundred_dollars():
	settings = SimpleNamespace(
		chief_usergroup_id="S_CHIEF",
		president_vp_usergroup_id="S_PRESIDENT_VP",
	)
	assert _should_notify_subteam_watchers({"items": [{"requested_amount": 99.99}]}, settings)
	assert not _should_notify_subteam_watchers({"items": [{"requested_amount": 100}]}, settings)
	assert not _should_notify_subteam_watchers({"items": [{"requested_amount": 250}]}, settings)
	assert not _should_notify_subteam_watchers({"items": [{"requested_amount": 250.01}]}, settings)


def test_chief_and_executive_roles_must_authorize_approval_and_rejection():
	approval_tiers = (
		({"items": [{"requested_amount": 100}]}, ("S_CHIEF", "S_PRESIDENT_VP")),
		({"items": [{"requested_amount": 251}]}, ("S_PRESIDENT_VP",)),
	)
	for approval_data, required_roles in approval_tiers:
		for is_approved in (True, False):
			assert _is_authorized_manager_decision(
				required_role_usergroup_ids=required_roles,
				manager_in_required_role=False,
			) is False
			assert _is_authorized_manager_decision(
				required_role_usergroup_ids=required_roles,
				manager_in_required_role=True,
			) is True