"""Mide consultas reales de la app con EXPLAIN (ANALYZE, BUFFERS).

Uso:
    python -m cortecaja.explain --label antes
    python -m cortecaja.explain --label despues

Corre cada consulta varias veces sobre la base de volumen y guarda la mediana,
el tipo de lectura del plan y los bloques leídos en reports/explain_<label>.json.
"""

import argparse
import json
import statistics
from pathlib import Path

import psycopg
from psycopg.conninfo import make_conninfo

from cortecaja.db import database_url

ROOT = Path(__file__).resolve().parents[2]
REPORTS = ROOT / "reports"
RUNS = 7

QUERIES = {
    # La ficha de un conflicto busca todas las veces que llegó un evento (web/queries.observations).
    "observaciones_de_un_evento": """
        SELECT r.batch_id, r.row_number, r.payload, b.file_name, b.received_at
        FROM raw.source_record r
        JOIN raw.ingestion_batch b ON b.batch_id = r.batch_id
        WHERE b.source = 'gateway_events'
          AND r.payload->>'provider' = %(provider)s AND r.payload->>'event_id' = %(event_id)s
        ORDER BY b.received_at, r.batch_id
    """,
    # La bandeja: los 50 casos abiertos con más dinero involucrado (web/queries.cases + paginación).
    "bandeja_top_50": """
        SELECT case_id, case_type, entity_key, difference
        FROM ops.reconciliation_case
        WHERE status = 'open'
        ORDER BY ABS(difference) DESC, case_id
        LIMIT 50
    """,
}


def walk(node: dict):
    yield node
    for child in node.get("Plans", []):
        yield from walk(child)


def measure(conn, sql: str, params: dict) -> dict:
    times, plan = [], None
    for _ in range(RUNS):
        result = conn.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql, params).fetchone()[0][0]
        times.append(result["Execution Time"])
        plan = result["Plan"]
    nodes = list(walk(plan))
    text = conn.execute("EXPLAIN (ANALYZE, BUFFERS, COSTS OFF, TIMING OFF) " + sql, params).fetchall()
    return {
        "median_ms": round(statistics.median(times), 3),
        "min_ms": round(min(times), 3),
        "runs": RUNS,
        "scans": sorted({f"{n['Node Type']} on {n['Relation Name']}" + (f" using {n['Index Name']}" if "Index Name" in n else "")
                         for n in nodes if "Relation Name" in n}),
        "shared_hit_blocks": plan.get("Shared Hit Blocks", 0),
        "shared_read_blocks": plan.get("Shared Read Blocks", 0),
        "plan": [line for (line,) in text],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Mide consultas con EXPLAIN (ANALYZE, BUFFERS).")
    parser.add_argument("--label", required=True)
    parser.add_argument("--database", default="cortecaja_volume")
    args = parser.parse_args()

    with psycopg.connect(make_conninfo(database_url(), dbname=args.database), autocommit=True) as conn:
        conn.execute("ANALYZE")                         # estadísticas al día para el planificador
        sizes = dict(conn.execute(
            """SELECT 'raw.source_record', COUNT(*) FROM raw.source_record
               UNION ALL SELECT 'ops.reconciliation_case (open)', COUNT(*)
                         FROM ops.reconciliation_case WHERE status = 'open'"""
        ).fetchall())
        # Un evento con conflicto real del escenario: es el caso que abre esa ficha.
        provider, event_id = conn.execute(
            """SELECT split_part(entity_key, '/', 1), split_part(entity_key, '/', 2)
               FROM ops.reconciliation_case WHERE case_type = 'data_conflict'
               ORDER BY case_id LIMIT 1"""
        ).fetchone()
        params = {"provider": provider, "event_id": event_id}

        results = {name: measure(conn, sql, params) for name, sql in QUERIES.items()}

    report = {"label": args.label, "table_sizes": sizes, "params": params, "queries": results}
    REPORTS.mkdir(exist_ok=True)
    out = REPORTS / f"explain_{args.label}.json"
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"Tamaños: {sizes}")
    for name, r in results.items():
        print(f"\n{name}: mediana {r['median_ms']} ms (mín {r['min_ms']} ms, {r['runs']} corridas)")
        print(f"  lecturas: {'; '.join(r['scans'])}")
        print(f"  bloques en memoria: {r['shared_hit_blocks']:,} · leídos de disco: {r['shared_read_blocks']:,}")
    print(f"\nReporte: {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()