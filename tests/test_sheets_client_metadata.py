from sheets_client import _row_to_purchase_log_entry


def test_purchase_log_entry_reads_original_message_metadata():
	row = [""] * 22
	row[0] = "REQ-000001"
	row[20] = "C123"
	row[21] = "123.456"

	entry = _row_to_purchase_log_entry(row)

	assert entry.original_channel_id == "C123"
	assert entry.original_message_ts == "123.456"


def test_older_purchase_log_rows_default_original_message_metadata():
	entry = _row_to_purchase_log_entry(["REQ-000001"])

	assert entry.original_channel_id == ""
	assert entry.original_message_ts == ""
