-- 004: conciliación nivel 1 — pedido → pasarela
--
-- Pregunta: ¿lo que se cobró por cada pedido corresponde a lo que valía el pedido?
-- Regla de oro: agregar CADA lado a nivel de pedido ANTES de cruzarlos.
-- Parámetro p_cutoff: solo cuenta lo recibido hasta esa hora ("¿qué sabíamos entonces?").

CREATE SCHEMA recon;

CREATE FUNCTION recon.order_gateway(p_cutoff TIMESTAMPTZ DEFAULT 'infinity')
RETURNS TABLE (
    order_ref         TEXT,
    status            TEXT,
    expected_amount   NUMERIC(14,2),
    charged_amount    NUMERIC(14,2),
    difference        NUMERIC(14,2),
    refunded_amount   NUMERIC(14,2),
    charge_count      INTEGER,
    payment_methods   TEXT
)
LANGUAGE sql STABLE
AS $$
WITH known_batches AS (          -- los lotes que ya habían llegado al corte
    SELECT batch_id
    FROM raw.ingestion_batch
    WHERE status = 'loaded' AND received_at <= p_cutoff
),

-- LADO A — una fila por pedido: cuánto valía
order_totals AS (
    SELECT o.order_id,
           COALESCE(SUM(i.price + i.freight), 0.00) AS expected_amount
    FROM core.orders o
    JOIN known_batches kb ON kb.batch_id = o.src_batch_id
    LEFT JOIN core.order_items i
           ON i.order_id = o.order_id
          AND i.src_batch_id IN (SELECT batch_id FROM known_batches)
    GROUP BY o.order_id
),

-- Eventos conocidos al corte, con una marca de posible cobro duplicado:
-- mismo pedido, mismo importe, mismo medio, menos de 15 minutos después del anterior.
known_events AS (
    SELECT e.*,
           e.event_type = 'charge'
           AND e.occurred_at - LAG(e.occurred_at) OVER (
                   PARTITION BY e.order_ref, e.event_type, e.amount, e.payment_method
                   ORDER BY e.occurred_at
               ) < INTERVAL '15 minutes' AS looks_duplicated
    FROM core.payment_events e
    JOIN known_batches kb ON kb.batch_id = e.src_batch_id
),

-- LADO B — una fila por pedido: cuánto se cobró y se reembolsó
payment_totals AS (
    SELECT order_ref,
           COALESCE(SUM(amount) FILTER (WHERE event_type = 'charge'), 0.00) AS charged_amount,
           COALESCE(SUM(amount) FILTER (WHERE event_type = 'refund'), 0.00) AS refunded_amount,
           COUNT(*) FILTER (WHERE event_type = 'charge')                 AS charge_count,
           BOOL_OR(COALESCE(looks_duplicated, false))                    AS has_duplicate,
           STRING_AGG(DISTINCT payment_method, ', ' ORDER BY payment_method)
               FILTER (WHERE event_type = 'charge')                      AS payment_methods
    FROM known_events
    GROUP BY order_ref
),

-- CRUCE — ambos lados ya tienen una fila por pedido, así que nada se multiplica.
-- FULL OUTER JOIN conserva pedidos sin cobros y cobros sin pedido.
joined AS (
    SELECT COALESCE(o.order_id, p.order_ref)  AS order_ref,
           o.order_id IS NOT NULL             AS order_exists,
           o.expected_amount,
           COALESCE(p.charged_amount, 0.00)   AS charged_amount,
           COALESCE(p.refunded_amount, 0.00)  AS refunded_amount,
           COALESCE(p.charge_count, 0)::INT   AS charge_count,
           COALESCE(p.has_duplicate, false)   AS has_duplicate,
           p.payment_methods
    FROM order_totals o
    FULL OUTER JOIN payment_totals p ON p.order_ref = o.order_id
)

SELECT order_ref,
       -- Las reglas se evalúan en orden: la primera que se cumple gana.
       CASE
           WHEN NOT order_exists                   THEN 'charge_without_order'
           WHEN has_duplicate                      THEN 'duplicate_charge'
           WHEN charge_count = 0                   THEN 'no_payment'
           WHEN charged_amount < expected_amount   THEN 'partial_payment'
           WHEN charged_amount > expected_amount   THEN 'overcharged'
           ELSE 'matched'
       END                                         AS status,
       expected_amount,
       charged_amount,
       charged_amount - COALESCE(expected_amount, 0) AS difference,
       refunded_amount,
       charge_count,
       payment_methods
FROM joined
ORDER BY (charged_amount = COALESCE(expected_amount, 0) AND NOT has_duplicate AND order_exists),
         order_ref;
$$;