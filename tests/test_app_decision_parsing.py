from app import (
	DEPLOYED_COMMIT_VERSION,
	TUTORIAL_DELETE_DELAY_SECONDS,
	_extract_receipt_links_from_message,
	_normalize_receipt_links,
	_parse_manager_decision_text,
	_get_bot_version,
	_tutorial_delete_is_allowed,
)


def test_tutorial_delete_is_sender_only_until_three_minutes():
	created_at = 1_000.0

	assert _tutorial_delete_is_allowed("U123", "U123", created_at, created_at) is True
	assert _tutorial_delete_is_allowed(
		"U456", "U123", created_at, created_at + TUTORIAL_DELETE_DELAY_SECONDS - 1
	) is False
	assert _tutorial_delete_is_allowed(
		"U456", "U123", created_at, created_at + TUTORIAL_DELETE_DELAY_SECONDS
	) is True


def test_bot_version_is_commit_count_format():
	version = _get_bot_version()
	assert version.startswith("v")
	assert version[1:].isdigit() or version == "vunknown"


def test_bot_version_uses_deployment_fallback_constant(monkeypatch):
	monkeypatch.setattr("app.subprocess.run", lambda *args, **kwargs: (_ for _ in ()).throw(OSError()))

	assert _get_bot_version() == DEPLOYED_COMMIT_VERSION


def test_tutorial_delete_rejects_legacy_payload_for_other_users():
	assert _tutorial_delete_is_allowed("U456", "U123", None, 1_000.0) is False


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


def test_extract_receipt_links_preserves_all_image_attachments():
	message = {
		"files": [
			{"mimetype": "application/pdf", "permalink": "pdf-link"},
			{"mimetype": "image/jpeg", "permalink": "first-image"},
			{"filetype": "png", "permalink": "second-image"},
		]
	}

	assert _extract_receipt_links_from_message(message) == ["first-image", "second-image"]


def test_extract_receipt_links_deduplicates_repeated_file_metadata():
	message = {
		"files": [
			{"id": "F123", "mimetype": "image/jpeg", "permalink": "first-image"},
			{"id": "F123", "mimetype": "image/jpeg", "permalink": "first-image"},
			{"id": "F456", "mimetype": "image/jpeg", "permalink": "second-image"},
		]
	}

	assert _extract_receipt_links_from_message(message) == ["first-image", "second-image"]


def test_normalize_receipt_links_supports_legacy_single_link_payload():
	assert _normalize_receipt_links({"receipt_link": "legacy-link"}) == ["legacy-link"]
	assert _normalize_receipt_links({"receipt_links": ["first", "", "second"]}) == ["first", "second"]