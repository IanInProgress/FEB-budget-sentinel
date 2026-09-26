from app import (
	BNO_ADMIN_USER_ID,
	_get_bot_version,
	_is_authorized_manager_decision,
	_parse_manager_decision_text,
	_request_requires_bno_admin_approval,
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


def test_unaccounted_request_can_only_be_approved_by_bno_admin():
	approval_data = {
		"items": [
			{"reference_id": "ADMIN-000", "is_unaccounted": True},
		],
	}

	assert _request_requires_bno_admin_approval(approval_data) is True
	assert _is_authorized_manager_decision(
		approval_data,
		is_approved=True,
		manager_id="U_OTHER_MANAGER",
	) is False
	assert _is_authorized_manager_decision(
		approval_data,
		is_approved=True,
		manager_id=BNO_ADMIN_USER_ID,
	) is True
	assert _is_authorized_manager_decision(
		approval_data,
		is_approved=False,
		manager_id="U_OTHER_MANAGER",
	) is True


def test_any_unaccounted_item_restricts_a_bulk_request_approval():
	approval_data = {
		"items": [
			{"reference_id": "BNO-001", "is_unaccounted": False},
			{"reference_id": "EECS-000", "is_unaccounted": True},
		],
	}

	assert _request_requires_bno_admin_approval(approval_data) is True
	assert _is_authorized_manager_decision(
		approval_data,
		is_approved=True,
		manager_id="U_OTHER_MANAGER",
	) is False


def test_regular_request_approval_is_not_restricted():
	approval_data = {"items": [{"reference_id": "BNO-001", "is_unaccounted": False}]}

	assert _request_requires_bno_admin_approval(approval_data) is False
	assert _is_authorized_manager_decision(
		approval_data,
		is_approved=True,
		manager_id="U_OTHER_MANAGER",
	) is True