BEGIN;

DO $block$
BEGIN
  IF current_user <> 'sage' THEN
    RAISE EXCEPTION 'self-role compatibility migration requires sage';
  END IF;
  IF to_regclass('memory.entity_mention') IS NULL THEN
    RAISE EXCEPTION 'memory.entity_mention prerequisite is missing';
  END IF;
END
$block$;

ALTER TABLE memory.entity_mention
  DROP CONSTRAINT IF EXISTS entity_mention_check;

ALTER TABLE memory.entity_mention
  ADD CONSTRAINT entity_mention_check CHECK (
    (mention_kind = 'self_reference'
     AND entity_type = 'self'
     AND name_text IS NULL
     AND (relationship_role IS NULL OR relationship_role = 'user:self'))
    OR
    (mention_kind = 'named'
     AND name_text IS NOT NULL AND btrim(name_text) <> ''
     AND length(name_text) <= 500)
    OR
    (mention_kind = 'role_only'
     AND name_text IS NULL AND relationship_role IS NOT NULL
     AND btrim(relationship_role) <> '' AND length(relationship_role) <= 500)
    OR
    (mention_kind = 'anonymous' AND name_text IS NULL)
  ) NOT VALID;

ALTER TABLE memory.entity_mention
  VALIDATE CONSTRAINT entity_mention_check;

COMMIT;
