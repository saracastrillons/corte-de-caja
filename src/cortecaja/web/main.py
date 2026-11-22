"""Mesa de conciliación (FastAPI + Jinja).

Uso:  uvicorn cortecaja.web.main:app --reload
"""

from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from psycopg.rows import dict_row

from cortecaja.db import connect
from cortecaja.web import queries
from cortecaja.web.format import CASE_LABELS, LEVEL_LABELS, brl, short_date

HERE = Path(__file__).parent

app = FastAPI(title="Corte de Caja")
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

templates = Jinja2Templates(directory=HERE / "templates")
templates.env.filters["brl"] = brl
templates.env.filters["short_date"] = short_date
templates.env.globals["CASE_LABELS"] = CASE_LABELS
templates.env.globals["LEVEL_LABELS"] = LEVEL_LABELS


def get_conn():
    """Una conexión por petición, con las fechas en hora de São Paulo."""
    with connect(autocommit=True, row_factory=dict_row) as conn:
        conn.execute("SET TIME ZONE 'America/Sao_Paulo'")
        yield conn


@app.get("/", response_class=HTMLResponse)
def desk(request: Request, view: str = "open", conn=Depends(get_conn)):
    day = queries.operating_day(conn)
    status = "resolved" if view == "resolved" else "open"
    return templates.TemplateResponse(request, "desk.html", {
        "day": day,
        "view": status,
        "totals": queries.day_totals(conn, day),
        "last_close": queries.last_close(conn, day),
        "counts": queries.case_counts(conn),
        "cases": queries.cases(conn, day, status),
    })