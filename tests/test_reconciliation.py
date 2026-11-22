"""Los casos de prueba del proyecto, con resultados escritos a mano.

Cada prueba corresponde a una fila de la tabla "Caso de prueba → Resultado esperado".
"""

from datetime import date
from decimal import Decimal

from cortecaja.close import run_close
from conftest import at, load, load_day_17, load_day_18


def order_row(conn, order_ref, cutoff="infinity"):
    return conn.execute(
        "SELECT status, expected_amount, charged_amount FROM recon.order_gateway(%s) WHERE order_ref = %s",
        (cutoff, order_ref),
    ).fetchone()


def bank_report(conn, business_date, cutoff="infinity"):
    return conn.execute(
        "SELECT item_key, status, expected_amount, received_amount FROM recon.gateway_bank(%s, %s)",
        (business_date, cutoff),
    ).fetchall()


def settlement_status(conn, key, business_date, cutoff="infinity"):
    return next(row for row in bank_report(conn, business_date, cutoff) if row[0] == key)


# 1 ─ Se procesa dos veces el mismo extracto → mismos depósitos, montos y casos
def test_same_statement_twice_changes_nothing(conn):
    load_day_17(conn)
    load_day_18(conn)
    run_close(conn, date(2026, 11, 18), at(18, 23, 59), publish=True)
    before = (bank_report(conn, "2026-11-18"),
              conn.execute("SELECT COUNT(*), SUM(amount) FROM core.bank_entries").fetchone(),
              conn.execute("SELECT COUNT(*) FROM ops.reconciliation_case").fetchone())

    again = load(conn, "bank_statement", "bank_2026-11-18.csv", at(18, 19))
    run_close(conn, date(2026, 11, 18), at(18, 23, 59), publish=True)

    after = (bank_report(conn, "2026-11-18"),
             conn.execute("SELECT COUNT(*), SUM(amount) FROM core.bank_entries").fetchone(),
             conn.execute("SELECT COUNT(*) FROM ops.reconciliation_case").fetchone())
    assert again.status == "duplicate_file"
    assert after == before


# 2 ─ Un mismo evento llega en dos archivos diferentes → un solo efecto sobre el saldo
def test_same_event_in_two_files_counts_once(conn):
    load_day_17(conn)
    summary = load(conn, "gateway_events", "gateway_2026-11-18.jsonl", at(18, 8, 5))

    assert summary.identical == 1                                    # EVT-04 repetido
    assert order_row(conn, "ORD-1003") == ("matched", Decimal("400.00"), Decimal("400.00"))


# 3 ─ El mismo ID llega con un importe distinto → dos observaciones y un conflicto visible
def test_same_id_with_another_amount_keeps_both_versions(conn):
    load_day_17(conn)
    load(conn, "gateway_events", "gateway_2026-11-18.jsonl", at(18, 8, 5))

    observations = conn.execute(
        """SELECT payload->>'amount' FROM raw.source_record
           WHERE payload->>'event_id' = 'EVT-10' ORDER BY batch_id"""
    ).fetchall()
    stored = conn.execute(
        "SELECT amount FROM core.payment_events WHERE external_event_id = 'EVT-10'"
    ).fetchone()[0]
    conflict = conn.execute(
        """SELECT expected_amount, received_amount, difference FROM ops.reconciliation_case
           WHERE case_type = 'data_conflict' AND entity_key = 'simpay/EVT-10' AND status = 'open'"""
    ).fetchone()

    assert observations == [("150.00",), ("160.00",)]
    assert stored == Decimal("150.00")                               # no se sobrescribió
    assert conflict == (Decimal("150.00"), Decimal("160.00"), Decimal("10.00"))


# 4 ─ Un pedido tiene tres artículos y dos pagos → cada uno se suma una vez
def test_three_items_and_two_payments_are_not_multiplied(conn):
    load_day_17(conn)

    items, payments = conn.execute(
        """SELECT (SELECT COUNT(*) FROM core.order_items WHERE order_id = 'ORD-1001'),
                  (SELECT COUNT(*) FROM core.payment_events WHERE order_ref = 'ORD-1001')"""
    ).fetchone()
    assert (items, payments) == (3, 2)                               # el escenario sigue teniendo la trampa
    assert order_row(conn, "ORD-1001") == ("matched", Decimal("500.00"), Decimal("500.00"))


# 5 ─ Un depósito llega después del cierre → cambia el estado operativo y el cierre
#     anterior sigue siendo reproducible
def test_late_deposit_resolves_the_case_and_keeps_the_old_close(conn):
    load_day_17(conn)
    close_17 = run_close(conn, date(2026, 11, 17), at(17, 23, 59), publish=True)
    load_day_18(conn)
    run_close(conn, date(2026, 11, 18), at(18, 23, 59), publish=True)

    case = conn.execute(
        """SELECT case_type, status FROM ops.reconciliation_case
           WHERE entity_key = 'simpay/STL-1' AND case_type <> 'data_conflict'"""
    ).fetchone()
    published = conn.execute(
        """SELECT item_status, received_amount FROM ops.close_result
           WHERE close_id = %s AND item_key = 'simpay/STL-1'""", (close_17["close_id"],)
    ).fetchone()

    assert case == ("overdue_deposit", "resolved")
    assert published == ("overdue", Decimal("0.00"))                 # la foto no cambió
    assert settlement_status(conn, "simpay/STL-1", "2026-11-17", at(17, 23, 59))[1] == "overdue"
    assert settlement_status(conn, "simpay/STL-1", "2026-11-18")[1] == "reconciled"


# 6 ─ Dos depósitos coinciden en importe y fecha → el caso sigue en revisión
def test_two_equal_deposits_stay_in_review(conn):
    load_day_17(conn)
    conn.execute("SELECT recon.match_by_reference()")

    links = conn.execute(
        "SELECT COUNT(*) FROM core.settlement_deposits WHERE settlement_id = 'STL-4'"
    ).fetchone()[0]
    assert links == 0                                                # nunca se concilia solo
    assert settlement_status(conn, "simpay/STL-4", "2026-11-17")[1] == "ambiguous"


# 7 ─ Falta el extracto bancario del día → el cierre figura como incompleto
def test_missing_bank_statement_makes_the_close_incomplete(conn):
    load_day_17(conn, with_bank=False)
    result = run_close(conn, date(2026, 11, 17), at(17, 23, 59), publish=True)

    assert result["status"] == "incomplete"
    assert result["missing"] == ["bank_statement"]
    assert conn.execute("SELECT COUNT(*) FROM ops.close_run WHERE status = 'published'").fetchone()[0] == 0


# Extra ─ El neto de R$1.250 se reconstruye línea por línea
def test_the_1250_settlement_breaks_down_line_by_line(conn):
    load_day_17(conn)
    lines = conn.execute(
        """SELECT line_type, amount FROM core.settlement_lines
           WHERE settlement_id = 'STL-1' ORDER BY line_number"""
    ).fetchall()

    assert [amount for _, amount in lines] == [Decimal(x) for x in ("500", "450", "400", "-60", "-40")]
    assert sum(amount for _, amount in lines) == Decimal("1250.00")