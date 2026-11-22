"""Reglas v2: lo que los datos reales de Olist obligaron a cambiar."""

import json
from decimal import Decimal

from conftest import at
from cortecaja.ingest import ingest_file


def load_order(conn, tmp_path, item_total: str, charges: list[tuple[str, str, int]]):
    """Un pedido de un artículo y sus cobros: (medio, importe, minuto)."""
    orders = tmp_path / "orders.csv"
    orders.write_text("order_id,customer_id,order_status,order_purchase_timestamp\n"
                      "O-1,C-1,delivered,2026-11-17 10:00:00\n", encoding="utf-8")
    items = tmp_path / "items.csv"
    items.write_text("order_id,order_item_id,product_id,price,freight_value\n"
                     f"O-1,1,P-1,{item_total},0.00\n", encoding="utf-8")
    gateway = tmp_path / "gateway.jsonl"
    gateway.write_text("".join(json.dumps({
        "record_type": "payment_event", "provider": "simpay", "event_id": f"E-{n}",
        "order_ref": "O-1", "event_type": "charge", "payment_method": method, "amount": amount,
        "currency": "BRL", "occurred_at": f"2026-11-17T10:{minute:02d}:00-03:00",
    }) + "\n" for n, (method, amount, minute) in enumerate(charges, start=1)), encoding="utf-8")

    ingest_file(conn, "store_orders", orders, at(17, 11))
    ingest_file(conn, "store_order_items", items, at(17, 11))
    ingest_file(conn, "gateway_events", gateway, at(17, 11))
    return conn.execute("SELECT status FROM recon.order_gateway() WHERE order_ref = 'O-1'").fetchone()[0]


def test_one_cent_rounding_is_tolerated(conn, tmp_path):
    assert load_order(conn, tmp_path, "10.00", [("credit_card", "10.01", 1)]) == "matched"


def test_more_than_the_tolerance_is_still_a_difference(conn, tmp_path):
    assert load_order(conn, tmp_path, "10.00", [("credit_card", "10.05", 1)]) == "overcharged"


def test_equal_vouchers_are_not_duplicate_charges(conn, tmp_path):
    status = load_order(conn, tmp_path, "10.00", [("voucher", "5.00", 1), ("voucher", "5.00", 2)])
    assert status == "matched"


def test_equal_card_charges_are_still_duplicates(conn, tmp_path):
    status = load_order(conn, tmp_path, "10.00", [("credit_card", "10.00", 1), ("credit_card", "10.00", 3)])
    assert status == "duplicate_charge"


def test_tolerance_is_a_parameter(conn, tmp_path):
    load_order(conn, tmp_path, "10.00", [("credit_card", "10.01", 1)])
    strict = conn.execute(
        "SELECT status FROM recon.order_gateway('infinity', %s) WHERE order_ref = 'O-1'", (Decimal("0"),)
    ).fetchone()[0]
    assert strict == "overcharged"