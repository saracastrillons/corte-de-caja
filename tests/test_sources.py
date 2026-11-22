"""Pruebas de lectura y validación. No necesitan base de datos."""

from decimal import Decimal

import pytest

from cortecaja.sources import RowError, StructureError, read_rows, validate


def test_amounts_are_exact_decimals():
    row = validate("bank_statement", {
        "bank_account": "CC-0001", "entry_id": "B1", "booked_on": "2026-11-17",
        "amount": "0.10", "currency": "BRL", "description": "X", "reference": "",
    })
    assert row["amount"] + Decimal("0.20") == Decimal("0.30")
    assert row["reference"] is None


def test_more_than_two_decimals_is_rejected():
    with pytest.raises(RowError, match="2 decimales"):
        validate("bank_statement", {
            "bank_account": "CC-0001", "entry_id": "B1", "booked_on": "2026-11-17",
            "amount": "12.345", "currency": "BRL", "description": "X", "reference": "",
        })


def test_only_brl_is_accepted():
    with pytest.raises(RowError, match="BRL"):
        validate("bank_statement", {
            "bank_account": "CC-0001", "entry_id": "B1", "booked_on": "2026-11-17",
            "amount": "5.00", "currency": "USD", "description": "X", "reference": "",
        })


def test_settlement_lines_must_add_up_to_the_reported_net():
    with pytest.raises(RowError, match="suman"):
        validate("gateway_events", {
            "record_type": "settlement", "provider": "simpay", "settlement_id": "S1",
            "settled_at": "2026-11-17T08:00:00-03:00", "expected_payout_on": "2026-11-18",
            "net_amount": "100.00", "currency": "BRL",
            "lines": [{"line": 1, "type": "charge", "event_id": "E1", "amount": "90.00"}],
        })


def test_naive_store_timestamps_are_read_in_sao_paulo_time():
    row = validate("store_orders", {
        "order_id": "O1", "customer_id": "C1", "order_status": "delivered",
        "order_purchase_timestamp": "2026-11-14 09:58:00",
    })
    assert row["purchased_at"].utcoffset().total_seconds() == -3 * 3600


def test_missing_columns_fail_the_whole_file():
    with pytest.raises(StructureError, match="Faltan columnas"):
        read_rows("bank_statement", b"cuenta,monto\n1,2\n")


def test_a_broken_json_line_does_not_break_the_file():
    rows = read_rows("gateway_events", b'{"record_type":"payment_event"}\nesto no es json\n')
    assert len(rows) == 2
    with pytest.raises(RowError, match="JSON"):
        validate("gateway_events", rows[1])