BEGIN;

LOCK TABLE user_settings.assistant_response_preference_v1
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

COMMIT;
