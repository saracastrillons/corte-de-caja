"""Conexión a PostgreSQL."""

import os

import psycopg

# Base local de Docker Compose (puerto 5433). En CI se cambia con la variable DATABASE_URL.
DEFAULT_URL = "postgresql://cortecaja:cortecaja@localhost:5433/cortecaja"


def database_url() -> str:
    return os.environ.get("DATABASE_URL", DEFAULT_URL)


def connect(**kwargs) -> psycopg.Connection:
    return psycopg.connect(database_url(), **kwargs)