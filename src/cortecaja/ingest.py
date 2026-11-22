"""Carga un archivo: lo registra como lote, guarda cada fila en raw y pasa las válidas a core.

Uso:
    python -m cortecaja.ingest <fuente> <archivo> [--received-at 2026-11-17T18:00:00-03:00]

Fuentes: store_orders, store_order_items, gateway_events, bank_statement

Versión por lotes: en vez de ir a la base una o dos veces por fila, valida todo el archivo
en Python, consulta las referencias de una vez y escribe cada tabla con un solo executemany.
Las reglas son las mismas que en la versión fila a fila; solo cambia cuántas idas y vueltas hay.
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

# Orden de escritura: primero lo que otros registros citan (los eventos antes que las liquidaciones).
WRITE_ORDER = ["order", "order_item", "payment_event", "settlement", "bank_entry"]


@dataclass
class Row:
    """Una fila del archivo mientras se procesa."""
    number: int
    raw: dict
    record: dict | None = None
    error: str | None = None


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


def pairs(keys) -> tuple[list, list]:
    """{(a, 1), (b, 2)} -> ([a, b], [1, 2]) para consultar muchas claves con unnest()."""
    keys = list(keys)
    return [k[0] for k in keys], [k[1] for k in keys]


# ---------------------------------------------------------------------------
# 1) Referencias: TODAS las del archivo en dos o tres consultas
# ---------------------------------------------------------------------------

def check_references(conn: psycopg.Connection, rows: list[Row]) -> None:
    """Marca con su motivo las filas que citan algo que no existe. No escribe nada."""
    items = [r for r in rows if r.record and r.record["kind"] == "order_item"]
    if items:
        wanted = list({r.record["order_id"] for r in items})
        known = {order_id for (order_id,) in conn.execute(
            "SELECT order_id FROM core.orders WHERE order_id = ANY(%s)", (wanted,))}
        for r in items:
            if r.record["order_id"] not in known:
                r.error = f"El pedido {r.record['order_id']} no existe"

    settlements = [r for r in rows if r.record and r.record["kind"] == "settlement"]
    if not settlements:
        return

    # Eventos que una liquidación puede citar: los de la base y los válidos de este mismo archivo.
    in_file = {}
    for r in rows:
        if r.record and r.record["kind"] == "payment_event":
            in_file.setdefault((r.record["provider"], r.record["external_event_id"]), r.record["event_type"])
    cited = {(r.record["provider"], line["external_event_id"])
             for r in settlements for line in r.record["lines"] if line["external_event_id"]}
    providers, event_ids = pairs(cited)
    in_db = {(p, e): t for p, e, t in conn.execute(
        """SELECT provider, external_event_id, event_type FROM core.payment_events
           WHERE (provider, external_event_id) IN (SELECT unnest(%s::text[]), unnest(%s::text[]))""",
        (providers, event_ids))}
    settled = {(p, e): s for p, e, s in conn.execute(
        """SELECT provider, external_event_id, settlement_id FROM core.settlement_lines
           WHERE (provider, external_event_id) IN (SELECT unnest(%s::text[]), unnest(%s::text[]))""",
        (providers, event_ids))}

    for r in settlements:
        record = r.record
        try:
            for line in record["lines"]:
                if line["external_event_id"] is None:
                    continue
                key = (record["provider"], line["external_event_id"])
                event_type = in_db.get(key) or in_file.get(key)
                if event_type is None:
                    raise RowError(f"La línea {line['line_number']} cita el evento {key[1]}, que no existe")
                if event_type != line["line_type"]:
                    raise RowError(f"El evento {key[1]} es '{event_type}' pero la línea dice '{line['line_type']}'")
                if settled.get(key) not in (None, record["settlement_id"]):
                    raise RowError(f"El evento {key[1]} ya está en la liquidación {settled[key]}")
            for line in record["lines"]:          # este archivo también "reclama" sus eventos
                if line["external_event_id"]:
                    settled[(record["provider"], line["external_event_id"])] = record["settlement_id"]
        except RowError as error:
            r.error = str(error)


# ---------------------------------------------------------------------------
# 2) Escritura en core: insertar lo nuevo de una vez y comparar lo que ya existía
# ---------------------------------------------------------------------------

def values_for(record: dict, batch_id: int, row_number: int) -> dict:
    values = {k: v for k, v in record.items() if k not in ("kind", "lines")}
    return values | {"src_batch_id": batch_id, "src_row": row_number}


def write_many(conn: psycopg.Connection, target: Target, values: list[dict]) -> list[tuple[str, tuple | None]]:
    """Para cada fila devuelve ('inserted', None), ('identical', fila) o ('conflict', fila_existente)."""
    columns = list(values[0])
    table = sql.Identifier(*target.table)
    key_cols = sql.SQL(", ").join(map(sql.Identifier, target.key))
    insert = sql.SQL(
        "INSERT INTO {table} ({cols}) VALUES ({vals}) ON CONFLICT ({key}) DO NOTHING RETURNING {key}"
    ).format(table=table, cols=sql.SQL(", ").join(map(sql.Identifier, columns)),
             vals=sql.SQL(", ").join(sql.Placeholder() * len(columns)), key=key_cols)

    inserted = set()
    with conn.cursor() as cur:
        cur.executemany(insert, [list(v.values()) for v in values], returning=True)
        while True:                                     # un resultado por fila enviada
            inserted.update(tuple(row) for row in cur.fetchall())
            if not cur.nextset():
                break

    # Lo que no se insertó ya existía: se trae en UNA consulta para compararlo.
    keys = [tuple(v[k] for k in target.key) for v in values]
    need, seen = set(), set()
    for key in keys:
        if key not in inserted or key in seen:          # ya existía, o se repite dentro del archivo
            need.add(key)
        seen.add(key)

    existing = {}
    if need:
        sample = next(iter(need))                       # el tipo de cada columna de la clave
        unnests = sql.SQL(", ").join(
            sql.SQL("unnest(%s::bigint[])" if isinstance(value, int) else "unnest(%s::text[])")
            for value in sample)
        select = sql.SQL("SELECT {key}, {cols} FROM {table} WHERE ({key}) IN (SELECT {unnests})").format(
            key=key_cols, cols=sql.SQL(", ").join(map(sql.Identifier, target.compare)),
            table=table, unnests=unnests)
        params = [[key[i] for key in need] for i in range(len(target.key))]
        n = len(target.key)
        existing = {tuple(row[:n]): tuple(row[n:]) for row in conn.execute(select, params)}

    outcomes, claimed = [], set()
    for key, v in zip(keys, values):
        if key in inserted and key not in claimed:      # la primera aparición es la que entró
            claimed.add(key)
            outcomes.append(("inserted", None))
            continue
        old = existing[key]
        new = tuple(v[c] for c in target.compare)
        outcomes.append(("identical" if old == new else "conflict", old))
    return outcomes


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


def write_core(conn: psycopg.Connection, rows: list[Row], batch_id: int, summary: Summary) -> None:
    valid = [r for r in rows if r.error is None]
    for kind in WRITE_ORDER:
        group = [r for r in valid if r.record["kind"] == kind]
        if not group:
            continue
        target = TARGETS[kind]
        values = [values_for(r.record, batch_id, r.number) for r in group]
        new_settlements = []

        for r, v, (outcome, existing) in zip(group, values, write_many(conn, target, values)):
            if outcome == "inserted":
                summary.inserted += 1
                if kind == "settlement":
                    new_settlements.append(r)
            elif outcome == "identical":
                summary.identical += 1
            else:
                summary.conflicts += 1
                open_conflict(conn, target, v, existing, batch_id)

        if new_settlements:                             # las líneas de todas las liquidaciones nuevas
            with conn.cursor() as cur:
                cur.executemany(
                    """INSERT INTO core.settlement_lines
                         (provider, settlement_id, line_number, line_type, external_event_id,
                          amount, src_batch_id, src_row)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                    [(r.record["provider"], r.record["settlement_id"], line["line_number"],
                      line["line_type"], line["external_event_id"], line["amount"], batch_id, r.number)
                     for r in new_settlements for line in r.record["lines"]],
                )


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
        raw_rows = read_rows(source, content)
    except StructureError as error:
        with conn.transaction():
            return Summary(new_batch("failed", str(error)), "failed", message=str(error))

    # 3) Validar en Python (sin tocar la base) y revisar las referencias de una vez.
    rows = []
    for number, raw in enumerate(raw_rows, start=1):
        row = Row(number, raw)
        try:
            row.record = validate(source, raw)
        except RowError as error:
            row.error = str(error)
        rows.append(row)

    # 4) Todo el archivo en UNA transacción: o queda completo, o no queda nada.
    with conn.transaction():
        check_references(conn, rows)
        batch_id = new_batch("received")
        summary = Summary(batch_id, "loaded", rows_received=len(rows))

        with conn.cursor() as cur:                      # cada fila tal como llegó, en un solo envío
            cur.executemany(
                "INSERT INTO raw.source_record VALUES (%s, %s, %s, %s, %s)",
                [(batch_id, r.number, Jsonb(r.raw), r.error is None, r.error) for r in rows],
            )
        for r in rows:
            if r.error:
                summary.rows_rejected += 1
                summary.rejections.append(f"fila {r.number}: {r.error}")

        write_core(conn, rows, batch_id, summary)

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