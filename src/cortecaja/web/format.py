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

# estado de una partida en un cierre -> (texto, tono)
RESULT_LABELS = {
    "reconciled": ("Conciliado", "done"),
    "in_transit": ("En tránsito", "watch"),
    "overdue":    ("Vencido", "act"),
    "mismatch":   ("Con diferencia", "act"),
    "unmatched":  ("Sin pareja", "review"),
    "in_review":  ("En revisión", "review"),
}

LINE_LABELS = {"charge": "Cobro", "refund": "Reembolso", "fee": "Comisión"}

CLOSE_STATUS = {
    "published": "publicado", "superseded": "reemplazado",
    "incomplete": "incompleto", "draft": "borrador",
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


def gap_sentence(case: dict, day: date) -> str:
    """La frase que explica la diferencia, en el idioma de quien revisa el cierre."""
    if case["status"] == "resolved":
        return f"Conciliado el {short_date(case['resolved_at'])}"
    diff = case["difference"] or Decimal("0")
    kind = case["case_type"]
    if kind == "in_transit":
        return f"Esperado para el {short_date(case['due_on'])}"
    if kind == "overdue_deposit":
        late = (day - case["due_on"]).days
        return f"Vencido desde el {short_date(case['due_on'])} · {late} día{'s' if late != 1 else ''}"
    if kind == "amount_mismatch":
        side = "por debajo" if diff < 0 else "por encima"
        return f"Recibido {brl(abs(diff))} {side}"
    if kind == "ambiguous_match":
        return "Varios depósitos coinciden en monto y fecha"
    if kind == "unmatched_bank_entry":
        return "Depósito sin liquidación asociada"
    if kind == "duplicate_charge":
        return f"Cobrado {brl(diff)} de más"
    if kind == "partial_payment":
        return f"Faltan {brl(abs(diff))} por cobrar"
    if kind == "charge_without_order":
        return "No existe un pedido con esa referencia"
    if kind == "data_conflict":
        return f"Dos versiones del mismo evento difieren en {brl(abs(diff))}"
    return ""