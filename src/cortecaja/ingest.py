"""Carga un archivo: lo registra como lote, guarda cada fila en raw y pasa las válidas a core.

Uso:
    python -m cortecaja.ingest <fuente> <archivo> [--received-at 2026-11-17T18:00:00-03:00]

Fuentes: store_orders, store_order_items, gateway_events, bank_statement
"""

import argparse
import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from cortecaja.db import connect
from cortecaja.sources import SOURCES, RowError, StructureError, read_rows, validate


@dataclass
class Target:
    """Cómo se escribe un tipo de registro en core."""
    table: tuple[str, str]       # (esquema, tabla)
    key: list[str]               # columnas de la clave de negocio
    compare: list[str]           # columnas que deben coincidir para considerarlo "idéntico"
    level: str                   # nivel de conciliación, si hay conflicto
    entity_type: str             # tipo de entidad, si hay conflicto
    amount: str | None = None    # columna de importe, para mostrar la diferencia del conflicto


TARGETS = {
    "order": Target(("core", "orders"), ["order_id"],
                    ["customer_id", "order_status", "purchased_at"], "order_gateway", "order"),
    "order_item": Target(("core", "order_items"), ["order_id", "line_number"],
                         ["product_id", "price", "freight"], "order_gateway", "order"),
    "payment_event": Target(("core", "payment_events"), ["provider", "external_event_id"],
                            ["order_ref", "event_type", "payment_method", "amount", "occurred_at"],
                            "order_gateway", "payment_event", amount="amount"),
    "settlement": Target(("core", "settlements"), ["provider", "settlement_id"],
                         ["settled_at", "expected_payout_on", "reported_net"],
                         "gateway_bank", "settlement", amount="reported_net"),
    "bank_entry": Target(("core", "bank_entries"), ["bank_account", "external_entry_id"],
                         ["booked_on", "amount", "description", "reference"],
                         "gateway_bank", "bank_entry", amount="amount"),
}


@dataclass
class Summary:
    batch_id: int
    status: str
    rows_received: int = 0
    rows_rejected: int = 0
    inserted: int = 0
    identical: int = 0
    conflicts: int = 0
    rejections: list[str] = field(default_factory=list)
    message: str | None = None


# ---------------------------------------------------------------------------
# Escritura en core: insertar si es nuevo, comparar si ya existía
# ---------------------------------------------------------------------------

def write_or_compare(conn: psycopg.Connection, target: Target, values: dict) -> tuple[str, tuple | None]:
    """Devuelve ('inserted', None), ('identical', fila) o ('conflict', fila_existente)."""
    columns = list(values)
    insert = sql.SQL(
        "INSERT INTO {table} ({cols}) VALUES ({vals}) ON CONFLICT ({key}) DO NOTHING RETURNING 1"
    ).format(
        table=sql.Identifier(*target.table),
        cols=sql.SQL(", ").join(map(sql.Identifier, columns)),
        vals=sql.SQL(", ").join(sql.Placeholder() * len(columns)),
        key=sql.SQL(", ").join(map(sql.Identifier, target.key)),
    )
    if conn.execute(insert, list(values.values())).fetchone():
        return "inserted", None

    select = sql.SQL("SELECT {cols} FROM {table} WHERE {where}").format(
        cols=sql.SQL(", ").join(map(sql.Identifier, target.compare)),
        table=sql.Identifier(*target.table),
        where=sql.SQL(" AND ").join(sql.SQL("{} = %s").format(sql.Identifier(k)) for k in target.key),
    )
    existing = conn.execute(select, [values[k] for k in target.key]).fetchone()
    incoming = tuple(values[c] for c in target.compare)
    return ("identical" if tuple(existing) == incoming else "conflict"), existing


def open_conflict(conn, target: Target, values: dict, existing: tuple, batch_id: int) -> None:
    """Mismo identificador, contenido distinto: no se sobrescribe; se abre un caso."""
    entity_key = "/".join(str(values[k]) for k in target.key)
    old_amount = new_amount = None
    if target.amount:
        old_amount = existing[target.compare.index(target.amount)]
        new_amount = values[target.amount]

    # El conflicto existe desde que llegó el archivo que lo provocó.
    received_at = conn.execute(
        "SELECT received_at FROM raw.ingestion_batch WHERE batch_id = %s", (batch_id,)
    ).fetchone()[0]

    row = conn.execute(
        """
        INSERT INTO ops.reconciliation_case
            (level, case_type, entity_type, entity_key, expected_amount, received_amount,
             next_action, opened_at)
        VALUES (%s, 'data_conflict', %s, %s, %s, %s,
                'Confirmar con la fuente cuál versión es la correcta', %s)
        ON CONFLICT (case_type, entity_type, entity_key) WHERE status = 'open' DO NOTHING
        RETURNING case_id
        """,
        (target.level, target.entity_type, entity_key, old_amount, new_amount, received_at),
    ).fetchone()

    if row:
        case_id, event_type = row[0], "opened"
    else:
        case_id = conn.execute(
            """SELECT case_id FROM ops.reconciliation_case
               WHERE case_type = 'data_conflict' AND entity_type = %s AND entity_key = %s
                 AND status = 'open'""",
            (target.entity_type, entity_key),
        ).fetchone()[0]
        event_type = "note"

    changed = [c for c, old in zip(target.compare, existing) if values[c] != old]
    conn.execute(
        """INSERT INTO ops.case_event
             (case_id, event_type, to_status, note, actor, caused_by_batch_id, recorded_at)
           VALUES (%s, %s, 'open', %s, 'ingest', %s, %s)""",
        (case_id, event_type, f"Llegó otra versión de {entity_key}; cambia: {', '.join(changed)}",
         batch_id, received_at),
    )


def check_references(conn: psycopg.Connection, record: dict) -> None:
    """Reglas que necesitan mirar la base. Si fallan, la fila se rechaza con su motivo."""
    if record["kind"] == "order_item":
        if not conn.execute("SELECT 1 FROM core.orders WHERE order_id = %s", (record["order_id"],)).fetchone():
            raise RowError(f"El pedido {record['order_id']} no existe")

    if record["kind"] == "settlement":
        for line in record["lines"]:
            event_id = line["external_event_id"]
            if event_id is None:
                continue
            found = conn.execute(
                """SELECT e.event_type, l.settlement_id
                   FROM core.payment_events e
                   LEFT JOIN core.settlement_lines l
                          ON l.provider = e.provider AND l.external_event_id = e.external_event_id
                   WHERE e.provider = %s AND e.external_event_id = %s""",
                (record["provider"], event_id),
            ).fetchone()
            if found is None:
                raise RowError(f"La línea {line['line_number']} cita el evento {event_id}, que no existe")
            if found[0] != line["line_type"]:
                raise RowError(f"El evento {event_id} es '{found[0]}' pero la línea dice '{line['line_type']}'")
            if found[1] not in (None, record["settlement_id"]):
                raise RowError(f"El evento {event_id} ya está en la liquidación {found[1]}")


def apply_record(conn, record: dict, batch_id: int, row_number: int, summary: Summary) -> None:
    kind = record["kind"]
    target = TARGETS[kind]
    values = {k: v for k, v in record.items() if k not in ("kind", "lines")}
    values |= {"src_batch_id": batch_id, "src_row": row_number}

    outcome, existing = write_or_compare(conn, target, values)

    if outcome == "inserted":
        summary.inserted += 1
        if kind == "settlement":
            for line in record["lines"]:
                conn.execute(
                    """INSERT INTO core.settlement_lines
                         (provider, settlement_id, line_number, line_type, external_event_id,
                          amount, src_batch_id, src_row)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                    (record["provider"], record["settlement_id"], line["line_number"],
                     line["line_type"], line["external_event_id"], line["amount"], batch_id, row_number),
                )
    elif outcome == "identical":
        summary.identical += 1
    else:
        summary.conflicts += 1
        open_conflict(conn, target, values, existing, batch_id)


# ---------------------------------------------------------------------------
# El proceso completo de un archivo
# ---------------------------------------------------------------------------

def ingest_file(conn: psycopg.Connection, source: str, path: Path,
                received_at: datetime | None = None) -> Summary:
    content = path.read_bytes()
    sha256 = hashlib.sha256(content).hexdigest()
    received_at = received_at or datetime.now().astimezone()

    def new_batch(status: str, message: str | None = None) -> int:
        return conn.execute(
            """INSERT INTO raw.ingestion_batch (source, file_name, file_sha256, received_at, status, error_message)
               VALUES (%s, %s, %s, %s, %s, %s) RETURNING batch_id""",
            (source, path.name, sha256, received_at, status, message),
        ).fetchone()[0]

    # 1) ¿Ya cargamos exactamente este archivo? Se deja constancia y no se toca nada más.
    with conn.transaction():
        previous = conn.execute(
            """SELECT batch_id FROM raw.ingestion_batch
               WHERE source = %s AND file_sha256 = %s AND status IN ('received', 'loaded')""",
            (source, sha256),
        ).fetchone()
        if previous:
            message = f"Archivo idéntico al lote {previous[0]}; no se vuelve a procesar"
            return Summary(new_batch("duplicate_file", message), "duplicate_file", message=message)

    # 2) ¿El archivo tiene la estructura correcta?
    try:
        rows = read_rows(source, content)
    except StructureError as error:
        with conn.transaction():
            return Summary(new_batch("failed", str(error)), "failed", message=str(error))

    # 3) Todo el archivo en UNA transacción: o queda completo, o no queda nada.
    with conn.transaction():
        batch_id = new_batch("received")
        summary = Summary(batch_id, "loaded", rows_received=len(rows))

        for row_number, row in enumerate(rows, start=1):
            try:
                record = validate(source, row)
                check_references(conn, record)
            except RowError as error:
                summary.rows_rejected += 1
                summary.rejections.append(f"fila {row_number}: {error}")
                conn.execute("INSERT INTO raw.source_record VALUES (%s, %s, %s, false, %s)",
                             (batch_id, row_number, Jsonb(row), str(error)))
                continue

            conn.execute("INSERT INTO raw.source_record VALUES (%s, %s, %s, true, NULL)",
                         (batch_id, row_number, Jsonb(row)))
            apply_record(conn, record, batch_id, row_number, summary)

        conn.execute(
            """UPDATE raw.ingestion_batch
               SET status = 'loaded', rows_received = %s, rows_rejected = %s
               WHERE batch_id = %s""",
            (summary.rows_received, summary.rows_rejected, batch_id),
        )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Carga un archivo de origen en la base.")
    parser.add_argument("source", choices=SOURCES)
    parser.add_argument("path", type=Path)
    parser.add_argument("--received-at", type=datetime.fromisoformat,
                        help="Hora de recepción simulada (para la demo). Por defecto: ahora.")
    args = parser.parse_args()

    with connect(autocommit=True) as conn:
        s = ingest_file(conn, args.source, args.path, args.received_at)

    print(f"Lote {s.batch_id} · {args.source} · {args.path.name}")
    print(f"  estado: {s.status}")
    if s.message:
        print(f"  {s.message}")
    if s.status == "loaded":
        print(f"  filas: {s.rows_received} · rechazadas: {s.rows_rejected}")
        print(f"  nuevas: {s.inserted} · idénticas: {s.identical} · conflictos: {s.conflicts}")
        for rejection in s.rejections:
            print(f"  ✗ {rejection}")


if __name__ == "__main__":
    main()