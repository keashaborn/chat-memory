BEGIN;

DO $$
DECLARE
  nonempty_tables text;
BEGIN
  SELECT string_agg(table_name, ', ' ORDER BY table_name)
  INTO nonempty_tables
  FROM (
    SELECT 'entity' AS table_name WHERE EXISTS (SELECT 1 FROM memory.entity)
    UNION ALL SELECT 'evidence' WHERE EXISTS (SELECT 1 FROM memory.evidence)
    UNION ALL SELECT 'claim' WHERE EXISTS (SELECT 1 FROM memory.claim)
    UNION ALL SELECT 'candidate' WHERE EXISTS (SELECT 1 FROM memory.candidate)
    UNION ALL SELECT 'user_preference' WHERE EXISTS (SELECT 1 FROM memory.user_preference)
    UNION ALL SELECT 'retrieval_trace' WHERE EXISTS (SELECT 1 FROM memory.retrieval_trace)
    UNION ALL SELECT 'projection_outbox' WHERE EXISTS (SELECT 1 FROM memory.projection_outbox)
  ) AS populated;

  IF nonempty_tables IS NOT NULL THEN
    RAISE EXCEPTION
      'REFUSING rollback: memory schema contains data in: %', nonempty_tables;
  END IF;
END
$$;

DROP SCHEMA IF EXISTS memory CASCADE;

COMMIT;
