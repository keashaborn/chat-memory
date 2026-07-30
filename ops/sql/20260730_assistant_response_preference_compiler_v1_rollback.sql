BEGIN;

ALTER TABLE user_settings.assistant_response_preference_v1
  DROP CONSTRAINT IF EXISTS assistant_response_preference_active_candidate_fk,
  DROP CONSTRAINT IF EXISTS assistant_response_preference_active_compiler,
  DROP CONSTRAINT IF EXISTS assistant_response_preference_active_plan_sha,
  DROP CONSTRAINT IF EXISTS assistant_response_preference_compiled_marker,
  DROP CONSTRAINT IF EXISTS assistant_response_preference_narrative_length;

UPDATE user_settings.assistant_response_preference_v1
   SET custom_instructions=preference_narrative;

ALTER TABLE user_settings.assistant_response_preference_v1
  DROP COLUMN IF EXISTS active_compiled_at,
  DROP COLUMN IF EXISTS active_compiler_version,
  DROP COLUMN IF EXISTS active_compilation_plan_sha256,
  DROP COLUMN IF EXISTS active_compilation_rejections,
  DROP COLUMN IF EXISTS active_compilation_summary,
  DROP COLUMN IF EXISTS active_compilation_candidate_id,
  DROP COLUMN IF EXISTS preference_narrative;

DROP TABLE IF EXISTS
  user_settings.assistant_response_preference_compilation_candidate_v1;

COMMIT;
