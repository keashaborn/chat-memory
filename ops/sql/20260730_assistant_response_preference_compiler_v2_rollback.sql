BEGIN;

LOCK TABLE user_settings.assistant_response_preference_v1
  IN SHARE ROW EXCLUSIVE MODE;

LOCK TABLE user_settings.assistant_response_preference_compilation_candidate_v1
  IN SHARE ROW EXCLUSIVE MODE;

DO $$
BEGIN
  IF EXISTS (
    SELECT 1
      FROM user_settings.assistant_response_preference_v1
     WHERE char_length(preference_narrative) > 1200
  ) THEN
    RAISE EXCEPTION
      'rollback blocked: preference narratives longer than 1200 characters exist';
  END IF;

  IF EXISTS (
    SELECT 1
      FROM user_settings.assistant_response_preference_compilation_candidate_v1
     WHERE char_length(source_narrative) > 1200
        OR cardinality(rule_ids) > 6
        OR cardinality(summary) > 10
        OR compiler_version <> 'assistant_preference_compiler_v1'
        OR rule_ids && ARRAY[
          'contextual_playfulness',
          'precise_plain_language',
          'evidence_first_conclusions',
          'information_dense'
        ]::text[]
  ) THEN
    RAISE EXCEPTION
      'rollback blocked: v2 preference compilation candidates exist';
  END IF;

  IF EXISTS (
    SELECT 1
      FROM user_settings.assistant_response_preference_v1
     WHERE active_compiler_version IS NOT NULL
       AND active_compiler_version <> 'assistant_preference_compiler_v1'
  ) THEN
    RAISE EXCEPTION
      'rollback blocked: an active v2 preference compilation exists';
  END IF;
END
$$;

ALTER TABLE user_settings.assistant_response_preference_v1
  DROP CONSTRAINT IF EXISTS assistant_response_preference_narrative_length;

ALTER TABLE user_settings.assistant_response_preference_v1
  ADD CONSTRAINT assistant_response_preference_narrative_length
  CHECK (
    preference_narrative IS NULL
    OR char_length(preference_narrative) BETWEEN 1 AND 1200
  );

COMMENT ON COLUMN
  user_settings.assistant_response_preference_v1.preference_narrative IS
  'Owner-authored editable preference narrative, maximum 1200 characters. '
  'Never projected directly into an answer prompt.';

ALTER TABLE user_settings.assistant_response_preference_compilation_candidate_v1
  DROP CONSTRAINT IF EXISTS assistant_preference_candidate_narrative_v2,
  DROP CONSTRAINT IF EXISTS assistant_preference_candidate_rule_count_v2,
  DROP CONSTRAINT IF EXISTS assistant_preference_candidate_rule_ids_v2,
  DROP CONSTRAINT IF EXISTS assistant_preference_candidate_summary_count_v2,
  DROP CONSTRAINT IF EXISTS assistant_preference_candidate_compiler_version_v2,
  DROP CONSTRAINT IF EXISTS assistant_response_preference_compilatio_source_narrative_check,
  DROP CONSTRAINT IF EXISTS assistant_response_preference_compilation_candid_rule_ids_check,
  DROP CONSTRAINT IF EXISTS assistant_response_preference_compilation_candi_rule_ids_check1,
  DROP CONSTRAINT IF EXISTS assistant_response_preference_compilation_candida_summary_check,
  DROP CONSTRAINT IF EXISTS assistant_response_preference_compilatio_compiler_version_check;

ALTER TABLE user_settings.assistant_response_preference_compilation_candidate_v1
  ADD CONSTRAINT assistant_response_preference_compilatio_source_narrative_check
    CHECK (char_length(source_narrative) <= 1200),
  ADD CONSTRAINT assistant_response_preference_compilation_candid_rule_ids_check
    CHECK (cardinality(rule_ids) <= 6),
  ADD CONSTRAINT assistant_response_preference_compilation_candi_rule_ids_check1
    CHECK (
      rule_ids <@ ARRAY[
        'direct_answers_first',
        'restrained_reassurance',
        'evidence_based_challenge',
        'no_unsolicited_closing_offers',
        'minimal_paraphrase',
        'no_generic_praise',
        'practical_focus',
        'question_restraint',
        'candid_uncertainty'
      ]::text[]
    ),
  ADD CONSTRAINT assistant_response_preference_compilation_candida_summary_check
    CHECK (cardinality(summary) <= 10),
  ADD CONSTRAINT assistant_response_preference_compilatio_compiler_version_check
    CHECK (compiler_version = 'assistant_preference_compiler_v1');

ALTER TABLE user_settings.assistant_response_preference_v1
  DROP CONSTRAINT IF EXISTS assistant_response_preference_active_compiler_v2,
  DROP CONSTRAINT IF EXISTS assistant_response_preference_active_compiler;

ALTER TABLE user_settings.assistant_response_preference_v1
  ADD CONSTRAINT assistant_response_preference_active_compiler
    CHECK (
      active_compiler_version IS NULL
      OR active_compiler_version = 'assistant_preference_compiler_v1'
    );

COMMIT;
