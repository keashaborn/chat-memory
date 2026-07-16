BEGIN;

DO $block$
BEGIN
  IF current_user <> 'sage' THEN
    RAISE EXCEPTION 'self-role compatibility rollback requires sage';
  END IF;
  IF EXISTS (
    SELECT 1
      FROM memory.entity_mention
     WHERE mention_kind = 'self_reference'
       AND relationship_role IS NOT NULL
  ) THEN
    RAISE EXCEPTION 'rollback refuses stored canonical self roles';
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
     AND relationship_role IS NULL)
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
