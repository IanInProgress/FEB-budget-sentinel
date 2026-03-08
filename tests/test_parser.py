from parser import parse_purchase_text
from budget_checker import Status


def test_parse_valid_basic():
    r = parse_purchase_text('ADMIN-001, 50.00, Need supplies')
    assert r.ok is True
    assert r.reference_id == "ADMIN-001"
    assert r.subteam_tab == "Admin"
    assert abs(r.requested_amount - 50.0) < 1e-9
    assert r.reason == "Need supplies"


def test_parse_valid_case_insensitive():
    r = parse_purchase_text('admin-001, 100.00, Test request')
    assert r.ok is True
    assert r.reference_id == "ADMIN-001"
    assert r.subteam_tab == "Admin"


def test_parse_with_dollar_sign():
    r = parse_purchase_text('EECS-001, $75.50, Component purchase')
    assert r.ok is True
    assert r.reference_id == "EECS-001"
    assert abs(r.requested_amount - 75.50) < 1e-9


def test_parse_invalid_prefix():
    r = parse_purchase_text('UNKNOWN-001, 50.00, test')
    assert r.ok is False
    assert r.status == Status.INVALID_COMMAND


def test_parse_rejects_negative():
    r = parse_purchase_text('ADMIN-001, -50, reason')
    assert r.ok is False
    assert r.status == Status.INVALID_COMMAND


def test_parse_rejects_zero():
    r = parse_purchase_text('ADMIN-001, 0, reason')
    assert r.ok is False
    assert r.status == Status.INVALID_COMMAND


def test_parse_missing_reason():
    r = parse_purchase_text('ADMIN-001, 50.00')
    assert r.ok is False
    assert r.status == Status.INVALID_COMMAND


def test_parse_invalid_format():
    r = parse_purchase_text('ADMIN-001 50.00 reason')
    assert r.ok is False
    assert r.status == Status.INVALID_COMMAND


def test_parse_valid_all_subteams():
    subteams = [
        ("ADMIN-001", "Admin"),
        ("DYNA-001", "Dynamics"),
        ("CHAS-001", "Chassis"),
        ("POWER-001", "Powertrain"),
        ("COMP-001", "Composites"),
        ("ERGO-001", "Brakes/Ergo"),
        ("MECH-001", "Accumulator MechE"),
        ("EECS-001", "EECS"),
        ("AERO-001", "Aero"),
        ("AUTO-001", "Auto"),
        ("MANU-001", "Manufacturing"),
    ]
    for ref_id, tab_name in subteams:
        r = parse_purchase_text(f'{ref_id}, 100.00, test')
        assert r.ok is True
        assert r.reference_id == ref_id
        assert r.subteam_tab == tab_name


def test_parse_unaccounted_with_item_name():
    r = parse_purchase_text('ADMIN-000 Toilet Paper, 25.00, Need for office')
    assert r.ok is True
    assert r.reference_id == "ADMIN-000"
    assert r.subteam_tab == "Admin"
    assert r.is_unaccounted is True
    assert r.provided_item_name == "Toilet Paper"
    assert abs(r.requested_amount - 25.0) < 1e-9
    assert r.reason == "Need for office"


def test_parse_unaccounted_without_item_name():
    r = parse_purchase_text('EECS-000, 50.00, Some reason')
    assert r.ok is False
    assert r.status == Status.INVALID_COMMAND
    assert "require an item name" in r.error_message.lower()


def test_parse_regular_item_is_not_unaccounted():
    r = parse_purchase_text('ADMIN-001, 50.00, Regular item')
    assert r.ok is True
    assert r.is_unaccounted is False
    assert r.provided_item_name is None

