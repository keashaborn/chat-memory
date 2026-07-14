BEGIN;

DO $$
DECLARE
  table_name text;
  row_exists boolean;
BEGIN
  IF current_user <> 'sage' THEN
    RAISE EXCEPTION
      'memory V1 preference/project staging rollback must run as sage, current_user=%',
      current_user;
  END IF;

  FOREACH table_name IN ARRAY ARRAY[
    'preference_candidate',
    'preference_candidate_evidence',
    'preference_candidate_review',
    'preference_candidate_review_replacement',
    'project_space',
    'project_knowledge_candidate',
    'project_knowledge_candidate_evidence',
    'project_knowledge_candidate_review',
    'project_knowledge_candidate_review_replacement',
    'project_knowledge_head',
    'project_knowledge_revision',
    'project_knowledge_revision_evidence',
    'project_knowledge_relation'
  ]
  LOOP
    IF to_regclass(format('memory.%I', table_name)) IS NOT NULL THEN
      EXECUTE format(
        'SELECT EXISTS (SELECT 1 FROM memory.%I LIMIT 1)', table_name
      ) INTO row_exists;
      IF row_exists THEN
        RAISE EXCEPTION 'refusing to drop nonempty memory.%', table_name;
      END IF;
    END IF;
  END LOOP;

  IF to_regclass('memory.evidence_lifecycle_event') IS NOT NULL THEN
    SELECT EXISTS (
      SELECT 1
      FROM memory.evidence_lifecycle_event
      WHERE preference_candidate_link_count <> 0
         OR project_candidate_link_count <> 0
         OR project_revision_link_count <> 0
    ) INTO row_exists;
    IF row_exists THEN
      RAISE EXCEPTION
        'refusing to remove specialized lifecycle counts from nonempty audit data';
    END IF;
  END IF;
END
$$;

DROP TRIGGER IF EXISTS evidence_lifecycle_specialized_counts
  ON memory.evidence_lifecycle_event;
DROP TRIGGER IF EXISTS preference_candidate_active_evidence_guard
  ON memory.preference_candidate_evidence;
DROP TRIGGER IF EXISTS project_candidate_active_evidence_guard
  ON memory.project_knowledge_candidate_evidence;
DROP TRIGGER IF EXISTS project_revision_active_evidence_guard
  ON memory.project_knowledge_revision_evidence;
DROP TRIGGER IF EXISTS preference_review_insert_guard
  ON memory.preference_candidate_review;
DROP TRIGGER IF EXISTS project_review_insert_guard
  ON memory.project_knowledge_candidate_review;
DROP TRIGGER IF EXISTS preference_review_replacement_insert_guard
  ON memory.preference_candidate_review_replacement;
DROP TRIGGER IF EXISTS project_review_replacement_insert_guard
  ON memory.project_knowledge_candidate_review_replacement;
DROP TRIGGER IF EXISTS project_revision_insert_guard
  ON memory.project_knowledge_revision;

DO $triggers$
DECLARE
  table_name text;
BEGIN
  FOREACH table_name IN ARRAY ARRAY[
    'preference_candidate',
    'preference_candidate_evidence',
    'preference_candidate_review',
    'preference_candidate_review_replacement',
    'project_space',
    'project_knowledge_candidate',
    'project_knowledge_candidate_evidence',
    'project_knowledge_candidate_review',
    'project_knowledge_candidate_review_replacement',
    'project_knowledge_head',
    'project_knowledge_revision',
    'project_knowledge_revision_evidence',
    'project_knowledge_relation'
  ]
  LOOP
    IF to_regclass(format('memory.%I', table_name)) IS NOT NULL THEN
      EXECUTE format(
        'DROP TRIGGER IF EXISTS specialized_append_only_guard ON memory.%I',
        table_name
      );
    END IF;
  END LOOP;
END
$triggers$;

DROP FUNCTION IF EXISTS memory.populate_specialized_evidence_link_counts();
DROP FUNCTION IF EXISTS memory.guard_specialized_active_evidence();
DROP FUNCTION IF EXISTS memory.guard_specialized_memory_append_only();
DROP FUNCTION IF EXISTS memory.guard_preference_review_insert();
DROP FUNCTION IF EXISTS memory.guard_project_review_insert();
DROP FUNCTION IF EXISTS memory.guard_preference_review_replacement_insert();
DROP FUNCTION IF EXISTS memory.guard_project_review_replacement_insert();
DROP FUNCTION IF EXISTS memory.guard_project_revision_insert();

REVOKE ALL PRIVILEGES ON
  memory.preference_candidate_evidence,
  memory.project_knowledge_candidate_evidence,
  memory.project_knowledge_revision_evidence
FROM memory_evidence_maintainer;

REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA memory
  FROM memory_review_maintainer;
REVOKE ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA memory
  FROM memory_review_maintainer;
REVOKE ALL PRIVILEGES ON SCHEMA memory FROM memory_review_maintainer;

DROP TABLE IF EXISTS memory.project_knowledge_relation;
DROP TABLE IF EXISTS memory.project_knowledge_revision_evidence;
DROP TABLE IF EXISTS memory.project_knowledge_revision;
DROP TABLE IF EXISTS memory.project_knowledge_head;
DROP TABLE IF EXISTS memory.project_knowledge_candidate_review_replacement;
DROP TABLE IF EXISTS memory.project_knowledge_candidate_review;
DROP TABLE IF EXISTS memory.project_knowledge_candidate_evidence;
DROP TABLE IF EXISTS memory.project_knowledge_candidate;
DROP TABLE IF EXISTS memory.project_space;
DROP TABLE IF EXISTS memory.preference_candidate_review_replacement;
DROP TABLE IF EXISTS memory.preference_candidate_review;
DROP TABLE IF EXISTS memory.preference_candidate_evidence;
DROP TABLE IF EXISTS memory.preference_candidate;

ALTER TABLE memory.evidence_lifecycle_event
  DROP CONSTRAINT IF EXISTS evidence_lifecycle_specialized_counts_ck,
  DROP COLUMN IF EXISTS preference_candidate_link_count,
  DROP COLUMN IF EXISTS project_candidate_link_count,
  DROP COLUMN IF EXISTS project_revision_link_count;

GRANT SELECT, INSERT, UPDATE, DELETE ON memory.user_preference TO brains_app;

DROP ROLE IF EXISTS memory_review_maintainer;

COMMIT;
