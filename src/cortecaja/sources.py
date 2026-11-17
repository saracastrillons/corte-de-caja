"""Lectura y validación de los archivos de entrada.

Este módulo NO toca la base de datos: recibe el contenido de un archivo y devuelve
filas limpias o explica por qué una fila no sirve. Por eso se puede probar sin Postgres.
"""

import csv
import io
import json
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

SOURCES = ("store_orders", "store_order_items", "gateway_events", "bank_statement")

# Olist trae fechas sin zona horaria; son horas de São Paulo.
STORE_TZ = ZoneInfo("America/Sao_Paulo")

EXPECTED_COLUMNS = {
    "store_orders": ["order_id", "customer_id", "order_status", "order_purchase_timestamp"],
    "store_order_items": ["order_id", "order_item_id", "product_id", "price", "freight_value"],
    "bank_statement": ["bank_account", "entry_id", "booked_on", "amount", "currency",
                       "description", "reference"],
}

PAYMENT_METHODS = {"credit_card", "debit_card", "boleto", "voucher"}
CENT = Decimal("0.01")


class StructureError(Exception):
    """Todo el archivo es inválido (vacío, encabezados equivocados...). El lote queda 'failed'."""


class RowError(Exception):
    """Una fila concreta es inválida. Se guarda con su motivo y el resto del archivo sigue."""


# ---------------------------------------------------------------------------
# 1) Leer el archivo: bytes -> lista de diccionarios (todavía sin validar)
# ---------------------------------------------------------------------------

def read_rows(source: str, content: bytes) -> list[dict]:
    text = content.decode("utf-8-sig")

    if source == "gateway_events":
        lines = [line for line in text.splitlines() if line.strip()]
        if not lines:
            raise StructureError("El archivo no trae registros.")
        rows = []
        for line in lines:
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                obj = None
            # Una línea dañada no tumba el archivo: se guarda tal cual para revisarla.
            rows.append(obj if isinstance(obj, dict) else {"_unparsed_line": line})
        return rows

    reader = csv.DictReader(io.StringIO(text, newline=""))
    expected = EXPECTED_COLUMNS[source]
    missing = [col for col in expected if col not in (reader.fieldnames or [])]
    if missing:
        raise StructureError(f"Faltan columnas: {', '.join(missing)}")
    rows = list(reader)
    if not rows:
        raise StructureError("El archivo no trae filas.")
    return rows


# ---------------------------------------------------------------------------
# 2) Ayudantes de validación: cada uno devuelve el valor limpio o lanza RowError
# ---------------------------------------------------------------------------

def required(row: dict, field: str) -> str:
    value = row.get(field)
    if value is None or str(value).strip() == "":
        raise RowError(f"Falta el campo '{field}'")
    return str(value).strip()


def optional(row: dict, field: str) -> str | None:
    value = row.get(field)
    return None if value is None or str(value).strip() == "" else str(value).strip()


def money(value: str, field: str) -> Decimal:
    try:
        amount = Decimal(value)
    except InvalidOperation:
        raise RowError(f"'{field}' no es un importe válido: {value!r}") from None
    if not amount.is_finite() or amount != amount.quantize(CENT):
        raise RowError(f"'{field}' debe tener máximo 2 decimales: {value!r}")
    return amount


def brl(row: dict) -> str:
    currency = required(row, "currency")
    if currency != "BRL":
        raise RowError(f"Moneda no soportada: {currency!r} (solo BRL)")
    return currency


def timestamp(value: str, field: str) -> datetime:
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        raise RowError(f"'{field}' no es una fecha y hora válida: {value!r}") from None
    return moment if moment.tzinfo else moment.replace(tzinfo=STORE_TZ)


def day(value: str, field: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise RowError(f"'{field}' no es una fecha válida: {value!r}") from None


def one_of(value: str, allowed: set[str], field: str) -> str:
    if value not in allowed:
        raise RowError(f"'{field}' tiene un valor desconocido: {value!r}")
    return value


# ---------------------------------------------------------------------------
# 3) Validar una fila según su fuente -> diccionario listo para la base
# ---------------------------------------------------------------------------

def validate(source: str, row: dict) -> dict:
    if "_unparsed_line" in row:
        raise RowError("La línea no es un JSON válido")
    return VALIDATORS[source](row)


def validate_order(row: dict) -> dict:
    return {
        "kind": "order",
        "order_id": required(row, "order_id"),
        "customer_id": required(row, "customer_id"),
        "order_status": required(row, "order_status"),
        "purchased_at": timestamp(required(row, "order_purchase_timestamp"), "order_purchase_timestamp"),
    }


def validate_order_item(row: dict) -> dict:
    line = required(row, "order_item_id")
    if not line.isdigit() or int(line) < 1:
        raise RowError(f"'order_item_id' debe ser un entero positivo: {line!r}")
    price = money(required(row, "price"), "price")
    freight = money(required(row, "freight_value"), "freight_value")
    if price < 0 or freight < 0:
        raise RowError("El precio y el envío no pueden ser negativos")
    return {
        "kind": "order_item",
        "order_id": required(row, "order_id"),
        "line_number": int(line),
        "product_id": required(row, "product_id"),
        "price": price,
        "freight": freight,
    }


def validate_gateway(row: dict) -> dict:
    record_type = required(row, "record_type")
    if record_type == "payment_event":
        return validate_payment_event(row)
    if record_type == "settlement":
        return validate_settlement(row)
    raise RowError(f"'record_type' desconocido: {record_type!r}")


def validate_payment_event(row: dict) -> dict:
    amount = money(required(row, "amount"), "amount")
    if amount <= 0:
        raise RowError("El importe de un evento debe ser positivo")
    return {
        "kind": "payment_event",
        "provider": required(row, "provider"),
        "external_event_id": required(row, "event_id"),
        "order_ref": required(row, "order_ref"),
        "event_type": one_of(required(row, "event_type"), {"charge", "refund"}, "event_type"),
        "payment_method": one_of(required(row, "payment_method"), PAYMENT_METHODS, "payment_method"),
        "amount": amount,
        "currency": brl(row),
        "occurred_at": timestamp(required(row, "occurred_at"), "occurred_at"),
    }


def validate_settlement(row: dict) -> dict:
    net = money(required(row, "net_amount"), "net_amount")
    raw_lines = row.get("lines")
    if not isinstance(raw_lines, list) or not raw_lines:
        raise RowError("La liquidación no trae líneas")

    lines = []
    for raw in raw_lines:
        if not isinstance(raw, dict):
            raise RowError("Una línea de la liquidación no es un objeto")
        line_type = one_of(required(raw, "type"), {"charge", "refund", "fee"}, "type")
        amount = money(required(raw, "amount"), "amount")
        event_id = optional(raw, "event_id")
        if line_type == "charge" and amount <= 0:
            raise RowError(f"Línea {raw.get('line')}: un cobro debe ser positivo")
        if line_type in ("refund", "fee") and amount >= 0:
            raise RowError(f"Línea {raw.get('line')}: un reembolso o comisión debe ser negativo")
        if (line_type == "fee") != (event_id is None):
            raise RowError(f"Línea {raw.get('line')}: solo la comisión va sin evento")
        lines.append({
            "line_number": int(required(raw, "line")),
            "line_type": line_type,
            "external_event_id": event_id,
            "amount": amount,
        })

    total = sum((line["amount"] for line in lines), Decimal("0"))
    if total != net:
        raise RowError(f"Las líneas suman {total} pero el neto reportado es {net}")

    return {
        "kind": "settlement",
        "provider": required(row, "provider"),
        "settlement_id": required(row, "settlement_id"),
        "settled_at": timestamp(required(row, "settled_at"), "settled_at"),
        "expected_payout_on": day(required(row, "expected_payout_on"), "expected_payout_on"),
        "reported_net": net,
        "currency": brl(row),
        "lines": lines,
    }


def validate_bank_entry(row: dict) -> dict:
    amount = money(required(row, "amount"), "amount")
    if amount == 0:
        raise RowError("Un movimiento bancario no puede ser de 0")
    return {
        "kind": "bank_entry",
        "bank_account": required(row, "bank_account"),
        "external_entry_id": required(row, "entry_id"),
        "booked_on": day(required(row, "booked_on"), "booked_on"),
        "amount": amount,
        "currency": brl(row),
        "description": required(row, "description"),
        "reference": optional(row, "reference"),
    }


VALIDATORS = {
    "store_orders": validate_order,
    "store_order_items": validate_order_item,
    "gateway_events": validate_gateway,
    "bank_statement": validate_bank_entry,
}