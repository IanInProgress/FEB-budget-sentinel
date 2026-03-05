from parser import parse_purchase_text
from budget_checker import Status


def test_parse_valid_basic():
    r = parse_purchase_text('electronics "CAN transceiver" 42.50')
    assert r.ok is True
    assert r.subteam == "electronics"
    assert r.item_name == "CAN transceiver"
    assert abs(r.requested_amount - 42.50) < 1e-9


def test_parse_valid_with_dollar_sign():
    r = parse_purchase_text('suspension "rod ends" $180')
    assert r.ok is True
    assert r.subteam == "suspension"
    assert r.item_name == "rod ends"
    assert abs(r.requested_amount - 180.0) < 1e-9


def test_parse_rejects_negative():
    r = parse_purchase_text('electronics "thing" -1')
    assert r.ok is False
    assert r.status == Status.INVALID_COMMAND


def test_parse_rejects_zero():
    r = parse_purchase_text('electronics "thing" 0')
    assert r.ok is False
    assert r.status == Status.INVALID_COMMAND


def test_parse_invalid_format():
    r = parse_purchase_text("electronics CAN transceiver 42.50")
    assert r.ok is False
    assert r.status == Status.INVALID_COMMAND

