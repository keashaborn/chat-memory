BEGIN;

DO $block$
DECLARE
  table_name text;
  has_rows boolean;
BEGIN
  IF current_user <> 'sage' THEN
    RAISE EXCEPTION
      'memory V1 projection targets V5 rollback must run as sage, current_user=%',
      current_user;
  END IF;
  FOREACH table_name IN ARRAY ARRAY[
    'preference_head_v5',
    'preference_revision_v5',
    'project_knowledge_head_v5',
    'project_knowledge_revision_v5'
  ]
  LOOP
    IF to_regclass(format('memory.%I', table_name)) IS NOT NULL THEN
      EXECUTE format('SELECT EXISTS (SELECT 1 FROM memory.%I)', table_name)
        INTO STRICT has_rows;
      IF has_rows THEN
        RAISE EXCEPTION
          'refusing V5 target rollback: memory.% contains rows', table_name;
      END IF;
    END IF;
  END LOOP;
END
$block$;

ALTER TABLE IF EXISTS memory.preference_head_v5
  DROP CONSTRAINT IF EXISTS preference_head_v5_current_revision_fk;
ALTER TABLE IF EXISTS memory.project_knowledge_head_v5
  DROP CONSTRAINT IF EXISTS project_head_v5_current_revision_fk;

DROP TABLE IF EXISTS memory.project_knowledge_revision_v5;
DROP TABLE IF EXISTS memory.project_knowledge_head_v5;
DROP TABLE IF EXISTS memory.preference_revision_v5;
DROP TABLE IF EXISTS memory.preference_head_v5;

DROP FUNCTION IF EXISTS memory.guard_project_revision_temporal_v5();
DROP FUNCTION IF EXISTS memory.guard_project_head_update_v5();
DROP FUNCTION IF EXISTS memory.guard_preference_revision_insert_v5();
DROP FUNCTION IF EXISTS memory.guard_preference_head_update_v5();
DROP FUNCTION IF EXISTS memory.guard_v5_durable_actor();

COMMIT;
