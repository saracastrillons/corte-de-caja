-- 001: esquemas y zona de recepción (raw)


CREATE SCHEMA IF NOT EXISTS raw;
CREATE SCHEMA IF NOT EXISTS core;
CREATE SCHEMA IF NOT EXISTS ops;

-- Una fila = un archivo recibido (o un intento de recibirlo)
CREATE TABLE raw.ingestion_batch (
    batch_id       BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source         TEXT        NOT NULL
                   CHECK (source IN ('store_orders', 'store_order_items',
                                     'gateway_events', 'bank_statement')),
    file_name      TEXT        NOT NULL,
    file_sha256    CHAR(64)    NOT NULL,
    received_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    rows_received  INTEGER     NOT NULL DEFAULT 0 CHECK (rows_received >= 0),
    rows_rejected  INTEGER     NOT NULL DEFAULT 0 CHECK (rows_rejected >= 0),
    status         TEXT        NOT NULL DEFAULT 'received'
                   CHECK (status IN ('received', 'loaded', 'failed', 'duplicate_file')),
    error_message  TEXT
);

-- El mismo archivo (misma huella) solo puede quedar cargado una vez por fuente.
-- Los reenvíos se registran como 'duplicate_file' para dejar evidencia.
CREATE UNIQUE INDEX ux_batch_file_once
    ON raw.ingestion_batch (source, file_sha256)
    WHERE status IN ('received', 'loaded');

-- Una fila = un registro de un archivo, guardado tal como llegó
CREATE TABLE raw.source_record (
    batch_id       BIGINT  NOT NULL REFERENCES raw.ingestion_batch (batch_id),
    row_number     INTEGER NOT NULL CHECK (row_number >= 1),
    payload        JSONB   NOT NULL,
    is_valid       BOOLEAN NOT NULL,
    reject_reason  TEXT,
    PRIMARY KEY (batch_id, row_number),
    CHECK (is_valid OR reject_reason IS NOT NULL)
);

