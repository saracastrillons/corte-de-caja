-- 007: índice para encontrar las observaciones de un evento en raw
--
-- La ficha de un conflicto busca todas las veces que llegó un mismo evento leyendo el JSON
-- original (payload->>'event_id'). Sin índice, Postgres recorre TODAS las filas recibidas.
-- Un índice de expresión sobre exactamente esa expresión le permite ir directo a ellas.
-- Medición antes/después: python -m cortecaja.explain (ver docs/performance.md).

CREATE INDEX ix_source_record_event_id
    ON raw.source_record ((payload->>'event_id'));