from app import _parse_manager_decision_text


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