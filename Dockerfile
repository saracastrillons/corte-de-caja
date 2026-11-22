FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Instalación editable: el código busca sql/ y data/demo/ junto a src/.
COPY pyproject.toml ./
COPY src ./src
RUN pip install -e .

COPY sql ./sql
COPY data/demo ./data/demo

EXPOSE 8000

# Al arrancar: migra, carga la demo si la base está vacía y enciende la mesa.
CMD ["sh", "-c", "python -m cortecaja.demo && uvicorn cortecaja.web.main:app --host 0.0.0.0 --port 8000"]