BEGIN;

LOCK TABLE user_settings.assistant_response_preference_v1
  IN SHARE ROW EXCLUSIVE MODE;

ALTER TABLE user_settings.assistant_response_preference_v1
  DROP CONSTRAINT IF EXISTS assistant_response_preference_narrative_length;

ALTER TABLE user_settings.assistant_response_preference_v1
  ADD CONSTRAINT assistant_response_preference_narrative_length
  CHECK (
    preference_narrative IS NULL
    OR char_length(preference_narrative) BETWEEN 1 AND 8000
  );

COMMENT ON COLUMN
  user_settings.assistant_response_preference_v1.preference_narrative IS
  'Owner-authored editable preference narrative, maximum 8000 characters. '
  'Never projected directly into an answer prompt; only an approved typed '
  'compilation may affect response presentation.';

COMMIT;
