-- 002: modelo normalizado (core)


-- 1) PEDIDOS — una fila = un pedido de la tienda
CREATE TABLE core.orders (
    order_id      TEXT        PRIMARY KEY,
    customer_id   TEXT        NOT NULL,
    order_status  TEXT        NOT NULL,
    purchased_at  TIMESTAMPTZ NOT NULL,
    src_batch_id  BIGINT      NOT NULL,
    src_row       INTEGER     NOT NULL,
    FOREIGN KEY (src_batch_id, src_row) REFERENCES raw.source_record (batch_id, row_number)
);

-- 2) ARTÍCULOS — una fila = una línea de un pedido
CREATE TABLE core.order_items (
    order_id      TEXT          NOT NULL REFERENCES core.orders (order_id),
    line_number   INTEGER       NOT NULL CHECK (line_number >= 1),
    product_id    TEXT          NOT NULL,
    price         NUMERIC(14,2) NOT NULL CHECK (price >= 0),
    freight       NUMERIC(14,2) NOT NULL CHECK (freight >= 0),
    src_batch_id  BIGINT        NOT NULL,
    src_row       INTEGER       NOT NULL,
    PRIMARY KEY (order_id, line_number),
    FOREIGN KEY (src_batch_id, src_row) REFERENCES raw.source_record (batch_id, row_number)
);

-- 3) EVENTOS DE LA PASARELA — una fila = un cobro o un reembolso
CREATE TABLE core.payment_events (
    provider           TEXT          NOT NULL,
    external_event_id  TEXT          NOT NULL,
    order_ref          TEXT          NOT NULL,
    event_type         TEXT          NOT NULL CHECK (event_type IN ('charge', 'refund')),
    payment_method     TEXT          NOT NULL
                       CHECK (payment_method IN ('credit_card', 'debit_card', 'boleto', 'voucher')),
    amount             NUMERIC(14,2) NOT NULL CHECK (amount > 0),
    currency           CHAR(3)       NOT NULL DEFAULT 'BRL' CHECK (currency = 'BRL'),
    occurred_at        TIMESTAMPTZ   NOT NULL,
    src_batch_id       BIGINT        NOT NULL,
    src_row            INTEGER       NOT NULL,
    PRIMARY KEY (provider, external_event_id),
    FOREIGN KEY (src_batch_id, src_row) REFERENCES raw.source_record (batch_id, row_number)
);
CREATE INDEX ix_payment_events_order_ref ON core.payment_events (order_ref);

-- 4) LIQUIDACIONES — una fila = un pago que la pasarela promete enviar al banco
CREATE TABLE core.settlements (
    provider            TEXT          NOT NULL,
    settlement_id       TEXT          NOT NULL,
    settled_at          TIMESTAMPTZ   NOT NULL,
    expected_payout_on  DATE          NOT NULL,
    reported_net        NUMERIC(14,2) NOT NULL,
    currency            CHAR(3)       NOT NULL DEFAULT 'BRL' CHECK (currency = 'BRL'),
    src_batch_id        BIGINT        NOT NULL,
    src_row             INTEGER       NOT NULL,
    PRIMARY KEY (provider, settlement_id),
    FOREIGN KEY (src_batch_id, src_row) REFERENCES raw.source_record (batch_id, row_number)
);

-- 5) LÍNEAS DE LIQUIDACIÓN — una fila = un componente del neto (cobro, reembolso o comisión)
CREATE TABLE core.settlement_lines (
    provider           TEXT          NOT NULL,
    settlement_id      TEXT          NOT NULL,
    line_number        INTEGER       NOT NULL CHECK (line_number >= 1),
    line_type          TEXT          NOT NULL CHECK (line_type IN ('charge', 'refund', 'fee')),
    external_event_id  TEXT,
    amount             NUMERIC(14,2) NOT NULL,
    src_batch_id       BIGINT        NOT NULL,
    src_row            INTEGER       NOT NULL,
    PRIMARY KEY (provider, settlement_id, line_number),
    FOREIGN KEY (provider, settlement_id)     REFERENCES core.settlements (provider, settlement_id),
    FOREIGN KEY (provider, external_event_id) REFERENCES core.payment_events (provider, external_event_id),
    FOREIGN KEY (src_batch_id, src_row)       REFERENCES raw.source_record (batch_id, row_number),
    -- el signo cuenta la historia: los cobros suman; los reembolsos y la comisión restan
    CHECK ((line_type = 'charge' AND amount > 0) OR (line_type IN ('refund', 'fee') AND amount < 0)),
    -- la comisión no viene de un evento; los cobros y reembolsos sí
    CHECK ((line_type = 'fee') = (external_event_id IS NULL)),
    -- un mismo evento no puede entrar dos veces en las liquidaciones
    UNIQUE (provider, external_event_id)
);

-- 6) MOVIMIENTOS DEL BANCO — una fila = una línea del extracto bancario
CREATE TABLE core.bank_entries (
    bank_account       TEXT          NOT NULL,
    external_entry_id  TEXT          NOT NULL,
    booked_on          DATE          NOT NULL,
    amount             NUMERIC(14,2) NOT NULL CHECK (amount <> 0),
    currency           CHAR(3)       NOT NULL DEFAULT 'BRL' CHECK (currency = 'BRL'),
    description        TEXT          NOT NULL,
    reference          TEXT,
    src_batch_id       BIGINT        NOT NULL,
    src_row            INTEGER       NOT NULL,
    PRIMARY KEY (bank_account, external_entry_id),
    FOREIGN KEY (src_batch_id, src_row) REFERENCES raw.source_record (batch_id, row_number)
);
CREATE INDEX ix_bank_entries_reference ON core.bank_entries (reference);

-- 7) VÍNCULO LIQUIDACIÓN ↔ DEPÓSITO — una fila = cuánto de un depósito cubre una liquidación
CREATE TABLE core.settlement_deposits (
    provider           TEXT          NOT NULL,
    settlement_id      TEXT          NOT NULL,
    bank_account       TEXT          NOT NULL,
    external_entry_id  TEXT          NOT NULL,
    allocated_amount   NUMERIC(14,2) NOT NULL CHECK (allocated_amount > 0),
    matched_by         TEXT          NOT NULL CHECK (matched_by IN ('reference', 'breakdown', 'manual')),
    matched_at         TIMESTAMPTZ   NOT NULL DEFAULT now(),
    PRIMARY KEY (provider, settlement_id, bank_account, external_entry_id),
    FOREIGN KEY (provider, settlement_id)          REFERENCES core.settlements (provider, settlement_id),
    FOREIGN KEY (bank_account, external_entry_id)  REFERENCES core.bank_entries (bank_account, external_entry_id)
);
CREATE INDEX ix_settlement_deposits_entry ON core.settlement_deposits (bank_account, external_entry_id);

