"""Cómo se muestran montos, fechas y estados en pantalla."""

from datetime import date, datetime
from decimal import Decimal

MONTHS = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]

# tipo de caso -> (texto, tono). El tono define el color; el texto siempre acompaña al color.
CASE_LABELS = {
    "overdue_deposit":      ("Depósito vencido", "act"),
    "amount_mismatch":      ("Diferencia de monto", "act"),
    "duplicate_charge":     ("Cobro duplicado", "act"),
    "data_conflict":        ("Conflicto de datos", "act"),
    "ambiguous_match":      ("Coincidencia ambigua", "review"),
    "unmatched_bank_entry": ("Depósito sin pareja", "review"),
    "partial_payment":      ("Pago parcial", "review"),
    "charge_without_order": ("Cobro sin pedido", "review"),
    "in_transit":           ("En tránsito", "watch"),
}

LEVEL_LABELS = {
    "order_gateway": "Pedido → pasarela",
    "gateway_bank": "Pasarela → banco",
}


def brl(value, signed: bool = False) -> str:
    """1250.5 -> 'R$ 1.250,50' (formato brasileño, con signo menos tipográfico)."""
    if value is None:
        return "—"
    amount = Decimal(value)
    digits = f"{abs(amount):,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")
    sign = "−" if amount < 0 else ("+" if signed and amount > 0 else "")
    return f"{sign}R$ {digits}"


def short_date(value) -> str:
    """date(2026, 11, 18) -> '18 nov 2026'; con datetime agrega la hora."""
    if value is None:
        return "—"
    text = f"{value.day} {MONTHS[value.month - 1]} {value.year}"
    if isinstance(value, datetime):
        text += f" · {value:%H:%M}"
    return text