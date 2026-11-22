"""Escenario de volumen: Olist completo + pasarela y banco simulados, con tiempos medidos.

Uso:
    python -m cortecaja.simulate          # primero, generar los archivos
    python -m cortecaja.volume            # luego, cargarlo todo en una base aparte

Usa su PROPIA base (cortecaja_volume): tu base de la demo no se toca.
Al final imprime un resumen y lo guarda en reports/volume_<reglas>.json.
"""

import argparse
import csv
import json
import statistics
import time
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg
from psycopg.conninfo import make_conninfo

from cortecaja.close import RULES_VERSION, run_close
from cortecaja.db import database_url
from cortecaja.ingest import ingest_file
from cortecaja.migrate import migrate

ROOT = Path(__file__).resolve().parents[2]
OLIST_DIR = ROOT / "data" / "olist"
GENERATED = ROOT / "data" / "generated" / "olist"
REPORTS = ROOT / "reports"
SAO_PAULO = ZoneInfo("America/Sao_Paulo")

# Qué estado final esperamos según lo que el simulador inyectó en cada liquidación.
EXPECTED = {
    "ok": {"reconciled"},
    "late": {"reconciled"},
    "short": {"mismatch"},
    "no_reference": {"suggested", "ambiguous"},
    "never": {"overdue", "in_transit"},
}


def at(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=SAO_PAULO)


def day_of(path: Path) -> date:
    """gateway_2017-10-02.jsonl -> date(2017, 10, 2)"""
    return date.fromisoformat(path.stem.split("_")[1])


def fresh_database(name: str) -> str:
    with psycopg.connect(database_url(), autocommit=True) as admin:
        admin.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
        admin.execute(f"CREATE DATABASE {name}")
    return make_conninfo(database_url(), dbname=name)


def timed(fn, *args):
    start = time.perf_counter()
    result = fn(*args)
    return result, time.perf_counter() - start


def load_everything(conn) -> dict:
    gateway = {day_of(p): p for p in (GENERATED / "gateway").glob("*.jsonl")}
    bank = {day_of(p): p for p in (GENERATED / "bank").glob("*.csv")}
    first_day = min(gateway)

    # 1) Datos maestros de la tienda: los archivos de Olist tal cual.
    _, t_orders = timed(ingest_file, conn, "store_orders",
                        OLIST_DIR / "olist_orders_dataset.csv", at(first_day, 7))
    _, t_items = timed(ingest_file, conn, "store_order_items",
                       OLIST_DIR / "olist_order_items_dataset.csv", at(first_day, 7))
    print(f"  pedidos {t_orders:.1f} s · artículos {t_items:.1f} s")

    # 2) Un lote por archivo diario, en el orden en que habrían llegado.
    per_file = []
    days = sorted(set(gateway) | set(bank))
    for n, day in enumerate(days, start=1):
        if day in gateway:
            _, seconds = timed(ingest_file, conn, "gateway_events", gateway[day], at(day, 8, 5))
            per_file.append(seconds)
        if day in bank:
            _, seconds = timed(ingest_file, conn, "bank_statement", bank[day], at(day, 18))
            per_file.append(seconds)
        if n % 100 == 0:
            print(f"  {n}/{len(days)} días cargados")

    close_day = max(d for d in days if d in gateway and d in bank)
    return {"orders_seconds": t_orders, "items_seconds": t_items, "per_file": per_file,
            "days": len(days), "close_day": close_day, "bank_files": bank}


def summarize(conn, close_day: date) -> dict:
    q = lambda sql, *p: conn.execute(sql, p).fetchall()

    rows = dict(q("""SELECT b.source, COUNT(*) FROM raw.source_record r
                     JOIN raw.ingestion_batch b USING (batch_id) GROUP BY b.source"""))
    rejected = q("""SELECT regexp_replace(reject_reason, '(PAY|REF)-[0-9a-f]+-[0-9]+|[0-9.]+', '#', 'g') AS reason,
                           COUNT(*)
                    FROM raw.source_record WHERE NOT is_valid GROUP BY 1 ORDER BY 2 DESC""")
    batches = dict(q("SELECT status, COUNT(*) FROM raw.ingestion_batch GROUP BY status"))
    cases = q("""SELECT case_type, COUNT(*), SUM(ABS(difference))
                 FROM ops.reconciliation_case WHERE status = 'open'
                 GROUP BY case_type ORDER BY 2 DESC""")
    order_status = dict(q("SELECT status, COUNT(*) FROM recon.order_gateway() GROUP BY status"))
    settlement_status = {key.split("/", 1)[1]: status for key, status in q(
        """SELECT item_key, status FROM recon.gateway_bank(%s) WHERE item_type = 'settlement'""", close_day)}

    # Comparación con la verdad que dejó el simulador.
    with open(GENERATED / "truth.csv", encoding="utf-8", newline="") as f:
        truth = list(csv.DictReader(f))
    agreement, confusion = Counter(), Counter()
    false_reconciled = 0
    for t in truth:
        got = settlement_status.get(t["settlement_id"], "missing")
        expected = EXPECTED[t["injected"]]
        if t["injected"] == "late" and t["deposit_day"] > close_day.isoformat():
            expected = {"overdue", "in_transit"}
        agreement["ok" if got in expected else "different"] += 1
        confusion[f"{t['injected']} → {got}"] += 1
        if got == "reconciled" and t["injected"] in ("short", "never", "no_reference"):
            false_reconciled += 1

    auto = sum(1 for s in settlement_status.values() if s == "reconciled")
    return {
        "rows_by_source": rows,
        "batches_by_status": batches,
        "rejected_rows": [{"reason": r, "rows": n} for r, n in rejected],
        "open_cases": [{"case_type": t, "cases": n, "amount": str(a)} for t, n, a in cases],
        "orders_by_status": order_status,
        "settlements": len(settlement_status),
        "settlements_auto_reconciled": auto,
        "auto_reconciled_pct": round(100 * auto / max(len(settlement_status), 1), 2),
        "truth_agreement": dict(agreement),
        "truth_confusion": dict(sorted(confusion.items())),
        "false_reconciliations": false_reconciled,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Carga el escenario de volumen y mide tiempos.")
    parser.add_argument("--database", default="cortecaja_volume")
    args = parser.parse_args()

    url = fresh_database(args.database)
    with psycopg.connect(url, autocommit=True) as conn:
        migrate(conn)
        print(f"Base {args.database} creada · reglas {RULES_VERSION}")

        loaded, t_load = timed(load_everything, conn)
        close_day = loaded["close_day"]

        # Reenviar el último extracto: no debe cambiar nada.
        resend = ingest_file(conn, "bank_statement", loaded["bank_files"][close_day], at(close_day, 19))

        close, t_close = timed(run_close, conn, close_day, at(close_day, 23, 59), True)
        summary = summarize(conn, close_day)

    per_file = sorted(loaded["per_file"])
    report = {
        "rules_version": RULES_VERSION,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "close_day": close_day.isoformat(),
        "days": loaded["days"],
        "timings_seconds": {
            "orders_file": round(loaded["orders_seconds"], 2),
            "items_file": round(loaded["items_seconds"], 2),
            "daily_files": len(per_file),
            "daily_file_median": round(statistics.median(per_file), 3),
            "daily_file_p95": round(per_file[int(len(per_file) * 0.95) - 1], 3),
            "full_load": round(t_load, 1),
            "full_period_close": round(t_close, 1),
        },
        "resend_status": resend.status,
        "close": {"status": close["status"], "items": close["items"], "cases": close["cases"],
                  "totals": {k: str(v) for k, v in close["totals"].items()}},
        **summary,
    }

    REPORTS.mkdir(exist_ok=True)
    out = REPORTS / f"volume_{RULES_VERSION}.json"
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    t = report["timings_seconds"]
    print(f"\nCarga completa: {t['full_load']} s · {t['daily_files']} archivos diarios "
          f"(mediana {t['daily_file_median']} s, p95 {t['daily_file_p95']} s)")
    print(f"Cierre del periodo completo ({close['items']:,} partidas): {t['full_period_close']} s")
    print(f"Reenvío del último extracto: {resend.status}")
    print(f"Liquidaciones conciliadas solas: {report['settlements_auto_reconciled']:,} de "
          f"{report['settlements']:,} ({report['auto_reconciled_pct']} %)")
    print(f"Coincidencia con la verdad: {report['truth_agreement']} · "
          f"falsas conciliaciones: {report['false_reconciliations']}")
    print("Casos abiertos:")
    for c in report["open_cases"]:
        print(f"  {c['case_type']:<22} {c['cases']:>6,}   R$ {c['amount']}")
    print("Filas rechazadas:")
    for r in report["rejected_rows"]:
        print(f"  {r['rows']:>6,}  {r['reason']}")
    print(f"\nReporte: {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()