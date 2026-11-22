-- 006: reglas v2 de la conciliación pedido → pasarela, aprendidas con los datos reales de Olist
--
-- 1. Tolerancia de redondeo: 273 pedidos de Olist difieren en un solo centavo entre lo que
--    suman los artículos y lo que se pagó. No es un problema de negocio: con una tolerancia
--    explícita (p_tolerance, por defecto R$0,01) dejan de abrir casos.
-- 2. Los cupones no son cobros duplicados: 269 pedidos pagan con varios cupones del mismo valor
--    en el mismo minuto. La detección de duplicados ahora solo mira medios que mueven dinero.
--
-- Se reemplaza la función completa (la 004 no se edita: ya está aplicada en otras bases).

DROP FUNCTION recon.order_gateway(TIMESTAMPTZ);

CREATE FUNCTION recon.order_gateway(
    p_cutoff    TIMESTAMPTZ   DEFAULT 'infinity',
    p_tolerance NUMERIC(14,2) DEFAULT 0.01
)
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
WITH known_batches AS (
    SELECT batch_id
    FROM raw.ingestion_batch
    WHERE status = 'loaded' AND received_at <= p_cutoff
),

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

known_events AS (
    SELECT e.*,
           e.event_type = 'charge'
           AND e.payment_method <> 'voucher'                       -- v2: los cupones no cuentan
           AND e.occurred_at - LAG(e.occurred_at) OVER (
                   PARTITION BY e.order_ref, e.event_type, e.amount, e.payment_method
                   ORDER BY e.occurred_at
               ) < INTERVAL '15 minutes' AS looks_duplicated
    FROM core.payment_events e
    JOIN known_batches kb ON kb.batch_id = e.src_batch_id
),

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
),

classified AS (
    SELECT j.*,
           CASE
               WHEN NOT order_exists                                 THEN 'charge_without_order'
               WHEN has_duplicate                                    THEN 'duplicate_charge'
               WHEN charge_count = 0                                 THEN 'no_payment'
               WHEN charged_amount < expected_amount - p_tolerance   THEN 'partial_payment'   -- v2
               WHEN charged_amount > expected_amount + p_tolerance   THEN 'overcharged'       -- v2
               ELSE 'matched'
           END AS status
    FROM joined j
)

SELECT order_ref,
       status,
       expected_amount,
       charged_amount,
       charged_amount - COALESCE(expected_amount, 0) AS difference,
       refunded_amount,
       charge_count,
       payment_methods
FROM classified
ORDER BY status = 'matched', order_ref;
$$;