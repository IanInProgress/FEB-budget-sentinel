from app import _get_bot_version, _parse_manager_decision_text


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


def test_parse_manager_decision_rejects_aliases_and_prose():
	assert _parse_manager_decision_text(":white_check_mark:") == (False, False, None)
	assert _parse_manager_decision_text(":x:") == (False, False, None)
	assert _parse_manager_decision_text("Please ✅") == (False, False, None)