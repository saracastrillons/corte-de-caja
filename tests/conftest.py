"""Preparación compartida de las pruebas.

Cada prueba recibe una base de datos NUEVA, copiada de una plantilla ya migrada.
Así ninguna prueba depende de lo que hizo otra, y tu base de trabajo nunca se toca.
"""

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from cortecaja.db import database_url
from cortecaja.ingest import ingest_file
from cortecaja.migrate import migrate

TEMPLATE_DB = "cortecaja_test_template"
TEST_DB = "cortecaja_test"
DEMO = Path(__file__).resolve().parents[1] / "data" / "demo"
SAO_PAULO = ZoneInfo("America/Sao_Paulo")


def url_for(dbname: str) -> str:
    return make_conninfo(database_url(), dbname=dbname)


@pytest.fixture(scope="session")
def admin():
    """Conexión a la base principal, solo para crear y borrar las bases de prueba."""
    with psycopg.connect(database_url(), autocommit=True) as conn:
        yield conn


@pytest.fixture(scope="session")
def template(admin):
    """Una base migrada una sola vez, que sirve de molde para todas las pruebas."""
    admin.execute(f"DROP DATABASE IF EXISTS {TEMPLATE_DB} WITH (FORCE)")
    admin.execute(f"CREATE DATABASE {TEMPLATE_DB}")
    with psycopg.connect(url_for(TEMPLATE_DB), autocommit=True) as conn:
        migrate(conn)
    yield TEMPLATE_DB
    admin.execute(f"DROP DATABASE IF EXISTS {TEMPLATE_DB} WITH (FORCE)")


@pytest.fixture
def conn(admin, template):
    """Una base limpia por prueba (copiar la plantilla tarda milisegundos)."""
    admin.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")
    admin.execute(f"CREATE DATABASE {TEST_DB} TEMPLATE {template}")
    with psycopg.connect(url_for(TEST_DB), autocommit=True) as connection:
        yield connection
    admin.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")


# ---------------------------------------------------------------------------
# Ayudantes para cargar el escenario de demo con horas de recepción simuladas
# ---------------------------------------------------------------------------

def at(day: int, hour: int, minute: int = 0) -> datetime:
    """Una hora de noviembre de 2026 en São Paulo."""
    return datetime(2026, 11, day, hour, minute, tzinfo=SAO_PAULO)


def load(conn, source: str, file_name: str, received_at: datetime):
    return ingest_file(conn, source, DEMO / file_name, received_at)


def load_store(conn):
    load(conn, "store_orders", "orders.csv", at(17, 8))
    load(conn, "store_order_items", "order_items.csv", at(17, 8))


def load_day_17(conn, with_bank: bool = True):
    load_store(conn)
    load(conn, "gateway_events", "gateway_2026-11-17.jsonl", at(17, 8, 5))
    if with_bank:
        load(conn, "bank_statement", "bank_2026-11-17.csv", at(17, 18))


def load_day_18(conn):
    load(conn, "gateway_events", "gateway_2026-11-18.jsonl", at(18, 8, 5))
    load(conn, "bank_statement", "bank_2026-11-18.csv", at(18, 18))