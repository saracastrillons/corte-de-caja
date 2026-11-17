"""Aplica, en orden, las migraciones de sql/migrations que todavía no se han aplicado.

Uso:  python -m cortecaja.migrate
"""

import hashlib
from pathlib import Path

import psycopg

from cortecaja.db import connect

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "sql" / "migrations"

REGISTRY_DDL = """
CREATE TABLE IF NOT EXISTS public.schema_migrations (
    version     TEXT        PRIMARY KEY,
    checksum    CHAR(64)    NOT NULL,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


def discover(directory: Path = MIGRATIONS_DIR) -> list[Path]:
    """Archivos con nombre 001_algo.sql, 002_algo.sql..., ordenados por número."""
    return sorted(directory.glob("[0-9][0-9][0-9]_*.sql"))


def checksum(path: Path) -> str:
    """Huella del archivo: si alguien lo edita después de aplicarlo, la huella cambia."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def migrate(conn: psycopg.Connection, directory: Path = MIGRATIONS_DIR) -> list[str]:
    """Aplica las migraciones pendientes y devuelve las que aplicó en esta corrida."""
    with conn.transaction():
        conn.execute(REGISTRY_DDL)

    applied = dict(conn.execute("SELECT version, checksum FROM public.schema_migrations").fetchall())

    newly_applied = []
    for path in discover(directory):
        version = path.stem
        digest = checksum(path)

        if version in applied:
            if applied[version] != digest:
                raise RuntimeError(
                    f"La migración {path.name} cambió después de aplicarse. "
                    "No se edita una migración aplicada: crea una nueva con el siguiente número."
                )
            continue

        # La migración y su registro se guardan juntos, o no se guarda ninguno.
        with conn.transaction():
            # utf-8-sig: tolera la marca invisible (BOM) que a veces pone Windows al inicio
            conn.execute(path.read_text(encoding="utf-8-sig"))
            conn.execute(
                "INSERT INTO public.schema_migrations (version, checksum) VALUES (%s, %s)",
                (version, digest),
            )
        newly_applied.append(version)

    return newly_applied


def main() -> None:
    with connect(autocommit=True) as conn:
        newly_applied = migrate(conn)

    if newly_applied:
        for version in newly_applied:
            print(f"Aplicada: {version}")
    else:
        print("La base ya está al día. No hay migraciones pendientes.")


if __name__ == "__main__":
    main()