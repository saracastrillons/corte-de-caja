-- 005: conciliación nivel 2 — pasarela → banco
--
-- Pregunta: ¿llegó al banco el neto de cada liquidación?
-- Paso A: emparejar depósitos con liquidaciones por referencia (escribe vínculos confirmados).
-- Paso B: clasificar cada liquidación y cada depósito sin pareja, al corte que se pida.


-- PASO A — Emparejamiento por referencia.
-- Una referencia confiable identifica la pareja aunque el monto no coincida.
-- Es idempotente: correrlo dos veces no crea vínculos repetidos.
CREATE FUNCTION recon.match_by_reference()
RETURNS INTEGER
LANGUAGE sql
AS $$
WITH inserted AS (
    INSERT INTO core.settlement_deposits
        (provider, settlement_id, bank_account, external_entry_id, allocated_amount, matched_by)
    SELECT s.provider, s.settlement_id, b.bank_account, b.external_entry_id, b.amount, 'reference'
    FROM core.bank_entries b
    JOIN core.settlements s ON s.settlement_id = b.reference
    WHERE b.amount > 0
      AND NOT EXISTS (                      -- el depósito todavía no tiene pareja
          SELECT 1 FROM core.settlement_deposits d
          WHERE d.bank_account = b.bank_account
            AND d.external_entry_id = b.external_entry_id)
    ON CONFLICT DO NOTHING
    RETURNING 1
)
SELECT COUNT(*)::INT FROM inserted;
$$;


-- PASO B — Clasificación al corte.
-- p_business_date: el día que se está cerrando.
-- p_cutoff: solo cuenta lo recibido hasta esa hora.
CREATE FUNCTION recon.gateway_bank(p_business_date DATE, p_cutoff TIMESTAMPTZ DEFAULT 'infinity')
RETURNS TABLE (
    item_type        TEXT,
    item_key         TEXT,
    status           TEXT,
    expected_amount  NUMERIC(14,2),
    received_amount  NUMERIC(14,2),
    difference       NUMERIC(14,2),
    due_on           DATE,
    days_late        INTEGER,
    evidence         TEXT
)
LANGUAGE sql STABLE
AS $$
WITH known_batches AS (
    SELECT batch_id
    FROM raw.ingestion_batch
    WHERE status = 'loaded' AND received_at <= p_cutoff
),

known_settlements AS (
    SELECT s.*
    FROM core.settlements s
    JOIN known_batches kb ON kb.batch_id = s.src_batch_id
),

known_deposits AS (                       -- abonos del extracto ya recibidos y contabilizados
    SELECT b.*
    FROM core.bank_entries b
    JOIN known_batches kb ON kb.batch_id = b.src_batch_id
    WHERE b.amount > 0 AND b.booked_on <= p_business_date
),

known_links AS (                          -- un vínculo cuenta si sus DOS lados ya se conocían
    SELECT d.*
    FROM core.settlement_deposits d
    JOIN known_settlements s ON s.provider = d.provider AND s.settlement_id = d.settlement_id
    JOIN known_deposits    b ON b.bank_account = d.bank_account
                            AND b.external_entry_id = d.external_entry_id
),

-- Depósitos sin pareja que podrían corresponder a una liquidación por monto y fecha.
-- Son solo SUGERENCIAS: nunca se concilian solos.
candidates AS (
    SELECT s.provider, s.settlement_id, b.bank_account, b.external_entry_id
    FROM known_settlements s
    JOIN known_deposits b
      ON b.amount = s.reported_net
     AND b.reference IS NULL
     AND b.booked_on BETWEEN s.expected_payout_on - 1 AND s.expected_payout_on + 3
    WHERE NOT EXISTS (SELECT 1 FROM known_links l
                      WHERE l.provider = s.provider AND l.settlement_id = s.settlement_id)
      AND NOT EXISTS (SELECT 1 FROM known_links l
                      WHERE l.bank_account = b.bank_account
                        AND l.external_entry_id = b.external_entry_id)
),

-- NIVEL LIQUIDACIÓN — una fila por liquidación
settlement_side AS (
    SELECT s.provider || '/' || s.settlement_id                     AS item_key,
           s.reported_net                                           AS expected_amount,
           COALESCE(l.received, 0.00)                               AS received_amount,
           s.expected_payout_on,
           l.deposits,
           c.candidate_count,
           c.candidate_list
    FROM known_settlements s
    LEFT JOIN (SELECT provider, settlement_id,
                      SUM(allocated_amount) AS received,
                      STRING_AGG(external_entry_id, ', ' ORDER BY external_entry_id) AS deposits
               FROM known_links
               GROUP BY provider, settlement_id) l
           ON l.provider = s.provider AND l.settlement_id = s.settlement_id
    LEFT JOIN (SELECT provider, settlement_id,
                      COUNT(*) AS candidate_count,
                      STRING_AGG(external_entry_id, ', ' ORDER BY external_entry_id) AS candidate_list
               FROM candidates
               GROUP BY provider, settlement_id) c
           ON c.provider = s.provider AND c.settlement_id = s.settlement_id
),

-- NIVEL DEPÓSITO — una fila por depósito que no quedó vinculado a ninguna liquidación
deposit_side AS (
    SELECT b.bank_account || '/' || b.external_entry_id             AS item_key,
           b.amount                                                 AS received_amount,
           b.booked_on,
           b.reference,
           b.description,
           EXISTS (SELECT 1 FROM candidates c
                   WHERE c.bank_account = b.bank_account
                     AND c.external_entry_id = b.external_entry_id) AS is_candidate
    FROM known_deposits b
    WHERE NOT EXISTS (SELECT 1 FROM known_links l
                      WHERE l.bank_account = b.bank_account
                        AND l.external_entry_id = b.external_entry_id)
),

-- Cada lado se clasifica a su propio nivel y luego se apilan en un solo reporte.
report AS (
    SELECT 'settlement'                                 AS item_type,
           item_key,
           CASE                                         -- prioridad explícita, de arriba abajo
               WHEN received_amount = expected_amount     THEN 'reconciled'
               WHEN received_amount > 0                   THEN 'mismatch'
               WHEN candidate_count >= 2                  THEN 'ambiguous'
               WHEN candidate_count = 1                   THEN 'suggested'
               WHEN expected_payout_on >= p_business_date THEN 'in_transit'
               ELSE 'overdue'
           END                                          AS status,
           expected_amount,
           received_amount,
           received_amount - expected_amount            AS difference,
           expected_payout_on                           AS due_on,
           CASE WHEN received_amount <> expected_amount
                THEN GREATEST(p_business_date - expected_payout_on, 0)
           END                                          AS days_late,
           CASE
               WHEN deposits IS NOT NULL       THEN 'Depósito: ' || deposits
               WHEN candidate_list IS NOT NULL THEN 'Posibles depósitos: ' || candidate_list
               ELSE 'Sin depósito identificado'
           END                                          AS evidence
    FROM settlement_side

    UNION ALL

    SELECT 'bank_entry',
           item_key,
           CASE WHEN is_candidate THEN 'ambiguous' ELSE 'unmatched' END,
           NULL,
           received_amount,
           received_amount,
           booked_on,
           NULL,
           description || COALESCE(' · ref ' || reference, ' · sin referencia')
    FROM deposit_side
)

SELECT *
FROM report
ORDER BY status = 'reconciled', item_type DESC, item_key;
$$;