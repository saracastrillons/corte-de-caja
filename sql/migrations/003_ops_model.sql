-- 003: operación (ops) — casos, historial y cierres
BEGIN;

-- 1) CASOS — una fila = una diferencia que alguien debe revisar
CREATE TABLE ops.reconciliation_case (
    case_id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    level            TEXT          NOT NULL CHECK (level IN ('order_gateway', 'gateway_bank')),
    case_type        TEXT          NOT NULL CHECK (case_type IN (
                         'in_transit', 'overdue_deposit', 'amount_mismatch',
                         'unmatched_bank_entry', 'ambiguous_match', 'duplicate_charge',
                         'partial_payment', 'charge_without_order', 'data_conflict')),
    entity_type      TEXT          NOT NULL CHECK (entity_type IN ('order', 'settlement', 'bank_entry', 'payment_event')),
    entity_key       TEXT          NOT NULL,
    expected_amount  NUMERIC(14,2),
    received_amount  NUMERIC(14,2),
    difference       NUMERIC(14,2) GENERATED ALWAYS AS
                         (COALESCE(received_amount, 0) - COALESCE(expected_amount, 0)) STORED,
    status           TEXT          NOT NULL DEFAULT 'open'
                     CHECK (status IN ('open', 'resolved', 'dismissed')),
    due_on           DATE,
    next_action      TEXT          NOT NULL,
    opened_at        TIMESTAMPTZ   NOT NULL DEFAULT now(),
    resolved_at      TIMESTAMPTZ,
    CHECK ((status = 'open') = (resolved_at IS NULL))
);

-- Reprocesar un archivo nunca puede abrir el mismo caso dos veces
CREATE UNIQUE INDEX ux_case_open_once
    ON ops.reconciliation_case (case_type, entity_type, entity_key)
    WHERE status = 'open';

-- 2) HISTORIAL — una fila = algo que le pasó a un caso (solo se agrega, nunca se edita)
CREATE TABLE ops.case_event (
    event_id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    case_id             BIGINT      NOT NULL REFERENCES ops.reconciliation_case (case_id),
    event_type          TEXT        NOT NULL CHECK (event_type IN (
                            'opened', 'amount_updated', 'status_changed', 'note', 'resolved', 'reopened')),
    from_status         TEXT,
    to_status           TEXT,
    note                TEXT,
    actor               TEXT        NOT NULL,
    caused_by_batch_id  BIGINT      REFERENCES raw.ingestion_batch (batch_id),
    recorded_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_case_event_case ON ops.case_event (case_id, recorded_at);

CREATE FUNCTION ops.forbid_change() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'La tabla % es de solo agregar: no se puede %', TG_TABLE_NAME, TG_OP;
END;
$$;

CREATE TRIGGER case_event_append_only
    BEFORE UPDATE OR DELETE ON ops.case_event
    FOR EACH ROW EXECUTE FUNCTION ops.forbid_change();

-- 3) CIERRES — una fila = una versión del cierre de un día
CREATE TABLE ops.close_run (
    close_id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    business_date        DATE        NOT NULL,
    version              INTEGER     NOT NULL CHECK (version >= 1),
    received_cutoff      TIMESTAMPTZ NOT NULL,
    rules_version        TEXT        NOT NULL,
    status               TEXT        NOT NULL DEFAULT 'draft'
                         CHECK (status IN ('draft', 'incomplete', 'published', 'superseded')),
    missing_sources      TEXT[]      NOT NULL DEFAULT '{}',
    total_expected       NUMERIC(14,2),
    total_received       NUMERIC(14,2),
    total_pending        NUMERIC(14,2),
    supersedes_close_id  BIGINT      REFERENCES ops.close_run (close_id),
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    published_at         TIMESTAMPTZ,
    UNIQUE (business_date, version),
    CHECK ((status IN ('published', 'superseded')) = (published_at IS NOT NULL)),
    CHECK (status <> 'published' OR cardinality(missing_sources) = 0)
);

-- 4) RESULTADOS DEL CIERRE — una fila = la foto de una partida tal como estaba al publicar
CREATE TABLE ops.close_result (
    close_id         BIGINT        NOT NULL REFERENCES ops.close_run (close_id),
    item_type        TEXT          NOT NULL CHECK (item_type IN ('order', 'settlement', 'bank_entry')),
    item_key         TEXT          NOT NULL,
    item_status      TEXT          NOT NULL CHECK (item_status IN (
                         'reconciled', 'in_transit', 'overdue', 'mismatch', 'unmatched', 'in_review')),
    expected_amount  NUMERIC(14,2),
    received_amount  NUMERIC(14,2),
    case_id          BIGINT        REFERENCES ops.reconciliation_case (case_id),
    PRIMARY KEY (close_id, item_type, item_key)
);

COMMIT;