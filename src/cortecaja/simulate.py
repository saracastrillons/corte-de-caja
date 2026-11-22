"""Simulador de volumen: convierte los pagos reales de Olist en archivos de pasarela y banco.

Uso:
    python -m cortecaja.simulate                # escribe en data/generated/olist/
    python -m cortecaja.simulate --seed 7       # otra semilla, otros errores inyectados

Lo que viene de Olist (real): pedidos, artículos, pagos (medio, importe, cuotas).
Lo que se simula (con semilla fija): comisiones, liquidaciones, reembolsos parciales,
depósitos bancarios y los errores inyectados. Cada error queda anotado en truth.csv
para poder comprobar después si el sistema lo clasificó bien.
"""

import argparse
import csv
import json
import random
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OLIST_DIR = ROOT / "data" / "olist"
OUT_DIR = ROOT / "data" / "generated" / "olist"

PROVIDER = "simpay"
BANK_ACCOUNT = "CC-0001"
CENT = Decimal("0.01")

# Lo que cobra la pasarela y cuándo paga, por medio de pago (simulado).
FEE_RATE = {"credit_card": Decimal("0.0349"), "debit_card": Decimal("0.0199"), "boleto": Decimal("0.0150")}
PAYOUT_DAYS = {"credit_card": 2, "debit_card": 1, "boleto": 1}
METHOD_CODE = {"credit_card": "CC", "debit_card": "DC", "boleto": "BL"}

# Errores inyectados en el banco (proporción de liquidaciones) y en la pasarela (de eventos).
P_LATE, P_SHORT, P_NO_REFERENCE, P_NEVER = 0.03, 0.01, 0.01, 0.005
P_REFUND, P_DUPLICATE_EVENT, P_CONFLICT_EVENT = 0.015, 0.003, 0.0005
P_UNKNOWN_DEPOSIT_PER_DAY = 0.05


def money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass
class Event:
    event_id: str
    order_ref: str
    event_type: str
    method: str
    amount: Decimal
    occurred_at: datetime

    def to_json(self) -> dict:
        return {"record_type": "payment_event", "provider": PROVIDER, "event_id": self.event_id,
                "order_ref": self.order_ref, "event_type": self.event_type,
                "payment_method": self.method, "amount": str(self.amount), "currency": "BRL",
                "occurred_at": self.occurred_at.isoformat()}


def read_olist() -> tuple[dict, dict]:
    """Devuelve la hora de compra de cada pedido y sus pagos, tal como vienen en Olist."""
    purchased = {}
    with open(OLIST_DIR / "olist_orders_dataset.csv", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            purchased[row["order_id"]] = datetime.fromisoformat(row["order_purchase_timestamp"] + "-03:00")
    payments = defaultdict(list)
    with open(OLIST_DIR / "olist_order_payments_dataset.csv", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            payments[row["order_id"]].append(row)
    return purchased, payments


def build_events(purchased: dict, payments: dict, rng: random.Random) -> list[Event]:
    events = []
    for order_id in sorted(payments):
        if order_id not in purchased:
            continue
        for row in sorted(payments[order_id], key=lambda r: int(r["payment_sequential"])):
            seq = int(row["payment_sequential"])
            charge = Event(f"PAY-{order_id}-{seq}", order_id, "charge", row["payment_type"],
                           Decimal(row["payment_value"]), purchased[order_id] + timedelta(minutes=seq))
            events.append(charge)
            # Reembolso parcial simulado, al día siguiente, solo en tarjeta.
            if charge.method == "credit_card" and charge.amount > 20 and rng.random() < P_REFUND:
                share = Decimal(rng.randint(10, 50)) / 100
                events.append(Event(f"REF-{order_id}-{seq}", order_id, "refund", charge.method,
                                    money(charge.amount * share),
                                    charge.occurred_at.replace(hour=10, minute=0, second=0) + timedelta(days=1)))
    return events


def settleable(event: Event) -> bool:
    """La pasarela solo liquida medios que mueven dinero y eventos con importe."""
    return event.method in FEE_RATE and event.amount > 0


def build_settlements(events: list[Event]) -> list[dict]:
    """Una liquidación por día y medio de pago, con sus cobros, reembolsos y comisión."""
    groups = defaultdict(list)
    for event in events:
        if settleable(event):
            groups[(event.occurred_at.date(), event.method)].append(event)

    settlements = []
    for (day, method), group in sorted(groups.items()):
        lines, number = [], 0
        for event in sorted(group, key=lambda e: (e.occurred_at, e.event_id)):
            number += 1
            sign = 1 if event.event_type == "charge" else -1
            lines.append({"line": number, "type": event.event_type, "event_id": event.event_id,
                          "amount": str(sign * event.amount)})
        charges = sum((e.amount for e in group if e.event_type == "charge"), Decimal("0"))
        fee = money(charges * FEE_RATE[method])
        if fee > 0:
            lines.append({"line": number + 1, "type": "fee", "event_id": None, "amount": str(-fee)})
        net = sum((Decimal(line["amount"]) for line in lines), Decimal("0"))
        if net <= 0:
            continue
        settlements.append({
            "record_type": "settlement", "provider": PROVIDER,
            "settlement_id": f"STL-{day:%Y%m%d}-{METHOD_CODE[method]}",
            "settled_at": datetime.combine(day, datetime.min.time()).replace(hour=23).isoformat() + "-03:00",
            "expected_payout_on": (day + timedelta(days=PAYOUT_DAYS[method])).isoformat(),
            "net_amount": str(net), "currency": "BRL", "lines": lines,
            "_day": day,
        })
    return settlements


def build_bank(settlements: list[dict], last_day: date, rng: random.Random) -> tuple[dict, list[dict]]:
    """Depósitos por día y la verdad de lo que se inyectó en cada liquidación."""
    by_day = defaultdict(list)
    truth = []
    for s in settlements:
        due = date.fromisoformat(s["expected_payout_on"])
        net = Decimal(s["net_amount"])
        roll = rng.random()
        if roll < P_NEVER:
            kind, day, amount, reference = "never", None, None, s["settlement_id"]
        elif roll < P_NEVER + P_LATE:
            kind, day, amount, reference = "late", due + timedelta(days=rng.randint(1, 3)), net, s["settlement_id"]
        elif roll < P_NEVER + P_LATE + P_SHORT:
            short = money(Decimal(rng.uniform(0.5, 5.0)))
            kind, day, amount, reference = "short", due, net - short, s["settlement_id"]
        elif roll < P_NEVER + P_LATE + P_SHORT + P_NO_REFERENCE:
            kind, day, amount, reference = "no_reference", due, net, None
        else:
            kind, day, amount, reference = "ok", due, net, s["settlement_id"]

        if day is not None and day <= last_day:
            by_day[day].append({"amount": amount, "reference": reference,
                                "description": "SIMPAY LIQUIDACAO" if reference else "PIX RECEBIDO"})
        truth.append({"settlement_id": s["settlement_id"], "injected": kind,
                      "expected_payout_on": s["expected_payout_on"],
                      "deposit_day": day.isoformat() if day else "",
                      "deposit_amount": str(amount) if amount is not None else ""})

    # Depósitos que no son de la pasarela: nadie debería conciliarlos.
    day = min(by_day)
    while day <= last_day:
        if rng.random() < P_UNKNOWN_DEPOSIT_PER_DAY:
            by_day[day].append({"amount": money(Decimal(rng.uniform(50, 900))), "reference": None,
                                "description": "TED RECEBIDA"})
        day += timedelta(days=1)
    return by_day, truth


def write_outputs(events, settlements, bank_by_day, truth, rng: random.Random) -> dict:
    gateway_dir, bank_dir = OUT_DIR / "gateway", OUT_DIR / "bank"
    gateway_dir.mkdir(parents=True, exist_ok=True)
    bank_dir.mkdir(parents=True, exist_ok=True)

    gateway_by_day = defaultdict(list)
    for event in events:
        gateway_by_day[event.occurred_at.date()].append(event.to_json())
        # Reenvíos de la pasarela al día siguiente: idénticos o con otro importe.
        roll = rng.random()
        if roll < P_CONFLICT_EVENT:
            changed = event.to_json() | {"amount": str(event.amount + 1)}
            gateway_by_day[event.occurred_at.date() + timedelta(days=1)].append(changed)
        elif roll < P_CONFLICT_EVENT + P_DUPLICATE_EVENT:
            gateway_by_day[event.occurred_at.date() + timedelta(days=1)].append(event.to_json())
    for s in settlements:
        gateway_by_day[s["_day"]].append({k: v for k, v in s.items() if not k.startswith("_")})

    for day, records in sorted(gateway_by_day.items()):
        with open(gateway_dir / f"gateway_{day:%Y-%m-%d}.jsonl", "w", encoding="utf-8", newline="\n") as f:
            for record in records:            # eventos primero, liquidaciones al final
                f.write(json.dumps(record, ensure_ascii=False) + "\n")

    for day, deposits in sorted(bank_by_day.items()):
        with open(bank_dir / f"bank_{day:%Y-%m-%d}.csv", "w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["bank_account", "entry_id", "booked_on", "amount", "currency",
                             "description", "reference"])
            for n, d in enumerate(deposits, start=1):
                writer.writerow([BANK_ACCOUNT, f"BNK-{day:%Y%m%d}-{n:04d}", day.isoformat(),
                                 str(d["amount"]), "BRL", d["description"], d["reference"] or ""])

    with open(OUT_DIR / "truth.csv", "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(truth[0]))
        writer.writeheader()
        writer.writerows(truth)

    return {"gateway_files": len(gateway_by_day), "bank_files": len(bank_by_day),
            "events": len(events), "settlements": len(settlements),
            "deposits": sum(len(d) for d in bank_by_day.values())}


def main() -> None:
    parser = argparse.ArgumentParser(description="Genera archivos de pasarela y banco a partir de Olist.")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    rng = random.Random(args.seed)

    purchased, payments = read_olist()
    events = build_events(purchased, payments, rng)
    settlements = build_settlements(events)
    last_day = max(e.occurred_at.date() for e in events)
    bank_by_day, truth = build_bank(settlements, last_day, rng)
    stats = write_outputs(events, settlements, bank_by_day, truth, rng)

    print(f"Semilla {args.seed} · salida en {OUT_DIR.relative_to(ROOT)}")
    for name, value in stats.items():
        print(f"  {name}: {value:,}")


if __name__ == "__main__":
    main()