from sheets_client import _row_to_purchase_log_entry


def test_purchase_log_entry_reads_original_message_metadata():
	row = [""] * 23
	row[0] = "REQ-000001"
	row[21] = "C123"
	row[22] = "123.456"

	entry = _row_to_purchase_log_entry(row)

	assert entry.original_channel_id == "C123"
	assert entry.original_message_ts == "123.456"


def test_older_purchase_log_rows_default_original_message_metadata():
	entry = _row_to_purchase_log_entry(["REQ-000001"])

	assert entry.original_channel_id == ""
	assert entry.original_message_ts == ""


def test_purchase_log_entry_reads_fields_after_company_name_column():
	row = [""] * 23
	row[0] = "REQ-000002"
	row[3] = "approved"
	row[4] = "U123"
	row[6] = "EECS"
	row[7] = "EECS-001"
	row[8] = "Solder"
	row[9] = "Digi-Key"
	row[10] = "Lab supplies"
	row[11] = "42.50"
	row[12] = "true"
	row[16] = "Not needed"
	row[18] = "2"

	entry = _row_to_purchase_log_entry(row)

	assert entry.amount_usd == 42.5
	assert entry.bundle_line_number == 2
	assert entry.company_name == "Digi-Key"
	assert entry.purchase_reason == "Lab supplies"
	assert entry.is_unaccounted is True
	assert entry.rejection_reason == "Not needed"
