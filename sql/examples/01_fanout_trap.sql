-- La trampa del JOIN que multiplica dinero, con el pedido ORD-1001
-- (3 artículos que suman R$500 y 2 pagos: tarjeta R$450 + cupón R$50).

\echo '--- MAL: unir articulos y pagos directamente por pedido ---'
SELECT i.order_id,
       COUNT(*)                   AS filas_del_join,
       SUM(i.price + i.freight)   AS esperado,
       SUM(e.amount)              AS cobrado
FROM core.order_items i
JOIN core.payment_events e
  ON e.order_ref = i.order_id AND e.event_type = 'charge'
WHERE i.order_id = 'ORD-1001'
GROUP BY i.order_id;

\echo '--- BIEN: agregar cada lado por pedido y luego cruzar ---'
WITH articulos AS (
    SELECT order_id, SUM(price + freight) AS esperado
    FROM core.order_items
    WHERE order_id = 'ORD-1001'
    GROUP BY order_id
),
pagos AS (
    SELECT order_ref, SUM(amount) AS cobrado
    FROM core.payment_events
    WHERE order_ref = 'ORD-1001' AND event_type = 'charge'
    GROUP BY order_ref
)
SELECT a.order_id, a.esperado, p.cobrado
FROM articulos a
JOIN pagos p ON p.order_ref = a.order_id;