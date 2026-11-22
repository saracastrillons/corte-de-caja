"""Consultas que alimentan la mesa. Solo leen: la interfaz nunca recalcula montos por su cuenta."""

from datetime import date, datetime

import psycopg

from cortecaja.close import RESULT_STATUS


# ---------------------------------------------------------------------------
# La mesa: día operativo, totales y bandeja
# ---------------------------------------------------------------------------

def operating_day(conn: psycopg.Connection) -> date:
    """El último día con un cierre publicado es el 'hoy' de la operación."""
    row = conn.execute(
        """SELECT MAX(business_date) AS day FROM ops.close_run
           WHERE status IN ('published', 'superseded')"""
    ).fetchone()
    return row["day"] or date.today()


def last_close(conn: psycopg.Connection, day: date) -> dict | None:
    return conn.execute(
        """SELECT close_id, version, status, received_cutoff, published_at, missing_sources
           FROM ops.close_run
           WHERE business_date = %s
           ORDER BY version DESC
           LIMIT 1""",
        (day,),
    ).fetchone()


def day_totals(conn: psycopg.Connection, day: date) -> dict:
    """Esperado, recibido y pendiente con todo lo que sabemos hoy (nivel pasarela → banco)."""
    return conn.execute(
        """
        SELECT
          COALESCE(SUM(expected_amount) FILTER (WHERE item_type = 'settlement' AND due_on <= %(d)s), 0) AS expected,
          COALESCE(SUM(received_amount) FILTER (WHERE item_type = 'settlement' AND due_on <= %(d)s), 0) AS received,
          COALESCE(SUM(expected_amount - received_amount)
                   FILTER (WHERE item_type = 'settlement' AND due_on <= %(d)s), 0)                    AS pending,
          COALESCE(SUM(expected_amount) FILTER (WHERE item_type = 'settlement' AND due_on > %(d)s), 0)  AS in_transit,
          COALESCE(SUM(received_amount) FILTER (WHERE item_type = 'bank_entry'), 0)                     AS unidentified
        FROM recon.gateway_bank(%(d)s)
        """,
        {"d": day},
    ).fetchone()


def case_counts(conn: psycopg.Connection) -> dict:
    rows = conn.execute(
        "SELECT status, COUNT(*) AS n FROM ops.reconciliation_case GROUP BY status"
    ).fetchall()
    return {row["status"]: row["n"] for row in rows}


CASE_COLUMNS = """
    case_id, case_type, level, entity_type, entity_key, status,
    expected_amount, received_amount, difference,
    due_on, next_action, opened_at, resolved_at,
    CASE WHEN status = 'open'
         THEN GREATEST(%(d)s - opened_at::date, 0)
         ELSE resolved_at::date - opened_at::date
    END AS days_open
"""


PAGE_SIZE = 25


def case_type_counts(conn: psycopg.Connection, status: str) -> list[dict]:
    """Cuántos casos hay de cada tipo y cuánto dinero involucran: los filtros de la bandeja."""
    return conn.execute(
        """SELECT case_type, COUNT(*) AS n, SUM(ABS(difference)) AS amount
           FROM ops.reconciliation_case
           WHERE status = %s
           GROUP BY case_type
           ORDER BY SUM(ABS(difference)) DESC, case_type""",
        (status,),
    ).fetchall()


def cases(conn: psycopg.Connection, day: date, status: str, case_type: str | None = None,
          search: str | None = None, page: int = 1) -> tuple[list[dict], int]:
    """La bandeja: primero lo que más dinero involucra. Devuelve (casos de la página, total)."""
    params = {"d": day, "s": status, "t": case_type, "q": f"%{search}%" if search else None,
              "limit": PAGE_SIZE, "offset": (page - 1) * PAGE_SIZE}
    where = """status = %(s)s
               AND (%(t)s::text IS NULL OR case_type = %(t)s)
               AND (%(q)s::text IS NULL OR entity_key ILIKE %(q)s)"""
    total = conn.execute(
        f"SELECT COUNT(*) AS n FROM ops.reconciliation_case WHERE {where}", params
    ).fetchone()["n"]
    rows = conn.execute(
        f"""SELECT {CASE_COLUMNS}
            FROM ops.reconciliation_case
            WHERE {where}
            ORDER BY ABS(difference) DESC, case_id
            LIMIT %(limit)s OFFSET %(offset)s""",
        params,
    ).fetchall()
    return rows, total


def flow_summary(conn: psycopg.Connection, day: date) -> dict:
    """El recorrido del dinero en tres etapas, con cuántas partidas cuadran en cada una."""
    orders = conn.execute(
        """SELECT COUNT(*) FILTER (WHERE status <> 'charge_without_order') AS total,
                  COUNT(*) FILTER (WHERE status = 'matched')               AS ok,
                  COUNT(*) FILTER (WHERE status NOT IN ('matched', 'charge_without_order')) AS issues,
                  COUNT(*) FILTER (WHERE status = 'charge_without_order')  AS orphans,
                  COALESCE(SUM(charged_amount), 0)                          AS amount
           FROM recon.order_gateway()"""
    ).fetchone()
    bank = conn.execute(
        """SELECT COUNT(*) FILTER (WHERE item_type = 'settlement')                              AS total,
                  COUNT(*) FILTER (WHERE item_type = 'settlement' AND status = 'reconciled')     AS ok,
                  COUNT(*) FILTER (WHERE item_type = 'settlement' AND status = 'in_transit')     AS waiting,
                  COUNT(*) FILTER (WHERE item_type = 'settlement'
                                   AND status NOT IN ('reconciled', 'in_transit'))               AS issues,
                  COUNT(*) FILTER (WHERE item_type = 'bank_entry')                               AS unidentified,
                  COALESCE(SUM(expected_amount) FILTER (WHERE item_type = 'settlement'), 0)      AS amount
           FROM recon.gateway_bank(%s)""",
        (day,),
    ).fetchone()
    deposits = conn.execute(
        """SELECT COUNT(*) AS total, COALESCE(SUM(amount), 0) AS amount
           FROM core.bank_entries WHERE amount > 0 AND booked_on <= %s""",
        (day,),
    ).fetchone()
    deposits["unidentified"] = bank["unidentified"]
    deposits["ok"] = deposits["total"] - bank["unidentified"]
    return {"orders": orders, "settlements": bank, "deposits": deposits}


def case_detail(conn: psycopg.Connection, case_id: int, day: date) -> dict | None:
    return conn.execute(
        f"SELECT {CASE_COLUMNS} FROM ops.reconciliation_case WHERE case_id = %(id)s",
        {"d": day, "id": case_id},
    ).fetchone()


def case_history(conn: psycopg.Connection, case_id: int) -> list[dict]:
    """Cada cambio del caso, con el archivo que lo provocó."""
    return conn.execute(
        """SELECT e.event_type, e.note, e.actor, e.recorded_at,
                  b.batch_id, b.file_name, b.received_at
           FROM ops.case_event e
           LEFT JOIN raw.ingestion_batch b ON b.batch_id = e.caused_by_batch_id
           WHERE e.case_id = %s
           ORDER BY e.event_id""",
        (case_id,),
    ).fetchall()


# ---------------------------------------------------------------------------
# La ficha de evidencia: los dos registros frente a frente
# Cada fila trae src_batch_id / src_row para poder llegar al archivo de origen.
# ---------------------------------------------------------------------------

def split_key(key: str) -> tuple[str, str]:
    """'simpay/STL-1' -> ('simpay', 'STL-1')"""
    left, _, right = key.partition("/")
    return left, right


def settlement_record(conn: psycopg.Connection, key: str) -> dict | None:
    provider, settlement_id = split_key(key)
    head = conn.execute(
        """SELECT s.*, b.file_name, b.received_at
           FROM core.settlements s
           JOIN raw.ingestion_batch b ON b.batch_id = s.src_batch_id
           WHERE s.provider = %s AND s.settlement_id = %s""",
        (provider, settlement_id),
    ).fetchone()
    if head:
        head["lines"] = conn.execute(
            """SELECT l.line_number, l.line_type, l.external_event_id, l.amount,
                      l.src_batch_id, l.src_row, e.order_ref, e.payment_method, e.occurred_at
               FROM core.settlement_lines l
               LEFT JOIN core.payment_events e
                      ON e.provider = l.provider AND e.external_event_id = l.external_event_id
               WHERE l.provider = %s AND l.settlement_id = %s
               ORDER BY l.line_number""",
            (provider, settlement_id),
        ).fetchall()
    return head


def linked_deposits(conn: psycopg.Connection, key: str) -> list[dict]:
    provider, settlement_id = split_key(key)
    return conn.execute(
        """SELECT be.*, d.allocated_amount, d.matched_by, b.file_name, b.received_at
           FROM core.settlement_deposits d
           JOIN core.bank_entries be
             ON be.bank_account = d.bank_account AND be.external_entry_id = d.external_entry_id
           JOIN raw.ingestion_batch b ON b.batch_id = be.src_batch_id
           WHERE d.provider = %s AND d.settlement_id = %s
           ORDER BY be.booked_on, be.external_entry_id""",
        (provider, settlement_id),
    ).fetchall()


def candidate_deposits(conn: psycopg.Connection, day: date, net) -> list[dict]:
    """Depósitos que recon.gateway_bank marcó como candidatos por el mismo monto."""
    return conn.execute(
        """SELECT be.*, b.file_name, b.received_at
           FROM recon.gateway_bank(%s) g
           JOIN core.bank_entries be ON be.bank_account || '/' || be.external_entry_id = g.item_key
           JOIN raw.ingestion_batch b ON b.batch_id = be.src_batch_id
           WHERE g.item_type = 'bank_entry' AND g.status = 'ambiguous' AND g.received_amount = %s
           ORDER BY be.external_entry_id""",
        (day, net),
    ).fetchall()


def bank_entry(conn: psycopg.Connection, key: str) -> dict | None:
    account, entry_id = split_key(key)
    return conn.execute(
        """SELECT be.*, b.file_name, b.received_at
           FROM core.bank_entries be
           JOIN raw.ingestion_batch b ON b.batch_id = be.src_batch_id
           WHERE be.bank_account = %s AND be.external_entry_id = %s""",
        (account, entry_id),
    ).fetchone()


def order_record(conn: psycopg.Connection, order_id: str) -> dict | None:
    head = conn.execute(
        """SELECT o.*, b.file_name, b.received_at
           FROM core.orders o
           JOIN raw.ingestion_batch b ON b.batch_id = o.src_batch_id
           WHERE o.order_id = %s""",
        (order_id,),
    ).fetchone()
    if head:
        head["items"] = conn.execute(
            """SELECT i.*, i.price + i.freight AS total, b.file_name, b.received_at
               FROM core.order_items i
               JOIN raw.ingestion_batch b ON b.batch_id = i.src_batch_id
               WHERE i.order_id = %s
               ORDER BY i.line_number""",
            (order_id,),
        ).fetchall()
    return head


def payment_events(conn: psycopg.Connection, order_ref: str) -> list[dict]:
    return conn.execute(
        """SELECT e.*, b.file_name, b.received_at
           FROM core.payment_events e
           JOIN raw.ingestion_batch b ON b.batch_id = e.src_batch_id
           WHERE e.order_ref = %s
           ORDER BY e.occurred_at, e.external_event_id""",
        (order_ref,),
    ).fetchall()


def observations(conn: psycopg.Connection, key: str) -> list[dict]:
    """Todas las veces que llegó un mismo evento, tal como llegaron (para los conflictos)."""
    provider, event_id = split_key(key)
    return conn.execute(
        """SELECT r.batch_id, r.row_number, r.payload, b.file_name, b.received_at
           FROM raw.source_record r
           JOIN raw.ingestion_batch b ON b.batch_id = r.batch_id
           WHERE b.source = 'gateway_events'
             AND r.payload->>'provider' = %s AND r.payload->>'event_id' = %s
           ORDER BY b.received_at, r.batch_id""",
        (provider, event_id),
    ).fetchall()


def source_row(conn: psycopg.Connection, batch_id: int, row_number: int) -> dict | None:
    return conn.execute(
        """SELECT r.*, b.source, b.file_name, b.file_sha256, b.received_at, b.status AS batch_status
           FROM raw.source_record r
           JOIN raw.ingestion_batch b ON b.batch_id = r.batch_id
           WHERE r.batch_id = %s AND r.row_number = %s""",
        (batch_id, row_number),
    ).fetchone()


# ---------------------------------------------------------------------------
# Cierres: lista y comparación "tal como se publicó" vs "con los datos de hoy"
# ---------------------------------------------------------------------------

def closes(conn: psycopg.Connection) -> list[dict]:
    return conn.execute(
        """SELECT close_id, business_date, version, status, received_cutoff, published_at,
                  missing_sources, total_expected, total_received, total_pending, supersedes_close_id,
                  (SELECT COUNT(*) FROM ops.close_result r
                   WHERE r.close_id = c.close_id AND r.item_status <> 'reconciled') AS open_items
           FROM ops.close_run c
           ORDER BY business_date DESC, version DESC"""
    ).fetchall()


def close_run(conn: psycopg.Connection, close_id: int) -> dict | None:
    return conn.execute("SELECT * FROM ops.close_run WHERE close_id = %s", (close_id,)).fetchone()


def live_items(conn: psycopg.Connection, day: date) -> dict:
    """Estado actual de cada partida, con todo lo recibido hasta hoy."""
    items = {}
    for row in conn.execute(
        "SELECT order_ref, status, expected_amount, charged_amount FROM recon.order_gateway()"
    ):
        items[("order", row["order_ref"])] = {
            "status": RESULT_STATUS[row["status"]], "expected": row["expected_amount"],
            "received": row["charged_amount"], "evidence": None,
        }
    for row in conn.execute(
        """SELECT item_type, item_key, status, expected_amount, received_amount, evidence
           FROM recon.gateway_bank(%s)""", (day,)
    ):
        items[(row["item_type"], row["item_key"])] = {
            "status": RESULT_STATUS[row["status"]], "expected": row["expected_amount"],
            "received": row["received_amount"], "evidence": row["evidence"],
        }
    return items


def late_files(conn: psycopg.Connection, item_type: str, key: str, cutoff: datetime) -> list[dict]:
    """Archivos recibidos DESPUÉS del corte que tocan esta partida: explican el cambio."""
    if item_type == "settlement":
        provider, settlement_id = split_key(key)
        sql = """SELECT DISTINCT b.batch_id, b.file_name, b.received_at, be.src_row AS row_number
                 FROM core.settlement_deposits d
                 JOIN core.bank_entries be
                   ON be.bank_account = d.bank_account AND be.external_entry_id = d.external_entry_id
                 JOIN raw.ingestion_batch b ON b.batch_id = be.src_batch_id
                 WHERE d.provider = %s AND d.settlement_id = %s AND b.received_at > %s"""
        params = (provider, settlement_id, cutoff)
    elif item_type == "order":
        sql = """SELECT DISTINCT b.batch_id, b.file_name, b.received_at, e.src_row AS row_number
                 FROM core.payment_events e
                 JOIN raw.ingestion_batch b ON b.batch_id = e.src_batch_id
                 WHERE e.order_ref = %s AND b.received_at > %s"""
        params = (key, cutoff)
    else:
        account, entry_id = split_key(key)
        sql = """SELECT b.batch_id, b.file_name, b.received_at, be.src_row AS row_number
                 FROM core.bank_entries be
                 JOIN raw.ingestion_batch b ON b.batch_id = be.src_batch_id
                 WHERE be.bank_account = %s AND be.external_entry_id = %s AND b.received_at > %s"""
        params = (account, entry_id, cutoff)
    return conn.execute(sql + " ORDER BY received_at", params).fetchall()


def close_comparison(conn: psycopg.Connection, close: dict, today: date) -> list[dict]:
    """Cada partida de la foto publicada junto a su estado actual."""
    published = conn.execute(
        """SELECT item_type, item_key, item_status, expected_amount, received_amount
           FROM ops.close_result WHERE close_id = %s""",
        (close["close_id"],),
    ).fetchall()
    live = live_items(conn, today)

    rows = []
    for item in published:
        now = live.get((item["item_type"], item["item_key"]))
        changed = now is not None and (
            now["status"] != item["item_status"] or now["received"] != item["received_amount"]
        )
        rows.append({
            "item_type": item["item_type"],
            "item_key": item["item_key"],
            "then": {"status": item["item_status"], "expected": item["expected_amount"],
                     "received": item["received_amount"]},
            "now": now,
            "changed": changed,
            "files": late_files(conn, item["item_type"], item["item_key"], close["received_cutoff"])
                     if changed else [],
        })

    rows.sort(key=lambda r: (not r["changed"], r["then"]["status"] == "reconciled",
                             r["item_type"] != "settlement", r["item_key"]))
    return rows