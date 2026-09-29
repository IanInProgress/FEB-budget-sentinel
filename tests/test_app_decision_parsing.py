from types import SimpleNamespace

from app import (
	_get_bot_version,
	_is_authorized_manager_decision,
	_parse_manager_decision_text,
	_required_approval_usergroup_ids,
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