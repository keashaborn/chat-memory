BEGIN;

DO $$
DECLARE
  nonempty_tables text;
BEGIN
  SELECT string_agg(table_name, ', ' ORDER BY table_name)
  INTO nonempty_tables
  FROM (
    SELECT 'artifact' AS table_name WHERE EXISTS (SELECT 1 FROM memory.artifact)
    UNION ALL SELECT 'artifact_occurrence' WHERE EXISTS (SELECT 1 FROM memory.artifact_occurrence)
    UNION ALL SELECT 'artifact_section' WHERE EXISTS (SELECT 1 FROM memory.artifact_section)
    UNION ALL SELECT 'artifact_endorsement' WHERE EXISTS (SELECT 1 FROM memory.artifact_endorsement)
    UNION ALL
    SELECT 'candidate artifact provenance'
    WHERE EXISTS (
      SELECT 1
      FROM memory.candidate
      WHERE source_artifact_id IS NOT NULL
         OR source_section_id IS NOT NULL
         OR source_char_start IS NOT NULL
         OR source_char_end IS NOT NULL
    )
  ) AS populated;

  IF nonempty_tables IS NOT NULL THEN
    RAISE EXCEPTION
      'REFUSING artifact rollback: memory schema contains data in: %', nonempty_tables;
  END IF;
END
$$;

DROP INDEX IF EXISTS memory.candidate_owner_artifact_section_idx;

ALTER TABLE memory.candidate
  DROP CONSTRAINT IF EXISTS candidate_artifact_section_fk,
  DROP CONSTRAINT IF EXISTS candidate_artifact_source_shape_ck,
  DROP COLUMN IF EXISTS source_artifact_id,
  DROP COLUMN IF EXISTS source_section_id,
  DROP COLUMN IF EXISTS source_char_start,
  DROP COLUMN IF EXISTS source_char_end;

DROP TABLE IF EXISTS memory.artifact_endorsement;
DROP TABLE IF EXISTS memory.artifact_occurrence;
DROP TABLE IF EXISTS memory.artifact_section;
DROP TABLE IF EXISTS memory.artifact;

COMMIT;
