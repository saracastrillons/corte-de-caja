"""Consultas que alimentan la mesa. Solo leen: la interfaz nunca recalcula montos por su cuenta."""

from datetime import date

import psycopg


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


def cases(conn: psycopg.Connection, day: date, status: str) -> list[dict]:
    """La bandeja: primero lo que más dinero involucra."""
    return conn.execute(
        """
        SELECT case_id, case_type, level, entity_key,
               expected_amount, received_amount, difference,
               due_on, next_action, opened_at, resolved_at,
               CASE WHEN status = 'open'
                    THEN GREATEST(%(d)s - opened_at::date, 0)
                    ELSE resolved_at::date - opened_at::date
               END AS days_open
        FROM ops.reconciliation_case
        WHERE status = %(s)s
        ORDER BY ABS(difference) DESC, case_id
        """,
        {"d": day, "s": status},
    ).fetchall()