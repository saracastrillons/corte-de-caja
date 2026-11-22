"""Mesa de conciliación (FastAPI + Jinja).

Uso:  uvicorn cortecaja.web.main:app --reload
"""

import json
import math
from datetime import date
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from psycopg.rows import dict_row

from cortecaja.db import connect
from cortecaja.web import queries
from cortecaja.web.format import (CASE_HELP, CASE_LABELS, CLOSE_STATUS, LEVEL_LABELS, LINE_LABELS,
                                  RESULT_LABELS, brl, days_label, gap_sentence, humanize, number,
                                  pct, short_date)

HERE = Path(__file__).parent

app = FastAPI(title="Corte de Caja")
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")


def desk_url(view: str = "open", type: str | None = None, q: str | None = None,
             page: int = 1, case: int | None = None) -> str:
    """Arma el enlace de la mesa conservando filtros, búsqueda y página."""
    params = {"view": view}
    if type:
        params["type"] = type
    if q:
        params["q"] = q
    if page > 1:
        params["page"] = page
    if case:
        params["case"] = case
    return "/?" + urlencode(params) + "#bandeja"


templates = Jinja2Templates(directory=HERE / "templates")
templates.env.filters.update(brl=brl, short_date=short_date, number=number, days_label=days_label,
                             humanize=humanize)
templates.env.globals.update(
    CASE_LABELS=CASE_LABELS, CASE_HELP=CASE_HELP, LEVEL_LABELS=LEVEL_LABELS,
    RESULT_LABELS=RESULT_LABELS, LINE_LABELS=LINE_LABELS, CLOSE_STATUS=CLOSE_STATUS,
    desk_url=desk_url, pct=pct,
)



def get_conn():
    """Una conexión por petición, con las fechas en hora de São Paulo."""
    with connect(autocommit=True, row_factory=dict_row) as conn:
        conn.execute("SET TIME ZONE 'America/Sao_Paulo'")
        yield conn


# ---------------------------------------------------------------------------
# La mesa
# ---------------------------------------------------------------------------

def build_evidence(conn, case: dict, day: date) -> dict:
    """Arma los dos registros que se enfrentan en la ficha, según el tipo de caso."""
    key = case["entity_key"]
    evidence = {"gap": gap_sentence(case, day), "history": queries.case_history(conn, case["case_id"])}

    if case["case_type"] == "data_conflict":
        seen = queries.observations(conn, key)
        evidence.update(kind="conflict", first=seen[:1], later=seen[1:])
    elif case["entity_type"] == "settlement":
        settlement = queries.settlement_record(conn, key)
        deposits = queries.linked_deposits(conn, key)
        candidates = [] if deposits else queries.candidate_deposits(conn, day, settlement["reported_net"])
        evidence.update(kind="settlement", settlement=settlement, deposits=deposits, candidates=candidates,
                        received=sum((d["allocated_amount"] for d in deposits), Decimal("0.00")))
    elif case["entity_type"] == "bank_entry":
        evidence.update(kind="bank_entry", deposit=queries.bank_entry(conn, key))
    else:
        payments = queries.payment_events(conn, key)
        evidence.update(kind="order", order=queries.order_record(conn, key), payments=payments,
                        charged=sum((p["amount"] for p in payments if p["event_type"] == "charge"),
                                    Decimal("0.00")))
    return evidence


@app.get("/", response_class=HTMLResponse)
def desk(request: Request, view: str = "open", type: str | None = None, q: str | None = None,
         page: int = 1, case: int | None = None, conn=Depends(get_conn)):
    day = queries.operating_day(conn)
    status = "resolved" if view == "resolved" else "open"
    q = (q or "").strip() or None
    type = type if type in CASE_LABELS else None

    rows, total = queries.cases(conn, day, status, type, q, max(page, 1))
    selected = queries.case_detail(conn, case, day) if case else None
    return templates.TemplateResponse(request, "desk.html", {
        "day": day,
        "view": status,
        "filter_type": type,
        "q": q,
        "page": max(page, 1),
        "pages": max(math.ceil(total / queries.PAGE_SIZE), 1),
        "page_size": queries.PAGE_SIZE,
        "total": total,
        "totals": queries.day_totals(conn, day),
        "flow": queries.flow_summary(conn, day),
        "last_close": queries.last_close(conn, day),
        "counts": queries.case_counts(conn),
        "type_counts": queries.case_type_counts(conn, status),
        "cases": rows,
        "selected": selected,
        "evidence": build_evidence(conn, selected, day) if selected else None,
    })


@app.get("/source/{batch_id}/{row_number}", response_class=HTMLResponse)
def source(request: Request, batch_id: int, row_number: int, conn=Depends(get_conn)):
    """La fila exacta del archivo de origen: el final del rastro de cualquier cifra."""
    row = queries.source_row(conn, batch_id, row_number)
    if row is None:
        raise HTTPException(status_code=404, detail="Esa fila no existe")
    pretty = json.dumps(row["payload"], indent=2, ensure_ascii=False)
    return templates.TemplateResponse(request, "source.html", {"row": row, "pretty": pretty})


# ---------------------------------------------------------------------------
# Cierres
# ---------------------------------------------------------------------------

@app.get("/closes", response_class=HTMLResponse)
def closes(request: Request, conn=Depends(get_conn)):
    return templates.TemplateResponse(request, "closes.html", {"closes": queries.closes(conn)})


@app.get("/closes/{close_id}", response_class=HTMLResponse)
def close_detail(request: Request, close_id: int, only_changes: bool = False, conn=Depends(get_conn)):
    close = queries.close_run(conn, close_id)
    if close is None:
        raise HTTPException(status_code=404, detail="Ese cierre no existe")

    today = queries.operating_day(conn)
    rows = queries.close_comparison(conn, close, today)

    # Totales de las MISMAS liquidaciones, tal como se publicaron y como están hoy.
    due = [r for r in rows if r["item_type"] == "settlement" and r["then"]["status"] != "in_transit"]
    expected = sum((r["then"]["expected"] for r in due), Decimal("0.00"))
    received_now = sum((r["now"]["received"] for r in due if r["now"]), Decimal("0.00"))
    now_totals = {"expected": expected, "received": received_now, "pending": expected - received_now}

    return templates.TemplateResponse(request, "close_detail.html", {
        "close": close,
        "today": today,
        "rows": [r for r in rows if r["changed"]] if only_changes else rows,
        "changed_count": sum(r["changed"] for r in rows),
        "total_count": len(rows),
        "only_changes": only_changes,
        "now_totals": now_totals,
    })