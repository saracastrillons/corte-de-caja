"""Cierre de un día: empareja, actualiza la bandeja de casos y guarda la foto del cierre.

Uso:
    python -m cortecaja.close 2026-11-17 --cutoff 2026-11-17T23:59:59-03:00 --publish

Sin --publish el cierre queda como borrador. Si falta una fuente obligatoria,
queda 'incomplete' y la última versión publicada sigue vigente.
"""

import argparse
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from zoneinfo import ZoneInfo

import psycopg

from cortecaja.db import connect

RULES_VERSION = "v2"
REQUIRED_SOURCES = ("gateway_events", "bank_statement")
LOCAL_TZ = ZoneInfo("America/Sao_Paulo")

# Estado de la conciliación -> (tipo de caso, siguiente paso). Lo que no está aquí no abre caso.
CASE_RULES = {
    "in_transit":           ("in_transit", "Seguir observando: el depósito está dentro de plazo"),
    "overdue":              ("overdue_deposit", "Consultar con la pasarela por el depósito vencido"),
    "mismatch":             ("amount_mismatch", "Revisar la diferencia con la pasarela"),
    "overcharged":          ("amount_mismatch", "Revisar por qué se cobró más que el pedido"),
    "ambiguous":            ("ambiguous_match", "Elegir el depósito correcto y dejar el motivo"),
    "suggested":            ("ambiguous_match", "Confirmar el depósito sugerido"),
    "unmatched":            ("unmatched_bank_entry", "Identificar el origen del movimiento"),
    "duplicate_charge":     ("duplicate_charge", "Investigar el cobro duplicado antes de publicar"),
    "partial_payment":      ("partial_payment", "Confirmar si falta un pago o hubo un ajuste"),
    "no_payment":           ("partial_payment", "Confirmar si el pedido se cobró por otro medio"),
    "charge_without_order": ("charge_without_order", "Buscar el pedido o solicitar el reembolso"),
}

# Estado de la conciliación -> estado en la foto del cierre (ops.close_result)
RESULT_STATUS = {
    "matched": "reconciled", "reconciled": "reconciled",
    "in_transit": "in_transit",
    "overdue": "overdue",
    "mismatch": "mismatch", "overcharged": "mismatch", "partial_payment": "mismatch",
    "unmatched": "unmatched", "charge_without_order": "unmatched",
    "ambiguous": "in_review", "suggested": "in_review",
    "duplicate_charge": "in_review", "no_payment": "in_review",
}


@dataclass
class Item:
    """Una partida conciliada: un pedido, una liquidación o un depósito."""
    level: str
    item_type: str
    key: str
    status: str
    expected: Decimal | None
    received: Decimal | None
    due_on: date | None
    evidence: str | None

    def case_rule(self) -> tuple[str, str] | None:
        # Un depósito candidato no abre caso propio: lo cubre el caso de su liquidación.
        if self.item_type == "bank_entry" and self.status == "ambiguous":
            return None
        return CASE_RULES.get(self.status)


# ---------------------------------------------------------------------------
# 1) Reunir los resultados de los dos niveles
# ---------------------------------------------------------------------------

def collect_items(conn: psycopg.Connection, business_date: date, cutoff: datetime) -> list[Item]:
    items = [
        Item("order_gateway", "order", key, status, expected, charged, None, None)
        for key, status, expected, charged in conn.execute(
            """SELECT order_ref, status, expected_amount, charged_amount
               FROM recon.order_gateway(%s)""", (cutoff,))
    ]
    items += [
        Item("gateway_bank", item_type, key, status, expected, received, due_on, evidence)
        for item_type, key, status, expected, received, due_on, evidence in conn.execute(
            """SELECT item_type, item_key, status, expected_amount, received_amount, due_on, evidence
               FROM recon.gateway_bank(%s, %s)""", (business_date, cutoff))
    ]
    return items


# ---------------------------------------------------------------------------
# 2) Bandeja de casos: abrir, actualizar o resolver según lo que se sabe al corte
# ---------------------------------------------------------------------------

def log_event(conn, case_id, event_type, from_status, to_status, note, as_of, batch_id) -> None:
    conn.execute(
        """INSERT INTO ops.case_event
             (case_id, event_type, from_status, to_status, note, actor, caused_by_batch_id, recorded_at)
           VALUES (%s, %s, %s, %s, %s, 'close', %s, %s)""",
        (case_id, event_type, from_status, to_status, note, batch_id, as_of),
    )


def open_cases(conn: psycopg.Connection) -> dict:
    """Todos los casos abiertos (menos conflictos) en UNA consulta: (tipo, clave) -> caso."""
    return {
        (entity_type, key): (case_id, case_type, expected, received)
        for entity_type, key, case_id, case_type, expected, received in conn.execute(
            """SELECT entity_type, entity_key, case_id, case_type, expected_amount, received_amount
               FROM ops.reconciliation_case
               WHERE status = 'open' AND case_type <> 'data_conflict'""")
    }


def sync_cases(conn: psycopg.Connection, items: list[Item], as_of: datetime, batch_id: int | None) -> dict:
    counts = {"opened": 0, "updated": 0, "resolved": 0}
    known = open_cases(conn)          # antes: una consulta por partida (100.000 con Olist)

    for item in items:
        current = known.get((item.item_type, item.key))
        rule = item.case_rule()

        if rule is None:                                        # la partida ya cuadra
            if current:
                conn.execute(
                    """UPDATE ops.reconciliation_case
                       SET status = 'resolved', resolved_at = %s, received_amount = %s
                       WHERE case_id = %s""",
                    (as_of, item.received, current[0]),
                )
                log_event(conn, current[0], "resolved", "open", "resolved",
                          f"Conciliado. {item.evidence or ''}".strip(), as_of, batch_id)
                counts["resolved"] += 1
            continue

        case_type, next_action = rule
        if current is None:                                     # problema nuevo
            case_id = conn.execute(
                """INSERT INTO ops.reconciliation_case
                     (level, case_type, entity_type, entity_key, expected_amount, received_amount,
                      due_on, next_action, opened_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                   RETURNING case_id""",
                (item.level, case_type, item.item_type, item.key, item.expected, item.received,
                 item.due_on, next_action, as_of),
            ).fetchone()[0]
            log_event(conn, case_id, "opened", None, "open",
                      f"{case_type}. {item.evidence or ''}".strip(), as_of, batch_id)
            counts["opened"] += 1

        elif (current[1], current[2], current[3]) != (case_type, item.expected, item.received):
            conn.execute(                                       # el problema cambió
                """UPDATE ops.reconciliation_case
                   SET case_type = %s, expected_amount = %s, received_amount = %s, next_action = %s
                   WHERE case_id = %s""",
                (case_type, item.expected, item.received, next_action, current[0]),
            )
            event = "status_changed" if current[1] != case_type else "amount_updated"
            log_event(conn, current[0], event, "open", "open",
                      f"{current[1]} → {case_type}. {item.evidence or ''}".strip(), as_of, batch_id)
            counts["updated"] += 1

    return counts


# ---------------------------------------------------------------------------
# 3) La foto del cierre
# ---------------------------------------------------------------------------

def missing_sources(conn: psycopg.Connection, business_date: date, cutoff: datetime) -> list[str]:
    """Fuentes obligatorias que no llegaron durante el día del cierre."""
    day_start = datetime.combine(business_date, time.min, LOCAL_TZ)
    present = {
        source for (source,) in conn.execute(
            """SELECT DISTINCT source FROM raw.ingestion_batch
               WHERE status = 'loaded' AND received_at >= %s AND received_at <= %s""",
            (day_start, cutoff))
    }
    return [source for source in REQUIRED_SOURCES if source not in present]


def save_close(conn, business_date, cutoff, as_of, items, missing, publish) -> tuple[int, int, str, dict]:
    due = [i for i in items if i.item_type == "settlement" and i.due_on <= business_date]
    totals = {
        "expected": sum((i.expected for i in due), Decimal("0.00")),
        "received": sum((i.received for i in due), Decimal("0.00")),
    }
    totals["pending"] = totals["expected"] - totals["received"]

    status = "incomplete" if missing else ("published" if publish else "draft")
    version = conn.execute(
        "SELECT COALESCE(MAX(version), 0) + 1 FROM ops.close_run WHERE business_date = %s",
        (business_date,),
    ).fetchone()[0]

    previous = None
    if status == "published":                       # la versión anterior queda como histórica
        previous = conn.execute(
            """UPDATE ops.close_run SET status = 'superseded'
               WHERE business_date = %s AND status = 'published'
               RETURNING close_id""",
            (business_date,),
        ).fetchone()

    close_id = conn.execute(
        """INSERT INTO ops.close_run
             (business_date, version, received_cutoff, rules_version, status, missing_sources,
              total_expected, total_received, total_pending, supersedes_close_id, created_at, published_at)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
           RETURNING close_id""",
        (business_date, version, cutoff, RULES_VERSION, status, missing,
         totals["expected"], totals["received"], totals["pending"],
         previous[0] if previous else None, as_of, as_of if status == "published" else None),
    ).fetchone()[0]

        # La foto completa en un solo envío (executemany agrupa las filas en lotes).
    case_ids = {key: case[0] for key, case in open_cases(conn).items()}
    with conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO ops.close_result
                 (close_id, item_type, item_key, item_status, expected_amount, received_amount, case_id)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            [(close_id, item.item_type, item.key, RESULT_STATUS[item.status],
              item.expected, item.received, case_ids.get((item.item_type, item.key)))
             for item in items],
        )
    return close_id, version, status, totals


# ---------------------------------------------------------------------------
# El proceso completo, en UNA transacción
# ---------------------------------------------------------------------------

def run_close(conn: psycopg.Connection, business_date: date, cutoff: datetime, publish: bool) -> dict:
    as_of = min(cutoff, datetime.now().astimezone())    # el reloj de la demo nunca va al futuro
    with conn.transaction():
        matched = conn.execute("SELECT recon.match_by_reference()").fetchone()[0]
        last_batch = conn.execute(
            """SELECT MAX(batch_id) FROM raw.ingestion_batch
               WHERE status = 'loaded' AND received_at <= %s""", (cutoff,),
        ).fetchone()[0]

        items = collect_items(conn, business_date, cutoff)

        # La bandeja solo avanza en el tiempo: rehacer un cierre viejo no revive casos resueltos.
        latest_cutoff = conn.execute("SELECT MAX(received_cutoff) FROM ops.close_run").fetchone()[0]
        if latest_cutoff is None or cutoff >= latest_cutoff:
            cases = sync_cases(conn, items, as_of, last_batch)
        else:
            cases = {"opened": 0, "updated": 0, "resolved": 0}

        missing = missing_sources(conn, business_date, cutoff)
        close_id, version, status, totals = save_close(
            conn, business_date, cutoff, as_of, items, missing, publish)

    return {"close_id": close_id, "version": version, "status": status, "matched": matched,
            "cases": cases, "missing": missing, "totals": totals, "items": len(items)}


def main() -> None:
    parser = argparse.ArgumentParser(description="Corre el cierre de un día.")
    parser.add_argument("business_date", type=date.fromisoformat)
    parser.add_argument("--cutoff", type=datetime.fromisoformat,
                        help="Corte de recepción. Por defecto: ahora.")
    parser.add_argument("--publish", action="store_true", help="Publicar si está completo.")
    args = parser.parse_args()
    cutoff = args.cutoff or datetime.now().astimezone()

    with connect(autocommit=True) as conn:
        r = run_close(conn, args.business_date, cutoff, args.publish)

    t = r["totals"]
    print(f"Cierre {args.business_date} · versión {r['version']} (id {r['close_id']}) · {r['status']}")
    print(f"  corte de recepción: {cutoff.isoformat()}")
    print(f"  vínculos nuevos por referencia: {r['matched']}")
    print(f"  partidas revisadas: {r['items']}")
    print(f"  casos · abiertos: {r['cases']['opened']} · actualizados: {r['cases']['updated']}"
          f" · resueltos: {r['cases']['resolved']}")
    print(f"  esperado R$ {t['expected']} · recibido R$ {t['received']} · pendiente R$ {t['pending']}")
    if r["missing"]:
        print(f"  ⚠ faltan fuentes: {', '.join(r['missing'])}. La última versión publicada sigue vigente.")


if __name__ == "__main__":
    main()