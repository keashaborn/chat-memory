BEGIN;

DO $$
BEGIN
  IF current_user <> 'sage' THEN
    RAISE EXCEPTION
      'memory V1 review/apply rollback must run as sage, current_user=%',
      current_user;
  END IF;
  IF EXISTS (SELECT 1 FROM memory.preference_apply_event LIMIT 1)
     OR EXISTS (SELECT 1 FROM memory.preference_revision_evidence LIMIT 1)
     OR EXISTS (SELECT 1 FROM memory.preference_revision LIMIT 1)
     OR EXISTS (SELECT 1 FROM memory.project_space_registration_event LIMIT 1)
     OR EXISTS (SELECT 1 FROM memory.project_knowledge_apply_event LIMIT 1)
     OR EXISTS (SELECT 1 FROM memory.user_preference LIMIT 1)
     OR EXISTS (SELECT 1 FROM memory.preference_candidate_review LIMIT 1)
     OR EXISTS (SELECT 1 FROM memory.project_knowledge_candidate_review LIMIT 1) THEN
    RAISE EXCEPTION
      'review/apply rollback refused: review, revision, event, or active preference rows exist';
  END IF;
END
$$;

DO $$
BEGIN
  IF to_regprocedure('memory.review_request_sha(jsonb)') IS NOT NULL THEN
    ALTER FUNCTION memory.review_request_sha(jsonb) OWNER TO sage;
  END IF;
  IF to_regprocedure('memory.guard_user_preference_controlled_write()') IS NOT NULL THEN
    ALTER FUNCTION memory.guard_user_preference_controlled_write() OWNER TO sage;
  END IF;
  IF to_regprocedure('memory.guard_preference_revision_insert()') IS NOT NULL THEN
    ALTER FUNCTION memory.guard_preference_revision_insert() OWNER TO sage;
  END IF;
  IF to_regprocedure('memory.register_project_space(uuid,text,text,jsonb)') IS NOT NULL THEN
    ALTER FUNCTION memory.register_project_space(uuid,text,text,jsonb) OWNER TO sage;
  END IF;
  IF to_regprocedure(
    'memory.review_preference_candidate(uuid,uuid,text,text,text,text,text,text[],uuid[],jsonb)'
  ) IS NOT NULL THEN
    ALTER FUNCTION memory.review_preference_candidate(
      uuid,uuid,text,text,text,text,text,text[],uuid[],jsonb
    ) OWNER TO sage;
  END IF;
  IF to_regprocedure(
    'memory.review_project_candidate(uuid,uuid,text,text,text,text,text,text[],uuid[],jsonb)'
  ) IS NOT NULL THEN
    ALTER FUNCTION memory.review_project_candidate(
      uuid,uuid,text,text,text,text,text,text[],uuid[],jsonb
    ) OWNER TO sage;
  END IF;
  IF to_regprocedure(
    'memory.apply_preference_candidate(uuid,uuid,uuid,jsonb)'
  ) IS NOT NULL THEN
    ALTER FUNCTION memory.apply_preference_candidate(uuid,uuid,uuid,jsonb)
      OWNER TO sage;
  END IF;
  IF to_regprocedure(
    'memory.apply_project_candidate(uuid,uuid,uuid,jsonb)'
  ) IS NOT NULL THEN
    ALTER FUNCTION memory.apply_project_candidate(uuid,uuid,uuid,jsonb)
      OWNER TO sage;
  END IF;
END
$$;

DROP TRIGGER IF EXISTS user_preference_controlled_write_guard
  ON memory.user_preference;
DROP TRIGGER IF EXISTS preference_revision_insert_guard
  ON memory.preference_revision;

DROP FUNCTION IF EXISTS memory.apply_project_candidate(uuid,uuid,uuid,jsonb);
DROP FUNCTION IF EXISTS memory.apply_preference_candidate(uuid,uuid,uuid,jsonb);
DROP FUNCTION IF EXISTS memory.review_project_candidate(
  uuid,uuid,text,text,text,text,text,text[],uuid[],jsonb
);
DROP FUNCTION IF EXISTS memory.review_preference_candidate(
  uuid,uuid,text,text,text,text,text,text[],uuid[],jsonb
);
DROP FUNCTION IF EXISTS memory.register_project_space(uuid,text,text,jsonb);
DROP FUNCTION IF EXISTS memory.guard_preference_revision_insert();
DROP FUNCTION IF EXISTS memory.guard_user_preference_controlled_write();
DROP FUNCTION IF EXISTS memory.review_request_sha(jsonb);

ALTER TABLE memory.user_preference
  DROP CONSTRAINT IF EXISTS user_preference_current_revision_fk,
  DROP CONSTRAINT IF EXISTS user_preference_accepted_review_fk,
  DROP CONSTRAINT IF EXISTS user_preference_reviewed_shape_ck;

DROP TABLE IF EXISTS memory.preference_apply_event;
DROP TABLE IF EXISTS memory.preference_revision_evidence;
DROP TABLE IF EXISTS memory.preference_revision;
DROP TABLE IF EXISTS memory.project_knowledge_apply_event;
DROP TABLE IF EXISTS memory.project_space_registration_event;

ALTER TABLE memory.user_preference
  DROP COLUMN IF EXISTS preference_class,
  DROP COLUMN IF EXISTS preference_domain,
  DROP COLUMN IF EXISTS polarity,
  DROP COLUMN IF EXISTS scope,
  DROP COLUMN IF EXISTS stability,
  DROP COLUMN IF EXISTS surface_policy,
  DROP COLUMN IF EXISTS sensitivity,
  DROP COLUMN IF EXISTS current_revision_id,
  DROP COLUMN IF EXISTS revision_number,
  DROP COLUMN IF EXISTS content_sha256,
  DROP COLUMN IF EXISTS accepted_review_id;

DROP INDEX IF EXISTS memory.preference_review_owner_request_uq;
DROP INDEX IF EXISTS memory.project_review_owner_request_uq;

ALTER TABLE memory.preference_candidate_review
  DROP CONSTRAINT IF EXISTS preference_review_request_sha_ck,
  DROP COLUMN IF EXISTS request_id,
  DROP COLUMN IF EXISTS request_sha256;

ALTER TABLE memory.project_knowledge_candidate_review
  DROP CONSTRAINT IF EXISTS project_review_request_sha_ck,
  DROP COLUMN IF EXISTS request_id,
  DROP COLUMN IF EXISTS request_sha256;

REVOKE INSERT ON
  memory.preference_candidate_review,
  memory.preference_candidate_review_replacement,
  memory.project_space,
  memory.project_knowledge_candidate_review,
  memory.project_knowledge_candidate_review_replacement,
  memory.project_knowledge_head,
  memory.project_knowledge_revision,
  memory.project_knowledge_revision_evidence
FROM memory_review_maintainer;

REVOKE INSERT, UPDATE ON memory.user_preference
  FROM memory_review_maintainer;

COMMIT;
