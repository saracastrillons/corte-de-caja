"""Reconstruye la historia completa de la demo con un solo comando.

Uso:
    python -m cortecaja.demo            # carga la demo solo si la base está vacía
    python -m cortecaja.demo --reset    # borra todo y la vuelve a contar desde cero

La historia: el 17 se cargan pedidos, pasarela y banco y se publica el cierre
(la liquidación de R$1.250 aparece vencida). El 18 llegan la pasarela y el banco
con el depósito tardío, se publica el cierre del 18 y se reenvía el extracto.
"""

import argparse
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg

from cortecaja.close import run_close
from cortecaja.db import connect
from cortecaja.ingest import ingest_file
from cortecaja.migrate import migrate

DEMO_DIR = Path(__file__).resolve().parents[2] / "data" / "demo"
SAO_PAULO = ZoneInfo("America/Sao_Paulo")


def at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 11, day, hour, minute, tzinfo=SAO_PAULO)


# (fuente, archivo, hora de recepción simulada) — en el orden en que "llegaron"
DAY_17 = [
    ("store_orders", "orders.csv", at(17, 8)),
    ("store_order_items", "order_items.csv", at(17, 8)),
    ("gateway_events", "gateway_2026-11-17.jsonl", at(17, 8, 5)),
    ("bank_statement", "bank_2026-11-17.csv", at(17, 18)),
]
DAY_18 = [
    ("gateway_events", "gateway_2026-11-18.jsonl", at(18, 8, 5)),
    ("bank_statement", "bank_2026-11-18.csv", at(18, 18)),
    ("bank_statement", "bank_2026-11-18.csv", at(18, 19)),     # reenvío: debe ignorarse
]


def reset(conn: psycopg.Connection) -> None:
    conn.execute("DROP SCHEMA IF EXISTS recon, ops, core, raw CASCADE")
    conn.execute("DROP TABLE IF EXISTS public.schema_migrations")


def has_data(conn: psycopg.Connection) -> bool:
    return conn.execute("SELECT EXISTS (SELECT 1 FROM raw.ingestion_batch)").fetchone()[0]


def play(conn: psycopg.Connection) -> None:
    for source, name, received_at in DAY_17:
        summary = ingest_file(conn, source, DEMO_DIR / name, received_at)
        print(f"  17 nov · lote {summary.batch_id} · {name} · {summary.status}")
    result = run_close(conn, date(2026, 11, 17), at(17, 23, 59), publish=True)
    print(f"  cierre 17 nov · {result['status']} · pendiente R$ {result['totals']['pending']}")

    for source, name, received_at in DAY_18:
        summary = ingest_file(conn, source, DEMO_DIR / name, received_at)
        print(f"  18 nov · lote {summary.batch_id} · {name} · {summary.status}")
    result = run_close(conn, date(2026, 11, 18), at(18, 23, 59), publish=True)
    print(f"  cierre 18 nov · {result['status']} · pendiente R$ {result['totals']['pending']}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Carga la historia de la demo.")
    parser.add_argument("--reset", action="store_true", help="Borra todo antes de cargar.")
    args = parser.parse_args()

    with connect(autocommit=True) as conn:
        if args.reset:
            reset(conn)
        migrate(conn)
        if has_data(conn):
            print("La base ya tiene datos: no se carga la demo (usa --reset para empezar de cero).")
            return
        print("Cargando la demo…")
        play(conn)
    print("Listo. Abre la mesa y busca simpay/STL-1.")


if __name__ == "__main__":
    main()